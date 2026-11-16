"""模型输出归一化层（``core/normalize.py``）的单元测试。

真实百炼调用暴露过的坑：``emotion`` 返回 ``pleading``（不在契约 7 值内）、
``dialogues`` 塞字符串、``focus_region`` 给 3 个数、``motion`` 给大写、
``narration_text`` 超过 120 字——这些都必须被收敛而不是让整话崩掉。
"""

from __future__ import annotations

from core.normalize import (
    EMOTION_VALUES,
    MOTION_VALUES,
    clip_text,
    coerce_bool,
    coerce_floats,
    coerce_int,
    coerce_str,
    coerce_str_list,
    normalize_enum,
    normalize_panel_payload,
    normalize_script_payload,
)


# --------------------------------------------------------------------------
# 基础类型收敛
# --------------------------------------------------------------------------
def test_normalize_enum_accepts_exact_and_case_insensitive() -> None:
    assert normalize_enum("calm", EMOTION_VALUES, "calm") == "calm"
    assert normalize_enum("ANGRY", EMOTION_VALUES, "calm") == "angry"
    assert normalize_enum("Zoom In", MOTION_VALUES, "static") == "zoom_in"
    assert normalize_enum("pan-down", MOTION_VALUES, "static") == "pan_down"


def test_normalize_enum_falls_back_when_unmatched() -> None:
    assert normalize_enum("pleading", EMOTION_VALUES, "calm") == "calm"
    assert normalize_enum(None, EMOTION_VALUES, "calm") == "calm"
    assert normalize_enum("", EMOTION_VALUES, "calm") == "calm"


def test_normalize_enum_unique_substring_match() -> None:
    # "very angry" 只命中一个合法值
    assert normalize_enum("very angry", EMOTION_VALUES, "calm") == "angry"


def test_coerce_helpers() -> None:
    assert coerce_str("  hi  ") == "hi"
    assert coerce_str(None, "d") == "d"
    assert coerce_str(12) == "12"
    assert coerce_str_list(["a", "", None, "b"]) == ["a", "b"]
    assert coerce_str_list("single") == ["single"]
    assert coerce_str_list(None) == []
    assert coerce_bool("是") is True
    assert coerce_bool("false") is False
    assert coerce_bool(None, True) is True
    assert coerce_int("7") == 7
    assert coerce_int("bad", 3) == 3
    assert coerce_int(99, 0, low=0, high=10) == 10
    assert coerce_int(-5, 0, low=0, high=10) == 0
    assert coerce_floats([0.1, 0.2, 0.3, 0.4]) == (0.1, 0.2, 0.3, 0.4)
    assert coerce_floats([0.1, 0.2, 0.3]) is None
    assert coerce_floats(None) is None
    assert clip_text("x" * 200, 120) == "x" * 120


# --------------------------------------------------------------------------
# Stage 3 面板输出
# --------------------------------------------------------------------------
def test_normalize_panel_payload_repairs_out_of_range_emotion() -> None:
    payload = normalize_panel_payload(
        {
            "scene_summary": "  两人在峡谷中  ",
            "dialogues": [
                {"speaker_guess": "Bam", "text": "Who are you?", "emotion": "pleading"},
                {"text": "别过来", "emotion": "scared"},
            ],
            "sfx_list": "HUFF",
            "focus_region": [0.1, 0.2, 0.3],
            "is_transition_blank": "是",
        },
        panel_id="panel_007",
    )
    assert payload["panel_id"] == "panel_007"
    assert payload["scene_summary"] == "两人在峡谷中"
    assert payload["dialogues"][0]["emotion"] == "calm"
    assert payload["dialogues"][1]["emotion"] == "calm"
    assert payload["sfx_list"] == ["HUFF"]
    assert payload["focus_region"] is None
    assert payload["is_transition_blank"] is True


def test_normalize_panel_payload_drops_empty_dialogue_text() -> None:
    payload = normalize_panel_payload(
        {"scene_summary": "空镜", "dialogues": [{"text": "  "}, {"text": "有内容"}]},
        panel_id="panel_001",
    )
    assert len(payload["dialogues"]) == 1
    assert payload["dialogues"][0]["text"] == "有内容"


# --------------------------------------------------------------------------
# Stage 4 剧本输出
# --------------------------------------------------------------------------
def test_normalize_script_payload_repairs_and_filters() -> None:
    payload = normalize_script_payload(
        {
            "hook_title": "钩子",
            "beats": [
                {
                    "narration_text": "短",
                    "target_panel_ids": ["panel_001"],
                },
                {
                    "narration_text": "少年握棍突进，蓝光迸射",
                    "tts_text": "BAM",
                    "target_panel_ids": ["panel_002", "panel_999"],
                    "motion": "ZOOM_IN",
                    "pause_after_ms": 9999,
                },
                {
                    "narration_text": "指向不存在面板",
                    "target_panel_ids": ["panel_888"],
                },
            ],
            "episode_digest": {"one_line_hook": "钩子", "summary": "概要", "keywords": "甲,乙"},
            "character_updates": [
                {"name": "Bam", "aliases": ["主角"], "visual_traits": "黑发", "current_status": "登场", "first_seen_ep": 5, "last_seen_ep": 2}
            ],
            "new_plot_threads": [{"title": "伏笔", "status": "unknown"}],
        },
        episode_id="wt_95_ep_002",
        episode_no=2,
        valid_panel_ids={"panel_001", "panel_002"},
    )
    # 太短的旁白被丢弃；指向不存在面板的 beat 也被丢弃
    assert len(payload["beats"]) == 1
    beat = payload["beats"][0]
    assert beat["beat_id"] == "b2"
    assert beat["target_panel_ids"] == ["panel_002"]
    assert beat["motion"] == "zoom_in"
    assert beat["pause_after_ms"] == 2000
    assert beat["tts_text"] == "BAM"
    assert payload["episode_id"] == "wt_95_ep_002"
    assert payload["episode_digest"]["episode_no"] == 2
    assert payload["episode_digest"]["keywords"] == ["甲,乙"]
    # first_seen 不能晚于 last_seen
    assert payload["character_updates"][0]["first_seen_ep"] == 2
    assert payload["character_updates"][0]["last_seen_ep"] == 5
    # 伏笔状态越界 → open
    assert payload["new_plot_threads"][0]["status"] == "open"


def test_normalize_script_payload_keeps_tts_text_independent() -> None:
    payload = normalize_script_payload(
        {
            "beats": [
                {
                    "narration_text": "他走向塔门",
                    "tts_text": "重霄 chóng xiāo",
                    "target_panel_ids": ["panel_001"],
                }
            ]
        },
        episode_id="wt_95_ep_001",
        episode_no=1,
    )
    assert payload["beats"][0]["narration_text"] == "他走向塔门"
    assert payload["beats"][0]["tts_text"] == "重霄 chóng xiāo"
