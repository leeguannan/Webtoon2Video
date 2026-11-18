"""Patch v4.0 Patch 19：镜头组编排（Scene-First 撰稿）回归测试。

覆盖规格里的五条硬断言：

1. :func:`stages.s3b_scenes.auto_build_storyboard` 能把 30+ 格（含过渡空页与
   地点切换）的整话稳定切成有序、覆盖全部有效面板的连续镜头组，末尾高潮独立成幕；
2. :func:`stages.s3b_scenes.load_or_build_storyboard` 遇磁盘上 ``source="human"``
   的编排**绝不覆写**；
3. :func:`stages.s4_scriptwriter.compute_episode_beat_budget` 的上限明显大于
   旧的写死值 50；
4. :func:`stages.s4_scriptwriter.validate_storyboard_coverage` 能精准检出
   「尾幕一条解说都没有」与「漏掉必保原话」；
5. 端到端：首稿漏掉第 3 幕时，只对第 3 幕重调一次补写，最终覆盖校验全通过。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
import respx

from core.schemas import (
    CharacterProfile,
    EpisodeDigest,
    EpisodeStoryboard,
    PlotThread,
    StoryBibleV2,
    StoryboardScene,
    VisionWindowOutput,
    load_model,
    save_model,
)
from providers.llm_bailian import BailianClient
from stages import s3b_scenes, s4_scriptwriter

CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

_PANEL_RE = re.compile(r"^- (p_\d{3})：", re.MULTILINE)


async def _no_sleep(_seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------
# 公共装置
# ---------------------------------------------------------------------------
def _panel(
    panel_id: str,
    summary: str,
    *,
    blank: bool = False,
    non_story_type: str = "none",
    dialogue: str | None = "一句台词。",
) -> dict:
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
        "non_story_type": non_story_type,
    }


def _long_vision() -> VisionWindowOutput:
    """30+ 格的整话：病房 →（过渡页）→ 客厅 →（过渡页）→ 墓地 → 尾幕高潮。"""
    panels: list[dict] = []
    for index in range(1, 9):
        panels.append(_panel(f"p_{index:03d}", f"病房里，小白攥紧信纸，第 {index} 格。"))
    panels.append(_panel("p_009", "过渡空页。", blank=True, dialogue=None))
    for index in range(10, 19):
        panels.append(_panel(f"p_{index:03d}", f"客厅中两人对峙，第 {index} 格。"))
    panels.append(_panel("p_019", "过渡空页。", blank=True, dialogue=None))
    for index in range(20, 27):
        panels.append(_panel(f"p_{index:03d}", f"墓地里风很冷，第 {index} 格。"))
    panels.append(_panel("p_027", "过渡空页。", blank=True, dialogue=None))
    panels.append(_panel("p_028", "墓碑前恩彗终于开口。", dialogue="早知道就别倒油了。"))
    panels.append(_panel("p_029", "镜头怼在恩彗脸上。", dialogue="我记住了。"))
    panels.append(
        _panel(
            "p_030",
            "版权页。",
            non_story_type="copyright_notice",
            dialogue=None,
        )
    )
    return VisionWindowOutput.model_validate({"panels": panels})


def _storyboard_scenes() -> list[StoryboardScene]:
    return [
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
            title="第 2 幕 · 客厅",
            location="客厅",
            panel_ids=["p_003", "p_004"],
            enabled=True,
            must_cover=True,
        ),
        StoryboardScene(
            scene_id="sc_003",
            scene_index=3,
            title="第 3 幕 · 墓地",
            location="墓地",
            panel_ids=["p_005", "p_006"],
            enabled=True,
            must_cover=True,
            user_note="必须讲清恩彗在墓碑前的那句狠话。",
            locked_quotes=["早知道就别倒油了"],
        ),
    ]


def _make_series(tmp_path: Path, *, episode_no: int = 2) -> Path:
    series_dir = tmp_path / "workspace" / "wt_storyboard"
    series_dir.mkdir(parents=True)
    bible = StoryBibleV2(
        series_id="wt_storyboard",
        series_title="镜头组测试",
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

    vision_dir = series_dir / "episodes" / f"ep_{episode_no:03d}" / "03_vision"
    vision_dir.mkdir(parents=True)
    save_model(
        VisionWindowOutput.model_validate(
            {
                "panels": [
                    _panel("p_001", "病房里，小白攥紧信纸。"),
                    _panel("p_002", "病房里，走廊灯忽明忽暗。"),
                    _panel("p_003", "客厅里，两人隔着茶几对视。"),
                    _panel("p_004", "客厅里，恩彗攥紧了拳头。"),
                    _panel("p_005", "墓地里，恩彗站在墓碑前。"),
                    _panel("p_006", "墓碑上刻着旧名字。"),
                ]
            }
        ),
        vision_dir / "vision.json",
    )
    return series_dir


def _response(content: str) -> httpx.Response:
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


def _overview_payload() -> str:
    return json.dumps(
        {
            "hook_title": "雨夜来客",
            "episode_digest": {
                "one_line_hook": "一句话钩子",
                "summary": "整话通读后得到的剧情摘要",
                "keywords": ["雨夜"],
                "characters_present": ["小白"],
            },
            "character_updates": [],
            "new_plot_threads": [],
            "resolved_thread_ids": [],
        },
        ensure_ascii=False,
    )


def _story_payload() -> str:
    prose = "这一段讲的是雨夜里的相遇与试探，人物在沉默中一点点交出底牌。"
    return json.dumps(
        {
            "episode_id": "wt_storyboard_ep_002",
            "hook_opening": "雨夜有人敲门，来者不善。",
            "full_story_markdown": f"## 雨夜试探\n\n{prose}",
            "chapters": [
                {
                    "chapter_index": index,
                    "chapter_title": f"第 {index} 幕",
                    "panel_hint_range": [first, last],
                    "story_prose": prose,
                }
                for index, (first, last) in enumerate(
                    [("p_001", "p_002"), ("p_003", "p_004"), ("p_005", "p_006")],
                    start=1,
                )
            ],
        },
        ensure_ascii=False,
    )


def _beats_payload(panel_ids: list[str], *, quote: str = "") -> str:
    narration = f"{panel_ids[0]} 的解说句要写够十个字以上。"
    if quote:
        narration = f"{quote}，{narration}"
    return json.dumps(
        {
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": narration,
                    "target_panel_ids": [panel_id],
                    "motion": "static",
                    "pause_after_ms": 200,
                }
                for panel_id in panel_ids
            ]
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# 断言 1：自动聚类
# ---------------------------------------------------------------------------
def test_auto_build_storyboard_splits_into_ordered_scenes() -> None:
    vision = _long_vision()
    board = s3b_scenes.auto_build_storyboard("wt_storyboard_ep_002", vision)

    assert board.source == "auto"
    assert board.episode_id == "wt_storyboard_ep_002"
    assert board.scenes, "至少要切出一个镜头组"

    # 幕序连续、标题形如「第 i 幕」。
    assert [scene.scene_index for scene in board.scenes] == list(
        range(1, len(board.scenes) + 1)
    )
    for scene in board.scenes:
        assert scene.title.startswith(f"第 {scene.scene_index} 幕")
        assert scene.enabled is True
        assert scene.must_cover is True

    # 覆盖全部有效面板（过渡空页与版权页不进镜头组），顺序单调不减。
    flattened = [panel_id for scene in board.scenes for panel_id in scene.panel_ids]
    expected = [
        f"p_{index:03d}"
        for index in list(range(1, 9))
        + list(range(10, 19))
        + list(range(20, 27))
        + [28, 29]
    ]
    assert flattened == expected
    assert "p_009" not in flattened and "p_027" not in flattened
    assert "p_030" not in flattened

    # 末尾高潮（不足 3 格）独立成收尾幕，严禁丢弃。
    tail = board.scenes[-1]
    assert tail.panel_ids == ["p_028", "p_029"]

    # 幕数落在目标区间（5~10）。
    assert 5 <= len(board.scenes) <= 10


# ---------------------------------------------------------------------------
# 断言 2：人工编排保护
# ---------------------------------------------------------------------------
def test_load_or_build_storyboard_never_overwrites_human_edits(tmp_path) -> None:
    series_dir = tmp_path / "workspace" / "wt_human"
    script_dir = series_dir / "episodes" / "ep_002" / "04_script"
    script_dir.mkdir(parents=True)
    board_path = script_dir / s3b_scenes.STORYBOARD_FILENAME
    human = EpisodeStoryboard(
        episode_id="wt_human_ep_002",
        source="human",
        scenes=[
            StoryboardScene(
                scene_id="sc_001",
                scene_index=1,
                title="人工第一幕",
                location="病房",
                panel_ids=["p_001"],
                enabled=False,
                must_cover=False,
                user_note="这一段要慢慢讲。",
                locked_quotes=["你欠我一个解释"],
            )
        ],
    )
    s3b_scenes.save_storyboard(human, board_path)
    before = board_path.read_text(encoding="utf-8")

    vision = _long_vision()
    loaded = s3b_scenes.load_or_build_storyboard(
        "wt_human_ep_002", vision, board_path
    )
    assert loaded.source == "human"
    assert len(loaded.scenes) == 1
    assert loaded.scenes[0].title == "人工第一幕"
    assert loaded.scenes[0].user_note == "这一段要慢慢讲。"
    assert loaded.scenes[0].locked_quotes == ["你欠我一个解释"]
    assert loaded.scenes[0].enabled is False
    assert loaded.scenes[0].must_cover is False
    assert board_path.read_text(encoding="utf-8") == before

    # 连 reset=True 也不能覆盖人工编排。
    again = s3b_scenes.load_or_build_storyboard(
        "wt_human_ep_002", vision, board_path, reset=True
    )
    assert again.source == "human"
    assert again.scenes[0].title == "人工第一幕"
    assert board_path.read_text(encoding="utf-8") == before


def test_storyboard_path_for_points_to_script_dir() -> None:
    episode_dir = Path("/tmp/series/episodes/ep_003")
    assert s3b_scenes.storyboard_path_for(episode_dir) == (
        episode_dir / "04_script" / "storyboard.json"
    )


# ---------------------------------------------------------------------------
# 断言 3：动态配额
# ---------------------------------------------------------------------------
def test_compute_episode_beat_budget_exceeds_legacy_hard_cap() -> None:
    low, high = s4_scriptwriter.compute_episode_beat_budget(8, 75)
    assert low == 40
    assert 60 <= high <= 85
    assert high > s4_scriptwriter.BEAT_TOTAL_MAX  # 旧的写死上限 50 不够用

    # 单幕 / 单格也不会给出离谱的区间。
    assert s4_scriptwriter.compute_episode_beat_budget(1, 1) == (35, 55)
    assert s4_scriptwriter.compute_episode_beat_budget(0, 0) == (35, 55)

    word_low, word_high = s4_scriptwriter.compute_story_word_budget(8, 75)
    assert word_low == 1040
    assert word_high == 1680


# ---------------------------------------------------------------------------
# 断言 4：覆盖硬校验
# ---------------------------------------------------------------------------
def test_validate_storyboard_coverage_detects_missing_tail_scene() -> None:
    scenes = _storyboard_scenes()
    beats = [
        {
            "narration_text": "病房里的开场白。",
            "tts_text": "病房里的开场白。",
            "target_panel_ids": ["p_001"],
        },
        {
            "narration_text": "客厅里的对峙。",
            "tts_text": "客厅里的对峙。",
            "target_panel_ids": ["p_003"],
        },
    ]
    missing = s4_scriptwriter.validate_storyboard_coverage(beats, scenes)
    assert [scene.scene_id for scene in missing] == ["sc_003"]


def test_validate_storyboard_coverage_detects_missing_locked_quote() -> None:
    scenes = _storyboard_scenes()
    beats = [
        {
            "narration_text": "病房里的开场白。",
            "tts_text": "病房里的开场白。",
            "target_panel_ids": ["p_001"],
        },
        {
            "narration_text": "客厅里的对峙。",
            "tts_text": "客厅里的对峙。",
            "target_panel_ids": ["p_003"],
        },
        {
            "narration_text": "墓地里风很冷，没有人说话。",
            "tts_text": "墓地里风很冷，没有人说话。",
            "target_panel_ids": ["p_005"],
        },
    ]
    missing = s4_scriptwriter.validate_storyboard_coverage(beats, scenes)
    assert [scene.scene_id for scene in missing] == ["sc_003"]

    # 补上必保原话之后必须通过。
    beats[-1]["narration_text"] = "早知道就别倒油了，恩彗终于开口。"
    assert s4_scriptwriter.validate_storyboard_coverage(beats, scenes) == []


# ---------------------------------------------------------------------------
# 断言 5：端到端 —— 首稿漏掉第 3 幕 → 只补写第 3 幕
# ---------------------------------------------------------------------------
@pytest.mark.anyio
@respx.mock
async def test_missing_scene_is_rewritten_one_scene_at_a_time(tmp_path) -> None:
    series_dir = _make_series(tmp_path)
    script_dir = series_dir / "episodes" / "ep_002" / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    board_path = script_dir / s3b_scenes.STORYBOARD_FILENAME
    s3b_scenes.save_storyboard(
        EpisodeStoryboard(
            episode_id="wt_storyboard_ep_002",
            source="auto",
            scenes=_storyboard_scenes(),
        ),
        board_path,
    )

    rewrite_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        prompt = body["messages"][-1]["content"]
        if "story_prose" in prompt:
            return _response(_story_payload())
        if "target_panel_ids" in prompt:
            panel_ids = [
                panel_id
                for panel_id in _PANEL_RE.findall(prompt)
                if panel_id in {"p_001", "p_002", "p_003", "p_004", "p_005", "p_006"}
            ]
            if "上一稿漏掉了本幕必保台词" in prompt:
                # 定向补写：只针对缺失的那一幕。
                rewrite_calls.append(prompt)
                return _response(_beats_payload(["p_005", "p_006"], quote="早知道就别倒油了"))
            if "p_005" in panel_ids:
                # 首稿漏掉了必保原话。
                return _response(_beats_payload(panel_ids))
            return _response(_beats_payload(panel_ids))
        return _response(_overview_payload())

    respx.post(CHAT_URL).mock(side_effect=handler)
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, script_path = await s4_scriptwriter.run_stage4(
            series_dir,
            2,
            client,
            model="qwen-plus",
            episode_id="wt_storyboard_ep_002",
            series_title="镜头组测试",
            storyboard_path=board_path,
        )

    assert script_path.is_file()
    # 只对第 3 幕补写了一次（没有把别的幕重写一遍）。
    assert len(rewrite_calls) == 1
    assert "p_005" in rewrite_calls[0]
    assert "p_001" not in rewrite_calls[0]

    # 最终覆盖校验全通过：每一幕都有解说句，必保原话也念了出来。
    scenes = load_model(EpisodeStoryboard, board_path).scenes
    beats = [
        {
            "narration_text": beat.narration_text,
            "tts_text": beat.tts_text,
            "target_panel_ids": list(beat.target_panel_ids),
        }
        for beat in script.beats
    ]
    assert s4_scriptwriter.validate_storyboard_coverage(beats, scenes) == []
    assert any("早知道就别倒油了" in beat.narration_text for beat in script.beats)
