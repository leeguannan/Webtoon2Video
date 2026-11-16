"""百炼兼容模式客户端（``providers/llm_bailian.py``）离线测试。"""

from __future__ import annotations

import httpx
import pytest
import respx

from core.config import ConfigError
from providers.llm_base import (
    ChatMessage,
    ContentPart,
    LLMError,
    LLMRequestError,
    LLMResponseError,
    LLMTruncatedError,
    extract_json_text,
    parse_json_response,
)
from providers.llm_bailian import BailianClient

CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
FAKE_KEY = "sk-test-fake-key"


async def _no_sleep(_seconds: float) -> None:
    """测试里跳过退避等待。"""
    return None


def _completion(content: str, *, model: str = "qwen-plus", status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
        },
    )


def _make_client(**kwargs) -> BailianClient:
    params: dict = {"api_key": FAKE_KEY, "sleep": _no_sleep}
    params.update(kwargs)
    return BailianClient(**params)


@pytest.mark.anyio
@respx.mock
async def test_chat_returns_content_and_usage() -> None:
    route = respx.post(CHAT_URL).mock(return_value=_completion("你好，我是解说稿"))
    async with _make_client() as client:
        response = await client.chat(
            [ChatMessage(role="user", content="讲个故事")], model="qwen-plus"
        )
    assert response.content == "你好，我是解说稿"
    assert response.model == "qwen-plus"
    assert response.usage.prompt_tokens == 11
    assert response.usage.total_tokens == 33
    assert route.call_count == 1


@pytest.mark.anyio
@respx.mock
async def test_chat_sends_authorization_and_payload() -> None:
    route = respx.post(CHAT_URL).mock(return_value=_completion("ok"))
    async with _make_client() as client:
        await client.chat(
            [ChatMessage(role="user", content="讲个故事")],
            model="qwen-plus",
            temperature=0.5,
            max_tokens=256,
        )
    request = route.calls[0].request
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert "referer" not in request.headers  # 百炼接口不携带 WEBTOON 的 Referer
    payload = route.calls[0].request.content.decode("utf-8")
    assert '"model": "qwen-plus"' in payload or '"model":"qwen-plus"' in payload
    assert "讲个故事" in payload


@pytest.mark.anyio
@respx.mock
async def test_multimodal_message_is_sent_as_image_url_part() -> None:
    route = respx.post(CHAT_URL).mock(return_value=_completion("ok"))
    message = ChatMessage(
        role="user",
        content=[
            ContentPart(type="text", text="描述这张图"),
            ContentPart(type="image_url", image_url="data:image/jpeg;base64,QUJD"),
        ],
    )
    async with _make_client() as client:
        await client.chat([message], model="qwen3-vl-plus")
    payload = route.calls[0].request.content.decode("utf-8")
    assert '"image_url"' in payload
    assert "data:image/jpeg;base64,QUJD" in payload


@pytest.mark.anyio
@respx.mock
async def test_server_error_is_retried_then_succeeds() -> None:
    route = respx.post(CHAT_URL).mock(
        side_effect=[_completion("boom", status=500), _completion("成功")]
    )
    async with _make_client(max_retries=2) as client:
        response = await client.chat(
            [ChatMessage(role="user", content="hi")], model="qwen-plus"
        )
    assert response.content == "成功"
    assert route.call_count == 2


@pytest.mark.anyio
@respx.mock
async def test_client_error_is_not_retried() -> None:
    route = respx.post(CHAT_URL).mock(return_value=_completion("参数非法", status=400))
    async with _make_client(max_retries=3) as client:
        with pytest.raises(LLMRequestError):
            await client.chat([ChatMessage(role="user", content="hi")], model="qwen-plus")
    assert route.call_count == 1


@pytest.mark.anyio
@respx.mock
async def test_retry_exhausted_raises_llm_error() -> None:
    route = respx.post(CHAT_URL).mock(return_value=_completion("持续失败", status=503))
    async with _make_client(max_retries=2) as client:
        with pytest.raises(LLMError):
            await client.chat([ChatMessage(role="user", content="hi")], model="qwen-plus")
    assert route.call_count == 3  # 首次 + 2 次重试


@pytest.mark.anyio
@respx.mock
async def test_timeout_is_retried_then_raises() -> None:
    route = respx.post(CHAT_URL).mock(side_effect=httpx.TimeoutException("timeout"))
    async with _make_client(max_retries=1) as client:
        with pytest.raises(LLMError):
            await client.chat([ChatMessage(role="user", content="hi")], model="qwen-plus")
    assert route.call_count == 2


@pytest.mark.anyio
@respx.mock
async def test_response_without_choices_raises() -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json={"model": "qwen-plus", "choices": []})
    )
    async with _make_client() as client:
        with pytest.raises(LLMResponseError):
            await client.chat([ChatMessage(role="user", content="hi")], model="qwen-plus")


@pytest.mark.anyio
@respx.mock
async def test_content_list_is_flattened() -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "qwen-plus",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "第一段"},
                                {"type": "text", "text": "第二段"},
                            ],
                        }
                    }
                ],
            },
        )
    )
    async with _make_client() as client:
        response = await client.chat(
            [ChatMessage(role="user", content="hi")], model="qwen-plus"
        )
    assert response.content == "第一段第二段"


@pytest.mark.anyio
async def test_missing_api_key_raises_config_error(tmp_path) -> None:
    missing = tmp_path / "no-such.env"
    with pytest.raises(ConfigError):
        BailianClient(env={}, env_file=str(missing))


@pytest.mark.anyio
async def test_api_key_is_read_from_env_file(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# 注释\nDASHSCOPE_API_KEY='sk-from-file'\n", encoding="utf-8")
    async with BailianClient(env={}, env_file=str(env_file)) as client:
        assert client.api_key == "sk-from-file"


def test_parse_json_response_strips_code_fence() -> None:
    payload = parse_json_response('```json\n{"a": 1}\n```')
    assert payload == {"a": 1}


def test_parse_json_response_rejects_garbage() -> None:
    with pytest.raises(LLMResponseError):
        parse_json_response("不是 JSON")


@pytest.mark.anyio
@respx.mock
async def test_finish_reason_is_exposed_on_response() -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "qwen-plus",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            },
        )
    )
    async with _make_client() as client:
        response = await client.chat(
            [ChatMessage(role="user", content="hi")], model="qwen-plus"
        )
    assert response.finish_reason == "length"


def test_extract_json_text_tolerates_unclosed_fence() -> None:
    # 输出被 max_tokens 截断时，闭合围栏丢失，只剩开头围栏。
    text = '```json\n{"a": 1, "b": [1, 2'
    assert extract_json_text(text) == '{"a": 1, "b": [1, 2'


def test_parse_json_response_raises_truncated_error_on_length() -> None:
    broken = '```json\n{"hook_title": "x", "beats": [{"narration_text": "半截'
    with pytest.raises(LLMTruncatedError, match="截断"):
        parse_json_response(broken, finish_reason="length")
    # 同样的残片，若供应商未报 length，仍按普通非法 JSON 归因。
    with pytest.raises(LLMResponseError) as exc_info:
        parse_json_response(broken)
    assert not isinstance(exc_info.value, LLMTruncatedError)
