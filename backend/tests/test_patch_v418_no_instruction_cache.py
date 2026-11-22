"""Patch v4.18：情绪指令不再进入 Stage 5 缓存指纹 + 单句重配无条件覆盖缓存。

三条主线（全部离线：假合成器不触网，``trim_silence=False`` 不依赖本机 ffmpeg，
时长探测走 ``wave`` 直读）：

1. **改情绪指令不重新合成**——情绪指令只是送音语气提示，不参与「是不是同一段音频」
   的判定，改了它不得触发重新 TTS（否则调个语气就把整话配音全部重算一遍）；
2. **真正影响音频的输入仍然失效**——改 ``narration_text``（送音文本）或
   ``speed_override``（语速）必须重新合成；
3. **单句重配要能覆盖旧缓存**——重配后的音频可能与旧音频**字节长度完全相同**
   （同一句话、同样采样率与时长），只比文件大小会误判「内容没变」而留下旧音频，
   于是「重配完再跑整话」又变回旧声音。

情绪指令仍然**原样透传**给 ``client.synthesize``（契约不变），只是不进缓存指纹。
"""

from __future__ import annotations

import io
import json
import math
import struct
import wave
from pathlib import Path
from typing import Any, Sequence

import pytest
from PIL import Image

from core.schemas import (
    EpisodeDigest,
    NarrationScript,
    ScriptBeat,
    save_model,
)
from providers.tts_base import TTSResult
from stages import s0_catalog, s5_tts

SERIES_ID = "wt_v418"
SAMPLE_RATE = 24000


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------
def _tone(seconds: float, freq: float = 440.0, amp: float = 0.3) -> list[int]:
    """生成一段单声道正弦波采样（离线、确定性）。"""
    count = int(SAMPLE_RATE * seconds)
    return [
        int(amp * 32767 * math.sin(2 * math.pi * freq * index / SAMPLE_RATE + math.pi / 2))
        for index in range(count)
    ]


def _wav_bytes(samples: Sequence[int], rate: int = SAMPLE_RATE) -> bytes:
    """把采样打包成真实 WAV 字节（能被 ``wave`` 直读时长）。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))
    return buffer.getvalue()


class FakeTTS:
    """假合成器：记录调用参数，返回真实 WAV 字节（绝不联网）。"""

    def __init__(self, *, samples: Sequence[int] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self._samples = list(samples) if samples is not None else _tone(0.3)

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str | None = None,
        language_type: str = "Chinese",
        **kwargs: Any,
    ) -> TTSResult:
        self.calls.append(
            {
                "text": text,
                "instruction": instruction,
                "voice": voice,
                "language_type": language_type,
                **kwargs,
            }
        )
        return TTSResult(
            audio=_wav_bytes(self._samples),
            content_type="audio/wav",
            voice=voice or "Fake",
        )

    async def aclose(self) -> None:
        self.closed = True


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
        episode_id=f"{SERIES_ID}_ep_001",
        hook_title="雨夜来客",
        beats=list(beats),
        episode_digest=EpisodeDigest(
            episode_no=1,
            one_line_hook="雨夜有人来访",
            summary="来客自称银行行长。",
        ),
    )


def _register(root: Path) -> tuple[str, Path]:
    """用 s0_catalog 登记作品，返回 (series_id, series_dir)。"""
    src = root / "src"
    ep_src = src / "ep_001"
    ep_src.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 600), (40, 80, 120)).save(ep_src / "001.png")
    catalog = s0_catalog.import_local_catalog(src)
    s0_catalog.CatalogRegistry(root).register(catalog)
    return catalog.series_id, root / catalog.series_id


def _write_script(series_dir: Path, beats: Sequence[ScriptBeat]) -> Path:
    script_dir = series_dir / "episodes" / "ep_001" / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    save_model(_script(beats), script_dir / "script.json")
    return script_dir / "script.json"


async def _run_stage5(
    series_dir: Path,
    beats: Sequence[ScriptBeat],
    client: FakeTTS,
    **kwargs: Any,
) -> dict[str, Any]:
    """写剧本并跑 Stage 5，返回统计信息（reused / synthesized）。"""
    _write_script(series_dir, beats)
    stats: dict[str, Any] = {}
    await s5_tts.run_stage5(
        series_dir,
        1,
        client,
        trim_silence=False,
        stats_out=stats,
        **kwargs,
    )
    return stats


def _audio_dir(series_dir: Path) -> Path:
    return series_dir / "episodes" / "ep_001" / "05_audio"


def _beat_audio_bytes(series_dir: Path, beat_id: str) -> bytes:
    """读该 beat 的成品音频字节。"""
    path = _audio_dir(series_dir) / f"beat_{beat_id}.wav"
    assert path.is_file(), f"缺少成品音频：{path}"
    return path.read_bytes()


# ---------------------------------------------------------------------------
# A. 情绪指令不进缓存指纹
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_emotion_instruction_change_does_not_resynthesize(tmp_path: Path) -> None:
    """只改情绪指令：一条 TTS 都不调，全部复用缓存。"""
    _series_id, series_dir = _register(tmp_path)

    calm = [_beat("b1", "银行行长推门而入。", emotion_instruction="平静叙述")]
    first = FakeTTS()
    stats_first = await _run_stage5(series_dir, calm, first)
    assert len(first.calls) == 1
    assert stats_first.get("synthesized") == 1
    assert stats_first.get("reused", 0) == 0
    # 情绪指令仍然原样透传给合成器（契约不变）
    assert first.calls[0]["instruction"] == "平静叙述"

    # 第 2 轮：只把情绪指令从「平静叙述」改成「愤怒咆哮」，其余一律不变。
    angry = [_beat("b1", "银行行长推门而入。", emotion_instruction="愤怒咆哮")]
    second = FakeTTS()
    stats_second = await _run_stage5(series_dir, angry, second)
    assert len(second.calls) == 0
    assert stats_second.get("reused") == 1
    assert stats_second.get("synthesized") == 0


def test_fingerprint_excludes_instruction_but_plan_keeps_it() -> None:
    """指纹里没有 ``instruction``，但 ``plan.instruction`` 仍透传情绪指令。"""
    beat = _beat("b1", "银行行长推门而入。", emotion_instruction="愤怒咆哮")
    plan = s5_tts._resolve_beat_synth_plan(
        FakeTTS(),
        beat,
        overrides=None,
        voice=None,
    )
    assert plan is not None
    assert "instruction" not in plan.fingerprint
    assert plan.instruction == "愤怒咆哮"


# ---------------------------------------------------------------------------
# B. 真正影响音频的输入仍然让缓存失效
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_narration_change_still_invalidates_cache(tmp_path: Path) -> None:
    """改台词（送音文本变了）：必须重新合成。"""
    _series_id, series_dir = _register(tmp_path)

    first = FakeTTS()
    await _run_stage5(series_dir, [_beat("b1", "银行行长推门而入。")], first)
    assert len(first.calls) == 1

    changed = [_beat("b1", "银行行长把伞收了进来。")]
    second = FakeTTS()
    stats = await _run_stage5(series_dir, changed, second)
    assert len(second.calls) == 1
    assert "把伞收了进来" in second.calls[0]["text"]
    assert stats.get("synthesized") == 1
    assert stats.get("reused", 0) == 0


@pytest.mark.anyio
async def test_speed_override_change_still_invalidates_cache(tmp_path: Path) -> None:
    """改单句语速：必须重新合成。"""
    _series_id, series_dir = _register(tmp_path)

    first = FakeTTS()
    await _run_stage5(series_dir, [_beat("b1", "银行行长推门而入。")], first)
    assert len(first.calls) == 1

    changed = [_beat("b1", "银行行长推门而入。", speed_override=1.2)]
    second = FakeTTS()
    stats = await _run_stage5(series_dir, changed, second)
    assert len(second.calls) == 1
    assert stats.get("synthesized") == 1
    assert stats.get("reused", 0) == 0


# ---------------------------------------------------------------------------
# C. 单句重配要覆盖旧缓存（字节长度相同、波形不同）
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_single_beat_resynthesize_overwrites_cache_with_same_size(
    tmp_path: Path,
) -> None:
    """重配后的音频字节长度与旧音频相同：整话重跑也必须拿到新音频，不能回退成旧音频。"""
    _series_id, series_dir = _register(tmp_path)
    beats = [_beat("b1", "银行行长推门而入。")]

    # 首轮：440Hz 音调
    first = FakeTTS(samples=_tone(0.3, 440.0))
    await _run_stage5(series_dir, beats, first)
    assert len(first.calls) == 1
    old_audio = _beat_audio_bytes(series_dir, "b1")

    # 单句重配：880Hz 音调——采样数相同，WAV 字节长度完全一样，波形却不同。
    resynth = FakeTTS(samples=_tone(0.3, 880.0))
    await s5_tts.resynthesize_single_beat(
        resynth, series_dir, 1, "b1", trim_silence=False
    )
    assert len(resynth.calls) == 1
    new_audio = _beat_audio_bytes(series_dir, "b1")
    assert len(new_audio) == len(old_audio)
    assert new_audio != old_audio

    # 再跑整话（开缓存）：必须复用重配后的新音频，不能变回旧声音。
    again = FakeTTS()
    stats = await _run_stage5(series_dir, beats, again)
    assert again.calls == []
    assert stats.get("reused") == 1
    assert stats.get("synthesized") == 0
    assert _beat_audio_bytes(series_dir, "b1") == new_audio


# ---------------------------------------------------------------------------
# D. 缓存索引与清单仍然自洽
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_cache_index_and_manifest_stay_consistent(tmp_path: Path) -> None:
    """缓存索引里的时长与清单一致，缓存目录里只有一条音频 + 索引文件。"""
    _series_id, series_dir = _register(tmp_path)
    await _run_stage5(series_dir, [_beat("b1", "银行行长推门而入。")], FakeTTS())

    cache_dir = _audio_dir(series_dir) / "_cache"
    assert cache_dir.is_dir()
    index = json.loads((cache_dir / "index.json").read_text(encoding="utf-8"))
    assert len(index["entries"]) == 1
    entry = next(iter(index["entries"].values()))
    assert entry["duration_ms"] > 0

    manifest = json.loads(
        (_audio_dir(series_dir) / "audio_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest[0]["duration_ms"] == entry["duration_ms"]
