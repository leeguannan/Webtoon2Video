"""MiniMax Speech 2.8 语音合成（Patch v3.4 Patch 16）。

接口形态：

- 端点：``{base_url}/t2a_v2``（仅当配置了 ``GroupId`` 时追加 ``?GroupId=xxx``）；
- 请求体：``{"model", "text", "stream": False, "language_boost", "output_format": "hex",
  "voice_setting": {...}, "audio_setting": {...}}``；
- 响应：``{"data": {"audio": "<hex>"}, "extra_info": {...}, "trace_id": "...",
  "base_resp": {"status_code": 0, ...}}``——**MiniMax 常在 HTTP 200 的 JSON 里返回
  错误码**，因此错误判定必须读 ``base_resp.status_code``，不能只看 HTTP 状态码；
  音频则位于**嵌套**的 ``data.data.audio``（不是顶层 ``audio``）。

两条硬约束：

1. **安全情绪枚举**：只传 MiniMax 认识的离散枚举（``calm`` / ``happy`` / ``sad`` /
   ``angry`` / ``fearful`` / ``surprised`` / ``disgusted``），严禁传 ``whisper``
   （会让音频发闷失真）；未命中就**不传** ``emotion`` 字段，由模型自行抑扬顿挫。
2. **跨模型音色防踩坑**：Qwen/Edge 的音色 ID（如 ``Cherry``）发给 MiniMax 会返回
   ``2013 invalid voice_id`` 并让整话失败，统一由
   :func:`core.config.resolve_voice_for_tts_model` 平滑替换（见该函数文档）。
3. **音色 ID 必须真实存在**：MiniMax 对未知音色返回 ``2054 voice id not exist``，
   本模块的预置清单全部取自 ``POST /v1/get_voice`` 的真实 ``system_voice``。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any, Awaitable, Callable

import httpx

from core.config import (
    ConfigError,
    resolve_minimax_api_key_with_env_file,
    resolve_minimax_group_id,
)
from core.http_client import build_client
from providers.tts_base import TTSError, TTSRequestError, TTSResponseError, TTSResult

__all__ = [
    "MiniMaxTTS",
    "MINIMAX_VOICES",
    "MINIMAX_VOICE_IDS",
    "MINIMAX_NARRATOR_VOICE_IDS",
    "MINIMAX_MALE_VOICE_IDS",
    "MINIMAX_FEMALE_VOICE_IDS",
    "DEFAULT_MODEL",
    "DEFAULT_VOICE",
    "RETRYABLE_STATUS",
    "RETRYABLE_CODES",
    "FATAL_CODES",
    "fetch_minimax_voices",
    "voice_sample_filename",
]

DEFAULT_MODEL = "speech-2.8-hd"

#: 默认旁白音色（电台男主播：富有诗意、流畅引人入胜，适合漫画解说）。
#:
#: 注意：MiniMax 的 ``system_voice`` 里**没有** ``Chinese (Mandarin)_Serene_Narrator``
#: 这类 ID，用不存在的音色会拿到 ``2054 voice id not exist``；本表所有 ID 均取自
#: ``POST /v1/get_voice`` 返回的真实系统音色清单。
DEFAULT_VOICE = "Chinese (Mandarin)_Radio_Host"

#: MiniMax 官方接口根地址（``/v1``）。
DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"

#: 默认环境变量名（API Key / GroupId）。
DEFAULT_API_KEY_ENV = "MINIMAX_API_KEY"
DEFAULT_GROUP_ID_ENV = "MINIMAX_GROUP_ID"

#: 可重试的 HTTP 状态码（与百炼保持同一套）。
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: 可重试的 ``base_resp.status_code``（限流 / 繁忙 / 上游临时故障）。
RETRYABLE_CODES = frozenset({1000, 1002, 1024, 1039})

#: ``1002`` = RPM 限流（rate limit exceeded(RPM)）。Patch v4.3：命中它要
#: 全局降速 + 冷却，而不是各自退避后一起再撞。
RATE_LIMIT_CODE = 1002

#: 不可重试的 ``base_resp.status_code``（鉴权失败 / 余额不足 / voice_id 非法）。
#: - 1004：鉴权失败；1008：余额不足；2013：voice_id 非法；2054：voice_id 不存在。
FATAL_CODES = frozenset({1004, 1008, 2013, 2054})


#: ``base_resp.status_code`` → 面向用户的中文原因说明。
_FATAL_CODE_REASONS: dict[int, str] = {
    1004: "鉴权失败（API Key 无效或无权访问该模型）",
    1008: "账户余额不足（请在 MiniMax 控制台充值，或更换 API Key）",
    2013: "voice_id 非法",
    2054: "voice_id 不存在（该音色 ID 不在 MiniMax 系统音色清单内）",
}


def _fatal_message(code: int, message: str, voice: str) -> str:
    """把不可重试错误码翻译成可读文案（含出错音色）。"""
    reason = _FATAL_CODE_REASONS.get(code, "请求被拒绝")
    detail = message.strip() or "服务端未返回原因"
    return f"MiniMax TTS {reason} (status_code={code})，当前音色 {voice}：{detail}"


# ---------------------------------------------------------------------------
# 音色目录
# ---------------------------------------------------------------------------

#: 预置音色目录（28 个）：id / name / gender / style / recommended_for。
#:
#: 全部 ID 取自 MiniMax ``POST /v1/get_voice`` 返回的真实 ``system_voice`` 清单
#: （用不存在的 ID 合成会返回 ``2054 voice id not exist``）。
MINIMAX_VOICES: tuple[dict[str, str], ...] = (
    # -- 推荐说书旁白（5 个）------------------------------------------------
    {
        "id": "Chinese (Mandarin)_Radio_Host",
        "name": "电台男主播",
        "gender": "male",
        "style": "沉稳叙事",
        "recommended_for": "悬疑/剧情长漫主旁白",
    },
    {
        "id": "Chinese (Mandarin)_Male_Announcer",
        "name": "播报男声",
        "gender": "male",
        "style": "磁性有力",
        "recommended_for": "节奏紧凑的解说旁白",
    },
    {
        "id": "Chinese (Mandarin)_Reliable_Executive",
        "name": "沉稳高管",
        "gender": "male",
        "style": "商务稳重",
        "recommended_for": "都市/商战/悬疑旁白",
    },
    {
        "id": "Chinese (Mandarin)_News_Anchor",
        "name": "新闻女声",
        "gender": "female",
        "style": "清晰利落",
        "recommended_for": "资讯速报型旁白",
    },
    {
        "id": "Chinese (Mandarin)_Gentle_Senior",
        "name": "温柔学姐",
        "gender": "female",
        "style": "温厚叙事",
        "recommended_for": "抒情/回忆段落",
    },
    # -- 男角色音色（11 个）------------------------------------------------
    {
        "id": "male-qn-qingse",
        "name": "青涩青年",
        "gender": "male",
        "style": "少年感清亮",
        "recommended_for": "校园/青涩少年角色",
    },
    {
        "id": "male-qn-qingse-jingpin",
        "name": "青涩青年-精品",
        "gender": "male",
        "style": "高品质少年感",
        "recommended_for": "重要少年角色",
    },
    {
        "id": "male-qn-jingying",
        "name": "精英青年",
        "gender": "male",
        "style": "干练精英",
        "recommended_for": "都市精英男主",
    },
    {
        "id": "male-qn-jingying-jingpin",
        "name": "精英青年-精品",
        "gender": "male",
        "style": "高品质精英",
        "recommended_for": "重要精英角色",
    },
    {
        "id": "male-qn-badao",
        "name": "霸道青年",
        "gender": "male",
        "style": "强势霸道",
        "recommended_for": "霸总/反派角色",
    },
    {
        "id": "male-qn-badao-jingpin",
        "name": "霸道青年-精品",
        "gender": "male",
        "style": "高品质霸道",
        "recommended_for": "重要反派角色",
    },
    {
        "id": "male-qn-daxuesheng",
        "name": "青年大学生",
        "gender": "male",
        "style": "阳光学生",
        "recommended_for": "大学生角色",
    },
    {
        "id": "Chinese (Mandarin)_Gentleman",
        "name": "温润男声",
        "gender": "male",
        "style": "温润知性",
        "recommended_for": "成熟男主",
    },
    {
        "id": "Chinese (Mandarin)_Pure-hearted_Boy",
        "name": "清澈邻家弟弟",
        "gender": "male",
        "style": "清爽干净",
        "recommended_for": "少年主角",
    },
    {
        "id": "Chinese (Mandarin)_Unrestrained_Young_Man",
        "name": "不羁青年",
        "gender": "male",
        "style": "洒脱不羁",
        "recommended_for": "自由不羁角色",
    },
    {
        "id": "Chinese (Mandarin)_Humorous_Elder",
        "name": "搞笑大爷",
        "gender": "male",
        "style": "幽默慈和",
        "recommended_for": "长辈/大叔角色",
    },
    # -- 女角色音色（12 个）------------------------------------------------
    {
        "id": "female-shaonv",
        "name": "年轻少女",
        "gender": "female",
        "style": "清甜少女",
        "recommended_for": "少女主角",
    },
    {
        "id": "female-shaonv-jingpin",
        "name": "少女-精品",
        "gender": "female",
        "style": "高品质少女",
        "recommended_for": "重要少女角色",
    },
    {
        "id": "female-yujie",
        "name": "成熟御姐",
        "gender": "female",
        "style": "御姐成熟",
        "recommended_for": "成熟女角色",
    },
    {
        "id": "female-yujie-jingpin",
        "name": "御姐-精品",
        "gender": "female",
        "style": "精品御姐",
        "recommended_for": "重要女角色",
    },
    {
        "id": "female-tianmei",
        "name": "甜美女声",
        "gender": "female",
        "style": "甜美亲和",
        "recommended_for": "甜美女友",
    },
    {
        "id": "female-tianmei-jingpin",
        "name": "甜美-精品",
        "gender": "female",
        "style": "精品甜美",
        "recommended_for": "重要甜美女角色",
    },
    {
        "id": "Chinese (Mandarin)_Warm_Girl",
        "name": "温暖少女",
        "gender": "female",
        "style": "温暖治愈",
        "recommended_for": "治愈系女主",
    },
    {
        "id": "Chinese (Mandarin)_Warm_Bestie",
        "name": "温暖闺蜜",
        "gender": "female",
        "style": "温柔亲切",
        "recommended_for": "闺蜜角色",
    },
    {
        "id": "Chinese (Mandarin)_Mature_Woman",
        "name": "傲娇御姐",
        "gender": "female",
        "style": "端庄成熟",
        "recommended_for": "成熟女性角色",
    },
    {
        "id": "Chinese (Mandarin)_Sweet_Lady",
        "name": "甜美女声",
        "gender": "female",
        "style": "甜美端庄",
        "recommended_for": "淑女角色",
    },
    {
        "id": "Chinese (Mandarin)_Wise_Women",
        "name": "阅历姐姐",
        "gender": "female",
        "style": "知性理性",
        "recommended_for": "学者/职场女性",
    },
    {
        "id": "Chinese (Mandarin)_Kind-hearted_Elder",
        "name": "花甲奶奶",
        "gender": "female",
        "style": "慈祥温厚",
        "recommended_for": "长辈女性角色",
    },
)

#: 全部音色 ID（顺序同 :data:`MINIMAX_VOICES`）。
MINIMAX_VOICE_IDS: tuple[str, ...] = tuple(str(item["id"]) for item in MINIMAX_VOICES)

#: 推荐说书旁白音色 ID（5 个）。
MINIMAX_NARRATOR_VOICE_IDS: tuple[str, ...] = (
    "Chinese (Mandarin)_Radio_Host",
    "Chinese (Mandarin)_Male_Announcer",
    "Chinese (Mandarin)_Reliable_Executive",
    "Chinese (Mandarin)_News_Anchor",
    "Chinese (Mandarin)_Gentle_Senior",
)

#: 男角色音色 ID（11 个）。
MINIMAX_MALE_VOICE_IDS: tuple[str, ...] = (
    "male-qn-qingse",
    "male-qn-qingse-jingpin",
    "male-qn-jingying",
    "male-qn-jingying-jingpin",
    "male-qn-badao",
    "male-qn-badao-jingpin",
    "male-qn-daxuesheng",
    "Chinese (Mandarin)_Gentleman",
    "Chinese (Mandarin)_Pure-hearted_Boy",
    "Chinese (Mandarin)_Unrestrained_Young_Man",
    "Chinese (Mandarin)_Humorous_Elder",
)

#: 女角色音色 ID（12 个）。
MINIMAX_FEMALE_VOICE_IDS: tuple[str, ...] = (
    "female-shaonv",
    "female-shaonv-jingpin",
    "female-yujie",
    "female-yujie-jingpin",
    "female-tianmei",
    "female-tianmei-jingpin",
    "Chinese (Mandarin)_Warm_Girl",
    "Chinese (Mandarin)_Warm_Bestie",
    "Chinese (Mandarin)_Mature_Woman",
    "Chinese (Mandarin)_Sweet_Lady",
    "Chinese (Mandarin)_Wise_Women",
    "Chinese (Mandarin)_Kind-hearted_Elder",
)

#: 样例文件名里允许保留的字符；其余字符统一替换成 ``_``（MiniMax 音色 ID
#: 带括号与空格，如 ``Chinese (Mandarin)_Radio_Host`` →
#: ``Chinese__Mandarin__Radio_Host.wav``），避免路径穿越。
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_\- ]+")


def voice_sample_filename(voice_id: str) -> str:
    """MiniMax 音色 ID → 试听样例文件名（非法字符替换成下划线）。"""
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", voice_id.strip()).replace(" ", "_")
    return f"{cleaned or 'unknown'}.wav"


# ---------------------------------------------------------------------------
# 情绪映射
# ---------------------------------------------------------------------------

#: 情绪指令关键词 → MiniMax 安全枚举（**按顺序**匹配，先命中先算）。
#:
#: 严禁 ``whisper``：MiniMax 收到该枚举会让音频发闷失真。
_EMOTION_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("怒", "吼", "斥", "骂", "暴躁"), "angry"),
    (("哭", "悲", "伤心", "哽咽", "绝望", "凄凉"), "sad"),
    (("慌", "吓", "抖", "恐惧", "紧张", "窒息", "惊恐"), "fearful"),
    (("惊", "震", "瞪", "错愕"), "surprised"),
    (("笑", "喜", "开心", "激动", "兴奋"), "happy"),
    (("厌", "冷哼", "鄙夷", "恶心"), "disgusted"),
    (("沉稳", "冷静", "平稳", "低语", "轻声", "压低"), "calm"),
)


def _map_emotion(instruction: str) -> str | None:
    """自然语言情绪指令 → MiniMax 安全枚举；未命中返回 ``None``。

    未命中时**不传** ``emotion`` 字段，由 Speech 2.8 依据上下文自行抑扬顿挫。
    """
    text = (instruction or "").strip()
    if not text:
        return None
    for keywords, emotion in _EMOTION_RULES:
        for keyword in keywords:
            if keyword in text:
                return emotion
    return None


def _brief(text: str, limit: int = 300) -> str:
    """截断错误体，避免把整页 HTML 打进日志。"""
    cleaned = text.strip().replace("\n", " ")
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


def _locate_audio_payload(data: dict[str, Any]) -> str:
    """从响应体里取出音频负载（hex 串或下载 URL）。

    MiniMax ``t2a_v2`` 的**真实**响应结构是嵌套的::

        {"data": {"audio": "<hex>", "status": 2, "ced": ""},
         "extra_info": {...}, "trace_id": "...", "base_resp": {...}}

    音频在 ``data.data.audio``；早期文档只写了顶层 ``audio``，两处都兜住。
    """
    inner = data.get("data")
    if isinstance(inner, dict):
        nested = str(inner.get("audio") or "").strip()
        if nested:
            return nested
    return str(data.get("audio") or "").strip()


def _locate_sample_rate(data: dict[str, Any]) -> int:
    """从 ``extra_info`` 取真实采样率（缺省 32000，即请求里的 ``audio_setting``）。"""
    extra = data.get("extra_info")
    if isinstance(extra, dict):
        try:
            rate = int(extra.get("audio_sample_rate") or 0)
        except (TypeError, ValueError):
            rate = 0
        if rate > 0:
            return rate
    return 32000


def _voice_description(description: Any, fallback: str) -> str:
    """音色描述：MiniMax 的 ``description`` 是**字符串列表**，取第一条。"""
    if isinstance(description, str):
        return description.strip() or fallback
    if isinstance(description, (list, tuple)):
        for item in description:
            text = str(item).strip()
            if text:
                return text
    return fallback


# ---------------------------------------------------------------------------
# 合成器
# ---------------------------------------------------------------------------


class _RateLimiter:
    """MiniMax t2a_v2 的进程内 RPM 节流闸门（Patch v4.3）。

    MiniMax 对单账号的每分钟请求数有上限，超限会返回
    ``base_resp.status_code=1002 rate limit exceeded(RPM)``；继续猛打还会
    出现连接被断 / 超时。实测：连续 17 条成功后即触发限流。

    这里做两件事：

    1. **节流**：每分钟最多 ``rpm`` 次请求（令牌桶），主动不撞限；
    2. **自适应降速**：真撞到 1002 时立刻全局冷却并把速率减半，连续成功
       若干次后再缓慢加性恢复——避免多个 worker 一起退避又一起重试的惊群。
    """

    def __init__(
        self,
        rpm: int,
        *,
        min_rpm: int = 2,
        cooldown_s: float = 30.0,
    ) -> None:
        self._rpm = max(0, int(rpm))
        self._min_rpm = max(1, int(min_rpm))
        #: 恢复上限 = 初始速率（只在被降速后往回加，不超过配置值）。
        self._max_rpm = self._rpm
        self._cooldown_s = max(0.0, float(cooldown_s))
        self._interval = (60.0 / self._rpm) if self._rpm > 0 else 0.0
        self._next_allowed = 0.0
        self._cooldown_until = 0.0
        self._success_streak = 0
        self._lock: asyncio.Lock | None = None

    @property
    def rpm(self) -> int:
        """当前生效的每分钟请求上限（0 表示不限流）。"""
        return self._rpm

    @property
    def cooldown_remaining(self) -> float:
        """全局冷却剩余秒数（0 表示已解除）。"""
        return max(0.0, self._cooldown_until - time.monotonic())

    async def acquire(self) -> None:
        """占用一次请求额度；必要时等待到下一个允许发起的时刻。"""
        if self._rpm <= 0:
            return
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            now = time.monotonic()
            base = max(self._next_allowed, self._cooldown_until)
            self._next_allowed = max(now, self._next_allowed) + self._interval
            wait = base - now
        if wait > 0:
            await asyncio.sleep(wait)

    def penalize(self) -> None:
        """撞到限流：全局冷却 + 速率减半。"""
        now = time.monotonic()
        self._cooldown_until = max(self._cooldown_until, now + self._cooldown_s)
        if self._rpm > 0:
            self._rpm = max(self._min_rpm, self._rpm // 2)
            self._interval = 60.0 / self._rpm
        self._success_streak = 0

    def on_success(self) -> None:
        """成功一次：连续若干次后缓慢恢复速率（加性增）。"""
        self._success_streak += 1
        if self._rpm <= 0 or self._success_streak < 10:
            return
        self._success_streak = 0
        if self._rpm < self._max_rpm:
            self._rpm += 1
            self._interval = 60.0 / self._rpm


class MiniMaxTTS:
    """MiniMax Speech 2.8 合成器（``speech-2.8-hd`` / ``speech-2.8-turbo``）。"""

    #: 原生支持语速：Stage 5 命中该标记时把 speed 交给服务端，
    #: **不再**用 ffmpeg ``atempo`` 二次变速（避免音色被拉糊）。
    supports_native_speed: bool = True

    #: Patch v4.3：要求 Stage 5 严格串行调度（一次只允许一条 beat 在途）。
    #: MiniMax 对并发与瞬时速率极敏感：多协程排队时，一次限流重试的长冷却
    #: 会让后续请求的节流时隙脱节、冷却结束后突发连打，引发 1002 雪崩。
    #: 串行 + provider 内 RPM 闸门与时隙始终和真实时间同步，实测 12 RPM 可稳定跑完。
    serial_requests: bool = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        group_id: str | None = None,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        timeout_s: float = 120.0,
        max_retries: int = 3,
        retry_backoff_s: float = 1.0,
        rate_limit_rpm: int = 20,
        rate_limit_min_rpm: int = 5,
        rate_limit_cooldown_s: float = 90.0,
        max_concurrency: int = 1,
        env: dict[str, str] | None = None,
        env_file: str | None = None,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        group_id_env: str = DEFAULT_GROUP_ID_ENV,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url or DEFAULT_BASE_URL
        self._group_id = group_id
        self._model = model
        self._voice = voice
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._retry_backoff_s = retry_backoff_s
        self._env = env
        self._env_file = env_file
        self._api_key_env = api_key_env
        self._group_id_env = group_id_env
        self._http_client = http_client
        self._sleep = sleep
        # Patch v4.3：MiniMax 有 RPM 限流，这里加进程内节流闸门。
        self._limiter = _RateLimiter(
            rate_limit_rpm,
            min_rpm=rate_limit_min_rpm,
            cooldown_s=rate_limit_cooldown_s,
        )
        # Patch v4.3：同一 client 的在途请求上限（默认串行）。
        self._max_concurrency = max(1, int(max_concurrency))
        self._semaphore: asyncio.Semaphore | None = None

    # -- 只读属性 ---------------------------------------------------------
    @property
    def model(self) -> str:
        """当前 TTS 模型名（供上层做跨模型音色兜底）。"""
        return self._model

    @property
    def voice(self) -> str:
        """当前默认音色 ID。"""
        return self._voice

    @property
    def base_url(self) -> str:
        return self._base_url

    # -- 内部工具 ---------------------------------------------------------
    def _resolve_key(self) -> str:
        if self._api_key:
            return self._api_key.strip()
        try:
            return resolve_minimax_api_key_with_env_file(
                env=self._env,
                env_file=self._env_file,
                env_name=self._api_key_env,
            )
        except ConfigError as exc:  # pragma: no cover - 交由调用方给出可读信息
            raise TTSError(f"缺少 {self._api_key_env}：{exc}") from exc

    def _resolve_group_id(self) -> str:
        if self._group_id:
            return str(self._group_id).strip()
        return resolve_minimax_group_id(
            env=self._env,
            env_file=self._env_file,
            env_name=self._group_id_env,
        )

    def _endpoint(self) -> str:
        url = f"{self._base_url.rstrip('/')}/t2a_v2"
        group_id = self._resolve_group_id()
        if group_id:
            url = f"{url}?GroupId={group_id}"
        return url

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is not None:
            return self._http_client
        return build_client(referer=None, timeout=self._timeout_s)

    # -- 合成 -------------------------------------------------------------
    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str | None = None,
        language_type: str = "Chinese",
        speed: float = 1.0,
    ) -> TTSResult:
        """合成一句语音，返回音频字节。

        ``instruction`` 是情绪指令：这里**不**原样透传，而是映射成 MiniMax 的
        安全离散枚举（未命中则不传该字段）。
        """
        if not text.strip():
            raise TTSRequestError("待合成文本为空")

        effective_voice = voice or self._voice
        payload: dict[str, Any] = {
            "model": self._model,
            "text": text,
            "stream": False,
            "language_boost": "Chinese",
            "output_format": "hex",
            "voice_setting": {
                "voice_id": effective_voice,
                "speed": round(max(0.5, min(2.0, float(speed))), 2),
                "vol": 1.0,
                "pitch": 0,
            },
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": "wav",
                "channel": 1,
            },
        }
        emotion = _map_emotion(instruction)
        if emotion:
            payload["emotion"] = emotion

        headers = {
            "Authorization": f"Bearer {self._resolve_key()}",
            "Content-Type": "application/json",
        }
        client = self._client()
        url = self._endpoint()

        # Patch v4.3：同一 client 的在途请求数硬上限（默认 1）。
        # MiniMax 对并发极敏感：多条请求同时在途会触发 1002，甚至连接被断；
        # 上层给的并发度在这里统一退化为串行，再由 RPM 闸门控速。
        async with self._request_slot():
            return await self._synthesize_with_retries(
                client, url, headers, payload, effective_voice
            )

    def _request_slot(self) -> asyncio.Semaphore:
        """返回并发闸门（懒创建，避免跨事件循环复用）。"""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(max(1, int(self._max_concurrency)))
        return self._semaphore

    async def _synthesize_with_retries(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        effective_voice: str,
    ) -> TTSResult:
        """在并发闸门内执行请求 + 退避重试（Patch v4.3）。"""
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            # Patch v4.3：先过 RPM 闸门（限流时会被推迟到允许发起的时刻）。
            await self._limiter.acquire()
            rate_limited = False
            try:
                response = await client.post(url, headers=headers, json=payload)
            except httpx.TimeoutException as exc:
                last_error = TTSError(
                    f"MiniMax TTS 请求超时（第 {attempt + 1} 次，timeout={self._timeout_s}s）："
                    f"{type(exc).__name__}"
                )
            except httpx.HTTPError as exc:
                last_error = TTSError(
                    f"MiniMax TTS 请求失败（第 {attempt + 1} 次）：{type(exc).__name__}: {exc}"
                )
            else:
                if response.status_code == 200:
                    data = self._parse_response(response)
                    base_resp = data.get("base_resp") or {}
                    code = int(base_resp.get("status_code", 0) or 0)
                    if code == 0:
                        self._limiter.on_success()
                        return await self._extract_audio(client, data, effective_voice)
                    message = str(base_resp.get("status_msg") or "").strip()
                    if code in RETRYABLE_CODES:
                        # 可重试错误码（限流/繁忙/上游临时故障）：退避后重试。
                        if code == RATE_LIMIT_CODE:
                            # 撞到 RPM 限流：全局冷却 + 降速，避免继续惊群。
                            rate_limited = True
                            self._limiter.penalize()
                        last_error = TTSError(
                            f"MiniMax TTS 返回可重试错误码 {code}"
                            f"（第 {attempt + 1} 次）：{message or _brief(response.text)}"
                        )
                    else:
                        raise TTSRequestError(
                            _fatal_message(
                                code,
                                message or _brief(response.text),
                                effective_voice,
                            )
                        )
                elif response.status_code in RETRYABLE_STATUS:
                    last_error = TTSError(
                        f"MiniMax TTS 返回可重试状态码 {response.status_code}"
                        f"（第 {attempt + 1} 次）：{_brief(response.text)}"
                    )
                else:
                    raise TTSRequestError(
                        f"MiniMax TTS 请求被拒绝（HTTP {response.status_code}）："
                        f"{_brief(response.text)}"
                    )

            if attempt < self._max_retries:
                # Patch v4.3：退避加抖动；撞到限流时额外等全局冷却结束。
                delay = self._retry_backoff_s * (2**attempt)
                delay += random.uniform(0.0, max(0.1, delay * 0.3))
                if rate_limited:
                    delay += self._limiter.cooldown_remaining
                await self._sleep(delay)

        raise TTSError(f"MiniMax TTS 重试耗尽：{last_error}")

    def _parse_response(self, response: httpx.Response) -> dict[str, Any]:
        """解析 ``t2a_v2`` 的 JSON 响应体。"""
        try:
            data = response.json()
        except ValueError as exc:
            raise TTSResponseError(
                f"MiniMax TTS 响应不是合法 JSON：{_brief(response.text)}"
            ) from exc
        if not isinstance(data, dict):
            raise TTSResponseError(
                f"MiniMax TTS 响应结构异常：{_brief(response.text)}"
            )
        return data

    async def _extract_audio(
        self,
        client: httpx.AsyncClient,
        data: dict[str, Any],
        voice: str,
    ) -> TTSResult:
        """从成功响应里取出音频字节（hex 或可下载 URL）。"""
        raw = _locate_audio_payload(data)
        if not raw:
            raise TTSResponseError(f"MiniMax TTS 响应里没有音频：{_brief(str(data))}")
        if raw.startswith("http"):
            download = await client.get(raw)
            if download.status_code != 200:
                raise TTSRequestError(
                    f"下载 MiniMax TTS 音频失败（HTTP {download.status_code}）："
                    f"{_brief(download.text)}"
                )
            return TTSResult(
                audio=download.content,
                content_type="audio/wav",
                sample_rate=_locate_sample_rate(data),
                voice=voice,
            )
        try:
            audio = bytes.fromhex(raw)
        except ValueError as exc:
            raise TTSResponseError(f"MiniMax TTS 音频 hex 解码失败：{_brief(raw)}") from exc
        return TTSResult(
            audio=audio,
            content_type="audio/wav",
            sample_rate=_locate_sample_rate(data),
            voice=voice,
        )

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()

    async def __aenter__(self) -> "MiniMaxTTS":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


# ---------------------------------------------------------------------------
# 账户音色拉取
# ---------------------------------------------------------------------------


async def fetch_minimax_voices(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    group_id: str | None = None,
    env: dict[str, str] | None = None,
    env_file: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    timeout_s: float = 30.0,
) -> list[dict[str, Any]]:
    """拉取账户可用音色（含克隆音色）；失败时优雅降级回静态目录。

    返回条目结构与 :data:`MINIMAX_VOICES` 一致，并额外带 ``is_cloned`` /
    ``category`` 字段（克隆音色 ``is_cloned=True``、``category="cloned"``）。
    """
    resolved_base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    resolved_group = (
        str(group_id).strip()
        if group_id
        else resolve_minimax_group_id(env=env, env_file=env_file)
    )
    url = f"{resolved_base}/get_voice"
    if resolved_group:
        url = f"{url}?GroupId={resolved_group}"

    try:
        key = (
            api_key.strip()
            if api_key
            else resolve_minimax_api_key_with_env_file(env=env, env_file=env_file)
        )
    except ConfigError:
        return [dict(item) for item in MINIMAX_VOICES]

    client = http_client
    owns_client = client is None
    try:
        if client is None:
            client = build_client(referer=None, timeout=timeout_s)
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={"voice_type": "all"},
        )
        if response.status_code != 200:
            return [dict(item) for item in MINIMAX_VOICES]
        data = response.json()
    except Exception:  # noqa: BLE001 - 拉取失败必须降级，不能影响主流程
        return [dict(item) for item in MINIMAX_VOICES]
    finally:
        if owns_client and client is not None:
            await client.aclose()

    voices: list[dict[str, Any]] = [dict(item) for item in MINIMAX_VOICES]
    known = {str(item["id"]) for item in voices}
    for raw in data.get("system_voice") or []:
        voice_id = str(raw.get("voice_id") or "").strip()
        if not voice_id or voice_id in known:
            continue
        known.add(voice_id)
        voices.append(
            {
                "id": voice_id,
                "name": str(raw.get("voice_name") or voice_id),
                "gender": str(raw.get("gender") or "unknown"),
                "style": _voice_description(raw.get("description"), "系统音色"),
                "recommended_for": "系统音色",
                "is_cloned": False,
                "category": "system",
            }
        )
    for bucket in ("voice_cloning", "voice_generation"):
        for raw in data.get(bucket) or []:
            voice_id = str(raw.get("voice_id") or "").strip()
            if not voice_id or voice_id in known:
                continue
            known.add(voice_id)
            voices.append(
                {
                    "id": voice_id,
                    "name": str(raw.get("voice_name") or voice_id),
                    "gender": str(raw.get("gender") or "unknown"),
                    "style": _voice_description(raw.get("description"), "克隆音色"),
                    "recommended_for": "自定义克隆音色",
                    "is_cloned": True,
                    "category": "cloned",
                }
            )
    return voices
