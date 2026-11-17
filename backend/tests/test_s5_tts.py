"""Stage 5（``stages/s5_tts.py``）离线测试。

全部用假合成器（不发网络请求），静音裁剪用本机真实的 ffmpeg 二进制跑一遍。
"""

from __future__ import annotations

import io
import json
import math
import shutil
import struct
import wave
from pathlib import Path
from typing import Any, Sequence

import pytest

from core.schemas import (
    EpisodeDigest,
    NarrationScript,
    ScriptBeat,
    load_model,
    save_model,
)
from providers.tts_base import TTSError, TTSResult
from stages import s5_tts

SAMPLE_RATE = 24000


# ----------------------------------------------------------------------
# 测试辅助
# ----------------------------------------------------------------------
def _tone(seconds: float, freq: float = 440.0, amp: float = 0.3) -> list[int]:
    """生成单声道有声音采样（起始相位 π/2，保证第一个采样就是高电平）。"""
    count = int(SAMPLE_RATE * seconds)
    return [
        int(amp * 32767 * math.sin(2 * math.pi * freq * index / SAMPLE_RATE + math.pi / 2))
        for index in range(count)
    ]


def _silence(seconds: float) -> list[int]:
    return [0] * int(SAMPLE_RATE * seconds)


def _wav_bytes(samples: Sequence[int], rate: int = SAMPLE_RATE) -> bytes:
    """把采样写成 PCM WAV 字节。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))
    return buffer.getvalue()


class FakeTTS:
    """假合成器：记录调用参数，返回真实 WAV 字节（绝不联网）。"""

    def __init__(
        self,
        *,
        samples: Sequence[int] | None = None,
        fail_texts: Sequence[str] = (),
        fail_all: bool = False,
        delay: float = 0.0,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._samples = list(samples) if samples is not None else _tone(0.3)
        self._fail_texts = set(fail_texts)
        self._fail_all = fail_all
        self._delay = delay

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str | None = None,
        language_type: str = "Chinese",
    ) -> TTSResult:
        self.calls.append(
            {
                "text": text,
                "instruction": instruction,
                "voice": voice,
                "language_type": language_type,
            }
        )
        if self._delay:
            import asyncio

            await asyncio.sleep(self._delay)
        if self._fail_all or text in self._fail_texts:
            raise TTSError(f"假失败：{text}")
        return TTSResult(
            audio=_wav_bytes(self._samples),
            content_type="audio/wav",
            voice=voice or "Fake",
        )


def _beat(beat_id: str, narration_text: str, **kwargs: Any) -> ScriptBeat:
    payload: dict[str, Any] = {
        "beat_id": beat_id,
        "narration_text": narration_text,
        "target_panel_ids": ["panel_001"],
    }
    payload.update(kwargs)
    return ScriptBeat(**payload)


def _script(beats: Sequence[ScriptBeat]) -> NarrationScript:
    return NarrationScript(
        episode_id="wt_test_ep_001",
        hook_title="雨夜来客",
        beats=list(beats),
        episode_digest=EpisodeDigest(
            episode_no=1,
            one_line_hook="雨夜有人来访",
            summary="来客自称银行行长。",
        ),
    )


def _make_series(
    tmp_path: Path,
    *,
    script: NarrationScript | None = None,
    episode_no: int = 1,
) -> Path:
    """搭一个作品目录（可选地写好 ``04_script/script.json``）。"""
    series_dir = tmp_path / "wt_test"
    script_dir = series_dir / "episodes" / f"ep_{episode_no:03d}" / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    if script is not None:
        save_model(script, script_dir / "script.json")
    return series_dir


# ----------------------------------------------------------------------
# 双文本分离（铁律）
# ----------------------------------------------------------------------
@pytest.mark.anyio
async def test_pronunciation_override_only_affects_spoken_text(tmp_path) -> None:
    """多音字替换只进送音文本，字幕文本一个字都不动。"""
    script = _script([_beat("beat_001", "银行行长推门而入。")])
    client = FakeTTS()

    metas = await s5_tts.synthesize_episode(
        client,
        script=script,
        audio_dir=tmp_path / "05_audio",
        overrides={"银行": "yín háng"},
    )

    assert client.calls[0]["text"] == "yín háng行长推门而入。"
    assert client.calls[0]["text"] != script.beats[0].narration_text
    # 字幕文本原样保留
    assert script.beats[0].narration_text == "银行行长推门而入。"
    assert metas[0].beat_id == "beat_001"


def test_apply_pronunciation_prefers_longest_key() -> None:
    """长 key 优先：短 key 是长 key 的前缀时不能抢先替换。"""
    assert s5_tts.apply_pronunciation("重霄", {"重": "x", "重霄": "y"}) == "y"
    # 没有长 key 命中时，短 key 照常生效
    assert s5_tts.apply_pronunciation("重量", {"重": "x", "重霄": "y"}) == "x量"
    # 空词典 / 空文本是安全的
    assert s5_tts.apply_pronunciation("银行", {}) == "银行"
    assert s5_tts.apply_pronunciation("", {"银行": "yín háng"}) == ""


@pytest.mark.anyio
async def test_manifest_never_contains_pronunciation_replacement(tmp_path) -> None:
    """音频清单里绝不能出现多音字替换结果，磁盘上的剧本也保持原样。"""
    script = _script([_beat("beat_001", "银行行长推门而入。")])
    series_dir = _make_series(tmp_path, script=script)
    client = FakeTTS()

    manifest_path = await s5_tts.run_stage5(
        series_dir, 1, client, overrides={"银行": "yín háng"}
    )

    raw = manifest_path.read_text(encoding="utf-8")
    assert "yín háng" not in raw
    payload = json.loads(raw)
    assert "narration_text" not in payload[0]

    # 送进 TTS 的文本被替换过，字幕文本没有
    assert client.calls[0]["text"] == "yín háng行长推门而入。"
    assert client.calls[0]["text"] != script.beats[0].narration_text

    # 落盘的剧本文件原样保留字幕文本
    on_disk = load_model(
        NarrationScript, series_dir / "episodes" / "ep_001" / "04_script" / "script.json"
    )
    assert on_disk.beats[0].narration_text == "银行行长推门而入。"


def test_build_spoken_text_prefers_tts_text_then_falls_back() -> None:
    """``tts_text`` 优先；为空时退回 ``narration_text``。"""
    with_tts = _beat("b1", "银行行长", tts_text="银行 yín háng 行长")
    assert s5_tts.build_spoken_text(with_tts, {}) == "银行 yín háng 行长"

    without_tts = _beat("b2", "银行行长", tts_text=None)
    assert s5_tts.build_spoken_text(without_tts, {}) == "银行行长"

    blank_tts = _beat("b3", "银行行长", tts_text="   ")
    assert s5_tts.build_spoken_text(blank_tts, {}) == "银行行长"

    # 退回字幕文本时，发音词典依然只作用在送音文本上
    assert s5_tts.build_spoken_text(without_tts, {"银行": "yín háng"}) == "yín háng行长"


def test_build_spoken_text_ignores_fragment_tts_text() -> None:
    """``tts_text`` 只写了被替换的那个词时，必须回退字幕原句。

    回归：wt_5081 ep_001 的 b33 —— 字幕 35 字、``tts_text`` 只有「混蛋」两个字，
    合成后整条解说只剩 0.35s，时间轴塌缩成 0.96s，画面与字幕一闪而过。
    """
    narration = "他歇斯底里地喊出真相：“人都是金仁哲杀的！我知道是谁指使那个混蛋的！”"
    fragment = _beat("b33", narration, tts_text="混蛋")
    assert s5_tts.build_spoken_text(fragment, {}) == narration

    # 脱敏字版本：``tts_text`` 只是片段、又不是字幕原句的子串，也不得独占整句
    masked = _beat("b34", "他指使那个混X的！", tts_text="混蛋")
    assert s5_tts.build_spoken_text(masked, {}) == "他指使那个混X的！"

    # 回退后发音词典照旧只作用在送音文本上
    assert s5_tts.build_spoken_text(fragment, {"混蛋": "hún dàn"}) == narration.replace(
        "混蛋", "hún dàn"
    )


def test_build_spoken_text_uses_whole_sentence_tts_text() -> None:
    """``tts_text`` 是整句送音文本（含替换后的标准汉字）时照常采用。"""
    beat = _beat("b35", "他指使那个混X的！", tts_text="他指使那个混蛋的！")
    assert s5_tts.build_spoken_text(beat, {}) == "他指使那个混蛋的！"

    # 整句比字幕短一点点也仍然是整句（只替换了脱敏字）
    beat2 = _beat("b36", "他指使那个混X的！", tts_text="他指使那个混蛋的")
    assert s5_tts.build_spoken_text(beat2, {}) == "他指使那个混蛋的"


@pytest.mark.anyio
async def test_emotion_instruction_passed_verbatim(tmp_path) -> None:
    """``emotion_instruction`` 原样透传给合成器，不改写、不加情绪词。"""
    instruction = "语速稍快，气息微促地说"
    script = _script([_beat("beat_001", "他猛地推开门。", emotion_instruction=instruction)])
    client = FakeTTS()

    await s5_tts.synthesize_episode(
        client, script=script, audio_dir=tmp_path / "05_audio"
    )

    assert client.calls[0]["instruction"] == instruction


@pytest.mark.anyio
async def test_voice_override_only_when_explicit(tmp_path) -> None:
    """``voice=None`` 时不覆盖 provider 默认音色；显式指定才覆盖。"""
    script = _script([_beat("beat_001", "一句话。")])

    client = FakeTTS()
    await s5_tts.synthesize_episode(client, script=script, audio_dir=tmp_path / "a")
    assert client.calls[0]["voice"] is None

    client2 = FakeTTS()
    await s5_tts.synthesize_episode(
        client2, script=script, audio_dir=tmp_path / "b", voice="Cherry"
    )
    assert client2.calls[0]["voice"] == "Cherry"


# ----------------------------------------------------------------------
# 静音裁剪（真实 ffmpeg）
# ----------------------------------------------------------------------
@pytest.mark.anyio
async def test_ffmpeg_trims_leading_and_trailing_silence(tmp_path) -> None:
    """真实 ffmpeg 裁剪：首字节不再是静音，且时长明显短于原始音频。"""
    if shutil.which("ffmpeg") is None:
        pytest.skip("本机没有 ffmpeg，跳过静音裁剪测试")

    original = _silence(0.5) + _tone(0.5) + _silence(0.5)
    client = FakeTTS(samples=original)
    script = _script([_beat("beat_001", "测试裁剪。")])
    audio_dir = tmp_path / "05_audio"

    metas = await s5_tts.synthesize_episode(client, script=script, audio_dir=audio_dir)

    assert len(metas) == 1
    meta = metas[0]
    audio_path = audio_dir / meta.audio_path
    assert audio_path.is_file()

    with wave.open(str(audio_path), "rb") as handle:
        frames = handle.getnframes()
        first_sample = struct.unpack("<h", handle.readframes(1)[:2])[0]

    # 前导 0.5s 静音被裁掉：第一个采样就是人声
    assert abs(first_sample) > 500, f"首采样仍是静音：{first_sample}"
    # 时长取裁剪后的真实值，明显小于原始 1.5s
    assert meta.duration_ms < 1500 * 0.8
    assert meta.duration_ms == int(round(frames * 1000 / SAMPLE_RATE))
    # 尾部只留 50ms 缓冲
    assert 450 <= meta.duration_ms <= 650


@pytest.mark.anyio
async def test_missing_ffmpeg_keeps_original_audio_and_records_issue(tmp_path) -> None:
    """裁剪不可用时不能拖垮整话：保留原音频，并把问题记录下来。"""
    samples = _tone(0.4)
    client = FakeTTS(samples=samples)
    script = _script([_beat("beat_001", "一句话。")])
    audio_dir = tmp_path / "05_audio"

    metas = await s5_tts.synthesize_episode(
        client,
        script=script,
        audio_dir=audio_dir,
        ffmpeg_bin="definitely-not-a-real-ffmpeg-binary",
    )

    assert len(metas) == 1
    audio_path = audio_dir / metas[0].audio_path
    assert audio_path.is_file()
    assert metas[0].duration_ms == 400

    issues = json.loads(
        (audio_dir / s5_tts.AUDIO_ISSUES_FILENAME).read_text(encoding="utf-8")
    )
    assert issues[0]["stage"] == "trim_silence"
    assert "beat_001" == issues[0]["beat_id"]


# ----------------------------------------------------------------------
# 清单落盘与读回
# ----------------------------------------------------------------------
@pytest.mark.anyio
async def test_audio_manifest_written_and_loaded_back(tmp_path) -> None:
    """``audio_manifest.json`` 落盘，``load_audio_manifest`` 读回一致。"""
    script = _script(
        [
            _beat("beat_001", "第一句话。", pause_after_ms=320),
            _beat("beat_002", "第二句话。"),
        ]
    )
    series_dir = _make_series(tmp_path, script=script)
    client = FakeTTS()

    manifest_path = await s5_tts.run_stage5(series_dir, 1, client)

    assert manifest_path == series_dir / "episodes" / "ep_001" / "05_audio" / "audio_manifest.json"
    assert manifest_path.is_file()

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(payload, list) and len(payload) == 2
    # 键名与 pydantic 模型字段一致
    assert set(payload[0]) == {
        "beat_id",
        "audio_path",
        "duration_ms",
        "pause_after_ms",
        "word_timestamps",
    }
    assert payload[0]["pause_after_ms"] == 320
    # Patch v3.2 Patch 12：默认停顿由 250ms 收紧到 150ms。
    assert payload[1]["pause_after_ms"] == 150
    assert payload[0]["word_timestamps"] == []

    metas = s5_tts.load_audio_manifest(manifest_path.parent)
    assert [meta.beat_id for meta in metas] == ["beat_001", "beat_002"]
    assert metas[0].audio_path == "beat_beat_001.wav"
    assert metas[0].duration_ms == payload[0]["duration_ms"]
    # 清单里是相对 05_audio 的文件名，音频文件真实存在
    for meta in metas:
        assert (manifest_path.parent / meta.audio_path).is_file()


@pytest.mark.anyio
async def test_beat_id_is_sanitised_for_filename_only(tmp_path) -> None:
    """非法字符只影响文件名，manifest 里保留原始 ``beat_id``。"""
    script = _script([_beat("beat/001 危险", "一句话。")])
    client = FakeTTS()

    metas = await s5_tts.synthesize_episode(
        client, script=script, audio_dir=tmp_path / "05_audio"
    )

    assert metas[0].beat_id == "beat/001 危险"
    assert metas[0].audio_path == "beat_beat_001_危险.wav"
    assert (tmp_path / "05_audio" / metas[0].audio_path).is_file()


# ----------------------------------------------------------------------
# 失败降级
# ----------------------------------------------------------------------
@pytest.mark.anyio
async def test_single_beat_failure_does_not_break_episode(tmp_path) -> None:
    """单条 beat 失败只记录问题，其余 beat 照常产出。"""
    script = _script(
        [
            _beat("beat_001", "第一句话。"),
            _beat("beat_002", "会失败的一句。"),
            _beat("beat_003", "第三句话。"),
        ]
    )
    client = FakeTTS(fail_texts={"会失败的一句。"})
    audio_dir = tmp_path / "05_audio"

    metas = await s5_tts.synthesize_episode(client, script=script, audio_dir=audio_dir)

    assert [meta.beat_id for meta in metas] == ["beat_001", "beat_003"]
    issues = json.loads((audio_dir / s5_tts.AUDIO_ISSUES_FILENAME).read_text(encoding="utf-8"))
    assert any(item["beat_id"] == "beat_002" for item in issues)


@pytest.mark.anyio
async def test_all_beats_failed_raises_tts_stage_error(tmp_path) -> None:
    """全部失败必须抛 :class:`TTSStageError`。"""
    script = _script([_beat("beat_001", "一句话。"), _beat("beat_002", "另一句。")])
    client = FakeTTS(fail_all=True)

    with pytest.raises(s5_tts.TTSStageError):
        await s5_tts.synthesize_episode(
            client, script=script, audio_dir=tmp_path / "05_audio"
        )


@pytest.mark.anyio
async def test_run_stage5_without_script_file_raises(tmp_path) -> None:
    """缺少 ``04_script/script.json`` 时必须报错。"""
    series_dir = _make_series(tmp_path)

    with pytest.raises(s5_tts.TTSStageError):
        await s5_tts.run_stage5(series_dir, 1, FakeTTS())


@pytest.mark.anyio
async def test_run_stage5_without_script_file_does_not_create_audio_dir(tmp_path) -> None:
    """输入缺失时不应留下半成品目录。"""
    series_dir = _make_series(tmp_path)

    with pytest.raises(s5_tts.TTSStageError):
        await s5_tts.run_stage5(series_dir, 1, FakeTTS())

    assert not (series_dir / "episodes" / "ep_001" / "05_audio").exists()


# ----------------------------------------------------------------------
# 其它
# ----------------------------------------------------------------------
def test_audio_dir_for_matches_episode_layout(tmp_path) -> None:
    assert s5_tts.audio_dir_for(tmp_path, 3) == (
        tmp_path / "episodes" / "ep_003" / "05_audio"
    )
    assert s5_tts.AUDIO_DIR_NAME == "05_audio"


@pytest.mark.anyio
async def test_manifest_order_follows_script_beats(tmp_path) -> None:
    """并发合成后清单顺序仍与剧本 beats 一致。"""
    script = _script([_beat(f"beat_{index:03d}", f"第{index}句话。") for index in range(1, 6)])
    client = FakeTTS(delay=0.01)

    metas = await s5_tts.synthesize_episode(
        client, script=script, audio_dir=tmp_path / "05_audio", concurrency=3
    )

    assert [meta.beat_id for meta in metas] == [f"beat_{index:03d}" for index in range(1, 6)]
