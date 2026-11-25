"""Patch v3.2 Patch 14：BGM/SFX 响度归一与显性混音修复的离线测试。

对应 ``PATCH_v3.2_VOICE_CAST_AND_AUDIO_UX.md`` Patch 14 的两条验收：

1. ``_prepare_audio`` 的 ffmpeg 命令里，BGM 链先 ``loudnorm=I=-16:TP=-1.5:LRA=11``
   再施加默认增益（Patch v4.0 Patch 18 起由 ``-11.5dB`` 提为 ``-8.0dB``）；SFX 链含
   ``adelay``、``dynaudnorm`` 与增益（Patch v4.0 Patch 18 起由 ``-3.5dB`` 提为 ``-2.0dB``），
   真实 ffmpeg 跑出来的音效轨非静音。
2. ``s6_timeline`` 把 ``sfx_list`` 里的「心驚 / 嘎咿 / 啪」分别匹配为
   ``heart_thump`` / ``whoosh`` / ``door_slam``，且遵守 6000ms 节流与 8 条上限。

命令捕获类用例全部离线（假 ``run_ffmpeg``）；只有端到端非静音用例需要本机 ffmpeg。
"""

from __future__ import annotations

import io
import math
import shutil
import struct
import wave
from pathlib import Path

import pytest

from core.config import AppConfig
from core.schemas import (
    AudioClipMeta,
    CameraMotion,
    RenderTimeline,
    ScriptBeat,
    SfxEvent,
    TimelineScene,
)
from stages import s6_timeline, s7_renderer

FFMPEG_BIN = shutil.which("ffmpeg")


# --------------------------------------------------------------------------
# 素材构造
# --------------------------------------------------------------------------
def _tone_wav_bytes(
    seconds: float, rate: int = 44100, freq: float = 440.0, amp: float = 0.3
) -> bytes:
    """造一段带正弦波的真实 wav（非静音，供 RMS 断言与 ffmpeg 解码）。"""
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
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_tone_wav_bytes(seconds, freq=freq, amp=amp))


def _make_timeline(
    root: Path, *, sfx_events: list[SfxEvent] | None = None
) -> tuple[Path, RenderTimeline]:
    """造一话最小 Stage 6 产物：1 场景 + 1 人声 + 可选音效事件。"""
    episode_dir = root / "episodes" / "ep_001"
    (episode_dir / "05_audio").mkdir(parents=True, exist_ok=True)
    _write_wav(episode_dir / "05_audio" / "a1.wav", 1.0, freq=300.0)

    scenes = [
        TimelineScene(
            scene_index=0,
            beat_id="b1",
            panel_id="p_001",
            image_path="02_panels/p_001.png",
            start_ms=0,
            end_ms=1000,
            duration_ms=1000,
            motion=CameraMotion.ZOOM_IN,
        )
    ]
    audios = [
        AudioClipMeta(
            beat_id="b1", audio_path="a1.wav", duration_ms=900, pause_after_ms=100
        ),
    ]
    timeline = RenderTimeline(
        episode_id="ep_001",
        total_duration_ms=2000,
        scenes=scenes,
        audios=audios,
        subtitles=[],
        sfx_events=sfx_events or [],
    )
    return episode_dir, timeline


class _RecordingFFmpeg:
    """替换 ``s7_renderer.run_ffmpeg``：记录每次命令并造占位产物。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: object) -> str:
        self.calls.append(list(args))
        target = Path(args[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake")
        return ""

    @property
    def joined(self) -> str:
        return " ".join(" ".join(call) for call in self.calls)


# --------------------------------------------------------------------------
# 1. 配置默认值与侧链参数
# --------------------------------------------------------------------------
def test_render_config_defaults_bumped_v32() -> None:
    """BGM -8.0dB、SFX -2.0dB，三处默认值必须一致（Patch v4.0 Patch 18 再次上调）。"""
    config = AppConfig()
    assert config.render.bgm_gain_db == -8.0
    assert config.render.sfx_gain_db == -2.0
    assert s7_renderer.DEFAULT_SFX_GAIN_DB == -2.0


def test_sidechain_uses_gentle_v32_ducking() -> None:
    """侧链压缩钉死为柔和漫评闪避四参数（Patch v4.0 Patch 18 微调）。"""
    assert s7_renderer._SIDECHAIN_THRESHOLD == 0.06
    assert s7_renderer._SIDECHAIN_RATIO == 2.0
    assert s7_renderer._SIDECHAIN_ATTACK_MS == 30
    assert s7_renderer._SIDECHAIN_RELEASE_MS == 350


# --------------------------------------------------------------------------
# 2. _prepare_audio 滤镜链（命令捕获）
# --------------------------------------------------------------------------
def _prepare_with_bgm_and_sfx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, sfx_events: list[SfxEvent]
) -> tuple[_RecordingFFmpeg, Path, RenderTimeline]:
    recorder = _RecordingFFmpeg()
    monkeypatch.setattr(s7_renderer, "run_ffmpeg", recorder)

    episode_dir, timeline = _make_timeline(tmp_path, sfx_events=sfx_events)
    bgm = tmp_path / "bgm.wav"
    _write_wav(bgm, 1.0, freq=180.0)

    assets = tmp_path / "assets"
    (assets / "sfx").mkdir(parents=True, exist_ok=True)
    (assets / "sfx" / "heart_thump.wav").write_bytes(_tone_wav_bytes(0.5))

    s7_renderer._prepare_audio(
        timeline,
        episode_dir=episode_dir,
        work_dir=tmp_path / "work",
        total_s=2.0,
        bgm_path=bgm,
        bgm_gain_db=AppConfig().render.bgm_gain_db,
        ffmpeg_bin="ffmpeg",
        sfx_events=sfx_events,
        assets_dir=assets,
    )
    return recorder, episode_dir, timeline


def test_prepare_audio_bgm_chain_loudnorm_before_gain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BGM 链：loudnorm 响度归一在前，-8.0dB 增益在后。"""
    recorder, _, _ = _prepare_with_bgm_and_sfx(
        tmp_path,
        monkeypatch,
        sfx_events=[SfxEvent(sfx_key="heart_thump", start_ms=200)],
    )
    joined = recorder.joined
    assert "loudnorm=I=-16:TP=-1.5:LRA=11" in joined
    assert "volume=-8.0dB" in joined
    # 顺序契约：同一条 BGM 链里 loudnorm 必须出现在 volume 之前。
    bgm_chain = next(part for part in joined.split(";") if "loudnorm" in part)
    assert bgm_chain.index("loudnorm") < bgm_chain.index("volume=")


def test_prepare_audio_sfx_chain_calibrates_before_adelay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SFX 链（Patch v4.37）：响度校准 → 淡入 → 限幅 → **最后**才 adelay 对齐。

    旧链是「adelay → dynaudnorm → volume」：前导静音会让 dynaudnorm 误判
    「素材本身电平低」并越抬越响，同一条素材落在不同时刻的音量对不上。
    新链先量去静音后的有效响度再校准，adelay 只负责时间对齐。
    """
    monkeypatch.setattr(s7_renderer, "probe_duration_ms", lambda *a, **k: 500)
    recorder, _, _ = _prepare_with_bgm_and_sfx(
        tmp_path,
        monkeypatch,
        sfx_events=[SfxEvent(sfx_key="heart_thump", start_ms=200)],
    )
    joined = recorder.joined
    assert "adelay=200|200" in joined
    assert "volume=-2.0dB" in joined
    assert "afade=t=in:st=0:d=0.005" in joined
    assert "alimiter=limit=-1.5dB:attack=2:release=40" in joined
    assert "dynaudnorm" not in joined
    assert "[sfx]" in joined
    sfx_chain = next(part for part in joined.split(";") if "adelay=200|200" in part)
    assert (
        sfx_chain.index("volume=")
        < sfx_chain.index("afade=")
        < sfx_chain.index("alimiter=")
        < sfx_chain.index("adelay")
    )


def test_schema_default_event_falls_back_to_config_gain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SfxEvent 不带显式增益（schema 默认 -10.0）时沿用渲染配置 -2.0dB。"""
    recorder, _, _ = _prepare_with_bgm_and_sfx(
        tmp_path, monkeypatch, sfx_events=[SfxEvent(sfx_key="heart_thump", start_ms=0)]
    )
    assert "volume=-2.0dB" in recorder.joined
    assert "volume=-10.0dB" not in recorder.joined


def test_explicit_event_gain_overrides_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """事件显式给出 -6.0dB 时覆盖配置默认值。"""
    recorder, _, _ = _prepare_with_bgm_and_sfx(
        tmp_path,
        monkeypatch,
        sfx_events=[SfxEvent(sfx_key="heart_thump", start_ms=0, gain_db=-6.0)],
    )
    assert "volume=-6.0dB" in recorder.joined


def test_mix_log_prints_bgm_name_and_sfx_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stage 7 显性混音日志：bgm 文件名/增益 + sfx 可用数/事件总数。"""
    _prepare_with_bgm_and_sfx(
        tmp_path,
        monkeypatch,
        sfx_events=[SfxEvent(sfx_key="heart_thump", start_ms=200)],
    )
    out = capsys.readouterr().out
    assert "[s7] 音频混合：bgm=bgm.wav (-8.0dB), sfx=1/1 条" in out

    # 无 BGM、且音效素材缺失（usable=0，total=1）的日志形态。
    recorder = _RecordingFFmpeg()
    monkeypatch.setattr(s7_renderer, "run_ffmpeg", recorder)
    episode_dir, timeline = _make_timeline(
        tmp_path / "none", sfx_events=[SfxEvent(sfx_key="missing_key", start_ms=100)]
    )
    s7_renderer._prepare_audio(
        timeline,
        episode_dir=episode_dir,
        work_dir=tmp_path / "work2",
        total_s=2.0,
        bgm_path=None,
        bgm_gain_db=-11.5,
        ffmpeg_bin="ffmpeg",
        sfx_events=list(timeline.sfx_events or []),
        assets_dir=tmp_path / "assets",
    )
    out2 = capsys.readouterr().out
    assert "[s7] 音频混合：bgm=NONE (-11.5dB), sfx=0/1 条" in out2


# --------------------------------------------------------------------------
# 3. 真实 ffmpeg 端到端：音效轨非静音
# --------------------------------------------------------------------------
@pytest.mark.skipif(FFMPEG_BIN is None, reason="本机没有 ffmpeg，跳过真实混音用例")
@pytest.mark.slow
def test_prepare_audio_real_ffmpeg_produces_non_silent_sfx(tmp_path: Path) -> None:
    """整条备音链跑通：sfx.wav 真实存在且 RMS>0（dynaudnorm 不会把音效压成静音）。"""
    episode_dir, timeline = _make_timeline(
        tmp_path, sfx_events=[SfxEvent(sfx_key="heart_thump", start_ms=200)]
    )
    bgm = tmp_path / "bgm.wav"
    _write_wav(bgm, 1.0, freq=160.0)
    assets = tmp_path / "assets"
    (assets / "sfx").mkdir(parents=True, exist_ok=True)
    (assets / "sfx" / "heart_thump.wav").write_bytes(_tone_wav_bytes(0.5, freq=660.0))
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    voice_path, bgm_path, sfx_path = s7_renderer._prepare_audio(
        timeline,
        episode_dir=episode_dir,
        work_dir=work_dir,
        total_s=2.0,
        bgm_path=bgm,
        bgm_gain_db=-11.5,
        ffmpeg_bin=FFMPEG_BIN or "ffmpeg",
        sfx_events=list(timeline.sfx_events or []),
        assets_dir=assets,
    )

    assert voice_path.is_file() and bgm_path is not None and bgm_path.is_file()
    assert sfx_path is not None and sfx_path.is_file()

    with wave.open(str(sfx_path), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
        channels = handle.getnchannels()
        width = handle.getsampwidth()
    assert width == 2
    sample_count = len(raw) // (2 * channels)
    assert sample_count > 0
    peaks = struct.unpack("<" + "h" * (sample_count * channels), raw)
    rms = math.sqrt(sum(value * value for value in peaks) / len(peaks))
    assert rms > 100.0, f"音效轨疑似静音：RMS={rms}"


# --------------------------------------------------------------------------
# 4. s6_timeline 保底关键词映射（规格验收二）
# --------------------------------------------------------------------------
def _keyword_beat(beat_id: str, panel_id: str) -> ScriptBeat:
    return ScriptBeat(
        beat_id=beat_id,
        narration_text="一句解说",
        speaker_role="narrator",
        target_panel_ids=[panel_id],
        motion=CameraMotion.ZOOM_IN,
    )


def test_acceptance_three_keywords_resolve_to_expected_keys() -> None:
    """规格原文：sfx_list 的「心驚 / 嘎咿 / 啪」→ heart_thump / whoosh / door_slam。"""
    panel_map = {
        "p_001": ["心驚"],
        "p_002": ["嘎咿"],
        "p_003": ["啪"],
    }
    beat_h = _keyword_beat("b1", "p_001")
    beat_w = _keyword_beat("b2", "p_002")
    beat_d = _keyword_beat("b3", "p_003")
    assert s6_timeline.resolve_sfx_key(beat_h, panel_map) == "heart_thump"
    assert s6_timeline.resolve_sfx_key(beat_w, panel_map) == "whoosh"
    assert s6_timeline.resolve_sfx_key(beat_d, panel_map) == "door_slam"


def test_keyword_map_covers_all_seven_groups() -> None:
    """扩充后的 7 组关键词每组至少有一个代表词能命中。"""
    cases = {
        "heart_thump": "顫",
        "door_slam": "甩開",
        "whoosh": "吱",
        "phone_msg": "手機",
        "footstep": "噠",
        "gasp": "倒吸",
        "crash": "碎",
    }
    for expected_key, keyword in cases.items():
        panel_map = {"p_001": [keyword]}
        assert (
            s6_timeline.resolve_sfx_key(_keyword_beat("b1", "p_001"), panel_map)
            == expected_key
        )


def test_keyword_fallback_respects_six_second_throttle() -> None:
    """三个命中点间隔不足 6s 时节流；常量保持 6000ms / 8 条不变。"""
    assert s6_timeline.SFX_MIN_GAP_MS == 6000
    assert s6_timeline.SFX_MAX_EVENTS == 8

    panel_map = {"p_001": ["心驚"], "p_002": ["嘎咿"], "p_003": ["啪"]}
    candidates: list[tuple[int, str]] = []
    for start_ms, (beat_id, panel_id) in [
        (0, ("b1", "p_001")),
        (3000, ("b2", "p_002")),
        (6000, ("b3", "p_003")),
    ]:
        key = s6_timeline.resolve_sfx_key(_keyword_beat(beat_id, panel_id), panel_map)
        assert key is not None
        candidates.append((start_ms, key))

    events = s6_timeline.select_sfx_events(candidates)
    assert [event.start_ms for event in events] == [0, 6000]
    assert [event.sfx_key for event in events] == ["heart_thump", "door_slam"]


def test_keyword_events_keep_max_eight() -> None:
    """单话音效上限仍是 8 条。"""
    many = [(index * 6000, "whoosh") for index in range(12)]
    assert len(s6_timeline.select_sfx_events(many)) == 8


def test_plain_scene_description_does_not_trigger_sfx() -> None:
    """不含关键词的普通画面描述（如「安静的走廊」）不得误触发音效。"""
    panel_map = {"p_001": ["安静的走廊，空无一人"]}
    assert s6_timeline.resolve_sfx_key(_keyword_beat("b1", "p_001"), panel_map) is None
