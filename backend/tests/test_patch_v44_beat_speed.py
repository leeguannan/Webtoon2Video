"""Patch v4.4：单句 TTS 语速微调（``ScriptBeat.speed_override``）。

覆盖四件事：

1. ``speed_override`` 在音色解析层生效：只换 ``spec.speed``，音色来源
   （``source`` / ``label`` / ``role`` / ``gender``）原样保留，且与
   ``voice_override`` 正交可叠加；``resolve_beat_voice_assignment_without_override``
   始终返回角色默认语速，供界面前端对照显示；
2. ``_synthesize_beat`` 的三条分支（显式 ``voice`` / ``series_tts is None`` /
   四级角色解析）在 ``speed_override`` 存在时都把同一语速送进 provider；
3. ``_merge_script_beats``：前端没回传该字段时继承旧值，显式传 ``null`` 时
   真正清空（恢复角色默认语速），传数值时正常更新；
4. ``WorkspaceView`` 下发 ``speed_override``，且 ``resolved_voice`` 同时携带
   当前生效语速 ``speed`` 与角色默认语速 ``role_speed``。
"""

from __future__ import annotations

import io
import json
import wave
from pathlib import Path
from typing import Any

import pytest

from core.config import (
    AppConfig,
    SeriesTTSConfig,
    TTSConfig,
    VoiceSpec,
    WorkspaceConfig,
)
from core.schemas import ScriptBeat
from providers.tts_base import TTSResult
from stages import s5_tts
from stages.s5_tts import (
    MAX_SPEED,
    MIN_SPEED,
    resolve_beat_voice_assignment,
    resolve_beat_voice_assignment_without_override,
)
from web.server import WorkspaceView, _merge_script_beats

SERIES_ID = "wt_speed"


def _series_tts() -> SeriesTTSConfig:
    return SeriesTTSConfig(
        narrator=VoiceSpec(voice="Chinese (Mandarin)_Radio_Host", speed=1.2),
        default_male=VoiceSpec(voice="male-qn-jingying", speed=1.15),
        default_female=VoiceSpec(voice="female-shaonv", speed=1.15),
        character_voices={
            "恩彗": VoiceSpec(voice="Chinese (Mandarin)_Warm_Girl", speed=1.18),
        },
    )


def _beat(beat_id: str, speaker_role: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "beat_id": beat_id,
        "narration_text": f"{beat_id} 的解说词",
        "emotion_instruction": "用自然、流畅且有代入感的漫评解说语气说出",
        "speaker_role": speaker_role,
        "target_panel_ids": ["p_001"],
    }
    payload.update(extra)
    return payload


def _wav_bytes(seconds: float = 0.2, rate: int = 24000) -> bytes:
    """造一段合法的单声道 16bit WAV（静音即可），让时长探针走 wave 直读。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


class FakeTTS:
    """假合成器：记录调用参数（重点抓 speed），返回真实 WAV 字节，绝不联网。"""

    model = ""

    def __init__(self, *, supports_native_speed: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self.supports_native_speed = supports_native_speed

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str | None = None,
        **kwargs: Any,
    ) -> TTSResult:
        self.calls.append(
            {"text": text, "instruction": instruction, "voice": voice, **kwargs}
        )
        return TTSResult(audio=_wav_bytes(), content_type="audio/wav", voice=voice or "Fake")


# ---------------------------------------------------------------------------
# A. 音色解析层：speed_override 只换语速，不碰来源
# ---------------------------------------------------------------------------


def test_speed_override_replaces_role_speed_but_keeps_source() -> None:
    """角色默认 1.2x，微调 0.9x：语速被替换，narrator 来源标记原样保留。"""
    beat = ScriptBeat.model_validate(_beat("b_001", "narrator", speed_override=0.9))

    assignment = resolve_beat_voice_assignment(
        beat, series_tts=_series_tts(), characters=[]
    )

    assert assignment.spec.speed == 0.9
    assert assignment.spec.voice == "Chinese (Mandarin)_Radio_Host"
    assert assignment.source == "narrator"
    assert assignment.label == "讲解者（旁白）"
    assert assignment.role == "narrator"


def test_speed_override_stacks_with_voice_override() -> None:
    """语速微调与逐句指定音色正交：source 仍是 voice_override，语速取微调值。"""
    beat = ScriptBeat.model_validate(
        _beat(
            "b_001",
            "恩彗",
            voice_override="Chinese (Mandarin)_Sweet_Lady",
            speed_override=1.3,
        )
    )

    assignment = resolve_beat_voice_assignment(
        beat, series_tts=_series_tts(), characters=[]
    )

    assert assignment.source == "voice_override"
    assert assignment.label == "指定音色"
    assert assignment.spec.voice == "Chinese (Mandarin)_Sweet_Lady"
    assert assignment.spec.speed == 1.3
    # 角色信息保留，界面仍能显示「这句是谁说的」。
    assert assignment.role == "恩彗"


def test_without_override_always_returns_role_default_speed() -> None:
    """对照入口不受 speed_override 影响，始终给角色默认语速。"""
    beat = ScriptBeat.model_validate(_beat("b_001", "narrator", speed_override=0.9))

    base = resolve_beat_voice_assignment_without_override(
        beat, series_tts=_series_tts(), characters=[]
    )

    assert base.source == "narrator"
    assert base.spec.speed == 1.2

    # 没有微调时，两个入口的语速自然相等。
    plain = ScriptBeat.model_validate(_beat("b_002", "narrator"))
    plain_assignment = resolve_beat_voice_assignment(
        plain, series_tts=_series_tts(), characters=[]
    )
    assert plain_assignment.spec.speed == base.spec.speed == 1.2


def test_speed_override_is_normalized_into_supported_range() -> None:
    """schema 允许 0.5–2.0，合成前仍由 normalize_speed 收敛到 provider 区间。"""
    beat = ScriptBeat.model_validate(_beat("b_001", "narrator", speed_override=2.0))

    assignment = resolve_beat_voice_assignment(
        beat, series_tts=_series_tts(), characters=[]
    )

    assert assignment.spec.speed == MAX_SPEED

    beat_low = ScriptBeat.model_validate(
        _beat("b_002", "narrator", speed_override=0.5)
    )
    low = resolve_beat_voice_assignment(
        beat_low, series_tts=_series_tts(), characters=[]
    )
    assert low.spec.speed == MIN_SPEED


# ---------------------------------------------------------------------------
# B. 合成层：_synthesize_beat 三条分支都应用 speed_override
# ---------------------------------------------------------------------------


async def _synth_call(
    tmp_path: Path,
    beat: ScriptBeat,
    *,
    voice: str | None,
    series_tts: SeriesTTSConfig | None,
) -> dict[str, Any]:
    client = FakeTTS(supports_native_speed=True)
    audio_dir = tmp_path / "05_audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    meta, issues = await s5_tts._synthesize_beat(
        client,
        beat,
        audio_dir=audio_dir,
        overrides=None,
        voice=voice,
        default_voice="Elias",
        voice_cast=None,
        series_tts=series_tts,
        characters=[],
        ffmpeg_bin="ffmpeg",
        trim_silence=False,
    )
    assert meta is not None, f"合成不应失败：{issues}"
    assert issues == []
    assert client.calls, "provider 应收到一次合成调用"
    return client.calls[0]


@pytest.mark.anyio
async def test_synthesize_applies_speed_override_in_explicit_voice_branch(
    tmp_path: Path,
) -> None:
    """显式 ``--voice`` 分支原本语速恒 1.0，微调后必须把语速送进 provider。"""
    beat = ScriptBeat.model_validate(
        _beat("b_001", "narrator", speed_override=1.3)
    )

    call = await _synth_call(
        tmp_path, beat, voice="Elias", series_tts=_series_tts()
    )

    assert call["voice"] == "Elias"
    assert call["speed"] == 1.3


@pytest.mark.anyio
async def test_synthesize_applies_speed_override_without_series_tts(
    tmp_path: Path,
) -> None:
    """老作品（series_tts=None）分支同样要应用单句语速微调。"""
    beat = ScriptBeat.model_validate(
        _beat("b_001", "male_antagonist", speed_override=0.9)
    )

    call = await _synth_call(tmp_path, beat, voice=None, series_tts=None)

    assert call["speed"] == 0.9


@pytest.mark.anyio
async def test_synthesize_applies_speed_override_in_role_branch(
    tmp_path: Path,
) -> None:
    """四级角色解析分支：微调语速覆盖角色默认；不微调时沿用角色默认。"""
    overridden = ScriptBeat.model_validate(
        _beat("b_001", "narrator", speed_override=1.42)
    )
    call = await _synth_call(
        tmp_path, overridden, voice=None, series_tts=_series_tts()
    )
    assert call["speed"] == 1.42

    plain = ScriptBeat.model_validate(_beat("b_002", "narrator"))
    plain_call = await _synth_call(
        tmp_path, plain, voice=None, series_tts=_series_tts()
    )
    assert plain_call["speed"] == 1.2


# ---------------------------------------------------------------------------
# C. 保存合并：缺失继承、显式 null 清空、数值更新
# ---------------------------------------------------------------------------


def test_merge_preserves_speed_override_when_field_missing() -> None:
    """前端没回传 speed_override → 继承旧值，不能把人工微调抹掉。"""
    existing = [
        ScriptBeat.model_validate(
            _beat("b_001", "narrator", speed_override=1.3)
        )
    ]

    merged = _merge_script_beats(
        existing, [_beat("b_001", "narrator", narration_text="改过的解说词")]
    )

    assert merged[0].speed_override == 1.3
    assert merged[0].narration_text == "改过的解说词"


def test_merge_clears_speed_override_on_explicit_null() -> None:
    """显式传 null（NULLABLE_BEAT_FIELDS）→ 真正清空，恢复角色默认语速。"""
    existing = [
        ScriptBeat.model_validate(
            _beat("b_001", "narrator", speed_override=1.3)
        )
    ]

    merged = _merge_script_beats(
        existing, [_beat("b_001", "narrator", speed_override=None)]
    )

    assert merged[0].speed_override is None


def test_merge_updates_speed_override_value() -> None:
    """传新数值 → 正常更新；旧 beat 没有该字段（历史数据）时也能直接落值。"""
    existing = [ScriptBeat.model_validate(_beat("b_001", "narrator"))]

    merged = _merge_script_beats(
        existing, [_beat("b_001", "narrator", speed_override=0.88)]
    )

    assert merged[0].speed_override == 0.88


# ---------------------------------------------------------------------------
# D. WorkspaceView：beats() 下发 speed_override 与 speed / role_speed
# ---------------------------------------------------------------------------


def _write_workspace(tmp_path: Path, beats: list[dict[str, Any]]) -> AppConfig:
    series_dir = tmp_path / SERIES_ID
    script_dir = series_dir / "episodes" / "ep_001" / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    (series_dir / "series_config.yaml").write_text(
        "tts:\n"
        "  narrator:\n"
        "    voice: Chinese (Mandarin)_Radio_Host\n"
        "    speed: 1.2\n"
        "  default_male:\n"
        "    voice: male-qn-jingying\n"
        "    speed: 1.15\n"
        "  default_female:\n"
        "    voice: female-shaonv\n"
        "    speed: 1.15\n",
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
        "beats": beats,
    }
    (script_dir / "script.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return AppConfig(
        workspace=WorkspaceConfig(root=tmp_path),
        tts=TTSConfig(
            default_voice="Elias",
            narrator=VoiceSpec(voice="Elias", speed=1.18),
            default_male=VoiceSpec(voice="Ethan", speed=1.15),
            default_female=VoiceSpec(voice="Cherry", speed=1.15),
        ),
    )


def test_workspace_view_exposes_speed_override_and_role_speed(tmp_path: Path) -> None:
    """beats() 透传 speed_override；resolved_voice 同时给 speed 与 role_speed。"""
    config = _write_workspace(
        tmp_path,
        [
            _beat("b_001", "narrator", speed_override=0.9),
            _beat("b_002", "narrator"),
        ],
    )

    beats = WorkspaceView(config).beats(SERIES_ID, 1)

    # 字段原样下发（含 None 缺省态）。
    assert beats[0]["speed_override"] == 0.9
    assert beats[1]["speed_override"] is None

    # 微调句：speed 是本句生效语速，role_speed 仍是角色默认 1.2。
    overridden = beats[0]["resolved_voice"]
    assert overridden["speed"] == 0.9
    assert overridden["role_speed"] == 1.2

    # 未微调句：两者相等，都等于讲解者默认语速。
    plain = beats[1]["resolved_voice"]
    assert plain["speed"] == plain["role_speed"] == 1.2
