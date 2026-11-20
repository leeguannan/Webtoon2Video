"""各话流水线状态清单（manifest）管理。

目录约定（规范第 1.1 节）::

    workspace/{series_id}/episodes/ep_001/
        01_raw/ 02_panels/ 03_vision/ 04_script/ 05_audio/ 06_plan/ 07_out/
        manifest.json

``Stage 0`` 属于作品级目录抓取，不进入单话 manifest；单话 manifest 只跟踪 Stage 1~7。
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.schemas import StageStatus

STAGE_KEYS: tuple[str, ...] = (
    "s1_crawler",
    "s2_segmenter",
    "s3_vision",
    "s4_scriptwriter",
    "s5_tts",
    "s6_timeline",
    "s7_renderer",
)

STAGE_DIR_NAMES: dict[str, str] = {
    "s1_crawler": "01_raw",
    "s2_segmenter": "02_panels",
    "s3_vision": "03_vision",
    "s4_scriptwriter": "04_script",
    "s5_tts": "05_audio",
    "s6_timeline": "06_plan",
    "s7_renderer": "07_out",
}

#: 判定阶段「产物存在」的规则：``阶段 → (相对路径, 判定方式)``。
#:
#: **为什么需要它**：manifest 记录的是「谁跑过这个阶段」，但产物落在磁盘上。
#: 两者必须能互相印证——否则会出现「界面显示待执行、磁盘上成片已在」的矛盾
#: （CLI 跑的产物最初就没有 manifest）。
#: 判定方式：``image_dir``＝目录里至少有一张图；``file``＝文件存在且非空；
#: ``file_glob``＝按通配符找到至少一个非空文件。
STAGE_ARTIFACT_RULES: dict[str, tuple[str, str, str]] = {
    "s1_crawler": ("01_raw", "image_dir", ""),
    "s2_segmenter": ("02_panels", "image_dir", ""),
    "s3_vision": ("03_vision", "file", "vision.json"),
    "s4_scriptwriter": ("04_script", "file", "script.json"),
    "s5_tts": ("05_audio", "file", "audio_manifest.json"),
    "s6_timeline": ("06_plan", "file", "timeline.json"),
    "s7_renderer": ("07_out", "file_glob", "*.mp4"),
}

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})

#: 一键归档（只留成片）时要清空的话次级过程目录；目录本身保留，仅清内部内容。
#: 其中 ``03b_scenes`` 是 Patch v4.0 后的分镜产物，不属于 STAGE_DIR_NAMES 七段映射，
#: 故在此按字面量维护。
ARCHIVE_INTERMEDIATE_DIRS: tuple[str, ...] = (
    "01_raw",
    "02_panels",
    "03_vision",
    "03b_scenes",
    "04_script",
    "05_audio",
    "06_plan",
)


def dir_size(path: Path) -> int:
    """递归统计目录内全部文件的逻辑字节数（不跟随符号链接）。"""
    if not path.is_dir():
        return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file() and not child.is_symlink():
            try:
                total += child.stat().st_size
            except OSError:  # pragma: no cover - 权限/竞态不应阻断统计
                continue
    return total


def _hardlink_or_copy(src: Path, dst: Path) -> None:
    """把 ``src`` 落到 ``dst``：优先硬链接（同盘零额外占用），失败再复制。

    与 ``stages.s7_renderer._link_or_copy`` 同构；core 层不反向依赖 stages，
    故在此保留一份等价实现。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def utc_now() -> str:
    """当前 UTC 时间（ISO-8601，秒精度）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ManifestEntry(BaseModel):
    """单阶段的执行状态。"""

    model_config = ConfigDict(extra="forbid")

    stage: str
    status: StageStatus = StageStatus.PENDING
    artifact: str | None = None
    error: str | None = None
    updated_at: str = ""


class EpisodeManifest(BaseModel):
    """单话 manifest（落盘为 ``episodes/ep_XXX/manifest.json``）。"""

    model_config = ConfigDict(extra="forbid")

    episode_id: str
    series_id: str = ""
    episode_no: int
    stages: dict[str, ManifestEntry] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    # Patch v4.11：一键清理过程文件（只留成片）后的归档标记。
    # 旧 manifest 没有这三个键，靠默认值向后兼容，extra="forbid" 不受影响。
    archived: bool = False
    archived_at: str | None = None
    archived_duration_ms: int = 0


class ManifestManager:
    """单部漫画下各话 manifest 的读写。"""

    def __init__(self, series_dir: str | Path, *, series_id: str = "") -> None:
        self.series_dir = Path(series_dir).expanduser()
        self.series_id = series_id

    # ---- 路径 ----
    def episode_dir(self, episode_no: int) -> Path:
        return self.series_dir / "episodes" / f"ep_{episode_no:03d}"

    def stage_dir(self, episode_no: int, stage: str) -> Path:
        if stage not in STAGE_DIR_NAMES:
            raise KeyError(f"未知阶段：{stage}（合法值：{', '.join(STAGE_KEYS)}）")
        return self.episode_dir(episode_no) / STAGE_DIR_NAMES[stage]

    def manifest_path(self, episode_no: int) -> Path:
        return self.episode_dir(episode_no) / "manifest.json"

    def episode_id(self, episode_no: int) -> str:
        return f"{self.series_id}_ep_{episode_no:03d}" if self.series_id else f"ep_{episode_no:03d}"

    # ---- 目录 ----
    def ensure_episode_dirs(self, episode_no: int) -> Path:
        """创建该话的 7 个阶段目录。"""
        base = self.episode_dir(episode_no)
        base.mkdir(parents=True, exist_ok=True)
        for dir_name in STAGE_DIR_NAMES.values():
            (base / dir_name).mkdir(parents=True, exist_ok=True)
        return base

    # ---- 读写 ----
    def load(self, episode_no: int) -> EpisodeManifest:
        """读取 manifest；文件不存在时返回初始状态（不落盘）。"""
        path = self.manifest_path(episode_no)
        if path.is_file():
            return EpisodeManifest.model_validate_json(path.read_text(encoding="utf-8"))
        now = utc_now()
        return EpisodeManifest(
            episode_id=self.episode_id(episode_no),
            series_id=self.series_id,
            episode_no=episode_no,
            created_at=now,
            updated_at=now,
        )

    def save(self, manifest: EpisodeManifest) -> Path:
        """写入 manifest.json。"""
        manifest.updated_at = utc_now()
        if not manifest.created_at:
            manifest.created_at = manifest.updated_at
        path = self.manifest_path(manifest.episode_no)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        return path

    # ---- 状态更新 ----
    def update_stage(
        self,
        episode_no: int,
        stage: str,
        status: StageStatus,
        *,
        artifact: str | None = None,
        error: str | None = None,
    ) -> EpisodeManifest:
        """更新某阶段状态并立即落盘。"""
        if stage not in STAGE_DIR_NAMES:
            raise KeyError(f"未知阶段：{stage}（合法值：{', '.join(STAGE_KEYS)}）")
        manifest = self.load(episode_no)
        entry = manifest.stages.get(stage) or ManifestEntry(stage=stage)
        entry.status = status
        entry.updated_at = utc_now()
        if artifact is not None:
            entry.artifact = artifact
        if error is not None:
            entry.error = error
        manifest.stages[stage] = entry
        self.save(manifest)
        return manifest

    def mark_running(self, episode_no: int, stage: str) -> EpisodeManifest:
        return self.update_stage(episode_no, stage, StageStatus.RUNNING)

    def mark_done(self, episode_no: int, stage: str, *, artifact: str | None = None) -> EpisodeManifest:
        return self.update_stage(episode_no, stage, StageStatus.DONE, artifact=artifact)

    def mark_failed(self, episode_no: int, stage: str, *, error: str | None = None) -> EpisodeManifest:
        return self.update_stage(episode_no, stage, StageStatus.FAILED, error=error)

    def stage_status(self, episode_no: int, stage: str) -> StageStatus:
        """查询阶段状态；未记录时为 ``PENDING``。"""
        manifest = self.load(episode_no)
        entry = manifest.stages.get(stage)
        return entry.status if entry is not None else StageStatus.PENDING

    def is_stage_done(self, episode_no: int, stage: str) -> bool:
        return self.stage_status(episode_no, stage) is StageStatus.DONE

    # ------------------------------------------------------------------
    # 按步向后级联清理（Patch v4.1 Patch 21）
    # ------------------------------------------------------------------
    #: ``04_script`` 下「重跑必然作废」的文件（人工稿、镜头组之外的产物）。
    SCRIPT_STAGE_TRANSIENT_FILES: tuple[str, ...] = (
        "script.json",
        "script_readable.md",
        "story_draft.json",
        "story_draft.md",
    )

    def reset_from_stage(
        self,
        episode_no: int,
        from_stage: str,
        *,
        include_human_storyboard: bool = False,
    ) -> dict[str, Any]:
        """清空 ``from_stage`` 及其之后所有阶段的产物，并把状态重置为「待执行」。

        **为什么需要**：manifest 记录的是「跑过没有」，磁盘上留着的是「昨天的旧片」。
        只重跑某一步时，下游的旧产物与旧绿色完成标记会混在一起，让人分不清
        哪些是这次重做的、哪些还是老的。这里把该步及下游的磁盘产物、成片导出、
        manifest 记录一并清空，让状态重新回到「待执行」。

        规则：

        * ``from_stage`` 必须是 ``s1_crawler`` ~ ``s7_renderer`` 之一；
        * 阶段目录只清空内部文件与子目录，目录本身保留；
        * Stage 4 精细清理：剧本 / 故事稿一律删；人工编排的镜头组
          （``storyboard.json`` 的 ``source == "human"``）默认保留，
          传 ``include_human_storyboard=True`` 才连它一起删；
        * Stage 7 额外清掉作品级 ``exports/EP{话次}_*`` 导出成片。
        """
        if from_stage not in STAGE_KEYS:
            raise ValueError(
                f"只能从 S1 ~ S7 清理单话下游阶段，非法阶段：{from_stage}"
            )
        affected = STAGE_KEYS[STAGE_KEYS.index(from_stage) :]
        removed = 0
        preserved_human_storyboard = False

        def _remove_file(path: Path) -> None:
            nonlocal removed
            if path.is_file():
                try:
                    path.unlink()
                    removed += 1
                except OSError:  # pragma: no cover - 权限异常不应阻断清理
                    pass

        def _clear_dir(path: Path) -> None:
            nonlocal removed
            if not path.is_dir():
                return
            for child in sorted(path.iterdir()):
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
                else:
                    _remove_file(child)

        for stage in affected:
            stage_dir = self.stage_dir(episode_no, stage)
            if stage == "s4_scriptwriter":
                for name in self.SCRIPT_STAGE_TRANSIENT_FILES:
                    _remove_file(stage_dir / name)
                board_path = stage_dir / "storyboard.json"
                if board_path.is_file():
                    source = ""
                    try:
                        payload = json.loads(board_path.read_text(encoding="utf-8"))
                        source = str(payload.get("source") or "")
                    except (OSError, ValueError):
                        source = ""
                    if (
                        from_stage == "s4_scriptwriter"
                        and source == "human"
                        and not include_human_storyboard
                    ):
                        preserved_human_storyboard = True
                    else:
                        _remove_file(board_path)
                _remove_file(
                    self.series_dir / "bible_snapshots" / f"ep_{episode_no:03d}.json"
                )
            else:
                _clear_dir(stage_dir)
                if stage == "s7_renderer":
                    exports_dir = self.series_dir / "exports"
                    prefix = f"EP{episode_no:03d}_"
                    if exports_dir.is_dir():
                        for child in sorted(exports_dir.iterdir()):
                            if child.is_file() and child.name.startswith(prefix):
                                _remove_file(child)

        manifest = self.load(episode_no)
        for stage in affected:
            manifest.stages.pop(stage, None)
        # Patch v4.11：任何阶段重跑都会重建过程产物，归档标记自动失效。
        manifest.archived = False
        manifest.archived_at = None
        manifest.archived_duration_ms = 0
        self.save(manifest)

        return {
            "episode_id": self.episode_id(episode_no),
            "episode_no": episode_no,
            "from_stage": from_stage,
            "cleared_stages": list(affected),
            "removed_count": removed,
            "preserved_human_storyboard": preserved_human_storyboard,
        }

    # ------------------------------------------------------------------
    # 一键归档：清理过程文件、只留成片（Patch v4.11）
    # ------------------------------------------------------------------
    def archive_episode_keep_video(
        self,
        episode_no: int,
        *,
        duration_ms: int = 0,
    ) -> dict[str, Any]:
        """清理本话全部过程文件，只保留最终成片，并把 manifest 标为已归档。

        前置条件（防误删）：

        * ``07_out/{episode_id}.mp4`` 或作品级 ``exports/EP{no:03d}_*.mp4``
          至少有一份真实存在且非空，两者皆无直接抛 :class:`ValueError`；
        * 只有 exports 有成片时，先反向恢复一份到 ``07_out``，保证试看入口不断。

        清理范围（目录本身保留，仅清内部内容）：

        * ``01_raw`` / ``02_panels`` / ``03_vision`` / ``03b_scenes`` /
          ``04_script`` / ``05_audio`` / ``06_plan``；
        * ``07_out/_work`` 渲染工作目录；
        * ``bible_snapshots/ep_{no:03d}.json`` 本话快照。

        明确不碰：``07_out/*.mp4`` 成片、本话 ``manifest.json``、
        作品级 ``story_bible.json``（后续话次的剧情记忆）。

        清理后把 ``07_out`` 与 ``exports`` 两份成片合并为同一硬链接，
        同盘只占一份体积。返回释放/保留字节数与归档时长。
        """
        episode_dir = self.episode_dir(episode_no)
        out_dir = episode_dir / STAGE_DIR_NAMES["s7_renderer"]
        exports_dir = self.series_dir / "exports"
        export_prefix = f"EP{episode_no:03d}_"

        manifest = self.load(episode_no)
        resolved_id = manifest.episode_id or self.episode_id(episode_no)
        out_path = out_dir / f"{resolved_id}.mp4"
        export_paths = (
            sorted(
                p
                for p in exports_dir.glob(f"{export_prefix}*.mp4")
                if p.is_file() and p.stat().st_size > 0
            )
            if exports_dir.is_dir()
            else []
        )
        out_ready = out_path.is_file() and out_path.stat().st_size > 0
        if not out_ready:
            # 兼容历史异名成片：07_out 下任意非空 mp4 都视为规范成片。
            out_candidates = sorted(
                p
                for p in out_dir.glob("*.mp4")
                if p.is_file() and p.stat().st_size > 0
            )
            if out_candidates:
                out_path = out_candidates[0]
                out_ready = True

        if not out_ready and not export_paths:
            raise ValueError(
                f"ep_{episode_no:03d} 没有可用成片（07_out 与 exports 均缺失或为空），"
                "拒绝清理过程文件"
            )

        # 先提时长：传入值优先，其次读即将被清掉的 06_plan/timeline.json。
        effective_duration = int(duration_ms or 0)
        if effective_duration <= 0:
            timeline_path = (
                episode_dir / STAGE_DIR_NAMES["s6_timeline"] / "timeline.json"
            )
            if timeline_path.is_file():
                try:
                    payload = json.loads(timeline_path.read_text(encoding="utf-8"))
                    effective_duration = int(payload.get("total_duration_ms") or 0)
                except (OSError, ValueError):
                    effective_duration = 0

        bytes_before = dir_size(episode_dir)

        # 07_out 成片丢失而 exports 还在：反向恢复，保证两个入口都可用。
        if not out_ready:
            _hardlink_or_copy(export_paths[0], out_path)

        # 清理过程目录内容（保留空目录）。
        for dirname in ARCHIVE_INTERMEDIATE_DIRS:
            target = episode_dir / dirname
            if not target.is_dir():
                continue
            for child in sorted(target.iterdir()):
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except OSError:  # pragma: no cover - 权限异常不阻断整体清理
                        pass

        # 渲染工作目录（07_out/_work）与本话剧情快照；成片与 manifest 不动。
        shutil.rmtree(out_dir / "_work", ignore_errors=True)
        snapshot = (
            self.series_dir / "bible_snapshots" / f"ep_{episode_no:03d}.json"
        )
        if snapshot.is_file():
            try:
                snapshot.unlink()
            except OSError:  # pragma: no cover
                pass

        # 双份成片合并为硬链接：非同一 inode 时以 07_out 为规范源重建。
        for export_path in export_paths:
            try:
                if os.path.samefile(out_path, export_path):
                    continue
            except OSError:
                pass
            _hardlink_or_copy(out_path, export_path)

        bytes_after = dir_size(episode_dir)
        freed_bytes = max(0, bytes_before - bytes_after)
        kept_bytes = sum(
            p.stat().st_size
            for p in out_dir.glob("*.mp4")
            if p.is_file() and not p.is_symlink()
        )

        manifest.archived = True
        manifest.archived_at = utc_now()
        manifest.archived_duration_ms = effective_duration
        self.save(manifest)

        return {
            "episode_id": self.episode_id(episode_no),
            "episode_no": episode_no,
            "archived": True,
            "archived_at": manifest.archived_at,
            "archived_duration_ms": effective_duration,
            "freed_bytes": freed_bytes,
            "kept_bytes": kept_bytes,
        }


# --------------------------------------------------------------------------
# 磁盘产物推断
# --------------------------------------------------------------------------
def infer_stage_status(episode_dir: str | Path) -> dict[str, StageStatus]:
    """按磁盘产物推断各阶段状态（不写盘）。

    **为什么需要**：manifest 是「执行记录」，产物是「事实」。当执行记录缺失
    （例如产物是由 CLI 直接产出、当时还没有写 manifest）时，磁盘产物才是唯一
    可信的真相来源——界面不能对着真实存在的成片显示「待执行」。

    只做「有产物 ⇒ done」的单向判断：没有产物一律 pending，
    绝不因为「目录存在」就判定完成（空目录不算数）。
    """
    base = Path(episode_dir).expanduser()
    result: dict[str, StageStatus] = {}
    for stage, (rel, kind, pattern) in STAGE_ARTIFACT_RULES.items():
        target = base / rel
        done = False
        if kind == "image_dir":
            done = target.is_dir() and any(
                child.suffix.lower() in _IMAGE_SUFFIXES for child in target.iterdir()
            )
        elif kind == "file":
            path = target / pattern
            done = path.is_file() and path.stat().st_size > 0
        elif kind == "file_glob":
            done = any(
                child.is_file() and child.stat().st_size > 0
                for child in target.glob(pattern)
            ) if target.is_dir() else False
        result[stage] = StageStatus.DONE if done else StageStatus.PENDING
    return result


def infer_stage_artifacts(episode_dir: str | Path) -> dict[str, str]:
    """按磁盘产物推断各阶段的产物摘要（文件名），供界面展示。"""
    base = Path(episode_dir).expanduser()
    result: dict[str, str] = {}
    for stage, (rel, kind, pattern) in STAGE_ARTIFACT_RULES.items():
        target = base / rel
        try:
            if kind == "image_dir" and target.is_dir():
                images = sorted(
                    child.name
                    for child in target.iterdir()
                    if child.suffix.lower() in _IMAGE_SUFFIXES
                )
                if images:
                    result[stage] = f"{len(images)} 张图"
                    continue
            if kind == "file":
                path = target / pattern
                if path.is_file() and path.stat().st_size > 0:
                    result[stage] = path.name
                    continue
            if kind == "file_glob" and target.is_dir():
                files = sorted(
                    child.name
                    for child in target.glob(pattern)
                    if child.is_file() and child.stat().st_size > 0
                )
                if files:
                    result[stage] = files[0]
                    continue
        except OSError:
            continue
    return result
