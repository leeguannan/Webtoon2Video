"""图片预处理：等比缩放 + Base64 Data URL 编码。

规范第 2 节：百炼视觉模型只接受 **长边 ≤ 1280px** 的图片（``qwen3-vl-plus``），
且必须以 Base64 Data URL 内联传入，不能直���把几十张切片拼成超大图发过去。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image, ImageOps

#: 百炼视觉模型的长边上限（规范第 2 节硬约束）。
MAX_VISION_EDGE = 1280

#: JPEG 编码质量。
DEFAULT_QUALITY = 85

DATA_URL_PREFIX = "data:image/jpeg;base64,"


def load_image(path: str | Path) -> Image.Image:
    """读取图片并按 EXIF 方向摆正，统一转成 RGB。"""
    with Image.open(path) as raw:
        image = ImageOps.exif_transpose(raw)
        return image.convert("RGB")


def resize_for_vision(image: Image.Image, *, max_edge: int = MAX_VISION_EDGE) -> Image.Image:
    """等比缩放，保证长边不超过 ``max_edge``；小图不放大。"""
    width, height = image.size
    longest = max(width, height)
    if longest <= 0 or longest <= max_edge:
        return image
    ratio = max_edge / longest
    target = (max(1, round(width * ratio)), max(1, round(height * ratio)))
    return image.resize(target, Image.Resampling.LANCZOS)


def encode_pil_data_url(
    image: Image.Image,
    *,
    quality: int = DEFAULT_QUALITY,
) -> str:
    """把**内存中**的 PIL 图片编码成 Base64 Data URL（不再做尺寸缩放）。

    Stage 3 的「超高画格纵向重叠切片」需要在内存里切好再送模型，
    切片本身已经是可控尺寸（≤1024×1600），不能再走「长边缩到 1280」那条路，
    否则等于把刚切好的细节又抹掉一次。
    """
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"{DATA_URL_PREFIX}{payload}"


def encode_image_data_url(
    path: str | Path,
    *,
    max_edge: int = MAX_VISION_EDGE,
    quality: int = DEFAULT_QUALITY,
) -> str:
    """把图片缩放并编码成 Base64 Data URL（``data:image/jpeg;base64,...``）。"""
    image = resize_for_vision(load_image(path), max_edge=max_edge)
    return encode_pil_data_url(image, quality=quality)


def decode_data_url_size(data_url: str) -> tuple[int, int]:
    """从 Data URL 反解出图片尺寸（测试与诊断用）。"""
    if not data_url.startswith(DATA_URL_PREFIX):
        raise ValueError("不是本模块生成的 JPEG Data URL")
    raw = base64.b64decode(data_url[len(DATA_URL_PREFIX) :])
    with Image.open(io.BytesIO(raw)) as image:
        return image.size
