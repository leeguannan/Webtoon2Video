"""Patch v4.23：人工保存不再强制把角色名改回规范名，改成「只读检查 + 用户确认放行」。

背景（实测 wt_5081 第 5 话）：用户把 b10 的「我未来的丈夫被告白的场面」改成
「我未来的丈夫被告白的场面」并保存，刷新后又被后端的角色名归一改回了原文，
于是「保存没有用」。根因是 PUT 剧本时后端无条件调用
``_canonicalize_script_beats`` 覆盖用户输入。

本文件锁死 v4.23 的新契约：

* 人工保存默认**原样落盘**，只在响应里回传只读告警；
* 只有显式 ``auto_unify_characters=True`` 才归一；
* 只读检查不误报「小恩小惠」「施以恩惠于」「未来的丈夫」这类惯用词与关系称谓；
* Stage 4 的 AI 生成阶段照旧自动归一（本次不动）。

全部离线：不触网、不依赖 ffmpeg。
"""

from __future__ import annotations

import json
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
from web.job_runner import JobRunner
from web.server import create_app

SERIES_ID = "wt_v423"


# ---------------------------------------------------------------------------
# 装置
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


def _canonicalizer(*, characters: Sequence[CharacterProfile], names: Sequence[str], texts: Sequence[str]):
    return build_character_canonicalizer(
        characters=list(characters),
        extra_names=list(names),
        extra_texts=list(texts),
    )


def _api(series_dir: Path, config: AppConfig) -> TestClient:
    runner = JobRunner(config, tts_factory=lambda: None)
    return TestClient(create_app(config, runner=runner))


# ---------------------------------------------------------------------------
# A. 只读检查：inspect_script_beats
# ---------------------------------------------------------------------------
def test_inspect_reports_homophone_speaker_role() -> None:
    """同音异写的说话人 → homophone_variant + 建议名。"""
    canonicalizer = _canonicalizer(
        characters=[_character("恩彗")],
        names=["恩慧"],
        texts=["恩慧推门而入。"],
    )
    beat = _beat("b1", "恩慧推门而入。", speaker_role="恩慧")
    warnings = canonicalizer.inspect_script_beats([beat])
    role_warnings = [w for w in warnings if w.field == "speaker_role"]
    assert role_warnings, warnings
    warning = role_warnings[0]
    assert warning.reason == "homophone_variant"
    assert warning.suggested_canonical == "恩彗"
    assert warning.found_name == "恩慧"
    assert warning.beat_id == "b1"
    assert warning.beat_index == 0
    assert warning.message


def test_inspect_reports_alias_variant_in_narration_text() -> None:
    """正文里的别名写法 → alias_variant + 上下文 snippet。"""
    canonicalizer = _canonicalizer(
        characters=[_character("道振", aliases=["丈夫", "南道振"])],
        names=["道振"],
        texts=["南道振走进病房。"],
    )
    beat = _beat("b1", "南道振走进病房。", speaker_role="道振")
    warnings = canonicalizer.inspect_script_beats([beat])
    text_warnings = [w for w in warnings if w.field == "narration_text"]
    assert text_warnings, warnings
    warning = text_warnings[0]
    assert warning.reason == "alias_variant"
    assert warning.suggested_canonical == "道振"
    assert "南道振" in warning.snippet


def test_inspect_reports_unregistered_speaker() -> None:
    """不在角色表里的说话人 → unregistered_speaker，且没有建议名。"""
    canonicalizer = _canonicalizer(
        characters=[_character("恩彗")],
        names=["恩彗"],
        texts=["恩彗站在门口。"],
    )
    beat = _beat("b1", "恩彗站在门口。", speaker_role="陌生人")
    warnings = canonicalizer.inspect_script_beats([beat])
    role_warnings = [w for w in warnings if w.field == "speaker_role"]
    assert role_warnings, warnings
    assert role_warnings[0].reason == "unregistered_speaker"
    assert role_warnings[0].suggested_canonical is None


def test_inspect_skips_narrator_and_empty_role() -> None:
    """旁白 / narrator / 空说话人不报。"""
    canonicalizer = _canonicalizer(
        characters=[_character("恩彗")],
        names=["恩彗"],
        texts=["恩彗站在门口。"],
    )
    beats = [
        _beat("b1", "恩彗站在门口。", speaker_role="旁白"),
        _beat("b2", "恩彗站在门口。", speaker_role="narrator"),
        _beat("b3", "恩彗站在门口。", speaker_role=""),
    ]
    warnings = canonicalizer.inspect_script_beats(beats)
    assert [w for w in warnings if w.field == "speaker_role"] == []


def test_inspect_ignores_relation_words() -> None:
    """关系称谓（「丈夫」）不进正文规则，不该报。"""
    canonicalizer = _canonicalizer(
        characters=[_character("道振", aliases=["丈夫", "南道振"])],
        names=["道振"],
        texts=["真没想到我会亲眼看见我未来的丈夫被告白的场面。"],
    )
    beat = _beat(
        "b10",
        "真没想到我会亲眼看见我未来的丈夫被告白的场面。",
        speaker_role="道振",
    )
    warnings = canonicalizer.inspect_script_beats([beat])
    assert warnings == [], warnings


def test_inspect_ignores_protected_phrases() -> None:
    """「小恩小惠」「施以恩惠于」这类惯用词搭配不报。"""
    canonicalizer = _canonicalizer(
        characters=[_character("恩彗")],
        names=["恩彗", "恩慧"],
        texts=["施以恩惠于小恩小惠。"],
    )
    beats = [
        _beat("b1", "施以恩惠于小恩小惠。", speaker_role="旁白"),
    ]
    warnings = canonicalizer.inspect_script_beats(beats)
    assert warnings == [], warnings


def test_inspect_checks_tts_text_field() -> None:
    """送音文本也要检查（字段名 tts_text）。"""
    canonicalizer = _canonicalizer(
        characters=[_character("恩彗")],
        names=["恩慧"],
        texts=["恩慧推门而入。"],
    )
    beat = _beat(
        "b1",
        "雨点砸在窗台上。",
        tts_text="恩慧推门而入。",
        speaker_role="恩慧",
    )
    warnings = canonicalizer.inspect_script_beats([beat])
    fields = {w.field for w in warnings}
    assert "tts_text" in fields, warnings


def test_inspect_skips_tts_text_when_same_as_narration() -> None:
    """Patch v4.25：送音文本与字幕原文去空白后相同时不重复报同一处。"""
    canonicalizer = _canonicalizer(
        characters=[_character("恩彗")],
        names=["恩慧"],
        texts=["恩慧推门而入。"],
    )
    beat = _beat(
        "b1",
        "恩慧推门而入。",
        tts_text="恩慧推门而入。",
        speaker_role="恩慧",
    )
    warnings = canonicalizer.inspect_script_beats([beat])
    assert {w.field for w in warnings} == {"speaker_role", "narration_text"}


def test_inspect_does_not_mutate_beats() -> None:
    """只读检查不得改写任何文本。"""
    canonicalizer = _canonicalizer(
        characters=[_character("恩彗")],
        names=["恩慧"],
        texts=["恩慧推门而入。"],
    )
    beat = _beat("b1", "恩慧推门而入。", speaker_role="恩慧")
    before = beat.model_dump()
    canonicalizer.inspect_script_beats([beat])
    assert beat.model_dump() == before


# ---------------------------------------------------------------------------
# B. 只读检查接口：POST /script/check-characters
# ---------------------------------------------------------------------------
def test_check_characters_endpoint_returns_warnings_without_touching_disk(
    tmp_path: Path,
) -> None:
    """预检接口只回告警，不落盘。"""
    series_id, series_dir, config = _register(tmp_path)
    _bible(series_dir, series_id)
    script_path = _write_script(series_dir, [_beat("b1", "银行行长推门而入。")])
    before = script_path.read_text(encoding="utf-8")

    api = _api(series_dir, config)
    response = api.post(
        f"/api/episodes/{series_id}_ep_001/script/check-characters",
        json={
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "恩慧推门而入，雨水顺着伞骨滑落。",
                    "speaker_role": "恩慧",
                    "target_panel_ids": ["p_001"],
                    "pause_after_ms": 200,
                }
            ]
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["has_warnings"] is True
    warnings = payload["warnings"]
    assert warnings
    reasons = {w["reason"] for w in warnings}
    assert "homophone_variant" in reasons
    assert all({"beat_id", "beat_index", "field", "found_name"} <= set(w) for w in warnings)
    # 磁盘上的剧本不能被预检改动。
    assert script_path.read_text(encoding="utf-8") == before


def test_check_characters_endpoint_without_beats_uses_saved_script(
    tmp_path: Path,
) -> None:
    """不传 beats 时检查磁盘上已落盘的剧本。"""
    series_id, series_dir, config = _register(tmp_path)
    _bible(series_dir, series_id)
    _write_script(
        series_dir,
        [_beat("b1", "恩慧推门而入，雨水顺着伞骨滑落。", speaker_role="恩慧")],
    )

    api = _api(series_dir, config)
    response = api.post(
        f"/api/episodes/{series_id}_ep_001/script/check-characters",
        json={},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["has_warnings"] is True
    assert payload["warnings"]


def test_check_characters_endpoint_reports_no_warnings_for_canonical_names(
    tmp_path: Path,
) -> None:
    """写法本来就规范时没有告警。"""
    series_id, series_dir, config = _register(tmp_path)
    _bible(series_dir, series_id)
    _write_script(series_dir, [_beat("b1", "恩彗推门而入。", speaker_role="恩彗")])

    api = _api(series_dir, config)
    response = api.post(
        f"/api/episodes/{series_id}_ep_001/script/check-characters",
        json={},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["has_warnings"] is False
    assert payload["warnings"] == []


def test_check_characters_endpoint_404_for_unknown_episode(tmp_path: Path) -> None:
    series_id, series_dir, config = _register(tmp_path)
    _bible(series_dir, series_id)
    _write_script(series_dir, [_beat("b1", "银行行长推门而入。")])

    api = _api(series_dir, config)
    response = api.post(
        "/api/episodes/wt_missing_ep_999/script/check-characters",
        json={},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# C. 保存接口：默认原样落盘，显式要求才归一
# ---------------------------------------------------------------------------
def test_put_script_keeps_user_text_and_reports_warnings(tmp_path: Path) -> None:
    """默认保存：原文落盘 + 响应带 character_warnings。"""
    series_id, series_dir, config = _register(tmp_path)
    _bible(series_dir, series_id)
    _write_script(series_dir, [_beat("b1", "银行行长推门而入。")])

    api = _api(series_dir, config)
    response = api.put(
        f"/api/episodes/{series_id}_ep_001/script",
        json={
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "恩慧推门而入，雨水顺着伞骨滑落。",
                    "speaker_role": "恩慧",
                    "target_panel_ids": ["p_001"],
                    "pause_after_ms": 200,
                }
            ]
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert "character_warnings" in payload
    assert payload["character_warnings"], payload["character_warnings"]
    saved = load_model(
        NarrationScript, series_dir / "episodes" / "ep_001" / "04_script" / "script.json"
    )
    assert saved.beats[0].narration_text == "恩慧推门而入，雨水顺着伞骨滑落。"
    assert saved.beats[0].speaker_role == "恩慧"


def test_put_script_auto_unify_flag_unifies_names(tmp_path: Path) -> None:
    """auto_unify_characters=True：按规范名归一后再落盘。"""
    series_id, series_dir, config = _register(tmp_path)
    _bible(series_dir, series_id)
    _write_script(series_dir, [_beat("b1", "银行行长推门而入。")])

    api = _api(series_dir, config)
    response = api.put(
        f"/api/episodes/{series_id}_ep_001/script",
        json={
            "auto_unify_characters": True,
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "恩慧推门而入，雨水顺着伞骨滑落。",
                    "speaker_role": "恩慧",
                    "target_panel_ids": ["p_001"],
                    "pause_after_ms": 200,
                }
            ],
        },
    )
    assert response.status_code == 200
    saved = load_model(
        NarrationScript, series_dir / "episodes" / "ep_001" / "04_script" / "script.json"
    )
    assert saved.beats[0].narration_text == "恩彗推门而入，雨水顺着伞骨滑落。"
    assert saved.beats[0].speaker_role == "恩彗"


def test_put_script_auto_unify_still_spares_relation_words(tmp_path: Path) -> None:
    """即使用户点名归一，关系泛称「丈夫」也不能被改回角色本名。"""
    series_id, series_dir, config = _register(tmp_path)
    _bible(series_dir, series_id)
    _write_script(series_dir, [_beat("b10", "银行行长推门而入。")])

    api = _api(series_dir, config)
    response = api.put(
        f"/api/episodes/{series_id}_ep_001/script",
        json={
            "auto_unify_characters": True,
            "beats": [
                {
                    "beat_id": "b10",
                    "narration_text": "真没想到我会亲眼看见我未来的丈夫被告白的场面。",
                    "speaker_role": "道振",
                    "target_panel_ids": ["p_001"],
                    "pause_after_ms": 200,
                }
            ],
        },
    )
    assert response.status_code == 200
    saved = load_model(
        NarrationScript, series_dir / "episodes" / "ep_001" / "04_script" / "script.json"
    )
    assert saved.beats[0].narration_text == "真没想到我会亲眼看见我未来的丈夫被告白的场面。"


def test_stage4_still_unifies_generated_scripts(tmp_path: Path) -> None:
    """Patch v4.23 只改人工保存，Stage 4 生成阶段照旧自动归一。"""
    canonicalizer = _canonicalizer(
        characters=[_character("恩彗")],
        names=["恩慧"],
        texts=["恩慧推门而入。"],
    )
    payload = canonicalizer.canonicalize_script_dict(
        {
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "恩慧推门而入。",
                    "speaker_role": "恩慧",
                }
            ]
        }
    )
    assert payload["beats"][0]["narration_text"] == "恩彗推门而入。"
    assert payload["beats"][0]["speaker_role"] == "恩彗"


def test_warning_payload_is_json_serialisable(tmp_path: Path) -> None:
    """告警结构要能直接进 JSON 响应。"""
    canonicalizer = _canonicalizer(
        characters=[_character("恩彗")],
        names=["恩慧"],
        texts=["恩慧推门而入。"],
    )
    beat = _beat("b1", "恩慧推门而入。", speaker_role="恩慧")
    warnings = canonicalizer.inspect_script_beats([beat])
    assert warnings
    dumped = json.loads(json.dumps([w.to_dict() for w in warnings]))
    assert dumped
    assert dumped[0]["found_name"] == "恩慧"


# ---------------------------------------------------------------------------
# D. 预检口径：角色表基线（Patch v4.23 补测）
# ---------------------------------------------------------------------------
def test_check_characters_endpoint_flags_unregistered_speaker(tmp_path: Path) -> None:
    """说话人不在作品角色表里 → unregistered_speaker（服务端路径也要能报出来）。"""
    series_id, series_dir, config = _register(tmp_path)
    _bible(series_dir, series_id)
    _write_script(series_dir, [_beat("b1", "银行行长推门而入。")])

    api = _api(series_dir, config)
    response = api.post(
        f"/api/episodes/{series_id}_ep_001/script/check-characters",
        json={
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "一个陌生人敲了敲门。",
                    "speaker_role": "陌生人",
                    "target_panel_ids": ["p_001"],
                    "pause_after_ms": 200,
                }
            ]
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["has_warnings"] is True
    role_warnings = [w for w in payload["warnings"] if w["field"] == "speaker_role"]
    assert role_warnings, payload["warnings"]
    assert role_warnings[0]["reason"] == "unregistered_speaker"
    assert role_warnings[0]["suggested_canonical"] is None


def test_check_characters_endpoint_flags_alias_speaker_role(tmp_path: Path) -> None:
    """说话人写成角色别名（「南道振」）→ alias_variant + 建议规范名。"""
    series_id, series_dir, config = _register(tmp_path)
    _bible(series_dir, series_id)
    _write_script(series_dir, [_beat("b1", "银行行长推门而入。")])

    api = _api(series_dir, config)
    response = api.post(
        f"/api/episodes/{series_id}_ep_001/script/check-characters",
        json={
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "他走进病房。",
                    "speaker_role": "南道振",
                    "target_panel_ids": ["p_001"],
                    "pause_after_ms": 200,
                }
            ]
        },
    )
    assert response.status_code == 200
    payload = response.json()
    role_warnings = [w for w in payload["warnings"] if w["field"] == "speaker_role"]
    assert role_warnings, payload["warnings"]
    assert role_warnings[0]["reason"] == "alias_variant"
    assert role_warnings[0]["suggested_canonical"] == "道振"


def test_check_characters_endpoint_skips_generic_role_slots(tmp_path: Path) -> None:
    """旁白与性别槽位（male/female）不是角色本名，不该报。"""
    series_id, series_dir, config = _register(tmp_path)
    _bible(series_dir, series_id)
    _write_script(series_dir, [_beat("b1", "银行行长推门而入。")])

    api = _api(series_dir, config)
    response = api.post(
        f"/api/episodes/{series_id}_ep_001/script/check-characters",
        json={
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "他清了清嗓子。",
                    "speaker_role": "male",
                    "target_panel_ids": ["p_001"],
                    "pause_after_ms": 200,
                },
                {
                    "beat_id": "b2",
                    "narration_text": "她转过身。",
                    "speaker_role": "narrator",
                    "target_panel_ids": ["p_001"],
                    "pause_after_ms": 200,
                },
            ]
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["has_warnings"] is False, payload["warnings"]
    assert payload["warnings"] == []


def test_inspect_skips_generic_role_slots() -> None:
    """通用说话人标记（male / female / 独白）不参与角色名检查。"""
    canonicalizer = _canonicalizer(
        characters=[_character("恩彗")],
        names=["恩彗"],
        texts=["恩彗站在门口。"],
    )
    beats = [
        _beat("b1", "他清了清嗓子。", speaker_role="male"),
        _beat("b2", "她转过身。", speaker_role="female_lead"),
        _beat("b3", "她低声说道。", speaker_role="独白"),
    ]
    warnings = canonicalizer.inspect_script_beats(beats)
    assert [w for w in warnings if w.field == "speaker_role"] == []
