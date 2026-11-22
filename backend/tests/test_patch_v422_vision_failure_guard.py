"""Patch v4.22：视觉模型不可用时**立刻中止**，别让下游空转到「beats 为空」。

背景（用户实测，第五话制作失败）：

    [23:22:45] [失败] 模型没有产出任何可用的解说句（beats 为空）

真实因果链（不是 Stage 4 的锅）：

* Stage 3 的 88 个面板**全部**视觉抽取失败——配置里的视觉模型
  ``qwen-vl-max`` 这个模型 ID 在百炼已下线（HTTP 404 ``Model not exist``）；
* 但 Stage 3 当时是「失败也照常落盘」，于是磁盘上的 ``vision.json`` 只剩下
  7 张空白过渡页占位，**上一轮成功的 88 格视觉结果被这份空文件覆盖**；
* Stage 4 拿到空候选池，三轮重写都产不出解说句，最后报了一个完全误导人的
  「模型没有产出任何可用的解说句（beats 为空）」。

本文件锁死三条护栏：

* 视觉抽取**全军覆没**时立刻抛 :class:`VisionError`（带模型名 + 首个错误），
  并且**不覆盖**已有的 ``vision.json``；
* 视觉模型返回 HTTP 200 但**内容为空**（纯文本模型被误选成视觉模型）时，
  报「返回空内容」而不是让人看不懂的「不是合法 JSON」；
* 磁盘上残留的**空编排**（``scenes`` 为空）视为无效，重建而不是沿用。

全部离线：不触网、不依赖 ffmpeg。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pytest
from PIL import Image

from core.schemas import (
    EpisodeStoryboard,
    VisionWindowOutput,
    load_model,
    save_model,
)
from providers.llm_base import ChatResponse, LLMError
from stages import s3_vision, s3b_scenes

#: 实测在百炼已不可用的视觉模型 ID（404 Model not exist / 403 Access denied）。
_RETIRED_VISION_MODELS = frozenset(
    {
        "qwen-vl-max",
        "qwen-vl-max-latest",
        "qwen-vl-plus",
        "qwen3-vl-plus-latest",
        "qwen3-vl-flash",
        "qwen2.5-vl-72b-instruct",
    }
)


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------
class _BoomLLM:
    """模型 ID 下线：每次调用都抛 404 Model not exist。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: Sequence[Any], *, model: str, **kwargs: Any) -> ChatResponse:
        self.calls += 1
        raise LLMError(
            '百炼返回 404：{"error":{"message":"Model not exist.",'
            '"code":"model_not_found"}}'
        )


class _EmptyLLM:
    """HTTP 200 但内容为空（纯文本模型被误选成视觉模型）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: Sequence[Any], *, model: str, **kwargs: Any) -> ChatResponse:
        self.calls += 1
        return ChatResponse(content="", model=model)


class _OkLLM:
    """正常返回面板 JSON。"""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    async def chat(self, messages: Sequence[Any], *, model: str, **kwargs: Any) -> ChatResponse:
        self.calls += 1
        return ChatResponse(content=self.payload, model=model)


def _panel_payload(panel_id: str) -> str:
    return json.dumps(
        {
            "panel_id": panel_id,
            "scene_summary": "病房里，小白攥紧信纸，抬头看向门口。",
            "dialogues": [{"speaker_guess": "小白", "text": "别走。", "emotion": "tense"}],
            "sfx_list": [],
        },
        ensure_ascii=False,
    )


def _series(tmp_path: Path, panel_ids: Sequence[str] = ("p_001", "p_002")) -> Path:
    """造一个带 02_panels 图片的剧集目录。"""
    series_dir = tmp_path / "workspace" / "wt_v422"
    panels_dir = series_dir / "episodes" / "ep_001" / "02_panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    for panel_id in panel_ids:
        Image.new("RGB", (600, 900), (12, 34, 56)).save(panels_dir / f"{panel_id}.png")
    return series_dir


def _vision(panel_count: int = 6) -> VisionWindowOutput:
    """若干格普通剧情格。"""
    panels = []
    for index in range(1, panel_count + 1):
        panels.append(
            {
                "panel_id": f"p_{index:03d}",
                "scene_summary": f"病房里，小白攥紧信纸，第 {index} 格。",
                "dialogues": [
                    {"speaker_guess": "小白", "text": "别走。", "emotion": "tense"}
                ],
                "sfx_list": [],
                "is_transition_blank": False,
                "is_pure_text_card": False,
                "non_story_type": "none",
            }
        )
    return VisionWindowOutput.model_validate({"panels": panels})


# ---------------------------------------------------------------------------
# A. 视觉抽取全军覆没 → 立刻中止
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_stage3_aborts_when_every_panel_fails(tmp_path: Path) -> None:
    """模型 ID 下线时整话抽取全灭 → 立刻抛错，不落盘空 vision。"""
    series_dir = _series(tmp_path)
    client = _BoomLLM()

    with pytest.raises(s3_vision.VisionError) as excinfo:
        await s3_vision.run_stage3(
            series_dir,
            1,
            client,  # type: ignore[arg-type]
            model="qwen-vl-max",
            on_panel_error="skip",
        )

    message = str(excinfo.value)
    assert "qwen-vl-max" in message
    assert "全部失败" in message
    assert "Model not exist" in message


@pytest.mark.anyio
async def test_stage3_keeps_previous_vision_when_all_panels_fail(tmp_path: Path) -> None:
    """全军覆没时**不能覆盖**上一次成功的 vision.json（那是用户已经花的钱）。"""
    series_dir = _series(tmp_path)
    vision_path = series_dir / "episodes" / "ep_001" / "03_vision" / "vision.json"
    vision_path.parent.mkdir(parents=True, exist_ok=True)
    good = _vision(3)
    save_model(good, vision_path)
    before = vision_path.read_text(encoding="utf-8")

    with pytest.raises(s3_vision.VisionError):
        await s3_vision.run_stage3(
            series_dir,
            1,
            _BoomLLM(),  # type: ignore[arg-type]
            model="qwen-vl-max",
            on_panel_error="skip",
        )

    assert vision_path.is_file()
    assert vision_path.read_text(encoding="utf-8") == before
    assert len(load_model(VisionWindowOutput, vision_path).panels) == 3


@pytest.mark.anyio
async def test_stage3_reports_empty_model_content(tmp_path: Path) -> None:
    """HTTP 200 但内容为空（模型不支持图片输入）→ 报「返回空内容」。"""
    series_dir = _series(tmp_path)

    with pytest.raises(s3_vision.VisionError) as excinfo:
        await s3_vision.run_stage3(
            series_dir,
            1,
            _EmptyLLM(),  # type: ignore[arg-type]
            model="deepseek-v4.1-flash-1017",
            on_panel_error="skip",
        )

    message = str(excinfo.value)
    assert "返回空内容" in message
    assert "deepseek-v4.1-flash-1017" in message


@pytest.mark.anyio
async def test_stage3_partial_failure_still_writes(tmp_path: Path) -> None:
    """个别面板失败仍然照常落盘成功结果（既有行为不破）。"""

    class _HalfBroken:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages: Sequence[Any], *, model: str, **kwargs: Any) -> ChatResponse:
            self.calls += 1
            if self.calls == 1:
                raise LLMError("百炼返回 500：boom")
            return ChatResponse(content=_panel_payload("p_002"), model=model)

    series_dir = _series(tmp_path)
    path = await s3_vision.run_stage3(
        series_dir,
        1,
        _HalfBroken(),  # type: ignore[arg-type]
        model="qwen3-vl-plus",
        on_panel_error="skip",
    )
    output = load_model(VisionWindowOutput, path)
    assert [panel.panel_id for panel in output.panels] == ["p_002"]
    failures = json.loads((path.parent / "vision_failures.json").read_text(encoding="utf-8"))
    assert failures and failures[0]["panel_id"] == "p_001"


# ---------------------------------------------------------------------------
# B. 磁盘上的空编排视为无效，重建
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_empty_storyboard_on_disk_is_rebuilt(tmp_path: Path) -> None:
    """空编排（Stage 3 全军覆没留下的垃圾）→ 重建，不沿用。"""
    series_dir = _series(tmp_path)
    board_path = series_dir / "episodes" / "ep_001" / "04_script" / "storyboard.json"
    board_path.parent.mkdir(parents=True, exist_ok=True)
    save_model(
        EpisodeStoryboard(
            episode_id="wt_v422_ep_001",
            source="auto",
            scenes=[],
            auto_strategy="",
        ),
        board_path,
    )

    board = await s3b_scenes.resolve_or_build_storyboard(
        None,
        episode_id="wt_v422_ep_001",
        vision=_vision(),
        storyboard_path=board_path,
    )

    assert board.scenes, "空编排必须重建出可用分镜"
    assert board.scenes[0].panel_ids


def test_empty_storyboard_on_disk_is_rebuilt_sync(tmp_path: Path) -> None:
    """同步版同样把空编排视为无效。"""
    series_dir = _series(tmp_path)
    board_path = series_dir / "episodes" / "ep_001" / "04_script" / "storyboard.json"
    board_path.parent.mkdir(parents=True, exist_ok=True)
    save_model(
        EpisodeStoryboard(
            episode_id="wt_v422_ep_001",
            source="auto",
            scenes=[],
            auto_strategy="",
        ),
        board_path,
    )

    board = s3b_scenes.load_or_build_storyboard(
        "wt_v422_ep_001",
        _vision(),
        board_path,
    )

    assert board.scenes, "空编排必须重建出可用分镜"
    assert board.scenes[0].panel_ids


# ---------------------------------------------------------------------------
# C. 配置护栏：视觉模型不能配成已下线的 ID
# ---------------------------------------------------------------------------
def test_configured_vision_model_is_not_retired() -> None:
    """backend/config.yaml 里的视觉模型必须实测可用（qwen-vl-max 已下线）。"""
    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    text = config_path.read_text(encoding="utf-8")
    assert "vision:" in text
    model = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("vision:"):
            model = stripped.split(":", 1)[1].strip()
            break
    assert model, "config.yaml 必须配置 models.vision"
    assert model not in _RETIRED_VISION_MODELS, (
        f"config.yaml 的视觉模型 {model} 在百炼已不可用（404/403），会让 Stage 3 全军覆没"
    )
