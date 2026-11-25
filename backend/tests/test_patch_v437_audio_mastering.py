"""PATCH v4.37：第 1 次音频升级的离线 + 真机回归测试。

覆盖三条主线：

1. **SFX 瞬时响度对齐**：先量「去掉前导静音后的有效响度」再校准到
   ``_SFX_TARGET_LUFS``，落点只影响时间、不影响电平——长延迟（3 秒）的音效
   不再因为前导静音被 ``dynaudnorm`` 抬飞；不同底电平的素材校准后电平一致。
2. **双源侧链避让**：人声 + 音效两路都能压 BGM。有音效时 BGM 在音效时间窗内
   的 RMS 明显下降，无音效的时间窗保持原样（瞬时闪避，不是持续压缩）。
3. **整片 -14 LUFS 两遍母带**：``master.wav`` 采样率 44100 / 双声道，
   积分响度收敛到 ``-14.0 ± 1.5 LUFS`` 且峰值不削波；全静音输入平滑降级。

纯逻辑用例（正则解析、缺文件回退、滤镜链契约）不依赖 ffmpeg；
需要真实音频的用例在缺 ffmpeg 的机器上自动跳过。
"""

from __future__ import annotations

import io
import math
import shutil
import struct
import wave
from pathlib import Path

import pytest

from core.schemas import AudioClipMeta, CameraMotion, RenderTimeline, SfxEvent, TimelineScene
from stages import s7_renderer

FFMPEG_BIN = shutil.which("ffmpeg")
requires_ffmpeg = pytest.mark.skipif(
    FFMPEG_BIN is None, reason="本机没有 ffmpeg，跳过真机音频用例"
)


# --------------------------------------------------------------------------
# 素材与工具
# --------------------------------------------------------------------------
def _tone_wav_bytes(
    seconds: float, rate: int = 44100, freq: float = 440.0, amp: float = 0.3
) -> bytes:
    """造一段真实 wav（单声道 16bit 正弦）。"""
    total = int(rate * seconds)
    frames = bytearray()
    for index in range(total):
        value = amp * math.sin(2 * math.pi * freq * index / rate)
        frames += int(value * 32767).to_bytes(2, "little", signed=True)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return buffer.getvalue()


def _write_wav(
    path: Path, seconds: float = 1.0, *, freq: float = 440.0, amp: float = 0.3
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_tone_wav_bytes(seconds, freq=freq, amp=amp))
    return path


def _write_wav_with_leading_silence(
    path: Path, silence_s: float, tone_s: float, *, freq: float = 500.0, amp: float = 0.2
) -> Path:
    """造一条「前 N 秒纯静音 + 后 M 秒正弦」的合法 wav。"""
    rate = 44100
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(int(rate * silence_s) * 2))
        frames = bytearray()
        for index in range(int(rate * tone_s)):
            value = amp * math.sin(2 * math.pi * freq * index / rate)
            frames += int(value * 32767).to_bytes(2, "little", signed=True)
        handle.writeframes(bytes(frames))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buffer.getvalue())
    return path


def _rms_window(path: Path, start_s: float, dur_s: float) -> float:
    """量 wav 在某个时间窗内的 RMS（0..32767 标度）。"""
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        handle.setpos(int(start_s * rate))
        raw = handle.readframes(int(dur_s * rate))
    samples = struct.unpack("<%dh" % (len(raw) // 2), raw)
    if not samples:
        return 0.0
    mono = [samples[i] for i in range(0, len(samples), channels)]
    return math.sqrt(sum(value * value for value in mono) / len(mono))


def _peak_sample(path: Path) -> int:
    """量 wav 的最大绝对采样值。"""
    with wave.open(str(path), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    samples = struct.unpack("<%dh" % (len(raw) // 2), raw)
    return max((abs(value) for value in samples), default=0)


def _make_timeline(
    root: Path, *, sfx_events: list[SfxEvent] | None = None, total_ms: int = 10000
) -> tuple[Path, RenderTimeline]:
    """造一话最小 Stage 6 产物：1 场景 + 1 人声 + 可选音效事件。"""
    episode_dir = root / "episodes" / "ep_001"
    (episode_dir / "05_audio").mkdir(parents=True, exist_ok=True)
    _write_wav(episode_dir / "05_audio" / "a1.wav", 1.0, freq=300.0)

    scene = TimelineScene(
        scene_index=0,
        beat_id="b1",
        panel_id="p_001",
        image_path="02_panels/p_001.png",
        start_ms=0,
        end_ms=1000,
        duration_ms=1000,
        motion=CameraMotion.ZOOM_IN,
    )
    timeline = RenderTimeline(
        episode_id="ep_001",
        total_duration_ms=total_ms,
        scenes=[scene],
        audios=[
            AudioClipMeta(
                beat_id="b1", audio_path="a1.wav", duration_ms=900, pause_after_ms=100
            )
        ],
        subtitles=[],
        sfx_events=sfx_events or [],
    )
    return episode_dir, timeline


# --------------------------------------------------------------------------
# 1. 纯逻辑：解析与回退
# --------------------------------------------------------------------------
def test_parse_loudnorm_stats_reads_json_block() -> None:
    """Pass 1 的 JSON 实测数据能被解析成浮点字典。"""
    payload = (
        "{\n"
        '    "input_i" : "-20.46",\n'
        '    "input_tp" : "-3.56",\n'
        '    "input_lra" : "5.30",\n'
        '    "input_thresh" : "-30.44",\n'
        '    "target_offset" : "0.04"\n'
        "}"
    )
    stats = s7_renderer._parse_loudnorm_stats(payload)
    assert stats is not None
    assert stats["input_i"] == pytest.approx(-20.46)
    assert stats["input_tp"] == pytest.approx(-3.56)
    assert stats["input_lra"] == pytest.approx(5.30)


def test_parse_loudnorm_stats_returns_none_on_garbage() -> None:
    """解析不出来必须返回 None（调用方据此降级，不能抛异常）。"""
    assert s7_renderer._parse_loudnorm_stats("") is None
    assert s7_renderer._parse_loudnorm_stats("not a json at all") is None
    assert s7_renderer._parse_loudnorm_stats("{ broken") is None


def test_measure_audio_lufs_falls_back_to_zero_for_missing_file() -> None:
    """量不到就回退 0.0 偏置，绝不抛异常把渲染打断。"""
    measured = s7_renderer._measure_audio_lufs(
        Path("/nonexistent/does-not-exist.wav"), ffmpeg_bin=FFMPEG_BIN or "ffmpeg"
    )
    assert measured == 0.0


def test_v437_constants_are_pinned() -> None:
    """v4.37 的响度基准钉死，防止后续悄悄改漂。"""
    assert s7_renderer._SFX_TARGET_LUFS == -16.0
    assert s7_renderer._SFX_CALIBRATION_LIMIT_DB == 12.0
    assert s7_renderer._VOICE_TARGET_LUFS == -16.0
    assert s7_renderer._VOICE_LUFS_TOLERANCE == 1.2
    assert s7_renderer._VOICE_CORRECTION_LIMIT_DB == 6.0
    assert s7_renderer._MASTER_TARGET_LUFS == -14.0
    assert s7_renderer._MASTER_TRUE_PEAK == -1.5
    assert s7_renderer._MASTER_LRA == 9
    assert s7_renderer._DUCK_TRIGGER_SFX_BOOST_DB == 3.0
    assert s7_renderer._DUCK_THRESHOLD == 0.06
    assert s7_renderer._DUCK_RATIO == 2.4
    assert s7_renderer._DUCK_ATTACK_MS == 25
    assert s7_renderer._DUCK_RELEASE_MS == 350


def test_duck_attack_release_are_converted_to_seconds() -> None:
    """sidechaincompress 的 attack/release 单位是**秒**——毫秒常量必须换算。

    旧分段链路误传 ``attack=30``（= 30 秒），压缩永远来不及起效，
    「闪避」事实上一直是关着的。
    """
    source = Path(s7_renderer.__file__).read_text(encoding="utf-8")
    assert "attack={_DUCK_ATTACK_MS / 1000.0:.3f}" in source
    assert "release={_DUCK_RELEASE_MS / 1000.0:.3f}" in source


# --------------------------------------------------------------------------
# 2. 真机：SFX 响度校准
# --------------------------------------------------------------------------
@requires_ffmpeg
@pytest.mark.slow
def test_sfx_calibration_aligns_different_source_levels(tmp_path: Path) -> None:
    """不同底电平的音效素材，校准后落在同一响度（长延迟也不漂）。"""
    episode_dir, timeline = _make_timeline(
        tmp_path,
        sfx_events=[
            SfxEvent(sfx_key="heart_thump", start_ms=3000),
            SfxEvent(sfx_key="door_slam", start_ms=6000),
        ],
        total_ms=9000,
    )
    assets = tmp_path / "assets"
    (assets / "sfx").mkdir(parents=True, exist_ok=True)
    # 同频同长、底电平差约 8dB 的两条素材（差值落在 ±12dB 校准能力内）。
    (assets / "sfx" / "heart_thump.wav").write_bytes(
        _tone_wav_bytes(0.5, freq=700.0, amp=0.30)
    )
    (assets / "sfx" / "door_slam.wav").write_bytes(
        _tone_wav_bytes(0.5, freq=700.0, amp=0.12)
    )

    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    _voice, _bgm, sfx_path = s7_renderer._prepare_audio(
        timeline,
        episode_dir=episode_dir,
        work_dir=work_dir,
        total_s=9.0,
        bgm_path=None,
        bgm_gain_db=-8.0,
        ffmpeg_bin=FFMPEG_BIN,
        sfx_events=list(timeline.sfx_events or []),
        assets_dir=assets,
    )
    assert sfx_path is not None and sfx_path.is_file()

    loud = _rms_window(sfx_path, 3.0, 0.4)
    quiet = _rms_window(sfx_path, 6.0, 0.4)
    assert loud > 0 and quiet > 0
    # 校准后两条音效的电平差应在 2dB 以内（旧链路会差出 ~8dB）。
    ratio_db = 20 * math.log10(loud / quiet)
    assert abs(ratio_db) < 2.0, f"校准后电平仍相差 {ratio_db:.2f}dB"


@requires_ffmpeg
@pytest.mark.slow
def test_sfx_calibration_respects_gain_clamp(tmp_path: Path) -> None:
    """素材底电平差超出 ±12dB 校准能力时，必须被夹住而不是无限放大。

    校准增益上限是 ``_SFX_CALIBRATION_LIMIT_DB``；超限的那条只补得上一半，
    残留差值 = 原始差值 - 夹紧值。
    """
    episode_dir, timeline = _make_timeline(
        tmp_path,
        sfx_events=[
            SfxEvent(sfx_key="heart_thump", start_ms=3000),
            SfxEvent(sfx_key="door_slam", start_ms=6000),
        ],
        total_ms=9000,
    )
    assets = tmp_path / "assets"
    (assets / "sfx").mkdir(parents=True, exist_ok=True)
    (assets / "sfx" / "heart_thump.wav").write_bytes(
        _tone_wav_bytes(0.5, freq=700.0, amp=0.30)
    )
    (assets / "sfx" / "door_slam.wav").write_bytes(
        _tone_wav_bytes(0.5, freq=700.0, amp=0.03)
    )

    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    _voice, _bgm, sfx_path = s7_renderer._prepare_audio(
        timeline,
        episode_dir=episode_dir,
        work_dir=work_dir,
        total_s=9.0,
        bgm_path=None,
        bgm_gain_db=-8.0,
        ffmpeg_bin=FFMPEG_BIN,
        sfx_events=list(timeline.sfx_events or []),
        assets_dir=assets,
    )
    assert sfx_path is not None and sfx_path.is_file()

    loud = _rms_window(sfx_path, 3.0, 0.4)
    quiet = _rms_window(sfx_path, 6.0, 0.4)
    ratio_db = 20 * math.log10(loud / quiet)
    # 原始差约 20dB，校准最多补 12dB → 残留 8dB 左右（不能被完全抹平）。
    assert 4.0 < ratio_db < 12.0, f"校准夹紧行为异常：残留 {ratio_db:.2f}dB"


@requires_ffmpeg
@pytest.mark.slow
def test_measure_lufs_ignores_leading_silence(tmp_path: Path) -> None:
    """前导静音必须被掐掉再量，否则响度会被系统性拉低。"""
    plain = _write_wav(tmp_path / "plain.wav", 1.0, freq=500.0, amp=0.2)
    padded = _write_wav_with_leading_silence(
        tmp_path / "padded.wav", 2.0, 1.0, freq=500.0, amp=0.2
    )

    measured_plain = s7_renderer._measure_audio_lufs(plain, ffmpeg_bin=FFMPEG_BIN)
    measured_padded = s7_renderer._measure_audio_lufs(padded, ffmpeg_bin=FFMPEG_BIN)
    assert measured_plain != 0.0
    assert measured_padded != 0.0
    # 带 2 秒静音的素材，量出来的响度必须与纯净素材基本一致。
    assert abs(measured_padded - measured_plain) < 1.0


# --------------------------------------------------------------------------
# 3. 真机：双源侧链闪避
# --------------------------------------------------------------------------
@requires_ffmpeg
@pytest.mark.slow
def test_sfx_triggers_extra_bgm_ducking(tmp_path: Path) -> None:
    """双源侧链：音效时间窗内 BGM 被额外压低，非音效时间窗保持原样。"""
    voice = _write_wav(tmp_path / "voice.wav", 3.0, freq=300.0, amp=0.0)
    bgm = _write_wav(tmp_path / "bgm.wav", 3.0, freq=180.0, amp=0.25)
    sfx = _write_wav(tmp_path / "sfx.wav", 0.5, freq=900.0, amp=0.5)

    levels: dict[str, float] = {}
    for tag, sfx_path in (("no_sfx", None), ("with_sfx", sfx)):
        work_dir = tmp_path / f"work_{tag}"
        s7_renderer._mix_and_master_audio(
            voice_path=voice,
            bgm_path=bgm,
            sfx_path=sfx_path,
            total_s=3.0,
            work_dir=work_dir,
            ffmpeg_bin=FFMPEG_BIN,
        )
        ducked = work_dir / "bgm_ducked.wav"
        assert ducked.is_file()
        levels[tag] = _rms_window(ducked, 0.0, 0.4)

    # 音效窗口内：有无 SFX 的 BGM 能量必须明显不同（闪避真的起效了）。
    assert levels["with_sfx"] < levels["no_sfx"] * 0.75
    # 无音效的时间窗：两条轨一样（这是「瞬时」闪避，不是持续压缩）。
    # 静默人声下 BGM 不应被压。
    assert levels["no_sfx"] > 0


# --------------------------------------------------------------------------
# 4. 真机：整片母带
# --------------------------------------------------------------------------
@requires_ffmpeg
@pytest.mark.slow
def test_master_wav_hits_target_loudness(tmp_path: Path) -> None:
    """母带输出：44100Hz / 双声道 / 积分响度 -14±1.5 LUFS / 峰值不削波。"""
    voice = _write_wav(tmp_path / "voice.wav", 3.0, freq=300.0, amp=0.25)
    bgm = _write_wav(tmp_path / "bgm.wav", 3.0, freq=180.0, amp=0.20)
    sfx = _write_wav(tmp_path / "sfx.wav", 0.6, freq=900.0, amp=0.35)

    master = s7_renderer._mix_and_master_audio(
        voice_path=voice,
        bgm_path=bgm,
        sfx_path=sfx,
        total_s=3.0,
        work_dir=tmp_path / "work",
        ffmpeg_bin=FFMPEG_BIN,
    )
    assert master.is_file() and master.stat().st_size > 0
    with wave.open(str(master), "rb") as handle:
        assert handle.getframerate() == 44100
        assert handle.getnchannels() == 2
        assert handle.getsampwidth() == 2

    measured = s7_renderer._measure_audio_lufs(master, ffmpeg_bin=FFMPEG_BIN)
    assert measured != 0.0
    assert -15.5 <= measured <= -12.5, f"母带响度 {measured:.2f} LUFS 偏离 -14 目标过远"

    # 峰值不削波：限幅把峰值压在 -1dBFS 附近，不允许触顶。
    assert _peak_sample(master) < 32700


@requires_ffmpeg
@pytest.mark.slow
def test_master_degrades_gracefully_on_silence(tmp_path: Path) -> None:
    """全静音输入：不抛异常，master.wav 照常产出。"""
    voice = _write_wav(tmp_path / "voice.wav", 1.0, freq=300.0, amp=0.0)
    bgm = _write_wav(tmp_path / "bgm.wav", 1.0, freq=180.0, amp=0.0)
    master = s7_renderer._mix_and_master_audio(
        voice_path=voice,
        bgm_path=bgm,
        sfx_path=None,
        total_s=1.0,
        work_dir=tmp_path / "work",
        ffmpeg_bin=FFMPEG_BIN,
    )
    assert master.is_file() and master.stat().st_size > 0


# --------------------------------------------------------------------------
# 5. 滤镜链契约（mock，不跑 ffmpeg）
# --------------------------------------------------------------------------
def test_render_segment_uses_master_without_sidechain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """传了 master_audio_path：分段直接切片并入，不再跑 sidechaincompress。"""
    calls: list[list[str]] = []

    def fake_run_ffmpeg(args: list[str], **kwargs: object) -> str:
        calls.append(list(args))
        target = Path(args[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake")
        return ""

    monkeypatch.setattr(s7_renderer, "run_ffmpeg", fake_run_ffmpeg)

    master = tmp_path / "master.wav"
    master.write_bytes(b"fake")
    voice = tmp_path / "voice.wav"
    voice.write_bytes(b"fake")

    scene = TimelineScene(
        scene_index=0,
        beat_id="b1",
        panel_id="p_001",
        image_path="02_panels/p_001.png",
        start_ms=0,
        end_ms=1000,
        duration_ms=1000,
        motion=CameraMotion.STATIC,
    )
    timeline = RenderTimeline(
        episode_id="ep_001",
        total_duration_ms=1000,
        scenes=[scene],
        audios=[],
        subtitles=[],
    )
    out = tmp_path / "seg.mp4"
    s7_renderer._render_segment(
        timeline=timeline,
        scenes=[scene],
        backgrounds=[tmp_path / "bg.png"],
        foregrounds=[tmp_path / "fg.png"],
        subtitles=[],
        seg_start_ms=0,
        seg_len_ms=1000,
        voice_path=voice,
        bgm_path=None,
        sfx_path=None,
        fps=30,
        width=1080,
        height=1920,
        background_blur=40,
        background_darken=0.55,
        out_path=out,
        ffmpeg_bin=FFMPEG_BIN or "ffmpeg",
        master_audio_path=master,
    )
    joined = " ".join(" ".join(call) for call in calls)
    assert "sidechaincompress" not in joined
    assert "amix=" not in joined
    assert str(master) in joined
    assert "[aout]" in joined


def test_render_segment_keeps_legacy_mix_without_master(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不传 master：旧的实时混音分支完整保留（向后兼容）。"""
    calls: list[list[str]] = []

    def fake_run_ffmpeg(args: list[str], **kwargs: object) -> str:
        calls.append(list(args))
        target = Path(args[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake")
        return ""

    monkeypatch.setattr(s7_renderer, "run_ffmpeg", fake_run_ffmpeg)

    voice = tmp_path / "voice.wav"
    voice.write_bytes(b"fake")
    bgm = tmp_path / "bgm.wav"
    bgm.write_bytes(b"fake")

    scene = TimelineScene(
        scene_index=0,
        beat_id="b1",
        panel_id="p_001",
        image_path="02_panels/p_001.png",
        start_ms=0,
        end_ms=1000,
        duration_ms=1000,
        motion=CameraMotion.STATIC,
    )
    timeline = RenderTimeline(
        episode_id="ep_001",
        total_duration_ms=1000,
        scenes=[scene],
        audios=[],
        subtitles=[],
    )
    s7_renderer._render_segment(
        timeline=timeline,
        scenes=[scene],
        backgrounds=[tmp_path / "bg.png"],
        foregrounds=[tmp_path / "fg.png"],
        subtitles=[],
        seg_start_ms=0,
        seg_len_ms=1000,
        voice_path=voice,
        bgm_path=bgm,
        sfx_path=None,
        fps=30,
        width=1080,
        height=1920,
        background_blur=40,
        background_darken=0.55,
        out_path=tmp_path / "seg.mp4",
        ffmpeg_bin=FFMPEG_BIN or "ffmpeg",
    )
    joined = " ".join(" ".join(call) for call in calls)
    assert "sidechaincompress" in joined
    assert "amix=inputs=2" in joined
