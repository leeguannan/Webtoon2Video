"""把模型返回的自由格式 JSON 归一化到规范第 3 节锁定的数据契约。

百炼等 LLM 的返回不可控：枚举值会越界（``emotion="pleading"`` 不在契约的 7 个
取值里）、列表可能塞字符串、坐标可能给 3 个数、文本可能超长。契约的字段名与
枚举值由规范锁定、不能改，因此容错只能发生在「模型输出 → 契约对象」这一层，
否则一次越界就会让整话流水线崩掉。
"""

from __future__ import annotations

import re
from typing import Any, Sequence

# 与 ``core.schemas`` 保持一致的契约取值
EMOTION_VALUES: tuple[str, ...] = (
    "calm",
    "happy",
    "angry",
    "sad",
    "shocked",
    "whisper",
    "tense",
)
MOTION_VALUES: tuple[str, ...] = (
    "static",
    "zoom_in",
    "zoom_out",
    "pan_down",
    "pan_up",
    "shake",
)
PLOT_THREAD_STATUS: tuple[str, ...] = ("open", "resolved")

#: Patch v3.1 Patch 10：标准音效触发标签白名单。
#:
#: Stage 4B 只在剧情高潮或明显物理动作点给 beat 标一个音效 key；Stage 6 据此
#: 生成 :class:`core.schemas.SfxEvent`（全话最多 8 个、间隔 >= 6000ms），
#: Stage 7 再去 ``assets/sfx/<sfx_key>.wav`` 取素材。非法值一律丢弃。
SFX_CUE_KEYS: tuple[str, ...] = (
    "heart_thump",
    "door_slam",
    "whoosh",
    "gasp",
    "phone_msg",
    "footstep",
    "crash",
)

# 契约里的长度上限
MAX_NARRATION_CHARS = 120
MIN_NARRATION_CHARS = 2


def normalize_enum(value: Any, allowed: Sequence[str], default: str) -> str:
    """把模型返回的枚举值收敛到契约允许的取值。

    依次尝试：完全相等 → 忽略大小写 → 归一化连字符/空格 → 唯一包含关系匹配；
    都不成立时回落到 ``default``。
    """
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return default
        low = raw.lower()
        for item in allowed:
            if low == item:
                return item
        squashed = low.replace("-", "_").replace(" ", "_")
        for item in allowed:
            if squashed == item:
                return item
        hits = [item for item in allowed if item in squashed or squashed in item]
        if len(hits) == 1:
            return hits[0]
    return default


def coerce_str(value: Any, default: str = "") -> str:
    """转成字符串；None / 容器类型回落到默认值。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return default


def coerce_list(value: Any) -> list[Any]:
    """转成列表；标量视为单元素列表，None 视为空列表。"""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if value is None:
        return []
    return [value]


def coerce_str_list(value: Any, *, limit: int | None = None) -> list[str]:
    """转成字符串列表（去掉空串，可选截断长度）。"""
    items: list[str] = []
    for item in coerce_list(value):
        if isinstance(item, dict):
            item = item.get("text") or item.get("name") or item.get("value")
        text = coerce_str(item)
        if text:
            items.append(text)
    if limit is not None:
        items = items[:limit]
    return items


def coerce_bool(value: Any, default: bool = False) -> bool:
    """转成布尔值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "yes", "y", "1", "是", "对"}:
            return True
        if low in {"false", "no", "n", "0", "否", "错"}:
            return False
    return default


def coerce_int(
    value: Any,
    default: int = 0,
    *,
    low: int | None = None,
    high: int | None = None,
) -> int:
    """转成整数并夹到 ``[low, high]`` 区间。"""
    if isinstance(value, bool):
        number = int(value)
    elif isinstance(value, (int, float)):
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(float(value.strip()))
        except ValueError:
            number = default
    else:
        number = default
    if low is not None and number < low:
        number = low
    if high is not None and number > high:
        number = high
    return number


def coerce_floats(value: Any, count: int = 4) -> tuple[float, ...] | None:
    """把坐标转成定长浮点元组；数量不对时返回 ``None``（契约允许为空）。"""
    items = coerce_list(value)
    numbers: list[float] = []
    for item in items:
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            numbers.append(float(item))
        elif isinstance(item, str):
            try:
                numbers.append(float(item.strip()))
            except ValueError:
                continue
    if len(numbers) != count:
        return None
    return tuple(numbers)


def clip_text(value: str, limit: int = MAX_NARRATION_CHARS) -> str:
    """按字符数截断文本。"""
    return value if len(value) <= limit else value[:limit]


# --------------------------------------------------------------------------
# Patch v3.1 Patch 9：字幕 / TTS 字符净化
# --------------------------------------------------------------------------
#: 逐字符剔除的「字体缺失 / 无法发音」符号。
#:
#: 成片 03:46 出现过一个「☒」方块，根因是台词里带了 ``♥`` 这类装饰符号：
#: 中文字体没有这些字形，字幕渲染画成豆腐块；TTS 则直接跳过该字符（读出来缺字）。
HYGIENE_STRIP_CHARS = "♥♡❤❥★☆♪♫♬✦✧※☒☐□■◆◇●○"

#: 整段剔除的 Unicode 区段（控制字符 / 零宽 / Emoji / 私用区 / 变体选择符）。
HYGIENE_STRIP_RANGES: tuple[tuple[int, int], ...] = (
    (0x0000, 0x0008),  # C0 控制字符（\t 与 \n 单独放行）
    (0x000B, 0x000C),  # \v / \f
    (0x000E, 0x001F),  # \r 及其后
    (0x007F, 0x009F),  # DEL + C1 控制字符
    (0x200B, 0x200D),  # 零宽空格 / ZWNJ / ZWJ
    (0x2600, 0x27BF),  # Misc Symbols + Dingbats（★ ☆ ✓ ✗ …）
    (0x2B00, 0x2BFF),  # Misc Symbols and Arrows（⭐ …）
    (0x1F000, 0x1F2FF),  # 麻将 / 多米诺 / 扑克
    (0x1F300, 0x1FAFF),  # Misc Symbols and Pictographs（绝大多数 Emoji）
    (0xE000, 0xF8FF),  # Private Use Area（私用区）
    (0xFE00, 0xFE0F),  # Variation Selectors（含 U+FE0F）
    (0xFEFF, 0xFEFF),  # BOM / 零宽不换行空格
)


def _in_hygiene_range(code: int) -> bool:
    """判断码位是否落在需要整体剔除的区段内。"""
    for low, high in HYGIENE_STRIP_RANGES:
        if low <= code <= high:
            return True
    return False


def sanitize_narration_and_tts(text: str, *, for_tts: bool = False) -> str:
    """清除会渲染成豆腐块「☒」或让 TTS 吞字的字符。

    * 删除字体大概率缺失、且无法发音的装饰符号（``♥`` ``★`` ``☒`` …）；
    * 删除 Emoji 区段、私用区字符与 ``U+FE0F`` 变体选择符；
    * 归一化异常波浪号 / 破折号组合；``for_tts=True`` 时额外去掉末尾多余的
      ``~``（合成时会读出怪音）。

    参数
    ----
    text:
        原始文本。
    for_tts:
        该文本是否会送进 TTS 合成（``tts_text``）。``True`` 时执行尾符清理；
        ``False`` 时保留单个 ``~``（字幕里它还可能承担语气符号的作用）。
    """
    if not text:
        return ""
    kept: list[str] = []
    for char in text:
        if char in ("\n", "\t"):
            kept.append(char)
            continue
        if char in HYGIENE_STRIP_CHARS:
            continue
        if _in_hygiene_range(ord(char)):
            continue
        kept.append(char)
    cleaned = "".join(kept)
    # 全角波浪号归一化成半角，后续统一按 "~" 处理。
    cleaned = cleaned.replace("～", "~")
    # 连续波浪号只留 1 个；连续破折号收敛成中文标准的一组「——」。
    cleaned = re.sub(r"~{2,}", "~", cleaned)
    cleaned = re.sub(r"—{3,}", "——", cleaned)
    # 删掉符号后可能留下多余空白。
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]*\n[ \t]*", "\n", cleaned)
    cleaned = cleaned.strip()
    if for_tts:
        # 末尾多余的 "~" 会让合成读出怪音，直接去掉。
        cleaned = cleaned.rstrip("~ ")
    return cleaned


def normalize_script_payload(
    payload: dict[str, Any],
    *,
    episode_id: str,
    episode_no: int,
    valid_panel_ids: set[str] | None = None,
) -> dict[str, Any]:
    """把模型返回的剧本 JSON 归一化到 ``NarrationScript`` 契约。

    * ``beat_id`` 缺失时按序号补齐；
    * ``narration_text`` 长度必须落在契约区间，过短的 beat 直接丢弃；
    * ``target_panel_ids`` 过滤空值，并可按真实存在的面板 ID 过滤；
    * ``motion`` / 伏笔状态等枚举越界时收敛；
    * 话号与话次 ID 一律以流水线为准。
    """
    beats: list[dict[str, Any]] = []
    raw_beats = coerce_list(payload.get("beats"))
    for index, item in enumerate(raw_beats, start=1):
        if not isinstance(item, dict):
            continue
        narration = coerce_str(item.get("narration_text") or item.get("text"))
        # Patch v3.1 Patch 9：进字幕 / TTS 之前先净化，杜绝「☒」方块与吞字。
        narration = sanitize_narration_and_tts(narration)
        if len(narration) < MIN_NARRATION_CHARS:
            continue
        narration = clip_text(narration, MAX_NARRATION_CHARS)
        panel_ids = [
            panel_id
            for panel_id in coerce_str_list(item.get("target_panel_ids"))
            if valid_panel_ids is None or panel_id in valid_panel_ids
        ]
        if not panel_ids:
            continue
        tts_raw = item.get("tts_text")
        tts_text = coerce_str(tts_raw) if tts_raw is not None else None
        if tts_text is not None:
            tts_text = sanitize_narration_and_tts(tts_text, for_tts=True)
            tts_text = clip_text(tts_text, MAX_NARRATION_CHARS) or None
        # Patch v3.1 Patch 10：可选音效标签（白名单收敛，非法值 / "none" 丢弃）。
        sfx_cue_raw = item.get("sfx_cue")
        sfx_cue = coerce_str(sfx_cue_raw).lower() if sfx_cue_raw is not None else ""
        if sfx_cue not in SFX_CUE_KEYS:
            sfx_cue = None
        beats.append(
            {
                "beat_id": coerce_str(item.get("beat_id")) or f"b{index}",
                "narration_text": narration,
                "tts_text": tts_text,
                "emotion_instruction": coerce_str(item.get("emotion_instruction"))
                or "用自然、流畅且有代入感的漫评解说语气说出",
                "speaker_role": coerce_str(item.get("speaker_role")) or "narrator",
                "target_panel_ids": panel_ids,
                "motion": normalize_enum(item.get("motion"), MOTION_VALUES, "zoom_in"),
                "pause_after_ms": coerce_int(item.get("pause_after_ms"), 250, low=0, high=2000),
                "sfx_cue": sfx_cue,
            }
        )

    digest_raw = payload.get("episode_digest")
    digest: dict[str, Any] = {
        "episode_no": episode_no,
        "one_line_hook": "",
        "summary": "",
        "keywords": [],
        "characters_present": [],
    }
    if isinstance(digest_raw, dict):
        digest["one_line_hook"] = coerce_str(digest_raw.get("one_line_hook"))
        digest["summary"] = coerce_str(digest_raw.get("summary"))
        digest["keywords"] = coerce_str_list(digest_raw.get("keywords"))
        digest["characters_present"] = coerce_str_list(digest_raw.get("characters_present"))

    character_updates: list[dict[str, Any]] = []
    for item in coerce_list(payload.get("character_updates")):
        if not isinstance(item, dict):
            continue
        name = coerce_str(item.get("name"))
        if not name:
            continue
        first_seen = coerce_int(item.get("first_seen_ep"), episode_no, low=1)
        last_seen = coerce_int(item.get("last_seen_ep"), episode_no, low=1)
        character_updates.append(
            {
                "name": name,
                "aliases": coerce_str_list(item.get("aliases")),
                "visual_traits": coerce_str(item.get("visual_traits")),
                "current_status": coerce_str(item.get("current_status")),
                "first_seen_ep": min(first_seen, last_seen),
                "last_seen_ep": max(first_seen, last_seen),
            }
        )

    new_threads: list[dict[str, Any]] = []
    for index, item in enumerate(coerce_list(payload.get("new_plot_threads")), start=1):
        if not isinstance(item, dict):
            continue
        title = coerce_str(item.get("title"))
        if not title:
            continue
        status = normalize_enum(item.get("status"), PLOT_THREAD_STATUS, "open")
        resolved_ep = item.get("resolved_ep")
        new_threads.append(
            {
                "thread_id": coerce_str(item.get("thread_id")) or f"t{index}",
                "title": title,
                "description": coerce_str(item.get("description")),
                "introduced_ep": coerce_int(item.get("introduced_ep"), episode_no, low=1),
                "status": status,
                "resolved_ep": coerce_int(resolved_ep, 0, low=1) if status == "resolved" and resolved_ep else None,
            }
        )

    return {
        "episode_id": episode_id,
        "hook_title": coerce_str(payload.get("hook_title")) or f"第 {episode_no} 话",
        "beats": beats,
        "episode_digest": digest,
        "character_updates": character_updates,
        "new_plot_threads": new_threads,
        "resolved_thread_ids": coerce_str_list(payload.get("resolved_thread_ids")),
    }


def normalize_panel_payload(payload: dict[str, Any], *, panel_id: str) -> dict[str, Any]:
    """把模型返回的面板 JSON 归一化到 ``PanelVisionInfo`` 契约。

    模型给的 ``emotion`` 可能越界（真实调用出现过 ``pleading``），对白列表可能
    是字符串，坐标可能给 3 个数——这里逐字段收敛，契约本身不动。
    """
    dialogues: list[dict[str, Any]] = []
    for item in coerce_list(payload.get("dialogues")):
        if isinstance(item, dict):
            entry = item
        elif isinstance(item, str):
            entry = {"text": item}
        else:
            continue
        text = coerce_str(entry.get("text"))
        if not text:
            continue
        dialogues.append(
            {
                "speaker_guess": coerce_str(entry.get("speaker_guess") or entry.get("speaker")),
                "text": text,
                "emotion": normalize_enum(entry.get("emotion"), EMOTION_VALUES, "calm"),
            }
        )

    return {
        "panel_id": panel_id,
        "scene_summary": coerce_str(payload.get("scene_summary") or payload.get("summary")),
        "dialogues": dialogues,
        "sfx_list": coerce_str_list(payload.get("sfx_list") or payload.get("sfx")),
        "focus_region": coerce_floats(payload.get("focus_region"), 4),
        "is_transition_blank": coerce_bool(payload.get("is_transition_blank")),
        # Patch v3.1 Patch 10：纯字卡标记（只有文字框、没有人脸/肢体动作）。
        "is_pure_text_card": coerce_bool(payload.get("is_pure_text_card")),
    }
