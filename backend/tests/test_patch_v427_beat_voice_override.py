"""Patch v4.27：单句音色覆盖（voice_override）+ 角色名误报修复。

两项能力：

* (A) 审核台「配音角色与语速」与分镜大图弹窗都能为**单句**直接挑一个 MiniMax
  音色（``voice_override``），用于一次性 / 龙套角色单独配音；
* (B) 修复保存时弹出的「角色名写法确认」误报：
  - ``female_antagonist`` / ``male_mature`` 这类**通用槽位**不是角色本名，
    不该报「未登记的角色」；
  - 已经单独指定音色的句子（一次性角色），Stage 5 会直接用该音色合成，
    不需要作品角色表里有这个人，也不该报「未登记的角色」。

本文件锁死后端契约（前端交互在 ``src/components/`` 里改）：

* 只读检查 ``inspect_script_beats`` 的三条豁免规则；
* ``PUT /api/episodes/{id}/script`` 保存 / 清除 ``voice_override`` 后，
  ``resolved_voice.source`` 在「指定音色」与「按角色解析」之间正确切换。

全部离线：不触网、不调用 TTS。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from fastapi.testclient import TestClient
from PIL import Image

from core.character_unify import build_character_canonicalizer
from core.config import AppConfig, WorkspaceConfig
from core.schemas import (
    CharacterProfile,
    EpisodeDigest,
    NarrationScript,
    ScriptBeat,
    StoryBibleV2,
    load_model,
    save_model,
)
from stages import s0_catalog
from stages.s5_tts import _SLOT_GENDER
from web.job_runner import JobRunner
from web.server import create_app

#: 一个真实存在的 MiniMax 音色 ID（百炼 MiniMax Speech 音色清单里的一条）。
MINIMAX_VOICE = "Chinese (Mandarin)_Radio_Host"


# ---------------------------------------------------------------------------
# 装置（与 test_patch_v423_manual_save_char_check.py 同构，便于对照）
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


def _character(name: str, *, aliases: Sequence[str] = ()) -> CharacterProfile:
    return CharacterProfile(
        name=name,
        aliases=list(aliases),
        visual_traits="",
        current_status="",
        first_seen_ep=1,
        last_seen_ep=1,
    )


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


def _bible(series_dir: Path, series_id: str) -> None:
    save_model(
        StoryBibleV2(
            series_id=series_id,
            series_title="测试漫画",
            characters=[
                _character("恩彗"),
                _character("道振", aliases=["丈夫", "南道振"]),
            ],
        ),
        series_dir / "story_bible.json",
    )


def _canonicalizer(
    *, characters: Sequence[CharacterProfile], names: Sequence[str] = (), texts: Sequence[str] = ()
):
    return build_character_canonicalizer(
        characters=list(characters),
        extra_names=list(names),
        extra_texts=list(texts),
    )


def _api(series_dir: Path, config: AppConfig) -> TestClient:
    runner = JobRunner(config, tts_factory=lambda: None)
    return TestClient(create_app(config, runner=runner))


def _reasons(warnings: Sequence[Any]) -> set[str]:
    return {warning.reason for warning in warnings}


# ---------------------------------------------------------------------------
# A. 只读检查：单句音色覆盖与通用槽位不再误报
# ---------------------------------------------------------------------------
def test_voice_override_skips_unregistered_speaker() -> None:
    """已经单独指定音色的一次性角色，不该报「未登记的角色」。"""
    canonicalizer = _canonicalizer(
        characters=[_character("恩彗")],
        texts=["路人甲举起伞，站在街角。"],
    )
    beat = _beat(
        "b1",
        "路人甲举起伞，站在街角。",
        speaker_role="路人甲",
        voice_override=MINIMAX_VOICE,
    )
    warnings = canonicalizer.inspect_script_beats([beat])
    assert "unregistered_speaker" not in _reasons(warnings), warnings


def test_voice_override_still_reports_known_character_typo() -> None:
    """指定音色不掩盖「已知角色写错名字」——那仍要提醒。"""
    canonicalizer = _canonicalizer(
        characters=[_character("恩彗")],
        texts=["恩慧推门而入。"],
    )
    beat = _beat(
        "b1",
        "恩慧推门而入。",
        speaker_role="恩慧",
        voice_override=MINIMAX_VOICE,
    )
    warnings = canonicalizer.inspect_script_beats([beat])
    assert "homophone_variant" in _reasons(warnings), warnings
    speaker_warnings = [w for w in warnings if w.field == "speaker_role"]
    assert speaker_warnings, warnings
    assert speaker_warnings[0].suggested_canonical == "恩彗"


def test_generic_role_slots_do_not_trigger_unregistered_speaker() -> None:
    """``female_antagonist`` / ``male_mature`` 是通用槽位，不是角色本名。"""
    canonicalizer = _canonicalizer(characters=[_character("恩彗")])
    for slot in ("female_antagonist", "male_mature"):
        beat = _beat("b1", "她冷眼扫过全场。", speaker_role=slot)
        warnings = canonicalizer.inspect_script_beats([beat])
        assert "unregistered_speaker" not in _reasons(warnings), (slot, warnings)


def test_slot_gender_map_covers_generic_slots() -> None:
    """Stage 5 的性别槽位表要认这两个新槽位。"""
    assert _SLOT_GENDER["female_antagonist"] == "female"
    assert _SLOT_GENDER["male_mature"] == "male"


def test_unregistered_speaker_still_reported_without_override() -> None:
    """没指定音色、又不在角色表里 → 照旧报「未登记的角色」。"""
    canonicalizer = _canonicalizer(characters=[_character("恩彗")])
    beat = _beat("b1", "路人甲举起伞，站在街角。", speaker_role="路人甲")
    warnings = canonicalizer.inspect_script_beats([beat])
    assert "unregistered_speaker" in _reasons(warnings), warnings


# ---------------------------------------------------------------------------
# B. 保存接口：单句音色的落盘与清除
# ---------------------------------------------------------------------------
def _put_script(
    api: TestClient,
    series_id: str,
    beats: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    response = api.put(
        f"/api/episodes/{series_id}_ep_001/script",
        json={"beats": list(beats)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _resolved_voice(api: TestClient, series_id: str, beat_id: str) -> dict[str, Any]:
    payload = api.get(f"/api/series/{series_id}").json()
    episodes = payload["episodes"]
    assert episodes, payload
    beats = episodes[0]["beats"]
    item = next(beat for beat in beats if beat["beat_id"] == beat_id)
    return item["resolved_voice"]


def test_put_script_saves_voice_override_and_switches_resolved_voice(tmp_path: Path) -> None:
    """保存单句音色后：落盘带上 voice_override，resolved_voice.source 变成 voice_override。"""
    series_id, series_dir, config = _register(tmp_path)
    _bible(series_dir, series_id)
    _write_script(series_dir, [_beat("b1", "路人甲举起伞，站在街角。")])

    api = _api(series_dir, config)
    _put_script(
        api,
        series_id,
        [
            {
                "beat_id": "b1",
                "narration_text": "路人甲举起伞，站在街角。",
                "target_panel_ids": ["p_001"],
                "voice_override": MINIMAX_VOICE,
            }
        ],
    )

    saved = load_model(
        NarrationScript, series_dir / "episodes" / "ep_001" / "04_script" / "script.json"
    )
    assert saved.beats[0].voice_override == MINIMAX_VOICE

    resolved = _resolved_voice(api, series_id, "b1")
    assert resolved["source"] == "voice_override", resolved
    assert resolved["voice"] == MINIMAX_VOICE, resolved
    # 同时给出「清空指定后按角色会用哪个音色」
    assert resolved["role_voice"]["voice"] != MINIMAX_VOICE, resolved


def test_put_script_clearing_voice_override_restores_role_voice(tmp_path: Path) -> None:
    """回传空串 = 清除单句音色，恢复按角色解析。"""
    series_id, series_dir, config = _register(tmp_path)
    _bible(series_dir, series_id)
    _write_script(
        series_dir,
        [_beat("b1", "路人甲举起伞，站在街角。", voice_override=MINIMAX_VOICE)],
    )

    api = _api(series_dir, config)
    _put_script(
        api,
        series_id,
        [
            {
                "beat_id": "b1",
                "narration_text": "路人甲举起伞，站在街角。",
                "target_panel_ids": ["p_001"],
                "voice_override": "",
            }
        ],
    )

    saved = load_model(
        NarrationScript, series_dir / "episodes" / "ep_001" / "04_script" / "script.json"
    )
    assert saved.beats[0].voice_override == ""

    resolved = _resolved_voice(api, series_id, "b1")
    assert resolved["source"] != "voice_override", resolved
    assert resolved["voice"] != MINIMAX_VOICE, resolved


def test_put_script_keeps_voice_override_when_not_returned(tmp_path: Path) -> None:
    """前端没回传 voice_override 时继承旧值，别被顺手抹掉。"""
    series_id, series_dir, config = _register(tmp_path)
    _bible(series_dir, series_id)
    _write_script(
        series_dir,
        [_beat("b1", "路人甲举起伞，站在街角。", voice_override=MINIMAX_VOICE)],
    )

    api = _api(series_dir, config)
    _put_script(
        api,
        series_id,
        [
            {
                "beat_id": "b1",
                "narration_text": "路人甲换了个站姿。",
                "target_panel_ids": ["p_001"],
            }
        ],
    )

    saved = load_model(
        NarrationScript, series_dir / "episodes" / "ep_001" / "04_script" / "script.json"
    )
    assert saved.beats[0].voice_override == MINIMAX_VOICE


# ---------------------------------------------------------------------------
# C. 预检接口：单句音色不再误报
# ---------------------------------------------------------------------------
def test_check_characters_endpoint_skips_unregistered_for_voice_override(
    tmp_path: Path,
) -> None:
    """POST script/check-characters：带单句音色的未登记角色不产生告警。"""
    series_id, series_dir, config = _register(tmp_path)
    _bible(series_dir, series_id)
    _write_script(series_dir, [_beat("b1", "路人甲举起伞，站在街角。")])

    api = _api(series_dir, config)
    response = api.post(
        f"/api/episodes/{series_id}_ep_001/script/check-characters",
        json={
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "路人甲举起伞，站在街角。",
                    "target_panel_ids": ["p_001"],
                    "speaker_role": "路人甲",
                    "voice_override": MINIMAX_VOICE,
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["has_warnings"] is False, payload
    assert payload["warnings"] == []
