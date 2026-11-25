"""``core.schemas`` 数据契约锁定测试（规范第 3 节，v2.4）。

这些用例的作用是**锁死字段名与取值域**：任何对既有字段的重命名或类型放宽都会让本文件失败，
从而阻止下游模块悄悄漂移。新增字段请同步更新本文件的契约清单（并在规范中登记）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from core.schemas import (
    ArcSummary,
    AudioClipMeta,
    CameraMotion,
    CharacterProfile,
    DialogueBubble,
    EpisodeCatalogItem,
    EpisodeDigest,
    EpisodeMeta,
    NarrationScript,
    PanelMeta,
    PanelVisionInfo,
    PlotThread,
    RenderTimeline,
    ScriptBeat,
    SeriesCatalog,
    SeriesIndexItem,
    StageStatus,
    StoryBibleV2,
    SubtitleLine,
    TimelineScene,
    VisionWindowOutput,
    WordTimestamp,
    load_model,
    save_model,
)

# --------------------------------------------------------------------------
# 契约清单：字段名锁定
# --------------------------------------------------------------------------
FIELD_LOCKS: list[tuple[type, tuple[str, ...]]] = [
    (
        SeriesIndexItem,
        ("series_id", "series_title", "list_url", "total_episodes", "done_episodes"),
    ),
    (
        EpisodeCatalogItem,
        ("episode_no", "episode_id", "title", "viewer_url", "publish_date", "is_locked"),
    ),
    (SeriesCatalog, ("series_id", "series_title", "list_url", "episodes")),
    (
        EpisodeMeta,
        (
            "episode_id",
            "source_url",
            "title_name",
            "episode_title",
            "raw_images",
            "image_width",
            "total_raw_slices",
        ),
    ),
    (
        PanelMeta,
        (
            "panel_id",
            "image_path",
            "width",
            "height",
            "aspect_ratio",
            "source_slice_range",
            "is_tall_panel",
            "edge_density",
        ),
    ),
    (DialogueBubble, ("speaker_guess", "text", "emotion")),
    (
        PanelVisionInfo,
        (
            "panel_id",
            "scene_summary",
            "dialogues",
            "sfx_list",
            "focus_region",
            "is_transition_blank",
            # Patch v3.1 Patch 10：纯字卡标记（默认 False，向后兼容旧 vision.json）
            "is_pure_text_card",
            # Patch v2.5-1：非剧情内容标记（默认 "none"，向后兼容）
            "non_story_type",
        ),
    ),
    (VisionWindowOutput, ("panels",)),
    (
        CharacterProfile,
        (
            "name",
            "aliases",
            "visual_traits",
            "current_status",
            "first_seen_ep",
            "last_seen_ep",
            # Patch v3.2 Patch 12：角色性别（默认 "unknown"，向后兼容旧 story_bible）
            "gender",
        ),
    ),
    (
        PlotThread,
        ("thread_id", "title", "description", "introduced_ep", "status", "resolved_ep"),
    ),
    (ArcSummary, ("arc_name", "ep_range", "summary")),
    (
        EpisodeDigest,
        ("episode_no", "one_line_hook", "summary", "keywords", "characters_present"),
    ),
    (
        StoryBibleV2,
        (
            "series_id",
            "series_title",
            "narration_style",
            "characters",
            "plot_threads",
            "arc_summaries",
            "episodes_index",
            "pronunciation_overrides",
            "snapshots_ep_nos",
        ),
    ),
    (
        ScriptBeat,
        (
            "beat_id",
            "narration_text",
            "tts_text",
            "emotion_instruction",
            "speaker_role",
            "target_panel_ids",
            "motion",
            "pause_after_ms",
            # Patch v2.6-5：一镜多句时用 focus_hint 区分上下半格。
            "focus_hint",
            # Patch v3.1 Patch 10：可选音效触发标签（默认 None，向后兼容旧剧本）
            "sfx_cue",
            # Patch v4.40：音效在本句内的落点（首 / 中 / 尾，默认 "start"）
            "sfx_position",
            # Patch v3.2 Patch 12：说话人性别（默认 "unknown"，向后兼容旧剧本）
            "speaker_gender",
            # Patch v4.2：逐句指定音色（默认空串 = 跟随角色解析）
            "voice_override",
            # Patch v4.4：单句语速微调（默认 None = 跟随角色默认语速）
            "speed_override",
            # Patch v4.13：这条解说归属的镜头组（幕）（默认 None = 尚未归属）
            "scene_id",
        ),
    ),
    (
        NarrationScript,
        (
            "episode_id",
            "hook_title",
            "beats",
            "episode_digest",
            "character_updates",
            "new_plot_threads",
            "resolved_thread_ids",
        ),
    ),
    (WordTimestamp, ("text", "start_ms", "end_ms")),
    (
        AudioClipMeta,
        (
            "beat_id",
            "audio_path",
            "duration_ms",
            "pause_after_ms",
            "word_timestamps",
            # Patch v4.40：句内内联音效的落点（毫秒偏移 + 音效键）
            "inline_sfx",
        ),
    ),
    (
        TimelineScene,
        (
            "scene_index",
            "beat_id",
            "panel_id",
            "image_path",
            "start_ms",
            "end_ms",
            "duration_ms",
            "motion",
            "focus_region",
            # Patch v2.6-5：focus_region 是 focus_hint 取景带时置 True，渲染端据此裁切。
            "focus_band",
            # Patch v4.30：接力扫镜区间（同格连续平移时由 Stage 6 写入，Stage 7 读取）。
            "pan_start_ratio",
            "pan_end_ratio",
        ),
    ),
    (SubtitleLine, ("index", "beat_id", "text", "start_ms", "end_ms")),
    (
        RenderTimeline,
        (
            "episode_id",
            "aspect_ratio",
            "width",
            "height",
            "fps",
            "total_duration_ms",
            "scenes",
            "audios",
            "subtitles",
            # Patch v3.1 Patch 10：音效事件表（默认空列表，向后兼容旧 timeline.json）
            "sfx_events",
        ),
    ),
]


@pytest.mark.parametrize(("model_cls", "expected"), FIELD_LOCKS)
def test_field_names_are_locked(model_cls: type, expected: tuple[str, ...]) -> None:
    """字段名与顺序锁定：改名即失败。"""
    assert tuple(model_cls.model_fields) == expected


def test_camera_motion_values() -> None:
    assert {motion.value for motion in CameraMotion} == {
        "static",
        "zoom_in",
        "zoom_out",
        "pan_down",
        "pan_up",
        # Patch v4.28：新增真正的横向扫镜。
        "pan_left",
        "pan_right",
        "shake",
    }


def test_stage_status_values() -> None:
    assert {status.value for status in StageStatus} == {"pending", "running", "done", "failed"}


# --------------------------------------------------------------------------
# 铁律一：画幅锁定 9:16
# --------------------------------------------------------------------------
def test_render_timeline_defaults_to_9x16() -> None:
    timeline = RenderTimeline(
        episode_id="wt_1_ep_001",
        total_duration_ms=1000,
        scenes=[],
        audios=[],
        subtitles=[],
    )
    assert timeline.aspect_ratio == "9:16"
    assert (timeline.width, timeline.height, timeline.fps) == (1080, 1920, 30)


def test_render_timeline_rejects_other_aspect_ratio() -> None:
    with pytest.raises(ValidationError):
        RenderTimeline(
            episode_id="wt_1_ep_001",
            aspect_ratio="16:9",
            total_duration_ms=1000,
            scenes=[],
            audios=[],
            subtitles=[],
        )


# --------------------------------------------------------------------------
# 铁律二：字幕文本与 TTS 文本分离
# --------------------------------------------------------------------------
def test_narration_text_and_tts_text_are_independent() -> None:
    beat = ScriptBeat(
        beat_id="b1",
        narration_text="他推开了那扇门",
        tts_text="他推开了那扇门（mén）",
        target_panel_ids=["p1"],
    )
    assert beat.narration_text == "他推开了那扇门"
    assert beat.tts_text == "他推开了那扇门（mén）"


def test_narration_text_length_bounds() -> None:
    with pytest.raises(ValidationError):
        ScriptBeat(beat_id="b1", narration_text="短", target_panel_ids=["p1"])
    with pytest.raises(ValidationError):
        ScriptBeat(beat_id="b1", narration_text="长" * 121, target_panel_ids=["p1"])


def test_tts_text_may_be_none() -> None:
    beat = ScriptBeat(beat_id="b1", narration_text="正常长度文本", target_panel_ids=["p1"])
    assert beat.tts_text is None


def test_target_panel_ids_must_not_be_empty() -> None:
    with pytest.raises(ValidationError):
        ScriptBeat(beat_id="b1", narration_text="正常长度文本", target_panel_ids=[])


def test_pause_after_ms_bounds() -> None:
    with pytest.raises(ValidationError):
        ScriptBeat(beat_id="b1", narration_text="正常长度文本", target_panel_ids=["p1"], pause_after_ms=-1)
    with pytest.raises(ValidationError):
        ScriptBeat(beat_id="b1", narration_text="正常长度文本", target_panel_ids=["p1"], pause_after_ms=2001)


def test_script_beat_defaults() -> None:
    beat = ScriptBeat(beat_id="b1", narration_text="正常长度文本", target_panel_ids=["p1"])
    assert beat.motion is CameraMotion.ZOOM_IN
    # Patch v3.2 Patch 12：默认停顿由 250ms 收紧到 150ms。
    assert beat.pause_after_ms == 150
    assert beat.speaker_role == "narrator"
    assert beat.emotion_instruction


# --------------------------------------------------------------------------
# 取值域锁定
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "emotion",
    ["calm", "happy", "angry", "sad", "shocked", "whisper", "tense"],
)
def test_dialogue_bubble_accepts_known_emotions(emotion: str) -> None:
    bubble = DialogueBubble(speaker_guess="主角", text="台词", emotion=emotion)
    assert bubble.emotion == emotion


def test_dialogue_bubble_rejects_unknown_emotion() -> None:
    with pytest.raises(ValidationError):
        DialogueBubble(speaker_guess="主角", text="台词", emotion="excited")


def test_plot_thread_status_is_open_by_default() -> None:
    thread = PlotThread(
        thread_id="t1",
        title="身世之谜",
        description="主角身世未明",
        introduced_ep=1,
    )
    assert thread.status == "open"
    assert thread.resolved_ep is None


def test_plot_thread_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        PlotThread(
            thread_id="t1",
            title="身世之谜",
            description="主角身世未明",
            introduced_ep=1,
            status="pending",
        )


def test_arc_summary_ep_range_is_int_tuple() -> None:
    arc = ArcSummary(arc_name="第1-10话篇章", ep_range=(1, 10), summary="开篇")
    assert arc.ep_range == (1, 10)
    assert isinstance(arc.ep_range, tuple)


# --------------------------------------------------------------------------
# StoryBibleV2 默认值
# --------------------------------------------------------------------------
def test_story_bible_defaults() -> None:
    bible = StoryBibleV2(series_id="wt_1", series_title="测试漫画")
    assert bible.narration_style == "热血悬疑漫评风格，节奏紧凑，口语化有代入感"
    assert bible.characters == []
    assert bible.plot_threads == []
    assert bible.arc_summaries == []
    assert bible.episodes_index == []
    assert bible.pronunciation_overrides == {}
    assert bible.snapshots_ep_nos == []


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------
def test_save_and_load_model_roundtrip(tmp_path: Path) -> None:
    script = NarrationScript(
        episode_id="wt_1_ep_001",
        hook_title="钩子标题",
        beats=[ScriptBeat(beat_id="b1", narration_text="第一段解说", target_panel_ids=["p1"])],
        episode_digest=EpisodeDigest(
            episode_no=1,
            one_line_hook="主角登场",
            summary="第一话概要",
            keywords=["登场"],
            characters_present=["主角"],
        ),
    )
    path = tmp_path / "nested" / "script.json"
    save_model(script, path)
    assert path.is_file()
    loaded = load_model(NarrationScript, path)
    assert loaded == script
    assert loaded.beats[0].narration_text == "第一段解说"
