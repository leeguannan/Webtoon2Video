"""Stage 7（FFmpeg 成片渲染）的离线测试。

测试会**真的调用本机 ffmpeg** 编码出片（没有 ffmpeg 的机器整体 skip）。
素材全部在临时目录里现造：Pillow 面板图 + ``wave`` 合成音频 + 字幕 PNG，
不触碰网络、不依赖 Stage 2~6 的真实产物。
"""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from core.config import AppConfig, RenderConfig
from core.schemas import (
    AudioClipMeta,
    CameraMotion,
    RenderTimeline,
    SubtitleLine,
    TimelineScene,
    save_model,
)
from stages.s7_renderer import (
    _MAX_SCENES_PER_SEGMENT,
    RenderError,
    build_scene_filter,
    dedupe_series_export_links,
    exports_dir_for,
    out_dir_for,
    probe_duration_ms,
    render_episode,
    render_timeline,
    run_ffmpeg,
)

#: 允许用环境变量指定 ffmpeg（CI 里可能是绝对路径）。
FFMPEG_BIN = os.environ.get("S7_FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = shutil.which("ffprobe")

pytestmark = pytest.mark.skipif(
    shutil.which(FFMPEG_BIN) is None,
    reason="本机没有 ffmpeg，跳过 Stage 7 的真实渲染测试",
)

#: 真渲染用例统一用 ultrafast 预设跑（断言强度不变，只是不在测试里浪费时间）。
#: 生产链路仍走 RenderConfig.video_preset（默认 medium）。
TEST_VIDEO_PRESET = "ultrafast"

#: 真渲染时用的轻量 AppConfig：只把 x264 预设换成 ultrafast，其余与默认一致。
FAST_RENDER_CONFIG = AppConfig(render=RenderConfig(video_preset=TEST_VIDEO_PRESET))


# --------------------------------------------------------------------------
# 素材构造
# --------------------------------------------------------------------------
def _write_wav(path: Path, seconds: float, freq: float = 440.0, amp: float = 0.25) -> None:
    """写一段单声道正弦 wav（真实可解码的音频，不是空文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 44100
    total = int(rate * seconds)
    frames = bytearray()
    for i in range(total):
        value = amp * math.sin(2 * math.pi * freq * i / rate)
        frames += struct.pack("<h", int(value * 32767))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))


def _write_panel(path: Path, color: tuple[int, int, int]) -> None:
    """写一张 800×1400 的面板图（中间一块明显的纯色，便于抽帧验颜色）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (800, 1400), (18, 22, 38))
    draw = ImageDraw.Draw(image)
    draw.rectangle([80, 160, 720, 1240], fill=color)
    draw.ellipse([300, 520, 500, 720], fill=(250, 240, 60))
    image.save(path)


def _write_subtitle_png(path: Path) -> None:
    """写一张 1080×1920 的 RGBA 字幕图：底部一块**纯红不透明**色块。

    为什么用纯色块而不是文字：本机 ffmpeg 没有 libfreetype，测试端无法可靠渲染字体；
    纯色块能被 yuv420p 量化后仍精确判定（RGB→YUV→RGB 的红几乎不变）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle([300, 1500, 780, 1700], fill=(255, 0, 0, 255))
    image.save(path)


def _make_workspace(root: Path) -> tuple[Path, RenderTimeline]:
    """造一话的完整 Stage 6 产物（时间轴 + 字幕 PNG + 面板 + 音频）。"""
    episode_dir = root / "episodes" / "ep_001"
    plan_dir = episode_dir / "06_plan"
    (plan_dir / "subtitles").mkdir(parents=True, exist_ok=True)
    (episode_dir / "02_panels").mkdir(parents=True, exist_ok=True)
    (episode_dir / "05_audio").mkdir(parents=True, exist_ok=True)

    _write_panel(episode_dir / "02_panels" / "panel_001.png", (200, 50, 50))
    _write_panel(episode_dir / "02_panels" / "panel_002.png", (50, 80, 200))
    _write_wav(episode_dir / "05_audio" / "a1.wav", 0.8, 300.0)
    _write_wav(episode_dir / "05_audio" / "a2.wav", 0.8, 500.0)
    _write_subtitle_png(plan_dir / "subtitles" / "0001.png")

    scenes = [
        TimelineScene(
            scene_index=0,
            beat_id="beat_001",
            panel_id="panel_001",
            image_path="02_panels/panel_001.png",
            start_ms=0,
            end_ms=1000,
            duration_ms=1000,
            motion=CameraMotion.ZOOM_IN,
        ),
        TimelineScene(
            scene_index=1,
            beat_id="beat_002",
            panel_id="panel_002",
            image_path="02_panels/panel_002.png",
            start_ms=1000,
            end_ms=2000,
            duration_ms=1000,
            motion=CameraMotion.PAN_DOWN,
        ),
    ]
    audios = [
        AudioClipMeta(beat_id="beat_001", audio_path="a1.wav", duration_ms=800, pause_after_ms=200),
        AudioClipMeta(beat_id="beat_002", audio_path="a2.wav", duration_ms=800, pause_after_ms=200),
    ]
    subtitles = [
        SubtitleLine(index=1, beat_id="beat_001", text="第一句解说", start_ms=0, end_ms=1000),
    ]
    timeline = RenderTimeline(
        episode_id="ep_001",
        total_duration_ms=2000,
        scenes=scenes,
        audios=audios,
        subtitles=subtitles,
    )
    save_model(timeline, plan_dir / "timeline.json")
    return episode_dir, timeline


# --------------------------------------------------------------------------
# 共享渲染产物（真实编码较慢，module 级只渲一次）
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, RenderTimeline, Path]:
    """渲染一次成片，供时长/编码/字幕三个断言复用。"""
    root = tmp_path_factory.mktemp("s7_render")
    episode_dir, timeline = _make_workspace(root)
    out_path = root / "out" / "ep_001.mp4"
    render_timeline(
        timeline,
        episode_dir=episode_dir,
        out_path=out_path,
        ffmpeg_bin=FFMPEG_BIN,
        work_dir=root / "work",
        video_preset=TEST_VIDEO_PRESET,
    )
    return out_path, timeline, root


def _probe_streams(path: Path) -> list[dict]:
    """用 ffprobe 读流信息（宽高 / 编码名）。"""
    if not FFPROBE_BIN:
        pytest.skip("本机没有 ffprobe，跳过流信息断言")
    completed = subprocess.run(
        [
            FFPROBE_BIN,
            "-v",
            "error",
            "-show_entries",
            "stream=width,height,codec_name,codec_type",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return list(json.loads(completed.stdout).get("streams", []))


def _grab_frame(video: Path, at_s: float, out_png: Path) -> Image.Image:
    """从成片里抽一帧并读回像素。"""
    subprocess.run(
        [
            FFMPEG_BIN,
            "-y",
            "-hide_banner",
            "-ss",
            f"{at_s:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-update",
            "1",
            str(out_png),
        ],
        capture_output=True,
        check=True,
    )
    with Image.open(out_png) as image:
        return image.convert("RGB")


# --------------------------------------------------------------------------
# 1. run_ffmpeg
# --------------------------------------------------------------------------
def test_run_ffmpeg_returns_stdout() -> None:
    """正常执行返回 stdout。"""
    output = run_ffmpeg(["-hide_banner", "-version"], ffmpeg_bin=FFMPEG_BIN)
    assert "ffmpeg" in output.lower()


def test_run_ffmpeg_raises_with_stderr_tail() -> None:
    """参数错误时抛 RenderError，且异常里带 stderr 的关键片段。"""
    with pytest.raises(RenderError) as excinfo:
        run_ffmpeg(["-definitely-not-a-real-option"], ffmpeg_bin=FFMPEG_BIN)
    message = str(excinfo.value)
    assert "ffmpeg" in message
    assert "Unrecognized option" in message or "definitely-not-a-real-option" in message
    # 异常里只保留日志尾部，不该把整份 ffmpeg 日志塞进来。
    assert len(message) < 4000


# --------------------------------------------------------------------------
# 2. 成片产物与时长
# --------------------------------------------------------------------------
@pytest.mark.slow
def test_render_timeline_writes_mp4(rendered: tuple[Path, RenderTimeline, Path]) -> None:
    """产出 mp4，文件存在且大小 > 0。"""
    out_path, _timeline, _root = rendered
    assert out_path.is_file()
    assert out_path.stat().st_size > 0


@pytest.mark.slow
def test_render_timeline_duration_matches_timeline(rendered: tuple[Path, RenderTimeline, Path]) -> None:
    """成片时长与 ``total_duration_ms`` 的误差在容差内。

    容差 **±300ms**：30fps 下约 ±9 帧。误差来源是容器时长取整与 AAC 帧长
    （1024 samples ≈ 23ms），渲染本身按 ``-t`` 精确截断，不会差到秒级。
    """
    out_path, timeline, _root = rendered
    duration_ms = probe_duration_ms(out_path, ffmpeg_bin=FFMPEG_BIN)
    assert abs(duration_ms - timeline.total_duration_ms) <= 300


# --------------------------------------------------------------------------
# 3. 画幅与编码
# --------------------------------------------------------------------------
@pytest.mark.slow
def test_output_is_1080x1920_h264_aac(rendered: tuple[Path, RenderTimeline, Path]) -> None:
    """输出必须是 1080×1920 的 h264 视频流 + aac 音频流。"""
    out_path, _timeline, _root = rendered
    streams = _probe_streams(out_path)
    video = next(s for s in streams if s.get("codec_type") == "video")
    audio = next(s for s in streams if s.get("codec_type") == "audio")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert video["codec_name"] == "h264"
    assert audio["codec_name"] == "aac"


# --------------------------------------------------------------------------
# 4. 字幕烧录
# --------------------------------------------------------------------------
@pytest.mark.slow
def test_subtitle_png_is_burned_into_frame(rendered: tuple[Path, RenderTimeline, Path]) -> None:
    """字幕 PNG 里的红色块必须出现在成片对应时间窗的帧上。"""
    out_path, _timeline, root = rendered
    inside = _grab_frame(out_path, 0.5, root / "frame_inside.png")
    red, green, blue = inside.getpixel((540, 1600))
    assert red > 200 and green < 60 and blue < 60, f"字幕像素应为红色，实际 {(red, green, blue)}"


@pytest.mark.slow
def test_subtitle_only_shows_inside_its_time_window(rendered: tuple[Path, RenderTimeline, Path]) -> None:
    """字幕只在 ``[start_ms, end_ms]`` 窗口内出现（enable=between 生效）。"""
    out_path, _timeline, root = rendered
    outside = _grab_frame(out_path, 1.5, root / "frame_outside.png")
    red, green, blue = outside.getpixel((540, 1600))
    assert not (red > 200 and green < 60 and blue < 60), "字幕窗口外不应出现红色块"


# --------------------------------------------------------------------------
# 5. render_episode 双份落盘
# --------------------------------------------------------------------------
@pytest.mark.slow
def test_render_episode_writes_both_copies(tmp_path: Path) -> None:
    """``07_out/{episode_id}.mp4`` 与 ``exports/EP{话次:03d}_{title}.mp4`` 都要有。

    Patch v4.11：两份成片必须是**硬链接**（同 inode），同盘只占一份体积，
    且渲染结束后 ``07_out/_work`` 工作目录被清理干净。
    """
    episode_dir, _timeline = _make_workspace(tmp_path)
    out_path = render_episode(
        tmp_path,
        1,
        ffmpeg_bin=FFMPEG_BIN,
        title="测试标题",
        config=FAST_RENDER_CONFIG,
    )

    expected = out_dir_for(tmp_path, 1) / "ep_001.mp4"
    assert out_path == expected
    assert out_path.is_file() and out_path.stat().st_size > 0

    export = exports_dir_for(tmp_path) / "EP001_测试标题.mp4"
    assert export.is_file()
    assert export.stat().st_size > 0
    assert export.stat().st_size == out_path.stat().st_size
    # 硬链接：同一 inode / 设备，链接计数为 2，不产生第二份磁盘占用。
    assert os.path.samefile(out_path, export)
    assert out_path.stat().st_ino == export.stat().st_ino
    assert out_path.stat().st_dev == export.stat().st_dev
    assert out_path.stat().st_nlink == 2
    # 工作目录在成功路径上也必须被清掉。
    assert not (out_dir_for(tmp_path, 1) / "_work").exists()


def test_render_episode_clears_work_dir_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch v4.11：渲染中途异常时 ``finally`` 仍要清掉 ``07_out/_work``。

    同时验证开跑前会先清掉上轮崩溃残留的工作目录（残留文件不会混进本次渲染）。
    """
    from stages import s7_renderer

    episode_dir, _timeline = _make_workspace(tmp_path)
    work_dir = out_dir_for(tmp_path, 1) / "_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    stale_segment = work_dir / "seg_0000.mp4"
    stale_segment.write_bytes(b"stale-crash-leftover")

    def _boom(*args, **kwargs):
        # 模拟分段渲染阶段崩溃：此时 work_dir 已被重新创建。
        assert work_dir.is_dir()
        raise RenderError("模拟渲染中途失败")

    monkeypatch.setattr(s7_renderer, "render_timeline", _boom)

    with pytest.raises(RenderError):
        render_episode(tmp_path, 1, ffmpeg_bin=FFMPEG_BIN, title="测试标题")

    # finally 必须清理整个工作目录，残留分段也不能留下。
    assert not work_dir.exists()
    assert not stale_segment.exists()


def test_dedupe_series_export_links_merges_duplicate_copies(tmp_path: Path) -> None:
    """Patch v4.11：既有的双份独立 MP4 在 dedupe 后合并为硬链接，只占一份体积。"""
    series_dir = tmp_path / "wt_dedupe"
    episode_dir = series_dir / "episodes" / "ep_001"
    out_dir = episode_dir / "07_out"
    exports_dir = series_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    exports_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / "wt_dedupe_ep_001.mp4"
    export_path = exports_dir / "EP001_final.mp4"
    payload = b"\x00" * 4096 + b"fake-mp4-payload"
    out_path.write_bytes(payload)
    # 先造成「两份独立副本、内容相同但 inode 不同」的历史状态。
    shutil.copy2(out_path, export_path)
    assert not os.path.samefile(out_path, export_path)
    inode_before = out_path.stat().st_ino
    disk_before = out_path.stat().st_size + export_path.stat().st_size

    resolved = dedupe_series_export_links(series_dir, 1, episode_id="wt_dedupe_ep_001")

    assert resolved == out_path
    assert os.path.samefile(out_path, export_path)
    assert export_path.stat().st_ino == inode_before  # 保留 07_out 原 inode，只重建 export
    assert out_path.stat().st_nlink == 2
    # 合并后两份路径逻辑大小之和仍是两份，但物理上只占一份。
    assert out_path.stat().st_size + export_path.stat().st_size == disk_before


def test_dedupe_series_export_links_restores_missing_out_copy(tmp_path: Path) -> None:
    """Patch v4.11：07_out 成片丢失但 exports 还在时，反向硬链接恢复，不丢片。"""
    series_dir = tmp_path / "wt_restore"
    episode_dir = series_dir / "episodes" / "ep_001"
    out_dir = episode_dir / "07_out"
    exports_dir = series_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    exports_dir.mkdir(parents=True, exist_ok=True)

    export_path = exports_dir / "EP001_final.mp4"
    export_path.write_bytes(b"fake-mp4" * 256)

    resolved = dedupe_series_export_links(series_dir, 1, episode_id="wt_restore_ep_001")

    out_path = out_dir / "wt_restore_ep_001.mp4"
    assert resolved == out_path
    assert out_path.is_file()
    assert os.path.samefile(out_path, export_path)


def test_dedupe_series_export_links_without_any_video(tmp_path: Path) -> None:
    """Patch v4.11：07_out 与 exports 都没有成片时返回 None，不凭空造文件。"""
    series_dir = tmp_path / "wt_empty"
    (series_dir / "episodes" / "ep_001" / "07_out").mkdir(parents=True, exist_ok=True)
    (series_dir / "exports").mkdir(parents=True, exist_ok=True)

    assert dedupe_series_export_links(series_dir, 1, episode_id="wt_empty_ep_001") is None


# --------------------------------------------------------------------------
# 6. 缺时间轴时报错
# --------------------------------------------------------------------------
def test_render_episode_without_timeline_raises(tmp_path: Path) -> None:
    """缺 ``06_plan/timeline.json`` 必须抛 RenderError（而不是别的异常类型）。"""
    (tmp_path / "episodes" / "ep_001").mkdir(parents=True)
    with pytest.raises(RenderError):
        render_episode(tmp_path, 1, ffmpeg_bin=FFMPEG_BIN)


# --------------------------------------------------------------------------
# 7. 带 BGM 也能渲染
# --------------------------------------------------------------------------
@pytest.mark.slow
def test_render_with_bgm(tmp_path: Path) -> None:
    """有 BGM 时正常渲染，且成片仍带 aac 音轨。"""
    episode_dir, timeline = _make_workspace(tmp_path)
    bgm = tmp_path / "bgm.wav"
    _write_wav(bgm, 0.5, 220.0, amp=0.5)

    out_path = render_timeline(
        timeline,
        episode_dir=episode_dir,
        out_path=tmp_path / "out" / "bgm.mp4",
        bgm_path=bgm,
        ffmpeg_bin=FFMPEG_BIN,
        work_dir=tmp_path / "work",
        video_preset=TEST_VIDEO_PRESET,
    )
    assert out_path.is_file() and out_path.stat().st_size > 0
    streams = _probe_streams(out_path)
    audio = next(s for s in streams if s.get("codec_type") == "audio")
    assert audio["codec_name"] == "aac"


# --------------------------------------------------------------------------
# 8. 滤镜片段（纯字符串断言，不跑 ffmpeg）
# --------------------------------------------------------------------------
def test_build_scene_filter_shape(tmp_path: Path) -> None:
    """场景片段必须含「模糊暗化背景 + 安全框前景 + 运镜 + 安全区叠加」四要素（Patch v3.0-8）。"""
    from stages import s7_renderer

    episode_dir, timeline = _make_workspace(tmp_path)
    scene = timeline.scenes[0]
    fragment = build_scene_filter(scene, timeline, work_dir=tmp_path / "work", fps=30)

    # 背景：放大铺满 + 规范固定的 boxblur 与 eq 暗化降饱和（旧 gblur/colorchannelmixer 已移除）。
    assert "force_original_aspect_ratio=increase" in fragment
    assert "boxblur=30:4" in fragment
    assert "eq=brightness=-0.38:saturation=0.75" in fragment
    assert "gblur" not in fragment and "colorchannelmixer" not in fragment
    # 前景：等比缩放进 1020×1220 安全框，zoompan 输出尺寸即安全框（不再是 1080×1920）。
    assert "force_original_aspect_ratio=decrease" in fragment
    assert "zoompan=" in fragment
    assert "d=30" in fragment and "s=1020x1220" in fragment
    assert "s=1080x1920" not in fragment
    # 合成：固定叠加公式，前景（pad 后恒高 1220）下沿 = 180+(1260-1220)+1220 = 1420 <= 1450。
    assert "overlay=x=(W-w)/2:y=180+(1260-h)/2" in fragment
    overlay_y = s7_renderer.SAFE_TOP + (
        s7_renderer.SAFE_ZONE_HEIGHT - s7_renderer.SAFE_BOX_HEIGHT
    ) // 2
    overlay_end = overlay_y + s7_renderer.SAFE_BOX_HEIGHT
    assert overlay_y == 200 and overlay_end == 1420
    assert overlay_end <= 1450
    assert s7_renderer.SAFE_BOTTOM_LIMIT == 1440


def test_path_helpers(tmp_path: Path) -> None:
    """目录辅助函数的路径形状。"""
    assert out_dir_for(tmp_path, 7) == tmp_path / "episodes" / "ep_007" / "07_out"


def test_sidechain_uses_gentle_v32_ducking(monkeypatch, tmp_path: Path) -> None:
    """柔和漫评闪避参数被钉死（Patch v4.37 起闪避上移到整片母带阶段）。

    BGM 先经 ``loudnorm`` 统一到 -16 LUFS 再降 -8.0dB，基线已规范化。
    Patch v4.37 之前闪避在**每个分段**里跑（threshold=0.06 / ratio=2.0 /
    attack=30ms / release=350ms），分段之间会因各自响度不同而跳变；现在改成
    在整片母带阶段闪避一次（双源侧链：人声 + 音效都能压 BGM），
    参数为 threshold=0.06 / ratio=2.4 / attack=25ms / release=350ms
    （FFmpeg 的 sidechaincompress 用**秒**，故实际传入 0.025 / 0.350）。
    """
    from stages import s7_renderer

    # 旧的分段实时混音参数保留（向后兼容分支仍在），但整片母带走新参数。
    assert s7_renderer._SIDECHAIN_THRESHOLD == 0.06
    assert s7_renderer._SIDECHAIN_RATIO == 2.0
    assert s7_renderer._SIDECHAIN_ATTACK_MS == 30
    assert s7_renderer._SIDECHAIN_RELEASE_MS == 350
    assert s7_renderer._DUCK_THRESHOLD == 0.06
    assert s7_renderer._DUCK_RATIO == 2.4
    assert s7_renderer._DUCK_ATTACK_MS == 25
    assert s7_renderer._DUCK_RELEASE_MS == 350

    captured: list[list[str]] = []

    def fake_run_ffmpeg(args, *, ffmpeg_bin="ffmpeg", timeout_s=900):
        captured.append(list(args))
        # 真的 ffmpeg 会写出产物；这里只造占位文件，让 render_timeline 的产物校验通过。
        target = Path(args[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake")
        return ""

    monkeypatch.setattr(s7_renderer, "run_ffmpeg", fake_run_ffmpeg)

    episode_dir, timeline = _make_workspace(tmp_path)
    bgm = tmp_path / "bgm.wav"
    _write_wav(bgm, 0.5, 220.0, amp=0.5)
    render_timeline(
        timeline,
        episode_dir=episode_dir,
        out_path=tmp_path / "out" / "sidechain.mp4",
        bgm_path=bgm,
        ffmpeg_bin=FFMPEG_BIN,
        work_dir=tmp_path / "work",
    )

    joined = " ".join(" ".join(call) for call in captured)
    assert "sidechaincompress=" in joined
    # sidechaincompress 的 attack/release 单位是**秒**，毫秒常量必须换算后再传。
    assert (
        f"threshold={s7_renderer._DUCK_THRESHOLD}:ratio={s7_renderer._DUCK_RATIO}:"
        f"attack={s7_renderer._DUCK_ATTACK_MS / 1000.0:.3f}:"
        f"release={s7_renderer._DUCK_RELEASE_MS / 1000.0:.3f}"
    ) in joined
    assert exports_dir_for(tmp_path) == tmp_path / "exports"


# --------------------------------------------------------------------------
# 分段渲染（长话内存保护）
# --------------------------------------------------------------------------
def _make_long_workspace(root: Path, *, scene_count: int = 20) -> tuple[Path, RenderTimeline]:
    """构造一个 ``scene_count`` 场景的长时间轴（每场景 1 秒）。"""
    episode_dir = root / "episodes" / "ep_001"
    plan_dir = episode_dir / "06_plan"
    (plan_dir / "subtitles").mkdir(parents=True, exist_ok=True)
    (episode_dir / "02_panels").mkdir(parents=True, exist_ok=True)
    (episode_dir / "05_audio").mkdir(parents=True, exist_ok=True)

    scenes: list[TimelineScene] = []
    audios: list[AudioClipMeta] = []
    subtitles: list[SubtitleLine] = []
    for index in range(scene_count):
        name = f"{index + 1:03d}"
        _write_panel(
            episode_dir / "02_panels" / f"panel_{name}.png",
            ((index * 7) % 200 + 20, 60, 120),
        )
        _write_wav(episode_dir / "05_audio" / f"a{name}.wav", 0.6, 300.0 + index * 5)
        # 字幕 PNG 按 SubtitleLine.index 命名（Stage 6 的 {index:04d}.png 约定）
        _write_subtitle_png(plan_dir / "subtitles" / f"{index + 1:04d}.png")
        start = index * 1000
        scenes.append(
            TimelineScene(
                scene_index=index,
                beat_id=f"beat_{name}",
                panel_id=f"panel_{name}",
                image_path=f"02_panels/panel_{name}.png",
                start_ms=start,
                end_ms=start + 1000,
                duration_ms=1000,
                motion=CameraMotion.ZOOM_IN,
            )
        )
        audios.append(
            AudioClipMeta(
                beat_id=f"beat_{name}",
                audio_path=f"a{name}.wav",
                duration_ms=600,
                pause_after_ms=400,
            )
        )
        subtitles.append(
            SubtitleLine(
                index=index + 1,
                beat_id=f"beat_{name}",
                text=f"第{index + 1}句",
                start_ms=start,
                end_ms=start + 1000,
            )
        )
    timeline = RenderTimeline(
        episode_id="ep_001",
        total_duration_ms=scene_count * 1000,
        scenes=scenes,
        audios=audios,
        subtitles=subtitles,
    )
    save_model(timeline, plan_dir / "timeline.json")
    return episode_dir, timeline


@pytest.mark.slow
def test_long_timeline_is_rendered_in_segments(tmp_path: Path) -> None:
    """长话按分段渲染，段产物再拼接成完整成片。"""
    episode_dir, timeline = _make_long_workspace(tmp_path, scene_count=20)
    out_path = tmp_path / "out" / "ep_001.mp4"
    work_dir = tmp_path / "work"

    render_timeline(
        timeline,
        episode_dir=episode_dir,
        out_path=out_path,
        ffmpeg_bin=FFMPEG_BIN,
        work_dir=work_dir,
        video_preset=TEST_VIDEO_PRESET,
    )

    assert out_path.is_file()
    assert out_path.stat().st_size > 0
    segments = sorted(work_dir.glob("seg_*.mp4"))
    assert len(segments) == math.ceil(20 / _MAX_SCENES_PER_SEGMENT)
    duration_ms = probe_duration_ms(out_path, ffmpeg_bin=FFMPEG_BIN)
    assert abs(duration_ms - timeline.total_duration_ms) <= 500


def test_each_segment_keeps_input_count_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分段的意义：单个滤镜图的输入数不能随话长线性增长。

    整话一次拼进一个 filter_complex 时，74 场景 = 222 个输入，
    实测会把 swscale 内存打爆（"Failed initializing scaling graph"）。
    """
    from stages import s7_renderer as module

    episode_dir, timeline = _make_long_workspace(tmp_path, scene_count=20)
    calls: list[list[str]] = []

    def fake_run_ffmpeg(args: list[str], **kwargs: object) -> str:
        calls.append(list(args))
        target = Path(args[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00" * 128)
        return ""

    monkeypatch.setattr(module, "run_ffmpeg", fake_run_ffmpeg)

    render_timeline(
        timeline,
        episode_dir=episode_dir,
        out_path=tmp_path / "out" / "ep_001.mp4",
        ffmpeg_bin=FFMPEG_BIN,
        work_dir=tmp_path / "work",
    )

    # 去掉背景预渲染（每次一帧静态图）与最后的 concat 调用。
    segment_calls = [
        call
        for call in calls
        if "-filter_complex" in call
        and any("concat=n=" in str(item) for item in call)
    ]
    assert len(segment_calls) == math.ceil(20 / _MAX_SCENES_PER_SEGMENT)
    for call in segment_calls:
        input_count = call.count("-i")
        # 2×场景数（背景+前景）+ 字幕数 + 人声(+BGM)
        assert input_count <= 2 * _MAX_SCENES_PER_SEGMENT + _MAX_SCENES_PER_SEGMENT + 2


def test_focus_band_scene_gets_crop_filter(tmp_path: Path) -> None:
    """Patch v2.6-5：focus_band=True 的场景在前景链加 crop，把镜头偏移到取景带。"""
    scene = TimelineScene(
        scene_index=0,
        beat_id="b1",
        panel_id="panel_001",
        image_path=str(tmp_path / "p.png"),
        start_ms=0,
        end_ms=1000,
        duration_ms=1000,
        motion=CameraMotion.ZOOM_IN,
        focus_region=(0.0, 0.0, 1.0, 0.5),
        focus_band=True,
    )
    timeline = RenderTimeline(
        episode_id="ep_001",
        total_duration_ms=1000,
        scenes=[scene],
        audios=[],
        subtitles=[],
    )
    fragment = build_scene_filter(scene, timeline, work_dir=tmp_path)
    # 背景链本来就有 crop=1080:1920（铺满画幅），取景带裁切看的是表达式形式。
    assert "crop=w='iw*" in fragment
    assert "ih*0.5000" in fragment


def test_scene_without_focus_band_keeps_full_frame(tmp_path: Path) -> None:
    """没有 focus_band 时不裁切，原图仍完整居中（实机验收确认过的构图）。"""
    scene = TimelineScene(
        scene_index=0,
        beat_id="b1",
        panel_id="panel_001",
        image_path=str(tmp_path / "p.png"),
        start_ms=0,
        end_ms=1000,
        duration_ms=1000,
        motion=CameraMotion.ZOOM_IN,
        focus_region=(0.1, 0.1, 0.5, 0.5),
        focus_band=False,
    )
    timeline = RenderTimeline(
        episode_id="ep_001",
        total_duration_ms=1000,
        scenes=[scene],
        audios=[],
        subtitles=[],
    )
    fragment = build_scene_filter(scene, timeline, work_dir=tmp_path)
    assert "crop=w='iw*" not in fragment


# --------------------------------------------------------------------------
# 9. Patch v3.0-8：抖音安全分区版式验收
# --------------------------------------------------------------------------
def test_ass_subtitle_uses_douyin_safe_zone(tmp_path: Path) -> None:
    """外挂 ASS 必须锁 1080×1920、MarginV 落在 220~280，字幕锚在下方纯暗区。"""
    import re

    from stages import s6_timeline

    episode_dir, timeline = _make_workspace(tmp_path)
    ass_text = s6_timeline.render_ass(timeline)

    assert "PlayResX: 1080" in ass_text
    assert "PlayResY: 1920" in ass_text
    assert "WrapStyle: 0" in ass_text
    # Style 行以「MarginL, MarginR, MarginV, Encoding」结尾，MarginV 是倒数第二字段。
    style_line = next(line for line in ass_text.splitlines() if line.startswith("Style: Default,"))
    match = re.search(r",(\d+),1$", style_line)
    assert match is not None
    margin_v = int(match.group(1))
    assert 220 <= margin_v <= 280
    assert margin_v == s6_timeline.ASS_MARGIN_V == 250
    # 规范固定的字号与描边色。
    assert f",{s6_timeline.ASS_FONT_SIZE}," in style_line
    assert "&H00121216" in style_line
    # 字幕锚区：1920 - 250 = 1670 上沿，主画面下沿 1440 之下、抖音遮挡区 1720 之上。
    assert 1440 < 1920 - margin_v <= 1700


def test_foreground_overlay_never_crosses_safe_bottom(tmp_path: Path) -> None:
    """overlay 纵向终点坐标严格 <= 1450：pad 后前景恒高 1220，终点固定为 1420。"""
    from stages import s7_renderer

    episode_dir, timeline = _make_workspace(tmp_path)
    scene = timeline.scenes[0]
    fragment = build_scene_filter(scene, timeline, work_dir=tmp_path / "work", fps=30)

    # 前景先 pad 成固定 1020×1220，overlay 公式里的 h 运行时恒等于 1220。
    assert f"pad={s7_renderer.SAFE_WIDTH}:{s7_renderer.SAFE_BOX_HEIGHT}" in fragment
    assert "overlay=x=(W-w)/2:y=180+(1260-h)/2" in fragment
    overlay_y = s7_renderer.SAFE_TOP + (
        s7_renderer.SAFE_ZONE_HEIGHT - s7_renderer.SAFE_BOX_HEIGHT
    ) // 2
    overlay_end = overlay_y + s7_renderer.SAFE_BOX_HEIGHT
    assert overlay_y == 200 and overlay_end == 1420
    assert overlay_end <= 1450 and overlay_end <= s7_renderer.SAFE_BOTTOM_LIMIT


def test_tall_panel_pan_down_scans_inside_safe_window(tmp_path: Path) -> None:
    """超长竖格的 pan_down：宽度铺满 2040，在 2040×2440 超采样视窗内从顶扫到底。

    Patch v4.28：扫镜不再用 zoompan（它会把长图压扁、并把 y 取样坐标夹死），
    改为「等比缩放 → 2x 超采样 → 滑动 crop → 缩回 1020×1220」。

    Patch v4.30：滑动位置改为「速度封顶 + 起止定格 + Smoothstep 缓动」，
    因此这里断言的是新表达式而不是旧的 ``(ih-2440)*n/29`` 匀速扫。
    """

    def _pan_fragment(source_size: tuple[int, int]) -> str:
        scene = TimelineScene(
            scene_index=0,
            beat_id="b1",
            panel_id="panel_001",
            image_path=str(tmp_path / "tall.png"),
            start_ms=0,
            end_ms=1000,
            duration_ms=1000,
            motion=CameraMotion.PAN_DOWN,
        )
        timeline = RenderTimeline(
            episode_id="ep_001",
            total_duration_ms=1000,
            scenes=[scene],
            audios=[],
            subtitles=[],
        )
        return build_scene_filter(
            scene, timeline, work_dir=tmp_path, fps=30, source_size=source_size
        )

    # 800×2400（比例 3.0）触发长竖格链路：宽度铺满 2040，超采样视窗内匀速扫底。
    tall_fragment = _pan_fragment((800, 2400))
    assert "scale=2040:-2" in tall_fragment
    assert "setsar=1" in tall_fragment
    assert "crop=w=2040:h=2440" in tall_fragment
    assert "scale=1020:1220:flags=lanczos" in tall_fragment
    # Patch v4.30：速度封顶（min(可动行程, 舒适速度 × 有效时长)）+ Smoothstep 缓动。
    assert "min((ih-2440)," in tall_fragment
    assert "clip((n-4)/21.0,0,1)" in tall_fragment
    assert "3-2*(clip((n-4)/21.0,0,1))" in tall_fragment
    # 不再走旧的「缩小 + pad + 1.12 倍放大」链路，也不再有会压扁长图的 zoompan。
    assert "1.12" not in tall_fragment
    assert "force_original_aspect_ratio=decrease" not in tall_fragment
    assert "zoompan=" not in tall_fragment

    # 普通竖格（比例 1.25）改走「等比适配 + 1.16x 余量」的滑动链路。
    normal_fragment = _pan_fragment((800, 1000))
    assert "scale=2040:-2" not in normal_fragment
    assert "1.16" in normal_fragment
    assert "crop=w=2040:h=2440" in normal_fragment
    assert "zoompan=" not in normal_fragment

    # static 场景即使源图超长也不触发扫底链路。
    static_scene = TimelineScene(
        scene_index=0,
        beat_id="b1",
        panel_id="panel_001",
        image_path=str(tmp_path / "tall.png"),
        start_ms=0,
        end_ms=1000,
        duration_ms=1000,
        motion=CameraMotion.STATIC,
    )
    static_timeline = RenderTimeline(
        episode_id="ep_001",
        total_duration_ms=1000,
        scenes=[static_scene],
        audios=[],
        subtitles=[],
    )
    static_fragment = build_scene_filter(
        static_scene, static_timeline, work_dir=tmp_path, source_size=(800, 2400)
    )
    assert "scale=1020:-2" not in static_fragment
    assert "zoompan=" not in static_fragment
