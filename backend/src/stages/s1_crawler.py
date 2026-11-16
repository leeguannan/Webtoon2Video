"""Stage 1：WEBTOON 图片**仅本地下载**（规范第 4.2 节 + 铁律一）。

铁律：所有对 ``webtoons.com`` 的请求必须携带 ``Referer: https://www.webtoons.com/``
（由 ``core.http_client.build_client`` 统一注入，本模块不再各自拼请求头）。

流程：观看页 HTML → 提取图片直链 → 并发下载 → **校验图片头防残缺** → 写入
``episodes/ep_XXX/01_raw/``。另支持 ``local://`` 本地文件夹导入（直接复制本地图片）。
本模块不上传、不做云端处理，只落本地文件。
"""

from __future__ import annotations

import asyncio
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from PIL import Image

from core.http_client import is_local_url, local_path_from_url, make_local_url
from core.manifest import STAGE_DIR_NAMES
from core.schemas import EpisodeCatalogItem
from stages.s0_catalog import iter_local_images

#: 支持的图片格式（文件头魔数）。
IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
)

#: 格式 → 落盘扩展名。
IMAGE_EXTENSIONS: dict[str, str] = {
    "jpeg": ".jpg",
    "png": ".png",
    "gif": ".gif",
    "webp": ".webp",
    "bmp": ".bmp",
}

#: 单个文件大小上限（防超大图/恶意响应）。
MAX_IMAGE_BYTES = 32 * 1024 * 1024

_IMAGE_URL_RE = re.compile(
    r"^https?://[^\s\"']+\.(?:jpe?g|png|gif|webp|bmp)(?:\?[^\s\"']*)?$", re.IGNORECASE
)

#: 站点用于占位的静态资源（透明底图 / loading 图），不是漫画页。
_PLACEHOLDER_URL_RE = re.compile(
    r"(?:bg_transparency|placeholder|spacer|loading|blank)[^/]*\.(?:jpe?g|png|gif|webp)",
    re.IGNORECASE,
)


class CrawlerError(RuntimeError):
    """Stage 1 下载流程失败。"""


class ImageValidationError(CrawlerError):
    """图片校验失败（非图片、截断或损坏）。"""


@dataclass
class CrawlResult:
    """单话下载结果。"""

    episode_no: int
    episode_id: str
    raw_dir: Path
    saved: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def total(self) -> int:
        return len(self.saved) + len(self.skipped) + len(self.failures)


# --------------------------------------------------------------------------
# 图片校验
# --------------------------------------------------------------------------
def detect_image_format(data: bytes) -> str | None:
    """按文件头魔数判定图片格式；不是图片返回 ``None``。"""
    if not data:
        return None
    for magic, fmt in IMAGE_MAGIC:
        if data.startswith(magic):
            return fmt
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def validate_image_bytes(data: bytes, *, deep_verify: bool = True) -> str:
    """校验图片字节，返回图片格式；不合格抛 ``ImageValidationError``。

    校验两层：

    1. **图片头**：魔数必须命中已知格式（挡住 HTML 错误页、空响应）。
    2. **深度校验**（``deep_verify=True``）：用 Pillow 解码校验，能挡住「头正常但内容被截断」的残图。
    """
    if not data:
        raise ImageValidationError("图片内容为空")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageValidationError(f"图片超过大小上限（{len(data)} 字节）")
    fmt = detect_image_format(data)
    if fmt is None:
        raise ImageValidationError(f"图片头非法，前 16 字节：{data[:16]!r}")
    if deep_verify:
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
        except Exception as exc:  # Pillow 可能抛多种异常，统一转成校验失败
            raise ImageValidationError(f"图片内容损坏：{exc}") from exc
    return fmt


# --------------------------------------------------------------------------
# 观看页解析
# --------------------------------------------------------------------------
def parse_image_urls(html: str) -> list[str]:
    """从话次观看页 HTML 提取图片直链（保持原始顺序、去重）。

    真实站点把原图放在 ``data-url``，``src`` 是透明占位图；旧站点用
    ``data-src`` / ``data-original`` / ``data-lazy-src``。因此按
    「数据属性优先、``src`` 兜底」的顺序取值，并丢弃占位图。
    """
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one(
        "#_imageList, div._imageList, div.image_list, div.viewer_img, #imageList"
    )
    nodes = container.find_all("img") if container is not None else soup.find_all("img")
    urls: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        src = (
            node.get("data-url")
            or node.get("data-src")
            or node.get("data-original")
            or node.get("data-lazy-src")
            or node.get("src")
            or ""
        ).strip()
        if not src or src.startswith("data:"):
            continue
        if _PLACEHOLDER_URL_RE.search(src):
            continue
        if not _IMAGE_URL_RE.match(src):
            continue
        if src in seen:
            continue
        seen.add(src)
        urls.append(src)
    return urls


def image_filename(index: int, fmt: str) -> str:
    """切片文件名（``001.jpg`` / ``002.png``），编号即切片顺序。"""
    return f"{index:03d}{IMAGE_EXTENSIONS.get(fmt, '.bin')}"


def raw_dir_for(series_dir: str | Path, episode_no: int) -> Path:
    """某话的原始图片目录：``episodes/ep_XXX/01_raw/``。"""
    return (
        Path(series_dir)
        / "episodes"
        / f"ep_{episode_no:03d}"
        / STAGE_DIR_NAMES["s1_crawler"]
    )


def _find_existing(raw_dir: Path, index: int) -> Path | None:
    """查找已下载且文件头合法的同序号切片（用于断点续跑）。"""
    for extension in IMAGE_EXTENSIONS.values():
        candidate = raw_dir / f"{index:03d}{extension}"
        if candidate.is_file():
            try:
                validate_image_bytes(candidate.read_bytes(), deep_verify=False)
            except ImageValidationError:
                continue
            return candidate
    return None


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------
async def _fetch_and_store(
    client: httpx.AsyncClient,
    url: str,
    raw_dir: Path,
    index: int,
    *,
    deep_verify: bool,
) -> tuple[Path | None, str | None]:
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        return None, f"请求失败：{exc}"
    if response.status_code != 200:
        return None, f"HTTP {response.status_code}"
    data = response.content
    expected = response.headers.get("content-length")
    if expected and expected.isdigit() and int(expected) != len(data):
        return None, f"响应体不完整（content-length={expected}，实际 {len(data)} 字节）"
    try:
        fmt = validate_image_bytes(data, deep_verify=deep_verify)
    except ImageValidationError as exc:
        return None, str(exc)
    target = raw_dir / image_filename(index, fmt)
    temp = raw_dir / f".{target.name}.part"
    temp.write_bytes(data)
    temp.replace(target)
    return target, None


async def _download_one(
    client: httpx.AsyncClient,
    url: str,
    raw_dir: Path,
    index: int,
    *,
    semaphore: asyncio.Semaphore,
    deep_verify: bool,
) -> tuple[Path | None, str | None, bool]:
    async with semaphore:
        existing = _find_existing(raw_dir, index)
        if existing is not None:
            return existing, None, True
        path, error = await _fetch_and_store(client, url, raw_dir, index, deep_verify=deep_verify)
        return path, error, False


async def download_images(
    client: httpx.AsyncClient,
    urls: list[str],
    raw_dir: str | Path,
    *,
    concurrency: int = 8,
    deep_verify: bool = True,
) -> tuple[list[Path], list[Path], list[dict[str, str]]]:
    """并发下载图片到 ``raw_dir``。

    返回 ``(已保存, 已跳过（断点续跑）, 失败列表)``；失败项含 ``url`` 与 ``error``。
    """
    target_dir = Path(raw_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    outcomes = await asyncio.gather(
        *(
            _download_one(
                client, url, target_dir, index, semaphore=semaphore, deep_verify=deep_verify
            )
            for index, url in enumerate(urls, start=1)
        )
    )
    saved: list[Path] = []
    skipped: list[Path] = []
    failures: list[dict[str, str]] = []
    for url, (path, error, was_skipped) in zip(urls, outcomes):
        if error:
            failures.append({"url": url, "error": error})
            continue
        if path is None:
            failures.append({"url": url, "error": "未知失败"})
            continue
        (skipped if was_skipped else saved).append(path)
    return saved, skipped, failures


# --------------------------------------------------------------------------
# 单话抓取
# --------------------------------------------------------------------------
def import_local_episode(
    local_dir: str | Path,
    raw_dir: str | Path,
    *,
    episode_no: int = 1,
    episode_id: str = "",
    deep_verify: bool = True,
) -> CrawlResult:
    """把本地目录的图片导入 ``01_raw/``（``local://`` 导入）。"""
    source = Path(local_dir).expanduser().resolve()
    target_dir = Path(raw_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    result = CrawlResult(episode_no=episode_no, episode_id=episode_id, raw_dir=target_dir)
    if not source.is_dir():
        result.failures.append({"url": make_local_url(source), "error": "本地目录不存在"})
        return result

    images = iter_local_images(source)
    if not images:
        result.failures.append({"url": make_local_url(source), "error": "本地目录没有可用图片"})
        return result
    for index, image in enumerate(images, start=1):
        data = image.read_bytes()
        try:
            fmt = validate_image_bytes(data, deep_verify=deep_verify)
        except ImageValidationError as exc:
            result.failures.append({"url": str(image), "error": str(exc)})
            continue
        target = target_dir / image_filename(index, fmt)
        target.write_bytes(data)
        result.saved.append(target)
    return result


async def crawl_episode(
    client: httpx.AsyncClient,
    episode: EpisodeCatalogItem,
    raw_dir: str | Path,
    *,
    concurrency: int = 8,
    deep_verify: bool = True,
) -> CrawlResult:
    """抓取单话的全部原始图片到 ``raw_dir``。"""
    target_dir = Path(raw_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    result = CrawlResult(
        episode_no=episode.episode_no, episode_id=episode.episode_id, raw_dir=target_dir
    )

    if is_local_url(episode.viewer_url):
        local_dir = local_path_from_url(episode.viewer_url)
        local_result = import_local_episode(
            local_dir,
            target_dir,
            episode_no=episode.episode_no,
            episode_id=episode.episode_id,
            deep_verify=deep_verify,
        )
        result.saved = local_result.saved
        result.skipped = local_result.skipped
        result.failures = local_result.failures
        return result

    try:
        response = await client.get(episode.viewer_url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        result.failures.append({"url": episode.viewer_url, "error": f"观看页抓取失败：{exc}"})
        return result

    urls = parse_image_urls(response.text)
    if not urls:
        result.failures.append({"url": episode.viewer_url, "error": "观看页未解析到图片直链"})
        return result

    saved, skipped, failures = await download_images(
        client, urls, target_dir, concurrency=concurrency, deep_verify=deep_verify
    )
    result.saved = saved
    result.skipped = skipped
    result.failures = failures
    return result


async def crawl_episodes(
    client: httpx.AsyncClient,
    episodes: list[EpisodeCatalogItem],
    series_dir: str | Path,
    *,
    concurrency: int = 8,
    deep_verify: bool = True,
) -> list[CrawlResult]:
    """按话次批量下载到 ``series_dir/episodes/ep_XXX/01_raw/``。"""
    results: list[CrawlResult] = []
    for episode in episodes:
        results.append(
            await crawl_episode(
                client,
                episode,
                raw_dir_for(series_dir, episode.episode_no),
                concurrency=concurrency,
                deep_verify=deep_verify,
            )
        )
    return results
