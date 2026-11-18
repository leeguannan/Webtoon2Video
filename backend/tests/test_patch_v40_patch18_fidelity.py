"""Patch v4.0 Patch 18：识图纠偏 + 暗黑高潮格护栏 + 引号误拆修复 + 混音调优。

覆盖规格里的 4 块修复：

1. **识图纠偏**（``prompts/vision_extract.md``）：气泡尾巴认人铁律、动作耦合
   拟声词必须写进 ``scene_summary``、暗黑高潮格禁止标成空页 / 纯字卡 /
   纯拟声格；``s3_vision.protect_dramatic_panels`` 在落盘前兜底把这类格救回来。
2. **空白页保护**（``s2_segmenter.is_blank_panel``）：深黑底 + 高亮白色对白框的
   高潮剧情格不能被当成纯黑空白页切掉。
3. **引号误拆修复**（``s4_scriptwriter.split_mixed_quote_beats``）：叙述句里作
   名词 / 代称的引号短语（「这个“孤儿”」）不再被劈成支离破碎的三条 Beat，
   真实引导句拆分行为保持不变。
4. **混音调优**：BGM / 音效增益与侧链参数按新默认值落地。

全部离线：不触网、不调 ffmpeg。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from core.config import RenderConfig
from core.schemas import CharacterProfile, DialogueBubble, PanelVisionInfo
from stages import s2_segmenter, s3_vision, s4_scriptwriter, s7_renderer

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROMPT_PATH = BACKEND_DIR / "prompts" / "vision_extract.md"
EXAMPLE_CONFIG_PATH = BACKEND_DIR / "config.example.yaml"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _character(name: str, gender: str = "unknown") -> CharacterProfile:
    return CharacterProfile(
        name=name,
        visual_traits="黑色短发，眼神锐利",
        current_status="在场",
        first_seen_ep=1,
        last_seen_ep=1,
        gender=gender,
    )


def _beat(
    narration_text: str,
    *,
    beat_id: str = "b1",
    speaker_role: str = "narrator",
    speaker_gender: str = "unknown",
    tts_text: str | None = None,
    pause_after_ms: int | None = None,
    panel_ids: list[str] | None = None,
) -> dict:
    beat: dict = {
        "beat_id": beat_id,
        "narration_text": narration_text,
        "tts_text": tts_text,
        "emotion_instruction": "用自然、流畅且有代入感的漫评解说语气说出",
        "speaker_role": speaker_role,
        "speaker_gender": speaker_gender,
        "target_panel_ids": panel_ids or ["p_001"],
        "motion": "zoom_in",
        "focus_hint": "full",
    }
    if pause_after_ms is not None:
        beat["pause_after_ms"] = pause_after_ms
    return beat


def _panel(
    panel_id: str,
    *,
    scene_summary: str = "",
    dialogues: list[DialogueBubble] | None = None,
    is_transition_blank: bool = False,
    is_pure_text_card: bool = False,
    non_story_type: str = "none",
) -> PanelVisionInfo:
    return PanelVisionInfo(
        panel_id=panel_id,
        scene_summary=scene_summary,
        dialogues=dialogues or [],
        is_transition_blank=is_transition_blank,
        is_pure_text_card=is_pure_text_card,
        non_story_type=non_story_type,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# 1. 引号误拆修复：句中引用词不再被劈碎
# ---------------------------------------------------------------------------


def test_embedded_quote_word_stays_single_narrator_beat() -> None:
    """叙述句里的「孤儿」是代称不是台词 → 保持一条 narrator Beat。"""
    beats = s4_scriptwriter.split_mixed_quote_beats(
        [_beat("继母恶毒地让他们带着这个“孤儿”立刻离开。")]
    )
    assert len(beats) == 1
    assert beats[0]["speaker_role"] == "narrator"
    assert beats[0]["speaker_gender"] == "unknown"
    # 双引号保留给字幕，不能把词拆出去。
    assert beats[0]["narration_text"] == "继母恶毒地让他们带着这个“孤儿”立刻离开。"


def test_spoken_quote_with_lead_verb_still_splits_into_two() -> None:
    """真实引导句仍然拆成「旁白引导 + 角色台词」两条。"""
    beats = s4_scriptwriter.split_mixed_quote_beats(
        [_beat("道振平静说道：“别怕，有我在。”")],
        characters=[_character("道振", "male")],
    )
    assert len(beats) == 2
    narrator, dialogue = beats
    assert narrator["narration_text"] == "道振平静说道："
    assert narrator["speaker_role"] == "narrator"
    assert dialogue["narration_text"] == "“别怕，有我在。”"
    assert dialogue["speaker_role"] == "道振"
    assert dialogue["speaker_gender"] == "male"


def test_spoken_quote_without_registry_still_splits_into_two() -> None:
    """没有角色表时也能从引导词认出说话人并拆开。"""
    beats = s4_scriptwriter.split_mixed_quote_beats(
        [_beat("继母怒骂：“混账！”")]
    )
    assert len(beats) == 2
    assert beats[0]["speaker_role"] == "narrator"
    assert beats[1]["speaker_role"] == "继母"
    assert beats[1]["narration_text"] == "“混账！”"


def test_embedded_quote_word_mixed_with_spoken_quote() -> None:
    """同一句里既有台词又有句中引用词 → 只拆出真正的台词。"""
    beats = s4_scriptwriter.split_mixed_quote_beats(
        [_beat("继母指着恩彗鼻子骂：“我看到你们就倒胃口！”然后她指着那个“孤儿”走开。")]
    )
    assert len(beats) == 3
    assert beats[0]["speaker_role"] == "narrator"
    assert beats[1]["speaker_role"] == "继母"
    assert beats[1]["narration_text"] == "“我看到你们就倒胃口！”"
    assert beats[2]["speaker_role"] == "narrator"
    assert "“孤儿”" in beats[2]["narration_text"]


def test_is_spoken_quote_segment_contract() -> None:
    """_is_spoken_quote_segment 的三条充分条件。"""
    is_spoken = s4_scriptwriter._is_spoken_quote_segment
    # ① 整条几乎就是纯引号台词。
    assert is_spoken("别怕，有我在。", "", "", full_lead_in_len=0) is True
    # ② 引号前是引导词（说话动词 / 心理动词 + 引导标点）。
    assert is_spoken("别怕。", "道振平静说道：", "", full_lead_in_len=8) is True
    assert is_spoken("这地方不对劲", "他心想：", "，随后退了一步。", full_lead_in_len=12) is True
    # ③ 引号内容自成一句完整的话。
    assert is_spoken("就算我死了。", "他只留下一句冰冷判决：", "", full_lead_in_len=12) is True
    # 句中嵌入的引用词：没有引导词、字数不够、后面还接着同一句正文。
    assert is_spoken("孤儿", "继母恶毒地让他们带着这个", "立刻离开。", full_lead_in_len=17) is False


def test_psychological_lead_keeps_own_quote_beat() -> None:
    """心理独白仍保留独立引号 Beat，且留在旁白声线（Patch 17 契约不变）。"""
    beats = s4_scriptwriter.split_mixed_quote_beats(
        [_beat("他心想：“这地方不对劲”，随后退了一步。")]
    )
    quote_beats = [
        beat
        for beat in beats
        if beat["narration_text"].strip().startswith("“")
        and beat["narration_text"].strip().endswith("”")
    ]
    assert quote_beats, "心理独白的引号内容应被拆成独立 beat"
    assert all(beat["speaker_role"] == "narrator" for beat in quote_beats)


# ---------------------------------------------------------------------------
# 2. 暗黑高潮格护栏
# ---------------------------------------------------------------------------


def test_protect_dramatic_panels_restores_dark_climax_panel() -> None:
    """暗底红光 + 有效台词的高潮格被强制恢复成普通剧情格。"""
    panel = _panel(
        "p_003",
        scene_summary="深黑底色中一道血红警示光扫过，道振的脸在火光里半明半暗。",
        dialogues=[DialogueBubble(speaker_guess="道振", text="你们谁都别想走！")],
        is_transition_blank=True,
        is_pure_text_card=True,
        non_story_type="sfx_only",
    )
    protected = s3_vision.protect_dramatic_panels([panel])
    assert len(protected) == 1
    restored = protected[0]
    assert restored.is_transition_blank is False
    assert restored.is_pure_text_card is False
    assert restored.non_story_type == "none"


def test_protect_dramatic_panels_restores_panel_by_scene_summary_only() -> None:
    """没有台词、但写明了人物与动作的高潮格也要救回来。"""
    panel = _panel(
        "p_004",
        scene_summary="继母抬手扇了道振一耳光，道振脸颊瞬间泛起红印。",
        is_transition_blank=True,
        non_story_type="sfx_only",
    )
    restored = s3_vision.protect_dramatic_panels([panel])[0]
    assert restored.is_transition_blank is False
    assert restored.is_pure_text_card is False
    assert restored.non_story_type == "none"


def test_protect_dramatic_panels_keeps_banner_and_sfx_panels() -> None:
    """版权页 / 推广页 / 纯拟声格不动（不能把它们救成剧情格）。"""
    banner = _panel(
        "p_001",
        scene_summary="本漫画版权归作者所有，禁止转载。",
        is_transition_blank=False,
        non_story_type="copyright_notice",
    )
    sfx_only = _panel(
        "p_002",
        scene_summary="一道血红闪电劈过夜空。",
        dialogues=[DialogueBubble(speaker_guess="", text="啪！")],
        non_story_type="sfx_only",
    )
    protected = s3_vision.protect_dramatic_panels([banner, sfx_only])
    assert protected[0].non_story_type == "copyright_notice"
    assert protected[1].non_story_type == "sfx_only"


# ---------------------------------------------------------------------------
# 3. 空白页保护：深黑底 + 高亮对白框
# ---------------------------------------------------------------------------


def test_is_blank_panel_keeps_dark_panel_with_bright_dialogue_box() -> None:
    """深黑底 + 一小块高亮白框：全局指标像空白页，但局部有结构 → 有效画面。"""
    gray = np.zeros((400, 800), dtype=np.uint8)
    gray[200:224, 400:424] = 255  # 高亮白色对白框

    # 全局指标确实「像空白页」，否则这条用例测不到 Patch 18 的新判据。
    mid = float(((gray > 40) & (gray < 215)).mean())
    edges = float(s2_segmenter.canny_edge_density(gray).mean())
    assert mid < s2_segmenter.BLANK_PANEL_INK_RATIO
    assert edges < s2_segmenter.BLANK_PANEL_EDGE_RATIO

    assert s2_segmenter.is_blank_panel(gray) is False


def test_is_blank_panel_still_flags_pure_black() -> None:
    """纯黑页仍然判成空白页（保护判据不能滥伤）。"""
    gray = np.zeros((300, 300), dtype=np.uint8)
    assert s2_segmenter.is_blank_panel(gray) is True


# ---------------------------------------------------------------------------
# 4. 混音调优：增益与侧链参数
# ---------------------------------------------------------------------------


def test_render_config_defaults_match_patch18_gains() -> None:
    render = RenderConfig()
    assert render.bgm_gain_db == -8.0
    assert render.sfx_gain_db == -2.0


def test_example_config_matches_render_defaults() -> None:
    text = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
    assert "bgm_gain_db: -8.0" in text
    assert "sfx_gain_db: -2.0" in text


def test_renderer_sidechain_and_sfx_defaults_match_patch18() -> None:
    assert s7_renderer._SIDECHAIN_THRESHOLD == 0.06
    assert s7_renderer._SIDECHAIN_RATIO == 2.0
    assert s7_renderer._SIDECHAIN_ATTACK_MS == 30
    assert s7_renderer._SIDECHAIN_RELEASE_MS == 350
    assert s7_renderer.DEFAULT_SFX_GAIN_DB == -2.0


# ---------------------------------------------------------------------------
# 5. 识图 prompt：尾巴认人 + 拟声词写进 scene_summary + 暗黑高潮格
# ---------------------------------------------------------------------------


def test_vision_prompt_requires_tail_tracking_and_dramatic_guard() -> None:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    assert "尾巴" in text
    assert "scene_summary" in text
    assert "is_transition_blank" in text
    assert "is_pure_text_card" in text
    assert "sfx_only" in text
