"""把模型返回的自由格式 JSON 归一化到规范第 3 节锁定的数据契约。

百炼等 LLM 的返回不可控：枚举值会越界（``emotion="pleading"`` 不在契约的 7 个
取值里）、列表可能塞字符串、坐标可能给 3 个数、文本可能超长。契约的字段名与
枚举值由规范锁定、不能改，因此容错只能发生在「模型输出 → 契约对象」这一层，
否则一次越界就会让整话流水线崩掉。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from utils.tts_tags import (
    MINIMAX_INTERJECTION_TAGS,
    normalize_minimax_pause_tags,
    strip_minimax_tts_tags,
)

logger = logging.getLogger(__name__)

# 与 ``core.schemas`` 保持一致的契约取值
EMOTION_VALUES: tuple[str, ...] = (
    "calm",
    "happy",
    "angry",
    "sad",
    "shocked",
    "whisper",
    "tense",
)
MOTION_VALUES: tuple[str, ...] = (
    "static",
    "zoom_in",
    "zoom_out",
    "pan_down",
    "pan_up",
    # Patch v4.28：新增横向扫镜（左右横扫）。
    "pan_left",
    "pan_right",
    "shake",
)
PLOT_THREAD_STATUS: tuple[str, ...] = ("open", "resolved")

#: Patch v3.1 Patch 10：标准音效触发标签白名单。
#:
#: Stage 4B 只在剧情高潮或明显物理动作点给 beat 标一个音效 key；Stage 6 据此
#: 生成 :class:`core.schemas.SfxEvent`（全话最多 8 个、间隔 >= 6000ms），
#: Stage 7 再去 ``assets/sfx/<sfx_key>.wav`` 取素材。非法值一律丢弃。
#:
#: Patch v4.38：白名单从 7 个扩到 **24 个**——保留原有 7 个，启用盘面已存在
#: 但一直没挂载的 4 个手机类（``phone_ringing`` / ``phone_vibrating`` /
#: ``phone_call`` / ``phone_unavailable``），再加 13 个新类（开门、耳光、
#: 拳击、刀剑交击、衣料摩擦、轰击、掌声、玻璃碎裂、雷声、钟表滴答、翻页、
#: 快门、鸣笛）。
SFX_CUE_KEYS: tuple[str, ...] = (
    "heart_thump",
    "door_slam",
    "door_open",
    "footstep",
    "slap",
    "punch",
    "sword_clash",
    "cloth_rustle",
    "whoosh",
    "gasp",
    "boom_hit",
    "applause",
    "crash",
    "glass_shatter",
    "thunder",
    "clock_tick",
    "page_turn",
    "phone_msg",
    "phone_ringing",
    "phone_vibrating",
    "phone_call",
    "phone_unavailable",
    "camera_shutter",
    "car_horn",
)

#: Patch v4.38：音效曲库目录（前端「音效」下拉与试听面板的数据源）。
#:
#: 每个条目包含：
#:
#: * ``key`` —— :data:`SFX_CUE_KEYS` 里的白名单键；
#: * ``label`` —— 中文名（前端展示用）；
#: * ``category`` —— 分类（前端按分类分组下拉）；
#: * ``fallback_key`` —— **盘面暂时没有该素材时回源复用的通用音效键**
#:   （``None`` 表示没有兜底，缺素材就真缺）。回源链最长两级，不会递归成环。
#: Patch v4.42：一级筛选大类（音效资源管理器顶部的分段按钮）。
#:
#: ``category`` 是**细粒度**标签（生理 / 门 / 击打 / 爆炸 / 通讯 …），共 13 类，
#: 太碎，不适合做「先按大类缩小范围、再按细类定位」的浏览动线；
#: ``group`` 是给它加的一层**一级大类**，固定四个取值。
#:
#: - 动作：动作本身会发生的打击/脚步/开门类，最常用；
#: - 转场：承担「翻页/换气/破空」这类节奏衔接的音效；
#: - 环境：场景底噪类（撞击、碎裂、雷声、钟表、翻页）；
#: - 道具：具体物件发出的声音（手机、相机、汽车）。
SFX_GROUP_CATEGORIES: tuple[str, ...] = ("动作", "环境", "转场", "道具")

SFX_CUE_CATALOG: tuple[dict[str, str], ...] = (
    {"key": "heart_thump", "label": "心跳", "category": "生理", "group": "动作", "fallback_key": ""},
    {"key": "gasp", "label": "倒吸一口气", "category": "生理", "group": "转场", "fallback_key": ""},
    {"key": "door_slam", "label": "摔门", "category": "门", "group": "动作", "fallback_key": "crash"},
    {"key": "door_open", "label": "开门", "category": "门", "group": "动作", "fallback_key": "whoosh"},
    {"key": "footstep", "label": "脚步", "category": "脚步", "group": "动作", "fallback_key": ""},
    {"key": "slap", "label": "耳光", "category": "击打", "group": "动作", "fallback_key": "door_slam"},
    {"key": "punch", "label": "拳击", "category": "击打", "group": "动作", "fallback_key": "crash"},
    {"key": "sword_clash", "label": "刀剑交击", "category": "击打", "group": "动作", "fallback_key": "crash"},
    {"key": "boom_hit", "label": "轰击", "category": "爆炸", "group": "转场", "fallback_key": "crash"},
    {"key": "crash", "label": "撞击", "category": "爆炸", "group": "环境", "fallback_key": ""},
    {"key": "glass_shatter", "label": "玻璃碎裂", "category": "碎裂", "group": "环境", "fallback_key": "crash"},
    {"key": "thunder", "label": "雷声", "category": "爆炸", "group": "环境", "fallback_key": "crash"},
    {"key": "cloth_rustle", "label": "衣料摩擦", "category": "摩擦", "group": "动作", "fallback_key": "whoosh"},
    {"key": "whoosh", "label": "破空", "category": "摩擦", "group": "转场", "fallback_key": ""},
    {"key": "applause", "label": "掌声", "category": "人群", "group": "转场", "fallback_key": "gasp"},
    {"key": "clock_tick", "label": "钟表滴答", "category": "钟表", "group": "环境", "fallback_key": ""},
    {"key": "page_turn", "label": "翻页", "category": "纸页", "group": "环境", "fallback_key": "whoosh"},
    {"key": "camera_shutter", "label": "快门", "category": "相机", "group": "道具", "fallback_key": "whoosh"},
    {"key": "car_horn", "label": "鸣笛", "category": "交通", "group": "道具", "fallback_key": "whoosh"},
    {"key": "phone_msg", "label": "短信提示", "category": "通讯", "group": "道具", "fallback_key": ""},
    {"key": "phone_ringing", "label": "电话铃响", "category": "通讯", "group": "道具", "fallback_key": ""},
    {"key": "phone_vibrating", "label": "手机震动", "category": "通讯", "group": "道具", "fallback_key": ""},
    {"key": "phone_call", "label": "通话接通", "category": "通讯", "group": "道具", "fallback_key": "phone_msg"},
    {"key": "phone_unavailable", "label": "忙音", "category": "通讯", "group": "道具", "fallback_key": "phone_msg"},
)

#: Patch v4.38：音效键别名（小规模，只收敛**明确同义**的写法）。
#:
#: 只做「一个词明确指向同一个音效」的收敛；``none`` / ``not_a_real_key``
#: 这类不是同义词、而是「没有音效」，一律不在表里，照旧归一化成 ``None``。
SFX_CUE_ALIASES: dict[str, str] = {
    "slap_face": "slap",
    "phone_ring": "phone_ringing",
    "vibrating": "phone_vibrating",
    "explosion": "boom_hit",
}

#: Patch v4.38：音效键 → 兜底键（由 :data:`SFX_CUE_CATALOG` 派生）。
#:
#: 空串表示「没有兜底」——盘面缺素材就是真缺，不静默换一个不相干的音效。
SFX_CUE_FALLBACKS: dict[str, str] = {
    entry["key"]: (entry.get("fallback_key") or "")
    for entry in SFX_CUE_CATALOG
}


def sfx_group_of(sfx_key: str, *, assets_dir: str | Path | None = None) -> str:
    """返回音效键的**一级大类**（动作 / 环境 / 转场 / 道具）。

    Patch v4.42：音效资源管理器顶部的分段按钮用这一层做粗筛；
    找不到键时返回空串（前端按「未归类」处理，不丢条目）。

    Patch v4.43：改查**动态生效曲库**（内置 24 条 + 用户自定义条目），
    传入 ``assets_dir`` 可指定另一个资产根目录。
    """
    key = str(sfx_key or "").strip().lower()
    for entry in get_effective_sfx_catalog(assets_dir=assets_dir):
        if entry["key"] == key:
            return entry.get("group") or ""
    return ""


def sfx_fallback_key(sfx_key: str, *, assets_dir: str | Path | None = None) -> str:
    """返回音效键的**兜底键**（没有兜底则返回空串）。

    Patch v4.38：Stage 7 找不到 ``sfx/<key>.*`` 真实素材时，用它回源复用通用音效。

    Patch v4.43：改查**动态生效曲库**，自定义音效也能配自己的兜底。
    """
    key = str(sfx_key or "").strip().lower()
    for entry in get_effective_sfx_catalog(assets_dir=assets_dir):
        if entry["key"] == key:
            return entry.get("fallback_key") or ""
    return ""


# --------------------------------------------------------------------------
# Patch v4.43：全局音效资源库（动态曲库注册表）
# --------------------------------------------------------------------------
#: 用户自定义音效的注册表文件名（落在 ``<assets>/sfx/`` 下）。
#:
#: 结构：``{"custom": [...], "overrides": {...}, "hidden": [...]}``
#:
#: * ``custom`` —— 用户新增的自定义音效条目（与内置条目同构）；
#: * ``overrides`` —— 对内置条目的元数据覆盖（label / group / category / fallback_key）；
#: * ``hidden`` —— 被停用的内置音效键（停用后不在生效曲库里，但随时可还原）。
#:
#: 文件缺失 / 不是合法 JSON / 结构损坏 —— 一律安全回退成空配置，
#: **绝不抛异常**：曲库配置坏了不该让整条流水线崩掉。
SFX_CUSTOM_CONFIG_FILENAME = "custom_sfx.json"

#: 自定义音效键的合法形状：``^[a-z][a-z0-9_]{1,39}$``（小写开头，可含数字与下划线）。
SFX_CUSTOM_KEY_RE: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

#: 音效元数据里允许被用户覆盖 / 自定义的字段。
SFX_EDITABLE_META_FIELDS: tuple[str, ...] = ("label", "group", "category", "fallback_key")

#: 动态曲库缓存：键 = 配置文件路径，值 = (mtime, catalog, keys)。
#:
#: 归一化每句都要校验音效标签，逐句读盘太慢；配置文件是静态资产，
#: 按 mtime 缓存即可，用户一改配置缓存自然失效。
_SFX_REGISTRY_CACHE: dict[str, tuple[float, list[dict[str, str]], tuple[str, ...]]] = {}


def _sfx_assets_root(assets_dir: str | Path | None = None) -> Path:
    """音效资产根目录（缺省取全局 ``assets_root()``）。

    延迟导入 ``core.config``：``core.config`` 不依赖 ``core.normalize``，
    但顶层互相 import 容易在测试里踩到循环导入。
    """
    if assets_dir is not None:
        return Path(assets_dir).expanduser()
    from core.config import assets_root  # 局部导入，避免模块级循环依赖

    return Path(assets_root())


def _sfx_config_path(assets_dir: str | Path | None = None) -> Path:
    """自定义音效注册表文件的绝对路径。"""
    return _sfx_assets_root(assets_dir) / "sfx" / SFX_CUSTOM_CONFIG_FILENAME


def load_custom_sfx_config(assets_dir: str | Path | None = None) -> dict[str, Any]:
    """读取自定义音效注册表（文件缺失 / 损坏时安全回退空配置）。"""
    path = _sfx_config_path(assets_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    custom: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw.get("custom") or []:
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("key") or "").strip().lower()
        if not SFX_CUSTOM_KEY_RE.match(key) or key in seen:
            continue
        seen.add(key)
        custom.append(
            {
                "key": key,
                "label": str(item.get("label") or key),
                "category": str(item.get("category") or "自定义"),
                "group": str(item.get("group") or "道具"),
                "fallback_key": str(item.get("fallback_key") or ""),
            }
        )

    overrides: dict[str, dict[str, str]] = {}
    raw_overrides = raw.get("overrides")
    if isinstance(raw_overrides, Mapping):
        for key, patch in raw_overrides.items():
            name = str(key or "").strip().lower()
            if not name or not isinstance(patch, Mapping):
                continue
            overrides[name] = {
                field: str(patch.get(field) or "")
                for field in SFX_EDITABLE_META_FIELDS
                if field in patch
            }

    hidden: list[str] = []
    for item in raw.get("hidden") or []:
        name = str(item or "").strip().lower()
        if name and name not in hidden:
            hidden.append(name)

    return {"custom": custom, "overrides": overrides, "hidden": hidden}


def save_custom_sfx_config(
    config: Mapping[str, Any], assets_dir: str | Path | None = None
) -> None:
    """写回自定义音效注册表（原子写：先写临时文件再替换）。"""
    path = _sfx_config_path(assets_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "custom": list(config.get("custom") or []),
        "overrides": dict(config.get("overrides") or {}),
        "hidden": list(config.get("hidden") or []),
    }
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)
    _SFX_REGISTRY_CACHE.pop(str(path), None)


def get_effective_sfx_catalog(
    assets_dir: str | Path | None = None,
) -> list[dict[str, str]]:
    """返回**当前生效**的音效曲库（内置 24 条 + 覆盖 + 自定义 − 停用）。

    生效顺序：

    1. 内置 :data:`SFX_CUE_CATALOG` 24 条；
    2. 套上 ``overrides`` 里的元数据覆盖（label / group / category / fallback_key）；
    3. 追加 ``custom`` 里的自定义条目（同名覆盖内置条目）；
    4. 剔除 ``hidden`` 里的键（停用的内置音效）。

    返回的每个条目形如
    ``{"key", "label", "category", "group", "fallback_key"}``。
    """
    path = _sfx_config_path(assets_dir)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = -1.0
    cached = _SFX_REGISTRY_CACHE.get(str(path))
    if cached is not None and cached[0] == mtime:
        return [dict(entry) for entry in cached[1]]

    config = load_custom_sfx_config(assets_dir)
    overrides = config["overrides"]
    hidden = set(config["hidden"])

    catalog: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in SFX_CUE_CATALOG:
        key = entry["key"]
        if key in hidden:
            continue
        patch = overrides.get(key) or {}
        merged = {
            "key": key,
            "label": patch.get("label") or entry["label"],
            "category": patch.get("category") or entry["category"],
            "group": patch.get("group") or entry.get("group") or "",
            "fallback_key": patch.get("fallback_key") or entry.get("fallback_key") or "",
        }
        catalog.append(merged)
        seen.add(key)

    for entry in config["custom"]:
        key = entry["key"]
        if key in hidden:
            continue
        if key in seen:
            # 自定义条目与内置同名 —— 以自定义为准（后写覆盖先写）。
            for index, item in enumerate(catalog):
                if item["key"] == key:
                    catalog[index] = dict(entry)
                    break
            continue
        catalog.append(dict(entry))
        seen.add(key)

    _SFX_REGISTRY_CACHE[str(path)] = (
        mtime,
        [dict(entry) for entry in catalog],
        tuple(entry["key"] for entry in catalog),
    )
    return catalog


def get_effective_sfx_keys(
    assets_dir: str | Path | None = None,
) -> tuple[str, ...]:
    """返回当前生效的音效键集合（内置 + 自定义 − 停用）。"""
    return tuple(entry["key"] for entry in get_effective_sfx_catalog(assets_dir=assets_dir))


def is_valid_sfx_key(sfx_key: str, assets_dir: str | Path | None = None) -> bool:
    """判断一个音效键在**当前生效曲库**里是否合法。"""
    key = str(sfx_key or "").strip().lower()
    if not key:
        return False
    return key in get_effective_sfx_keys(assets_dir=assets_dir)



#: Patch v4.43：一句解说上最多保留几条音效建议。
MAX_SFX_SUGGESTIONS: int = 3

#: Patch v4.43：音效建议的合法来源标记。
SFX_SUGGESTION_SOURCES: tuple[str, ...] = ("llm", "context_rule")


def normalize_sfx_suggestions(
    raw: Any,
    *,
    sfx_cue: str | None = None,
    assets_dir: str | Path | None = None,
) -> list[dict[str, str]]:
    """规范化一句解说上的音效建议（Patch v4.43）。

    规则：

    1. 接受 ``list[dict]``（完整建议）或 ``list[str]``（只有 key）；
    2. **别名收敛**：``slap_face`` → ``slap``（与 :data:`SFX_CUE_ALIASES` 一致）；
    3. **有效性校验**：key 必须属于**当前生效曲库**（内置 24 条 + 自定义 − 停用），
       非法的一律丢弃；
    4. **按 key 去重**：同一个 key 只留第一条；
    5. **最多 3 条**（:data:`MAX_SFX_SUGGESTIONS`）；
    6. ``source`` 只认 ``llm`` / ``context_rule``，其余回落 ``llm``；
    7. **旧数据兼容**：本句只填了旧的整句级 ``sfx_cue`` 时，自动把它转成一条
       建议（``source="llm"``），让旧剧本在新的建议栏里也看得见。
    """
    items: list[dict[str, str]] = []
    seen: set[str] = set()

    def _append(key: str, reason: str, source: str, anchor_text: str) -> None:
        if len(items) >= MAX_SFX_SUGGESTIONS:
            return
        if key in seen:
            return
        seen.add(key)
        items.append(
            {
                "key": key,
                "reason": reason,
                "source": source,
                "anchor_text": anchor_text,
            }
        )

    for entry in coerce_list(raw):
        if isinstance(entry, Mapping):
            raw_key = entry.get("key")
            reason = coerce_str(entry.get("reason"))
            source = coerce_str(entry.get("source")).strip().lower()
            anchor_text = coerce_str(entry.get("anchor_text"))
        else:
            raw_key = entry
            reason = ""
            source = ""
            anchor_text = ""
        key = coerce_str(raw_key).strip().lower()
        if not key:
            continue
        key = SFX_CUE_ALIASES.get(key, key)
        if not is_valid_sfx_key(key, assets_dir=assets_dir):
            continue
        _append(
            key,
            reason,
            source if source in SFX_SUGGESTION_SOURCES else "llm",
            anchor_text,
        )

    if not items:
        legacy = coerce_str(sfx_cue).strip().lower()
        legacy = SFX_CUE_ALIASES.get(legacy, legacy)
        if legacy and is_valid_sfx_key(legacy, assets_dir=assets_dir):
            _append(legacy, "旧剧本的整句级音效标记", "llm", "")

    return items


# --------------------------------------------------------------------------
# Patch v4.40：句中内联音效标签（``[sfx:key]``）
# --------------------------------------------------------------------------
#: 内联音效标签的形状：``[sfx:key]``（允许 ``sfx`` 与冒号之间有空白）。
#:
#: 标签**只允许出现在送音文本（``tts_text``）里**：它标的是「这一句的第几毫秒
#: 响一下音效」，不是台词。字幕轨（``narration_text``）若原样带上，屏幕上会
#: 印出 ``[sfx:slap]`` 这种莫名其妙的东西，所以字幕轨一律剥除。
#:
#: 与 :data:`utils.tts_tags.INLINE_SFX_TAG_PATTERN` 同形（两边各留一份是了
#: 避免 ``utils.tts_tags`` 反向依赖 ``core.normalize`` 形成循环导入；
#: ``tests/test_patch_v440_inline_sfx_timing.py`` 会断言两者 pattern 一致）。
INLINE_SFX_TAG_RE: re.Pattern[str] = re.compile(
    r"\[\s*sfx\s*:\s*([a-zA-Z0-9_\-]+)\s*\]"
)

#: 相邻重复的内联音效标签（中间只隔空白）会被折叠成一个。
_INLINE_SFX_DUPLICATE_RE: re.Pattern[str] = re.compile(
    r"(\[\s*sfx\s*:\s*[a-zA-Z0-9_\-]+\s*\])(?:\s*\[\s*sfx\s*:\s*[a-zA-Z0-9_\-]+\s*\])+"
)


def normalize_inline_sfx_tags(
    text: str, *, assets_dir: str | Path | None = None
) -> str:
    """规范化送音文本里的内联音效标签（Patch v4.40）。

    规则：

    1. **别名收敛**：``[sfx:slap_face]`` → ``[sfx:slap]``（与 :data:`SFX_CUE_ALIASES` 一致）；
    2. **白名单校验**：key 不在**当前生效曲库**里的标签**直接剥离**
       （既不报错也不保留——模型写错一个键不该让整句配音失败）。
       Patch v4.43 起白名单 = 内置 24 条 + 用户自定义 − 已停用；
    3. **规范写法**：合法标签统一写成 ``[sfx:<key>]``（去掉多余空白）；
    4. **相邻重复折叠**：``[sfx:slap][sfx:slap]`` → ``[sfx:slap]``
       （同一个音效连着响两遍没有意义，反而会挤掉别的音效事件）；
    5. 其它文本**原样保留**（含停顿标签 ``<#0.5#>`` 与语气词标签）。

    参数
    ----
    text:
        原始送音文本。
    assets_dir:
        音效资产根目录（缺省取全局 ``assets_root()``）。

    返回
    ----
    ``str``
        规范化后的送音文本。
    """
    if not text or "sfx" not in text.lower():
        return text

    valid_keys = set(get_effective_sfx_keys(assets_dir=assets_dir))

    def _replace(match: re.Match[str]) -> str:
        key = SFX_CUE_ALIASES.get(match.group(1).strip().lower(), match.group(1).strip().lower())
        if key not in valid_keys:
            return ""
        return f"[sfx:{key}]"

    normalized = INLINE_SFX_TAG_RE.sub(_replace, text)

    # 相邻重复折叠：连续多个标签只保留第一个（合法标签已统一写法，比对更简单）。
    def _collapse(match: re.Match[str]) -> str:
        return re.match(r"\[\s*sfx\s*:\s*[a-zA-Z0-9_\-]+\s*\]", match.group(0)).group(0)

    normalized = _INLINE_SFX_DUPLICATE_RE.sub(_collapse, normalized)
    return normalized

# 契约里的长度上限
MAX_NARRATION_CHARS = 120
MIN_NARRATION_CHARS = 2

#: Patch v3.2 Patch 12：说话人性别合法值。与 ``core.schemas.ScriptBeat`` 一致。
SPEAKER_GENDERS: tuple[str, ...] = ("male", "female", "unknown")

#: Patch v4.40：音效在本句内的落点。与 ``core.schemas.ScriptBeat.sfx_position`` 一致。
#:
#: * ``start``：句首（默认，等价于 Patch v3.1 的 beat 级 ``sfx_cue`` 行为）；
#: * ``mid``：句中（该句音频时长的一半处）；
#: * ``end``：句尾（该句音频结束前 150ms）。
SFX_POSITIONS: tuple[str, ...] = ("start", "mid", "end")


def normalize_enum(value: Any, allowed: Sequence[str], default: str) -> str:
    """把模型返回的枚举值收敛到契约允许的取值。

    依次尝试：完全相等 → 忽略大小写 → 归一化连字符/空格 → 唯一包含关系匹配；
    都不成立时回落到 ``default``。
    """
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return default
        low = raw.lower()
        for item in allowed:
            if low == item:
                return item
        squashed = low.replace("-", "_").replace(" ", "_")
        for item in allowed:
            if squashed == item:
                return item
        hits = [item for item in allowed if item in squashed or squashed in item]
        if len(hits) == 1:
            return hits[0]
    return default


def coerce_str(value: Any, default: str = "") -> str:
    """转成字符串；None / 容器类型回落到默认值。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return default


def coerce_list(value: Any) -> list[Any]:
    """转成列表；标量视为单元素列表，None 视为空列表。"""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if value is None:
        return []
    return [value]


def coerce_str_list(value: Any, *, limit: int | None = None) -> list[str]:
    """转成字符串列表（去掉空串，可选截断长度）。"""
    items: list[str] = []
    for item in coerce_list(value):
        if isinstance(item, dict):
            item = item.get("text") or item.get("name") or item.get("value")
        text = coerce_str(item)
        if text:
            items.append(text)
    if limit is not None:
        items = items[:limit]
    return items


def coerce_bool(value: Any, default: bool = False) -> bool:
    """转成布尔值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "yes", "y", "1", "是", "对"}:
            return True
        if low in {"false", "no", "n", "0", "否", "错"}:
            return False
    return default


def coerce_int(
    value: Any,
    default: int = 0,
    *,
    low: int | None = None,
    high: int | None = None,
) -> int:
    """转成整数并夹到 ``[low, high]`` 区间。"""
    if isinstance(value, bool):
        number = int(value)
    elif isinstance(value, (int, float)):
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(float(value.strip()))
        except ValueError:
            number = default
    else:
        number = default
    if low is not None and number < low:
        number = low
    if high is not None and number > high:
        number = high
    return number


def coerce_float(
    value: Any,
    default: float | None = None,
    *,
    low: float | None = None,
    high: float | None = None,
) -> float | None:
    """转成浮点数；无法解析时返回 ``default``（默认 ``None``）。

    Patch v4.13：用于读取单句语速 ``speed_override``——模型可能给
    ``1.1``、``"1.1"`` 或 ``null``，三种都要能安全落盘。
    """
    if isinstance(value, bool):
        number: float | None = float(value)
    elif isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            number = default
    else:
        number = default
    if number is None:
        return None
    if low is not None and number < low:
        number = low
    if high is not None and number > high:
        number = high
    return number


def coerce_floats(value: Any, count: int = 4) -> tuple[float, ...] | None:
    """把坐标转成定长浮点元组；数量不对时返回 ``None``（契约允许为空）。"""
    items = coerce_list(value)
    numbers: list[float] = []
    for item in items:
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            numbers.append(float(item))
        elif isinstance(item, str):
            try:
                numbers.append(float(item.strip()))
            except ValueError:
                continue
    if len(numbers) != count:
        return None
    return tuple(numbers)


def clip_text(value: str, limit: int = MAX_NARRATION_CHARS) -> str:
    """按「可发音字符数」截断文本，MiniMax 标签整段保留且不计入额度。

    Patch v4.14：模型偶尔会写出 ``(clear-throat)`` / ``<#0.5#>`` 这类标签，
    硬切会切出 ``(clear-th`` 或 ``<#0.`` 这种半截标签，模型端解析会直接
    报错。这里的规则是：

    * 语气词 / 停顿标签**不计入**长度额度（标签是给模型的指令，不是台词）；
    * 标签要么完整保留，要么整段丢弃，**绝不切一半**；
    * 正文精确截到 ``limit`` 字，因此结果可能比 ``limit`` 略长（多出的
      是保留的标签字符）。
    """
    if not value:
        return value
    if spoken_length(value) <= limit:
        return value
    out: list[str] = []
    budget = limit
    cursor = 0
    for match in _MINIMAX_TAG_SPAN_PATTERN.finditer(value):
        chunk = value[cursor : match.start()]
        spoken = spoken_length(chunk)
        if spoken > budget:
            # 正文已经超额度：截断正文，并丢弃这条标签及其后内容。
            out.append(chunk[:budget])
            return "".join(out).strip()
        out.append(chunk)
        budget -= spoken
        cursor = match.end()
        # 标签本身不计入额度，完整保留。
        out.append(match.group(0))
    out.append(value[cursor:][:budget])
    return "".join(out).strip()


# --------------------------------------------------------------------------
# Patch v4.13 / v4.14：繁简统一 + 标签感知的长度控制
# --------------------------------------------------------------------------
#: MiniMax 语气词 / 停顿标签在文本中的位置（截断时不能切在标签中间）。
#:
#: Patch v4.40：内联音效标签 ``[sfx:key]`` 也纳入——截断送音文本时不能把标签
#: 从中间切开（``[sfx:sla`` 会被模型端解析报错），标签字符也不计入长度额度。
_MINIMAX_TAG_SPAN_PATTERN: re.Pattern[str] = re.compile(
    "|".join(re.escape(tag) for tag in MINIMAX_INTERJECTION_TAGS)
    + r"|<#\d+(?:\.\d{1,2})?#>"
    + r"|" + INLINE_SFX_TAG_RE.pattern
)

#: 单句语速合法区间（与 ``core.schemas.ScriptBeat.speed_override`` 一致）。
MIN_SPEED_OVERRIDE = 0.5
MAX_SPEED_OVERRIDE = 2.0

#: OpenCC 繁简转换器（惰性创建；缺依赖时降级为「不转换」）。
_T2S_CONVERTER: Any = None
_T2S_READY = False

#: Patch v4.44：转换配置候选（按优先级）。
#:
#: ``tw2sp`` = 繁体中文 → 简体 + 台湾常用词 → 大陆常用词（带词汇转换），
#: 对「後臺 / 計畫 / 資訊」这类繁台用词更友好；缺该配置时无缝降级 ``t2s``
#: （只换字形、不动词汇），再不行才降级为「不转换」。
_T2S_CONFIGS: tuple[str, ...] = ("tw2sp", "t2s")

#: 当前生效的 OpenCC 配置名（``None`` = 未启用转换）。
_T2S_ACTIVE_CONFIG: str | None = None


def _load_opencc_converter(config_name: str) -> Any:
    """按配置名创建 OpenCC 转换器；不可用返回 ``None``（只降级）。"""
    try:
        from opencc import OpenCC
    except Exception as exc:  # noqa: BLE001 - 缺依赖只降级，不阻断流水线
        logger.warning("[normalize] OpenCC 不可用，繁简转换降级为不转换：%s", exc)
        return None
    try:
        return OpenCC(config_name)
    except Exception as exc:  # noqa: BLE001 - 单个配置不可用就试下一个
        logger.warning(
            "[normalize] OpenCC 配置 %s 不可用（%s），尝试下一个降级配置",
            config_name,
            exc,
        )
        return None


def _get_t2s_converter() -> Any:
    """惰性创建 OpenCC 转换器；优先 ``tw2sp``，缺配置时降级 ``t2s``。

    Patch v4.44：繁中作品入库的台词常带台湾用词（後臺、計畫、資訊），
    只做字形转换会留下繁台词汇；``tw2sp`` 能连词汇一起转简体。

    缺依赖或初始化失败时返回 ``None``（只降级，不阻断流水线）。
    """
    global _T2S_CONVERTER, _T2S_READY, _T2S_ACTIVE_CONFIG
    if _T2S_READY:
        return _T2S_CONVERTER
    _T2S_READY = True
    for config_name in _T2S_CONFIGS:
        converter = _load_opencc_converter(config_name)
        if converter is not None:
            _T2S_CONVERTER = converter
            _T2S_ACTIVE_CONFIG = config_name
            logger.info("[normalize] OpenCC 繁简转换器已启用：%s", config_name)
            break
    if _T2S_CONVERTER is None:
        logger.warning("[normalize] 没有可用的 OpenCC 配置，繁简转换降级为不转换")
    return _T2S_CONVERTER


def to_simplified_chinese(text: str) -> str:
    """把文本里的繁体字转成简体（Patch v4.13）。

    全链路只产出简体，避免同一句台词在字幕 / 送音 / 视觉抽取三处出现
    繁简混排（例如「後臺」与「后台」）。ASCII 字符、标点与 MiniMax 标签
    （``(coughs)`` / ``<#0.5#>``）原样透传，OpenCC 不会动它们。

    参数
    ----
    text:
        原始文本。

    返回
    ----
    ``str``
        转换后的简体文本；转换器不可用时原样返回。
    """
    if not text:
        return ""
    converter = _get_t2s_converter()
    if converter is None:
        return text
    try:
        converted = converter.convert(text)
    except Exception as exc:  # noqa: BLE001 - 转换失败只降级，不阻断流水线
        logger.warning("[normalize] 繁简转换失败，原样返回：%s", exc)
        return text
    # Patch v4.44：``tw2sp`` 是「台湾用语 → 大陆用语」的整句转换，对**已经是
    # 简体**的文本会反向劣化（例如「为什么」→「为什幺」）。只有原文确实含
    # 繁体字时才做转换；纯简体文本原样返回（零回归铁律）。
    if _T2S_ACTIVE_CONFIG == "tw2sp":
        plain = _load_opencc_converter("t2s")
        if plain is not None:
            try:
                if plain.convert(text) == text:
                    return text
            except Exception:  # noqa: BLE001 - 判断失败就按转换结果走
                pass
    return converted


def spoken_length(text: str) -> int:
    """文本里「真正会被念出来」的字符数（MiniMax 标签不计入）。

    语气词 / 停顿标签是给模型的指令，不占字幕与送音的语义长度预算。
    """
    if not text:
        return 0
    return len(strip_minimax_tts_tags(text))


def clip_text_by_spoken_length(value: str, limit: int = MAX_NARRATION_CHARS) -> str:
    """按「可发音正文长度」截断文本，标签字符不计入额度。

    Patch v4.14：与 :func:`clip_text` 同语义（标签豁免额度、绝不腰斩），
    单独起名是为了在送音文本（``tts_text``）链路上表意更清楚。
    """
    return clip_text(value, limit)


# --------------------------------------------------------------------------
# Patch v3.1 Patch 9：字幕 / TTS 字符净化
# --------------------------------------------------------------------------
#: 逐字符剔除的「字体缺失 / 无法发音」符号。
#:
#: 成片 03:46 出现过一个「☒」方块，根因是台词里带了 ``♥`` 这类装饰符号：
#: 中文字体没有这些字形，字幕渲染画成豆腐块；TTS 则直接跳过该字符（读出来缺字）。
HYGIENE_STRIP_CHARS = "♥♡❤❥★☆♪♫♬✦✧※☒☐□■◆◇●○"

#: 整段剔除的 Unicode 区段（控制字符 / 零宽 / Emoji / 私用区 / 变体选择符）。
HYGIENE_STRIP_RANGES: tuple[tuple[int, int], ...] = (
    (0x0000, 0x0008),  # C0 控制字符（\t 与 \n 单独放行）
    (0x000B, 0x000C),  # \v / \f
    (0x000E, 0x001F),  # \r 及其后
    (0x007F, 0x009F),  # DEL + C1 控制字符
    (0x200B, 0x200D),  # 零宽空格 / ZWNJ / ZWJ
    (0x2600, 0x27BF),  # Misc Symbols + Dingbats（★ ☆ ✓ ✗ …）
    (0x2B00, 0x2BFF),  # Misc Symbols and Arrows（⭐ …）
    (0x1F000, 0x1F2FF),  # 麻将 / 多米诺 / 扑克
    (0x1F300, 0x1FAFF),  # Misc Symbols and Pictographs（绝大多数 Emoji）
    (0xE000, 0xF8FF),  # Private Use Area（私用区）
    (0xFE00, 0xFE0F),  # Variation Selectors（含 U+FE0F）
    (0xFEFF, 0xFEFF),  # BOM / 零宽不换行空格
)


def _in_hygiene_range(code: int) -> bool:
    """判断码位是否落在需要整体剔除的区段内。"""
    for low, high in HYGIENE_STRIP_RANGES:
        if low <= code <= high:
            return True
    return False


def sanitize_narration_and_tts(text: str, *, for_tts: bool = False) -> str:
    """清除会渲染成豆腐块「☒」或让 TTS 吞字的字符。

    * 删除字体大概率缺失、且无法发音的装饰符号（``♥`` ``★`` ``☒`` …）；
    * 删除 Emoji 区段、私用区字符与 ``U+FE0F`` 变体选择符；
    * 归一化异常波浪号 / 破折号组合；``for_tts=True`` 时额外去掉末尾多余的
      ``~``（合成时会读出怪音）；
    * Patch v4.40：``for_tts=False``（字幕轨）时**剥除**内联音效标签
      ``[sfx:key]``——屏幕绝不印出音效标记；``for_tts=True`` 时**保留并规范化**
      合法标签（这是音效落点的唯一来源）。

    参数
    ----
    text:
        原始文本。
    for_tts:
        该文本是否会送进 TTS 合成（``tts_text``）。``True`` 时执行尾符清理；
        ``False`` 时保留单个 ``~``（字幕里它还可能承担语气符号的作用）。
    """
    if not text:
        return ""
    kept: list[str] = []
    for char in text:
        if char in ("\n", "\t"):
            kept.append(char)
            continue
        if char in HYGIENE_STRIP_CHARS:
            continue
        if _in_hygiene_range(ord(char)):
            continue
        kept.append(char)
    cleaned = "".join(kept)
    # 全角波浪号归一化成半角，后续统一按 "~" 处理。
    cleaned = cleaned.replace("～", "~")
    # 连续波浪号只留 1 个；连续破折号收敛成中文标准的一组「——」。
    cleaned = re.sub(r"~{2,}", "~", cleaned)
    cleaned = re.sub(r"—{3,}", "——", cleaned)
    # 删掉符号后可能留下多余空白。
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]*\n[ \t]*", "\n", cleaned)
    cleaned = cleaned.strip()
    if for_tts:
        # 末尾多余的 "~" 会让合成读出怪音，直接去掉。
        cleaned = cleaned.rstrip("~ ")
        # Patch v4.40：送音轨保留内联音效标签（别名收敛 + 白名单校验 + 相邻去重）。
        cleaned = normalize_inline_sfx_tags(cleaned)
    else:
        # Patch v4.40：字幕轨绝不能印出 [sfx:key]。
        cleaned = INLINE_SFX_TAG_RE.sub("", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned


#: Patch v3.2 Patch 12：对话引号归一化。
#:
#: 模型偶尔会把对话写成单引号（``‘…’`` / ``'…'``）或英文直引号（``"…"``），
#: 下游的「引号对白拆分」只认中文双引号，所以落盘前统一收敛成 ``“…”``。
#: 英文缩写（``I'm`` / ``don't``）前后都是 ASCII 字母，不会命中任何规则。
_DIALOGUE_QUOTE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # 中文冒号 + 中文单引号：道振说：‘别怕，有我在。’
    (re.compile(r"：‘([^’\n]{2,120})’"), "：“\\1”"),
    # 中文冒号 + 英文单引号：道振说：'别怕，有我在。'
    (re.compile(r"：'([^'\n]{2,120})'"), "：“\\1”"),
    # 半角冒号 + 英文双引号：道振说:"别怕"
    (re.compile(r":\"([^\"\n]{2,120})\""), "：“\\1”"),
    # 中文冒号 + 英文双引号：道振说："别怕"
    (re.compile(r"：\"([^\"\n]{2,120})\""), "：“\\1”"),
    # 紧跟在汉字后的单引号对白：他说‘别怕’，然后走了
    (re.compile(r"(?<=[\u4e00-\u9fff])‘([^’\n]{2,120})’"), "“\\1”"),
    (re.compile(r"(?<=[\u4e00-\u9fff])'([^'\n]{2,120})'"), "“\\1”"),
)

#: 整段被单引号包裹的独立台词（前后是空白或标点）。
_STANDALONE_QUOTE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?:^|(?<=[\s，。！？：；、,!?;]))‘([^’\n]{2,120})’"
            r"(?=$|[\s，。！？：；、,!?;])"
        ),
        "“\\1”",
    ),
    (
        re.compile(
            r"(?:^|(?<=[\s，。！？：；、,!?;]))'([^'\n]{2,120})'"
            r"(?=$|[\s，。！？：；、,!?;])"
        ),
        "“\\1”",
    ),
)


def normalize_dialogue_quotes(text: str) -> str:
    """把对话里的单引号 / 英文直引号统一成中文双引号 ``“…”``。

    Patch v3.2 Patch 12：大模型偶尔把对白写成 ``‘…’`` 或 ``"…"``，
    而 Stage 4B 的「旁白 + 引号对白」确定性拆分只认中文双引号，
    所以在归一化阶段先把引号形态收敛掉。

    * ``道振说：‘别怕，有我在。’`` → ``道振说：“别怕，有我在。”``
    * ``"别怕"`` → ``“别怕”``
    * 英文缩写（``I'm`` / ``don't``）前后都是 ASCII 字母，不会被误替换。
    """
    if not text:
        return ""
    cleaned = text
    for pattern, replacement in _DIALOGUE_QUOTE_RULES:
        cleaned = pattern.sub(replacement, cleaned)
    for pattern, replacement in _STANDALONE_QUOTE_RULES:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


#: 相邻重复对白去重用的文本指纹：去掉标点 / 引号 / 空白。
#:
#: 模型写出的两句「这个地方是用来做什么的呀？」可能只差一个问号，
#: 直接比字符串判不出重复，去掉标点后就能识别。
_DEDUPE_STRIP_RE = re.compile(r"[^\w]+", re.UNICODE)

#: 「后一条被前一条完全包含」时，后一条至少要有这么多字才算重复（Patch v3.4）。
_DEDUPE_CONTAINED_MIN_CHARS = 6


def _dedupe_text_key(text: Any) -> str:
    """去重比较用的文本指纹（去掉标点 / 引号 / 空白）。

    Patch v4.14：语气词 / 停顿标签**不参与**比较——同一句话加了个 ``(sighs)``
    仍然算重复，否则模型换个语气就能绕过去重。
    """
    return _DEDUPE_STRIP_RE.sub("", strip_minimax_tts_tags(coerce_str(text)))


def _beat_panel_ids(beat: dict[str, Any]) -> set[str]:
    """beat 的分镜 ID 集合（去重时判断两条是否落在同一画面）。"""
    return {item for item in coerce_str_list(beat.get("target_panel_ids")) if item}


def deduplicate_adjacent_beats(beats: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """相邻重复对白确定性去重（Patch v3.4 Patch 17）。

    相邻两条 Beat 命中以下任一条件时，丢弃**后一条**（保留信息量更完整 / 更早的那句）：

    1. 去掉标点 / 引号 / 空白后的 ``tts_text``（缺省退回 ``narration_text``）完全相同；
    2. 后一条的文本被前一条完全包含（后一条 ≥ 6 字），且两条共享同一组
       ``target_panel_ids``（同一画面里的重复啰嗦）。

    命中时打 INFO 日志，便于回溯到底是哪句被去掉。
    """
    kept: list[dict[str, Any]] = []
    for beat in beats:
        if not isinstance(beat, dict):
            continue
        if kept:
            previous = kept[-1]
            previous_key = _dedupe_text_key(
                previous.get("tts_text") or previous.get("narration_text") or ""
            )
            current_key = _dedupe_text_key(
                beat.get("tts_text") or beat.get("narration_text") or ""
            )
            if previous_key and current_key:
                identical = previous_key == current_key
                contained = (
                    not identical
                    and len(current_key) >= _DEDUPE_CONTAINED_MIN_CHARS
                    and current_key in previous_key
                    and bool(_beat_panel_ids(previous) & _beat_panel_ids(beat))
                )
                if identical or contained:
                    logger.info(
                        "[normalize] 相邻重复对白去重：丢弃 %s（与 %s 重复）",
                        coerce_str(beat.get("beat_id")) or "?",
                        coerce_str(previous.get("beat_id")) or "?",
                    )
                    continue
        kept.append(beat)
    return kept


#: 心理活动关键词（Patch v3.4 Patch 17）。
#:
#: 引号前的引导文本命中这些词时，引号里的是「心里话」而不是角色开口，
#: 保持旁白声线朗读，不切走角色声线。
PSYCHOLOGICAL_LEAD_KEYWORDS: tuple[str, ...] = (
    "心想",
    "暗想",
    "默念",
    "心里",
    "想到",
    "意识到",
    "回响",
    "脑海",
)

_PSYCHOLOGICAL_LEAD_RE = re.compile("|".join(PSYCHOLOGICAL_LEAD_KEYWORDS))

#: 引号内容少于该字数视为短促拟声 / 心理闪念（Patch v3.4 Patch 17）。
#:
#: 「嗯？」「……！」这类纯拟声没有真正的说话人，切成角色声线既突兀又多余。
MIN_DIALOGUE_CHARS = 6

#: 整段文本都是引号对白时的匹配规则。
_QUOTED_LINE_RE = re.compile(r"^“([^”]+)”$")

#: 分镜 ID 里的序号（``p_001`` → 1）。
_PANEL_INDEX_RE = re.compile(r"(\d+)")

#: 视为「旁白」的 ``speaker_role`` 取值（大小写无关）。
NARRATOR_ROLE_NAMES: frozenset[str] = frozenset({"narrator", "旁白", "解说"})


def quote_char_count(text: Any) -> int:
    """引号内容的净字数（去掉标点 / 引号 / 空白）。"""
    return len(_DEDUPE_STRIP_RE.sub("", coerce_str(text)))


def extract_quoted_text(text: Any) -> str | None:
    """整段文本是不是纯引号对白；是则返回引号内文本，否则返回 ``None``。"""
    stripped = coerce_str(text).strip()
    match = _QUOTED_LINE_RE.fullmatch(stripped)
    return match.group(1) if match else None


def first_panel_index(panel_ids: Any) -> int | None:
    """取分镜 ID 列表里第一个可解析的序号（``p_003`` → 3）。"""
    for panel_id in coerce_str_list(panel_ids):
        match = _PANEL_INDEX_RE.search(panel_id)
        if match:
            try:
                return int(match.group(1))
            except ValueError:  # pragma: no cover - 正则已保证是数字
                continue
    return None


def is_psychological_lead(text: Any) -> bool:
    """引号前的引导文本是不是心理活动描写。"""
    return bool(_PSYCHOLOGICAL_LEAD_RE.search(coerce_str(text)))


def resolve_dialogue_speaker_inheritance(
    beats: Sequence[Mapping[str, Any]],
    *,
    max_panel_gap: int = 1,
) -> dict[str, tuple[str, str]]:
    """连续对白说话人继承（Patch v3.4 Patch 17）。

    消除「男角色说一半突然退回旁白女声」：一条**没有写明「XX说：」**的纯引号
    对白（``speaker_role`` 仍是旁白，或性别未知），若与上一句已识别的对白同属
    连续对话序列（分镜间隔不超过 ``max_panel_gap`` 格、中间没有旁白打断），
    就继承上一句的说话人与性别。

    返回 ``{beat_id: (speaker_role, speaker_gender)}``，只包含需要继承的 beat；
    短促拟声 / 心理闪念（引号内不足 :data:`MIN_DIALOGUE_CHARS` 字）不继承——
    它们本来就留在旁白声线。
    """
    inheritance: dict[str, tuple[str, str]] = {}
    last_speaker = ""
    last_gender = ""
    last_panel_index: int | None = None

    for beat in beats:
        if not isinstance(beat, Mapping):
            continue
        quoted = extract_quoted_text(beat.get("narration_text"))
        role = coerce_str(beat.get("speaker_role"))
        gender = coerce_str(beat.get("speaker_gender")).lower()

        if quoted is None:
            # 纯旁白：打断连续对话序列。
            last_speaker = ""
            last_gender = ""
            last_panel_index = None
            continue

        if role and role.lower() not in NARRATOR_ROLE_NAMES:
            # 认出了具体说话人：记住他，供后面的纯引号对白继承。
            last_speaker = role
            last_gender = gender if gender in {"male", "female"} else ""
            last_panel_index = first_panel_index(beat.get("target_panel_ids"))
            continue

        # 纯引号对白但没写明说话人 → 尝试继承上一句对白。
        panel_index = first_panel_index(beat.get("target_panel_ids"))
        if (
            last_speaker
            and last_panel_index is not None
            and panel_index is not None
            and 0 <= panel_index - last_panel_index <= max_panel_gap
            and quote_char_count(quoted) >= MIN_DIALOGUE_CHARS
        ):
            beat_id = coerce_str(beat.get("beat_id"))
            if beat_id:
                inheritance[beat_id] = (last_speaker, last_gender or "unknown")
    return inheritance


def apply_dialogue_speaker_inheritance(
    beats: Sequence[dict[str, Any]],
    *,
    max_panel_gap: int = 1,
) -> list[dict[str, Any]]:
    """把 :func:`resolve_dialogue_speaker_inheritance` 的结果写回 beat。"""
    inheritance = resolve_dialogue_speaker_inheritance(
        beats, max_panel_gap=max_panel_gap
    )
    result: list[dict[str, Any]] = []
    for beat in beats:
        if not isinstance(beat, dict):
            continue
        updated = dict(beat)
        hit = inheritance.get(coerce_str(updated.get("beat_id")))
        if hit:
            updated["speaker_role"] = hit[0]
            updated["speaker_gender"] = hit[1]
        result.append(updated)
    return result


def normalize_script_payload(
    payload: dict[str, Any],
    *,
    episode_id: str,
    episode_no: int,
    valid_panel_ids: set[str] | None = None,
    canonicalizer: Any | None = None,
) -> dict[str, Any]:
    """把模型返回的剧本 JSON 归一化到 ``NarrationScript`` 契约。

    * ``beat_id`` 缺失时按序号补齐；
    * ``narration_text`` 长度必须落在契约区间，过短的 beat 直接丢弃；
    * ``target_panel_ids`` 过滤空值，并可按真实存在的面板 ID 过滤；
    * ``motion`` / 伏笔状态等枚举越界时收敛；
    * 相邻重复对白确定性去重（Patch v3.4 Patch 17）；
    * 话号与话次 ID 一律以流水线为准；
    * ``canonicalizer``（Patch v4.16）非空时，收尾再把角色名 / 解说词里的
      角色别名归并到规范名（类型标注见 :class:`core.character_unify.CharacterNameCanonicalizer`，
      这里用 ``Any`` 以避免与 character_unify 形成循环导入）。
    """
    beats: list[dict[str, Any]] = []
    raw_beats = coerce_list(payload.get("beats"))
    for index, item in enumerate(raw_beats, start=1):
        if not isinstance(item, dict):
            continue
        narration = coerce_str(item.get("narration_text") or item.get("text"))
        # Patch v4.14：字幕轨绝不能出现 (laughs) / <#0.5#> 这类送音标记。
        narration = strip_minimax_tts_tags(narration)
        # Patch v4.13：全链路统一简体，避免字幕 / 送音 / 视觉抽取繁简混排。
        narration = to_simplified_chinese(narration)
        # Patch v3.2 Patch 12：对白引号统一成中文双引号（拆对白的前置条件）。
        narration = normalize_dialogue_quotes(narration)
        # Patch v3.1 Patch 9：进字幕 / TTS 之前先净化，杜绝「☒」方块与吞字。
        narration = sanitize_narration_and_tts(narration)
        if len(narration) < MIN_NARRATION_CHARS:
            continue
        narration = clip_text(narration, MAX_NARRATION_CHARS)
        panel_ids = [
            panel_id
            for panel_id in coerce_str_list(item.get("target_panel_ids"))
            if valid_panel_ids is None or panel_id in valid_panel_ids
        ]
        if not panel_ids:
            continue
        tts_raw = item.get("tts_text")
        tts_text = coerce_str(tts_raw) if tts_raw is not None else None
        if tts_text is not None:
            # Patch v4.13：送音文本同样统一简体。
            tts_text = to_simplified_chinese(tts_text)
            tts_text = normalize_dialogue_quotes(tts_text)
            tts_text = sanitize_narration_and_tts(tts_text, for_tts=True)
            # Patch v4.14：停顿标签规范化（合并相邻、钳位秒数、剥除贴首贴尾）。
            tts_text = normalize_minimax_pause_tags(tts_text)
            # 标签字符不计入长度额度，且绝不从标签中间腰斩。
            tts_text = clip_text_by_spoken_length(tts_text, MAX_NARRATION_CHARS)
            # 截断可能把停顿标签留在句尾（变成孤立停顿），兜底再规范化一次。
            tts_text = normalize_minimax_pause_tags(tts_text) or None
        # Patch v3.1 Patch 10：可选音效标签（白名单收敛，非法值 / "none" 丢弃）。
        # Patch v4.38：先过一遍**同义别名**表（只收敛明确同义写法），再查白名单。
        # Patch v4.43：白名单换成**动态生效曲库**（内置 24 条 + 用户自定义）。
        sfx_cue_raw = item.get("sfx_cue")
        sfx_cue = coerce_str(sfx_cue_raw).lower() if sfx_cue_raw is not None else ""
        sfx_cue = SFX_CUE_ALIASES.get(sfx_cue, sfx_cue)
        if not is_valid_sfx_key(sfx_cue):
            sfx_cue = None
        # Patch v4.40：音效在本句内的落点（start / mid / end），非法值回落到 start。
        sfx_position = coerce_str(item.get("sfx_position")).strip().lower()
        if sfx_position not in SFX_POSITIONS:
            sfx_position = "start"
        # Patch v4.43：AI 音效建议（方案 B 的建议栏数据源）。
        sfx_suggestions = normalize_sfx_suggestions(
            item.get("sfx_suggestions"),
            sfx_cue=sfx_cue,
        )
        # Patch v3.2 Patch 12：说话人性别（male / female / unknown）。
        speaker_gender = coerce_str(item.get("speaker_gender")).lower()
        if speaker_gender not in SPEAKER_GENDERS:
            speaker_gender = "unknown"
        # Patch v4.13：单句语速微调（模型只在明显节奏 / 情绪变化点给）。
        speed_override = coerce_float(item.get("speed_override"))
        if speed_override is not None:
            speed_override = min(
                MAX_SPEED_OVERRIDE, max(MIN_SPEED_OVERRIDE, speed_override)
            )
        # Patch v4.13：本句归属的镜头组（幕）；缺省为空，由上层按分镜回退。
        scene_id = coerce_str(item.get("scene_id")).strip() or None
        beats.append(
            {
                "beat_id": coerce_str(item.get("beat_id")) or f"b{index}",
                "narration_text": narration,
                "tts_text": tts_text,
                "emotion_instruction": coerce_str(item.get("emotion_instruction"))
                or "用自然、流畅且有代入感的漫评解说语气说出",
                "speaker_role": coerce_str(item.get("speaker_role")) or "narrator",
                "speaker_gender": speaker_gender,
                "target_panel_ids": panel_ids,
                "motion": normalize_enum(item.get("motion"), MOTION_VALUES, "zoom_in"),
                "pause_after_ms": coerce_int(item.get("pause_after_ms"), 150, low=0, high=2000),
                "sfx_cue": sfx_cue,
                # Patch v4.40：音效落点。
                "sfx_position": sfx_position,
                # Patch v4.43：AI 音效建议（最多 3 条）。
                "sfx_suggestions": sfx_suggestions,
                "speed_override": speed_override,
                "scene_id": scene_id,
            }
        )

    digest_raw = payload.get("episode_digest")
    digest: dict[str, Any] = {
        "episode_no": episode_no,
        "one_line_hook": "",
        "summary": "",
        "keywords": [],
        "characters_present": [],
    }
    if isinstance(digest_raw, dict):
        digest["one_line_hook"] = coerce_str(digest_raw.get("one_line_hook"))
        digest["summary"] = coerce_str(digest_raw.get("summary"))
        digest["keywords"] = coerce_str_list(digest_raw.get("keywords"))
        digest["characters_present"] = coerce_str_list(digest_raw.get("characters_present"))

    character_updates: list[dict[str, Any]] = []
    for item in coerce_list(payload.get("character_updates")):
        if not isinstance(item, dict):
            continue
        name = coerce_str(item.get("name"))
        if not name:
            continue
        first_seen = coerce_int(item.get("first_seen_ep"), episode_no, low=1)
        last_seen = coerce_int(item.get("last_seen_ep"), episode_no, low=1)
        gender = coerce_str(item.get("gender")).lower()
        if gender not in SPEAKER_GENDERS:
            gender = "unknown"
        character_updates.append(
            {
                "name": name,
                "aliases": coerce_str_list(item.get("aliases")),
                "visual_traits": coerce_str(item.get("visual_traits")),
                "current_status": coerce_str(item.get("current_status")),
                "first_seen_ep": min(first_seen, last_seen),
                "last_seen_ep": max(first_seen, last_seen),
                # Patch v3.2 Patch 12：角色性别（角色表靠它做男女声路由）。
                "gender": gender,
            }
        )

    new_threads: list[dict[str, Any]] = []
    for index, item in enumerate(coerce_list(payload.get("new_plot_threads")), start=1):
        if not isinstance(item, dict):
            continue
        title = coerce_str(item.get("title"))
        if not title:
            continue
        status = normalize_enum(item.get("status"), PLOT_THREAD_STATUS, "open")
        resolved_ep = item.get("resolved_ep")
        new_threads.append(
            {
                "thread_id": coerce_str(item.get("thread_id")) or f"t{index}",
                "title": title,
                "description": coerce_str(item.get("description")),
                "introduced_ep": coerce_int(item.get("introduced_ep"), episode_no, low=1),
                "status": status,
                "resolved_ep": coerce_int(resolved_ep, 0, low=1) if status == "resolved" and resolved_ep else None,
            }
        )

    result = {
        "episode_id": episode_id,
        "hook_title": coerce_str(payload.get("hook_title")) or f"第 {episode_no} 话",
        # Patch v3.4 Patch 17：相邻重复对白确定性去重（模型偶尔把同一句写两遍）。
        "beats": deduplicate_adjacent_beats(beats),
        "episode_digest": digest,
        "character_updates": character_updates,
        "new_plot_threads": new_threads,
        "resolved_thread_ids": coerce_str_list(payload.get("resolved_thread_ids")),
    }

    # Patch v4.16：角色名全链路统一。Stage 4 可能把同一个角色写成「恩慧」「恩惠」，
    # 这里用 canonicalizer 把角色名、解说词与摘要里的别名归并到规范名。
    # 注意 ``canonicalize_script_dict`` 返回的是**新字典**（不原地改），必须接住返回值。
    if canonicalizer is not None:
        result = canonicalizer.canonicalize_script_dict(result)
    return result


def normalize_panel_payload(payload: dict[str, Any], *, panel_id: str) -> dict[str, Any]:
    """把模型返回的面板 JSON 归一化到 ``PanelVisionInfo`` 契约。

    模型给的 ``emotion`` 可能越界（真实调用出现过 ``pleading``），对白列表可能
    是字符串，坐标可能给 3 个数——这里逐字段收敛，契约本身不动。
    """
    dialogues: list[dict[str, Any]] = []
    for item in coerce_list(payload.get("dialogues")):
        if isinstance(item, dict):
            entry = item
        elif isinstance(item, str):
            entry = {"text": item}
        else:
            continue
        text = coerce_str(entry.get("text"))
        if not text:
            continue
        dialogues.append(
            {
                "speaker_guess": coerce_str(entry.get("speaker_guess") or entry.get("speaker")),
                "text": text,
                "emotion": normalize_enum(entry.get("emotion"), EMOTION_VALUES, "calm"),
            }
        )

    return {
        "panel_id": panel_id,
        "scene_summary": coerce_str(payload.get("scene_summary") or payload.get("summary")),
        "dialogues": dialogues,
        "sfx_list": coerce_str_list(payload.get("sfx_list") or payload.get("sfx")),
        "focus_region": coerce_floats(payload.get("focus_region"), 4),
        "is_transition_blank": coerce_bool(payload.get("is_transition_blank")),
        # Patch v3.1 Patch 10：纯字卡标记（只有文字框、没有人脸/肢体动作）。
        "is_pure_text_card": coerce_bool(payload.get("is_pure_text_card")),
    }
