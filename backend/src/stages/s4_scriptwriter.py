"""Stage 4：调用百炼 ``qwen-plus`` 结合记忆快照生成解说稿。

规范第 4.1 / 4.2 节要点：

* 强制加载 ``ep_{K-1}`` 只读快照（由 :class:`core.memory.StoryBibleManager` 保证）；
* Prompt 上下文 = 篇章摘要 + 近 3 话详摘 + ``open`` 伏笔 + 至多 2 条远期召回；
* 产出 ``NarrationScript``（JSON）与可读版 ``script_readable.md``；
* 结束后合并 ``character_updates`` / ``new_plot_threads`` / ``resolved_thread_ids``，
  写新快照 ``bible_snapshots/ep_{no:03d}.json``；每满 10 话用百炼压缩归档一条 ``ArcSummary``。
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.normalize import (
    MIN_NARRATION_CHARS,
    MIN_DIALOGUE_CHARS,
    apply_dialogue_speaker_inheritance,
    coerce_str,
    coerce_str_list,
    deduplicate_adjacent_beats,
    is_psychological_lead,
    normalize_dialogue_quotes,
    normalize_script_payload,
    quote_char_count,
)
from core.memory import Stage4Context, StoryBibleManager
from core.panel_ids import PanelIdResolver, panel_sort_key
from core.schemas import (
    ArcSummary,
    CharacterProfile,
    EpisodeStoryDraft,
    EpisodeStoryboard,
    NarrationScript,
    PanelVisionInfo,
    StoryboardScene,
    StoryChapter,
    VisionWindowOutput,
    load_model,
    save_model,
)
from providers.llm_base import (
    ChatMessage,
    LLMClient,
    LLMError,
    LLMTruncatedError,
    parse_json_response,
)

logger = logging.getLogger(__name__)

SCRIPT_DIR_NAME = "04_script"
OVERVIEW_PROMPT_FILENAME = "script_overview.md"
#: Patch v3.0-7：Step 4A「故事编剧」提示词（说书体正文，Story-First）。
#: 文件在 ``backend/prompts/`` 下，与 ``04_script/story_draft.md``（人工稿）同名但不同目录。
STORY_PROMPT_FILENAME = "story_draft.md"
#: Patch v3.0-7：Step 4B「拆拍配图」提示词。
NARRATE_PROMPT_FILENAME = "script_narrate.md"
#: 机器可读的故事稿快照。
STORY_DRAFT_FILENAME = "story_draft.json"
#: 人工可编辑的故事稿（记事本直接改，改完用 ``w2v narrate-from-story`` 出片）。
STORY_DRAFT_MD_FILENAME = "story_draft.md"
SCRIPT_FILENAME = "script.json"
READABLE_FILENAME = "script_readable.md"

#: 单批撰稿覆盖的最大面板数。长话（实测 77 格）会被切成多批，每批输出体量
#: 远低于 ``max_tokens``，从根上消除 ``finish_reason=length`` 截断整份 JSON 的问题。
DEFAULT_BATCH_SIZE = 15
#: 概览段只产元数据（钩子/摘要/角色/伏笔），4000 token 充裕。
OVERVIEW_MAX_TOKENS = 4000
#: Patch v3.0-7：4A 说书体正文 550~1100 字 + 3~8 章章节稿，6000 不够用。
STORY_DRAFT_MAX_TOKENS = 12000
#: Patch v3.0-7：4B 单章拆拍的输出上限（整章约 5~20 条解说句）。
NARRATE_MAX_TOKENS = 8000
#: Patch v3.0-7：单章候选分镜上限，超出则按序拆成多次调用。
#: 单章 4B 调用的候选分镜上限。Patch v3.0 小修：40 太小，单章稍长就被拆成
#: 两批，而两批都带着同一份 chapter.story_prose，等于把同一段故事讲两遍。
#: 120 覆盖常规整话规模，只有极端超长话才会触发切片（并平分条数预算）。
CHAPTER_MAX_PANELS = 120
#: 百炼 qwen3 系模型默认开启长链思考（thinking），复杂撰稿任务的推理 token 会
#: 撞穿 180s 请求超时（实测 88 格 4A 连续 4 次 ReadTimeout）；compatible-mode 统一
#: 接受 ``enable_thinking=False``（qwen3.8-max / qwen-plus 均实测兼容），关掉后
#: 同一 4A 任务 34.2s 完成且产出过 schema 校验。所有 Stage 4 LLM 调用统一透传。
_NO_THINKING_KWARGS: dict[str, object] = {"enable_thinking": False}

#: Patch v3.3 Patch 15：Stage 4 新旗舰模型未开通/无权限时的有序降级链。
#: 配置的主模型在链中时取其后的候选；不在链中（自定义模型）时整条链都作为候选。
MODEL_FALLBACK_CHAIN: tuple[str, ...] = (
    "qwen4-max",
    "qwen3.8-max",
    "qwen3.7-plus",
    "qwen-plus",
)

#: 模型不可用类错误的识别标记（百炼 ModelNotFound / AccessDenied /
#: InvalidParameter 与 HTTP 404/403，消息体均为英文错误码或中文「未开通」）。
_MODEL_ACCESS_ERROR_MARKERS: tuple[str, ...] = (
    "modelnotfound",
    "accessdenied",
    "invalidparameter",
    "permission denied",
    "not found",
    "no access",
    "404",
    "403",
    "未开通",
    "无权限",
    "没有权限",
)


def is_model_access_error(exc: BaseException) -> bool:
    """判断异常是否为「模型未开通/无权限/不存在」类（可降级重试）。

    只认 provider 层的 :class:`LLMError`，截断、JSON 结构等其他错误不触发降级。
    """
    if not isinstance(exc, LLMError):
        return False
    message = str(exc).lower()
    return any(marker in message for marker in _MODEL_ACCESS_ERROR_MARKERS)


def _fallback_candidates(primary_model: str) -> list[str]:
    """主模型之后的降级候选（主模型不在链中时返回整条链）。"""
    if primary_model in MODEL_FALLBACK_CHAIN:
        index = MODEL_FALLBACK_CHAIN.index(primary_model)
        return list(MODEL_FALLBACK_CHAIN[index + 1 :])
    return list(MODEL_FALLBACK_CHAIN)


async def run_with_model_fallback(
    primary_model: str,
    call_async: Callable[[str], Awaitable[Any]],
    *,
    label: str = "Stage 4 LLM 调用",
) -> Any:
    """按 :data:`MODEL_FALLBACK_CHAIN` 执行一次带降级的模型调用。

    ``call_async(model)`` 接收实际使用的模型名、返回 awaitable；只有模型访问类
    错误（见 :func:`is_model_access_error`）才换下一个候选，其余异常原样抛出，
    候选耗尽后抛出最后一次异常。
    """
    candidates = _fallback_candidates(primary_model)
    current = primary_model
    while True:
        try:
            return await call_async(current)
        except Exception as exc:  # noqa: BLE001 - 按错误类型决定是否降级
            if not is_model_access_error(exc) or not candidates:
                raise
            next_model = candidates.pop(0)
            print(
                f"[s4_scriptwriter] 告警：{label} 模型 {current} 不可用"
                f"（{type(exc).__name__}: {exc}），自动回退到 {next_model}",
                file=sys.stderr,
            )
            current = next_model
#: Patch v2.6-5：顺位校验失败后，把错误详情打回模型重写的最大次数。
ALIGNMENT_MAX_RETRIES = 2
#: ``ScriptBeat.focus_hint`` 的合法取值（与 schemas 保持一致）。
FOCUS_HINTS = frozenset({"full", "top", "bottom", "center"})
#: Patch v3.0-7：``tts_text`` 里的拼音（含声调字母或连续 ASCII 字母）必须被拦下。
#: 模型自己写拼音会让 TTS 念成字母；多音字读音只能由 Stage 5 的发音词典注入。
TTS_PINYIN_PATTERN = re.compile(
    r"[a-zA-Zāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]{2,}"
)
#: Patch v3.0-7：专有名词白名单（不是拼音，不拦）。
TTS_ALLOWED_WORDS = frozenset(
    {
        "webtoon",
        "line",
        "bgm",
        "ok",
        "mc",
        "dj",
        "tv",
        "mv",
        "op",
        "ed",
        "cd",
        "se",
        "pc",
        "npc",
        "id",
    }
)
#: 从顺位校验错误文本里提取 panel id（兼容 ``p_005`` 与旧写法 ``panel_005``）。
_ERROR_PANEL_ID_RE = re.compile(r"p(?:anel)?_\d{3}")
#: 全话解说句条数规格（与 prompts/script_narrate.md 铁律一保持一致）。
#: Patch v3.1 Patch 10：整体收紧到 35~50 条，让成品稳定落在 2.5~3.5 分钟。
BEAT_TOTAL_MIN = 35
BEAT_TOTAL_MAX = 50

#: Patch v3.1 Patch 10：故事正文里禁止出现的「元词汇」黑名单。
#:
#: 这些词是「看图写话」的产物——把画面里的字、框、气泡当成可念的东西念出来，
#: 或者像旁白一样描述镜头本身。命中只告警（附带反馈给 4B），不阻断流水线。
META_VOCABULARY_BLACKLIST: tuple[str, ...] = (
    "手写体",
    "二字",
    "字幕",
    "幕布",
    "画面中",
    "画面上",
    "这一格",
    "镜头",
    "气泡",
    "文字框",
    "对话框",
)

#: 连续以「他 / 她」开头的句子达到该长度即视为「代词连环糊涂账」。
PRONOUN_RUN_LIMIT = 3

#: 句末标点（切句用）。
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")


def _split_sentences(text: str) -> list[str]:
    """按句末标点切句，忽略空句。"""
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(text or "") if part.strip()]


def detect_meta_vocabulary(text: str) -> list[str]:
    """扫出故事正文里的元词汇与「他/她」连环开头。

    返回问题描述列表（空列表表示干净）。两项检查：

    1. **元词汇**：命中 :data:`META_VOCABULARY_BLACKLIST` 里的词（如「手写体」
       「幕布」「画面中」）——这些是把画面的字/框当实体念出来的典型痕迹；
    2. **代词连环**：连续 :data:`PRONOUN_RUN_LIMIT` 句以上以「他」「她」开头，
       观众闭眼听会分不清谁对谁做了什么。
    """
    problems: list[str] = []
    body = text or ""
    hits = [word for word in META_VOCABULARY_BLACKLIST if word in body]
    if hits:
        problems.append("正文出现元词汇：" + "、".join(hits))

    run = 0
    longest = 0
    for sentence in _split_sentences(body):
        if sentence[:1] in {"他", "她"}:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    if longest >= PRONOUN_RUN_LIMIT:
        problems.append(f"正文有连续 {longest} 句以「他/她」开头，人物指代混乱")
    return problems


def _story_style_feedback(draft: object) -> str:
    """把 4A 产物里的元词汇/代词问题整理成给 4B 的附加反馈（非阻断）。"""
    chapters = getattr(draft, "chapters", None) or []
    problems: list[str] = []
    for chapter in chapters:
        prose = getattr(chapter, "story_prose", "") or ""
        for item in detect_meta_vocabulary(prose):
            problems.append(f"{item}（第 {getattr(chapter, 'chapter_index', '?')} 章）")
    full = getattr(draft, "full_story_markdown", "") or ""
    for item in detect_meta_vocabulary(full):
        problems.append(item)
    if not problems:
        return ""
    return (
        "【故事稿自检告警】"
        + "；".join(dict.fromkeys(problems))
        + "。解说句里请改用人物姓名或明确身份指代，不要念出画面里的字/框/镜头，"
        "也不要连续用「他/她」开头。"
    )


def _filter_feedback_for_window(
    errors: Sequence[str], window_panel_ids: Sequence[str]
) -> list[str]:
    """把全局校验错误过滤成「本章能修」的反馈。

    真机实测：把全话错误广播给每一章时，模型在自己窗口里根本看不到出错的
    panel（如第 1 章收到 p_047 的错误），反馈无效且干扰重写。规则：

    * 错误文本含 panel id（顺位类错误）：仅当至少一个 id 落在本章窗口才保留；
    * 不含 panel id（拼音、空绑定等）：任何章都可能犯，保留并全局广播。
    """
    window = set(window_panel_ids)
    matched: list[str] = []
    for item in errors:
        ids = _ERROR_PANEL_ID_RE.findall(item)
        if not ids or any(pid in window for pid in ids):
            matched.append(item)
    return matched


def _chapter_beat_budget(chapter_count: int) -> tuple[int, int]:
    """按章数平分全话 35~50 条的条数预算，返回单章 ``(下限, 上限)``。"""
    chapter_count = max(1, chapter_count)
    low = max(3, BEAT_TOTAL_MIN // chapter_count)
    high = max(low + 2, -(-BEAT_TOTAL_MAX // chapter_count))  # ceil 除法
    return low, high


def _split_budget(budget_text: str, parts: int) -> list[str]:
    """把 ``"5~11 条"`` 形式的章预算按切片数平分；解析失败则首片保留原文。"""
    parts = max(1, parts)
    numbers = [int(value) for value in re.findall(r"\d+", budget_text or "")]
    if not numbers:
        return [budget_text] + [""] * (parts - 1)
    low = numbers[0]
    high = numbers[-1] if len(numbers) >= 2 else low
    per_low = max(1, low // parts)
    per_high = max(per_low + 1, -(-high // parts))
    return [f"{per_low}~{per_high} 条" for _ in range(parts)]


# --------------------------------------------------------------------------
# Patch v4.0 Patch 19：Scene-Grounded 逐幕撰稿（动态配额 + 覆盖校验）
# --------------------------------------------------------------------------
#: Patch v4.0 Patch 19：全话解说句条数的动态上限（旧写死 50 条已废弃）。
BEAT_TOTAL_HARD_MAX = 90
#: Patch v4.0 Patch 19：单话故事正文的动态字数区间。
STORY_WORD_HARD_MAX = 1700


def compute_episode_beat_budget(
    active_scene_count: int, valid_panel_count: int
) -> tuple[int, int]:
    """按「幕数 + 分镜密度」计算全话解说句条数预算，返回 ``(下限, 上限)``。

    * 每幕基础 6~10 条；
    * ``total_min = max(35, active_scene_count * 5)``；
    * ``total_max = min(90, max(55, active_scene_count * 10,
      round(valid_panel_count * 1.05)))``。

    例：8 幕 / 75 格 → ``(40, 80)``（旧写死的 50 条上限明显不够用）。
    """
    scenes = max(1, int(active_scene_count))
    panels = max(0, int(valid_panel_count))
    total_min = max(BEAT_TOTAL_MIN, scenes * 5)
    total_max = min(
        BEAT_TOTAL_HARD_MAX,
        max(55, scenes * 10, round(panels * 1.05)),
    )
    if total_max < total_min:
        total_max = total_min
    return total_min, total_max


def compute_story_word_budget(
    active_scene_count: int, valid_panel_count: int
) -> tuple[int, int]:
    """按幕数计算单话故事正文字数预算，返回 ``(下限, 上限)``。

    每幕约 140~210 字，整体区间
    ``[max(600, active_scene_count * 130), min(1700, max(1000, active_scene_count * 210))]``。
    """
    scenes = max(1, int(active_scene_count))
    low = max(600, scenes * 130)
    high = min(STORY_WORD_HARD_MAX, max(1000, scenes * 210))
    if high < low:
        high = low
    return low, high


def _strip_punctuation(text: str) -> str:
    """去掉标点与空白，只留文字本体（用于必保原话比对）。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", coerce_str(text))


def _quote_core(quote: str) -> str:
    """必保原话的核心片段（去标点后的正文）。"""
    return _strip_punctuation(quote)


def validate_storyboard_coverage(
    beats: Sequence[dict[str, Any]],
    active_scenes: Sequence[StoryboardScene],
) -> list[StoryboardScene]:
    """校验解说句是否覆盖了每个必保幕，返回**缺失幕列表**（空列表表示全通过）。

    判定规则（Patch v4.0 Patch 19）：

    1. 幕必须 ``enabled`` 且 ``must_cover``；
    2. 命中该幕 ``panel_ids`` 的 beat 数量 ``>= 1``（该幕分镜 ``>= 3`` 格时至少 ``>= 2`` 条）；
    3. 该幕 ``locked_quotes`` 的每一句，去标点后的核心片段（或前 6 字核心短语）
       必须出现在命中该幕的 beat ``narration_text`` / ``tts_text`` 里。
    """
    missing: list[StoryboardScene] = []
    for scene in active_scenes:
        if not scene.enabled or not scene.must_cover:
            continue
        panel_ids = set(scene.panel_ids or [])
        hit = [
            beat
            for beat in beats
            if panel_ids & set(coerce_str_list(beat.get("target_panel_ids") or []))
        ]
        required = 2 if len(panel_ids) >= 3 else 1
        if len(hit) < required:
            missing.append(scene)
            continue
        spoken = _strip_punctuation(
            " ".join(
                f"{coerce_str(beat.get('narration_text'))}"
                f"{coerce_str(beat.get('tts_text'))}"
                for beat in hit
            )
        )
        for quote in scene.locked_quotes or []:
            core = _quote_core(quote)
            if not core:
                continue
            probe = core[:6]
            if core not in spoken and probe not in spoken:
                missing.append(scene)
                break
    return missing


def _storyboard_constraints_block(
    active_scenes: Sequence[StoryboardScene],
) -> str:
    """给 4A 故事反写的「分幕约束」文本。"""
    if not active_scenes:
        return (
            "【分幕约束】本话未启用镜头组编排：章节数 3~8 章，"
            "按分镜时间顺序单调排列，每章自带 panel_hint_range。"
        )
    lines = [
        f"【分幕约束】本话已切分为 {len(active_scenes)} 个镜头组，"
        "章节数量与顺序必须与下表严格一一对应（第 i 章 = 第 i 幕）：",
    ]
    for index, scene in enumerate(active_scenes, start=1):
        panel_ids = list(scene.panel_ids or [])
        span = f"{panel_ids[0]} ~ {panel_ids[-1]}" if panel_ids else "（无分镜）"
        lines.append(
            f"- 第 {index} 幕（{scene.title or '未命名幕'}）："
            f"分镜 {span}，共 {len(panel_ids)} 格"
        )
        if scene.user_note:
            lines.append(f"  · 必讲要点：{scene.user_note}")
        if scene.locked_quotes:
            lines.append(f"  · 必保原话：{'；'.join(scene.locked_quotes)}")
        if scene.visual_recap:
            lines.append(f"  · 画面复核：{scene.visual_recap}")
    return "\n".join(lines)


def _scene_directives_block(scene: StoryboardScene | None) -> str:
    """给 4B 拆拍的「本幕指令」文本。"""
    if scene is None:
        return "（本幕没有额外约束，按故事节奏自行拆拍）"
    lines = [f"本幕标题：{scene.title or '未命名幕'}"]
    if scene.user_note:
        lines.append(f"必讲要点：{scene.user_note}")
    if scene.locked_quotes:
        lines.append(f"必保原话（必须原样念出）：{'；'.join(scene.locked_quotes)}")
    if scene.visual_recap:
        lines.append(f"画面复核：{scene.visual_recap}")
    lines.append("本幕关键动作与转折必须讲到；只允许跳过纯粹重复的静态反应格。")
    return "\n".join(lines)


def _important_panel_ids(
    panels: Sequence[Any],
    active_scenes: Sequence[StoryboardScene],
    *,
    scene_mode: bool,
) -> list[str]:
    """「重要分镜」= 有对白、且（Scene-Grounded 下）落在**启用幕**里的分镜。

    Patch v4.0 Patch 20.1：启用镜头组编排后，重要分镜的统计范围必须收窄到
    启用幕。否则被禁用幕或未编入任何幕的分镜会把 4B 拖进反复重写，
    而这些分镜本来就不该被讲。
    """
    ids = [
        panel.panel_id for panel in panels if getattr(panel, "dialogues", None)
    ]
    if not scene_mode:
        return ids
    enabled_panel_ids: set[str] = set()
    for scene in active_scenes:
        if scene.enabled:
            enabled_panel_ids.update(scene.panel_ids or [])
    return [panel_id for panel_id in ids if panel_id in enabled_panel_ids]


def _chapter_panel_span(
    chapter: StoryChapter,
    ordered_panel_ids: Sequence[str],
) -> set[str]:
    """把章节的 ``panel_hint_range`` 展开成它覆盖的分镜集合。"""
    first, last = (chapter.panel_hint_range or ("", ""))
    if not first or not last:
        return set()
    order = {panel_id: index for index, panel_id in enumerate(ordered_panel_ids)}
    start = order.get(first)
    end = order.get(last)
    if start is None or end is None:
        # 提示范围里出现未知分镜时，退化为「两个端点 + 区间内的全部分镜」。
        span = {first, last}
        known = [order[panel_id] for panel_id in (first, last) if panel_id in order]
        if known:
            lo, hi = min(known), max(known)
            span.update(ordered_panel_ids[lo : hi + 1])
        return span
    lo, hi = (start, end) if start <= end else (end, start)
    return set(ordered_panel_ids[lo : hi + 1])


def _pair_chapters_with_scenes(
    chapters: Sequence[StoryChapter],
    scenes: Sequence[StoryboardScene],
    ordered_panel_ids: Sequence[str],
) -> dict[int, int]:
    """把 4A 产出的章节配到镜头组上，返回 ``{幕下标: 章节下标}``。

    Patch v4.0 Patch 20.1：4A 的章节数与镜头组数不一定相等（人工改稿、
    章节切分粒度与自动聚类不同），按下标配对会整体错位——第 3 幕拿到
    第 2 章的正文。这里改成先算「章节覆盖的分镜区间」与「幕覆盖的分镜
    集合」的重叠度，再做稳定的一一匹配（重叠多的优先）。
    """
    chapter_spans = [_chapter_panel_span(ch, ordered_panel_ids) for ch in chapters]
    scene_spans = [set(scene.panel_ids or []) for scene in scenes]
    candidates: list[tuple[int, int, int]] = []
    for scene_index, scene_span in enumerate(scene_spans):
        for chapter_index, chapter_span in enumerate(chapter_spans):
            overlap = len(scene_span & chapter_span)
            if overlap:
                candidates.append((overlap, scene_index, chapter_index))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    pairs: dict[int, int] = {}
    used: set[int] = set()
    for _overlap, scene_index, chapter_index in candidates:
        if scene_index in pairs or chapter_index in used:
            continue
        pairs[scene_index] = chapter_index
        used.add(chapter_index)
    if not pairs and chapters:
        # 完全对不上重叠时退化成按下标配对，保持旧行为兜底。
        for index in range(min(len(chapters), len(scenes))):
            pairs[index] = index
    return pairs


def _splice_scene_beats(
    beats: list[dict[str, Any]],
    scene: StoryboardScene,
    new_beats: Sequence[dict[str, Any]],
    ordered_panel_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """把某幕补写出来的解说句插回全话列表中该幕应在的位置。

    Patch v4.0 Patch 20.1：插入前先剔除**与本幕分镜有交集的旧解说**
    （首稿漏掉本幕时往往只留下一两条半幕解说）。补写是整幕重讲，
    旧残句若留着，新旧两句会并排出现——同一幕被讲两遍，甚至因为
    旧句指向更靠前的分镜而造成顺位倒挂。

    插入位置取「第一条命中本幕的旧解说」所在处；因为它之前的句子
    都不命中本幕（不会被剔除），所以剔除后插入点无需再做偏移修正。
    """
    if not new_beats:
        return beats
    scene_panels = set(scene.panel_ids or [])
    order = {panel_id: index for index, panel_id in enumerate(ordered_panel_ids)}
    scene_start = order.get(
        scene.panel_ids[0] if scene.panel_ids else "", len(ordered_panel_ids)
    )
    kept: list[dict[str, Any]] = []
    insert_at: int | None = None
    for beat in beats:
        ids = set(coerce_str_list(beat.get("target_panel_ids") or []))
        # 与本幕有交集的旧解说：整幕重写前先剔除，避免重复与倒序。
        if scene_panels and ids & scene_panels:
            if insert_at is None:
                insert_at = len(kept)
            continue
        kept.append(beat)
    if insert_at is None:
        insert_at = len(kept)
        later_index: int | None = None
        for index, beat in enumerate(kept):
            ids = coerce_str_list(beat.get("target_panel_ids") or [])
            if not ids:
                continue
            positions = [order.get(panel_id, len(order)) for panel_id in ids]
            if min(positions) > scene_start:
                later_index = index
                break
        if later_index is not None:
            insert_at = later_index
    return kept[:insert_at] + list(new_beats) + kept[insert_at:]

#: 篇章摘要压缩器（Stage 4 用百炼 ``qwen-plus`` 压缩待归档原文）。
ArcCompressor = Callable[[str], Awaitable[str]]


# ---------------------------------------------------------------------------
# Patch v4.6：叙事节奏自动留白（句间停顿 / 转折留白，纯函数 + 确定性规则）
# ---------------------------------------------------------------------------
#: 引号引导句的停顿上限（<= 该值视为引导词，规则 0 保护，绝不拉长）。
PACE_LEAD_IN_MAX_MS = 80
#: 规则 A：跨幕结尾留白（Patch v4.8 加宽：1150ms，给下一幕 BGM 留出淡入铺底）。
PACE_SCENE_END_MS = 1150
#: 规则 B：转折 / 悬念揭示 / 高潮留白（Patch v4.8 加宽：950ms）。
PACE_TURN_MS = 950
#: 规则 C：长连说换气口留白。
PACE_BREATH_MS = 480
#: 连续语音累计达到该时长即考虑插换气口。
PACE_STREAK_TRIGGER_MS = 11000
#: 两次长停顿之间至少隔开的 beat 数，避免连续重停顿打断节奏。
PACE_MIN_BEAT_GAP = 3
#: 原始停顿达到该值即视为既有换气口，重置连续语音累计（规则 C 口径）。
PACE_EXISTING_LONG_MS = 420
#: 「长停顿」间隔判定阈值（原本或本轮产生的 pause >= 该值，规则 B 口径）。
PACE_LONG_PAUSE_MS = 450
#: 停顿合法区间（与 ``ScriptBeat.pause_after_ms`` 的 pydantic 约束一致）。
PACE_PAUSE_MIN_MS = 0
PACE_PAUSE_MAX_MS = 2000

#: 高置信度转折 / 揭示关键词（命中即候选 950ms 留白）。
PACE_TURN_KEYWORDS: tuple[str, ...] = (
    "其实", "竟然", "居然", "然而", "没想到", "却发现", "真相", "秘密",
    "另一面", "原来", "直到这时", "可就在这时", "谁也没想到", "不仅如此",
    "更可怕的是", "突然", "骤然", "猛地", "开始播放",
)
#: 可触发转折留白的音效标签。
PACE_TURN_SFX: frozenset[str] = frozenset(
    {"heart_thump", "gasp", "crash", "door_slam"}
)
#: 悬念 / 震惊尾句标点组合。
PACE_SUSPENSE_ENDINGS: tuple[str, ...] = ("……", "…？", "？！", "...？")
#: 允许插换气口的自然句末标点。
PACE_BREATH_ENDINGS: tuple[str, ...] = ("。", "！", "？", "”", "……")


@dataclass(slots=True)
class PacingReport:
    """自动节奏留白的执行明细（供接口回显与 Stage 4 日志）。"""

    updated_count: int = 0
    scene_end_count: int = 0
    turn_count: int = 0
    breath_count: int = 0
    changed_beat_ids: list[str] = field(default_factory=list)


def _beat_text(beat: dict[str, Any]) -> str:
    return str(beat.get("narration_text") or "")


def _beat_speaker(beat: dict[str, Any]) -> str:
    return str(beat.get("speaker_role") or "narrator")


def _is_lead_in(beat: dict[str, Any], next_beat: dict[str, Any] | None) -> bool:
    """规则 0：引号引导句保护——这类短停顿绝不允许被任何规则拉长。"""
    if next_beat is None:
        return False
    text = _beat_text(beat).strip()
    current_pause = int(beat.get("pause_after_ms") or 0)
    # 旁白引导词（「XX 说道：」）且后面还跟着一条 beat。
    if _beat_speaker(beat) == "narrator" and text.endswith(("：", ":")):
        return True
    # 显式短停顿（<=80ms）且下一条是角色台词：念完引导词立刻接角色声音。
    next_speaker = _beat_speaker(next_beat)
    next_text = _beat_text(next_beat)
    if current_pause <= PACE_LEAD_IN_MAX_MS and (
        next_speaker != "narrator" or "“" in next_text
    ):
        return True
    return False


def _is_suspense_ending(text: str) -> bool:
    """悬念 / 震惊尾句：特殊尾标点，或去标点后 <=16 字的短反问 / 惊呼。"""
    stripped = text.strip()
    if stripped.endswith(PACE_SUSPENSE_ENDINGS):
        return True
    if stripped.endswith(("？", "！")):
        core = re.sub(r"[^\w\u4e00-\u9fff]", "", stripped)
        if len(core) <= 16:
            return True
    return False


def _scene_panel_index(
    scenes: Sequence[StoryboardScene] | None,
) -> dict[str, int]:
    """启用幕（``enabled=True`` 且 ``panel_ids`` 非空）序号 → 面板映射。

    幕序号按 ``scenes`` 中的出现顺序从 1 递增，只统计启用幕，用于判定
    每条 beat 归属的启用幕与跨幕边界。
    """
    panel_to_scene: dict[str, int] = {}
    if not scenes:
        return panel_to_scene
    ordinal = 0
    for scene in scenes:
        if not getattr(scene, "enabled", True) or not scene.panel_ids:
            continue
        ordinal += 1
        for panel_id in scene.panel_ids:
            panel_to_scene.setdefault(str(panel_id), ordinal)
    return panel_to_scene


def _beat_scene_ordinal(
    beat: dict[str, Any], panel_to_scene: dict[str, int]
) -> int | None:
    """按 ``target_panel_ids`` 取该 beat 归属的启用幕序号（映射不到返 None）。"""
    for panel_id in beat.get("target_panel_ids") or []:
        ordinal = panel_to_scene.get(str(panel_id))
        if ordinal is not None:
            return ordinal
    return None


def apply_narrative_pacing(
    beats: Sequence[dict[str, Any]],
    *,
    scenes: Sequence[StoryboardScene] | None = None,
    audio_durations_ms: Mapping[str, int] | None = None,
    min_long_gap_beats: int = PACE_MIN_BEAT_GAP,
    streak_trigger_ms: int = PACE_STREAK_TRIGGER_MS,
) -> tuple[list[dict[str, Any]], PacingReport]:
    """对解说句的 ``pause_after_ms`` 做叙事节奏留白（纯函数，不改入参）。

    规则优先级：规则 0（引号引导句保护）> 规则 A（跨幕结尾 1150ms）>
    规则 B（转折 / 悬念 / 高潮 950ms）> 规则 C（长连说换气 480ms）。
    每条 beat 最多命中一个规则；只抬高、不压低停顿，结果统一夹逼到
    ``[0, 2000]``。返回 ``(新 beat 列表, PacingReport)``，入参原对象与
    ``narration_text`` / ``tts_text`` / ``speaker_role`` /
    ``target_panel_ids`` / ``beat_id`` 顺序均不被修改。
    """
    paced = [dict(beat) for beat in beats]
    report = PacingReport()
    if not paced:
        return paced, report

    durations = dict(audio_durations_ms or {})
    panel_to_scene = _scene_panel_index(scenes)
    active_scene_count = len(set(panel_to_scene.values()))
    scene_ordinals = [
        _beat_scene_ordinal(beat, panel_to_scene) for beat in paced
    ]
    original_pauses = [int(beat.get("pause_after_ms") or 0) for beat in paced]

    streak_ms = 0
    last_long_idx: int | None = None
    total = len(paced)

    for i, beat in enumerate(paced):
        text = _beat_text(beat)
        stripped = text.strip()
        original = original_pauses[i]
        next_beat = paced[i + 1] if i + 1 < total else None
        is_lead = _is_lead_in(beat, next_beat)

        # 当前句语音时长：优先真实 TTS 时长，否则按去空白字幕字数估算。
        beat_id = str(beat.get("beat_id") or "")
        real_duration = int(durations.get(beat_id) or 0)
        if real_duration > 0:
            duration_ms = real_duration
        else:
            char_count = len(re.sub(r"\s+", "", text))
            duration_ms = max(900, char_count * 210)

        # 既有停顿本身就是换气 / 长停顿：>=420 重置连说累计；>=450 记录
        # 长停顿位置（规则 B/C 的最小间隔都以它为基准）。
        if original >= PACE_EXISTING_LONG_MS:
            streak_ms = 0
        if original >= PACE_LONG_PAUSE_MS:
            last_long_idx = i
        streak_ms += duration_ms

        # 规则 0：引导句只累加连说时长，绝不拉长，也不重置换气状态。
        if is_lead:
            continue

        gap_ok = (
            last_long_idx is None or (i - last_long_idx) >= min_long_gap_beats
        )
        new_pause = original
        rule_hit: str | None = None

        # ---- 规则 A：跨幕结尾留白 1150ms ----
        crosses_scene = False
        if i + 1 < total:
            current_scene = scene_ordinals[i]
            next_scene = scene_ordinals[i + 1]
            if (
                current_scene is not None
                and next_scene is not None
                and current_scene != next_scene
            ):
                crosses_scene = True
        elif active_scene_count >= 2:
            # 全话最后一条且存在 >=2 个启用幕：收尾留白。
            crosses_scene = True
        if crosses_scene:
            raised = max(original, PACE_SCENE_END_MS)
            if raised > original:
                new_pause = raised
                rule_hit = "scene_end"

        # ---- 规则 B：转折 / 悬念揭示 / 高潮留白 950ms ----
        if rule_hit is None and gap_ok:
            sfx_cue = str(beat.get("sfx_cue") or "")
            keyword_hit = any(
                keyword in text for keyword in PACE_TURN_KEYWORDS
            )
            if (
                keyword_hit
                or _is_suspense_ending(text)
                or sfx_cue in PACE_TURN_SFX
            ):
                raised = max(original, PACE_TURN_MS)
                if raised > original:
                    new_pause = raised
                    rule_hit = "turn"

        # ---- 规则 C：长连说换气口 480ms ----
        if (
            rule_hit is None
            and gap_ok
            and i + 1 < total
            and streak_ms >= streak_trigger_ms
            and stripped.endswith(PACE_BREATH_ENDINGS)
        ):
            # 距下一幕边界至少隔开 2 句：边界（新幕第一句的下标）最早 i+3。
            next_boundary: int | None = None
            for j in range(i + 1, total):
                left = scene_ordinals[j - 1]
                right = scene_ordinals[j]
                if (
                    left is not None
                    and right is not None
                    and left != right
                ):
                    next_boundary = j
                    break
            boundary_ok = (
                next_boundary is None or (next_boundary - i) >= 3
            )
            if boundary_ok:
                raised = max(original, PACE_BREATH_MS)
                if raised > original:
                    new_pause = raised
                    rule_hit = "breath"

        if rule_hit is not None:
            beat["pause_after_ms"] = max(
                PACE_PAUSE_MIN_MS, min(PACE_PAUSE_MAX_MS, new_pause)
            )
            report.changed_beat_ids.append(beat_id)
            streak_ms = 0
            last_long_idx = i
            if rule_hit == "scene_end":
                report.scene_end_count += 1
            elif rule_hit == "turn":
                report.turn_count += 1
            else:
                report.breath_count += 1

    # 统一夹逼（含未被规则触达的 beat，防御非法历史值），不改任何其它字段。
    for beat in paced:
        beat["pause_after_ms"] = max(
            PACE_PAUSE_MIN_MS,
            min(PACE_PAUSE_MAX_MS, int(beat.get("pause_after_ms") or 0)),
        )
    report.updated_count = len(report.changed_beat_ids)
    return paced, report


class ScriptWriterError(RuntimeError):
    """Stage 4 撰稿失败。"""


def script_dir_for(series_dir: str | Path, episode_no: int) -> Path:
    """``04_script`` 目录路径。"""
    return Path(series_dir) / "episodes" / f"ep_{episode_no:03d}" / SCRIPT_DIR_NAME


def load_prompt_template(name: str) -> str:
    """读取撰稿提示词模板。"""
    path = Path(__file__).resolve().parents[2] / "prompts" / name
    if not path.is_file():
        raise ScriptWriterError(f"提示词模板缺失：{path}")
    return path.read_text(encoding="utf-8")


def render_prompt(template: str, mapping: dict[str, str]) -> str:
    """把模板中的 ``{{TOKEN}}`` 占位符替换为实际内容。"""
    rendered = template
    for key, value in mapping.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def format_panel_list(panels: Sequence[PanelVisionInfo]) -> str:
    """把一批 Stage 3 面板渲染成撰稿 Prompt 片段。

    Patch v3.1 Patch 10：纯字卡（``is_pure_text_card=True``）会在分镜 ID 后面
    标上 ``[纯字卡]``，提示 4B 尽量跳过这类「白底黑字 PPT 格」。
    """
    if not panels:
        return "（本话暂无视觉抽取结果）"
    lines: list[str] = []
    for panel in panels:
        dialogues = "；".join(
            f"{item.speaker_guess}：{item.text}" for item in panel.dialogues
        )
        parts = [f"- {panel.panel_id}："]
        if getattr(panel, "is_pure_text_card", False):
            parts.append("[纯字卡]")
        parts.append(panel.scene_summary)
        if dialogues:
            parts.append(f"对白：{dialogues}")
        if panel.sfx_list:
            parts.append(f"音效：{'、'.join(panel.sfx_list)}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def format_panel_block(vision: VisionWindowOutput | None) -> str:
    """把整话 Stage 3 视觉结果渲染成撰稿 Prompt 片段。"""
    if vision is None:
        return format_panel_list([])
    return format_panel_list(vision.panels)


def chunk_panels(
    panels: Sequence[PanelVisionInfo], size: int = DEFAULT_BATCH_SIZE
) -> list[list[PanelVisionInfo]]:
    """把面板按固定大小切成多批（分批撰稿用）。"""
    if size <= 0:
        raise ValueError("分批大小必须为正整数")
    return [list(panels[i : i + size]) for i in range(0, len(panels), size)]


def _panel_sort_key(panel_id: str) -> tuple[str, int, str]:
    """分镜 ID 的自然序（``p_2`` 排在 ``p_10`` 前面）。"""
    return panel_sort_key(panel_id)


def _ordered_panel_ids(valid_panel_ids: Iterable[str]) -> list[str]:
    """有效分镜的顺序（传入序列则保持原序，传入集合则按自然序排列）。"""
    if isinstance(valid_panel_ids, (list, tuple)):
        return [str(item) for item in valid_panel_ids]
    return sorted((str(item) for item in valid_panel_ids), key=panel_sort_key)


def _beat_target_panel_ids(beat: object) -> list[str]:
    """取一条 beat 的目标分镜（兼容原始 dict 与 ``ScriptBeat``）。"""
    if isinstance(beat, dict):
        raw = beat.get("target_panel_ids")
    else:
        raw = getattr(beat, "target_panel_ids", None)
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    return []


def _canonicalize_beat_panel_ids(
    beats: Sequence[dict], valid_panel_ids: Iterable[str] | None
) -> None:
    """把 beat 引用的分镜 ID 归一化成本话实际使用的规范 ID（原地修改）。

    模型、历史产物与外部脚本可能引用 ``panel_001`` / ``ep_001_p001`` 这类
    旧写法，而本话真实的分镜 ID 是 ``p_001``（Stage 2 落盘的文件名主干，
    Stage 3 原样透传）。这里用与顺位校验同一套 :class:`PanelIdResolver`
    归一化，保证旧引用能被识别、也能通过 ``normalize_script_payload``
    的「只保留有效分镜」过滤。

    解析不到的 ID 原样保留——后面的归一化会把它们丢掉，报错信息也更贴近
    模型当初写下的原文。
    """
    if not valid_panel_ids:
        return
    resolver = PanelIdResolver(list(_ordered_panel_ids(valid_panel_ids)))
    for beat in beats:
        if not isinstance(beat, dict):
            continue
        raw = beat.get("target_panel_ids")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            continue
        canonical: list[str] = []
        for item in raw:
            text = str(item)
            resolved = resolver.resolve(text)
            canonical.append(resolved if resolved else text)
        beat["target_panel_ids"] = canonical


def validate_beat_alignment(
    beats: Sequence[object],
    valid_panel_ids: Iterable[str],
    *,
    important_panel_ids: Iterable[str] | None = None,
) -> list[str]:
    """校验「解说 ↔ 分镜」的顺位对齐。

    返回错误列表，空列表表示通过。两项检查：

    1. 每条 beat 的 ``target_panel_ids`` 必须全部有效，且**内部序号严格递增**；
    2. 全部 beat 展开后的分镜序号**全局单调不减**（不许回头讲前面的画面）。

    Patch v3.0-7 起**不再检查覆盖率**：Story-First 的前提就是「好故事是主角，
    分镜只是配合讲故事的插图」，跳过 30%~70% 的无关/重复/低信息格是正常行为，
    强行要求覆盖反而会把解说逼回「逐格描细节」的老路。``important_panel_ids``
    形参保留只为兼容既有调用与测试，不再参与报错。

    **ID 归一化**：``valid_panel_ids`` 与 beat 里引用的 ID 都先经过
    :class:`core.panel_ids.PanelIdResolver`（精确 → 文件名主干 → 尾部数字），
    因此历史产物里的 ``panel_001``、``ep_001_p001`` 与新流水线的 ``p_001``
    互相都能认出来，不会被误判成「引用了不存在的分镜」。
    """
    errors: list[str] = []
    order = _ordered_panel_ids(valid_panel_ids)
    resolver = PanelIdResolver(order)
    index_of = {panel_id: index for index, panel_id in enumerate(order)}
    bound: set[str] = set()
    last_index = -1

    for position, beat in enumerate(beats, start=1):
        raw_panel_ids = _beat_target_panel_ids(beat)
        if not raw_panel_ids:
            errors.append(f"第 {position} 条解说没有绑定任何分镜")
            continue
        panel_ids: list[str] = []
        for raw_panel_id in raw_panel_ids:
            canonical = resolver.resolve(raw_panel_id)
            if canonical is None:
                errors.append(
                    f"第 {position} 条解说引用了不存在的分镜 {raw_panel_id}"
                )
                continue
            panel_ids.append(canonical)
        indexes: list[int] = []
        for panel_id in panel_ids:
            indexes.append(index_of[panel_id])
            bound.add(panel_id)
        if len(indexes) >= 2 and any(
            indexes[i] >= indexes[i + 1] for i in range(len(indexes) - 1)
        ):
            errors.append(
                f"第 {position} 条解说内部的分镜顺序必须严格递增：{'、'.join(panel_ids)}"
            )
        if indexes and indexes[0] < last_index:
            errors.append(
                f"第 {position} 条解说把镜头拉回了前面的画面（{'、'.join(panel_ids)}），"
                "分镜顺序必须单调不减"
            )
        if indexes:
            last_index = max(last_index, indexes[-1])

    return errors


def validate_tts_text_no_pinyin(beats: Sequence[object]) -> list[str]:
    """校验 ``tts_text`` 里没有拼音（Patch v3.0-7）。

    配音文本由模型自己写时，只允许「标准汉字同音替代」（如「混蛋」「混账」「靠北」），
    **禁止**出现 ``hùn xiǎo`` 这类带声调字母或 ASCII 拼音——TTS 会照字母念出来。
    多音字读音的正确来源是 Stage 5 的发音词典（``pronunciation_overrides``），
    它在合成时注入，与模型产出的 ``tts_text`` 是两条互不干扰的通道。

    返回错误列表，空列表表示通过。
    """
    errors: list[str] = []
    for position, beat in enumerate(beats, start=1):
        if isinstance(beat, dict):
            raw = beat.get("tts_text")
        else:
            raw = getattr(beat, "tts_text", None)
        if not isinstance(raw, str) or not raw.strip():
            continue
        for match in TTS_PINYIN_PATTERN.finditer(raw):
            token = match.group(0)
            if token.lower() in TTS_ALLOWED_WORDS:
                continue
            errors.append(
                f"第 {position} 条解说的配音文本含拼音「{token}」，"
                "请改写成标准汉字同音词（如「混蛋」「混账」「靠北」）"
            )
            break
    return errors


def strip_pinyin(text: str) -> str:
    """把 ``tts_text`` 里的拼音片段剥掉（兜底用）。

    白名单专有名词（``WEBTOON`` / ``BGM`` 等）不在剥除范围内。
    剥完若只剩空白，说明这句的配音文本全靠拼音承载 → 返回空串，
    调用方应把 ``tts_text`` 置 ``None``（字幕 ``narration_text`` 绝不受影响）。
    """
    cleaned = TTS_PINYIN_PATTERN.sub(
        lambda match: match.group(0)
        if match.group(0).lower() in TTS_ALLOWED_WORDS
        else "",
        text,
    )
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def render_story_markdown(draft: EpisodeStoryDraft, *, episode_no: int = 0) -> str:
    """把故事稿渲染成**人工可编辑**的 Markdown（``04_script/story_draft.md``）。

    章节按 ``## 标题`` 分节，正文即该章节的说书体故事；人工改完直接用
    ``w2v narrate-from-story`` 续跑，解析时按同样的 ``## `` 规则回读。
    """
    lines: list[str] = [f"# 第 {episode_no} 话 · 故事初稿", ""]
    lines.extend(
        [
            "> 这是自动生成的故事初稿。可以直接改写下面的章节正文，"
            "改完运行 `w2v narrate-from-story` 继续出片。",
            "",
        ]
    )
    for chapter in draft.chapters:
        title = chapter.chapter_title or f"第 {chapter.chapter_index} 段"
        lines.append(f"## {title}")
        lines.append("")
        prose = chapter.story_prose.strip()
        lines.append(prose if prose else "（本章正文为空）")
        lines.append("")
    if not draft.chapters:
        lines.append("## （无章节）")
        lines.append("")
        lines.append(draft.full_story_markdown.strip() or "（无正文）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_story_markdown(text: str) -> list[tuple[str, str]]:
    """解析人工改写的故事稿 Markdown → ``[(章节标题, 章节正文), ...]``。

    规则与 :func:`render_story_markdown` 对称：以 ``## `` 开头的行是章节标题行，
    其后到下一个 ``## `` 之间的非空行都是该章节正文。
    """
    sections: list[tuple[str, str]] = []
    title: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if title is not None:
                sections.append((title, "\n".join(body).strip()))
            title = line[3:].strip()
            body = []
            continue
        if title is None:
            continue
        if line.strip():
            body.append(line.rstrip())
    if title is not None:
        sections.append((title, "\n".join(body).strip()))
    return sections


def story_draft_from_sections(
    episode_id: str,
    sections: Sequence[tuple[str, str]],
    *,
    ordered_panel_ids: Sequence[str],
    base: EpisodeStoryDraft | None = None,
) -> EpisodeStoryDraft:
    """用人工稿章节重建 :class:`EpisodeStoryDraft`。

    分镜区间沿用机器稿（``story_draft.json``）里已有的 ``panel_hint_range``；
    若人工改动了章节数量，则按顺序把分镜**均匀切分**给各章，保证候选分镜不重叠。
    """
    total = len(ordered_panel_ids)
    chapters: list[StoryChapter] = []
    count = len(sections)
    for index, (title, prose) in enumerate(sections, start=1):
        hint = ("", "")
        if base is not None and index <= len(base.chapters):
            hint = base.chapters[index - 1].panel_hint_range
        if not hint[0] or not hint[1]:
            if count <= 1:
                hint = (
                    ordered_panel_ids[0] if total else "",
                    ordered_panel_ids[-1] if total else "",
                )
            else:
                low = (index - 1) * total // count
                high = index * total // count - 1
                if low > high:
                    low = high
                hint = (
                    ordered_panel_ids[low] if total else "",
                    ordered_panel_ids[high] if total else "",
                )
        chapters.append(
            StoryChapter(
                chapter_index=index,
                chapter_title=title,
                panel_hint_range=hint,
                story_prose=prose,
            )
        )
    full_story = "\n\n".join(
        f"## {title}\n\n{prose}".strip() for title, prose in sections if prose
    )
    return EpisodeStoryDraft(
        episode_id=episode_id,
        hook_opening=base.hook_opening if base is not None else "",
        full_story_markdown=full_story,
        chapters=chapters,
    )


def _chapter_blocks(
    draft: EpisodeStoryDraft | None,
    ordered_panel_ids: Sequence[str],
) -> list[tuple[StoryChapter, list[str]]]:
    """把章节稿与分镜窗口配对：每一章给出它可以挑选的分镜 ID 列表。

    窗口来自 ``chapter.panel_hint_range``（4A 故事反写给的提示区间）；
    缺失或非法时按章节数均匀切分，保证窗口互不重叠且按时间递增，
    这样 4B 逐章顺位产出时全局单调不减天然成立。
    """
    if draft is None or not draft.chapters:
        return []
    windows = assign_chapter_panels(draft.chapters, list(ordered_panel_ids))
    return list(zip(draft.chapters, windows, strict=True))


def _format_panel_ids(
    panel_ids: Sequence[str], panel_map: dict[str, PanelVisionInfo]
) -> str:
    """按给定顺序渲染候选分镜清单（给 4B 挑图用）。

    Patch v3.1 Patch 10：过渡空页 / 窄条（``is_transition_blank=True``）在这里
    再过滤一次，保证送给 4B 的候选清单里绝不出现「纯黑过渡页」这种废帧。

    Patch v3.2 Patch 12：**纯字卡**（``is_pure_text_card=True``）在这里被硬剔除，
    从源头杜绝「白底黑字 PPT 朗读」；整章只剩纯字卡时才兜底保留。
    """
    panels = [
        panel_map[panel_id]
        for panel_id in panel_ids
        if panel_id in panel_map and not panel_map[panel_id].is_transition_blank
    ]
    return format_panel_list(exclude_pure_text_cards(panels))


#: Patch v3.2 Patch 12：中文双引号对白（含引号内文本）。
_QUOTED_SPEECH_RE = re.compile(r"“([^”]+)”")

#: 角色槽位 → 性别（``speaker_role`` 填的是槽位名而不是人名时用）。
_GENDER_BY_ROLE_SLOT: dict[str, str] = {
    "male_lead": "male",
    "male_antagonist": "male",
    "male": "male",
    "female_lead": "female",
    "female_mature": "female",
    "female": "female",
}

#: 说话引导动词（用于从引号外文本推断说话人）。
_SPEECH_VERBS: tuple[str, ...] = (
    "说",
    "道",
    "开口",
    "低声",
    "轻声",
    "冷声",
    "沉声",
    "怒声",
    "大喊",
    "喊",
    "问",
    "答",
    "念",
    "哼",
    "笑",
    "叹",
    "呼",
    "嘀咕",
    "嘟囔",
    "呢喃",
    "声音",
    "嗓子",
    "嗓音",
    "骂",
    "吼",
    "吼叫",
    "喊叫",
    "嚷",
    "斥",
    "怒斥",
    "反问",
    "请求",
    "惊呼",
    "答应",
    "呛",
    "嘶",
)

#: Patch v4.0 Patch 18：引号内容「是不是真开口」的判定依据。
#:
#: ``lead_before`` 以这些引导标点结尾，说明引号里的话是**有人当场说出来的**。
_QUOTE_LEAD_PUNCT: tuple[str, ...] = ("：", ":", "——", "，")

#: 引号内容自成一句的句末标点（独立语调）。
_SENTENCE_END_PUNCT: tuple[str, ...] = ("。", "！", "？", "…")

#: 「完整主谓句式」的粗判正则：主语代词 + 不超过 6 个字 + 谓词。
_COMPLETE_SENTENCE_RE = re.compile(
    r"(我|你|他|她|它|咱|大家|我们|你们|他们|她们)"
    r"[^，。！？、…]{0,6}"
    r"(会|要|能|不敢|不能|不会|没有|是|有|想|去|来|走|说|道|问|答|快|别|滚|"
    r"帮|给|爱|恨|死|活|打|杀|救|怕|哭|笑|叫|喊|醒|回|放|拿|抓|拉|推|开|关|"
    r"站|坐|躺|睡|敢|让|把|为|在|等)"
)

#: 不能当人名的开头（避免把「她轻声说」里的「她轻」当成角色名）。
_NON_NAME_PREFIXES: frozenset[str] = frozenset(
    {"他", "她", "它", "我", "你", "咱", "众", "有人", "两人", "三人"}
)

#: 代词/泛指首字：以这些字开头的引导句说的是「某个人」，不是角色名
#: （「他只留下一句冰冷判决：」里没有名字，不能硬猜）。
_PRONOUN_LEADING_CHARS: frozenset[str] = frozenset("他她它我你咱众人")

#: 视为「旁白」的 ``speaker_role`` 取值。
_NARRATOR_ROLES: frozenset[str] = frozenset({"narrator", "旁白", "解说"})


def exclude_pure_text_cards(
    panels: Sequence[PanelVisionInfo],
) -> list[PanelVisionInfo]:
    """剔除纯字卡（``is_pure_text_card=True``）；整批都是纯字卡时兜底保留。

    Patch v3.2 Patch 12：纯字卡是「白底黑字 PPT」，一旦被选进候选池就会被
    念成 PPT 朗读。整批都是纯字卡时保留原样（否则这一章无处可挑）。
    """
    kept = [
        panel for panel in panels if not getattr(panel, "is_pure_text_card", False)
    ]
    return kept or list(panels)


def _character_gender_index(
    characters: Sequence[CharacterProfile] | None,
) -> dict[str, str]:
    """角色姓名 / 别名 → 性别。"""
    index: dict[str, str] = {}
    for character in characters or ():
        name = coerce_str(getattr(character, "name", ""))
        gender = coerce_str(getattr(character, "gender", "unknown")) or "unknown"
        if name:
            index.setdefault(name, gender)
        for alias in getattr(character, "aliases", None) or ():
            alias_text = coerce_str(alias)
            if alias_text:
                index.setdefault(alias_text, gender)
    return index


def _infer_speaker(
    lead_in: str, gender_index: dict[str, str]
) -> tuple[str, str]:
    """从引号外引导词推断说话人姓名与性别。

    1) 已登记角色优先（``StoryBibleV2.characters`` 里的姓名 / 别名）；
    2) 兜底启发式：引号外文本开头的 2 字 + 其后出现说话动词
       （"道振平静说道：" → "道振"）。
    """
    if not lead_in:
        return "", "unknown"
    hits = [
        (lead_in.find(name), name, gender)
        for name, gender in gender_index.items()
        if name and name in lead_in
    ]
    if hits:
        hits.sort(key=lambda item: (item[0], -len(item[1])))
        _, name, gender = hits[0]
        return name, gender
    head = lead_in.strip()
    for size in (2, 3):
        if len(head) <= size:
            break
        candidate = head[:size]
        if candidate in _NON_NAME_PREFIXES:
            continue
        if candidate[0] in _PRONOUN_LEADING_CHARS:
            # 「他只留下一句冰冷判决：」「她顺从地转身离开：」——引号外只是代词
            # 引导，硬猜人名会造出「他只」「她顺」这种假角色。
            continue
        if not re.fullmatch(r"[\u4e00-\u9fff]+", candidate):
            continue
        if any(verb in head[size:] for verb in _SPEECH_VERBS):
            return candidate, "unknown"
    return "", "unknown"


def _resolve_speaker_gender(
    speaker_role: str,
    gender_index: dict[str, str],
    fallback: str = "unknown",
) -> str:
    """按姓名 → 角色槽位 → 兜底的顺序解析说话人性别。"""
    if speaker_role:
        gender = gender_index.get(speaker_role)
        if gender:
            return gender
        slot = _GENDER_BY_ROLE_SLOT.get(speaker_role.strip().lower())
        if slot:
            return slot
    return fallback if fallback in ("male", "female", "unknown") else "unknown"


def _derive_tts_text(inner: str, original_tts: str | None) -> str:
    """对白子 Beat 的送音文本：去掉外层双引号，保留句末语气标点。

    送进 TTS 的文本若还带着 ``“…”``，合成会出现怪异停顿，所以这里剥掉。
    """
    if original_tts:
        stripped = original_tts.strip().strip("“”")
        if inner and inner in stripped:
            start = stripped.index(inner)
            return stripped[start : start + len(inner)]
    return inner


def _pick_speaker_role(
    beat: dict[str, Any], lead_in: str, gender_index: dict[str, str]
) -> str:
    """确定对白子 Beat 的说话人：原 beat 的说话人优先，其次从引导词推断。"""
    role = coerce_str(beat.get("speaker_role"))
    if role and role.strip().lower() not in _NARRATOR_ROLES:
        return role
    name, _gender = _infer_speaker(lead_in, gender_index)
    return name or role or "narrator"


def _is_spoken_quote_segment(
    inner: str,
    lead_before: str,
    tail_after: str,
    *,
    full_lead_in_len: int,
) -> bool:
    """这段引号是不是「角色真的开口说出来的话」。

    Patch v4.0 Patch 18：叙述句里作名词 / 代称的引号短语不是台词。
    ``继母恶毒地让他们带着这个“孤儿”立刻离开。`` 里的 ``“孤儿”`` 只是
    一个词，硬拆会把一句话劈成「继母恶毒地让他们带着这个 / 孤儿 /
    立刻离开。」三条支离破碎的 Beat。

    只认下面几种「确实有人在说」的充分条件（任一成立即认为是台词）：

    1. 整条 Beat 基本就是纯引号台词（引号外没有实质旁白）；
    2. 引号前是引导词：以引导标点收尾，且前文出现说话动词
       （``道振平静说道：``）或心理活动动词（``他心想：``）；
    3. 引号内容自成一句完整的话：以句末语调标点收尾，或者净字数
       达到 :data:`MIN_DIALOGUE_CHARS` 且内部含完整主谓句式。

    都不成立时按「句中嵌入引用词」处理：并回引导文本，双引号保留给字幕。
    """
    text = coerce_str(inner).strip()
    if not text:
        return False
    # ① 整条几乎就是纯引号台词。
    if full_lead_in_len < 2:
        return True
    # ② 引号前有引导词（说话动词 / 心理动词 + 引导标点）。
    lead = coerce_str(lead_before).rstrip()
    for punct in _QUOTE_LEAD_PUNCT:
        if not lead.endswith(punct):
            continue
        head = lead[: -len(punct)]
        if any(verb in head for verb in _SPEECH_VERBS) or is_psychological_lead(head):
            return True
        break
    # ③ 引号内容自成一句完整的话。
    if text.endswith(_SENTENCE_END_PUNCT):
        return True
    tail = coerce_str(tail_after).strip()
    tail_continues = bool(tail) and tail[0] not in _SENTENCE_END_PUNCT
    if (
        quote_char_count(text) >= MIN_DIALOGUE_CHARS
        and _COMPLETE_SENTENCE_RE.search(text)
        and not tail_continues
    ):
        return True
    return False


def split_mixed_quote_beats(
    beats: Sequence[dict[str, Any]],
    *,
    characters: Sequence[CharacterProfile] | None = None,
    default_pause_after_ms: int = 160,
) -> list[dict[str, Any]]:
    """确定性拆分「引号外旁白 + 引号内对白」混写的 Beat。

    Patch v3.2 Patch 12：模型经常把 ``道振说：“……”`` 写成同一条 Beat，
    结果要么「角色自己念引导词」，要么「旁白把台词念完」。这里在落盘前
    100% 拆成两条连续 Beat：

    * 引号外旁白 → ``speaker_role="narrator"``、``pause_after_ms=60``
      （念完引导词立刻接角色声音）；
    * 引号内对白 → 字幕保留 ``“…”``、送音文本剥掉外层引号，
      ``speaker_role`` 填说话人姓名、``speaker_gender`` 自动补全。

    整条本来就是纯引号对白时不做拆分，只归属角色并剥掉引号。
    所有子 Beat 共享同一组 ``target_panel_ids``（画面稳住不跳），
    并按新顺序重新编号 ``b1, b2, …``。

    Patch v4.0 Patch 18：不是所有引号都是台词。叙述句里作名词 / 代称的
    引号短语（``继母恶毒地让他们带着这个“孤儿”立刻离开。`` 里的 ``“孤儿”``）
    会并回引导文本，保留双引号给字幕，不再把一句话劈成三条支离破碎的
    Beat；判定规则见 :func:`_is_spoken_quote_segment`。
    """
    gender_index = _character_gender_index(characters)
    result: list[dict[str, Any]] = []
    for raw in beats:
        if not isinstance(raw, dict):
            continue
        beat = dict(raw)
        narration = normalize_dialogue_quotes(coerce_str(beat.get("narration_text")))
        beat["narration_text"] = narration
        matches = list(_QUOTED_SPEECH_RE.finditer(narration))
        if not matches:
            # 没有引号的普通 Beat：只补齐说话人性别（角色表里有名字就按表补）。
            beat["speaker_gender"] = _resolve_speaker_gender(
                coerce_str(beat.get("speaker_role")),
                gender_index,
                coerce_str(beat.get("speaker_gender")),
            )
            result.append(beat)
            continue

        # 按引号边界切段：(kind, text, lead_before)
        # lead_before = 该引号段之前紧邻的引号外文本（没有则为 ""），
        # 用来判断这段台词是不是有独立的引导词（「继母指着…骂：」）。
        lead_parts: list[str] = []
        cursor = 0
        for match in matches:
            lead_parts.append(narration[cursor : match.start()])
            cursor = match.end()
        lead_parts.append(narration[cursor:])
        lead_in = "".join(lead_parts)
        original_tts = beat.get("tts_text")
        original_tts_text = coerce_str(original_tts)
        try:
            pause = int(beat.get("pause_after_ms"))
        except (TypeError, ValueError):
            pause = default_pause_after_ms

        # Patch v4.0 Patch 18：逐段判断引号是不是「角色真的开口说的话」。
        # 叙述句里作名词 / 代称的引号短语（「这个“孤儿”」）不是台词，
        # 直接并回引导文本，不再单开子 Beat。
        spoken_flags = [
            _is_spoken_quote_segment(
                match.group(1),
                lead_parts[index],
                lead_parts[index + 1],
                full_lead_in_len=len(lead_in.strip()),
            )
            for index, match in enumerate(matches)
        ]

        if len(lead_in.strip()) < 2:
            # 纯引号对白：整体归属对应角色，只剥外层引号 + 补角色信息。
            inner = "".join(match.group(1) for match in matches).strip()
            speaker_role = _pick_speaker_role(beat, lead_in, gender_index)
            beat["speaker_role"] = speaker_role
            beat["speaker_gender"] = _resolve_speaker_gender(
                speaker_role, gender_index, coerce_str(beat.get("speaker_gender"))
            )
            if inner:
                beat["tts_text"] = _derive_tts_text(inner, original_tts_text)
            result.append(beat)
            continue

        segments: list[tuple[str, str, str]] = []
        pending_lead = ""
        for index, match in enumerate(matches):
            pending_lead += lead_parts[index]
            if spoken_flags[index]:
                segments.append(("lead", pending_lead, ""))
                segments.append(("quote", match.group(1), pending_lead))
                pending_lead = ""
                continue
            # 句中嵌入的引用词：并回引导文本，双引号保留给字幕。
            pending_lead += match.group(0)
        pending_lead += lead_parts[len(matches)]
        if pending_lead:
            segments.append(("lead", pending_lead, ""))

        speaker_role = _pick_speaker_role(beat, lead_in, gender_index)
        prev_quote_role = ""
        for kind, text, lead_before in segments:
            if kind == "lead":
                lead_text = text.strip()
                if not lead_text:
                    continue
                child = dict(beat)
                child["narration_text"] = lead_text
                child["tts_text"] = lead_text
                child["speaker_role"] = "narrator"
                child["speaker_gender"] = "unknown"
                child["pause_after_ms"] = 60
                result.append(child)
                continue
            inner = text.strip()
            if not inner:
                continue
            if lead_before.strip():
                # 这段台词有独立引导词（「继母指着…骂：」）→ 按引导词认人。
                role = _pick_speaker_role(beat, lead_before, gender_index)
            else:
                # 紧跟在另一段引号后面（「…！”“立刻带着…！”」）→ 同一人接着说。
                role = prev_quote_role or speaker_role
            # Patch v3.4 Patch 17：心理独白保留旁白。
            #
            # 引号前导文本命中心理活动关键词（「他心想：…」「他心里盘算…」）时，
            # 引号里的是心里话而不是角色开口，保持旁白声线朗读，不切角色音色。
            thought_lead = lead_before if lead_before.strip() else lead_in
            if is_psychological_lead(thought_lead):
                role = "narrator"
            elif (
                not lead_before.strip()
                and quote_char_count(inner) < MIN_DIALOGUE_CHARS
            ):
                # 短促拟声（「嗯？」「……！」）没有说话人：没有引导词时留在旁白声线，
                # 也不继承上一段引号的角色。有明确引导词的短句（「道振说：“别怕。”」）
                # 仍归该角色——Patch v3.2 的旧契约不变。
                role = "narrator"
            if role != "narrator":
                # 只有认出了具体角色才记住他（心理独白不参与「同一人接着说」推断）。
                prev_quote_role = role
            child = dict(beat)
            child["narration_text"] = f"“{inner}”"
            child["tts_text"] = _derive_tts_text(inner, original_tts_text)
            child["speaker_role"] = role
            if role == "narrator":
                child["speaker_gender"] = "unknown"
            else:
                child["speaker_gender"] = _resolve_speaker_gender(
                    role, gender_index, coerce_str(beat.get("speaker_gender"))
                )
            child["pause_after_ms"] = pause
            result.append(child)

    for index, beat in enumerate(result, start=1):
        beat["beat_id"] = f"b{index}"
    return result



def _format_character(character: object) -> str:
    """渲染单个角色（兼容 ``CharacterProfile`` 与带 ``.line`` 的 ``CharacterReference``）。"""
    line = getattr(character, "line", None)
    if isinstance(line, str) and line:
        return f"- {line}"
    name = getattr(character, "name", "未知角色")
    aliases = getattr(character, "aliases", None) or []
    alias_text = f"（别名：{'、'.join(aliases)}）" if aliases else ""
    traits = getattr(character, "visual_traits", "") or ""
    status = getattr(character, "current_status", "") or ""
    suffix = "；".join(part for part in (traits, f"当前状态：{status}" if status else "") if part)
    return f"- {name}{alias_text}：{suffix}" if suffix else f"- {name}{alias_text}"


def _minimal_memory_block(context: Stage4Context) -> str:
    """批次调用用的精简记忆：只留角色图谱与未回收伏笔（控 prompt 体量 + 防剧透）。"""
    lines: list[str] = ["【角色图谱】"]
    if context.characters:
        lines.extend(_format_character(ref) for ref in context.characters)
    else:
        lines.append("- （暂无已登记角色）")
    lines.extend(["", "【未回收伏笔】"])
    if context.open_threads:
        for thread in context.open_threads:
            lines.append(f"- {thread.thread_id} {thread.title}：{thread.description}")
    else:
        lines.append("- （暂无未回收伏笔）")
    return "\n".join(lines)


def _overview_brief(overview: dict) -> str:
    """把整话概览压成给批次 prompt 的短摘要。"""
    digest = overview.get("episode_digest")
    if not isinstance(digest, dict):
        digest = {}
    lines = [
        f"- 钩子标题：{overview.get('hook_title') or '（无）'}",
        f"- 一句话钩子：{digest.get('one_line_hook') or '（无）'}",
        f"- 本话摘要：{digest.get('summary') or '（无）'}",
    ]
    keywords = digest.get("keywords")
    if isinstance(keywords, list) and keywords:
        lines.append("- 关键词：" + "、".join(str(item) for item in keywords))
    characters = digest.get("characters_present")
    if isinstance(characters, list) and characters:
        lines.append("- 出场角色：" + "、".join(str(item) for item in characters))
    return "\n".join(lines)


def _tail_narration(beats: list[dict]) -> str:
    """取一批 beats 里最后一条有效解说句，用作下一批的衔接提示。"""
    for beat in reversed(beats):
        text = beat.get("narration_text") or beat.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def render_readable_script(script: NarrationScript, *, base_source: str = "") -> str:
    """渲染可读版解说稿（``script_readable.md``）。

    ``narration_text``（字幕文本）与 ``tts_text``（配音文本）分列展示，
    杜绝多音字替换污染字幕。
    """
    digest = script.episode_digest
    lines: list[str] = [
        f"# 第 {digest.episode_no} 话 · {script.hook_title}",
        "",
    ]
    if base_source:
        lines.extend([f"> 记忆基线：`{base_source}`", ""])
    lines.extend(["## 解说稿", ""])
    for index, beat in enumerate(script.beats, start=1):
        lines.append(
            f"### {index}. `{beat.beat_id}` ｜ 运镜 `{getattr(beat.motion, 'value', beat.motion)}` ｜ 停顿 {beat.pause_after_ms}ms"
        )
        lines.append(f"- **旁白（字幕）**：{beat.narration_text}")
        if beat.tts_text:
            lines.append(f"- **配音文本**：{beat.tts_text}")
        else:
            lines.append("- **配音文本**：（与字幕一致）")
        lines.append(f"- **情绪指令**：{beat.emotion_instruction}")
        lines.append(f"- **目标分镜**：{'、'.join(beat.target_panel_ids)}")
        lines.append("")

    lines.extend(["## 本话摘要", ""])
    lines.append(f"- **钩子**：{digest.one_line_hook}")
    lines.append(f"- **摘要**：{digest.summary}")
    if digest.keywords:
        lines.append(f"- **关键词**：{'、'.join(digest.keywords)}")
    if digest.characters_present:
        lines.append(f"- **出场角色**：{'、'.join(digest.characters_present)}")
    lines.append("")

    if script.character_updates:
        lines.extend(["### 角色变更", ""])
        for character in script.character_updates:
            alias_text = f"（别名：{'、'.join(character.aliases)}）" if character.aliases else ""
            lines.append(
                f"- {character.name}{alias_text}：{character.visual_traits}｜{character.current_status}"
            )
        lines.append("")

    if script.new_plot_threads:
        lines.extend(["### 新埋伏笔", ""])
        for thread in script.new_plot_threads:
            lines.append(f"- `{thread.thread_id}` {thread.title}：{thread.description}")
        lines.append("")

    if script.resolved_thread_ids:
        lines.extend(["### 已回收伏笔", ""])
        for thread_id in script.resolved_thread_ids:
            lines.append(f"- `{thread_id}`")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


async def _write_overview(
    client: LLMClient,
    *,
    model: str,
    context: Stage4Context,
    panels: list[PanelVisionInfo],
    episode_id: str,
    series_title: str,
    template: str | None,
    temperature: float,
) -> dict:
    """第一段：通读整话，只产出钩子/摘要/角色/伏笔等元数据（不写解说句）。"""
    prompt = render_prompt(
        template if template is not None else load_prompt_template(OVERVIEW_PROMPT_FILENAME),
        {
            "MEMORY": context.prompt_block(),
            "PANELS": format_panel_list(panels),
            "EPISODE_ID": episode_id,
            "EPISODE_NO": str(context.episode_no),
            "SERIES_TITLE": series_title or "未知作品",
            "BASE_SOURCE": context.base_source,
        },
    )
    # Patch v3.3 Patch 15：新模型未开通时按 MODEL_FALLBACK_CHAIN 自动降级。
    response = await run_with_model_fallback(
        model,
        lambda chosen: client.chat(
            [ChatMessage(role="user", content=prompt)],
            model=chosen,
            temperature=temperature,
            max_tokens=OVERVIEW_MAX_TOKENS,
            **_NO_THINKING_KWARGS,
        ),
        label="Stage 4 整话概览",
    )
    data = parse_json_response(response.content, finish_reason=response.finish_reason)
    if not isinstance(data, dict):
        raise ScriptWriterError("整话概览返回的 JSON 结构异常")
    return data


async def _write_story_draft(
    client: LLMClient,
    *,
    model: str,
    context: Stage4Context,
    panels: list[PanelVisionInfo],
    episode_id: str,
    series_title: str,
    template: str | None,
    temperature: float,
    story_word_budget: str = "",
    storyboard_constraints: str = "",
) -> EpisodeStoryDraft:
    """Stage 4A：全话通读 + 说书体故事反写，产出按时间单调排列的章节稿。

    这一步是「先完整读完整话再逐页讲述」的第二道保险：先把因果与反转理顺，
    落成章节稿（``EpisodeStoryDraft``），再由 Stage 4B 按章节锚定到具体分镜拆拍。

    Patch v3.0-7：提示词换成 ``prompts/story_draft.md``（说书体正文，Story-First），
    模型默认走 ``story_model``（见 :func:`write_episode_script`）。

    Patch v4.0 Patch 19：新增动态字数配额（``story_word_budget``）与分幕约束
    （``storyboard_constraints``），章节数量/顺序必须与镜头组一一对应。
    """
    prompt = render_prompt(
        template if template is not None else load_prompt_template(STORY_PROMPT_FILENAME),
        {
            "MEMORY": context.prompt_block(),
            "PANELS": format_panel_list(panels),
            "EPISODE_ID": episode_id,
            "EPISODE_NO": str(context.episode_no),
            "SERIES_TITLE": series_title or "未知作品",
            "BASE_SOURCE": context.base_source,
            "STORY_WORD_BUDGET": story_word_budget or "600~1000 字",
            "STORYBOARD_CONSTRAINTS": storyboard_constraints
            or "【分幕约束】章节数 3~8 章，按分镜时间顺序单调排列。",
        },
    )
    # Patch v3.3 Patch 15：qwen4-max 未开通时自动降级到 qwen3.8-max/qwen-plus。
    response = await run_with_model_fallback(
        model,
        lambda chosen: client.chat(
            [ChatMessage(role="user", content=prompt)],
            model=chosen,
            temperature=temperature,
            max_tokens=STORY_DRAFT_MAX_TOKENS,
            **_NO_THINKING_KWARGS,
        ),
        label="Stage 4A 故事反写",
    )
    data = parse_json_response(response.content, finish_reason=response.finish_reason)
    if not isinstance(data, dict):
        raise ScriptWriterError("故事反写返回的 JSON 结构异常")
    payload = dict(data)
    payload.setdefault("episode_id", episode_id)
    try:
        return EpisodeStoryDraft.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError
        raise ScriptWriterError(f"故事反写结果不合法：{exc}") from exc


def _fallback_story_draft(
    episode_id: str, panels: Sequence[PanelVisionInfo], overview_text: str
) -> EpisodeStoryDraft:
    """故事反写失败时的兜底：用整话概览当唯一一章，保证 4B 仍有上下文。"""
    first = panels[0].panel_id if panels else "panel_001"
    last = panels[-1].panel_id if panels else "panel_001"
    prose = overview_text or "（未获得结构化故事反写，改用整话概览作为参考）"
    return EpisodeStoryDraft(
        episode_id=episode_id,
        full_story_markdown=prose,
        chapters=[
            StoryChapter(
                chapter_index=1,
                chapter_title="整话概览兜底",
                panel_hint_range=(first, last),
                story_prose=prose,
            )
        ],
    )


def assign_chapter_panels(
    chapters: Sequence[StoryChapter],
    ordered_panel_ids: Sequence[str],
) -> list[list[str]]:
    """把有效分镜按章节切成一组**互不重叠、顺序递增**的候选窗口。

    章节的 ``panel_hint_range`` 由 4A 的故事反写给出（形如 ``p_001`` ~ ``p_020``），
    这里用 :class:`core.panel_ids.PanelIdResolver` 归一化后取对应区间；
    区间缺失或非法时退化为「按章节数均匀切分」。

    窗口互不重叠且顺序递增，是 Story-First 允许「跳过 30%~70% 分镜」的技术前提：
    4B 在本章窗口内自由挑图，既不会重复讲同一格，也不会回头讲上一章的画面。
    """
    total = len(ordered_panel_ids)
    count = len(chapters)
    if count == 0:
        return []
    if total == 0:
        return [[] for _ in chapters]

    resolver = PanelIdResolver(list(ordered_panel_ids))
    index_of = {panel_id: index for index, panel_id in enumerate(ordered_panel_ids)}
    windows: list[list[str]] = []
    prev_end = -1
    for position, chapter in enumerate(chapters):
        low: int | None = None
        high: int | None = None
        hint = chapter.panel_hint_range or ("", "")
        if hint[0] and hint[1]:
            start = resolver.resolve(hint[0])
            end = resolver.resolve(hint[1])
            if start in index_of and end in index_of:
                low, high = index_of[start], index_of[end]
        if low is None or high is None or high < low:
            # 兜底：按章节数均匀切分。
            low = position * total // count
            high = (position + 1) * total // count - 1
            if high < low:
                high = low
        low = max(low, prev_end + 1)
        if high < low:
            high = low
        if high > total - 1:
            high = total - 1
        if low > high or low > total - 1:
            windows.append([])
            continue
        windows.append(list(ordered_panel_ids[low : high + 1]))
        prev_end = high
    return windows


async def _write_narration_chapter(
    client: LLMClient,
    *,
    model: str,
    context: Stage4Context,
    chapter: StoryChapter,
    panel_ids: Sequence[str],
    panel_map: dict[str, PanelVisionInfo],
    story_full: str,
    previous_tail: str,
    alignment_feedback: str,
    episode_id: str,
    series_title: str,
    template: str | None,
    temperature: float,
    beat_budget: str = "",
    scene_directives: str = "",
) -> list[dict]:
    """Step 4B：对一章故事做一次拆拍调用，产出这一章的解说句草稿。

    * 模型只能从 ``panel_ids``（本章候选分镜）里挑图，**允许跳过**——
      Story-First 的核心就是「好故事是主角，分镜只是配合讲故事的插图」；
    * 章节窗口超过 ``CHAPTER_MAX_PANELS`` 时按上限切片，逐片调用；
    * 响应被 ``max_tokens`` 截断时按候选分镜对半拆开重跑，避免整章报废；
    * ``beat_budget`` 是给模型的条数配额文本（如「5~11 条」），切片时按片数平分；
    * ``scene_directives`` 是 Patch v4.0 Patch 19 的「本幕指令」（必讲要点 /
      必保原话 / 画面复核），让拆拍严格落在本幕镜头组里；
    * 返回原始 dict 列表（未归一化），由上层统一重排 ``beat_id`` 并做顺位校验。
    """
    if not panel_ids:
        return []
    if len(panel_ids) > CHAPTER_MAX_PANELS:
        chunks = list(_chunk_ids(list(panel_ids), CHAPTER_MAX_PANELS))
        per_chunk = _split_budget(beat_budget, len(chunks))
        total_panels = len(panel_ids)
        consumed = 0
        result: list[dict] = []
        for chunk, chunk_budget in zip(chunks, per_chunk, strict=True):
            # Patch v3.1 Patch 9：每片只带自己那一段章节正文，避免整章复述。
            ratio_start = consumed / total_panels
            consumed += len(chunk)
            ratio_end = consumed / total_panels
            result.extend(
                await _write_narration_chapter(
                    client,
                    model=model,
                    context=context,
                    chapter=_slice_chapter_prose(chapter, ratio_start, ratio_end),
                    panel_ids=chunk,
                    panel_map=panel_map,
                    story_full=story_full,
                    previous_tail=previous_tail,
                    alignment_feedback=alignment_feedback,
                    episode_id=episode_id,
                    series_title=series_title,
                    template=template,
                    temperature=temperature,
                    beat_budget=chunk_budget,
                    scene_directives=scene_directives,
                )
            )
        return result

    chapter_title = chapter.chapter_title or f"第 {chapter.chapter_index} 段"
    prompt = render_prompt(
        template if template is not None else load_prompt_template(NARRATE_PROMPT_FILENAME),
        {
            "SERIES_TITLE": series_title or "未知作品",
            "EPISODE_NO": str(context.episode_no),
            "EPISODE_ID": episode_id,
            "CHAPTER_TITLE": chapter_title,
            "CHAPTER_STORY": chapter.story_prose
            or "（本章没有正文，请依据整话故事主干讲述这一段）",
            "STORY_FULL": story_full or chapter.story_prose,
            "MEMORY_MIN": _minimal_memory_block(context),
            "PANELS": _format_panel_ids(panel_ids, panel_map),
            "PREVIOUS_TAIL": previous_tail or "（本话开头，没有上一句需要衔接）",
            "ALIGNMENT_FEEDBACK": alignment_feedback or "（首轮，没有需要修正的问题）",
            "BEAT_BUDGET": beat_budget or "按故事节奏自行决定条数",
            "SCENE_DIRECTIVES": scene_directives or "（本幕没有额外约束）",
        },
    )
    try:
        # Patch v3.3 Patch 15：4B 拆拍同样享受新模型未开通降级保护。
        response = await run_with_model_fallback(
            model,
            lambda chosen: client.chat(
                [ChatMessage(role="user", content=prompt)],
                model=chosen,
                temperature=temperature,
                max_tokens=NARRATE_MAX_TOKENS,
                **_NO_THINKING_KWARGS,
            ),
            label="Stage 4B 章节拆拍",
        )
        data = parse_json_response(response.content, finish_reason=response.finish_reason)
    except LLMTruncatedError as exc:
        if len(panel_ids) <= 1:
            raise ScriptWriterError(
                f"章节「{chapter_title}」的单格解说输出仍被 max_tokens 截断，无法继续拆分"
            ) from exc
        midpoint = len(panel_ids) // 2
        half_budgets = _split_budget(beat_budget, 2)
        left = await _write_narration_chapter(
            client,
            model=model,
            context=context,
            chapter=chapter,
            panel_ids=panel_ids[:midpoint],
            panel_map=panel_map,
            story_full=story_full,
            previous_tail=previous_tail,
            alignment_feedback=alignment_feedback,
            episode_id=episode_id,
            series_title=series_title,
            template=template,
            temperature=temperature,
            beat_budget=half_budgets[0],
            scene_directives=scene_directives,
        )
        right = await _write_narration_chapter(
            client,
            model=model,
            context=context,
            chapter=chapter,
            panel_ids=panel_ids[midpoint:],
            panel_map=panel_map,
            story_full=story_full,
            previous_tail=_tail_narration(left),
            alignment_feedback=alignment_feedback,
            episode_id=episode_id,
            series_title=series_title,
            template=template,
            temperature=temperature,
            beat_budget=half_budgets[1],
            scene_directives=scene_directives,
        )
        return left + right
    if not isinstance(data, dict):
        raise ScriptWriterError("章节拆拍返回的 JSON 结构异常")
    beats = data.get("beats")
    if not isinstance(beats, list):
        return []
    return [item for item in beats if isinstance(item, dict)]


def _chunk_ids(ids: list[str], size: int) -> list[list[str]]:
    """把 ID 列表按固定长度切块。"""
    if size <= 0:
        raise ValueError("size must be positive")
    return [ids[index : index + size] for index in range(0, len(ids), size)]


def _slice_chapter_prose(
    chapter: StoryChapter, ratio_start: float, ratio_end: float
) -> StoryChapter:
    """按分片占比切一段章节正文，避免每一片都复述整章。

    超长章节（候选分镜超过 ``CHAPTER_MAX_PANELS``）会被切成多片逐片调用，
    如果每片都带同一份 ``story_prose``，模型就会把整章复述一遍（Patch v3.1
    把上限提到 120 格后这个风险更明显）。这里按每片候选分镜的占比，从章节正文
    里取对应的一段，切点尽量落在段落边界上。
    """
    prose = (chapter.story_prose or "").strip()
    if not prose or ratio_end <= ratio_start:
        return chapter
    paragraphs = [part for part in prose.split("\n\n") if part.strip()]
    if not paragraphs:
        return chapter
    total = sum(len(part) for part in paragraphs)
    if total <= 0:
        return chapter
    picked: list[str] = []
    offset = 0
    for part in paragraphs:
        end = offset + len(part)
        if end > total * ratio_start and offset < total * ratio_end:
            picked.append(part)
        offset = end
    if not picked:
        return chapter
    sliced = "\n\n".join(picked)
    if sliced == prose:
        return chapter
    return chapter.model_copy(update={"story_prose": sliced})


def _strip_pinyin_from_beats(beats: Sequence[dict]) -> None:
    """兜底：从 ``tts_text`` 里剥掉拼音。

    只动送音文本（``tts_text``），**绝不改字幕文本**（``narration_text``）；
    剥完为空则置 ``None``，由 Stage 5 用 ``narration_text`` 兜底配音。
    """
    for beat in beats:
        if not isinstance(beat, dict):
            continue
        tts_text = beat.get("tts_text")
        if not isinstance(tts_text, str) or not tts_text.strip():
            continue
        cleaned = strip_pinyin(tts_text)
        beat["tts_text"] = cleaned or None


def _clamp_regressive_beats(
    beats: Sequence[dict], ordered_panel_ids: list[str]
) -> list[str]:
    """确定性兜底：把重写三轮仍「回看」的 beat 钳制到当前主画面。

    模型在心理活动 / 回忆段落常想给旁白配更早的表情格，与全局单调不减铁律
    冲突，且 LLM 重写往往不收敛。重试耗尽后调用本函数做最后钳制：

    * beat 的目标分镜全部早于当前进度 → 整体锚定到上一条已推进到的主画面
      （符合撰稿 Prompt 里「多句解说可连续停留同一主画面」的约定）；
    * 部分早于当前进度、或 beat 内部逆序 / 重复 → 丢弃回看分镜，保留合规
      分镜（去重且内部严格递增）；
    * 解说与字幕文本一字不动，只改 ``target_panel_ids``。

    返回钳制日志（每条一句人话描述），由调用方打到 stderr；输出保证能通过
    :func:`validate_beat_alignment`。
    """
    index_of = {panel_id: index for index, panel_id in enumerate(ordered_panel_ids)}
    last_index = -1
    logs: list[str] = []
    for position, beat in enumerate(beats, start=1):
        if not isinstance(beat, dict):
            continue
        raw_ids = _beat_target_panel_ids(beat)
        pairs = [(panel_id, index_of[panel_id]) for panel_id in raw_ids if panel_id in index_of]
        if not pairs:
            continue
        kept = [(panel_id, idx) for panel_id, idx in pairs if idx >= last_index]
        # 去重保序，顺带保证 beat 内部严格递增（首条 beat 的 kept 必非空）。
        deduped: list[str] = []
        for panel_id, idx in kept:
            if not deduped or idx > index_of[deduped[-1]]:
                deduped.append(panel_id)
        original_ids = [panel_id for panel_id, _ in pairs]
        if not deduped:
            # 全部目标分镜都在回看：锚定到当前主画面（此时 last_index 必 >= 0）。
            anchor_id = ordered_panel_ids[last_index]
            beat["target_panel_ids"] = [anchor_id]
            logs.append(
                f"第 {position} 条解说回看 {original_ids}，钳制到当前主画面 {anchor_id}"
            )
            continue
        beat["target_panel_ids"] = deduped
        if deduped != original_ids:
            logs.append(
                f"第 {position} 条解说丢弃回看分镜，保留 {deduped}（原 {original_ids}）"
            )
        last_index = max(last_index, max(index_of[panel_id] for panel_id in deduped))
    return logs


def _apply_focus_hints(
    script: NarrationScript,
    raw_beats: Sequence[dict],
    valid_panel_ids: set[str] | None,
) -> None:
    """把模型给的 ``focus_hint`` 回填到剧本。

    ``core/normalize.py`` 不在本次补丁的允许清单内，而它输出的 beat 键里
    没有 ``focus_hint``，归一化时会被丢弃。这里用与归一化**完全相同**的
    存活规则（dict + 解说文字 ≥ ``MIN_NARRATION_CHARS`` + 目标分镜非空）
    复算一遍，保证与 ``script.beats`` 一一对应后再回填。
    """
    hints: list[str] = []
    for item in raw_beats:
        if not isinstance(item, dict):
            continue
        narration = coerce_str(item.get("narration_text") or item.get("text"))
        if len(narration) < MIN_NARRATION_CHARS:
            continue
        panel_ids = [
            panel_id
            for panel_id in coerce_str_list(item.get("target_panel_ids"))
            if valid_panel_ids is None or panel_id in valid_panel_ids
        ]
        if not panel_ids:
            continue
        raw_hint = coerce_str(item.get("focus_hint")).lower()
        hints.append(raw_hint if raw_hint in FOCUS_HINTS else "full")
    if len(hints) != len(script.beats):
        print(
            f"[s4_scriptwriter] focus_hint 回填跳过：原始 {len(hints)} 条 / "
            f"归一化后 {len(script.beats)} 条，无法安全对齐。",
            file=sys.stderr,
        )
        return
    for beat, hint in zip(script.beats, hints):
        beat.focus_hint = hint


async def write_episode_script(
    client: LLMClient,
    *,
    model: str,
    context: Stage4Context,
    episode_id: str,
    vision: VisionWindowOutput | None = None,
    series_title: str = "",
    overview_template: str | None = None,
    beats_template: str | None = None,
    story_draft_template: str | None = None,
    story_draft_path: Path | None = None,
    temperature: float = 0.7,
    batch_size: int = DEFAULT_BATCH_SIZE,
    story_model: str = "",
    story_markdown_path: Path | None = None,
    story_source_markdown: str | None = None,
    require_story_source: bool = False,
    force_story_markdown: bool = False,
    storyboard: EpisodeStoryboard | None = None,
    storyboard_path: Path | None = None,
    vision_client: LLMClient | None = None,
    vision_model: str = "",
    panels_dir: Path | None = None,
    reset_storyboard: bool = False,
    storyboard_sink: list[EpisodeStoryboard] | None = None,
) -> NarrationScript:
    """Story-First 两步撰稿：先讲清故事，再顺着故事挑图配解说。

    * **第一段（4A）**：整话通读 + 故事反写，产出说书体章节稿
      （``EpisodeStoryDraft``，落盘 ``04_script/story_draft.json`` 与
      人工可编辑的 ``04_script/story_draft.md``）；
    * **第二段（4B）**：以章节稿为唯一主干，**逐章**把故事切成解说句
      （``prompts/script_narrate.md``），每章只在自己的候选分镜窗口里挑图，
      允许跳过 30%~70% 的分镜；合并后用 :func:`validate_beat_alignment`
      做顺位硬校验、用 :func:`validate_tts_text_no_pinyin` 做配音文本硬校验，
      不通过就把错误详情打回模型重写（最多 ``ALIGNMENT_MAX_RETRIES`` 次）。

    人工介入：若 ``04_script/story_draft.md`` 已被人工改过（或直接传入
    ``story_source_markdown``），4B 直接采用人工稿，不再调用 4A。

    Patch v4.0 Patch 19（Scene-Grounded 逐幕撰稿）：传入 ``storyboard`` 或
    ``storyboard_path`` 时启用镜头组编排——

    * 4A 拿到「分幕约束」，章节数量与顺序必须与启用的镜头组一一对应；
    * 4B 逐幕拆拍（每幕只看自己的分镜窗口，并收到本幕必讲要点/必保原话）；
    * 条数配额改用 :func:`compute_episode_beat_budget` 动态伸缩；
    * 结束后用 :func:`validate_storyboard_coverage` 硬校验覆盖度，缺失幕
      **只对该幕**定向补写。
    """
    all_panels = list(vision.panels) if vision else []
    # Patch v2.5-1 第三重：版权声明/推广尾页/过渡空页硬过滤——既不进入 LLM
    # 上下文（overview 与章节拆拍都只看 story_panels），其 panel_id 也不会进入
    # 下方 valid_panel_ids，模型若违规引用，normalize_script_payload 会直接丢弃
    # 该条 beat（target_panel_ids 过滤后为空的 beat 整条剔除）。
    panels = [
        panel
        for panel in all_panels
        if not panel.is_transition_blank and panel.non_story_type == "none"
    ]
    filtered_ids = {panel.panel_id for panel in all_panels} - {
        panel.panel_id for panel in panels
    }
    if filtered_ids:
        print(
            f"[s4_scriptwriter] filtered non-story panels: {sorted(filtered_ids)}",
            file=sys.stderr,
        )

    # Patch v3.2 Patch 12：纯字卡（白底黑字 PPT）从撰稿上下文里硬剔除，
    # overview / 4A 故事反写 / 4B 候选池都看不到它，避免被念成 PPT 朗读。
    story_panels = exclude_pure_text_cards(panels)

    overview = await _write_overview(
        client,
        model=model,
        context=context,
        panels=story_panels,
        episode_id=episode_id,
        series_title=series_title,
        template=overview_template,
        temperature=temperature,
    )
    overview_text = _overview_brief(overview)

    valid_panel_ids = {panel.panel_id for panel in panels} if panels else None
    ordered_panel_ids = _ordered_panel_ids(valid_panel_ids or [])
    panel_map = {panel.panel_id: panel for panel in panels}

    # ------------------------------------------------------------------
    # Patch v4.0 Patch 19：镜头组编排（storyboard）+ 整幕视觉联看复核
    # ------------------------------------------------------------------
    # * 显式传入 storyboard 或 storyboard_path → 启用 Scene-Grounded 逐幕撰稿；
    # * 两者都没有时，也在内存里聚一份镜头组，用于动态字数配额。
    resolved_storyboard: EpisodeStoryboard | None = None
    active_scenes: list[StoryboardScene] = []
    scene_mode = False
    if vision is not None and vision.panels:
        # 局部导入，避免 stages 包内部的循环依赖。
        from stages import s3b_scenes

        if storyboard is not None:
            resolved_storyboard = storyboard
            scene_mode = True
        elif storyboard_path is not None:
            resolved_storyboard = s3b_scenes.load_or_build_storyboard(
                episode_id,
                vision,
                Path(storyboard_path),
                reset=reset_storyboard,
            )
            scene_mode = True
        else:
            resolved_storyboard = s3b_scenes.auto_build_storyboard(episode_id, vision)
        if resolved_storyboard is not None:
            active_scenes = [
                scene for scene in resolved_storyboard.scenes if scene.enabled
            ]
            if vision_client is not None and panels_dir is not None:
                resolved_storyboard = await s3b_scenes.enrich_storyboard_with_vision(
                    vision_client,
                    storyboard=resolved_storyboard,
                    panels_dir=panels_dir,
                    model=vision_model,
                    force=False,
                    vision=vision,
                )
                active_scenes = [
                    scene for scene in resolved_storyboard.scenes if scene.enabled
                ]
        if storyboard_sink is not None and resolved_storyboard is not None:
            # Patch v4.0 Patch 20.1：把「整幕视觉复核后」的镜头组交出去，
            # 供 run_stage4 回写（auto 版需要把 visual_recap 落盘）。
            storyboard_sink.append(resolved_storyboard)
        if scene_mode and active_scenes:
            # Patch v4.0 Patch 20.1：用户手动把某些分镜编入启用幕后，这些分镜
            # 即使被自动流程判成过渡页 / 纯字卡，也要参与本话撰稿；版权声明页
            # 与推广尾页则永远不放行。
            all_panel_map = {
                panel.panel_id: panel for panel in (list(vision.panels) if vision else [])
            }
            enabled_panel_ids = {
                panel_id
                for scene in active_scenes
                if scene.enabled
                for panel_id in (scene.panel_ids or [])
            }
            current_ids = valid_panel_ids or set()
            extra_ids = [
                panel_id
                for panel_id in sorted(enabled_panel_ids)
                if panel_id not in current_ids
                and panel_id in all_panel_map
                and all_panel_map[panel_id].non_story_type
                not in ("copyright_notice", "promo_banner")
            ]
            if extra_ids:
                if valid_panel_ids is None:
                    valid_panel_ids = set()
                valid_panel_ids.update(extra_ids)
                for panel_id in extra_ids:
                    panel_map[panel_id] = all_panel_map[panel_id]
                ordered_panel_ids = _ordered_panel_ids(valid_panel_ids)
                print(
                    "[s4_scriptwriter] 用户编排额外放行分镜："
                    + "、".join(extra_ids),
                    file=sys.stderr,
                )
        if scene_mode and active_scenes and resolved_storyboard is not None:
            # Patch v4.0 Patch 20.1：被用户禁用（enabled=False）的镜头组，
            # 其**独有**分镜整格排除——既不进入重要分镜统计，也不进入
            # 有效分镜集合，避免讲出用户明确关掉的那一幕。
            enabled_panel_ids = {
                panel_id
                for scene in active_scenes
                for panel_id in (scene.panel_ids or [])
            }
            disabled_panel_ids = {
                panel_id
                for scene in resolved_storyboard.scenes
                if not scene.enabled
                for panel_id in (scene.panel_ids or [])
            }
            drop_ids = disabled_panel_ids - enabled_panel_ids
            if drop_ids and valid_panel_ids is not None:
                valid_panel_ids = valid_panel_ids - drop_ids
                ordered_panel_ids = _ordered_panel_ids(valid_panel_ids)
                for panel_id in drop_ids:
                    panel_map.pop(panel_id, None)
                print(
                    "[s4_scriptwriter] 已排除被禁用镜头组的分镜："
                    + "、".join(sorted(drop_ids)),
                    file=sys.stderr,
                )
    story_word_low, story_word_high = compute_story_word_budget(
        max(1, len(active_scenes)), len(ordered_panel_ids)
    )
    story_word_budget = f"{story_word_low}~{story_word_high} 字"
    storyboard_constraints = _storyboard_constraints_block(
        active_scenes if scene_mode else []
    )

    # ------------------------------------------------------------------
    # Stage 4A：整话通读 → 故事反写（章节稿）
    # ------------------------------------------------------------------
    previous_draft = _load_story_draft(story_draft_path)
    human_markdown = _resolve_human_markdown(
        story_markdown_path,
        previous_draft,
        episode_no=context.episode_no,
        explicit=story_source_markdown,
    )
    if require_story_source and human_markdown is None:
        raise ScriptWriterError(
            "没有找到可用的故事初稿（04_script/story_draft.md），"
            "请先执行 w2v story-draft 生成初稿并人工审阅"
        )

    story_draft: EpisodeStoryDraft | None = None
    if human_markdown is not None:
        sections = parse_story_markdown(human_markdown)
        if not sections:
            raise ScriptWriterError(
                "故事初稿里没有解析出任何章节（每章需要以 '## ' 开头的标题行）"
            )
        story_draft = story_draft_from_sections(
            episode_id,
            sections,
            ordered_panel_ids=ordered_panel_ids,
            base=previous_draft,
        )
        print(
            "[s4_scriptwriter] 检测到人工改稿的故事初稿，直接采用（跳过 4A 故事反写）。",
            file=sys.stderr,
        )
    else:
        try:
            story_draft = await _write_story_draft(
                client,
                model=story_model or model,
                context=context,
                panels=story_panels,
                episode_id=episode_id,
                series_title=series_title,
                template=story_draft_template,
                temperature=temperature,
                story_word_budget=story_word_budget,
                storyboard_constraints=storyboard_constraints,
            )
        except Exception as exc:  # noqa: BLE001 - 故事反写失败不阻断流水线
            # 故事反写失败不阻断流水线：退回整话概览作为 4B 的锚点。
            print(
                f"[s4_scriptwriter] 故事反写失败，退回概览兜底：{exc}",
                file=sys.stderr,
            )
            story_draft = _fallback_story_draft(episode_id, panels, overview_text)

    if story_draft_path is not None:
        Path(story_draft_path).parent.mkdir(parents=True, exist_ok=True)
        save_model(story_draft, Path(story_draft_path))
    if story_markdown_path is not None:
        markdown_path = Path(story_markdown_path)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        if human_markdown is None or force_story_markdown:
            # 自动稿只在「人工没改过」时覆写，避免抹掉人工改稿。
            markdown_path.write_text(
                render_story_markdown(story_draft, episode_no=context.episode_no),
                encoding="utf-8",
            )

    # ------------------------------------------------------------------
    # Stage 4B：逐章拆拍 + 顺位/配音文本硬校验（失败打回重写）
    # ------------------------------------------------------------------
    important_panel_ids = _important_panel_ids(
        panels,
        active_scenes,
        scene_mode=scene_mode,
    )
    chapter_blocks = _chapter_blocks(story_draft, ordered_panel_ids)
    if not chapter_blocks:
        chapter_blocks = [
            (
                StoryChapter(
                    chapter_index=1,
                    chapter_title="整话",
                    panel_hint_range=("", ""),
                    story_prose=story_draft.full_story_markdown,
                ),
                list(ordered_panel_ids),
            )
        ]

    merged_beats: list[dict] = []
    errors: list[str] = []
    budget_low, budget_high = _chapter_beat_budget(len(chapter_blocks))
    # Patch v4.0 Patch 19：Scene-Grounded 逐幕计划。
    #
    # * 默认仍按 4A 章节稿拆拍（向后兼容 Patch v3.x 的行为与历史单测）；
    # * 启用镜头组编排时，改为「一幕一章一章拍」，窗口锁定该幕自己的分镜。
    chapter_plan: list[tuple[StoryChapter, list[str], StoryboardScene | None]] = [
        (chapter, window, None) for chapter, window in chapter_blocks
    ]
    scene_budgets: list[str] = []
    if scene_mode and active_scenes:
        chapters = list(story_draft.chapters or [])
        # Patch v4.0 Patch 20.1：章幕数量不等时按「分镜区间重叠」配对，
        # 避免按下标错位（详见 _pair_chapters_with_scenes）。
        pairs = _pair_chapters_with_scenes(chapters, active_scenes, ordered_panel_ids)
        used_chapters = set(pairs.values())
        unused = [
            index for index in range(len(chapters)) if index not in used_chapters
        ]
        if unused and pairs:
            # 没被任何幕选中的章节：正文并入「幕序号最大的已配对章节」，不丢内容。
            target = max(pairs)
            extra = "\n".join(
                (chapters[index].story_prose or "") for index in unused
            ).strip()
            if extra:
                base = chapters[target]
                chapters[target] = base.model_copy(
                    update={
                        "story_prose": "\n".join(
                            [base.story_prose or "", extra]
                        ).strip()
                    }
                )
        chapter_plan = []
        for index, scene in enumerate(active_scenes):
            chapter_index = pairs.get(index)
            if chapter_index is not None:
                chapter = chapters[chapter_index]
            else:
                chapter = StoryChapter(
                    chapter_index=index + 1,
                    chapter_title=scene.title or f"第 {index + 1} 幕",
                    panel_hint_range=(
                        (scene.panel_ids[0], scene.panel_ids[-1])
                        if scene.panel_ids
                        else ("", "")
                    ),
                    story_prose=scene.visual_recap or scene.user_note or "",
                )
            window = [
                panel_id
                for panel_id in scene.panel_ids
                if not valid_panel_ids or panel_id in valid_panel_ids
            ]
            chapter_plan.append((chapter, window, scene))
        episode_low, episode_high = compute_episode_beat_budget(
            len(active_scenes), len(ordered_panel_ids)
        )
        scene_budgets = _split_budget(
            f"{episode_low}~{episode_high} 条", len(chapter_plan)
        )
    # Patch v3.1 Patch 10：4A 故事稿自检（元词汇 / 「他/她」连环开头）。
    # 只告警 + 把问题反馈给 4B，不阻断流水线。
    style_feedback = _story_style_feedback(story_draft)
    if style_feedback:
        print(f"[s4_scriptwriter] {style_feedback}", file=sys.stderr)
    for attempt in range(ALIGNMENT_MAX_RETRIES + 1):
        merged_beats = []
        chapter_errors: list[str] = []
        previous_tail = ""
        for plan_index, (chapter, window, scene) in enumerate(chapter_plan):
            # 真机修复：反馈按章过滤，模型只收到自己候选窗口内能修的错误
            # （不含 panel id 的拼音/空绑定类错误仍全局广播）。
            chapter_feedback = "\n".join(
                f"- {item}" for item in _filter_feedback_for_window(errors, window)
            )
            if style_feedback:
                chapter_feedback = (
                    f"{chapter_feedback}\n{style_feedback}" if chapter_feedback else style_feedback
                )
            chapter_budget = (
                scene_budgets[plan_index]
                if scene_budgets and plan_index < len(scene_budgets)
                else f"{budget_low}~{budget_high} 条"
            )
            raw_beats = await _write_narration_chapter(
                client,
                model=model,
                context=context,
                chapter=chapter,
                panel_ids=window,
                panel_map=panel_map,
                story_full=story_draft.full_story_markdown or chapter.story_prose,
                previous_tail=previous_tail,
                alignment_feedback=chapter_feedback,
                episode_id=episode_id,
                series_title=series_title,
                template=beats_template,
                temperature=temperature,
                beat_budget=chapter_budget,
                scene_directives=_scene_directives_block(scene),
            )
            if not raw_beats:
                chapter_errors.append(
                    f"第 {chapter.chapter_index} 章"
                    f"（{chapter.chapter_title or '未命名'}）没有产出解说句"
                )
            merged_beats.extend(raw_beats)
            previous_tail = _tail_narration(raw_beats)

        # 各章模型可能各自从 b1 编号，合并后统一重排，保证 beat_id 全话唯一。
        for index, beat in enumerate(merged_beats, start=1):
            beat["beat_id"] = f"b{index}"

        # 模型/历史产物可能引用 panel_001 这类旧写法，先归一化成本话的规范 ID，
        # 顺位校验与后续 normalize_script_payload 才能认得它们。
        _canonicalize_beat_panel_ids(merged_beats, valid_panel_ids)

        # Patch v3.2 Patch 12：确定性拆分「旁白 + 引号对白」混写的 Beat，
        # 让角色原话由角色声线念出（拆分后会重新编号 b1, b2, …）。
        merged_beats = split_mixed_quote_beats(
            merged_beats, characters=context.characters
        )

        # Patch v3.4 Patch 17：相邻重复对白去重（同一句连说两遍）+ 连续对白
        # 说话人继承（没写「XX说：」的纯引号对白继承上一句的说话人）。
        merged_beats = deduplicate_adjacent_beats(merged_beats)
        merged_beats = apply_dialogue_speaker_inheritance(merged_beats)
        for index, beat in enumerate(merged_beats, start=1):
            beat["beat_id"] = f"b{index}"

        errors = chapter_errors
        errors += validate_beat_alignment(
            merged_beats,
            ordered_panel_ids,
            important_panel_ids=important_panel_ids,
        )
        errors += validate_tts_text_no_pinyin(merged_beats)
        if not errors:
            break
        print(
            f"[s4_scriptwriter] 第 {attempt + 1} 轮校验未通过（{len(errors)} 项），"
            "把错误详情打回模型重写。",
            file=sys.stderr,
        )
        for item in errors:
            print(f"[s4_scriptwriter]   - {item}", file=sys.stderr)
    if errors:
        print(
            "[s4_scriptwriter] 重写次数已用尽，采用最后一版结果（顺序可能仍有偏差）。",
            file=sys.stderr,
        )
        # 兜底 1：把 tts_text 里的拼音剥掉（字幕 narration_text 绝不受影响）。
        _strip_pinyin_from_beats(merged_beats)
        # 兜底 2：确定性钳制仍回看的 beat，保证落盘剧本 100% 全局单调不减。
        clamp_logs = _clamp_regressive_beats(merged_beats, ordered_panel_ids)
        for line in clamp_logs:
            print(f"[s4_scriptwriter]   - 顺位钳制：{line}", file=sys.stderr)
        remaining = validate_beat_alignment(
            merged_beats,
            ordered_panel_ids,
            important_panel_ids=important_panel_ids,
        )
        if remaining:
            print(
                f"[s4_scriptwriter] 钳制后仍有 {len(remaining)} 项顺位异常：{remaining}",
                file=sys.stderr,
            )
        else:
            print(
                f"[s4_scriptwriter] 顺位钳制完成，共修正 {len(clamp_logs)} 条解说。",
                file=sys.stderr,
            )

    # ------------------------------------------------------------------
    # Patch v4.0 Patch 19：镜头组覆盖硬校验 + 缺失幕单幕定向补写
    # ------------------------------------------------------------------
    if scene_mode and active_scenes:
        missing_scenes = validate_storyboard_coverage(merged_beats, active_scenes)
        if missing_scenes:
            titles = "、".join(
                f"第 {scene.scene_index} 幕（{scene.title or '未命名'}）"
                for scene in missing_scenes
            )
            print(
                f"[s4_scriptwriter] 镜头组覆盖不足：{titles} 缺少解说或必保原话，"
                "逐幕定向补写。",
                file=sys.stderr,
            )
            for scene_index, scene in enumerate(missing_scenes):
                plan_index = next(
                    (
                        index
                        for index, item in enumerate(chapter_plan)
                        if item[2] is not None and item[2].scene_id == scene.scene_id
                    ),
                    None,
                )
                if plan_index is None:
                    continue
                chapter, window, _ = chapter_plan[plan_index]
                locked = "、".join(scene.locked_quotes or [])
                reason = (
                    f"上一稿漏掉了本幕必保台词「{locked}」或未覆盖尾幕分镜，"
                    "请完整讲述本幕冲突与反转。"
                    if locked
                    else "上一稿未覆盖本幕分镜，请完整讲述本幕冲突与反转。"
                )
                budget = (
                    scene_budgets[plan_index]
                    if scene_budgets and plan_index < len(scene_budgets)
                    else f"{budget_low}~{budget_high} 条"
                )
                new_beats = await _write_narration_chapter(
                    client,
                    model=model,
                    context=context,
                    chapter=chapter,
                    panel_ids=window,
                    panel_map=panel_map,
                    story_full=story_draft.full_story_markdown or chapter.story_prose,
                    previous_tail="",
                    alignment_feedback=reason,
                    episode_id=episode_id,
                    series_title=series_title,
                    template=beats_template,
                    temperature=temperature,
                    beat_budget=budget,
                    scene_directives=_scene_directives_block(scene),
                )
                if not new_beats:
                    continue
                merged_beats = _splice_scene_beats(
                    merged_beats, scene, new_beats, ordered_panel_ids
                )
            for index, beat in enumerate(merged_beats, start=1):
                beat["beat_id"] = f"b{index}"
            _canonicalize_beat_panel_ids(merged_beats, valid_panel_ids)
            merged_beats = split_mixed_quote_beats(
                merged_beats, characters=context.characters
            )
            merged_beats = deduplicate_adjacent_beats(merged_beats)
            merged_beats = apply_dialogue_speaker_inheritance(merged_beats)
            for index, beat in enumerate(merged_beats, start=1):
                beat["beat_id"] = f"b{index}"
            errors = validate_beat_alignment(
                merged_beats,
                ordered_panel_ids,
                important_panel_ids=important_panel_ids,
            )
            errors += validate_tts_text_no_pinyin(merged_beats)
            if errors:
                # Patch v4.0 Patch 20.1：补写合并后若仍有顺位/拼音问题，
                # 与 4B 重试耗尽时走同一套兜底（剥拼音 + 夹逼顺位），
                # 避免因为补写引入的异常直接漏到下游渲染。
                _strip_pinyin_from_beats(merged_beats)
                _clamp_regressive_beats(merged_beats, ordered_panel_ids)
                errors = validate_beat_alignment(
                    merged_beats,
                    ordered_panel_ids,
                    important_panel_ids=important_panel_ids,
                )
                errors += validate_tts_text_no_pinyin(merged_beats)
                if errors:
                    print(
                        f"[s4_scriptwriter] 补写后仍有 {len(errors)} 项校验异常：{errors}",
                        file=sys.stderr,
                    )

    # Patch v4.6：叙事节奏自动留白。短剧本（<6 拍且非多幕）保持模型原始停顿，
    # 避免把一两句话的短解说也按长篇节奏拉开；S4 阶段尚无真实 TTS 时长，
    # 规则 C 的句长按字幕字数估算（真实时长在 Web 端 auto-pacing 接口里补齐）。
    if len(merged_beats) >= 6 or (scene_mode and len(active_scenes) >= 2):
        merged_beats, pacing_report = apply_narrative_pacing(
            merged_beats,
            scenes=active_scenes if scene_mode else None,
        )
        if pacing_report.updated_count > 0:
            print(
                "[s4_scriptwriter] 自动节奏留白："
                f"共调整 {pacing_report.updated_count} 处停顿"
                f"（跨幕 {pacing_report.scene_end_count} / "
                f"转折 {pacing_report.turn_count} / "
                f"换气 {pacing_report.breath_count}），"
                f"beat={pacing_report.changed_beat_ids}",
                file=sys.stderr,
            )

    payload = {
        "hook_title": overview.get("hook_title"),
        "episode_digest": overview.get("episode_digest"),
        "character_updates": overview.get("character_updates"),
        "new_plot_threads": overview.get("new_plot_threads"),
        "resolved_thread_ids": overview.get("resolved_thread_ids"),
        "beats": merged_beats,
    }
    normalized = normalize_script_payload(
        payload,
        episode_id=episode_id,
        episode_no=context.episode_no,
        valid_panel_ids=valid_panel_ids,
    )
    try:
        script = NarrationScript.model_validate(normalized)
    except Exception as exc:  # pydantic ValidationError
        raise ScriptWriterError(f"模型返回的剧本不合法：{exc}") from exc
    if not script.beats:
        raise ScriptWriterError("模型没有产出任何可用的解说句（beats 为空）")
    _apply_focus_hints(script, merged_beats, valid_panel_ids)
    # 话号与话次 ID 由流水线决定，模型给出的值一律以流水线为准。
    script.episode_id = episode_id
    script.episode_digest.episode_no = context.episode_no
    return script


def _load_story_draft(story_draft_path: Path | None) -> EpisodeStoryDraft | None:
    """读取上一轮落盘的故事稿（用于人工稿回填分镜区间）。"""
    if story_draft_path is None:
        return None
    path = Path(story_draft_path)
    if not path.is_file():
        return None
    try:
        return load_model(EpisodeStoryDraft, path)
    except Exception:  # noqa: BLE001 - 历史产物格式异常时按「没有旧稿」处理
        return None


def _resolve_human_markdown(
    story_markdown_path: Path | None,
    previous_draft: EpisodeStoryDraft | None,
    *,
    episode_no: int,
    explicit: str | None = None,
) -> str | None:
    """判断是否有人工改稿，返回人工稿正文（没有则返回 ``None``）。

    判定规则：磁盘上的 ``story_draft.md`` 与「上一轮自动稿渲染结果」不一致，
    就认为被人工改过；没有历史 JSON 时，磁盘上存在的稿子一律当人工稿。
    """
    if explicit is not None and explicit.strip():
        return explicit
    if story_markdown_path is None:
        return None
    path = Path(story_markdown_path)
    if not path.is_file():
        return None
    try:
        on_disk = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not on_disk.strip():
        return None
    if previous_draft is None:
        return on_disk
    auto_markdown = render_story_markdown(previous_draft, episode_no=episode_no)
    if on_disk.strip() != auto_markdown.strip():
        return on_disk
    return None


def extract_vision_keywords(vision: VisionWindowOutput | None) -> list[str]:
    """从视觉结果里抽取召回关键词（角色名 + 对白里的角色名）。"""
    keywords: list[str] = []
    if vision is None:
        return keywords
    for panel in vision.panels:
        for dialogue in panel.dialogues:
            speaker = (dialogue.speaker_guess or "").strip()
            if speaker and speaker not in {"未知角色", "旁白"} and speaker not in keywords:
                keywords.append(speaker)
    return keywords


async def _archive_arc(
    manager: StoryBibleManager,
    script: NarrationScript,
    *,
    compressor: ArcCompressor | None,
) -> ArcSummary | None:
    """每满 N 话归档一条篇章摘要（压缩器可用时走百炼 ``qwen-plus``）。"""
    episode_no = script.episode_digest.episode_no
    if compressor is None:
        return None
    raw = manager.build_arc_raw_text(episode_no, extra_digest=script.episode_digest)
    if raw is None:
        return None
    interval = manager.arc_summary_interval
    start = episode_no - interval + 1
    summary = await compressor(raw)
    return ArcSummary(
        arc_name=f"第{start}-{episode_no}话篇章",
        ep_range=(start, episode_no),
        summary=summary,
    )


async def write_story_draft_document(
    series_dir: str | Path,
    episode_no: int,
    client: LLMClient,
    *,
    model: str,
    vision: VisionWindowOutput | None = None,
    manager: StoryBibleManager | None = None,
    episode_id: str = "",
    series_title: str = "",
    template: str | None = None,
    temperature: float = 0.7,
    force: bool = False,
) -> tuple[EpisodeStoryDraft, Path, Path]:
    """只跑 Stage 4A：生成故事初稿并落盘，返回 ``(故事稿, json 路径, md 路径)``。

    供 ``w2v story-draft`` 使用：产出 ``04_script/story_draft.json`` 与
    人工可编辑的 ``04_script/story_draft.md``，然后停下等待人工审阅。
    已有初稿时默认拒绝覆写（``force=True`` 才覆盖）。
    """
    series_root = Path(series_dir)
    memory = manager or StoryBibleManager(series_root, series_title=series_title)
    if vision is None:
        vision_path = (
            series_root / "episodes" / f"ep_{episode_no:03d}" / "03_vision" / "vision.json"
        )
        if vision_path.is_file():
            vision = load_model(VisionWindowOutput, vision_path)
    panels = [
        panel
        for panel in (vision.panels if vision else [])
        if not panel.is_transition_blank and panel.non_story_type == "none"
    ]
    target_dir = script_dir_for(series_root, episode_no)
    json_path = target_dir / STORY_DRAFT_FILENAME
    markdown_path = target_dir / STORY_DRAFT_MD_FILENAME
    if markdown_path.is_file() and not force:
        raise ScriptWriterError(
            f"故事初稿已存在：{markdown_path}（如需重新生成请加 --force）"
        )
    context = memory.build_stage4_context(
        episode_no, vision_keywords=extract_vision_keywords(vision)
    )
    draft = await _write_story_draft(
        client,
        model=model,
        context=context,
        panels=panels,
        episode_id=episode_id or f"ep_{episode_no:03d}",
        series_title=series_title,
        template=template,
        temperature=temperature,
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    save_model(draft, json_path)
    markdown_path.write_text(
        render_story_markdown(draft, episode_no=episode_no), encoding="utf-8"
    )
    return draft, json_path, markdown_path


async def run_stage4(
    series_dir: str | Path,
    episode_no: int,
    client: LLMClient,
    *,
    model: str,
    episode_id: str = "",
    series_title: str = "",
    vision: VisionWindowOutput | None = None,
    manager: StoryBibleManager | None = None,
    overview_template: str | None = None,
    beats_template: str | None = None,
    story_draft_template: str | None = None,
    arc_compressor: ArcCompressor | None = None,
    story_model: str = "",
    story_markdown_path: str | Path | None = None,
    story_source_markdown: str | None = None,
    require_story_source: bool = False,
    force_story_markdown: bool = False,
    storyboard: EpisodeStoryboard | None = None,
    storyboard_path: str | Path | None = None,
    vision_client: LLMClient | None = None,
    vision_model: str = "",
    panels_dir: str | Path | None = None,
    reset_storyboard: bool = False,
) -> tuple[NarrationScript, Path]:
    """执行 Stage 4：出稿 → 落盘 → 更新记忆与快照，返回 ``(剧本, script.json 路径)``。

    产物：``04_script/script.json``、``04_script/script_readable.md``、
    ``04_script/story_draft.json``（Stage 4A 的故事稿）与
    ``04_script/story_draft.md``（人工可编辑的故事初稿）。

    Patch v4.0 Patch 19：传入 ``storyboard_path`` 时启用 Scene-Grounded 逐幕撰稿，
    并把镜头组编排落盘到 ``04_script/storyboard.json``（人工编排不会被覆写）。
    """
    series_root = Path(series_dir)
    resolved_episode_id = episode_id or f"ep_{episode_no:03d}"
    memory = manager or StoryBibleManager(series_root, series_title=series_title)

    if vision is None:
        vision_path = (
            series_root / "episodes" / f"ep_{episode_no:03d}" / "03_vision" / "vision.json"
        )
        if vision_path.is_file():
            vision = load_model(VisionWindowOutput, vision_path)

    base, source = memory.resolve_base_bible(episode_no)
    context = memory.build_stage4_context(
        episode_no, vision_keywords=extract_vision_keywords(vision)
    )
    target_dir = script_dir_for(series_root, episode_no)
    target_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = (
        Path(story_markdown_path)
        if story_markdown_path is not None
        else target_dir / STORY_DRAFT_MD_FILENAME
    )
    # Patch v4.0 Patch 19：镜头组编排（缺失时自动聚类并落盘；人工编排原样保留）。
    resolved_storyboard = storyboard
    if storyboard_path is not None:
        from stages import s3b_scenes

        board_path = Path(storyboard_path)
        if resolved_storyboard is None and vision is not None:
            resolved_storyboard = s3b_scenes.load_or_build_storyboard(
                resolved_episode_id,
                vision,
                board_path,
                reset=reset_storyboard,
            )
    storyboard_sink: list[EpisodeStoryboard] = []
    script = await write_episode_script(
        client,
        model=model,
        context=context,
        episode_id=resolved_episode_id,
        vision=vision,
        series_title=series_title,
        overview_template=overview_template,
        beats_template=beats_template,
        story_draft_template=story_draft_template,
        story_draft_path=target_dir / STORY_DRAFT_FILENAME,
        story_model=story_model,
        story_markdown_path=markdown_path,
        story_source_markdown=story_source_markdown,
        require_story_source=require_story_source,
        force_story_markdown=force_story_markdown,
        storyboard=resolved_storyboard,
        storyboard_path=storyboard_path,
        vision_client=vision_client,
        vision_model=vision_model,
        panels_dir=panels_dir,
        reset_storyboard=reset_storyboard,
        storyboard_sink=storyboard_sink,
    )
    # Patch v4.0 Patch 20.1：auto 版镜头组**总是**回写（把整幕视觉复核得到的
    # visual_recap 落盘，下次打开镜头编排 Tab 直接能看到摘要）；
    # 人工编排（source == "human"）一律不覆写用户的 user_note / locked_quotes。
    final_storyboard = storyboard_sink[-1] if storyboard_sink else resolved_storyboard
    if (
        storyboard_path is not None
        and final_storyboard is not None
        and final_storyboard.source != "human"
    ):
        from stages import s3b_scenes

        s3b_scenes.save_storyboard(final_storyboard, Path(storyboard_path))

    script_path = target_dir / SCRIPT_FILENAME
    save_model(script, script_path)
    (target_dir / READABLE_FILENAME).write_text(
        render_readable_script(script, base_source=source), encoding="utf-8"
    )

    arc = await _archive_arc(memory, script, compressor=arc_compressor)
    memory.apply_episode_result(script, base=base, arc_summary=arc)
    return script, script_path


def build_arc_compressor(
    client: LLMClient,
    *,
    model: str,
    max_chars: int = 600,
) -> ArcCompressor:
    """构造走百炼 ``qwen-plus`` 的篇章摘要压缩器。"""

    async def compress(raw: str) -> str:
        prompt = (
            "下面是某部漫画连续若干话的剧情详摘，请压缩成一段不超过 "
            f"{max_chars} 字的阶段性剧情摘要，保留关键转折与人物变化，不要评价、不要抒情。\n\n{raw}"
        )
        response = await run_with_model_fallback(
            model,
            lambda chosen: client.chat(
                [ChatMessage(role="user", content=prompt)],
                model=chosen,
                temperature=0.3,
                **_NO_THINKING_KWARGS,
            ),
            label="篇章记忆压缩",
        )
        text = response.content.strip()
        if len(text) > max_chars:
            text = text[: max(1, max_chars - 1)] + "…"
        return text

    return compress


def summarize_episode_digests(digests: Sequence[object]) -> str:
    """把若干详摘拼成一段文本（调试与测试辅助）。"""
    lines: list[str] = []
    for digest in digests:
        episode_no = getattr(digest, "episode_no", 0)
        hook = getattr(digest, "one_line_hook", "")
        summary = getattr(digest, "summary", "")
        lines.append(f"第{episode_no}话：{hook}｜{summary}")
    return "\n".join(lines)
