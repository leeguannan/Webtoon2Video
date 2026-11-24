"""PATCH v4.31：取消竖扫 + 「同画防跳变 / 呼吸节奏」自动动效回归。

覆盖：

* 自动链路彻底取消 ``pan_down`` / ``pan_up``（长竖格改走推拉 / 静止）；
* 同画面「第 1 拍动、第 2 拍静」交替，消除同图连动回弹跳帧；
* 连续动态上限（3 拍）后强制插一拍静止呼吸；
* 禁止连震；
* 引导短句静帧化；
* 横向宽格才给左右横扫（宽高比阈值 1.35）；
* Stage 7 推拉幅度收紧到 1.08；
* Stage 6 接力只对「同方向」连续平移成组，反向运镜倒序切段。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.schemas import CameraMotion, PanelMeta, RenderTimeline, TimelineScene
from stages.s4_scriptwriter import apply_auto_motions
from stages.s6_timeline import _assign_pan_relay
from stages.s7_renderer import _motion_expressions, build_scene_filter

# ---------------------------------------------------------------------------
# 造数辅助
# ---------------------------------------------------------------------------


def _panel_meta(
    panel_id: str,
    width: int,
    height: int,
    *,
    tall: bool = False,
) -> PanelMeta:
    return PanelMeta(
        panel_id=panel_id,
        image_path=f"{panel_id}.png",
        width=width,
        height=height,
        aspect_ratio=(width / height) if height else 0.0,
        source_slice_range=(0, 0),
        is_tall_panel=tall,
    )


def _beat(
    beat_id: str,
    text: str,
    panel_id: str,
    *,
    motion: str = "zoom_in",
    sfx: str | None = None,
    emotion: str | None = None,
    pause: int = 320,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "beat_id": beat_id,
        "narration_text": text,
        "tts_text": text,
        "speaker_role": "narrator",
        "target_panel_ids": [panel_id],
        "motion": motion,
        "pause_after_ms": pause,
        "voice_override": "",
    }
    if sfx is not None:
        payload["sfx_cue"] = sfx
    if emotion is not None:
        payload["emotion"] = emotion
    return payload


def _scene(
    index: int,
    panel_id: str,
    motion: CameraMotion,
    duration_ms: int,
) -> TimelineScene:
    return TimelineScene(
        scene_index=index,
        beat_id=f"b{index}",
        panel_id=panel_id,
        image_path=f"{panel_id}.png",
        start_ms=index * duration_ms,
        end_ms=(index + 1) * duration_ms,
        duration_ms=duration_ms,
        motion=motion,
    )


def _fragment(tmp_path: Path, motion: CameraMotion) -> str:
    scene = TimelineScene(
        scene_index=0,
        beat_id="b1",
        panel_id="panel_001",
        image_path=str(tmp_path / "panel.png"),
        start_ms=0,
        end_ms=1000,
        duration_ms=1000,
        motion=motion,
    )
    timeline = RenderTimeline(
        episode_id="ep_001",
        total_duration_ms=1000,
        scenes=[scene],
        audios=[],
        subtitles=[],
    )
    return build_scene_filter(
        scene,
        timeline,
        work_dir=tmp_path,
        fps=30,
        source_size=(800, 1000),
    )


# ---------------------------------------------------------------------------
# 1. 取消竖扫
# ---------------------------------------------------------------------------


def test_tall_panel_never_gets_vertical_pan() -> None:
    """超长竖格不再自动分配竖扫（竖扫只保留在底层渲染，兼容老工程）。"""
    metas = [_panel_meta("p_tall", 800, 2400, tall=True)]
    beats = [
        _beat(f"b{index}", f"这是长竖格上的第{index}句解说词。", "p_tall")
        for index in range(1, 5)
    ]
    assigned, report = apply_auto_motions(beats, panel_metas=metas)
    motions = [beat["motion"] for beat in assigned]
    assert "pan_down" not in motions
    assert "pan_up" not in motions
    assert report.tall_pan_count == 0


def test_legacy_vertical_pan_beats_are_replaced() -> None:
    """老剧本里的 pan_down / pan_up 重新自动分配时会被替换掉。"""
    metas = [_panel_meta("p_tall", 800, 2400, tall=True)]
    beats = [
        _beat("b1", "第一句解说词在这里。", "p_tall", motion="pan_down"),
        _beat("b2", "第二句解说词在这里。", "p_tall", motion="pan_up"),
    ]
    assigned, report = apply_auto_motions(beats, panel_metas=metas)
    motions = [beat["motion"] for beat in assigned]
    assert "pan_down" not in motions
    assert "pan_up" not in motions
    assert report.changed_beat_ids == ["b1", "b2"]


# ---------------------------------------------------------------------------
# 2. 同画面防跳变
# ---------------------------------------------------------------------------


def test_same_panel_second_beat_is_static() -> None:
    """同画面连拍：第 1 拍动、第 2 拍强制静，消除同图连动回弹跳帧。"""
    metas = [_panel_meta("p_normal", 1000, 1200)]
    beats = [
        _beat("b1", "同一张画面上的第一句解说词。", "p_normal"),
        _beat("b2", "同一张画面上的第二句解说词。", "p_normal"),
    ]
    assigned, _ = apply_auto_motions(beats, panel_metas=metas)
    motions = [beat["motion"] for beat in assigned]
    assert motions[0] != "static"
    assert motions[1] == "static"


def test_same_panel_four_beats_alternate() -> None:
    """同图 4 拍：动-静-动-静 交替，第 3 拍补一次轻动。"""
    metas = [_panel_meta("p_tall", 800, 2400, tall=True)]
    beats = [
        _beat(f"b{index}", f"这是同一张长图上的第{index}句解说。", "p_tall")
        for index in range(1, 5)
    ]
    assigned, _ = apply_auto_motions(beats, panel_metas=metas)
    motions = [beat["motion"] for beat in assigned]
    assert motions[0] != "static"
    assert motions[1] == "static"
    assert motions[2] != "static"
    assert motions[3] == "static"
    # 轻动必须与上一次动的方向不同（换向），避免一路推到底。
    assert motions[2] != motions[0]


# ---------------------------------------------------------------------------
# 3. 呼吸节奏
# ---------------------------------------------------------------------------


def test_dynamic_streak_limit_inserts_breath() -> None:
    """连续动态最多 3 拍，第 4 拍必须插静止呼吸。"""
    metas = [_panel_meta(f"p{index}", 1000, 1200) for index in range(5)]
    beats = [
        _beat(f"b{index}", f"这是第{index}句比较长的解说内容。", f"p{index}")
        for index in range(5)
    ]
    assigned, _ = apply_auto_motions(beats, panel_metas=metas)
    motions = [beat["motion"] for beat in assigned]
    assert motions[3] == "static"
    # 前三拍都是动态。
    assert all(motion != "static" for motion in motions[:3])
    # 呼吸之后重新起动，不至于死屏。
    assert motions[4] != "static"


def test_static_streak_avoids_dead_screen() -> None:
    """连续多拍静止后切入新图，必须补一次推拉，避免死屏。"""
    metas = [_panel_meta(f"p{index}", 1000, 1200) for index in range(5)]
    beats = [
        _beat(
            f"b{index}",
            "短句：" if index < 4 else "转入新画面的一句解说内容。",
            f"p{index}",
            pause=40 if index < 4 else 320,
        )
        for index in range(5)
    ]
    assigned, _ = apply_auto_motions(beats, panel_metas=metas)
    motions = [beat["motion"] for beat in assigned]
    assert motions[4] != "static"


def test_guide_short_sentence_becomes_static() -> None:
    """短引导句（净字数 ≤10 且冒号结尾 / 停顿极短）优先静止。"""
    metas = [_panel_meta("p_normal", 1000, 1200)]
    beats = [
        _beat("b1", "看这里：", "p_normal", pause=60),
        _beat("b2", "这是正文的第一句解说内容。", "p_normal", pause=320),
    ]
    assigned, _ = apply_auto_motions(beats, panel_metas=metas)
    motions = [beat["motion"] for beat in assigned]
    assert motions[0] == "static"
    assert motions[1] != "static"


def test_shake_never_consecutive() -> None:
    """禁止连震：任意相邻两拍都不能同时是 shake。"""
    metas = [_panel_meta(f"p{index}", 1000, 1200) for index in range(6)]
    beats = [
        _beat(f"b{index}", "砰！", f"p{index}", sfx="crash") for index in range(6)
    ]
    assigned, _ = apply_auto_motions(beats, panel_metas=metas)
    motions = [beat["motion"] for beat in assigned]
    for index in range(1, len(motions)):
        assert not (motions[index - 1] == "shake" and motions[index] == "shake")


def test_shake_followed_by_release() -> None:
    """刚震完的下一拍优先泄力（静止或拉远），不给继续震动。"""
    metas = [_panel_meta(f"p{index}", 1000, 1200) for index in range(2)]
    beats = [
        _beat("b1", "砰！一拳砸在桌上。", "p1", sfx="crash"),
        _beat("b2", "然后一切归于安静。", "p2"),
    ]
    assigned, _ = apply_auto_motions(beats, panel_metas=metas)
    motions = [beat["motion"] for beat in assigned]
    assert motions[0] == "shake"
    assert motions[1] in ("static", "zoom_out")


# ---------------------------------------------------------------------------
# 4. 横向宽格
# ---------------------------------------------------------------------------


def test_wide_panel_gets_horizontal_sweep() -> None:
    """宽高比 ≥ 1.35 的宽格才给左右横扫，且连续宽格交替方向。"""
    metas = [
        _panel_meta("p_wide_a", 1600, 900),
        _panel_meta("p_wide_b", 1600, 900),
    ]
    beats = [
        _beat("b1", "第一张宽图上的解说内容。", "p_wide_a"),
        _beat("b2", "第二张宽图上的解说内容。", "p_wide_b"),
    ]
    assigned, report = apply_auto_motions(beats, panel_metas=metas)
    motions = {beat["beat_id"]: beat["motion"] for beat in assigned}
    assert motions["b1"] == "pan_right"
    assert motions["b2"] == "pan_left"
    assert report.wide_pan_count == 2


def test_narrow_wide_ratio_is_not_swept() -> None:
    """宽高比不足 1.35 的「偏宽」格不再算宽格，不给横扫。"""
    metas = [_panel_meta("p_mid", 1300, 1000)]
    beats = [_beat("b1", "偏宽格上的第一句解说内容。", "p_mid")]
    assigned, report = apply_auto_motions(beats, panel_metas=metas)
    motions = [beat["motion"] for beat in assigned]
    assert "pan_left" not in motions
    assert "pan_right" not in motions
    assert report.wide_pan_count == 0


# ---------------------------------------------------------------------------
# 5. Stage 7：推拉幅度收紧到 1.08
# ---------------------------------------------------------------------------


def test_zoom_amplitude_tightened_to_1_08() -> None:
    """推拉幅度从 1.10 微收紧到 1.08，与静帧对比更自然耐看。"""
    zoom_in, _, _ = _motion_expressions("zoom_in", 30)
    zoom_out, _, _ = _motion_expressions("zoom_out", 30)
    assert zoom_in.startswith("1.0+0.08*(")
    assert zoom_out.startswith("1.08-0.08*(")
    assert "0.10*(" not in zoom_in
    assert "0.10*(" not in zoom_out


def test_zoom_fragment_uses_1_08(tmp_path: Path) -> None:
    fragment = _fragment(tmp_path, CameraMotion.ZOOM_IN)
    assert "zoompan=z='1.0+0.08*(" in fragment
    assert "0.10*(" not in fragment


# ---------------------------------------------------------------------------
# 6. Stage 6：接力只对同方向成组，反向倒序
# ---------------------------------------------------------------------------


def test_pan_relay_groups_same_direction_only() -> None:
    """连续同方向横扫成组接力；方向相反（right → left）必须断开。"""
    scenes = [
        _scene(0, "panel_001", CameraMotion.PAN_RIGHT, 1000),
        _scene(1, "panel_001", CameraMotion.PAN_RIGHT, 1000),
        _scene(2, "panel_001", CameraMotion.PAN_LEFT, 1000),
    ]
    _assign_pan_relay(scenes)
    # 前两拍同向 → 接力区间切分（第二段起点 > 0）。
    assert scenes[0].pan_start_ratio is not None
    assert scenes[0].pan_end_ratio is not None
    assert scenes[1].pan_start_ratio is not None
    assert scenes[1].pan_start_ratio > scenes[0].pan_start_ratio
    # 反向那一拍没有同向伙伴 → 不参与接力（ratio 保持 None）。
    assert scenes[2].pan_start_ratio is None
    assert scenes[2].pan_end_ratio is None


def test_reverse_pan_relay_is_descending() -> None:
    """反向运镜（pan_left）按 1.0 → 0.0 倒序切段，不折返。"""
    scenes = [
        _scene(0, "panel_001", CameraMotion.PAN_LEFT, 1000),
        _scene(1, "panel_001", CameraMotion.PAN_LEFT, 1000),
    ]
    _assign_pan_relay(scenes)
    first = scenes[0]
    second = scenes[1]
    assert first.pan_start_ratio is not None and first.pan_end_ratio is not None
    assert second.pan_start_ratio is not None and second.pan_end_ratio is not None
    # 倒序：后一段的起点更小（镜头继续往回走，而不是跳回开头）。
    assert second.pan_start_ratio < first.pan_start_ratio
    assert second.pan_end_ratio <= first.pan_end_ratio


def test_pan_relay_breaks_on_panel_change() -> None:
    """换分镜即断开接力。"""
    scenes = [
        _scene(0, "panel_001", CameraMotion.PAN_RIGHT, 1000),
        _scene(1, "panel_002", CameraMotion.PAN_RIGHT, 1000),
    ]
    _assign_pan_relay(scenes)
    assert scenes[0].pan_start_ratio is None
    assert scenes[1].pan_start_ratio is None


def test_pan_relay_ignores_non_pan_motion() -> None:
    """非平移运镜不参与接力。"""
    scenes = [
        _scene(0, "panel_001", CameraMotion.ZOOM_IN, 1000),
        _scene(1, "panel_001", CameraMotion.ZOOM_IN, 1000),
    ]
    _assign_pan_relay(scenes)
    assert scenes[0].pan_start_ratio is None
    assert scenes[1].pan_start_ratio is None
