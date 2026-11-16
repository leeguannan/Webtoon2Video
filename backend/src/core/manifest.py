"""各话流水线状态清单（manifest）管理。

目录约定（规范第 1.1 节）::

    workspace/{series_id}/episodes/ep_001/
        01_raw/ 02_panels/ 03_vision/ 04_script/ 05_audio/ 06_plan/ 07_out/
        manifest.json

``Stage 0`` 属于作品级目录抓取，不进入单话 manifest；单话 manifest 只跟踪 Stage 1~7。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

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
