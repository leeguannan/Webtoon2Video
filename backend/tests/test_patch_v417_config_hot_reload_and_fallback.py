"""Patch v4.17：配置热重载同步到后台调度器 + Stage 4 降级链纳入 DeepSeek。

覆盖两件事：

1. ``PUT /api/config`` 之后，**不用重启后端**，后台任务立刻用上新配置的模型
   （此前只刷新 ``app.state.config``，``JobRunner`` 仍持有启动时的快照，
   于是出现「界面显示新模型、后台仍用旧模型」）。
2. ``MODEL_FALLBACK_CHAIN`` 把百炼 DeepSeek 放在链首：账号未开通该模型权限时，
   Stage 4 按链有序退回 qwen4-max → qwen3.8-max → qwen3.7-plus → qwen-plus。

全部离线运行：假 LLM 客户端 + 临时工作区，不触网、不碰真实工作区。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.config import AppConfig, WorkspaceConfig
from providers.llm_base import LLMRequestError
from stages import s0_catalog
from stages.s4_scriptwriter import (
    MODEL_FALLBACK_CHAIN,
    _fallback_candidates,
    run_with_model_fallback,
)
from web.job_runner import JobRunner
from web.server import create_app

DEEPSEEK_MODEL = "deepseek-v4.1-flash-1017"


def _register(root: Path) -> tuple[str, Path, AppConfig]:
    """登记一个作品与第 1 话，返回 (series_id, series_dir, config)。"""
    src = root / "src"
    ep_src = src / "ep_001"
    ep_src.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 600), (40, 80, 120)).save(ep_src / "001.png")
    catalog = s0_catalog.import_local_catalog(src)
    s0_catalog.CatalogRegistry(root).register(catalog)
    config = AppConfig(workspace=WorkspaceConfig(root=root))
    return catalog.series_id, root / catalog.series_id, config


def _make_client(root: Path, config: AppConfig) -> tuple[TestClient, JobRunner, Any]:
    """构造 TestClient + JobRunner（注入假客户端，避免触网）。"""
    runner = JobRunner(config, client_factory=lambda: object())
    app = create_app(config, runner=runner)
    return TestClient(app), runner, app


def test_put_config_hot_reloads_runner_and_view(tmp_path: Path) -> None:
    """保存配置后，后台调度器与工作区视图都要拿到新配置（不必重启后端）。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _series_id, _series_dir, config = _register(workspace)
    client, runner, app = _make_client(workspace, config)

    assert runner.config.models.script != DEEPSEEK_MODEL

    response = client.put(
        "/api/config",
        json={
            "scriptModel": DEEPSEEK_MODEL,
            "storyModel": DEEPSEEK_MODEL,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["scriptModel"] == DEEPSEEK_MODEL
    assert payload["storyModel"] == DEEPSEEK_MODEL

    # 界面回显之外，后台调度器与视图必须同步更新
    assert runner.config.models.script == DEEPSEEK_MODEL
    assert runner.config.models.story_model == DEEPSEEK_MODEL
    assert app.state.config.models.script == DEEPSEEK_MODEL
    assert app.state.view.config.models.script == DEEPSEEK_MODEL
    # 落盘也要正确，下次重启仍然生效
    assert (workspace / "config.yaml").is_file()


@pytest.mark.anyio
async def test_hot_reloaded_model_is_used_by_next_background_run(tmp_path: Path) -> None:
    """热重载之后提交的后台任务必须用新模型，而不是进程启动时的旧模型。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    series_id, _series_dir, config = _register(workspace)
    client, runner, _app = _make_client(workspace, config)

    captured: list[dict[str, Any]] = []

    async def fake_run_stage(state: Any, **kwargs: Any) -> None:
        captured.append(kwargs)

    runner._run_stage = fake_run_stage  # type: ignore[method-assign]
    runner.make_tts_client = lambda: object()  # type: ignore[method-assign]

    client.put(
        "/api/config",
        json={"scriptModel": DEEPSEEK_MODEL, "storyModel": DEEPSEEK_MODEL},
    )

    await runner._run_episode(series_id, 1, from_stage="s4_scriptwriter", mode="full_video")

    assert captured, "后台任务没有跑到任何阶段"
    assert captured[0]["script_model"] == DEEPSEEK_MODEL
    assert captured[0]["story_model"] == DEEPSEEK_MODEL


def test_fallback_chain_starts_with_deepseek() -> None:
    """降级链以 DeepSeek 起头，qwen 系的相对顺序保持不变。"""
    assert MODEL_FALLBACK_CHAIN[0] == DEEPSEEK_MODEL
    assert set(MODEL_FALLBACK_CHAIN) >= {
        "qwen4-max",
        "qwen3.8-max",
        "qwen3.7-plus",
        "qwen-plus",
    }
    # 主模型是 DeepSeek → 候选是 qwen 系（依次降级）
    assert _fallback_candidates(DEEPSEEK_MODEL) == [
        "qwen4-max",
        "qwen3.8-max",
        "qwen3.7-plus",
        "qwen-plus",
    ]
    # 主模型是 qwen 系 → 候选仍只取它之后的那些（既有语义不变）
    assert _fallback_candidates("qwen3.7-plus") == ["qwen-plus"]
    assert _fallback_candidates("qwen4-max") == [
        "qwen3.8-max",
        "qwen3.7-plus",
        "qwen-plus",
    ]


@pytest.mark.anyio
async def test_fallback_degrades_from_deepseek_to_qwen4_max() -> None:
    """DeepSeek 无权限时，Stage 4 自动回退到 qwen4-max。"""
    calls: list[str] = []

    async def call_async(model: str) -> str:
        calls.append(model)
        if model == DEEPSEEK_MODEL:
            raise LLMRequestError(
                "百炼返回 404：ModelNotFound：The model deepseek-v4.1-flash-1017 does not exist"
            )
        return f"ok:{model}"

    result = await run_with_model_fallback(
        DEEPSEEK_MODEL, call_async, label="Stage 4 LLM 调用"
    )

    assert result == "ok:qwen4-max"
    assert calls == [DEEPSEEK_MODEL, "qwen4-max"]
