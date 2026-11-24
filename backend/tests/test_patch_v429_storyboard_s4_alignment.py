"""PATCH v4.29：镜头编排重组后 Stage 4 与分镜头对齐的回归测试。

背景：用户在编排页做「拆分幕 / 合并幕 / 跨幕移格 / 删格 / 禁用幕 /
复用分镜」之后重跑 Stage 4，解说与多音字审核页出现分镜头乱序、大量解说
与画面对不上。根因分布在四处：

1. **过期画面复核摘要未失效**——重组后 ``visual_recap`` 还挂着旧分镜组合
   的摘要，4A 拿着它去讲早就被改掉的画面；
2. **撰稿范围没收敛**——概览与 4A 看着整话全部分镜，4B 却只在启用幕的
   窗口里挑图，两边看到的世界不一致；
3. **章幕配对下标 bug**——章幕数量不等时把「幕下标」当「章节下标」用，
   正文被写进完全不相干的章节；
4. **跨幕残句**——补写整幕时，旧解说残句因 ``target_panel_ids`` 已被钳制
   到邻幕分镜而没被剔除；强行钳制时锚点又被固定到上一幕末尾分镜。

本文件按上面四类场景逐条覆盖。
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
    PanelVisionInfo,
    PlotThread,
    StoryBibleV2,
    StoryboardScene,
    StoryChapter,
    VisionWindowOutput,
    save_model,
)
from providers.llm_bailian import BailianClient
from stages import s4_scriptwriter, s3b_scenes
from web.server import _validate_storyboard_scenes

CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
_PANEL_RE = re.compile(r"^- (p_\d{3})：", re.MULTILINE)


async def _no_sleep(_seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------
# 造数工具
# ---------------------------------------------------------------------------


def _panel(panel_id: str, **kwargs) -> PanelVisionInfo:
    return PanelVisionInfo(
        panel_id=panel_id,
        scene_summary=kwargs.get("summary", f"{panel_id} 的画面"),
        dialogues=[],
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
    visual_recap: str = "",
) -> StoryboardScene:
    return StoryboardScene(
        scene_id=scene_id,
        scene_index=index,
        title=f"第 {index} 幕",
        panel_ids=panel_ids,
        enabled=enabled,
        visual_recap=visual_recap,
    )


# ---------------------------------------------------------------------------
# 场景 1：分镜池收敛（禁用幕剔除 + 孤儿剔除 + 人工放行）
# ---------------------------------------------------------------------------


def test_converge_panel_pool_drops_disabled_and_orphan_panels():
    """启用幕之外的格子（禁用幕独有 + 无人引用的孤儿）必须退出撰稿范围。"""
    panels = [_panel(f"p{i}") for i in range(1, 5)]
    all_panels = list(panels)
    storyboard = EpisodeStoryboard(
        episode_id="ep_001",
        source="human",
        scenes=[
            _scene("sc_001", ["p1", "p2"], index=1),
            _scene("sc_002", ["p3"], index=2, enabled=False),
        ],
    )
    active_scenes = [scene for scene in storyboard.scenes if scene.enabled]

    valid_ids, ordered_ids, panel_map, story_panels = s4_scriptwriter._converge_panel_pool(
        panels=panels,
        all_panels=all_panels,
        resolved_storyboard=storyboard,
        active_scenes=active_scenes,
        scene_mode=True,
    )

    # p3 属于禁用幕；p4 不被任何幕引用（孤儿）。两者都必须被剔除。
    assert valid_ids == {"p1", "p2"}
    assert ordered_ids == ["p1", "p2"]
    assert set(panel_map) == {"p1", "p2"}
    assert [panel.panel_id for panel in story_panels] == ["p1", "p2"]


def test_converge_panel_pool_keeps_manually_released_panel():
    """用户把过渡页编进启用幕后，该格要被「放行」回撰稿范围。

    这里的过渡页**不在** ``panels`` 里（撰稿上下文已把它过滤掉），
    却在 ``all_panels`` 里——放行后它必须同时出现在 ``valid_panel_ids``
    与 ``ordered_panel_ids`` 中，否则 4A 看不到它、4B 也选不到它。
    """
    panels = [_panel("p1")]
    all_panels = [_panel("p1"), _panel("p2", non_story_type="transition_blank")]
    storyboard = EpisodeStoryboard(
        episode_id="ep_001",
        source="human",
        scenes=[_scene("sc_001", ["p1", "p2"], index=1)],
    )
    active_scenes = [scene for scene in storyboard.scenes if scene.enabled]

    valid_ids, ordered_ids, panel_map, story_panels = s4_scriptwriter._converge_panel_pool(
        panels=panels,
        all_panels=all_panels,
        resolved_storyboard=storyboard,
        active_scenes=active_scenes,
        scene_mode=True,
    )

    assert valid_ids == {"p1", "p2"}
    assert ordered_ids == ["p1", "p2"]
    assert set(panel_map) == {"p1", "p2"}
    assert {panel.panel_id for panel in story_panels} <= {"p1", "p2"}


def test_converge_panel_pool_never_releases_copyright_or_promo():
    """版权声明页 / 推广尾页永远不放行，也不进入撰稿范围。"""
    panels = [_panel("p1")]
    all_panels = [
        _panel("p1"),
        _panel("p2", non_story_type="copyright_notice"),
        _panel("p3", non_story_type="promo_banner"),
    ]
    storyboard = EpisodeStoryboard(
        episode_id="ep_001",
        source="human",
        scenes=[_scene("sc_001", ["p1", "p2", "p3"], index=1)],
    )
    active_scenes = [scene for scene in storyboard.scenes if scene.enabled]

    valid_ids, _ordered_ids, _panel_map, _story_panels = s4_scriptwriter._converge_panel_pool(
        panels=panels,
        all_panels=all_panels,
        resolved_storyboard=storyboard,
        active_scenes=active_scenes,
        scene_mode=True,
    )

    assert valid_ids == {"p1"}


def test_converge_panel_pool_without_scene_mode_keeps_legacy_behavior():
    """未启用逐幕模式时保持旧行为：不剔除任何分镜。"""
    panels = [_panel("p1"), _panel("p2"), _panel("p3")]
    valid_ids, ordered_ids, panel_map, story_panels = s4_scriptwriter._converge_panel_pool(
        panels=panels,
        all_panels=list(panels),
        resolved_storyboard=None,
        active_scenes=[],
        scene_mode=False,
    )
    assert valid_ids == {"p1", "p2", "p3"}
    assert ordered_ids == ["p1", "p2", "p3"]
    assert set(panel_map) == {"p1", "p2", "p3"}
    assert [panel.panel_id for panel in story_panels] == ["p1", "p2", "p3"]


# ---------------------------------------------------------------------------
# 场景 2：章幕配对下标 bug 修复（未配对章节就近归并）
# ---------------------------------------------------------------------------


def test_merge_unpaired_chapters_does_not_mix_scene_and_chapter_index():
    """章数 < 幕数时，未配对章节必须并入**已配对幕**对应的章节，且绝不越界。

    旧实现取 ``target = max(pairs)``（**幕**下标）后直接 ``chapters[target]``：
    本例 ``pairs = {2: 0}``（只有第 3 幕配到第 1 章），``max(pairs)`` 是 2，
    而章节只有 2 个 → 直接 ``IndexError``；即便不崩也会把正文写进错章节。
    """
    chapters = [
        StoryChapter(
            chapter_index=1,
            chapter_title="第一章",
            panel_hint_range=("p1", "p2"),
            story_prose="正文A",
        ),
        StoryChapter(
            chapter_index=2,
            chapter_title="第二章",
            panel_hint_range=("p5", "p5"),
            story_prose="正文B",
        ),
    ]
    # 三幕：第 0 幕 p1~p2、第 1 幕 p3~p4、第 2 幕 p5。
    active_scenes = [
        _scene("sc_001", ["p1", "p2"], index=1),
        _scene("sc_002", ["p3", "p4"], index=2),
        _scene("sc_003", ["p5"], index=3),
    ]
    # 只有第 2 幕（幕下标 2）配到了第 0 章；第 1 章没配上任何幕。
    pairs = {2: 0}
    ordered = ["p1", "p2", "p3", "p4", "p5"]

    merged = s4_scriptwriter._merge_unpaired_chapters(
        chapters, pairs, active_scenes, ordered
    )

    assert len(merged) == 2
    # 正文归入「幕下标 → 章节下标」正确映射的那一章（章 0），不是章 1。
    assert "正文B" in merged[0].story_prose
    # 原章节正文不被丢弃。
    assert merged[1].story_prose == "正文B"


def test_merge_unpaired_chapters_never_writes_out_of_range():
    """章数 > 幕数时按分镜区间就近归并，不越界也不丢正文。"""
    chapters = [
        StoryChapter(
            chapter_index=index + 1,
            chapter_title=f"第 {index + 1} 章",
            panel_hint_range=(f"p{index + 1}", f"p{index + 1}"),
            story_prose=f"正文{index + 1}",
        )
        for index in range(4)
    ]
    active_scenes = [
        _scene("sc_001", ["p1", "p2"], index=1),
        _scene("sc_002", ["p3", "p4"], index=2),
    ]
    # 只有幕 0 配上章 0；章 1~3 未配对。
    pairs = {0: 0}
    ordered = ["p1", "p2", "p3", "p4"]

    merged = s4_scriptwriter._merge_unpaired_chapters(
        chapters, pairs, active_scenes, ordered
    )

    assert len(merged) == 4
    # 所有未配对章节的正文都要被保留（不能丢）。
    joined = " ".join(chapter.story_prose for chapter in merged)
    for index in range(1, 5):
        assert f"正文{index}" in joined


def test_merge_unpaired_chapters_uses_nearest_scene_not_last_chapter():
    """未配对章节按分镜区间邻近度归并，而不是「全塞进末章」。"""
    chapters = [
        StoryChapter(
            chapter_index=1,
            chapter_title="第一章",
            panel_hint_range=("p1", "p1"),
            story_prose="正文A",
        ),
        StoryChapter(
            chapter_index=2,
            chapter_title="第二章",
            panel_hint_range=("p2", "p2"),
            story_prose="正文B",
        ),
        StoryChapter(
            chapter_index=3,
            chapter_title="第三章",
            panel_hint_range=("p3", "p3"),
            story_prose="正文C",
        ),
    ]
    active_scenes = [
        _scene("sc_001", ["p1"], index=1),
        _scene("sc_002", ["p2"], index=2),
        _scene("sc_003", ["p3"], index=3),
    ]
    # 只有幕 0 / 幕 2 配上章 0 / 章 2；章 1 未配对 → 就近归到幕 0（章 0）。
    pairs = {0: 0, 2: 2}
    ordered = ["p1", "p2", "p3"]

    merged = s4_scriptwriter._merge_unpaired_chapters(
        chapters, pairs, active_scenes, ordered
    )

    assert "正文B" in merged[0].story_prose
    assert merged[1].story_prose == "正文B"
    assert merged[2].story_prose == "正文C"


def test_scene_window_deduplicates_reused_panels():
    """「再加一格」复用同一格时，交给 4B 的候选窗口必须保序去重。"""
    scene = _scene("sc_001", ["p1", "p2", "p1", "p3"], index=1)
    window = s4_scriptwriter._scene_window(scene, {"p1", "p2", "p3"})
    assert window == ["p1", "p2", "p3"]


def test_scene_window_filters_out_of_pool_panels():
    """收敛后的分镜池之外的格子不能进候选窗口。"""
    scene = _scene("sc_001", ["p1", "p9"], index=1)
    window = s4_scriptwriter._scene_window(scene, {"p1", "p2"})
    assert window == ["p1"]


# ---------------------------------------------------------------------------
# 场景 3：跨幕残句（_splice_scene_beats / _clamp_regressive_beats）
# ---------------------------------------------------------------------------


def test_splice_scene_beats_removes_residual_beat_by_scene_id():
    """旧句已被钳制到邻幕分镜、但 ``scene_id`` 仍属于本幕时，必须一并剔除。"""
    scene = _scene("sc_002", ["p3", "p4"], index=2)
    beats = [
        {
            "beat_id": "b1",
            "narration_text": "上一幕的句子",
            "target_panel_ids": ["p1"],
            "scene_id": "sc_001",
        },
        {
            "beat_id": "b2",
            "narration_text": "本幕被钳制到邻幕的残句",
            "target_panel_ids": ["p1"],
            "scene_id": "sc_002",
        },
    ]
    new_beats = [
        {
            "beat_id": "b3",
            "narration_text": "本幕重写后的句子",
            "target_panel_ids": ["p3"],
            "scene_id": "sc_002",
        }
    ]

    merged = s4_scriptwriter._splice_scene_beats(
        beats, scene, new_beats, ["p1", "p2", "p3", "p4"]
    )

    texts = [beat["narration_text"] for beat in merged]
    assert "本幕被钳制到邻幕的残句" not in texts
    assert "本幕重写后的句子" in texts
    assert "上一幕的句子" in texts


def test_clamp_regressive_beats_anchors_inside_own_scene():
    """强行钳制时锚点必须留在本幕，不能挂到上一幕末尾分镜。"""
    scenes = [
        _scene("sc_001", ["p1", "p2", "p3"], index=1),
        _scene("sc_002", ["p4", "p5"], index=2),
    ]
    beats = [
        {
            "beat_id": "b1",
            "narration_text": "第一幕推进到 p3",
            "target_panel_ids": ["p3"],
            "scene_id": "sc_001",
        },
        {
            "beat_id": "b2",
            "narration_text": "第二幕回看",
            "target_panel_ids": ["p1"],
            "scene_id": "sc_002",
        },
    ]

    logs = s4_scriptwriter._clamp_regressive_beats(
        beats, ["p1", "p2", "p3", "p4", "p5"], scenes=scenes
    )

    # 旧行为会锚到 p3（上一幕末尾）；新行为必须留在第二幕自己的分镜里。
    assert beats[1]["target_panel_ids"] == ["p4"]
    assert logs
    assert "sc_002" in logs[0]


def test_clamp_regressive_beats_without_scenes_keeps_legacy_anchor():
    """没有幕信息时保持旧行为：锚到当前主画面。"""
    beats = [
        {
            "beat_id": "b1",
            "narration_text": "第一句",
            "target_panel_ids": ["p2"],
        },
        {
            "beat_id": "b2",
            "narration_text": "回看",
            "target_panel_ids": ["p1"],
        },
    ]
    s4_scriptwriter._clamp_regressive_beats(beats, ["p1", "p2"])
    assert beats[1]["target_panel_ids"] == ["p2"]


# ---------------------------------------------------------------------------
# 场景 4：重组后 visual_recap 失效（服务端校验 + 人工编排回写）
# ---------------------------------------------------------------------------


def test_validate_storyboard_scenes_invalidates_stale_visual_recap():
    """分镜组合变了、提交值仍等于旧摘要 → 服务端强制清空。"""
    existing = [_scene("sc_001", ["p1", "p2"], index=1, visual_recap="旧摘要")]
    scenes = _validate_storyboard_scenes(
        [
            {
                "scene_id": "sc_001",
                "title": "第 1 幕",
                "panel_ids": ["p1", "p3"],
                "visual_recap": "旧摘要",
            }
        ],
        {"p1", "p2", "p3"},
        existing_scenes=existing,
    )
    assert scenes[0].visual_recap == ""


def test_validate_storyboard_scenes_keeps_user_rewritten_recap():
    """用户主动重写过摘要 → 保留（不能一律清空）。"""
    existing = [_scene("sc_001", ["p1", "p2"], index=1, visual_recap="旧摘要")]
    scenes = _validate_storyboard_scenes(
        [
            {
                "scene_id": "sc_001",
                "title": "第 1 幕",
                "panel_ids": ["p1", "p3"],
                "visual_recap": "我新写的摘要",
            }
        ],
        {"p1", "p2", "p3"},
        existing_scenes=existing,
    )
    assert scenes[0].visual_recap == "我新写的摘要"


def test_validate_storyboard_scenes_keeps_recap_when_panels_unchanged():
    """分镜组合没变 → 摘要原样保留。"""
    existing = [_scene("sc_001", ["p1", "p2"], index=1, visual_recap="摘要")]
    scenes = _validate_storyboard_scenes(
        [
            {
                "scene_id": "sc_001",
                "title": "第 1 幕",
                "panel_ids": ["p1", "p2"],
                "visual_recap": "摘要",
            }
        ],
        {"p1", "p2"},
        existing_scenes=existing,
    )
    assert scenes[0].visual_recap == "摘要"


def test_merge_visual_recap_into_human_storyboard_only_fills_empty(
    tmp_path: Path,
):
    """人工编排回写：只补空，绝不覆盖用户已写的摘要，也不动其它字段。"""
    storyboard_path = tmp_path / "storyboard.json"
    disk = EpisodeStoryboard(
        episode_id="ep_001",
        source="human",
        scenes=[
            _scene("sc_001", ["p1"], index=1, visual_recap=""),
            _scene("sc_002", ["p2"], index=2, visual_recap="用户自己写的"),
        ],
    )
    s3b_scenes.save_storyboard(disk, storyboard_path)

    final = EpisodeStoryboard(
        episode_id="ep_001",
        source="human",
        scenes=[
            _scene("sc_001", ["p1"], index=1, visual_recap="模型复核摘要"),
            _scene("sc_002", ["p2"], index=2, visual_recap="模型另一份摘要"),
        ],
    )

    merged = s4_scriptwriter._merge_visual_recap_into_human_storyboard(
        final, storyboard_path
    )
    assert merged is not None
    assert merged.source == "human"
    by_id = {scene.scene_id: scene for scene in merged.scenes}
    assert by_id["sc_001"].visual_recap == "模型复核摘要"
    # 用户写过的摘要不被覆盖。
    assert by_id["sc_002"].visual_recap == "用户自己写的"
    # 分镜结构不受影响。
    assert by_id["sc_001"].panel_ids == ["p1"]
    assert by_id["sc_002"].panel_ids == ["p2"]


def test_merge_visual_recap_into_human_storyboard_returns_none_when_nothing_to_fill(
    tmp_path: Path,
):
    """磁盘上摘要都非空 → 无需落盘，返回 None。"""
    storyboard_path = tmp_path / "storyboard.json"
    disk = EpisodeStoryboard(
        episode_id="ep_001",
        source="human",
        scenes=[_scene("sc_001", ["p1"], index=1, visual_recap="已有")],
    )
    s3b_scenes.save_storyboard(disk, storyboard_path)

    final = EpisodeStoryboard(
        episode_id="ep_001",
        source="human",
        scenes=[_scene("sc_001", ["p1"], index=1, visual_recap="模型摘要")],
    )
    assert (
        s4_scriptwriter._merge_visual_recap_into_human_storyboard(
            final, storyboard_path
        )
        is None
    )


# ---------------------------------------------------------------------------
# 场景 4b：编排范围收敛到启用幕（端到端级：面板池 + 幕一致性）
# ---------------------------------------------------------------------------


def test_vision_panels_and_storyboard_stay_consistent_after_convergence():
    """收敛后的分镜池与启用幕引用的分镜集合必须完全一致。"""
    panels = [_panel(f"p{i}") for i in range(1, 6)]
    storyboard = EpisodeStoryboard(
        episode_id="ep_001",
        source="human",
        scenes=[
            _scene("sc_001", ["p1", "p2"], index=1),
            _scene("sc_002", ["p4"], index=2),
            _scene("sc_003", ["p5"], index=3, enabled=False),
        ],
    )
    active_scenes = [scene for scene in storyboard.scenes if scene.enabled]
    vision = VisionWindowOutput(panels=panels)

    valid_ids, ordered_ids, _panel_map, story_panels = s4_scriptwriter._converge_panel_pool(
        panels=panels,
        all_panels=list(vision.panels),
        resolved_storyboard=storyboard,
        active_scenes=active_scenes,
        scene_mode=True,
    )

    enabled_ids = {
        panel_id
        for scene in active_scenes
        for panel_id in (scene.panel_ids or [])
    }
    assert valid_ids == enabled_ids
    assert set(ordered_ids) == enabled_ids
    assert {panel.panel_id for panel in story_panels} <= enabled_ids


# ---------------------------------------------------------------------------
# 场景 5：4A 章数与启用幕数不符 → 定向反馈 + 重试一次
# ---------------------------------------------------------------------------


def _scene_count_feedback_mentions_scene_count_and_bounds():
    """反馈文本必须写清期望幕数与每幕的分镜边界（模型据此纠正章数）。"""
    active_scenes = [
        _scene("sc_001", ["p_001", "p_002"], index=1),
        _scene("sc_002", ["p_003"], index=2),
    ]
    feedback = s4_scriptwriter._scene_count_feedback(
        active_scenes, 3, ["p_001", "p_002", "p_003"]
    )
    assert "分幕数目校验失败" in feedback
    assert "3 章" in feedback
    assert "2 幕" in feedback
    assert "p_001 ~ p_002" in feedback
    assert "p_003" in feedback


def _vision_panel(panel_id: str, summary: str) -> dict:
    return {
        "panel_id": panel_id,
        "scene_summary": summary,
        "dialogues": [
            {"speaker_guess": "小白", "text": "一句台词。", "emotion": "tense"}
        ],
        "sfx_list": [],
        "is_transition_blank": False,
        "is_pure_text_card": False,
        "non_story_type": "none",
    }


def _make_series(tmp_path: Path) -> tuple[Path, Path]:
    """铺一套最小可跑的「话次」：故事圣经 + 视觉结果 + 两幕人工编排。"""
    series_dir = tmp_path / "workspace" / "wt_v429"
    series_dir.mkdir(parents=True)
    bible = StoryBibleV2(
        series_id="wt_v429",
        series_title="对齐测试",
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
                    _vision_panel("p_004", "客厅里，恩彗攥紧了拳头。"),
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
            episode_id="wt_v429_ep_002",
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
                    panel_ids=["p_003", "p_004"],
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
            "episode_id": "wt_v429_ep_002",
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
                    "beat_id": "b1",
                    "narration_text": f"{panel_id} 的解说句要写够十个字以上。",
                    "target_panel_ids": [panel_id],
                    "motion": "static",
                    "pause_after_ms": 200,
                }
                for panel_id in panel_ids
            ]
        },
        ensure_ascii=False,
    )


@pytest.mark.anyio
@respx.mock
async def test_stage4_retries_4a_when_chapter_count_mismatches_scenes(tmp_path):
    """4A 章数与启用幕数不符 → 带定向反馈重试一次 4A。

    旧实现只跑一次 4A，章数错了就一路错到 4B：解说被挂到错误的幕上，
    成片里出现「讲 A 幕、配 B 幕画面」。
    """
    series_dir, board_path = _make_series(tmp_path)
    story_prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        prompt = body["messages"][-1]["content"]
        if "story_prose" in prompt:
            story_prompts.append(prompt)
            if len(story_prompts) == 1:
                # 第一次：3 章 ≠ 2 幕。
                return _llm_response(
                    _story_payload(
                        [("p_001", "p_002"), ("p_003", "p_003"), ("p_004", "p_004")]
                    )
                )
            return _llm_response(
                _story_payload([("p_001", "p_002"), ("p_003", "p_004")])
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
        script, script_path = await s4_scriptwriter.run_stage4(
            series_dir,
            2,
            client,
            model="qwen-plus",
            episode_id="wt_v429_ep_002",
            series_title="对齐测试",
            storyboard_path=board_path,
        )

    assert script_path.is_file()
    # 4A 跑了两次：第一次章数不对，被定向反馈后重试。
    assert len(story_prompts) == 2
    assert "分幕数目校验失败" in story_prompts[1]
    # 最终剧本的每一句都落在启用幕的分镜上。
    used = {
        panel_id
        for beat in script.beats
        for panel_id in beat.target_panel_ids
    }
    assert used <= {"p_001", "p_002", "p_003", "p_004"}
