"""PATCH v4.36：「切幕预备」模式 + 4A/4B 双层解耦的回归测试。

两个需求，一句话概括：

1. **切幕预备模式**（``mode="storyboard_prep"``）——跑完 S1~S3 就停下，顺带做
   一次语义切幕（Stage 3b），等人确认镜头编排后再单独跑一次 S4。
   旧流程是「先跑到 S4 出稿 → 调幕 → 从 S4 重跑」，S4 白跑一遍。
2. **4A/4B 双层解耦**——v4.29 把「启用幕以外的分镜」从撰稿范围彻底剔除，
   于是用户把一格纯对话/空镜「移出」或「标记跳过」之后，S4 写稿就看不到这格
   的台词，剧情被剪断（跳过格子被误当成跳过剧情）。
   现在：**4A 看全貌**（移出/跳过的分镜标 ``[仅参考剧情·不出镜]``，台词不丢），
   **4B 仍然严格只看启用幕**（参考分镜一格都不会出镜）。
   只有「整段被关掉的镜头组」和版权/推广页才真正从 4A 视野消失。

本文件覆盖上面两类场景。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.schemas import (
    CharacterProfile,
    EpisodeCatalogItem,
    EpisodeDigest,
    EpisodeStoryboard,
    PanelVisionInfo,
    PlotThread,
    SeriesCatalog,
    StoryBibleV2,
    StoryboardScene,
    VisionWindowOutput,
    save_model,
)
from core.manifest import ManifestManager
from providers.llm_bailian import BailianClient
from stages import s3b_scenes, s4_scriptwriter
from web.job_runner import (
    FULL_LAST_STAGE,
    PHASE1_LAST_STAGE,
    STORYBOARD_PREP_LAST_STAGE,
    JobRunner,
)

CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
_PANEL_RE = re.compile(r"^- (p_\d{3})：", re.MULTILINE)

SERIES_ID = "wt_v436"


async def _no_sleep(_seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------
# 造数工具
# ---------------------------------------------------------------------------


def _panel(panel_id: str, **kwargs) -> PanelVisionInfo:
    return PanelVisionInfo(
        panel_id=panel_id,
        scene_summary=kwargs.get("summary", f"{panel_id} 的画面"),
        dialogues=kwargs.get("dialogues", []),
        sfx_list=[],
        non_story_type=kwargs.get("non_story_type", "none"),
        is_pure_text_card=kwargs.get("is_pure_text_card", False),
    )


def _scene(
    scene_id: str,
    panel_ids: list[str],
    *,
    index: int = 1,
    enabled: bool = True,
) -> StoryboardScene:
    return StoryboardScene(
        scene_id=scene_id,
        scene_index=index,
        title=f"第 {index} 幕",
        panel_ids=panel_ids,
        enabled=enabled,
    )


def _vision_panel(panel_id: str, summary: str, *, text: str = "") -> dict[str, Any]:
    return {
        "panel_id": panel_id,
        "scene_summary": summary,
        "dialogues": (
            [{"speaker_guess": "小白", "text": text, "emotion": "tense"}] if text else []
        ),
        "sfx_list": [],
        "is_transition_blank": False,
        "is_pure_text_card": False,
        "non_story_type": "none",
    }


# ---------------------------------------------------------------------------
# A. 4A / 4B 双层解耦
# ---------------------------------------------------------------------------


def test_build_story_context_panels_keeps_removed_panels_as_reference_only():
    """被移出/跳过的分镜：4A 看得见（保剧情），但标成「仅参考剧情·不出镜」。"""
    panels = [_panel(f"p_{i:03d}") for i in range(1, 5)]
    storyboard = EpisodeStoryboard(
        episode_id="ep_001",
        source="human",
        scenes=[
            _scene("sc_001", ["p_001", "p_002"], index=1),
            _scene("sc_002", ["p_003"], index=2),
        ],
    )
    active_scenes = [scene for scene in storyboard.scenes if scene.enabled]
    # p_004 不被任何幕引用（用户在编排页把它「移出」了）。
    valid_ids = {"p_001", "p_002", "p_003"}

    context, reference_only = s4_scriptwriter._build_story_context_panels(
        panels=panels,
        all_panels=panels,
        valid_panel_ids=valid_ids,
        resolved_storyboard=storyboard,
        active_scenes=active_scenes,
        scene_mode=True,
    )

    assert [panel.panel_id for panel in context] == [
        "p_001",
        "p_002",
        "p_003",
        "p_004",
    ]
    assert reference_only == {"p_004"}


def test_build_story_context_panels_drops_disabled_scene_only_panels():
    """整段被关掉的镜头组 → 连 4A 的参考都不给（用户明确不要这一段）。"""
    panels = [_panel(f"p_{i:03d}") for i in range(1, 5)]
    storyboard = EpisodeStoryboard(
        episode_id="ep_001",
        source="human",
        scenes=[
            _scene("sc_001", ["p_001"], index=1),
            _scene("sc_002", ["p_002"], index=2, enabled=False),
        ],
    )
    active_scenes = [scene for scene in storyboard.scenes if scene.enabled]

    context, reference_only = s4_scriptwriter._build_story_context_panels(
        panels=panels,
        all_panels=panels,
        valid_panel_ids={"p_001"},
        resolved_storyboard=storyboard,
        active_scenes=active_scenes,
        scene_mode=True,
    )

    ids = [panel.panel_id for panel in context]
    assert "p_002" not in ids  # 禁用幕独有
    assert "p_001" in ids
    assert "p_002" not in reference_only


def test_build_story_context_panels_drops_copyright_and_promo():
    """版权页 / 推广尾页：连参考都不给。"""
    panels = [
        _panel("p_001"),
        _panel("p_002", non_story_type="copyright_notice"),
        _panel("p_003", non_story_type="promo_banner"),
    ]
    storyboard = EpisodeStoryboard(
        episode_id="ep_001",
        source="human",
        scenes=[_scene("sc_001", ["p_001"], index=1)],
    )
    active_scenes = [scene for scene in storyboard.scenes if scene.enabled]

    context, _ = s4_scriptwriter._build_story_context_panels(
        panels=panels,
        all_panels=panels,
        valid_panel_ids={"p_001"},
        resolved_storyboard=storyboard,
        active_scenes=active_scenes,
        scene_mode=True,
    )

    assert [panel.panel_id for panel in context] == ["p_001"]


def test_format_panel_list_marks_reference_only_panels():
    """参考分镜在 prompt 里必须带标记，且台词与画面描述一字不少。"""
    panels = [
        _panel(
            "p_004",
            summary="客厅里，恩彗攥紧了拳头。",
            dialogues=[{"speaker_guess": "小白", "text": "你终于肯说实话了。", "emotion": "tense"}],
        )
    ]
    rendered = s4_scriptwriter.format_panel_list(panels, {"p_004"})
    assert "[仅参考剧情·不出镜]" in rendered
    assert "客厅里，恩彗攥紧了拳头。" in rendered
    assert "你终于肯说实话了。" in rendered


def test_format_panel_list_without_reference_marks_unchanged():
    """没传参考集合时输出与旧行为一致（不含标记）。"""
    panels = [_panel("p_001", summary="病房里，小白攥紧信纸。")]
    rendered = s4_scriptwriter.format_panel_list(panels)
    assert "[仅参考剧情·不出镜]" not in rendered
    assert "- p_001：" in rendered
    assert "病房里，小白攥紧信纸。" in rendered


def test_storyboard_constraints_block_mentions_reference_panels():
    """4A 的分幕约束里必须写清「参考分镜的台词要融入相邻章节」。"""
    scenes = [_scene("sc_001", ["p_001", "p_002"], index=1)]
    block = s4_scriptwriter._storyboard_constraints_block(scenes, {"p_004"})
    assert "【仅参考分镜】" in block
    assert "p_004" in block
    assert "不得为其单独开章" in block


def test_storyboard_constraints_block_without_reference_panels_unchanged():
    """没有参考分镜时约束块与旧行为一致。"""
    scenes = [_scene("sc_001", ["p_001"], index=1)]
    block = s4_scriptwriter._storyboard_constraints_block(scenes)
    assert "【仅参考分镜】" not in block
    assert "第 1 幕" in block


# ---------------------------------------------------------------------------
# A2. 端到端：4A 能看到参考分镜，4B 选不到
# ---------------------------------------------------------------------------


def _make_series(tmp_path: Path) -> tuple[Path, Path]:
    """铺一套最小可跑的「话次」：故事圣经 + 视觉结果 + 两幕人工编排。

    ``p_004`` 不被任何幕引用——模拟用户在编排页把它「移出」了。
    """
    series_dir = tmp_path / "workspace" / SERIES_ID
    series_dir.mkdir(parents=True)
    bible = StoryBibleV2(
        series_id=SERIES_ID,
        series_title="切幕预备测试",
        characters=[
            CharacterProfile(
                name="小白",
                aliases=[],
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

    episode_dir = series_dir / "episodes" / "ep_002"
    vision_dir = episode_dir / "03_vision"
    vision_dir.mkdir(parents=True)
    save_model(
        VisionWindowOutput.model_validate(
            {
                "panels": [
                    _vision_panel("p_001", "病房里，小白攥紧信纸。"),
                    _vision_panel("p_002", "病房里，走廊灯忽明忽暗。"),
                    _vision_panel("p_003", "客厅里，两人隔着茶几对视。"),
                    _vision_panel(
                        "p_004",
                        "客厅里，恩彗攥紧了拳头。",
                        text="你终于肯说实话了。",
                    ),
                ]
            }
        ),
        vision_dir / "vision.json",
    )

    script_dir = episode_dir / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    board_path = script_dir / s3b_scenes.STORYBOARD_FILENAME
    s3b_scenes.save_storyboard(
        EpisodeStoryboard(
            episode_id=f"{SERIES_ID}_ep_002",
            source="human",
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
                    title="第 2 幕 · 客厅",
                    location="客厅",
                    panel_ids=["p_003"],
                    enabled=True,
                    must_cover=True,
                ),
            ],
        ),
        board_path,
    )
    return series_dir, board_path


def _llm_response(content: str) -> httpx.Response:
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


def _story_payload(chapter_ranges: list[tuple[str, str]]) -> str:
    prose = "这一段讲的是雨夜里的相遇与试探，人物在沉默中一点点交出底牌。"
    return json.dumps(
        {
            "episode_id": f"{SERIES_ID}_ep_002",
            "hook_opening": "雨夜有人敲门，来者不善。",
            "full_story_markdown": f"## 雨夜试探\n\n{prose}",
            "chapters": [
                {
                    "chapter_index": index,
                    "chapter_title": f"第 {index} 幕",
                    "panel_hint_range": [first, last],
                    "story_prose": prose,
                }
                for index, (first, last) in enumerate(chapter_ranges, start=1)
            ],
        },
        ensure_ascii=False,
    )


def _beats_payload(panel_ids: list[str]) -> str:
    return json.dumps(
        {
            "beats": [
                {
                    "beat_id": f"b{index}",
                    "narration_text": f"{panel_id} 的解说句要写够十个字以上。",
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
async def test_stage4_context_split_keeps_reference_panels_out_of_beats(tmp_path):
    """4A 能看到被移出的分镜（保剧情），4B 选不到（不出镜）。"""
    series_dir, board_path = _make_series(tmp_path)
    story_prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        prompt = body["messages"][-1]["content"]
        if "story_prose" in prompt:
            story_prompts.append(prompt)
            return _llm_response(
                _story_payload([("p_001", "p_002"), ("p_003", "p_003")])
            )
        if "target_panel_ids" in prompt:
            panel_ids = [
                panel_id
                for panel_id in _PANEL_RE.findall(prompt)
                if panel_id in {"p_001", "p_002", "p_003", "p_004"}
            ]
            return _llm_response(_beats_payload(panel_ids or ["p_001"]))
        return _llm_response(_overview_payload())

    respx.post(CHAT_URL).mock(side_effect=handler)
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, _script_path = await s4_scriptwriter.run_stage4(
            series_dir,
            2,
            client,
            model="qwen-plus",
            episode_id=f"{SERIES_ID}_ep_002",
            series_title="切幕预备测试",
            storyboard_path=board_path,
        )

    # 4A 的 prompt 里能看到被移出的 p_004，且带「仅参考剧情·不出镜」标记。
    assert story_prompts, "4A 没有被调用"
    assert "p_004" in story_prompts[0]
    assert "[仅参考剧情·不出镜]" in story_prompts[0]
    # 分幕约束里也写清了参考分镜怎么用。
    assert "【仅参考分镜】" in story_prompts[0]

    # 最终剧本一格参考分镜都没引用。
    used = {
        panel_id for beat in script.beats for panel_id in beat.target_panel_ids
    }
    assert "p_004" not in used
    assert used <= {"p_001", "p_002", "p_003"}


@pytest.mark.anyio
@respx.mock
async def test_stage4_drops_reference_panel_even_if_model_picks_it(tmp_path):
    """模型违规挑了参考分镜 → 落盘前必须被剔掉（4B 候选窗口只认启用幕）。"""
    series_dir, board_path = _make_series(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        prompt = body["messages"][-1]["content"]
        if "story_prose" in prompt:
            return _llm_response(
                _story_payload([("p_001", "p_002"), ("p_003", "p_003")])
            )
        if "target_panel_ids" in prompt:
            # 模型违规：把参考分镜 p_004 也写进来了。
            return _llm_response(_beats_payload(["p_001", "p_003", "p_004"]))
        return _llm_response(_overview_payload())

    respx.post(CHAT_URL).mock(side_effect=handler)
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, _script_path = await s4_scriptwriter.run_stage4(
            series_dir,
            2,
            client,
            model="qwen-plus",
            episode_id=f"{SERIES_ID}_ep_002",
            series_title="切幕预备测试",
            storyboard_path=board_path,
        )

    used = {
        panel_id for beat in script.beats for panel_id in beat.target_panel_ids
    }
    assert "p_004" not in used


# ---------------------------------------------------------------------------
# B. 「切幕预备」模式
# ---------------------------------------------------------------------------


def test_storyboard_prep_last_stage_constant():
    """切幕预备模式的终点是 S3（视觉抽取）。"""
    assert STORYBOARD_PREP_LAST_STAGE == "s3_vision"
    assert PHASE1_LAST_STAGE == "s4_scriptwriter"
    assert FULL_LAST_STAGE == "s7_renderer"


def test_manifest_preserves_semantic_storyboard_on_s4_rerun(tmp_path: Path):
    """从 S4 重跑时保留「已完成的语义切幕」（Patch v4.36）。"""
    series_dir = tmp_path / "workspace" / SERIES_ID
    episode_dir = series_dir / "episodes" / "ep_001"
    script_dir = episode_dir / "04_script"
    script_dir.mkdir(parents=True)
    board_path = script_dir / s3b_scenes.STORYBOARD_FILENAME
    s3b_scenes.save_storyboard(
        EpisodeStoryboard(
            episode_id="wt_v436_ep_001",
            source="auto",
            scenes=[_scene("sc_001", ["p_001"], index=1)],
            auto_strategy="semantic",
        ),
        board_path,
    )

    manager = ManifestManager(series_dir, series_id=SERIES_ID)
    summary = manager.reset_from_stage(1, "s4_scriptwriter")

    assert board_path.is_file(), "语义切幕成果被误删了"
    assert summary.get("preserved_human_storyboard") is True


def test_manifest_preserves_human_storyboard_on_s4_rerun(tmp_path: Path):
    """人工编排照旧保留（既有行为不变）。"""
    series_dir = tmp_path / "workspace" / SERIES_ID
    episode_dir = series_dir / "episodes" / "ep_001"
    script_dir = episode_dir / "04_script"
    script_dir.mkdir(parents=True)
    board_path = script_dir / s3b_scenes.STORYBOARD_FILENAME
    s3b_scenes.save_storyboard(
        EpisodeStoryboard(
            episode_id="wt_v436_ep_001",
            source="human",
            scenes=[_scene("sc_001", ["p_001"], index=1)],
        ),
        board_path,
    )

    manager = ManifestManager(series_dir, series_id=SERIES_ID)
    manager.reset_from_stage(1, "s4_scriptwriter")

    assert board_path.is_file()


def test_manifest_removes_rule_placeholder_storyboard_on_s4_rerun(tmp_path: Path):
    """规则切幕占位（auto_strategy="rule"）不是真成果 → 照旧清掉。"""
    series_dir = tmp_path / "workspace" / SERIES_ID
    episode_dir = series_dir / "episodes" / "ep_001"
    script_dir = episode_dir / "04_script"
    script_dir.mkdir(parents=True)
    board_path = script_dir / s3b_scenes.STORYBOARD_FILENAME
    s3b_scenes.save_storyboard(
        EpisodeStoryboard(
            episode_id="wt_v436_ep_001",
            source="auto",
            scenes=[_scene("sc_001", ["p_001"], index=1)],
            auto_strategy="rule",
        ),
        board_path,
    )

    manager = ManifestManager(series_dir, series_id=SERIES_ID)
    manager.reset_from_stage(1, "s4_scriptwriter")

    assert not board_path.exists()


class _FakeRegistry:
    """只提供 JobRunner._run_episode 用到的两个方法。"""

    def __init__(self, series_dir: Path, catalog: SeriesCatalog) -> None:
        self._series_dir = series_dir
        self._catalog = catalog

    def load_catalog(self, series_id: str) -> SeriesCatalog | None:
        return self._catalog

    def series_dir(self, series_id: str) -> Path:
        return self._series_dir


def _catalog() -> SeriesCatalog:
    return SeriesCatalog(
        series_id=SERIES_ID,
        series_title="切幕预备测试",
        list_url="https://example.com/list",
        episodes=[
            EpisodeCatalogItem(
                episode_no=1,
                episode_id=f"{SERIES_ID}_ep_001",
                title="第 1 话",
                viewer_url="https://example.com/1",
            )
        ],
    )


@pytest.mark.anyio
async def test_storyboard_prep_mode_stops_after_s3(tmp_path, monkeypatch):
    """切幕预备模式：跑到 S3 就停，S4 **不执行**，只做一次切幕预备。"""
    series_dir = tmp_path / "workspace" / SERIES_ID
    series_dir.mkdir(parents=True)
    executed: list[str] = []
    prepared: list[str] = []

    async def _fake_run_stage(self, state, *, stage, **kwargs) -> None:
        executed.append(stage)

    async def _fake_prepare(self, state, series_dir_, episode, llm_client, story_model):
        prepared.append(episode.episode_id)

    monkeypatch.setattr(JobRunner, "_run_stage", _fake_run_stage)
    monkeypatch.setattr(JobRunner, "_stage_prepare_storyboard", _fake_prepare)
    monkeypatch.setattr(
        JobRunner,
        "registry",
        property(lambda self: _FakeRegistry(series_dir, _catalog())),
    )
    monkeypatch.setattr(ManifestManager, "reset_from_stage", lambda *a, **k: {})

    runner = JobRunner(
        _app_config(tmp_path), client_factory=lambda: object(), tts_factory=lambda: None
    )
    await runner._run_episode(
        SERIES_ID, 1, from_stage="s1_crawler", mode="storyboard_prep"
    )

    assert executed == ["s1_crawler", "s2_segmenter", "s3_vision"]
    assert "s4_scriptwriter" not in executed
    assert prepared == [f"{SERIES_ID}_ep_001"]


@pytest.mark.anyio
async def test_storyboard_prep_mode_promotes_s4_rerun_to_phase1(
    tmp_path, monkeypatch
):
    """切幕预备模式下点「S4 从此步重跑」→ 提升为出稿止步，S4 真的会跑。"""
    series_dir = tmp_path / "workspace" / SERIES_ID
    series_dir.mkdir(parents=True)
    executed: list[str] = []
    prepared: list[str] = []

    async def _fake_run_stage(self, state, *, stage, **kwargs) -> None:
        executed.append(stage)

    async def _fake_prepare(self, state, series_dir_, episode, llm_client, story_model):
        prepared.append(episode.episode_id)

    monkeypatch.setattr(JobRunner, "_run_stage", _fake_run_stage)
    monkeypatch.setattr(JobRunner, "_stage_prepare_storyboard", _fake_prepare)
    monkeypatch.setattr(
        JobRunner,
        "registry",
        property(lambda self: _FakeRegistry(series_dir, _catalog())),
    )
    monkeypatch.setattr(ManifestManager, "reset_from_stage", lambda *a, **k: {})

    runner = JobRunner(
        _app_config(tmp_path), client_factory=lambda: object(), tts_factory=lambda: None
    )
    await runner._run_episode(
        SERIES_ID, 1, from_stage="s4_scriptwriter", mode="storyboard_prep"
    )

    assert executed == ["s4_scriptwriter"]
    assert prepared == []


@pytest.mark.anyio
async def test_storyboard_prep_mode_promotes_s5_rerun_to_full_video(
    tmp_path, monkeypatch
):
    """切幕预备模式下点「S5 从此步重跑」→ 提升为全流程，一路跑到 S7。"""
    series_dir = tmp_path / "workspace" / SERIES_ID
    series_dir.mkdir(parents=True)
    executed: list[str] = []

    async def _fake_run_stage(self, state, *, stage, **kwargs) -> None:
        executed.append(stage)

    monkeypatch.setattr(JobRunner, "_run_stage", _fake_run_stage)
    monkeypatch.setattr(
        JobRunner,
        "registry",
        property(lambda self: _FakeRegistry(series_dir, _catalog())),
    )
    monkeypatch.setattr(ManifestManager, "reset_from_stage", lambda *a, **k: {})

    runner = JobRunner(
        _app_config(tmp_path), client_factory=lambda: object(), tts_factory=lambda: None
    )
    await runner._run_episode(
        SERIES_ID, 1, from_stage="s5_tts", mode="storyboard_prep"
    )

    assert executed == ["s5_tts", "s6_timeline", "s7_renderer"]


def _app_config(tmp_path: Path):
    from core.config import AppConfig, WorkspaceConfig

    return AppConfig(workspace=WorkspaceConfig(root=tmp_path / "workspace"))


# ---------------------------------------------------------------------------
# B2. 「单次 S4」：切幕预备 + 人工确认后，S4 只跑一遍
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_storyboard_prep_then_confirm_runs_stage4_exactly_once(
    tmp_path, monkeypatch
):
    """核心场景：先切幕预备（不跑 S4），确认编排后再跑一次 S4 —— S4 全程只跑一遍。"""
    series_dir = tmp_path / "workspace" / SERIES_ID
    series_dir.mkdir(parents=True)
    executed: list[str] = []
    prepared_boards: list[Path] = []

    async def _fake_run_stage(self, state, *, stage, **kwargs) -> None:
        executed.append(stage)

    async def _fake_prepare(self, state, series_dir_, episode, llm_client, story_model):
        episode_dir = series_dir_ / "episodes" / f"ep_{episode.episode_no:03d}"
        board_path = s3b_scenes.storyboard_path_for(episode_dir)
        s3b_scenes.save_storyboard(
            EpisodeStoryboard(
                episode_id=episode.episode_id,
                source="auto",
                scenes=[_scene("sc_001", ["p_001"], index=1)],
                auto_strategy="semantic",
            ),
            board_path,
        )
        prepared_boards.append(board_path)

    monkeypatch.setattr(JobRunner, "_run_stage", _fake_run_stage)
    monkeypatch.setattr(JobRunner, "_stage_prepare_storyboard", _fake_prepare)
    monkeypatch.setattr(
        JobRunner,
        "registry",
        property(lambda self: _FakeRegistry(series_dir, _catalog())),
    )
    # 真实的 manifest 复位逻辑（要验证「语义切幕成果不被误删」）。
    monkeypatch.setattr(
        JobRunner,
        "_reset_manifest",
        lambda self, *a, **k: None,
        raising=False,
    )

    runner = JobRunner(
        _app_config(tmp_path), client_factory=lambda: object(), tts_factory=lambda: None
    )

    # 1) 切幕预备：S1~S3，S4 不跑。
    await runner._run_episode(
        SERIES_ID, 1, from_stage="s1_crawler", mode="storyboard_prep"
    )
    assert "s4_scriptwriter" not in executed
    assert len(prepared_boards) == 1
    assert prepared_boards[0].is_file()

    # 2) 人工确认编排后，单独跑一次 S4（对应 /storyboard/generate 的提交）。
    await runner._run_episode(
        SERIES_ID, 1, from_stage="s4_scriptwriter", mode="phase1_only"
    )
    assert executed.count("s4_scriptwriter") == 1


@pytest.mark.anyio
async def test_storyboard_prep_then_confirm_preserves_semantic_board(
    tmp_path, monkeypatch
):
    """人工确认后跑 S4：磁盘上的语义切幕成果必须还在（不被 reset 清掉）。"""
    series_dir = tmp_path / "workspace" / SERIES_ID
    series_dir.mkdir(parents=True)

    async def _fake_run_stage(self, state, *, stage, **kwargs) -> None:
        return None

    monkeypatch.setattr(JobRunner, "_run_stage", _fake_run_stage)
    monkeypatch.setattr(
        JobRunner,
        "registry",
        property(lambda self: _FakeRegistry(series_dir, _catalog())),
    )

    episode_dir = series_dir / "episodes" / "ep_001"
    board_path = s3b_scenes.storyboard_path_for(episode_dir)
    s3b_scenes.save_storyboard(
        EpisodeStoryboard(
            episode_id=f"{SERIES_ID}_ep_001",
            source="auto",
            scenes=[_scene("sc_001", ["p_001"], index=1)],
            auto_strategy="semantic",
        ),
        board_path,
    )

    runner = JobRunner(
        _app_config(tmp_path), client_factory=lambda: object(), tts_factory=lambda: None
    )
    await runner._run_episode(
        SERIES_ID, 1, from_stage="s4_scriptwriter", mode="phase1_only"
    )

    assert board_path.is_file()
    assert json.loads(board_path.read_text(encoding="utf-8"))["auto_strategy"] == "semantic"


# ---------------------------------------------------------------------------
# B3. API 层的 mode 校验
# ---------------------------------------------------------------------------


def test_api_rejects_unknown_run_mode(tmp_path):
    """非法 mode 必须 400，而不是悄悄按默认模式跑。"""
    from fastapi.testclient import TestClient

    from web.server import create_app

    client = TestClient(create_app(_app_config(tmp_path)))
    response = client.post(
        "/api/jobs/batch", json={"items": [{"series_id": "x", "episode_no": 1}], "mode": "bogus"}
    )
    assert response.status_code == 400
    assert "storyboard_prep" in response.json()["detail"]


def test_api_accepts_storyboard_prep_mode(tmp_path):
    """切幕预备模式是合法 mode。"""
    from fastapi.testclient import TestClient

    from web.server import create_app

    client = TestClient(create_app(_app_config(tmp_path)))
    response = client.post(
        "/api/jobs/batch",
        json={
            "items": [{"series_id": "wt_v436", "episode_no": 1}],
            "mode": "storyboard_prep",
        },
    )
    assert response.status_code == 200
