"""核心数据契约（锁定版）。

对应规范 ``WEBTOON2VIDEO_DEV_SPEC.md`` 第 3 节，v2.4 已锁定：
字段名、字段类型与默认值**不得私自改动**；后续模块只能在此基础上扩展新模型，
不得重命名既有字段。新增模型请另起文件或在本文件末尾显式追加。

铁律提醒：字幕文本 ``narration_text`` 与 TTS 文本 ``tts_text`` 必须分离，
多音字替换只能进入 ``tts_text``。
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class CameraMotion(str, Enum):
    STATIC = "static"
    ZOOM_IN = "zoom_in"
    ZOOM_OUT = "zoom_out"
    PAN_DOWN = "pan_down"
    PAN_UP = "pan_up"
    SHAKE = "shake"


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class SeriesIndexItem(BaseModel):
    series_id: str                        # "wt_88201"
    series_title: str
    list_url: str
    total_episodes: int = 0
    done_episodes: int = 0


class EpisodeCatalogItem(BaseModel):
    episode_no: int
    episode_id: str                       # "wt_88201_ep_001"
    title: str
    viewer_url: str
    publish_date: str = ""
    is_locked: bool = False


class SeriesCatalog(BaseModel):
    series_id: str
    series_title: str
    list_url: str
    episodes: list[EpisodeCatalogItem]


class EpisodeMeta(BaseModel):
    episode_id: str
    source_url: str | None = None
    title_name: str = ""
    episode_title: str = ""
    raw_images: list[str]
    image_width: int
    total_raw_slices: int


class PanelMeta(BaseModel):
    panel_id: str
    image_path: str
    width: int
    height: int
    aspect_ratio: float
    source_slice_range: tuple[int, int]
    is_tall_panel: bool
    edge_density: float = 0.0


class DialogueBubble(BaseModel):
    speaker_guess: str
    text: str
    emotion: Literal["calm", "happy", "angry", "sad", "shocked", "whisper", "tense"] = "calm"


class PanelVisionInfo(BaseModel):
    panel_id: str
    scene_summary: str
    dialogues: list[DialogueBubble] = Field(default_factory=list)
    sfx_list: list[str] = Field(default_factory=list)
    focus_region: tuple[float, float, float, float] | None = None
    is_transition_blank: bool = False
    #: Patch v3.1 Patch 10：纯字卡标记——本格主体只是一块文字框/内心独白卡片，
    #: 没有角色表情与肢体动作。Stage 4B 会给这类分镜打上 ``[纯字卡]`` 标签并
    #: 尽量跳过，避免「白底黑字 PPT 朗读」。
    is_pure_text_card: bool = False
    #: Patch v2.5-1：非剧情内容标记（默认 "none"，向后兼容旧 JSON）。
    #: Patch v3.0 Patch 6：新增 ``sfx_only``——整格只有拟声词特效字/纯气氛背景、
    #: 没有人物动作与剧情对白的画面。
    non_story_type: Literal[
        "none",
        "transition_blank",
        "copyright_notice",
        "promo_banner",
        "sfx_only",
    ] = "none"


class VisionWindowOutput(BaseModel):
    panels: list[PanelVisionInfo]


class StoryboardScene(BaseModel):
    """Stage 3b/4 镜头组（一幕连续剧情）。

    Patch v4.0 Patch 19：Scene-First 镜头组架构。一幕 = 一段连续剧情，
    对应 ``04_script/storyboard.json`` 里的一个镜头组。人工可以在
    ``user_note`` 里写编导要点、在 ``locked_quotes`` 里钉死必保原话，
    用 ``enabled`` 关掉不想要的幕、用 ``must_cover`` 标记必须讲到���幕。
    """

    scene_id: str
    scene_index: int
    title: str = ""
    location: str = ""
    panel_ids: list[str] = Field(..., min_length=1)
    enabled: bool = True
    must_cover: bool = True
    user_note: str = ""
    locked_quotes: list[str] = Field(default_factory=list)
    visual_recap: str = ""


class EpisodeStoryboard(BaseModel):
    """单话镜头编排清单（落盘于 04_script/storyboard.json）。

    Patch v4.0 Patch 19。``source == "human"`` 表示这份编排是人工编辑过的，
    自动重建流程**绝不覆写**它。
    """

    episode_id: str
    source: Literal["auto", "human"] = "auto"
    scenes: list[StoryboardScene] = Field(default_factory=list)


class CharacterProfile(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    visual_traits: str
    current_status: str
    first_seen_ep: int
    last_seen_ep: int
    #: Patch v3.2 Patch 12：角色性别。未登记时为 ``"unknown"``，
    #: 下游据此给「未单独配置音色的角色」路由通用男声 / 通用女声。
    gender: Literal["male", "female", "unknown"] = "unknown"


class PlotThread(BaseModel):
    thread_id: str
    title: str
    description: str
    introduced_ep: int
    status: Literal["open", "resolved"] = "open"
    resolved_ep: int | None = None


class ArcSummary(BaseModel):
    arc_name: str
    ep_range: tuple[int, int]
    summary: str


class EpisodeDigest(BaseModel):
    episode_no: int
    one_line_hook: str
    summary: str
    keywords: list[str] = Field(default_factory=list)
    characters_present: list[str] = Field(default_factory=list)


class StoryBibleV2(BaseModel):
    series_id: str
    series_title: str
    narration_style: str = "热血悬疑漫评风格，节奏紧凑，口语化有代入感"
    characters: list[CharacterProfile] = Field(default_factory=list)
    plot_threads: list[PlotThread] = Field(default_factory=list)
    arc_summaries: list[ArcSummary] = Field(default_factory=list)
    episodes_index: list[EpisodeDigest] = Field(default_factory=list)
    pronunciation_overrides: dict[str, str] = Field(default_factory=dict)
    snapshots_ep_nos: list[int] = Field(default_factory=list)


class ScriptBeat(BaseModel):
    beat_id: str
    narration_text: str = Field(..., min_length=2, max_length=120)
    tts_text: str | None = None
    emotion_instruction: str = "用自然、流畅且有代入感的漫评解说语气说出"
    speaker_role: str = "narrator"
    target_panel_ids: list[str] = Field(..., min_length=1)
    motion: CameraMotion = CameraMotion.ZOOM_IN
    #: Patch v3.2 Patch 12：默认停顿从 250ms 收紧到 150ms（提振整片节奏）。
    pause_after_ms: int = Field(default=150, ge=0, le=2000)
    #: Patch v2.6-5：镜头焦点提示。一格多句时，第 1 句讲黑框内心独白用
    #: ``top``，第 2 句讲人物口头台词用 ``bottom``；``full`` 表示整格取景
    #: （默认值，向后兼容旧剧本）。
    focus_hint: Literal["full", "top", "bottom", "center"] = "full"
    #: Patch v3.1 Patch 10：可选音效触发标签。仅在剧情高潮或明显物理动作点
    #: 输出一个标准 key（``heart_thump`` / ``door_slam`` / ``whoosh`` / ``gasp`` /
    #: ``phone_msg`` / ``footstep`` / ``crash``）；普通对话与叙述留空。
    #: Stage 6 会据此生成 :class:`SfxEvent`（全话最多 8 个、间隔 >= 6000ms）。
    sfx_cue: str | None = None
    #: Patch v3.2 Patch 12：说话人性别。与 ``speaker_role`` 配套，
    #: 用于把「未在作品词典里单独指定音色的角色」路由到通用男声 / 通用女声。
    speaker_gender: Literal["male", "female", "unknown"] = "unknown"


class NarrationScript(BaseModel):
    episode_id: str
    hook_title: str
    beats: list[ScriptBeat]
    episode_digest: EpisodeDigest
    character_updates: list[CharacterProfile] = Field(default_factory=list)
    new_plot_threads: list[PlotThread] = Field(default_factory=list)
    resolved_thread_ids: list[str] = Field(default_factory=list)


class StoryChapter(BaseModel):
    """Stage 4A「故事反写」的章节单元。

    一章 = 一段连续剧情，覆盖 ``panel_hint_range`` 指向的连续分镜区间
    （闭区间，两端都是真实分镜 ID）。

    Patch v3.0-7 起新增 ``chapter_title``（章节标题）与 ``panel_hint_range``
    （仅供 4B 缩小检索范围）；``panel_range`` / ``dramatic_intent`` 作为
    Patch v2.6-5 的旧字段保留，读取旧 ``story_draft.json`` 时自动映射。
    """

    chapter_index: int
    chapter_title: str = ""
    panel_hint_range: tuple[str, str] = ("", "")
    story_prose: str = ""
    #: 旧字段（Patch v2.6-5），保留只为兼容历史 story_draft.json。
    panel_range: tuple[str, str] = ("", "")
    #: 旧字段（Patch v2.6-5），保留只为兼容历史 story_draft.json。
    dramatic_intent: str = ""

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_fields(cls, data: Any) -> Any:
        """旧稿只有 ``panel_range`` / ``dramatic_intent`` 时自动补齐新字段。"""
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        if not payload.get("panel_hint_range") and payload.get("panel_range"):
            payload["panel_hint_range"] = payload["panel_range"]
        if not payload.get("chapter_title") and payload.get("dramatic_intent"):
            payload["chapter_title"] = payload["dramatic_intent"]
        return payload


class EpisodeStoryDraft(BaseModel):
    """Stage 4A「全话通读 + 故事反写」的产物。

    Patch v3.0-7 起新增 ``hook_opening``（开篇钩子）与 ``full_story_markdown``
    （完整说书体正文，供人工审阅与 4B 拆拍使用）；
    ``overall_plot_arc`` 作为旧字段保留，读取旧 story_draft.json 时自动映射。

    章节必须按分镜时间顺序单调排列，首尾相接覆盖本话全部有效分镜；
    严禁把后半话的反转提前写进前半话段落（那是「剧透」，会让逐页讲述失去悬念）。
    """

    episode_id: str
    hook_opening: str = ""
    full_story_markdown: str = ""
    chapters: list[StoryChapter] = Field(default_factory=list)
    #: 旧字段（Patch v2.6-5），保留只为兼容历史 story_draft.json。
    overall_plot_arc: str = ""

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_fields(cls, data: Any) -> Any:
        """旧稿只有 ``overall_plot_arc`` 时把它当作全文正文。"""
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        if not payload.get("full_story_markdown") and payload.get("overall_plot_arc"):
            payload["full_story_markdown"] = payload["overall_plot_arc"]
        return payload


class WordTimestamp(BaseModel):
    text: str
    start_ms: int
    end_ms: int


class AudioClipMeta(BaseModel):
    beat_id: str
    audio_path: str
    duration_ms: int
    pause_after_ms: int
    word_timestamps: list[WordTimestamp] = Field(default_factory=list)


class TimelineScene(BaseModel):
    scene_index: int
    beat_id: str
    panel_id: str
    image_path: str
    start_ms: int
    end_ms: int
    duration_ms: int
    motion: CameraMotion
    focus_region: tuple[float, float, float, float] | None = None
    #: Patch v2.6-5：``focus_region`` 是否是「取景带」。
    #: True = 由 ``ScriptBeat.focus_hint``（top/bottom/center）推导的取景带，
    #: Stage 7 应当据此裁切前景（镜头偏移到上/下/中部）；
    #: False = Stage 3 给出的主体框，仅作参考，不改变「原图完整居中」的构图。
    focus_band: bool = False


class SubtitleLine(BaseModel):
    index: int
    beat_id: str
    text: str
    start_ms: int
    end_ms: int


class SfxEvent(BaseModel):
    """Patch v3.1 Patch 10：时间轴上的音效事件。

    Stage 6 依据 ``ScriptBeat.sfx_cue``（或 ``vision.json`` 里的 ``sfx_list``
    关键词兜底）生成，Stage 7 用它把 ``assets/sfx/<sfx_key>.wav`` 混进成片。
    """

    sfx_key: str
    start_ms: int
    gain_db: float = -10.0


class RenderTimeline(BaseModel):
    episode_id: str
    aspect_ratio: Literal["9:16"] = "9:16"
    width: int = 1080
    height: int = 1920
    fps: int = 30
    total_duration_ms: int
    scenes: list[TimelineScene]
    audios: list[AudioClipMeta]
    subtitles: list[SubtitleLine]
    #: Patch v3.1 Patch 10：音效事件（默认空列表，旧 timeline.json 照常反序列化）。
    sfx_events: list[SfxEvent] = Field(default_factory=list)


def save_model(model: BaseModel, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")


def load_model(model_cls: type[BaseModel], path: Path):
    return model_cls.model_validate_json(path.read_text(encoding="utf-8"))
