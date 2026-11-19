"""Stage 7：FFmpeg 成片渲染（画面合成 + 字幕烧录 + BGM 闪避 + 混音）。

输入是 Stage 6 的唯一产物 ``06_plan/timeline.json``（:class:`RenderTimeline`），
输出两份成片（规范 §4.3）::

    episodes/ep_XXX/07_out/{episode_id}.mp4
    exports/EP{话次:03d}_{title}.mp4   # Patch v2.5-3：可读文件名

画面方案（规范 §2 / §4.2）：**原图高斯模糊铺底 + 顶层居中保留完整分镜运镜**。
即每张面板图先放大裁切铺满 1080×1920 做高斯模糊与压暗当背景，
原图等比缩放（不裁内容）后居中叠在背景上，再叠加缓慢运镜（zoompan）。

**为什么分两次 ffmpeg 调用**（本模块最关键的一条取舍）：

``sidechaincompress`` 的两个输入是 *request pad*，**不能同时取自 filtergraph 内部的
label**——一旦两边都是图内 label，ffmpeg 会把 label 当成 *stream specifier* 去输入文件里
找，报 ``Stream specifier 'voice' ... matches no streams`` /
``Error binding filtergraph inputs/outputs: Invalid argument``。
因此人声轨必须先落盘成临时 wav，再作为**输入文件流**参与闪避：

* 调用 A「备音」：按时间轴拼人声 → ``voice.wav``；同时把 BGM 规范化并降音量 → ``bgm.wav``；
* 调用 B「成片」：画面（场景 concat + 字幕 overlay）+ BGM 闪避 + 混音 → mp4。

**为什么字幕走 PNG overlay 而不是 ``subtitles`` 滤镜**：本机 ffmpeg 编译时缺
``--enable-libass`` / ``--enable-libfreetype``，``subtitles`` / ``ass`` / ``drawtext``
滤镜全部不可用。Stage 6 因此额外产出每条字幕一张 1080×1920 的 RGBA 透明 PNG
（``06_plan/subtitles/{index:04d}.png``），本模块用 ``overlay`` + ``enable='between(t,…)'``
把它们按时间窗烧录进画面；``subtitles.ass`` 仍作为「外挂字幕交付物」保留。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.branding_render import (
    load_episode_render_options,
    render_cover_image,
    render_outro_image,
    resolve_effective_branding,
)
from core.config import AppConfig, SeriesConfig, assets_root, load_series_config
from core.schemas import (
    EpisodeStoryboard,
    RenderTimeline,
    SfxEvent,
    StoryboardScene,
    SubtitleLine,
    TimelineScene,
    load_model,
)
from stages.s3b_scenes import load_storyboard, storyboard_path_for

#: Stage 7 输出目录名（对应 ``core.manifest.STAGE_DIR_NAMES["s7_renderer"]``）。
OUT_DIR_NAME = "07_out"
#: 作品级成片导出目录名。
EXPORTS_DIR_NAME = "exports"
#: Stage 6 计划目录名。
PLAN_DIR_NAME = "06_plan"
#: Stage 6 字幕 PNG 子目录名。
SUBTITLE_DIR_NAME = "subtitles"
#: Stage 5 音频目录名。
AUDIO_DIR_NAME = "05_audio"
#: Stage 2 面板目录名。
PANEL_DIR_NAME = "02_panels"
#: 时间轴文件名。
TIMELINE_FILENAME = "timeline.json"

#: 锁定的成片画幅（铁律：9:16 / 1080×1920 / 30fps）。
DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_FPS = 30

# --------------------------------------------------------------------------
# Patch v3.0-8：抖音 9:16 安全分区版式（100% 杜绝字幕压脸与气泡遮挡）
# --------------------------------------------------------------------------
#: 主画面安全框尺寸：等比缩放进 1020×1220 的包围盒，绝不裁切内容。
SAFE_WIDTH = 1020
SAFE_BOX_HEIGHT = 1220
#: 安全框上沿（固定 y=180）；安全区纵向带总高 1260（y=180~1440）。
SAFE_TOP = 180
SAFE_ZONE_HEIGHT = 1260
#: 主画面下沿硬上限：overlay 纵向终点必须 <= 1440（实测最大 1420）。
SAFE_BOTTOM_LIMIT = 1440
#: Patch v3.0-8：高宽比超过该阈值视为「超长竖格」，pan_down 改为宽度铺满 + 视窗内扫底。
TALL_PANEL_RATIO = 1.5
#: 背景固定质感滤镜（规范明文）：同图放大铺满后高斯模糊 + 暗化降饱和，
#: 营造沉浸暗调，彻底消除黑/白边生硬感。
BACKGROUND_BLUR_FILTER = "boxblur=30:4"
BACKGROUND_EQ_FILTER = "eq=brightness=-0.38:saturation=0.75"

#: 内部音频统一参数：sidechaincompress 要求主/侧链采样率一致，所以先统一再闪避。
_AUDIO_RATE = 44100
_AUDIO_LAYOUT = "stereo"

#: ``amix`` 单次混合的最大输入数（超过就分组再混，避免滤镜 pad 上限）。
_MIX_GROUP_SIZE = 16

#: BGM 闪避（``sidechaincompress``）参数（Patch v4.0 Patch 18：更晚、更缓地退让）。
#:
#: **threshold=0.06 / ratio=2.0**：Patch 18 起 BGM 抬到 -8.0dB，基线更高，
#: 闪避阈值跟着抬一点、压缩比降下来，说话时 BGM 只温和退让，配乐不会忽大忽小。
#:
#: **attack=30ms / release=350ms**：30ms 起压避免音节开头的泵感；350ms 释放让句间停顿
#: （250~400ms）结束时 BGM 自然托底，长句尾音里也不会频繁起伏。
_SIDECHAIN_THRESHOLD = 0.06
_SIDECHAIN_RATIO = 2.0
_SIDECHAIN_ATTACK_MS = 30
_SIDECHAIN_RELEASE_MS = 350

#: 错误信息里保留的 stderr 末尾长度（够定位问题，又不会把整份 ffmpeg 日志塞进异常）。
_STDERR_TAIL_CHARS = 2000

# --------------------------------------------------------------------------
# Patch v3.1 Patch 11 B：零版权素材（默认 BGM / 音效）
# --------------------------------------------------------------------------
#: 同名素材的解析优先级：压缩率高的格式优先（``.mp3`` > ``.m4a`` > ``.wav`` > ``.ogg``）。
ASSET_SUFFIX_PRIORITY: tuple[str, ...] = (".mp3", ".m4a", ".wav", ".ogg")

#: 素材根目录名（相对 ``backend/``）。
ASSETS_DIR_NAME = "assets"
#: 氛围音乐子目录名。
BGM_DIR_NAME = "bgm"
#: 音效子目录名。
SFX_DIR_NAME = "sfx"

#: 音效默认增益（dB）。Patch v3.2 Patch 14 从 -10dB 提升到 -3.5dB：
#: 旧值混入后心跳/甩门等音效几乎听不见；混入前另做 ``dynaudnorm`` 峰值归一防破音。
#: Patch v4.0 Patch 18 再抬到 -2.0dB：扇耳光 / 摔碎这类关键音效要听得见。
DEFAULT_SFX_GAIN_DB = -2.0

#: ``SfxEvent.gain_db`` 的 schema 默认值（``core/schemas.py`` 不在 Patch 14 的允许修改
#: 清单内，不能直接改它）。事件增益等于该值视为「未显式指定」，沿用渲染配置的
#: ``sfx_gain_db``；只有显式写成别的值才覆盖配置——否则 schema 默认的 -10.0 会让
#: 配置里的新默认值 -2.0dB 永远不生效。
_SFX_EVENT_SCHEMA_DEFAULT_GAIN = float(SfxEvent.model_fields["gain_db"].default)

#: 文件名里不允许出现的字符（Windows/macOS 通用安全集）。
_UNSAFE_NAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


class RenderError(RuntimeError):
    """Stage 7 渲染失败（ffmpeg 调用失败、素材缺失、时间轴非法等）。"""


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
def _episode_dir(series_dir: str | Path, episode_no: int) -> Path:
    """单话根目录：``{series_dir}/episodes/ep_{no:03d}``。"""
    return Path(series_dir) / "episodes" / f"ep_{int(episode_no):03d}"


def out_dir_for(series_dir: str | Path, episode_no: int) -> Path:
    """Stage 7 输出目录：``episodes/ep_XXX/07_out``。"""
    return _episode_dir(series_dir, episode_no) / OUT_DIR_NAME


def exports_dir_for(series_dir: str | Path) -> Path:
    """作品级导出目录：``{series_dir}/exports``。"""
    return Path(series_dir) / EXPORTS_DIR_NAME


def plan_dir_for(series_dir: str | Path, episode_no: int) -> Path:
    """Stage 6 计划目录：``episodes/ep_XXX/06_plan``。"""
    return _episode_dir(series_dir, episode_no) / PLAN_DIR_NAME


def _timeline_path(series_dir: str | Path, episode_no: int) -> Path:
    """时间轴文件路径：``episodes/ep_XXX/06_plan/timeline.json``。"""
    return plan_dir_for(series_dir, episode_no) / TIMELINE_FILENAME


def _safe_filename(name: str) -> str:
    """把标题清洗成可安全用作文件名的字符串。"""
    cleaned = _UNSAFE_NAME_CHARS.sub("_", str(name or "").strip())
    return cleaned.strip(". ") or "untitled"


# --------------------------------------------------------------------------
# ffmpeg 调用
# --------------------------------------------------------------------------
def _tail(text: str | None, limit: int = _STDERR_TAIL_CHARS) -> str:
    """取日志末尾若干字符（异常里只放尾部，够定位又不淹没 traceback）。"""
    value = (text or "").strip()
    if len(value) > limit:
        value = "…" + value[-limit:]
    return value


def run_ffmpeg(args: list[str], *, ffmpeg_bin: str = "ffmpeg", timeout_s: int = 3600) -> str:
    """同步执行 ffmpeg，返回 stdout。

    **为什么捕获 stderr 而不是直接抛**：ffmpeg 的进度与错误全在 stderr，
    只有失败时才需要把它并进异常；正常路径不制造噪声。

    失败时抛 :class:`RenderError`，异常里保留 stderr **末尾**若干字符——
    ffmpeg 的报错信息几乎总在日志尾部（前面是上百行滤镜参数回显）。

    **为什么默认超时是 3600s**：长话（74 场景 / 296 秒成片）渲染本身就要十几分钟，
    900s 会在渲染途中被砍断，留下一个没有 moov 的残缺 mp4。
    """
    command = [str(ffmpeg_bin), *[str(a) for a in args]]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RenderError(f"找不到 ffmpeg 可执行文件：{ffmpeg_bin}") from exc
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else exc.stderr
        raise RenderError(f"ffmpeg 执行超时（{timeout_s}s）：{_tail(stderr)}") from exc

    if completed.returncode != 0:
        raise RenderError(
            f"ffmpeg 执行失败（退出码 {completed.returncode}）：\n{_tail(completed.stderr)}"
        )
    return completed.stdout


def _ffprobe_bin(ffmpeg_bin: str = "ffmpeg") -> str | None:
    """找 ffprobe：优先与 ffmpeg 同目录，其次 PATH。"""
    candidate = Path(str(ffmpeg_bin)).parent / "ffprobe"
    if str(ffmpeg_bin) != "ffmpeg" and candidate.is_file():
        return str(candidate)
    found = shutil.which("ffprobe")
    return found


def probe_duration_ms(path: Path, *, ffmpeg_bin: str = "ffmpeg") -> int:
    """读取媒体时长（毫秒）。

    优先用 ``ffprobe``（准确）；没有 ffprobe 时退回解析 ``ffmpeg -i`` 的 stderr。
    """
    target = Path(path)
    if not target.exists():
        raise RenderError(f"待探测的文件不存在：{target}")

    ffprobe = _ffprobe_bin(ffmpeg_bin)
    if ffprobe:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            try:
                data = json.loads(completed.stdout or "{}")
                duration = float((data.get("format") or {}).get("duration", 0.0))
            except (ValueError, TypeError, json.JSONDecodeError):
                duration = 0.0
            if duration > 0:
                return int(round(duration * 1000))

    # 兜底：ffmpeg -i 会把时长打在 stderr 的 "Duration:" 行上。
    completed = subprocess.run(
        [str(ffmpeg_bin), "-i", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", completed.stderr or "")
    if match:
        hours, minutes, seconds = match.groups()
        total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        return int(round(total * 1000))
    raise RenderError(f"无法读取媒体时长：{target}")


# --------------------------------------------------------------------------
# 素材路径解析
# --------------------------------------------------------------------------
def _resolve_image_path(image_path: str, episode_dir: Path) -> Path | None:
    """解析面板图片路径：绝对路径直接用，相对路径按 ``episode_dir`` 解析。

    兼容两种相对写法：相对单话根目录（``02_panels/panel_001.png``）
    与只给文件名（``panel_001.png``，再回落到 ``02_panels/``）。
    """
    raw = str(image_path or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    for base in (episode_dir, episode_dir / PANEL_DIR_NAME):
        resolved = base / candidate
        if resolved.is_file():
            return resolved
    return None


def _resolve_audio_path(audio_path: str, episode_dir: Path) -> Path | None:
    """解析音频路径：绝对路径直接用，相对路径按 ``05_audio/`` 解析。"""
    raw = str(audio_path or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    audio_dir = episode_dir / AUDIO_DIR_NAME
    for base in (audio_dir, episode_dir):
        resolved = base / candidate
        if resolved.is_file():
            return resolved
    return None


def _subtitle_png_path(episode_dir: Path, index: int) -> Path | None:
    """字幕 PNG 路径：``06_plan/subtitles/{index:04d}.png``（Stage 6 的命名约定）。"""
    subtitle_dir = episode_dir / PLAN_DIR_NAME / SUBTITLE_DIR_NAME
    for name in (f"{int(index):04d}.png", f"{int(index)}.png"):
        candidate = subtitle_dir / name
        if candidate.is_file():
            return candidate
    return None


def _read_image_size(path: Path) -> tuple[int, int] | None:
    """读图片 ``(宽, 高)``；PIL 不可用或文件损坏时返回 ``None``（调用方走通用链路）。"""
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
        return int(width), int(height)
    except Exception:
        return None


# --------------------------------------------------------------------------
# 画面滤镜
# --------------------------------------------------------------------------
def _motion_of(scene: TimelineScene) -> str:
    """取运镜名（小写字符串；枚举与裸字符串都兼容）。"""
    raw = getattr(scene, "motion", "static")
    value = getattr(raw, "value", raw)
    return str(value or "static").strip().lower() or "static"


def _motion_expressions(motion: str, frames: int) -> tuple[str, str, str]:
    """返回 ``zoompan`` 的 ``z`` / ``x`` / ``y`` 表达式。

    **为什么 x/y 要除以 zoom**：zoompan 的 x/y 是「从放大后的输入里取样的左上角坐标」，
    取 ``(iw-iw/zoom)/2`` 才是放大后的**居中**取样，否则画面会偏向左上角。
    ``pan_*`` 与 ``shake`` 额外放大到 1.12 作为运镜余量，
    否则位移/抖动会把画布边缘拉出空白。
    """
    center_x = "(iw-iw/zoom)/2"
    center_y = "(ih-ih/zoom)/2"
    span = max(1, int(frames))
    if motion == "zoom_in":
        return ("min(zoom+0.0008,1.12)", center_x, center_y)
    if motion == "zoom_out":
        return ("if(lte(zoom,1.0),1.12,max(1.001,zoom-0.0008))", center_x, center_y)
    if motion == "pan_down":
        # 镜头向下移动：取样点从画幅顶端移到底端。
        return ("1.12", center_x, f"(ih-ih/zoom)*(on/{span})")
    if motion == "pan_up":
        # 镜头向上移动：取样点从画幅底端移回顶端。
        return ("1.12", center_x, f"(ih-ih/zoom)*(1-on/{span})")
    if motion == "shake":
        return ("1.12", f"{center_x}+sin(on*1.7)*6", f"{center_y}+cos(on*2.3)*6")
    return ("1.0", center_x, center_y)


def _focus_band_crop(scene: TimelineScene) -> str:
    """按 ``focus_hint`` 取景带生成 ``crop`` 滤镜（Patch v2.6-5）。

    只有 ``focus_band=True`` 的场景才裁切；其余场景保持「原图完整居中未裁切」，
    这是实机验收确认过的构图，不能因为加了对焦就破坏它。
    """
    if not bool(getattr(scene, "focus_band", False)):
        return ""
    region = getattr(scene, "focus_region", None)
    if not region or len(region) != 4:
        return ""
    x, y, w, h = (float(value) for value in region)
    if w <= 0.01 or h <= 0.01:
        return ""
    x = min(max(x, 0.0), 0.99)
    y = min(max(y, 0.0), 0.99)
    w = min(max(w, 0.01), 1.0 - x)
    h = min(max(h, 0.01), 1.0 - y)
    return (
        f"crop=w='iw*{w:.4f}':h='ih*{h:.4f}':x='iw*{x:.4f}':y='ih*{y:.4f}'"
    )


def build_scene_filter(
    scene: TimelineScene,
    timeline: RenderTimeline,
    *,
    work_dir: Path,
    fps: int = DEFAULT_FPS,
    background_blur: int = 40,
    background_darken: float = 0.55,
    background_index: int | None = None,
    foreground_index: int | None = None,
    source_size: tuple[int, int] | None = None,
) -> str:
    """生成单个场景的 ``filter_complex`` 片段。

    片段结构（三条链，Patch v3.0-8 抖音安全分区版式）：

    1. **背景链**：``scale=…:force_original_aspect_ratio=increase`` 放大裁切铺满
       1080×1920，再 ``boxblur=30:4`` 模糊 + ``eq=brightness=-0.38:saturation=0.75``
       暗化降饱和——这一层只负责氛围，不承载信息；
    2. **前景链（主画面安全区）**：等比缩放进 ``1020×1220`` 安全框（**不裁内容**），
       ``format=yuva420p`` + ``pad=…:color=black@0.0`` 补透明边到安全框大小，
       再用 ``zoompan`` 做运镜；运镜严格在安全框内完成，输出尺寸即 1020×1220；
    3. **合成链**：``overlay=x=(W-w)/2:y=180+(1260-h)/2`` 把前景叠到背景的
       居中偏上安全区，下沿最高只到 y=1420，**绝不越过 y=1440**。

    **``background_index`` 给了就用预渲染背景**：背景是静态图，模糊只需算一次；
    放在帧循环里逐帧重算 1080×1920 的高斯模糊，是长话渲染慢到超时的主因
    （实测 74 场景 / 296 秒成片只有 0.065x，900s 只渲出 58 秒内容）。
    传 ``None`` 则走旧的 split 路径（滤镜图内直接模糊），仅供单测与对照使用。

    **为什么前景先 trim 成单帧再喂 zoompan**：``zoompan`` 的 ``zoom`` 变量在**每个输入帧**
    开始时重置为 1.0，直接对循环图片流用 ``d=1`` 等于没有运镜；先 ``trim=end_frame=1``
    取一张静帧，再让 ``zoompan`` 用 ``d=<帧数>`` 把它展开成整段动画，zoom 才会逐帧累加。

    ``work_dir`` 是中间产物目录（``render_timeline`` 把人声轨等临时文件写在这里）；
    本函数把模糊与运镜全部放在 filtergraph 内完成，不需要额外磁盘文件，
    保留该参数是为了让调用方传入统一的工作目录、保持接口形状一致。

    ``background_blur`` / ``background_darken`` 自 Patch v3.0-8 起仅为接口兼容保留：
    背景滤镜规格已由规范固定为 ``boxblur=30:4`` 与 ``eq=brightness=-0.38:saturation=0.75``，
    不再受这两个参数驱动。

    ``source_size`` 为源分镜 ``(宽, 高)`` 像素尺寸。``pan_down`` 遇到超长竖格
    （``高 > 1.5 × 宽``）时不再走「缩小 + pad + 1.12 倍放大」的旧链路（窄图在安全框里
    只产生约 130px 的假运镜），改为**宽度铺满 1020、高度等比放大后在 1020×1220 视窗内
    从顶匀速扫到底**（``z=1.0``、``y=(ih-1220)*on/(frames-1)``），符合规范对超长竖格的
    明确要求；``None`` 或尺寸不达标时保持原链路，离线单测不传也能工作。
    """
    width = int(getattr(timeline, "width", DEFAULT_WIDTH) or DEFAULT_WIDTH)
    height = int(getattr(timeline, "height", DEFAULT_HEIGHT) or DEFAULT_HEIGHT)
    fps = int(fps or getattr(timeline, "fps", DEFAULT_FPS) or DEFAULT_FPS)
    index = int(scene.scene_index)
    frames = max(1, int(round(float(scene.duration_ms) / 1000.0 * fps)))
    motion = _motion_of(scene)

    chains: list[str] = []
    if background_index is None:
        # 旧路径：从前景 split 出背景，模糊在帧循环里逐帧计算。
        chains.append(f"[{index}:v]split=2[b{index}src][f{index}src]")
        chains.append(
            f"[b{index}src]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},{BACKGROUND_BLUR_FILTER},{BACKGROUND_EQ_FILTER}[b{index}]"
        )
        fg_label = f"f{index}src"
    else:
        # 新路径：背景是预渲染好的静态图（模糊只算一次），这里只做格式对齐。
        chains.append(f"[{int(background_index)}:v]format=yuva420p[b{index}]")
        fg_label = f"{int(foreground_index if foreground_index is not None else index)}:v"

    # Patch v2.6-5：focus_hint 为 top/bottom/center 时，镜头偏移到对应取景带。
    # 只在 ``focus_band=True`` 时裁切——默认仍保证「原图完整居中不裁切」。
    crop = _focus_band_crop(scene)
    if crop:
        chains.append(f"[{fg_label}]{crop}[c{index}]")
        fg_label = f"c{index}"

    # Patch v3.0-8：超长竖格的 pan_down 走「宽度铺满 + 视窗内扫底」专用链路。
    # focus_band 已自行取景裁切，不与超长格链路叠加；source_size 缺失时退回通用链路。
    is_tall_pan = (
        motion == "pan_down"
        and not crop
        and source_size is not None
        and int(source_size[1]) > TALL_PANEL_RATIO * int(source_size[0])
    )
    if is_tall_pan:
        # scale 到宽 1020 后高必然 > 1530（源高宽比 > 1.5），zoompan 在 1220 高视窗内
        # 从 y=0 匀速扫到 y=ih-1220；frames=1 时 span 取 1 防除零，单帧定格在顶端。
        span = max(1, frames - 1)
        chains.append(
            f"[{fg_label}]trim=end_frame=1,setpts=PTS-STARTPTS,"
            f"scale={SAFE_WIDTH}:-2,format=yuva420p,"
            f"zoompan=z='1.0':x=0:y='(ih-{SAFE_BOX_HEIGHT})*on/{span}':"
            f"d={frames}:s={SAFE_WIDTH}x{SAFE_BOX_HEIGHT}:fps={fps}[f{index}]"
        )
    elif motion == "static":
        # static 不加 zoompan：直接用输入的每一帧，保证长度与场景时长一致。
        chains.append(
            f"[{fg_label}]scale={SAFE_WIDTH}:{SAFE_BOX_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"format=yuva420p,pad={SAFE_WIDTH}:{SAFE_BOX_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black@0.0[f{index}]"
        )
    else:
        zoom, pan_x, pan_y = _motion_expressions(motion, frames)
        chains.append(
            f"[{fg_label}]trim=end_frame=1,setpts=PTS-STARTPTS,"
            f"scale={SAFE_WIDTH}:{SAFE_BOX_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"format=yuva420p,pad={SAFE_WIDTH}:{SAFE_BOX_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black@0.0,"
            f"zoompan=z='{zoom}':x='{pan_x}':y='{pan_y}':d={frames}:s={SAFE_WIDTH}x{SAFE_BOX_HEIGHT}:fps={fps}[f{index}]"
        )

    # Patch v3.0-8：前景固定 1020×1220，在 y=180~1440 安全带内居中（h=1220 时 y=200、下沿 1420）。
    chains.append(
        f"[b{index}][f{index}]overlay=x=(W-w)/2:y={SAFE_TOP}+({SAFE_ZONE_HEIGHT}-h)/2[s{index}]"
    )
    return ";".join(chains)


# --------------------------------------------------------------------------
# 音频混合
# --------------------------------------------------------------------------
def _mix_labels(labels: Sequence[str], *, prefix: str) -> tuple[str, list[str]]:
    """把多个音频 label 混合成一个 label，返回 ``(结果 label, 追加的滤镜链)``。

    **为什么要分组**：``amix`` 的输入 pad 数量有上限，
    一次混合几十路音频会直接报错；这里先组内混合、再混合组结果，
    任意数量的输入都能拼成一条轨。
    """
    if not labels:
        raise RenderError("没有可混合的音频输入")
    if len(labels) == 1:
        return labels[0], []

    chains: list[str] = []
    current = list(labels)
    level = 0
    while len(current) > _MIX_GROUP_SIZE:
        groups: list[str] = []
        for start in range(0, len(current), _MIX_GROUP_SIZE):
            chunk = current[start : start + _MIX_GROUP_SIZE]
            out = f"{prefix}g{level}_{start}"
            chains.append(
                "[" + "][".join(chunk) + f"]amix=inputs={len(chunk)}:duration=longest:normalize=0[{out}]"
            )
            groups.append(out)
        current = groups
        level += 1

    out = f"{prefix}mix"
    chains.append(
        "[" + "][".join(current) + f"]amix=inputs={len(current)}:duration=longest:normalize=0[{out}]"
    )
    return out, chains


# --------------------------------------------------------------------------
# 渲染主流程
# --------------------------------------------------------------------------
_MAX_SCENES_PER_SEGMENT = 8
"""单个渲染分段最多包含多少个场景。

**为什么要分段**：整话一次性塞进一个 ``filter_complex`` 时，输入数 =
``2 × 场景数 + 字幕数``；长话（74 场景 / 73 字幕）会到 222 个输入，
每个输入各带一个解码线程与帧缓冲，滤镜图的 frame pool 随之膨胀——
实测 16GB 机器上 swscale 直接报
``Failed initializing scaling graph (Resource temporarily unavailable)``，
成片写出 0 字节（30 场景 / 91 输入尚可，74 场景必崩）。

分段之后每段的输入数恒定在 26 以内，内存峰值与话长无关；
段产物再由 concat demuxer 无损拼接（``-c copy``），画面与音轨无二次编码损失。
"""


def resolve_audio_asset(
    directory: str | Path,
    name: str,
    *,
    suffixes: Sequence[str] = ASSET_SUFFIX_PRIORITY,
) -> Path | None:
    """在目录里按优先级找同名素材，返回命中的文件；找不到返回 ``None``。

    **为什么按后缀优先级找**：同名素材可能同时存在 ``.mp3`` 与 ``.wav``，
    优先用压缩率高的格式（``.mp3`` > ``.m4a`` > ``.wav`` > ``.ogg``），
    渲染时下载/解码开销更小。
    """
    stem = Path(str(name)).stem
    if not stem:
        return None
    root = Path(directory).expanduser()
    for suffix in suffixes:
        candidate = root / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def resolve_default_bgm(
    config: AppConfig | None = None,
    *,
    assets_dir: str | Path | None = None,
) -> Path | None:
    """按 ``render.default_bgm`` 找默认氛围音乐；找不到返回 ``None``。

    找不到同名素材时回退到 ``bgm/`` 目录里的第一个音频文件——
    这样「装了素材但没配名字」也能出片。

    ``render.auto_bgm`` 为 ``False`` 时**不启用**默认音乐（返回 ``None``）：
    用户显式关掉自动配乐时，不能偷偷铺一层氛围垫底。
    """
    settings = config or AppConfig()
    if not settings.render.auto_bgm:
        return None
    root = Path(assets_dir).expanduser() if assets_dir else assets_root()
    bgm_dir = root / BGM_DIR_NAME
    if not bgm_dir.is_dir():
        return None
    hit = resolve_audio_asset(bgm_dir, settings.render.default_bgm)
    if hit is not None:
        return hit
    for suffix in ASSET_SUFFIX_PRIORITY:
        for candidate in sorted(bgm_dir.glob(f"*{suffix}")):
            if candidate.is_file():
                return candidate
    return None


def resolve_sfx_asset(
    sfx_key: str,
    *,
    assets_dir: str | Path | None = None,
) -> Path | None:
    """按音效键找素材文件（``sfx/<key>.<ext>``）；找不到返回 ``None``。"""
    root = Path(assets_dir).expanduser() if assets_dir else assets_root()
    return resolve_audio_asset(root / SFX_DIR_NAME, sfx_key)


# --------------------------------------------------------------------------
# Patch v4.7：按幕 BGM（曲库扫描 / 单幕解析 / 时间轴分幕 / 多轨平滑混音）
# --------------------------------------------------------------------------
#: 多幕 BGM 交叉淡化的默认重叠时长与静音侧的自身淡变上限（毫秒）。
DEFAULT_BGM_CROSSFADE_MS = 1400
_BGM_SILENT_FADE_MS = 700


def list_bgm_library(
    series_dir: str | Path | None,
    assets_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """扫描作品级与全局两个 BGM 曲库，返回曲目元数据列表。

    作品级目录 ``<series_dir>/assets/bgm`` 标记 ``scope="series"``，
    全局目录 ``<assets_dir>/bgm``（缺省取 :func:`assets_root`）标记
    ``scope="global"``；作品级排在前面，同名时前端可据此优先展示。
    只收 :data:`ASSET_SUFFIX_PRIORITY` 里的音频后缀。
    """
    tracks: list[dict[str, Any]] = []

    def _scan(root: Path, scope: str) -> None:
        if not root.is_dir():
            return
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.suffix.lower() not in ASSET_SUFFIX_PRIORITY:
                continue
            tracks.append(
                {
                    "filename": path.name,
                    "scope": scope,
                    "path": path,
                    "size_bytes": path.stat().st_size,
                }
            )

    if series_dir is not None:
        _scan(Path(series_dir) / ASSETS_DIR_NAME / BGM_DIR_NAME, "series")
    global_root = Path(assets_dir).expanduser() if assets_dir else assets_root()
    _scan(global_root / BGM_DIR_NAME, "global")
    return tracks


def resolve_scene_bgm_path(
    scene: StoryboardScene,
    *,
    series_dir: Path,
    series_config: SeriesConfig,
    settings: AppConfig,
    assets_dir: str | Path | None = None,
) -> tuple[Path | None, float]:
    """解析单幕 BGM 的实际文件与增益，返回 ``(path | None, gain_db)``。

    决策顺序：

    1. ``scene.bgm`` 为 ``none``/``silent`` → 本幕显式静音 ``(None, gain)``；
    2. ``scene.bgm`` 是具体文件名 → 作品曲库优先、全局曲库兜底，再按裸路径；
    3. ``scene.bgm`` 为空（自动）→ 先按 ``bgm_mood`` 同名命中（如
       ``sad_piano.mp3``），否则回退作品级配置与全局默认 BGM。

    增益优先取幕级 ``bgm_gain_db``，缺省用渲染配置 ``render.bgm_gain_db``。
    """
    gain = (
        float(scene.bgm_gain_db)
        if scene.bgm_gain_db is not None
        else float(settings.render.bgm_gain_db)
    )
    series_bgm_dir = series_dir / ASSETS_DIR_NAME / BGM_DIR_NAME
    global_root = Path(assets_dir).expanduser() if assets_dir else assets_root()
    global_bgm_dir = global_root / BGM_DIR_NAME
    choice = (scene.bgm or "").strip()

    if choice.lower() in {"none", "silent"}:
        return None, gain

    if choice:
        hit = resolve_audio_asset(series_bgm_dir, choice)
        if hit is None:
            hit = resolve_audio_asset(global_bgm_dir, choice)
        if hit is None:
            candidate = Path(choice)
            hit = candidate if candidate.is_absolute() else series_dir / candidate
            if not hit.is_file():
                print(f"[s7] 警告：幕 {scene.scene_id} 指定的 BGM {choice!r} 不存在，回退自动配乐")
                hit = None
        if hit is not None:
            return hit, gain

    # 自动模式：先按情绪档同名命中（作品级优先，再全局）。
    mood = (scene.bgm_mood or "").strip()
    if mood:
        hit = resolve_audio_asset(series_bgm_dir, mood)
        if hit is None:
            hit = resolve_audio_asset(global_bgm_dir, mood)
        if hit is not None:
            return hit, gain

    configured = (series_config.bgm or "").strip() if series_config else ""
    if configured:
        candidate = Path(configured)
        hit = candidate if candidate.is_absolute() else series_dir / candidate
        if hit.is_file():
            return hit, gain
    default_hit = resolve_default_bgm(settings, assets_dir=assets_dir)
    return default_hit, gain


@dataclass(frozen=True, slots=True)
class BgmSpan:
    """正文时间轴上的一段 BGM（毫秒，相对正文 0 点；允许负起点覆盖封页）。"""

    start_ms: int
    end_ms: int
    bgm_path: Path | None
    gain_db: float


def build_scene_bgm_spans(
    storyboard: EpisodeStoryboard,
    timeline: RenderTimeline,
    *,
    series_dir: Path,
    series_config: SeriesConfig,
    settings: AppConfig,
    assets_dir: str | Path | None = None,
    cover_duration_ms: int = 0,
    outro_duration_ms: int = 0,
) -> list[BgmSpan]:
    """按启用幕把正文 ``[0, total_duration_ms]`` 连续切成若干 :class:`BgmSpan`。

    * ``timeline.scenes`` 的 ``panel_id`` 映射到幕（``panel_ids`` 命中），
      对不上幕的时间格归入相邻前幕（开头悬空归第一幕），保证区间无空洞；
    * 相邻幕若解析出同一文件且增益（保留 1 位小数）相同，自动合并；
    * 首段向前含封页时长（负起点）、末段向后含结尾时长，由混音环节夹取。
    """
    active_scenes = [
        scene
        for scene in storyboard.scenes
        if scene.enabled and scene.panel_ids
    ]
    if not active_scenes:
        return []

    panel_owner: dict[str, StoryboardScene] = {}
    for scene in active_scenes:
        for panel_id in scene.panel_ids:
            panel_owner.setdefault(panel_id, scene)

    resolved: dict[str, tuple[Path | None, float]] = {
        scene.scene_id: resolve_scene_bgm_path(
            scene,
            series_dir=series_dir,
            series_config=series_config,
            settings=settings,
            assets_dir=assets_dir,
        )
        for scene in active_scenes
    }

    total_ms = max(0, int(timeline.total_duration_ms or 0))
    if total_ms <= 0:
        return []

    # 1) 时间格按出现顺序归组到 owner（None = 没对上任何启用幕）。
    raw: list[tuple[int, int, StoryboardScene | None]] = []
    for tl_scene in sorted(timeline.scenes or [], key=lambda item: int(item.start_ms)):
        owner = panel_owner.get(tl_scene.panel_id)
        start = max(0, int(tl_scene.start_ms))
        end = min(total_ms, int(tl_scene.end_ms))
        if end <= start:
            continue
        if raw and raw[-1][2] is owner:
            prev_start, _prev_end, prev_owner = raw[-1]
            raw[-1] = (prev_start, end, prev_owner)
        else:
            raw.append((start, end, owner))
    if not raw:
        return []

    # 2) 悬空段归并：开头的 None 归第一幕，其余 None 归前一幕。
    first_owner = next((owner for _, _, owner in raw if owner is not None), None)
    if first_owner is None:
        return []
    last_owner: StoryboardScene | None = None
    filled: list[tuple[int, int, StoryboardScene]] = []
    for start, end, owner in raw:
        if owner is None:
            owner = last_owner or first_owner
        filled.append((start, end, owner))
        last_owner = owner

    # 3) 用 owner 切换点把时间轴切成连续无空洞区间 [boundary[i], boundary[i+1])。
    boundaries: list[tuple[int, StoryboardScene]] = []
    current: StoryboardScene | None = None
    for start, _end, owner in filled:
        if owner is not current:
            boundaries.append((start, owner))
            current = owner

    spans: list[BgmSpan] = []
    for index, (start, owner) in enumerate(boundaries):
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else total_ms
        path, gain = resolved[owner.scene_id]
        spans.append(BgmSpan(start, end, path, gain))

    # 4) 相邻同曲同增益合并。
    merged: list[BgmSpan] = []
    for span in spans:
        if (
            merged
            and merged[-1].bgm_path == span.bgm_path
            and round(merged[-1].gain_db, 1) == round(span.gain_db, 1)
        ):
            prev = merged[-1]
            merged[-1] = BgmSpan(prev.start_ms, span.end_ms, prev.bgm_path, prev.gain_db)
        else:
            merged.append(span)

    # 5) 封页/结尾向外扩展。
    cover_ms = max(0, int(cover_duration_ms))
    outro_ms = max(0, int(outro_duration_ms))
    if merged:
        first = merged[0]
        merged[0] = BgmSpan(first.start_ms - cover_ms, first.end_ms, first.bgm_path, first.gain_db)
        last = merged[-1]
        merged[-1] = BgmSpan(last.start_ms, last.end_ms + outro_ms, last.bgm_path, last.gain_db)
    return merged


def _bgm_span_neighbours(
    spans: Sequence[BgmSpan], index: int
) -> tuple[BgmSpan | None, BgmSpan | None]:
    """取相邻 span（跳过不存在的索引）。"""
    prev_span = spans[index - 1] if index > 0 else None
    next_span = spans[index + 1] if index + 1 < len(spans) else None
    return prev_span, next_span


def synthesize_multi_bgm_wav(
    spans: Sequence[BgmSpan],
    *,
    total_s: float,
    out_path: Path,
    ffmpeg_bin: str = "ffmpeg",
    crossfade_ms: int = DEFAULT_BGM_CROSSFADE_MS,
) -> Path | None:
    """把多幕 BGM 按绝对时间轴预合成一条立体声 WAV。

    方案：每条非静音 :class:`BgmSpan` 独立成轨，在跨幕边界两侧各扩展半个
    重叠区并做 ``afade`` 淡入淡出，``adelay`` 落到绝对时间点后多路
    ``amix`` 混合；静音幕不生成轨道，混合后该时段自然无声。

    * 相邻都是非静音且曲目不同：单边扩展
      ``min(crossfade_ms, 邻段时长*0.35, 本段时长*0.35) // 2``；
    * 邻段是静音或位于片头片尾：本段做不超过 700ms 的自身淡变；
    * 全部 span 都静音时返回 ``None``，调用方按「无 BGM」处理。
    """
    total_ms = max(1, int(round(total_s * 1000)))
    clipped: list[BgmSpan] = []
    for span in spans:
        start = max(0, int(span.start_ms))
        end = min(total_ms, int(span.end_ms))
        if end > start:
            clipped.append(BgmSpan(start, end, span.bgm_path, span.gain_db))
    if not clipped:
        return None
    if all(span.bgm_path is None for span in clipped):
        return None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    args: list[str] = ["-y", "-hide_banner"]
    tracks: list[dict[str, float | int | str]] = []
    for index, span in enumerate(clipped):
        if span.bgm_path is None:
            continue
        prev_span, next_span = _bgm_span_neighbours(clipped, index)
        duration_ms = span.end_ms - span.start_ms

        def _half_extent(neighbour: BgmSpan | None) -> int:
            if neighbour is None or neighbour.bgm_path is None:
                return 0
            neighbour_duration = max(1, neighbour.end_ms - neighbour.start_ms)
            return max(
                0,
                min(
                    int(crossfade_ms),
                    int(neighbour_duration * 0.35),
                    int(duration_ms * 0.35),
                )
                // 2,
            )

        left_ext = _half_extent(prev_span)
        right_ext = _half_extent(next_span)
        # 夹取到整片时间轴之内。
        left_ext = min(left_ext, span.start_ms)
        right_ext = min(right_ext, total_ms - span.end_ms)
        track_start = span.start_ms - left_ext
        track_end = span.end_ms + right_ext
        track_duration_ms = track_end - track_start

        # 淡变时长：跨曲重叠区两侧各 ext，总重叠 2*ext；静音/边界侧 <=700ms。
        fade_in_ms = (2 * left_ext) if left_ext > 0 else min(_BGM_SILENT_FADE_MS, duration_ms)
        fade_out_ms = (
            2 * right_ext if right_ext > 0 else min(_BGM_SILENT_FADE_MS, duration_ms)
        )
        fade_in_ms = min(fade_in_ms, track_duration_ms)
        fade_out_ms = min(fade_out_ms, track_duration_ms - fade_in_ms)

        args += ["-stream_loop", "-1", "-i", str(span.bgm_path)]
        tracks.append(
            {
                "index": len(tracks),
                "delay": track_start,
                "duration_s": track_duration_ms / 1000.0,
                "fade_in_ms": fade_in_ms,
                "fade_out_ms": fade_out_ms,
                "gain_db": span.gain_db,
            }
        )

    # 注意：loudnorm 必须放在 amix 之后的总线上，不能放在各轨内。
    # 实测 ffmpeg 9 中 loudnorm 收到有限流 EOF 后会提前透传结束信号，
    # 导致其后的 adelay 缓冲样本不被 flush（大延迟轨道整体丢失，
    # 输出只剩 atrim 截出的前几秒）。轨内只做成型，总线统一归一化，
    # 响度目标与单曲 BGM 链路（I=-16/TP=-1.5/LRA=11）保持一致。
    chains: list[str] = []
    labels: list[str] = []
    for track in tracks:
        idx = int(track["index"])
        delay = int(track["delay"])
        duration_s = float(track["duration_s"])
        chain = (
            f"[{idx}:a]aformat=sample_fmts=fltp:sample_rates={_AUDIO_RATE}:"
            f"channel_layouts={_AUDIO_LAYOUT},atrim=0:{duration_s:.3f},"
            f"volume={float(track['gain_db']):.1f}dB"
        )
        fade_in_ms = int(track["fade_in_ms"])
        fade_out_ms = int(track["fade_out_ms"])
        if fade_in_ms > 0:
            chain += f",afade=t=in:st=0:d={fade_in_ms / 1000.0:.3f}"
        if fade_out_ms > 0:
            out_start = max(0.0, duration_s - fade_out_ms / 1000.0)
            chain += f",afade=t=out:st={out_start:.3f}:d={fade_out_ms / 1000.0:.3f}"
        chain += f",adelay={delay}|{delay},apad,atrim=0:{total_s:.3f}[bm{idx}]"
        chains.append(chain)
        labels.append(f"bm{idx}")

    mixed, mix_chains = _mix_labels(labels, prefix="bm")
    chains.extend(mix_chains)
    chains.append(
        f"[{mixed}]loudnorm=I=-16:TP=-1.5:LRA=11,apad,atrim=0:{total_s:.3f}[bgmout]"
    )
    args += ["-filter_complex", ";".join(chains)]
    args += [
        "-map",
        "[bgmout]",
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(_AUDIO_RATE),
        "-ac",
        "2",
        str(out_path),
    ]
    run_ffmpeg(args, ffmpeg_bin=ffmpeg_bin)
    return out_path


def _beat_start_map(scenes: Sequence[TimelineScene], clips: Sequence[Any]) -> dict[str, int]:
    """beat_id → 音频起始毫秒。

    **为什么从场景反推**：``AudioClipMeta`` 里没有绝对时间，
    但 Stage 6 保证「一个 beat 的所有场景共享同一个起点」，
    所以用该 beat 第一个场景的 ``start_ms`` 作为音频落点；
    没有对应场景的 beat 退化成「上一句结束 + 停顿」的串行推算。
    """
    starts: dict[str, int] = {}
    for scene in scenes:
        starts.setdefault(str(scene.beat_id), int(scene.start_ms))
    cursor = 0
    for clip in clips:
        beat_id = str(getattr(clip, "beat_id", "") or "")
        if beat_id not in starts:
            starts[beat_id] = cursor
        cursor = max(cursor, starts.get(beat_id, cursor) + int(getattr(clip, "duration_ms", 0) or 0))
        cursor += max(0, int(getattr(clip, "pause_after_ms", 0) or 0))
    return starts


def _prepare_audio(
    timeline: RenderTimeline,
    *,
    episode_dir: Path,
    work_dir: Path,
    total_s: float,
    bgm_path: Path | None,
    bgm_gain_db: float,
    ffmpeg_bin: str,
    sfx_events: Sequence[SfxEvent] = (),
    enable_sfx: bool = True,
    sfx_gain_db: float = DEFAULT_SFX_GAIN_DB,
    assets_dir: Path | None = None,
    audio_offset_ms: int = 0,
    bgm_fade_in_ms: int = 0,
    bgm_fade_out_ms: int = 0,
    bgm_pre_mixed: bool = False,
) -> tuple[Path, Path | None, Path | None]:
    """调用 A「备音」：产出人声轨、BGM 轨与音效轨。

    返回 ``(voice.wav, bgm.wav | None, sfx.wav | None)``。

    **为什么先落盘再进成片调用**：``sidechaincompress`` 的侧链输入只能取自输入文件流，
    人声轨必须是真实文件才能作为闪避触发器。

    Patch v4.5：``audio_offset_ms`` 把所有人声/音效落点整体后移（给封页引导留白）；
    ``bgm_fade_in_ms`` / ``bgm_fade_out_ms`` 在 BGM 轨首尾加平滑 ``afade``。
    三个参数全为 0（默认）时滤镜链与历史行为完全一致。

    Patch v4.7：``bgm_pre_mixed=True`` 表示 ``bgm_path`` 已是多幕预合成的全长
    WAV（内含响度归一、分幕增益与交叉淡化），此时不再 ``-stream_loop``、
    不再重复施加 ``loudnorm`` / ``volume``，只保留格式规范与 v4.5 首尾淡变。
    """
    audio_offset_ms = max(0, int(audio_offset_ms))
    voice_path = work_dir / "voice.wav"
    bgm_norm_path = work_dir / "bgm.wav"
    sfx_norm_path = work_dir / "sfx.wav"

    clips = list(timeline.audios or [])
    starts = _beat_start_map(timeline.scenes or [], clips)

    usable: list[tuple[Path, int]] = []
    for clip in clips:
        resolved = _resolve_audio_path(getattr(clip, "audio_path", ""), episode_dir)
        if resolved is None:
            # 缺音频不阻断渲染：画面照常出片，只是这句没有配音。
            print(f"[s7] 警告：找不到音频 {getattr(clip, 'audio_path', '')!r}，该 beat 静音处理")
            continue
        usable.append(
            (
                resolved,
                int(starts.get(str(getattr(clip, "beat_id", "") or ""), 0))
                + audio_offset_ms,
            )
        )

    # ---- 音效轨（Patch v3.1 Patch 11 B4）----
    sfx_inputs: list[tuple[Path, int, float]] = []
    if enable_sfx:
        for event in sfx_events or []:
            key = str(getattr(event, "sfx_key", "") or "").strip()
            if not key:
                continue
            asset = resolve_sfx_asset(key, assets_dir=assets_dir)
            if asset is None:
                print(f"[s7] 警告：找不到音效素材 {key!r}，该音效跳过")
                continue
            gain = sfx_gain_db
            event_gain = getattr(event, "gain_db", None)
            if (
                event_gain is not None
                and float(event_gain) != _SFX_EVENT_SCHEMA_DEFAULT_GAIN
            ):
                # 事件显式给了不同于 schema 默认值的音量就用它，否则以渲染配置为准。
                gain = float(event_gain)
            sfx_inputs.append(
                (
                    asset,
                    max(0, int(getattr(event, "start_ms", 0) or 0)) + audio_offset_ms,
                    float(gain),
                )
            )

    # Patch 14：显性混音日志——bgm 文件名/增益与 sfx 实际可用数/事件总数一眼可查，
    # 素材缺失或 enable_sfx=False 时 usable 会小于 total，配合上方警告定位漏播音效。
    bgm_label = bgm_path.name if bgm_path is not None else "NONE"
    print(
        f"[s7] 音频混合：bgm={bgm_label} ({float(bgm_gain_db):.1f}dB), "
        f"sfx={len(sfx_inputs)}/{len(tuple(sfx_events or ()))} 条"
    )

    args: list[str] = ["-y", "-hide_banner"]
    for path, _ in usable:
        args += ["-i", str(path)]
    bgm_index = len(usable)
    if bgm_path is not None:
        if bgm_pre_mixed:
            # Patch v4.7：预合成 WAV 已是整片全长，直接顺序读取即可。
            args += ["-i", str(bgm_path)]
        else:
            # -stream_loop -1 让短 BGM 循环铺满整条片，末尾由 atrim 精确截断。
            args += ["-stream_loop", "-1", "-i", str(bgm_path)]
    sfx_base = len(usable) + (1 if bgm_path is not None else 0)
    for path, _start, _gain in sfx_inputs:
        args += ["-i", str(path)]

    chains: list[str] = []
    labels: list[str] = []
    for index, (_, start) in enumerate(usable):
        chains.append(
            f"[{index}:a]aformat=sample_fmts=fltp:sample_rates={_AUDIO_RATE}:"
            f"channel_layouts={_AUDIO_LAYOUT},adelay={start}|{start},"
            f"apad,atrim=0:{total_s:.3f}[va{index}]"
        )
        labels.append(f"va{index}")

    if labels:
        mixed, mix_chains = _mix_labels(labels, prefix="v")
        chains.extend(mix_chains)
        chains.append(f"[{mixed}]apad,atrim=0:{total_s:.3f}[voice]")
    else:
        # 没有可用人声也要留一条静音轨，成片始终带 AAC 音轨。
        chains.append(f"anullsrc=r={_AUDIO_RATE}:cl={_AUDIO_LAYOUT}:d={total_s:.3f}[voice]")

    if bgm_path is not None:
        if bgm_pre_mixed:
            # Patch v4.7：多幕预合成轨已含 loudnorm/分幕增益/交叉淡化，
            # 这里只规范采样格式并截到整片时长，v4.5 首尾 afade 仍可叠加。
            bgm_filters = (
                f"[{bgm_index}:a]aformat=sample_fmts=fltp:sample_rates={_AUDIO_RATE}:"
                f"channel_layouts={_AUDIO_LAYOUT},apad,atrim=0:{total_s:.3f}"
            )
        else:
            # Patch 14：先 loudnorm 响度归一（统一各来源 BGM 的音量基线）再降增益，
            # 顺序保持「loudnorm → volume=bgm_gain_db → sidechaincompress」。
            # Patch v4.5：atrim 之后按需追加首尾 afade（封页渐入 / 结尾随画面同步淡出）。
            bgm_filters = (
                f"[{bgm_index}:a]aformat=sample_fmts=fltp:sample_rates={_AUDIO_RATE}:"
                f"channel_layouts={_AUDIO_LAYOUT},loudnorm=I=-16:TP=-1.5:LRA=11,"
                f"volume={float(bgm_gain_db):.1f}dB,"
                f"apad,atrim=0:{total_s:.3f}"
            )
        total_ms = max(1, int(round(total_s * 1000)))
        fade_in_ms = min(max(0, int(bgm_fade_in_ms)), total_ms // 2)
        fade_out_ms = min(max(0, int(bgm_fade_out_ms)), total_ms - fade_in_ms)
        if fade_in_ms > 0:
            bgm_filters += f",afade=t=in:st=0:d={fade_in_ms / 1000.0:.3f}"
        if fade_out_ms > 0:
            out_start_s = max(0.0, (total_ms - fade_out_ms) / 1000.0)
            bgm_filters += (
                f",afade=t=out:st={out_start_s:.3f}:d={fade_out_ms / 1000.0:.3f}"
            )
        bgm_filters += "[bgm]"
        chains.append(bgm_filters)

    sfx_labels: list[str] = []
    for offset, (_path, start, gain) in enumerate(sfx_inputs):
        # Patch 14：adelay 对齐事件时间点 → dynaudnorm 动态峰值归一（清晰且不爆音）
        # → volume 施加配置/事件增益。
        chains.append(
            f"[{sfx_base + offset}:a]aformat=sample_fmts=fltp:sample_rates={_AUDIO_RATE}:"
            f"channel_layouts={_AUDIO_LAYOUT},adelay={start}|{start},dynaudnorm,"
            f"volume={gain:.1f}dB,"
            f"apad,atrim=0:{total_s:.3f}[sx{offset}]"
        )
        sfx_labels.append(f"sx{offset}")
    if sfx_labels:
        mixed_sfx, sfx_chains = _mix_labels(sfx_labels, prefix="s")
        chains.extend(sfx_chains)
        chains.append(f"[{mixed_sfx}]apad,atrim=0:{total_s:.3f}[sfx]")

    args += ["-filter_complex", ";".join(chains)]
    args += ["-map", "[voice]", "-c:a", "pcm_s16le", "-ar", str(_AUDIO_RATE), "-ac", "2", str(voice_path)]
    if bgm_path is not None:
        args += ["-map", "[bgm]", "-c:a", "pcm_s16le", "-ar", str(_AUDIO_RATE), "-ac", "2", str(bgm_norm_path)]
    if sfx_labels:
        args += ["-map", "[sfx]", "-c:a", "pcm_s16le", "-ar", str(_AUDIO_RATE), "-ac", "2", str(sfx_norm_path)]

    run_ffmpeg(args, ffmpeg_bin=ffmpeg_bin)
    return (
        voice_path,
        (bgm_norm_path if bgm_path is not None else None),
        (sfx_norm_path if sfx_labels else None),
    )


def _prepare_background(
    image_path: Path,
    out_path: Path,
    *,
    width: int,
    height: int,
    background_blur: int,
    background_darken: float,
    ffmpeg_bin: str = "ffmpeg",
) -> Path:
    """把「放大裁切 + 高斯模糊 + 暗化降饱和」的背景一次性渲染成静态图。

    **为什么必须预渲染**：原滤镜链把模糊放在帧循环里，对同一张静态图
    每帧都重算一次 1080×1920 的高斯模糊。实测 74 场景 / 296 秒的成片渲染速度
    只有 0.065x，900 秒超时只渲出 58 秒内容——背景本该只算一次。
    预渲染用的是同一串滤镜，像素结果与滤镜图内逐帧计算等价。

    Patch v3.0-8：滤镜规格按抖音安全分区规范固定为
    ``boxblur=30:4,eq=brightness=-0.38:saturation=0.75``；
    ``background_blur`` / ``background_darken`` 参数仅为接口兼容保留，不再参与渲染。
    """
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},{BACKGROUND_BLUR_FILTER},{BACKGROUND_EQ_FILTER}"
    )
    run_ffmpeg(
        [
            "-y",
            "-hide_banner",
            "-i",
            str(image_path),
            "-vf",
            vf,
            "-frames:v",
            "1",
            str(out_path),
        ],
        ffmpeg_bin=ffmpeg_bin,
        timeout_s=300,
    )
    return out_path


def _prepare_subtitle_png(png_path: Path, out_path: Path) -> tuple[int, int]:
    """把整帧字幕 PNG 裁成只含文字的最小矩形，返回它在整帧里的 ``(x, y)`` 偏移。

    **为什么裁**：Stage 6 产出的是整帧 1080×1920 RGBA，但实测文字只占 4.8% 面积。
    ``overlay`` 要对整帧做 alpha 混合，等于每帧白做 95% 的无用混合；
    73 条字幕串行叠加时，这项开销直接压垮渲染速度。
    裁剪后 overlay 只需混合文字那一小块。
    """
    from PIL import Image

    with Image.open(png_path) as image:
        rgba = image.convert("RGBA")
        bbox = rgba.getbbox()
        if bbox is None:
            # 全透明字幕（理论上不该出现）：原样复制，交给上层处理。
            rgba.save(out_path)
            return 0, 0
        x0, y0, _x1, _y1 = bbox
        rgba.crop(bbox).save(out_path)
    return int(x0), int(y0)


def _render_segment(
    *,
    timeline: RenderTimeline,
    scenes: Sequence[TimelineScene],
    backgrounds: Sequence[Path],
    foregrounds: Sequence[Path],
    subtitles: Sequence[tuple[SubtitleLine, Path, int, int]],
    seg_start_ms: int,
    seg_len_ms: int,
    voice_path: Path,
    bgm_path: Path | None,
    sfx_path: Path | None = None,
    fps: int,
    width: int,
    height: int,
    background_blur: int,
    background_darken: float,
    out_path: Path,
    ffmpeg_bin: str,
    # ---- Patch v4.5：作品级封页 / 双结尾片包（全默认 = 历史行为 100% 一致）----
    cover_png: Path | None = None,
    cover_duration_ms: int = 0,
    cover_fade_in_ms: int = 0,
    cover_fade_out_ms: int = 0,
    outro_png: Path | None = None,
    outro_duration_ms: int = 0,
    outro_fade_in_ms: int = 0,
    outro_fade_out_ms: int = 0,
    seg_audio_start_ms: int | None = None,
    seg_audio_len_ms: int | None = None,
    subtitle_shift_ms: int = 0,
) -> Path:
    """渲染一个分段，输出带音轨的独立 mp4。

    **分段的意义**：见 ``_MAX_SCENES_PER_SEGMENT`` 的说明——整话一个滤镜图会
    把内存峰值推到机器扛不住的量级（222 个输入时 swscale 直接失败）。

    段内时间一律相对 ``seg_start_ms``：字幕的 ``enable='between(t,…)'``
    用相对时间，音频用 ``-ss``/``-t`` 在输入端切片。

    Patch v4.5：封页只在第一段接入、结尾只在最后一段接入（单段时两者同段）。
    所有片包参数缺省时，输入列表、滤镜链与输出时长与历史链路逐字一致。
    """
    scene_count = len(scenes)
    seg_s = max(0.001, seg_len_ms / 1000.0)

    # ---- 封页时长数学（夹取保证 xfade/concat 的输入时长恒合法）----
    cover_active = cover_png is not None
    outro_active = outro_png is not None
    if cover_active:
        eff_cover_ms = max(200, int(cover_duration_ms))
        eff_cover_fade_out = min(
            max(0, int(cover_fade_out_ms)),
            max(0, eff_cover_ms - 100),
            max(0, seg_len_ms - 100),
        )
        eff_cover_fade_in = min(
            max(0, int(cover_fade_in_ms)),
            max(0, eff_cover_ms - eff_cover_fade_out),
        )
    else:
        eff_cover_ms = 0
        eff_cover_fade_in = 0
        eff_cover_fade_out = 0

    # ---- 结尾时长数学 ----
    if outro_active:
        eff_outro_ms = max(200, int(outro_duration_ms))
        eff_outro_fade_in = min(
            max(0, int(outro_fade_in_ms)),
            max(0, eff_outro_ms - 100),
            max(0, seg_len_ms - 100),
        )
        eff_outro_fade_out = min(
            max(0, int(outro_fade_out_ms)),
            max(0, eff_outro_ms - eff_outro_fade_in),
        )
    else:
        eff_outro_ms = 0
        eff_outro_fade_in = 0
        eff_outro_fade_out = 0

    #: 结尾起点 = 正文 + 封页（xfade offset 基准）。
    pre_outro_ms = seg_len_ms + eff_cover_ms
    seg_total_ms = seg_len_ms + eff_cover_ms + eff_outro_ms
    seg_total_s = max(0.001, seg_total_ms / 1000.0)

    # 音频切片窗口：不传片包参数时回退历史口径（seg_start / seg_len）。
    if seg_audio_start_ms is not None:
        audio_start_s = max(0.0, seg_audio_start_ms / 1000.0)
    else:
        audio_start_s = max(0.0, seg_start_ms / 1000.0)
    if seg_audio_len_ms is not None:
        audio_len_s = max(0.001, seg_audio_len_ms / 1000.0)
    else:
        audio_len_s = seg_s
    subtitle_shift_s = max(0.0, int(subtitle_shift_ms) / 1000.0)
    branding_active = cover_active or outro_active

    # ---- 输入列表：背景 N → 前景 N → 字幕 M →（封页）→（结尾）→ BGM → 人声 → 音效 ----
    args: list[str] = ["-y", "-hide_banner"]
    for path, scene in zip(backgrounds, scenes):
        seconds = max(0.001, float(scene.duration_ms) / 1000.0)
        args += ["-loop", "1", "-framerate", str(fps), "-t", f"{seconds:.3f}", "-i", str(path)]
    for path, scene in zip(foregrounds, scenes):
        seconds = max(0.001, float(scene.duration_ms) / 1000.0)
        args += ["-loop", "1", "-framerate", str(fps), "-t", f"{seconds:.3f}", "-i", str(path)]
    for _line, png, _x, _y in subtitles:
        args += ["-loop", "1", "-framerate", str(fps), "-t", f"{seg_s:.3f}", "-i", str(png)]

    subtitle_base = scene_count * 2
    next_input = subtitle_base + len(subtitles)
    cover_index: int | None = None
    outro_index: int | None = None
    if cover_active:
        cover_index = next_input
        next_input += 1
        args += [
            "-loop", "1", "-framerate", str(fps),
            "-t", f"{eff_cover_ms / 1000.0:.3f}", "-i", str(cover_png),
        ]
    if outro_active:
        outro_index = next_input
        next_input += 1
        args += [
            "-loop", "1", "-framerate", str(fps),
            "-t", f"{eff_outro_ms / 1000.0:.3f}", "-i", str(outro_png),
        ]

    bgm_index = next_input
    voice_index = bgm_index
    if bgm_path is not None:
        args += [
            "-ss", f"{audio_start_s:.3f}", "-t", f"{audio_len_s:.3f}", "-i", str(bgm_path),
        ]
        voice_index = bgm_index + 1
    args += ["-ss", f"{audio_start_s:.3f}", "-t", f"{audio_len_s:.3f}", "-i", str(voice_path)]
    sfx_index = voice_index + 1
    if sfx_path is not None:
        args += ["-ss", f"{audio_start_s:.3f}", "-t", f"{audio_len_s:.3f}", "-i", str(sfx_path)]

    # ---- 画面链 ----
    chains: list[str] = []
    for offset, scene in enumerate(scenes):
        chains.append(
            build_scene_filter(
                scene,
                timeline,
                work_dir=out_path.parent,
                fps=fps,
                background_blur=background_blur,
                background_darken=background_darken,
                background_index=offset,
                foreground_index=scene_count + offset,
                source_size=_read_image_size(foregrounds[offset]),
            )
        )
    chains.append(
        "".join(f"[s{int(scene.scene_index)}]" for scene in scenes)
        + f"concat=n={len(scenes)}:v=1:a=0[vcat]"
    )

    video_label = "vcat"
    if branding_active:
        # 统一帧率/时间基/像素格式，xfade 与 concat 都要求两侧输入参数一致。
        chains.append(f"[vcat]fps={fps},settb=AVTB,format=yuv420p[vbody]")
        video_label = "vbody"

        # ---- 封页：黑场淡入 → 停顿 → 叠化到正文第 1 帧 ----
        if cover_active:
            cover_chain = f"[{cover_index}:v]"
            if eff_cover_fade_in > 0:
                cover_chain += f"fade=t=in:st=0:d={eff_cover_fade_in / 1000.0:.3f},"
            cover_chain += f"fps={fps},settb=AVTB,format=yuv420p[vcover]"
            chains.append(cover_chain)
            if eff_cover_fade_out >= 50:
                # 正文头部 clone 出叠化区，xfade 后正文第 1 帧在 t=eff_cover_ms 精确全显。
                fade_out_s = eff_cover_fade_out / 1000.0
                chains.append(
                    f"[{video_label}]tpad=start_duration={fade_out_s:.3f}:start_mode=clone[vbodypre]"
                )
                offset_s = (eff_cover_ms - eff_cover_fade_out) / 1000.0
                chains.append(
                    f"[vcover][vbodypre]xfade=transition=fade:duration={fade_out_s:.3f}:"
                    f"offset={offset_s:.3f}[vwithcover]"
                )
            else:
                chains.append(f"[vcover][{video_label}]concat=n=2:v=1:a=0[vwithcover]")
            video_label = "vwithcover"

    # 字幕逐条 overlay：串行叠加（overlay 的副输入不能是图内 label 的多路并联）。
    # 第一段字幕整体后移 eff_cover_ms，封页停顿期间画面上绝对没有字幕。
    for offset, (line, _png, offset_x, offset_y) in enumerate(subtitles):
        chains.append(f"[{subtitle_base + offset}:v]format=rgba[sub{offset}]")
        start = (
            max(0.0, (float(line.start_ms) - seg_start_ms) / 1000.0) + subtitle_shift_s
        )
        end_raw = (float(line.end_ms) - seg_start_ms) / 1000.0 + subtitle_shift_s
        end = min(seg_total_s, max(start + 0.05, end_raw))
        next_label = f"vsub{offset}"
        chains.append(
            f"[{video_label}][sub{offset}]overlay={offset_x}:{offset_y}:"
            f"enable='between(t,{start:.3f},{end:.3f})'[{next_label}]"
        )
        video_label = next_label

    # ---- 结尾：正文末帧之上叠化浮现 → 停留 → 随 fade out 淡出至黑场 ----
    if outro_active:
        chains.append(f"[{video_label}]fps={fps},settb=AVTB,format=yuv420p[vmain]")
        outro_chain = f"[{outro_index}:v]"
        if eff_outro_fade_out > 0:
            fade_out_start_s = (eff_outro_ms - eff_outro_fade_out) / 1000.0
            outro_chain += (
                f"fade=t=out:st={fade_out_start_s:.3f}:"
                f"d={eff_outro_fade_out / 1000.0:.3f},"
            )
        outro_chain += f"fps={fps},settb=AVTB,format=yuv420p[voutro]"
        chains.append(outro_chain)
        if eff_outro_fade_in >= 50:
            fade_in_s = eff_outro_fade_in / 1000.0
            chains.append(
                f"[vmain]tpad=stop_duration={fade_in_s:.3f}:stop_mode=clone[vmainpad]"
            )
            chains.append(
                f"[vmainpad][voutro]xfade=transition=fade:duration={fade_in_s:.3f}:"
                f"offset={pre_outro_ms / 1000.0:.3f}[vfinal]"
            )
        else:
            chains.append("[vmain][voutro]concat=n=2:v=1:a=0[vfinal]")
        video_label = "vfinal"

    # ---- 音频链（切片/补齐时长统一用 audio_len_s，无片包时等于 seg_s）----
    if bgm_path is not None:
        # 两个输入都写成「输入文件流」：sidechaincompress 的 request pad 不接受图内 label。
        chains.append(
            f"[{bgm_index}:a][{voice_index}:a]sidechaincompress="
            f"threshold={_SIDECHAIN_THRESHOLD}:ratio={_SIDECHAIN_RATIO}:"
            f"attack={_SIDECHAIN_ATTACK_MS}:release={_SIDECHAIN_RELEASE_MS}[duck]"
        )
        chains.append(f"[duck]apad,atrim=0:{audio_len_s:.3f}[bgmv]")
        chains.append(
            f"[{voice_index}:a]aformat=sample_fmts=fltp:sample_rates={_AUDIO_RATE}:"
            f"channel_layouts={_AUDIO_LAYOUT}[vin]"
        )
        if sfx_path is not None:
            # 音效轨与人声、BGM 三路混音（音效本身已在备音阶段做了延迟与降音量）。
            chains.append(
                f"[{sfx_index}:a]aformat=sample_fmts=fltp:sample_rates={_AUDIO_RATE}:"
                f"channel_layouts={_AUDIO_LAYOUT}[sfxv]"
            )
            chains.append(
                "[vin][bgmv][sfxv]amix=inputs=3:duration=first:normalize=0[aout]"
            )
        else:
            chains.append("[vin][bgmv]amix=inputs=2:duration=first:normalize=0[aout]")
    elif sfx_path is not None:
        chains.append(
            f"[{voice_index}:a]aformat=sample_fmts=fltp:sample_rates={_AUDIO_RATE}:"
            f"channel_layouts={_AUDIO_LAYOUT},apad,atrim=0:{audio_len_s:.3f}[vin]"
        )
        chains.append(
            f"[{sfx_index}:a]aformat=sample_fmts=fltp:sample_rates={_AUDIO_RATE}:"
            f"channel_layouts={_AUDIO_LAYOUT},apad,atrim=0:{audio_len_s:.3f}[sfxv]"
        )
        chains.append("[vin][sfxv]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        chains.append(
            f"[{voice_index}:a]aformat=sample_fmts=fltp:sample_rates={_AUDIO_RATE}:"
            f"channel_layouts={_AUDIO_LAYOUT},apad,atrim=0:{audio_len_s:.3f}[aout]"
        )

    args += ["-filter_complex", ";".join(chains)]
    args += ["-map", f"[{video_label}]", "-map", "[aout]"]
    args += [
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", str(_AUDIO_RATE),
        "-ac", "2",
        "-r", str(fps),
        "-s", f"{width}x{height}",
        "-t", f"{seg_total_s:.3f}",
        "-movflags", "+faststart",
        str(out_path),
    ]

    run_ffmpeg(args, ffmpeg_bin=ffmpeg_bin)
    if not out_path.is_file() or out_path.stat().st_size <= 0:
        raise RenderError(f"分段未生成或为空文件：{out_path}")
    return out_path


def render_timeline(
    timeline: RenderTimeline,
    *,
    episode_dir: Path,
    out_path: Path,
    bgm_path: Path | None = None,
    ffmpeg_bin: str = "ffmpeg",
    background_blur: int = 40,
    background_darken: float = 0.55,
    bgm_gain_db: float = -18.0,
    work_dir: Path | None = None,
    enable_sfx: bool = True,
    sfx_gain_db: float = DEFAULT_SFX_GAIN_DB,
    assets_dir: Path | None = None,
    # ---- Patch v4.5：片包参数（全默认 = 历史行为 100% 一致）----
    cover_png: Path | None = None,
    cover_duration_ms: int = 0,
    cover_fade_in_ms: int = 0,
    cover_fade_out_ms: int = 0,
    outro_png: Path | None = None,
    outro_duration_ms: int = 0,
    outro_fade_in_ms: int = 0,
    outro_fade_out_ms: int = 0,
    bgm_fade_in_ms: int = 0,
    bgm_fade_out_ms: int = 0,
    bgm_pre_mixed: bool = False,
) -> Path:
    """按时间轴渲染成片，返回成片路径。

    流程：调用 A 备音（人声 + BGM + 音效）→ 调用 B 成片（画面 + 字幕 + 闪避 + 混音）。

    * 画面：每个场景一条滤镜片段 → ``concat`` 串成一条；
    * 字幕：``06_plan/subtitles/{index:04d}.png`` 逐条 ``overlay``，
      ``enable='between(t,start,end)'`` 控制显示窗口；
    * BGM：``sidechaincompress``（侧链 = 人声轨）做闪避，再与人声 ``amix`` 混合；
    * 音效：``timeline.sfx_events`` 里的事件已在备音阶段 ``adelay`` 对齐 + 降音量，
      这里再与人声、BGM 三路混音。

    Patch v4.5：``cover_png`` 只接到第一段、``outro_png`` 只接到最后一段；
    备音轨按封页时长整体后移人声/音效并加长到「封页 + 正文 + 结尾」，
    BGM 首尾可加 ``afade``。不传任何片包参数时与历史链路完全一致。
    """
    episode_dir = Path(episode_dir).expanduser()
    out_path = Path(out_path).expanduser()
    work_dir = Path(work_dir).expanduser() if work_dir else out_path.parent / "_s7_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    scenes = sorted(timeline.scenes or [], key=lambda s: int(s.scene_index))
    if not scenes:
        raise RenderError("时间轴里没有任何场景（scenes 为空），无法渲染")
    # 重新编号：滤镜片段用 scene_index 当输入索引，必须与输入顺序一致。
    scenes = [scene.model_copy(update={"scene_index": i}) for i, scene in enumerate(scenes)]

    fps = int(timeline.fps or DEFAULT_FPS) or DEFAULT_FPS
    width = int(timeline.width or DEFAULT_WIDTH) or DEFAULT_WIDTH
    height = int(timeline.height or DEFAULT_HEIGHT) or DEFAULT_HEIGHT
    total_ms = int(timeline.total_duration_ms or 0)
    if total_ms <= 0:
        raise RenderError(f"时间轴总时长非法：{total_ms} ms")

    # ---- Patch v4.5：片包生效时长（备音轨与分段切片都按扩展总时长对齐）----
    cover_active = cover_png is not None
    outro_active = outro_png is not None
    eff_cover_ms = max(200, int(cover_duration_ms)) if cover_active else 0
    eff_outro_ms = max(200, int(outro_duration_ms)) if outro_active else 0
    branding_on = cover_active or outro_active
    extended_total_ms = total_ms + eff_cover_ms + eff_outro_ms
    total_s = extended_total_ms / 1000.0

    image_paths: list[Path] = []
    for scene in scenes:
        resolved = _resolve_image_path(scene.image_path, episode_dir)
        if resolved is None:
            raise RenderError(
                f"场景 {scene.scene_index} 的图片不存在：{scene.image_path!r}"
                f"（已按 {episode_dir} 与 {episode_dir / PANEL_DIR_NAME} 解析）"
            )
        image_paths.append(resolved)

    bgm_input: Path | None = None
    if bgm_path is not None:
        candidate = Path(bgm_path).expanduser()
        if candidate.is_file():
            bgm_input = candidate
        else:
            print(f"[s7] 警告：BGM 不存在（{candidate}），本次不铺 BGM")

    voice_path, bgm_norm_path, sfx_norm_path = _prepare_audio(
        timeline,
        episode_dir=episode_dir,
        work_dir=work_dir,
        total_s=total_s,
        bgm_path=bgm_input,
        bgm_gain_db=bgm_gain_db,
        ffmpeg_bin=ffmpeg_bin,
        sfx_events=list(timeline.sfx_events or []),
        enable_sfx=enable_sfx,
        sfx_gain_db=sfx_gain_db,
        assets_dir=assets_dir,
        audio_offset_ms=eff_cover_ms,
        bgm_fade_in_ms=bgm_fade_in_ms if branding_on else 0,
        bgm_fade_out_ms=bgm_fade_out_ms if branding_on else 0,
        bgm_pre_mixed=bgm_pre_mixed,
    )

    # ---- 字幕 PNG ----
    subtitle_lines = sorted(timeline.subtitles or [], key=lambda s: int(s.start_ms))
    subtitle_paths: list[tuple[SubtitleLine, Path]] = []
    for line in subtitle_lines:
        png = _subtitle_png_path(episode_dir, line.index)
        if png is None:
            raise RenderError(
                f"缺少字幕 PNG：{episode_dir / PLAN_DIR_NAME / SUBTITLE_DIR_NAME / f'{line.index:04d}.png'}"
                "（请先执行 Stage 6 生成字幕 PNG）"
            )
        subtitle_paths.append((line, png))

    # ---- 背景预渲染（静态图，高斯模糊只算一次）----
    background_paths: list[Path] = []
    background_cache: dict[Path, Path] = {}
    for index, path in enumerate(image_paths):
        cached = background_cache.get(path)
        if cached is None:
            cached = work_dir / f"bg_{index:04d}.png"
            _prepare_background(
                path,
                cached,
                width=width,
                height=height,
                background_blur=background_blur,
                background_darken=background_darken,
                ffmpeg_bin=ffmpeg_bin,
            )
            background_cache[path] = cached
        background_paths.append(cached)

    # ---- 字幕裁剪（只留文字区域，overlay 不再对整帧做 alpha 混合）----
    subtitle_inputs: list[tuple[SubtitleLine, Path, int, int]] = []
    for line, png in subtitle_paths:
        cropped = work_dir / f"sub_{line.index:04d}.png"
        offset_x, offset_y = _prepare_subtitle_png(png, cropped)
        subtitle_inputs.append((line, cropped, offset_x, offset_y))

    # ---- 分段渲染 ----
    # 整话塞进一个滤镜图会因为输入数过多（74 场景 = 222 个输入）把内存打爆，
    # 详见 _MAX_SCENES_PER_SEGMENT 的说明。这里按场景数切段，段产物再无损拼接。
    segment_paths: list[Path] = []
    segment_count = (len(scenes) + _MAX_SCENES_PER_SEGMENT - 1) // _MAX_SCENES_PER_SEGMENT
    for seg_index, seg_start in enumerate(
        range(0, len(scenes), _MAX_SCENES_PER_SEGMENT)
    ):
        seg_scenes = scenes[seg_start : seg_start + _MAX_SCENES_PER_SEGMENT]
        seg_bg = background_paths[seg_start : seg_start + _MAX_SCENES_PER_SEGMENT]
        seg_fg = image_paths[seg_start : seg_start + _MAX_SCENES_PER_SEGMENT]
        seg_begin_ms = int(seg_scenes[0].start_ms)
        seg_end_ms = int(seg_scenes[-1].end_ms)
        seg_subs = [
            item
            for item in subtitle_inputs
            if int(item[0].start_ms) < seg_end_ms and int(item[0].end_ms) > seg_begin_ms
        ]
        seg_len_ms = sum(int(scene.duration_ms) for scene in seg_scenes)
        seg_out = work_dir / f"seg_{seg_index:04d}.mp4"

        # Patch v4.5：封页只接第一段、结尾只接最后一段（单段时两者同段）。
        is_first_segment = seg_index == 0
        is_last_segment = seg_index == segment_count - 1
        seg_cover_ms = eff_cover_ms if is_first_segment else 0
        seg_outro_ms = eff_outro_ms if is_last_segment else 0
        if branding_on:
            seg_audio_start_ms = (
                0 if is_first_segment else eff_cover_ms + seg_begin_ms
            )
            seg_audio_len_ms = seg_len_ms + seg_cover_ms + seg_outro_ms
        else:
            seg_audio_start_ms = None
            seg_audio_len_ms = None

        _render_segment(
            timeline=timeline,
            scenes=seg_scenes,
            backgrounds=seg_bg,
            foregrounds=seg_fg,
            subtitles=seg_subs,
            seg_start_ms=seg_begin_ms,
            seg_len_ms=seg_len_ms,
            voice_path=voice_path,
            bgm_path=bgm_norm_path,
            sfx_path=sfx_norm_path,
            fps=fps,
            width=width,
            height=height,
            background_blur=background_blur,
            background_darken=background_darken,
            out_path=seg_out,
            ffmpeg_bin=ffmpeg_bin,
            cover_png=cover_png if cover_active and is_first_segment else None,
            cover_duration_ms=cover_duration_ms,
            cover_fade_in_ms=cover_fade_in_ms,
            cover_fade_out_ms=cover_fade_out_ms,
            outro_png=outro_png if outro_active and is_last_segment else None,
            outro_duration_ms=outro_duration_ms,
            outro_fade_in_ms=outro_fade_in_ms,
            outro_fade_out_ms=outro_fade_out_ms,
            seg_audio_start_ms=seg_audio_start_ms,
            seg_audio_len_ms=seg_audio_len_ms,
            subtitle_shift_ms=seg_cover_ms,
        )
        segment_paths.append(seg_out)

    # ---- 拼接分段（-c copy，无二次编码损失）----
    if len(segment_paths) == 1:
        shutil.copyfile(segment_paths[0], out_path)
    else:
        list_path = work_dir / "segments.txt"
        list_path.write_text(
            "\n".join(f"file '{path.resolve()}'" for path in segment_paths) + "\n",
            encoding="utf-8",
        )
        run_ffmpeg(
            [
                "-y",
                "-hide_banner",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(out_path),
            ],
            ffmpeg_bin=ffmpeg_bin,
        )
    if not out_path.is_file() or out_path.stat().st_size <= 0:
        raise RenderError(f"成片未生成或为空文件：{out_path}")
    return out_path


# --------------------------------------------------------------------------
# 单话入口
# --------------------------------------------------------------------------
def _series_title(series_dir: Path) -> str | None:
    """读 Stage 0 的作品标题（``catalog.json`` 的 ``series_title``）；读不到返回 ``None``。"""
    catalog = series_dir / "catalog.json"
    if not catalog.is_file():
        return None
    try:
        data = json.loads(catalog.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    title = str(data.get("series_title") or "").strip() if isinstance(data, dict) else ""
    return title or None


def render_episode(
    series_dir: str | Path,
    episode_no: int,
    *,
    episode_id: str | None = None,
    bgm_path: str | Path | None = None,
    ffmpeg_bin: str = "ffmpeg",
    config: AppConfig | None = None,
    title: str | None = None,
) -> Path:
    """渲染一话的成片，返回 ``07_out/{episode_id}.mp4`` 的路径。

    流程：读 ``06_plan/timeline.json`` → 渲染 → 落 ``07_out/`` → 复制到 ``exports/``。
    BGM 缺省时从作品级 ``series_config.yaml`` 的 ``bgm`` 字段读取。
    """
    series_dir = Path(series_dir).expanduser()
    episode_dir = _episode_dir(series_dir, episode_no)

    timeline_path = _timeline_path(series_dir, episode_no)
    if not timeline_path.is_file():
        raise RenderError(f"缺少时间轴文件：{timeline_path}（请先执行 Stage 6）")
    try:
        timeline = load_model(RenderTimeline, timeline_path)
    except Exception as exc:
        raise RenderError(f"时间轴解析失败：{timeline_path}（{exc}）") from exc

    settings = config or AppConfig()
    resolved_ffmpeg = str(ffmpeg_bin or settings.render.ffmpeg_bin)

    resolved_episode_id = str(episode_id or timeline.episode_id or f"ep_{int(episode_no):03d}")

    # ---- BGM：显式参数优先 → 作品级配置 → 默认素材（Patch v3.1 Patch 11 B4）----
    resolved_bgm: Path | None = None
    if bgm_path:
        resolved_bgm = Path(bgm_path).expanduser()
        if not resolved_bgm.is_absolute():
            resolved_bgm = series_dir / resolved_bgm
    else:
        configured = load_series_config(series_dir).bgm
        if configured:
            candidate = Path(configured).expanduser()
            resolved_bgm = candidate if candidate.is_absolute() else series_dir / candidate
        else:
            resolved_bgm = resolve_default_bgm(settings)
            if resolved_bgm is not None:
                print(f"[s7] 使用默认氛围音乐：{resolved_bgm}")

    out_dir = out_dir_for(series_dir, episode_no)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{resolved_episode_id}.mp4"
    work_dir = out_dir / "_work"

    # ---- Patch v4.5：作品级封页写字 + 双结尾图片包（底图缺失只跳过、不中断）----
    series_config = load_series_config(series_dir)
    episode_options = load_episode_render_options(episode_dir)
    effective = resolve_effective_branding(series_config, episode_options, series_dir)

    cover_composed: Path | None = None
    if effective.cover_enabled and effective.cover_image_path is not None:
        try:
            cover_composed = work_dir / "cover_composed.png"
            render_cover_image(
                effective.cover_image_path,
                cover_composed,
                episode_no=int(episode_no),
                cover_text=effective.cover_text,
                override_text=effective.cover_text_override,
                width=settings.video.width,
                height=settings.video.height,
            )
        except Exception as exc:  # noqa: BLE001 - 片包是增强项，失败只降级不阻断
            print(f"[s7] 警告：封页渲染失败，本话跳过片头：{exc}")
            cover_composed = None

    outro_composed: Path | None = None
    if effective.outro_mode != "none" and effective.outro_image_path is not None:
        try:
            outro_composed = work_dir / "outro_composed.png"
            render_outro_image(
                effective.outro_image_path,
                outro_composed,
                width=settings.video.width,
                height=settings.video.height,
            )
        except Exception as exc:  # noqa: BLE001 - 片包是增强项，失败只降级不阻断
            print(f"[s7] 警告：结尾图渲染失败，本话跳过片尾：{exc}")
            outro_composed = None

    # ---- Patch v4.7：按幕 BGM 多轨平滑混音（失败只降级，绝不阻断出片）----
    # 与 render_timeline 内部夹取口径保持一致：片包实际生效时长。
    eff_cover_ms = max(200, int(effective.cover_duration_ms)) if cover_composed else 0
    eff_outro_ms = max(200, int(effective.outro_duration_ms)) if outro_composed else 0
    bgm_pre_mixed = False
    mixed_bgm_path: Path | None = None
    mixed_bgm_gain = settings.render.bgm_gain_db
    try:
        storyboard = load_storyboard(storyboard_path_for(episode_dir))
        if storyboard is not None:
            spans = build_scene_bgm_spans(
                storyboard,
                timeline,
                series_dir=series_dir,
                series_config=series_config,
                settings=settings,
                cover_duration_ms=eff_cover_ms,
                outro_duration_ms=eff_outro_ms,
            )
            if spans:
                distinct_keys = {
                    (span.bgm_path, round(span.gain_db, 1)) for span in spans
                }
                has_silent = any(span.bgm_path is None for span in spans)
                if len(distinct_keys) >= 2 or has_silent:
                    extended_total_s = (
                        int(timeline.total_duration_ms or 0)
                        + eff_cover_ms
                        + eff_outro_ms
                    ) / 1000.0
                    mixed_bgm_path = work_dir / "multi_bgm.wav"
                    synthesized = synthesize_multi_bgm_wav(
                        spans,
                        total_s=extended_total_s,
                        out_path=mixed_bgm_path,
                        ffmpeg_bin=resolved_ffmpeg,
                    )
                    if synthesized is not None:
                        # 预合成轨已含各幕响度归一/增益/淡化，下游不再重复施加。
                        resolved_bgm = synthesized
                        mixed_bgm_gain = 0.0
                        bgm_pre_mixed = True
                        print(
                            f"[s7] 按幕 BGM 预合成：{len(spans)} 段、"
                            f"{len(distinct_keys)} 种曲目/增益，静音幕 "
                            f"{sum(1 for span in spans if span.bgm_path is None)}"
                        )
                    else:
                        # 全部幕都显式静音：本话不铺 BGM。
                        resolved_bgm = None
                else:
                    # 单一曲目单一增益：直接复用单曲链路，零额外合成开销。
                    only = spans[0]
                    resolved_bgm = only.bgm_path
                    mixed_bgm_gain = only.gain_db
    except Exception as exc:  # noqa: BLE001 - 按幕配乐是增强项，失败回退单曲逻辑
        print(f"[s7] 警告：按幕 BGM 处理失败，回退单曲配乐：{exc}")
        bgm_pre_mixed = False

    render_timeline(
        timeline,
        episode_dir=episode_dir,
        out_path=out_path,
        bgm_path=resolved_bgm,
        ffmpeg_bin=resolved_ffmpeg,
        background_blur=settings.render.background_blur,
        background_darken=settings.render.background_darken,
        bgm_gain_db=mixed_bgm_gain,
        work_dir=work_dir,
        enable_sfx=settings.render.enable_sfx,
        sfx_gain_db=settings.render.sfx_gain_db,
        cover_png=cover_composed,
        cover_duration_ms=effective.cover_duration_ms,
        cover_fade_in_ms=effective.cover_fade_in_ms,
        cover_fade_out_ms=effective.cover_fade_out_ms,
        outro_png=outro_composed,
        outro_duration_ms=effective.outro_duration_ms,
        outro_fade_in_ms=effective.outro_fade_in_ms,
        outro_fade_out_ms=effective.outro_fade_out_ms,
        bgm_fade_in_ms=effective.cover_fade_in_ms if cover_composed is not None else 0,
        bgm_fade_out_ms=effective.outro_fade_out_ms if outro_composed is not None else 0,
        bgm_pre_mixed=bgm_pre_mixed,
    )

    # 清理中间音频，避免污染 07_out。
    shutil.rmtree(work_dir, ignore_errors=True)

    # ---- 双份落盘：07_out/ + exports/（Patch v2.5-3：EP{话次:03d}_{安全标题}.mp4） ----
    resolved_title = title or _series_title(series_dir) or resolved_episode_id
    exports_dir = exports_dir_for(series_dir)
    exports_dir.mkdir(parents=True, exist_ok=True)
    export_path = exports_dir / f"EP{int(episode_no):03d}_{_safe_filename(resolved_title)}.mp4"
    shutil.copy2(out_path, export_path)
    return out_path


__all__ = [
    "ASSET_SUFFIX_PRIORITY",
    "ASSETS_DIR_NAME",
    "AUDIO_DIR_NAME",
    "BGM_DIR_NAME",
    "BgmSpan",
    "DEFAULT_BGM_CROSSFADE_MS",
    "DEFAULT_FPS",
    "DEFAULT_HEIGHT",
    "DEFAULT_SFX_GAIN_DB",
    "DEFAULT_WIDTH",
    "EXPORTS_DIR_NAME",
    "OUT_DIR_NAME",
    "PANEL_DIR_NAME",
    "PLAN_DIR_NAME",
    "SFX_DIR_NAME",
    "SUBTITLE_DIR_NAME",
    "TIMELINE_FILENAME",
    "RenderError",
    "build_scene_bgm_spans",
    "build_scene_filter",
    "exports_dir_for",
    "list_bgm_library",
    "out_dir_for",
    "plan_dir_for",
    "probe_duration_ms",
    "render_episode",
    "render_timeline",
    "resolve_audio_asset",
    "resolve_default_bgm",
    "resolve_scene_bgm_path",
    "resolve_sfx_asset",
    "run_ffmpeg",
    "synthesize_multi_bgm_wav",
]
