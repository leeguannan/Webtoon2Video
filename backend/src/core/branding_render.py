"""作品级封页 / 双结尾图片包渲染（Patch v4.5）。

职责：

1. 上传图片的 Base64 解码与规范化保存（EXIF 纠偏、统一 PNG）；
2. 话次中文数字与封页文案生成；
3. 自适应 1080×1920 画布铺图（柔暗模糊底 + 等比居中，杜绝黑边与误裁）；
4. 封页话次文字绘制（复用 ``core.subtitle_render`` 的字体解析/回退）；
5. 单话渲染选项 ``render_options.json`` 的读写与最终片包解析。

本模块不依赖 FastAPI / FFmpeg，纯 Pillow + Pydantic，方便单元测试。
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps
from pydantic import ValidationError, field_validator

from core.config import CoverTextSpec, SeriesBrandingConfig, SeriesConfig, _StrictModel
from core.subtitle_render import DEFAULT_FONT_NAME, load_font, resolve_font_path

logger = logging.getLogger(__name__)

#: 片包图片在作品目录下的固定子目录。
BRANDING_DIR_NAME = "assets/branding"
#: 单话渲染选项文件名（落在 ``episodes/ep_XXX/`` 下）。
RENDER_OPTIONS_FILENAME = "render_options.json"
#: 三个合法图片槽位。
BRANDING_SLOTS = ("cover", "outro_continue", "outro_final")

#: 上传图片体积上限（15MB，解码前的 Base64 载荷约束）。
MAX_BRANDING_BYTES = 15 * 1024 * 1024

#: 片包标准画布（与 VideoConfig 的 9:16 成片一致）。
CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1920

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")

_DIGITS = "零一二三四五六七八九"
_UNITS = ("", "十", "百", "千")


# ---------------------------------------------------------------------------
# 上传图片解码与规范化
# ---------------------------------------------------------------------------


def decode_base64_image(content_base64: str) -> bytes:
    """解码前端上传的 Base64 图片，兼容 ``data:image/...;base64,`` 前缀。

    超出 :data:`MAX_BRANDING_BYTES` 或内容非法时抛 :class:`ValueError`。
    """
    if not isinstance(content_base64, str) or not content_base64.strip():
        raise ValueError("图片内容为空")
    raw = content_base64.strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Base64 图片解码失败") from exc
    if not data:
        raise ValueError("图片内容为空")
    if len(data) > MAX_BRANDING_BYTES:
        raise ValueError("图片体积超过 15MB 上限")
    return data


def save_branding_image(raw_bytes: bytes, out_path: str | Path) -> Path:
    """用 Pillow 规范化图片并保存为标准 PNG。

    - ``ImageOps.exif_transpose`` 纠正手机拍照的 EXIF 方向；
    - 带透明通道存 RGBA，其余统一 RGB；
    - 解析失败抛 :class:`ValueError`（由 Web 层转 400）。
    """
    out_path = Path(out_path)
    try:
        with Image.open(io.BytesIO(raw_bytes)) as image:
            image = ImageOps.exif_transpose(image)
            image.load()
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            image.save(out_path, format="PNG")
    except (OSError, ValueError) as exc:
        raise ValueError(f"图片解析失败：{exc}") from exc
    return out_path


# ---------------------------------------------------------------------------
# 话次数字与文案
# ---------------------------------------------------------------------------


def int_to_chinese(n: int) -> str:
    """把 1~9999 的整数转成中文数字（1→一，10→十，11→十一，105→一百零五）。

    超出支持范围时回退为阿拉伯数字字符串，保证调用方永不抛异常。
    """
    if not isinstance(n, int) or isinstance(n, bool) or n < 1 or n > 9999:
        return str(n)
    text = str(n)
    length = len(text)
    parts: list[str] = []
    zero_pending = False
    for index, char in enumerate(text):
        digit = int(char)
        unit_index = length - 1 - index
        if digit == 0:
            zero_pending = True
            continue
        if zero_pending:
            parts.append("零")
            zero_pending = False
        parts.append(_DIGITS[digit])
        parts.append(_UNITS[unit_index])
    result = "".join(parts)
    # 口语习惯：11~19 说「十一」而不是「一十一」。
    if result.startswith("一十"):
        result = result[1:]
    return result


def format_cover_label(
    episode_no: int,
    spec: CoverTextSpec,
    override_text: str | None = None,
) -> str:
    """生成封页话次文案。

    - 非空 ``override_text`` 直接 strip 返回；
    - 否则用 ``{ep}``（受 ``use_chinese_numeral`` 控制）与 ``{ep_no}``（恒阿拉伯）
      填充 ``spec.template``。
    """
    if override_text is not None and override_text.strip():
        return override_text.strip()
    ep_text = int_to_chinese(episode_no) if spec.use_chinese_numeral else str(episode_no)
    return spec.template.format(ep=ep_text, ep_no=episode_no)


# ---------------------------------------------------------------------------
# 自适应画布与封页/结尾渲染
# ---------------------------------------------------------------------------


def _safe_hex_color(value: str, fallback: str) -> str:
    """校验 HEX 颜色（``#RRGGBB`` / ``#RGB``），非法时返回兜底色。"""
    if isinstance(value, str) and _HEX_COLOR_RE.match(value.strip()):
        return value.strip()
    return fallback


def _load_cover_font(spec: CoverTextSpec, size: int) -> ImageFont.ImageFont:
    """按规格加载字体；指定字体与默认中文字体都失败时回退 Pillow 内置位图字体。"""
    for font_name in (spec.font_name, DEFAULT_FONT_NAME):
        try:
            return load_font(resolve_font_path(font_name), size)
        except Exception:  # noqa: BLE001 - 字体环境不可控，逐级回退保证可用
            continue
    logger.warning("branding: 系统未找到任何可用 TrueType 中文字体，回退 Pillow 内置位图字体")
    return ImageFont.load_default()


def fit_image_to_canvas(
    image_path: str | Path,
    *,
    width: int = CANVAS_WIDTH,
    height: int = CANVAS_HEIGHT,
) -> Image.Image:
    """把任意比例图片铺成 ``width×height`` 的 9:16 RGB 画布。

    - 源图比例与目标差距 ``<= 0.03``：直接 Lanczos 拉伸铺满；
    - 否则先 cover-crop 放大裁切 → GaussianBlur(30) → 压暗到 62% 做柔暗底，
      再把源图 :func:`ImageOps.contain` 等比居中贴上——既无黑边，也不会裁掉
      用户预留的写字区域。
    """
    with Image.open(image_path) as opened:
        source = ImageOps.exif_transpose(opened)
        source.load()

    target_ratio = width / height
    source_ratio = source.width / source.height
    if abs(source_ratio - target_ratio) <= 0.03:
        return source.convert("RGB").resize((width, height), Image.LANCZOS)

    background = ImageOps.fit(source, (width, height), method=Image.LANCZOS)
    background = background.filter(ImageFilter.GaussianBlur(radius=30))
    background = ImageEnhance.Brightness(background).enhance(0.62).convert("RGB")

    contained = ImageOps.contain(source, (width, height), method=Image.LANCZOS)
    offset = ((width - contained.width) // 2, (height - contained.height) // 2)
    if contained.mode == "RGBA":
        background.paste(contained, offset, contained)
    else:
        background.paste(contained.convert("RGB"), offset)
    return background


def render_cover_image(
    image_path: str | Path,
    out_path: str | Path,
    *,
    episode_no: int,
    cover_text: CoverTextSpec,
    override_text: str | None = None,
    width: int = CANVAS_WIDTH,
    height: int = CANVAS_HEIGHT,
) -> Path:
    """渲染含话次文字的封页 PNG（RGB），返回落盘路径。"""
    canvas = fit_image_to_canvas(image_path, width=width, height=height).convert("RGB")

    if cover_text.enabled:
        label = format_cover_label(episode_no, cover_text, override_text)
        font = _load_cover_font(cover_text, cover_text.font_size)
        draw = ImageDraw.Draw(canvas)
        anchor_x = cover_text.x_ratio * width
        anchor_y = cover_text.y_ratio * height
        fill = _safe_hex_color(cover_text.color, "#FFFFFF")
        stroke_fill = _safe_hex_color(cover_text.stroke_color, "#000000")
        stroke_width = max(0, int(cover_text.stroke_width))
        draw.text(
            (anchor_x, anchor_y),
            label,
            font=font,
            fill=fill,
            anchor="mm",
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="PNG")
    return out_path


def render_outro_image(
    image_path: str | Path,
    out_path: str | Path,
    *,
    width: int = CANVAS_WIDTH,
    height: int = CANVAS_HEIGHT,
) -> Path:
    """渲染规范化的结尾图 PNG（RGB，无文字），返回落盘路径。"""
    canvas = fit_image_to_canvas(image_path, width=width, height=height).convert("RGB")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="PNG")
    return out_path


# ---------------------------------------------------------------------------
# 单话渲染选项
# ---------------------------------------------------------------------------


class EpisodeRenderOptions(_StrictModel):
    """单话封页/结尾覆盖项（``episodes/ep_XXX/render_options.json``）。

    与作品级 :class:`~core.config.SeriesBrandingConfig` 配合，
    改动后只需重跑 Stage 7 合成即可生效。
    """

    #: 本话是否启用封页（False = 本话跳过片头）。
    cover_enabled: bool = True
    #: 封页文字覆盖；空串/空白归一为 None（None = 按模板自动生成「第一话」）。
    cover_text_override: str | None = None
    #: 结尾模式：inherit = 跟随作品默认。
    outro_mode: Literal["inherit", "none", "continue", "final"] = "inherit"

    @field_validator("cover_text_override")
    @classmethod
    def _normalize_override(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


def load_episode_render_options(episode_dir: str | Path) -> EpisodeRenderOptions:
    """读取单话渲染选项；文件不存在或损坏时安全返回默认实例。"""
    path = Path(episode_dir) / RENDER_OPTIONS_FILENAME
    if not path.is_file():
        return EpisodeRenderOptions()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return EpisodeRenderOptions.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError):
        logger.warning("branding: 渲染选项文件损坏，回退默认值：%s", path)
        return EpisodeRenderOptions()


def save_episode_render_options(
    episode_dir: str | Path,
    options: EpisodeRenderOptions,
) -> Path:
    """落盘单话渲染选项，返回文件路径。"""
    episode_dir = Path(episode_dir)
    episode_dir.mkdir(parents=True, exist_ok=True)
    path = episode_dir / RENDER_OPTIONS_FILENAME
    path.write_text(options.model_dump_json(indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 最终片包解析
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectiveBranding:
    """作品级配置 + 单话选项解析后的最终封页/结尾决策。"""

    cover_enabled: bool
    cover_image_path: Path | None
    cover_duration_ms: int
    cover_fade_in_ms: int
    cover_fade_out_ms: int
    cover_text: CoverTextSpec
    cover_text_override: str | None
    #: 已把 ``inherit`` 解析掉的实际结尾模式。
    outro_mode: Literal["none", "continue", "final"]
    outro_image_path: Path | None
    outro_duration_ms: int
    outro_fade_in_ms: int
    outro_fade_out_ms: int


def _resolve_image(series_dir: Path, relative_path: str | None) -> Path | None:
    """把作品目录相对路径解析为绝对路径；文件缺失时返回 None（优雅跳过）。"""
    if not relative_path:
        return None
    candidate = Path(relative_path)
    resolved = candidate if candidate.is_absolute() else series_dir / candidate
    return resolved if resolved.is_file() else None


def resolve_effective_branding(
    series_config: SeriesConfig,
    options: EpisodeRenderOptions,
    series_dir: str | Path,
) -> EffectiveBranding:
    """合并作品级片包配置与单话覆盖项，得到 Stage 7 可直接消费的决策对象。

    任何底图文件缺失都降级为「不启用」而不是抛异常，保证流水线不中断。
    """
    branding: SeriesBrandingConfig = series_config.branding
    series_dir = Path(series_dir)

    cover_path = _resolve_image(series_dir, branding.cover_image)
    cover_enabled = bool(options.cover_enabled and cover_path is not None)

    outro_mode: Literal["none", "continue", "final"] = (
        branding.default_outro_mode
        if options.outro_mode == "inherit"
        else options.outro_mode
    )
    if outro_mode == "continue":
        outro_path = _resolve_image(series_dir, branding.outro_continue_image)
    elif outro_mode == "final":
        outro_path = _resolve_image(series_dir, branding.outro_final_image)
    else:
        outro_path = None
    if outro_path is None:
        outro_mode = "none"

    return EffectiveBranding(
        cover_enabled=cover_enabled,
        cover_image_path=cover_path if cover_enabled else None,
        cover_duration_ms=branding.cover_duration_ms,
        cover_fade_in_ms=branding.cover_fade_in_ms,
        cover_fade_out_ms=branding.cover_fade_out_ms,
        cover_text=branding.cover_text,
        cover_text_override=options.cover_text_override,
        outro_mode=outro_mode,
        outro_image_path=outro_path,
        outro_duration_ms=branding.outro_duration_ms,
        outro_fade_in_ms=branding.outro_fade_in_ms,
        outro_fade_out_ms=branding.outro_fade_out_ms,
    )


__all__ = [
    "BRANDING_DIR_NAME",
    "BRANDING_SLOTS",
    "CANVAS_HEIGHT",
    "CANVAS_WIDTH",
    "MAX_BRANDING_BYTES",
    "RENDER_OPTIONS_FILENAME",
    "EffectiveBranding",
    "EpisodeRenderOptions",
    "decode_base64_image",
    "fit_image_to_canvas",
    "format_cover_label",
    "int_to_chinese",
    "load_episode_render_options",
    "render_cover_image",
    "render_outro_image",
    "resolve_effective_branding",
    "save_branding_image",
    "save_episode_render_options",
]
