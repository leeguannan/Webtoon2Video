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

from core.config import SeriesTTSConfig, VoiceSpec, resolve_voice_for_tts_model
from core.normalize import resolve_dialogue_speaker_inheritance
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
    "BeatVoiceAssignment",
    "MAX_SPEED",
    "MIN_SPEED",
    "LOUDNESS_RESAMPLE_FILTER",
    "LOUDNESS_TRIM_FILTER",
    "LOUDNORM_FILTER",
    "SILENCE_TRIM_FILTER",
    "TTSStageError",
    "apply_pronunciation",
    "audio_dir_for",
    "build_spoken_text",
    "clean_stale_audio",
    "load_audio_manifest",
    "load_bible_characters",
    "normalize_speed",
    "resolve_beat_voice",
    "resolve_beat_voice_assignment",
    "resolve_beat_voice_spec",
    "resynthesize_single_beat",
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

#: 单句响度归一滤镜（Patch v3.4 Patch 17）。
#:
#: 目标响度 ``-16 LUFS``（播客 / 有声书常用值）、峰值上限 ``-1.5 dBTP``、
#: 响度范围 ``LRA=7``。放在同一条滤镜链末尾，**逐句**归一——
#: 逐句归一比整话归一更稳：整话归一会被最响的一句拉低，正常句反而变轻。
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=7"

#: ``loudnorm`` 会把输出采样率强制拉高到 192kHz（体积膨胀约 6 倍），
#: 因此归一后立刻重采样回 24kHz（与既有百炼链路保持一致：不改变下游对
#: 时长 / 采样率的既有假设，也避免 192kHz 的体积膨胀）。
LOUDNESS_RESAMPLE_FILTER = "aresample=24000"

#: 归一之后的二次首尾裁剪（Patch v3.4 Patch 17）。
#:
#: ``loudnorm`` 的升采样滤波会在音频开头/结尾留下约 ``-43dB`` 的极低电平振铃，
#: 破坏「裁完的第一个采样就是人声」这个不变式（实测首采样从 9830 掉到 218）。
#: 归一后再裁一次即可清掉振铃：阈值取 ``-35dB``（约 1.8% 幅度，远低于人声
#: 起音电平），正常人声的首尾样本都在 ``-30dB`` 以上，不会被误裁。
#:
#: Patch v4.10：**尾端**（作用在反向流上的那一段）放宽到 ``-40dB`` +
#: ``start_silence=0.08``——四声 / 轻声收尾的音节尾部会自然衰减到 ``-35dB``
#: 以下，旧阈值容易被过早切断，出现「最后一个字被削掉」的听感；``-40dB``
#: 仍高于 loudnorm 振铃的 ``-43~-47dB``，振铃照样清得掉。头端保持
#: ``-35dB:start_silence=0`` 不变（起音必须贴紧时间轴，不留前导静音）。
LOUDNESS_TRIM_FILTER = (
    "silenceremove=start_periods=1:start_threshold=-35dB:start_silence=0:detection=peak,"
    "areverse,"
    "silenceremove=start_periods=1:start_threshold=-40dB:start_silence=0.08:detection=peak,"
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
    """裁掉首尾静音（并按需变速 + 响度归一），返回 ``(处理后文件, 问题描述)``。

    失败时返回 ``(None, 原因)``，调用方保留原音频——单条裁剪失败不允许拖垮整话。

    Patch v3.2 Patch 13 B3：变速用 ffmpeg ``atempo`` 滤镜（不变调、不发尖），
    并**合并进同一条滤镜链**（裁剪 + 变速只跑一次 ffmpeg，零额外子进程开销）。

    Patch v3.4 Patch 17：链尾再串 ``loudnorm=I=-16:TP=-1.5:LRA=7`` 做**单句**
    响度归一（-16 LUFS），归一后用 ``aresample=24000`` 把 ``loudnorm`` 撑到
    192kHz 的输出拉回 24kHz，再补一次 ``-35dB`` 首尾裁剪清掉归一带来的振铃。
    """
    ffmpeg = _resolve_binary(ffmpeg_bin)
    if not ffmpeg:
        return None, f"未找到 ffmpeg 可执行文件（{ffmpeg_bin}），跳过静音裁剪"

    normalized_speed = normalize_speed(speed)
    audio_filter = SILENCE_TRIM_FILTER
    if abs(normalized_speed - 1.0) > 1e-6:
        audio_filter = f"{audio_filter},atempo={normalized_speed:.4f}".rstrip("0").rstrip(".")
    # Patch v3.4 Patch 17：同一条滤镜链末尾串上单句 -16 LUFS 归一 + 重采样，
    # 仍然是「裁剪 + 变速 + 归一」一次 ffmpeg 跑完，零额外子进程开销。
    # 归一后再做一次首尾裁剪：清掉 loudnorm 升采样留下的 -47dB 振铃。
    audio_filter = (
        f"{audio_filter},{LOUDNORM_FILTER},{LOUDNESS_RESAMPLE_FILTER},{LOUDNESS_TRIM_FILTER}"
    )

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


def _role_gender(role: str, gender_index: Mapping[str, str]) -> str:
    """角色名/性别槽位 → 性别；说不清时返回空串。

    Patch v3.4 审核台：用户在审核台把一句改成「通用女声」或某个角色名之后，
    ``beat.speaker_gender`` 往往还留着 Stage 4 写下的旧值（男角色改成女角色后
    仍然是 ``male``）。所以角色（角色名 / 性别槽位）优先于 ``speaker_gender``，
    后者只当角色说不清时的兜底。
    """
    lowered = str(role or "").strip().lower()
    if not lowered:
        return ""
    found = gender_index.get(lowered)
    if found:
        return found
    return _SLOT_GENDER.get(lowered, "")


def _explicit_gender(beat: ScriptBeat) -> str:
    """``beat.speaker_gender`` 里的合法性别值（非法值当空）。"""
    value = str(getattr(beat, "speaker_gender", "") or "").lower()
    return value if value in {"male", "female"} else ""


def _resolve_gender(
    beat: ScriptBeat,
    *,
    gender_index: Mapping[str, str],
) -> str:
    """按「角色 → ``beat.speaker_gender``」推断说话人性别。"""
    role = str(getattr(beat, "speaker_role", "") or "").strip()
    role_gender = _role_gender(role, gender_index)
    if role_gender:
        return role_gender
    return _explicit_gender(beat) or "unknown"


@dataclass(frozen=True)
class BeatVoiceAssignment:
    """一个 beat 的配音落点：音色 + 来源说明（Patch v3.4 审核台用）。

    Stage 5 解析音色时顺手留下「这句话是谁在念」的出处，审核台直接渲染这份结果，
    避免前端另写一套判定，导致「界面上看到的」和「实际听到的」不一致。
    """

    #: 最终使用的音色 + 语速。
    spec: VoiceSpec
    #: 来源代号：``narrator`` / ``character`` / ``default_male`` / ``default_female`` /
    #: ``legacy_male`` / ``legacy_female`` / ``legacy_cast`` / ``voice_override``。
    source: str
    #: 人类可读的来源说明，例如「讲解者（旁白）」「角色专属 · 恩彗」「通用女声」。
    label: str
    #: 命中的角色名（没命中则空串）。
    role: str
    #: 解析出的说话人性别：``male`` / ``female`` / ``unknown``。
    gender: str


def resolve_beat_voice_assignment(
    beat: ScriptBeat,
    *,
    series_tts: SeriesTTSConfig | None = None,
    characters: Sequence[CharacterProfile] | None = None,
    default_voice: str | None = None,
    voice_cast: Mapping[str, str] | None = None,
) -> BeatVoiceAssignment:
    """解析「音色 + 语速 + 来源」，并处理逐句指定音色（Patch v4.2）。

    先按四级规则解析（见 :func:`_resolve_beat_voice_assignment_base`），再叠加
    ``beat.voice_override``：非空时强制使用该音色（语速沿用解析结果），
    来源标记为 ``voice_override``，界面显示「指定音色」。

    Patch v4.4：最后叠加 ``beat.speed_override``——非 ``None`` 时只替换语速
    （经 :func:`normalize_speed` 收敛），``source`` / ``label`` / ``role`` /
    ``gender`` 全部保留，音色来源不因为语速微调而改变。

    跨供应商兜底不在这里做：MiniMax 模型收到百炼音色时，统一在合成阶段
    （:func:`resolve_voice_for_tts_model`）换成同定位的 MiniMax 音色。
    """
    assignment = _resolve_beat_voice_assignment_base(
        beat,
        series_tts=series_tts,
        characters=characters,
        default_voice=default_voice,
        voice_cast=voice_cast,
    )
    override = str(getattr(beat, "voice_override", "") or "").strip()
    if override:
        assignment = BeatVoiceAssignment(
            spec=VoiceSpec(voice=override, speed=assignment.spec.speed),
            source="voice_override",
            label="指定音色",
            role=assignment.role,
            gender=assignment.gender,
        )
    speed_override = getattr(beat, "speed_override", None)
    if speed_override is not None:
        # 单句语速微调与音色解析正交：只换 spec.speed，来源标记保持角色 / 指定音色原样。
        assignment = BeatVoiceAssignment(
            spec=VoiceSpec(
                voice=assignment.spec.voice,
                speed=normalize_speed(speed_override),
            ),
            source=assignment.source,
            label=assignment.label,
            role=assignment.role,
            gender=assignment.gender,
        )
    return assignment


def resolve_beat_voice_assignment_without_override(
    beat: ScriptBeat,
    *,
    series_tts: SeriesTTSConfig | None = None,
    characters: Sequence[CharacterProfile] | None = None,
    default_voice: str | None = None,
    voice_cast: Mapping[str, str] | None = None,
) -> BeatVoiceAssignment:
    """忽略 ``beat.voice_override`` 的角色解析结果（Patch v4.2）。

    审核台需要同时展示「本句指定的音色」和「如果清空指定、按角色会用哪个音色」，
    这里单独暴露一份不含 override 的解析结果，避免前端另写一套角色判定。
    """
    return _resolve_beat_voice_assignment_base(
        beat,
        series_tts=series_tts,
        characters=characters,
        default_voice=default_voice,
        voice_cast=voice_cast,
    )


def _resolve_beat_voice_assignment_base(
    beat: ScriptBeat,
    *,
    series_tts: SeriesTTSConfig | None = None,
    characters: Sequence[CharacterProfile] | None = None,
    default_voice: str | None = None,
    voice_cast: Mapping[str, str] | None = None,
) -> BeatVoiceAssignment:
    """四级解析「音色 + 语速 + 来源」（Patch v3.2 Patch 13 B2 的来源可解释版）。

    解析顺序：

    1. ``beat.speaker_role`` 是旁白（``narrator`` / ``旁白``）→ ``series_tts.narrator``；
    2. ``series_tts.character_voices`` 命中该角色（精确 → 忽略大小写）
       → 该角色的专属音色；
    3. 按性别兜底：``male`` → ``series_tts.default_male``，``female`` →
       ``series_tts.default_female``（性别取自 ``beat.speaker_gender``、
       StoryBible 角色表或旧槽位）；作品级槽位没配音色时才回落到全局
       ``voice_cast`` 的旧音色（Patch v3.1 旧行为）。
    4. 仍判不出 → 回退 ``series_tts.narrator``。

    ``series_tts`` 为 ``None``（没配作品级模板）时退化为 Patch v3.1 的旧行为：
    只按 ``voice_cast`` 查音色、语速恒为 1.0（原速，不改变既有产物）。
    """
    role = str(getattr(beat, "speaker_role", "") or "").strip()
    lowered = role.lower()

    if series_tts is None:
        voice = resolve_beat_voice(beat, default_voice=default_voice, voice_cast=voice_cast)
        return BeatVoiceAssignment(
            spec=VoiceSpec(voice=voice or DEFAULT_VOICE, speed=1.0),
            source="legacy_cast",
            label="旧槽位音色",
            role=role,
            gender="unknown",
        )

    gender_index = _character_gender_index(characters)
    role_gender = _role_gender(role, gender_index)
    explicit_gender = _explicit_gender(beat)
    # 角色名（作品级专属音色 / 角色表里的名字）说明「这句是谁说的」，
    # 比 Stage 4 留下的 speaker_gender 更可信；角色名判不出性别时宁可回到
    # 旁白，也不要用陈旧的性别把它错配成男声 / 女声。
    role_is_character = bool(role) and (
        role in series_tts.character_voices
        or lowered in {str(name).strip().lower() for name in series_tts.character_voices}
        or lowered in gender_index
    )
    if role_is_character:
        gender = role_gender or "unknown"
    else:
        gender = role_gender or explicit_gender or "unknown"

    if lowered in _NARRATOR_ROLES or role in _NARRATOR_ROLES:
        return BeatVoiceAssignment(
            spec=series_tts.narrator,
            source="narrator",
            label="讲解者（旁白）",
            role=role,
            gender=gender,
        )

    # 2) 已识别角色的专属音色：精确 → 忽略大小写。
    if role and role in series_tts.character_voices:
        return BeatVoiceAssignment(
            spec=series_tts.character_voices[role],
            source="character",
            label=f"角色专属 · {role}",
            role=role,
            gender=gender,
        )
    if lowered:
        for name, spec in series_tts.character_voices.items():
            if str(name).strip().lower() == lowered:
                return BeatVoiceAssignment(
                    spec=spec,
                    source="character",
                    label=f"角色专属 · {name}",
                    role=str(name),
                    gender=gender,
                )

    # 3) 按性别兜底。
    #
    # 作品级配置优先：作品已经配了「通用男声 / 通用女声」时，就用作品里的音色，
    # 这样在审核台改作品配音设置（或在这里改角色）能真正生效；作品级没配音色
    # 才回落到全局旧槽位表 ``voice_cast``（保持 Patch v3.1 的旧行为）。
    if gender in {"male", "female"}:
        spec = series_tts.default_male if gender == "male" else series_tts.default_female
        slot_label = "通用男声" if gender == "male" else "通用女声"
        if spec.voice:
            return BeatVoiceAssignment(
                spec=spec,
                source="default_male" if gender == "male" else "default_female",
                label=slot_label,
                role=role,
                gender=gender,
            )
        legacy_voice = None
        cast = {str(k): str(v) for k, v in (voice_cast or {}).items()}
        if role and role in cast:
            legacy_voice = cast[role]
        elif lowered and lowered in cast:
            legacy_voice = cast[lowered]
        if legacy_voice:
            return BeatVoiceAssignment(
                spec=VoiceSpec(voice=legacy_voice, speed=spec.speed),
                source="legacy_male" if gender == "male" else "legacy_female",
                label=f"{slot_label}（旧槽位音色）",
                role=role,
                gender=gender,
            )
        return BeatVoiceAssignment(
            spec=spec,
            source="default_male" if gender == "male" else "default_female",
            label=slot_label,
            role=role,
            gender=gender,
        )

    # 4) 判不出性别 → 回退旁白。
    return BeatVoiceAssignment(
        spec=series_tts.narrator,
        source="narrator",
        label="讲解者（旁白）",
        role=role,
        gender=gender,
    )


def resolve_beat_voice_spec(
    beat: ScriptBeat,
    *,
    series_tts: SeriesTTSConfig | None = None,
    characters: Sequence[CharacterProfile] | None = None,
    default_voice: str | None = None,
    voice_cast: Mapping[str, str] | None = None,
) -> VoiceSpec:
    """四级解析「音色 + 语速」（Patch v3.2 Patch 13 B2）。

    解析顺序见 :func:`resolve_beat_voice_assignment`；本函数只取其中的音色部分，
    保证「审核台展示的来源」与「Stage 5 实际合成的音色」出自同一份逻辑。
    """
    return resolve_beat_voice_assignment(
        beat,
        series_tts=series_tts,
        characters=characters,
        default_voice=default_voice,
        voice_cast=voice_cast,
    ).spec


def _voice_role_kind(
    beat: ScriptBeat,
    *,
    characters: Sequence[CharacterProfile] | None = None,
) -> str:
    """判定 beat 的音色角色定位（``narrator`` / ``male`` / ``female``）。

    Patch v3.4 Patch 16：跨模型音色兜底时需要知道「这个音色本来是给谁用的」，
    才能换成同定位的 MiniMax 音色（旁白 / 男配 / 女配）。
    """
    role = str(getattr(beat, "speaker_role", "") or "").strip()
    if role.lower() in _NARRATOR_ROLES or role in _NARRATOR_ROLES:
        return "narrator"
    gender_index = _character_gender_index(characters)
    role_gender = _role_gender(role, gender_index)
    if role_gender:
        return role_gender
    if role and role.lower() in gender_index:
        # 角色名但角色表没写性别：不采信可能过期的 speaker_gender，按旁白定位。
        return "narrator"
    explicit = _explicit_gender(beat)
    return explicit if explicit in {"male", "female"} else "narrator"


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
    tts_model: str | None = None,
    inheritance: Mapping[str, tuple[str, str]] | None = None,
) -> tuple[AudioClipMeta | None, list[_Issue]]:
    """合成单个 beat 的音频，返回 ``(清单项, 问题列表)``。"""
    issues: list[_Issue] = []
    # Patch v3.4 Patch 17：连续对白说话人继承。没写「XX说：」的纯引号对白
    # 继承上一句对白的说话人 / 性别，避免「男角色说一半突然变女声」。
    inherited = (inheritance or {}).get(beat.beat_id)
    if inherited:
        logger.info(
            "[s5_tts] %s：继承对白说话人 %s（%s）",
            beat.beat_id,
            inherited[0],
            inherited[1],
        )
        beat = beat.model_copy(
            update={"speaker_role": inherited[0], "speaker_gender": inherited[1]}
        )
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
        # Patch v4.2：逐句指定音色优先于旧槽位表。
        override = str(getattr(beat, "voice_override", "") or "").strip()
        beat_voice = override or resolve_beat_voice(
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
    # Patch v4.4：单句语速微调对三条分支一视同仁——显式 ``--voice`` 与
    # ``series_tts is None`` 的旧契约分支原本语速恒为 1.0，这里同样覆盖；
    # 四级解析分支里 ``resolve_beat_voice_spec`` 已应用过一次，再赋同值
    # （``normalize_speed`` 对区间内值幂等），保证三条路径行为一致。
    beat_speed_override = getattr(beat, "speed_override", None)
    if beat_speed_override is not None:
        speed = normalize_speed(beat_speed_override)
    if beat_voice:
        # Patch v3.4 Patch 16：跨模型音色兜底。把百炼/Edge 音色（如 Cherry）
        # 发给 MiniMax 会直接返回 2013 invalid voice_id 并让整话失败，
        # 这里统一换成同定位（旁白/男配/女配）的 MiniMax 音色。
        model_name = tts_model or str(getattr(client, "model", "") or "")
        if model_name:
            beat_voice = resolve_voice_for_tts_model(
                beat_voice,
                tts_model=model_name,
                role_kind=_voice_role_kind(beat, characters=characters),
            )
        extra["voice"] = beat_voice
        logger.info(
            "[s5_tts] %s：speaker_role=%s → 音色 %s（语速 %.2fx）",
            beat.beat_id,
            getattr(beat, "speaker_role", "") or "narrator",
            beat_voice,
            speed,
        )

    # Patch v3.4 Patch 16：provider 自带语速控制（MiniMax）时把语速交给它，
    # ffmpeg 不再二次变速，避免「两段变速」导致语速失真。
    native_speed = bool(getattr(client, "supports_native_speed", False))
    if native_speed:
        extra["speed"] = speed
        trim_speed = 1.0
    else:
        trim_speed = speed

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
            raw_path, ffmpeg_bin=ffmpeg_bin, speed=trim_speed
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


#: 上一轮遗留音频的文件名模式（Patch v3.4 Patch 17）。
_STALE_AUDIO_PATTERNS: tuple[str, ...] = ("beat_*.wav", "beat_*.mp3")


def clean_stale_audio(audio_dir: Path) -> list[str]:
    """清理 ``05_audio`` 里上一轮遗留的 beat 音频（Patch v3.4 Patch 17）。

    重跑撰稿后 beat 编号会变（相邻去重少几条、拆拍多几条都会让编号错位），
    上一轮的 ``beat_*.wav`` 会原地留下：下游按文件名引用时会拿到**旧音频**，
    音频清单与目录内容也会对不上。

    只删 ``beat_`` 前缀的音频文件，不碰其它素材。
    """
    removed: list[str] = []
    for pattern in _STALE_AUDIO_PATTERNS:
        for path in sorted(audio_dir.glob(pattern)):
            if not path.is_file():
                continue
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("[s5_tts] 删除旧音频失败（%s）：%s", path.name, exc)
                continue
            removed.append(path.name)
    if removed:
        logger.info(
            "[s5_tts] 清理旧音频 %d 个：%s", len(removed), ", ".join(removed[:10])
        )
    return removed


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
    retry_rounds: int = 1,
    placeholder_on_failure: bool = False,
    ffmpeg_bin: str = "ffmpeg",
    trim_silence: bool = True,
    tts_model: str | None = None,
) -> list[AudioClipMeta]:
    """合成整话音频，返回音频清单（顺序与剧本 beats 一致）。

    * 普通 provider 按 ``concurrency`` 并发；带 ``serial_requests`` 标记的
      provider（MiniMax）严格串行，配合其进程内 RPM 闸门避免限流雪崩。
    * 单条 beat 失败只记录问题并跳过；**全部**失败才抛 :class:`TTSStageError`。
    * ``overrides`` 是发音词典，只作用于送音文本。
    * ``voice`` 为显式指定音色（优先）；否则按 ``beat.speaker_role`` 查
      ``voice_cast``，再回退 ``default_voice``（Patch v3.1 Patch 11 A3）。
    * ``series_tts`` 存在时改走四级音色 + 语速解析（Patch v3.2 Patch 13 B2），
      ``characters`` 用于按角色表判定性别。
    * ``tts_model`` 是当前 TTS 模型名（Patch v3.4 Patch 16）；用于跨模型音色兜底，
      缺省时从 client 的 ``model`` 属性推断。
    * ``retry_rounds`` 是失败 beat 的**串行**补齐轮数（Patch v4.3）。
    * ``placeholder_on_failure`` 打开后，补齐仍失败的 beat 用等长静音占位，
      保证时间轴与字幕不错位（默认关闭，保持"失败 beat 不进音频清单"的既有契约）。
    """
    if not script.beats:
        raise TTSStageError("剧本里没有任何 beat，无法合成音频")

    target_dir = Path(audio_dir)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TTSStageError(f"音频目录不可写：{target_dir}：{exc}") from exc

    # Patch v3.4 Patch 17：先清掉上一轮遗留的 beat 音频，避免重跑后
    # 目录里混着编号错位的旧文件（下游按文件名引用会拿到旧音频）。
    clean_stale_audio(target_dir)

    # Patch v4.3：同时清掉上一轮的问题记录。否则整轮全部成功（或补齐成功）后，
    # 旧的 audio_issues.json 会原地残留，让人误以为仍有 beat 合成失败。
    stale_issues = target_dir / AUDIO_ISSUES_FILENAME
    try:
        if stale_issues.is_file():
            stale_issues.unlink()
    except OSError as exc:  # pragma: no cover - 清理失败不应阻断合成
        logger.warning("[s5_tts] 删除旧问题记录失败（%s）：%s", stale_issues, exc)

    # Patch v3.4 Patch 17：连续对白说话人继承（没写「XX说：」的纯引号对白
    # 继承上一句的说话人与性别），在这里先算好，供各 beat 合成时套用。
    inheritance = resolve_dialogue_speaker_inheritance(
        [beat.model_dump() for beat in script.beats]
    )

    async def synth_one(beat: ScriptBeat) -> tuple[AudioClipMeta | None, list[_Issue]]:
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
            tts_model=tts_model,
            inheritance=inheritance,
        )

    # Patch v4.3：带进程内 RPM 闸门的 provider（MiniMax）要求严格串行。
    # 79 个协程 gather + 双层信号量时，一次限流重试的长冷却会独占 provider 闸门，
    # 排队协程预先分到的节流时隙在冷却结束后全部过期，进而突发连打触发 1002 雪崩
    # （实测 6 路并发下前 21 条成功、其后整批 1002）。串行调度下 limiter 时隙始终
    # 与真实时间同步，与独立探针「12 RPM 连续 60/60 成功」的节奏一致。
    serial_mode = bool(getattr(client, "serial_requests", False)) or int(concurrency) <= 1
    if serial_mode:
        results: list[tuple[AudioClipMeta | None, list[_Issue]] | BaseException] = []
        for beat in script.beats:
            try:
                results.append(await synth_one(beat))
            except BaseException as exc:  # noqa: BLE001 - 对齐 gather(return_exceptions=True)
                results.append(exc)
    else:
        semaphore = asyncio.Semaphore(max(1, int(concurrency)))

        async def worker(beat: ScriptBeat) -> tuple[AudioClipMeta | None, list[_Issue]]:
            async with semaphore:
                return await synth_one(beat)

        results = await asyncio.gather(
            *(worker(beat) for beat in script.beats), return_exceptions=True
        )

    # metas 与 script.beats **同序**：缺音频的 beat 先留 None，
    # 补齐 / 占位后再回填，保证下游时间轴与字幕不错位。
    metas: list[AudioClipMeta | None] = []
    issues: list[_Issue] = []
    failed: list[tuple[int, ScriptBeat, list[_Issue]]] = []
    for index, (beat, outcome) in enumerate(zip(script.beats, results)):
        if isinstance(outcome, BaseException):
            issue = _Issue(
                beat.beat_id, "synthesize", f"合成异常：{type(outcome).__name__}: {outcome}"
            )
            issues.append(issue)
            metas.append(None)
            failed.append((index, beat, [issue]))
            continue
        meta, beat_issues = outcome
        metas.append(meta)
        issues.extend(beat_issues)
        if meta is None:
            failed.append((index, beat, list(beat_issues)))

    # Patch v4.3：失败 beat **串行**补齐。并发补齐在限流场景只会更快撞限，
    # 串行 + provider 侧的 RPM 闸门才能稳定把缺的句子补回来。
    # 补齐成功的 beat 其首轮失败记录随后从 issues 清除，避免 audio_issues.json
    # 里残留「已救回的假警报」误导排查（只保留真正没救回来的问题）。
    resolved_by_retry: set[str] = set()
    for round_index in range(max(0, int(retry_rounds))):
        if not failed:
            break
        pending = failed
        failed = []
        logger.info(
            "[s5_tts] 第 %d 轮补齐 %d 条失败 beat（串行）",
            round_index + 1,
            len(pending),
        )
        for index, beat, _beat_issues in pending:
            meta, extra_issues = await synth_one(beat)
            issues.extend(extra_issues)
            if meta is not None:
                metas[index] = meta
                resolved_by_retry.add(beat.beat_id)
            else:
                failed.append((index, beat, list(extra_issues)))

    if resolved_by_retry:
        before = len(issues)
        issues = [
            item
            for item in issues
            if not (item.beat_id in resolved_by_retry and item.stage == "synthesize")
        ]
        logger.info(
            "[s5_tts] 补齐轮救回 %d 条 beat，已清除其首轮失败记录（%d → %d 条 issue）",
            len(resolved_by_retry),
            before,
            len(issues),
        )

    # Patch v4.3：仍然失败的 beat 用等长静音占位，保住时间轴与字幕不错位，
    # 同时把问题写进 audio_issues.json（不静默吞掉）。
    # 占位是**可选**行为（默认关闭，保持"失败 beat 不进音频清单"的既有契约）；
    # 打开后时间轴与字幕不再因个别句子失败而错位。
    if placeholder_on_failure:
        for index, beat, beat_issues in failed:
            placeholder = _write_silence_placeholder(
                target_dir,
                beat,
                text=build_spoken_text(beat, overrides),
            )
            if placeholder is None:
                continue
            metas[index] = placeholder
            detail = beat_issues[-1].message if beat_issues else "未知原因"
            issues.append(
                _Issue(
                    beat.beat_id,
                    "synthesize",
                    f"合成失败且补齐未成功，已用静音占位（时间轴/字幕不错位）：{detail}",
                )
            )

    final_metas = [meta for meta in metas if meta is not None]
    if not final_metas:
        detail = "\n".join(f"- [{item.stage}] {item.beat_id}：{item.message}" for item in issues)
        raise TTSStageError(f"本话所有 beat 合成失败（共 {len(script.beats)} 条）：\n{detail}")

    _write_issues(target_dir, issues)
    return final_metas


async def resynthesize_single_beat(
    client: Any,
    series_dir: str | Path,
    episode_no: int,
    beat_id: str,
    *,
    overrides: Mapping[str, str] | None = None,
    voice: str | None = None,
    default_voice: str | None = None,
    voice_cast: Mapping[str, str] | None = None,
    series_tts: SeriesTTSConfig | None = None,
    characters: Sequence[CharacterProfile] | None = None,
    ffmpeg_bin: str = "ffmpeg",
    trim_silence: bool = True,
    tts_model: str | None = None,
) -> AudioClipMeta:
    """只重新合成一个 beat（Patch v4.12），其余 beat 的 WAV 与清单顺序保持不变。

    与整话 :func:`synthesize_episode` 的关键差异：

    * **绝不调用** :func:`clean_stale_audio`，不删除任何其它 beat 的音频文件；
    * 读现有 ``audio_manifest.json``，原位替换目标 beat 的条目；清单里原本缺该句时，
      按 ``script.beats`` 的顺序插回正确位置；
    * ``audio_issues.json`` 里该 beat 的旧问题记录被移除，没有剩余问题时整文件删除；
    * 静音裁剪 / 响度归一 / 尾端保留 / 时长探测与主链路完全一致（同一个
      :func:`_synthesize_beat`），包括连续对白说话人继承。
    """
    series_root = Path(series_dir)
    episode_dir = series_root / "episodes" / f"ep_{episode_no:03d}"
    script_path = episode_dir / "04_script" / "script.json"
    if not script_path.is_file():
        raise TTSStageError(f"缺少 Stage 4 剧本文件：{script_path}")
    try:
        script = load_model(NarrationScript, script_path)
    except Exception as exc:
        raise TTSStageError(f"剧本文件解析失败：{script_path}：{exc}") from exc

    target = next((beat for beat in script.beats if beat.beat_id == beat_id), None)
    if target is None:
        raise TTSStageError(f"剧本里找不到 beat：{beat_id}")

    audio_dir = audio_dir_for(series_root, episode_no)
    manifest_path = audio_dir / AUDIO_MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise TTSStageError(
            f"该话还没跑过 Stage 5，缺少音频清单：{manifest_path}，请先运行 Stage 5 配音"
        )
    metas = load_audio_manifest(audio_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)

    # 连续对白说话人继承按整话剧本重算，单句重配也能继承前句角色声线。
    inheritance = resolve_dialogue_speaker_inheritance(
        [beat.model_dump() for beat in script.beats]
    )

    old_index = next(
        (index for index, item in enumerate(metas) if item.beat_id == beat_id), None
    )
    old_audio_name = metas[old_index].audio_path if old_index is not None else None

    meta, new_issues = await _synthesize_beat(
        client,
        target,
        audio_dir=audio_dir,
        overrides=overrides,
        voice=voice,
        default_voice=default_voice,
        voice_cast=voice_cast,
        series_tts=series_tts,
        characters=characters,
        ffmpeg_bin=ffmpeg_bin,
        trim_silence=trim_silence,
        tts_model=tts_model,
        inheritance=inheritance,
    )
    if meta is None:
        detail = "；".join(item.message for item in new_issues) or "未知原因"
        raise TTSStageError(f"单句重配失败（{beat_id}）：{detail}")

    # 新合成换了后缀（如旧 mp3 → 新 wav）时，只清理该 beat 自己的旧文件，
    # 其它 beat 的文件一个都不碰。
    if old_audio_name and old_audio_name != meta.audio_path:
        stale_path = audio_dir / Path(old_audio_name).name
        try:
            if stale_path.is_file():
                stale_path.unlink()
        except OSError as exc:  # 清理失败不阻断：新音频与清单已经可用
            logger.warning("[s5_tts] 单句重配清理旧音频失败（%s）：%s", stale_path, exc)

    if old_index is not None:
        metas[old_index] = meta
    else:
        # 清单原本缺该句：按 script.beats 顺序插回正确位置。
        script_order = {beat.beat_id: index for index, beat in enumerate(script.beats)}
        target_order = script_order.get(beat_id, len(script.beats))
        insert_at = len(metas)
        for index, item in enumerate(metas):
            if script_order.get(item.beat_id, len(script.beats) + 1) > target_order:
                insert_at = index
                break
        metas.insert(insert_at, meta)

    manifest_path.write_text(
        json.dumps(
            [item.model_dump(mode="json") for item in metas],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # 问题记录：移除该 beat 的旧条目，追加本次新问题；没有剩余问题就删文件。
    issues_path = audio_dir / AUDIO_ISSUES_FILENAME
    remaining: list[dict[str, str]] = []
    if issues_path.is_file():
        try:
            raw_issues = json.loads(issues_path.read_text(encoding="utf-8"))
            if isinstance(raw_issues, list):
                remaining = [
                    item
                    for item in raw_issues
                    if isinstance(item, dict) and item.get("beat_id") != beat_id
                ]
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[s5_tts] 读取旧问题记录失败（%s）：%s", issues_path, exc)
            remaining = []
    remaining.extend(item.as_dict() for item in new_issues)
    try:
        if remaining:
            issues_path.write_text(
                json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        elif issues_path.is_file():
            issues_path.unlink()
    except OSError as exc:  # pragma: no cover - 问题记录落盘失败不影响主结果
        logger.warning("[s5_tts] 问题记录写回失败（%s）：%s", issues_path, exc)

    return meta


def _write_silence_placeholder(
    audio_dir: Path,
    beat: ScriptBeat,
    *,
    text: str,
) -> AudioClipMeta | None:
    """为合成失败的 beat 写一段等长静音（Patch v4.3）。

    目的不是"假装合成成功"，而是让下游时间轴与字幕**不错位**：缺失的一句
    仍然占住它应有的时长，问题同时写进 ``audio_issues.json``。
    """
    duration_ms = max(400, int(len(text.strip()) / 4.5 * 1000))
    path = Path(audio_dir) / f"beat_{safe_beat_id(beat.beat_id)}.wav"
    frames = int(32000 * duration_ms / 1000)
    try:
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(32000)
            handle.writeframes(b"\x00\x00" * frames)
    except (OSError, wave.Error) as exc:
        logger.warning(
            "[s5_tts] 静音占位写入失败（%s）：%s", beat.beat_id, exc
        )
        return None
    return AudioClipMeta(
        beat_id=beat.beat_id,
        audio_path=path.name,
        duration_ms=duration_ms,
        pause_after_ms=(
            beat.pause_after_ms if beat.pause_after_ms is not None else DEFAULT_PAUSE_AFTER_MS
        ),
        word_timestamps=[],
    )


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
    retry_rounds: int = 1,
    placeholder_on_failure: bool = False,
    ffmpeg_bin: str = "ffmpeg",
    trim_silence: bool = True,
    tts_model: str | None = None,
) -> Path:
    """执行 Stage 5，返回 ``05_audio/audio_manifest.json`` 路径。

    ``script`` 为 ``None`` 时从 ``04_script/script.json`` 读取；文件不存在直接报错
    （Stage 5 的输入只能是 Stage 4 的产物，不能凭空造稿）。

    ``voice`` 是显式音色；``voice_cast`` 是角色音色表；``default_voice`` 是兜底音色
    （Patch v3.1 Patch 11 A3）。``series_tts`` / ``characters`` 启用作品级
    四级音色 + 语速路由（Patch v3.2 Patch 13 B2）。
    ``tts_model`` 是当前 TTS 模型名（Patch v3.4 Patch 16），用于跨模型音色兜底。
    ``retry_rounds`` 是失败 beat 的串行补齐轮数（Patch v4.3）。
    ``placeholder_on_failure`` 打开后，失败的 beat 用等长静音占位保住时间轴（Patch v4.3）。
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
        retry_rounds=retry_rounds,
        placeholder_on_failure=placeholder_on_failure,
        ffmpeg_bin=ffmpeg_bin,
        trim_silence=trim_silence,
        tts_model=tts_model,
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
