"""Patch v3.4：审核台「本句配音」解析的测试。

覆盖两件事：

1. Stage 5 的 ``resolve_beat_voice_assignment`` 是**唯一**的音色判定实现，
   ``resolve_beat_voice_spec`` 只是它的薄封装（两者结果必须一致）。
2. Web 端 ``WorkspaceView.beats()`` 为每条 beat 下发 ``resolved_voice``
   （音色 / 语速 / 来源 / 角色 / 性别），审核台直接渲染这份结果。
"""

from __future__ import annotations

import json
from pathlib import Path

from core.config import (
    AppConfig,
    SeriesTTSConfig,
    TTSConfig,
    VoiceSpec,
    WorkspaceConfig,
    load_series_config,
    merge_series_tts,
)
from core.schemas import ScriptBeat
from stages.s5_tts import (
    load_bible_characters,
    resolve_beat_voice_assignment,
    resolve_beat_voice_spec,
)
from web.server import WorkspaceView

SERIES_ID = "wt_voice"


def _write_workspace(tmp_path: Path) -> Path:
    series_dir = tmp_path / SERIES_ID
    (series_dir / "episodes" / "ep_001" / "04_script").mkdir(parents=True, exist_ok=True)
    return series_dir


def _write_bible(series_dir: Path) -> None:
    payload = {
        "series_id": SERIES_ID,
        "series_title": "测试作品",
        "characters": [
            {
                "name": "恩彗",
                "visual_traits": "浅灰短发",
                "current_status": "在线",
                "first_seen_ep": 1,
                "last_seen_ep": 1,
                "gender": "female",
            },
            {
                "name": "道振",
                "visual_traits": "黑发男性",
                "current_status": "在线",
                "first_seen_ep": 1,
                "last_seen_ep": 1,
                "gender": "male",
            },
        ],
    }
    (series_dir / "story_bible.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _write_series_config(series_dir: Path) -> None:
    content = (
        "tts:\n"
        "  narrator:\n"
        "    voice: Chinese (Mandarin)_Radio_Host\n"
        "    speed: 1.2\n"
        "  default_male:\n"
        "    voice: male-qn-jingying\n"
        "    speed: 1.15\n"
        "  default_female:\n"
        "    voice: female-shaonv\n"
        "    speed: 1.15\n"
        "  character_voices:\n"
        "    恩彗:\n"
        "      voice: Chinese (Mandarin)_Warm_Girl\n"
        "      speed: 1.18\n"
    )
    (series_dir / "series_config.yaml").write_text(content, encoding="utf-8")


def _write_script(series_dir: Path, beats: list[dict]) -> None:
    payload = {
        "episode_id": f"{SERIES_ID}_ep_001",
        "hook_title": "钩子",
        "episode_digest": {
            "episode_no": 1,
            "one_line_hook": "钩子",
            "summary": "一句话摘要",
        },
        "beats": beats,
    }
    path = series_dir / "episodes" / "ep_001" / "04_script" / "script.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _beat(beat_id: str, speaker_role: str) -> dict:
    return {
        "beat_id": beat_id,
        "narration_text": f"{beat_id} 的解说词",
        "emotion_instruction": "用自然、流畅且有代入感的漫评解说语气说出",
        "speaker_role": speaker_role,
        "target_panel_ids": ["p_001"],
    }


def _view(tmp_path: Path) -> WorkspaceView:
    config = AppConfig(
        workspace=WorkspaceConfig(root=tmp_path),
        tts=TTSConfig(
            default_voice="Elias",
            narrator=VoiceSpec(voice="Elias", speed=1.18),
            default_male=VoiceSpec(voice="Ethan", speed=1.15),
            default_female=VoiceSpec(voice="Cherry", speed=1.15),
        ),
    )
    return WorkspaceView(config)


def test_beats_expose_resolved_voice_for_narrator(tmp_path: Path) -> None:
    """旁白句 → 「讲解者（旁白）」+ 作品级旁白音色。"""
    series_dir = _write_workspace(tmp_path)
    _write_bible(series_dir)
    _write_series_config(series_dir)
    _write_script(series_dir, [_beat("b_001", "narrator")])

    beats = _view(tmp_path).beats(SERIES_ID, 1)

    assert len(beats) == 1
    resolved = beats[0]["resolved_voice"]
    assert resolved["label"] == "讲解者（旁白）"
    assert resolved["source"] == "narrator"
    assert resolved["voice"] == "Chinese (Mandarin)_Radio_Host"
    assert resolved["speed"] == 1.2


def test_beats_expose_resolved_voice_for_character(tmp_path: Path) -> None:
    """角色专属音色命中 → 「角色专属 · 恩彗」。"""
    series_dir = _write_workspace(tmp_path)
    _write_bible(series_dir)
    _write_series_config(series_dir)
    _write_script(series_dir, [_beat("b_001", "恩彗")])

    beats = _view(tmp_path).beats(SERIES_ID, 1)

    resolved = beats[0]["resolved_voice"]
    assert resolved["label"] == "角色专属 · 恩彗"
    assert resolved["source"] == "character"
    assert resolved["role"] == "恩彗"
    assert resolved["voice"] == "Chinese (Mandarin)_Warm_Girl"


def test_beats_expose_resolved_voice_by_gender_fallback(tmp_path: Path) -> None:
    """没配专属音色的角色 → 按角色性别落到通用男声 / 通用女声。"""
    series_dir = _write_workspace(tmp_path)
    _write_bible(series_dir)
    _write_series_config(series_dir)
    _write_script(
        series_dir,
        [_beat("b_001", "道振"), _beat("b_002", "路人")],
    )

    beats = _view(tmp_path).beats(SERIES_ID, 1)

    male = beats[0]["resolved_voice"]
    assert male["label"] == "通用男声"
    assert male["source"] == "default_male"
    assert male["gender"] == "male"
    assert male["voice"] == "male-qn-jingying"

    # 「路人」既不在角色表也没有性别 → 回退旁白。
    unknown = beats[1]["resolved_voice"]
    assert unknown["label"] == "讲解者（旁白）"
    assert unknown["gender"] == "unknown"
    assert unknown["voice"] == "Chinese (Mandarin)_Radio_Host"


def test_series_level_default_voice_overrides_legacy_cast(tmp_path: Path) -> None:
    """作品级配了通用男声，就用作品级的音色（而不是全局旧槽位表里的百炼音色）。

    旧槽位角色（如 ``male_antagonist``）在全局 ``voice_cast`` 里有映射，
    早期逻辑会优先沿用旧音色，导致审核台改作品配音设置不生效。
    """
    series_dir = _write_workspace(tmp_path)
    _write_bible(series_dir)
    _write_series_config(series_dir)
    _write_script(series_dir, [_beat("b_001", "male_antagonist")])

    beats = _view(tmp_path).beats(SERIES_ID, 1)

    resolved = beats[0]["resolved_voice"]
    assert resolved["label"] == "通用男声"
    assert resolved["source"] == "default_male"
    assert resolved["voice"] == "male-qn-jingying"
    assert resolved["speed"] == 1.15


def test_legacy_cast_used_when_series_slot_has_no_voice() -> None:
    """作品级槽位没配音色时，才回落到全局旧槽位表（保持 Patch v3.1 旧行为）。"""
    series_tts = SeriesTTSConfig(
        narrator=VoiceSpec(voice="Chinese (Mandarin)_Radio_Host", speed=1.2),
        default_male=VoiceSpec(voice="", speed=1.15),
        default_female=VoiceSpec(voice="", speed=1.15),
        character_voices={},
    )
    beat = ScriptBeat.model_validate(_beat("b_001", "male_antagonist"))

    assignment = resolve_beat_voice_assignment(
        beat,
        series_tts=series_tts,
        characters=[],
        voice_cast={"male_antagonist": "Ryan"},
    )

    assert assignment.source == "legacy_male"
    assert assignment.label == "通用男声（旧槽位音色）"
    assert assignment.spec.voice == "Ryan"


def test_role_change_overrides_stale_speaker_gender(tmp_path: Path) -> None:
    """审核台把男角色改成女声后，性别要跟着角色走。

    ``speaker_gender`` 是 Stage 4 写下的旧值（男角色改成女角色后仍是 ``male``），
    如果它优先于角色，就会出现「选了通用女声、实际还是男声」。
    """
    series_dir = _write_workspace(tmp_path)
    _write_bible(series_dir)
    _write_series_config(series_dir)
    beat = _beat("b_001", "female")
    beat["speaker_gender"] = "male"  # 陈旧的旧角色性别
    _write_script(series_dir, [beat])

    beats = _view(tmp_path).beats(SERIES_ID, 1)

    resolved = beats[0]["resolved_voice"]
    assert resolved["label"] == "通用女声"
    assert resolved["source"] == "default_female"
    assert resolved["gender"] == "female"
    assert resolved["voice"] == "female-shaonv"


def test_character_role_ignores_stale_speaker_gender(tmp_path: Path) -> None:
    """角色名优先：选了「恩彗」就按角色表判定性别，不吃旧的 speaker_gender。"""
    series_dir = _write_workspace(tmp_path)
    _write_bible(series_dir)
    _write_series_config(series_dir)
    beat = _beat("b_001", "恩彗")
    beat["speaker_gender"] = "male"
    _write_script(series_dir, [beat])

    beats = _view(tmp_path).beats(SERIES_ID, 1)

    resolved = beats[0]["resolved_voice"]
    assert resolved["label"] == "角色专属 · 恩彗"
    assert resolved["source"] == "character"
    assert resolved["voice"] == "Chinese (Mandarin)_Warm_Girl"
    assert resolved["gender"] == "female"


def test_beats_expose_speaker_gender_for_studio_editing(tmp_path: Path) -> None:
    """审核台要能读到当前性别，改角色时才知道该同步成什么。"""
    series_dir = _write_workspace(tmp_path)
    _write_bible(series_dir)
    _write_series_config(series_dir)
    beat = _beat("b_001", "male_antagonist")
    beat["speaker_gender"] = "male"
    _write_script(series_dir, [beat])

    beats = _view(tmp_path).beats(SERIES_ID, 1)

    assert beats[0]["speaker_gender"] == "male"


def test_resolve_beat_voice_spec_matches_assignment(tmp_path: Path) -> None:
    """``resolve_beat_voice_spec`` 必须与 assignment 同源（审核台与 Stage 5 不能分叉）。"""
    series_dir = _write_workspace(tmp_path)
    _write_bible(series_dir)
    _write_series_config(series_dir)

    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    merged = merge_series_tts(
        config.tts, load_series_config(series_dir).tts
    )
    characters = load_bible_characters(series_dir)

    for speaker_role in ("narrator", "恩彗", "道振"):
        beat = ScriptBeat.model_validate(_beat(f"b_{speaker_role}", speaker_role))
        assignment = resolve_beat_voice_assignment(
            beat, series_tts=merged, characters=characters
        )
        spec = resolve_beat_voice_spec(
            beat, series_tts=merged, characters=characters
        )
        assert spec.voice == assignment.spec.voice
        assert spec.speed == assignment.spec.speed
        assert assignment.label
