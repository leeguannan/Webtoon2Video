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
from pathlib import Path
from typing import Any

from core.config import AppConfig, load_series_config
from core.schemas import RenderTimeline, SubtitleLine, TimelineScene, load_model

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

#: BGM 闪避（``sidechaincompress``）参数。
#:
#: **threshold 为什么是 0.01 而不是 0.05**：阈值是线性幅度。百炼 TTS 的人声 RMS 实测约
#: ``-29.5 dBFS``（≈0.033），阈值 0.05（≈-26 dBFS）比人声的**平均电平还高**，
#: 于是只有人声峰值才触发压缩，整段闪避几乎失效（实测停顿段 BGM 只比人声段高 1.1 dB）。
#: 0.01（≈-40 dBFS）低于人声 RMS，人声一开口就压住 BGM。
#:
#: **release 为什么是 200ms**：句间停顿只有 250~400ms，release=300ms 时 BGM 在整段停顿里
#: 都还没恢复，听起来像「背景音被永久压住」。200ms 能在停顿里回升一截。
_SIDECHAIN_THRESHOLD = 0.01
_SIDECHAIN_RATIO = 8
_SIDECHAIN_ATTACK_MS = 10
_SIDECHAIN_RELEASE_MS = 200

#: 错误信息里保留的 stderr 末尾长度（够定位问题，又不会把整份 ffmpeg 日志塞进异常）。
_STDERR_TAIL_CHARS = 2000

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
) -> tuple[Path, Path | None]:
    """调用 A「备音」：产出人声轨与规范化 BGM，返回 ``(voice.wav, bgm.wav | None)``。

    **为什么先落盘再进成片调用**：``sidechaincompress`` 的侧链输入只能取自输入文件流，
    人声轨必须是真实文件才能作为闪避触发器。
    """
    voice_path = work_dir / "voice.wav"
    bgm_norm_path = work_dir / "bgm.wav"

    clips = list(timeline.audios or [])
    starts = _beat_start_map(timeline.scenes or [], clips)

    usable: list[tuple[Path, int]] = []
    for clip in clips:
        resolved = _resolve_audio_path(getattr(clip, "audio_path", ""), episode_dir)
        if resolved is None:
            # 缺音频不阻断渲染：画面照常出片，只是这句没有配音。
            print(f"[s7] 警告：找不到音频 {getattr(clip, 'audio_path', '')!r}，该 beat 静音处理")
            continue
        usable.append((resolved, int(starts.get(str(getattr(clip, "beat_id", "") or ""), 0))))

    args: list[str] = ["-y", "-hide_banner"]
    for path, _ in usable:
        args += ["-i", str(path)]
    bgm_index = len(usable)
    if bgm_path is not None:
        # -stream_loop -1 让短 BGM 循环铺满整条片，末尾由 atrim 精确截断。
        args += ["-stream_loop", "-1", "-i", str(bgm_path)]

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
        # BGM 在这里就降音量：规范要求的「volume=bgm_gain_db → sidechaincompress」顺序。
        chains.append(
            f"[{bgm_index}:a]aformat=sample_fmts=fltp:sample_rates={_AUDIO_RATE}:"
            f"channel_layouts={_AUDIO_LAYOUT},volume={float(bgm_gain_db):.1f}dB,"
            f"apad,atrim=0:{total_s:.3f}[bgm]"
        )

    args += ["-filter_complex", ";".join(chains)]
    args += ["-map", "[voice]", "-c:a", "pcm_s16le", "-ar", str(_AUDIO_RATE), "-ac", "2", str(voice_path)]
    if bgm_path is not None:
        args += ["-map", "[bgm]", "-c:a", "pcm_s16le", "-ar", str(_AUDIO_RATE), "-ac", "2", str(bgm_norm_path)]

    run_ffmpeg(args, ffmpeg_bin=ffmpeg_bin)
    return voice_path, (bgm_norm_path if bgm_path is not None else None)


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
    fps: int,
    width: int,
    height: int,
    background_blur: int,
    background_darken: float,
    out_path: Path,
    ffmpeg_bin: str,
) -> Path:
    """渲染一个分段，输出带音轨的独立 mp4。

    **分段的意义**：见 ``_MAX_SCENES_PER_SEGMENT`` 的说明——整话一个滤镜图会
    把内存峰值推到机器扛不住的量级（222 个输入时 swscale 直接失败）。

    段内时间一律相对 ``seg_start_ms``：字幕的 ``enable='between(t,…)'``
    用相对时间，音频用 ``-ss``/``-t`` 在输入端切片。
    """
    scene_count = len(scenes)
    seg_s = max(0.001, seg_len_ms / 1000.0)
    audio_start_s = max(0.0, seg_start_ms / 1000.0)

    # ---- 输入列表：背景 N 个 → 前景 N 个 → 字幕 M 个 →（可选 BGM）→ 人声 ----
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
    bgm_index = subtitle_base + len(subtitles)
    voice_index = bgm_index
    if bgm_path is not None:
        args += [
            "-ss", f"{audio_start_s:.3f}", "-t", f"{seg_s:.3f}", "-i", str(bgm_path),
        ]
        voice_index = bgm_index + 1
    args += ["-ss", f"{audio_start_s:.3f}", "-t", f"{seg_s:.3f}", "-i", str(voice_path)]

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

    # 字幕逐条 overlay：串行叠加（overlay 的副输入不能是图内 label 的多路并联）。
    video_label = "vcat"
    for offset, (line, _png, offset_x, offset_y) in enumerate(subtitles):
        chains.append(f"[{subtitle_base + offset}:v]format=rgba[sub{offset}]")
        start = max(0.0, (float(line.start_ms) - seg_start_ms) / 1000.0)
        end = min(seg_s, max(start + 0.05, (float(line.end_ms) - seg_start_ms) / 1000.0))
        next_label = f"vsub{offset}"
        chains.append(
            f"[{video_label}][sub{offset}]overlay={offset_x}:{offset_y}:"
            f"enable='between(t,{start:.3f},{end:.3f})'[{next_label}]"
        )
        video_label = next_label

    # ---- 音频链 ----
    if bgm_path is not None:
        # 两个输入都写成「输入文件流」：sidechaincompress 的 request pad 不接受图内 label。
        chains.append(
            f"[{bgm_index}:a][{voice_index}:a]sidechaincompress="
            f"threshold={_SIDECHAIN_THRESHOLD}:ratio={_SIDECHAIN_RATIO}:"
            f"attack={_SIDECHAIN_ATTACK_MS}:release={_SIDECHAIN_RELEASE_MS}[duck]"
        )
        chains.append(f"[duck]apad,atrim=0:{seg_s:.3f}[bgmv]")
        chains.append(
            f"[{voice_index}:a]aformat=sample_fmts=fltp:sample_rates={_AUDIO_RATE}:"
            f"channel_layouts={_AUDIO_LAYOUT}[vin]"
        )
        chains.append("[vin][bgmv]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        chains.append(
            f"[{voice_index}:a]aformat=sample_fmts=fltp:sample_rates={_AUDIO_RATE}:"
            f"channel_layouts={_AUDIO_LAYOUT},apad,atrim=0:{seg_s:.3f}[aout]"
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
        "-t", f"{seg_s:.3f}",
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
) -> Path:
    """按时间轴渲染成片，返回成片路径。

    流程：调用 A 备音（人声 + BGM）→ 调用 B 成片（画面 + 字幕 + 闪避 + 混音）。

    * 画面：每个场景一条滤镜片段 → ``concat`` 串成一条；
    * 字幕：``06_plan/subtitles/{index:04d}.png`` 逐条 ``overlay``，
      ``enable='between(t,start,end)'`` 控制显示窗口；
    * BGM：``sidechaincompress``（侧链 = 人声轨）做闪避，再与人声 ``amix`` 混合。
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
    total_s = total_ms / 1000.0

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

    voice_path, bgm_norm_path = _prepare_audio(
        timeline,
        episode_dir=episode_dir,
        work_dir=work_dir,
        total_s=total_s,
        bgm_path=bgm_input,
        bgm_gain_db=bgm_gain_db,
        ffmpeg_bin=ffmpeg_bin,
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
            fps=fps,
            width=width,
            height=height,
            background_blur=background_blur,
            background_darken=background_darken,
            out_path=seg_out,
            ffmpeg_bin=ffmpeg_bin,
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

    # ---- BGM：显式参数优先，其次作品级配置 ----
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

    out_dir = out_dir_for(series_dir, episode_no)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{resolved_episode_id}.mp4"
    work_dir = out_dir / "_work"

    render_timeline(
        timeline,
        episode_dir=episode_dir,
        out_path=out_path,
        bgm_path=resolved_bgm,
        ffmpeg_bin=resolved_ffmpeg,
        background_blur=settings.render.background_blur,
        background_darken=settings.render.background_darken,
        bgm_gain_db=settings.render.bgm_gain_db,
        work_dir=work_dir,
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
    "AUDIO_DIR_NAME",
    "DEFAULT_FPS",
    "DEFAULT_HEIGHT",
    "DEFAULT_WIDTH",
    "EXPORTS_DIR_NAME",
    "OUT_DIR_NAME",
    "PANEL_DIR_NAME",
    "PLAN_DIR_NAME",
    "SUBTITLE_DIR_NAME",
    "TIMELINE_FILENAME",
    "RenderError",
    "build_scene_filter",
    "exports_dir_for",
    "out_dir_for",
    "plan_dir_for",
    "probe_duration_ms",
    "render_episode",
    "render_timeline",
    "run_ffmpeg",
]
