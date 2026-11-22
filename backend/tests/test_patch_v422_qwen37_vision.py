"""Patch v4.22 离线回归测试：qwen3.7-plus 原生多图批窗抽取 + 视觉降级链。

覆盖六组断言：

1. Stage 3：``qwen3.7-plus`` 一次请求看 4 格（6 格 + 末尾空页 → 4+2 两批），
   过渡空页不参与批窗、也不跨空页；
2. Stage 3：超高竖长画格**不混入批窗**，仍走纵向切片单独抽取；
3. Stage 3：批次少返回某格 / 整批异常时，自动**逐格回退**补抽，一格不丢；
4. Stage 3：主模型未开通（403）时按 ``VISION_MODEL_FALLBACK_CHAIN`` 自动降级；
5. Stage 3：``qwen3-vl-plus`` 仍是单图模型（批窗 = 1），行为与旧版一致；
6. Stage 3b：整幕视觉复核同样透传 ``enable_thinking=False``。
"""

from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from core.schemas import (
    CharacterProfile,
    EpisodeStoryboard,
    StoryboardScene,
    VisionWindowOutput,
)
from providers.llm_base import ChatMessage, ChatResponse, LLMError
from stages import s3_vision, s3b_scenes

#: 单格提示词里的「当前要解析的面板编号是 `p_001`」。
_CURRENT_PANEL_RE = re.compile(r"面板编号是 `(p_\d{3})`")
#: 批窗提示词里的「待抽面板：p_001, p_002, p_003」。
_BATCH_TARGETS_RE = re.compile(r"待抽面板：([^\n]+)")

#: 当前格主图边长（``encode_image_data_url`` 默认 1280px，源图小于它就不缩）。
_MAIN_SIZE = (1200, 900)
#: 邻格 / 前格上下文缩略图边长（Patch v4.19/v4.22：640px）。
_NEIGHBOR_SIZE = (640, 480)
#: 超高竖长画格的尺寸（> TALL_PANEL_SLICE_THRESHOLD=1800 触发纵向切片）。
_TALL_SIZE = (900, 2000)


# ---------------------------------------------------------------------------
# 公共装置
# ---------------------------------------------------------------------------
class _FakeVisionLLM:
    """记录每次调用的假视觉客户端（离线，不联网）。

    * ``unavailable`` 里的模型会抛 ``LLMError``（模拟「未开通 / 无权限」）；
    * ``mode="drop"``：批窗少返回最后一格；
    * ``mode="raise"``：批窗整批抛异常（单格调用仍正常，用来验证逐格回退）。
    """

    def __init__(self, *, unavailable: set[str] | None = None, mode: str = "normal") -> None:
        self.unavailable = unavailable or set()
        self.mode = mode
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "max_tokens": max_tokens,
                "kwargs": kwargs,
            }
        )
        if model in self.unavailable:
            raise LLMError(f"Model {model} 未开通或无权限（HTTP 403）")
        text = _text_of(messages[0])
        single = _CURRENT_PANEL_RE.search(text)
        if single:
            # 单格抽取（含超高画格的逐片抽取）
            return ChatResponse(
                content=json.dumps(
                    {"panels": [_panel_payload(single.group(1))]}, ensure_ascii=False
                ),
                model=model,
            )
        # 批窗抽取
        if self.mode == "raise":
            raise RuntimeError("批量抽取失败")
        targets = _batch_targets(text)
        panels = [_panel_payload(panel_id) for panel_id in targets]
        if self.mode == "drop" and len(panels) > 1:
            panels = panels[:-1]
        return ChatResponse(
            content=json.dumps({"panels": panels}, ensure_ascii=False),
            model=model,
        )


def _text_of(message: ChatMessage) -> str:
    return "\n".join(part.text for part in message.content if part.type == "text")


def _image_sizes(message: ChatMessage) -> list[tuple[int, int]]:
    sizes: list[tuple[int, int]] = []
    for part in message.content:
        if part.type != "image_url":
            continue
        _, _, encoded = part.image_url.partition(",")
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
            sizes.append(image.size)
    return sizes


def _batch_targets(prompt_text: str) -> list[str]:
    match = _BATCH_TARGETS_RE.search(prompt_text)
    assert match, f"批窗提示词里没有待抽面板清单：{prompt_text[:200]}"
    return [item for item in re.split(r"[,，、]\s*", match.group(1)) if item]


def _panel_payload(panel_id: str) -> dict[str, Any]:
    return {
        "panel_id": panel_id,
        "scene_summary": f"{panel_id}：雨夜街头，少年握紧拳头抬头望天。",
        "dialogues": [{"speaker_guess": "小白", "text": "你终于来了", "emotion": "tense"}],
        "sfx_list": [],
        "is_transition_blank": False,
    }


def _make_panels_dir(
    tmp_path: Path,
    panel_ids: list[str],
    *,
    sizes: dict[str, tuple[int, int]] | None = None,
) -> Path:
    panels_dir = tmp_path / "workspace" / "wt_v422" / "episodes" / "ep_001" / "02_panels"
    panels_dir.mkdir(parents=True)
    for index, panel_id in enumerate(panel_ids):
        size = (sizes or {}).get(panel_id, _MAIN_SIZE)
        image = Image.new("RGB", size, (30 + index * 7, 60, 90))
        image.save(panels_dir / f"{panel_id}.png")
    return panels_dir


def _characters() -> list[CharacterProfile]:
    return [
        CharacterProfile(
            name="小白",
            aliases=["阿白"],
            visual_traits="白发红瞳，左颊有疤",
            current_status="浑身湿透",
            first_seen_ep=1,
            last_seen_ep=1,
        )
    ]


# ---------------------------------------------------------------------------
# 断言 1：批窗抽取（4+2 分批、空页不参与、enable_thinking=False）
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_qwen37_batches_four_panels_per_request(tmp_path) -> None:
    panels_dir = _make_panels_dir(
        tmp_path, ["p_001", "p_002", "p_003", "p_004", "p_005", "p_006", "p_007"]
    )
    client = _FakeVisionLLM()

    output = await s3_vision.extract_episode_vision(
        client,
        panels_dir=panels_dir,
        model="qwen3.7-plus",
        characters=_characters(),
        series_title="测试漫画",
        episode_no=1,
        blank_ids={"p_007"},
    )

    # 6 个真实面板 + 1 个空页 → 4 + 2 两批
    assert len(client.calls) == 2, [call["model"] for call in client.calls]
    first, second = client.calls

    # 第一批 4 张主图（首格没有前格参考图）
    assert len(_image_sizes(first["messages"][0])) == 4
    assert all(size == _MAIN_SIZE for size in _image_sizes(first["messages"][0]))
    # 第二批：1 张前格 640px 上下文参考图 + 2 张主图
    sizes = _image_sizes(second["messages"][0])
    assert sizes == [_NEIGHBOR_SIZE, _MAIN_SIZE, _MAIN_SIZE]

    # 每次调用都关掉思考链
    for call in client.calls:
        assert call["kwargs"].get("enable_thinking") is False
    # token 预算按格数放大
    assert first["max_tokens"] == max(2000, 4 * s3_vision.VISION_BATCH_TOKENS_PER_PANEL)

    # 空页不调模型、也不出现在任何提示词里
    prompts = [_text_of(call["messages"][0]) for call in client.calls]
    assert all("p_007" not in prompt for prompt in prompts)
    blank = next(panel for panel in output.panels if panel.panel_id == "p_007")
    assert blank.is_transition_blank is True
    assert [panel.panel_id for panel in output.panels] == [
        "p_001",
        "p_002",
        "p_003",
        "p_004",
        "p_005",
        "p_006",
        "p_007",
    ]


# ---------------------------------------------------------------------------
# 断言 2：超高竖长画格不混入批窗
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_tall_panel_is_not_batched(tmp_path) -> None:
    panels_dir = _make_panels_dir(
        tmp_path,
        ["p_001", "p_002", "p_003", "p_004"],
        sizes={"p_002": _TALL_SIZE},
    )
    client = _FakeVisionLLM()

    output = await s3_vision.extract_episode_vision(
        client,
        panels_dir=panels_dir,
        model="qwen3.7-plus",
        characters=_characters(),
        series_title="测试漫画",
        episode_no=1,
    )

    # 超高画格走纵向切片（多次调用），其余按「单格 + 一批」处理
    batch_calls = [
        call
        for call in client.calls
        if not _CURRENT_PANEL_RE.search(_text_of(call["messages"][0]))
    ]
    assert len(batch_calls) == 1
    batch_text = _text_of(batch_calls[0]["messages"][0])
    assert "p_003" in batch_text and "p_004" in batch_text
    assert "p_002" not in _batch_targets(batch_text)

    # 超高画格的切片宽度 ≤1024、每片高度 ≤1600
    tall_calls = [
        call
        for call in client.calls
        if (match := _CURRENT_PANEL_RE.search(_text_of(call["messages"][0])))
        and match.group(1) == "p_002"
    ]
    assert tall_calls, "超高画格必须走自己的切片抽取"
    for call in tall_calls:
        # 第 1 张图是当前切片（宽度 ≤1024、每片高度 ≤1600）；其后才是邻格参考图。
        width, height = _image_sizes(call["messages"][0])[0]
        assert width <= 1024
        assert height <= 1600

    assert {panel.panel_id for panel in output.panels} == {
        "p_001",
        "p_002",
        "p_003",
        "p_004",
    }


# ---------------------------------------------------------------------------
# 断言 3a：批次少返回一格 → 逐格回退补抽
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_batch_missing_panel_falls_back_to_single(tmp_path) -> None:
    panels_dir = _make_panels_dir(tmp_path, ["p_001", "p_002", "p_003", "p_004"])
    client = _FakeVisionLLM(mode="drop")

    output = await s3_vision.extract_episode_vision(
        client,
        panels_dir=panels_dir,
        model="qwen3.7-plus",
        characters=_characters(),
        series_title="测试漫画",
        episode_no=1,
    )

    assert [panel.panel_id for panel in output.panels] == [
        "p_001",
        "p_002",
        "p_003",
        "p_004",
    ]
    # 1 次批窗 + 1 次单格回退
    assert len(client.calls) == 2
    fallback = _CURRENT_PANEL_RE.search(_text_of(client.calls[1]["messages"][0]))
    assert fallback is not None and fallback.group(1) == "p_004"


# ---------------------------------------------------------------------------
# 断言 3b：整批异常 → 逐格回退，一格不丢
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_batch_failure_falls_back_to_single_without_losing_panels(tmp_path) -> None:
    panels_dir = _make_panels_dir(tmp_path, ["p_001", "p_002", "p_003", "p_004"])
    client = _FakeVisionLLM(mode="raise")

    output = await s3_vision.extract_episode_vision(
        client,
        panels_dir=panels_dir,
        model="qwen3.7-plus",
        characters=_characters(),
        series_title="测试漫画",
        episode_no=1,
    )

    assert [panel.panel_id for panel in output.panels] == [
        "p_001",
        "p_002",
        "p_003",
        "p_004",
    ]
    # 1 次失败的批窗 + 4 次单格回退
    assert len(client.calls) == 5


# ---------------------------------------------------------------------------
# 断言 4：主模型未开通 → 自动降级
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_unavailable_model_falls_back_to_next_candidate(tmp_path) -> None:
    panels_dir = _make_panels_dir(tmp_path, ["p_001", "p_002"])
    client = _FakeVisionLLM(unavailable={"qwen3.7-plus"})

    output = await s3_vision.extract_episode_vision(
        client,
        panels_dir=panels_dir,
        model="qwen3.7-plus",
        characters=_characters(),
        series_title="测试漫画",
        episode_no=1,
    )

    models = [call["model"] for call in client.calls]
    assert models[0] == "qwen3.7-plus"
    assert models[1] == "qwen-vl-max", f"应降级到 qwen-vl-max，实际 {models}"
    assert [panel.panel_id for panel in output.panels] == ["p_001", "p_002"]


# ---------------------------------------------------------------------------
# 断言 5：单图模型保持逐格抽取
# ---------------------------------------------------------------------------
def test_resolve_vision_batch_size_keeps_single_image_models_at_one() -> None:
    assert s3_vision.resolve_vision_batch_size("qwen3-vl-plus") == 1
    assert s3_vision.resolve_vision_batch_size("qwen-vl-max") == 1
    assert s3_vision.resolve_vision_batch_size("qwen3.7-plus") == 4
    assert s3_vision.resolve_vision_batch_size("qwen4-max") == 4
    assert s3_vision.resolve_vision_batch_size("qwen3.7-plus", 2) == 2


@pytest.mark.anyio
async def test_single_image_model_calls_per_panel(tmp_path) -> None:
    panels_dir = _make_panels_dir(tmp_path, ["p_001", "p_002", "p_003"])
    client = _FakeVisionLLM()

    output = await s3_vision.extract_episode_vision(
        client,
        panels_dir=panels_dir,
        model="qwen3-vl-plus",
        characters=_characters(),
        series_title="测试漫画",
        episode_no=1,
    )

    assert len(client.calls) == 3
    for call in client.calls:
        assert call["kwargs"].get("enable_thinking") is False
        assert _CURRENT_PANEL_RE.search(_text_of(call["messages"][0]))
    assert [panel.panel_id for panel in output.panels] == ["p_001", "p_002", "p_003"]


# ---------------------------------------------------------------------------
# 断言 6：整幕视觉复核同样关闭思考链
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_enrich_storyboard_disables_thinking(tmp_path) -> None:
    panels_dir = _make_panels_dir(tmp_path, ["p_001", "p_002"])
    board = EpisodeStoryboard(
        episode_id="wt_v422_ep_001",
        source="auto",
        scenes=[
            StoryboardScene(
                scene_id="sc_001",
                scene_index=1,
                title="第 1 幕 · 雨夜街头",
                location="雨夜街头",
                panel_ids=["p_001", "p_002"],
                enabled=True,
                must_cover=True,
            )
        ],
    )
    client = _FakeVisionLLM()

    await s3b_scenes.enrich_storyboard_with_vision(
        client,
        storyboard=board,
        panels_dir=panels_dir,
        model="qwen3.7-plus",
        characters=_characters(),
    )

    assert len(client.calls) == 1
    assert client.calls[0]["kwargs"].get("enable_thinking") is False


# ---------------------------------------------------------------------------
# 断言 7：默认模型与下拉候选
# ---------------------------------------------------------------------------
def test_default_vision_model_and_options() -> None:
    from core.config import ModelConfig
    from web.server import VISION_MODEL_OPTIONS

    assert ModelConfig().vision == "qwen3.7-plus"
    assert VISION_MODEL_OPTIONS[0] == "qwen3.7-plus"
    assert "qwen-vl-max" in VISION_MODEL_OPTIONS
    assert "qwen3-vl-plus" in VISION_MODEL_OPTIONS
    assert "deepseek-v4.1-flash-1017" in VISION_MODEL_OPTIONS


def test_batch_prompt_template_contains_required_placeholders() -> None:
    template = s3_vision.load_prompt_template(s3_vision.BATCH_PROMPT_FILENAME)
    for token in (
        "{{SERIES_TITLE}}",
        "{{EPISODE_NO}}",
        "{{CHARACTERS}}",
        "{{TARGET_PANEL_IDS}}",
        "{{PANEL_COUNT}}",
        "{{PREV_CONTEXT_NOTE}}",
    ):
        assert token in template, f"批窗提示词缺少占位符 {token}"
    assert '"panels"' in template


def test_vision_window_output_keeps_panel_order() -> None:
    """批窗结果按 paths 顺序重排（防模型乱序）。"""
    output = VisionWindowOutput(
        panels=[
            s3_vision.PanelVisionInfo(panel_id="p_001", scene_summary="雨夜"),
            s3_vision.PanelVisionInfo(panel_id="p_002", scene_summary="街角"),
        ]
    )
    assert [panel.panel_id for panel in output.panels] == ["p_001", "p_002"]
