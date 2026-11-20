"""Stage 0：作品目录抓取与全局注册表维护（规范第 1.1 / 4.2 节）。

职责边界：

* ``workspace/index.json``：全局作品索引表，列出所有已导入漫画（``SeriesIndexItem``）。
* ``workspace/{series_id}/catalog.json``：单部漫画的话次目录（``SeriesCatalog``）。
* **翻页抓取**：WEBTOON 目录页按页加载，本模块负责逐页跟随「下一页」直到抓完。
* **区间解析**：把目录页里的 ``1-50`` / ``Episodes 11-20`` 之类区间标题解析成话号区间；
  条目自身缺失话号时，按所在区间偏移回填。

本模块只抓元数据，**不下载任何图片**；图片下载属 Stage 1（``s1_crawler.py``）。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from core.http_client import make_local_url
from core.schemas import EpisodeCatalogItem, SeriesCatalog, SeriesIndexItem

#: 单次抓取最多翻多少页（防御死循环）。
MAX_PAGES = 200

#: 本地导入时认作图片的后缀。
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})


class CatalogError(RuntimeError):
    """Stage 0 抓取或注册表读写失败。"""


@dataclass(frozen=True)
class EpisodeRange:
    """话号区间（如 ``1-50``）。"""

    start: int
    end: int

    @property
    def count(self) -> int:
        return max(0, self.end - self.start + 1)

    def contains(self, episode_no: int) -> bool:
        return self.start <= episode_no <= self.end

    def __str__(self) -> str:
        return f"{self.start}-{self.end}"


# --------------------------------------------------------------------------
# 区间解析
# --------------------------------------------------------------------------
_RANGE_RE = re.compile(r"(?P<start>\d{1,4})\s*[-~～—]\s*(?P<end>\d{1,4})")
_DATE_LIKE_RE = re.compile(r"^\d{4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?$")
#: 区间标题（比通用区间更严格，避免把「Episode 1-2: 标题」误判成区间）。
_RANGE_HEADER_RE = re.compile(
    r"^(?:episodes?|chapters?|ch\.|no\.?)?\s*(?P<start>\d{1,4})\s*[-~～—]\s*(?P<end>\d{1,4})"
    r"(?:\s*(?:话|회|eps?))?$",
    re.IGNORECASE,
)
_EPISODE_NO_TEXT_RE = re.compile(r"(?:episode|ep\.?)\s*#?\s*(?P<no>\d{1,4})", re.IGNORECASE)
_DATE_TEXT_RE = re.compile(
    r"(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2})|(?:[A-Z][a-z]{2}\s+\d{1,2},\s*\d{4})"
)


def parse_episode_range(text: str) -> EpisodeRange | None:
    """从任意文本中解析 ``start-end`` 区间；不是区间返回 ``None``。"""
    if not text:
        return None
    stripped = text.strip()
    if _DATE_LIKE_RE.match(stripped):
        return None
    match = _RANGE_RE.search(stripped)
    if not match:
        return None
    start = int(match.group("start"))
    end = int(match.group("end"))
    if end < start:
        start, end = end, start
    return EpisodeRange(start, end)


def parse_range_header(text: str) -> EpisodeRange | None:
    """严格解析区间标题（``Episodes 11-20`` / ``1-50话``）；不像区间返回 ``None``。"""
    if not text:
        return None
    stripped = " ".join(text.split())
    if _DATE_LIKE_RE.match(stripped):
        return None
    match = _RANGE_HEADER_RE.match(stripped)
    if not match:
        return None
    start = int(match.group("start"))
    end = int(match.group("end"))
    if end < start:
        start, end = end, start
    return EpisodeRange(start, end)


def parse_episode_selection(spec: str) -> list[int]:
    """把 ``"1-5,8,10-12"`` 解析成 ``[1,2,3,4,5,8,10,11,12]``（去重升序）。"""
    numbers: set[int] = set()
    for chunk in re.split(r"[,，\s]+", (spec or "").strip()):
        if not chunk:
            continue
        rng = parse_episode_range(chunk)
        if rng is not None:
            numbers.update(range(rng.start, rng.end + 1))
            continue
        if chunk.isdigit():
            numbers.add(int(chunk))
            continue
        raise ValueError(f"无法解析话次区间：{chunk!r}")
    return sorted(numbers)


# --------------------------------------------------------------------------
# URL 派生
# --------------------------------------------------------------------------
def _slug_from_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    parts = [part for part in parts if part.lower() not in {"en", "zh", "kr", "ja"}]
    if parts and parts[-1].lower() in {"list", "viewer"}:
        parts = parts[:-1]
    slug = parts[-1] if parts else ""
    return re.sub(r"[^0-9a-zA-Z]+", "_", slug).strip("_").lower()


def series_id_from_url(list_url: str) -> str:
    """由目录页 URL 推导 ``series_id``（形如 ``wt_88201``）。"""
    query = parse_qs(urlparse(list_url).query)
    title_no = (query.get("title_no") or [""])[0]
    if title_no.isdigit():
        return f"wt_{title_no}"
    slug = _slug_from_url(list_url)
    return f"wt_{slug}" if slug else "wt_unknown"


def series_title_from_url(list_url: str) -> str:
    """由目录页 URL 推导作品标题（slug 首字母大写）。"""
    slug = _slug_from_url(list_url)
    if not slug:
        return "未命名作品"
    return " ".join(word.capitalize() for word in slug.split("_"))


# --------------------------------------------------------------------------
# 目录页解析
# --------------------------------------------------------------------------
_TITLE_SELECTORS = (".subj", ".episode_title", ".tit", ".title", "strong", "p")
_DATE_SELECTORS = (".date", ".date_wrap", ".subj_date", ".info .date", "time")
_EPISODE_LINK_MARKERS = ("/viewer", "episode_no=")


def _is_episode_link(href: str) -> bool:
    return any(marker in href for marker in _EPISODE_LINK_MARKERS)


def _episode_no_from_url(href: str) -> int | None:
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    raw = (query.get("episode_no") or [""])[0]
    if raw.isdigit():
        return int(raw)
    match = re.search(r"/ep-(\d+)", parsed.path)
    if match:
        return int(match.group(1))
    return None


def _episode_no_from_text(text: str) -> int | None:
    match = _EPISODE_NO_TEXT_RE.search(text or "")
    if match:
        return int(match.group("no"))
    return None


#: 目录页里「一条话次」的容器选择器（真实站点 + 旧版站点）。
_EPISODE_ITEM_SELECTORS = (
    "li._episodeItem",
    "li.detail_list_item",
    "ul.detail_list > li",
    "tr.detail_list_item",
)


def _episode_item_containers(soup: BeautifulSoup) -> list[Tag]:
    """取出目录页里的话次条目容器（去重）。"""
    containers: list[Tag] = []
    seen: set[int] = set()
    for selector in _EPISODE_ITEM_SELECTORS:
        for node in soup.select(selector):
            if id(node) in seen:
                continue
            seen.add(id(node))
            containers.append(node)
    return containers


def _episode_no_from_attr(container: Tag) -> int | None:
    """从 ``data-episode-no`` 属性取话次 ID（真实站点最可靠的来源）。"""
    raw = (container.get("data-episode-no") or "").strip()
    if raw.isdigit():
        return int(raw)
    raw = (container.get("data-episodeid") or "").strip()
    return int(raw) if raw.isdigit() else None


def _container_of(anchor: Tag) -> Tag:
    return anchor.find_parent("li") or anchor.find_parent("tr") or anchor.find_parent("div") or anchor


def _extract_title(container: Tag, anchor: Tag) -> str:
    for selector in _TITLE_SELECTORS:
        node = container.select_one(selector)
        if node is None:
            continue
        text = node.get_text(" ", strip=True)
        if text and not _DATE_LIKE_RE.match(text) and not _DATE_TEXT_RE.fullmatch(text):
            return text
    text = (anchor.get("title") or "").strip()
    if text:
        return text
    return anchor.get_text(" ", strip=True)


def _extract_date(container: Tag) -> str:
    for selector in _DATE_SELECTORS:
        node = container.select_one(selector)
        if node is None:
            continue
        text = node.get_text(" ", strip=True)
        if text:
            return text
    match = _DATE_TEXT_RE.search(container.get_text(" ", strip=True))
    return match.group(0) if match else ""


def _is_locked(container: Tag) -> bool:
    classes = " ".join(container.get("class") or []).lower()
    if "lock" in classes:
        return True
    if container.select_one("[class*='lock' i]") is not None:
        return True
    text = container.get_text(" ", strip=True).lower()
    return any(marker in text for marker in ("preview", "locked", "members only"))


class CatalogParser:
    """目录页 HTML → 结构化话次条目。"""

    @staticmethod
    def parse_series_title(html: str) -> str:
        """从目录页提取作品标题。"""
        soup = BeautifulSoup(html, "lxml")
        for selector in ("h1.subj", "h1", "meta[property='og:title']", "title"):
            node = soup.select_one(selector)
            if node is None:
                continue
            if node.name == "meta":
                text = (node.get("content") or "").strip()
            else:
                text = node.get_text(" ", strip=True)
            if not text:
                continue
            text = re.split(r"\s*[|｜]\s*WEBTOON", text, flags=re.IGNORECASE)[0].strip()
            if text:
                return text
        return ""

    @staticmethod
    def parse_episodes(html: str, *, list_url: str, series_id: str) -> list[EpisodeCatalogItem]:
        """解析目录页中的全部话次条目（按话号升序、话号去重）。

        真实站点的列表条目在 ``li._episodeItem`` 里，页面侧栏还有指向第 1 话的
        「开始阅读」按钮——后者不是目录条目，必须排除，否则会凭空多出一话。
        """
        soup = BeautifulSoup(html, "lxml")
        items: dict[int, EpisodeCatalogItem] = {}
        current_range: EpisodeRange | None = None
        offset = 0
        item_ids = {id(node) for node in _episode_item_containers(soup)}

        for tag in soup.find_all(True):
            if tag.name == "a":
                href = (tag.get("href") or "").strip()
                if not _is_episode_link(href):
                    continue
                if item_ids:
                    parent_item = tag.find_parent("li")
                    if parent_item is None or id(parent_item) not in item_ids:
                        continue
                container = _container_of(tag)
                episode_no = _episode_no_from_attr(container)
                if episode_no is None:
                    episode_no = _episode_no_from_url(href)
                if episode_no is None:
                    episode_no = _episode_no_from_text(tag.get_text(" ", strip=True))
                if episode_no is None:
                    episode_no = _episode_no_from_text(container.get_text(" ", strip=True))
                if episode_no is None:
                    if current_range is None:
                        continue
                    episode_no = current_range.start + offset
                    offset += 1
                elif current_range is not None and current_range.contains(episode_no):
                    offset = episode_no - current_range.start + 1
                if episode_no in items:
                    continue
                items[episode_no] = EpisodeCatalogItem(
                    episode_no=episode_no,
                    episode_id=f"{series_id}_ep_{episode_no:03d}",
                    title=_extract_title(container, tag),
                    viewer_url=urljoin(list_url, href),
                    publish_date=_extract_date(container),
                    is_locked=_is_locked(container),
                )
                continue

            if _contains_episode_link(tag):
                continue
            text = " ".join(tag.get_text(" ", strip=True).split())
            if not text or len(text) > 40:
                continue
            header = parse_range_header(text)
            if header is not None:
                current_range = header
                offset = 0
        return sorted(items.values(), key=lambda item: item.episode_no)

    @staticmethod
    def parse_page_urls(html: str, current_url: str) -> list[str]:
        """解析目录页分页控件，返回本页可跳转的其它页地址。

        真实站点的分页是 ``div.paginate`` 里的一串页码链接（``a.pg_page``），
        没有 "next" 文本，只有 ``pg_next`` 一条链；把整窗页码都取出来才能并发抓取。
        """
        soup = BeautifulSoup(html, "lxml")
        urls: list[str] = []
        seen: set[str] = set()
        for scope in soup.select("div.paginate, ul.paginate, nav.paginate"):
            for anchor in scope.find_all("a"):
                href = (anchor.get("href") or "").strip()
                if not href or href.startswith("#") or href.lower().startswith("javascript"):
                    continue
                absolute = urljoin(current_url, href)
                if absolute == current_url or absolute in seen:
                    continue
                seen.add(absolute)
                urls.append(absolute)
        return urls

    @staticmethod
    def parse_next_page_url(html: str, current_url: str) -> str | None:
        """解析「下一页」链接；没有则返回 ``None``。"""
        soup = BeautifulSoup(html, "lxml")
        for anchor in soup.find_all("a"):
            href = (anchor.get("href") or "").strip()
            if not href or href.startswith("#") or href.lower().startswith("javascript"):
                continue
            rel = [value.lower() for value in (anchor.get("rel") or [])]
            classes = " ".join(anchor.get("class") or []).lower()
            text = anchor.get_text(" ", strip=True).lower()
            if "next" in rel or "next" in classes or text in {"next", ">", "»", "next »", "다음"}:
                return urljoin(current_url, href)
        return None


def _contains_episode_link(tag: Tag) -> bool:
    for anchor in tag.find_all("a"):
        if _is_episode_link((anchor.get("href") or "").strip()):
            return True
    return False


# --------------------------------------------------------------------------
# 注册表读写
# --------------------------------------------------------------------------
class CatalogRegistry:
    """``workspace/index.json`` 与 ``workspace/{series_id}/catalog.json`` 的读写。"""

    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).expanduser()

    @property
    def index_path(self) -> Path:
        return self.workspace_root / "index.json"

    def series_dir(self, series_id: str) -> Path:
        return self.workspace_root / series_id

    def catalog_path(self, series_id: str) -> Path:
        return self.series_dir(series_id) / "catalog.json"

    # -- 全局索引 --------------------------------------------------------
    def load_index(self) -> list[SeriesIndexItem]:
        path = self.index_path
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CatalogError(f"index.json 解析失败：{exc}") from exc
        if not isinstance(raw, list):
            raise CatalogError("index.json 顶层必须是 JSON 数组")
        return [SeriesIndexItem.model_validate(item) for item in raw]

    def save_index(self, items: Sequence[SeriesIndexItem]) -> Path:
        path = self.index_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [item.model_dump() for item in items]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def upsert_series(self, item: SeriesIndexItem) -> list[SeriesIndexItem]:
        """按 ``series_id`` 覆盖或追加全局索引条目。"""
        items = self.load_index()
        for index, existing in enumerate(items):
            if existing.series_id == item.series_id:
                items[index] = item
                break
        else:
            items.append(item)
        self.save_index(items)
        return items

    def get_series(self, series_id: str) -> SeriesIndexItem | None:
        for item in self.load_index():
            if item.series_id == series_id:
                return item
        return None

    def remove_series(self, series_id: str) -> bool:
        """从全局索引移除作品（不删磁盘目录）。"""
        items = self.load_index()
        remaining = [item for item in items if item.series_id != series_id]
        if len(remaining) == len(items):
            return False
        self.save_index(remaining)
        return True

    def update_progress(self, series_id: str, done_episodes: int) -> SeriesIndexItem | None:
        """更新已完成话数。"""
        item = self.get_series(series_id)
        if item is None:
            return None
        item.done_episodes = done_episodes
        self.upsert_series(item)
        return item

    # -- 作品目录 --------------------------------------------------------
    def load_catalog(self, series_id: str) -> SeriesCatalog | None:
        path = self.catalog_path(series_id)
        if not path.is_file():
            return None
        return SeriesCatalog.model_validate_json(path.read_text(encoding="utf-8"))

    def save_catalog(self, catalog: SeriesCatalog) -> Path:
        path = self.catalog_path(catalog.series_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(catalog.model_dump_json(indent=2), encoding="utf-8")
        return path

    def register(self, catalog: SeriesCatalog, *, done_episodes: int = 0) -> SeriesIndexItem:
        """同时写入作品目录与全局索引。"""
        self.save_catalog(catalog)
        item = SeriesIndexItem(
            series_id=catalog.series_id,
            series_title=catalog.series_title,
            list_url=catalog.list_url,
            total_episodes=len(catalog.episodes),
            done_episodes=done_episodes,
        )
        self.upsert_series(item)
        return item


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------
async def _fetch_catalog_page(
    client: httpx.AsyncClient,
    url: str,
    *,
    retries: int = 2,
    backoff_s: float = 1.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str | None:
    """抓取一个目录页；返回 HTML，404 视为越界返回 ``None``。

    ``sleep`` 可注入（测试传空实现即可跳过重试退避），默认仍是 ``asyncio.sleep``。
    """
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt < retries:
                await sleep(backoff_s * (2**attempt))
                continue
            raise CatalogError(
                f"目录页抓取失败：{url}（{type(exc).__name__}: {exc or '无详细信息'}）"
            ) from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 500:
            last_error = httpx.HTTPStatusError(
                f"HTTP {response.status_code}", request=response.request, response=response
            )
            if attempt < retries:
                await sleep(backoff_s * (2**attempt))
                continue
            raise CatalogError(
                f"目录页抓取失败：{url}（HTTP {response.status_code}）"
            ) from last_error
        if response.status_code >= 400:
            raise CatalogError(f"目录页抓取失败：{url}（HTTP {response.status_code}）")
        return response.text
    raise CatalogError(f"目录页抓取失败：{url}（{type(last_error).__name__ if last_error else '未知'}）")


async def fetch_series_catalog(
    client: httpx.AsyncClient,
    list_url: str,
    *,
    series_id: str | None = None,
    series_title: str | None = None,
    max_pages: int = MAX_PAGES,
    concurrency: int = 8,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> SeriesCatalog:
    """翻页抓取目录页并合并成 ``SeriesCatalog``。

    抓到的条目按发布顺序（站点内部 ``episode_no`` 升序）重编号为「作品内第 N 话」
    （1 起），站点内部 ID 仍保留在 ``viewer_url`` 里可回溯。

    ``sleep`` 可注入（测试传空实现即可跳过重试退避），默认仍是 ``asyncio.sleep``。
    """
    resolved_id = series_id or series_id_from_url(list_url)
    resolved_title = series_title or series_title_from_url(list_url)
    items: dict[int, EpisodeCatalogItem] = {}
    visited: set[str] = set()
    pending: list[str] = [list_url]
    pages = 0
    title_locked = False

    while pending and pages < max_pages:
        batch = [url for url in pending if url not in visited][:concurrency]
        if not batch:
            break
        visited.update(batch)
        pages += len(batch)
        htmls = await asyncio.gather(
            *(_fetch_catalog_page(client, url, sleep=sleep) for url in batch)
        )
        for url, html in zip(batch, htmls):
            if html is None:
                continue
            if not title_locked:
                parsed_title = CatalogParser.parse_series_title(html)
                if parsed_title:
                    resolved_title = parsed_title
                    title_locked = True
            for item in CatalogParser.parse_episodes(html, list_url=url, series_id=resolved_id):
                items.setdefault(item.episode_no, item)
            for nxt in CatalogParser.parse_page_urls(html, url):
                if nxt not in visited and nxt not in pending:
                    pending.append(nxt)

    if not items:
        raise CatalogError(f"目录页未解析到任何话次：{list_url}")

    ordered = sorted(items.values(), key=lambda item: item.episode_no)
    for index, item in enumerate(ordered, start=1):
        item.episode_no = index
        item.episode_id = f"{resolved_id}_ep_{index:03d}"
    return SeriesCatalog(
        series_id=resolved_id,
        series_title=resolved_title,
        list_url=list_url,
        episodes=ordered,
    )


def iter_local_images(folder: Path) -> list[Path]:
    """列出目录下的图片文件（按文件名排序，保证切片顺序稳定）。"""
    return sorted(
        (path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES),
        key=lambda path: path.name,
    )


def _episode_no_from_folder(name: str, fallback: int) -> int:
    match = re.search(r"(\d+)", name)
    return int(match.group(1)) if match else fallback


def import_local_catalog(
    local_path: str | Path,
    *,
    series_id: str | None = None,
    series_title: str | None = None,
) -> SeriesCatalog:
    """把本地文件夹登记为一部漫画（``local://`` 导入）。

    目录结构两种：

    * 每个子目录是一话（``ep_003/``、``3/``），话号取目录名里的数字；
    * 目录里直接是图片 → 视为单话。
    """
    root = Path(local_path).expanduser().resolve()
    if not root.is_dir():
        raise CatalogError(f"本地目录不存在：{root}")
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", root.name).strip("_").lower() or "import"
    resolved_id = series_id or f"wt_local_{slug}"
    resolved_title = series_title or root.name

    subdirs = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name)
    episodes: list[EpisodeCatalogItem] = []
    if subdirs and any(iter_local_images(folder) for folder in subdirs):
        for index, folder in enumerate(subdirs, start=1):
            if not iter_local_images(folder):
                continue
            episode_no = _episode_no_from_folder(folder.name, index)
            episodes.append(
                EpisodeCatalogItem(
                    episode_no=episode_no,
                    episode_id=f"{resolved_id}_ep_{episode_no:03d}",
                    title=folder.name,
                    viewer_url=make_local_url(folder),
                    publish_date="",
                    is_locked=False,
                )
            )
    else:
        if not iter_local_images(root):
            raise CatalogError(f"本地目录没有可用图片：{root}")
        episodes.append(
            EpisodeCatalogItem(
                episode_no=1,
                episode_id=f"{resolved_id}_ep_001",
                title=resolved_title,
                viewer_url=make_local_url(root),
                publish_date="",
                is_locked=False,
            )
        )
    return SeriesCatalog(
        series_id=resolved_id,
        series_title=resolved_title,
        list_url=make_local_url(root),
        episodes=episodes,
    )
