"""环境冒烟测试：确认后端虚拟环境、基础依赖与 src-layout 导入路径均可用。

对应开发规范第一步的「确认 pytest 可正常执行」验收项，后续 Stage 单元测试可在此文件之外新增。
"""

from __future__ import annotations

import importlib

import pytest

# 基础依赖 → 实际 import 名（与 pyproject.toml dependencies 一一对应）。
DEPENDENCY_MODULES: dict[str, str] = {
    "httpx": "httpx",
    "beautifulsoup4": "bs4",
    "lxml": "lxml",
    "pydantic": "pydantic",
    "pyyaml": "yaml",
    "opencv-python-headless": "cv2",
    "numpy": "numpy",
    "pillow": "PIL",
    "openai": "openai",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "sse-starlette": "sse_starlette",
    "respx": "respx",
}

# 规范要求的后端包骨架（src-layout，导入根为 backend/src）。
BACKEND_PACKAGES: tuple[str, ...] = ("core", "stages", "providers", "web")


@pytest.mark.parametrize("package_name", sorted(DEPENDENCY_MODULES))
def test_dependency_importable(package_name: str) -> None:
    module = importlib.import_module(DEPENDENCY_MODULES[package_name])
    assert module is not None


@pytest.mark.parametrize("package_name", BACKEND_PACKAGES)
def test_backend_package_importable(package_name: str) -> None:
    module = importlib.import_module(package_name)
    assert module is not None


def test_pytest_runtime_available() -> None:
    assert pytest.__version__
