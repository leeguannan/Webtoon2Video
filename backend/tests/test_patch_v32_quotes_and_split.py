"""Patch v3.2 Patch 12：中文双引号归一化 + 旁白/引号对白确定性拆分 + 纯字卡拦截。"""

from __future__ import annotations

from core.normalize import normalize_dialogue_quotes, normalize_script_payload
from core.schemas import (
    CharacterProfile,
    EpisodeDigest,
    NarrationScript,
    PanelVisionInfo,
    ScriptBeat,
)
from stages import s4_scriptwriter


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


# --------------------------------------------------------------------------------------
# 1. normalize_dialogue_quotes：引号形态归一化
# --------------------------------------------------------------------------------------


def test_normalize_dialogue_quotes_converts_ascii_single_quotes() -> None:
    assert (
        normalize_dialogue_quotes("道振平静说道：'别怕，有我在。'")
        == "道振平静说道：“别怕，有我在。”"
    )


def test_normalize_dialogue_quotes_converts_curly_single_quotes() -> None:
    assert (
        normalize_dialogue_quotes("道振平静说道：‘别怕，有我在。’")
        == "道振平静说道：“别怕，有我在。”"
    )


def test_normalize_dialogue_quotes_converts_english_double_quotes() -> None:
    assert normalize_dialogue_quotes('道振说:"别怕"') == "道振说：“别怕”"
    assert normalize_dialogue_quotes("道振说：“别怕”") == "道振说：“别怕”"


def test_normalize_dialogue_quotes_converts_standalone_quoted_line() -> None:
    assert normalize_dialogue_quotes("'别怕，有我在。'") == "“别怕，有我在。”"
    assert normalize_dialogue_quotes("‘别怕，有我在。’") == "“别怕，有我在。”"


def test_normalize_dialogue_quotes_keeps_english_apostrophes() -> None:
    """英文缩写里的单引号不能被当成对话引号改写。"""
    original = "I'm fine, don't worry."
    assert normalize_dialogue_quotes(original) == original


def test_normalize_script_payload_normalizes_quotes_on_both_fields() -> None:
    payload = {
        "hook_title": "钩子",
        "episode_digest": {"episode_no": 1, "one_line_hook": "钩子", "summary": "概述"},
        "beats": [
            _beat(
                "道振平静说道：'别怕，有我在。'",
                tts_text="道振平静说道：'别怕，有我在。'",
            )
        ],
    }
    normalized = normalize_script_payload(payload, episode_id="ep_001", episode_no=1)
    beat = normalized["beats"][0]
    assert beat["narration_text"] == "道振平静说道：“别怕，有我在。”"
    assert beat["tts_text"] == "道振平静说道：“别怕，有我在。”"


def test_normalize_script_payload_converges_speaker_gender() -> None:
    payload = {
        "hook_title": "钩子",
        "episode_digest": {"episode_no": 1, "one_line_hook": "钩子", "summary": "概述"},
        "beats": [
            _beat("她抬起头看向窗外。", speaker_gender="female"),
            _beat("他转身走出门外。", speaker_gender="not-a-gender"),
        ],
    }
    normalized = normalize_script_payload(payload, episode_id="ep_001", episode_no=1)
    genders = [beat["speaker_gender"] for beat in normalized["beats"]]
    assert genders == ["female", "unknown"]


# --------------------------------------------------------------------------------------
# 2. split_mixed_quote_beats：确定性拆分「旁白 + 引号对白」
# --------------------------------------------------------------------------------------


def test_split_mixed_quote_beats_splits_narration_and_dialogue() -> None:
    """规格验收 1：'道振平静说道：别怕，有我在。' 必拆成两条连续 Beat。"""
    beats = s4_scriptwriter.split_mixed_quote_beats(
        [_beat("道振平静说道：'别怕，有我在。'")],
        characters=[_character("道振", "male")],
    )
    assert len(beats) == 2

    narrator, dialogue = beats
    assert narrator["narration_text"] == "道振平静说道："
    assert narrator["speaker_role"] == "narrator"
    assert narrator["speaker_gender"] == "unknown"
    assert narrator["pause_after_ms"] == 60

    assert dialogue["narration_text"] == "“别怕，有我在。”"
    assert dialogue["tts_text"] == "别怕，有我在。"
    assert dialogue["speaker_role"] == "道振"
    assert dialogue["speaker_gender"] == "male"

    # 两条共享同一组分镜，画面不跳；beat_id 重新编号。
    assert narrator["target_panel_ids"] == dialogue["target_panel_ids"] == ["p_001"]
    assert [beat["beat_id"] for beat in beats] == ["b1", "b2"]


def test_split_mixed_quote_beats_infers_speaker_from_lead_in_without_registry() -> None:
    """没有角色表时，也要能从「XX说：」的引导词里认出说话人。"""
    beats = s4_scriptwriter.split_mixed_quote_beats(
        [_beat("道振平静说道：“别怕，有我在。”")]
    )
    assert len(beats) == 2
    assert beats[1]["speaker_role"] == "道振"
    assert beats[1]["speaker_gender"] == "unknown"


def test_split_mixed_quote_beats_keeps_pure_quote_beat() -> None:
    """整条就是引号对白时不拆，只归属角色并剥掉外层引号。"""
    beats = s4_scriptwriter.split_mixed_quote_beats(
        [_beat("“就算我死了，你也不用来参加我的葬礼。”", speaker_role="female_lead")]
    )
    assert len(beats) == 1
    assert beats[0]["narration_text"] == "“就算我死了，你也不用来参加我的葬礼。”"
    assert beats[0]["tts_text"] == "就算我死了，你也不用来参加我的葬礼。"
    assert beats[0]["speaker_role"] == "female_lead"
    assert beats[0]["speaker_gender"] == "female"


def test_split_mixed_quote_beats_leaves_plain_beats_untouched() -> None:
    beats = s4_scriptwriter.split_mixed_quote_beats(
        [_beat("恩彗站在病房门前，阳光照不暖她发冷的心。", beat_id="b7")]
    )
    assert len(beats) == 1
    assert beats[0]["narration_text"] == "恩彗站在病房门前，阳光照不暖她发冷的心。"
    assert beats[0]["speaker_role"] == "narrator"


def test_split_mixed_quote_beats_keeps_original_pause_for_dialogue() -> None:
    beats = s4_scriptwriter.split_mixed_quote_beats(
        [_beat("道振说：“别怕。”", pause_after_ms=200)]
    )
    assert beats[0]["pause_after_ms"] == 60
    assert beats[1]["pause_after_ms"] == 200


def test_split_mixed_quote_beats_defaults_dialogue_pause_when_absent() -> None:
    beats = s4_scriptwriter.split_mixed_quote_beats([_beat("道振说：“别怕。”")])
    assert beats[1]["pause_after_ms"] == 160


def test_split_mixed_quote_beats_handles_multiple_quote_segments() -> None:
    beats = s4_scriptwriter.split_mixed_quote_beats(
        [_beat("道振说：“别怕。”恩彗没有回答。", speaker_gender="unknown")]
    )
    # 旁白 → 对白 → 旁白，三段连续。
    assert [beat["speaker_role"] for beat in beats] == ["narrator", "道振", "narrator"]
    assert beats[2]["narration_text"] == "恩彗没有回答。"


def test_split_mixed_quote_beats_does_not_guess_pronoun_lead() -> None:
    """引号外只有代词引导时不硬猜人名（避免造出「他只」这种假角色）。"""
    beats = s4_scriptwriter.split_mixed_quote_beats(
        [_beat("他只留下一句冰冷判决：“就算我死了，你也不用来参加我的葬礼。”")]
    )
    assert len(beats) == 2
    assert beats[0]["speaker_role"] == "narrator"
    assert beats[1]["speaker_role"] == "narrator"


def test_split_mixed_quote_beats_inherits_speaker_for_consecutive_quotes() -> None:
    """同一条 Beat 里连着两段引号（没有第二段引导词）→ 同一人接着说。"""
    beats = s4_scriptwriter.split_mixed_quote_beats(
        [_beat("继母指着恩彗鼻子骂：“我看到你们就倒胃口！”“立刻带着这个孤儿离开这里！”")]
    )
    quotes = [beat for beat in beats if beat["narration_text"].startswith("“")]
    assert len(quotes) == 2
    assert [beat["speaker_role"] for beat in quotes] == ["继母", "继母"]


def test_split_mixed_quote_beats_fills_gender_from_character_registry() -> None:
    """角色表里有性别时，普通 Beat 也要把 speaker_gender 补齐。"""
    beats = s4_scriptwriter.split_mixed_quote_beats(
        [_beat("恩彗站在窗边，没有回头。", speaker_role="道振")],
        characters=[_character("道振", "male")],
    )
    assert beats[0]["speaker_gender"] == "male"


def test_normalize_script_payload_carries_character_gender() -> None:
    """角色更新里的性别要透传进角色表（非法值收敛成 unknown）。"""
    payload = {
        "hook_title": "钩子",
        "episode_digest": {"episode_no": 1, "one_line_hook": "钩子", "summary": "概述"},
        "beats": [_beat("恩彗站在病房门前。")],
        "character_updates": [
            {
                "name": "恩彗",
                "visual_traits": "少女",
                "current_status": "在病房",
                "gender": "female",
            },
            {
                "name": "道振",
                "visual_traits": "青年",
                "current_status": "在场",
                "gender": "not-a-gender",
            },
        ],
    }
    normalized = normalize_script_payload(payload, episode_id="ep_001", episode_no=1)
    genders = [item["gender"] for item in normalized["character_updates"]]
    assert genders == ["female", "unknown"]


# --------------------------------------------------------------------------------------
# 3. 纯字卡硬过滤（候选面板池）
# --------------------------------------------------------------------------------------

def _panel(panel_id: str, *, pure_text_card: bool = False) -> PanelVisionInfo:
    return PanelVisionInfo(
        panel_id=panel_id,
        scene_summary="雨夜街头，少年攥紧信纸。",
        is_pure_text_card=pure_text_card,
    )


def test_candidate_pool_excludes_pure_text_cards() -> None:
    """规格验收 2：候选面板池里的纯字卡 ID 不该出现。"""
    panel_map = {
        "p_001": _panel("p_001"),
        "p_002": _panel("p_002", pure_text_card=True),
    }
    rendered = s4_scriptwriter._format_panel_ids(["p_001", "p_002"], panel_map)
    assert "p_001" in rendered
    assert "p_002" not in rendered


def test_candidate_pool_keeps_text_cards_when_all_are_text_cards() -> None:
    """整章只剩纯字卡时兜底保留（否则这一章无处可挑）。"""
    panel_map = {"p_002": _panel("p_002", pure_text_card=True)}
    rendered = s4_scriptwriter._format_panel_ids(["p_002"], panel_map)
    assert "p_002" in rendered


def test_exclude_pure_text_cards_keeps_original_when_all_filtered() -> None:
    panels = [_panel("p_002", pure_text_card=True)]
    assert s4_scriptwriter.exclude_pure_text_cards(panels) == panels
    mixed = [_panel("p_001"), _panel("p_002", pure_text_card=True)]
    kept = s4_scriptwriter.exclude_pure_text_cards(mixed)
    assert [panel.panel_id for panel in kept] == ["p_001"]


# --------------------------------------------------------------------------------------
# 4. Schema 扩展与向后兼容
# --------------------------------------------------------------------------------------


def test_character_profile_gender_defaults_to_unknown() -> None:
    character = CharacterProfile(
        name="恩彗",
        visual_traits="长发少女",
        current_status="在病房",
        first_seen_ep=1,
        last_seen_ep=1,
    )
    assert character.gender == "unknown"


def test_script_beat_speaker_gender_defaults_to_unknown() -> None:
    beat = ScriptBeat(
        beat_id="b1",
        narration_text="恩彗站在病房门前。",
        target_panel_ids=["p_001"],
    )
    assert beat.speaker_gender == "unknown"
    assert beat.pause_after_ms == 150


def test_legacy_script_json_without_new_fields_still_loads() -> None:
    """旧版 script.json（无 gender / speaker_gender）必须零报错反序列化。"""
    script = NarrationScript.model_validate(
        {
            "episode_id": "ep_001",
            "hook_title": "钩子",
            "episode_digest": {
                "episode_no": 1,
                "one_line_hook": "钩子",
                "summary": "概述",
            },
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "恩彗站在病房门前。",
                    "target_panel_ids": ["p_001"],
                }
            ],
        }
    )
    assert script.beats[0].speaker_gender == "unknown"
    assert script.beats[0].pause_after_ms == 150


def test_script_beat_accepts_explicit_speaker_gender() -> None:
    beat = ScriptBeat(
        beat_id="b1",
        narration_text="“别怕，有我在。”",
        target_panel_ids=["p_001"],
        speaker_role="道振",
        speaker_gender="male",
    )
    assert beat.speaker_gender == "male"
    assert beat.speaker_role == "道振"


def test_episode_digest_contract_unchanged() -> None:
    digest = EpisodeDigest(episode_no=1, one_line_hook="钩子", summary="概述")
    assert digest.episode_no == 1
