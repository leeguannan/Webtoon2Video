"""Patch v4.21：语义切幕不再被前端预览绕过 + 思考开关与模型降级链补齐。

背景（用户实测）：

* 前端「分镜」页**首次打开**就会立刻落一份**规则切幕**的占位编排；Stage 4
  拿到这份文件后当成最终形态直接用，于是 LLM 语义切幕（通看整话摘要、按真实
  地点切换 / 时间跳跃 / 闪回 / 冲突转换切幕）**根本没被触发**——语义切幕等于
  被前端预览绕过了。
* 语义切幕的模型调用没有透传 ``enable_thinking=False``（百炼 qwen3 系默认开
  长链思考，结构化小任务开了容易撞穿请求超时），也没走 Stage 4 的模型降级链
  （主模型未开通就直接失败）。

本文件锁死：

* 前端预览落盘的占位编排标记 ``auto_strategy="rule"``，Stage 4 通过
  :func:`resolve_or_build_storyboard` 拿到后**自动升级一次**语义切幕并落盘
  ``"semantic"``，之后不再重复重切；
* 人工编排（``source="human"``）与已是 ``"semantic"`` 的编排：**绝不重切、
  绝不覆写**；
* 语义切幕的模型调用带 ``enable_thinking=False`` 且 ``max_tokens >= 3500``；
* 主模型报「未开通 / 404」时自动按 ``MODEL_FALLBACK_CHAIN`` 降级并成功；
* ``POST /storyboard/reset`` 落盘的是 ``"rule"`` 占位编排，返回前剔除纯文字卡。

全部离线：不触网、不依赖 ffmpeg。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.config import AppConfig, WorkspaceConfig
from core.schemas import (
    EpisodeStoryboard,
    StoryboardScene,
    VisionWindowOutput,
    load_model,
    save_model,
)
from providers.llm_base import ChatResponse, LLMError
from stages import s0_catalog, s3b_scenes, s4_scriptwriter
from web.job_runner import JobRunner
from web.server import create_app

SERIES_ID = "wt_v421"

#: 实测的粉色章节标题条（Patch v4.20 认定为纯文字卡，不能进任何一幕）。
_PINK_TITLE_STRIP = (
    "浅粉色背景上显示一行黑体中文文字，内容为'在醫院裡看到的道振…'，无角色、无动作、无气泡。"
)


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------
def _panel(
    panel_id: str,
    summary: str,
    *,
    dialogue: str | None = "一句台词。",
    speaker: str = "小白",
    blank: bool = False,
) -> dict[str, Any]:
    return {
        "panel_id": panel_id,
        "scene_summary": summary,
        "dialogues": (
            [{"speaker_guess": speaker, "text": dialogue, "emotion": "tense"}]
            if dialogue
            else []
        ),
        "sfx_list": [],
        "is_transition_blank": blank,
        "is_pure_text_card": False,
        "non_story_type": "none",
    }


def _vision() -> VisionWindowOutput:
    """8 格剧情 + 1 页过渡空页 + 1 条纯文字卡标题条。"""
    panels: list[dict[str, Any]] = []
    for index in range(1, 9):
        panels.append(_panel(f"p_{index:03d}", f"病房里，小白攥紧信纸，第 {index} 格。"))
    panels.append(_panel("p_009", "过渡空页。", dialogue=None, blank=True))
    panels.append(
        _panel("p_010", _PINK_TITLE_STRIP, dialogue="在醫院裡看到的道振…", speaker="旁白")
    )
    return VisionWindowOutput.model_validate({"panels": panels})


def _semantic_payload() -> str:
    """模型给出的三幕切分（覆盖 p_001~p_008，跳过过渡空页与纯字卡）。"""
    return json.dumps(
        {
            "scenes": [
                {
                    "title": "第 1 幕 · 病房",
                    "location": "病房",
                    "panel_ids": ["p_001", "p_002", "p_003"],
                },
                {
                    "title": "第 2 幕 · 走廊",
                    "location": "走廊",
                    "panel_ids": ["p_004", "p_005"],
                },
                {
                    "title": "第 3 幕 · 墓园",
                    "location": "墓园",
                    "panel_ids": ["p_006", "p_007", "p_008"],
                },
            ]
        },
        ensure_ascii=False,
    )


class _FakeLLM:
    """记录调用参数的假 LLM；指定模型抛「未开通」错误以触发降级链。"""

    def __init__(self, payload: str, *, unavailable: Sequence[str] = ()) -> None:
        self.payload = payload
        self.unavailable = set(unavailable)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: Sequence[Any],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "kwargs": kwargs,
            }
        )
        if model in self.unavailable:
            raise LLMError(f"Model {model} 未开通或无权限（HTTP 404）")
        return ChatResponse(content=self.payload, model=model)

    @property
    def models(self) -> list[str]:
        return [call["model"] for call in self.calls]


def _register(root: Path) -> tuple[str, Path, AppConfig]:
    src = root / "src"
    ep_src = src / "ep_001"
    ep_src.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 600), (40, 80, 120)).save(ep_src / "001.png")
    catalog = s0_catalog.import_local_catalog(src)
    s0_catalog.CatalogRegistry(root).register(catalog)
    config = AppConfig(workspace=WorkspaceConfig(root=root))
    return catalog.series_id, root / catalog.series_id, config


def _write_vision(series_dir: Path, vision: VisionWindowOutput) -> Path:
    path = series_dir / "episodes" / "ep_001" / "03_vision" / "vision.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_model(vision, path)
    return path


def _storyboard_path(series_dir: Path) -> Path:
    return series_dir / "episodes" / "ep_001" / "04_script" / "storyboard.json"


def _scene(index: int, panel_ids: Sequence[str]) -> StoryboardScene:
    return StoryboardScene(
        scene_id=f"sc_{index:03d}",
        scene_index=index,
        title=f"第 {index} 幕 · 测试",
        location="测试",
        panel_ids=list(panel_ids),
    )


def _rule_storyboard(episode_id: str, vision: VisionWindowOutput) -> EpisodeStoryboard:
    """前端预览落盘的规则切幕占位编排。"""
    board = s3b_scenes.auto_build_storyboard(episode_id, vision)
    return board.model_copy(update={"auto_strategy": "rule"})


# ---------------------------------------------------------------------------
# A. 前端预览落盘的占位编排必须带 "rule" 标记
# ---------------------------------------------------------------------------
def test_get_storyboard_marks_rule_placeholder(tmp_path: Path) -> None:
    """首次打开分镜页 → 落盘的编排标记为规则切幕占位。"""
    series_id, series_dir, config = _register(tmp_path)
    vision = _vision()
    _write_vision(series_dir, vision)

    runner = JobRunner(config, tts_factory=lambda: None)
    api = TestClient(create_app(config, runner=runner))
    response = api.get(f"/api/episodes/{series_id}_ep_001/storyboard")
    assert response.status_code == 200
    payload = response.json()
    assert payload["auto_strategy"] == "rule"
    assert payload["source"] == "auto"

    on_disk = load_model(EpisodeStoryboard, _storyboard_path(series_dir))
    assert on_disk.auto_strategy == "rule"
    assert on_disk.source == "auto"


def test_auto_build_storyboard_stays_strategy_empty() -> None:
    """规则切幕本身不标记策略（既有行为不变，占位标记由调用方补）。"""
    board = s3b_scenes.auto_build_storyboard("wt_v421_ep_001", _vision())
    assert board.auto_strategy == ""


# ---------------------------------------------------------------------------
# B. Stage 4 拿到 "rule" 占位 → 自动升级成语义切幕
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_resolve_or_build_upgrades_rule_placeholder_to_semantic(tmp_path: Path) -> None:
    """语义切幕不再被前端预览绕过：占位编排自动升级一次并落盘。"""
    series_id, series_dir, config = _register(tmp_path)
    vision = _vision()
    _write_vision(series_dir, vision)
    path = _storyboard_path(series_dir)
    save_model(_rule_storyboard(f"{series_id}_ep_001", vision), path)

    client = _FakeLLM(_semantic_payload())
    board = await s3b_scenes.resolve_or_build_storyboard(
        client,
        episode_id=f"{series_id}_ep_001",
        vision=vision,
        storyboard_path=path,
        model="qwen3-vl-plus",
    )

    # 只切一次。
    assert len(client.calls) == 1
    assert board.auto_strategy == "semantic"
    assert len(board.scenes) == 3
    flattened = [panel_id for scene in board.scenes for panel_id in scene.panel_ids]
    assert flattened == [f"p_{index:03d}" for index in range(1, 9)]

    # 落盘的编排也升级了，下次不再重切。
    on_disk = load_model(EpisodeStoryboard, path)
    assert on_disk.auto_strategy == "semantic"
    assert len(on_disk.scenes) == 3


@pytest.mark.anyio
async def test_semantic_storyboard_is_not_recut(tmp_path: Path) -> None:
    """已经是语义切幕的编排：绝不重复调用模型，也绝不覆写。"""
    series_id, series_dir, config = _register(tmp_path)
    vision = _vision()
    _write_vision(series_dir, vision)
    path = _storyboard_path(series_dir)
    board = EpisodeStoryboard(
        episode_id=f"{series_id}_ep_001",
        source="auto",
        scenes=[_scene(1, ["p_001", "p_002"])],
        auto_strategy="semantic",
    )
    save_model(board, path)
    before = path.read_bytes()

    client = _FakeLLM(_semantic_payload())
    resolved = await s3b_scenes.resolve_or_build_storyboard(
        client,
        episode_id=f"{series_id}_ep_001",
        vision=vision,
        storyboard_path=path,
        model="qwen3-vl-plus",
    )

    assert client.calls == []
    assert resolved.auto_strategy == "semantic"
    assert path.read_bytes() == before


@pytest.mark.anyio
async def test_human_storyboard_is_never_upgraded(tmp_path: Path) -> None:
    """人工编排保护：即使标记成 "rule"，也绝不重切、绝不覆写。"""
    series_id, series_dir, config = _register(tmp_path)
    vision = _vision()
    _write_vision(series_dir, vision)
    path = _storyboard_path(series_dir)
    board = EpisodeStoryboard(
        episode_id=f"{series_id}_ep_001",
        source="human",
        scenes=[_scene(1, ["p_001", "p_002"])],
        auto_strategy="rule",
    )
    save_model(board, path)
    before = path.read_bytes()

    client = _FakeLLM(_semantic_payload())
    resolved = await s3b_scenes.resolve_or_build_storyboard(
        client,
        episode_id=f"{series_id}_ep_001",
        vision=vision,
        storyboard_path=path,
        model="qwen3-vl-plus",
    )

    assert client.calls == []
    assert resolved.source == "human"
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# C. 模型调用参数：关思考 + 放宽 token + 降级链
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_semantic_build_disables_thinking_and_raises_token_budget() -> None:
    """语义切幕必须透传 enable_thinking=False，且 max_tokens 放宽到 3500+。"""
    client = _FakeLLM(_semantic_payload())
    board = await s3b_scenes.semantic_build_storyboard(
        client,
        episode_id="wt_v421_ep_001",
        vision=_vision(),
        model="qwen3-vl-plus",
    )

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["kwargs"].get("enable_thinking") is False
    assert (call["max_tokens"] or 0) >= 3500
    assert board.auto_strategy == "semantic"


@pytest.mark.anyio
async def test_semantic_build_falls_back_when_primary_model_not_enabled() -> None:
    """主模型未开通（404）→ 自动按 MODEL_FALLBACK_CHAIN 降级并切幕成功。"""
    primary = s4_scriptwriter.MODEL_FALLBACK_CHAIN[0]
    expected_next = s4_scriptwriter.MODEL_FALLBACK_CHAIN[1]
    client = _FakeLLM(_semantic_payload(), unavailable=[primary])

    board = await s3b_scenes.semantic_build_storyboard(
        client,
        episode_id="wt_v421_ep_001",
        vision=_vision(),
        model=primary,
    )

    assert client.models == [primary, expected_next]
    assert board.auto_strategy == "semantic"
    assert len(board.scenes) == 3


@pytest.mark.anyio
async def test_semantic_build_falls_back_to_rule_when_all_models_fail() -> None:
    """所有候选都不可用 → 无感回退规则切幕（流水线不中断）。"""
    client = _FakeLLM(
        _semantic_payload(), unavailable=list(s4_scriptwriter.MODEL_FALLBACK_CHAIN)
    )
    board = await s3b_scenes.semantic_build_storyboard(
        client,
        episode_id="wt_v421_ep_001",
        vision=_vision(),
        model=s4_scriptwriter.MODEL_FALLBACK_CHAIN[0],
    )

    # 回退产物是规则切幕：没有标记语义策略。
    assert board.auto_strategy == ""
    assert board.source == "auto"


# ---------------------------------------------------------------------------
# D. 重置：落盘 "rule" 占位 + 返回前剔除纯文字卡
# ---------------------------------------------------------------------------
def test_reset_storyboard_marks_rule_and_strips_text_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /storyboard/reset → 落盘 "rule" 占位编排，返回不含纯文字卡。"""
    series_id, series_dir, config = _register(tmp_path)
    vision = _vision()
    _write_vision(series_dir, vision)

    stripped: list[EpisodeStoryboard] = []
    original = s3b_scenes.strip_text_cards_from_storyboard

    def _spy(storyboard: EpisodeStoryboard, vision_arg: VisionWindowOutput) -> EpisodeStoryboard:
        stripped.append(storyboard)
        return original(storyboard, vision_arg)

    monkeypatch.setattr(s3b_scenes, "strip_text_cards_from_storyboard", _spy)

    runner = JobRunner(config, tts_factory=lambda: None)
    api = TestClient(create_app(config, runner=runner))
    response = api.post(f"/api/episodes/{series_id}_ep_001/storyboard/reset")
    assert response.status_code == 200

    # 重置返回前确实走了剔除纯文字卡这一步。
    assert stripped, "reset 未调用 strip_text_cards_from_storyboard"

    payload = response.json()
    assert payload["auto_strategy"] == "rule"
    flattened = [
        panel_id for scene in payload["scenes"] for panel_id in scene["panel_ids"]
    ]
    assert "p_010" not in flattened
    assert "p_009" not in flattened

    on_disk = load_model(EpisodeStoryboard, _storyboard_path(series_dir))
    assert on_disk.auto_strategy == "rule"
    assert on_disk.source == "auto"
