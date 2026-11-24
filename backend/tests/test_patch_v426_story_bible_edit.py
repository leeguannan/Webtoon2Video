"""Patch v4.26：跨话记忆库 Layer 1 人物实体图谱可编辑 + 快照与专属音色联动同步。

背景（用户诉求）：
* 「四层跨话记忆库」页只能新增角色，人物实体图谱（Layer 1）不可改；
  改主名、性别、别名、出场话次、外貌、状态全靠手改 ``story_bible.json``；
* 角色主名一改，``series_config.yaml`` 里挂着的专属音色（``tts.character_voices``
  的键就是角色名）就失效；
* 按话只读快照 ``bible_snapshots/ep_*.json`` 是「重跑某话」的基线（防剧透），
  手工改了 live 档案却不更新快照，重跑时角色信息会前后不一致。

本文件锁死 v4.26 的新契约：

* ``PUT /api/series/{series_id}/memory`` 支持 ``character_renames`` /
  ``deleted_character_names`` / ``sync_snapshots``；
* 角色提交必须过校验（空名 / 重名 / 首现话次 / 最近出场话次）→ 400 中文提示；
* 别名自动去重并剔除主名；
* 改名时迁移 ``tts.character_voices`` 键；
* 快照同步严守防剧透：未来才登场的角色不进历史快照、``last_seen_ep`` 钳位、
  历史 ``current_status`` 不被最新状态倒灌。

全部离线：不触网、不依赖 ffmpeg。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import yaml
from fastapi.testclient import TestClient
from PIL import Image

from core.config import AppConfig, WorkspaceConfig
from core.schemas import CharacterProfile, StoryBibleV2, load_model, save_model
from stages import s0_catalog
from web.job_runner import JobRunner
from web.server import create_app

SERIES_ID = "wt_v426"


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


def _character(
    name: str,
    *,
    aliases: Sequence[str] = (),
    status: str = "",
    traits: str = "",
    gender: str = "unknown",
    first_seen_ep: int = 1,
    last_seen_ep: int = 1,
) -> CharacterProfile:
    return CharacterProfile(
        name=name,
        aliases=list(aliases),
        visual_traits=traits,
        current_status=status,
        first_seen_ep=first_seen_ep,
        last_seen_ep=last_seen_ep,
        gender=gender,  # type: ignore[arg-type]
    )


def _dump(profile: CharacterProfile) -> dict[str, Any]:
    return profile.model_dump()


def _write_bible(
    series_dir: Path, series_id: str, characters: Sequence[CharacterProfile]
) -> Path:
    path = series_dir / "story_bible.json"
    save_model(
        StoryBibleV2(
            series_id=series_id,
            series_title="测试漫画",
            characters=list(characters),
        ),
        path,
    )
    return path


def _write_snapshot(
    series_dir: Path,
    episode_no: int,
    characters: Sequence[CharacterProfile],
    *,
    series_id: str = SERIES_ID,
) -> Path:
    snapshot_dir = series_dir / "bible_snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / f"ep_{episode_no:03d}.json"
    save_model(
        StoryBibleV2(
            series_id=series_id,
            series_title="测试漫画",
            characters=list(characters),
            snapshots_ep_nos=[episode_no],
        ),
        path,
    )
    return path


def _write_series_config(series_dir: Path, voices: dict[str, Any]) -> Path:
    path = series_dir / "series_config.yaml"
    path.write_text(
        yaml.safe_dump({"tts": {"character_voices": voices}}, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def _api(config: AppConfig) -> TestClient:
    runner = JobRunner(config, tts_factory=lambda: None)
    return TestClient(create_app(config, runner=runner))


def _put_characters(
    client: TestClient,
    series_id: str,
    characters: Sequence[CharacterProfile],
    **extra: Any,
) -> Any:
    payload: dict[str, Any] = {"characters": [_dump(c) for c in characters]}
    payload.update(extra)
    return client.put(f"/api/series/{series_id}/memory", json=payload)


def _snapshot_characters(series_dir: Path, episode_no: int) -> list[CharacterProfile]:
    path = series_dir / "bible_snapshots" / f"ep_{episode_no:03d}.json"
    return list(load_model(StoryBibleV2, path).characters)


def _live_characters(series_dir: Path) -> list[CharacterProfile]:
    return list(load_model(StoryBibleV2, series_dir / "story_bible.json").characters)


# ---------------------------------------------------------------------------
# A. 400 校验
# ---------------------------------------------------------------------------
def test_empty_character_name_returns_400(tmp_path) -> None:
    """主名不能为空。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(series_dir, series_id, [_character("恩彗")])
    client = _api(config)

    response = _put_characters(client, series_id, [_character("   ")])

    assert response.status_code == 400
    assert "主名" in response.json()["detail"]


def test_duplicate_character_name_returns_400(tmp_path) -> None:
    """同一请求内主名重复必须被拦。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(series_dir, series_id, [_character("恩彗")])
    client = _api(config)

    response = _put_characters(
        client, series_id, [_character("恩彗"), _character("恩彗", first_seen_ep=2)]
    )

    assert response.status_code == 400
    assert "重复" in response.json()["detail"]


def test_invalid_first_seen_ep_returns_400(tmp_path) -> None:
    """首现话次必须 ≥ 1。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(series_dir, series_id, [_character("恩彗")])
    client = _api(config)

    response = _put_characters(
        client, series_id, [_character("恩彗", first_seen_ep=0, last_seen_ep=0)]
    )

    assert response.status_code == 400
    assert "首现话次" in response.json()["detail"]


def test_last_seen_ep_before_first_seen_ep_returns_400(tmp_path) -> None:
    """最近出场话次不能早于首现话次。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(series_dir, series_id, [_character("恩彗")])
    client = _api(config)

    response = _put_characters(
        client, series_id, [_character("恩彗", first_seen_ep=3, last_seen_ep=2)]
    )

    assert response.status_code == 400
    assert "最近出场话次" in response.json()["detail"]


def test_aliases_are_deduplicated_and_exclude_main_name(tmp_path) -> None:
    """别名去重并剔除主名。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(series_dir, series_id, [_character("恩彗")])
    client = _api(config)

    response = _put_characters(
        client,
        series_id,
        [_character("恩彗", aliases=["小慧", "小慧", "恩彗", "  ", "恩慧"])],
    )

    assert response.status_code == 200
    saved = _live_characters(series_dir)[0]
    assert saved.aliases == ["小慧", "恩慧"]


# ---------------------------------------------------------------------------
# B. 专属音色键迁移
# ---------------------------------------------------------------------------
def test_rename_migrates_character_voice_key(tmp_path) -> None:
    """改主名必须把专属音色键迁到新名（否则音色失效）。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(series_dir, series_id, [_character("道振", aliases=["丈夫"])])
    _write_series_config(series_dir, {"道振": "Elias", "恩彗": "Katerina"})
    client = _api(config)

    response = _put_characters(
        client,
        series_id,
        [_character("南道振", aliases=["丈夫"]), _character("恩彗")],
        character_renames=[{"from_name": "道振", "to_name": "南道振"}],
    )

    assert response.status_code == 200
    data = yaml.safe_load(
        (series_dir / "series_config.yaml").read_text(encoding="utf-8")
    )
    voices = data["tts"]["character_voices"]
    assert voices["南道振"] == "Elias"
    assert "道振" not in voices
    assert voices["恩彗"] == "Katerina"


def test_rename_does_not_overwrite_existing_voice_key(tmp_path) -> None:
    """新键已有音色时不覆盖（用户手工配过的优先）。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(series_dir, series_id, [_character("道振")])
    _write_series_config(series_dir, {"道振": "Elias", "南道振": "Katerina"})
    client = _api(config)

    response = _put_characters(
        client,
        series_id,
        [_character("南道振")],
        character_renames=[{"from_name": "道振", "to_name": "南道振"}],
    )

    assert response.status_code == 200
    data = yaml.safe_load(
        (series_dir / "series_config.yaml").read_text(encoding="utf-8")
    )
    voices = data["tts"]["character_voices"]
    assert voices["南道振"] == "Katerina"
    assert "道振" not in voices


# ---------------------------------------------------------------------------
# C. 快照同步
# ---------------------------------------------------------------------------
def test_rename_syncs_snapshot_character_name(tmp_path) -> None:
    """改名后历史快照里的角色名必须跟着改。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(series_dir, series_id, [_character("道振", status="第1话状态")])
    _write_snapshot(series_dir, 2, [_character("道振", status="第1话状态")])
    client = _api(config)

    response = _put_characters(
        client,
        series_id,
        [_character("南道振", status="第1话状态")],
        character_renames=[{"from_name": "道振", "to_name": "南道振"}],
    )

    assert response.status_code == 200
    snapshot = _snapshot_characters(series_dir, 2)
    assert [c.name for c in snapshot] == ["南道振"]


def test_delete_removes_character_from_snapshots(tmp_path) -> None:
    """删除角色必须从历史快照里一并剔除。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(
        series_dir, series_id, [_character("恩彗"), _character("云飞", first_seen_ep=2)]
    )
    _write_snapshot(
        series_dir,
        2,
        [_character("恩彗"), _character("云飞", first_seen_ep=2)],
    )
    client = _api(config)

    response = _put_characters(
        client,
        series_id,
        [_character("恩彗")],
        deleted_character_names=["云飞"],
    )

    assert response.status_code == 200
    assert [c.name for c in _snapshot_characters(series_dir, 2)] == ["恩彗"]


def test_merge_two_names_in_snapshot_keeps_single_character(tmp_path) -> None:
    """快照里同时存在旧名与新名时合并成一个，旧名降为别名。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(series_dir, series_id, [_character("南道振", aliases=["道振"])])
    _write_snapshot(
        series_dir,
        2,
        [
            _character("道振", aliases=["丈夫"], first_seen_ep=1, last_seen_ep=1),
            _character("南道振", aliases=[], first_seen_ep=2, last_seen_ep=2),
        ],
    )
    client = _api(config)

    response = _put_characters(
        client,
        series_id,
        [_character("南道振", aliases=["道振"])],
        character_renames=[{"from_name": "道振", "to_name": "南道振"}],
    )

    assert response.status_code == 200
    snapshot = _snapshot_characters(series_dir, 2)
    assert [c.name for c in snapshot] == ["南道振"]
    merged = snapshot[0]
    assert "道振" in merged.aliases
    assert "丈夫" in merged.aliases
    assert merged.first_seen_ep == 1
    assert merged.last_seen_ep == 2


def test_sync_snapshots_false_leaves_snapshots_untouched(tmp_path) -> None:
    """sync_snapshots=False 时历史快照必须原样不动。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(series_dir, series_id, [_character("道振")])
    _write_snapshot(series_dir, 2, [_character("道振")])
    client = _api(config)

    response = _put_characters(
        client,
        series_id,
        [_character("南道振")],
        character_renames=[{"from_name": "道振", "to_name": "南道振"}],
        sync_snapshots=False,
    )

    assert response.status_code == 200
    assert [c.name for c in _snapshot_characters(series_dir, 2)] == ["道振"]


# ---------------------------------------------------------------------------
# D. 防剧透
# ---------------------------------------------------------------------------
def test_future_character_is_not_written_into_earlier_snapshot(tmp_path) -> None:
    """未来才登场的角色不得进入更早的话次快照。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(
        series_dir,
        series_id,
        [_character("恩彗"), _character("未来客", first_seen_ep=5, last_seen_ep=5)],
    )
    _write_snapshot(series_dir, 2, [_character("恩彗")])
    client = _api(config)

    response = _put_characters(
        client,
        series_id,
        [
            _character("恩彗"),
            _character("未来客", first_seen_ep=5, last_seen_ep=5),
        ],
    )

    assert response.status_code == 200
    assert [c.name for c in _snapshot_characters(series_dir, 2)] == ["恩彗"]


def test_snapshot_last_seen_ep_is_clamped_to_episode(tmp_path) -> None:
    """快照里的最近出场话次必须钳位到该话（不能记录未来）。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(
        series_dir,
        series_id,
        [_character("恩彗", first_seen_ep=1, last_seen_ep=9)],
    )
    _write_snapshot(
        series_dir, 2, [_character("恩彗", first_seen_ep=1, last_seen_ep=2)]
    )
    client = _api(config)

    response = _put_characters(
        client,
        series_id,
        [_character("恩彗", first_seen_ep=1, last_seen_ep=9)],
    )

    assert response.status_code == 200
    snapshot = _snapshot_characters(series_dir, 2)
    assert snapshot[0].last_seen_ep <= 2


def test_missing_character_is_backfilled_into_snapshot(tmp_path) -> None:
    """live 里首现不晚于该话、但快照缺失的角色必须补录。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(
        series_dir,
        series_id,
        [_character("恩彗"), _character("云飞", first_seen_ep=2, last_seen_ep=2)],
    )
    _write_snapshot(series_dir, 2, [_character("恩彗")])
    client = _api(config)

    response = _put_characters(
        client,
        series_id,
        [
            _character("恩彗"),
            _character("云飞", first_seen_ep=2, last_seen_ep=2),
        ],
    )

    assert response.status_code == 200
    names = [c.name for c in _snapshot_characters(series_dir, 2)]
    assert "云飞" in names


# ---------------------------------------------------------------------------
# E. current_status 保护
# ---------------------------------------------------------------------------
def test_snapshot_current_status_is_preserved_when_not_changed(tmp_path) -> None:
    """没改状态时，历史快照的状态不能被最新状态倒灌。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(series_dir, series_id, [_character("恩彗", status="第1话状态")])
    _write_snapshot(series_dir, 2, [_character("恩彗", status="第1话状态")])
    client = _api(config)

    # 只改外貌，状态不动
    response = _put_characters(
        client,
        series_id,
        [_character("恩彗", status="第1话状态", traits="黑长直")],
    )

    assert response.status_code == 200
    snapshot = _snapshot_characters(series_dir, 2)
    assert snapshot[0].current_status == "第1话状态"
    assert snapshot[0].visual_traits == "黑长直"


def test_snapshot_current_status_follows_user_edit(tmp_path) -> None:
    """用户确实改了状态时，快照状态跟着更新。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(series_dir, series_id, [_character("恩彗", status="第1话状态")])
    _write_snapshot(series_dir, 2, [_character("恩彗", status="第1话状态")])
    client = _api(config)

    response = _put_characters(
        client,
        series_id,
        [_character("恩彗", status="第3话加入主角小队")],
    )

    assert response.status_code == 200
    assert (
        _snapshot_characters(series_dir, 2)[0].current_status == "第3话加入主角小队"
    )
