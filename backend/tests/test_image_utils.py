"""图片缩放与 Base64 Data URL 编码的离线测试。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from providers.image_utils import (
    DATA_URL_PREFIX,
    MAX_VISION_EDGE,
    decode_data_url_size,
    encode_image_data_url,
    resize_for_vision,
)


def _write_image(path: Path, size: tuple[int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (180, 40, 60)).save(path)
    return path


def test_resize_for_vision_limits_long_edge() -> None:
    image = Image.new("RGB", (2000, 1000))
    resized = resize_for_vision(image)
    assert resized.size == (MAX_VISION_EDGE, 640)


def test_resize_for_vision_keeps_portrait_ratio() -> None:
    resized = resize_for_vision(Image.new("RGB", (1000, 2000)))
    assert resized.size == (640, MAX_VISION_EDGE)


def test_resize_for_vision_does_not_upscale() -> None:
    resized = resize_for_vision(Image.new("RGB", (400, 300)))
    assert resized.size == (400, 300)


def test_encode_image_data_url_produces_jpeg_data_url(tmp_path) -> None:
    path = _write_image(tmp_path / "panel.png", (900, 1600))
    data_url = encode_image_data_url(path)
    assert data_url.startswith(DATA_URL_PREFIX)
    assert decode_data_url_size(data_url) == (720, 1280)


def test_encode_image_data_url_resizes_large_image(tmp_path) -> None:
    path = _write_image(tmp_path / "big.png", (2000, 3000))
    width, height = decode_data_url_size(encode_image_data_url(path))
    assert max(width, height) == MAX_VISION_EDGE
