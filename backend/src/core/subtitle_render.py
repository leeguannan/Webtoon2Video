"""字幕透明 PNG 渲染（Pillow）。

**为什么单独放在 core 而不是 stages**：Stage 6 负责产出逐条字幕的透明 PNG，
Stage 7 负责用 ffmpeg ``overlay`` 滤镜把这些 PNG 按时间烧录进成片。两端都要用
同一套「字体探测 + 换行 + 描边 + 底部居中」逻辑，放在 core 下可以避免 stages
之间互相 import（Stage 7 直接 ``from core.subtitle_render import ...`` 即可）。

**为什么不用 libass 烧字幕**：本机 ffmpeg 9.0.1 没有编译 libass，
``subtitles`` / ``ass`` / ``drawtext`` 滤镜全部不可用。因此字幕烧录走
「PNG overlay」路线：Stage 6 渲染好 PNG，Stage 7 只做像素合成，零字体依赖、
零滤镜依赖，中文渲染完全可控。ASS 文件仍然照常产出，作为外挂字幕交付物
（外挂播放器 / 剪辑软件 / 平台上传都用得上）。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from core.normalize import sanitize_narration_and_tts

#: 字幕渲染异常。
class SubtitleRenderError(RuntimeError):
    """字幕 PNG 渲染失败（字体缺失、图片写入失败等）。"""


#: 默认字体名（macOS 自带简体中文字体）。
DEFAULT_FONT_NAME = "Hiragino Sans GB"

#: 字体候选（按顺序探测，第一个存在的即可用）。
#: macOS 覆盖较全；``Arial Unicode.ttf`` 作为最后兜底（覆盖中日韩）。
FONT_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("Hiragino Sans GB", "/System/Library/Fonts/Hiragino Sans GB.ttc"),
    ("STHeiti Light", "/System/Library/Fonts/STHeiti Light.ttc"),
    ("STHeiti Medium", "/System/Library/Fonts/STHeiti Medium.ttc"),
    ("Arial Unicode MS", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
)

#: 字体文件后缀（用于判定用户传入的是路径还是字体名）。
_FONT_SUFFIXES = frozenset({".ttf", ".ttc", ".otf", ".otc"})

#: 描边宽度（像素）：白字黑边是竖屏漫画解说最稳的可读性组合。
DEFAULT_STROKE_WIDTH = 4

#: 阴影偏移（像素）。
DEFAULT_SHADOW_OFFSET = 3

#: 避头尾禁则（kinsoku）：禁止出现在行首的字符——后置标点/闭引号/闭括号等。
_NO_LINE_START_CHARS = frozenset("，。、；：？！…—·’”）】》」』.,;:!?)]}%")

#: 避头尾禁则：禁止出现在行尾的字符——开引号/左括号等，必须随后续文字一起换行。
_NO_LINE_END_CHARS = frozenset("“‘（【《「『([")


def _iter_font_files() -> list[tuple[str, Path]]:
    """返回候选字体中**当前机器上真实存在**的 ``(名称, 路径)`` 列表。"""
    result: list[tuple[str, Path]] = []
    for name, raw_path in FONT_CANDIDATES:
        path = Path(raw_path)
        if path.is_file():
            result.append((name, path))
    return result


def resolve_font_path(font_name: str | Path = DEFAULT_FONT_NAME) -> Path:
    """解析出可用的字体文件路径。

    规则（按顺序）：

    1. ``font_name`` 本身就是存在的字体文件 → 直接用（允许 Stage 7 自定义字体）；
    2. ``font_name`` 与候选字体名匹配（忽略大小写）且文件存在 → 用它；
    3. 否则回退到候选列表里第一个存在的字体；
    4. 一个都不存在 → 抛 :class:`SubtitleRenderError`（宁可报错也不要画出方块）。

    为什么必须报错：中文字体缺失时 Pillow 会静默画成「豆腐块」，
    成品字幕全是方框却没有任何告警，属于最难排查的静默失败。
    """
    if font_name:
        raw = Path(str(font_name)).expanduser()
        if raw.is_file():
            return raw
        wanted = str(font_name).strip().lower()
        for name, path in _iter_font_files():
            if name.lower() == wanted or path.stem.lower() == wanted:
                return path
    available = _iter_font_files()
    if not available:
        raise SubtitleRenderError(
            "找不到任何可用的中文字体，请显式传入字体文件路径（可选字体："
            + "、".join(name for name, _ in FONT_CANDIDATES)
            + "）"
        )
    return available[0][1]


def load_font(font_path: str | Path, size: int) -> ImageFont.FreeTypeFont:
    """按字号加载 TrueType 字体（``.ttc`` 取第 0 号子字体）。"""
    path = Path(font_path)
    try:
        return ImageFont.truetype(str(path), int(size))
    except OSError as exc:  # pragma: no cover - 依赖具体系统错误文本
        raise SubtitleRenderError(f"字体加载失败：{path}（{exc}）") from exc


def _split_paragraphs(text: str) -> list[str]:
    """把文本按换行拆成段落（保留空行语义）。"""
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return normalized.split("\n")


def strip_render_unsafe_chars(text: str) -> str:
    """渲染前的最后一道兜底：剔除任何可能变成方块的字符。

    Pillow 遇到字体里没有的字形会**静默**画成豆腐块「☒」——不报错、不留痕，
    只有看成片才能发现（成片 03:46 就这么翻车过一次）。字幕文本可能来自旧版
    ``script.json``（没经过 ``core.normalize`` 净化），所以这里在
    ``wrap_text`` / ``draw.text`` 之前再筛一遍：控制符、私用区字符
    （U+E000–U+F8FF）、Emoji 区段与各类装饰符号一律剔除。
    """
    return sanitize_narration_and_tts(text or "")


def _is_legal_cut(current: str, cut: int) -> bool:
    """切分下标是否满足避头尾：新行首字非后置标点，旧行末字非前置符号。"""
    return (
        1 <= cut < len(current)
        and current[cut] not in _NO_LINE_START_CHARS
        and current[cut - 1] not in _NO_LINE_END_CHARS
    )


def _find_kinsoku_cut(
    current: str,
    char: str,
    idx: int,
    paragraph: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    stroke_width: int,
) -> int | None:
    """避头尾禁则下的回退切分下标；无禁则冲突或找不到合法切分时返回 ``None``。

    返回 ``None`` 时调用方按朴素位置安全折行（``current`` 整行落下、``char``
    起新行），保证极窄宽度下也不会死循环或丢字。
    """
    # 普通字符在普通位置超宽：没有禁则冲突，维持朴素折行位置。
    if char not in _NO_LINE_START_CHARS and current[-1] not in _NO_LINE_END_CHARS:
        return None

    tail_all_punct = idx == len(paragraph) - 1 or all(
        rest in _NO_LINE_START_CHARS for rest in paragraph[idx + 1 :]
    )

    def fits(cut: int) -> bool:
        return font.getlength(current[cut:] + char) + 2 * stroke_width <= max_width

    # 句末标点且已到段落尾部：优先让新行至少带回 2 个非标点字符，
    # 避免「单字+标点」孤尾（如「闪过。」被拆成「闪 / 过。」）。
    if char in _NO_LINE_START_CHARS and tail_all_punct:
        for cut in range(len(current) - 1, 1, -1):
            if not _is_legal_cut(current, cut) or not fits(cut):
                continue
            moved = current[cut:]
            normal_count = sum(
                c not in _NO_LINE_START_CHARS and c not in _NO_LINE_END_CHARS
                for c in moved
            )
            if normal_count >= 2:
                return cut

    # 常规避头/避尾：尽量少回退，取最大的合法切分（新行至少带 1 字）。
    for cut in range(len(current) - 1, 0, -1):
        if _is_legal_cut(current, cut):
            return cut
    return None


def wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    *,
    stroke_width: int = 0,
) -> list[str]:
    """按像素宽度折行（中英混排安全：逐字符累积，不依赖空格）。

    为什么需要折行：竖屏 1080 宽的成片上，一句 30 字的解说按 64px 字号会顶满整行，
    烧录后几乎贴着左右边缘；折到 ``max_width`` 以内更符合竖屏排版习惯。

    折行同时执行中文避头尾禁则（kinsoku）：后置标点（``，。：`` 等）不许落到
    行首，前置符号（``“（《`` 等）不许留在行尾；句末长句在宽度允许时至少带
    2 个非标点字符换行，避免「单字+标点」孤尾。禁则只调整切分位置、不改字面，
    单段落始终满足 ``"".join(wrapped_lines) == paragraph``。
    """
    lines: list[str] = []
    for paragraph in _split_paragraphs(text):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for idx, char in enumerate(paragraph):
            if current and (
                font.getlength(current + char) + 2 * stroke_width > max_width
            ):
                cut = _find_kinsoku_cut(
                    current, char, idx, paragraph, font, max_width, stroke_width
                )
                if cut is None:
                    # 安全兜底：窄宽度/行首连续禁则字符时按原位置折行。
                    lines.append(current)
                    current = char
                else:
                    lines.append(current[:cut])
                    current = current[cut:] + char
            else:
                current += char
        lines.append(current)
    return lines or [""]


def _fit_font(
    font_path: Path,
    text: str,
    font_size: int,
    max_width: float,
    *,
    stroke_width: int,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """选择「能塞进宽度」的字号与折行结果。

    先按目标字号折行；仍超宽则按比例缩小字号重试（最多 6 轮），
    缩到下限仍放不下就按最后一次结果输出（宁可多行也不丢字）。
    """
    size = max(8, int(font_size))
    font = load_font(font_path, size)
    lines = wrap_text(text, font, max_width, stroke_width=stroke_width)
    for _ in range(6):
        widest = max((font.getlength(line) for line in lines), default=0.0)
        if widest + 2 * stroke_width <= max_width or size <= 12:
            break
        size = max(12, int(size * (max_width / max(widest, 1.0))))
        font = load_font(font_path, size)
        lines = wrap_text(text, font, max_width, stroke_width=stroke_width)
    return font, lines


def render_subtitle_png(
    text: str,
    out_path: str | Path,
    *,
    width: int = 1080,
    height: int = 1920,
    font_name: str | Path = DEFAULT_FONT_NAME,
    font_size: int = 64,
    margin_v: int = 220,
    margin_h: int = 80,
    stroke_width: int = DEFAULT_STROKE_WIDTH,
    shadow_offset: int = DEFAULT_SHADOW_OFFSET,
    line_spacing: float = 1.25,
) -> Path:
    """渲染一张 **整幅画布尺寸** 的 RGBA 透明字幕 PNG。

    为什么画布是 1080×1920 而不是裁剪成文字大小：Stage 7 用 ffmpeg ``overlay``
    直接叠加时，素材尺寸必须与成片一致（1080×1920），否则 overlay 需要额外
    的缩放/定位参数；整幅透明画布 + 底部居中排版，Stage 7 只需
    ``overlay=0:0:enable='between(t,start,end)'`` 即可。

    样式：白字 + 黑色描边 + 轻微阴影，底部居中（对齐 ASS 里的 ``{\\an2}``）。
    """
    target = Path(out_path)
    font_path = resolve_font_path(font_name)
    # 兜底净化：保证永远不会画出「☒」方块。
    text = strip_render_unsafe_chars(text)
    max_width = float(max(1, int(width) - 2 * int(margin_h)))
    font, lines = _fit_font(
        font_path,
        text,
        font_size,
        max_width,
        stroke_width=int(stroke_width),
    )

    canvas = Image.new("RGBA", (int(width), int(height)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    line_height = int(font.size * float(line_spacing))
    total_height = line_height * len(lines)
    # 底部居中：整块文字的下沿距画面底部 margin_v。
    y = int(height) - int(margin_v) - total_height
    for line in lines:
        text_width = font.getlength(line)
        x = (int(width) - text_width) / 2.0
        if shadow_offset:
            # 阴影层：整体右下方偏移一点，制造「浮在画面之上」的立体感。
            draw.text(
                (x + shadow_offset, y + shadow_offset),
                line,
                font=font,
                fill=(0, 0, 0, 150),
                stroke_width=int(stroke_width),
                stroke_fill=(0, 0, 0, 150),
                anchor="lt",
            )
        draw.text(
            (x, y),
            line,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=int(stroke_width),
            stroke_fill=(0, 0, 0, 255),
            anchor="lt",
        )
        y += line_height

    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, format="PNG")
    return target


def subtitle_filename(index: int) -> str:
    """字幕 PNG 文件名（4 位补零，与 ``SubtitleLine.index`` 一一对应）。"""
    return f"{int(index):04d}.png"


__all__ = [
    "DEFAULT_FONT_NAME",
    "DEFAULT_SHADOW_OFFSET",
    "DEFAULT_STROKE_WIDTH",
    "FONT_CANDIDATES",
    "SubtitleRenderError",
    "load_font",
    "render_subtitle_png",
    "resolve_font_path",
    "strip_render_unsafe_chars",
    "subtitle_filename",
    "wrap_text",
]
