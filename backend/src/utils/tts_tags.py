"""MiniMax Speech 2.8 的「语气词标签」与「句中停顿标签」工具（Patch v4.14）。

MiniMax 的 TTS 接口在送音文本里认两类标记：

* **语气词标签**：``(laughs)`` / ``(coughs)`` / ``(clear-throat)`` … 共 19 个，
  必须原样带括号送过去，模型才会按对应情绪发声；
* **停顿标签**：``<#0.5#>``，单位秒，夹在两段可发音文字之间表示停顿。

这两个标记**只允许出现在送音文本（``tts_text``）里**。字幕轨（``narration_text``）
若原样带上，屏幕会显示出 ``(laughs)`` 这种奇怪的东西，所以本模块提供
:func:`strip_minimax_tts_tags` 给字幕轨清洗。

另外，剧本校验（``validate_tts_text_no_pinyin``）会把「拉丁字母串」当成拼音
误杀，``(coughs)`` / ``(clear-throat)`` 里全是拉丁字母，因此校验前必须先用
:func:`mask_minimax_tts_tags` 把**完整白名单标签**换成等长非字母占位符。
"""

from __future__ import annotations

import re

#: MiniMax Speech 2.8 官方支持的语气词标签（19 个，顺序即官方文档顺序）。
MINIMAX_INTERJECTION_TAGS: tuple[str, ...] = (
    "(laughs)",
    "(chuckle)",
    "(coughs)",
    "(clear-throat)",
    "(groans)",
    "(breath)",
    "(pant)",
    "(inhale)",
    "(exhale)",
    "(gasps)",
    "(sniffs)",
    "(sighs)",
    "(snorts)",
    "(burps)",
    "(lip-smacking)",
    "(humming)",
    "(hissing)",
    "(emm)",
    "(sneezes)",
)

#: 语气词标签 → 校验占位符用的掩码字符（等长替换，避免破坏下标）。
_MASK_CHAR = "〇"

#: 严格合法的停顿标签：整数或至多两位小数秒数。
MINIMAX_PAUSE_TAG_PATTERN: re.Pattern[str] = re.compile(r"<#(\d+(?:\.\d{1,2})?)#>")

#: 停顿秒数下界（0.01 秒 = 10 毫秒）。
MINIMAX_PAUSE_MIN_SECONDS: float = 0.01
#: 停顿秒数上界（99.99 秒）。
MINIMAX_PAUSE_MAX_SECONDS: float = 99.99

#: 宽松匹配：用于识别「像停顿标签但不合规」的写法（如 ``<#1.234#>`` / ``<#abc#>``）。
_LOOSE_PAUSE_TAG_PATTERN: re.Pattern[str] = re.compile(r"<#[^<>]{0,32}#>")

#: 数字型但小数位超限的停顿写法（``<#1.234#>``）：四舍五入到两位小数后保留。
_LOOSE_NUMERIC_PAUSE_TAG_PATTERN: re.Pattern[str] = re.compile(r"<#(\d+(?:\.\d+)?)#>")

#: 连续的停顿标签（中间允许空白），用于合并。
_PAUSE_RUN_PATTERN: re.Pattern[str] = re.compile(
    r"(?:<#\d+(?:\.\d{1,2})?#>)(?:\s*(?:<#\d+(?:\.\d{1,2})?#>))+"
)

#: Patch v4.40：句中内联音效标签的形状（``[sfx:key]``）。
#:
#: 与 :data:`core.normalize.INLINE_SFX_TAG_RE` **同形**（两边各留一份是为了
#: 避免 ``utils.tts_tags`` 反向依赖 ``core.normalize`` 形成循环导入；
#: ``tests/test_patch_v440_inline_sfx_timing.py`` 会断言两者 pattern 一致）。
INLINE_SFX_TAG_PATTERN: re.Pattern[str] = re.compile(
    r"\[\s*sfx\s*:\s*([a-zA-Z0-9_\-]+)\s*\]"
)

#: 语气词标签 + 合法停顿标签 + 内联音效标签的**完整匹配**模式。
#:
#: 供「按标签边界切段处理」的场合使用：拼音校验、发音词典替换等操作都要
#: 在标签之外进行，绝不能把标签内容当成普通文本改写。
#:
#: Patch v4.40：``[sfx:key]`` 也纳入切分——标签里的 ``slap`` 是音效键而不是
#: 台词，不参与拼音校验、也不该被发音词典改写。
MINIMAX_TAG_SPLIT_PATTERN: re.Pattern[str] = re.compile(
    "|".join(re.escape(tag) for tag in MINIMAX_INTERJECTION_TAGS)
    + r"|<#\d+(?:\.\d{1,2})?#>"
    + r"|" + INLINE_SFX_TAG_PATTERN.pattern
)


def _clamp_seconds(value: float) -> float:
    """把停顿秒数钳到官方支持区间。"""
    if value < MINIMAX_PAUSE_MIN_SECONDS:
        return MINIMAX_PAUSE_MIN_SECONDS
    if value > MINIMAX_PAUSE_MAX_SECONDS:
        return MINIMAX_PAUSE_MAX_SECONDS
    return value


def _format_seconds(value: float) -> str:
    """把秒数格式化成标签内部文本（最多两位小数、去掉多余的尾随 0）。"""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _format_pause_tag(value: float) -> str:
    """把秒数格式化成完整停顿标签，如 ``0.5`` → ``<#0.5#>``。"""
    return f"<#{_format_seconds(_clamp_seconds(value))}#>"


def normalize_minimax_pause_tags(text: str) -> str:
    """规范化送音文本里的停顿标签。

    规则：

    1. **合并**：相邻或仅隔空白的多个停顿标签合并成一个，秒数相加
       （``<#0.5#><#0.8#>`` → ``<#1.3#>``）；
    2. **钳位**：合并后或单个标签的秒数钳到 ``[0.01, 99.99]``；
    3. **贴首 / 贴尾剔除**：整句最开头或最末尾的孤立停顿标签直接删掉
       （停顿只能放在句中两段可发音文字之间）；
    4. **清理越界写法**：形如停顿标签但不合规的写法（``<#abc#>``）一律删除，
       避免模型端解析报错；数字型但小数位超限的写法（``<#1.234#>``）四舍五入
       到两位小数后保留（``<#1.23#>``）。

    参数
    ----
    text:
        原始送音文本。

    返回
    ----
    ``str``
        规范化后的文本。
    """
    if not text or "<#" not in text:
        return text

    # 1) 先规范化不合规的停顿写法（保持其余文本不动）：不合规写法会让模型端解析报错。
    #    顺序要紧：先把「数字型但小数位超限」的写法四舍五入到两位小数保留
    #    （<#1.234#> → <#1.23#>），再把剩余的形似标签但不合规的写法删除
    #    （<#abc#>）。若顺序颠倒，<#1.234#> 会在四舍五入之前就被整段删掉。
    merged = _LOOSE_NUMERIC_PAUSE_TAG_PATTERN.sub(
        lambda m: _format_pause_tag(round(float(m.group(1)), 2)), text
    )
    merged = _LOOSE_PAUSE_TAG_PATTERN.sub(
        lambda m: m.group(0) if MINIMAX_PAUSE_TAG_PATTERN.fullmatch(m.group(0)) else "",
        merged,
    )

    # 2) 合并相邻 / 仅隔空白的多个停顿标签（秒数相加）。
    def _merge(match: re.Match[str]) -> str:
        total = 0.0
        for part in MINIMAX_PAUSE_TAG_PATTERN.findall(match.group(0)):
            total += float(part)
        return _format_pause_tag(total)

    merged = _PAUSE_RUN_PATTERN.sub(_merge, merged)

    # 3) 单个合法标签也做一次钳位（<#0#> → <#0.01#>，<#120#> → <#99.99#>）。
    merged = MINIMAX_PAUSE_TAG_PATTERN.sub(
        lambda m: _format_pause_tag(float(m.group(1))), merged
    )

    # 4) 剥除贴首 / 贴尾的孤立停顿标签（停顿只能放在句中两段可发音文字之间）。
    #    若剥除后整句会变空，则保留它（此时它是句子的全部内容，不是「孤立」标签）。
    #    剥除时同步去掉标签内侧的空白，避免留下 " 你好" / "你好 " 这种残边。
    leading = re.match(r"^(?P<ws>\s*)<#\d+(?:\.\d{1,2})?#>", merged)
    if leading is not None and merged[leading.end() :].strip():
        merged = merged[leading.end() :].lstrip()
    trailing = re.search(r"<#\d+(?:\.\d{1,2})?#>(?P<ws>\s*)$", merged)
    if trailing is not None and merged[: trailing.start()].strip():
        merged = merged[: trailing.start()].rstrip()
    return merged


def strip_minimax_tts_tags(text: str) -> str:
    """把送音文本里的语气词 / 停顿标签全部抹掉（用于净化字幕轨）。

    * 移除 19 个语气词标签；
    * 移除停顿标签（含不合规写法）；
    * Patch v4.40：移除句中内联音效标签 ``[sfx:key]``——字幕轨**绝不能**
      把音效标记印到屏幕上；
    * 收敛多余空白，句首句尾去空白。

    参数
    ----
    text:
        原始文本（通常是 ``tts_text`` 或 ``narration_text``）。

    返回
    ----
    ``str``
        不含任何 MiniMax 标签的纯文本。
    """
    if not text:
        return ""
    cleaned = text
    for tag in MINIMAX_INTERJECTION_TAGS:
        cleaned = cleaned.replace(tag, "")
    cleaned = _LOOSE_PAUSE_TAG_PATTERN.sub("", cleaned)
    cleaned = INLINE_SFX_TAG_PATTERN.sub("", cleaned)
    # 抹掉标签后可能留下「多余空格 / 空格 + 标点」的空洞，收敛掉。
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([，。！？、；：,.!?;])", r"\1", cleaned)
    cleaned = re.sub(r"([，。！？、；：,.!?;])[ \t]+", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def mask_minimax_tts_tags(text: str) -> str:
    """把完整白名单语气词标签 / 合法停顿标签替换成等长非字母占位符。

    用途：剧本校验（``validate_tts_text_no_pinyin`` / ``strip_pinyin``）会把
    拉丁字母串一律当成拼音误杀，而 ``(coughs)`` / ``(clear-throat)`` 恰好全是
    拉丁字母。本函数先把这些**完整匹配**的标签遮蔽掉，校验就能只针对真正的
    拼音与漏括号英文。

    * 占位符与原文等长，**不改变任何字符下标**；
    * 只遮蔽「完整标签」，裸单词（如 ``coughs``）不会被遮蔽，仍会被校验拦下；
    * Patch v4.40：``[sfx:key]`` 里的 ``key``（``slap`` / ``boom_hit`` …）同样
      是拉丁字母，**必须**一起遮蔽，否则带内联音效标签的剧本会被整条判成
      「配音文本含拼音」而全线报错。

    参数
    ----
    text:
        原始送音文本。

    返回
    ----
    ``str``
        标签位置被占位符替换后的文本（长度与入参一致）。
    """
    if not text:
        return ""

    masked = text
    for tag in MINIMAX_INTERJECTION_TAGS:
        masked = masked.replace(tag, _MASK_CHAR * len(tag))
    masked = MINIMAX_PAUSE_TAG_PATTERN.sub(
        lambda m: _MASK_CHAR * len(m.group(0)), masked
    )
    masked = INLINE_SFX_TAG_PATTERN.sub(
        lambda m: _MASK_CHAR * len(m.group(0)), masked
    )
    return masked


def has_minimax_pause_tag(text: str) -> bool:
    """文本里是否存在合法停顿标签。"""
    return bool(text) and bool(MINIMAX_PAUSE_TAG_PATTERN.search(text))


def count_minimax_interjection_tags(text: str) -> int:
    """统计文本里的语气词标签个数（用于提示与测试）。"""
    if not text:
        return 0
    return sum(text.count(tag) for tag in MINIMAX_INTERJECTION_TAGS)


__all__ = [
    "INLINE_SFX_TAG_PATTERN",
    "MINIMAX_INTERJECTION_TAGS",
    "MINIMAX_PAUSE_MAX_SECONDS",
    "MINIMAX_PAUSE_MIN_SECONDS",
    "MINIMAX_PAUSE_TAG_PATTERN",
    "MINIMAX_TAG_SPLIT_PATTERN",
    "count_minimax_interjection_tags",
    "has_minimax_pause_tag",
    "mask_minimax_tts_tags",
    "normalize_minimax_pause_tags",
    "strip_minimax_tts_tags",
]
