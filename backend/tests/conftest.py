"""pytest 全局夹具。

anyio 的 pytest 插件默认会参数化 ``anyio_backend``；这里固定为 asyncio，
避免任何异步测试尝试运行 trio（未安装）。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
