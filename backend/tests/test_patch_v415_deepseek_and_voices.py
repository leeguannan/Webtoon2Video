"""Patch v4.15：百炼 DeepSeek 模型接入 + 音色选择仅保留 MiniMax。

覆盖四件事：

1. ``/api/config`` 的模型下拉候选含 ``deepseek-v4.1-flash-1017``，且
   ``PUT /api/config`` 能把它落盘并回显；
2. 百炼客户端遇到 400 ``InvalidParameter(enable_thinking)`` 时，自动剥掉该
   参数并**原地用同一个 model 重试 1 次**（DeepSeek 系列不支持思考开关）；
3. 404 ``ModelNotFound`` 一律不触发剥离重试，只请求 1 次并抛
   ``LLMRequestError``；
4. ``parse_json_response`` 能剥掉 ``<think>...</think>`` 思考块再取 JSON。

全部离线：不触网，百炼请求用 respx 打桩。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
import yaml
from fastapi.testclient import TestClient

from core.config import AppConfig, WorkspaceConfig
from providers.llm_base import (
    ChatMessage,
    LLMRequestError,
    parse_json_response,
)
from providers.llm_bailian import BailianClient
from web.server import create_app

CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
FAKE_KEY = "sk-test-fake-key"

#: Patch v4.15：百炼上的 DeepSeek 高性价比模型。
DEEPSEEK_MODEL = "deepseek-v4.1-flash-1017"


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


def _error_response(code: str, message: str, *, status: int) -> httpx.Response:
    """百炼错误体（结构与真实 OpenAI 兼容模式一致）。"""
    return httpx.Response(status, json={"code": code, "message": message})


def _make_client(**kwargs) -> BailianClient:
    params: dict = {"api_key": FAKE_KEY, "sleep": _no_sleep}
    params.update(kwargs)
    return BailianClient(**params)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "ws"


@pytest.fixture()
def client(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = create_app(AppConfig(workspace=WorkspaceConfig(root=workspace)))
    # 切到临时工作区，避免读写仓库里真实的 config.yaml。
    monkeypatch.chdir(workspace)
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. /api/config：DeepSeek 候选 + 落盘回显
# ---------------------------------------------------------------------------


def test_config_endpoint_offers_deepseek_model(client: TestClient) -> None:
    """验收①：模型下拉候选含 deepseek-v4.1-flash-1017。"""
    payload = client.get("/api/config").json()
    assert DEEPSEEK_MODEL in payload["scriptModelOptions"]
    assert DEEPSEEK_MODEL in payload["storyModelOptions"]
    # 原有的主力模型仍在（不得因新增候选而挤掉）。
    assert payload["scriptModelOptions"][0] == "qwen3.7-plus"
    assert payload["storyModelOptions"][0] == "qwen4-max"
    # 默认值不变（禁改 core/config.py 的默认值）。
    assert payload["scriptModel"] == "qwen3.7-plus"
    assert payload["storyModel"] == "qwen4-max"


def test_put_config_persists_deepseek_model(client: TestClient, workspace: Path) -> None:
    """验收①：PUT 选中的 DeepSeek 模型能落盘并回显。"""
    response = client.put(
        "/api/config",
        json={"scriptModel": DEEPSEEK_MODEL, "storyModel": DEEPSEEK_MODEL},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scriptModel"] == DEEPSEEK_MODEL
    assert body["storyModel"] == DEEPSEEK_MODEL

    saved = yaml.safe_load((workspace / "config.yaml").read_text(encoding="utf-8"))
    assert saved["models"]["script"] == DEEPSEEK_MODEL
    assert saved["models"]["story_model"] == DEEPSEEK_MODEL


# ---------------------------------------------------------------------------
# 2./3. 百炼客户端：enable_thinking 剥离重试 / 模型不存在不重试
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_invalid_enable_thinking_is_stripped_and_retried_in_place() -> None:
    """验收②：400 InvalidParameter(enable_thinking) → 剥离后原地重试成功。"""
    route = respx.post(CHAT_URL).mock(
        side_effect=[
            _error_response(
                "InvalidParameter",
                "Parameter enable_thinking is not supported for this model.",
                status=400,
            ),
            _completion("重试成功", model=DEEPSEEK_MODEL),
        ]
    )
    async with _make_client(max_retries=2) as client:
        response = await client.chat(
            [ChatMessage(role="user", content="讲个故事")],
            model=DEEPSEEK_MODEL,
            enable_thinking=False,
        )
    assert response.content == "重试成功"
    assert route.call_count == 2
    # 第一次带 enable_thinking，第二次已剥离。
    first = route.calls[0].request.content.decode("utf-8")
    second = route.calls[1].request.content.decode("utf-8")
    assert "enable_thinking" in first
    assert "enable_thinking" not in second
    # 重试用的是同一个模型（不静默降级到别的模型）。
    assert DEEPSEEK_MODEL in second


@pytest.mark.anyio
@respx.mock
async def test_model_not_found_is_not_retried() -> None:
    """验收③：404 ModelNotFound 只请求 1 次并抛 LLMRequestError。"""
    route = respx.post(CHAT_URL).mock(
        return_value=_error_response(
            "ModelNotFound",
            "The model does not exist.",
            status=404,
        )
    )
    async with _make_client(max_retries=3) as client:
        with pytest.raises(LLMRequestError):
            await client.chat(
                [ChatMessage(role="user", content="讲个故事")],
                model=DEEPSEEK_MODEL,
                enable_thinking=False,
            )
    assert route.call_count == 1


@pytest.mark.anyio
@respx.mock
async def test_access_denied_is_not_retried() -> None:
    """AccessDenied 即便以 400 返回，也不触发「剥离 enable_thinking 重试」。"""
    route = respx.post(CHAT_URL).mock(
        return_value=_error_response(
            "AccessDenied",
            "AccessDenied: enable_thinking is not allowed for this account.",
            status=400,
        )
    )
    async with _make_client(max_retries=3) as client:
        with pytest.raises(LLMRequestError):
            await client.chat(
                [ChatMessage(role="user", content="讲个故事")],
                model=DEEPSEEK_MODEL,
                enable_thinking=False,
            )
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# 4. <think> 思考块剥离
# ---------------------------------------------------------------------------


def test_parse_json_response_strips_think_block() -> None:
    """验收④：<think>…</think> 思考块被剥掉后照常解析 JSON。"""
    data = parse_json_response(
        '<think>分析中，先想一想语气词怎么写</think>\n```json\n{"ok": true}\n```'
    )
    assert data == {"ok": True}
