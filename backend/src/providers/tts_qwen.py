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
import re
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
    "QWEN3_TTS_VOICES",
    "QWEN3_VOICE_IDS",
    "CORE_VOICE_IDS",
    "voice_sample_filename",
]

DEFAULT_MODEL = "qwen3-tts-flash"

#: qwen3-tts-flash 的系统音色（男声「墨讲师」，适合漫评主旁白）。
#:
#: Patch v3.1 Patch 11 A1：主旁白默认音色改为 ``Elias``；角色音色由
#: :func:`stages.s5_tts.resolve_beat_voice` 按 ``beat.speaker_role`` 路由。
DEFAULT_VOICE = "Elias"

#: 百炼 ``qwen3-tts-flash`` 全量官方中文/方言/戏剧音色目录（Patch v3.3 Patch 15，34 个）。
#:
#: 前端作品配音设置页用这份目录渲染分组音色下拉；``init_voice_samples.py`` 用它
#: 决定要生成哪几个静态试听样例。字段：
#:
#: - ``voice_id``：百炼音色 ID（合成接口的 ``voice`` 字段，可能含空格，如 ``Eldric Sage``）；
#: - ``display_name``：官方中文名；
#: - ``gender``：``male`` / ``female``（用于默认男声/女声兜底）；
#: - ``age_group``：``child`` / ``youth`` / ``middle`` / ``elder``；
#: - ``category``：``narrator``（旁白）/ ``character``（角色）/ ``dialect``（方言）/ ``drama``（戏剧）；
#: - ``description``：音色特点与适用场景。
#:
#: 目录之外的其余百炼官方音色 ID（共 49 种）不做枚举限制，``VoiceSpec.voice``
#: 可直接手填透传，只是没有静态试听样例。
QWEN3_TTS_VOICES: tuple[dict[str, str], ...] = (
    # -- 男声：普通话主旁白与青年/大叔/老者（11 个） ---------------------
    {
        "voice_id": "Elias",
        "display_name": "墨讲师",
        "gender": "male",
        "age_group": "middle",
        "category": "narrator",
        "description": "磁性沉稳，适合悬疑/剧情漫评主旁白",
    },
    {
        "voice_id": "Neil",
        "display_name": "阿闻",
        "gender": "male",
        "age_group": "youth",
        "category": "narrator",
        "description": "标准主播腔，字正腔圆、语流利落",
    },
    {
        "voice_id": "Kai",
        "display_name": "凯",
        "gender": "male",
        "age_group": "youth",
        "category": "narrator",
        "description": "温和磁性「耳朵SPA」，适合情感/治愈线旁白",
    },
    {
        "voice_id": "Ethan",
        "display_name": "晨煦",
        "gender": "male",
        "age_group": "youth",
        "category": "character",
        "description": "阳光标准普通话，适合年轻男主",
    },
    {
        "voice_id": "Moon",
        "display_name": "月白",
        "gender": "male",
        "age_group": "youth",
        "category": "character",
        "description": "清冷帅气，适合高冷/腹黑男主",
    },
    {
        "voice_id": "Vincent",
        "display_name": "田叔",
        "gender": "male",
        "age_group": "middle",
        "category": "character",
        "description": "沙哑烟嗓大叔，极适合父亲/boss/反派",
    },
    {
        "voice_id": "Eldric Sage",
        "display_name": "沧明子",
        "gender": "male",
        "age_group": "elder",
        "category": "character",
        "description": "沉稳睿智老者、师尊/长辈",
    },
    {
        "voice_id": "Arthur",
        "display_name": "徐大爷",
        "gender": "male",
        "age_group": "elder",
        "category": "narrator",
        "description": "沧桑质朴说书老人",
    },
    {
        "voice_id": "Nofish",
        "display_name": "不吃鱼",
        "gender": "male",
        "age_group": "youth",
        "category": "character",
        "description": "幽默个性男配角",
    },
    {
        "voice_id": "Mochi",
        "display_name": "沙小弥",
        "gender": "male",
        "age_group": "child",
        "category": "character",
        "description": "机灵聪明少年音",
    },
    {
        "voice_id": "Pip",
        "display_name": "调皮小新",
        "gender": "male",
        "age_group": "child",
        "category": "character",
        "description": "活泼调皮男童声",
    },
    # -- 女声：普通话女主/御姐/少女/老妇（15 个） -----------------------
    {
        "voice_id": "Cherry",
        "display_name": "芊悦",
        "gender": "female",
        "age_group": "youth",
        "category": "character",
        "description": "阳光明亮亲切女声，适合现代女主",
    },
    {
        "voice_id": "Serena",
        "display_name": "苏瑶",
        "gender": "female",
        "age_group": "youth",
        "category": "narrator",
        "description": "温柔小姐姐，适合言情女旁白",
    },
    {
        "voice_id": "Seren",
        "display_name": "小婉",
        "gender": "female",
        "age_group": "youth",
        "category": "narrator",
        "description": "舒缓柔声讲故事",
    },
    {
        "voice_id": "Maia",
        "display_name": "四月",
        "gender": "female",
        "age_group": "youth",
        "category": "narrator",
        "description": "知性优雅女声",
    },
    {
        "voice_id": "Mia",
        "display_name": "乖小妹",
        "gender": "female",
        "age_group": "youth",
        "category": "character",
        "description": "温顺乖巧甜美少女",
    },
    {
        "voice_id": "Bellona",
        "display_name": "燕铮莺",
        "gender": "female",
        "age_group": "youth",
        "category": "character",
        "description": "洪亮正统有气场，适合大女主/女反派",
    },
    {
        "voice_id": "Katerina",
        "display_name": "卡捷琳娜",
        "gender": "female",
        "age_group": "middle",
        "category": "character",
        "description": "成熟有韵味女声，适合继母/反派女",
    },
    {
        "voice_id": "Vivian",
        "display_name": "十三",
        "gender": "female",
        "age_group": "youth",
        "category": "character",
        "description": "自信可爱带小脾气",
    },
    {
        "voice_id": "Momo",
        "display_name": "茉兔",
        "gender": "female",
        "age_group": "youth",
        "category": "character",
        "description": "俏皮搞怪闺蜜",
    },
    {
        "voice_id": "Chelsie",
        "display_name": "千雪",
        "gender": "female",
        "age_group": "youth",
        "category": "character",
        "description": "二次元软萌少女",
    },
    {
        "voice_id": "Bella",
        "display_name": "萌宝",
        "gender": "female",
        "age_group": "youth",
        "category": "character",
        "description": "古灵精怪萌系少女",
    },
    {
        "voice_id": "Bunny",
        "display_name": "萌小姬",
        "gender": "female",
        "age_group": "child",
        "category": "character",
        "description": "可爱小萝莉",
    },
    {
        "voice_id": "Nini",
        "display_name": "邻家妹妹",
        "gender": "female",
        "age_group": "youth",
        "category": "character",
        "description": "自然邻家妹妹",
    },
    {
        "voice_id": "Stella",
        "display_name": "美少女阿月",
        "gender": "female",
        "age_group": "youth",
        "category": "character",
        "description": "甜美二次元美少女",
    },
    {
        "voice_id": "Ebona",
        "display_name": "诡婆婆",
        "gender": "female",
        "age_group": "elder",
        "category": "character",
        "description": "悬疑故事老年女声",
    },
    # -- 方言与剧场张力特色音色（8 个） ---------------------------------
    {
        "voice_id": "Dylan",
        "display_name": "北京晓东",
        "gender": "male",
        "age_group": "youth",
        "category": "dialect",
        "description": "北京胡同青年",
    },
    {
        "voice_id": "Jada",
        "display_name": "上海阿珍",
        "gender": "female",
        "age_group": "middle",
        "category": "dialect",
        "description": "上海话特色女声",
    },
    {
        "voice_id": "Sunny",
        "display_name": "杭州阿晴",
        "gender": "female",
        "age_group": "youth",
        "category": "dialect",
        "description": "温柔江南口音",
    },
    {
        "voice_id": "Eric",
        "display_name": "四川程川",
        "gender": "male",
        "age_group": "youth",
        "category": "dialect",
        "description": "四川话特色男声",
    },
    {
        "voice_id": "Marcus",
        "display_name": "陕西秦川",
        "gender": "male",
        "age_group": "middle",
        "category": "dialect",
        "description": "陕西硬汉男声",
    },
    {
        "voice_id": "Li",
        "display_name": "南京老李",
        "gender": "male",
        "age_group": "middle",
        "category": "dialect",
        "description": "南京话大叔",
    },
    {
        "voice_id": "Ryan",
        "display_name": "甜茶",
        "gender": "male",
        "age_group": "youth",
        "category": "drama",
        "description": "强烈戏剧节奏与爆发张力男声",
    },
    {
        "voice_id": "Jennifer",
        "display_name": "詹妮弗",
        "gender": "female",
        "age_group": "youth",
        "category": "drama",
        "description": "电影叙事质感音色",
    },
)

#: 音色 ID 列表（顺序同 :data:`QWEN3_TTS_VOICES`）。
QWEN3_VOICE_IDS: tuple[str, ...] = tuple(item["voice_id"] for item in QWEN3_TTS_VOICES)

#: 高频核心音色（``init_voice_samples.py --tier core`` 的 14 个，快速起步）。
#: 即 Patch v3.2 已验证的 13 个 + 新增标准主播 ``Neil``。
CORE_VOICE_IDS: tuple[str, ...] = (
    "Elias",
    "Neil",
    "Ethan",
    "Moon",
    "Kai",
    "Ryan",
    "Marcus",
    "Cherry",
    "Serena",
    "Maia",
    "Katerina",
    "Vivian",
    "Chelsie",
    "Bella",
)

#: 样例文件名里允许保留的字符（字母/数字/下划线/连字符/空格），其余一律剔除，
#: 防止手填音色 ID 造成路径穿越；空格随后统一替换成下划线。
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_\- ]+")


def voice_sample_filename(voice_id: str) -> str:
    """音色 ID → 试听样例文件名。

    含空格的 ID 安全映射为下划线（``Eldric Sage`` → ``Eldric_Sage.wav``）；
    由于下划线本身是保留字符，URL 里无论传 ``Eldric%20Sage`` 还是 ``Eldric_Sage``
    都会解析到同一个文件。
    """
    cleaned = _UNSAFE_FILENAME_CHARS.sub("", voice_id.strip()).replace(" ", "_")
    return f"{cleaned or 'unknown'}.wav"

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
