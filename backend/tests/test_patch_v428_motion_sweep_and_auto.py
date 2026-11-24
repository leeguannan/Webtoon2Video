"""Patch v4.28：扫镜不压扁 + 横向扫镜 + 全自动分配画面动效。

覆盖四组：

1. **S7 纵向扫镜**：超长竖格的 ``pan_down`` / ``pan_up`` 走「等比缩放 → 2x
   超采样 → 滑动 crop → 缩回」，链路上有 ``setsar=1`` 与 ``crop=w=2040:h=2440``，
   **不再出现会把长图压扁的 ``zoompan=z='1.0'``**；
2. **S7 横向扫镜**：宽图（宽 / 高 > 安全框比）的 ``pan_left`` / ``pan_right``
   按高度等比放大后横向滑动 crop（保留原比例），超窄横幅有放大倍率上限保护；
3. **自动分配动效引擎**：``apply_auto_motions`` 按分镜尺寸（长竖格 / 宽格）、
   音效与情绪分配运镜，并打散连续重复（不得连震 3 拍、不得连用同一动效 3 拍）；
4. **Web API**���``POST /api/episodes/{id}/auto-motions`` 正常落盘
   ``script.json`` / ``script_readable.md``，且**不破坏** ``voice_override``
   与 ``pause_after_ms``，也不触碰音频清单。

全部离线：纯函数 + TestClient，不触网、不消耗百炼额度。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.config import AppConfig, WorkspaceConfig
from core.schemas import (
    CameraMotion,
    EpisodeDigest,
    NarrationScript,
    PanelMeta,
    RenderTimeline,
    ScriptBeat,
    TimelineScene,
    load_model,
    save_model,
)
from stages.s4_scriptwriter import apply_auto_motions, load_episode_panel_metas
from stages.s7_renderer import build_scene_filter
from web.server import create_app

#: 允许用环境变量指定 ffmpeg（CI 里可能是绝对路径）。
FFMPEG_BIN = os.environ.get("S7_FFMPEG_BIN", "ffmpeg")


# --------------------------------------------------------------------------
# S7 运镜链路
# --------------------------------------------------------------------------
def _motion_fragment(
    tmp_path: Path,
    motion: CameraMotion,
    source_size: tuple[int, int],
    *,
    frames: int = 30,
) -> str:
    """构造单个场景的前景滤镜片段（只取前景链，便于断言运镜细节）。"""
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
        source_size=source_size,
    )


def test_tall_panel_pan_down_uses_supersampled_crop(tmp_path: Path) -> None:
    """超长竖格竖扫：2x 超采样 + 滑动 crop，不再有压扁长图的 zoompan。"""
    fragment = _motion_fragment(tmp_path, CameraMotion.PAN_DOWN, (800, 2400))
    # 等比缩放到 2040 宽（高度按比例），绝不强制压到 1020x1220。
    assert "scale=2040:-2" in fragment
    assert "scale=1020:1220:force_original_aspect_ratio=decrease" not in fragment
    # 像素宽高比归一 + 2x 超采样视窗。
    assert "setsar=1" in fragment
    assert "crop=w=2040:h=2440" in fragment
    # Patch v4.30：滑动改为「速度封顶 + 起止定格 + Smoothstep 缓动」，
    # n 从 0 计，hold=4 帧、active=22 帧（span=21）。
    assert "min((ih-2440)," in fragment
    assert "clip((n-4)/21.0,0,1)" in fragment
    # 最后缩回安全框。
    assert "scale=1020:1220:flags=lanczos" in fragment
    # 旧链路（会把长图压扁、并把取样 y 夹死）必须彻底消失。
    assert "zoompan=" not in fragment
    assert "z='1.0'" not in fragment


def test_tall_panel_pan_up_scrolls_backwards(tmp_path: Path) -> None:
    """超长竖格上移：crop 的 y 方向与竖扫相反（从底扫到顶）。"""
    fragment = _motion_fragment(tmp_path, CameraMotion.PAN_UP, (800, 2400))
    assert "scale=2040:-2" in fragment
    assert "setsar=1" in fragment
    assert "crop=w=2040:h=2440" in fragment
    # Patch v4.30：从行程末端往回扫（同样是速度封顶 + Smoothstep）。
    assert "(ih-2440)-min((ih-2440)," in fragment
    assert "clip((n-4)/21.0,0,1)" in fragment
    assert "zoompan=" not in fragment


def test_wide_panel_uses_horizontal_sliding_crop(tmp_path: Path) -> None:
    """宽图横扫：按高 2440 等比放大后横向滑动 crop，保留原比例。"""
    right = _motion_fragment(tmp_path, CameraMotion.PAN_RIGHT, (1800, 1000))
    assert "scale=-2:2440" in right
    assert "setsar=1" in right
    assert "crop=w=2040:h=2440" in right
    # Patch v4.30：横向同样是速度封顶 + Smoothstep（横扫上限略高于竖扫）。
    assert "min((iw-2040)," in right
    assert "clip((n-4)/21.0,0,1)" in right
    assert "y='0'" in right
    assert "scale=1020:1220:flags=lanczos" in right
    assert "zoompan=" not in right

    left = _motion_fragment(tmp_path, CameraMotion.PAN_LEFT, (1800, 1000))
    assert "(iw-2040)-min((iw-2040)," in left
    assert "scale=-2:2440" in left
    assert "zoompan=" not in left


def test_narrow_banner_limits_scale_factor(tmp_path: Path) -> None:
    """超窄横幅（宽高比 > 2.8）：放大倍率封顶 2.2x，并补透明边到 2440 高。"""
    fragment = _motion_fragment(tmp_path, CameraMotion.PAN_RIGHT, (3000, 400))
    assert "2.2" in fragment
    assert "pad=" in fragment
    assert "crop=w=2040:h=2440" in fragment
    assert "zoompan=" not in fragment


def test_zoom_motions_keep_setsar(tmp_path: Path) -> None:
    """推拉 / 震动 / 静止的 scale 之后统一补 setsar=1，避免非方形像素。"""
    for motion in (
        CameraMotion.ZOOM_IN,
        CameraMotion.ZOOM_OUT,
        CameraMotion.SHAKE,
        CameraMotion.STATIC,
    ):
        fragment = _motion_fragment(tmp_path, motion, (800, 1000))
        assert "setsar=1" in fragment, motion.value


def _focus_motion_fragment(
    tmp_path: Path,
    motion: CameraMotion,
    source_size: tuple[int, int],
    focus_region: tuple[float, float, float, float],
) -> str:
    """构造带 focus 取景带的场景滤镜片段（越界取景带会被夹逼）。"""
    scene = TimelineScene(
        scene_index=0,
        beat_id="b1",
        panel_id="panel_001",
        image_path=str(tmp_path / "panel.png"),
        start_ms=0,
        end_ms=1000,
        duration_ms=1000,
        motion=motion,
        focus_region=focus_region,
        focus_band=True,
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
        source_size=source_size,
    )


def test_out_of_bounds_focus_region_does_not_misjudge_tall_panel(
    tmp_path: Path,
) -> None:
    """越界 focus 取景带必须按**裁完后的比例**判定走 fit 分支。

    回归：真实成片曾在 ``focus_region=(0, 0.5, 1, 1)``（下边界越界，ffmpeg 实际
    只裁到高 0.5）时，被 Python 侧按未夹逼的比例误判成「超长竖格」，
    ``scale=2040:-2`` 后画面高不足 2440，滑动 crop 越界导致整条渲染失败。
    """
    fragment = _focus_motion_fragment(
        tmp_path,
        CameraMotion.PAN_DOWN,
        (690, 1518),
        (0.0, 0.5, 1.0, 1.0),
    )
    # 取景带被夹逼到画面内（高度只剩 0.5）。
    assert "h='ih*0.5000'" in fragment
    # 裁完是 690×759（接近方图），不能走超长竖格的定宽缩放。
    assert "scale=2040:-2" not in fragment
    # 必须留出 1.16x 余量并补齐画布。
    assert "1.16" in fragment
    assert "pad=w='max(2040,iw)'" in fragment
    assert "pad=w='max(2040,iw)':h='max(2440,ih)'" in fragment


def test_pan_chain_always_pads_to_supersampled_canvas(tmp_path: Path) -> None:
    """四个平移运镜都必须补透明边到 ≥2040×2440，滑动 crop 才不会越界。"""
    for motion, size in (
        (CameraMotion.PAN_DOWN, (800, 2400)),
        (CameraMotion.PAN_UP, (800, 2400)),
        (CameraMotion.PAN_LEFT, (1800, 1000)),
        (CameraMotion.PAN_RIGHT, (3000, 400)),
    ):
        fragment = _motion_fragment(tmp_path, motion, size)
        assert "pad=w='max(2040,iw)':h='max(2440,ih)'" in fragment, motion.value
        assert "crop=w=2040:h=2440" in fragment, motion.value


def test_pan_chain_survives_real_ffmpeg_with_clamped_focus(
    tmp_path: Path,
) -> None:
    """真机回归：越界 focus 取景带 + 平移运镜，ffmpeg 不再报 crop 越界。"""
    if shutil.which(FFMPEG_BIN) is None:
        pytest.skip("本机没有 ffmpeg，跳过真实渲染回归")
    panel = tmp_path / "panel.png"
    Image.new("RGB", (690, 1518), (30, 40, 60)).save(panel)
    fragment = _focus_motion_fragment(
        tmp_path,
        CameraMotion.PAN_DOWN,
        (690, 1518),
        (0.0, 0.5, 1.0, 1.0),
    )
    completed = subprocess.run(
        [
            FFMPEG_BIN,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(panel),
            "-filter_complex",
            fragment,
            "-map",
            "[s0]",
            "-frames:v",
            "2",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Invalid too big" not in completed.stderr


# --------------------------------------------------------------------------
# 自动分配画面动效
# --------------------------------------------------------------------------
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
    voice_override: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "beat_id": beat_id,
        "narration_text": text,
        "tts_text": text,
        "speaker_role": "narrator",
        "target_panel_ids": [panel_id],
        "motion": motion,
        "pause_after_ms": pause,
        "voice_override": voice_override,
    }
    if sfx is not None:
        payload["sfx_cue"] = sfx
    if emotion is not None:
        payload["emotion"] = emotion
    return payload


def test_auto_motions_assigns_by_panel_geometry() -> None:
    """长竖格 → 竖扫（Patch v4.30 起同格第 2 拍接力竖扫）；宽格 → 左右横扫。"""
    metas = [
        _panel_meta("p_tall", 800, 2400, tall=True),
        _panel_meta("p_wide_a", 1600, 900),
        _panel_meta("p_wide_b", 1600, 900),
        _panel_meta("p_normal", 1000, 1200),
    ]
    beats = [
        _beat("b1", "超长竖格第一句。", "p_tall"),
        _beat("b2", "超长竖格第二句。", "p_tall"),
        _beat("b3", "宽图第一句。", "p_wide_a"),
        _beat("b4", "宽图第二句。", "p_wide_b"),
        _beat("b5", "普通竖格一句。", "p_normal"),
    ]
    assigned, report = apply_auto_motions(beats, panel_metas=metas)
    motions = {beat["beat_id"]: beat["motion"] for beat in assigned}
    # 规则 A：长竖格首拍竖扫；Patch v4.30 起同格第 2 拍继续竖扫（接力扫镜）。
    assert motions["b1"] == "pan_down"
    assert motions["b2"] == "pan_down"
    # 规则 B：宽格左右横扫交替。
    assert motions["b3"] == "pan_right"
    assert motions["b4"] == "pan_left"
    # 规则 D：普通竖格默认推近。
    assert motions["b5"] == "zoom_in"
    # updated_count 只统计「被改动」的拍（b5 原本就是 zoom_in，未变）。
    assert report.updated_count == 4
    assert report.changed_beat_ids == ["b1", "b2", "b3", "b4"]
    assert report.tall_pan_count == 2
    assert report.wide_pan_count == 2
    assert report.push_pull_count == 0


def test_auto_motions_assigns_shake_on_impact() -> None:
    """规则 C：冲击音效或愤怒 / 震惊短句配 shake，且不得连震 3 拍。"""
    metas = [_panel_meta("p_normal", 1000, 1200)]
    beats = [
        _beat("b1", "砰！一拳砸在桌上。", "p_normal", sfx="crash"),
        _beat("b2", "怒吼", "p_normal", emotion="angry"),
        _beat("b3", "再怒", "p_normal", emotion="angry"),
        _beat("b4", "还怒", "p_normal", emotion="angry"),
    ]
    assigned, report = apply_auto_motions(beats, panel_metas=metas)
    motions = [beat["motion"] for beat in assigned]
    assert motions[0] == "shake"
    assert motions[1] == "shake"
    # 连震上限 2 拍：第 3 拍必须换掉。
    assert motions[2] != "shake"
    # 全程不得出现 3 连震。
    for index in range(2, len(motions)):
        assert len(set(motions[index - 2 : index + 1])) > 1
    assert report.shake_count == sum(1 for m in motions if m == "shake")


def test_auto_motions_breaks_repeated_motion() -> None:
    """规则 D：连续 2 拍同动效时第 3 拍自动打散，不出现 3 连。"""
    metas = [
        _panel_meta("p_tall_a", 800, 2400, tall=True),
        _panel_meta("p_tall_b", 800, 2400, tall=True),
        _panel_meta("p_tall_c", 800, 2400, tall=True),
        _panel_meta("p_normal_a", 1000, 1200),
        _panel_meta("p_normal_b", 1000, 1200),
        _panel_meta("p_normal_c", 1000, 1200),
    ]
    beats = [
        _beat("b1", "长格一。", "p_tall_a"),
        _beat("b2", "长格二。", "p_tall_b"),
        _beat("b3", "长格三。", "p_tall_c"),
        _beat("b4", "普通一。", "p_normal_a"),
        _beat("b5", "普通二。", "p_normal_b"),
        _beat("b6", "普通三。", "p_normal_c"),
    ]
    assigned, _ = apply_auto_motions(beats, panel_metas=metas)
    motions = [beat["motion"] for beat in assigned]
    for index in range(2, len(motions)):
        window = motions[index - 2 : index + 1]
        assert len(set(window)) > 1, window
    # 三个普通竖格连续出现时也必须有变化。
    assert len(set(motions[3:6])) > 1


def test_auto_motions_preserves_other_fields() -> None:
    """自动分配只改 motion：文本 / 停顿 / 音色 / 双文本分离都不动。"""
    metas = [_panel_meta("p_tall", 800, 2400, tall=True)]
    beats = [
        _beat(
            "b1",
            "超长竖格第一句。",
            "p_tall",
            pause=765,
            voice_override="voice_x",
        )
    ]
    assigned, report = apply_auto_motions(beats, panel_metas=metas)
    assert assigned[0]["motion"] == "pan_down"
    assert assigned[0]["narration_text"] == "超长竖格第一句。"
    assert assigned[0]["tts_text"] == "超长竖格第一句。"
    assert assigned[0]["pause_after_ms"] == 765
    assert assigned[0]["voice_override"] == "voice_x"
    # 入参不被就地修改（纯函数）。
    assert beats[0]["motion"] == "zoom_in"
    assert report.changed_beat_ids == ["b1"]


def test_load_episode_panel_metas_falls_back_to_scan(tmp_path: Path) -> None:
    """没有清单文件时按 02_panels 的图片尺寸兜底。"""
    panels_dir = tmp_path / "02_panels"
    panels_dir.mkdir(parents=True)
    Image.new("RGB", (800, 2400), (10, 20, 30)).save(panels_dir / "panel_001.png")
    metas = load_episode_panel_metas(tmp_path)
    assert len(metas) == 1
    assert metas[0].panel_id == "panel_001"
    assert metas[0].width == 800
    assert metas[0].height == 2400


# --------------------------------------------------------------------------
# Web API
# --------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path / "ws"))
    return TestClient(create_app(config))


def _make_source(root: Path) -> Path:
    source = root / "src"
    folder = source / "ep_001"
    folder.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 600), (30, 60, 90)).save(folder / "001.png")
    return source


def _seed_episode_artifacts(workspace: Path, series_id: str) -> Path:
    """铺出 auto-motions 所需的 script.json / audio_manifest.json / 面板图。"""
    episode_dir = workspace / series_id / "episodes" / "ep_001"
    panels_dir = episode_dir / "02_panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (800, 2400), (10, 20, 30)).save(panels_dir / "panel_001.png")
    Image.new("RGB", (1600, 900), (20, 30, 40)).save(panels_dir / "panel_002.png")

    beats = [
        ScriptBeat(
            beat_id="b1",
            narration_text="长竖格的一句解说。",
            tts_text="长竖格的一句解说。",
            target_panel_ids=["panel_001"],
            motion="static",
            pause_after_ms=520,
            voice_override="voice_custom",
        ),
        ScriptBeat(
            beat_id="b2",
            narration_text="宽图的一句解说。",
            tts_text="宽图的一句解说。",
            target_panel_ids=["panel_002"],
            motion="static",
            pause_after_ms=480,
        ),
    ]
    script = NarrationScript(
        episode_id=f"{series_id}_ep_001",
        hook_title="自动动效测试",
        beats=beats,
        episode_digest=EpisodeDigest(
            episode_no=1,
            one_line_hook="钩子",
            summary="摘要",
        ),
    )
    script_dir = episode_dir / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    save_model(script, script_dir / "script.json")
    (script_dir / "script_readable.md").write_text("# 解说文稿\n", encoding="utf-8")

    audio_dir = episode_dir / "05_audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    manifest = [
        {
            "beat_id": "b1",
            "audio_path": "05_audio/b1.wav",
            "duration_ms": 1000,
            "pause_after_ms": 520,
            "word_timestamps": [],
        },
        {
            "beat_id": "b2",
            "audio_path": "05_audio/b2.wav",
            "duration_ms": 1000,
            "pause_after_ms": 480,
            "word_timestamps": [],
        },
    ]
    (audio_dir / "audio_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return episode_dir


def test_auto_motions_endpoint_writes_script(
    client: TestClient, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    source = _make_source(workspace)
    series_id = client.post("/api/series", json={"url": str(source)}).json()["series_id"]
    episode_dir = _seed_episode_artifacts(workspace, series_id)
    episode_id = f"{series_id}_ep_001"

    response = client.post(f"/api/episodes/{episode_id}/auto-motions")
    assert response.status_code == 200, response.text
    payload = response.json()
    summary = payload["summary"]
    assert summary["updated_count"] == 2
    assert summary["tall_pan_count"] == 1
    assert summary["wide_pan_count"] == 1

    motions = {beat["beat_id"]: beat["motion"] for beat in payload["beats"]}
    assert motions["b1"] == "pan_down"
    assert motions["b2"] == "pan_right"

    # 落盘：script.json 写入新运镜，script_readable.md 同步更新。
    script = load_model(NarrationScript, episode_dir / "04_script" / "script.json")
    assert [beat.motion.value for beat in script.beats] == ["pan_down", "pan_right"]
    readable = (episode_dir / "04_script" / "script_readable.md").read_text(
        encoding="utf-8"
    )
    assert "PAN_DOWN" in readable or "pan_down" in readable

    # 不破坏 voice_override 与 pause_after_ms，也不碰音频清单。
    assert script.beats[0].voice_override == "voice_custom"
    assert script.beats[0].pause_after_ms == 520
    assert script.beats[1].pause_after_ms == 480
    manifest = json.loads(
        (episode_dir / "05_audio" / "audio_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest[0]["pause_after_ms"] == 520
    assert manifest[1]["pause_after_ms"] == 480


def test_auto_motions_endpoint_404_without_script(
    client: TestClient, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    source = _make_source(workspace)
    series_id = client.post("/api/series", json={"url": str(source)}).json()["series_id"]
    response = client.post(f"/api/episodes/{series_id}_ep_001/auto-motions")
    assert response.status_code == 404
