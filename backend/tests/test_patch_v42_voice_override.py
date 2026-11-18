"""Patch v4.2：逐句指定音色（审核台分镜大图弹窗设置配音音色 / 情绪指令）。

覆盖三件事：

1. ``ScriptBeat.voice_override`` 非空时，Stage 5 的音色解析必须以它为准
   （来源标记 ``voice_override``），语速沿用角色解析结果；
2. 审核台 ``WorkspaceView.beats()`` 必须下发 ``voice_override``，
   否则前端拿不到「这句是不是被指定过音色」；
3. Web 保存时前端若没回传该字段，必须继承旧值（别把人工指定的音色抹掉）；
   回传空串表示改回「跟随角色解析」。
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
)
from core.schemas import ScriptBeat
from stages.s5_tts import (
    resolve_beat_voice_assignment,
    resolve_beat_voice_assignment_without_override,
    resolve_beat_voice_spec,
)
from web.server import WorkspaceView, _merge_script_beats

SERIES_ID = "wt_override"


def _series_tts() -> SeriesTTSConfig:
    return SeriesTTSConfig(
        narrator=VoiceSpec(voice="Chinese (Mandarin)_Radio_Host", speed=1.2),
        default_male=VoiceSpec(voice="male-qn-jingying", speed=1.15),
        default_female=VoiceSpec(voice="female-shaonv", speed=1.15),
        character_voices={
            "恩彗": VoiceSpec(voice="Chinese (Mandarin)_Warm_Girl", speed=1.18),
        },
    )


def _beat(beat_id: str, speaker_role: str, **extra: object) -> dict:
    payload = {
        "beat_id": beat_id,
        "narration_text": f"{beat_id} 的解说词",
        "emotion_instruction": "用自然、流畅且有代入感的漫评解说语气说出",
        "speaker_role": speaker_role,
        "target_panel_ids": ["p_001"],
    }
    payload.update(extra)
    return payload


def test_voice_override_wins_over_role_assignment() -> None:
    """指定音色优先于「讲解者 / 角色专属 / 通用男女声」的解析结果。"""
    beat = ScriptBeat.model_validate(
        _beat("b_001", "恩彗", voice_override="Chinese (Mandarin)_Sweet_Lady")
    )

    assignment = resolve_beat_voice_assignment(
        beat, series_tts=_series_tts(), characters=[]
    )

    assert assignment.source == "voice_override"
    assert assignment.label == "指定音色"
    assert assignment.spec.voice == "Chinese (Mandarin)_Sweet_Lady"
    # 语速沿用角色解析结果（恩彗专属音色 1.18）。
    assert assignment.spec.speed == 1.18
    # 角色 / 性别信息保留，界面仍能显示「这句是谁说的」。
    assert assignment.role == "恩彗"


def test_empty_voice_override_keeps_role_assignment() -> None:
    """没指定音色（空串 / 缺省）时行为完全不变。"""
    beat = ScriptBeat.model_validate(_beat("b_001", "恩彗"))

    assignment = resolve_beat_voice_assignment(
        beat, series_tts=_series_tts(), characters=[]
    )

    assert assignment.source == "character"
    assert assignment.label == "角色专属 · 恩彗"
    assert assignment.spec.voice == "Chinese (Mandarin)_Warm_Girl"


def test_resolve_beat_voice_spec_follows_voice_override() -> None:
    """``resolve_beat_voice_spec`` 与 assignment 同源，指定音色同样生效。"""
    beat = ScriptBeat.model_validate(
        _beat("b_001", "narrator", voice_override="male-qn-badao")
    )

    assignment = resolve_beat_voice_assignment(
        beat, series_tts=_series_tts(), characters=[]
    )
    spec = resolve_beat_voice_spec(beat, series_tts=_series_tts(), characters=[])

    assert spec.voice == assignment.spec.voice == "male-qn-badao"


def test_legacy_branch_also_honors_voice_override() -> None:
    """没有作品级模板（老作品）时，逐句指定音色同样优先于旧槽位表。"""
    beat = ScriptBeat.model_validate(
        _beat("b_001", "male_antagonist", voice_override="Chinese (Mandarin)_Pure-hearted_Boy")
    )

    assignment = resolve_beat_voice_assignment(
        beat,
        series_tts=None,
        characters=[],
        voice_cast={"male_antagonist": "Ryan"},
    )

    assert assignment.source == "voice_override"
    assert assignment.spec.voice == "Chinese (Mandarin)_Pure-hearted_Boy"


def test_beats_expose_voice_override(tmp_path: Path) -> None:
    """审核台要能读到「这句被指定了音色」，以及它最终会用什么音色。"""
    series_dir = tmp_path / SERIES_ID
    script_dir = series_dir / "episodes" / "ep_001" / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    (series_dir / "series_config.yaml").write_text(
        "tts:\n"
        "  narrator:\n"
        "    voice: Chinese (Mandarin)_Radio_Host\n"
        "    speed: 1.2\n",
        encoding="utf-8",
    )
    payload = {
        "episode_id": f"{SERIES_ID}_ep_001",
        "hook_title": "钩子",
        "episode_digest": {
            "episode_no": 1,
            "one_line_hook": "钩子",
            "summary": "一句话摘要",
        },
        "beats": [
            _beat("b_001", "narrator", voice_override="female-shaonv"),
            _beat("b_002", "narrator"),
        ],
    }
    (script_dir / "script.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    config = AppConfig(
        workspace=WorkspaceConfig(root=tmp_path),
        tts=TTSConfig(
            default_voice="Elias",
            narrator=VoiceSpec(voice="Elias", speed=1.18),
            default_male=VoiceSpec(voice="Ethan", speed=1.15),
            default_female=VoiceSpec(voice="Cherry", speed=1.15),
        ),
    )
    beats = WorkspaceView(config).beats(SERIES_ID, 1)

    assert beats[0]["voice_override"] == "female-shaonv"
    assert beats[0]["resolved_voice"]["source"] == "voice_override"
    assert beats[0]["resolved_voice"]["voice"] == "female-shaonv"
    assert beats[1]["voice_override"] == ""
    assert beats[1]["resolved_voice"]["source"] == "narrator"


def test_beats_expose_role_voice_beside_override(tmp_path: Path) -> None:
    """指定音色时也要下发「清空指定后按角色会用哪个音色」，供界面双行展示。"""
    series_dir = tmp_path / SERIES_ID
    script_dir = series_dir / "episodes" / "ep_001" / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    (series_dir / "series_config.yaml").write_text(
        "tts:\n"
        "  narrator:\n"
        "    voice: Chinese (Mandarin)_Radio_Host\n"
        "    speed: 1.2\n",
        encoding="utf-8",
    )
    payload = {
        "episode_id": f"{SERIES_ID}_ep_001",
        "hook_title": "钩子",
        "episode_digest": {
            "episode_no": 1,
            "one_line_hook": "钩子",
            "summary": "一句话摘要",
        },
        "beats": [
            _beat("b_001", "narrator", voice_override="female-shaonv"),
            _beat("b_002", "narrator"),
        ],
    }
    (script_dir / "script.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    config = AppConfig(
        workspace=WorkspaceConfig(root=tmp_path),
        tts=TTSConfig(
            default_voice="Elias",
            narrator=VoiceSpec(voice="Elias", speed=1.18),
            default_male=VoiceSpec(voice="Ethan", speed=1.15),
            default_female=VoiceSpec(voice="Cherry", speed=1.15),
        ),
    )
    beats = WorkspaceView(config).beats(SERIES_ID, 1)

    resolved = beats[0]["resolved_voice"]
    assert resolved["source"] == "voice_override"
    assert resolved["role_voice"]["source"] == "narrator"
    assert resolved["role_voice"]["label"] == "讲解者（旁白）"
    assert resolved["role_voice"]["voice"] == "Chinese (Mandarin)_Radio_Host"
    # 没有逐句指定时不需要附带角色解析结果。
    assert "role_voice" not in beats[1]["resolved_voice"]


def test_resolve_beat_voice_assignment_without_override_ignores_override() -> None:
    """独立入口返回「按角色会用哪个音色」，不受逐句指定影响。"""
    beat = ScriptBeat.model_validate(
        _beat("b_001", "narrator", voice_override="female-shaonv")
    )

    assignment = resolve_beat_voice_assignment_without_override(
        beat, series_tts=_series_tts(), characters=[]
    )

    assert assignment.source == "narrator"
    assert assignment.label == "讲解者（旁白）"
    assert assignment.spec.voice == "Chinese (Mandarin)_Radio_Host"


def test_merge_script_beats_preserves_voice_override() -> None:
    """前端没回传 voice_override 时继承旧值；回传空串表示改回「跟随角色」。"""
    existing = [
        ScriptBeat.model_validate(
            _beat("b_001", "narrator", voice_override="female-shaonv")
        )
    ]

    merged = _merge_script_beats(
        existing, [_beat("b_001", "narrator", narration_text="改过的解说词")]
    )
    assert merged[0].voice_override == "female-shaonv"
    assert merged[0].narration_text == "改过的解说词"

    merged = _merge_script_beats(
        existing, [_beat("b_001", "narrator", voice_override="")]
    )
    assert merged[0].voice_override == ""
