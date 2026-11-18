"""Patch v4.3：MiniMax RPM 限流防护（provider 节流 + Stage 5 补齐 + 静音占位）。

背景：MiniMax ``t2a_v2`` 对单账号有每分钟请求数上限，超限返回
``base_resp.status_code=1002 rate limit exceeded(RPM)``。Stage 5 原来用 6 路
并发无节流地连打 81 条，导致 36 条 beat 没有音频 → 成片直接少句。
"""

from __future__ import annotations

import asyncio

import io
import json
import math
import struct
import wave
from typing import Any, Sequence

import httpx
import pytest
import respx

from core.config import ApiConfig, AppConfig, TTSConfig, create_tts_client
from core.schemas import EpisodeDigest, NarrationScript, ScriptBeat
from providers.tts_base import TTSError, TTSResult
from providers.tts_minimax import MiniMaxTTS, _RateLimiter
from stages import s5_tts

SAMPLE_RATE = 24000
CHAT_TTS_URL = "https://api.minimaxi.com/v1/t2a_v2"


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------
def _wav_bytes(seconds: float = 0.3, rate: int = SAMPLE_RATE) -> bytes:
    count = int(rate * seconds)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(0.3 * 32767 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(count)
            )
        )
    return buffer.getvalue()


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
        hook_title="限流测试",
        beats=list(beats),
        episode_digest=EpisodeDigest(
            episode_no=1,
            one_line_hook="测试",
            summary="测试",
        ),
    )


class FlakyTTS:
    """前 ``fail_times`` 次调用失败，之后成功（模拟限流恢复）。"""

    def __init__(self, *, fail_times: int = 1, fail_all: bool = False) -> None:
        self.calls: list[str] = []
        self._fail_times = fail_times
        self._fail_all = fail_all

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str | None = None,
        language_type: str = "Chinese",
    ) -> TTSResult:
        self.calls.append(text)
        if self._fail_all or len(self.calls) <= self._fail_times:
            raise TTSError("假失败：rate limit exceeded(RPM)")
        return TTSResult(
            audio=_wav_bytes(),
            content_type="audio/wav",
            voice=voice or "Fake",
        )


# ----------------------------------------------------------------------
# provider：限流闸门
# ----------------------------------------------------------------------
def test_rate_limiter_penalize_halves_rpm_and_sets_cooldown() -> None:
    limiter = _RateLimiter(20, min_rpm=2, cooldown_s=30.0)
    assert limiter.rpm == 20

    limiter.penalize()
    assert limiter.rpm == 10
    assert limiter.cooldown_remaining > 0

    limiter.penalize()
    assert limiter.rpm == 5

    # 连续降速有下限，不会归零
    for _ in range(10):
        limiter.penalize()
    assert limiter.rpm == 2


def test_rate_limiter_recovers_after_penalty() -> None:
    """被降速后，连续成功 10 次逐步加性恢复到初始速率（但不超过它）。"""
    limiter = _RateLimiter(10, min_rpm=2, cooldown_s=0.0)
    limiter.penalize()
    assert limiter.rpm == 5
    for _ in range(9):
        limiter.on_success()
    assert limiter.rpm == 5  # 不足 10 次不恢复
    limiter.on_success()
    assert limiter.rpm == 6
    for _ in range(40):
        limiter.on_success()
    assert limiter.rpm == 10  # 最多恢复到初始速率，不再往上加


@pytest.mark.anyio
async def test_rate_limiter_disabled_rpm_never_sleeps() -> None:
    limiter = _RateLimiter(0)
    await limiter.acquire()
    await limiter.acquire()
    assert limiter.rpm == 0


@pytest.mark.anyio
async def test_minimax_rate_limited_code_triggers_penalty_and_longer_backoff() -> None:
    """1002 必须触发全局降速，并把退避拉长（否则整条 beat 直接判死）。"""
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    with respx.mock:
        route = respx.post(CHAT_TTS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "base_resp": {"status_code": 1002, "status_msg": "rate limit exceeded(RPM)"},
                    "data": {},
                },
            )
        )
        client = MiniMaxTTS(
            api_key="test-key",
            max_retries=2,
            retry_backoff_s=1.0,
            rate_limit_rpm=600,
            rate_limit_cooldown_s=5.0,
            sleep=fake_sleep,
        )
        with pytest.raises(TTSError) as excinfo:
            await client.synthesize("测试限流。", voice="Chinese (Mandarin)_Radio_Host")

    assert "1002" in str(excinfo.value)
    assert route.call_count == 3  # 1 次首发 + 2 次重试
    # 每撞一次限流速率减半：600 -> 300 -> 150 -> 75
    assert client._limiter.rpm == 75
    # 退避被拉长：至少有一次 >= 冷却时间
    assert any(delay >= 5.0 for delay in delays)


@pytest.mark.anyio
async def test_minimax_success_resets_rate_limiter_streak() -> None:
    """成功请求走 on_success，连续成功 10 次后速率 +1。"""
    with respx.mock:
        respx.post(CHAT_TTS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                    "data": {"audio": "00", "status": 2, "ced": ""},
                    "extra_info": {"audio_sample_rate": 32000},
                },
            )
        )
        client = MiniMaxTTS(api_key="test-key", rate_limit_rpm=600)
        for _ in range(10):
            result = await client.synthesize("测试。", voice="Chinese (Mandarin)_Radio_Host")
            assert result.audio == b"\x00"
    # 未撞限流时速率保持在配置上限，不会自己往上加
    assert client._limiter.rpm == 600


# ----------------------------------------------------------------------
# Stage 5：补齐 + 静音占位
# ----------------------------------------------------------------------
@pytest.mark.anyio
async def test_stage5_retries_failed_beats_serially(tmp_path) -> None:
    """限流失败的 beat 必须被补齐回来，manifest 不能少句。"""
    audio_dir = tmp_path / "05_audio"
    script = _script(
        [_beat("b1", "第一句话。"), _beat("b2", "第二句话。"), _beat("b3", "第三句话。")]
    )
    client = FlakyTTS(fail_times=2)

    metas = await s5_tts.synthesize_episode(
        client,
        script=script,
        audio_dir=audio_dir,
        concurrency=3,
        retry_rounds=1,
        trim_silence=False,
    )

    assert [meta.beat_id for meta in metas] == ["b1", "b2", "b3"]
    assert len(client.calls) == 5  # 首轮 3 次 + 补齐 2 次（失败的 2 条）
    # 补齐成功后，首轮的失败记录必须从 audio_issues.json 清除（不留假警报）；
    # 全部救回时连文件都不再生成（无问题即无诊断文件）。
    issues_path = audio_dir / "audio_issues.json"
    issues = (
        json.loads(issues_path.read_text(encoding="utf-8")) if issues_path.is_file() else []
    )
    assert all("假失败" not in item["message"] for item in issues)
    assert {item["beat_id"] for item in issues}.isdisjoint({"b1", "b2"})


@pytest.mark.anyio
async def test_stage5_uses_silence_placeholder_when_retry_also_fails(tmp_path) -> None:
    """补齐仍失败时用等长静音占位，保住时间轴与字幕不错位。"""
    audio_dir = tmp_path / "05_audio"
    script = _script([_beat("b1", "唯一一句话。"), _beat("b2", "第二句话。")])
    client = FlakyTTS(fail_all=True)

    metas = await s5_tts.synthesize_episode(
        client,
        script=script,
        audio_dir=audio_dir,
        concurrency=2,
        retry_rounds=1,
        placeholder_on_failure=True,
        trim_silence=False,
    )

    assert [meta.beat_id for meta in metas] == ["b1", "b2"]
    for meta in metas:
        path = audio_dir / meta.audio_path
        assert path.is_file()
        with wave.open(str(path), "rb") as handle:
            assert handle.getnchannels() == 1
            assert handle.getframerate() == 32000
    issues = json.loads((audio_dir / "audio_issues.json").read_text(encoding="utf-8"))
    assert any("静音占位" in item["message"] for item in issues)


@pytest.mark.anyio
async def test_stage5_without_retry_round_skips_retry_but_keeps_placeholder(tmp_path) -> None:
    """retry_rounds=0 时不再重试，但仍写静音占位（时间轴不错位，问题留在 issues）。"""
    audio_dir = tmp_path / "05_audio"
    script = _script([_beat("b1", "唯一一句话。")])
    client = FlakyTTS(fail_all=True)

    metas = await s5_tts.synthesize_episode(
        client,
        script=script,
        audio_dir=audio_dir,
        concurrency=1,
        retry_rounds=0,
        placeholder_on_failure=True,
        trim_silence=False,
    )

    assert len(client.calls) == 1  # 没有补齐轮
    assert [meta.beat_id for meta in metas] == ["b1"]
    issues = json.loads((audio_dir / "audio_issues.json").read_text(encoding="utf-8"))
    assert any("静音占位" in item["message"] for item in issues)


@pytest.mark.anyio
async def test_stage5_placeholder_is_opt_in(tmp_path) -> None:
    """默认不开占位：失败句不进音频清单（与既有契约一致）。"""
    audio_dir = tmp_path / "05_audio"
    script = _script([_beat("b1", "唯一一句话。"), _beat("b2", "第二句话。")])
    client = FlakyTTS(fail_all=True)

    with pytest.raises(s5_tts.TTSStageError):
        await s5_tts.synthesize_episode(
            client,
            script=script,
            audio_dir=audio_dir,
            concurrency=2,
            retry_rounds=1,
            trim_silence=False,
        )


@pytest.mark.anyio
async def test_minimax_serialises_concurrent_requests() -> None:
    """同一 client 的并发调用必须被闸门压成串行（MiniMax 对并发极敏感）。"""
    live = 0
    max_live = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal live, max_live
        live += 1
        max_live = max(max_live, live)
        await asyncio.sleep(0.02)
        live -= 1
        return httpx.Response(
            200,
            json={
                "base_resp": {"status_code": 0, "status_msg": "success"},
                "data": {"audio": "00", "status": 2, "ced": ""},
                "extra_info": {"audio_sample_rate": 32000},
            },
        )

    with respx.mock:
        respx.post(CHAT_TTS_URL).mock(side_effect=handler)
        client = MiniMaxTTS(api_key="test-key", rate_limit_rpm=0, max_concurrency=1)
        await asyncio.gather(
            *(client.synthesize("测试。", voice="Chinese (Mandarin)_Radio_Host") for _ in range(4))
        )

    assert max_live == 1


class _SerialMarkerTTS:
    """带 ``serial_requests`` 标记的假 provider（模拟 MiniMaxTTS 的串行契约）。"""

    serial_requests = True

    def __init__(self) -> None:
        self.live = 0
        self.max_live = 0
        self.ordered: list[str] = []

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str | None = None,
        language_type: str = "Chinese",
    ) -> TTSResult:
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        await asyncio.sleep(0.01)
        self.live -= 1
        self.ordered.append(text)
        return TTSResult(audio=_wav_bytes(), content_type="audio/wav", voice=voice or "Fake")


@pytest.mark.anyio
async def test_stage5_serializes_when_provider_requests_it(tmp_path) -> None:
    """provider 声明 serial_requests 时，即使上层给了高并发也必须严格串行。"""
    audio_dir = tmp_path / "05_audio"
    beats = [_beat(f"b{i}", f"第{i}句话。") for i in range(1, 7)]
    client = _SerialMarkerTTS()

    metas = await s5_tts.synthesize_episode(
        client,
        script=_script(beats),
        audio_dir=audio_dir,
        concurrency=6,  # 上层给 6 路并发，但串行标记应覆盖它
        retry_rounds=0,
        trim_silence=False,
    )

    assert client.max_live == 1  # 任何时刻只有一条请求在途
    assert [meta.beat_id for meta in metas] == [f"b{i}" for i in range(1, 7)]
    assert client.ordered == [f"第{i}句话。" for i in range(1, 7)]  # 严格按剧本顺序


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
def test_config_defaults_expose_rate_limit_and_retry() -> None:
    assert ApiConfig().minimax_rate_limit_rpm == 20
    assert TTSConfig().retry_failed_rounds == 1
    assert TTSConfig().silence_placeholder_on_failure is False


def test_create_tts_client_wires_rate_limit_into_minimax() -> None:
    config = AppConfig()
    config.api.minimax_rate_limit_rpm = 15
    config.models.tts = "speech-2.8-hd"
    client = create_tts_client(config)
    assert isinstance(client, MiniMaxTTS)
    assert client._limiter.rpm == 15
