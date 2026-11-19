"""Stage 6 字幕折行：中文避头尾禁则（kinsoku）单测。

覆盖 ``core.subtitle_render.wrap_text`` 的四类排版铁律：

1. 后置标点（``，。：`` 等）不许被踢到下一行行首；
2. 句末长句宽度允许时，新行至少带 2 个非标点字符，杜绝「单字+标点」孤尾；
3. 前置符号（``“（《`` 等）不许留在上一行行尾，必须随后续文字一起换行；
4. 连续标点串（``……？！``）保持粘连，不拆出纯标点行。

另含边界安全（空串/多段落/单字宽）与朴素折行回归、PNG 渲染冒烟。
真实中文字体缺失时字体相关用例 skip，排版逻辑用等宽 mock font 保证离线必跑。
"""

from __future__ import annotations

import pytest
from PIL import Image

from core.subtitle_render import (
    DEFAULT_FONT_NAME,
    load_font,
    render_subtitle_png,
    resolve_font_path,
    wrap_text,
)


class _FixedWidthFont:
    """等宽 mock font：每个字符固定 ``unit`` 像素，断行位置可精确到字符数。"""

    def __init__(self, unit: float = 10.0) -> None:
        self._unit = unit

    def getlength(self, text: str) -> float:
        return len(text) * self._unit


@pytest.fixture(scope="module")
def font_64():
    """真实中文字体（64px）；本机无任何候选字体时跳过依赖字形的用例。"""
    try:
        return load_font(resolve_font_path(DEFAULT_FONT_NAME), 64)
    except Exception as exc:  # 字体缺失属于环境问题，不应让逻辑用例失败
        pytest.skip(f"本机无可用中文字体：{exc}")


# --- 真实复现：句末标点不许落行首 + 孤尾优化 ---------------------------------


def test_no_leading_fullwidth_colon_real_case(font_64) -> None:
    """16 字刚好放满、第 17 字「：」超宽：不得出现行首「：」，末行至少带 2 字。"""
    paragraph = "恩彗明明受伤，还是把该说的话说完："
    prefix = paragraph[:-1]
    # 刚好放得下 16 字前缀、放不下追加「：」后的 17 字。
    max_width = font_64.getlength(prefix)

    lines = wrap_text(paragraph, font_64, max_width)

    assert "".join(lines) == paragraph
    assert len(lines) == 2
    assert all(not line.startswith("：") for line in lines)
    assert lines[-1] == "说完："


def test_no_orphan_period_keeps_two_chars_real_case(font_64) -> None:
    """句末「。」超宽：禁止「过。」孤尾行，宽度允许时末行至少是「闪过。」。"""
    paragraph = "血色墙面和倒吊束缚的影像接连闪过。"
    prefix = paragraph[:-1]
    max_width = font_64.getlength(prefix)

    lines = wrap_text(paragraph, font_64, max_width)

    assert "".join(lines) == paragraph
    assert all(not line.startswith("。") for line in lines)
    assert "过。" not in lines
    assert lines[-1] == "闪过。"


# --- 等宽 mock：引号避尾、标点串粘连、边界安全 -------------------------------


def test_open_quote_carried_to_next_line() -> None:
    """「“」在满宽位置时必须跟「现」一起换行，不许留在上一行行尾。"""
    font = _FixedWidthFont()
    paragraph = "他冷笑着说：“现在才走太迟了。”"
    # 每行 7 字：前 7 字恰好在「“」处放满，第 8 字「现」触发折行。
    lines = wrap_text(paragraph, font, max_width=70)

    assert "".join(lines) == paragraph
    assert lines[0] == "他冷笑着说："
    assert lines[1].startswith("“现")
    open_marks = "“‘（【《「『(["
    assert all(not line.endswith(tuple(open_marks)) for line in lines[:-1])


def test_trailing_punctuation_cluster_kept_glued() -> None:
    """尾部「……？！」超宽时整串粘连换行，不拆出纯标点行。"""
    font = _FixedWidthFont()
    paragraph = "怎么会变成这样……？！"
    # 每行 9 字：折行恰好落在标点串区域。
    lines = wrap_text(paragraph, font, max_width=90)

    assert "".join(lines) == paragraph
    assert lines == ["怎么会变成", "这样……？！"]
    # 每一行都至少含一个普通文字字符，不存在纯标点行。
    assert all(any("一" <= char <= "鿿" for char in line) for line in lines)


def test_empty_paragraph_and_narrow_width_safety() -> None:
    """空串/多段落/单字级窄宽度：不抛异常、不死循环、不丢字、不产生空行。"""
    font = _FixedWidthFont()

    assert wrap_text("", font, max_width=100) == [""]

    narrow_text = "天地玄黄宇宙洪荒日月盈昃"
    narrow_lines = wrap_text(narrow_text, font, max_width=10)
    assert "".join(narrow_lines) == narrow_text
    assert all(len(line) == 1 for line in narrow_lines)
    assert all(line != "" for line in narrow_lines)

    multi = "第一段\n\n第二段"
    multi_lines = wrap_text(multi, font, max_width=100)
    assert "\n".join(multi_lines) == multi
    assert "" in multi_lines  # 空行语义保留


def test_plain_break_keeps_legacy_position() -> None:
    """无禁则冲突的纯汉字超宽：维持朴素折行位置（每行整 5 字），不额外回退。"""
    font = _FixedWidthFont()
    paragraph = "一二三四五六七八九十一二三四五"

    lines = wrap_text(paragraph, font, max_width=50)

    assert lines == ["一二三四五", "六七八九十", "一二三四五"]
    assert "".join(lines) == paragraph


# --- 渲染链路冒烟：新 wrap_text 被 _fit_font/render_subtitle_png 复用 ----------


def test_render_subtitle_png_smoke(tmp_path, font_64) -> None:
    """含句末标点的字幕走完整渲染链路，产出 1080×1920 RGBA 透明 PNG。"""
    out = render_subtitle_png(
        "恩彗明明受伤，还是把该说的话说完：",
        tmp_path / "kinsoku_sub.png",
    )

    assert out.is_file() and out.stat().st_size > 0
    with Image.open(out) as image:
        assert image.size == (1080, 1920)
        assert image.mode == "RGBA"
