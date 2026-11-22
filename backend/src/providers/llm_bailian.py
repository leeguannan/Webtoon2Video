"""阿里云百炼（DashScope）OpenAI 兼容模式客户端。

规范第 2 节选型：Stage 3 用 ``qwen3-vl-plus``、Stage 4 用 ``qwen-plus``，
统一走 ``https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions``。

要点：

* API Key 统一读环境变量 ``DASHSCOPE_API_KEY``（可由项目根 ``.env`` 引导注入）；
* 自愈重试：超时、网络错误与可重试状态码（429/5xx）按指数退避重试，4xx 直接失败；
* 图片以 Base64 Data URL 传递（``ContentPart`` 的 ``image_url``）。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from core.config import (
    DEFAULT_API_BASE_URL,
    AppConfig,
    resolve_api_key_with_env_file,
)
from core.http_client import build_client
from providers.llm_base import (
    ChatMessage,
    ChatResponse,
    LLMClient,
    LLMError,
    LLMRequestError,
    LLMResponseError,
    TokenUsage,
)

CHAT_COMPLETIONS_PATH = "/chat/completions"

#: 可重试的 HTTP 状态码（限流与服务端故障）。
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

_ERROR_BODY_LIMIT = 300


def _brief(response: httpx.Response) -> str:
    """截断响应体，避免把超长内容写进异常消息。"""
    text = response.text.strip()
    if len(text) > _ERROR_BODY_LIMIT:
        return text[:_ERROR_BODY_LIMIT] + "…"
    return text


def _flatten_content(content: Any) -> str:
    """把 OpenAI 兼容的 ``content`` 字段压成纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "".join(chunks)
    return str(content)


#: Patch v4.15：模型不存在时的报错特征（这些一律不触发「剥离 enable_thinking 重试」）。
_MODEL_MISSING_MARKERS = ("modelnotfound", "model not found", "does not exist")
#: Patch v4.15：鉴权 / 权限不足的特征（同样一律不触发重试）。
_ACCESS_DENIED_MARKERS = ("accessdenied", "access denied", "forbidden")
#: Patch v4.15：DeepSeek 等模型报「参数非法 / 不支持」的特征。
_INVALID_PARAMETER_MARKERS = (
    "invalidparameter",
    "unsupported",
    "unknown parameter",
    "extra inputs are not permitted",
)


def _should_retry_without_thinking(
    status_code: int,
    body_text: str,
    payload: dict[str, Any],
    model: str,
) -> bool:
    """判断是否该剥掉 ``enable_thinking`` 参数、用同一模型原地重试一次。

    Patch v4.15：DeepSeek 系列（``deepseek-v4.1-flash-1017`` 等）不支持
    ``enable_thinking``，百炼会直接返回 400 InvalidParameter。若不在客户端
    层自愈，上层会把它当成模型失败并静默降级到 ``MODEL_FALLBACK_CHAIN`` 的
    下一个模型（qwen3.8-max），用户看不到真实原因。

    触发条件（全部满足才剥离重试）：

    * 状态码是 400（404 / 403 一律不触发）；
    * 请求体里带了 ``enable_thinking``；
    * 响应体里没有「模型不存在」的特征，也没有「鉴权 / 权限不足」的特征
      （ModelNotFound / AccessDenied 即便以 400 返回也不触发）；
    * 且满足其一：响应体点名了 ``enable_thinking``；
      或模型不是 qwen 系（DeepSeek 等）且响应体是「参数非法 / 不支持」。
    """
    if status_code != 400:
        return False
    if "enable_thinking" not in payload:
        return False
    lowered = body_text.lower()
    if any(marker in lowered for marker in _MODEL_MISSING_MARKERS):
        return False
    if any(marker in lowered for marker in _ACCESS_DENIED_MARKERS):
        return False
    if "enable_thinking" in lowered:
        return True
    if not model.strip().lower().startswith("qwen"):
        if any(marker in lowered for marker in _INVALID_PARAMETER_MARKERS):
            return True
    return False


class BailianClient(LLMClient):
    """百炼兼容模式客户端（异步 + 自愈重试）。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        config: AppConfig | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
        retry_backoff_s: float | None = None,
        env: Mapping[str, str] | None = None,
        env_file: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._api_key = (api_key or "").strip() or resolve_api_key_with_env_file(
            config, env=env, env_file=env_file
        )
        api = config.api if config is not None else None
        self._base_url = (
            base_url
            or (api.base_url if api is not None else DEFAULT_API_BASE_URL)
        ).rstrip("/")
        self._timeout_s = timeout_s if timeout_s is not None else (api.timeout_s if api else 180.0)
        self._max_retries = (
            max_retries if max_retries is not None else (api.max_retries if api else 3)
        )
        self._retry_backoff_s = (
            retry_backoff_s
            if retry_backoff_s is not None
            else (api.retry_backoff_s if api else 1.0)
        )
        self._sleep = sleep
        self._owns_client = http_client is None
        self._client = http_client or build_client(referer=None, timeout=self._timeout_s)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def api_key(self) -> str:
        """当前使用的 API Key（仅用于诊断，不应写进日志）。"""
        return self._api_key

    async def aclose(self) -> None:
        """关闭自建客户端（外部传入的客户端不关闭）。"""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> BailianClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [message.to_payload() for message in messages],
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        payload.update(kwargs)
        return payload

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """发起一次对话补全；可重试故障按指数退避自愈。"""
        url = f"{self._base_url}{CHAT_COMPLETIONS_PATH}"
        payload = self._build_payload(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            kwargs=kwargs,
        )
        last_error: LLMError | None = None
        # Patch v4.15：剥掉 enable_thinking 后原地重试只做一次。
        stripped_thinking_retried = False
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(url, json=payload, headers=self._headers())
            except httpx.TimeoutException as exc:
                last_error = LLMError(
                    f"百炼请求超时（第 {attempt + 1} 次，{type(exc).__name__}，timeout={self._timeout_s}s）：{exc}"
                )
            except httpx.HTTPError as exc:
                last_error = LLMError(
                    f"百炼请求失败（第 {attempt + 1} 次，{type(exc).__name__}）：{exc}"
                )
            else:
                if (
                    not stripped_thinking_retried
                    and _should_retry_without_thinking(
                        response.status_code, _brief(response), payload, model
                    )
                ):
                    # Patch v4.15：模型不支持 enable_thinking（如 DeepSeek 系列）。
                    # 剥掉该参数后**原地用同一个 model 重试一次**，避免上层把它
                    # 当成模型失败并静默降级到 MODEL_FALLBACK_CHAIN 的下一个模型。
                    stripped_thinking_retried = True
                    payload = {k: v for k, v in payload.items() if k != "enable_thinking"}
                    print(
                        f"[llm_bailian] 模型 {model} 不支持 enable_thinking 参数，"
                        "已剥离该参数并原地重试一次。",
                        file=sys.stderr,
                    )
                    try:
                        retry_response = await self._client.post(
                            url, json=payload, headers=self._headers()
                        )
                    except httpx.HTTPError as exc:
                        last_error = LLMError(
                            f"百炼请求失败（剥离 enable_thinking 后重试，{type(exc).__name__}）：{exc}"
                        )
                    else:
                        if retry_response.status_code < 400:
                            return self._parse_response(retry_response, model=model)
                        if retry_response.status_code in RETRYABLE_STATUS:
                            last_error = LLMRequestError(
                                f"百炼返回 {retry_response.status_code}"
                                f"（剥离 enable_thinking 后重试）：{_brief(retry_response)}"
                            )
                        else:
                            raise LLMRequestError(
                                f"百炼返回 {retry_response.status_code}：{_brief(retry_response)}"
                            )
                elif response.status_code < 400:
                    return self._parse_response(response, model=model)
                elif response.status_code in RETRYABLE_STATUS:
                    last_error = LLMRequestError(
                        f"百炼返回 {response.status_code}（第 {attempt + 1} 次）：{_brief(response)}"
                    )
                else:
                    raise LLMRequestError(
                        f"百炼返回 {response.status_code}：{_brief(response)}"
                    )
            if attempt < self._max_retries:
                await self._sleep(self._retry_backoff_s * (2**attempt))
        raise last_error or LLMError("百炼请求失败")

    def _parse_response(self, response: httpx.Response, *, model: str) -> ChatResponse:
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMResponseError(
                f"百炼响应不是合法 JSON：{_brief(response)}"
            ) from exc
        if not isinstance(data, dict):
            raise LLMResponseError(f"百炼响应结构异常：{_brief(response)}")
        choices = data.get("choices") or []
        if not choices:
            raise LLMResponseError(f"百炼响应缺少 choices：{_brief(response)}")
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") or {}
        content = _flatten_content(message.get("content"))
        usage = data.get("usage") or {}
        return ChatResponse(
            content=content,
            model=str(data.get("model") or model),
            usage=TokenUsage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
            ),
            raw=data,
            finish_reason=str(first.get("finish_reason") or ""),
        )
