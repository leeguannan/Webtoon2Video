"""``core.manifest`` 各话流水线状态清单测试（规范 M1-T3）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.manifest import (
    STAGE_DIR_NAMES,
    STAGE_KEYS,
    EpisodeManifest,
    ManifestManager,
    infer_stage_artifacts,
    infer_stage_status,
)
from core.schemas import StageStatus


@pytest.fixture()
def manager(tmp_path: Path) -> ManifestManager:
    return ManifestManager(tmp_path, series_id="wt_1")


def test_stage_keys_and_dir_names_match() -> None:
    assert len(STAGE_KEYS) == 7
    assert set(STAGE_DIR_NAMES) == set(STAGE_KEYS)
    assert set(STAGE_DIR_NAMES.values()) == {
        "01_raw",
        "02_panels",
        "03_vision",
        "04_script",
        "05_audio",
        "06_plan",
        "07_out",
    }


def test_paths(manager: ManifestManager) -> None:
    assert manager.episode_dir(1) == manager.series_dir / "episodes" / "ep_001"
    assert manager.manifest_path(1) == manager.episode_dir(1) / "manifest.json"
    assert manager.episode_id(1) == "wt_1_ep_001"
    assert manager.stage_dir(1, "s1_crawler") == manager.episode_dir(1) / "01_raw"


def test_unknown_stage_raises_key_error(manager: ManifestManager) -> None:
    with pytest.raises(KeyError):
        manager.stage_dir(1, "s0_bogus")
    with pytest.raises(KeyError):
        manager.update_stage(1, "s0_bogus", StageStatus.RUNNING)


def test_ensure_episode_dirs_creates_seven_dirs(manager: ManifestManager) -> None:
    base = manager.ensure_episode_dirs(1)
    assert base.is_dir()
    for dir_name in STAGE_DIR_NAMES.values():
        assert (base / dir_name).is_dir()
    # 幂等
    manager.ensure_episode_dirs(1)


def test_load_missing_manifest_returns_initial_state(manager: ManifestManager) -> None:
    manifest = manager.load(1)
    assert manifest.episode_id == "wt_1_ep_001"
    assert manifest.episode_no == 1
    assert manifest.stages == {}
    assert manifest.created_at


def test_stage_status_lifecycle(manager: ManifestManager) -> None:
    assert manager.stage_status(1, "s1_crawler") is StageStatus.PENDING
    assert manager.is_stage_done(1, "s1_crawler") is False

    manager.mark_running(1, "s1_crawler")
    assert manager.stage_status(1, "s1_crawler") is StageStatus.RUNNING

    manager.mark_done(1, "s1_crawler", artifact="01_raw/ep_001.json")
    assert manager.stage_status(1, "s1_crawler") is StageStatus.DONE
    assert manager.is_stage_done(1, "s1_crawler") is True

    manager.mark_failed(2, "s3_vision", error="接口超时")
    assert manager.stage_status(2, "s3_vision") is StageStatus.FAILED
    entry = manager.load(2).stages["s3_vision"]
    assert entry.error == "接口超时"


def test_manifest_persists_to_disk(manager: ManifestManager) -> None:
    manager.ensure_episode_dirs(1)
    manager.mark_done(1, "s2_segmenter", artifact="02_panels/panels.json")

    path = manager.manifest_path(1)
    assert path.is_file()
    payload = path.read_text(encoding="utf-8")
    manifest = EpisodeManifest.model_validate_json(payload)
    assert manifest.stages["s2_segmenter"].artifact == "02_panels/panels.json"
    assert manifest.stages["s2_segmenter"].status is StageStatus.DONE


def test_episode_dirs_are_isolated_per_episode(manager: ManifestManager) -> None:
    manager.ensure_episode_dirs(1)
    manager.ensure_episode_dirs(2)
    assert manager.episode_dir(1) != manager.episode_dir(2)
    assert manager.stage_dir(1, "s1_crawler").is_dir()
    assert manager.stage_dir(2, "s1_crawler").is_dir()


# --------------------------------------------------------------------------
# 磁盘产物推断（新增：CLI / Web 之前从没写过 manifest，状态只能靠产物反推）
# --------------------------------------------------------------------------
def _episode_dir(tmp_path: Path, *, episode_no: int = 1) -> Path:
    path = tmp_path / "episodes" / f"ep_{episode_no:03d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_infer_stage_status_detects_finished_stages(tmp_path: Path) -> None:
    episode_dir = _episode_dir(tmp_path)
    (episode_dir / "01_raw").mkdir()
    (episode_dir / "01_raw" / "001.png").write_bytes(b"\x89PNG")
    (episode_dir / "03_vision").mkdir()
    (episode_dir / "03_vision" / "vision.json").write_text("{}", encoding="utf-8")

    inferred = infer_stage_status(episode_dir)

    assert inferred["s1_crawler"] is StageStatus.DONE
    assert inferred["s3_vision"] is StageStatus.DONE
    # 没有产物的阶段保持「未开始」，不能凭目录存在就算完成。
    assert inferred["s2_segmenter"] is StageStatus.PENDING
    assert inferred["s7_renderer"] is StageStatus.PENDING


def test_infer_stage_status_ignores_empty_dirs(tmp_path: Path) -> None:
    episode_dir = _episode_dir(tmp_path)
    (episode_dir / "07_out").mkdir()

    inferred = infer_stage_status(episode_dir)

    assert inferred["s7_renderer"] is StageStatus.PENDING


def test_infer_stage_status_detects_rendered_movie(tmp_path: Path) -> None:
    episode_dir = _episode_dir(tmp_path)
    (episode_dir / "07_out").mkdir()
    (episode_dir / "07_out" / "wt_1_ep_001.mp4").write_bytes(b"\x00" * 32)

    inferred = infer_stage_status(episode_dir)

    assert inferred["s7_renderer"] is StageStatus.DONE


def test_infer_stage_artifacts_summarises_outputs(tmp_path: Path) -> None:
    episode_dir = _episode_dir(tmp_path)
    (episode_dir / "01_raw").mkdir()
    (episode_dir / "01_raw" / "001.png").write_bytes(b"\x89PNG")
    (episode_dir / "01_raw" / "002.png").write_bytes(b"\x89PNG")
    (episode_dir / "04_script").mkdir()
    (episode_dir / "04_script" / "script.json").write_text("{}", encoding="utf-8")

    artifacts = infer_stage_artifacts(episode_dir)

    assert artifacts["s1_crawler"] == "2 张图"
    assert artifacts["s4_scriptwriter"] == "script.json"
