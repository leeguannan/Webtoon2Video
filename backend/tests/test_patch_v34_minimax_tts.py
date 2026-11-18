"""Patch v3.4 Patch 16：MiniMax TTS（speech-2.8-*）接入的离线测试。

四条验收线（对应 ``PATCH_v3.4_MINIMAX_SPEECH_2_8.md`` 的 Patch 16 验收段）：

1. **接入与密钥**：``MINIMAX_API_KEY`` / ``MINIMAX_GROUP_ID`` 走进程环境变量 →
   ``.env`` 的顺序读取；``t2a_v2`` 请求体/端点/错误码判定与规格逐条对齐。
2. **情绪安全**：情绪指令映射成 MiniMax 安全枚举（严禁 ``whisper``），未命中
   就不传 ``emotion`` 字段。
3. **跨模型音色兜底**：百炼/Edge 音色（``Cherry`` 等）发给 MiniMax 会直接
   ``2013 invalid voice_id``，必须被自动换成同定位的 MiniMax 音色；自定义克隆
   音色原样透传。
4. **分供应商试听缓存**：样例按 ``voice_samples/{provider}/`` 隔离，Web 端按
   供应商路由读取。

全部离线：HTTP 用 ``respx`` 打桩，不触网、不消耗 MiniMax 额度。
"""

from __future__ import annotations

import asyncio
import io
import json
import wave
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image

from core.config import (
    ApiConfig,
    AppConfig,
    ConfigError,
    TTSConfig,
    WorkspaceConfig,
    create_tts_client,
    resolve_minimax_api_key_with_env_file,
    resolve_minimax_group_id,
    resolve_voice_for_tts_model,
)
from core.schemas import (
    CameraMotion,
    EpisodeDigest,
    NarrationScript,
    ScriptBeat,
)
from providers.tts_base import TTSError, TTSRequestError, TTSResult
from providers.tts_minimax import (
    DEFAULT_VOICE,
    MINIMAX_FEMALE_VOICE_IDS,
    MINIMAX_MALE_VOICE_IDS,
    MINIMAX_NARRATOR_VOICE_IDS,
    MINIMAX_VOICE_IDS,
    MINIMAX_VOICES,
    MiniMaxTTS,
    _map_emotion,
    fetch_minimax_voices,
    voice_sample_filename,
)
from providers.tts_qwen import QwenTTS
from scripts import init_voice_samples
from stages import s5_tts
from web import server as web_server
from web.server import create_app

#: MiniMax 官方端点（Patch 16 规格固定）。
T2A_URL = "https://api.minimaxi.com/v1/t2a_v2"
T2A_URL_WITH_GROUP = f"{T2A_URL}?GroupId=grp-123"
GET_VOICE_URL = "https://api.minimaxi.com/v1/get_voice"

#: MiniMax 默认旁白音色。
NARRATOR_VOICE = "Chinese (Mandarin)_Radio_Host"


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------
def _wav_bytes(seconds: float = 0.2, rate: int = 32000) -> bytes:
    """造一段真实可解析的 wav。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


def asyncio_run(coro: Any) -> Any:
    """跑一个协程（仓库没装 pytest-asyncio，异步用例统一走这里）。"""
    return asyncio.run(coro)


def _ok_response(audio: bytes | None = None) -> httpx.Response:
    """MiniMax 成功响应：**真实**结构是嵌套的 ``data.data.audio``。

    实测（``POST /v1/t2a_v2``，2026-11）::

        {"data": {"audio": "<hex>", "status": 2, "ced": ""},
         "extra_info": {"audio_sample_rate": 32000, ...},
         "trace_id": "...", "base_resp": {"status_code": 0, "status_msg": "success"}}
    """
    payload = audio if audio is not None else _wav_bytes()
    return httpx.Response(
        200,
        json={
            "data": {"audio": payload.hex(), "status": 2, "ced": ""},
            "extra_info": {
                "audio_length": 200,
                "audio_sample_rate": 32000,
                "audio_size": len(payload),
                "audio_format": "wav",
                "audio_channel": 1,
            },
            "trace_id": "test-trace",
            "base_resp": {"status_code": 0, "status_msg": "success"},
        },
    )


def _ok_response_flat(audio: bytes | None = None) -> httpx.Response:
    """旧文档描述的扁平结构（顶层 ``audio``）——仍需兼容。"""
    payload = audio if audio is not None else _wav_bytes()
    return httpx.Response(
        200,
        json={
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "audio": payload.hex(),
            "extra_info": {},
        },
    )


def _error_response(code: int, message: str = "boom") -> httpx.Response:
    """MiniMax 业务错误：HTTP 200 但 ``base_resp.status_code != 0``。"""
    return httpx.Response(
        200,
        json={"base_resp": {"status_code": code, "status_msg": message}},
    )


def _client(**kwargs: Any) -> MiniMaxTTS:
    """构造一个不睡退避的 MiniMaxTTS（测试里不需要真等）。"""
    async def _no_sleep(_seconds: float) -> None:
        return None

    kwargs.setdefault("api_key", "sk-test")
    kwargs.setdefault("base_url", "https://api.minimaxi.com/v1")
    kwargs.setdefault("sleep", _no_sleep)
    return MiniMaxTTS(**kwargs)


def _beat(beat_id: str, role: str = "narrator") -> ScriptBeat:
    return ScriptBeat(
        beat_id=beat_id,
        narration_text=f"第{beat_id}句解说",
        tts_text=f"第{beat_id}句解说",
        speaker_role=role,
        target_panel_ids=["p_001"],
        motion=CameraMotion.ZOOM_IN,
    )


def _script(beats: list[ScriptBeat]) -> NarrationScript:
    return NarrationScript(
        episode_id="ep_001",
        hook_title="钩子",
        beats=beats,
        episode_digest=EpisodeDigest(
            episode_no=1,
            one_line_hook="一句话钩子",
            summary="摘要",
        ),
    )


class _RecordingTTS:
    """记录每次 ``synthesize`` 的参数（支持原生语速）。"""

    supports_native_speed = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str | None = None,
        language_type: str = "Chinese",
        speed: float = 1.0,
    ) -> TTSResult:
        self.calls.append(
            {
                "text": text,
                "instruction": instruction,
                "voice": voice,
                "language_type": language_type,
                "speed": speed,
            }
        )
        return TTSResult(
            audio=_wav_bytes(),
            content_type="audio/wav",
            sample_rate=32000,
            voice=voice or "",
        )


# --------------------------------------------------------------------------
# 1. 配置与 .env 密钥
# --------------------------------------------------------------------------
def test_api_config_exposes_minimax_defaults() -> None:
    """ApiConfig 新增 MiniMax 字段（Patch 16 A1）。"""
    api = ApiConfig()
    assert api.minimax_base_url == "https://api.minimaxi.com/v1"
    assert api.minimax_api_key_env == "MINIMAX_API_KEY"
    assert api.minimax_group_id_env == "MINIMAX_GROUP_ID"
    assert api.minimax_group_id == ""


def test_env_example_declares_minimax_keys() -> None:
    """``.env.example`` 写明两个 MiniMax 环境变量。"""
    root = Path(__file__).resolve().parents[2]
    text = (root / ".env.example").read_text(encoding="utf-8")
    assert "MINIMAX_API_KEY=" in text
    assert "MINIMAX_GROUP_ID=" in text


def test_resolve_minimax_api_key_prefers_process_env(tmp_path: Path) -> None:
    """进程环境变量优先于 ``.env``。"""
    env_file = tmp_path / ".env"
    env_file.write_text("MINIMAX_API_KEY=sk-from-file\n", encoding="utf-8")
    assert (
        resolve_minimax_api_key_with_env_file(
            env={"MINIMAX_API_KEY": "sk-from-env"}, env_file=str(env_file)
        )
        == "sk-from-env"
    )


def test_resolve_minimax_api_key_falls_back_to_env_file(tmp_path: Path) -> None:
    """进程环境变量缺失时读 ``.env``。"""
    env_file = tmp_path / ".env"
    env_file.write_text("MINIMAX_API_KEY=sk-from-file\n", encoding="utf-8")
    assert (
        resolve_minimax_api_key_with_env_file(env={}, env_file=str(env_file))
        == "sk-from-file"
    )


def test_resolve_minimax_api_key_missing_raises_config_error(tmp_path: Path) -> None:
    """两个来源都没有密钥时抛 ``ConfigError``（不是静默继续）。"""
    with pytest.raises(ConfigError):
        resolve_minimax_api_key_with_env_file(env={}, env_file=str(tmp_path / "missing.env"))


def test_resolve_minimax_group_id_reads_env_file(tmp_path: Path) -> None:
    """GroupId 走同一套「进程环境变量 → .env」顺序。"""
    env_file = tmp_path / ".env"
    env_file.write_text("MINIMAX_GROUP_ID=grp-file\n", encoding="utf-8")
    assert resolve_minimax_group_id(env={}, env_file=str(env_file)) == "grp-file"
    assert (
        resolve_minimax_group_id(
            env={"MINIMAX_GROUP_ID": "grp-env"}, env_file=str(env_file)
        )
        == "grp-env"
    )


def test_resolve_minimax_group_id_missing_returns_empty(tmp_path: Path) -> None:
    """GroupId 是可选的：缺失时返回空串（端点不带 ``?GroupId=``）。"""
    assert (
        resolve_minimax_group_id(env={}, env_file=str(tmp_path / "missing.env")) == ""
    )


# --------------------------------------------------------------------------
# 2. MiniMaxTTS 协议
# --------------------------------------------------------------------------
def test_minimax_voice_catalog_shape() -> None:
    """音色表 28 条，字段齐全（id/name/gender/style/recommended_for）。"""
    assert len(MINIMAX_VOICES) == 28
    assert len(MINIMAX_VOICE_IDS) == 28
    assert len(MINIMAX_NARRATOR_VOICE_IDS) == 5
    assert len(MINIMAX_MALE_VOICE_IDS) == 11
    assert len(MINIMAX_FEMALE_VOICE_IDS) == 12
    for item in MINIMAX_VOICES:
        assert {"id", "name", "gender", "style", "recommended_for"} <= set(item)
    assert DEFAULT_VOICE == NARRATOR_VOICE


def test_minimax_voice_catalog_excludes_ids_absent_from_minimax() -> None:
    """预置音色表不得含 MiniMax 不存在的 ID（曾踩 ``2054 voice id not exist``）。

    这几个 ID 在 MiniMax ``POST /v1/get_voice`` 返回的 ``system_voice`` 里查无此音，
    一旦写进清单就会让整话合成失败。
    """
    unknown_ids = {
        "Chinese (Mandarin)_Serene_Narrator",
        "Chinese (Mandarin)_Refreshing_Young_Man",
        "Chinese (Mandarin)_IntellectualGirl",
    }
    assert unknown_ids.isdisjoint(set(MINIMAX_VOICE_IDS))
    assert DEFAULT_VOICE in set(MINIMAX_VOICE_IDS)

    narrator = set(MINIMAX_NARRATOR_VOICE_IDS)
    male = set(MINIMAX_MALE_VOICE_IDS)
    female = set(MINIMAX_FEMALE_VOICE_IDS)
    assert narrator.isdisjoint(male) and narrator.isdisjoint(female)
    assert male.isdisjoint(female)
    assert narrator | male | female == set(MINIMAX_VOICE_IDS)


def test_minimax_voice_sample_filename_is_path_safe() -> None:
    """带括号的音色 ID 映射成安全文件名（路径穿越被清洗）。"""
    assert (
        voice_sample_filename("Chinese (Mandarin)_Radio_Host")
        == "Chinese__Mandarin__Radio_Host.wav"
    )
    assert voice_sample_filename("") == "unknown.wav"


@respx.mock
def test_minimax_tts_decodes_hex_audio() -> None:
    """成功响应：hex 音频解码成字节，返回 ``audio/wav``。"""
    route = respx.post(T2A_URL).mock(return_value=_ok_response())
    client = _client()
    result = asyncio_run(client.synthesize("你好", voice=NARRATOR_VOICE))

    assert result.audio == _wav_bytes()
    assert result.content_type == "audio/wav"
    assert result.voice == NARRATOR_VOICE
    assert route.call_count == 1


@respx.mock
def test_minimax_tts_reads_audio_from_nested_data_payload() -> None:
    """真实响应结构：音频在 ``data.data.audio``（不是顶层 ``audio``）。

    早期实现只读顶层 ``audio``，对着真实 API 会报
    ``响应里没有音频``，合成整话失败。
    """
    respx.post(T2A_URL).mock(return_value=_ok_response())
    client = _client()
    result = asyncio_run(client.synthesize("你好", voice=NARRATOR_VOICE))

    assert result.audio == _wav_bytes()
    assert result.content_type == "audio/wav"


@respx.mock
def test_minimax_tts_still_reads_flat_top_level_audio() -> None:
    """兼容旧文档描述的扁平结构（顶层 ``audio``）。"""
    respx.post(T2A_URL).mock(return_value=_ok_response_flat())
    client = _client()
    result = asyncio_run(client.synthesize("你好", voice=NARRATOR_VOICE))

    assert result.audio == _wav_bytes()


@respx.mock
def test_minimax_tts_reports_real_sample_rate() -> None:
    """采样率取 ``extra_info.audio_sample_rate``（真实值 32000）。"""
    respx.post(T2A_URL).mock(return_value=_ok_response())
    client = _client()
    result = asyncio_run(client.synthesize("你好", voice=NARRATOR_VOICE))

    assert result.sample_rate == 32000


@respx.mock
def test_minimax_tts_payload_matches_spec() -> None:
    """请求体逐字段对齐规格（含 voice_setting / audio_setting）。"""
    route = respx.post(T2A_URL).mock(return_value=_ok_response())
    client = _client(model="speech-2.8-turbo")
    asyncio_run(client.synthesize("深夜的街道", voice="male-qn-jingying", speed=1.18))

    body = json.loads(route.calls[0].request.content.decode("utf-8"))
    assert body["model"] == "speech-2.8-turbo"
    assert body["text"] == "深夜的街道"
    assert body["stream"] is False
    assert body["language_boost"] == "Chinese"
    assert body["output_format"] == "hex"
    assert body["voice_setting"]["voice_id"] == "male-qn-jingying"
    assert body["voice_setting"]["speed"] == 1.18
    assert body["voice_setting"]["vol"] == 1.0
    assert body["voice_setting"]["pitch"] == 0
    assert body["audio_setting"] == {
        "sample_rate": 32000,
        "bitrate": 128000,
        "format": "wav",
        "channel": 1,
    }
    headers = route.calls[0].request.headers
    assert headers["Authorization"] == "Bearer sk-test"
    assert headers["Content-Type"] == "application/json"


@respx.mock
def test_minimax_tts_clamps_speed_into_supported_range() -> None:
    """语速裁剪到 ``[0.5, 2.0]``。"""
    route = respx.post(T2A_URL).mock(return_value=_ok_response())
    client = _client()
    asyncio_run(client.synthesize("一", speed=5.0))
    asyncio_run(client.synthesize("二", speed=0.1))

    bodies = [json.loads(call.request.content.decode("utf-8")) for call in route.calls]
    assert bodies[0]["voice_setting"]["speed"] == 2.0
    assert bodies[1]["voice_setting"]["speed"] == 0.5


@respx.mock
def test_minimax_tts_appends_group_id_when_configured() -> None:
    """配置了 GroupId 才在端点追加 ``?GroupId=``。"""
    route = respx.post(T2A_URL_WITH_GROUP).mock(return_value=_ok_response())
    client = _client(group_id="grp-123")
    asyncio_run(client.synthesize("你好"))

    assert route.call_count == 1
    assert "GroupId=grp-123" in str(route.calls[0].request.url)


@respx.mock
def test_minimax_tts_without_group_id_keeps_plain_endpoint() -> None:
    """没有 GroupId 时端点就是 ``/t2a_v2``。"""
    route = respx.post(T2A_URL).mock(return_value=_ok_response())
    client = _client()
    asyncio_run(client.synthesize("你好"))

    assert "GroupId" not in str(route.calls[0].request.url)


# --------------------------------------------------------------------------
# 3. 情绪映射（安全枚举）
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        ("愤怒地吼叫", "angry"),
        ("悲伤地哭泣", "sad"),
        ("惊恐地发抖", "fearful"),
        ("震惊地瞪大眼睛", "surprised"),
        ("开心地笑", "happy"),
        ("嫌弃地冷哼", "disgusted"),
        ("沉稳地低语", "calm"),
    ],
)
def test_map_emotion_uses_safe_enums(instruction: str, expected: str) -> None:
    """情绪指令映射成 MiniMax 安全枚举。"""
    assert _map_emotion(instruction) == expected


def test_map_emotion_never_returns_whisper() -> None:
    """严禁 ``whisper``（会让音频发闷失真）。"""
    for instruction in ("轻声耳语", "低声呢喃", "沙哑耳语"):
        emotion = _map_emotion(instruction)
        assert emotion != "whisper"
        assert emotion in {
            "calm",
            "happy",
            "sad",
            "angry",
            "fearful",
            "surprised",
            "disgusted",
            None,
        }


def test_map_emotion_returns_none_when_unmatched() -> None:
    """未命中就返回 ``None``（调用方据此不传 emotion 字段）。"""
    assert _map_emotion("用自然流畅的语调讲解这段剧情") is None
    assert _map_emotion("") is None


@respx.mock
def test_minimax_tts_omits_emotion_field_when_unmatched() -> None:
    """未命中情绪时请求体里没有 ``emotion`` 字段。"""
    route = respx.post(T2A_URL).mock(return_value=_ok_response())
    client = _client()
    asyncio_run(client.synthesize("你好", instruction="用自然流畅的语调讲解这段剧情"))

    body = json.loads(route.calls[0].request.content.decode("utf-8"))
    assert "emotion" not in body


@respx.mock
def test_minimax_tts_sends_mapped_emotion() -> None:
    """命中情绪时把安全枚举写进请求体。"""
    route = respx.post(T2A_URL).mock(return_value=_ok_response())
    client = _client()
    asyncio_run(client.synthesize("你好", instruction="愤怒地吼叫"))

    body = json.loads(route.calls[0].request.content.decode("utf-8"))
    assert body["emotion"] == "angry"


# --------------------------------------------------------------------------
# 4. 错误码与重试
# --------------------------------------------------------------------------
@respx.mock
def test_minimax_tts_fatal_status_code_is_not_retried() -> None:
    """``2013``（voice_id 非法）等致命错误码直接报错，不重试。"""
    route = respx.post(T2A_URL).mock(return_value=_error_response(2013, "invalid voice_id"))
    client = _client()

    with pytest.raises(TTSRequestError) as excinfo:
        asyncio_run(client.synthesize("你好", voice="Cherry"))

    assert "2013" in str(excinfo.value)
    assert route.call_count == 1


@respx.mock
def test_minimax_tts_reports_unknown_voice_id() -> None:
    """``2054``（voice_id 不存在）文案点明是音色问题，且不重试。"""
    route = respx.post(T2A_URL).mock(return_value=_error_response(2054, "voice id not exist"))
    client = _client()

    with pytest.raises(TTSRequestError) as excinfo:
        asyncio_run(client.synthesize("你好", voice="Chinese (Mandarin)_Serene_Narrator"))

    message = str(excinfo.value)
    assert "2054" in message
    assert "不存在" in message
    assert route.call_count == 1


@respx.mock
def test_minimax_tts_reports_insufficient_balance() -> None:
    """``1008``（余额不足）文案点明余额，且不重试。"""
    route = respx.post(T2A_URL).mock(return_value=_error_response(1008, "insufficient balance"))
    client = _client()

    with pytest.raises(TTSRequestError) as excinfo:
        asyncio_run(client.synthesize("你好"))

    message = str(excinfo.value)
    assert "1008" in message
    assert "余额" in message
    assert route.call_count == 1


@respx.mock
def test_minimax_tts_retries_retryable_status_code() -> None:
    """可重试错误码（``1000`` 等）退避后重试并最终成功。"""
    route = respx.post(T2A_URL)
    route.side_effect = [
        _error_response(1000, "system busy"),
        _ok_response(),
    ]
    client = _client()
    result = asyncio_run(client.synthesize("你好"))

    assert result.audio == _wav_bytes()
    assert route.call_count == 2


@respx.mock
def test_minimax_tts_retries_retryable_http_status() -> None:
    """HTTP 429 / 500 走重试。"""
    route = respx.post(T2A_URL)
    route.side_effect = [
        httpx.Response(429, json={"message": "slow down"}),
        httpx.Response(500, text="oops"),
        _ok_response(),
    ]
    client = _client()
    result = asyncio_run(client.synthesize("你好"))

    assert result.audio == _wav_bytes()
    assert route.call_count == 3


@respx.mock
def test_minimax_tts_raises_after_retry_exhausted() -> None:
    """重试耗尽后抛 ``TTSError``。"""
    route = respx.post(T2A_URL).mock(return_value=_error_response(1002, "busy"))
    client = _client()

    with pytest.raises(TTSError):
        asyncio_run(client.synthesize("你好"))

    assert route.call_count == 4  # 首次 + 3 次重试


@respx.mock
def test_minimax_tts_non_retryable_http_error_raises_request_error() -> None:
    """HTTP 400 属请求错误，不重试。"""
    route = respx.post(T2A_URL).mock(return_value=httpx.Response(400, text="bad request"))
    client = _client()

    with pytest.raises(TTSRequestError):
        asyncio_run(client.synthesize("你好"))

    assert route.call_count == 1


# --------------------------------------------------------------------------
# 5. 账户音色拉取（克隆音色）
# --------------------------------------------------------------------------
@respx.mock
def test_fetch_minimax_voices_marks_cloned_voices() -> None:
    """拉取账户音色：克隆音色带 ``is_cloned=True`` / ``category="cloned"``。"""
    respx.post(GET_VOICE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "system_voice": [
                    {"voice_id": "Chinese (Mandarin)_Radio_Host", "voice_name": "电台男主播"},
                    {"voice_id": "voice-system-x", "voice_name": "系统音色 X"},
                ],
                "voice_cloning": [
                    {"voice_id": "my_cloned_voice_01", "voice_name": "我的克隆音色"},
                ],
                "voice_generation": [],
            },
        )
    )
    voices = asyncio_run(fetch_minimax_voices(api_key="sk-test"))

    by_id = {str(item["id"]): item for item in voices}
    assert by_id["my_cloned_voice_01"]["is_cloned"] is True
    assert by_id["my_cloned_voice_01"]["category"] == "cloned"
    assert by_id["voice-system-x"]["is_cloned"] is False


@respx.mock
def test_fetch_minimax_voices_reads_real_description_list() -> None:
    """真实 ``get_voice`` 响应的 ``description`` 是**字符串列表**，取第一条。"""
    respx.post(GET_VOICE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "system_voice": [
                    {
                        "voice_id": "voice-system-y",
                        "voice_name": "系统音色 Y",
                        "description": ["一位沉稳可靠的中年男性高管声音，标准普通话。"],
                        "created_time": "1970-01-01",
                    },
                ],
                "voice_cloning": [],
                "voice_generation": [],
            },
        )
    )
    voices = asyncio_run(fetch_minimax_voices(api_key="sk-test"))

    by_id = {str(item["id"]): item for item in voices}
    assert by_id["voice-system-y"]["style"] == "一位沉稳可靠的中年男性高管声音，标准普通话。"


@respx.mock
def test_fetch_minimax_voices_falls_back_to_static_catalog() -> None:
    """拉取失败时优雅降级回静态音色表。"""
    respx.post(GET_VOICE_URL).mock(return_value=httpx.Response(500, text="oops"))
    voices = asyncio_run(fetch_minimax_voices(api_key="sk-test"))

    assert len(voices) == len(MINIMAX_VOICES)


# --------------------------------------------------------------------------
# 6. 跨模型音色兜底 + 统一工厂
# --------------------------------------------------------------------------
def test_resolve_voice_for_tts_model_replaces_qwen_voice_for_minimax() -> None:
    """百炼音色发给 MiniMax 必须被替换（否则 2013 让整话失败）。"""
    assert (
        resolve_voice_for_tts_model("Cherry", tts_model="speech-2.8-hd", role_kind="narrator")
        == NARRATOR_VOICE
    )
    assert (
        resolve_voice_for_tts_model("Ethan", tts_model="speech-2.8-turbo", role_kind="male")
        == "male-qn-jingying"
    )
    assert (
        resolve_voice_for_tts_model("Katerina", tts_model="speech-2.8-hd", role_kind="female")
        == "female-yujie"
    )


def test_resolve_voice_for_tts_model_replaces_edge_voice_for_minimax() -> None:
    """Edge 音色（``zh-CN-*``）发给 MiniMax 同样被替换。"""
    assert (
        resolve_voice_for_tts_model(
            "zh-CN-XiaoxiaoNeural", tts_model="speech-2.8-hd", role_kind="narrator"
        )
        == NARRATOR_VOICE
    )


def test_resolve_voice_for_tts_model_keeps_custom_cloned_voice() -> None:
    """自定义克隆音色不在已知列表里，原样透传。"""
    assert (
        resolve_voice_for_tts_model(
            "my_cloned_voice_01", tts_model="speech-2.8-hd", role_kind="narrator"
        )
        == "my_cloned_voice_01"
    )


def test_resolve_voice_for_tts_model_keeps_qwen_voice_for_qwen_model() -> None:
    """百炼模型下百炼音色原样返回（不得被 MiniMax 音色污染）。"""
    assert (
        resolve_voice_for_tts_model("Cherry", tts_model="qwen3-tts-flash", role_kind="female")
        == "Cherry"
    )
    assert (
        resolve_voice_for_tts_model("Elias", tts_model="qwen3-tts-flash", role_kind="narrator")
        == "Elias"
    )


def test_resolve_voice_for_tts_model_maps_minimax_voice_back_for_qwen_model() -> None:
    """反向兜底：MiniMax 音色发给百炼模型时换成百炼音色。"""
    assert (
        resolve_voice_for_tts_model(
            NARRATOR_VOICE, tts_model="qwen3-tts-flash", role_kind="narrator"
        )
        == "Elias"
    )


def test_create_tts_client_routes_by_model() -> None:
    """统一工厂按模型名路由到对应 provider。"""
    config = AppConfig()
    assert isinstance(create_tts_client(config), QwenTTS)

    minimax_config = AppConfig(
        models=AppConfig().models.model_copy(update={"tts": "speech-2.8-hd"})
    )
    assert isinstance(create_tts_client(minimax_config), MiniMaxTTS)


def test_create_tts_client_applies_voice_fallback() -> None:
    """工厂层就把百炼音色兜底掉，绝不把 ``Elias`` 原样发给 MiniMax。"""
    base = AppConfig()
    minimax_config = AppConfig(
        models=base.models.model_copy(update={"tts": "speech-2.8-hd"}),
        tts=TTSConfig(default_voice="Elias"),
    )
    client = create_tts_client(minimax_config)

    assert isinstance(client, MiniMaxTTS)
    assert client.model == "speech-2.8-hd"
    assert client.voice == NARRATOR_VOICE
    assert client.supports_native_speed is True


# --------------------------------------------------------------------------
# 7. Web 接口：分供应商试听 + 配置联动
# --------------------------------------------------------------------------
def test_tts_voices_endpoint_exposes_minimax_voices(tmp_path: Path) -> None:
    """``/api/tts/voices`` 同时给出百炼清单与 MiniMax 清单。"""
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    with TestClient(create_app(config)) as client:
        payload = client.get("/api/tts/voices").json()

    assert len(payload["voices"]) == 34  # 百炼清单保持不变
    assert len(payload["minimax_voices"]) == 28
    first = payload["minimax_voices"][0]
    assert first["voice_id"] == NARRATOR_VOICE
    assert first["sample_url"] == f"/api/tts/samples/minimax/{quote(NARRATOR_VOICE, safe='')}"


def test_tts_sample_route_serves_provider_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按供应商读取试听样例（``voice_samples/minimax/``）。"""
    samples_dir = tmp_path / "voice_samples" / "minimax"
    samples_dir.mkdir(parents=True)
    payload = _wav_bytes()
    (samples_dir / "Chinese__Mandarin__Radio_Host.wav").write_bytes(payload)
    monkeypatch.setattr(web_server, "voice_sample_dir", lambda: tmp_path / "voice_samples")

    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    with TestClient(create_app(config)) as client:
        response = client.get(
            "/api/tts/samples/minimax/Chinese%20(Mandarin)_Radio_Host"
        )

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"].startswith("audio/wav")


def test_tts_sample_route_keeps_flat_legacy_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧扁平布局（``voice_samples/Elias.wav``）继续可读。"""
    samples_dir = tmp_path / "voice_samples"
    samples_dir.mkdir(parents=True)
    payload = _wav_bytes()
    (samples_dir / "Elias.wav").write_bytes(payload)
    monkeypatch.setattr(web_server, "voice_sample_dir", lambda: samples_dir)

    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    with TestClient(create_app(config)) as client:
        response = client.get("/api/tts/samples/Elias")

    assert response.status_code == 200
    assert response.content == payload


def test_tts_sample_route_reads_qwen_provider_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分供应商布局下，旧链接（``/api/tts/samples/Elias``）也能命中 ``qwen/`` 子目录。"""
    samples_dir = tmp_path / "voice_samples"
    (samples_dir / "qwen").mkdir(parents=True)
    payload = _wav_bytes()
    (samples_dir / "qwen" / "Elias.wav").write_bytes(payload)
    monkeypatch.setattr(web_server, "voice_sample_dir", lambda: samples_dir)

    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    with TestClient(create_app(config)) as client:
        response = client.get("/api/tts/samples/Elias")

    assert response.status_code == 200
    assert response.content == payload


def test_tts_sample_route_rejects_unknown_provider(tmp_path: Path) -> None:
    """未知供应商返回 404。"""
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    with TestClient(create_app(config)) as client:
        response = client.get("/api/tts/samples/unknown/Elias")

    assert response.status_code == 404


def test_config_payload_maps_tts_voice_for_active_model(tmp_path: Path) -> None:
    """StudioConfig 回显联动当前 TTS 模型（百炼音色 → MiniMax 旁白）。"""
    config = AppConfig(
        workspace=WorkspaceConfig(root=tmp_path),
        models=AppConfig().models.model_copy(update={"tts": "speech-2.8-hd"}),
        tts=TTSConfig(default_voice="Cherry"),
    )
    with TestClient(create_app(config)) as client:
        payload = client.get("/api/config").json()

    assert payload["ttsModel"] == "speech-2.8-hd"
    assert payload["ttsVoice"] == NARRATOR_VOICE
    assert len(payload["minimaxVoiceOptions"]) == 28
    assert "speech-2.8-hd" in payload["ttsModelOptions"]


def test_put_config_switching_tts_model_resets_incompatible_voice(tmp_path: Path) -> None:
    """切到 MiniMax 模型时把不兼容的默认音色换成推荐旁白。"""
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    with TestClient(create_app(config)) as client:
        payload = client.put("/api/config", json={"ttsModel": "speech-2.8-hd"}).json()

    assert payload["ttsModel"] == "speech-2.8-hd"
    assert payload["ttsVoice"] == NARRATOR_VOICE
    saved = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "default_voice" in saved
    assert NARRATOR_VOICE in saved


# --------------------------------------------------------------------------
# 7b. Web 端可设置 / 可试听（Patch v3.4 需求 2）
# --------------------------------------------------------------------------
def _register_series(workspace: Path) -> str:
    """登记一个本地作品（走 Stage 0 的导入 + 注册流程）。"""
    from stages import s0_catalog

    source = workspace / "src" / "ep_001"
    source.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 600), (40, 80, 120)).save(source / "001.png")
    catalog = s0_catalog.import_local_catalog(workspace / "src")
    s0_catalog.CatalogRegistry(workspace).register(catalog)
    return catalog.series_id


def test_series_tts_payload_exposes_minimax_voices(tmp_path: Path) -> None:
    """作品配音面板能拿到 MiniMax 28 音色（否则前端选不到 MiniMax 音色）。"""
    series_id = _register_series(tmp_path)
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    with TestClient(create_app(config)) as client:
        payload = client.get(f"/api/series/{series_id}/tts-config").json()

    minimax_voices = payload["minimax_voices"]
    assert len(minimax_voices) == 28
    ids = {item["voice_id"] for item in minimax_voices}
    assert NARRATOR_VOICE in ids
    # 与百炼清单分开下发：两套 provider 的音色不能混在一起。
    qwen_ids = {item["voice_id"] for item in payload["voices"]}
    assert not (ids & qwen_ids)


def test_voice_payload_entries_carry_provider_and_category(tmp_path: Path) -> None:
    """两套音色条目都带 provider / category，前端才能按供应商分组。"""
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    with TestClient(create_app(config)) as client:
        payload = client.get("/api/tts/voices").json()

    for item in payload["voices"]:
        assert item["provider"] == "qwen"
        assert item["category"]
    for item in payload["minimax_voices"]:
        assert item["provider"] == "minimax"
        assert item["category"] in {"narrator", "male", "female", "other"}
        assert item["description"]
        assert item["sample_url"].startswith("/api/tts/samples/minimax/")


def test_minimax_voice_categories_follow_gender_groups(tmp_path: Path) -> None:
    """MiniMax 分类与官方分组一致（旁白 5 / 男 11 / 女 12）。"""
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    with TestClient(create_app(config)) as client:
        payload = client.get("/api/tts/voices").json()

    by_category: dict[str, set[str]] = {}
    for item in payload["minimax_voices"]:
        by_category.setdefault(item["category"], set()).add(item["voice_id"])

    assert by_category["narrator"] == set(MINIMAX_NARRATOR_VOICE_IDS)
    assert by_category["male"] == set(MINIMAX_MALE_VOICE_IDS)
    assert by_category["female"] == set(MINIMAX_FEMALE_VOICE_IDS)


def test_config_payload_exposes_full_voice_options_for_preview(tmp_path: Path) -> None:
    """全局配置页的音色候选带 sample_url / sample_ready，才能试听。"""
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    with TestClient(create_app(config)) as client:
        payload = client.get("/api/config").json()

    voices = payload["ttsVoiceOptions"]
    assert len(voices) == 34
    assert voices[0]["sample_url"].startswith("/api/tts/samples/")
    assert "sample_ready" in voices[0]
    assert len(payload["minimaxVoiceOptions"]) == 28
    assert "speech-2.8-hd" in payload["ttsModelOptions"]


def test_series_tts_config_accepts_minimax_voice_ids(tmp_path: Path) -> None:
    """作品配音设置可以保存 MiniMax 音色 ID 并回读。"""
    series_id = _register_series(tmp_path)
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    body = {
        "narrator": {"voice": NARRATOR_VOICE, "speed": 1.2},
        "default_male": {"voice": "male-qn-jingying", "speed": 1.15},
        "default_female": {"voice": "female-shaonv", "speed": 1.15},
        "character_voices": {"恩彗": {"voice": "Chinese (Mandarin)_Warm_Girl", "speed": 1.18}},
    }
    with TestClient(create_app(config)) as client:
        response = client.put(f"/api/series/{series_id}/tts-config", json=body)
        assert response.status_code == 200, response.text
        payload = client.get(f"/api/series/{series_id}/tts-config").json()

    assert payload["narrator"]["voice"] == NARRATOR_VOICE
    assert payload["default_male"]["voice"] == "male-qn-jingying"
    assert payload["character_voices"]["恩彗"]["voice"] == "Chinese (Mandarin)_Warm_Girl"


def test_minimax_voice_sample_is_reachable_through_provider_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MiniMax 音色的试听样例可通过 provider 路由读到（web 端试听链路）。"""
    samples_dir = tmp_path / "voice_samples" / "minimax"
    samples_dir.mkdir(parents=True)
    payload = _wav_bytes()
    (samples_dir / "Chinese__Mandarin__Radio_Host.wav").write_bytes(payload)
    monkeypatch.setattr(web_server, "voice_sample_dir", lambda: tmp_path / "voice_samples")

    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    with TestClient(create_app(config)) as client:
        response = client.get(
            f"/api/tts/samples/minimax/{quote(NARRATOR_VOICE, safe='')}"
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")


# --------------------------------------------------------------------------
# 8. 试听缓存脚本（分供应商）
# --------------------------------------------------------------------------
def test_generate_voice_samples_minimax_provider_writes_provider_dir(
    tmp_path: Path,
) -> None:
    """``--provider minimax`` 的样例写进 ``<out>/minimax/``。"""
    client = _RecordingTTS()
    results = asyncio_run(
        init_voice_samples.generate_voice_samples(
            tmp_path, client=client, provider="minimax", trim=False
        )
    )

    assert len(results) == len(MINIMAX_VOICES)
    assert (tmp_path / "minimax" / "Chinese__Mandarin__Radio_Host.wav").is_file()
    assert not (tmp_path / "Chinese__Mandarin__Radio_Host.wav").exists()
    manifest = json.loads((tmp_path / "minimax" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["provider"] == "minimax"
    assert manifest["voices"][NARRATOR_VOICE]["file"] == (
        "Chinese__Mandarin__Radio_Host.wav"
    )


def test_generate_voice_samples_qwen_provider_writes_provider_dir(tmp_path: Path) -> None:
    """``--provider qwen`` 的样例写进 ``<out>/qwen/``。"""
    client = _RecordingTTS()
    results = asyncio_run(
        init_voice_samples.generate_voice_samples(
            tmp_path, client=client, provider="qwen", trim=False
        )
    )

    assert "Elias" in results
    assert (tmp_path / "qwen" / "Elias.wav").is_file()


def test_generate_voice_samples_default_keeps_flat_layout(tmp_path: Path) -> None:
    """不带 ``--provider`` 时保持旧行为（百炼样例写在输出目录根）。"""
    client = _RecordingTTS()
    asyncio_run(init_voice_samples.generate_voice_samples(tmp_path, client=client, trim=False))

    assert (tmp_path / "Elias.wav").is_file()
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["voices"]["Elias"]["file"] == "Elias.wav"


# --------------------------------------------------------------------------
# 9. Stage 5：音色兜底 + 原生语速
# --------------------------------------------------------------------------
def test_stage5_uses_minimax_voice_and_native_speed(tmp_path: Path) -> None:
    """Stage 5 遇到百炼音色时换成 MiniMax 音色，并把语速交给 provider。"""
    client = _RecordingTTS()
    script = _script([_beat("b1", "narrator"), _beat("b2", "female_lead")])

    asyncio_run(
        s5_tts.synthesize_episode(
            client,
            script=script,
            audio_dir=tmp_path / "audio",
            default_voice="Elias",
            voice_cast=TTSConfig().voice_cast,
            trim_silence=False,
            tts_model="speech-2.8-hd",
        )
    )

    assert client.calls[0]["voice"] == NARRATOR_VOICE
    assert client.calls[1]["voice"] == "female-yujie"
    # 原生语速：speed 交给 provider，ffmpeg 不再二次变速。
    assert client.calls[0]["speed"] == 1.0
