"""Patch v4.16：角色名全链路自动统一（同音错字 / 别名 → 唯一规范名）。

背景
----
Stage 4 的模型（尤其是 DeepSeek 系列）会把同一个角色的名字写成**同音不同字**
（``恩彗`` / ``恩慧`` / ``恩惠``），或者一会儿用全名、一会儿用外貌称呼
（``Bam`` / ``黑发橙眼少年``）。后果是：

* 角色表里同一个人被拆成多条档案（StoryBible Layer 1 重名）；
* 解说词里同一角色在不同句子用不同名字，审核台看着像中途换了个人；
* 角色专属音色匹配不上——``series_tts.character_voices`` 的键是规范名，
  写成同音错字就落回通用音色。

本模块提供 :class:`CharacterNameCanonicalizer`：给定「已知角色名 + 别名 +
作品级别名 + 本话出现的候选名」，产出一份「变体 → 规范名」的映射，并且
**分成两张表**：

* ``role_map``：用于**角色位**（``speaker_role`` / ``characters_present`` /
  ``character_updates[*].name``）——简称、别名、同音变体统统归一到规范名；
* ``text_rules``：用于**正文**（``narration_text`` / ``tts_text`` /
  故事稿正文 / 标题）——只做等长或变长方向的替换，且过滤掉外貌称呼与
  单字变体，避免「黑发橙眼少年」被塞进解说词、避免「小恩小惠」被改成
  「小恩小彗」。

规范名的优先级（规格要求）：

1. **已配置专属音色**（``series_tts.character_voices`` 的键）——用户手工配过的最权威；
2. **已有 StoryBible**（``story_bible.json`` 里的角色名）；
3. **出现频次更高者**（本话正文里出现更多的写法胜出）。

第 1 话还没有 StoryBible 时，只要作品配了角色专属音色，或本话正文里出现了
``恩彗`` / ``恩慧`` / ``恩惠`` 这组同音写法，仍能自发现并定出唯一规范名。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.normalize import to_simplified_chinese

__all__ = [
    "CharacterCheckWarning",
    "CharacterNameCanonicalizer",
    "are_homophone_name_variants",
    "build_character_canonicalizer",
]


# ----------------------------------------------------------------------
# Patch v4.23：只读检查用的结构化告警
# ----------------------------------------------------------------------
#: 告警原因：别名写法（``南道振`` → ``道振``）。
_REASON_ALIAS_VARIANT = "alias_variant"
#: 告警原因：同音异写（``恩慧`` → ``恩彗``）。
_REASON_HOMOPHONE_VARIANT = "homophone_variant"
#: 告警原因：说话人不在作品角色表里。
_REASON_UNREGISTERED_SPEAKER = "unregistered_speaker"

#: 不参与角色名检查的通用说话人标记（Patch v4.23）。
#:
#: 旁白之外，流水线还会把 ``male`` / ``female`` 这类**性别槽位**写进
#: ``speaker_role``（见 ``s4_scriptwriter._GENDER_BY_ROLE_SLOT``），它们不是
#: 角色本名，拿去和作品角色表比对只会得到一堆误报。
_GENERIC_ROLE_NAMES: frozenset[str] = frozenset(
    {
        "narrator",
        "旁白",
        "解说",
        "独白",
        "内心独白",
        "male",
        "male_lead",
        "male_antagonist",
        "female",
        "female_lead",
        "female_mature",
        "unknown",
        "未知",
        "众人",
        "群像",
        "音效",
        "sfx",
    }
)


@dataclass(frozen=True)
class CharacterCheckWarning:
    """人工保存前的「角色名写法」告警（Patch v4.23）。

    **只读**：本结构只是提示，绝不参与任何文本改写。字段与后端
    ``POST /api/episodes/{id}/script/check-characters`` 的响应一一对应。
    """

    #: 所属解说句的 ID。
    beat_id: str
    #: 所属解说句在本话中的序号（0-based）。
    beat_index: int
    #: 出问题的字段：``speaker_role`` / ``narration_text`` / ``tts_text``。
    field: str
    #: 当前写法。
    found_name: str
    #: 建议的规范名；``None`` 表示没有建议（说话人未登记）。
    suggested_canonical: str | None
    #: 原因：``alias_variant`` / ``homophone_variant`` / ``unregistered_speaker``。
    reason: str
    #: 给人看的一句话说明。
    message: str
    #: 上下文片段（出错位置前后各若干字）。
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        """转成可直接 JSON 序列化的字典。"""
        return {
            "beat_id": self.beat_id,
            "beat_index": self.beat_index,
            "field": self.field,
            "found_name": self.found_name,
            "suggested_canonical": self.suggested_canonical,
            "reason": self.reason,
            "message": self.message,
            "snippet": self.snippet,
        }


# ----------------------------------------------------------------------
# 同音字簇
# ----------------------------------------------------------------------
#: 同音（近音）字簇：同一簇内的字读音相同或极近，模型写错时最常互换。
#: 只收录**人名里真的会混用**的字，避免把常用词拉进来造成误伤。
_HOMOPHONE_CLUSTERS: tuple[tuple[str, ...], ...] = (
    ("彗", "慧", "惠", "蕙"),
    ("振", "真", "珍", "镇", "祯"),
    ("敏", "珉", "民", "闵"),
    ("宇", "雨", "羽", "禹"),
    ("贤", "弦", "娴", "轩", "宣", "炫"),
    ("熙", "希", "曦", "熹", "锡"),
    ("雅", "亚", "娅"),
    ("妍", "研", "延", "颜"),
    ("娜", "呐"),
    ("赫", "贺", "鹤"),
    ("俊", "峻", "骏", "浚"),
    ("浩", "昊", "皓", "豪"),
    ("哲", "喆", "辙"),
    ("琳", "林", "霖"),
    ("智", "志", "致", "知"),
    ("静", "净", "靖"),
    ("瑞", "睿", "锐"),
    ("成", "城", "诚"),
    ("泰", "太"),
    ("正", "政", "郑"),
    ("秀", "绣"),
    ("珠", "朱"),
    ("灿", "璨", "粲"),
    ("彬", "宾", "斌"),
    ("勋", "熏", "薰"),
    ("盛", "胜", "圣"),
    ("仁", "人"),
    ("在", "载", "宰"),
    ("彩", "采"),
)


def _build_homophone_index() -> dict[str, frozenset[str]]:
    """字 → 所属同音簇（一个字只归一簇，避免跨簇串联）。"""
    index: dict[str, frozenset[str]] = {}
    for cluster in _HOMOPHONE_CLUSTERS:
        members = frozenset(cluster)
        for char in cluster:
            index.setdefault(char, members)
    return index


_HOMOPHONE_INDEX: dict[str, frozenset[str]] = _build_homophone_index()


def are_homophone_name_variants(name_a: str, name_b: str) -> bool:
    """判断两个名字是否为「同音异写」的写法（Patch v4.16）。

    判定条件（全部满足才为 True）：

    * 两个字面转简体后**等长**且长度 ≥ 2（单字名不做同音判定，太容易误伤）；
    * **恰好一个**位置的字不同；
    * 那个不同的两个字属于同一个同音簇。

    ``恩彗`` vs ``恩惠`` → True；``恩彗`` vs ``恩慧`` → True；
    ``恩彗`` vs ``小恩`` → False（长度不同）。
    """
    left = to_simplified_chinese(str(name_a or "")).strip()
    right = to_simplified_chinese(str(name_b or "")).strip()
    if not left or not right or len(left) != len(right) or len(left) < 2:
        return False
    diff = [index for index in range(len(left)) if left[index] != right[index]]
    if len(diff) != 1:
        return False
    left_char = left[diff[0]]
    right_char = right[diff[0]]
    cluster = _HOMOPHONE_INDEX.get(left_char)
    if cluster is None:
        return False
    return right_char in cluster


# ----------------------------------------------------------------------
# 候选名提取与过滤
# ----------------------------------------------------------------------
#: 说话标记：候选角色名通常紧挨着这些字（``恩彗说`` / ``喊道恩慧``）。
_SPEAKER_MARK_AFTER = ("说", "道", "问", "答", "喊", "叫", "唤", "骂", "叹")
_SPEAKER_MARK_BEFORE = ("说", "道", "问", "答", "喊", "叫", "唤", "骂", "叹")

#: 明显不是人名的常见词/副词组合（候选提取时直接剔除）。
_STOPWORD_FRAGMENTS = (
    "大声", "小声", "冷笑", "苦笑", "微笑", "笑道", "喊道", "说道", "答道",
    "问道", "怒道", "叹道", "点头", "摇头", "抬头", "低头", "转身", "抬头",
    "这个", "那个", "自己", "什么", "怎么", "于是", "然后", "因为", "所以",
    "但是", "如果", "虽然", "突然", "已经", "还是", "就是", "不是", "没有",
    "他们", "她们", "我们", "你们", "大家", "对方", "此时", "这时", "那时",
    "众人", "两人", "三人", "一边", "一面", "第一", "第二", "第三",
)

#: 不能作为角色名的单字（代词 / 常见量词）。
_STOPWORD_CHARS = frozenset("他她它我你俺咱们这那啥谁哪啊吧呢吗哦嗯的地得着过")

#: 外貌 / 身份泛词：含这些词的别名是**描述性称呼**，不进正文替换表。
_DESCRIPTIVE_MARKERS = (
    "黑发", "棕发", "银发", "白发", "金发", "红发", "紫发", "蓝发", "卷发", "长发",
    "短发", "橙眼", "蓝眼", "绿眼", "紫眼", "灰眼", "金眼", "红眼", "异瞳",
    "少年", "少女", "男子", "女人", "男人", "女孩", "男孩", "女主", "男主",
    "主角", "反派", "老人", "青年", "女性", "男性", "小孩", "孩子", "大叔",
    "大妈", "小子", "丫头", "男人婆", "神秘人", "黑衣人", "面具男", "面具女",
)

#: 关系 / 身份泛称：含这些词的别名说的是「这个人的身份」而不是「这个人的
#: 名字」，同样不进正文替换表。
#:
#: Patch v4.20：story_bible 里常把「丈夫」「岳母」这类身份词登记成别名，
#: 于是用户写「我未来的丈夫」会被归一成角色本名「道振」——人工改好的字幕
#: 一保存就被冲回原文。角色位（``role_map``）不受影响，照常归一。
_RELATION_MARKERS = (
    "丈夫", "妻子", "老公", "老婆", "先生", "夫人", "太太", "小姐",
    "爸爸", "妈妈", "父亲", "母亲", "爸爸", "爹妈",
    "哥哥", "姐姐", "弟弟", "妹妹", "儿子", "女儿",
    "岳父", "岳母", "公公", "婆婆", "儿媳", "女婿",
    "嫂子", "姑妈", "姑父", "姨妈", "姨父", "舅舅", "舅妈",
    "叔叔", "婶婶", "伯伯", "伯母", "姑姑", "婶母",
    "师父", "师母", "徒弟", "师兄", "师姐", "学弟", "学妹",
    "未婚夫", "未婚妻", "前夫", "前妻", "恋人", "情侣",
    "对象", "邻居", "同事", "同学", "老师", "医生", "护士", "律师",
    "店长", "老板", "警察", "司机", "服务员", "快递员",
    "教练", "队友", "经纪人", "秘书", "助理", "管家", "仆人", "侍卫",
)

#: 惯用词前缀：``受人恩惠`` / ``施以恩惠`` / ``小恩小惠`` 这类搭配里的角色名
#: 只是普通词的一部分，替换会改变语义，必须保护。
_COMMON_PHRASE_PREFIXES = ("小", "受人", "施以", "给予", "报答", "感念", "莫大", "一点")
#: 惯用词后缀：``恩惠于`` / ``恩惠之``。
_COMMON_PHRASE_SUFFIXES = ("于", "之")

_CANDIDATE_AFTER_RE = re.compile(
    r"([\u4e00-\u9fff]{2,4})(?=" + "|".join(_SPEAKER_MARK_AFTER) + r")"
)
_CANDIDATE_BEFORE_RE = re.compile(
    r"(?<=[" + "|".join(_SPEAKER_MARK_BEFORE) + r"])([\u4e00-\u9fff]{2,4})"
)


def _is_plausible_name(candidate: str) -> bool:
    """候选名是否像一个人名（粗筛，避免把普通词当角色）。"""
    if len(candidate) < 2 or len(candidate) > 4:
        return False
    if any(char in _STOPWORD_CHARS for char in candidate):
        return False
    if any(fragment in candidate for fragment in _STOPWORD_FRAGMENTS):
        return False
    if any(marker in candidate for marker in _DESCRIPTIVE_MARKERS):
        return False
    return True


def extract_candidate_names(text: str) -> list[str]:
    """从正文里抽取「像角色名」的候选（Patch v4.16）。

    只在**说话标记旁边**抽取（``恩彗说`` / ``问道恩慧``），避免把普通词
    全部当成名字；再经 :func:`_is_plausible_name` 粗筛。
    """
    if not text:
        return []
    found: list[str] = []
    for match in _CANDIDATE_AFTER_RE.finditer(text):
        found.append(match.group(1))
    for match in _CANDIDATE_BEFORE_RE.finditer(text):
        found.append(match.group(1))
    return [item for item in found if _is_plausible_name(item)]


def _protected_spans(text: str, variant: str) -> list[tuple[int, int]]:
    """找出 ``variant`` 落在惯用词搭配里的区间（这些位置不能替换）。

    ``恩惠`` → 保护 ``小恩小惠``、``受人恩惠``、``施以恩惠``、``恩惠于``、
    ``恩惠之`` 等位置。
    """
    spans: list[tuple[int, int]] = []
    if not variant:
        return spans
    for prefix in _COMMON_PHRASE_PREFIXES:
        pattern = f"{re.escape(prefix)}{re.escape(variant)}"
        for match in re.finditer(pattern, text):
            spans.append(match.span())
    for suffix in _COMMON_PHRASE_SUFFIXES:
        pattern = f"{re.escape(variant)}{re.escape(suffix)}"
        for match in re.finditer(pattern, text):
            spans.append(match.span())
    return spans


def _overlaps(span: tuple[int, int], spans: Sequence[tuple[int, int]]) -> bool:
    start, end = span
    for other_start, other_end in spans:
        if start < other_end and other_start < end:
            return True
    return False


# ----------------------------------------------------------------------
# 规范名映射
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class CharacterNameCanonicalizer:
    """「变体 → 规范名」映射（Patch v4.16）。

    * ``role_map``：角色位用（``speaker_role`` 等），允许简称 / 别名 / 同音变体；
    * ``text_rules``：正文用（``(变体, 规范名)`` 元组，按变体长度降序），
      已过滤外貌称呼、单字变体与「简称拉长成全名」的情况。
    """

    canonical_names: tuple[str, ...] = ()
    role_map: Mapping[str, str] = field(default_factory=dict)
    text_rules: tuple[tuple[str, str], ...] = ()
    alias_notes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    # -- 角色位 -------------------------------------------------------
    def canonicalize_role(self, role: str) -> str:
        """归一 ``speaker_role`` 一类的角色位。

        旁白（``narrator`` / ``旁白`` / ``解说``）与空串原样保留。
        """
        text = str(role or "").strip()
        if not text:
            return role
        lowered = text.lower()
        if lowered in {"narrator", "旁白", "解说"}:
            return role
        return self.role_map.get(text, self.role_map.get(lowered, role))

    # -- 正文 ---------------------------------------------------------
    def canonicalize_text(self, text: str) -> str:
        """归一正文里的角色名写法（解说词 / 送音文本 / 故事稿正文）。"""
        if not text:
            return text
        result = text
        for variant, canonical in self.text_rules:
            if not variant or variant == canonical or variant not in result:
                continue
            protected = _protected_spans(result, variant)
            if not protected:
                result = result.replace(variant, canonical)
                continue
            # 有惯用词搭配：逐个匹配点判断，落在保护区内的原样保留。
            pieces: list[str] = []
            cursor = 0
            for match in re.finditer(re.escape(variant), result):
                if _overlaps(match.span(), protected):
                    continue
                pieces.append(result[cursor : match.start()])
                pieces.append(canonical)
                cursor = match.end()
            if not pieces:
                continue
            pieces.append(result[cursor:])
            result = "".join(pieces)
        return result

    def canonicalize_story_draft(self, draft: Any) -> Any:
        """归一故事稿（Stage 4A 产物）里的角色名。

        ``hook_opening`` / ``full_story_markdown`` / 章节标题与正文都会被处理。
        """
        if draft is None:
            return draft
        update: dict[str, Any] = {}
        for field_name in ("hook_opening", "full_story_markdown", "overall_plot_arc"):
            value = getattr(draft, field_name, None)
            if isinstance(value, str) and value:
                update[field_name] = self.canonicalize_text(value)
        chapters = getattr(draft, "chapters", None)
        if isinstance(chapters, list) and chapters:
            new_chapters = []
            for chapter in chapters:
                title = getattr(chapter, "chapter_title", "") or ""
                prose = getattr(chapter, "story_prose", "") or ""
                if (title and self.canonicalize_text(title) != title) or (
                    prose and self.canonicalize_text(prose) != prose
                ):
                    chapter = chapter.model_copy(
                        update={
                            "chapter_title": self.canonicalize_text(title),
                            "story_prose": self.canonicalize_text(prose),
                        }
                    )
                new_chapters.append(chapter)
            update["chapters"] = new_chapters
        if not update:
            return draft
        return draft.model_copy(update=update)

    def canonicalize_script_dict(self, payload: dict[str, Any]) -> dict[str, Any]:
        """归一 ``normalize_script_payload`` 的返回字典。

        * 每条 beat 的 ``speaker_role`` 走角色位归一；
        * ``narration_text`` / ``tts_text`` 走正文归一；
        * ``episode_digest.characters_present`` 与 ``character_updates[*].name``
          走角色位归一。
        """
        if not isinstance(payload, dict):
            return payload
        result = dict(payload)

        beats = result.get("beats")
        if isinstance(beats, list):
            new_beats: list[Any] = []
            for beat in beats:
                if not isinstance(beat, dict):
                    new_beats.append(beat)
                    continue
                beat = dict(beat)
                if "speaker_role" in beat:
                    beat["speaker_role"] = self.canonicalize_role(
                        str(beat.get("speaker_role") or "")
                    )
                for text_key in ("narration_text", "tts_text"):
                    value = beat.get(text_key)
                    if isinstance(value, str) and value:
                        beat[text_key] = self.canonicalize_text(value)
                new_beats.append(beat)
            result["beats"] = new_beats

        digest = result.get("episode_digest")
        if isinstance(digest, dict):
            digest = dict(digest)
            present = digest.get("characters_present")
            if isinstance(present, list):
                digest["characters_present"] = [
                    self.canonicalize_role(str(item)) for item in present
                ]
            result["episode_digest"] = digest

        updates = result.get("character_updates")
        if isinstance(updates, list):
            new_updates: list[Any] = []
            for item in updates:
                if not isinstance(item, dict):
                    new_updates.append(item)
                    continue
                item = dict(item)
                if item.get("name"):
                    item["name"] = self.canonicalize_role(str(item["name"]))
                new_updates.append(item)
            result["character_updates"] = new_updates

        return result

    # -- 只读检查（Patch v4.23） --------------------------------------
    def inspect_script_beats(self, beats: Sequence[Any]) -> list[CharacterCheckWarning]:
        """只读检查一批解说句里的角色名写法（Patch v4.23）。

        **不修改任何文本**：只产出告警，让前端弹确认框由用户决定要不要
        统一成规范名。Stage 4 的 AI 生成阶段仍然照旧自动归一
        （:meth:`canonicalize_script_dict`），这里只管人工编辑这一段。

        检查范围：

        * ``speaker_role``：写法不是规范名 → 报 ``alias_variant`` /
          ``homophone_variant``；写法不在作品角色表里 → 报
          ``unregistered_speaker``（没有建议名）。
        * ``narration_text`` / ``tts_text``：按 ``text_rules`` 扫描，
          复用 :func:`_protected_spans` 排除「小恩小惠」这类惯用词搭配，
          并按位置去重，同一处只报一次。
        """
        warnings: list[CharacterCheckWarning] = []
        for index, beat in enumerate(beats or []):
            beat_id = str(getattr(beat, "beat_id", "") or f"b{index + 1}")
            warnings.extend(self._inspect_speaker_role(beat, index, beat_id))
            narration = str(getattr(beat, "narration_text", "") or "")
            tts = str(getattr(beat, "tts_text", "") or "")
            # Patch v4.25：送音文本与字幕原文去空白后完全相同时，
            # 同一处角色名写法只报一次（否则同一句会被报两条重复告警）。
            same_text = bool(tts) and "".join(tts.split()) == "".join(narration.split())
            warnings.extend(self._inspect_text_field(beat, index, beat_id, "narration_text"))
            if not same_text:
                warnings.extend(self._inspect_text_field(beat, index, beat_id, "tts_text"))
        return warnings

    def _inspect_speaker_role(
        self, beat: Any, index: int, beat_id: str
    ) -> list[CharacterCheckWarning]:
        """检查一句的 ``speaker_role``（只读）。"""
        role = str(getattr(beat, "speaker_role", "") or "").strip()
        if not role:
            return []
        lowered = role.lower()
        if lowered in _GENERIC_ROLE_NAMES:
            return []
        # Patch v4.25：比对前先把角色名转成简体（**只用于比对**，不改原字段）。
        # 角色表里存的都是简体名，用户若在说话人栏里写了繁体（如「復仇」），
        # 直接比对会误判成「未登记的角色」。
        simplified = to_simplified_chinese(role)
        canonical = self.canonicalize_role(simplified)
        if canonical != simplified:
            reason = (
                _REASON_HOMOPHONE_VARIANT
                if are_homophone_name_variants(simplified, canonical)
                else _REASON_ALIAS_VARIANT
            )
            return [
                CharacterCheckWarning(
                    beat_id=beat_id,
                    beat_index=index,
                    field="speaker_role",
                    found_name=role,
                    suggested_canonical=canonical,
                    reason=reason,
                    message=(
                        f"第 {index + 1} 句的说话人写作「{role}」，"
                        f"本作品的角色规范名是「{canonical}」。"
                    ),
                    snippet=role,
                )
            ]
        if self.canonical_names and simplified not in self.canonical_names:
            return [
                CharacterCheckWarning(
                    beat_id=beat_id,
                    beat_index=index,
                    field="speaker_role",
                    found_name=role,
                    suggested_canonical=None,
                    reason=_REASON_UNREGISTERED_SPEAKER,
                    message=(
                        f"第 {index + 1} 句的说话人「{role}」不在本作品的角色表里，"
                        "配不出专属音色。"
                    ),
                    snippet=role,
                )
            ]
        return []

    def _inspect_text_field(
        self, beat: Any, index: int, beat_id: str, field_name: str
    ) -> list[CharacterCheckWarning]:
        """检查一句的正文（字幕原文 / 送音文本）里的角色名写法（只读）。"""
        text = str(getattr(beat, field_name, "") or "")
        if not text:
            return []
        warnings: list[CharacterCheckWarning] = []
        claimed: list[tuple[int, int]] = []
        for variant, canonical in self.text_rules:
            if not variant or variant == canonical or variant not in text:
                continue
            protected = _protected_spans(text, variant)
            for match in re.finditer(re.escape(variant), text):
                span = match.span()
                if _overlaps(span, protected):
                    continue
                if _overlaps(span, claimed):
                    continue
                claimed.append(span)
                reason = (
                    _REASON_HOMOPHONE_VARIANT
                    if are_homophone_name_variants(variant, canonical)
                    else _REASON_ALIAS_VARIANT
                )
                warnings.append(
                    CharacterCheckWarning(
                        beat_id=beat_id,
                        beat_index=index,
                        field=field_name,
                        found_name=variant,
                        suggested_canonical=canonical,
                        reason=reason,
                        message=(
                            f"第 {index + 1} 句的"
                            f"{'字幕原文' if field_name == 'narration_text' else '送音文本'}"
                            f"里写了「{variant}」，规范名是「{canonical}」。"
                        ),
                        snippet=self._context_snippet(text, span),
                    )
                )
        return warnings

    @staticmethod
    def _context_snippet(
        text: str, span: tuple[int, int], *, width: int = 12
    ) -> str:
        """截出错位置前后各若干字作为上下文。"""
        start = max(0, span[0] - width)
        end = min(len(text), span[1] + width)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        return f"{prefix}{text[start:end]}{suffix}"

    # -- 提示词 -------------------------------------------------------
    def prompt_lock_block(self) -> str:
        """给 Stage 4A / 4B 提示词追加的「角色名锁定」说明。

        没有可锁定的角色时返回空串（调用方原样拼接即可）。
        """
        if not self.canonical_names:
            return ""
        lines = [
            "【角色名锁定（Patch v4.16）】",
            "以下角色的**规范名**如下，正文与对白中一律使用规范名，",
            "禁止使用别名、外貌称呼或同音异写：",
        ]
        for name in self.canonical_names:
            aliases = self.alias_notes.get(name, ())
            if aliases:
                lines.append(f"- {name}（不要写成：{'、'.join(aliases)}）")
            else:
                lines.append(f"- {name}")
        return "\n".join(lines)


# ----------------------------------------------------------------------
# 构建
# ----------------------------------------------------------------------
#: 候选权威层级（数字越小越权威）。
_TIER_VOICE = 0  # 已配置专属音色（``series_tts.character_voices`` 的键）
_TIER_BIBLE = 1  # 已有 StoryBible / 作品级 ``character_aliases`` 的键
_TIER_DISCOVERED = 2  # 本话发现（``speaker_role`` / 正文里出现）

#: 同层级内的优先级：0 = 权威原名（不可被顶替），1 = 由原名派生出的同音写法。
_RANK_PRIMARY = 0
_RANK_DERIVED = 1


def _homophone_substitutions(name: str) -> list[str]:
    """列出该名字的「单字同音替换」变体（Patch v4.16）。

    ``恩彗`` → ``恩慧`` / ``恩惠`` / ``恩蕙``。只做**单字**替换，与
    :func:`are_homophone_name_variants` 的判定口径一致（恰好一个字不同）。
    """
    chars = list(name)
    variants: list[str] = []
    for index, char in enumerate(chars):
        cluster = _HOMOPHONE_INDEX.get(char)
        if not cluster:
            continue
        for member in sorted(cluster):
            if member == char:
                continue
            candidate = "".join(chars[:index] + [member] + chars[index + 1 :])
            if candidate != name and candidate not in variants:
                variants.append(candidate)
    return variants


def _normalize_alias_map(
    series_aliases: Mapping[str, str | Sequence[str]] | None,
) -> dict[str, list[str]]:
    """``{规范名: [别名...]}``（作品级 ``character_aliases``）。"""
    result: dict[str, list[str]] = {}
    for key, value in (series_aliases or {}).items():
        canonical = str(key or "").strip()
        if not canonical:
            continue
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, (list, tuple, set)):
            values = [str(item) for item in value]
        else:
            values = []
        bucket = result.setdefault(canonical, [])
        for item in values:
            item = str(item or "").strip()
            if item and item not in bucket:
                bucket.append(item)
    return result


def build_character_canonicalizer(
    *,
    characters: Sequence[Any] | None = None,
    series_tts: Any | None = None,
    series_aliases: Mapping[str, str | Sequence[str]] | None = None,
    extra_names: Iterable[str] | None = None,
    extra_texts: Iterable[str] | None = None,
) -> CharacterNameCanonicalizer:
    """构建角色名归一器（Patch v4.16）。

    参数：

    * ``characters``：作品 ``story_bible.json`` 里的角色表（``CharacterProfile``）；
    * ``series_tts``：作品级配音设置，其 ``character_voices`` 的键是**最权威**的角色名；
    * ``series_aliases``：作品级 ``character_aliases``（``{规范名: 别名或别名列表}``）；
    * ``extra_names``：本话额外出现的候选名（如 ``speaker_role`` 集合）；
    * ``extra_texts``：本话正文全文（用于抽取候选名 + 统计频次）。

    规范名优先级：已配置专属音色 > 已有 StoryBible > 出现频次更高者。
    """
    alias_map = _normalize_alias_map(series_aliases)

    # -- 1. 收集候选（tier 越小越权威） -------------------------------
    #: (名字, tier, rank)  tier: 0=专属音色 1=StoryBible 2=本话发现
    #:                  rank: 0=权威原名 1=由原名派生的同音写法
    candidates: list[tuple[str, int, int]] = []
    seen: set[str] = set()

    def _push(name: str, tier: int, rank: int) -> None:
        cleaned = to_simplified_chinese(str(name or "")).strip()
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        candidates.append((cleaned, tier, rank))

    character_voices = getattr(series_tts, "character_voices", None) or {}
    for name in character_voices:
        _push(name, _TIER_VOICE, _RANK_PRIMARY)
    for character in characters or []:
        _push(str(getattr(character, "name", "") or ""), _TIER_BIBLE, _RANK_PRIMARY)
    for canonical in alias_map:
        _push(canonical, _TIER_BIBLE, _RANK_PRIMARY)
    for name in extra_names or []:
        _push(str(name), _TIER_DISCOVERED, _RANK_PRIMARY)

    # -- 2. 正文里「出现过」的同音写法也要进候选 ----------------------
    # 模型把「恩彗」写成「恩慧」时，光靠角色表发现不了——必须回到正文里
    # 找同音替换的写法。已配置音色 / StoryBible 里的名字派生出来的写法
    # 归为「派生」永远不顶替原名；本话自己发现的写法则与原名同层，
    # 由出现频次决定谁当规范名。
    texts = [str(item or "") for item in (extra_texts or [])]
    joined = "\n".join(texts)
    base_candidates = list(candidates)
    for name, tier, _rank in base_candidates:
        derived_rank = _RANK_DERIVED if tier != _TIER_DISCOVERED else _RANK_PRIMARY
        for variant in _homophone_substitutions(name):
            if joined and variant in joined:
                _push(variant, tier, derived_rank)

    # -- 3. 频次统计（正文里出现越多越可能是规范名） ------------------
    frequencies: dict[str, int] = {}
    for text in texts:
        if not text:
            continue
        for candidate in extract_candidate_names(text):
            frequencies[candidate] = frequencies.get(candidate, 0) + 1
        for name, _tier, _rank in candidates:
            if name and name in text:
                frequencies[name] = frequencies.get(name, 0) + text.count(name)
    for name, _tier, _rank in candidates:
        frequencies.setdefault(name, 0)

    # -- 4. 选规范名 --------------------------------------------------
    ordered = sorted(
        enumerate(candidates),
        key=lambda item: (
            item[1][1],  # tier
            item[1][2],  # rank（权威原名优先于派生写法）
            -frequencies.get(item[1][0], 0),  # 同层内频次高者胜出
            item[0],
        ),
    )
    ordered = [item for _index, item in ordered]
    canonical_of: dict[str, str] = {}
    canonical_names: list[str] = []
    for name, _tier, _rank in ordered:
        owner = next(
            (
                existing
                for existing in canonical_names
                if are_homophone_name_variants(existing, name)
            ),
            None,
        )
        if owner is None:
            canonical_names.append(name)
            canonical_of[name] = name
        else:
            canonical_of[name] = owner

    # -- 5. 收集每个规范名的变体 --------------------------------------
    variants: dict[str, list[str]] = {name: [] for name in canonical_names}

    def _add_variant(canonical: str, variant: str) -> None:
        variant = to_simplified_chinese(str(variant or "")).strip()
        if not variant or variant == canonical:
            return
        bucket = variants.setdefault(canonical, [])
        if variant not in bucket:
            bucket.append(variant)

    # 5a. 显式别名
    for character in characters or []:
        name = str(getattr(character, "name", "") or "").strip()
        owner = canonical_of.get(name) or canonical_of.get(
            to_simplified_chinese(name).strip(), ""
        )
        if not owner:
            continue
        for alias in getattr(character, "aliases", None) or []:
            _add_variant(owner, alias)
    for canonical, aliases in alias_map.items():
        owner = canonical_of.get(canonical) or canonical
        for alias in aliases:
            _add_variant(owner, alias)
    # 5b. 同音变体（候选里与规范名同音但写法不同的）
    for name, owner in canonical_of.items():
        if name != owner:
            _add_variant(owner, name)
    # 5c. 别名的同音兄弟（正文里出现过才算数）
    for canonical, owner_variants in list(variants.items()):
        for variant in list(owner_variants):
            for sibling in _homophone_substitutions(variant):
                if joined and sibling in joined:
                    _add_variant(canonical, sibling)

    # -- 6. 生成两张映射 ----------------------------------------------
    role_map: dict[str, str] = {}
    text_rules: list[tuple[str, str]] = []
    alias_notes: dict[str, tuple[str, ...]] = {}

    for canonical in canonical_names:
        owner_variants = variants.get(canonical, [])
        alias_notes[canonical] = tuple(owner_variants)
        role_map[canonical] = canonical
        for variant in owner_variants:
            role_map[variant] = canonical
            role_map.setdefault(variant.lower(), canonical)
            role_map.setdefault(canonical.lower(), canonical)
            # 正文规则：外貌称呼不进正文；单字不改正文；简称不拉长成全名；
            # Patch v4.20：关系 / 身份泛称（丈夫 / 岳母……）也不进正文，
            # 否则用户写「我未来的丈夫」会被改回角色本名，保存后又被冲掉。
            if len(variant) < 2:
                continue
            if len(variant) < len(canonical):
                continue
            if any(marker in variant for marker in _DESCRIPTIVE_MARKERS):
                continue
            if any(marker in variant for marker in _RELATION_MARKERS):
                continue
            text_rules.append((variant, canonical))

    text_rules.sort(key=lambda item: (-len(item[0]), item[0]))

    return CharacterNameCanonicalizer(
        canonical_names=tuple(canonical_names),
        role_map=role_map,
        text_rules=tuple(text_rules),
        alias_notes=alias_notes,
    )
