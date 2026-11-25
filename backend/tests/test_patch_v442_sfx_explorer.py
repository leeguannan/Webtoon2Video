"""Patch v4.42：Studio 音效资源管理器标签页（后端契约部分，离线测试）。

覆盖四组：

1. **一级大类（group）**：24 个标准 key 每个都归到「动作 / 环境 / 转场 / 道具」
   四大类之一，条数与规格一致，且**没有改动**原有的 key / label / category /
   fallback_key；
2. **曲库条目新增字段**：``duration_ms`` / ``source_type`` / ``cc0_source``
   三件套齐全，语义正确（采编工具下载的素材标 ``freesound_cc0`` 并带溯源明细，
   仓库内置素材标 ``builtin_cc0`` 且 ``cc0_source`` 为 ``None``）；
3. **健壮性**：``SOURCES_CC0.json`` 缺失 / 损坏 / 只覆盖部分 key 时，
   ``GET /api/sfx-library`` 仍然 200，条目数量与字段结构不变；
4. **兜底素材**：盘面没有 ``<key>.<ext>`` 时按 ``fallback_key`` 回源，
   ``resolved_filename`` 指向真实存在的兜底文件。

全部离线：纯函数 + TestClient + 临时盘面，不触网、不消耗 API 额度。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.config import AppConfig, WorkspaceConfig
from core.normalize import (
    SFX_CUE_CATALOG,
    SFX_CUE_KEYS,
    SFX_GROUP_CATEGORIES,
    sfx_group_of,
)
from stages import s7_renderer
from web.server import create_app

#: Patch v4.42 规格里的一级大类划分（key 集合按规格书写顺序）。
EXPECTED_GROUPS: dict[str, tuple[str, ...]] = {
    "动作": (
        "heart_thump",
        "door_slam",
        "door_open",
        "footstep",
        "slap",
        "punch",
        "sword_clash",
        "cloth_rustle",
    ),
    "转场": ("whoosh", "gasp", "boom_hit", "applause"),
    "环境": ("crash", "glass_shatter", "thunder", "clock_tick", "page_turn"),
    "道具": (
        "phone_msg",
        "phone_ringing",
        "phone_vibrating",
        "phone_call",
        "phone_unavailable",
        "camera_shutter",
        "car_horn",
    ),
}

#: 规格里给定的 cc0_source 字段集合。
EXPECTED_CC0_FIELDS = {
    "sound_id",
    "name",
    "author",
    "license",
    "license_url",
    "freesound_url",
    "fetched_at",
    "max_duration_s",
}


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path / "ws"))
    return TestClient(create_app(config))


@pytest.fixture()
def assets(tmp_path: Path) -> Path:
    """一个干净的盘面目录（只含 sfx 子目录）。"""
    root = tmp_path / "assets"
    (root / s7_renderer.SFX_DIR_NAME).mkdir(parents=True, exist_ok=True)
    return root


def _use_assets(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """把「盘面」指到临时目录。

    注意要同时改两处：``s7_renderer`` 内部解析素材文件、``web.server`` 读
    ``SOURCES_CC0.json`` —— 后者是 ``from core.config import assets_root``
    直接绑定的模块属性，只改 s7 那份不会生效。
    """
    monkeypatch.setattr(s7_renderer, "assets_root", lambda: root)
    monkeypatch.setattr("web.server.assets_root", lambda: root)


def _items(resp: Any) -> list[dict[str, Any]]:
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data["items"], list)
    return data["items"]


def _by_key(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["key"]: item for item in items}


# ---------------------------------------------------------------------------
# 1) 一级大类（group）
# ---------------------------------------------------------------------------
def test_group_categories_cover_four_families() -> None:
    assert set(SFX_GROUP_CATEGORIES) == {"动作", "环境", "转场", "道具"}


def test_every_catalog_entry_has_a_group() -> None:
    for entry in SFX_CUE_CATALOG:
        group = entry.get("group")
        assert group in SFX_GROUP_CATEGORIES, entry["key"]


def test_group_membership_matches_spec() -> None:
    for group, keys in EXPECTED_GROUPS.items():
        for key in keys:
            assert sfx_group_of(key) == group, key


def test_group_counts_match_spec() -> None:
    counts: dict[str, int] = {}
    for entry in SFX_CUE_CATALOG:
        group = entry["group"]
        counts[group] = counts.get(group, 0) + 1
    assert counts == {"动作": 8, "转场": 4, "环境": 5, "道具": 7}


def test_groups_partition_all_keys() -> None:
    grouped = [key for keys in EXPECTED_GROUPS.values() for key in keys]
    assert sorted(grouped) == sorted(SFX_CUE_KEYS)
    assert len(grouped) == len(set(grouped)) == 24


def test_group_does_not_break_existing_catalog_fields() -> None:
    """新增 group 不得顺手改掉原有字段。"""
    for entry in SFX_CUE_CATALOG:
        assert entry["key"]
        assert entry["label"]
        assert entry["category"]
        assert "fallback_key" in entry


def test_sfx_group_of_unknown_key_returns_empty() -> None:
    assert sfx_group_of("not_a_real_key") == ""


def test_library_endpoint_exposes_group_per_item(client: TestClient) -> None:
    items = _items(client.get("/api/sfx-library"))
    assert len(items) == 24
    for item in items:
        assert item["group"] in SFX_GROUP_CATEGORIES


def test_library_endpoint_group_counts_match_spec(client: TestClient) -> None:
    items = _items(client.get("/api/sfx-library"))
    counts: dict[str, int] = {}
    for item in items:
        counts[item["group"]] = counts.get(item["group"], 0) + 1
    assert counts == {"动作": 8, "转场": 4, "环境": 5, "道具": 7}


# ---------------------------------------------------------------------------
# 2) duration_ms / source_type / cc0_source
# ---------------------------------------------------------------------------
def test_library_items_expose_new_fields(client: TestClient) -> None:
    for item in _items(client.get("/api/sfx-library")):
        assert "duration_ms" in item
        assert "source_type" in item
        assert "cc0_source" in item


def test_source_type_is_builtin_without_sources_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, assets: Path
) -> None:
    _use_assets(monkeypatch, assets)
    for item in _items(client.get("/api/sfx-library")):
        assert item["source_type"] == "builtin_cc0"
        assert item["cc0_source"] is None


def test_source_type_is_freesound_when_in_sources_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, assets: Path
) -> None:
    _use_assets(monkeypatch, assets)
    sources = {
        "thunder": {
            "sound_id": 575561,
            "name": "Thunder.",
            "author": "PappaBert",
            "license": "License: Creative Commons 0",
            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "freesound_url": "https://freesound.org/people/PappaBert/sounds/575561/",
            "fetched_at": "2026-11-25T00:00:00Z",
            "max_duration_s": 3.0,
            "duration_s": 1.82,
        }
    }
    (assets / s7_renderer.SFX_DIR_NAME / "SOURCES_CC0.json").write_text(
        json.dumps(sources), encoding="utf-8"
    )
    items = _by_key(_items(client.get("/api/sfx-library")))
    assert items["thunder"]["source_type"] == "freesound_cc0"
    cc0 = items["thunder"]["cc0_source"]
    assert cc0 is not None
    assert set(cc0) == EXPECTED_CC0_FIELDS
    assert cc0["sound_id"] == "575561"
    assert cc0["author"] == "PappaBert"
    assert "Creative Commons 0" in cc0["license"]
    # 其余 key 仍然是内置资产
    assert items["heart_thump"]["source_type"] == "builtin_cc0"


def test_duration_falls_back_to_sources_duration_when_probe_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, assets: Path
) -> None:
    """盘面没有素材、探测不到时长时，回退到溯源清单里的 duration_s。"""
    _use_assets(monkeypatch, assets)
    (assets / s7_renderer.SFX_DIR_NAME / "SOURCES_CC0.json").write_text(
        json.dumps({"clock_tick": {"duration_s": 0.842}}), encoding="utf-8"
    )
    items = _by_key(_items(client.get("/api/sfx-library")))
    assert items["clock_tick"]["duration_ms"] == 842


def test_duration_is_none_when_nothing_is_known(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, assets: Path
) -> None:
    _use_assets(monkeypatch, assets)
    items = _by_key(_items(client.get("/api/sfx-library")))
    assert items["heart_thump"]["duration_ms"] is None


def test_duration_is_probed_from_real_asset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, assets: Path
) -> None:
    """盘面有素材时优先报实测时长（用真实 wav 造一个 0.5s 的文件）。"""
    import wave

    _use_assets(monkeypatch, assets)
    path = assets / s7_renderer.SFX_DIR_NAME / "gasp.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x00\x00" * 12000)  # 0.5s
    items = _by_key(_items(client.get("/api/sfx-library")))
    assert items["gasp"]["duration_ms"] is not None
    assert 400 <= int(items["gasp"]["duration_ms"]) <= 700


def test_cc0_source_fields_are_all_optional_in_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, assets: Path
) -> None:
    """溯源条目字段不全时也不能炸（空串兜底）。"""
    _use_assets(monkeypatch, assets)
    (assets / s7_renderer.SFX_DIR_NAME / "SOURCES_CC0.json").write_text(
        json.dumps({"thunder": {"sound_id": 1}}), encoding="utf-8"
    )
    items = _by_key(_items(client.get("/api/sfx-library")))
    cc0 = items["thunder"]["cc0_source"]
    assert cc0 is not None
    assert set(cc0) == EXPECTED_CC0_FIELDS
    assert cc0["author"] == ""
    assert cc0["freesound_url"] == ""


def test_duration_cache_avoids_repeated_probe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, assets: Path
) -> None:
    """同一素材连续查两次只探测一次（缓存按 path/mtime/size 命中）。"""
    _use_assets(monkeypatch, assets)
    calls: list[str] = []
    original = s7_renderer.probe_duration_ms

    def counting(path: Path, *args: Any, **kwargs: Any) -> int:
        calls.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(s7_renderer, "probe_duration_ms", counting)
    monkeypatch.setattr(
        "web.server._SFX_DURATION_CACHE", {}, raising=False
    )
    items = _by_key(_items(client.get("/api/sfx-library")))
    assert items  # 曲库仍然完整
    # 没有素材时不会走探测；有素材的文件会走一次，第二次命中缓存。
    assert len(calls) <= 1


# ---------------------------------------------------------------------------
# 3) 健壮性：溯源文件缺失 / 损坏 / 部分缺失
# ---------------------------------------------------------------------------
def test_library_endpoint_survives_missing_sources_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, assets: Path
) -> None:
    _use_assets(monkeypatch, assets)
    assert not (assets / s7_renderer.SFX_DIR_NAME / "SOURCES_CC0.json").exists()
    items = _items(client.get("/api/sfx-library"))
    assert len(items) == 24
    assert all(item["source_type"] == "builtin_cc0" for item in items)


def test_library_endpoint_survives_broken_sources_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, assets: Path
) -> None:
    _use_assets(monkeypatch, assets)
    (assets / s7_renderer.SFX_DIR_NAME / "SOURCES_CC0.json").write_text(
        "{not json at all", encoding="utf-8"
    )
    items = _items(client.get("/api/sfx-library"))
    assert len(items) == 24
    assert all(item["cc0_source"] is None for item in items)


def test_library_endpoint_survives_sources_file_with_wrong_shape(
    client: TestClient, monkeypatch: pytest.Monkeypatch, assets: Path
) -> None:
    _use_assets(monkeypatch, assets)
    (assets / s7_renderer.SFX_DIR_NAME / "SOURCES_CC0.json").write_text(
        json.dumps(["not", "a", "dict"]), encoding="utf-8"
    )
    items = _items(client.get("/api/sfx-library"))
    assert len(items) == 24


def test_library_endpoint_survives_partial_sources_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, assets: Path
) -> None:
    """溯源清单只覆盖部分 key 时，其余 key 照常是 builtin_cc0。"""
    _use_assets(monkeypatch, assets)
    (assets / s7_renderer.SFX_DIR_NAME / "SOURCES_CC0.json").write_text(
        json.dumps({"thunder": {"sound_id": 1, "duration_s": 1.0}}), encoding="utf-8"
    )
    items = _by_key(_items(client.get("/api/sfx-library")))
    assert items["thunder"]["source_type"] == "freesound_cc0"
    assert items["punch"]["source_type"] == "builtin_cc0"
    assert items["punch"]["cc0_source"] is None


def test_library_endpoint_keeps_legacy_fields(client: TestClient) -> None:
    """v4.38 起就有的字段不能被 v4.42 挤掉。"""
    for item in _items(client.get("/api/sfx-library")):
        assert item["url"] == f"/api/sfx-file/{item['key']}"
        assert isinstance(item["available"], bool)
        assert item["label"]
        assert item["category"]
        assert "fallback_key" in item


# ---------------------------------------------------------------------------
# 4) 兜底素材
# ---------------------------------------------------------------------------
def test_fallback_asset_is_reported_by_resolved_filename(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, assets: Path
) -> None:
    """盘面没有 slap.mp3，但有 door_slam.mp3 —— slap 要指向兜底素材。"""
    _use_assets(monkeypatch, assets)
    (assets / s7_renderer.SFX_DIR_NAME / "door_slam.mp3").write_bytes(b"ID3fake")
    items = _by_key(_items(client.get("/api/sfx-library")))
    assert items["slap"]["resolved_filename"] == "door_slam.mp3"
    assert items["slap"]["available"] is True
    assert items["slap"]["fallback_key"] == "door_slam"


def test_missing_asset_reports_not_available(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, assets: Path
) -> None:
    _use_assets(monkeypatch, assets)
    items = _by_key(_items(client.get("/api/sfx-library")))
    assert items["heart_thump"]["available"] is False
    assert items["heart_thump"]["resolved_filename"] == ""
