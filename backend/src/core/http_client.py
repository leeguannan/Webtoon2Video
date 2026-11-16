"""HTTP 访问策略（铁律一的唯一归属地）。

所有对 ``webtoons.com`` 的请求——目录页、观看页、图片直链——都必须携带
``Referer: https://www.webtoons.com/``，否则会被防盗链拦截。Stage 0 / Stage 1
统一从本模块取客户端与请求头，避免各处各写一份 Referer。

同时集中管理 ``local://`` 本地导入 URI 的编解码（Stage 0 登记本地目录、
Stage 1 按本地目录导入图片都走这一套）。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import httpx

#: WEBTOON 防盗链要求的 Referer（规范第 0 节铁律一）。
DEFAULT_REFERER = "https://www.webtoons.com/"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT_S = 30.0

#: 本地目录导入 URI 的 scheme。
LOCAL_SCHEME = "local"


def build_client(
    *,
    referer: str | None = DEFAULT_REFERER,
    timeout: float = DEFAULT_TIMEOUT_S,
    http2: bool = False,
    headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    """创建携带 ``Referer`` 的异步 HTTP 客户端。

    ``referer=None`` 时不注入 ``Referer`` 头（百炼等非 WEBTOON 接口使用）。
    ``http2=True`` 需要额外安装 ``h2`` 包，否则 httpx 会抛 ``ImportError``；
    默认关闭以免引入未声明的依赖。
    """
    merged: dict[str, str] = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
    }
    if referer:
        merged["Referer"] = referer
    if headers:
        merged.update(headers)
    return httpx.AsyncClient(
        headers=merged,
        timeout=timeout,
        follow_redirects=True,
        http2=http2,
    )


def is_local_url(url: str) -> bool:
    """判断是否为 ``local://`` 本地导入 URI。"""
    return urlparse(url).scheme == LOCAL_SCHEME


def make_local_url(path: str | Path) -> str:
    """把本地目录路径编码成 ``local://`` URI（绝对路径）。"""
    resolved = Path(path).expanduser().resolve()
    return f"{LOCAL_SCHEME}://{resolved.as_posix()}"


def local_path_from_url(url: str) -> Path:
    """把 ``local://`` URI 解码回本地绝对路径。"""
    parsed = urlparse(url)
    if parsed.scheme != LOCAL_SCHEME:
        raise ValueError(f"不是 local:// URI：{url!r}")
    raw = parsed.path or ""
    if parsed.netloc:
        raw = f"/{parsed.netloc}{raw}"
    return Path(raw)
