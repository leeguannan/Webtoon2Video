"""Patch v4.20：纯文字卡不再进成片/分镜页 + 关系称谓不再被改回角色名。

两条主线（对应实测的两个用户反馈）：

1. **成片里多出一帧画面**——实测 wt_5081 第 4 话 b9 与 b10 之间多出一条
   690×55 的粉色章节标题条（``p_007``）。链路是：Stage 2 没把它判成装饰条 →
   Stage 3 也没标纯字卡 → ``protect_dramatic_panels`` 因为「有 ≥3 字台词」
   把它当高潮格救回 → Stage 4 把它当候选分镜挂到 b9 → Stage 6 把 b9 的时长
   拆成两帧，于是标题条夹在 b9 与 b10 中间。本文件锁死：
   纯文字卡不进候选池、不进分镜页、不被高潮格护栏救回。
2. **改字幕保存后不生效**——角色「道振」的别名里有「丈夫」，角色名归一把
   用户新写的「丈夫」改回「道振」，保存后与原文逐字相同，刷新仍是原文。
   本文件锁死：关系 / 身份泛称不进正文规则（但角色位仍然归一）。

全部离线：不触网、不依赖 ffmpeg。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.character_unify import build_character_canonicalizer
from core.config import AppConfig, WorkspaceConfig
from core.schemas import (
    CharacterProfile,
    EpisodeDigest,
    EpisodeStoryboard,
    NarrationScript,
    PanelVisionInfo,
    ScriptBeat,
    StoryBibleV2,
    StoryboardScene,
    VisionWindowOutput,
    load_model,
    save_model,
)
from stages import s0_catalog, s3_vision, s3b_scenes, s4_scriptwriter
from web.job_runner import JobRunner
from web.server import create_app

SERIES_ID = "wt_v420"

#: 实测 wt_5081 第 4 话的粉色章节标题条（b9 与 b10 之间多出的那一帧）。
_PINK_TITLE_STRIP = "浅粉色背景上显示一行黑体中文文字，内容为'在醫院裡看到的道振…'，无角色、无动作、无气泡。"
#: 同一话里真正的剧情格（有人物与动作）。
_REAL_SCENE = "恩彗站在病房窗前，手里攥着信纸，眼神发抖。"
#: wt_3924 那种「纯白背景 + 对话气泡」的画风：没有人物，但气泡是画出来的。
_WHITE_BALLOON = "纯白背景上一个椭圆形对话气泡，内含文字提问。"
#: Patch v4.0 Patch 18 的深黑底火光高潮格（护栏必须继续救回它）。
_DARK_CLIMAX = "深黑底色中一道血红警示光扫过，道振的脸在火光里半明半暗。"
#: 角色的内心独白卡：有角色归属，不该被当成「无人剧情的纯字卡」剔除。
_CHARACTER_QUOTE_CARD = "灰色背景上一个白底黑字矩形框，框内为内心独白文字，无角色形象或动作。"
#: 真实插画 + 角落一个文字框（wt_3924 p_070）：不能被当成纯字卡。
_ILLUSTRATION_WITH_CAPTION = (
    "特写一名男性腹部与腰际，上衣掀起露出布满纹身的腹肌与一只窥视的眼睛；"
    "画面下方有角色介绍文字框。"
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
    non_story_type: str = "none",
    pure_text_card: bool = False,
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
        "is_pure_text_card": pure_text_card,
        "non_story_type": non_story_type,
    }


def _vision(*panels: dict[str, Any]) -> VisionWindowOutput:
    return VisionWindowOutput.model_validate({"panels": list(panels)})


def _info(panel_id: str, summary: str, **kwargs: Any) -> PanelVisionInfo:
    return PanelVisionInfo.model_validate(_panel(panel_id, summary, **kwargs))


def _character(name: str, *, aliases: Sequence[str] = ()) -> CharacterProfile:
    return CharacterProfile(
        name=name,
        aliases=list(aliases),
        visual_traits="",
        current_status="",
        first_seen_ep=1,
        last_seen_ep=1,
    )


# ---------------------------------------------------------------------------
# A. 纯文字卡判定
# ---------------------------------------------------------------------------
def test_flat_title_strip_is_text_only_card() -> None:
    """粉色章节标题条：画面上只有一行字 → 纯文字卡。"""
    panel = _info("p_007", _PINK_TITLE_STRIP, dialogue="在醫院裡看到的道振…", speaker="旁白")
    assert s3_vision.is_text_only_card(panel) is True


def test_speech_bubble_panel_is_not_text_only_card() -> None:
    """画了对话气泡的格子是正式画面，不能按纯字卡剔除。"""
    panel = _info("p_112", _WHITE_BALLOON, dialogue="这是谁？", speaker="旁白")
    assert s3_vision.is_text_only_card(panel) is False


def test_dark_climax_panel_is_not_text_only_card() -> None:
    """Patch v4.0 Patch 18 的深黑底火光高潮格不能被当成纯字卡。"""
    panel = _info("p_018", _DARK_CLIMAX, dialogue="别过来！", speaker="道振")
    assert s3_vision.is_text_only_card(panel) is False


def test_character_quote_card_is_not_text_only_card() -> None:
    """角色内心独白卡有角色归属，保留（它的台词是角色说的）。"""
    panel = _info("p_005", _CHARACTER_QUOTE_CARD, dialogue="我明明……", speaker="恩彗(内心独白)")
    assert s3_vision.is_text_only_card(panel) is False


def test_illustration_with_caption_box_is_not_text_only_card() -> None:
    """真实插画 + 角落文字框不能被当成纯字卡。"""
    panel = _info("p_070", _ILLUSTRATION_WITH_CAPTION, dialogue=None)
    assert s3_vision.is_text_only_card(panel) is False


def test_regular_scene_panel_is_not_text_only_card() -> None:
    panel = _info("p_006", _REAL_SCENE, dialogue="而且还是被一位大美女告白…", speaker="旁白")
    assert s3_vision.is_text_only_card(panel) is False


def test_on_screen_text_speaker_recognises_narration() -> None:
    assert s3_vision.is_on_screen_text_speaker("旁白") is True
    assert s3_vision.is_on_screen_text_speaker("字幕") is True
    assert s3_vision.is_on_screen_text_speaker("道振") is False
    assert s3_vision.is_on_screen_text_speaker("恩彗(内心独白)") is False


# ---------------------------------------------------------------------------
# B. 高潮格护栏：纯字卡不救回，深黑火光格照旧救回
# ---------------------------------------------------------------------------
def test_protect_dramatic_does_not_rescue_text_only_card() -> None:
    """纯字卡不能被「有 ≥3 字台词」这条判据救回成剧情格。"""
    strip = _info("p_007", _PINK_TITLE_STRIP, dialogue="在醫院裡看到的道振…", speaker="旁白")
    protected = s3_vision.protect_dramatic_panels([strip])
    assert len(protected) == 1
    assert protected[0].is_pure_text_card is True
    assert protected[0].is_transition_blank is False
    assert protected[0].non_story_type == "none"


def test_protect_dramatic_still_rescues_dark_climax_panel() -> None:
    """Patch v4.0 Patch 18 红线：深黑底火光高潮格仍要救回成普通剧情格。"""
    climax = _info(
        "p_018",
        _DARK_CLIMAX,
        dialogue="别过来！",
        speaker="道振",
        blank=True,
        pure_text_card=True,
    )
    protected = s3_vision.protect_dramatic_panels([climax])
    assert len(protected) == 1
    assert protected[0].is_transition_blank is False
    assert protected[0].is_pure_text_card is False
    assert protected[0].non_story_type == "none"


# ---------------------------------------------------------------------------
# C. Stage 3b：分镜页不再列出纯文字卡
# ---------------------------------------------------------------------------
def test_auto_storyboard_excludes_text_only_cards() -> None:
    """自动切幕时，章节标题条不该进任何一幕。"""
    panels = [
        _panel(f"p_{index:03d}", f"病房里，小白攥紧信纸，第 {index} 格。")
        for index in range(1, 7)
    ]
    panels.append(_panel("p_007", _PINK_TITLE_STRIP, dialogue="在醫院裡看到的道振…", speaker="旁白"))
    panels.extend(
        _panel(f"p_{index:03d}", f"客厅里两人对峙，第 {index} 格。")
        for index in range(8, 13)
    )
    storyboard = s3b_scenes.auto_build_storyboard("wt_v420_ep_001", _vision(*panels))
    used = {panel_id for scene in storyboard.scenes for panel_id in scene.panel_ids}
    assert "p_007" not in used
    assert "p_001" in used


def test_strip_text_cards_from_storyboard_keeps_human_edits() -> None:
    """人工编排（source="human"）绝不被剔除纯字卡。"""
    vision = _vision(_panel("p_001", _REAL_SCENE))
    human = EpisodeStoryboard(
        episode_id="wt_v420_ep_001",
        source="human",
        scenes=[
            StoryboardScene(
                scene_id="sc_001",
                scene_index=1,
                title="人工编排",
                panel_ids=["p_001", "p_007"],
            )
        ],
    )
    stripped = s3b_scenes.strip_text_cards_from_storyboard(human, vision)
    assert stripped.scenes[0].panel_ids == ["p_001", "p_007"]
    assert stripped is human


def test_strip_text_cards_from_storyboard_removes_auto_cards() -> None:
    """自动编排里混进的纯字卡要被剔除（幕被清空则关掉该幕）。"""
    vision = _vision(
        _panel("p_001", _REAL_SCENE),
        _panel("p_007", _PINK_TITLE_STRIP, dialogue="在醫院裡看到的道振…", speaker="旁白"),
    )
    auto = EpisodeStoryboard(
        episode_id="wt_v420_ep_001",
        source="auto",
        scenes=[
            StoryboardScene(
                scene_id="sc_001",
                scene_index=1,
                title="第 1 幕 · 病房",
                panel_ids=["p_001", "p_007"],
            )
        ],
    )
    stripped = s3b_scenes.strip_text_cards_from_storyboard(auto, vision)
    assert stripped.scenes[0].panel_ids == ["p_001"]


# ---------------------------------------------------------------------------
# D. Stage 4：候选池剔除纯文字卡
# ---------------------------------------------------------------------------
def test_stage4_candidate_pool_drops_content_detected_text_cards() -> None:
    """Stage 4 的候选分镜池不能把纯字卡喂给模型。"""
    panels = [
        _info("p_001", _REAL_SCENE),
        _info("p_007", _PINK_TITLE_STRIP, dialogue="在醫院裡看到的道振…", speaker="旁白"),
    ]
    kept = s4_scriptwriter.exclude_pure_text_cards(panels)
    assert [panel.panel_id for panel in kept] == ["p_001"]


def test_stage4_candidate_pool_keeps_everything_when_all_are_cards() -> None:
    """整批都是纯字卡时兜底全保留（不能切出空的候选池）。"""
    panels = [
        _info("p_007", _PINK_TITLE_STRIP, dialogue="在醫院裡看到的道振…", speaker="旁白")
    ]
    kept = s4_scriptwriter.exclude_pure_text_cards(panels)
    assert [panel.panel_id for panel in kept] == ["p_007"]


# ---------------------------------------------------------------------------
# E. 角色名归一：关系 / 身份泛称不进正文
# ---------------------------------------------------------------------------
def test_relation_alias_is_not_rewritten_in_body_text() -> None:
    """「丈夫」是关系称谓，不是「道振」的另一种写法，正文不能被改回去。"""
    canonicalizer = build_character_canonicalizer(
        characters=[_character("道振", aliases=["丈夫", "南道振"])],
        series_tts=None,
        series_aliases=None,
    )
    text = "真没想到我会亲眼看见我未来的丈夫被告白的场面。"
    assert canonicalizer.canonicalize_text(text) == text
    # 人名变体仍然归一
    assert canonicalizer.canonicalize_text("南道振走进病房") == "道振走进病房"
    # 关系称谓也不该进正文规则
    assert ("丈夫", "道振") not in canonicalizer.text_rules


def test_relation_alias_still_mapped_in_role_slot() -> None:
    """角色位照旧归一：说「丈夫」时指的是「道振」。"""
    canonicalizer = build_character_canonicalizer(
        characters=[_character("道振", aliases=["丈夫", "南道振"])],
        series_tts=None,
        series_aliases=None,
    )
    assert canonicalizer.canonicalize_role("丈夫") == "道振"
    assert canonicalizer.canonicalize_role("道振") == "道振"


def test_homophone_variant_still_unified_after_relation_fix() -> None:
    """v4.16 的同音归一不能被这次修复误伤。"""
    canonicalizer = build_character_canonicalizer(
        characters=[_character("恩彗", aliases=["恩慧"])],
        series_tts=None,
        series_aliases=None,
    )
    assert canonicalizer.canonicalize_role("恩慧") == "恩彗"
    assert canonicalizer.canonicalize_text("恩慧推门而入") == "恩彗推门而入"


# ---------------------------------------------------------------------------
# F. Web API：人工保存不再把「丈夫」改回「道振」
# ---------------------------------------------------------------------------
def _register(root: Path) -> tuple[str, Path, AppConfig]:
    src = root / "src"
    ep_src = src / "ep_001"
    ep_src.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 600), (40, 80, 120)).save(ep_src / "001.png")
    catalog = s0_catalog.import_local_catalog(src)
    s0_catalog.CatalogRegistry(root).register(catalog)
    config = AppConfig(workspace=WorkspaceConfig(root=root))
    return catalog.series_id, root / catalog.series_id, config


def _write_script(series_dir: Path, beats: Sequence[ScriptBeat]) -> Path:
    script_dir = series_dir / "episodes" / "ep_001" / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    script = NarrationScript(
        episode_id=f"{series_dir.name}_ep_001",
        hook_title="雨夜来客",
        beats=list(beats),
        episode_digest=EpisodeDigest(
            episode_no=1,
            one_line_hook="雨夜有人来访",
            summary="来客自称银行行长。",
        ),
    )
    save_model(script, script_dir / "script.json")
    return script_dir / "script.json"


def _beat(beat_id: str, narration_text: str, **kwargs: Any) -> ScriptBeat:
    payload: dict[str, Any] = {
        "beat_id": beat_id,
        "narration_text": narration_text,
        "target_panel_ids": ["p_001"],
    }
    payload.update(kwargs)
    return ScriptBeat(**payload)


def test_put_script_keeps_relation_word(tmp_path: Path) -> None:
    """人工改稿保存后，「丈夫」必须原样落库（不能再被改回「道振」）。"""
    series_id, series_dir, config = _register(tmp_path)
    bible = StoryBibleV2(
        series_id=series_id,
        series_title="测试漫画",
        characters=[
            CharacterProfile(
                name="道振",
                aliases=["丈夫", "南道振"],
                visual_traits="黑发男性",
                current_status="平静",
                first_seen_ep=1,
                last_seen_ep=1,
            )
        ],
    )
    save_model(bible, series_dir / "story_bible.json")
    _write_script(
        series_dir,
        [_beat("b10", "真没想到我会亲眼看见我未来的道振被告白的场面。")],
    )

    runner = JobRunner(config, tts_factory=lambda: None)
    api = TestClient(create_app(config, runner=runner))
    response = api.put(
        f"/api/episodes/{series_id}_ep_001/script",
        json={
            "beats": [
                {
                    "beat_id": "b10",
                    "narration_text": "真没想到我会亲眼看见我未来的丈夫被告白的场面。",
                    "speaker_role": "道振",
                    "target_panel_ids": ["p_001"],
                    "pause_after_ms": 200,
                }
            ]
        },
    )
    assert response.status_code == 200
    saved = load_model(
        NarrationScript, series_dir / "episodes" / "ep_001" / "04_script" / "script.json"
    )
    assert saved.beats[0].narration_text == "真没想到我会亲眼看见我未来的丈夫被告白的场面。"


def test_put_script_still_unifies_person_name_variant(tmp_path: Path) -> None:
    """人名变体照旧归一（别把「南道振」也放过）。"""
    series_id, series_dir, config = _register(tmp_path)
    bible = StoryBibleV2(
        series_id=series_id,
        series_title="测试漫画",
        characters=[
            CharacterProfile(
                name="道振",
                aliases=["丈夫", "南道振"],
                visual_traits="黑发男性",
                current_status="平静",
                first_seen_ep=1,
                last_seen_ep=1,
            )
        ],
    )
    save_model(bible, series_dir / "story_bible.json")
    _write_script(series_dir, [_beat("b10", "南道振走进病房。")])

    runner = JobRunner(config, tts_factory=lambda: None)
    api = TestClient(create_app(config, runner=runner))
    response = api.put(
        f"/api/episodes/{series_id}_ep_001/script",
        json={
            "beats": [
                {
                    "beat_id": "b10",
                    "narration_text": "南道振走进病房。",
                    "speaker_role": "道振",
                    "target_panel_ids": ["p_001"],
                    "pause_after_ms": 200,
                }
            ]
        },
    )
    assert response.status_code == 200
    saved = load_model(
        NarrationScript, series_dir / "episodes" / "ep_001" / "04_script" / "script.json"
    )
    assert saved.beats[0].narration_text == "道振走进病房。"
    assert saved.beats[0].speaker_role == "道振"


# --------------------------------------------------------------------------
# Stage 6 兜底：纯文字卡永远不进成片
# --------------------------------------------------------------------------
def test_stage6_never_renders_text_card_panel(tmp_path: Path) -> None:
    """旧脚本里残留的纯字卡引用，Stage 6 也要挡住。

    实测 wt_5081 第 4 话：b9 的 ``target_panel_ids`` 里残留了粉色章节标题条
    ``p_007``，Stage 6 把 b9 的 957ms 拆成 ``p_006`` + ``p_007`` 两帧，
    于是在 b9 与 b10 之间多出一帧文字条。Stage 4 已经不再产出这种引用，
    这里锁死 Stage 6 的兜底：老脚本重跑时间轴也不会多出那一帧。
    """
    from stages import s6_timeline

    series_dir = tmp_path / "series"
    episode_dir = series_dir / "episodes" / "ep_001"
    panels_dir = episode_dir / "02_panels"
    panels_dir.mkdir(parents=True)
    for panel_id in ("p_001", "p_002"):
        Image.new("RGB", (120, 200), (30, 60, 90)).save(panels_dir / f"{panel_id}.png")

    save_model(
        VisionWindowOutput(
            panels=[
                PanelVisionInfo(panel_id="p_001", scene_summary=_REAL_SCENE),
                PanelVisionInfo(panel_id="p_002", scene_summary=_PINK_TITLE_STRIP),
            ]
        ),
        episode_dir / "03_vision" / "vision.json",
    )
    save_model(
        NarrationScript(
            episode_id="ep_001",
            hook_title="钩子标题",
            beats=[
                ScriptBeat(
                    beat_id="b9",
                    narration_text="她在心里嘀咕：",
                    target_panel_ids=["p_001", "p_002"],
                ),
                ScriptBeat(
                    beat_id="b10",
                    narration_text="真没想到我会亲眼看见我未来的丈夫被告白的场面。",
                    target_panel_ids=["p_001"],
                ),
            ],
            episode_digest=EpisodeDigest(
                episode_no=1, one_line_hook="钩子标题", summary="这一话的主线摘要"
            ),
        ),
        episode_dir / "04_script" / "script.json",
    )
    audio_dir = episode_dir / "05_audio"
    audio_dir.mkdir(parents=True)
    (audio_dir / "audio_manifest.json").write_text(
        json.dumps(
            [
                {
                    "beat_id": "b9",
                    "audio_path": "05_audio/b9.wav",
                    "duration_ms": 957,
                    "pause_after_ms": 150,
                },
                {
                    "beat_id": "b10",
                    "audio_path": "05_audio/b10.wav",
                    "duration_ms": 1000,
                    "pause_after_ms": 150,
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    timeline = s6_timeline.build_timeline_from_workspace(series_dir, 1)

    used_panels = [scene.panel_id for scene in timeline.scenes]
    assert "p_002" not in used_panels
    # b9 不再被拆成两帧（拆帧正是「b9 与 b10 之间多一帧」的成因）
    assert len([scene for scene in timeline.scenes if scene.beat_id == "b9"]) == 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
