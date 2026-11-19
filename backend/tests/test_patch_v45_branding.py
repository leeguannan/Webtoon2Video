"""Patch v4.5：作品级通用封页写字 + 双结尾图 + 平滑淡入淡出转场 的离线测试。

覆盖：

1. ``int_to_chinese`` / ``format_cover_label`` 的中文数字与模板逻辑；
2. ``render_cover_image`` / ``render_outro_image`` 的画布规范化与写字像素变化；
3. ``SeriesConfig`` / ``EpisodeRenderOptions`` 的旧配置兼容与往返持久化；
4. 真 FFmpeg 合成（封页 + 结尾 + BGM）的时长/流信息与基线一致性；
5. FastAPI（``TestClient``）片包 6+1 个接口的完整流程。

全部素材临时目录现造，不触网、不消耗模型额度。
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image, ImageChops, ImageDraw

from core.branding_render import (
    BRANDING_SLOTS,
    EpisodeRenderOptions,
    decode_base64_image,
    format_cover_label,
    int_to_chinese,
    load_episode_render_options,
    render_cover_image,
    render_outro_image,
    resolve_effective_branding,
    save_branding_image,
    save_episode_render_options,
)
from core.config import (
    AppConfig,
    CoverTextSpec,
    SeriesBrandingConfig,
    SeriesConfig,
    WorkspaceConfig,
    load_series_config,
)
from core.schemas import (
    AudioClipMeta,
    CameraMotion,
    RenderTimeline,
    SubtitleLine,
    TimelineScene,
    save_model,
)
from stages.s7_renderer import probe_duration_ms, render_episode, render_timeline
from web.server import create_app

#: 允许用环境变量指定 ffmpeg（CI 里可能是绝对路径）。
FFMPEG_BIN = os.environ.get("S7_FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = shutil.which("ffprobe")

needs_ffmpeg = pytest.mark.skipif(
    shutil.which(FFMPEG_BIN) is None,
    reason="本机没有 ffmpeg，跳过 Patch v4.5 的真实渲染测试",
)


# ---------------------------------------------------------------------------
# 素材构造（与 test_s7_renderer.py 同口径）
# ---------------------------------------------------------------------------
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
    """写一张 800×1400 的带内容面板图。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (800, 1400), (18, 22, 38))
    draw = ImageDraw.Draw(image)
    draw.rectangle([80, 160, 720, 1240], fill=color)
    draw.ellipse([300, 520, 500, 720], fill=(250, 240, 60))
    image.save(path)


def _make_timeline_workspace(root: Path) -> tuple[Path, RenderTimeline]:
    """造一话的 Stage 6 产物：2 场景各 1000ms（总 2000ms）+ 音频 + 字幕。"""
    episode_dir = root / "episodes" / "ep_001"
    plan_dir = episode_dir / "06_plan"
    (plan_dir / "subtitles").mkdir(parents=True, exist_ok=True)
    (episode_dir / "02_panels").mkdir(parents=True, exist_ok=True)
    (episode_dir / "05_audio").mkdir(parents=True, exist_ok=True)

    _write_panel(episode_dir / "02_panels" / "panel_001.png", (200, 50, 50))
    _write_panel(episode_dir / "02_panels" / "panel_002.png", (50, 80, 200))
    _write_wav(episode_dir / "05_audio" / "a1.wav", 0.8, 300.0)
    _write_wav(episode_dir / "05_audio" / "a2.wav", 0.8, 500.0)
    Image.new("RGBA", (1080, 1920), (0, 0, 0, 0)).save(
        plan_dir / "subtitles" / "0001.png"
    )

    scenes = [
        TimelineScene(
            scene_index=0,
            beat_id="beat_001",
            panel_id="panel_001",
            image_path="02_panels/panel_001.png",
            start_ms=0,
            end_ms=1000,
            duration_ms=1000,
            motion=CameraMotion.STATIC,
        ),
        TimelineScene(
            scene_index=1,
            beat_id="beat_002",
            panel_id="panel_002",
            image_path="02_panels/panel_002.png",
            start_ms=1000,
            end_ms=2000,
            duration_ms=1000,
            motion=CameraMotion.STATIC,
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


def _png_base64(size: tuple[int, int], color: tuple[int, int, int]) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _probe_streams(path: Path) -> list[dict]:
    if not FFPROBE_BIN:
        pytest.skip("本机没有 ffprobe，跳过流信息断言")
    completed = subprocess.run(
        [
            FFPROBE_BIN,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,codec_type",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return list(json.loads(completed.stdout).get("streams", []))


# ---------------------------------------------------------------------------
# 1. 中文数字与封面文案
# ---------------------------------------------------------------------------
def test_int_to_chinese_basic_and_overflow() -> None:
    assert int_to_chinese(1) == "一"
    assert int_to_chinese(10) == "十"
    assert int_to_chinese(11) == "十一"
    assert int_to_chinese(20) == "二十"
    assert int_to_chinese(23) == "二十三"
    assert int_to_chinese(105) == "一百零五"
    assert int_to_chinese(9999) == "九千九百九十九"
    # 超出 1~9999 回退阿拉伯数字字符串，不抛异常。
    assert int_to_chinese(10000) == "10000"
    assert int_to_chinese(0) == "0"


def test_format_cover_label_templates_and_override() -> None:
    spec = CoverTextSpec()
    assert format_cover_label(1, spec) == "第一话"
    assert format_cover_label(10, spec) == "第十话"
    assert format_cover_label(11, spec) == "第十一话"
    assert format_cover_label(23, spec) == "第二十三话"

    arabic = CoverTextSpec(use_chinese_numeral=False)
    assert format_cover_label(1, arabic) == "第1话"
    assert format_cover_label(23, arabic) == "第23话"

    # {ep_no} 始终是阿拉伯数字。
    no_template = CoverTextSpec(template="EP {ep_no}", use_chinese_numeral=True)
    assert format_cover_label(12, no_template) == "EP 12"

    # 非空覆盖文案优先（strip 掉首尾空白）。
    assert format_cover_label(1, spec, override_text="  大结局·前夜  ") == "大结局·前夜"
    # 纯空白覆盖视为未覆盖。
    assert format_cover_label(1, spec, override_text="   ") == "第一话"


def test_decode_base64_image_variants_and_size_limit() -> None:
    raw = Image.new("RGB", (8, 8), (1, 2, 3))
    buffer = io.BytesIO()
    raw.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

    assert decode_base64_image(encoded) == buffer.getvalue()
    assert decode_base64_image("data:image/png;base64," + encoded) == buffer.getvalue()

    with pytest.raises(ValueError):
        decode_base64_image("not-base64!!!")

    oversized = base64.b64encode(b"\x00" * (15 * 1024 * 1024 + 1)).decode("ascii")
    with pytest.raises(ValueError, match="体积"):
        decode_base64_image(oversized)


def test_save_branding_image_normalizes_to_png(tmp_path: Path) -> None:
    source = tmp_path / "src.webp"
    Image.new("RGBA", (64, 96), (10, 20, 30, 128)).save(source, format="WEBP")
    out = tmp_path / "normalized.png"
    save_branding_image(source.read_bytes(), out)
    with Image.open(out) as check:
        assert check.format == "PNG"
        assert check.mode in {"RGB", "RGBA"}
        assert check.size == (64, 96)


# ---------------------------------------------------------------------------
# 2. 封面写字 / 结尾铺图
# ---------------------------------------------------------------------------
def test_render_cover_and_outro_fit_canvas(tmp_path: Path) -> None:
    portrait = tmp_path / "portrait.png"
    landscape = tmp_path / "landscape.png"
    Image.new("RGB", (1080, 1920), (30, 40, 60)).save(portrait)
    # 非 9:16 横图：亮色调，便于断言柔暗底铺满后四角不是生硬黑边。
    Image.new("RGB", (1600, 900), (200, 100, 50)).save(landscape)

    cover_out = tmp_path / "cover.png"
    render_cover_image(portrait, cover_out, episode_no=1, cover_text=CoverTextSpec())
    outro_out = tmp_path / "outro.png"
    render_outro_image(landscape, outro_out)

    for path in (cover_out, outro_out):
        with Image.open(path) as check:
            assert check.size == (1080, 1920)
            assert check.mode == "RGB"

    with Image.open(outro_out) as check:
        corners = [
            check.getpixel((2, 2)),
            check.getpixel((1077, 2)),
            check.getpixel((2, 1917)),
            check.getpixel((1077, 1917)),
        ]
    # 柔暗底图必须铺满全画布：任何一角都不允许是纯黑 (0,0,0)。
    assert all(any(channel > 0 for channel in pixel) for pixel in corners)


def test_cover_text_changes_pixels_and_bad_hex_fallback(tmp_path: Path) -> None:
    source = tmp_path / "cover_src.png"
    Image.new("RGB", (1080, 1920), (40, 50, 70)).save(source)

    plain_out = tmp_path / "plain.png"
    text_out = tmp_path / "text.png"
    render_cover_image(
        source, plain_out, episode_no=3,
        cover_text=CoverTextSpec(enabled=False),
    )
    bad_color_spec = CoverTextSpec(color="not-a-hex", stroke_color="#zzzzzz")
    render_cover_image(
        source, text_out, episode_no=3, cover_text=bad_color_spec,
    )

    with Image.open(plain_out) as plain, Image.open(text_out) as text:
        diff = ImageChops.difference(plain.convert("RGB"), text.convert("RGB"))
        # 写字后像素必须确实发生变化。
        assert diff.getbbox() is not None

    # 覆盖文案同样生效且不触发字体异常。
    override_out = tmp_path / "override.png"
    render_cover_image(
        source, override_out, episode_no=3,
        cover_text=CoverTextSpec(), override_text="特别篇",
    )
    assert override_out.is_file() and override_out.stat().st_size > 0


# ---------------------------------------------------------------------------
# 3. 配置兼容与单话选项持久化
# ---------------------------------------------------------------------------
def test_legacy_series_yaml_gets_default_branding(tmp_path: Path) -> None:
    series_dir = tmp_path / "wt_demo"
    series_dir.mkdir()
    (series_dir / "series_config.yaml").write_text(
        "voice: Ethan\nbgm: assets/bgm/demo.wav\n", encoding="utf-8"
    )
    config = load_series_config(series_dir)
    assert config.voice == "Ethan"
    # 旧 yaml 没有 branding 键时，全套默认值必须可用。
    assert config.branding.default_outro_mode == "continue"
    assert config.branding.cover_duration_ms == 2000
    assert config.branding.cover_text.template == "第{ep}话"
    assert config.branding.cover_image is None


def test_branding_config_roundtrip_via_yaml(tmp_path: Path) -> None:
    series_dir = tmp_path / "wt_demo"
    series_dir.mkdir()
    branding = SeriesBrandingConfig(
        cover_image="assets/branding/cover.png",
        outro_continue_image="assets/branding/outro_continue.png",
        default_outro_mode="final",
        cover_duration_ms=1500,
        cover_text=CoverTextSpec(x_ratio=0.33, y_ratio=0.66, font_size=96),
    )
    (series_dir / "series_config.yaml").write_text(
        yaml.safe_dump(
            SeriesConfig(voice="Ryan", branding=branding).model_dump(),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    reloaded = load_series_config(series_dir)
    assert reloaded.voice == "Ryan"
    assert reloaded.branding.cover_image == "assets/branding/cover.png"
    assert reloaded.branding.default_outro_mode == "final"
    assert reloaded.branding.cover_duration_ms == 1500
    assert reloaded.branding.cover_text.x_ratio == 0.33
    assert reloaded.branding.cover_text.font_size == 96


def test_episode_render_options_roundtrip_and_corrupt_fallback(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep_001"
    episode_dir.mkdir()

    # 文件不存在 → 安全默认。
    defaults = load_episode_render_options(episode_dir)
    assert defaults.outro_mode == "inherit"
    assert defaults.cover_enabled is True
    assert defaults.cover_text_override is None

    options = EpisodeRenderOptions(
        cover_enabled=False,
        cover_text_override="  序章  ",
        outro_mode="final",
    )
    saved_path = save_episode_render_options(episode_dir, options)
    assert saved_path.name == "render_options.json"
    reloaded = load_episode_render_options(episode_dir)
    assert reloaded.cover_enabled is False
    assert reloaded.cover_text_override == "序章"
    assert reloaded.outro_mode == "final"

    # 文件损坏 → 不抛异常，回退默认实例。
    saved_path.write_text("{broken json", encoding="utf-8")
    fallback = load_episode_render_options(episode_dir)
    assert fallback.outro_mode == "inherit"

    # 严格模型拒绝非法枚举。
    with pytest.raises(Exception):
        EpisodeRenderOptions(outro_mode="bogus")  # type: ignore[arg-type]


def test_resolve_effective_branding_modes(tmp_path: Path) -> None:
    series_dir = tmp_path / "wt_demo"
    branding_dir = series_dir / "assets" / "branding"
    branding_dir.mkdir(parents=True)
    cover = branding_dir / "cover.png"
    outro_continue = branding_dir / "outro_continue.png"
    Image.new("RGB", (1080, 1920), (10, 20, 30)).save(cover)
    Image.new("RGB", (1080, 1920), (40, 50, 60)).save(outro_continue)

    config = SeriesConfig(
        branding=SeriesBrandingConfig(
            cover_image="assets/branding/cover.png",
            outro_continue_image="assets/branding/outro_continue.png",
            default_outro_mode="continue",
        )
    )
    # inherit → 跟随作品默认 continue。
    effective = resolve_effective_branding(
        config, EpisodeRenderOptions(), series_dir
    )
    assert effective.cover_enabled is True
    assert effective.cover_image_path == cover
    assert effective.outro_mode == "continue"
    assert effective.outro_image_path == outro_continue

    # 单话显式 final，但没上传完结图 → 优雅降级 none，不抛异常。
    effective_final = resolve_effective_branding(
        config, EpisodeRenderOptions(outro_mode="final"), series_dir
    )
    assert effective_final.outro_mode == "none"
    assert effective_final.outro_image_path is None

    # 单话跳过封面。
    effective_no_cover = resolve_effective_branding(
        config, EpisodeRenderOptions(cover_enabled=False), series_dir
    )
    assert effective_no_cover.cover_enabled is False
    assert effective_no_cover.cover_image_path is None


# ---------------------------------------------------------------------------
# 4. 真 FFmpeg：封页 + 结尾 + BGM 的时长数学
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_render_timeline_with_cover_outro_and_bgm(tmp_path: Path) -> None:
    episode_dir, timeline = _make_timeline_workspace(tmp_path)
    branding_dir = tmp_path / "assets" / "branding"
    branding_dir.mkdir(parents=True)
    cover_src = branding_dir / "cover.png"
    outro_src = branding_dir / "outro.png"
    Image.new("RGB", (1080, 1920), (12, 24, 40)).save(cover_src)
    Image.new("RGB", (1600, 900), (180, 90, 40)).save(outro_src)
    bgm_path = tmp_path / "bgm.wav"
    _write_wav(bgm_path, 8.0, freq=220)

    work_dir = tmp_path / "work"
    cover_png = work_dir / "cover_composed.png"
    outro_png = work_dir / "outro_composed.png"
    render_cover_image(cover_src, cover_png, episode_no=1, cover_text=CoverTextSpec())
    render_outro_image(outro_src, outro_png)

    branded_out = tmp_path / "branded.mp4"
    render_timeline(
        timeline,
        episode_dir=episode_dir,
        out_path=branded_out,
        bgm_path=bgm_path,
        work_dir=work_dir,
        ffmpeg_bin=FFMPEG_BIN,
        cover_png=cover_png,
        cover_duration_ms=1200,
        cover_fade_in_ms=350,
        cover_fade_out_ms=450,
        outro_png=outro_png,
        outro_duration_ms=1400,
        outro_fade_in_ms=700,
        outro_fade_out_ms=600,
        bgm_fade_in_ms=350,
        bgm_fade_out_ms=600,
    )
    assert branded_out.is_file() and branded_out.stat().st_size > 0

    # 总时长精确吻合 1200 + 2000 + 1400 = 4600ms（±150ms 帧对齐容差）。
    branded_duration = probe_duration_ms(branded_out, ffmpeg_bin=FFMPEG_BIN)
    assert abs(branded_duration - 4600) <= 150, branded_duration

    streams = _probe_streams(branded_out)
    codec_types = {stream["codec_type"] for stream in streams}
    assert {"audio", "video"} <= codec_types

    # 基线：不传任何片包参数时，时长与原链路一致（2000ms）。
    baseline_out = tmp_path / "baseline.mp4"
    render_timeline(
        timeline,
        episode_dir=episode_dir,
        out_path=baseline_out,
        bgm_path=bgm_path,
        work_dir=tmp_path / "work_base",
        ffmpeg_bin=FFMPEG_BIN,
    )
    baseline_duration = probe_duration_ms(baseline_out, ffmpeg_bin=FFMPEG_BIN)
    assert abs(baseline_duration - 2000) <= 150, baseline_duration


@needs_ffmpeg
def test_render_episode_branding_end_to_end(tmp_path: Path) -> None:
    episode_dir, _timeline = _make_timeline_workspace(tmp_path)
    branding_dir = tmp_path / "assets" / "branding"
    branding_dir.mkdir(parents=True)
    Image.new("RGB", (1080, 1920), (12, 24, 40)).save(branding_dir / "cover.png")
    Image.new("RGB", (1080, 1920), (60, 30, 50)).save(
        branding_dir / "outro_continue.png"
    )

    config = SeriesConfig(
        branding=SeriesBrandingConfig(
            cover_image="assets/branding/cover.png",
            outro_continue_image="assets/branding/outro_continue.png",
            cover_duration_ms=1200,
            cover_fade_in_ms=350,
            cover_fade_out_ms=450,
            outro_duration_ms=1400,
            outro_fade_in_ms=700,
            outro_fade_out_ms=600,
            default_outro_mode="continue",
        )
    )
    (tmp_path / "series_config.yaml").write_text(
        yaml.safe_dump(config.model_dump(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # 默认：封面 + 待续结尾 → 4600ms。
    out_path = render_episode(tmp_path, 1, ffmpeg_bin=FFMPEG_BIN)
    assert out_path.is_file()
    assert abs(probe_duration_ms(out_path, ffmpeg_bin=FFMPEG_BIN) - 4600) <= 150
    # work_dir 已在渲染后清理，不留中间产物。
    assert not (episode_dir / "07_out" / "_work").exists()

    # 单话切「不加结尾」→ 只剩封面 + 正文 = 3200ms。
    save_episode_render_options(
        episode_dir, EpisodeRenderOptions(outro_mode="none")
    )
    out_no_outro = render_episode(tmp_path, 1, ffmpeg_bin=FFMPEG_BIN)
    assert abs(probe_duration_ms(out_no_outro, ffmpeg_bin=FFMPEG_BIN) - 3200) <= 150

    # 再跳过封面 → 纯正文 2000ms。
    save_episode_render_options(
        episode_dir,
        EpisodeRenderOptions(outro_mode="none", cover_enabled=False),
    )
    out_plain = render_episode(tmp_path, 1, ffmpeg_bin=FFMPEG_BIN)
    assert abs(probe_duration_ms(out_plain, ffmpeg_bin=FFMPEG_BIN) - 2000) <= 150


@needs_ffmpeg
def test_render_episode_skips_missing_branding_image(tmp_path: Path) -> None:
    episode_dir, _timeline = _make_timeline_workspace(tmp_path)
    # 配置指向不存在的底图：只打印警告并优雅降级，绝不中断流水线。
    config = SeriesConfig(
        branding=SeriesBrandingConfig(
            cover_image="assets/branding/missing.png",
            outro_continue_image="assets/branding/missing_outro.png",
        )
    )
    (tmp_path / "series_config.yaml").write_text(
        yaml.safe_dump(config.model_dump(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    out_path = render_episode(tmp_path, 1, ffmpeg_bin=FFMPEG_BIN)
    assert out_path.is_file()
    assert abs(probe_duration_ms(out_path, ffmpeg_bin=FFMPEG_BIN) - 2000) <= 150


# ---------------------------------------------------------------------------
# 5. FastAPI 接口（TestClient，不触网）
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    workspace = tmp_path / "ws"
    config = AppConfig(workspace=WorkspaceConfig(root=workspace))
    return TestClient(create_app(config))


def _make_source(root: Path, episodes: int = 1) -> Path:
    """造一个本地漫画目录（每个子目录一话，每页带内容块避免被判空镜）。"""
    source = root / "src"
    for index in range(1, episodes + 1):
        folder = source / f"ep_{index:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        for slice_index in (1, 2):
            image = Image.new("RGB", (400, 600), (30 * slice_index, 60, 90))
            draw = ImageDraw.Draw(image)
            for row in range(3):
                for col in range(2):
                    draw.rectangle(
                        [
                            30 + col * 180,
                            40 + row * 180,
                            30 + col * 180 + 150,
                            40 + row * 180 + 150,
                        ],
                        fill=(200 - 30 * row, 180 - 20 * col, 160),
                    )
            image.save(folder / f"{slice_index:03d}.png")
    return source


def _register(client: TestClient, source: Path) -> str:
    response = client.post("/api/series", json={"url": str(source)})
    assert response.status_code == 200, response.text
    return str(response.json()["series_id"])


def test_branding_api_full_flow(client: TestClient, tmp_path: Path) -> None:
    source = _make_source(tmp_path)
    series_id = _register(client, source)
    base = f"/api/series/{series_id}/branding"

    # 1) 上传三张图（带 data: 前缀的 Base64 PNG）。
    for slot, color in zip(
        BRANDING_SLOTS,
        [((10, 20, 30)), ((40, 50, 60)), ((70, 80, 90))],
    ):
        response = client.post(
            base + "/upload",
            json={"slot": slot, "content_base64": _png_base64((1080, 1920), color)},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload[f"{_url_field(slot)}"].startswith("/workspace/")
        assert "?v=" in payload[f"{_url_field(slot)}"]

    # 2) GET 返回三个 URL 与字体清单。
    got = client.get(base).json()
    assert got["series_id"] == series_id
    for slot in BRANDING_SLOTS:
        assert got[_url_field(slot)]
        assert got[_image_field(slot)].endswith(f"{slot}.png")
    assert isinstance(got["font_options"], list)

    # 3) PUT 改坐标与默认结尾模式（图片字段不被覆盖）。
    put_response = client.put(
        base,
        json={
            "cover_text": {"x_ratio": 0.33, "y_ratio": 0.66, "font_size": 96},
            "default_outro_mode": "final",
            "cover_duration_ms": 1800,
        },
    )
    assert put_response.status_code == 200, put_response.text
    updated = put_response.json()
    assert updated["cover_text"]["x_ratio"] == 0.33
    assert updated["cover_text"]["y_ratio"] == 0.66
    assert updated["cover_text"]["font_size"] == 96
    assert updated["default_outro_mode"] == "final"
    assert updated["cover_duration_ms"] == 1800
    # 图片字段保持不动。
    assert updated["cover_image"] == "assets/branding/cover.png"

    # 4) cover-preview 真实渲染 PNG。
    preview = client.post(base + "/cover-preview", json={"episode_no": 1})
    assert preview.status_code == 200, preview.text
    assert "image/png" in preview.headers["content-type"]
    assert len(preview.content) > 0

    # 5) 单话 render-options 切 final，GET 作品详情可回读。
    # Stage 0 只登记目录，单话目录随后续阶段创建；这里手工铺一个。
    episode_dir = tmp_path / "ws" / series_id / "episodes" / "ep_001"
    episode_dir.mkdir(parents=True, exist_ok=True)
    episode_id = f"{series_id}_ep_001"
    options_response = client.put(
        f"/api/episodes/{episode_id}/render-options",
        json={"outro_mode": "final"},
    )
    assert options_response.status_code == 200, options_response.text
    assert options_response.json()["render_options"]["outro_mode"] == "final"

    series_detail = client.get(f"/api/series/{series_id}").json()
    episode = next(
        item for item in series_detail["episodes"] if item["episode_id"] == episode_id
    )
    assert episode["render_options"]["outro_mode"] == "final"

    # 6) DELETE 完结图：文件删除且配置置空。
    series_dir = tmp_path / "ws" / series_id
    deleted_path = series_dir / "assets" / "branding" / "outro_final.png"
    assert deleted_path.is_file()
    delete_response = client.delete(base + "/outro_final")
    assert delete_response.status_code == 200, delete_response.text
    deleted_payload = delete_response.json()
    assert deleted_payload["outro_final_image"] is None
    assert deleted_payload["outro_final_url"] is None
    assert not deleted_path.exists()


def test_branding_api_error_paths(client: TestClient, tmp_path: Path) -> None:
    source = _make_source(tmp_path)
    series_id = _register(client, source)
    base = f"/api/series/{series_id}/branding"

    # 未上传封面时预览 → 404 清晰提示。
    preview = client.post(base + "/cover-preview", json={"episode_no": 1})
    assert preview.status_code == 404
    assert "封页" in preview.json()["detail"]

    # 非法槽位 → 400。
    bad_slot = client.post(
        base + "/upload",
        json={"slot": "bogus", "content_base64": _png_base64((32, 32), (1, 2, 3))},
    )
    assert bad_slot.status_code == 400

    # 非法 Base64 → 400。
    bad_b64 = client.post(
        base + "/upload",
        json={"slot": "cover", "content_base64": "not-base64!!!"},
    )
    assert bad_b64.status_code == 400

    # 非法枚举写入 render-options → 422（pydantic 严格校验）。
    bad_options = client.put(
        f"/api/episodes/{series_id}_ep_001/render-options",
        json={"outro_mode": "bogus"},
    )
    assert bad_options.status_code == 422

    # 未知单话 ID → 404。
    missing_ep = client.put(
        f"/api/episodes/{series_id}_ep_999/render-options",
        json={"outro_mode": "none"},
    )
    assert missing_ep.status_code == 404

    # 未知作品 → 404。
    assert client.get("/api/series/wt_missing/branding").status_code == 404


def _url_field(slot: str) -> str:
    return {
        "cover": "cover_url",
        "outro_continue": "outro_continue_url",
        "outro_final": "outro_final_url",
    }[slot]


def _image_field(slot: str) -> str:
    return {
        "cover": "cover_image",
        "outro_continue": "outro_continue_image",
        "outro_final": "outro_final_image",
    }[slot]
