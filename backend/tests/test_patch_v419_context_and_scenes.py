"""Patch v4.19 离线回归测试：识图上下文滑窗 + 语义切幕 + 章幕 1:1 锁定。

覆盖四组断言：

1. Stage 3：连续多格时每格都带上**紧邻的前/后一格 640px 缩略图**，
   跨过渡空页**不串格**；单格调用行为与旧版完全一致；
2. Stage 3b：``semantic_build_storyboard`` 能解析模型给出的切幕结果并做
   确定性区间修补；模型输出异常时**无感回退** ``auto_build_storyboard``；
   ``resolve_or_build_storyboard`` 绝不覆写人工编排；
3. Stage 3b：``enrich_storyboard_with_vision`` 用 **896px** 长条图，
   并把 Layer 1 角色本名注入整幕复核提示词；
4. Stage 4：章节数与幕数一致时按序数 **1:1 锁定**，末章正文不再被错塞进前一章。
"""

from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from PIL import Image

from core.schemas import (
    CharacterProfile,
    EpisodeDigest,
    EpisodeStoryboard,
    PlotThread,
    StoryBibleV2,
    StoryboardScene,
    VisionWindowOutput,
    save_model,
)
from providers.llm_bailian import BailianClient
from providers.llm_base import ChatMessage, ChatResponse
from stages import s3_vision, s3b_scenes, s4_scriptwriter

CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

_PANEL_RE = re.compile(r"^- (p_\d{3})：", re.MULTILINE)
#: 提示词里「当前要解析的面板编号是 `p_001`」——用于把调用映射回本格。
_CURRENT_PANEL_RE = re.compile(r"面板编号是 `(p_\d{3})`")

#: 当前格主图的边长（``encode_image_data_url`` 默认 1280px）。
_MAIN_SIZE = (1200, 900)
#: 邻格缩略图的边长（Patch v4.19：640px）。
_NEIGHBOR_SIZE = (640, 480)


async def _no_sleep(_seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------
# 公共装置
# ---------------------------------------------------------------------------
class _FakeLLM:
    """记录每次调用的假客户端（离线，不联网）。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[list[ChatMessage]] = []

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        self.calls.append(messages)
        return ChatResponse(content=self.content, model=model)


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


def _make_panels_dir(tmp_path: Path, panel_ids: list[str]) -> Path:
    panels_dir = tmp_path / "workspace" / "wt_v419" / "episodes" / "ep_001" / "02_panels"
    panels_dir.mkdir(parents=True)
    for index, panel_id in enumerate(panel_ids):
        image = Image.new("RGB", _MAIN_SIZE, (30 + index * 10, 60, 90))
        image.save(panels_dir / f"{panel_id}.png")
    return panels_dir


def _panel_payload(panel_id: str) -> dict[str, Any]:
    return {
        "panel_id": panel_id,
        "scene_summary": f"{panel_id}：雨夜街头，少年握紧拳头抬头。",
        "dialogues": [{"speaker_guess": "小白", "text": "你终于来了", "emotion": "tense"}],
        "sfx_list": [],
        "is_transition_blank": False,
    }


def _vision_payload(panel_ids: list[str]) -> str:
    return json.dumps(
        {"panels": [_panel_payload(panel_id) for panel_id in panel_ids]},
        ensure_ascii=False,
    )


def _panel(
    panel_id: str,
    summary: str,
    *,
    blank: bool = False,
    dialogue: str | None = "一句台词。",
) -> dict[str, Any]:
    return {
        "panel_id": panel_id,
        "scene_summary": summary,
        "dialogues": (
            [{"speaker_guess": "小白", "text": dialogue, "emotion": "tense"}]
            if dialogue
            else []
        ),
        "sfx_list": [],
        "is_transition_blank": blank,
        "is_pure_text_card": False,
        "non_story_type": "none",
    }


# ---------------------------------------------------------------------------
# 断言 1：Stage 3 上下文邻格滑窗
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_s3_neighbor_window_attaches_640px_neighbors(tmp_path) -> None:
    """连续三格：中间格应同时带上前一格与后一格的 640px 缩略图。"""
    panel_ids = ["p_001", "p_002", "p_003"]
    panels_dir = _make_panels_dir(tmp_path, panel_ids)
    client = _FakeLLM(_vision_payload(panel_ids))

    await s3_vision.extract_episode_vision(
        client,
        panels_dir=panels_dir,
        model="qwen3-vl-plus",
        blank_ids=set(),
    )

    # 邻格不新增 LLM 调用次数：仍然是每格一次。
    assert len(client.calls) == 3

    by_panel: dict[str, ChatMessage] = {}
    for messages in client.calls:
        message = messages[0]
        prompt = _text_of(message)
        matched = _CURRENT_PANEL_RE.findall(prompt)
        assert matched, "提示词里必须带上当前分镜编号"
        by_panel[matched[0]] = message

    # 首格：只有后一格；末格：只有前一格；中间格：前后都有。
    assert len(_image_sizes(by_panel["p_001"])) == 2
    assert len(_image_sizes(by_panel["p_002"])) == 3
    assert len(_image_sizes(by_panel["p_003"])) == 2

    for panel_id, message in by_panel.items():
        sizes = _image_sizes(message)
        # 当前格永远是第 1 张图，且保持主图分辨率。
        assert sizes[0] == _MAIN_SIZE, panel_id
        # 邻格缩略图一律 640px。
        for size in sizes[1:]:
            assert size == _NEIGHBOR_SIZE, panel_id

    middle_prompt = _text_of(by_panel["p_002"])
    assert "前一格" in middle_prompt and "后一格" in middle_prompt
    assert s3_vision.NEIGHBOR_RULE_TEXT in middle_prompt


@pytest.mark.anyio
async def test_s3_blank_page_blocks_neighbor_reference(tmp_path) -> None:
    """过渡空页两侧不得互相引用（跨空页串格会把上一段剧情带进来）。"""
    panel_ids = ["p_001", "p_002", "p_003"]
    panels_dir = _make_panels_dir(tmp_path, panel_ids)
    client = _FakeLLM(_vision_payload(panel_ids))

    await s3_vision.extract_episode_vision(
        client,
        panels_dir=panels_dir,
        model="qwen3-vl-plus",
        blank_ids={"p_002"},
    )

    # 空页不调用模型：只有两格各一次。
    assert len(client.calls) == 2
    for messages in client.calls:
        message = messages[0]
        # 相邻是空页 → 没有邻格参考图，只有当前格主图。
        assert len(_image_sizes(message)) == 1
        prompt = _text_of(message)
        assert "没有可用的邻格参考图" in prompt
        assert s3_vision.NEIGHBOR_RULE_TEXT not in prompt


@pytest.mark.anyio
async def test_s3_single_panel_keeps_single_call_and_single_image(tmp_path) -> None:
    """单格调用与旧版完全一致：一次调用、一张图片、兜底文案。"""
    panels_dir = _make_panels_dir(tmp_path, ["p_001"])
    client = _FakeLLM(_vision_payload(["p_001"]))

    output = await s3_vision.extract_episode_vision(
        client,
        panels_dir=panels_dir,
        model="qwen3-vl-plus",
        blank_ids=set(),
    )

    assert len(client.calls) == 1
    assert len(_image_sizes(client.calls[0][0])) == 1
    assert "没有可用的邻格参考图" in _text_of(client.calls[0][0])
    assert [panel.panel_id for panel in output.panels] == ["p_001"]


def test_vision_prompt_template_has_neighbor_placeholder() -> None:
    """模板必须提供 {{NEIGHBOR_CONTEXT}} 占位符（否则会原样残留在提示词里）。"""
    template = s3_vision.load_prompt_template()
    assert "{{NEIGHBOR_CONTEXT}}" in template
    for token in ("{{CHARACTERS}}", "{{PANEL_ID}}", "{{SERIES_TITLE}}", "{{EPISODE_NO}}"):
        assert token in template
    assert "35~80 字" in template


# ---------------------------------------------------------------------------
# 断言 2：Stage 3b 语义切幕
# ---------------------------------------------------------------------------
def _semantic_vision() -> VisionWindowOutput:
    panels: list[dict[str, Any]] = []
    for index in range(1, 9):
        panels.append(_panel(f"p_{index:03d}", f"病房里，小白攥紧信纸，第 {index} 格。"))
    panels.append(_panel("p_009", "过渡空页。", blank=True, dialogue=None))
    return VisionWindowOutput.model_validate({"panels": panels})


@pytest.mark.anyio
async def test_semantic_build_storyboard_parses_and_repairs_ranges() -> None:
    """模型漏格时用确定性规则修补：连续、单调、无遗漏无重叠。"""
    vision = _semantic_vision()
    # 模型少给了 p_006（落在第 2 幕与第 3 幕之间的空隙）。
    payload = json.dumps(
        {
            "scenes": [
                {"title": "第 1 幕 · 病房", "location": "病房", "panel_ids": ["p_001", "p_002"]},
                {
                    "title": "第 2 幕 · 走廊",
                    "location": "走廊",
                    "panel_ids": ["p_003", "p_004", "p_005"],
                },
                {
                    "title": "第 3 幕 · 墓园",
                    "location": "墓园",
                    "panel_ids": ["p_007", "p_008"],
                },
            ]
        },
        ensure_ascii=False,
    )
    client = _FakeLLM(payload)

    board = await s3b_scenes.semantic_build_storyboard(
        client,
        episode_id="wt_v419_ep_001",
        vision=vision,
        model="qwen-plus",
    )

    assert board.source == "auto"
    assert len(board.scenes) == 3
    flattened = [panel_id for scene in board.scenes for panel_id in scene.panel_ids]
    # 有效格全部覆盖、不重不漏；过渡空页不进任何一幕。
    assert flattened == [f"p_{index:03d}" for index in range(1, 9)]
    assert "p_009" not in flattened
    for scene in board.scenes:
        assert scene.enabled is True
        assert scene.title.startswith(f"第 {scene.scene_index} 幕")

    # 提示词里必须带整话摘要与过渡空页分隔线。
    prompt = _text_of(client.calls[0][0])
    assert "p_001" in prompt
    assert "过渡空页" in prompt


@pytest.mark.anyio
async def test_semantic_build_storyboard_falls_back_to_auto_on_bad_output() -> None:
    """模型输出异常（不是 JSON）→ 无感回退规则切幕，流水线不中断。"""
    vision = _semantic_vision()
    expected = s3b_scenes.auto_build_storyboard("wt_v419_ep_001", vision)

    client = _FakeLLM("这不是 JSON，只是一段闲聊。")
    board = await s3b_scenes.semantic_build_storyboard(
        client,
        episode_id="wt_v419_ep_001",
        vision=vision,
        model="qwen-plus",
    )

    assert [scene.panel_ids for scene in board.scenes] == [
        scene.panel_ids for scene in expected.scenes
    ]
    assert board.source == "auto"


@pytest.mark.anyio
async def test_semantic_build_storyboard_falls_back_when_ranges_unusable() -> None:
    """模型给的全是未知分镜 → 无法修补 → 回退规则切幕。"""
    vision = _semantic_vision()
    expected = s3b_scenes.auto_build_storyboard("wt_v419_ep_001", vision)
    payload = json.dumps(
        {"scenes": [{"title": "第 1 幕", "location": "病房", "panel_ids": ["p_999"]}]},
        ensure_ascii=False,
    )
    client = _FakeLLM(payload)

    board = await s3b_scenes.semantic_build_storyboard(
        client,
        episode_id="wt_v419_ep_001",
        vision=vision,
        model="qwen-plus",
    )

    assert [scene.panel_ids for scene in board.scenes] == [
        scene.panel_ids for scene in expected.scenes
    ]


@pytest.mark.anyio
async def test_resolve_or_build_storyboard_never_overwrites_human_edits(tmp_path) -> None:
    """人工编排铁律：source == "human" 时连 reset=True 也不覆写、不调模型。"""
    script_dir = tmp_path / "workspace" / "wt_v419" / "episodes" / "ep_001" / "04_script"
    script_dir.mkdir(parents=True)
    board_path = script_dir / s3b_scenes.STORYBOARD_FILENAME
    human = EpisodeStoryboard(
        episode_id="wt_v419_ep_001",
        source="human",
        scenes=[
            StoryboardScene(
                scene_id="sc_001",
                scene_index=1,
                title="人工第一幕",
                location="病房",
                panel_ids=["p_001"],
                enabled=True,
                must_cover=True,
            )
        ],
    )
    s3b_scenes.save_storyboard(human, board_path)
    before = board_path.read_bytes()

    client = _FakeLLM(json.dumps({"scenes": []}, ensure_ascii=False))
    board = await s3b_scenes.resolve_or_build_storyboard(
        client,
        episode_id="wt_v419_ep_001",
        vision=_semantic_vision(),
        storyboard_path=board_path,
        model="qwen-plus",
        reset=True,
    )

    assert board.source == "human"
    assert board.scenes[0].title == "人工第一幕"
    assert client.calls == [], "人工编排不允许触发任何模型调用"
    assert board_path.read_bytes() == before


@pytest.mark.anyio
async def test_resolve_or_build_storyboard_keeps_existing_auto_board(tmp_path) -> None:
    """磁盘已有 auto 编排且非 reset → 直接复用，不重新切幕。"""
    script_dir = tmp_path / "workspace" / "wt_v419" / "episodes" / "ep_001" / "04_script"
    script_dir.mkdir(parents=True)
    board_path = script_dir / s3b_scenes.STORYBOARD_FILENAME
    existing = EpisodeStoryboard(
        episode_id="wt_v419_ep_001",
        source="auto",
        scenes=[
            StoryboardScene(
                scene_id="sc_001",
                scene_index=1,
                title="第 1 幕 · 病房",
                location="病房",
                panel_ids=["p_001", "p_002"],
                enabled=True,
                must_cover=True,
            )
        ],
    )
    s3b_scenes.save_storyboard(existing, board_path)

    client = _FakeLLM(json.dumps({"scenes": []}, ensure_ascii=False))
    board = await s3b_scenes.resolve_or_build_storyboard(
        client,
        episode_id="wt_v419_ep_001",
        vision=_semantic_vision(),
        storyboard_path=board_path,
        model="qwen-plus",
    )

    assert [scene.panel_ids for scene in board.scenes] == [["p_001", "p_002"]]
    assert client.calls == []


# ---------------------------------------------------------------------------
# 断言 3：整幕复核用 896px 长条图 + 角色本名
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_enrich_storyboard_uses_896_strip_and_character_names(tmp_path) -> None:
    panels_dir = _make_panels_dir(tmp_path, ["p_001", "p_002", "p_003"])
    board = EpisodeStoryboard(
        episode_id="wt_v419_ep_001",
        source="auto",
        scenes=[
            StoryboardScene(
                scene_id="sc_001",
                scene_index=1,
                title="第 1 幕 · 雨夜街头",
                location="雨夜街头",
                panel_ids=["p_001", "p_002", "p_003"],
                enabled=True,
                must_cover=True,
            )
        ],
    )
    characters = [
        CharacterProfile(
            name="小白",
            aliases=["阿白"],
            visual_traits="白发红瞳，左颊有疤",
            current_status="浑身湿透",
            first_seen_ep=1,
            last_seen_ep=1,
        )
    ]
    client = _FakeLLM("这一幕里，小白终于抓住了恩彗的手。\n【配乐情绪】suspense_lurk\n【配乐建议】钢琴 BPM 70")

    enriched = await s3b_scenes.enrich_storyboard_with_vision(
        client,
        storyboard=board,
        panels_dir=panels_dir,
        model="qwen3-vl-plus",
        vision=_semantic_vision(),
        characters=characters,
    )

    assert len(client.calls) == 1
    message = client.calls[0][0]
    sizes = _image_sizes(message)
    assert sizes, "整幕复核必须带长条图"
    # Patch v4.19：长条图宽度提到 896px（旧的 640px 看不清五官）。
    assert sizes[0][0] == 896
    assert sizes[0][1] <= 3840

    prompt = _text_of(message)
    assert "小白" in prompt
    assert "角色本名" in prompt
    assert enriched.scenes[0].visual_recap


@pytest.mark.anyio
async def test_enrich_storyboard_without_characters_keeps_base_prompt(tmp_path) -> None:
    """没有角色名单时提示词保持基础版（不注入空的角色块）。"""
    panels_dir = _make_panels_dir(tmp_path, ["p_001"])
    board = EpisodeStoryboard(
        episode_id="wt_v419_ep_001",
        source="auto",
        scenes=[
            StoryboardScene(
                scene_id="sc_001",
                scene_index=1,
                title="第 1 幕 · 雨夜街头",
                location="雨夜街头",
                panel_ids=["p_001"],
                enabled=True,
                must_cover=True,
            )
        ],
    )
    client = _FakeLLM("这一幕很安静。")

    await s3b_scenes.enrich_storyboard_with_vision(
        client,
        storyboard=board,
        panels_dir=panels_dir,
        model="qwen3-vl-plus",
        vision=_semantic_vision(),
    )

    prompt = _text_of(client.calls[0][0])
    assert "角色本名" not in prompt


# ---------------------------------------------------------------------------
# 断言 4：Stage 4 章幕 1:1 锁定（末章不再被错塞进前一章）
# ---------------------------------------------------------------------------
_PROSE_ONE = "章节一正文标记：雨夜的病房里，小白攥紧了那封信。"
_PROSE_TWO = "章节二正文标记：墓地里，恩彗终于把那句话说了出来。"


def _v419_series(tmp_path: Path) -> tuple[Path, Path]:
    series_dir = tmp_path / "workspace" / "wt_v419_s4"
    series_dir.mkdir(parents=True)
    bible = StoryBibleV2(
        series_id="wt_v419_s4",
        series_title="1:1 锁定测试",
        characters=[
            CharacterProfile(
                name="小白",
                aliases=["阿白"],
                visual_traits="白发红瞳",
                current_status="浑身湿透",
                first_seen_ep=1,
                last_seen_ep=1,
            )
        ],
        plot_threads=[
            PlotThread(
                thread_id="t_1",
                title="母亲的旧账本",
                description="账本最后一页被撕掉了",
                introduced_ep=1,
            )
        ],
        episodes_index=[
            EpisodeDigest(
                episode_no=1,
                one_line_hook="雨夜里的信",
                summary="小白在雨夜收到一封匿名信。",
                keywords=["雨夜"],
                characters_present=["小白"],
            )
        ],
    )
    save_model(bible, series_dir / "story_bible.json")
    snapshot_dir = series_dir / "bible_snapshots"
    snapshot_dir.mkdir(parents=True)
    save_model(bible, snapshot_dir / "ep_001.json")

    vision_dir = series_dir / "episodes" / "ep_002" / "03_vision"
    vision_dir.mkdir(parents=True)
    save_model(
        VisionWindowOutput.model_validate(
            {
                "panels": [
                    _panel("p_001", "病房里，小白攥紧信纸。"),
                    _panel("p_002", "病房里，走廊灯忽明忽暗。"),
                    _panel("p_003", "墓地里，恩彗站在墓碑前。"),
                    _panel("p_004", "墓碑上刻着旧名字。"),
                ]
            }
        ),
        vision_dir / "vision.json",
    )

    script_dir = series_dir / "episodes" / "ep_002" / "04_script"
    script_dir.mkdir(parents=True)
    board_path = script_dir / s3b_scenes.STORYBOARD_FILENAME
    s3b_scenes.save_storyboard(
        EpisodeStoryboard(
            episode_id="wt_v419_s4_ep_002",
            source="auto",
            scenes=[
                StoryboardScene(
                    scene_id="sc_001",
                    scene_index=1,
                    title="第 1 幕 · 病房",
                    location="病房",
                    panel_ids=["p_001", "p_002"],
                    enabled=True,
                    must_cover=True,
                ),
                StoryboardScene(
                    scene_id="sc_002",
                    scene_index=2,
                    title="第 2 幕 · 墓地",
                    location="墓地",
                    panel_ids=["p_003", "p_004"],
                    enabled=True,
                    must_cover=True,
                ),
            ],
        ),
        board_path,
    )
    return series_dir, board_path


def _http_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "qwen-plus",
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        },
    )


def _v419_story_payload() -> str:
    """4A 故事稿：第 2 章的 panel_hint_range 是错的（模型没对齐分镜号）。"""
    return json.dumps(
        {
            "episode_id": "wt_v419_s4_ep_002",
            "hook_opening": "雨夜有人敲门。",
            "full_story_markdown": "整话全文（不含章节标记）。",
            "chapters": [
                {
                    "chapter_index": 1,
                    "chapter_title": "第 1 幕",
                    "panel_hint_range": ["p_001", "p_002"],
                    "story_prose": _PROSE_ONE,
                },
                {
                    "chapter_index": 2,
                    "chapter_title": "第 2 幕",
                    "panel_hint_range": ["p_099", "p_099"],
                    "story_prose": _PROSE_TWO,
                },
            ],
        },
        ensure_ascii=False,
    )


def _v419_beats_payload(panel_ids: list[str]) -> str:
    return json.dumps(
        {
            "beats": [
                {
                    "beat_id": f"b{index}",
                    "narration_text": f"{panel_id} 的解说句要写够十个字以上才够长。",
                    "target_panel_ids": [panel_id],
                    "motion": "static",
                    "pause_after_ms": 200,
                }
                for index, panel_id in enumerate(panel_ids, start=1)
            ]
        },
        ensure_ascii=False,
    )


@pytest.mark.anyio
@respx.mock
async def test_stage4_locks_chapters_to_scenes_one_to_one(tmp_path) -> None:
    """章数 == 幕数时按序数 1:1 锁定：第 2 章的正文不会再被并进第 1 章。"""
    series_dir, _board_path = _v419_series(tmp_path)
    narrate_prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        prompt = body["messages"][-1]["content"]
        if "story_prose" in prompt:
            return _http_response(_v419_story_payload())
        if "target_panel_ids" in prompt:
            narrate_prompts.append(prompt)
            panel_ids = _PANEL_RE.findall(prompt)
            return _http_response(_v419_beats_payload(panel_ids))
        return _http_response(
            json.dumps(
                {
                    "hook_title": "雨夜来客",
                    "episode_digest": {
                        "one_line_hook": "一句话钩子",
                        "summary": "整话摘要",
                        "keywords": ["雨夜"],
                        "characters_present": ["小白"],
                    },
                    "character_updates": [],
                    "new_plot_threads": [],
                    "resolved_thread_ids": [],
                },
                ensure_ascii=False,
            )
        )

    respx.post(CHAT_URL).mock(side_effect=handler)
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        await s4_scriptwriter.run_stage4(
            series_dir,
            2,
            client,
            model="qwen-plus",
            episode_id="wt_v419_s4_ep_002",
            series_title="1:1 锁定测试",
            storyboard_path=_board_path,
        )

    assert len(narrate_prompts) == 2, "两幕应各写一次 4B"

    scene_one_prompts = [p for p in narrate_prompts if "p_001" in p]
    scene_two_prompts = [p for p in narrate_prompts if "p_003" in p]
    assert scene_one_prompts and scene_two_prompts

    # 第 1 章只讲第 1 幕的正文。
    assert _PROSE_ONE in scene_one_prompts[0]
    assert _PROSE_TWO not in scene_one_prompts[0]

    # 第 2 章只讲第 2 幕的正文（1:1 锁定后不再被并进第 1 章）。
    assert _PROSE_TWO in scene_two_prompts[0]
    assert _PROSE_ONE not in scene_two_prompts[0]


def test_pair_chapters_with_scenes_semantics_preserved() -> None:
    """区间重叠配对的旧语义保持不变（只有章幕数相等时才走 1:1 路径）。"""
    from core.schemas import StoryChapter

    chapters = [
        StoryChapter(chapter_index=1, chapter_title="一", panel_hint_range=("p_001", "p_002"), story_prose="a"),
        StoryChapter(chapter_index=2, chapter_title="二", panel_hint_range=("p_003", "p_004"), story_prose="b"),
    ]
    scenes = [
        StoryboardScene(
            scene_id="sc_001",
            scene_index=1,
            title="第 1 幕",
            location="病房",
            panel_ids=["p_001", "p_002"],
            enabled=True,
            must_cover=True,
        ),
        StoryboardScene(
            scene_id="sc_002",
            scene_index=2,
            title="第 2 幕",
            location="墓地",
            panel_ids=["p_003", "p_004"],
            enabled=True,
            must_cover=True,
        ),
    ]
    pairs = s4_scriptwriter._pair_chapters_with_scenes(
        chapters, scenes, ["p_001", "p_002", "p_003", "p_004"]
    )
    assert pairs == {0: 0, 1: 1}
