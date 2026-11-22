"""Stage 6：Audio-First 时间轴编排 + 字幕产出。

规范要点（``WEBTOON2VIDEO_DEV_SPEC.md`` §4.2 / §4.3 + 铁律）：

* **Audio-First**：成片长度由**已合成的音频时长**决定，不由「预估每句几秒」决定。
  时间轴按 Stage 5 产出的音频顺序串行排布，每句之间保留该句的 ``pause_after_ms``。
* **画幅锁定 9:16 / 1080×1920 / 30fps**，本模块不提供其它画幅。
* **字幕用 ``narration_text`` 原样**（多音字替换只存在于 ``tts_text``，
  Stage 5 已保证分离；本模块绝不做任何文本替换）。

产物（``episodes/ep_XXX/06_plan/``）::

    timeline.json      # RenderTimeline（Stage 7 的唯一输入）
    subtitles.ass      # 外挂字幕（外挂播放器 / 剪辑软件 / 平台上传）
    subtitles/0001.png # 逐条字幕透明 PNG（Stage 7 用 overlay 烧录）

**为什么 ASS 与 PNG 双轨**：本机 ffmpeg 没有 libass，``subtitles`` 滤镜不可用，
所以成片字幕由 PNG overlay 完成；但 ASS 文件是「外挂字幕交付物」的正式形态
（平台上传、剪辑二次加工都用它），两条轨道互不依赖，缺一不影响另一条。
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core.panel_ids import PanelIdResolver, trailing_number
from core.schemas import (
    AudioClipMeta,
    CameraMotion,
    NarrationScript,
    PanelMeta,
    RenderTimeline,
    SfxEvent,
    SubtitleLine,
    TimelineScene,
    VisionWindowOutput,
    load_model,
    save_model,
)
from core.subtitle_render import (
    DEFAULT_FONT_NAME,
    SubtitleRenderError,
    render_subtitle_png,
    resolve_font_path,
    subtitle_filename,
)

#: Stage 6 的目录名（对应 ``core.manifest.STAGE_DIR_NAMES["s6_timeline"]``）。
PLAN_DIR_NAME = "06_plan"
#: 字幕 PNG 子目录（位于 ``06_plan/subtitles/``）。
SUBTITLE_DIR_NAME = "subtitles"
#: Stage 5 音频目录。
AUDIO_DIR_NAME = "05_audio"
#: Stage 2 面板目录。
PANEL_DIR_NAME = "02_panels"
#: Stage 4 解说稿目录。
SCRIPT_DIR_NAME = "04_script"
#: Stage 3 视觉结果目录。
VISION_DIR_NAME = "03_vision"

#: 时间轴文件名。
TIMELINE_FILENAME = "timeline.json"
#: 外挂字幕文件名。
ASS_FILENAME = "subtitles.ass"
#: Stage 5 音频清单文件名。
AUDIO_MANIFEST_FILENAME = "audio_manifest.json"
#: Stage 3 视觉结果文件名。
VISION_FILENAME = "vision.json"

#: 参与画面匹配的面板图片后缀。
PANEL_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})

#: 参与音频兜底扫描的音频后缀。
AUDIO_SUFFIXES = frozenset({".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"})

#: ``pause_after_ms`` 的缺省值（Stage 5 未写该字段时使用）。
DEFAULT_PAUSE_MS = 250

#: 锁定的成片画幅（铁律：只出 9:16）。
DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_FPS = 30

#: ASS 样式里的默认边距（Patch v3.0-8：抖音安全分区，字幕锚定底部纯暗区）。
ASS_MARGIN_L = 65
ASS_MARGIN_R = 65
ASS_MARGIN_V = 250
#: Patch v3.0-8：字幕字号与 PNG 左右边距（与 ASS 的 MarginL/MarginR 对齐）。
ASS_FONT_SIZE = 56
PNG_MARGIN_H = 65

#: Patch v2.6-5：``focus_hint`` → 取景带（相对坐标 ``x, y, w, h``）。
#: ``full`` 不在此表中——它沿用 Stage 3 的主体框，渲染端不裁切。
FOCUS_BANDS: dict[str, tuple[float, float, float, float]] = {
    "top": (0.0, 0.0, 1.0, 0.5),
    "bottom": (0.0, 0.5, 1.0, 1.0),
    "center": (0.0, 0.25, 1.0, 0.75),
}

#: 尾部数字（``panel_001`` / ``ep_001_p001`` / ``p001`` → 1）。
#: 统一走 :mod:`core.panel_ids`，Stage 4 的顺位校验用同一套规则。
_TRAILING_NUMBER = re.compile(r"(\d+)\s*$")

# --------------------------------------------------------------------------
# 音效事件（Patch v3.1 Patch 11 B3）
# --------------------------------------------------------------------------
#: 任意两条音效事件之间的最小间隔（毫秒）。
#:
#: 为什么要有下限：音效是「点睛」，不是「背景」；一话里连着响会把解说压没。
SFX_MIN_GAP_MS = 6000

#: 单话音效事件数量上限。
SFX_MAX_EVENTS = 8

#: 保守关键词 → 音效键的兜底映射（只认 Stage 3 明确写出的音效描述）。
#:
#: 只在 beat 没写 ``sfx_cue`` 时启用，且必须命中关键词才触发，
#: 避免把普通画面描述（如「安静的走廊」）误判成音效。
#:
#: Patch v3.2 Patch 14：按规格扩充到 7 组（繁简双形都收），匹配为子串包含且
#: 按本字典插入顺序取第一个命中的组，故把最专属的词放在靠前位置。
#: 节流（``SFX_MIN_GAP_MS``=6000ms）与上限（``SFX_MAX_EVENTS``=8）不变。
SFX_KEYWORD_MAP: dict[str, tuple[str, ...]] = {
    "heart_thump": ("心驚", "心惊", "心跳", "心悸", "驚", "惊", "顫", "颤", "抖"),
    "door_slam": ("关门", "關門", "摔门", "砰", "啪", "甩開", "甩开", "撞"),
    "whoosh": ("嘎咿", "吱", "開門", "开门", "刷", "咻"),
    "phone_msg": (
        "短信",
        "消息",
        "叮",
        "鈴",
        "铃",
        "嘟",
        "嗡",
        "訊息",
        "讯息",
        "手機",
        "手机",
    ),
    "footstep": ("脚步", "腳步", "踏", "咚", "噠", "哒", "嗒"),
    "gasp": ("倒吸", "喘", "哈"),
    "crash": ("碎", "嘩啦", "哗啦", "爆"),
}


class TimelineError(RuntimeError):
    """Stage 6 时间轴编排失败。"""


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
def _episode_dir(series_dir: str | Path, episode_no: int) -> Path:
    """单话根目录：``{series_dir}/episodes/ep_{no:03d}``。"""
    return Path(series_dir) / "episodes" / f"ep_{episode_no:03d}"


def plan_dir_for(series_dir: str | Path, episode_no: int) -> Path:
    """Stage 6 计划目录：``episodes/ep_XXX/06_plan``。"""
    return _episode_dir(series_dir, episode_no) / PLAN_DIR_NAME


def audio_dir_for(series_dir: str | Path, episode_no: int) -> Path:
    """Stage 5 音频目录：``episodes/ep_XXX/05_audio``。"""
    return _episode_dir(series_dir, episode_no) / AUDIO_DIR_NAME


def panel_dir_for(series_dir: str | Path, episode_no: int) -> Path:
    """Stage 2 面板目录：``episodes/ep_XXX/02_panels``。"""
    return _episode_dir(series_dir, episode_no) / PANEL_DIR_NAME


def script_path_for(series_dir: str | Path, episode_no: int) -> Path:
    """Stage 4 解说稿路径：``episodes/ep_XXX/04_script/script.json``。"""
    return _episode_dir(series_dir, episode_no) / SCRIPT_DIR_NAME / "script.json"


def vision_path_for(series_dir: str | Path, episode_no: int) -> Path:
    """Stage 3 视觉结果路径：``episodes/ep_XXX/03_vision/vision.json``。"""
    return _episode_dir(series_dir, episode_no) / VISION_DIR_NAME / VISION_FILENAME


def audio_manifest_path_for(series_dir: str | Path, episode_no: int) -> Path:
    """Stage 5 音频清单路径：``episodes/ep_XXX/05_audio/audio_manifest.json``。"""
    return audio_dir_for(series_dir, episode_no) / AUDIO_MANIFEST_FILENAME


def subtitle_dir_for(series_dir: str | Path, episode_no: int) -> Path:
    """字幕 PNG 目录：``episodes/ep_XXX/06_plan/subtitles``。"""
    return plan_dir_for(series_dir, episode_no) / SUBTITLE_DIR_NAME


def subtitle_png_path_for(series_dir: str | Path, episode_no: int, index: int) -> Path:
    """第 ``index`` 条字幕的 PNG 路径（``06_plan/subtitles/0001.png``）。

    给 Stage 7 用：拿到 ``SubtitleLine.index`` 就能直接定位对应的透明 PNG，
    不必再自己拼路径（避免两边对命名规则理解不一致）。
    """
    return subtitle_dir_for(series_dir, episode_no) / subtitle_filename(index)


# --------------------------------------------------------------------------
# 时间换算小工具
# --------------------------------------------------------------------------
def frame_ms(fps: int = DEFAULT_FPS) -> int:
    """一帧的毫秒栅格（``1000 // fps``；30fps → 33ms）。

    为什么用 ``1000 // fps`` 而不是 ``1000 / fps``：毫秒是整数时间戳，
    用整除栅格才能保证「相邻切点永远落在同一张时间栅格上」，
    Stage 7 用 ``round(ms * fps / 1000)`` 反算帧号时误差恒定。
    """
    return max(1, 1000 // max(1, int(fps)))


def ms_to_frames(ms: int | float, fps: int = DEFAULT_FPS) -> int:
    """毫秒 → 帧号（四舍五入）。"""
    return int(round(float(ms) * max(1, int(fps)) / 1000.0))


def frames_to_ms(frames: int | float, fps: int = DEFAULT_FPS) -> int:
    """帧号 → 毫秒（四舍五入）。"""
    return int(round(float(frames) * 1000.0 / max(1, int(fps))))


def frame_count(timeline: RenderTimeline, fps: int = DEFAULT_FPS) -> int:
    """时间轴总帧数（**向上取整**，保证尾帧不被截断）。"""
    if timeline is None:
        return 0
    fps = int(timeline.fps or fps)
    return int(math.ceil(float(timeline.total_duration_ms) * fps / 1000.0))


def align_up(ms: int, fps: int = DEFAULT_FPS) -> int:
    """向上对齐到帧栅格（保证不与上一段重叠）。"""
    grid = frame_ms(fps)
    return int(math.ceil(int(ms) / grid) * grid)


def align_down(ms: int, fps: int = DEFAULT_FPS) -> int:
    """向下对齐到帧栅格。"""
    grid = frame_ms(fps)
    return int(math.floor(int(ms) / grid) * grid)


# --------------------------------------------------------------------------
# 面板索引
# --------------------------------------------------------------------------
def _trailing_number(value: str) -> int | None:
    """取字符串尾部数字（``panel_001`` → 1）；没有数字返回 ``None``。"""
    return trailing_number(value)


def _panel_stem(panel: PanelMeta) -> str:
    """面板的规范标识（图片文件名主干，如 ``p_001``）。

    为什么不用 ``PanelMeta.panel_id``：Stage 2 与 Stage 3 的 ID 历史上有过
    多套写法（``ep_001_p001`` / ``panel_001`` / ``p_001``），以**图片文件名
    主干**为准可以在两个阶段之间对齐，也是 Stage 7 找图的标准键。
    """
    image_path = getattr(panel, "image_path", "") or ""
    if image_path:
        return Path(image_path).stem
    return str(getattr(panel, "panel_id", "") or "")


class PanelIndex:
    """按多种 ID 形式查找面板（``p_001`` / ``panel_001`` / ``ep_001_p001``）。

    解析规则统一委托给 :mod:`core.panel_ids`，与 Stage 4 的顺位校验
    ``validate_beat_alignment`` 共用同一套归一化逻辑。
    """

    def __init__(self, panels: Sequence[PanelMeta]) -> None:
        self.panels: list[PanelMeta] = list(panels)
        self._by_stem: dict[str, PanelMeta] = {}
        for panel in self.panels:
            stem = _panel_stem(panel)
            if stem:
                self._by_stem.setdefault(stem, panel)
        self._resolver = PanelIdResolver(list(self._by_stem.keys()))

    def __bool__(self) -> bool:
        return bool(self.panels)

    def resolve(self, panel_id: str | None) -> PanelMeta | None:
        """按 ID 查面板；查不到返回 ``None``（调用方负责复用上一张）。"""
        if not panel_id:
            return None
        key = str(panel_id).strip()
        if not key:
            return None
        canonical = self._resolver.resolve(key)
        if canonical is None:
            return None
        return self._by_stem.get(canonical)

    def canonical_id(self, panel: PanelMeta) -> str:
        """面板的规范 ID（``p_001``）。"""
        return _panel_stem(panel)


# --------------------------------------------------------------------------
# 工作区读取
# --------------------------------------------------------------------------
def scan_panels(panel_dir: str | Path) -> list[PanelMeta]:
    """扫描 Stage 2 面板目录，返回 ``PanelMeta`` 列表（按文件名排序）。"""
    root = Path(panel_dir)
    if not root.is_dir():
        return []
    paths = sorted(
        (
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.lower() in PANEL_IMAGE_SUFFIXES
        ),
        key=lambda path: path.name,
    )
    panels: list[PanelMeta] = []
    for path in paths:
        width, height = _image_size(path)
        panels.append(
            PanelMeta(
                panel_id=path.stem,
                image_path=str(path.resolve()),
                width=width,
                height=height,
                aspect_ratio=(width / height) if height else 0.0,
                source_slice_range=(0, 0),
                is_tall_panel=bool(height and width and height >= 2 * width),
            )
        )
    return panels


def _image_size(path: Path) -> tuple[int, int]:
    """读取图片尺寸；失败时返回 ``(0, 0)``（不因尺寸问题中断流水线）。"""
    try:
        from PIL import Image  # 局部导入：仅在需要读尺寸时依赖 Pillow

        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:  # pragma: no cover - 损坏图片属于极端情况
        return 0, 0


def wav_duration_ms(path: str | Path) -> int:
    """读取 WAV 文件头计算真实时长（毫秒）。

    **为什么需要它**：``audio_manifest.json`` 缺失时，仍可以从已合成的 wav
    反推真实时长，满足「Audio-First：时长由音频决定」的底线（宁可读头，也不要瞎估）。
    """
    target = Path(path)
    try:
        with target.open("rb") as handle:
            header = handle.read(12)
            if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                raise TimelineError(f"不是合法的 WAV 文件：{target}")
            byte_rate: int | None = None
            data_size: int | None = None
            while True:
                chunk_header = handle.read(8)
                if len(chunk_header) < 8:
                    break
                chunk_id = chunk_header[:4]
                chunk_size = int.from_bytes(chunk_header[4:8], "little")
                if chunk_id == b"fmt ":
                    fmt = handle.read(chunk_size)
                    if len(fmt) >= 16:
                        byte_rate = int.from_bytes(fmt[8:12], "little")
                elif chunk_id == b"data":
                    data_size = chunk_size
                    break
                else:
                    handle.seek(chunk_size + (chunk_size & 1), 1)
            if byte_rate and data_size is not None:
                return int(round(data_size * 1000.0 / byte_rate))
    except TimelineError:
        raise
    except OSError as exc:
        raise TimelineError(f"读取 WAV 头失败：{target}（{exc}）") from exc
    raise TimelineError(f"WAV 文件缺少 fmt/data 块，无法计算时长：{target}")


def _parse_audio_items(data: Any) -> list[dict[str, Any]]:
    """从音频清单 JSON 里取出音频条目列表（兼容多种外层结构）。"""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("audios", "audio_clips", "clips", "items", "audio_manifest"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    raise TimelineError(
        "音频清单结构无法识别（应为数组，或包含 audios 数组的对象）"
    )


def parse_audio_manifest(path: str | Path) -> list[AudioClipMeta]:
    """解析 ``audio_manifest.json`` 为 ``AudioClipMeta`` 列表。

    容错点：

    * 缺 ``pause_after_ms`` → 补 ``250``（Stage 5 的默认停顿）；
    * ``duration_ms`` 缺失或为 0 → 尝试用对应 wav 文件的头信息算真实时长。
    """
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise TimelineError(f"读取音频清单失败：{target}（{exc}）") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TimelineError(f"音频清单不是合法 JSON：{target}（{exc}）") from exc

    items = _parse_audio_items(data)
    clips: list[AudioClipMeta] = []
    for index, item in enumerate(items, start=1):
        payload = dict(item)
        payload.setdefault("beat_id", f"beat_{index:03d}")
        if payload.get("pause_after_ms") is None:
            payload["pause_after_ms"] = DEFAULT_PAUSE_MS
        try:
            clip = AudioClipMeta.model_validate(payload)
        except Exception as exc:
            raise TimelineError(f"音频清单条目非法（第 {index} 条）：{exc}") from exc
        if clip.duration_ms <= 0 and clip.audio_path:
            audio_path = Path(clip.audio_path)
            if not audio_path.is_absolute():
                audio_path = target.parent / audio_path
            if audio_path.is_file() and audio_path.suffix.lower() == ".wav":
                clip = clip.model_copy(update={"duration_ms": wav_duration_ms(audio_path)})
        clips.append(clip)
    return clips


def audio_clips_from_files(audio_dir: str | Path) -> list[AudioClipMeta]:
    """兜底：没有音频清单时，直接扫描目录里的音频文件算时长。

    只有 wav 能读到精确时长（无第三方解码库），所以 mp3 等格式会被跳过；
    找不到任何可用音频时返回空列表，由调用方抛 :class:`TimelineError`。
    """
    root = Path(audio_dir)
    if not root.is_dir():
        return []
    clips: list[AudioClipMeta] = []
    paths = sorted(
        (path for path in root.iterdir() if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES),
        key=lambda path: path.name,
    )
    for path in paths:
        if path.suffix.lower() != ".wav":
            continue
        try:
            duration = wav_duration_ms(path)
        except TimelineError:
            continue
        clips.append(
            AudioClipMeta(
                beat_id=path.stem,
                audio_path=str(path.resolve()),
                duration_ms=duration,
                pause_after_ms=DEFAULT_PAUSE_MS,
            )
        )
    return clips


def load_audio_manifest(series_dir: str | Path, episode_no: int) -> list[AudioClipMeta]:
    """加载本话的音频清单（缺失时尝试用 wav 头兜底，都没有则报错）。"""
    manifest_path = audio_manifest_path_for(series_dir, episode_no)
    if manifest_path.is_file():
        return parse_audio_manifest(manifest_path)
    clips = audio_clips_from_files(audio_dir_for(series_dir, episode_no))
    if clips:
        return clips
    raise TimelineError(
        f"缺少音频清单：{manifest_path}"
        f"（且 {audio_dir_for(series_dir, episode_no)} 下没有可用的 wav 音频文件，"
        "无法按 Audio-First 规则确定时间轴长度）"
    )


def load_focus_regions(
    series_dir: str | Path, episode_no: int
) -> dict[str, tuple[float, float, float, float]]:
    """读取 Stage 3 的 ``focus_region``（可选，缺失就返回空映射）。

    ``focus_region`` 描述画面里最值得停留的区域，Stage 7 做运镜时可据此微调；
    缺失不影响时间轴编排。
    """
    path = vision_path_for(series_dir, episode_no)
    if not path.is_file():
        return {}
    try:
        vision = load_model(VisionWindowOutput, path)
    except Exception:
        return {}
    regions: dict[str, tuple[float, float, float, float]] = {}
    for panel in vision.panels:
        if panel.focus_region:
            regions[str(panel.panel_id)] = tuple(float(v) for v in panel.focus_region)
    return regions


# --------------------------------------------------------------------------
# 音效事件（Patch v3.1 Patch 11 B3）
# --------------------------------------------------------------------------
def load_panel_sfx_lists(
    series_dir: str | Path, episode_no: int
) -> dict[str, list[str]]:
    """读取 Stage 3 的 ``sfx_list``（可选，缺失就返回空映射）。

    这是音效事件的**兜底**来源：beat 自己没写 ``sfx_cue`` 时，
    才来这里按保守关键词猜（Patch v3.1 Patch 11 B3）。
    """
    path = vision_path_for(series_dir, episode_no)
    if not path.is_file():
        return {}
    try:
        vision = load_model(VisionWindowOutput, path)
    except Exception:
        return {}
    result: dict[str, list[str]] = {}
    for panel in vision.panels:
        items = [
            str(item)
            for item in (getattr(panel, "sfx_list", None) or [])
            if str(item).strip()
        ]
        if items:
            result[str(panel.panel_id)] = items
    return result


def resolve_sfx_key(
    beat: Any,
    panel_sfx_map: Mapping[str, Sequence[str]] | None = None,
) -> str | None:
    """决定某条 beat 触发哪个音效（Patch v3.1 Patch 11 B3）。

    主来源是 beat 自己写的 ``sfx_cue``；没写（或写成 ``none``）时，
    才用画面 ``sfx_list`` 的关键词兜底，且必须命中关键词才触发。
    """
    cue = str(getattr(beat, "sfx_cue", "") or "").strip().lower()
    if cue and cue != "none":
        return cue
    if not panel_sfx_map:
        return None

    resolver = PanelIdResolver(panel_sfx_map.keys())
    for raw_id in getattr(beat, "target_panel_ids", []) or []:
        panel_id = str(raw_id)
        items = panel_sfx_map.get(panel_id)
        if items is None:
            canonical = resolver.resolve(panel_id)
            items = panel_sfx_map.get(canonical) if canonical else None
        if not items:
            continue
        for item in items:
            text = str(item)
            for key, keywords in SFX_KEYWORD_MAP.items():
                if any(keyword in text for keyword in keywords):
                    return key
    return None


def select_sfx_events(
    candidates: Sequence[tuple[int, str]],
    *,
    min_gap_ms: int = SFX_MIN_GAP_MS,
    max_events: int = SFX_MAX_EVENTS,
) -> list[SfxEvent]:
    """把候选音效按时间排序后做节流（最小间隔 + 数量上限）。

    * 任意两条保留的事件间隔必须 ``>= min_gap_ms``（默认 6000ms）；
    * 整话最多 ``max_events`` 条（默认 8 条）。

    这两条限制保证音效是「点睛」而不是「背景」——连着响会把解说压没。
    """
    ordered = sorted(candidates, key=lambda item: (int(item[0]), str(item[1])))
    events: list[SfxEvent] = []
    last_start: int | None = None
    limit = max(0, int(max_events))
    for start_ms, key in ordered:
        if len(events) >= limit:
            break
        name = str(key or "").strip().lower()
        if not name or name == "none":
            continue
        start = int(start_ms)
        if last_start is not None and start - last_start < int(min_gap_ms):
            continue
        events.append(SfxEvent(sfx_key=name, start_ms=start))
        last_start = start
    return events


# --------------------------------------------------------------------------
# 时间轴编排
# --------------------------------------------------------------------------
def text_card_panel_ids(series_dir: str | Path, episode_no: int) -> set[str]:
    """判定本话哪些面板是「纯文字卡」（章节标题条 / 白底黑字 PPT）。

    读 Stage 3 的 ``03_vision/vision.json``，按**内容**判据识别（而不是模型
    自己打的 ``is_pure_text_card`` 标记——模型常常漏标）。

    读不到 / 解析失败一律返回空集合：宁可放过，也不要因为旧产物格式变化
    把整话的时间轴判废。
    """
    path = vision_path_for(series_dir, episode_no)
    if not path.is_file():
        return set()
    try:
        vision = load_model(VisionWindowOutput, path)
    except Exception:  # noqa: BLE001 - 视觉结果读不到就不过滤
        return set()
    try:
        from stages.s3_vision import is_text_only_card
    except Exception:  # noqa: BLE001 - 同上
        return set()
    return {
        panel.panel_id
        for panel in vision.panels
        if panel.panel_id and is_text_only_card(panel)
    }


def build_timeline(
    *,
    episode_id: str,
    script: NarrationScript,
    audios: Sequence[AudioClipMeta],
    panels: Sequence[PanelMeta],
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    fps: int = DEFAULT_FPS,
    focus_regions: Mapping[str, tuple[float, float, float, float]] | None = None,
    panel_sfx_map: Mapping[str, Sequence[str]] | None = None,
) -> RenderTimeline:
    """按 Audio-First 规则编排时间轴。

    **分配规则**

    1. 音频按 ``audios`` 的顺序串行排布（Stage 5 的产出顺序即成片播放顺序）；
    2. 第 i 段的起点 = 上一段终点 + 上一段 ``pause_after_ms``（停顿落在下一句之前），
       起点向上对齐到帧栅格（30fps → 33ms），保证串行且不重叠；
    3. 某条 beat 的 ``target_panel_ids`` 有多张面板时，该 beat 的画面时长**按面板数平均分**，
       每段画面至少 1 帧（``1000 // fps`` 毫秒）；面板不够分时自动减少画面数量；
    4. beat 匹配不到面板时**复用上一张面板**，保持画面连续，不产生空 scene；
    5. 字幕一条 beat 一条，文本取 ``narration_text`` **原样**，起止时间与该 beat 的音频一致。

    **帧对齐取舍**：只有「切点」（每句起点、每段画面边界）落在 33ms 栅格上，
    音频/字幕的**终点**保留真实毫秒（``start + 真实时长``），
    这样字幕与语音严格对齐，同时渲染端按帧切片不会抖动。
    """
    fps = max(1, int(fps))
    grid = frame_ms(fps)
    width = int(width)
    height = int(height)

    beats_by_id: dict[str, Any] = {}
    for beat in getattr(script, "beats", []) or []:
        beat_id = str(getattr(beat, "beat_id", "") or "")
        if beat_id:
            beats_by_id[beat_id] = beat

    index = PanelIndex(panels)
    if not index:
        raise TimelineError("没有可用面板，无法编排时间轴（Stage 2 面板目录为空）")

    regions: dict[str, tuple[float, float, float, float]] = {
        str(key): tuple(float(v) for v in value)  # type: ignore[misc]
        for key, value in (focus_regions or {}).items()
    }

    # ---- 第一轮：按音频串行规划每个 beat 的时间区间与画面分配 ----
    plans: list[dict[str, Any]] = []
    previous_panel: PanelMeta | None = None
    cursor = 0
    last_end = 0
    last_pause = 0

    for audio in audios:
        beat = beats_by_id.get(str(audio.beat_id))
        duration = max(0, int(audio.duration_ms))
        pause = max(0, int(audio.pause_after_ms or 0))
        if duration <= 0:
            # 没有真实时长的音频段无法排布（Audio-First 不允许用估值兜底）。
            cursor += pause
            continue

        beat_start = align_up(cursor, fps)
        beat_end = beat_start + duration

        # ---- 选画面 ----
        chosen: list[PanelMeta] = []
        if beat is not None:
            for panel_id in getattr(beat, "target_panel_ids", []) or []:
                panel = index.resolve(panel_id)
                if panel is not None:
                    chosen.append(panel)
        if not chosen:
            # 没有可用面板 → 复用上一张，保持画面连续（不产生空 scene）。
            chosen = [previous_panel] if previous_panel is not None else [index.panels[0]]

        # ---- 该 beat 内部按帧切分画面（每段至少 1 帧）----
        total_frames = max(1, duration // grid)
        count = max(1, min(len(chosen), total_frames))
        chosen = chosen[:count]
        base, remainder = divmod(total_frames, count)
        scene_frames = [base + (1 if i < remainder else 0) for i in range(count)]

        plans.append(
            {
                "audio": audio,
                "beat": beat,
                "start": beat_start,
                "end": beat_end,
                "panels": chosen,
                "frames": scene_frames,
            }
        )
        previous_panel = chosen[-1]
        last_end = beat_end
        last_pause = pause
        cursor = beat_end + pause

    # ---- 第二轮：生成 scene 与字幕 ----
    # 总时长 = 末段音频终点 + 末段停顿（停顿也是成片的一部分，不能凭空消失）。
    total_duration = last_end + last_pause if plans else 0
    scenes: list[TimelineScene] = []
    subtitles: list[SubtitleLine] = []
    for order, plan in enumerate(plans):
        beat_start = int(plan["start"])
        beat_end = int(plan["end"])
        audio = plan["audio"]
        beat = plan["beat"]
        panels_of_beat: list[PanelMeta] = plan["panels"]
        frames_of_beat: list[int] = plan["frames"]

        offset = 0
        for order_in_beat, (panel, frames) in enumerate(zip(panels_of_beat, frames_of_beat)):
            scene_start = beat_start + offset
            offset += int(frames) * grid
            if order_in_beat == len(frames_of_beat) - 1:
                # 最后一个画面延伸到「下一句的起点」：停顿期间画面不中断，避免黑屏；
                # 整条时间轴的最后一段延伸到总时长，保证画面覆盖到片尾。
                next_start = (
                    int(plans[order + 1]["start"])
                    if order + 1 < len(plans)
                    else total_duration
                )
                scene_end = max(next_start, scene_start + grid)
            else:
                scene_end = scene_start + int(frames) * grid
            focus_region, focus_band = _focus_for_beat(
                beat, regions, panel, index
            )
            scenes.append(
                TimelineScene(
                    scene_index=len(scenes),
                    beat_id=str(audio.beat_id),
                    panel_id=index.canonical_id(panel),
                    image_path=str(getattr(panel, "image_path", "") or ""),
                    start_ms=scene_start,
                    end_ms=scene_end,
                    duration_ms=scene_end - scene_start,
                    motion=_motion_of(beat),
                    focus_region=focus_region,
                    focus_band=focus_band,
                )
            )

        # ---- 字幕：一条 beat 一条，文本取 narration_text 原样 ----
        if beat is not None:
            narration = str(getattr(beat, "narration_text", "") or "")
            if narration:
                subtitles.append(
                    SubtitleLine(
                        index=len(subtitles) + 1,
                        beat_id=str(audio.beat_id),
                        text=narration,
                        start_ms=beat_start,
                        end_ms=beat_end,
                    )
                )

    total_duration = last_end + last_pause if plans else 0

    # ---- 音效事件（Patch v3.1 Patch 11 B3）----
    # 主来源：beat.sfx_cue；兜底：画面 sfx_list 的保守关键词。
    sfx_candidates: list[tuple[int, str]] = []
    for plan in plans:
        beat = plan["beat"]
        if beat is None:
            continue
        key = resolve_sfx_key(beat, panel_sfx_map)
        if key:
            sfx_candidates.append((int(plan["start"]), key))
    sfx_events = select_sfx_events(sfx_candidates)

    return RenderTimeline(
        episode_id=episode_id,
        width=width,
        height=height,
        fps=fps,
        total_duration_ms=total_duration,
        scenes=scenes,
        audios=list(audios),
        subtitles=subtitles,
        sfx_events=sfx_events,
    )


def _motion_of(beat: Any) -> CameraMotion:
    """取 beat 的运镜；缺失时回退 ``static``。"""
    motion = getattr(beat, "motion", None) if beat is not None else None
    if isinstance(motion, CameraMotion):
        return motion
    if isinstance(motion, str) and motion:
        try:
            return CameraMotion(motion)
        except ValueError:
            return CameraMotion.STATIC
    return CameraMotion.STATIC


def _focus_for(
    regions: Mapping[str, tuple[float, float, float, float]],
    panel: PanelMeta,
    index: PanelIndex,
) -> tuple[float, float, float, float] | None:
    """按「规范 ID → 原始 panel_id」的顺序查 ``focus_region``（查不到就 None）。"""
    for key in (
        index.canonical_id(panel),
        str(getattr(panel, "panel_id", "") or ""),
    ):
        if key and key in regions:
            return regions[key]
    return None


def _focus_for_beat(
    beat: Any,
    regions: Mapping[str, tuple[float, float, float, float]],
    panel: PanelMeta,
    index: PanelIndex,
) -> tuple[tuple[float, float, float, float] | None, bool]:
    """按 beat 的 ``focus_hint`` 决定镜头取景带（Patch v2.6-5）。

    * ``focus_hint="full"``：沿用 Stage 3 的主体框（``focus_region``），
      ``focus_band=False``，渲染端不做任何裁切（原图完整居中）；
    * ``focus_hint="top"/"bottom"/"center"``：改成对应的横向取景带，
      ``focus_band=True``，渲染端据此把镜头往上/往下/往中间偏移。

    一格多句时（黑框内心独白走 ``top``、人物口头对白走 ``bottom``），
    两条 beat 指向同一张面板但取景不同，时间轴因此可以一镜多景。
    """
    hint = str(getattr(beat, "focus_hint", "full") or "full").lower()
    band = FOCUS_BANDS.get(hint)
    if band is None:
        return _focus_for(regions, panel, index), False
    return band, True


def build_timeline_from_workspace(
    series_dir: str | Path,
    episode_no: int,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    fps: int = DEFAULT_FPS,
) -> RenderTimeline:
    """从工作区读齐 Stage 2/3/4/5 的产物并编排时间轴。

    缺 ``04_script/script.json`` 或缺音频清单（且无 wav 可兜底）时抛
    :class:`TimelineError`，错误信息写清缺哪一个文件。
    """
    script_path = script_path_for(series_dir, episode_no)
    if not script_path.is_file():
        raise TimelineError(
            f"缺少解说稿：{script_path}（Stage 4 尚未产出，无法编排时间轴）"
        )
    try:
        script = load_model(NarrationScript, script_path)
    except Exception as exc:
        raise TimelineError(f"解说稿解析失败：{script_path}（{exc}）") from exc

    audios = load_audio_manifest(series_dir, episode_no)
    if not audios:
        raise TimelineError(
            f"音频清单为空：{audio_manifest_path_for(series_dir, episode_no)}"
            "（Stage 5 尚未产出任何音频，无法按 Audio-First 规则编排时间轴）"
        )

    panels = scan_panels(panel_dir_for(series_dir, episode_no))
    if not panels:
        raise TimelineError(
            f"没有可用面板：{panel_dir_for(series_dir, episode_no)}"
            "（Stage 2 尚未产出面板图，无法编排时间轴）"
        )

    # Patch v4.20：纯文字卡（章节标题条 / 白底黑字）永远不进成片。
    #
    # Stage 4 的候选池与分镜编排已经把它们剔掉，正常流程走不到这里；
    # 这一层是给「旧脚本里还留着对纯文字卡的引用」兜底——历史产物里
    # 出现过粉色标题条被当成画面插进 b9 与 b10 之间的情况。剔除后，
    # 那一拍会自动回退到上一张画面（build_timeline 的兜底规则），
    # 不会多出一帧文字条。
    text_card_ids = text_card_panel_ids(series_dir, episode_no)
    if text_card_ids:
        panels = [panel for panel in panels if panel.panel_id not in text_card_ids]
        print(
            f"[s6_timeline] 剔除纯文字卡面板：{sorted(text_card_ids)}",
            file=sys.stderr,
        )

    focus_regions = load_focus_regions(series_dir, episode_no)
    # Patch v3.1 Patch 11 B3：画面音效描述（sfx_cue 缺失时的兜底来源）。
    panel_sfx_map = load_panel_sfx_lists(series_dir, episode_no)

    return build_timeline(
        episode_id=str(script.episode_id or f"ep_{episode_no:03d}"),
        script=script,
        audios=audios,
        panels=panels,
        width=width,
        height=height,
        fps=fps,
        focus_regions=focus_regions,
        panel_sfx_map=panel_sfx_map,
    )


# --------------------------------------------------------------------------
# ASS 外挂字幕
# --------------------------------------------------------------------------
def _ass_timestamp(ms: int) -> str:
    """毫秒 → ASS 时间戳（``H:MM:SS.cc``，厘秒精度）。"""
    total_cs = int(round(float(ms) / 10.0))
    total_cs = max(0, total_cs)
    hours, rest = divmod(total_cs, 360000)
    minutes, rest = divmod(rest, 6000)
    seconds, centis = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def escape_ass_text(text: str) -> str:
    """转义 ASS 正文。

    * ``{`` / ``}`` 是 ASS 的样式覆盖语法，必须转义成 ``\\{`` / ``\\}``，
      否则字幕里的花括号会把后面的文本当成命令解析掉；
    * 真实换行要写成 ``\\N``（ASS 的强制换行），否则多行字幕会挤成一行。
    """
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("{", r"\{").replace("}", r"\}")
    return normalized.replace("\n", r"\N")


def render_ass(
    timeline: RenderTimeline,
    *,
    font_name: str = DEFAULT_FONT_NAME,
    font_size: int = ASS_FONT_SIZE,
    margin_v: int = ASS_MARGIN_V,
) -> str:
    """生成标准 ASS 字幕文本（外挂字幕交付物）。

    样式：底部居中（``{\\an2}``）+ 淡入淡出（``{\\fad(120,120)}``），
    分辨率按成片锁定为 1080×1920。

    Patch v3.0-8 起按抖音安全分区固定样式：``MarginV=250`` 把单行/双行字幕
    锚定在 ``y ≈ 1530~1670`` 的纯暗区（主画面下沿 1440 之下、抖音原生遮挡区
    1720 之上）；``WrapStyle=0`` 智能均分换行。

    **注意**：本机 ffmpeg 没有 libass，成片字幕由 Stage 7 用 PNG overlay 烧录；
    本文件供外挂播放器 / 剪辑软件 / 平台上传使用。
    """
    lines: list[str] = [
        "[Script Info]",
        "; 由 Stage 6（Audio-First 时间轴）自动生成，请勿手工编辑",
        "ScriptType: v4.00+",
        f"PlayResX: {int(timeline.width)}",
        f"PlayResY: {int(timeline.height)}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 0",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        (
            f"Style: Default,{font_name},{int(font_size)},&H00FFFFFF,&H000000FF,"
            f"&H00121216,&H80000000,0,0,0,0,100,100,0,0,1,4,2,2,"
            f"{ASS_MARGIN_L},{ASS_MARGIN_R},{int(margin_v)},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for subtitle in timeline.subtitles:
        text = escape_ass_text(subtitle.text)
        lines.append(
            f"Dialogue: 0,{_ass_timestamp(subtitle.start_ms)},"
            f"{_ass_timestamp(subtitle.end_ms)},Default,,0,0,0,,"
            f"{{\\an2}}{{\\fad(120,120)}}{text}"
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 字幕 PNG（供 Stage 7 用 overlay 烧录）
# --------------------------------------------------------------------------
def render_subtitle_pngs(
    timeline: RenderTimeline,
    out_dir: str | Path,
    *,
    font_name: str = DEFAULT_FONT_NAME,
    font_size: int = ASS_FONT_SIZE,
    margin_v: int = ASS_MARGIN_V,
    margin_h: int = PNG_MARGIN_H,
    stroke_width: int = 4,
) -> list[Path]:
    """为每条字幕渲染一张 **1080×1920 RGBA 透明 PNG**。

    文件名 = ``SubtitleLine.index`` 的 4 位补零（``0001.png``），
    Stage 7 用 ``overlay`` 按 ``SubtitleLine.start_ms/end_ms`` 烧录即可。

    Patch v3.0-8：``margin_v=250`` 让文字块下沿落在 ``y=1670``，
    与外挂 ASS 的 ``MarginV`` 保持同一安全分区。
    """
    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for subtitle in timeline.subtitles:
        path = target_dir / subtitle_filename(subtitle.index)
        try:
            render_subtitle_png(
                subtitle.text,
                path,
                width=int(timeline.width),
                height=int(timeline.height),
                font_name=font_name,
                font_size=int(font_size),
                margin_v=int(margin_v),
                margin_h=int(margin_h),
                stroke_width=int(stroke_width),
            )
        except SubtitleRenderError as exc:
            raise TimelineError(f"字幕 PNG 渲染失败（第 {subtitle.index} 条）：{exc}") from exc
        paths.append(path)
    return paths


# --------------------------------------------------------------------------
# Stage 6 主流程
# --------------------------------------------------------------------------
def run_stage6(
    series_dir: str | Path,
    episode_no: int,
    *,
    episode_id: str | None = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    fps: int = DEFAULT_FPS,
    font_name: str = DEFAULT_FONT_NAME,
    font_size: int = ASS_FONT_SIZE,
) -> RenderTimeline:
    """执行 Stage 6：编排时间轴 → 落盘 → 产出 ASS 与字幕 PNG。

    落盘内容（``episodes/ep_XXX/06_plan/``）::

        timeline.json
        subtitles.ass
        subtitles/0001.png ...

    返回编排好的 :class:`RenderTimeline`。
    """
    timeline = build_timeline_from_workspace(
        series_dir, episode_no, width=width, height=height, fps=fps
    )
    if episode_id:
        timeline = timeline.model_copy(update={"episode_id": str(episode_id)})

    plan_dir = plan_dir_for(series_dir, episode_no)
    plan_dir.mkdir(parents=True, exist_ok=True)

    # 字体前置校验：缺中文字体时立刻报错，而不是先落盘一堆「豆腐块」PNG。
    try:
        resolve_font_path(font_name)
    except SubtitleRenderError as exc:
        raise TimelineError(f"字幕字体不可用：{exc}") from exc

    save_model(timeline, plan_dir / TIMELINE_FILENAME)
    (plan_dir / ASS_FILENAME).write_text(
        render_ass(timeline, font_name=font_name, font_size=font_size), encoding="utf-8"
    )
    render_subtitle_pngs(
        timeline,
        plan_dir / SUBTITLE_DIR_NAME,
        font_name=font_name,
        font_size=font_size,
    )
    return timeline


__all__ = [
    "ASS_FILENAME",
    "AUDIO_MANIFEST_FILENAME",
    "PLAN_DIR_NAME",
    "SFX_KEYWORD_MAP",
    "SFX_MAX_EVENTS",
    "SFX_MIN_GAP_MS",
    "SUBTITLE_DIR_NAME",
    "TIMELINE_FILENAME",
    "TimelineError",
    "align_down",
    "align_up",
    "audio_clips_from_files",
    "audio_dir_for",
    "audio_manifest_path_for",
    "build_timeline",
    "build_timeline_from_workspace",
    "escape_ass_text",
    "frame_count",
    "frame_ms",
    "frames_to_ms",
    "load_audio_manifest",
    "load_focus_regions",
    "load_panel_sfx_lists",
    "ms_to_frames",
    "panel_dir_for",
    "parse_audio_manifest",
    "plan_dir_for",
    "render_ass",
    "render_subtitle_pngs",
    "resolve_sfx_key",
    "run_stage6",
    "scan_panels",
    "script_path_for",
    "select_sfx_events",
    "subtitle_dir_for",
    "subtitle_png_path_for",
    "vision_path_for",
    "wav_duration_ms",
]
