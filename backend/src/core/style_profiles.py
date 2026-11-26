"""漫画风格预设注册表（Patch v4.44）。

**为什么要这层**：过去「剧情言情漫」与「热血动作漫」共用一条流水线，
切图策略、拆拍密度、停顿留白、运镜幅度全是写死的常量。
本模块把差异收进**强类型能力开关与数值**，让各 Stage 只读规格、不写分支。

**三条铁律**：

1. **零回归**：默认预设 ``drama_suspense`` 的每一个数值都等于 4.43 代码里的
   现行常量 —— 存量作品（未显式切换风格）行为与 4.43 **100% 等价**。
2. **禁止按作品名写死分支**：管线主干代码只读 :class:`StyleProfileSpec`，
   绝不允许出现 ``if series_name == ...``；新增风格只需往
   :data:`STYLE_PROFILE_REGISTRY` 加一条预设，不改主干。
3. **正交**：五个维度（切图 / 蒙太奇 / 文案 / 运镜 / 节奏）互不依赖，
   风格只是它们的组合。
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any, Mapping

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示，避免运行期循环导入
    from core.config import SeriesConfig

logger = logging.getLogger(__name__)

#: 默认（基线）风格 id —— 一切未配置场景都落这里。
DEFAULT_STYLE_PROFILE_ID = "drama_suspense"

#: 动作向风格 id。
ACTION_STYLE_PROFILE_ID = "action_combat"


class SegmentStrategySpec(BaseModel):
    """Stage 2 切图策略。"""

    #: 是否允许把「暗色低方差的连续过渡带」当作画格边界（黑底漫画需要）。
    allow_dark_gutter: bool = False
    #: 暗色过渡带的行均值上限（越低越严格）。
    dark_row_mean_max: float = 28.0
    #: 是否把孤立的拟声字 / 特效碎片并入紧邻主动作画面。
    merge_sfx_fragments: bool = False
    #: 单个切片组最多容纳几张切片（1 = 现行行为，不合并）。
    max_slices_per_group: int = 1


class MontageStrategySpec(BaseModel):
    """Stage 3 / 3b / 4 的蒙太奇（连招合并）策略。"""

    #: 是否允许把连续动作格合并成一条多镜 Beat。
    allow_action_combo: bool = False
    #: 连招合并的最小格数。
    combo_min_panels: int = 3
    #: 连招合并的最大格数。
    combo_max_panels: int = 6
    #: 单幕最多容纳多少格分镜。
    max_scene_panels: int = 12


class NarrativeStrategySpec(BaseModel):
    """Stage 4 文案策略。"""

    #: 追加到主 prompt 后的风格片段文件名（``backend/prompts/styles/``）。
    prompt_fragment_file: str | None = None
    #: 是否禁止「逐格播报」的流水账式解说。
    forbid_play_by_play: bool = False
    #: 是否强调现实 / 回忆时间线过渡与心理独白留白。
    emphasize_timeline_and_inner_monologue: bool = True
    #: 拆拍密度相对基线的倍率（1.0 = 与 4.43 一致；越小越精炼）。
    target_beat_density_ratio: float = 1.0


class MotionStrategySpec(BaseModel):
    """Stage 4 / 6 运镜策略。"""

    #: 宽幅格的判定宽高比（>= 即判为宽格，可走横扫）。
    wide_pan_ratio: float = 1.35
    #: 是否优先做纵向聚焦扫镜。
    prefer_vertical_focus_sweep: bool = True
    #: 连续动态动效最多几拍（之后必须插静止）。
    max_dynamic_streak: int = 3
    #: 连续震动最多几拍（不得连震）。
    max_shake_streak: int = 1


class PacingStrategySpec(BaseModel):
    """Stage 4 叙事节奏留白策略（单位毫秒）。"""

    #: 跨分镜切换的最低留白。
    panel_switch_ms: int = 400
    #: 转折 / 悬念 / 高潮句的留白。
    turn_ms: int = 950
    #: 跨幕结尾的留白。
    scene_end_ms: int = 1150
    #: 长连说换气的留白。
    breath_ms: int = 480
    #: 同一张画面两次之间的最低留白。
    same_panel_min_ms: int = 150
    #: 语速偏置倍率（1.0 = 不改语速）。
    default_speed_bias: float = 1.0


class StyleProfileSpec(BaseModel):
    """一个完整风格预设（5 个正交维度 + 元信息）。"""

    id: str = DEFAULT_STYLE_PROFILE_ID
    label: str = ""
    description: str = ""
    recommended_for: str = ""
    segment: SegmentStrategySpec = Field(default_factory=SegmentStrategySpec)
    montage: MontageStrategySpec = Field(default_factory=MontageStrategySpec)
    narrative: NarrativeStrategySpec = Field(default_factory=NarrativeStrategySpec)
    motion: MotionStrategySpec = Field(default_factory=MotionStrategySpec)
    pacing: PacingStrategySpec = Field(default_factory=PacingStrategySpec)

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典（前端展示 / API 响应）。"""
        return self.model_dump()


#: 内置风格预设注册表。新增风格只往这里加一条，**不改任何 Stage 主干代码**。
STYLE_PROFILE_REGISTRY: dict[str, StyleProfileSpec] = {
    DEFAULT_STYLE_PROFILE_ID: StyleProfileSpec(
        id=DEFAULT_STYLE_PROFILE_ID,
        label="言情悬疑 / 剧情标准",
        description=(
            "剧情言情漫的基线风格：心理独白留白充分，现实与回忆的时间线过渡自然，"
            "切图保守、节奏舒缓、运镜克制。"
        ),
        recommended_for="剧情 / 言情 / 悬疑 / 都市漫",
        segment=SegmentStrategySpec(),
        montage=MontageStrategySpec(),
        narrative=NarrativeStrategySpec(
            prompt_fragment_file="drama_suspense.md",
            forbid_play_by_play=False,
            emphasize_timeline_and_inner_monologue=True,
            target_beat_density_ratio=1.0,
        ),
        motion=MotionStrategySpec(),
        pacing=PacingStrategySpec(),
    ),
    ACTION_STYLE_PROFILE_ID: StyleProfileSpec(
        id=ACTION_STYLE_PROFILE_ID,
        label="热血动作 / 硬核格斗",
        description=(
            "动作格斗漫的快节奏风格：黑底画风可切、拟声字碎片并入主画面、"
            "连续动作格合并成一条蒙太奇短句、停顿极短、运镜幅度更大。"
        ),
        recommended_for="动作 / 格斗 / 武侠 / 热血漫",
        segment=SegmentStrategySpec(
            allow_dark_gutter=True,
            dark_row_mean_max=28.0,
            merge_sfx_fragments=True,
            max_slices_per_group=1,
        ),
        montage=MontageStrategySpec(
            allow_action_combo=True,
            combo_min_panels=3,
            combo_max_panels=6,
            max_scene_panels=16,
        ),
        narrative=NarrativeStrategySpec(
            prompt_fragment_file="action_combat.md",
            forbid_play_by_play=True,
            emphasize_timeline_and_inner_monologue=False,
            target_beat_density_ratio=0.55,
        ),
        motion=MotionStrategySpec(
            wide_pan_ratio=1.15,
            prefer_vertical_focus_sweep=True,
            max_dynamic_streak=6,
            max_shake_streak=1,
        ),
        pacing=PacingStrategySpec(
            panel_switch_ms=160,
            turn_ms=450,
            scene_end_ms=600,
            breath_ms=260,
            same_panel_min_ms=100,
            default_speed_bias=1.0,
        ),
    ),
}


def _fallback_profile() -> StyleProfileSpec:
    """回退用的基线预设（深拷贝，避免调用方改到注册表里的原件）。"""
    return STYLE_PROFILE_REGISTRY[DEFAULT_STYLE_PROFILE_ID].model_copy(deep=True)


def get_style_profile(profile_id: str | None) -> StyleProfileSpec:
    """按 id 取风格预设；未知 / 空值安全回退到 ``drama_suspense``。

    回退时打 warning 日志（不是 error）—— 存量作品没配置风格是正常情况。
    """
    key = str(profile_id or "").strip()
    if not key:
        return _fallback_profile()
    preset = STYLE_PROFILE_REGISTRY.get(key)
    if preset is None:
        logger.warning(
            "[style_profiles] 未知风格预设 %r，已回退到 %s",
            key,
            DEFAULT_STYLE_PROFILE_ID,
        )
        return _fallback_profile()
    return preset.model_copy(deep=True)


def list_style_profiles() -> list[dict[str, Any]]:
    """列出全部内置风格预设的元信息。"""
    return [
        {
            "id": preset.id,
            "label": preset.label,
            "description": preset.description,
            "recommended_for": preset.recommended_for,
        }
        for preset in STYLE_PROFILE_REGISTRY.values()
    ]


def _deep_merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """把 ``patch`` 深度合并进 ``base``（返回新字典，不改入参）。"""
    merged = copy.deepcopy(base)
    for key, value in patch.items():
        if value is None:
            continue
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def resolve_series_style_profile(
    series_config: "SeriesConfig | None",
    *,
    overrides: Mapping[str, Any] | None = None,
) -> StyleProfileSpec:
    """解析作品最终生效的风格规格。

    合并顺序（后者覆盖前者）：

    1. 内置预设（``series_config.style_profile`` 指向的那一条）；
    2. ``series_config.style_overrides``（作品级局部覆盖）；
    3. ``overrides``（调用方的临时局部覆盖）。
    """
    preset_id = ""
    series_overrides: Mapping[str, Any] = {}
    if series_config is not None:
        preset_id = str(getattr(series_config, "style_profile", "") or "").strip()
        raw_overrides = getattr(series_config, "style_overrides", None)
        if isinstance(raw_overrides, Mapping):
            series_overrides = raw_overrides
    base = get_style_profile(preset_id).model_dump()
    merged = _deep_merge(base, series_overrides)
    if overrides:
        merged = _deep_merge(merged, overrides)
    merged["id"] = preset_id or DEFAULT_STYLE_PROFILE_ID
    return StyleProfileSpec.model_validate(merged)


#: URL / 题材关键词 → 动作风格的特征词。
_ACTION_URL_MARKERS: tuple[str, ...] = (
    "/action/",
    "/action-combat/",
    "/martial-arts/",
    "/martial_arts/",
    "/fighting/",
    "/fight/",
    "/wuxia/",
    "/combat/",
)


def suggest_style_profile_from_url_or_genre(url_or_genre: str | None) -> str:
    """按作品 URL / 题材关键词推荐风格 id。

    命中动作向特征（``/action/``、``/martial-arts/`` …）推荐 ``action_combat``，
    其余一律回 ``drama_suspense``。**不做任何作品名硬编码判断**。
    """
    text = str(url_or_genre or "").strip().lower()
    if not text:
        return DEFAULT_STYLE_PROFILE_ID
    normalized = text.replace("\\", "/")
    for marker in _ACTION_URL_MARKERS:
        if marker in normalized:
            return ACTION_STYLE_PROFILE_ID
    return DEFAULT_STYLE_PROFILE_ID


__all__ = [
    "ACTION_STYLE_PROFILE_ID",
    "DEFAULT_STYLE_PROFILE_ID",
    "MontageStrategySpec",
    "MotionStrategySpec",
    "NarrativeStrategySpec",
    "PacingStrategySpec",
    "STYLE_PROFILE_REGISTRY",
    "SegmentStrategySpec",
    "StyleProfileSpec",
    "get_style_profile",
    "list_style_profiles",
    "resolve_series_style_profile",
    "suggest_style_profile_from_url_or_genre",
]
