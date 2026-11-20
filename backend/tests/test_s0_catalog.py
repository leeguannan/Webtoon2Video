"""Stage 0 单元测试（离线 Mock）：目录解析、翻页抓取、注册表读写、区间解析。"""

from __future__ import annotations

import httpx
import pytest

from core.http_client import build_client, local_path_from_url, make_local_url
from core.schemas import SeriesIndexItem as SeriesItem
from stages.s0_catalog import (
    CatalogError,
    CatalogParser,
    CatalogRegistry,
    EpisodeRange,
    fetch_series_catalog,
    import_local_catalog,
    parse_episode_range,
    parse_episode_selection,
    parse_range_header,
    series_id_from_url,
    series_title_from_url,
)

LIST_URL = "https://www.webtoons.com/en/fantasy/test-comic/list?title_no=95"
PAGE2_URL = "https://www.webtoons.com/en/fantasy/test-comic/list?title_no=95&page=2"

PAGE1_HTML = """
<!doctype html>
<html>
<head>
  <title>Test Comic | WEBTOON</title>
  <meta property="og:title" content="Test Comic">
</head>
<body>
  <h1 class="subj">Test Comic</h1>
  <ul class="episode_list">
    <li class="episode_item">
      <a href="/en/fantasy/test-comic/ep-1/viewer?title_no=95&episode_no=1" title="Episode 1">
        <img class="thum" src="https://cdn.example.com/thumb1.jpg" alt="">
      </a>
      <p class="subj">Episode 1 - The Beginning</p>
      <div class="date">Jun 1, 2024</div>
    </li>
    <li class="episode_item locked">
      <a href="/en/fantasy/test-comic/ep-2/viewer?title_no=95&episode_no=2">
        <span class="ico_lock"></span>
      </a>
      <p class="subj">Episode 2 - Locked</p>
      <div class="date">Jun 8, 2024</div>
    </li>
  </ul>
  <div class="paginate">
    <a class="next" href="/en/fantasy/test-comic/list?title_no=95&amp;page=2">Next</a>
  </div>
</body>
</html>
"""

PAGE2_HTML = """
<!doctype html>
<html>
<head><title>Test Comic | WEBTOON</title></head>
<body>
  <h1 class="subj">Test Comic</h1>
  <ul class="episode_list">
    <li class="episode_item">
      <a href="/en/fantasy/test-comic/ep-3/viewer?title_no=95&episode_no=3">Episode 3</a>
      <p class="subj">Episode 3 - Third</p>
      <div class="date">Jun 15, 2024</div>
    </li>
  </ul>
</body>
</html>
"""

#: 区间标题 + 条目自身不带话号的目录页。
RANGE_HTML = """
<!doctype html>
<html><body>
  <div class="episode_range">Episodes 11-20</div>
  <ul class="episode_list">
    <li class="episode_item"><a href="/en/fantasy/test-comic/viewer?p=1">A</a></li>
    <li class="episode_item"><a href="/en/fantasy/test-comic/viewer?p=2">B</a></li>
  </ul>
</body></html>
"""


# --------------------------------------------------------------------------
# 区间解析
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1-50", EpisodeRange(1, 50)),
        ("Episodes 11-20", EpisodeRange(11, 20)),
        ("1 ~ 5", EpisodeRange(1, 5)),
        ("第3-7话", EpisodeRange(3, 7)),
        ("50-1", EpisodeRange(1, 50)),
    ],
)
def test_parse_episode_range(text: str, expected: EpisodeRange) -> None:
    assert parse_episode_range(text) == expected


@pytest.mark.parametrize("text", ["", "Jun 1, 2024", "2024-06-01", "no numbers here"])
def test_parse_episode_range_rejects_non_ranges(text: str) -> None:
    assert parse_episode_range(text) is None


def test_parse_range_header_is_strict() -> None:
    assert parse_range_header("Episodes 11-20") == EpisodeRange(11, 20)
    assert parse_range_header("1-50话") == EpisodeRange(1, 50)
    # 标题里带额外文字时不能当区间用，否则会把「Episode 1-2: 标题」误判成区间
    assert parse_range_header("Episode 1-2: The Meeting") is None


def test_parse_episode_selection() -> None:
    assert parse_episode_selection("1-3,5,10-12") == [1, 2, 3, 5, 10, 11, 12]
    assert parse_episode_selection("2") == [2]
    assert parse_episode_selection("") == []


def test_parse_episode_selection_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_episode_selection("abc")


# --------------------------------------------------------------------------
# URL 派生
# --------------------------------------------------------------------------
def test_series_id_from_url_uses_title_no() -> None:
    assert series_id_from_url(LIST_URL) == "wt_95"


def test_series_id_from_url_falls_back_to_slug() -> None:
    assert series_id_from_url("https://www.webtoons.com/en/fantasy/tower-of-god/list") == (
        "wt_tower_of_god"
    )


def test_series_title_from_url() -> None:
    assert series_title_from_url("https://www.webtoons.com/en/fantasy/tower-of-god/list") == (
        "Tower Of God"
    )


# --------------------------------------------------------------------------
# 目录页解析
# --------------------------------------------------------------------------
def test_parse_series_title() -> None:
    assert CatalogParser.parse_series_title(PAGE1_HTML) == "Test Comic"


def test_parse_episodes_extracts_fields() -> None:
    items = CatalogParser.parse_episodes(PAGE1_HTML, list_url=LIST_URL, series_id="wt_95")
    assert [item.episode_no for item in items] == [1, 2]
    first = items[0]
    assert first.episode_id == "wt_95_ep_001"
    assert first.title == "Episode 1 - The Beginning"
    assert first.publish_date == "Jun 1, 2024"
    assert first.viewer_url == (
        "https://www.webtoons.com/en/fantasy/test-comic/ep-1/viewer?title_no=95&episode_no=1"
    )
    assert first.is_locked is False


def test_parse_episodes_marks_locked_items() -> None:
    items = CatalogParser.parse_episodes(PAGE1_HTML, list_url=LIST_URL, series_id="wt_95")
    assert items[1].is_locked is True


def test_parse_episodes_uses_range_header_for_missing_numbers() -> None:
    items = CatalogParser.parse_episodes(RANGE_HTML, list_url=LIST_URL, series_id="wt_95")
    assert [item.episode_no for item in items] == [11, 12]
    assert items[0].episode_id == "wt_95_ep_011"


def test_parse_next_page_url() -> None:
    assert CatalogParser.parse_next_page_url(PAGE1_HTML, LIST_URL) == PAGE2_URL
    assert CatalogParser.parse_next_page_url(PAGE2_HTML, PAGE2_URL) is None


# --------------------------------------------------------------------------
# 翻页抓取
# --------------------------------------------------------------------------
@pytest.mark.anyio
async def test_fetch_series_catalog_follows_pagination(respx_mock) -> None:
    respx_mock.get(LIST_URL).mock(return_value=httpx.Response(200, text=PAGE1_HTML))
    respx_mock.get(PAGE2_URL).mock(return_value=httpx.Response(200, text=PAGE2_HTML))

    async with build_client() as client:
        catalog = await fetch_series_catalog(client, LIST_URL)

    assert catalog.series_id == "wt_95"
    assert catalog.series_title == "Test Comic"
    assert [item.episode_no for item in catalog.episodes] == [1, 2, 3]
    assert catalog.episodes[-1].title == "Episode 3 - Third"


@pytest.mark.anyio
async def test_fetch_series_catalog_sends_referer(respx_mock) -> None:
    respx_mock.get(LIST_URL).mock(return_value=httpx.Response(200, text=PAGE1_HTML))
    respx_mock.get(PAGE2_URL).mock(return_value=httpx.Response(200, text=PAGE2_HTML))

    async with build_client() as client:
        await fetch_series_catalog(client, LIST_URL)

    assert respx_mock.calls
    for call in respx_mock.calls:
        assert call.request.headers["referer"] == "https://www.webtoons.com/"


@pytest.mark.anyio
async def test_fetch_series_catalog_raises_on_http_error(respx_mock) -> None:
    respx_mock.get(LIST_URL).mock(return_value=httpx.Response(500, text="boom"))

    async def _no_sleep(_seconds: float) -> None:
        """跳过重试退避（断言不变，只是不真等）。"""
        return None

    async with build_client() as client:
        with pytest.raises(CatalogError):
            await fetch_series_catalog(client, LIST_URL, sleep=_no_sleep)


@pytest.mark.anyio
async def test_fetch_series_catalog_raises_when_no_episodes(respx_mock) -> None:
    respx_mock.get(LIST_URL).mock(return_value=httpx.Response(200, text="<html></html>"))

    async with build_client() as client:
        with pytest.raises(CatalogError):
            await fetch_series_catalog(client, LIST_URL)


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------
def test_registry_round_trip(tmp_path) -> None:
    registry = CatalogRegistry(tmp_path)
    assert registry.load_index() == []
    assert registry.load_catalog("wt_95") is None

    series = SeriesItem(
        series_id="wt_95",
        series_title="Test Comic",
        list_url=LIST_URL,
        total_episodes=3,
        done_episodes=0,
    )
    registry.upsert_series(series)
    assert registry.index_path.is_file()
    assert [item.series_id for item in registry.load_index()] == ["wt_95"]
    assert registry.get_series("wt_95") == series

    # 覆盖同 id 不产生重复条目
    series.done_episodes = 2
    registry.upsert_series(series)
    assert len(registry.load_index()) == 1

    assert registry.update_progress("wt_95", 3).done_episodes == 3
    assert registry.remove_series("wt_95") is True
    assert registry.remove_series("wt_95") is False
    assert registry.load_index() == []


def test_registry_register_writes_catalog_and_index(tmp_path) -> None:
    registry = CatalogRegistry(tmp_path)
    catalog = _sample_catalog()
    item = registry.register(catalog, done_episodes=1)

    assert item.total_episodes == 2
    assert item.done_episodes == 1
    assert registry.catalog_path("wt_95").is_file()
    loaded = registry.load_catalog("wt_95")
    assert loaded is not None
    assert loaded.series_title == "Test Comic"
    assert [episode.episode_no for episode in loaded.episodes] == [1, 2]


def test_registry_rejects_broken_index(tmp_path) -> None:
    registry = CatalogRegistry(tmp_path)
    registry.index_path.parent.mkdir(parents=True, exist_ok=True)
    registry.index_path.write_text("{\"not\": \"a list\"}", encoding="utf-8")
    with pytest.raises(CatalogError):
        registry.load_index()


# --------------------------------------------------------------------------
# 本地导入
# --------------------------------------------------------------------------
def test_import_local_catalog_single_episode(tmp_path) -> None:
    folder = tmp_path / "my_comic"
    folder.mkdir()
    (folder / "001.png").write_bytes(b"x")
    (folder / "002.png").write_bytes(b"y")

    catalog = import_local_catalog(folder)
    assert catalog.series_id == "wt_local_my_comic"
    assert len(catalog.episodes) == 1
    assert catalog.episodes[0].episode_no == 1
    assert catalog.episodes[0].viewer_url.startswith("local://")
    assert catalog.episodes[0].viewer_url.endswith("/my_comic")


def test_import_local_catalog_multi_episode(tmp_path) -> None:
    folder = tmp_path / "multi"
    (folder / "ep_003").mkdir(parents=True)
    (folder / "ep_003" / "001.png").write_bytes(b"x")
    (folder / "ep_007").mkdir()
    (folder / "ep_007" / "001.png").write_bytes(b"x")

    catalog = import_local_catalog(folder)
    assert [episode.episode_no for episode in catalog.episodes] == [3, 7]
    assert catalog.episodes[0].episode_id == "wt_local_multi_ep_003"


def test_import_local_catalog_rejects_empty_dir(tmp_path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    with pytest.raises(CatalogError):
        import_local_catalog(folder)


def test_import_local_catalog_rejects_missing_dir(tmp_path) -> None:
    with pytest.raises(CatalogError):
        import_local_catalog(tmp_path / "missing")


def test_make_local_url_round_trip(tmp_path) -> None:
    folder = tmp_path / "abc"
    folder.mkdir()
    url = make_local_url(folder)
    assert url.startswith("local://")
    assert local_path_from_url(url) == folder.resolve()


def _sample_catalog():
    from core.schemas import EpisodeCatalogItem, SeriesCatalog

    return SeriesCatalog(
        series_id="wt_95",
        series_title="Test Comic",
        list_url=LIST_URL,
        episodes=[
            EpisodeCatalogItem(
                episode_no=1,
                episode_id="wt_95_ep_001",
                title="Episode 1",
                viewer_url="https://www.webtoons.com/en/fantasy/test-comic/ep-1/viewer?title_no=95&episode_no=1",
                publish_date="Jun 1, 2024",
            ),
            EpisodeCatalogItem(
                episode_no=2,
                episode_id="wt_95_ep_002",
                title="Episode 2",
                viewer_url="https://www.webtoons.com/en/fantasy/test-comic/ep-2/viewer?title_no=95&episode_no=2",
                publish_date="Jun 8, 2024",
            ),
        ],
    )
