"""大模型客户端的厂商无关契约（规范第 2 节「百炼 compatible-mode/v1」）。

本模块只定义数据结构与抽象基类：

* :class:`ChatMessage` / :class:`ContentPart`：OpenAI 兼容的��息格式（支持多模态图片）；
* :class:`ChatResponse` / :class:`TokenUsage`：统一响应封装；
* :class:`LLMClient`：厂商无关的对话补全契约；
* :func:`parse_json_response`：把模型返回的 JSON 文本（含 markdown 围栏）解析为对象。

具体实现见 :mod:`providers.llm_bailian`（阿里云百炼）。
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

ContentType = Literal["text", "image_url"]
MessageRole = Literal["system", "user", "assistant"]


class ProviderError(RuntimeError):
    """供应商调用相关错误的基类。"""


class LLMError(ProviderError):
    """大模型调用失败（网络、超时、重试耗尽）。"""


class LLMRequestError(LLMError):
    """请求被拒绝或参数非法（HTTP 4xx，不重试）。"""


class LLMResponseError(LLMError):
    """响应无法解析（非法 JSON、缺少 choices 等）。"""


class LLMTruncatedError(LLMResponseError):
    """响应被 ``max_tokens`` 截断（``finish_reason == "length"``），JSON 没写完。

    与普通「非法 JSON」分开：前者的修法是缩小单次输出体量（分批/两段式），
    后者才是提示词或模型格式问题。
    """


@dataclass(frozen=True)
class ContentPart:
    """多模态消息的一个片段：纯文本或图片 URL。"""

    type: ContentType
    text: str = ""
    image_url: str = ""

    def to_payload(self) -> dict[str, Any]:
        if self.type == "text":
            return {"type": "text", "text": self.text}
        return {"type": "image_url", "image_url": {"url": self.image_url}}


@dataclass(frozen=True)
class ChatMessage:
    """一条对话消息。"""

    role: MessageRole = "user"
    content: str | list[ContentPart] = ""

    def to_payload(self) -> dict[str, Any]:
        if isinstance(self.content, str):
            return {"role": self.role, "content": self.content}
        return {"role": self.role, "content": [part.to_payload() for part in self.content]}


@dataclass(frozen=True)
class TokenUsage:
    """Token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class ChatResponse:
    """对话补全的统一响应。"""

    content: str
    model: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: dict[str, Any] = field(default_factory=dict)
    #: 供应商给出的结束原因；``"length"`` 表示输出被 ``max_tokens`` 截断。
    #: 调用方据此区分「模型返回了非法 JSON」与「输出根本没写完」——后者再解析
    #: JSON 一定会失败，但报错的归因完全不同。
    finish_reason: str = ""

    @property
    def text(self) -> str:
        """与 :attr:`content` 等价，便于按文本语义取值。"""
        return self.content


class LLMClient(ABC):
    """厂商无关的对话补全客户端契约。"""

    @abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """发起一次对话补全。"""

    async def chat_text(self, messages: list[ChatMessage], *, model: str, **kwargs: Any) -> str:
        """只要文本内容的便捷封装。"""
        response = await self.chat(messages, model=model, **kwargs)
        return response.content


_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
# 只有开头围栏、没有闭合围栏的场景：输出在围栏内部被 max_tokens 截断。
_OPEN_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*", re.IGNORECASE)
_TRAILING_FENCE_RE = re.compile(r"\s*```\s*$")


def extract_json_text(text: str) -> str:
    """剥掉 markdown 代码围栏，取出 JSON 正文。

    成对围栏优先；若只有开头围栏（输出被截断、闭合围栏丢失），也剥掉开头
    围栏并返回剩余全文，让上层拿到真正的 JSON 残片去做归因。
    """
    stripped = text.strip()
    match = _CODE_FENCE_RE.search(stripped)
    if match:
        return match.group(1).strip()
    if _OPEN_FENCE_RE.match(stripped):
        return _TRAILING_FENCE_RE.sub("", _OPEN_FENCE_RE.sub("", stripped)).strip()
    return stripped


def parse_json_response(text: str, *, finish_reason: str = "") -> Any:
    """把模型返回的文本解析为 JSON 对象。

    解析失败时：若供应商报告 ``finish_reason == "length"``，抛
    :class:`LLMTruncatedError`（输出被 ``max_tokens`` 截断）；否则抛
    :class:`LLMResponseError`（格式问题）。
    """
    payload = extract_json_text(text)
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        if finish_reason == "length":
            raise LLMTruncatedError(
                "模型输出被 max_tokens 截断，JSON 未写完（finish_reason=length）；"
                f"残片开头：{payload[:200]!r}"
            ) from exc
        raise LLMResponseError(f"模型返回内容不是合法 JSON：{payload[:200]!r}") from exc
