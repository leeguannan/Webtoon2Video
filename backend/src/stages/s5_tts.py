"""Stage 5：TTS 语音合成（规范 M4-T2）。

职责边界：把 Stage 4 的 ``NarrationScript`` 变成 ``05_audio/`` 下的音频片段与清单。

铁律（与 :mod:`core.schemas` 的锁定契约一致）：

* **字幕与读音双文本分离**。屏幕字幕用的是 ``narration_text``，送进 TTS 的是
  ``spoken_text``（``tts_text`` + 多音字替换）。本模块**只**改写送音文本，
  ``narration_text`` 一个字都不动，也不会写回 ``04_script/script.json``。
* **情绪指令原样透传**。``emotion_instruction`` 直接作为 ``instruction`` 传给合成器，
  不改写、不追加情绪词（模型自己写的情绪指令比模板更贴题）。

静音裁剪：用系统 ``ffmpeg`` 的 ``silenceremove`` 滤镜裁掉首尾静音，
内部停顿**必须保留**（解说稿句间的留白是节奏的一部分）。
实测（ffmpeg 9.0.1）：``silenceremove`` 的 ``stop_periods=1`` 会把音频截断成
「最后一段非静音」，中间的停顿全部丢失；因此本模块采用 ``areverse`` 链式方案：
正向裁头、反向裁尾、再反回来。裁剪失败**不会**让整话失败——保留原音频并记录问题。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.config import SeriesTTSConfig, VoiceSpec
from core.schemas import (
    AudioClipMeta,
    CharacterProfile,
    NarrationScript,
    ScriptBeat,
    StoryBibleV2,
    load_model,
)
from providers.tts_base import TTSResult
from providers.tts_qwen import DEFAULT_VOICE

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIO_DIR_NAME",
    "AUDIO_MANIFEST_FILENAME",
    "AUDIO_ISSUES_FILENAME",
    "MAX_SPEED",
    "MIN_SPEED",
    "SILENCE_TRIM_FILTER",
    "TTSStageError",
    "apply_pronunciation",
    "audio_dir_for",
    "build_spoken_text",
    "load_audio_manifest",
    "load_bible_characters",
    "normalize_speed",
    "resolve_beat_voice",
    "resolve_beat_voice_spec",
    "run_stage5",
    "synthesize_episode",
]

#: ``05_audio`` 目录名（与 :data:`core.manifest.STAGE_DIR_NAMES` 保持一致）。
AUDIO_DIR_NAME = "05_audio"

#: 音频清单文件名（JSON 数组，元素为 :class:`AudioClipMeta`）。
AUDIO_MANIFEST_FILENAME = "audio_manifest.json"

#: 降级问题记录（裁剪失败等），单独成文件便于 CLI/Web 展示，不污染音频清单契约。
AUDIO_ISSUES_FILENAME = "audio_issues.json"

#: ``pause_after_ms`` 缺省值（与 :class:`core.schemas.ScriptBeat` 的默认值一致）。
DEFAULT_PAUSE_AFTER_MS = 250

#: 静音裁剪滤镜。
#:
#: 为什么用 ``areverse`` 链而不是一次 ``silenceremove`` 同时写 ``start_*``/``stop_*``：
#: ffmpeg 的 ``silenceremove`` 在 ``stop_periods=1`` 时的实际行为是「只保留最后一段
#: 非静音」，句中停顿会被整段丢弃——实测 1.9s 的「静音+音A+静音+音B+静音」被裁成
#: 0.557s 的「音B+尾静音」。因此头尾各用一次 ``start_periods=1``（第二次作用在
#: 反向流上），只动两端，内部停顿原样保留。
#:
#: 头端 ``start_silence=0``：完全不保留前导静音，裁完的第一个采样就是人声，
#: 时间轴对齐更准；阈值 ``-50dB`` 极低，不会切掉可听发音。
#: 尾端 ``start_silence=0.05``：反向流里保留 50ms 缓冲，避免句末被硬切出现爆音。
SILENCE_TRIM_FILTER = (
    "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0:detection=peak,"
    "areverse,"
    "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05:detection=peak,"
    "areverse"
)

#: ffmpeg 子进程超时（秒）。
FFMPEG_TIMEOUT_S = 120.0

#: 语速倍率的安全区间（Patch v3.2 Patch 13 B3）。
#:
#: ``atempo`` 滤镜在 0.5 ~ 100 之间都不失真，但 0.6 以下听感已明显拖沓、
#: 1.8 以上会明显失真，因此配置层与运行层都锁在这个区间。
MIN_SPEED = 0.6
MAX_SPEED = 1.8

#: 旧槽位 → 性别（Patch v3.2 Patch 13 B2 的兜底推断）。
_SLOT_GENDER: dict[str, str] = {
    "male": "male",
    "male_lead": "male",
    "male_antagonist": "male",
    "female": "female",
    "female_lead": "female",
    "female_mature": "female",
}

#: 旁白角色名（大小写无关）。
_NARRATOR_ROLES = frozenset({"narrator", "旁白", "解说"})


def normalize_speed(speed: float | int | None) -> float:
    """把语速倍率收敛到 ``[MIN_SPEED, MAX_SPEED]``；空值按原速 1.0 处理。"""
    if speed is None:
        return 1.0
    try:
        value = float(speed)
    except (TypeError, ValueError):
        return 1.0
    if value <= 0:
        return 1.0
    return max(MIN_SPEED, min(MAX_SPEED, value))


class TTSStageError(RuntimeError):
    """Stage 5 执行失败（剧本缺失、全部合成失败、清单非法等）。"""


# ----------------------------------------------------------------------
# 文本：双文本分离的唯一入口
# ----------------------------------------------------------------------
def apply_pronunciation(text: str, overrides: Mapping[str, str] | None) -> str:
    """把发音词典套到**送音文本**上。

    为什么要按 key 长度从长到短替换：短 key 往往是长 key 的前缀
    （「重」与「重霄」），先替换短的会把长的彻底破坏掉，长 key 优先才能保证
    「重霄」被完整命中。

    本函数只返回新字符串（``str`` 不可变），绝不修改入参，也绝不触碰字幕文本。
    """
    if not text or not overrides:
        return text
    result = text
    for key in sorted(overrides, key=len, reverse=True):
        value = overrides[key]
        if not key or not value or key not in result:
            continue
        result = result.replace(key, value)
    return result


#: ``tts_text`` 相对 ``narration_text`` 的长度占比下限。
#:
#: Stage 4 的 ``tts_text`` 契约是**整句送音文本**（与 ``narration_text`` 等量级，
#: 仅个别脱敏字被换成标准汉字同音词）。若它明显短于字幕原句，说明模型写的是
#: 「被替换的那一个词」（如「混蛋」）而不是整句——此时若直接拿它去合成，整句解说
#: 会退化成两三个字，画面与字幕随音频时长一起塌缩，后面直接跳过
#: （实测 wt_5081 ep_001 的 b33：字幕 35 字，音频只剩 0.35s，时间轴只有 0.96s）。
#:
#: 因此低于该占比、或只是字幕原句的子串时，一律回退字幕原句。
TTS_FRAGMENT_RATIO = 0.6


def build_spoken_text(beat: ScriptBeat, overrides: Mapping[str, str] | None = None) -> str:
    """构造某个 beat 的送音文本。

    ``tts_text`` 是 Stage 4 写好的**整句配音文本**，只有当它与字幕原句等量级
    （见 :data:`TTS_FRAGMENT_RATIO`）时才用它替代字幕原句；模型偶尔只写「被替换
    的那个词」（如「混蛋」），那不是整句，直接拿去合成会让整条解说只剩两三个字，
    此时回退字幕原句再套发音词典。字幕用的 ``narration_text`` 在这里**只读**。
    """
    narration = beat.narration_text
    tts_text = (beat.tts_text or "").strip()
    spoken = narration
    if (
        tts_text
        and tts_text not in narration
        and len(tts_text) >= len(narration) * TTS_FRAGMENT_RATIO
    ):
        spoken = tts_text
    elif tts_text and tts_text != narration:
        logger.warning(
            "[s5_tts] %s：tts_text 不是整句送音文本（%d 字 vs 字幕 %d 字），已回退字幕原句",
            beat.beat_id,
            len(tts_text),
            len(narration),
        )
    return apply_pronunciation(spoken, overrides)


def audio_dir_for(series_dir: str | Path, episode_no: int) -> Path:
    """``05_audio`` 目录路径：``series_dir/episodes/ep_{no:03d}/05_audio``。"""
    return Path(series_dir) / "episodes" / f"ep_{episode_no:03d}" / AUDIO_DIR_NAME


def safe_beat_id(beat_id: str) -> str:
    """把 ``beat_id`` 安全化为文件名片段。

    清单里仍然保留原始 ``beat_id``（契约字段），只有文件名做安全化，
    避免 ``/``、空格等字符把音频写到别的目录里去。
    """
    cleaned = re.sub(r"[^\w\-]+", "_", (beat_id or "").strip(), flags=re.UNICODE)
    cleaned = cleaned.strip("_")
    return cleaned or "beat"


# ----------------------------------------------------------------------
# 音频处理：ffmpeg 静音裁剪与时长探测
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class _Issue:
    """一条降级/失败记录。"""

    beat_id: str
    stage: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"beat_id": self.beat_id, "stage": self.stage, "message": self.message}


def _resolve_binary(name: str) -> str | None:
    """定位可执行文件：先按绝对/相对路径找，再查 ``PATH``。"""
    if not name:
        return None
    candidate = Path(name).expanduser()
    if candidate.is_file() and candidate.stat().st_mode & 0o111:
        return str(candidate)
    return shutil.which(name)


def _ffprobe_bin(ffmpeg_bin: str) -> str | None:
    """由 ``ffmpeg_bin`` 推导 ``ffprobe`` 路径（同目录优先，否则查 ``PATH``）。"""
    candidate = Path(ffmpeg_bin).expanduser()
    if candidate.is_file():
        sibling = candidate.with_name("ffprobe")
        if sibling.is_file():
            return str(sibling)
    return shutil.which("ffprobe")


async def _run_command(cmd: Sequence[str], *, timeout: float = FFMPEG_TIMEOUT_S) -> tuple[int, str, str]:
    """执行外部命令，返回 ``(退出码, stdout, stderr)``；启动失败不抛异常。"""
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:  # 命令不存在 / 不可执行
        return -1, "", f"命令启动失败：{type(exc).__name__}: {exc}"
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return -1, "", f"命令超时（{timeout}s）：{' '.join(cmd)}"
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _wav_duration_ms(path: Path) -> int | None:
    """用 ``wave`` 模块读 WAV 帧数算时长；不是合法 WAV 时返回 ``None``。"""
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            frames = handle.getnframes()
    except (wave.Error, EOFError, OSError):
        return None
    if not rate:
        return None
    return int(round(frames * 1000.0 / rate))


async def _probe_duration_ms(path: Path, *, ffmpeg_bin: str = "ffmpeg") -> int:
    """读取音频真实时长（毫秒）。

    先用 ``wave`` 直读（WAV 最快、不依赖外部进程），失败再退回 ``ffprobe``，
    两者都拿不到时返回 0（由调用方记为问题）。
    """
    if path.suffix.lower() == ".wav":
        duration = _wav_duration_ms(path)
        if duration is not None:
            return duration
    ffprobe = _ffprobe_bin(ffmpeg_bin)
    if ffprobe:
        code, stdout, stderr = await _run_command(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ]
        )
        if code == 0:
            try:
                return int(round(float(stdout.strip()) * 1000))
            except ValueError:
                pass
        elif stderr.strip():
            logger.warning("ffprobe 读取时长失败（%s）：%s", path.name, stderr.strip()[:300])
    return 0


async def _trim_silence(
    src: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
    timeout: float = FFMPEG_TIMEOUT_S,
    speed: float = 1.0,
) -> tuple[Path | None, str | None]:
    """裁掉首尾静音（并按需变速），返回 ``(处理后文件, 问题描述)``。

    失败时返回 ``(None, 原因)``，调用方保留原音频——单条裁剪失败不允许拖垮整话。

    Patch v3.2 Patch 13 B3：变速用 ffmpeg ``atempo`` 滤镜（不变调、不发尖），
    并**合并进同一条滤镜链**（裁剪 + 变速只跑一次 ffmpeg，零额外子进程开销）。
    """
    ffmpeg = _resolve_binary(ffmpeg_bin)
    if not ffmpeg:
        return None, f"未找到 ffmpeg 可执行文件（{ffmpeg_bin}），跳过静音裁剪"

    normalized_speed = normalize_speed(speed)
    audio_filter = SILENCE_TRIM_FILTER
    if abs(normalized_speed - 1.0) > 1e-6:
        audio_filter = f"{audio_filter},atempo={normalized_speed:.4f}".rstrip("0").rstrip(".")

    tmp_path = src.with_name(f".trim_{src.stem}.wav")
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-af",
        audio_filter,
        "-c:a",
        "pcm_s16le",
        str(tmp_path),
    ]
    code, _stdout, stderr = await _run_command(cmd, timeout=timeout)
    if code != 0:
        tmp_path.unlink(missing_ok=True)
        return None, f"静音裁剪失败（ffmpeg 退出码 {code}）：{stderr.strip()[:300] or '无错误输出'}"
    if not tmp_path.is_file() or tmp_path.stat().st_size <= 44:
        # 全静音输入会被裁成空音频：这种情况保留原音频。
        tmp_path.unlink(missing_ok=True)
        return None, "静音裁剪结果为空（全静音？），保留原音频"

    if src.suffix.lower() == ".wav":
        tmp_path.replace(src)
        return src, None
    # 非 WAV 来源：裁剪结果是 PCM WAV，直接作为正式产物（原始文件删除）。
    final_path = src.with_suffix(".wav")
    tmp_path.replace(final_path)
    src.unlink(missing_ok=True)
    return final_path, None


# ----------------------------------------------------------------------
# 合成
# ----------------------------------------------------------------------
def resolve_beat_voice(
    beat: ScriptBeat,
    *,
    default_voice: str | None = None,
    voice_cast: Mapping[str, str] | None = None,
) -> str | None:
    """按 ``beat.speaker_role`` 查角色音色表（Patch v3.1 Patch 11 A3）。

    查表顺序：

    1. 角色名精确匹配 ``voice_cast``；
    2. 角色名小写后匹配（容忍 ``Male_Lead`` / ``male_lead`` 混写）；
    3. 都没命中 → 回退 ``voice_cast["narrator"]``，再回退 ``default_voice``。

    ``voice_cast`` 为空时不改变既有行为（返回 ``default_voice`` 或 ``None``，
    由 provider 决定默认音色）。
    """
    cast = {
        str(key): str(value)
        for key, value in (voice_cast or {}).items()
        if str(key).strip() and str(value).strip()
    }
    role = str(getattr(beat, "speaker_role", "") or "").strip()
    if role and cast:
        if role in cast:
            return cast[role]
        lowered = role.lower()
        for key, value in cast.items():
            if key.lower() == lowered:
                return value
    fallback = cast.get("narrator") or default_voice
    return fallback or None


def _character_gender_index(
    characters: Sequence[CharacterProfile] | None,
) -> dict[str, str]:
    """角色名/别名 → 性别（小写键）。"""
    index: dict[str, str] = {}
    for character in characters or ():
        gender = str(getattr(character, "gender", "") or "unknown").lower()
        if gender not in {"male", "female"}:
            continue
        keys = [getattr(character, "name", "")] + list(getattr(character, "aliases", []) or [])
        for key in keys:
            cleaned = str(key or "").strip().lower()
            if cleaned:
                index[cleaned] = gender
    return index


def _resolve_gender(
    beat: ScriptBeat,
    *,
    gender_index: Mapping[str, str],
) -> str:
    """按 ``beat.speaker_gender`` → 角色表 → 旧槽位推断说话人性别。"""
    explicit = str(getattr(beat, "speaker_gender", "") or "").lower()
    if explicit in {"male", "female"}:
        return explicit
    role = str(getattr(beat, "speaker_role", "") or "").strip()
    if role:
        found = gender_index.get(role.lower())
        if found:
            return found
        slot = _SLOT_GENDER.get(role.lower())
        if slot:
            return slot
    return "unknown"


def resolve_beat_voice_spec(
    beat: ScriptBeat,
    *,
    series_tts: SeriesTTSConfig | None = None,
    characters: Sequence[CharacterProfile] | None = None,
    default_voice: str | None = None,
    voice_cast: Mapping[str, str] | None = None,
) -> VoiceSpec:
    """四级解析「音色 + 语速」（Patch v3.2 Patch 13 B2）。

    解析顺序：

    1. ``beat.speaker_role`` 是旁白（``narrator`` / ``旁白``）→ ``series_tts.narrator``；
    2. ``series_tts.character_voices`` 命中该角色（精确 → 别名 → 忽略大小写）
       → 该角色的专属音色；
    3. 按性别兜底：``male`` → ``series_tts.default_male``，``female`` →
       ``series_tts.default_female``（性别取自 ``beat.speaker_gender``、
       StoryBible 角色表或旧槽位）；
    4. 仍判不出 → 回退 ``series_tts.narrator``。

    ``series_tts`` 为 ``None``（没配作品级模板）时退化为 Patch v3.1 的旧行为：
    只按 ``voice_cast`` 查音色、语速恒为 1.0（原速，不改变既有产物）。
    """
    role = str(getattr(beat, "speaker_role", "") or "").strip()
    lowered = role.lower()

    if series_tts is None:
        voice = resolve_beat_voice(beat, default_voice=default_voice, voice_cast=voice_cast)
        return VoiceSpec(voice=voice or DEFAULT_VOICE, speed=1.0)

    if lowered in _NARRATOR_ROLES or role in _NARRATOR_ROLES:
        return series_tts.narrator

    # 2) 已识别角色的专属音色：精确 → 忽略大小写。
    if role and role in series_tts.character_voices:
        return series_tts.character_voices[role]
    if lowered:
        for name, spec in series_tts.character_voices.items():
            if str(name).strip().lower() == lowered:
                return spec

    gender = _resolve_gender(beat, gender_index=_character_gender_index(characters))

    # 3) 按性别兜底；若旧音色表里有该角色，优先沿用旧表里的音色 ID。
    if gender in {"male", "female"}:
        spec = series_tts.default_male if gender == "male" else series_tts.default_female
        legacy_voice = None
        cast = {str(k): str(v) for k, v in (voice_cast or {}).items()}
        if role and role in cast:
            legacy_voice = cast[role]
        elif lowered and lowered in cast:
            legacy_voice = cast[lowered]
        if legacy_voice:
            return VoiceSpec(voice=legacy_voice, speed=spec.speed)
        return spec

    # 4) 判不出性别 → 回退旁白。
    return series_tts.narrator


async def _synthesize_beat(
    client: Any,
    beat: ScriptBeat,
    *,
    audio_dir: Path,
    overrides: Mapping[str, str] | None,
    voice: str | None,
    default_voice: str | None = None,
    voice_cast: Mapping[str, str] | None = None,
    series_tts: SeriesTTSConfig | None = None,
    characters: Sequence[CharacterProfile] | None = None,
    ffmpeg_bin: str,
    trim_silence: bool,
) -> tuple[AudioClipMeta | None, list[_Issue]]:
    """合成单个 beat 的音频，返回 ``(清单项, 问题列表)``。"""
    issues: list[_Issue] = []
    spoken = build_spoken_text(beat, overrides)
    if not spoken.strip():
        issues.append(_Issue(beat.beat_id, "synthesize", "送音文本为空，已跳过"))
        return None, issues

    extra: dict[str, Any] = {}
    # 显式指定的音色优先（命令行 ``--voice`` / Web 单音覆盖）。
    # 没有作品级模板（series_tts=None）时保留 Patch v3.1 旧契约：只查
    # voice_cast，未命中返回 None，由 provider 自己决定默认音色（不硬塞）。
    # 有作品级模板时走四级音色 + 语速解析（Patch v3.2 Patch 13 B2）。
    speed = 1.0
    if voice:
        beat_voice: str | None = voice
    elif series_tts is None:
        beat_voice = resolve_beat_voice(
            beat, default_voice=default_voice, voice_cast=voice_cast
        )
    else:
        spec = resolve_beat_voice_spec(
            beat,
            series_tts=series_tts,
            characters=characters,
            default_voice=default_voice,
            voice_cast=voice_cast,
        )
        beat_voice = spec.voice
        speed = normalize_speed(spec.speed)
    if beat_voice:
        extra["voice"] = beat_voice
        logger.info(
            "[s5_tts] %s：speaker_role=%s → 音色 %s（语速 %.2fx）",
            beat.beat_id,
            getattr(beat, "speaker_role", "") or "narrator",
            beat_voice,
            speed,
        )
    try:
        result: TTSResult = await client.synthesize(
            spoken,
            instruction=beat.emotion_instruction,
            **extra,
        )
    except Exception as exc:  # 单条失败不影响其他 beat
        issues.append(
            _Issue(beat.beat_id, "synthesize", f"合成失败：{type(exc).__name__}: {exc}")
        )
        return None, issues

    audio = getattr(result, "audio", b"") or b""
    if not audio:
        issues.append(_Issue(beat.beat_id, "synthesize", "合成结果为空音频"))
        return None, issues

    suffix = getattr(result, "suffix", None) or ".wav"
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    raw_path = audio_dir / f"beat_{safe_beat_id(beat.beat_id)}{suffix}"
    raw_path.write_bytes(audio)

    final_path = raw_path
    if trim_silence:
        # 静音裁剪与变速合并在同一条 ffmpeg 滤镜链里（Patch v3.2 Patch 13 B3）：
        # 一次子进程同时完成「去首尾静音」+「atempo 不变调变速」。
        trimmed, problem = await _trim_silence(
            raw_path, ffmpeg_bin=ffmpeg_bin, speed=speed
        )
        if trimmed is not None:
            final_path = trimmed
        else:
            issues.append(_Issue(beat.beat_id, "trim_silence", problem or "静音裁剪失败"))

    duration_ms = await _probe_duration_ms(final_path, ffmpeg_bin=ffmpeg_bin)
    if duration_ms <= 0:
        issues.append(_Issue(beat.beat_id, "probe", f"无法读取音频时长：{final_path.name}"))

    meta = AudioClipMeta(
        beat_id=beat.beat_id,
        audio_path=final_path.name,
        duration_ms=duration_ms,
        pause_after_ms=(
            beat.pause_after_ms if beat.pause_after_ms is not None else DEFAULT_PAUSE_AFTER_MS
        ),
        # qwen3-tts-flash 非流式不返回词级时间戳，契约里就留空，
        # 避免下游拿到不完整的时间轴当成对齐依据。
        word_timestamps=[],
    )
    return meta, issues


def load_bible_characters(series_dir: str | Path) -> list[CharacterProfile]:
    """读 ``story_bible.json`` 里已登记的角色（含性别），读不到就返回空列表。

    Patch v3.2 Patch 13 B2：角色性别用于「未指定角色 → 按性别选通用音色」。
    """
    path = Path(series_dir) / "story_bible.json"
    if not path.is_file():
        return []
    try:
        bible = load_model(StoryBibleV2, path)
    except Exception as exc:  # 记忆体损坏不应拖垮配音
        logger.warning("[s5_tts] 读取角色表失败（%s）：%s", path, exc)
        return []
    return list(bible.characters)


async def synthesize_episode(
    client: Any,
    *,
    script: NarrationScript,
    audio_dir: Path,
    overrides: Mapping[str, str] | None = None,
    voice: str | None = None,
    default_voice: str | None = None,
    voice_cast: Mapping[str, str] | None = None,
    series_tts: SeriesTTSConfig | None = None,
    characters: Sequence[CharacterProfile] | None = None,
    concurrency: int = 4,
    ffmpeg_bin: str = "ffmpeg",
    trim_silence: bool = True,
) -> list[AudioClipMeta]:
    """并发合成整话音频，返回音频清单（顺序与剧本 beats 一致）。

    * 单条 beat 失败只记录问题并跳过；**全部**失败才抛 :class:`TTSStageError`。
    * ``overrides`` 是发音词典，只作用于送音文本。
    * ``voice`` 为显式指定音色（优先）；否则按 ``beat.speaker_role`` 查
      ``voice_cast``，再回退 ``default_voice``（Patch v3.1 Patch 11 A3）。
    * ``series_tts`` 存在时改走四级音色 + 语速解析（Patch v3.2 Patch 13 B2），
      ``characters`` 用于按角色表判定性别。
    """
    if not script.beats:
        raise TTSStageError("剧本里没有任何 beat，无法合成音频")

    target_dir = Path(audio_dir)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TTSStageError(f"音频目录不可写：{target_dir}：{exc}") from exc

    semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    async def worker(beat: ScriptBeat) -> tuple[AudioClipMeta | None, list[_Issue]]:
        async with semaphore:
            return await _synthesize_beat(
                client,
                beat,
                audio_dir=target_dir,
                overrides=overrides,
                voice=voice,
                default_voice=default_voice,
                voice_cast=voice_cast,
                series_tts=series_tts,
                characters=characters,
                ffmpeg_bin=ffmpeg_bin,
                trim_silence=trim_silence,
            )

    results = await asyncio.gather(
        *(worker(beat) for beat in script.beats), return_exceptions=True
    )

    metas: list[AudioClipMeta] = []
    issues: list[_Issue] = []
    for beat, outcome in zip(script.beats, results):
        if isinstance(outcome, BaseException):
            issues.append(
                _Issue(beat.beat_id, "synthesize", f"合成异常：{type(outcome).__name__}: {outcome}")
            )
            continue
        meta, beat_issues = outcome
        if meta is not None:
            metas.append(meta)
        issues.extend(beat_issues)

    if not metas:
        detail = "\n".join(f"- [{item.stage}] {item.beat_id}：{item.message}" for item in issues)
        raise TTSStageError(f"本话所有 beat 合成失败（共 {len(script.beats)} 条）：\n{detail}")

    _write_issues(target_dir, issues)
    return metas


def _write_issues(audio_dir: Path, issues: Sequence[_Issue]) -> None:
    """把降级问题写到 ``audio_issues.json`` 并打日志（不静默吞掉）。"""
    if not issues:
        return
    for item in issues:
        logger.warning("[s5_tts][%s] %s：%s", item.stage, item.beat_id, item.message)
    payload = [item.as_dict() for item in issues]
    path = Path(audio_dir) / AUDIO_ISSUES_FILENAME
    try:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover - 落盘失败不应影响主流程
        logger.warning("[s5_tts] 问题记录写入失败（%s）：%s", path, exc)


# ----------------------------------------------------------------------
# 阶段入口
# ----------------------------------------------------------------------
async def run_stage5(
    series_dir: str | Path,
    episode_no: int,
    client: Any,
    *,
    script: NarrationScript | None = None,
    overrides: Mapping[str, str] | None = None,
    voice: str | None = None,
    default_voice: str | None = None,
    voice_cast: Mapping[str, str] | None = None,
    series_tts: SeriesTTSConfig | None = None,
    characters: Sequence[CharacterProfile] | None = None,
    concurrency: int = 4,
    ffmpeg_bin: str = "ffmpeg",
    trim_silence: bool = True,
) -> Path:
    """执行 Stage 5，返回 ``05_audio/audio_manifest.json`` 路径。

    ``script`` 为 ``None`` 时从 ``04_script/script.json`` 读取；文件不存在直接报错
    （Stage 5 的输入只能是 Stage 4 的产物，不能凭空造稿）。

    ``voice`` 是显式音色；``voice_cast`` 是角色音色表；``default_voice`` 是兜底音色
    （Patch v3.1 Patch 11 A3）。``series_tts`` / ``characters`` 启用作品级
    四级音色 + 语速路由（Patch v3.2 Patch 13 B2）。
    """
    series_root = Path(series_dir)
    script_path = series_root / "episodes" / f"ep_{episode_no:03d}" / "04_script" / "script.json"
    if script is None:
        if not script_path.is_file():
            raise TTSStageError(f"缺少 Stage 4 剧本文件：{script_path}")
        try:
            script = load_model(NarrationScript, script_path)
        except Exception as exc:
            raise TTSStageError(f"剧本文件解析失败：{script_path}：{exc}") from exc

    audio_dir = audio_dir_for(series_root, episode_no)
    metas = await synthesize_episode(
        client,
        script=script,
        audio_dir=audio_dir,
        overrides=overrides,
        voice=voice,
        default_voice=default_voice,
        voice_cast=voice_cast,
        series_tts=series_tts,
        characters=characters,
        concurrency=concurrency,
        ffmpeg_bin=ffmpeg_bin,
        trim_silence=trim_silence,
    )

    audio_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = audio_dir / AUDIO_MANIFEST_FILENAME
    payload = [meta.model_dump(mode="json") for meta in metas]
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest_path


def load_audio_manifest(audio_dir: str | Path) -> list[AudioClipMeta]:
    """读回音频清单（用 :class:`AudioClipMeta` 校验字段）。"""
    path = Path(audio_dir) / AUDIO_MANIFEST_FILENAME
    if not path.is_file():
        raise TTSStageError(f"音频清单不存在：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TTSStageError(f"音频清单不是合法 JSON：{path}：{exc}") from exc
    if not isinstance(raw, list):
        raise TTSStageError(f"音频清单必须是 JSON 数组：{path}")
    metas: list[AudioClipMeta] = []
    for index, item in enumerate(raw):
        try:
            metas.append(AudioClipMeta.model_validate(item))
        except Exception as exc:
            raise TTSStageError(f"音频清单第 {index} 条非法：{path}：{exc}") from exc
    return metas
