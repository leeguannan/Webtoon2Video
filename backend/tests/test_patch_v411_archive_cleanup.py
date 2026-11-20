"""Patch v4.11：硬盘空间优化与「已发布话次一键清理过程文件（只留成片）」。

覆盖两条主线：

1. ``ManifestManager.archive_episode_keep_video`` 的清理边界——过程目录清空、
   ``07_out/_work`` 与本话剧情快照删除、成片/manifest/作品级 story_bible 保留、
   双份成片合并硬链接、字节台账与时长落档、无成片拒绝、reset 自动清归档标记；
2. 两个 HTTP API（单话 / 作品级批量）的 200/400/404/409 语义，以及归档后
   ``episode_item`` 仍保持 ``status=done``、时长不丢、``reclaimable_bytes=0``。

装置全部在 ``tmp_path`` 现造，不触网、不依赖真实 ffmpeg（归档路径不探片，
时长走 timeline / manifest 两级兜底）。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.config import AppConfig, WorkspaceConfig
from core.manifest import ARCHIVE_INTERMEDIATE_DIRS, ManifestManager
from stages import s0_catalog
from web.job_runner import JobRunner
from web.server import create_app

SERIES_ID = "wt_v411"
VIDEO_BYTES = b"\x00" * 8192 + b"fake-mp4-payload-v411"
TIMELINE_DURATION_MS = 321096


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------
def _build_full_episode(
    series_dir: Path,
    episode_no: int = 1,
    *,
    series_id: str = SERIES_ID,
    timeline_duration_ms: int | None = TIMELINE_DURATION_MS,
    with_out_video: bool = True,
) -> Path:
    """造一个七阶段产物齐全、含双份独立成片与剧情快照的话次目录。"""
    ep_dir = series_dir / "episodes" / f"ep_{episode_no:03d}"

    (ep_dir / "01_raw").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 80), (10, 20, 30)).save(ep_dir / "01_raw" / "page_001.png")

    (ep_dir / "02_panels").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 80), (40, 50, 60)).save(ep_dir / "02_panels" / "p_001.png")

    (ep_dir / "03_vision").mkdir(parents=True, exist_ok=True)
    (ep_dir / "03_vision" / "vision.json").write_text("{}", encoding="utf-8")

    (ep_dir / "03b_scenes").mkdir(parents=True, exist_ok=True)
    (ep_dir / "03b_scenes" / "scenes.json").write_text("{}", encoding="utf-8")

    (ep_dir / "04_script").mkdir(parents=True, exist_ok=True)
    (ep_dir / "04_script" / "script.json").write_text("{}", encoding="utf-8")

    (ep_dir / "05_audio").mkdir(parents=True, exist_ok=True)
    (ep_dir / "05_audio" / "audio_manifest.json").write_text("{}", encoding="utf-8")

    plan_dir = ep_dir / "06_plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    timeline_payload = (
        {} if timeline_duration_ms is None else {"total_duration_ms": timeline_duration_ms}
    )
    (plan_dir / "timeline.json").write_text(
        json.dumps(timeline_payload), encoding="utf-8"
    )

    out_dir = ep_dir / "07_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 模拟上轮崩溃残留的渲染工作目录（归档必须清掉）。
    (out_dir / "_work").mkdir(parents=True, exist_ok=True)
    (out_dir / "_work" / "seg_0000.mp4").write_bytes(b"stale-segment")

    episode_id = f"{series_id}_ep_{episode_no:03d}"
    out_path = out_dir / f"{episode_id}.mp4"
    export_path = series_dir / "exports" / f"EP{episode_no:03d}_final.mp4"
    if with_out_video:
        out_path.write_bytes(VIDEO_BYTES)
        # 先造成「两份独立副本、同内容不同 inode」的历史双占状态。
        export_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out_path, export_path)
    else:
        export_path.parent.mkdir(parents=True, exist_ok=True)

    # 无关话次的导出：任何清理都不能误伤。
    (series_dir / "exports").mkdir(parents=True, exist_ok=True)
    (series_dir / "exports" / "EP999_final.mp4").write_bytes(b"other-episode-export")

    snapshots = series_dir / "bible_snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    (snapshots / f"ep_{episode_no:03d}.json").write_text("{}", encoding="utf-8")

    # 作品级剧情记忆：归档绝不允许碰它（后续话次记忆连贯的底线）。
    (series_dir / "story_bible.json").write_text(
        json.dumps({"version": 2, "memory": "跨话记忆"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return ep_dir


def _register_series(
    root: Path, *, episode_count: int = 1
) -> tuple[str, Path, AppConfig]:
    """用 s0_catalog 登记作品，返回 (series_id, series_dir, config)。"""
    src = root / "src"
    for no in range(1, episode_count + 1):
        ep_src = src / f"ep_{no:03d}"
        ep_src.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (400, 600), (40, 80, 120)).save(ep_src / "001.png")
    catalog = s0_catalog.import_local_catalog(src)
    s0_catalog.CatalogRegistry(root).register(catalog)
    config = AppConfig(workspace=WorkspaceConfig(root=root))
    return catalog.series_id, root / catalog.series_id, config


# ---------------------------------------------------------------------------
# A. ManifestManager 级
# ---------------------------------------------------------------------------
def test_archive_clears_intermediates_and_merges_hardlinks(tmp_path: Path) -> None:
    series_dir = tmp_path / SERIES_ID
    ep_dir = _build_full_episode(series_dir)
    manager = ManifestManager(series_dir, series_id=SERIES_ID)
    out_path = ep_dir / "07_out" / f"{SERIES_ID}_ep_001.mp4"
    export_path = series_dir / "exports" / "EP001_final.mp4"
    assert not os.path.samefile(out_path, export_path)  # 前置：两份独立占用
    bible_before = (series_dir / "story_bible.json").read_bytes()

    summary = manager.archive_episode_keep_video(1)

    # 过程目录：目录本身保留、内部清空（含 03b_scenes）。
    for dirname in ARCHIVE_INTERMEDIATE_DIRS:
        target = ep_dir / dirname
        assert target.is_dir(), f"{dirname} 目录应保留"
        assert list(target.iterdir()) == [], f"{dirname} 内部应清空"

    # 工作目录与本话快照删除；成片与 manifest 保留。
    assert not (ep_dir / "07_out" / "_work").exists()
    assert not (series_dir / "bible_snapshots" / "ep_001.json").exists()
    assert out_path.is_file()
    assert out_path.stat().st_size == len(VIDEO_BYTES)
    assert (ep_dir / "manifest.json").is_file()

    # 作品级 story_bible 与无关导出原样保留。
    assert (series_dir / "story_bible.json").read_bytes() == bible_before
    assert (series_dir / "exports" / "EP999_final.mp4").is_file()

    # 双份成片合并为硬链接：同 inode、链接计数 2，同盘只占一份体积。
    assert os.path.samefile(out_path, export_path)
    assert out_path.stat().st_ino == export_path.stat().st_ino
    assert out_path.stat().st_nlink == 2

    # 台账与 manifest 落档。
    assert summary["archived"] is True
    assert summary["freed_bytes"] > 0
    assert summary["kept_bytes"] == len(VIDEO_BYTES)
    assert summary["archived_duration_ms"] == TIMELINE_DURATION_MS
    assert summary["archived_at"]
    archived = manager.load(1)
    assert archived.archived is True
    assert archived.archived_at is not None
    assert archived.archived_duration_ms == TIMELINE_DURATION_MS


def test_archive_without_video_raises_and_keeps_everything(tmp_path: Path) -> None:
    """无成片必须拒绝，且一个过程文件都不能删（防误删底线）。"""
    series_dir = tmp_path / SERIES_ID
    ep_dir = _build_full_episode(series_dir, with_out_video=False)
    manager = ManifestManager(series_dir, series_id=SERIES_ID)

    with pytest.raises(ValueError, match="拒绝清理过程文件"):
        manager.archive_episode_keep_video(1)

    assert (ep_dir / "01_raw" / "page_001.png").is_file()
    assert (ep_dir / "06_plan" / "timeline.json").is_file()
    assert (series_dir / "bible_snapshots" / "ep_001.json").is_file()
    assert manager.load(1).archived is False


def test_archive_restores_out_copy_from_exports(tmp_path: Path) -> None:
    """07_out 成片丢失而 exports 还在时，反向硬链接恢复，试看入口不断。"""
    series_dir = tmp_path / SERIES_ID
    ep_dir = _build_full_episode(series_dir)
    out_path = ep_dir / "07_out" / f"{SERIES_ID}_ep_001.mp4"
    export_path = series_dir / "exports" / "EP001_final.mp4"
    out_path.unlink()
    manager = ManifestManager(series_dir, series_id=SERIES_ID)

    summary = manager.archive_episode_keep_video(1)

    assert out_path.is_file()
    assert os.path.samefile(out_path, export_path)
    assert summary["kept_bytes"] == len(VIDEO_BYTES)


def test_archive_uses_passed_duration_when_timeline_has_none(tmp_path: Path) -> None:
    """timeline 没有 total_duration_ms 时，采用显式传入的时长落档。"""
    series_dir = tmp_path / SERIES_ID
    _build_full_episode(series_dir, timeline_duration_ms=None)
    manager = ManifestManager(series_dir, series_id=SERIES_ID)

    summary = manager.archive_episode_keep_video(1, duration_ms=12345)

    assert summary["archived_duration_ms"] == 12345
    assert manager.load(1).archived_duration_ms == 12345


def test_reset_from_stage_clears_archive_flags(tmp_path: Path) -> None:
    """重跑汇聚点 reset_from_stage 必须清掉归档三标记。"""
    series_dir = tmp_path / SERIES_ID
    _build_full_episode(series_dir)
    manager = ManifestManager(series_dir, series_id=SERIES_ID)
    manager.archive_episode_keep_video(1)
    assert manager.load(1).archived is True

    manager.reset_from_stage(1, "s5_tts")

    manifest = manager.load(1)
    assert manifest.archived is False
    assert manifest.archived_at is None
    assert manifest.archived_duration_ms == 0


def test_second_archive_is_safe_and_freed_zero(tmp_path: Path) -> None:
    """manager 层不拦二次归档：过程目录已空，freed_bytes 为 0 且成片不丢。"""
    series_dir = tmp_path / SERIES_ID
    ep_dir = _build_full_episode(series_dir)
    manager = ManifestManager(series_dir, series_id=SERIES_ID)
    first = manager.archive_episode_keep_video(1)
    second = manager.archive_episode_keep_video(1)

    assert first["freed_bytes"] > 0
    assert second["freed_bytes"] == 0
    assert manager.load(1).archived is True
    assert (ep_dir / "07_out" / f"{SERIES_ID}_ep_001.mp4").is_file()


# ---------------------------------------------------------------------------
# B. HTTP API 级
# ---------------------------------------------------------------------------
def test_cleanup_api_success_keeps_done_status_and_duration(tmp_path: Path) -> None:
    series_id, series_dir, config = _register_series(tmp_path)
    _build_full_episode(series_dir, series_id=series_id)
    client = TestClient(create_app(config))
    episode_id = f"{series_id}_ep_001"

    response = client.post(f"/api/episodes/{episode_id}/cleanup-intermediates")

    assert response.status_code == 200
    payload = response.json()
    assert payload["archived"] is True
    assert payload["freed_bytes"] > 0
    assert payload["kept_bytes"] == len(VIDEO_BYTES)
    episode = payload["episode"]
    assert episode["archived"] is True
    assert episode["status"] == "done"  # 归档不退化成 idle
    assert episode["reclaimable_bytes"] == 0
    assert episode["video_duration_ms"] == TIMELINE_DURATION_MS
    assert episode["disk_bytes"] >= len(VIDEO_BYTES)

    # timeline 已被清掉，再拉作品数据：时长走 archived_duration_ms 兜底。
    series_resp = client.get(f"/api/series/{series_id}")
    assert series_resp.status_code == 200
    items = {ep["episode_id"]: ep for ep in series_resp.json()["episodes"]}
    refreshed = items[episode_id]
    assert refreshed["status"] == "done"
    assert refreshed["archived"] is True
    assert refreshed["video_duration_ms"] == TIMELINE_DURATION_MS
    assert refreshed["reclaimable_bytes"] == 0


def test_cleanup_api_conflict_while_running(tmp_path: Path) -> None:
    series_id, series_dir, config = _register_series(tmp_path)
    _build_full_episode(series_dir, series_id=series_id)
    runner = JobRunner(config)
    client = TestClient(create_app(config, runner=runner))
    runner.state_of(series_id, 1).status = "running"

    response = client.post(
        f"/api/episodes/{series_id}_ep_001/cleanup-intermediates"
    )

    assert response.status_code == 409
    assert "请等待完成后再清理" in response.json()["detail"]
    # 被拒后过程文件原样保留。
    ep_dir = series_dir / "episodes" / "ep_001"
    assert (ep_dir / "01_raw" / "page_001.png").is_file()


def test_cleanup_api_rejects_without_video(tmp_path: Path) -> None:
    series_id, series_dir, config = _register_series(tmp_path)
    _build_full_episode(series_dir, series_id=series_id, with_out_video=False)
    client = TestClient(create_app(config))

    response = client.post(
        f"/api/episodes/{series_id}_ep_001/cleanup-intermediates"
    )

    assert response.status_code == 400
    assert "拒绝清理过程文件" in response.json()["detail"]


def test_cleanup_api_rejects_double_archive(tmp_path: Path) -> None:
    series_id, series_dir, config = _register_series(tmp_path)
    _build_full_episode(series_dir, series_id=series_id)
    client = TestClient(create_app(config))
    episode_id = f"{series_id}_ep_001"

    first = client.post(f"/api/episodes/{episode_id}/cleanup-intermediates")
    assert first.status_code == 200
    second = client.post(f"/api/episodes/{episode_id}/cleanup-intermediates")
    assert second.status_code == 400
    assert "已清理过过程文件" in second.json()["detail"]


def test_cleanup_api_unknown_episode_404(tmp_path: Path) -> None:
    _series_id, _series_dir, config = _register_series(tmp_path)
    client = TestClient(create_app(config))

    response = client.post("/api/episodes/wt_ghost_ep_001/cleanup-intermediates")

    assert response.status_code == 404


def test_series_cleanup_skips_running_archived_and_videoless(tmp_path: Path) -> None:
    series_id, series_dir, config = _register_series(tmp_path, episode_count=3)
    _build_full_episode(series_dir, 1, series_id=series_id)  # 正常可清理
    _build_full_episode(series_dir, 2, series_id=series_id)  # 运行中 → 跳过
    _build_full_episode(series_dir, 3, series_id=series_id)  # 预归档 → 跳过
    runner = JobRunner(config)
    client = TestClient(create_app(config, runner=runner))
    runner.state_of(series_id, 2).status = "running"
    manager = ManifestManager(series_dir, series_id=series_id)
    manager.archive_episode_keep_video(3)

    response = client.post(f"/api/series/{series_id}/cleanup-intermediates")

    assert response.status_code == 200
    payload = response.json()
    assert payload["cleaned_count"] == 1
    assert payload["freed_bytes"] > 0
    skipped = set(payload["skipped_episode_ids"])
    assert f"{series_id}_ep_002" in skipped
    assert f"{series_id}_ep_003" in skipped
    assert f"{series_id}_ep_001" not in skipped

    # ep1 已归档、ep2 原产物未动。
    assert manager.load(1).archived is True
    assert manager.load(2).archived is False
    ep2_dir = series_dir / "episodes" / "ep_002"
    assert (ep2_dir / "01_raw" / "page_001.png").is_file()


def test_series_cleanup_unknown_series_404(tmp_path: Path) -> None:
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    client = TestClient(create_app(config))

    response = client.post("/api/series/wt_ghost/cleanup-intermediates")

    assert response.status_code == 404
