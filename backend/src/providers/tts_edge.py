"""``edge-tts`` 兜底合成器（规范 M4-T1）。

百炼不可用时可用本实现顶上，代价是**不支持情绪指令**——``instruction``
会被拼成 SSML 的 ``prosody``/``style`` 只能近似表达，因此 Stage 5 应在
``instruction`` 非空且必须保留情绪时优先走百炼。

``edge-tts`` 是可选依赖：未安装时构造对象不报错，调用 ``synthesize`` 才给出
明确提示（Stage 5 可以据此回落到别的实现）。
"""

from __future__ import annotations

from typing import Any

from providers.tts_base import TTSError, TTSRequestError, TTSResult

__all__ = ["EdgeTTS", "DEFAULT_VOICE", "edge_tts_available"]

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

#: 情绪指令 → SSML 风格标签的粗略映射（edge-tts 不支持自由文本指令）。
_STYLE_BY_HINT: tuple[tuple[str, str], ...] = (
    ("急切", "cheerful"),
    ("激动", "cheerful"),
    ("愤怒", "angry"),
    ("生气", "angry"),
    ("悲伤", "sad"),
    ("难过", "sad"),
    ("哽咽", "sad"),
    ("低语", "whispering"),
    ("轻声", "whispering"),
    ("耳语", "whispering"),
    ("严肃", "serious"),
    ("紧张", "serious"),
)


def edge_tts_available() -> bool:
    """当前环境是否装了 ``edge-tts``。"""
    try:  # pragma: no cover - 取决于运行环境
        import edge_tts  # noqa: F401
    except Exception:
        return False
    return True


def _style_for(instruction: str) -> str:
    for hint, style in _STYLE_BY_HINT:
        if hint in instruction:
            return style
    return "general"


class EdgeTTS:
    """基于 ``edge-tts`` 的兜底合成器。"""

    def __init__(self, voice: str = DEFAULT_VOICE, *, rate: str = "+0%", proxy: str | None = None) -> None:
        self._voice = voice
        self._rate = rate
        self._proxy = proxy

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str | None = None,
        language_type: str = "Chinese",
    ) -> TTSResult:
        """合成语音。``instruction`` 只会被粗略映射为 SSML 风格。"""
        if not text.strip():
            raise TTSRequestError("待合成文本为空")
        try:
            import edge_tts
        except Exception as exc:  # pragma: no cover - 取决于运行环境
            raise TTSError(
                "edge-tts 未安装，无法作为兜底合成器；请先 `uv pip install edge-tts` 或改用百炼 qwen3-tts-flash"
            ) from exc

        style = _style_for(instruction)
        ssml = (
            "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='zh-CN'>"
            f"<voice name='{voice or self._voice}'>"
            f"<prosody rate='{self._rate}'>"
            f"<mstts:express-as xmlns:mstts='http://www.w3.org/2001/mstts' style='{style}'>"
            f"{_escape(text)}"
            "</mstts:express-as></prosody></voice></speak>"
        )

        communicator: Any = edge_tts.Communicate(ssml, voice or self._voice, proxy=self._proxy)
        chunks: list[bytes] = []
        async for chunk in communicator.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                chunks.append(chunk["data"])
        if not chunks:
            raise TTSError("edge-tts 未返回音频数据")
        return TTSResult(
            audio=b"".join(chunks),
            content_type="audio/mpeg",
            voice=voice or self._voice,
        )


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
