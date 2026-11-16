"""Stage 1 单元测试（离线 Mock）：只落本地、Referer 必带、图片验损、断点续跑、local:// 导入。"""

from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

from core.http_client import build_client, make_local_url
from core.schemas import EpisodeCatalogItem
from stages import s1_crawler
from stages.s1_crawler import (
    CrawlResult,
    ImageValidationError,
    crawl_episode,
    crawl_episodes,
    detect_image_format,
    download_images,
    image_filename,
    import_local_episode,
    parse_image_urls,
    raw_dir_for,
    validate_image_bytes,
)

VIEWER_URL = (
    "https://www.webtoons.com/en/fantasy/test-comic/ep-1/viewer?title_no=95&episode_no=1"
)
PANEL_1 = "https://cdn.example.com/panel_001.png"
PANEL_2 = "https://cdn.example.com/panel_002.png"

VIEWER_HTML = """
<!doctype html>
<html><body>
  <div id="_imageList">
    <img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" data-src="https://cdn.example.com/panel_001.png">
    <img src="https://cdn.example.com/panel_002.png">
    <img data-src="https://cdn.example.com/panel_001.png">
    <img src="https://cdn.example.com/tracker.js">
  </div>
</body></html>
"""


def png_bytes(color: tuple[int, int, int] = (255, 0, 0), size: tuple[int, int] = (16, 16)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (0, 0, 255)).save(buffer, format="JPEG")
    return buffer.getvalue()


# --------------------------------------------------------------------------
# 图片头 / 验损
# --------------------------------------------------------------------------
def test_detect_image_format() -> None:
    assert detect_image_format(png_bytes()) == "png"
    assert detect_image_format(jpeg_bytes()) == "jpeg"
    assert detect_image_format(b"GIF89a....") == "gif"
    assert detect_image_format(b"BM......") == "bmp"
    assert detect_image_format(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert detect_image_format(b"<html>not an image</html>") is None
    assert detect_image_format(b"") is None


def test_validate_image_bytes_accepts_valid_image() -> None:
    assert validate_image_bytes(png_bytes()) == "png"
    assert validate_image_bytes(jpeg_bytes()) == "jpeg"


def test_validate_image_bytes_rejects_empty_and_html() -> None:
    with pytest.raises(ImageValidationError):
        validate_image_bytes(b"")
    with pytest.raises(ImageValidationError):
        validate_image_bytes(b"<!DOCTYPE html><html>403 Forbidden</html>")


def test_validate_image_bytes_deep_verify_catches_truncated_image() -> None:
    data = png_bytes()
    truncated = data[: int(len(data) * 0.6)]

    # 头合法但内容被截断：深度校验必须报错
    with pytest.raises(ImageValidationError):
        validate_image_bytes(truncated, deep_verify=True)

    # 只查头时不报错——说明两层校验职责不同
    assert validate_image_bytes(truncated, deep_verify=False) == "png"


def test_validate_image_bytes_rejects_broken_payload() -> None:
    broken = b"\x89PNG\r\n\x1a\n" + b"garbage" * 10
    with pytest.raises(ImageValidationError):
        validate_image_bytes(broken)


def test_validate_image_bytes_enforces_size_limit(monkeypatch) -> None:
    monkeypatch.setattr(s1_crawler, "MAX_IMAGE_BYTES", 16)
    with pytest.raises(ImageValidationError):
        validate_image_bytes(png_bytes())


# --------------------------------------------------------------------------
# 观看页解析与命名
# --------------------------------------------------------------------------
def test_parse_image_urls_keeps_order_and_drops_junk() -> None:
    assert parse_image_urls(VIEWER_HTML) == [PANEL_1, PANEL_2]


def test_parse_image_urls_without_container() -> None:
    html = '<html><body><img src="https://cdn.example.com/a.jpg"></body></html>'
    assert parse_image_urls(html) == ["https://cdn.example.com/a.jpg"]


def test_image_filename() -> None:
    assert image_filename(1, "png") == "001.png"
    assert image_filename(12, "jpeg") == "012.jpg"
    assert image_filename(3, "webp") == "003.webp"


def test_raw_dir_for() -> None:
    assert raw_dir_for("/tmp/series", 3).as_posix() == "/tmp/series/episodes/ep_003/01_raw"


# --------------------------------------------------------------------------
# 并发下载
# --------------------------------------------------------------------------
@pytest.mark.anyio
async def test_download_images_saves_files_locally(respx_mock, tmp_path) -> None:
    respx_mock.get(PANEL_1).mock(return_value=httpx.Response(200, content=png_bytes((255, 0, 0))))
    respx_mock.get(PANEL_2).mock(return_value=httpx.Response(200, content=png_bytes((0, 255, 0))))

    raw_dir = tmp_path / "01_raw"
    async with build_client() as client:
        saved, skipped, failures = await download_images(client, [PANEL_1, PANEL_2], raw_dir)

    assert failures == []
    assert skipped == []
    assert [path.name for path in saved] == ["001.png", "002.png"]
    assert saved[0].read_bytes() == png_bytes((255, 0, 0))
    assert all(not path.name.endswith(".part") for path in raw_dir.iterdir())


@pytest.mark.anyio
async def test_download_images_always_sends_referer(respx_mock, tmp_path) -> None:
    respx_mock.get(PANEL_1).mock(return_value=httpx.Response(200, content=png_bytes()))

    async with build_client() as client:
        await download_images(client, [PANEL_1], tmp_path / "01_raw")

    assert respx_mock.calls
    for call in respx_mock.calls:
        assert call.request.headers["referer"] == "https://www.webtoons.com/"


@pytest.mark.anyio
async def test_download_images_rejects_non_image_payload(respx_mock, tmp_path) -> None:
    respx_mock.get(PANEL_1).mock(return_value=httpx.Response(200, content=png_bytes()))
    respx_mock.get(PANEL_2).mock(
        return_value=httpx.Response(200, content=b"<html>404</html>", headers={"content-type": "text/html"})
    )

    raw_dir = tmp_path / "01_raw"
    async with build_client() as client:
        saved, _skipped, failures = await download_images(client, [PANEL_1, PANEL_2], raw_dir)

    assert [path.name for path in saved] == ["001.png"]
    assert len(failures) == 1
    assert failures[0]["url"] == PANEL_2
    assert "图片头非法" in failures[0]["error"]
    assert not (raw_dir / "002.png").exists()


@pytest.mark.anyio
async def test_download_images_rejects_truncated_image(respx_mock, tmp_path) -> None:
    data = png_bytes()
    respx_mock.get(PANEL_1).mock(return_value=httpx.Response(200, content=data[: int(len(data) * 0.6)]))

    async with build_client() as client:
        saved, _skipped, failures = await download_images(client, [PANEL_1], tmp_path / "01_raw")

    assert saved == []
    assert len(failures) == 1
    assert "损坏" in failures[0]["error"]


@pytest.mark.anyio
async def test_download_images_rejects_incomplete_body(respx_mock, tmp_path) -> None:
    respx_mock.get(PANEL_1).mock(
        return_value=httpx.Response(
            200, content=png_bytes(), headers={"content-length": "999999"}
        )
    )

    raw_dir = tmp_path / "01_raw"
    async with build_client() as client:
        saved, _skipped, failures = await download_images(client, [PANEL_1], raw_dir)

    assert saved == []
    assert len(failures) == 1
    assert not (raw_dir / "001.png").exists()


@pytest.mark.anyio
async def test_download_images_skips_existing_valid_files(respx_mock, tmp_path) -> None:
    respx_mock.get(PANEL_1).mock(return_value=httpx.Response(200, content=png_bytes()))
    respx_mock.get(PANEL_2).mock(return_value=httpx.Response(200, content=png_bytes()))

    raw_dir = tmp_path / "01_raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / "001.png").write_bytes(png_bytes())

    async with build_client() as client:
        saved, skipped, failures = await download_images(client, [PANEL_1, PANEL_2], raw_dir)

    assert failures == []
    assert [path.name for path in skipped] == ["001.png"]
    assert [path.name for path in saved] == ["002.png"]
    # 断点续跑：已存在的切片不再发请求
    assert len(respx_mock.calls) == 1


# --------------------------------------------------------------------------
# local:// 导入
# --------------------------------------------------------------------------
def test_import_local_episode_copies_images(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "001.png").write_bytes(png_bytes())
    (source / "002.png").write_bytes(png_bytes())
    (source / "notes.txt").write_text("not an image", encoding="utf-8")

    raw_dir = tmp_path / "01_raw"
    result = import_local_episode(source, raw_dir, episode_no=4, episode_id="wt_95_ep_004")

    assert result.ok is True
    assert result.episode_no == 4
    assert result.episode_id == "wt_95_ep_004"
    assert [path.name for path in result.saved] == ["001.png", "002.png"]
    assert (raw_dir / "001.png").read_bytes() == png_bytes()


def test_import_local_episode_reports_corrupt_file(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "001.png").write_bytes(b"\x89PNG\r\n\x1a\n broken")

    result = import_local_episode(source, tmp_path / "01_raw")

    assert result.ok is False
    assert result.saved == []
    assert len(result.failures) == 1
    assert "损坏" in result.failures[0]["error"]


def test_import_local_episode_reports_missing_dir(tmp_path) -> None:
    result = import_local_episode(tmp_path / "missing", tmp_path / "01_raw")
    assert result.ok is False
    assert "不存在" in result.failures[0]["error"]


def test_import_local_episode_reports_empty_dir(tmp_path) -> None:
    source = tmp_path / "empty"
    source.mkdir()
    result = import_local_episode(source, tmp_path / "01_raw")
    assert result.ok is False
    assert "没有可用图片" in result.failures[0]["error"]


# --------------------------------------------------------------------------
# 单话 / 批量抓取
# --------------------------------------------------------------------------
@pytest.mark.anyio
async def test_crawl_episode_downloads_from_viewer(respx_mock, tmp_path) -> None:
    respx_mock.get(VIEWER_URL).mock(return_value=httpx.Response(200, text=VIEWER_HTML))
    respx_mock.get(PANEL_1).mock(return_value=httpx.Response(200, content=png_bytes()))
    respx_mock.get(PANEL_2).mock(return_value=httpx.Response(200, content=png_bytes()))

    episode = EpisodeCatalogItem(
        episode_no=1,
        episode_id="wt_95_ep_001",
        title="Episode 1",
        viewer_url=VIEWER_URL,
    )
    raw_dir = tmp_path / "01_raw"
    async with build_client() as client:
        result = await crawl_episode(client, episode, raw_dir)

    assert result.ok is True
    assert result.total == 2
    assert [path.name for path in result.saved] == ["001.png", "002.png"]
    # 观看页与图片请求都带 Referer
    for call in respx_mock.calls:
        assert call.request.headers["referer"] == "https://www.webtoons.com/"


@pytest.mark.anyio
async def test_crawl_episode_supports_local_url(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "001.png").write_bytes(png_bytes())

    episode = EpisodeCatalogItem(
        episode_no=2,
        episode_id="wt_95_ep_002",
        title="Episode 2",
        viewer_url=make_local_url(source),
    )
    async with build_client() as client:
        result = await crawl_episode(client, episode, tmp_path / "01_raw")

    assert result.ok is True
    assert [path.name for path in result.saved] == ["001.png"]


@pytest.mark.anyio
async def test_crawl_episode_reports_viewer_failure(respx_mock, tmp_path) -> None:
    respx_mock.get(VIEWER_URL).mock(return_value=httpx.Response(404, text="nope"))

    episode = EpisodeCatalogItem(
        episode_no=1,
        episode_id="wt_95_ep_001",
        title="Episode 1",
        viewer_url=VIEWER_URL,
    )
    async with build_client() as client:
        result = await crawl_episode(client, episode, tmp_path / "01_raw")

    assert result.ok is False
    assert "观看页抓取失败" in result.failures[0]["error"]


@pytest.mark.anyio
async def test_crawl_episode_reports_missing_images(respx_mock, tmp_path) -> None:
    respx_mock.get(VIEWER_URL).mock(return_value=httpx.Response(200, text="<html></html>"))

    episode = EpisodeCatalogItem(
        episode_no=1,
        episode_id="wt_95_ep_001",
        title="Episode 1",
        viewer_url=VIEWER_URL,
    )
    async with build_client() as client:
        result = await crawl_episode(client, episode, tmp_path / "01_raw")

    assert result.ok is False
    assert "未解析到图片直链" in result.failures[0]["error"]


@pytest.mark.anyio
async def test_crawl_episodes_writes_per_episode_dirs(respx_mock, tmp_path) -> None:
    respx_mock.get(VIEWER_URL).mock(return_value=httpx.Response(200, text=VIEWER_HTML))
    respx_mock.get(PANEL_1).mock(return_value=httpx.Response(200, content=png_bytes()))
    respx_mock.get(PANEL_2).mock(return_value=httpx.Response(200, content=png_bytes()))

    episodes = [
        EpisodeCatalogItem(
            episode_no=1, episode_id="wt_95_ep_001", title="Episode 1", viewer_url=VIEWER_URL
        ),
        EpisodeCatalogItem(
            episode_no=2, episode_id="wt_95_ep_002", title="Episode 2", viewer_url=VIEWER_URL
        ),
    ]
    async with build_client() as client:
        results = await crawl_episodes(client, episodes, tmp_path)

    assert len(results) == 2
    assert (tmp_path / "episodes" / "ep_001" / "01_raw" / "001.png").is_file()
    assert (tmp_path / "episodes" / "ep_002" / "01_raw" / "001.png").is_file()


def test_crawl_result_properties() -> None:
    result = CrawlResult(episode_no=1, episode_id="e1", raw_dir="x")
    assert result.ok is True
    assert result.total == 0
    result.failures.append({"url": "u", "error": "e"})
    assert result.ok is False
    assert result.total == 1
