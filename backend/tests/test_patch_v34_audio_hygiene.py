"""Patch v3.4 Patch 17：音频卫生（响度归一 / 旧音频清理 / 相邻去重 / 对白继承）。

覆盖规格里的 4 项修复：

1. 单句级 ``-16 LUFS`` 归一（``loudnorm=I=-16:TP=-1.5:LRA=7``）串在同一条
   ffmpeg 滤镜链末尾，且 provider 自带语速能力（MiniMax）时不叠加 ``atempo``；
2. ``05_audio/`` 里的上一轮残留音频在重新合成前被清理；
3. 相邻重复对白确定性去重；
4. 心理独白保留旁白 + 连续对白说话人 / 性别继承。

全部离线：不触网，ffmpeg 端到端用例在本机没有 ffmpeg 时自动跳过。
"""

from __future__ import annotations

import io
import math
import shutil
import struct
import wave
from pathlib import Path
from typing import Any, Sequence

import pytest

from core.normalize import (
    MIN_DIALOGUE_CHARS,
    apply_dialogue_speaker_inheritance,
    deduplicate_adjacent_beats,
    resolve_dialogue_speaker_inheritance,
)
from core.schemas import EpisodeDigest, NarrationScript, ScriptBeat
from providers.tts_base import TTSResult
from stages import s4_scriptwriter, s5_tts

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _wav_bytes(seconds: float = 0.3, rate: int = 24000, freq: int = 440) -> bytes:
    """造一段正弦波 WAV 字节。"""
    buffer = io.BytesIO()
    total = int(rate * seconds)
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for index in range(total):
            sample = int(12000 * math.sin(2 * math.pi * freq * index / rate))
            frames.extend(struct.pack("<h", sample))
        handle.writeframes(bytes(frames))
    return buffer.getvalue()


def _write_wav(path: Path, seconds: float = 1.0, rate: int = 24000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wav_bytes(seconds=seconds, rate=rate))
    return path


def _silence_then_tone_wav(rate: int = 24000) -> bytes:
    """前半段静音、后半段人声（首采样为正峰值）的 WAV 字节。"""
    buffer = io.BytesIO()
    silence_frames = int(rate * 0.5)
    tone_frames = int(rate * 0.5)
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for index in range(tone_frames):
            sample = int(
                9800 * math.sin(2 * math.pi * 440 * index / rate + math.pi / 2)
            )
            frames.extend(struct.pack("<h", sample))
        handle.writeframes(bytes(bytearray(silence_frames * 2) + bytes(frames)))
    return buffer.getvalue()


def _beat(beat_id: str, narration_text: str, **kwargs: Any) -> ScriptBeat:
    payload: dict[str, Any] = {
        "beat_id": beat_id,
        "narration_text": narration_text,
        "tts_text": narration_text,
        "target_panel_ids": ["p_001"],
        "speaker_role": "narrator",
    }
    payload.update(kwargs)
    return ScriptBeat(**payload)


def _script(beats: Sequence[ScriptBeat]) -> NarrationScript:
    return NarrationScript(
        episode_id="wt_hygiene_ep_001",
        hook_title="雨夜来客",
        beats=list(beats),
        episode_digest=EpisodeDigest(
            episode_no=1,
            one_line_hook="雨夜有人来访",
            summary="来客自称银行行长。",
        ),
    )


class FakeTTS:
    """假合成器：记录调用参数，返回真实 WAV 字节（绝不联网）。"""

    def __init__(self, *, supports_native_speed: bool = False, model: str = "") -> None:
        self.calls: list[dict[str, Any]] = []
        self.supports_native_speed = supports_native_speed
        self.model = model

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str | None = None,
        **kwargs: Any,
    ) -> TTSResult:
        self.calls.append(
            {
                "text": text,
                "instruction": instruction,
                "voice": voice,
                **kwargs,
            }
        )
        return TTSResult(
            audio=_wav_bytes(0.2),
            content_type="audio/wav",
            voice=voice or "Fake",
        )


def _capture_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """把 ffmpeg 滤镜链抓下来（不真的跑 ffmpeg）。"""
    captured: list[str] = []

    async def _fake_run_command(
        cmd: Sequence[str], *, timeout: float = 0.0
    ) -> tuple[int, str, str]:
        cmd_list = list(cmd)
        captured.append(cmd_list[cmd_list.index("-af") + 1])
        Path(cmd_list[-1]).write_bytes(_wav_bytes(0.2))
        return 0, "", ""

    monkeypatch.setattr(s5_tts, "_run_command", _fake_run_command)
    return captured


# ---------------------------------------------------------------------------
# A. 单句 -16 LUFS 响度归一
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_trim_silence_filter_ends_with_loudnorm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """滤镜链末尾必须串上 loudnorm + aresample（单句归一）。"""
    captured = _capture_filter(monkeypatch)
    src = _write_wav(tmp_path / "beat.wav", seconds=1.0)

    path, issue = await s5_tts._trim_silence(src, speed=1.0)
    assert issue is None
    assert path is not None
    assert len(captured) == 1
    audio_filter = captured[0]
    assert "loudnorm=I=-16:TP=-1.5:LRA=7" in audio_filter
    # loudnorm 之后立刻重采样回 24kHz（它会把输出撑到 192kHz）。
    assert "aresample=24000" in audio_filter
    assert audio_filter.index("loudnorm=") < audio_filter.index("aresample=24000")
    # 语速为 1.0 时不叠 atempo（Patch v3.2 旧契约）。
    assert "atempo" not in audio_filter


@pytest.mark.anyio
async def test_trim_silence_loudnorm_runs_after_atempo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """变速在前、响度归一在后（归一的是最终播放速度下的响度）。"""
    captured = _capture_filter(monkeypatch)
    src = _write_wav(tmp_path / "beat.wav", seconds=1.0)

    await s5_tts._trim_silence(src, speed=1.25)
    audio_filter = captured[0]
    assert "atempo=1.25" in audio_filter
    assert audio_filter.index("atempo=1.25") < audio_filter.index("loudnorm=")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="本机没有 ffmpeg，跳过端到端响度校验")
@pytest.mark.slow
@pytest.mark.anyio
async def test_trim_silence_real_ffmpeg_outputs_24k(tmp_path: Path) -> None:
    """真实 ffmpeg：归一后仍是 24kHz 单声道 WAV（loudnorm 的 192kHz 被拉回来）。"""
    src = _write_wav(tmp_path / "beat.wav", seconds=2.0, rate=24000)
    path, issue = await s5_tts._trim_silence(src, ffmpeg_bin="ffmpeg", speed=1.0)
    assert issue is None
    assert path is not None
    with wave.open(str(path), "rb") as handle:
        assert handle.getframerate() == 24000
        assert handle.getnchannels() == 1
    # loudnorm 不改变时长。
    duration = s5_tts._wav_duration_ms(path)
    assert duration is not None and 1800 < duration < 2200


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="本机没有 ffmpeg，跳过端到端响度校验")
@pytest.mark.slow
@pytest.mark.anyio
async def test_trim_silence_real_ffmpeg_trims_loudnorm_ringing(tmp_path: Path) -> None:
    """归一后的升采样振铃会被二次裁剪清掉：裁完第一个采样就是人声。"""
    src = tmp_path / "beat.wav"
    src.write_bytes(_silence_then_tone_wav())
    path, issue = await s5_tts._trim_silence(src, ffmpeg_bin="ffmpeg", speed=1.0)
    assert issue is None
    assert path is not None
    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    first = struct.unpack("<h", frames[:2])[0]
    assert abs(first) > 500
    # 静音段被裁掉：时长只剩后半段人声。
    duration = s5_tts._wav_duration_ms(path)
    assert duration is not None and 400 < duration < 700


@pytest.mark.anyio
async def test_native_speed_client_receives_speed_without_atempo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """provider 自带语速能力（MiniMax）时：语速交给 provider，ffmpeg 不二次变速。"""
    captured = _capture_filter(monkeypatch)
    client = FakeTTS(supports_native_speed=True, model="speech-2.8-hd")
    script = _script([_beat("b1", "这是一句需要加速的解说。")])

    metas = await s5_tts.synthesize_episode(
        client,
        script=script,
        audio_dir=tmp_path / "audio",
        series_tts=None,
        default_voice="Elias",
        ffmpeg_bin="ffmpeg",
        trim_silence=True,
    )
    assert len(metas) == 1
    assert client.calls, "provider 应收到一次合成调用"
    assert client.calls[0].get("speed") == 1.0
    # 原生语速路径下滤镜链里没有 atempo。
    assert captured and all("atempo" not in item for item in captured)


# ---------------------------------------------------------------------------
# B. 05_audio 旧音频清理
# ---------------------------------------------------------------------------


def test_clean_stale_audio_removes_beat_files(tmp_path: Path) -> None:
    audio_dir = tmp_path / "05_audio"
    audio_dir.mkdir(parents=True)
    (audio_dir / "beat_b1.wav").write_bytes(b"old")
    (audio_dir / "beat_b2.mp3").write_bytes(b"old")
    (audio_dir / "keep.wav").write_bytes(b"keep")
    (audio_dir / "manifest.json").write_text("{}", encoding="utf-8")

    removed = s5_tts.clean_stale_audio(audio_dir)

    assert sorted(removed) == ["beat_b1.wav", "beat_b2.mp3"]
    assert not (audio_dir / "beat_b1.wav").exists()
    assert not (audio_dir / "beat_b2.mp3").exists()
    # 非 beat 前缀的文件不动。
    assert (audio_dir / "keep.wav").exists()
    assert (audio_dir / "manifest.json").exists()


@pytest.mark.anyio
async def test_synthesize_episode_cleans_stale_audio(tmp_path: Path) -> None:
    """重新合成前清掉上一轮残留（重跑撰稿后 beat 编号会变）。"""
    audio_dir = tmp_path / "05_audio"
    audio_dir.mkdir(parents=True)
    stale = audio_dir / "beat_b9.wav"
    stale.write_bytes(b"old")
    other = audio_dir / "bgm.wav"
    other.write_bytes(b"bgm")

    client = FakeTTS()
    script = _script([_beat("b1", "这一句会被重新合成。")])
    metas = await s5_tts.synthesize_episode(
        client,
        script=script,
        audio_dir=audio_dir,
        series_tts=None,
        default_voice="Elias",
        ffmpeg_bin="ffmpeg",
        trim_silence=False,
    )

    assert len(metas) == 1
    assert not stale.exists()
    assert (audio_dir / "beat_b1.wav").exists()
    assert other.exists()


# ---------------------------------------------------------------------------
# C. 相邻重复对白去重
# ---------------------------------------------------------------------------


def test_deduplicate_adjacent_beats_drops_identical() -> None:
    beats = [
        {
            "beat_id": "b1",
            "tts_text": "这个地方是用来做什么的呀？",
            "target_panel_ids": ["p_001"],
        },
        {
            "beat_id": "b2",
            "tts_text": "这个地方是用来做什么的呀",
            "target_panel_ids": ["p_002"],
        },
    ]
    kept = deduplicate_adjacent_beats(beats)
    assert [beat["beat_id"] for beat in kept] == ["b1"]


def test_deduplicate_adjacent_beats_drops_contained_same_panel() -> None:
    beats = [
        {
            "beat_id": "b1",
            "tts_text": "你终于来了啊我等你好久了很久",
            "target_panel_ids": ["p_001"],
        },
        {
            "beat_id": "b2",
            "tts_text": "你终于来了啊",
            "target_panel_ids": ["p_001"],
        },
    ]
    kept = deduplicate_adjacent_beats(beats)
    assert [beat["beat_id"] for beat in kept] == ["b1"]


def test_deduplicate_adjacent_beats_keeps_different_panels() -> None:
    """不同画面里的短句不算重复（保留）。"""
    beats = [
        {
            "beat_id": "b1",
            "tts_text": "你终于来了啊我等你好久了很久",
            "target_panel_ids": ["p_001"],
        },
        {
            "beat_id": "b2",
            "tts_text": "你终于来了啊",
            "target_panel_ids": ["p_002"],
        },
    ]
    kept = deduplicate_adjacent_beats(beats)
    assert [beat["beat_id"] for beat in kept] == ["b1", "b2"]


def test_normalize_script_payload_deduplicates_adjacent_beats() -> None:
    """Stage 4B 归一化出口也会去重（不留重复句进配音）。"""
    payload = {
        "episode_id": "wt_test_ep_001",
        "hook_title": "雨夜来客",
        "beats": [
            {
                "beat_id": "b1",
                "narration_text": "你终于来了。",
                "tts_text": "你终于来了。",
                "target_panel_ids": ["p_001"],
            },
            {
                "beat_id": "b2",
                "narration_text": "你终于来了！",
                "tts_text": "你终于来了！",
                "target_panel_ids": ["p_002"],
            },
        ],
        "episode_digest": {
            "episode_no": 1,
            "one_line_hook": "雨夜有人来访",
            "summary": "来客自称银行行长。",
        },
    }
    from core.normalize import normalize_script_payload

    normalized = normalize_script_payload(
        payload, episode_id="wt_test_ep_001", episode_no=1
    )
    assert len(normalized["beats"]) == 1


# ---------------------------------------------------------------------------
# D. 心理独白保留旁白
# ---------------------------------------------------------------------------


def test_split_mixed_quote_beats_keeps_thought_as_narrator() -> None:
    """「他心想：“……”」里的引号内容是心里话 → 保留旁白声线。"""
    beats = [
        {
            "beat_id": "b1",
            "narration_text": "他心想：“这地方不对劲”，随后退了一步。",
            "tts_text": "他心想：这地方不对劲，随后退了一步。",
            "target_panel_ids": ["p_001"],
            "speaker_role": "narrator",
        }
    ]
    result = s4_scriptwriter.split_mixed_quote_beats(beats)
    quote_beats = [
        beat
        for beat in result
        if beat["narration_text"].strip().startswith("“")
        and beat["narration_text"].strip().endswith("”")
    ]
    assert quote_beats, "引号内容应被拆成独立 beat"
    for beat in quote_beats:
        assert beat["speaker_role"] == "narrator"
        assert beat.get("speaker_gender") == "unknown"


def test_split_mixed_quote_beats_short_quote_stays_narrator() -> None:
    """「嗯？」这类短拟声没有说话人 → 留在旁白声线。"""
    beats = [
        {
            "beat_id": "b1",
            "narration_text": "“嗯？”",
            "tts_text": "嗯？",
            "target_panel_ids": ["p_001"],
            "speaker_role": "narrator",
        }
    ]
    result = s4_scriptwriter.split_mixed_quote_beats(beats)
    assert len(result) == 1
    assert result[0]["speaker_role"] == "narrator"
    assert result[0].get("speaker_gender") == "unknown"


# ---------------------------------------------------------------------------
# E. 连续对白说话人继承
# ---------------------------------------------------------------------------


def test_resolve_dialogue_speaker_inheritance_within_one_panel() -> None:
    beats = [
        {
            "beat_id": "b1",
            "narration_text": "“你不该来这里。”",
            "speaker_role": "继母",
            "speaker_gender": "female",
            "target_panel_ids": ["p_001"],
        },
        {
            "beat_id": "b2",
            "narration_text": "“我偏要来看清楚。”",
            "speaker_role": "narrator",
            "speaker_gender": "unknown",
            "target_panel_ids": ["p_001"],
        },
    ]
    mapping = resolve_dialogue_speaker_inheritance(beats)
    assert mapping == {"b2": ("继母", "female")}


def test_resolve_dialogue_speaker_inheritance_skips_far_panels() -> None:
    """分镜间隔超过 1 格 → 不继承（不是连续对话）。"""
    beats = [
        {
            "beat_id": "b1",
            "narration_text": "“你不该来这里。”",
            "speaker_role": "继母",
            "speaker_gender": "female",
            "target_panel_ids": ["p_001"],
        },
        {
            "beat_id": "b2",
            "narration_text": "“我偏要来看清楚。”",
            "speaker_role": "narrator",
            "speaker_gender": "unknown",
            "target_panel_ids": ["p_004"],
        },
    ]
    assert resolve_dialogue_speaker_inheritance(beats) == {}


def test_resolve_dialogue_speaker_inheritance_broken_by_narration() -> None:
    """中间有旁白打断 → 不继承。"""
    beats = [
        {
            "beat_id": "b1",
            "narration_text": "“你不该来这里。”",
            "speaker_role": "继母",
            "speaker_gender": "female",
            "target_panel_ids": ["p_001"],
        },
        {
            "beat_id": "b2",
            "narration_text": "窗外雷声炸开。",
            "speaker_role": "narrator",
            "speaker_gender": "unknown",
            "target_panel_ids": ["p_001"],
        },
        {
            "beat_id": "b3",
            "narration_text": "“我偏要来看清楚。”",
            "speaker_role": "narrator",
            "speaker_gender": "unknown",
            "target_panel_ids": ["p_001"],
        },
    ]
    assert resolve_dialogue_speaker_inheritance(beats) == {}


def test_resolve_dialogue_speaker_inheritance_skips_short_quotes() -> None:
    """短拟声不继承（本来就留在旁白声线）。"""
    beats = [
        {
            "beat_id": "b1",
            "narration_text": "“你不该来这里。”",
            "speaker_role": "继母",
            "speaker_gender": "female",
            "target_panel_ids": ["p_001"],
        },
        {
            "beat_id": "b2",
            "narration_text": "“嗯？”",
            "speaker_role": "narrator",
            "speaker_gender": "unknown",
            "target_panel_ids": ["p_001"],
        },
    ]
    assert resolve_dialogue_speaker_inheritance(beats) == {}
    assert MIN_DIALOGUE_CHARS == 6


def test_apply_dialogue_speaker_inheritance_writes_back() -> None:
    beats = [
        {
            "beat_id": "b1",
            "narration_text": "“你不该来这里。”",
            "speaker_role": "继母",
            "speaker_gender": "female",
            "target_panel_ids": ["p_001"],
        },
        {
            "beat_id": "b2",
            "narration_text": "“我偏要来看清楚。”",
            "speaker_role": "narrator",
            "speaker_gender": "unknown",
            "target_panel_ids": ["p_001"],
        },
    ]
    updated = apply_dialogue_speaker_inheritance(beats)
    assert updated[1]["speaker_role"] == "继母"
    assert updated[1]["speaker_gender"] == "female"
    # 入参不被就地修改。
    assert beats[1]["speaker_role"] == "narrator"


@pytest.mark.anyio
async def test_synthesize_episode_inherits_dialogue_speaker(tmp_path: Path) -> None:
    """Stage 5 侧：继承后按继承到的说话人选音色（男角色不会突然变女声）。"""
    client = FakeTTS()
    script = _script(
        [
            _beat(
                "b1",
                "“你不该来这里。”",
                speaker_role="阿明",
                speaker_gender="male",
                target_panel_ids=["p_001"],
            ),
            _beat(
                "b2",
                "“我偏要来看清楚。”",
                speaker_role="narrator",
                speaker_gender="unknown",
                target_panel_ids=["p_001"],
            ),
        ]
    )

    metas = await s5_tts.synthesize_episode(
        client,
        script=script,
        audio_dir=tmp_path / "audio",
        voice_cast={"阿明": "Ethan", "narrator": "Elias"},
        series_tts=None,
        default_voice="Elias",
        ffmpeg_bin="ffmpeg",
        trim_silence=False,
    )

    assert len(metas) == 2
    voices = {call["voice"] for call in client.calls}
    assert voices == {"Ethan"}, f"两句话应都用阿明的音色，实际 {voices}"
