"""TTS 抽象层（规范 M4-T1）。

Stage 5 只依赖本模块的 :class:`TTSSynthesizer` 协议，具体实现可换：

- :mod:`providers.tts_qwen` —— 百炼 ``qwen3-tts-flash``（原生支持情绪指令）；
- :mod:`providers.tts_edge` —— ``edge-tts`` 兜底（未安装时给出明确报错）。

两条铁律在这里落位：

1. **双文本分离**：送进 TTS 的是 ``spoken_text``（可能已做多音字替换），
   屏幕字幕用的 ``narration_text`` 永远不会进入本模块。
2. **情绪透传**：``emotion_instruction`` 原样作为 ``instruction`` 传给合成器。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from core.schemas import WordTimestamp

__all__ = [
    "TTSError",
    "TTSRequestError",
    "TTSResponseError",
    "TTSResult",
    "TTSSynthesizer",
]


class TTSError(RuntimeError):
    """TTS 合成失败的基类。"""


class TTSRequestError(TTSError):
    """请求被服务端拒绝（4xx / 参数非法）。"""


class TTSResponseError(TTSError):
    """响应无法解析成音频。"""


@dataclass(frozen=True)
class TTSResult:
    """一次合成的结果。

    ``audio`` 是原始音频字节；``content_type`` 用于决定落盘扩展名
    （百炼返回 ``audio/x-wav``，落盘为 ``.wav``）。
    """

    audio: bytes
    content_type: str = "audio/wav"
    sample_rate: int = 24000
    voice: str = ""
    word_timestamps: list[WordTimestamp] = field(default_factory=list)

    @property
    def suffix(self) -> str:
        """按 content_type 推断落盘扩展名。"""
        if "wav" in self.content_type:
            return ".wav"
        if "mpeg" in self.content_type or "mp3" in self.content_type:
            return ".mp3"
        if "ogg" in self.content_type:
            return ".ogg"
        if "flac" in self.content_type:
            return ".flac"
        return ".wav"


@runtime_checkable
class TTSSynthesizer(Protocol):
    """Stage 5 依赖的最小合成接口。"""

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str | None = None,
        language_type: str = "Chinese",
    ) -> TTSResult:
        """把 ``text`` 合成语音。

        ``instruction`` 是自然语言情绪指令（如「语速稍快，气息微促地说」），
        实现方应尽量原样透传给服务端。
        """
        ...
