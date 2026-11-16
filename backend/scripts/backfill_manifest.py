"""按磁盘产物回填单话 manifest（历史工作区没有 manifest 时的补救）。

用法：
    python -m scripts.backfill_manifest --workspace /tmp/w2v_live/ws [--series wt_95]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.manifest import (
    STAGE_DIR_NAMES,
    STAGE_KEYS,
    ManifestManager,
    infer_stage_artifacts,
    infer_stage_status,
)
from core.schemas import StageStatus


def backfill(series_dir: Path, series_id: str, episode_no: int) -> dict[str, str]:
    episode_dir = series_dir / "episodes" / f"ep_{episode_no:03d}"
    manager = ManifestManager(series_dir, series_id=series_id)
    manifest = manager.load(episode_no)
    inferred = infer_stage_status(episode_dir)
    artifacts = infer_stage_artifacts(episode_dir)
    for stage in STAGE_KEYS:
        if inferred.get(stage) is not StageStatus.DONE:
            continue
        entry = manifest.stages.get(stage)
        if entry is not None and entry.status is StageStatus.DONE:
            continue
        manager.update_stage(episode_no, stage, StageStatus.DONE)
    return {
        stage: str(manager.stage_status(episode_no, stage).value) for stage in STAGE_KEYS
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--series", default="")
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser()
    index_path = workspace / "index.json"
    raw = json.loads(index_path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else list(raw.get("series", []))
    for item in items:
        series_id = str(item.get("series_id") or "")
        if args.series and series_id != args.series:
            continue
        series_dir = workspace / series_id
        episodes_dir = series_dir / "episodes"
        if not episodes_dir.is_dir():
            continue
        for episode_dir in sorted(episodes_dir.glob("ep_*")):
            if not episode_dir.is_dir():
                continue
            episode_no = int(episode_dir.name.split("_")[-1])
            statuses = backfill(series_dir, series_id, episode_no)
            print(f"{series_id} ep_{episode_no:03d}: {statuses}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
