"""M4-T1：百炼 qwen3-tts-flash 合成器与 edge-tts 兜底的离线测试。

全部用 respx 拦截 HTTP，不打真实网络。
"""

from __future__ import annotations

import base64
import io
import wave

import httpx
import pytest
import respx

from core.config import ConfigError
from providers.tts_base import TTSError, TTSRequestError, TTSResult
from providers.tts_edge import EdgeTTS, edge_tts_available
from providers.tts_qwen import DEFAULT_VOICE, QwenTTS

TTS_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
AUDIO_URL = "https://oss.example.com/audio/result.wav"
API_KEY = "sk-test-key"


def _wav_bytes(seconds: float = 0.2, rate: int = 24000) -> bytes:
    """造一段真实可解析的 wav。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


def _audio_response(url: str = AUDIO_URL) -> dict:
    return {
        "output": {
            "audio": {
                "id": "audio-1",
                "url": url,
                "expires_at": 1893456000,
            }
        },
        "request_id": "req-1",
    }


@pytest.fixture()
def api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", API_KEY)


@respx.mock
def test_synthesize_returns_audio_bytes(api_key_env: None) -> None:
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=_audio_response()))
    respx.get(AUDIO_URL).mock(return_value=httpx.Response(200, content=_wav_bytes(), headers={"content-type": "audio/x-wav"}))

    async def run() -> TTSResult:
        async with QwenTTS() as client:
            return await client.synthesize("你好，世界", instruction="语速稍快")

    import asyncio

    result = asyncio.run(run())
    assert result.audio[:4] == b"RIFF"
    assert result.content_type == "audio/x-wav"
    assert result.suffix == ".wav"


@respx.mock
def test_request_carries_auth_and_no_referer(api_key_env: None) -> None:
    route = respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=_audio_response()))
    respx.get(AUDIO_URL).mock(return_value=httpx.Response(200, content=_wav_bytes()))

    import asyncio

    async def run() -> None:
        async with QwenTTS() as client:
            await client.synthesize("测试")

    asyncio.run(run())
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    # 百炼不是 webtoons.com，不该带 Referer
    assert "referer" not in request.headers


@respx.mock
def test_instruction_is_sent_in_both_slots(api_key_env: None) -> None:
    route = respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=_audio_response()))
    respx.get(AUDIO_URL).mock(return_value=httpx.Response(200, content=_wav_bytes()))

    import asyncio

    async def run() -> None:
        async with QwenTTS() as client:
            await client.synthesize("台词", instruction="气息微促地低声说", voice="Ethan")

    asyncio.run(run())
    payload = route.calls.last.request.content.decode()
    assert '"instruction":"气息微促地低声说"' in payload
    assert '"voice":"Ethan"' in payload


@respx.mock
def test_empty_text_is_rejected_before_request(api_key_env: None) -> None:
    route = respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=_audio_response()))

    import asyncio

    async def run() -> None:
        async with QwenTTS() as client:
            await client.synthesize("   ")

    with pytest.raises(TTSRequestError):
        asyncio.run(run())
    assert not route.called


@respx.mock
def test_retryable_status_is_retried(api_key_env: None) -> None:
    route = respx.post(TTS_URL).mock(
        side_effect=[
            httpx.Response(500, text="server boom"),
            httpx.Response(200, json=_audio_response()),
        ]
    )
    respx.get(AUDIO_URL).mock(return_value=httpx.Response(200, content=_wav_bytes()))

    import asyncio

    async def run() -> TTSResult:
        async with QwenTTS(retry_backoff_s=0) as client:
            return await client.synthesize("重试")

    result = asyncio.run(run())
    assert result.audio
    assert route.call_count == 2


@respx.mock
def test_client_error_is_not_retried(api_key_env: None) -> None:
    route = respx.post(TTS_URL).mock(return_value=httpx.Response(400, json={"code": "InvalidParameter"}))
    respx.get(AUDIO_URL).mock(return_value=httpx.Response(200, content=_wav_bytes()))

    import asyncio

    async def run() -> None:
        async with QwenTTS(retry_backoff_s=0) as client:
            await client.synthesize("坏请求")

    with pytest.raises(TTSRequestError):
        asyncio.run(run())
    assert route.call_count == 1


@respx.mock
def test_retries_exhausted_raises_tts_error(api_key_env: None) -> None:
    respx.post(TTS_URL).mock(return_value=httpx.Response(503, text="unavailable"))

    import asyncio

    async def run() -> None:
        async with QwenTTS(max_retries=2, retry_backoff_s=0) as client:
            await client.synthesize("一直失败")

    with pytest.raises(TTSError):
        asyncio.run(run())


@respx.mock
def test_timeout_is_retried(api_key_env: None) -> None:
    route = respx.post(TTS_URL).mock(
        side_effect=[
            httpx.TimeoutException("timeout"),
            httpx.Response(200, json=_audio_response()),
        ]
    )
    respx.get(AUDIO_URL).mock(return_value=httpx.Response(200, content=_wav_bytes()))

    import asyncio

    async def run() -> TTSResult:
        async with QwenTTS(retry_backoff_s=0) as client:
            return await client.synthesize("超时")

    assert asyncio.run(run()).audio
    assert route.call_count == 2


@respx.mock
def test_response_without_audio_raises(api_key_env: None) -> None:
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json={"output": {"audio": {}}}))

    import asyncio

    async def run() -> None:
        async with QwenTTS() as client:
            await client.synthesize("没有音频")

    with pytest.raises(TTSError):
        asyncio.run(run())


@respx.mock
def test_inline_base64_audio_is_decoded(api_key_env: None) -> None:
    inline = base64.b64encode(_wav_bytes()).decode()
    respx.post(TTS_URL).mock(
        return_value=httpx.Response(200, json={"output": {"audio": {"data": inline}}})
    )

    import asyncio

    async def run() -> TTSResult:
        async with QwenTTS() as client:
            return await client.synthesize("内联音频")

    result = asyncio.run(run())
    assert result.audio[:4] == b"RIFF"


@respx.mock
def test_missing_api_key_raises_config_error() -> None:
    import asyncio

    async def run() -> None:
        async with QwenTTS(env={"PATH": "/usr/bin"}, env_file="/nonexistent/.env") as client:
            await client.synthesize("没 key")

    with pytest.raises((TTSError, ConfigError)):
        asyncio.run(run())


def test_edge_tts_reports_missing_dependency() -> None:
    import asyncio

    async def run() -> None:
        await EdgeTTS().synthesize("兜底")

    if edge_tts_available():
        pytest.skip("本机已安装 edge-tts，跳过缺失依赖分支")
    with pytest.raises(TTSError):
        asyncio.run(run())


def test_edge_tts_result_suffix_and_defaults() -> None:
    # Patch v3.1 Patch 11 A1：百炼默认音色改成男声「墨讲师」Elias。
    assert DEFAULT_VOICE == "Elias"
    assert TTSResult(audio=b"x").suffix == ".wav"
    assert TTSResult(audio=b"x", content_type="audio/mpeg").suffix == ".mp3"


def _placeholder_wav(seconds: float = 0.5, rate: int = 24000) -> bytes:
    """造一段百炼式的 WAV：长度字段写成 0x7FFFFFFF 占位。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    raw = bytearray(buffer.getvalue())
    # data chunk 在偏移 36（12 + 8(fmt) + 16）
    raw[40:44] = (0x7FFFFFFF).to_bytes(4, "little")
    raw[4:8] = (0x7FFFFFFF).to_bytes(4, "little")
    return bytes(raw)


def test_placeholder_wav_length_is_broken_before_fix() -> None:
    """先证明百炼式的占位长度确实会让 wave 读出错误的帧数。"""
    raw = _placeholder_wav()
    with wave.open(io.BytesIO(raw), "rb") as handle:
        assert handle.getnframes() > 10_000_000  # 明显不是 0.5 秒该有的帧数


def test_fix_wav_size_repairs_placeholder_lengths() -> None:
    from providers.tts_qwen import _fix_wav_size

    raw = _placeholder_wav(seconds=0.5)
    fixed = _fix_wav_size(raw)
    with wave.open(io.BytesIO(fixed), "rb") as handle:
        assert handle.getnframes() == 12000  # 0.5s * 24000Hz
        assert handle.getframerate() == 24000


def test_fix_wav_size_keeps_non_wav_untouched() -> None:
    from providers.tts_qwen import _fix_wav_size

    assert _fix_wav_size(b"not a wav") == b"not a wav"
