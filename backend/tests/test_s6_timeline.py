"""Stage 6（Audio-First 时间轴 + 字幕）的离线测试。

全部离线：临时目录里造 ``04_script/script.json``、``05_audio/audio_manifest.json``
与 ``02_panels/panel_XXX.png``，不触碰网络、不依赖 Stage 5 的真实 TTS。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from PIL import Image

from core.schemas import (
    AudioClipMeta,
    CameraMotion,
    EpisodeDigest,
    NarrationScript,
    PanelMeta,
    RenderTimeline,
    ScriptBeat,
    load_model,
    save_model,
)
from stages.s6_timeline import (
    ASS_FILENAME,
    PLAN_DIR_NAME,
    SUBTITLE_DIR_NAME,
    TIMELINE_FILENAME,
    TimelineError,
    build_timeline,
    build_timeline_from_workspace,
    frame_count,
    frame_ms,
    ms_to_frames,
    plan_dir_for,
    render_ass,
    render_subtitle_pngs,
    run_stage6,
)

#: 30fps 的帧栅格（毫秒）。
GRID = frame_ms(30)


# --------------------------------------------------------------------------
# 造数据
# --------------------------------------------------------------------------
def _panel(panel_id: str, image_dir: Path, width: int = 120, height: int = 200) -> PanelMeta:
    """造一张面板图并返回对应的 ``PanelMeta``。"""
    image_dir.mkdir(parents=True, exist_ok=True)
    path = image_dir / f"{panel_id}.png"
    Image.new("RGB", (width, height), (30, 60, 90)).save(path)
    return PanelMeta(
        panel_id=panel_id,
        image_path=str(path),
        width=width,
        height=height,
        aspect_ratio=width / height,
        source_slice_range=(0, height),
        is_tall_panel=False,
    )


def _beat(
    beat_id: str,
    narration: str,
    panel_ids: list[str],
    *,
    motion: CameraMotion = CameraMotion.ZOOM_IN,
    tts_text: str | None = None,
) -> ScriptBeat:
    return ScriptBeat(
        beat_id=beat_id,
        narration_text=narration,
        tts_text=tts_text,
        target_panel_ids=panel_ids,
        motion=motion,
    )


def _script(beats: list[ScriptBeat]) -> NarrationScript:
    return NarrationScript(
        episode_id="ep_001",
        hook_title="钩子标题",
        beats=beats,
        episode_digest=EpisodeDigest(
            episode_no=1,
            one_line_hook="钩子标题",
            summary="这一话的主线摘要",
        ),
    )


def _audio(beat_id: str, duration_ms: int, pause_after_ms: int) -> AudioClipMeta:
    return AudioClipMeta(
        beat_id=beat_id,
        audio_path=f"05_audio/{beat_id}.wav",
        duration_ms=duration_ms,
        pause_after_ms=pause_after_ms,
    )


def _write_script(series_dir: Path, episode_no: int, script: NarrationScript) -> Path:
    path = series_dir / "episodes" / f"ep_{episode_no:03d}" / "04_script" / "script.json"
    save_model(script, path)
    return path


def _write_audio_manifest(
    series_dir: Path, episode_no: int, audios: list[AudioClipMeta]
) -> Path:
    path = (
        series_dir
        / "episodes"
        / f"ep_{episode_no:03d}"
        / "05_audio"
        / "audio_manifest.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([clip.model_dump(mode="json") for clip in audios], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _expected_starts(audios: list[AudioClipMeta]) -> list[int]:
    """按文档规则独立复算每段音频的起点（起点向上对齐到帧栅格）。"""
    starts: list[int] = []
    cursor = 0
    for clip in audios:
        start = int(math.ceil(cursor / GRID) * GRID)
        starts.append(start)
        cursor = start + clip.duration_ms + clip.pause_after_ms
    return starts


@pytest.fixture()
def panels(tmp_path: Path) -> list[PanelMeta]:
    image_dir = tmp_path / "02_panels"
    return [_panel(f"panel_{i:03d}", image_dir) for i in (1, 2, 3)]


@pytest.fixture()
def three_beats() -> list[ScriptBeat]:
    return [
        _beat("b1", "第一句解说词，画面里主角推开了门。", ["panel_001"]),
        _beat("b2", "第二句解说词，气氛一下子紧张起来。", ["panel_002"]),
        _beat("b3", "第三句解说词，冲突彻底爆发。", ["panel_003"]),
    ]


@pytest.fixture()
def three_audios() -> list[AudioClipMeta]:
    return [
        _audio("b1", 1000, 250),
        _audio("b2", 2000, 0),
        _audio("b3", 1500, 500),
    ]


# --------------------------------------------------------------------------
# 1. Audio-First：串行排布 + 停顿 + 总时长
# --------------------------------------------------------------------------
def test_audio_first_layout_is_serial_with_pauses(
    three_beats: list[ScriptBeat], three_audios: list[AudioClipMeta], panels: list[PanelMeta]
) -> None:
    timeline = build_timeline(
        episode_id="ep_001",
        script=_script(three_beats),
        audios=three_audios,
        panels=panels,
    )

    starts = _expected_starts(three_audios)
    # 每段起点 = 上一段终点 + 上一段停顿（并对齐帧栅格）。
    assert starts == [0, 1254, 3267]
    assert [sub.start_ms for sub in timeline.subtitles] == starts
    assert [sub.end_ms for sub in timeline.subtitles] == [
        starts[0] + 1000,
        starts[1] + 2000,
        starts[2] + 1500,
    ]

    # 停顿被吸收进画面：上一段的最后一个 scene 一直延伸到下一句起点（不黑屏、不重叠）。
    beat_scenes: dict[str, list] = {}
    for scene in timeline.scenes:
        beat_scenes.setdefault(scene.beat_id, []).append(scene)
    assert beat_scenes["b1"][-1].end_ms == starts[1]
    assert beat_scenes["b2"][-1].end_ms == starts[2]

    # scene 串行且无重叠。
    for previous, current in zip(timeline.scenes, timeline.scenes[1:]):
        assert previous.end_ms <= current.start_ms
        assert previous.scene_index + 1 == current.scene_index

    # 总时长 = 末段终点 + 末段停顿。
    assert timeline.total_duration_ms == starts[2] + 1500 + 500 == 5267


def test_audio_first_uses_real_audio_duration_not_estimate(
    three_beats: list[ScriptBeat], panels: list[PanelMeta]
) -> None:
    """同一段文案下，音频时长翻倍 → 时间轴变长（时长由音频决定，不由文案估算）。"""
    short = build_timeline(
        episode_id="ep_001",
        script=_script(three_beats[:1]),
        audios=[_audio("b1", 1000, 250)],
        panels=panels,
    )
    long = build_timeline(
        episode_id="ep_001",
        script=_script(three_beats[:1]),
        audios=[_audio("b1", 4000, 250)],
        panels=panels,
    )
    assert long.total_duration_ms == short.total_duration_ms + 3000


# --------------------------------------------------------------------------
# 2. 字幕与音频对齐，且文本是 narration_text 原样
# --------------------------------------------------------------------------
def test_subtitles_align_with_audio_and_keep_narration_text(
    panels: list[PanelMeta],
) -> None:
    # 「重庆」的「重」是多音字：Stage 5 会在 tts_text 里做替换，但字幕必须用原文。
    narration = "他在重庆的雨夜里回头，重重地关上了门。"
    tts_text = "他在chong qing的雨夜里回头，chong chong地关上了门。"
    script = _script([_beat("b1", narration, ["panel_001"], tts_text=tts_text)])
    audios = [_audio("b1", 1500, 200)]

    timeline = build_timeline(
        episode_id="ep_001", script=script, audios=audios, panels=panels
    )

    assert len(timeline.subtitles) == 1
    subtitle = timeline.subtitles[0]
    assert subtitle.text == narration
    assert "chong" not in subtitle.text
    assert subtitle.start_ms == timeline.scenes[0].start_ms
    assert subtitle.end_ms == subtitle.start_ms + audios[0].duration_ms
    assert subtitle.beat_id == "b1"


def test_subtitle_index_is_sequential_from_one(
    three_beats: list[ScriptBeat], three_audios: list[AudioClipMeta], panels: list[PanelMeta]
) -> None:
    timeline = build_timeline(
        episode_id="ep_001",
        script=_script(three_beats),
        audios=three_audios,
        panels=panels,
    )
    assert [sub.index for sub in timeline.subtitles] == [1, 2, 3]


# --------------------------------------------------------------------------
# 3. 一条 beat 多个 target_panel_ids → 多个 scene
# --------------------------------------------------------------------------
def test_multi_panel_beat_splits_into_multiple_scenes(panels: list[PanelMeta]) -> None:
    script = _script([_beat("b1", "这一句要配两张画面。", ["panel_001", "panel_002"])])
    timeline = build_timeline(
        episode_id="ep_001",
        script=script,
        audios=[_audio("b1", 2000, 250)],
        panels=panels,
    )

    assert len(timeline.scenes) == 2
    assert [scene.scene_index for scene in timeline.scenes] == [0, 1]
    assert [scene.panel_id for scene in timeline.scenes] == ["panel_001", "panel_002"]
    for scene in timeline.scenes:
        assert scene.duration_ms >= GRID  # 每段至少一帧
        assert scene.beat_id == "b1"
    assert timeline.scenes[0].end_ms == timeline.scenes[1].start_ms
    # 字幕仍然一条（一条 beat 一条字幕）。
    assert len(timeline.subtitles) == 1


def test_beat_without_available_panel_reuses_previous_panel(
    three_beats: list[ScriptBeat], three_audios: list[AudioClipMeta], panels: list[PanelMeta]
) -> None:
    """面板匹配不上时复用上一张，不产生空 scene。"""
    script = _script(
        [
            _beat("b1", "第一句。", ["panel_001"]),
            _beat("b2", "第二句。", ["panel_999"]),  # 不存在的面板
        ]
    )
    timeline = build_timeline(
        episode_id="ep_001",
        script=script,
        audios=three_audios[:2],
        panels=panels,
    )
    assert [scene.panel_id for scene in timeline.scenes] == ["panel_001", "panel_001"]
    assert all(scene.image_path for scene in timeline.scenes)


# --------------------------------------------------------------------------
# 4. 30fps 帧对齐
# --------------------------------------------------------------------------
def test_timeline_starts_are_frame_aligned(
    three_beats: list[ScriptBeat], three_audios: list[AudioClipMeta], panels: list[PanelMeta]
) -> None:
    """实现口径：所有**起点**严格落在 33ms 栅格上（``start_ms % 33 == 0``）。

    终点保留真实毫秒（``起点 + 真实音频时长``），这样字幕与语音严格对齐；
    最后一个 scene 的终点 = 总时长（可能不在栅格上，因为总时长含末段停顿）。
    """
    timeline = build_timeline(
        episode_id="ep_001",
        script=_script(three_beats),
        audios=three_audios,
        panels=panels,
    )
    assert GRID == 33
    for scene in timeline.scenes:
        assert scene.start_ms % GRID == 0
    for subtitle in timeline.subtitles:
        assert subtitle.start_ms % GRID == 0
    # 帧栅格内相邻切点之间至少 1 帧。
    for previous, current in zip(timeline.scenes, timeline.scenes[1:]):
        assert current.start_ms - previous.end_ms >= 0


def test_frame_count_helpers() -> None:
    timeline = RenderTimeline(
        episode_id="ep_001",
        total_duration_ms=1000,
        scenes=[],
        audios=[],
        subtitles=[],
    )
    assert ms_to_frames(0, 30) == 0
    assert ms_to_frames(1000, 30) == 30
    assert frame_count(timeline, 30) == 30


# --------------------------------------------------------------------------
# 5. ASS 产出
# --------------------------------------------------------------------------
def test_render_ass_structure(
    three_beats: list[ScriptBeat], three_audios: list[AudioClipMeta], panels: list[PanelMeta]
) -> None:
    timeline = build_timeline(
        episode_id="ep_001",
        script=_script(three_beats),
        audios=three_audios,
        panels=panels,
    )
    ass = render_ass(timeline)

    assert "[Script Info]" in ass
    assert "PlayResX: 1080" in ass
    assert "PlayResY: 1920" in ass
    assert "[V4+ Styles]" in ass
    assert "[Events]" in ass
    assert ass.count("Dialogue:") == len(timeline.subtitles)
    assert "{\\an2}" in ass
    assert "{\\fad(120,120)}" in ass
    # 字幕文本出现在 Dialogue 行里。
    assert timeline.subtitles[0].text in ass


def test_render_ass_escapes_braces_and_newlines(panels: list[PanelMeta]) -> None:
    script = _script([_beat("b1", "他喊道：{别过来}！\n下一行。", ["panel_001"])])
    timeline = build_timeline(
        episode_id="ep_001",
        script=script,
        audios=[_audio("b1", 1000, 250)],
        panels=panels,
    )
    ass = render_ass(timeline)

    assert r"\{别过来\}" in ass
    assert "{别过来}" not in ass
    assert r"\N" in ass  # 换行写成 ASS 的强制换行
    assert "\n下一行" not in ass


# --------------------------------------------------------------------------
# 6. 字幕 PNG
# --------------------------------------------------------------------------
def test_render_subtitle_pngs(
    three_beats: list[ScriptBeat], three_audios: list[AudioClipMeta], panels: list[PanelMeta], tmp_path: Path
) -> None:
    timeline = build_timeline(
        episode_id="ep_001",
        script=_script(three_beats),
        audios=three_audios,
        panels=panels,
    )
    out_dir = tmp_path / "subtitles"
    paths = render_subtitle_pngs(timeline, out_dir)

    assert len(paths) == len(timeline.subtitles) == 3
    for path, subtitle in zip(paths, timeline.subtitles):
        assert path.exists()
        assert path.name == f"{subtitle.index:04d}.png"
        with Image.open(path) as image:
            assert image.size == (1080, 1920)
            assert image.mode == "RGBA"


# --------------------------------------------------------------------------
# 7. run_stage6 落盘 + 回读
# --------------------------------------------------------------------------
def test_run_stage6_writes_artifacts_and_roundtrips(tmp_path: Path) -> None:
    series_dir = tmp_path / "series"
    episode_no = 1
    episode_dir = series_dir / "episodes" / f"ep_{episode_no:03d}"
    (episode_dir / "02_panels").mkdir(parents=True, exist_ok=True)
    for i in (1, 2, 3):
        _panel(f"panel_{i:03d}", episode_dir / "02_panels")

    script = _script(
        [
            _beat("b1", "第一句解说词，主角推开了门。", ["panel_001"]),
            _beat("b2", "第二句解说词，气氛紧张起来。", ["panel_002"]),
        ]
    )
    audios = [_audio("b1", 1200, 250), _audio("b2", 1800, 300)]
    _write_script(series_dir, episode_no, script)
    _write_audio_manifest(series_dir, episode_no, audios)

    timeline = run_stage6(series_dir, episode_no)

    plan_dir = plan_dir_for(series_dir, episode_no)
    assert plan_dir.name == PLAN_DIR_NAME
    assert (plan_dir / TIMELINE_FILENAME).is_file()
    assert (plan_dir / ASS_FILENAME).is_file()
    subtitle_dir = plan_dir / SUBTITLE_DIR_NAME
    assert subtitle_dir.is_dir()
    assert (subtitle_dir / "0001.png").is_file()
    assert (subtitle_dir / "0002.png").is_file()

    reloaded = load_model(RenderTimeline, plan_dir / TIMELINE_FILENAME)
    assert reloaded.model_dump() == timeline.model_dump()
    assert reloaded.total_duration_ms == timeline.total_duration_ms
    assert len(reloaded.scenes) == len(timeline.scenes)
    assert len(reloaded.subtitles) == len(timeline.subtitles)


def test_build_timeline_from_workspace_matches_run_stage6(tmp_path: Path) -> None:
    series_dir = tmp_path / "series"
    episode_no = 2
    episode_dir = series_dir / "episodes" / f"ep_{episode_no:03d}"
    (episode_dir / "02_panels").mkdir(parents=True, exist_ok=True)
    _panel("panel_001", episode_dir / "02_panels")
    script = _script([_beat("b1", "只有一句解说词。", ["panel_001"])])
    _write_script(series_dir, episode_no, script)
    _write_audio_manifest(series_dir, episode_no, [_audio("b1", 900, 200)])

    timeline = build_timeline_from_workspace(series_dir, episode_no)
    assert timeline.episode_id == "ep_001"
    assert timeline.width == 1080
    assert timeline.height == 1920
    assert timeline.fps == 30
    assert timeline.aspect_ratio == "9:16"
    assert len(timeline.scenes) == 1
    assert len(timeline.subtitles) == 1


# --------------------------------------------------------------------------
# 8. 缺输入时报错
# --------------------------------------------------------------------------
def test_missing_script_raises_timeline_error(tmp_path: Path) -> None:
    series_dir = tmp_path / "series"
    episode_no = 1
    episode_dir = series_dir / "episodes" / f"ep_{episode_no:03d}"
    (episode_dir / "02_panels").mkdir(parents=True, exist_ok=True)
    _panel("panel_001", episode_dir / "02_panels")
    _write_audio_manifest(series_dir, episode_no, [_audio("b1", 1000, 250)])

    with pytest.raises(TimelineError) as excinfo:
        run_stage6(series_dir, episode_no)
    assert "script.json" in str(excinfo.value)


def test_missing_audio_manifest_raises_timeline_error(tmp_path: Path) -> None:
    series_dir = tmp_path / "series"
    episode_no = 1
    episode_dir = series_dir / "episodes" / f"ep_{episode_no:03d}"
    (episode_dir / "02_panels").mkdir(parents=True, exist_ok=True)
    _panel("panel_001", episode_dir / "02_panels")
    _write_script(
        series_dir,
        episode_no,
        _script([_beat("b1", "只有一句解说词。", ["panel_001"])]),
    )
    # 05_audio 存在但没有 wav（Stage 5 还没跑），也没有清单 → 必须报错而不是瞎估。
    (episode_dir / "05_audio").mkdir(parents=True, exist_ok=True)

    with pytest.raises(TimelineError) as excinfo:
        run_stage6(series_dir, episode_no)
    assert "audio_manifest.json" in str(excinfo.value)


# --------------------------------------------------------------------------
# 9. 总时长与最后一个 scene 的终点一致
# --------------------------------------------------------------------------
def test_total_duration_matches_last_scene_end(
    three_beats: list[ScriptBeat], three_audios: list[AudioClipMeta], panels: list[PanelMeta]
) -> None:
    timeline = build_timeline(
        episode_id="ep_001",
        script=_script(three_beats),
        audios=three_audios,
        panels=panels,
    )
    last_scene_end = timeline.scenes[-1].end_ms
    assert abs(timeline.total_duration_ms - last_scene_end) <= GRID
    # 字幕终点不超过总长。
    assert timeline.subtitles[-1].end_ms <= timeline.total_duration_ms


def test_focus_hint_shifts_camera_band(panels: list[PanelMeta]) -> None:
    """Patch v2.6-5：focus_hint=top/bottom 时场景改用对应取景带并标记 focus_band。"""
    top_beat = ScriptBeat(
        beat_id="b1",
        narration_text="黑框里的内心独白先讲一遍上半格。",
        target_panel_ids=["panel_001"],
        motion=CameraMotion.ZOOM_IN,
        focus_hint="top",
    )
    bottom_beat = ScriptBeat(
        beat_id="b2",
        narration_text="下半格的人物口头台词接着讲。",
        target_panel_ids=["panel_001"],
        motion=CameraMotion.PAN_DOWN,
        focus_hint="bottom",
    )
    full_beat = ScriptBeat(
        beat_id="b3",
        narration_text="这一格没有特别要求，整格照常展示。",
        target_panel_ids=["panel_002"],
        motion=CameraMotion.STATIC,
    )
    timeline = build_timeline(
        episode_id="ep_001",
        script=_script([top_beat, bottom_beat, full_beat]),
        audios=[
            _audio("b1", 1000, 0),
            _audio("b2", 1000, 0),
            _audio("b3", 1000, 0),
        ],
        panels=panels,
    )
    by_beat = {scene.beat_id: scene for scene in timeline.scenes}
    assert by_beat["b1"].focus_band is True
    assert by_beat["b1"].focus_region == (0.0, 0.0, 1.0, 0.5)
    assert by_beat["b2"].focus_band is True
    assert by_beat["b2"].focus_region == (0.0, 0.5, 1.0, 1.0)
    # focus_hint="full"（默认）：沿用原行为，不标记取景带。
    assert by_beat["b3"].focus_band is False
    assert by_beat["b3"].focus_region is None
