"""PATCH v4.30：Stage 7 运镜手感（速度封顶 / 起止定格 / Smoothstep / 接力扫镜）。

覆盖四类：

* 超长竖格（如 1:5）竖扫的**舒适速度封顶**——再长的长图也不硬加速；
* Smoothstep 缓动与起止定格的**表达式边界**；
* 同格多拍的**接力区间切分**（Stage 6 写 ratio、Stage 7 读 ratio）；
* ``zoom_in`` / ``zoom_out`` 的**随句长自适应幅度**；
* 以及一次**真实 ffmpeg 渲染**验证（无语法 / 越界错误，输出 1080×1920）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from core.schemas import CameraMotion, RenderTimeline, TimelineScene
from stages.s4_scriptwriter import apply_auto_motions
from stages.s6_timeline import _assign_pan_relay
from stages.s7_renderer import (
    PAN_MAX_SPEED_X_SS,
    PAN_MAX_SPEED_Y_SS,
    SS_HEIGHT,
    SS_WIDTH,
    _motion_expressions,
    build_scene_filter,
)

FFMPEG_BIN = os.environ.get("S7_FFMPEG_BIN", "ffmpeg")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _scene(
    tmp_path: Path,
    motion: CameraMotion,
    *,
    duration_ms: int = 2000,
    fps: int = 30,
    **kwargs: Any,
) -> tuple[TimelineScene, RenderTimeline]:
    scene = TimelineScene(
        scene_index=0,
        beat_id="b1",
        panel_id="panel_001",
        image_path=str(tmp_path / "panel.png"),
        start_ms=0,
        end_ms=duration_ms,
        duration_ms=duration_ms,
        motion=motion,
        **kwargs,
    )
    timeline = RenderTimeline(
        episode_id="ep_001",
        total_duration_ms=duration_ms,
        scenes=[scene],
        audios=[],
        subtitles=[],
    )
    return scene, timeline


def _fragment(
    tmp_path: Path,
    motion: CameraMotion,
    source_size: tuple[int, int] | None,
    *,
    duration_ms: int = 2000,
    fps: int = 30,
    **kwargs: Any,
) -> str:
    scene, timeline = _scene(
        tmp_path, motion, duration_ms=duration_ms, fps=fps, **kwargs
    )
    return build_scene_filter(
        scene, timeline, work_dir=tmp_path, fps=fps, source_size=source_size
    )


def _expected_budget(frames: int, fps: int) -> tuple[int, int, float]:
    """返回 (定格帧数, 有效运动帧数, 有效运动时长)。"""
    hold = max(0, min(int(round(0.18 * fps)), max(0, (frames - 2) // 6)))
    active = max(1, frames - 2 * hold)
    return hold, active, active / max(1, fps)


# ---------------------------------------------------------------------------
# 1. 速度封顶
# ---------------------------------------------------------------------------
def test_tall_panel_pan_speed_is_capped(tmp_path: Path) -> None:
    """超长竖格（1:5）竖扫：行程受「舒适速度 × 有效运动时长」封顶，不硬加速。"""
    duration_ms = 2000
    fps = 30
    frames = int(round(duration_ms / 1000 * fps))
    fragment = _fragment(
        tmp_path, CameraMotion.PAN_DOWN, (800, 2400), duration_ms=duration_ms, fps=fps
    )

    _, active, active_sec = _expected_budget(frames, fps)
    cap = PAN_MAX_SPEED_Y_SS * active_sec

    # 表达式里的行程是 min(可动行程, 封顶行程)——两层都必须在。
    assert "min((ih-2440)," in fragment
    assert f"{cap:.4f}" in fragment
    # 封顶值不得超过舒适速度上限换算出来的行程。
    assert cap <= PAN_MAX_SPEED_Y_SS * active_sec + 1e-6
    # 1:5 长图的可动行程远大于封顶行程：镜头只能扫到舒适速度为止。
    scaled_h = SS_WIDTH * 2400 / 800
    full_travel = scaled_h - SS_HEIGHT
    assert full_travel > cap


def test_horizontal_pan_speed_cap_is_independent(tmp_path: Path) -> None:
    """横扫有独立的（略高）速度上限，且同样封顶。"""
    duration_ms = 2000
    fps = 30
    frames = int(round(duration_ms / 1000 * fps))
    fragment = _fragment(
        tmp_path, CameraMotion.PAN_RIGHT, (1800, 1000), duration_ms=duration_ms, fps=fps
    )
    _, _, active_sec = _expected_budget(frames, fps)
    cap = PAN_MAX_SPEED_X_SS * active_sec
    assert "min((iw-2040)," in fragment
    assert f"{cap:.4f}" in fragment
    assert PAN_MAX_SPEED_X_SS > PAN_MAX_SPEED_Y_SS


def test_short_scene_does_not_pan_beyond_speed_cap(tmp_path: Path) -> None:
    """极短句（0.2 秒）也必须有封顶，且定格不会吃掉整段画面。"""
    duration_ms = 200
    fps = 30
    frames = max(1, int(round(duration_ms / 1000 * fps)))
    fragment = _fragment(
        tmp_path, CameraMotion.PAN_DOWN, (800, 2400), duration_ms=duration_ms, fps=fps
    )
    hold, active, active_sec = _expected_budget(frames, fps)
    assert hold == 0  # 帧数太少，不允许定格，否则画面完全不动
    assert active >= 1
    assert f"{PAN_MAX_SPEED_Y_SS * active_sec:.4f}" in fragment


# ---------------------------------------------------------------------------
# 2. Smoothstep 与起止定格
# ---------------------------------------------------------------------------
def test_pan_uses_smoothstep_with_hold_frames(tmp_path: Path) -> None:
    """扫镜用 Smoothstep 缓动，且起手 / 收尾各留一段定格。"""
    duration_ms = 2000
    fps = 30
    frames = int(round(duration_ms / 1000 * fps))
    fragment = _fragment(
        tmp_path, CameraMotion.PAN_DOWN, (800, 2400), duration_ms=duration_ms, fps=fps
    )
    hold, active, _ = _expected_budget(frames, fps)
    span = max(1, active - 1)
    progress = f"clip((n-{hold})/{span}.0,0,1)"
    assert progress in fragment
    assert f"3-2*({progress})" in fragment
    # 进度被夹在 0~1：起手（n < hold）静止、收尾（n > hold+span）静止。
    assert "clip(" in fragment and ",0,1)" in fragment


def test_pan_up_and_pan_left_move_towards_start(tmp_path: Path) -> None:
    """``pan_up`` / ``pan_left`` 从行程末端往回扫（表达式里是「终点减行程」）。"""
    up = _fragment(tmp_path, CameraMotion.PAN_UP, (800, 2400))
    assert "(ih-2440)-min((ih-2440)," in up
    left = _fragment(tmp_path, CameraMotion.PAN_LEFT, (1800, 1000))
    assert "(iw-2040)-min((iw-2040)," in left


def test_focus_band_pan_is_centred(tmp_path: Path) -> None:
    """有取景带时，扫镜围绕焦点范围居中，而不是一头撞到边角。"""
    fragment = _fragment(
        tmp_path,
        CameraMotion.PAN_DOWN,
        (1200, 2400),
        focus_region=(0.0, 0.0, 1.0, 0.5),
        focus_band=True,
    )
    assert "((ih-2440)-min((ih-2440)," in fragment


# ---------------------------------------------------------------------------
# 3. 接力扫镜
# ---------------------------------------------------------------------------
def test_relay_ratios_split_same_panel_group() -> None:
    """同格连续平移场景：按句长比例切分 [0,1]，相邻段有衔接余量。"""
    scenes = [
        TimelineScene(
            scene_index=0,
            beat_id="b1",
            panel_id="p1",
            image_path="p1.png",
            start_ms=0,
            end_ms=1000,
            duration_ms=1000,
            motion=CameraMotion.PAN_DOWN,
        ),
        TimelineScene(
            scene_index=1,
            beat_id="b2",
            panel_id="p1",
            image_path="p1.png",
            start_ms=1000,
            end_ms=3000,
            duration_ms=2000,
            motion=CameraMotion.PAN_DOWN,
        ),
        TimelineScene(
            scene_index=2,
            beat_id="b3",
            panel_id="p1",
            image_path="p1.png",
            start_ms=3000,
            end_ms=4000,
            duration_ms=1000,
            motion=CameraMotion.PAN_DOWN,
        ),
    ]
    _assign_pan_relay(scenes)
    ratios = [(s.pan_start_ratio, s.pan_end_ratio) for s in scenes]
    assert all(start is not None and end is not None for start, end in ratios)
    # 第一段从 0 开始，最后一段到 1 结束。
    assert ratios[0][0] == pytest.approx(0.0)
    assert ratios[-1][1] == pytest.approx(1.0)
    # 时长比例 1 : 2 : 1 → 区间中点约在 0.25 / 0.75 附近。
    assert ratios[0][1] == pytest.approx(0.25, abs=0.03)
    assert ratios[1][0] == pytest.approx(0.25, abs=0.03)
    assert ratios[1][1] == pytest.approx(0.75, abs=0.03)
    assert ratios[2][0] == pytest.approx(0.75, abs=0.03)
    # 单调递增，且相邻段首尾相接（允许 0.05 的衔接余量）。
    for previous, current in zip(ratios, ratios[1:]):
        assert current[0] <= previous[1] + 1e-6
        assert current[0] >= previous[1] - 0.06


def test_relay_breaks_on_panel_change() -> None:
    """换分镜就不再接力（ratio 保持 None）。"""
    scenes = [
        TimelineScene(
            scene_index=0,
            beat_id="b1",
            panel_id="p1",
            image_path="p1.png",
            start_ms=0,
            end_ms=1000,
            duration_ms=1000,
            motion=CameraMotion.PAN_DOWN,
        ),
        TimelineScene(
            scene_index=1,
            beat_id="b2",
            panel_id="p2",
            image_path="p2.png",
            start_ms=1000,
            end_ms=2000,
            duration_ms=1000,
            motion=CameraMotion.PAN_DOWN,
        ),
    ]
    _assign_pan_relay(scenes)
    assert scenes[0].pan_start_ratio is None
    assert scenes[1].pan_end_ratio is None


def test_relay_ignores_non_pan_motions() -> None:
    """非平移运镜不参与接力。"""
    scenes = [
        TimelineScene(
            scene_index=0,
            beat_id="b1",
            panel_id="p1",
            image_path="p1.png",
            start_ms=0,
            end_ms=1000,
            duration_ms=1000,
            motion=CameraMotion.ZOOM_IN,
        ),
        TimelineScene(
            scene_index=1,
            beat_id="b2",
            panel_id="p1",
            image_path="p1.png",
            start_ms=1000,
            end_ms=2000,
            duration_ms=1000,
            motion=CameraMotion.ZOOM_IN,
        ),
    ]
    _assign_pan_relay(scenes)
    assert scenes[0].pan_start_ratio is None
    assert scenes[1].pan_end_ratio is None


def test_relay_ratio_is_consumed_by_pan_filter(tmp_path: Path) -> None:
    """Stage 7 读接力区间：镜头从自己那一段的起点出发。"""
    fragment = _fragment(
        tmp_path,
        CameraMotion.PAN_DOWN,
        (800, 2400),
        pan_start_ratio=0.25,
        pan_end_ratio=0.75,
    )
    assert "0.250000*(ih-2440)" in fragment
    assert "min((0.750000-0.250000)*(ih-2440)," in fragment


# ---------------------------------------------------------------------------
# 4. 推拉自适应
# ---------------------------------------------------------------------------
def test_zoom_motions_adapt_to_duration() -> None:
    """zoom_in / zoom_out 随句长自适应：1.0↔1.08 之间完整推到位。

    Patch v4.31：幅度由 1.10 微收紧到 1.08（推得太满与静帧对比生硬）。
    """
    zoom_in, _, _ = _motion_expressions("zoom_in", 30)
    zoom_out, _, _ = _motion_expressions("zoom_out", 30)
    assert zoom_in.startswith("1.0+0.08*(")
    assert zoom_out.startswith("1.08-0.08*(")
    # 进度按帧序号计算，短句也能推到位。
    assert "clip(on/29.0,0,1)" in zoom_in
    assert "3-2*(clip(on/29.0,0,1))" in zoom_in
    # 长句用更长的时间轴，但推拉幅度恒定。
    long_in, _, _ = _motion_expressions("zoom_in", 120)
    assert "clip(on/119.0,0,1)" in long_in
    assert long_in.startswith("1.0+0.08*(")


def test_zoom_motion_fragment_has_adaptive_expression(tmp_path: Path) -> None:
    fragment = _fragment(tmp_path, CameraMotion.ZOOM_IN, (800, 1000), duration_ms=1000)
    assert "zoompan=z='1.0+0.08*(" in fragment
    assert "0.0008" not in fragment
    # Patch v4.31：不再使用旧的 1.10 幅度。
    assert "0.10*(" not in fragment


# ---------------------------------------------------------------------------
# 5. 真实 ffmpeg 渲染
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    shutil.which(FFMPEG_BIN) is None, reason="需要 ffmpeg 才能做真实渲染验证"
)
def test_real_ffmpeg_render_pan_scene(tmp_path: Path) -> None:
    """真实 ffmpeg 渲染：1:5 长竖格竖扫 + 接力区间，输出 1080×1920 且无越界。"""
    from PIL import Image, ImageDraw

    ffmpeg = shutil.which(FFMPEG_BIN)
    assert ffmpeg is not None
    panel = tmp_path / "tall.png"
    image = Image.new("RGB", (800, 2400), "white")
    draw = ImageDraw.Draw(image)
    for index in range(0, 2400, 200):
        draw.rectangle((300, index, 500, index + 120), fill=(220, 30, 30))
    image.save(panel)

    fragment = _fragment(
        tmp_path,
        CameraMotion.PAN_DOWN,
        (800, 2400),
        duration_ms=2000,
        pan_start_ratio=0.0,
        pan_end_ratio=0.5,
    )
    output = tmp_path / "scene.mp4"
    proc = subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-framerate", "30", "-i", str(panel),
            "-filter_complex", fragment,
            "-map", "[s0]",
            "-frames:v", "30",
            "-fps_mode", "passthrough",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Invalid too big" not in proc.stderr
    assert output.exists() and output.stat().st_size > 0

    frame = tmp_path / "frame.png"
    subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(output),
         "-frames:v", "1", str(frame)],
        capture_output=True,
        text=True,
        check=True,
    )
    with Image.open(frame) as shot:
        assert shot.size == (1080, 1920)


@pytest.mark.skipif(
    shutil.which(FFMPEG_BIN) is None, reason="需要 ffmpeg 才能做真实渲染验证"
)
def test_real_ffmpeg_render_zoom_scene(tmp_path: Path) -> None:
    """真实 ffmpeg 渲染：自适应推拉同样不报错。"""
    from PIL import Image, ImageDraw

    ffmpeg = shutil.which(FFMPEG_BIN)
    assert ffmpeg is not None
    panel = tmp_path / "normal.png"
    image = Image.new("RGB", (800, 1000), "white")
    ImageDraw.Draw(image).rectangle((300, 400, 500, 600), fill=(30, 90, 220))
    image.save(panel)

    fragment = _fragment(tmp_path, CameraMotion.ZOOM_IN, (800, 1000), duration_ms=1000)
    output = tmp_path / "zoom.mp4"
    proc = subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-framerate", "30", "-i", str(panel),
            "-filter_complex", fragment,
            "-map", "[s0]",
            "-frames:v", "30",
            "-fps_mode", "passthrough",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert output.exists() and output.stat().st_size > 0


# ---------------------------------------------------------------------------
# 6. 自动动效：长竖格接力竖扫
# ---------------------------------------------------------------------------
def test_auto_motions_keeps_pan_down_for_relay_beats() -> None:
    """Patch v4.31：取消竖扫 → 同一张长竖格连拍「第 1 拍动、第 2 拍静」交替。

    同图第 3 拍在上一拍已静的前提下补一次轻动，第 4 拍再静回去。
    """
    from core.schemas import PanelMeta

    meta = PanelMeta(
        panel_id="p_tall",
        image_path="p_tall.png",
        width=800,
        height=2400,
        aspect_ratio=800 / 2400,
        source_slice_range=(0, 0),
        is_tall_panel=True,
    )
    beats = [
        {
            "beat_id": f"b{index}",
            "narration_text": f"第{index}句。",
            "tts_text": f"第{index}句。",
            "speaker_role": "narrator",
            "target_panel_ids": ["p_tall"],
            "motion": "zoom_in",
            "pause_after_ms": 320,
        }
        for index in range(1, 5)
    ]
    assigned, _ = apply_auto_motions(beats, panel_metas=[meta])
    motions = [beat["motion"] for beat in assigned]
    # 竖扫彻底下线：自动分配永不产出 pan_down / pan_up。
    assert "pan_down" not in motions
    assert "pan_up" not in motions
    # 同图防跳变：第 1 拍动、第 2 拍静。
    assert motions[0] != "static"
    assert motions[1] == "static"
    # 第 3 拍补一次轻动，第 4 拍再静。
    assert motions[2] != "static"
    assert motions[3] == "static"


def test_auto_motions_relay_keeps_panel_content_untouched() -> None:
    """自动分配只改 motion，不动解说文本与停顿（Patch v4.31 取消竖扫后依然成立）。"""
    from core.schemas import PanelMeta

    meta = PanelMeta(
        panel_id="p_tall",
        image_path="p_tall.png",
        width=800,
        height=2400,
        aspect_ratio=800 / 2400,
        source_slice_range=(0, 0),
        is_tall_panel=True,
    )
    beats = [
        {
            "beat_id": "b1",
            "narration_text": "第一句。",
            "tts_text": "第一句。",
            "speaker_role": "narrator",
            "target_panel_ids": ["p_tall"],
            "motion": "zoom_in",
            "pause_after_ms": 480,
            "voice_override": "voice_custom",
        },
        {
            "beat_id": "b2",
            "narration_text": "第二句。",
            "tts_text": "第二句。",
            "speaker_role": "narrator",
            "target_panel_ids": ["p_tall"],
            "motion": "zoom_in",
            "pause_after_ms": 520,
            "voice_override": "voice_custom",
        },
    ]
    assigned, _ = apply_auto_motions(beats, panel_metas=[meta])
    assert [beat["motion"] for beat in assigned] == ["zoom_in", "static"]
    assert [beat["narration_text"] for beat in assigned] == ["第一句。", "第二句。"]
    assert [beat["pause_after_ms"] for beat in assigned] == [480, 520]
    assert all(beat["voice_override"] == "voice_custom" for beat in assigned)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
