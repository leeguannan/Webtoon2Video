"""Patch v4.32：镜头编排范围（Panel Scope）——「用户在分镜里选过的画面」的唯一权威口径。

**为什么需要这一层（这是「视频里出现了我没选过的画面」的根治点）**

「成片出现的画面」这条链路历史上有三个各自为政的 owner：

1. ``04_script/script.json`` 里每条 beat 的 ``target_panel_ids``（Stage 4 写、审核台改）；
2. ``04_script/storyboard.json`` 里每幕的 ``panel_ids``（用户在**镜头编排页**手工选定的范围）；
3. ``02_panels/`` 磁盘上真实存在的分镜图片（Stage 2 切出来的全部格子）。

三者之间没有任何一致性约束，于是出现两类「用户没选过的画面」进成片：

* **越界引用**：用户在审核台新增一句时，下拉框列的是**全部分镜**（含编排外的格子），
  保存链路（``PUT /api/episodes/{id}/script``）又完全不校验，于是 ``b_new_142 → p_094``
  这类「不在任何启用幕里」的分镜被写进剧本，Stage 6 原样渲染进成片。
* **多目标不可见**：Stage 4 会给一句解说配多格分镜（``target_panel_ids`` 长度 > 1），
  但审核台的下拉只暴露第 1 格，用户以为「只配了这一格」，成片却按格数均分时长轮播，
  第 2..N 格在审核台**完全不可见**——用户观感同样是「这帧我没选过」。

本模块把 **镜头编排页（storyboard）的启用幕范围**确立为唯一权威（canonical owner）：

* :func:`load_panel_scope` 读 ``04_script/storyboard.json``，取**启用幕**引用的分镜并集；
* :func:`clamp_beats_to_scope` 把剧本里越界的 target 收口回编排范围（写回层强制）；
* Stage 6 渲染层再用同一份 scope 过滤一次（渲染层兜底，历史文件与手工改盘也生效）。

**向后兼容**：编排文件不存在 / 损坏 / 没有任何启用幕时，scope 视为「不约束」
（返回 ``None`` 或空范围），旧工程的成片行为完全不变。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.panel_ids import PanelIdResolver, panel_sort_key, trailing_number
from core.schemas import EpisodeStoryboard, ScriptBeat, load_model

#: 镜头编排文件所在目录（与 ``core.manifest.STAGE_DIR_NAMES["s4_scriptwriter"]`` 一致）。
STORYBOARD_DIR_NAME = "04_script"
#: 镜头编排文件名。
STORYBOARD_FILENAME = "storyboard.json"


@dataclass(frozen=True)
class PanelScope:
    """镜头编排范围：启用幕引用到的分镜集合。"""

    #: 启用幕引用的分镜 ID（原始写法，未归一）。
    allowed_panel_ids: frozenset[str] = frozenset()
    #: ``scene_id → 该幕引用的分镜集合``（原始写法）。
    scene_panel_ids: Mapping[str, frozenset[str]] = field(default_factory=dict)
    #: 数据来源：``"storyboard"``（读到了编排文件）/ ``"empty"``（无启用幕，不约束）。
    source: str = "empty"

    def __bool__(self) -> bool:
        return bool(self.allowed_panel_ids)

    @property
    def is_empty(self) -> bool:
        """范围为空（不约束任何分镜）。"""
        return not self.allowed_panel_ids

    def canonical(self, panel_id: str | None) -> str | None:
        """把任意写法的分镜 ID 归一到编排范围内的规范写法；不在范围内返回 ``None``。"""
        if not panel_id:
            return None
        return self._resolver.resolve(str(panel_id))

    def allows(self, panel_id: str | None) -> bool:
        """该分镜是否在编排范围内。"""
        return self.canonical(panel_id) is not None

    def ordered_allowed(self) -> list[str]:
        """按自然序返回范围内的全部分镜。"""
        return sorted(self.allowed_panel_ids, key=panel_sort_key)

    def scene_panels(self, scene_id: str | None) -> list[str]:
        """某幕引用的分镜（自然序）。"""
        if not scene_id:
            return []
        return sorted(self.scene_panel_ids.get(scene_id, frozenset()), key=panel_sort_key)

    def nearest_allowed(self, panel_id: str | None) -> str | None:
        """找出编排范围内与给定分镜**序位最接近**的一格（用于越界兜底替换）。"""
        if self.is_empty:
            return None
        target = trailing_number(panel_id) if panel_id else None
        best: str | None = None
        best_gap: int | None = None
        for candidate in self.allowed_panel_ids:
            number = trailing_number(candidate)
            if number is None:
                continue
            gap = abs(number - target) if target is not None else 0
            if best_gap is None or gap < best_gap:
                best, best_gap = candidate, gap
        if best is not None:
            return best
        ordered = self.ordered_allowed()
        return ordered[0] if ordered else None

    @property
    def _resolver(self) -> PanelIdResolver:
        return PanelIdResolver(self.allowed_panel_ids)


@dataclass(frozen=True)
class ScopeViolation:
    """一次越界引用的收口记录（用于回给前端提示）。"""

    #: 出问题的 beat ID。
    beat_id: str
    #: 被剔除的编排外分镜。
    removed: tuple[str, ...] = ()
    #: 整体替换时用到的替代分镜（``None`` 表示只剔除、未整体替换）。
    replacement: str | None = None


def storyboard_path_for(episode_dir: Path | str) -> Path:
    """镜头编排文件的磁盘路径。"""
    return Path(episode_dir) / STORYBOARD_DIR_NAME / STORYBOARD_FILENAME


def scope_from_storyboard(storyboard: EpisodeStoryboard | None) -> PanelScope:
    """从编排对象提取启用幕范围（无启用幕 → 空范围，即不约束）。"""
    if storyboard is None:
        return PanelScope()
    allowed: set[str] = set()
    scene_panel_ids: dict[str, frozenset[str]] = {}
    for scene in storyboard.scenes:
        if not scene.enabled:
            continue
        panels = frozenset(str(pid) for pid in (scene.panel_ids or []) if str(pid))
        scene_panel_ids[str(scene.scene_id)] = panels
        allowed |= panels
    if not allowed:
        return PanelScope()
    return PanelScope(
        allowed_panel_ids=frozenset(allowed),
        scene_panel_ids=scene_panel_ids,
        source="storyboard",
    )


def load_panel_scope(episode_dir: Path | str) -> PanelScope | None:
    """读取镜头编排范围。

    返回 ``None`` 表示「没有编排文件，本层不约束」；返回空
    :class:`PanelScope` 表示「编排里没有任何启用幕，同样不约束」。
    """
    path = storyboard_path_for(episode_dir)
    if not path.is_file():
        return None
    try:
        storyboard = load_model(EpisodeStoryboard, path)
    except Exception:  # noqa: BLE001 - 编排文件损坏时按「不约束」处理，不阻塞流水线
        return None
    return scope_from_storyboard(storyboard)


def clamp_beats_to_scope(
    beats: Sequence[ScriptBeat],
    scope: PanelScope | None,
) -> tuple[list[ScriptBeat], list[ScopeViolation]]:
    """把剧本里越界（不在编排范围内）的目标分镜收口回编排范围。

    规则：

    * 命中范围内的 target 原样保留；越界的 target 剔除；
    * 若剔除后一个都不剩（整句都指向编排外的分镜），按
      「本句归属幕内的分镜 → 编排范围内序位最近的一格」顺序挑一格顶上，
      保证 ``ScriptBeat.target_panel_ids`` 永远非空；
    * 编排范围为空（无编排 / 无启用幕）→ 原样返回，不做任何改写。
    """
    if scope is None or scope.is_empty:
        return list(beats), []

    clamped: list[ScriptBeat] = []
    violations: list[ScopeViolation] = []
    for beat in beats:
        targets = [str(pid) for pid in (getattr(beat, "target_panel_ids", None) or [])]
        if not targets:
            clamped.append(beat)
            continue
        kept = [pid for pid in targets if scope.allows(pid)]
        removed = tuple(pid for pid in targets if not scope.allows(pid))
        if not removed:
            clamped.append(beat)
            continue

        replacement: str | None = None
        if not kept:
            scene_id = getattr(beat, "scene_id", None)
            for candidate in scope.scene_panels(scene_id):
                if scope.allows(candidate):
                    replacement = candidate
                    break
            if replacement is None:
                replacement = scope.nearest_allowed(targets[0])
            kept = [replacement] if replacement else targets

        clamped.append(beat.model_copy(update={"target_panel_ids": kept}))
        violations.append(
            ScopeViolation(
                beat_id=str(getattr(beat, "beat_id", "") or ""),
                removed=removed,
                replacement=replacement,
            )
        )
    return clamped, violations


def violations_payload(violations: Sequence[ScopeViolation]) -> list[dict[str, Any]]:
    """把收口记录转成可直接回给前端的 JSON 结构。"""
    return [
        {
            "beat_id": item.beat_id,
            "removed": list(item.removed),
            "replacement": item.replacement,
        }
        for item in violations
    ]
