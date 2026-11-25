"""Patch v4.43：全局音效资源库（增删改替换）+ 纯 [sfx:key] 单入口 + S4 上下文推荐。

覆盖五组：

1. **动态曲库纯函数**：注册表 ``custom_sfx.json`` 缺失 / 损坏时安全回退；
   内置 24 条 + 自定义 + 覆盖元数据 − 已停用 = 生效曲库；
   内置 ``SFX_CUE_CATALOG`` / ``SFX_CUE_KEYS`` **一字未改**（兼容旧单测）。
2. **自定义 key 全链路**：注册进曲库后，``normalize_inline_sfx_tags`` 认它、
   ``resolve_sfx_asset`` 找得到它的音频、``is_valid_sfx_key`` 放行。
3. **S4 上下文推荐**：``enrich_sfx_suggestions`` 按台词关键词 / 画面拟声词 /
   切幕语境补建议，最多 3 条、不重复推荐已有内联标签。
4. **方案 B 联动**：``apply_auto_motions`` / ``apply_narrative_pacing`` 在
   **只有 ``[sfx:key]``、没有 ``sfx_cue``** 的纯新方案下依旧生效。
5. **Server 曲库 CRUD**：新增 / 改元数据 / 上传替换 / 删除 / 停用 / 还原，
   以及剧本保存时保留 ``sfx_suggestions``。

全部离线：纯函数 + TestClient + 临时盘面，不触网、不消耗 API 额度。
"""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.config import AppConfig, WorkspaceConfig
from core.normalize import (
    SFX_CUE_CATALOG,
    SFX_CUE_KEYS,
    get_effective_sfx_catalog,
    get_effective_sfx_keys,
    is_valid_sfx_key,
    load_custom_sfx_config,
    normalize_inline_sfx_tags,
    normalize_sfx_suggestions,
    save_custom_sfx_config,
)
from core.schemas import (
    EpisodeDigest,
    NarrationScript,
    ScriptBeat,
    SfxSuggestion,
    load_model,
    save_model,
)
from stages import s4_scriptwriter, s7_renderer
from web.server import create_app

#: 内置曲库条目数（Patch v4.38 起一直是 24，v4.43 不许改）。
BUILTIN_COUNT = 24

#: 自定义音效键的合法形状（后端 SFX_CUSTOM_KEY_RE）。
CUSTOM_KEY = "knife_slice"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture()
def assets(tmp_path: Path) -> Path:
    """一个干净的盘面目录（只含 sfx 子目录）。"""
    root = tmp_path / "assets"
    (root / s7_renderer.SFX_DIR_NAME).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def client(tmp_path: Path, assets: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """把「盘面」指到临时目录的 TestClient。

    三处都要改：``core.config.assets_root``（normalize 的注册表走局部导入）、
    ``web.server.assets_root``（曲库清单读 SOURCES_CC0.json）、
    ``s7_renderer.assets_root``（素材查找）。
    """
    monkeypatch.setattr("core.config.assets_root", lambda: assets)
    monkeypatch.setattr("web.server.assets_root", lambda: assets)
    monkeypatch.setattr(s7_renderer, "assets_root", lambda: assets)
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path / "ws"))
    return TestClient(create_app(config))


def _register_custom(
    assets: Path,
    key: str = CUSTOM_KEY,
    *,
    label: str = "刀锋划过",
    group: str = "动作",
    category: str = "击打",
    fallback_key: str = "",
) -> None:
    """往注册表里塞一条自定义音效。"""
    config = load_custom_sfx_config(assets)
    config["custom"].append(
        {
            "key": key,
            "label": label,
            "group": group,
            "category": category,
            "fallback_key": fallback_key,
        }
    )
    save_custom_sfx_config(config, assets)


def _items(resp: Any) -> list[dict[str, Any]]:
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data["items"], list)
    return data["items"]


def _by_key(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["key"]: item for item in items}


def _tiny_wav_b64() -> str:
    """一段 0.1 秒静音 WAV 的 base64（够做上传链路测试）。"""
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x00\x00" * 2400)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


# ---------------------------------------------------------------------------
# 1) 动态曲库纯函数
# ---------------------------------------------------------------------------
def test_builtin_catalog_and_keys_are_untouched() -> None:
    """v4.43 不得改动内置 24 条 —— 旧单测（v4.38）仍要过。"""
    assert len(SFX_CUE_CATALOG) == BUILTIN_COUNT
    assert len(SFX_CUE_KEYS) == BUILTIN_COUNT
    assert {entry["key"] for entry in SFX_CUE_CATALOG} == set(SFX_CUE_KEYS)


def test_missing_registry_falls_back_to_empty_config(assets: Path) -> None:
    config = load_custom_sfx_config(assets)
    assert config["custom"] == []
    assert config["overrides"] == {}
    assert config["hidden"] == []


def test_corrupt_registry_falls_back_to_empty_config(assets: Path) -> None:
    path = assets / s7_renderer.SFX_DIR_NAME / "custom_sfx.json"
    path.write_text("{ not json at all", encoding="utf-8")
    config = load_custom_sfx_config(assets)
    assert config["custom"] == []
    assert config["overrides"] == {}


def test_registry_roundtrip(assets: Path) -> None:
    _register_custom(assets)
    config = load_custom_sfx_config(assets)
    assert [item["key"] for item in config["custom"]] == [CUSTOM_KEY]
    assert config["custom"][0]["label"] == "刀锋划过"


def test_effective_catalog_merges_custom_and_overrides(assets: Path) -> None:
    _register_custom(assets)
    config = load_custom_sfx_config(assets)
    config["overrides"]["heart_thump"] = {"label": "咚咚咚"}
    save_custom_sfx_config(config, assets)

    catalog = get_effective_sfx_catalog(assets)
    by_key = {entry["key"]: entry for entry in catalog}
    assert len(catalog) == BUILTIN_COUNT + 1
    assert by_key[CUSTOM_KEY]["label"] == "刀锋划过"
    assert by_key["heart_thump"]["label"] == "咚咚咚"
    # 未覆盖的字段仍是内置值
    assert by_key["heart_thump"]["group"] == "动作"


def test_effective_catalog_drops_hidden_entries(assets: Path) -> None:
    config = load_custom_sfx_config(assets)
    config["hidden"] = ["thunder"]
    save_custom_sfx_config(config, assets)

    catalog = get_effective_sfx_catalog(assets)
    keys = {entry["key"] for entry in catalog}
    assert "thunder" not in keys
    assert len(catalog) == BUILTIN_COUNT - 1


def test_effective_keys_follow_dynamic_catalog(assets: Path) -> None:
    assert CUSTOM_KEY not in get_effective_sfx_keys(assets)
    _register_custom(assets)
    assert CUSTOM_KEY in get_effective_sfx_keys(assets)


def test_is_valid_sfx_key_accepts_builtin_and_custom(assets: Path) -> None:
    assert is_valid_sfx_key("heart_thump", assets) is True
    assert is_valid_sfx_key("not_a_real_key", assets) is False
    _register_custom(assets)
    assert is_valid_sfx_key(CUSTOM_KEY, assets) is True


def test_hidden_builtin_key_is_no_longer_valid(assets: Path) -> None:
    config = load_custom_sfx_config(assets)
    config["hidden"] = ["thunder"]
    save_custom_sfx_config(config, assets)
    assert is_valid_sfx_key("thunder", assets) is False


# ---------------------------------------------------------------------------
# 2) 自定义 key 全链路
# ---------------------------------------------------------------------------
def test_normalize_inline_sfx_tags_accepts_custom_key(assets: Path) -> None:
    _register_custom(assets)
    assert (
        normalize_inline_sfx_tags("他[sfx:knife_slice]一刀划过", assets_dir=assets)
        == "他[sfx:knife_slice]一刀划过"
    )


def test_normalize_inline_sfx_tags_strips_unknown_key(assets: Path) -> None:
    assert (
        normalize_inline_sfx_tags("他[sfx:unknown_key]一刀划过", assets_dir=assets)
        == "他一刀划过"
    )


def test_normalize_sfx_suggestions_respects_dynamic_keys(assets: Path) -> None:
    _register_custom(assets)
    result = normalize_sfx_suggestions(
        [{"key": CUSTOM_KEY, "reason": "画面里有一道刀光"}],
        assets_dir=assets,
    )
    assert result == [
        {
            "key": CUSTOM_KEY,
            "reason": "画面里有一道刀光",
            "source": "llm",
            "anchor_text": "",
        }
    ]
    # 未注册的 key 被剔除
    assert normalize_sfx_suggestions([{"key": "nope_key"}], assets_dir=assets) == []


def test_resolve_sfx_asset_finds_custom_audio(assets: Path) -> None:
    _register_custom(assets)
    custom_dir = assets / s7_renderer.SFX_DIR_NAME / s7_renderer.SFX_CUSTOM_AUDIO_DIRNAME
    custom_dir.mkdir(parents=True, exist_ok=True)
    target = custom_dir / f"{CUSTOM_KEY}.wav"
    target.write_bytes(b"RIFF-fake")

    resolved = s7_renderer.resolve_sfx_asset(CUSTOM_KEY, assets_dir=assets)
    assert resolved == target


def test_resolve_sfx_asset_prefers_custom_over_builtin(assets: Path) -> None:
    """用户上传的音频优先于仓库默认素材。"""
    sfx_dir = assets / s7_renderer.SFX_DIR_NAME
    builtin = sfx_dir / "heart_thump.wav"
    builtin.write_bytes(b"RIFF-builtin")
    custom_dir = sfx_dir / s7_renderer.SFX_CUSTOM_AUDIO_DIRNAME
    custom_dir.mkdir(parents=True, exist_ok=True)
    custom = custom_dir / "heart_thump.wav"
    custom.write_bytes(b"RIFF-custom")

    resolved = s7_renderer.resolve_sfx_asset("heart_thump", assets_dir=assets)
    assert resolved == custom


def test_resolve_sfx_asset_uses_custom_fallback_chain(assets: Path) -> None:
    """自定义音效配了兜底键时，素材缺失按兜底回源。"""
    _register_custom(assets, fallback_key="whoosh")
    sfx_dir = assets / s7_renderer.SFX_DIR_NAME
    fallback = sfx_dir / "whoosh.wav"
    fallback.write_bytes(b"RIFF-fallback")

    resolved = s7_renderer.resolve_sfx_asset(CUSTOM_KEY, assets_dir=assets)
    assert resolved == fallback


# ---------------------------------------------------------------------------
# 3) S4 上下文推荐
# ---------------------------------------------------------------------------
def _beat(**kwargs: Any) -> dict[str, Any]:
    base = {
        "beat_id": "b1",
        "narration_text": "他一拳砸在桌上。",
        "tts_text": "",
        "target_panel_ids": ["p_001"],
        "scene_id": None,
    }
    base.update(kwargs)
    return base


def test_enrich_sfx_suggestions_from_keyword() -> None:
    beats = [_beat(narration_text="他一巴掌扇了过去。", tts_text="他一巴掌扇了过去。")]
    result = s4_scriptwriter.enrich_sfx_suggestions(beats)
    keys = [item["key"] for item in result[0]["sfx_suggestions"]]
    assert "slap" in keys
    entry = next(item for item in result[0]["sfx_suggestions"] if item["key"] == "slap")
    assert entry["source"] == "context_rule"
    assert entry["reason"]


def test_enrich_sfx_suggestions_skips_existing_inline_tag() -> None:
    beats = [_beat(tts_text="他一巴掌扇了过去。[sfx:slap]")]
    result = s4_scriptwriter.enrich_sfx_suggestions(beats)
    keys = [item["key"] for item in result[0]["sfx_suggestions"]]
    assert "slap" not in keys


def test_enrich_sfx_suggestions_keeps_model_suggestions() -> None:
    beats = [
        _beat(
            tts_text="安静。",
            sfx_suggestions=[
                {"key": "heart_thump", "reason": "心跳加速", "source": "llm"}
            ],
        )
    ]
    result = s4_scriptwriter.enrich_sfx_suggestions(beats)
    first = result[0]["sfx_suggestions"][0]
    assert first["key"] == "heart_thump"
    assert first["source"] == "llm"


def test_enrich_sfx_suggestions_caps_at_three() -> None:
    beats = [
        _beat(
            narration_text="摔门、玻璃碎裂、雷声轰鸣、一巴掌扇过去、脚步声响起。",
            tts_text="摔门、玻璃碎裂、雷声轰鸣、一巴掌扇过去、脚步声响起。",
        )
    ]
    result = s4_scriptwriter.enrich_sfx_suggestions(beats)
    assert len(result[0]["sfx_suggestions"]) <= 3


def test_enrich_sfx_suggestions_uses_panel_sfx_list() -> None:
    panels = [SimpleNamespace(panel_id="p_001", sfx_list=["雷声"])]
    beats = [_beat(narration_text="天色暗了下来。", tts_text="天色暗了下来。")]
    result = s4_scriptwriter.enrich_sfx_suggestions(beats, panels)
    keys = [item["key"] for item in result[0]["sfx_suggestions"]]
    assert "thunder" in keys


# ---------------------------------------------------------------------------
# 4) 方案 B 联动（只有 [sfx:key]，没有 sfx_cue）
# ---------------------------------------------------------------------------
def test_apply_auto_motions_detects_inline_impact_sfx() -> None:
    """纯 [sfx:key] 模式下，高燃句依然要震。"""
    beats = [
        _beat(
            narration_text="一拳砸下！",
            tts_text="一拳砸下！[sfx:punch]",
            emotion="angry",
        )
    ]
    assigned, _report = s4_scriptwriter.apply_auto_motions(beats)
    assert assigned[0]["motion"] == "shake"


def test_apply_auto_motions_detects_inline_sfx_without_emotion() -> None:
    beats = [
        _beat(narration_text="门被猛地踹开。", tts_text="门被猛地踹开。[sfx:door_slam]")
    ]
    assigned, _report = s4_scriptwriter.apply_auto_motions(beats)
    assert assigned[0]["motion"] == "shake"


def test_apply_narrative_pacing_detects_inline_sfx_turn() -> None:
    """纯 [sfx:key] 模式下，转折句依然要留 950ms 白。"""
    beats = [
        _beat(
            narration_text="竟然是他。",
            tts_text="竟然是他。[sfx:crash]",
            pause_after_ms=100,
        )
    ]
    paced, _report = s4_scriptwriter.apply_narrative_pacing(beats)
    assert paced[0]["pause_after_ms"] >= 950


# ---------------------------------------------------------------------------
# 5) Server 曲库 CRUD
# ---------------------------------------------------------------------------
def test_get_sfx_library_reports_ownership(client: TestClient) -> None:
    items = _items(client.get("/api/sfx-library"))
    assert len(items) == BUILTIN_COUNT
    by_key = _by_key(items)
    assert by_key["heart_thump"]["is_builtin"] is True
    assert by_key["heart_thump"]["has_custom_audio"] is False
    assert by_key["heart_thump"]["source_type"] in {
        "builtin_cc0",
        "freesound_cc0",
    }


def test_create_custom_sfx_asset(client: TestClient, assets: Path) -> None:
    resp = client.post(
        "/api/sfx-library",
        json={
            "key": CUSTOM_KEY,
            "label": "刀锋划过",
            "group": "动作",
            "category": "击打",
            "fallback_key": "",
        },
    )
    assert resp.status_code == 200, resp.text
    items = _by_key(_items(resp))
    assert CUSTOM_KEY in items
    assert items[CUSTOM_KEY]["is_builtin"] is False
    assert items[CUSTOM_KEY]["label"] == "刀锋划过"
    # 已登记但还没上传音频的自定义条目，不能被误标成「仓库内置 CC0」。
    assert items[CUSTOM_KEY]["source_type"] == "custom"
    assert items[CUSTOM_KEY]["has_custom_audio"] is False
    assert items[CUSTOM_KEY]["available"] is False
    # 注册表落盘
    assert (assets / s7_renderer.SFX_DIR_NAME / "custom_sfx.json").is_file()


def test_create_rejects_invalid_key(client: TestClient) -> None:
    resp = client.post(
        "/api/sfx-library",
        json={"key": "Bad-Key", "label": "非法键", "group": "动作", "category": "x"},
    )
    assert resp.status_code == 400
    assert "音效键" in resp.json()["detail"]


def test_create_rejects_duplicate_key(client: TestClient) -> None:
    resp = client.post(
        "/api/sfx-library",
        json={"key": "heart_thump", "label": "重复", "group": "动作", "category": "x"},
    )
    assert resp.status_code == 400
    assert "已存在" in resp.json()["detail"]


def test_patch_updates_label(client: TestClient) -> None:
    resp = client.patch(
        "/api/sfx-library/heart_thump",
        json={"label": "咚咚咚", "group": "动作", "category": "生理"},
    )
    assert resp.status_code == 200, resp.text
    items = _by_key(_items(resp))
    assert items["heart_thump"]["label"] == "咚咚咚"


def test_delete_builtin_requires_explicit_mode(client: TestClient) -> None:
    resp = client.delete("/api/sfx-library/heart_thump")
    assert resp.status_code == 400
    assert "不能直接删除" in resp.json()["detail"]


def test_disable_and_restore_builtin(client: TestClient) -> None:
    resp = client.delete("/api/sfx-library/thunder?mode=disable")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "thunder" in body["hidden_builtin_keys"]
    assert "thunder" not in _by_key(body["items"])

    resp = client.post("/api/sfx-library/thunder/restore")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "thunder" not in body["hidden_builtin_keys"]
    assert "thunder" in _by_key(body["items"])


def test_delete_custom_removes_entry_and_audio(client: TestClient, assets: Path) -> None:
    client.post(
        "/api/sfx-library",
        json={
            "key": CUSTOM_KEY,
            "label": "刀锋划过",
            "group": "动作",
            "category": "击打",
        },
    )
    custom_dir = assets / s7_renderer.SFX_DIR_NAME / s7_renderer.SFX_CUSTOM_AUDIO_DIRNAME
    custom_dir.mkdir(parents=True, exist_ok=True)
    (custom_dir / f"{CUSTOM_KEY}.wav").write_bytes(b"RIFF-fake")

    resp = client.delete(f"/api/sfx-library/{CUSTOM_KEY}")
    assert resp.status_code == 200, resp.text
    assert CUSTOM_KEY not in _by_key(_items(resp))
    assert not (custom_dir / f"{CUSTOM_KEY}.wav").exists()


def test_upload_replaces_audio_into_custom_dir(client: TestClient, assets: Path) -> None:
    resp = client.post(
        "/api/sfx-library/upload",
        json={
            "sfx_key": "heart_thump",
            "filename": "heart.wav",
            "content_base64": _tiny_wav_b64(),
        },
    )
    assert resp.status_code == 200, resp.text
    items = _by_key(_items(resp))
    assert items["heart_thump"]["has_custom_audio"] is True
    assert items["heart_thump"]["source_type"] == "custom_upload"
    custom_dir = assets / s7_renderer.SFX_DIR_NAME / s7_renderer.SFX_CUSTOM_AUDIO_DIRNAME
    assert (custom_dir / "heart_thump.wav").is_file()


def test_restore_audio_removes_uploaded_override(client: TestClient, assets: Path) -> None:
    client.post(
        "/api/sfx-library/upload",
        json={
            "sfx_key": "heart_thump",
            "filename": "heart.wav",
            "content_base64": _tiny_wav_b64(),
        },
    )
    resp = client.delete("/api/sfx-library/heart_thump?mode=restore_audio")
    assert resp.status_code == 200, resp.text
    items = _by_key(_items(resp))
    assert items["heart_thump"]["has_custom_audio"] is False


def test_sfx_file_rejects_unknown_key(client: TestClient) -> None:
    resp = client.get("/api/sfx-file/not_a_real_key")
    assert resp.status_code == 400
    assert "音效键" in resp.json()["detail"]


def test_sfx_file_serves_custom_key(client: TestClient, assets: Path) -> None:
    _register_custom(assets)
    custom_dir = assets / s7_renderer.SFX_DIR_NAME / s7_renderer.SFX_CUSTOM_AUDIO_DIRNAME
    custom_dir.mkdir(parents=True, exist_ok=True)
    (custom_dir / f"{CUSTOM_KEY}.wav").write_bytes(b"RIFF-fake")

    resp = client.get(f"/api/sfx-file/{CUSTOM_KEY}")
    assert resp.status_code == 200, resp.text
    assert resp.content == b"RIFF-fake"


# ---------------------------------------------------------------------------
# 6) 剧本保存保留 sfx_suggestions
# ---------------------------------------------------------------------------
def test_put_script_keeps_sfx_suggestions(tmp_path: Path, assets: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """保存剧本时，音效建议不能被丢掉。"""
    monkeypatch.setattr("core.config.assets_root", lambda: assets)
    monkeypatch.setattr("web.server.assets_root", lambda: assets)
    monkeypatch.setattr(s7_renderer, "assets_root", lambda: assets)

    workspace = tmp_path / "ws"
    source = tmp_path / "src" / "ep_001"
    source.mkdir(parents=True)
    Image.new("RGB", (400, 600), (30, 60, 90)).save(source / "001.png")

    client = TestClient(create_app(AppConfig(workspace=WorkspaceConfig(root=workspace))))
    series_id = client.post("/api/series", json={"url": str(source)}).json()["series_id"]
    episode_id = f"{series_id}_ep_001"

    episode_dir = workspace / series_id / "episodes" / "ep_001"
    panels_dir = episode_dir / "02_panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (360, 640), (10, 20, 30)).save(panels_dir / "p_001.png")

    script = NarrationScript(
        episode_id=episode_id,
        hook_title="音效建议接口测试",
        beats=[
            ScriptBeat(
                beat_id="b1",
                narration_text="他一拳砸下。",
                tts_text="他一拳砸下。[sfx:punch]",
                target_panel_ids=["p_001"],
                sfx_suggestions=[
                    SfxSuggestion(
                        key="punch",
                        reason="有冲击动作",
                        source="llm",
                        anchor_text="一拳",
                    )
                ],
            )
        ],
        episode_digest=EpisodeDigest(episode_no=1, one_line_hook="钩子", summary="摘要"),
    )
    script_dir = episode_dir / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    save_model(script, script_dir / "script.json")

    resp = client.put(
        f"/api/episodes/{episode_id}/script",
        json={"beats": [beat.model_dump() for beat in script.beats]},
    )
    assert resp.status_code == 200, resp.text

    saved = load_model(NarrationScript, script_dir / "script.json")
    assert len(saved.beats) == 1
    suggestion = saved.beats[0].sfx_suggestions[0]
    assert suggestion.key == "punch"
    assert suggestion.anchor_text == "一拳"
    assert suggestion.source == "llm"
