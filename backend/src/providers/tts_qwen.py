"""百炼 ``qwen3-tts-flash`` 语音合成（规范 M4-T1）。

接口形态（已用真实 key 探针验证）：

- 端点：``https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation``
  （注意 TTS **不在** ``compatible-mode/v1`` 下，需从配置 base_url 派生 DashScope 根地址）；
- 请求体：``{"model": "qwen3-tts-flash", "input": {"text", "voice", "language_type",
  "instruction"}, "parameters": {"instruction"}}``——情绪指令两个位置都接受；
- 响应：``output.audio.url``（24 小时有效的 OSS 签名地址，音频为 WAV），
  需要再 GET 一次把字节取回来。

情绪指令由 Stage 4 的 ``emotion_instruction`` 原样透传，不做任何改写。
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any, Awaitable, Callable

import httpx

from core.config import ConfigError, resolve_api_key_with_env_file
from core.http_client import build_client
from providers.tts_base import TTSError, TTSRequestError, TTSResponseError, TTSResult

__all__ = [
    "QwenTTS",
    "DEFAULT_MODEL",
    "DEFAULT_VOICE",
    "TTS_ENDPOINT_PATH",
    "BAILIAN_VOICE_CATALOG",
    "BAILIAN_VOICE_IDS",
]

DEFAULT_MODEL = "qwen3-tts-flash"

#: qwen3-tts-flash 的系统音色（男声「墨讲师」，适合漫评主旁白）。
#:
#: Patch v3.1 Patch 11 A1：主旁白默认音色改为 ``Elias``；角色音色由
#: :func:`stages.s5_tts.resolve_beat_voice` 按 ``beat.speaker_role`` 路由。
DEFAULT_VOICE = "Elias"

#: 百炼 ``qwen3-tts-flash`` 官方系统音色目录（Patch v3.2 Patch 13 A）。
#:
#: 前端作品配音设置页用这份目录渲染音色下拉；``init_voice_samples.py`` 用它
#: 决定要生成哪几个静态试听样例。字段：
#:
#: - ``id``：百炼音色 ID（合成接口的 ``voice`` 字段）；
#: - ``name``：官方中文名；
#: - ``gender``：``male`` / ``female``（用于默认男声/女声兜底）；
#: - ``traits``：音色特点；
#: - ``use_case``：适用场景。
BAILIAN_VOICE_CATALOG: tuple[dict[str, str], ...] = (
    {
        "id": "Elias",
        "name": "墨讲师",
        "gender": "male",
        "traits": "磁性沉稳、有故事讲述感",
        "use_case": "悬疑/剧情漫评主旁白（推荐默认）",
    },
    {
        "id": "Ethan",
        "name": "晨煦",
        "gender": "male",
        "traits": "阳光温暖青年标准普通话",
        "use_case": "现代男主 / 青春男旁白",
    },
    {
        "id": "Moon",
        "name": "月白",
        "gender": "male",
        "traits": "清冷率性帅气男声",
        "use_case": "高冷/腹黑男主",
    },
    {
        "id": "Kai",
        "name": "凯",
        "gender": "male",
        "traits": "温柔低沉有质感男声",
        "use_case": "深情男主 / 情感故事旁白",
    },
    {
        "id": "Ryan",
        "name": "甜茶",
        "gender": "male",
        "traits": "富有戏剧张力与情绪起伏男声",
        "use_case": "冲突反派 / 激烈台词",
    },
    {
        "id": "Marcus",
        "name": "秦川",
        "gender": "male",
        "traits": "沉稳硬汉男声（略带地方硬朗韵味）",
        "use_case": "霸道长辈 / 硬派反派",
    },
    {
        "id": "Cherry",
        "name": "芊悦",
        "gender": "female",
        "traits": "明亮亲切年轻女声",
        "use_case": "年轻女主",
    },
    {
        "id": "Serena",
        "name": "苏瑶",
        "gender": "female",
        "traits": "温柔细腻小姐姐声",
        "use_case": "温柔女主 / 抒情女旁白",
    },
    {
        "id": "Maia",
        "name": "四月",
        "gender": "female",
        "traits": "知性优雅女声",
        "use_case": "理性女配 / 叙事旁白",
    },
    {
        "id": "Katerina",
        "name": "卡捷琳娜",
        "gender": "female",
        "traits": "成熟富有韵味女声",
        "use_case": "继母 / 御姐反派",
    },
    {
        "id": "Vivian",
        "name": "十三",
        "gender": "female",
        "traits": "率性可爱略带小脾气",
        "use_case": "傲娇女配 / 闺蜜",
    },
    {
        "id": "Chelsie",
        "name": "千雪",
        "gender": "female",
        "traits": "甜美少女感",
        "use_case": "软萌/少女角色",
    },
    {
        "id": "Bella",
        "name": "萌宝",
        "gender": "female",
        "traits": "萝莉/孩童声线",
        "use_case": "幼年主角 / 儿童角色",
    },
)

#: 音色 ID 列表（顺序同 :data:`BAILIAN_VOICE_CATALOG`）。
BAILIAN_VOICE_IDS: tuple[str, ...] = tuple(item["id"] for item in BAILIAN_VOICE_CATALOG)

#: DashScope 原生（非 compatible-mode）多模态生成端点路径。
TTS_ENDPOINT_PATH = "/api/v1/services/aigc/multimodal-generation/generation"

#: 可重试的 HTTP 状态码。
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def _brief(text: str, limit: int = 300) -> str:
    """截断错误体，避免把整页 HTML 打进日志。"""
    text = text.strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _fix_wav_size(data: bytes) -> bytes:
    """修正百炼返回 WAV 里的「占位式」长度字段。

    百炼（TTS 流式产出的 WAV）把 RIFF 总长与 ``data`` chunk 长度都写成
    ``0x7FFFFFFF``（未知长度占位）。直接交给 ``wave`` 或按字节数估算时长的
    代码会读到天文数字的帧数，这里按**实际 payload 长度**就地修正，
    让下游的 wave / ffmpeg / 播放器都拿到真实时长。
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return data
    buffer = bytearray(data)
    offset = 12
    while offset + 8 <= len(buffer):
        chunk_id = bytes(buffer[offset : offset + 4])
        size = struct.unpack("<I", bytes(buffer[offset + 4 : offset + 8]))[0]
        if chunk_id == b"data":
            payload = len(buffer) - (offset + 8)
            struct.pack_into("<I", buffer, offset + 4, payload)
            struct.pack_into("<I", buffer, 4, len(buffer) - 8)
            return bytes(buffer)
        offset += 8 + size + (size % 2)
    return data


class QwenTTS:
    """百炼 ``qwen3-tts-flash`` 合成器。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        timeout_s: float = 120.0,
        max_retries: int = 3,
        retry_backoff_s: float = 1.0,
        env: dict[str, str] | None = None,
        env_file: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._voice = voice
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._retry_backoff_s = retry_backoff_s
        self._env = env
        self._env_file = env_file
        self._http_client = http_client
        self._sleep = sleep

    # -- 内部工具 ---------------------------------------------------------
    def _resolve_key(self) -> str:
        if self._api_key:
            return self._api_key.strip()
        try:
            return resolve_api_key_with_env_file(env=self._env, env_file=self._env_file)
        except ConfigError as exc:  # pragma: no cover - 交由调用方给出可读信息
            raise TTSError(f"缺少 DASHSCOPE_API_KEY：{exc}") from exc

    def _endpoint(self) -> str:
        base = (self._base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        if "/compatible-mode" in base:
            base = base.split("/compatible-mode", 1)[0]
        elif base.endswith("/api/v1"):
            base = base[: -len("/api/v1")]
        return base + TTS_ENDPOINT_PATH

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is not None:
            return self._http_client
        # 注意：不要在这里挂 ``Content-Type``。返回的 OSS 签名地址把
        # Content-Type 计入签名，回源时带上不匹配的 Content-Type 会得到
        # 403 SignatureDoesNotMatch。POST 的 Content-Type 在请求级单独设置。
        return build_client(referer=None, timeout=self._timeout_s)

    # -- 合成 -------------------------------------------------------------
    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str | None = None,
        language_type: str = "Chinese",
    ) -> TTSResult:
        """合成一句语音，返回音频字节。

        ``instruction`` 是情绪指令，原样透传给服务端（规范要求）。
        """
        if not text.strip():
            raise TTSRequestError("待合成文本为空")

        # Patch v3.1 Patch 11 A1：真正生效的音色 = 显式传入的 voice or 客户端默认音色。
        # 下面 payload 与返回的 TTSResult.voice 都必须用它，否则「按角色换音色」
        # 之后 TTSResult 里的 voice 仍然是客户端默认值，对不上账。
        effective_voice = voice or self._voice

        payload: dict[str, Any] = {
            "model": self._model,
            "input": {
                "text": text,
                "voice": effective_voice,
                "language_type": language_type,
            },
            "parameters": {},
        }
        if instruction:
            payload["input"]["instruction"] = instruction
            payload["parameters"]["instruction"] = instruction

        headers = {
            "Authorization": f"Bearer {self._resolve_key()}",
            "Content-Type": "application/json",
        }
        client = self._client()
        url = self._endpoint()
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await client.post(url, headers=headers, json=payload)
            except httpx.TimeoutException as exc:
                last_error = TTSError(
                    f"百炼 TTS 请求超时（第 {attempt + 1} 次，timeout={self._timeout_s}s）：{type(exc).__name__}"
                )
            except httpx.HTTPError as exc:
                last_error = TTSError(
                    f"百炼 TTS 请求失败（第 {attempt + 1} 次）：{type(exc).__name__}: {exc}"
                )
            else:
                if response.status_code == 200:
                    return await self._download_audio(client, response, effective_voice)
                if response.status_code in RETRYABLE_STATUS:
                    last_error = TTSError(
                        f"百炼 TTS 返回可重试状态码 {response.status_code}（第 {attempt + 1} 次）："
                        f"{_brief(response.text)}"
                    )
                else:
                    raise TTSRequestError(
                        f"百炼 TTS 请求被拒绝（HTTP {response.status_code}）：{_brief(response.text)}"
                    )

            if attempt < self._max_retries:
                await self._sleep(self._retry_backoff_s * (2**attempt))

        raise TTSError(f"百炼 TTS 重试耗尽：{last_error}")

    async def _download_audio(
        self, client: httpx.AsyncClient, response: httpx.Response, voice: str | None = None
    ) -> TTSResult:
        """从响应里取出 ``output.audio.url`` 并把音频字节下载回来。

        ``voice`` 是本次真正生效的音色（Patch v3.1 Patch 11 A1）：返回的
        :class:`TTSResult` 必须带上它，不能回退成客户端默认音色。
        """
        used_voice = voice or self._voice
        try:
            data = response.json()
        except ValueError as exc:
            raise TTSResponseError(f"百炼 TTS 响应不是合法 JSON：{_brief(response.text)}") from exc

        output = data.get("output") or {}
        audio = output.get("audio") or {}
        url = audio.get("url") or ""
        inline = audio.get("data") or ""
        if not url and inline:
            import base64

            return TTSResult(
                audio=base64.b64decode(inline),
                content_type=audio.get("content_type") or "audio/wav",
                voice=used_voice,
            )
        if not url:
            raise TTSResponseError(f"百炼 TTS 响应里没有音频：{_brief(response.text)}")

        download = await client.get(url)
        if download.status_code != 200:
            raise TTSRequestError(
                f"下载 TTS 音频失败（HTTP {download.status_code}）：{_brief(download.text)}"
            )
        content_type = download.headers.get("content-type", "audio/wav")
        return TTSResult(
            audio=_fix_wav_size(download.content),
            content_type=content_type,
            voice=used_voice,
        )

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()

    async def __aenter__(self) -> "QwenTTS":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
