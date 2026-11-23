"""FastAPI 控制台后端（规范第 5 节）。

对外暴露规范第 5 节列出的全部端点，并与根目录已定稿的前端原型（``src/App.tsx``）
保持字段级一致：

===========================  ==========================================
端点                        功能
===========================  ==========================================
``GET/POST /api/series``    列出书架 / 导入新漫画并运行 Stage 0
``GET /api/series/{sid}``   目录 + StoryBible + 各话 manifest 状态
``PUT /api/series/{sid}/memory``  人工编辑角色图谱 / 伏笔 / 叙事基调
``POST /api/jobs/batch``    按作品下发并发任务（跨作品并发，同作品 Stage 4 串行加锁）
``POST /api/episodes/{eid}/rerun`` 从指定阶段断点重跑
``PUT /api/episodes/{eid}/script`` 保存人工修改的 beats（可选从 Stage 5 重跑）
``PATCH /api/episodes/{eid}/panels/{pid}``  标记 / 取消 ``is_transition_blank``
``GET  /api/episodes/{eid}/panels/{file}``   分镜原图回源（Storyboard 缩略图）
``GET/PUT /api/episodes/{eid}/storyboard``   镜头编排读取 / 人工保存（Patch 20）
``POST /api/episodes/{eid}/storyboard/reset``    恢复自动分组
``POST /api/episodes/{eid}/storyboard/generate`` 按镜头组重跑 S4 / 一键制片
``GET /api/episodes/{eid}/events``            SSE 实时推送阶段状态与日志
``GET /api/episodes/{eid}/export``            下载成片 / 外挂字幕 / 文稿
``GET /api/series/{sid}/export-zip``          批量打包成片为 ZIP
``GET/PUT /api/config``                       全局配置读写
``GET/PUT /api/pronunciation-rules``          多音字词典读写（全局 + 作品级）
===========================  ==========================================
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sys
import tempfile
import time
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Iterator, Literal
from urllib.parse import quote

import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field, ValidationError
from sse_starlette.sse import EventSourceResponse

from core.branding_render import (
    BRANDING_SLOTS,
    EpisodeRenderOptions,
    decode_base64_image,
    load_episode_render_options,
    render_cover_image,
    save_branding_image,
    save_episode_render_options,
)
from core.config import (
    AppConfig,
    ConfigError,
    SERIES_CONFIG_FILENAME,
    SeriesBrandingConfig,
    SeriesTTSConfig,
    WorkspaceConfig,
    assets_root,
    find_config_path,
    load_config,
    load_env_file,
    load_series_config,
    merge_pronunciation_overrides,
    merge_series_tts,
    merge_voice_cast,
    recommended_voice_for_tts_model,
    resolve_voice_for_tts_model,
)
from core.subtitle_render import FONT_CANDIDATES
from core.manifest import (
    ARCHIVE_INTERMEDIATE_DIRS,
    ManifestManager,
    dir_size,
    infer_stage_artifacts,
    infer_stage_status,
)
from core.panel_ids import panel_sort_key
from core.normalize import to_simplified_chinese
from core.schemas import (
    ArcSummary,
    CharacterProfile,
    DialogueBubble,
    EpisodeStoryboard,
    NarrationScript,
    PlotThread,
    ScriptBeat,
    SeriesCatalog,
    StageStatus,
    StoryBibleV2,
    StoryboardScene,
    VisionWindowOutput,
    load_model,
    save_model,
)
from providers.tts_minimax import (
    MINIMAX_FEMALE_VOICE_IDS,
    MINIMAX_MALE_VOICE_IDS,
    MINIMAX_NARRATOR_VOICE_IDS,
    MINIMAX_VOICES,
    voice_sample_filename as minimax_voice_sample_filename,
)
from providers.tts_qwen import (
    DEFAULT_VOICE,
    QWEN3_TTS_VOICES,
    voice_sample_filename,
)
from stages import s0_catalog, s3b_scenes, s5_tts, s6_timeline, s7_renderer
from web.job_runner import STAGE_DEFS, STAGE_IDS, EpisodeState, JobError, JobRunner

#: 面板图片后缀。
PANEL_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})

#: 前端 ``StudioConfig`` 的默认音色（对应 ``src/data/mockStudioData.ts``）。
#: Patch v3.1 Patch 11 A1：直接取 provider 的系统默认音色（``Elias``），不再写死旧值。
DEFAULT_TTS_VOICE = DEFAULT_VOICE
DEFAULT_MAX_IMAGE_LONG_EDGE = 1280

#: 可选音色清单（前端下拉展示用）：全量 34 个官方音色的精简条目。
#: Patch v3.2 Patch 13 C2：百炼官方 13 个音色；Patch v3.3 Patch 15：扩充至 34 个
#: （含方言/戏剧音色），目录外官方 ID 仍可手填透传，只是没有静态试听样例。
TTS_VOICE_OPTIONS: tuple[dict[str, str], ...] = tuple(
    {
        "id": str(item["voice_id"]),
        "label": f"{item['display_name']}（{item['description']}）",
    }
    for item in QWEN3_TTS_VOICES
)

#: 试听样例目录名（相对 ``backend/assets/``）。
VOICE_SAMPLES_DIR_NAME = "voice_samples"

#: Patch v3.4 Patch 16：试听样例按供应商分目录（``voice_samples/{provider}/``），
#: 避免百炼与 MiniMax 的同名文件互相覆盖。
VOICE_SAMPLE_PROVIDERS: tuple[str, ...] = ("qwen", "minimax")

#: Patch v3.4 Patch 16：TTS 模型下拉（百炼 / MiniMax / Edge 三套 provider）。
TTS_MODEL_OPTIONS: tuple[str, ...] = (
    "qwen3-tts-flash",
    "qwen-audio-3.1-tts-plus",
    "speech-2.8-hd",
    "speech-2.8-turbo",
    "edge-tts",
)

#: Stage 4 模型下拉推荐项（Patch v3.3 Patch 15：新增 qwen4-max / qwen3.7-plus）。
#: Patch v4.15：新增 deepseek-v4.1-flash-1017（百炼 · DeepSeek 高性价比），
#: 需先在百炼控制台开通该模型的调用权限，未开通时按 MODEL_FALLBACK_CHAIN 降级。
STORY_MODEL_OPTIONS: tuple[str, ...] = (
    "qwen4-max",
    "qwen3.8-max",
    "qwen3.7-plus",
    "qwen-plus",
    "deepseek-v4.1-flash-1017",
)
SCRIPT_MODEL_OPTIONS: tuple[str, ...] = (
    "qwen3.7-plus",
    "qwen-plus",
    "deepseek-v4.1-flash-1017",
    "qwen3.8-max",
)

#: Patch v4.22：识图（视觉抽取）模型下拉。
#: qwen3.7-plus 原生多图 + 长上下文，Stage 3 走批窗抽取（主力推荐）；
#: qwen-vl-max 密集小字 / 气泡归属更强但百炼已下线（404），保留备查；
#: qwen3-vl-plus 经典视觉线（单图模型，逐格抽取）；
#: deepseek-v4.1-flash-1017 百炼多模态，实测带图返回空内容（对比测试用）。
VISION_MODEL_OPTIONS: tuple[str, ...] = (
    "qwen3.7-plus",
    "qwen-vl-max",
    "qwen3-vl-plus",
    "deepseek-v4.1-flash-1017",
)


def voice_sample_dir() -> Path:
    """试听样例目录：``backend/assets/voice_samples/``。"""
    return assets_root() / VOICE_SAMPLES_DIR_NAME


def _safe_provider_dir(provider: str) -> str:
    """供应商目录名安全化（只允许字母/数字/下划线/连字符）。"""
    return re.sub(r"[^A-Za-z0-9_\-]+", "", (provider or "").strip())


def provider_voice_sample_dir(provider: str) -> Path:
    """某供应商的样例目录：``backend/assets/voice_samples/{provider}/``。"""
    return voice_sample_dir() / _safe_provider_dir(provider)


def voice_sample_path(voice_id: str) -> Path | None:
    """某个音色的试听样例路径。

    Patch v3.3 Patch 15：目录外的百炼官方音色 ID 也允许手填使用，因此不再以
    音色表枚举拒收；统一经 :func:`voice_sample_filename` 安全化，含空格的
    ``Eldric Sage`` 与下划线写法 ``Eldric_Sage`` 解析到同一文件。
    """
    cleaned = voice_id.strip()
    if not cleaned:
        return None
    return voice_sample_dir() / voice_sample_filename(cleaned)


def minimax_voice_sample_path(voice_id: str) -> Path | None:
    """MiniMax 音色的试听样例路径（``voice_samples/minimax/``）。"""
    cleaned = voice_id.strip()
    if not cleaned:
        return None
    return provider_voice_sample_dir("minimax") / minimax_voice_sample_filename(cleaned)


def provider_voice_sample_candidates(provider: str, voice_id: str) -> list[Path]:
    """某供应商音色的样例候选路径：供应商目录优先，回退旧的扁平目录。"""
    cleaned = voice_id.strip()
    name = _safe_provider_dir(provider)
    if not cleaned or not name:
        return []
    filename = (
        minimax_voice_sample_filename(cleaned)
        if name == "minimax"
        else voice_sample_filename(cleaned)
    )
    return [provider_voice_sample_dir(name) / filename, voice_sample_dir() / filename]


def provider_voice_sample_path(provider: str, voice_id: str) -> Path | None:
    """按供应商解析样例路径（供应商目录优先）。"""
    candidates = provider_voice_sample_candidates(provider, voice_id)
    return candidates[0] if candidates else None


def _sample_ready(*candidates: Path | None) -> bool:
    """任一候选样例文件存在即视为就绪。"""
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return True
    return False


def voice_options_payload() -> list[dict[str, Any]]:
    """音色清单（前端分组下拉 + 试听按钮用）。"""
    payload: list[dict[str, Any]] = []
    for item in QWEN3_TTS_VOICES:
        voice_id = str(item["voice_id"])
        payload.append(
            {
                # 规范字段（Patch v3.3 结构）
                "voice_id": voice_id,
                "display_name": str(item["display_name"]),
                "gender": str(item["gender"]),
                "age_group": str(item["age_group"]),
                "category": str(item["category"]),
                "description": str(item["description"]),
                # Patch v3.4 Patch 16：供应商标记，前端据此分组，避免把两套
                # provider 的音色混进同一个 optgroup。
                "provider": "qwen",
                # 兼容别名（前端既有字段名，避免重复口径）
                "id": voice_id,
                "name": str(item["display_name"]),
                "label": f"{item['display_name']}（{item['description']}）",
                # Patch v3.3 Patch 15：带空格的音色 ID 做 URL 编码（Eldric Sage →
                # /api/tts/samples/Eldric%20Sage），路由侧再安全化回 Eldric_Sage.wav。
                "sample_url": f"/api/tts/samples/{quote(voice_id, safe='')}",
                # Patch v3.4 Patch 16：供应商目录优先，旧扁平目录仍兼容。
                "sample_ready": _sample_ready(
                    provider_voice_sample_path("qwen", voice_id),
                    voice_sample_path(voice_id),
                ),
            }
        )
    return payload


def _minimax_voice_category(voice_id: str) -> str:
    """MiniMax 音色归类（前端按类分组展示）。

    narrator=推荐说书旁白、male=男角色、female=女角色。
    """
    if voice_id in MINIMAX_NARRATOR_VOICE_IDS:
        return "narrator"
    if voice_id in MINIMAX_MALE_VOICE_IDS:
        return "male"
    if voice_id in MINIMAX_FEMALE_VOICE_IDS:
        return "female"
    return "other"


def minimax_voice_options_payload() -> list[dict[str, Any]]:
    """MiniMax 音色清单（Patch v3.4 Patch 16，28 条）。

    与百炼清单分开暴露，避免前端把两套 provider 的音色混在一起
    （百炼音色发给 MiniMax 会直接 2013 invalid voice_id）。
    """
    payload: list[dict[str, Any]] = []
    for item in MINIMAX_VOICES:
        voice_id = str(item["id"])
        payload.append(
            {
                "voice_id": voice_id,
                "display_name": str(item["name"]),
                "gender": str(item["gender"]),
                "style": str(item["style"]),
                "recommended_for": str(item["recommended_for"]),
                # Patch v3.4 Patch 16：与百炼条目同构的分类/描述字段，
                # 前端一套渲染逻辑即可同时渲染两套清单。
                "category": _minimax_voice_category(voice_id),
                "age_group": "adult",
                "description": f"{item['style']}，{item['recommended_for']}",
                "provider": "minimax",
                # 兼容别名（与百炼条目同构，前端可复用一套渲染逻辑）
                "id": voice_id,
                "name": str(item["name"]),
                "label": f"{item['name']}（{item['style']}）",
                "sample_url": f"/api/tts/samples/minimax/{quote(voice_id, safe='')}",
                "sample_ready": _sample_ready(
                    provider_voice_sample_path("minimax", voice_id),
                    minimax_voice_sample_path(voice_id),
                ),
            }
        )
    return payload


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class SeriesCreateRequest(BaseModel):
    """``POST /api/series`` 请求体。"""

    url: str
    title: str = ""


class MemoryUpdateRequest(BaseModel):
    """``PUT /api/series/{series_id}/memory`` 请求体。"""

    narration_style: str | None = None
    characters: list[dict[str, Any]] | None = None
    plot_threads: list[dict[str, Any]] | None = None
    arc_summaries: list[dict[str, Any]] | None = None


class JobBatchRequest(BaseModel):
    """``POST /api/jobs/batch`` 请求体。"""

    items: list[dict[str, Any]] = Field(default_factory=list)
    mode: str = "phase1_only"


class RerunRequest(BaseModel):
    """``POST /api/episodes/{ep_id}/rerun`` 请求体。"""

    from_stage: str = "s1_crawler"
    mode: str = "phase1_only"


class EpisodeResetRequest(BaseModel):
    """``POST /api/episodes/{ep_id}/reset`` 请求体（Patch v4.1 Patch 21）。"""

    from_stage: str
    include_human_storyboard: bool = False


class BeatRegenerateTTSRequest(BaseModel):
    """``POST /api/episodes/{eid}/beats/{bid}/regenerate-tts`` 请求体（Patch v4.12）。"""

    #: 当前编辑的单条 beat（可选）：给出时先按 PUT /script 同款合并策略落盘再重配。
    beat: dict[str, Any] | None = None
    #: 重配成功后是否顺带从 S6 提交整条重跑出片任务。
    rerun_from_s6: bool = False


class ScriptUpdateRequest(BaseModel):
    """``PUT /api/episodes/{ep_id}/script`` 请求体。"""

    beats: list[dict[str, Any]] = Field(default_factory=list)
    from_stage: str | None = None
    mode: str = "full_video"
    #: Patch v4.23：人工保存**默认不**强制恢复角色名（用户改的写法原样落盘）；
    #: 只有显式打开这个开关才按规范名归一。
    auto_unify_characters: bool = False
    #: Patch v4.25：用户勾选的「角色档案同步」动作。**默认为空 = 绝不动档案**。
    #: 每项形如 ``{"action": "add_alias"|"promote_canonical"|"register_character",
    #: "found_name": "南道振", "target_canonical": "道振"}``。
    sync_character_updates: list[dict[str, Any]] = Field(default_factory=list)


class CharacterCheckRequest(BaseModel):
    """``POST /api/episodes/{ep_id}/script/check-characters`` 请求体（Patch v4.23）。"""

    #: 待检查的草稿句子；缺省时检查已落盘的 ``script.json``。
    beats: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# Patch v4.0 Patch 20：镜头编排（Storyboard）接口
# ---------------------------------------------------------------------------
class StoryboardUpdateRequest(BaseModel):
    """``PUT /api/episodes/{ep_id}/storyboard`` 请求体。"""

    scenes: list[dict[str, Any]] = Field(default_factory=list)


class StoryboardGenerateRequest(BaseModel):
    """``POST /api/episodes/{ep_id}/storyboard/generate`` 请求体。"""

    scenes: list[dict[str, Any]] | None = None
    mode: str = "phase1_only"


#: 镜头组重生成支持的任务模式。
STORYBOARD_MODES: frozenset[str] = frozenset({"phase1_only", "full_video"})

#: 面板图片后缀 → media type（``/panels/{filename}`` 静态回源用）。
PANEL_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

#: 缩略图 JPEG 质量（Patch v4.25）。
PANEL_THUMBNAIL_QUALITY = 82


def _panel_thumbnail(
    source: Path, width: int
) -> tuple[Path, str]:
    """按宽度生成（并缓存）面板缩略图（Patch v4.25）。

    一话 200~300 个分镜时，前端把 690px 宽的原图整张拉进列表会瞬间吃掉
    几十兆流量与解码时间。这里按需缩到指定宽度（**只缩不放**），
    缓存到 ``02_panels/.thumbs/{stem}_w{width}.jpg``。

    缓存失效条件：目标不存在、或原图 mtime 比缓存新（重切图后原图会更新），
    或缓存文件读不出来。

    返回 ``(图片路径, media_type)``。原图本身已经比目标宽度窄时直接返回原图。
    """
    suffix = source.suffix.lower()
    media_type = PANEL_MEDIA_TYPES.get(suffix, "application/octet-stream")
    try:
        from providers.image_utils import load_image

        with load_image(source) as image:
            origin_width = int(image.width)
            if origin_width <= width:
                # 只缩不放：原图已经够窄，直接回原图。
                return source, media_type
            target_height = max(1, round(int(image.height) * width / origin_width))
            thumbs_dir = source.parent / ".thumbs"
            target = thumbs_dir / f"{source.stem}_w{width}.jpg"
            stale = True
            if target.is_file():
                try:
                    stale = target.stat().st_mtime < source.stat().st_mtime
                except OSError:
                    stale = True
            if stale:
                thumbs_dir.mkdir(parents=True, exist_ok=True)
                resized = image.resize((width, target_height), Image.LANCZOS)
                resized.save(target, format="JPEG", quality=PANEL_THUMBNAIL_QUALITY)
            return target, "image/jpeg"
    except Exception as exc:  # 缩略图失败不能挡住原图回源
        print(f"[web] 缩略图生成失败，回退原图：{source}（{exc}）", file=sys.stderr)
        return source, media_type


def _load_episode_vision(episode_dir: Path) -> VisionWindowOutput | None:
    """读取一话的 Stage 3 视觉结果；缺失或损坏时返回 ``None``。"""
    vision_path = episode_dir / "03_vision" / "vision.json"
    if not vision_path.is_file():
        return None
    try:
        return load_model(VisionWindowOutput, vision_path)
    except Exception:  # noqa: BLE001 - 视觉结果损坏按「尚未识图」处理
        return None


def _validate_storyboard_scenes(
    raw_scenes: list[dict[str, Any]],
    valid_panel_ids: set[str],
    *,
    existing_scenes: Sequence[StoryboardScene] | None = None,
) -> list[StoryboardScene]:
    """校验前端提交的镜头组并构造 ``StoryboardScene`` 列表。

    规则（违例一律抛 ``400``，不交给 pydantic 的 422）：

    * 至少保留一幕，每幕 ``panel_ids`` 非空；
    * 同一分镜不允许跨幕重复引用；
    * 所有分镜必须属于本话；
    * 幕内分镜按 :func:`panel_sort_key` 单调不减排列；
    * 跨幕顺序：后一幕的起始分镜必须排在上一幕末尾分镜之后
      （Patch v4.0 Patch 20.1），否则按剧情时间倒序了。

    Patch v4.7：请求体缺 ``bgm*`` 键时按 ``scene_id`` 从 ``existing_scenes``
    继承配乐四字段，防止其它面板保存时冲掉已配置的幕级 BGM。
    """
    if not raw_scenes:
        raise HTTPException(status_code=400, detail="至少保留一个镜头组")
    existing_by_id = {scene.scene_id: scene for scene in (existing_scenes or [])}
    seen: set[str] = set()
    scenes: list[StoryboardScene] = []
    previous_last_key: tuple[str, int, str] | None = None
    for index, raw in enumerate(raw_scenes):
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail=f"第 {index + 1} 幕格式不正确")
        scene_id = str(raw.get("scene_id") or f"sc_{index + 1:03d}")
        existing = existing_by_id.get(scene_id)
        bgm_mood = (
            str(raw["bgm_mood"])
            if raw.get("bgm_mood") is not None
            else (existing.bgm_mood if existing else "")
        )
        bgm_suggestion = (
            str(raw["bgm_suggestion"])
            if raw.get("bgm_suggestion") is not None
            else (existing.bgm_suggestion if existing else "")
        )
        # bgm / bgm_gain_db 的 None 是合法值（自动选择 / 默认增益），
        # 只能用「键是否存在」区分显式提交与缺键继承。
        bgm_value: str | None
        if "bgm" in raw:
            bgm_value = None if raw["bgm"] is None else str(raw["bgm"])
        else:
            bgm_value = existing.bgm if existing else None
        if "bgm_gain_db" in raw and raw["bgm_gain_db"] is not None:
            try:
                bgm_gain_db = float(raw["bgm_gain_db"])
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"第 {index + 1} 幕的 bgm_gain_db 必须是数字",
                ) from exc
            if not -30.0 <= bgm_gain_db <= 6.0:
                raise HTTPException(
                    status_code=400,
                    detail=f"第 {index + 1} 幕的 bgm_gain_db 必须在 -30~6 dB 之间",
                )
        else:
            bgm_gain_db = existing.bgm_gain_db if existing else None
        panel_ids = [str(pid).strip() for pid in (raw.get("panel_ids") or [])]
        panel_ids = [pid for pid in panel_ids if pid]
        if not panel_ids:
            raise HTTPException(
                status_code=400, detail=f"第 {index + 1} 幕的 panel_ids 不能为空"
            )
        duplicate = next((pid for pid in panel_ids if pid in seen), "")
        if duplicate:
            raise HTTPException(
                status_code=400,
                detail=f"分镜 {duplicate} 被多个镜头组重复引用，请先拆分或合并",
            )
        unknown = next((pid for pid in panel_ids if pid not in valid_panel_ids), "")
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"分镜 {unknown} 不属于本话有效分镜"
            )
        ordered = [panel_sort_key(pid) for pid in panel_ids]
        if any(ordered[i] > ordered[i + 1] for i in range(len(ordered) - 1)):
            raise HTTPException(
                status_code=400,
                detail=f"第 {index + 1} 幕的分镜顺序必须单调不减",
            )
        if previous_last_key is not None and ordered[0] <= previous_last_key:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"第 {index + 1} 幕与前一幕分镜顺序交叉，"
                    "请按剧情时间顺序排列镜头组"
                ),
            )
        previous_last_key = ordered[-1]
        seen.update(panel_ids)
        scenes.append(
            StoryboardScene(
                # 与 s3b_scenes 约定对齐：scene_index 1-based、scene_id 前缀 sc_。
                scene_id=scene_id,
                scene_index=index + 1,
                title=str(raw.get("title") or ""),
                location=str(raw.get("location") or ""),
                panel_ids=panel_ids,
                enabled=bool(raw.get("enabled", True)),
                must_cover=bool(raw.get("must_cover", True)),
                user_note=str(raw.get("user_note") or ""),
                locked_quotes=[
                    str(item) for item in (raw.get("locked_quotes") or []) if str(item)
                ],
                visual_recap=str(raw.get("visual_recap") or ""),
                bgm_mood=bgm_mood,
                bgm_suggestion=bgm_suggestion,
                bgm=bgm_value,
                bgm_gain_db=bgm_gain_db,
            )
        )
    return scenes


#: 前端保存脚本时不会回传这些字段（由 Stage 4 产出、前端只读展示）。
#: 客户端未提交时按 ``beat_id`` 从旧剧本继承，避免人工改一句字幕就把角色与音效标注抹掉。
PRESERVED_BEAT_FIELDS: tuple[str, ...] = (
    "speaker_role",
    "sfx_cue",
    "focus_hint",
    # Patch v3.2 Patch 12：说话人性别（前端还没回传该字段，别被前端保存抹掉）
    "speaker_gender",
    # Patch v4.2：逐句指定音色（前端未回传时继承旧值；回传空串 = 改回「跟随角色」）
    "voice_override",
    # Patch v4.4：单句语速微调（前端未回传时继承旧值；显式传 null = 清空，恢复角色默认语速）
    "speed_override",
    # Patch v4.13：解说句归属的镜头组（幕）；前端未回传时继承旧值，显式传 null = 解除归属
    "scene_id",
)

#: 上面这些字段里允许显式置 ``None``（清空）的，其余字段 ``None`` 非法，按「未提供」处理。
NULLABLE_BEAT_FIELDS: frozenset[str] = frozenset(
    {"sfx_cue", "speed_override", "scene_id"}
)

_MISSING = object()


def _merge_script_beats(
    existing: list[ScriptBeat], incoming: list[dict[str, Any]]
) -> list[ScriptBeat]:
    """把前端提交的 beats 合并进旧剧本，保留客户端未回传的只读字段。

    规则：请求体里**缺少**该键 → 继承旧值；显式传 ``null`` 且该字段允许为空 → 清空；
    显式传 ``null`` 但字段不允许为空（如 ``focus_hint``）→ 当作未提供并继承旧值，
    免得整个请求因为一个非法值被 422 拒掉。
    """
    by_id = {beat.beat_id: beat for beat in existing}
    merged: list[ScriptBeat] = []
    for item in incoming:
        raw = dict(item)
        old = by_id.get(str(raw.get("beat_id") or ""))
        if old is not None:
            for field in PRESERVED_BEAT_FIELDS:
                value = raw.get(field, _MISSING)
                if value is _MISSING or (
                    value is None and field not in NULLABLE_BEAT_FIELDS
                ):
                    raw[field] = getattr(old, field)
        merged.append(ScriptBeat.model_validate(raw))
    return merged


def _to_simplified_beat(beat: ScriptBeat) -> ScriptBeat:
    """把一句解说转成简体中文（Patch v4.13 全链路强制简体）。

    只动 ``narration_text``（屏幕字幕）与 ``tts_text``（送入 TTS 的文本）；
    语气词/停顿标签是 ASCII，转换器会原样透传，不会被腰斩。
    """
    updates: dict[str, Any] = {}
    narration = to_simplified_chinese(beat.narration_text)
    if narration != beat.narration_text:
        updates["narration_text"] = narration
    if beat.tts_text:
        tts_text = to_simplified_chinese(beat.tts_text)
        if tts_text != beat.tts_text:
            updates["tts_text"] = tts_text
    if not updates:
        return beat
    return beat.model_copy(update=updates)


def _episode_canonicalizer(
    series_dir: Path,
    beats: Sequence[ScriptBeat],
    *,
    include_role_names_as_candidates: bool = True,
) -> Any:
    """构建本话的角色名归一器（Patch v4.23：归一与只读检查共用一套口径）。

    规范名优先级：作品级专属音色 > 已有 StoryBible > 本话出现频次更高者。

    ``include_role_names_as_candidates=False``（**只读检查**用）：不把用户
    自己写的说话人名字当成新候选名——否则「不在作品角色表里」这件事永远
    判不出来（用户写什么，什么就成了规范名）。同时把角色名并进正文池，
    让「恩慧」这类同音写法依然能被认出来。
    """
    from stages.s4_scriptwriter import build_episode_canonicalizer

    characters = s5_tts.load_bible_characters(series_dir)
    roles = [beat.speaker_role for beat in beats if beat.speaker_role]
    texts = [
        text
        for beat in beats
        for text in (beat.narration_text, beat.tts_text or "")
        if text
    ]
    if include_role_names_as_candidates:
        extra_names: list[str] = list(roles)
    else:
        extra_names = []
        texts = texts + list(roles)
    return build_episode_canonicalizer(
        series_dir,
        characters,
        extra_names=extra_names,
        extra_texts=texts,
    )


def _canonicalize_script_beats(
    series_dir: Path, beats: list[ScriptBeat]
) -> list[ScriptBeat]:
    """统一一批句子的角色名写法（Patch v4.16）。

    * ``speaker_role`` 走角色位归一（全名 / 简称 / 同音变体 → 规范角色名）；
    * ``narration_text`` / ``tts_text`` 走正文归一（只替换确知的角色名变体，
      外貌称呼与「小恩小惠」这类惯用词不受影响）。

    任何异常都不应阻断人工保存，因此失败时原样返回。
    """
    if not beats:
        return beats
    try:
        canonicalizer = _episode_canonicalizer(series_dir, beats)
        payload = canonicalizer.canonicalize_script_dict(
            {"beats": [beat.model_dump(mode="json") for beat in beats]}
        )
        raw_beats = payload.get("beats") or []
        if len(raw_beats) != len(beats):
            return beats
        return [ScriptBeat.model_validate(raw) for raw in raw_beats]
    except Exception as exc:  # noqa: BLE001 - 角色名归一失败不应阻断保存
        print(f"[server] 角色名归一失败，保留原文：{exc}", file=sys.stderr)
        return beats


def _inspect_character_warnings(
    series_dir: Path, beats: Sequence[ScriptBeat]
) -> list[dict[str, Any]]:
    """只读检查一批句子的角色名写法（Patch v4.23）。

    **不改写任何文本**，只返回结构化告警。检查失败不阻断保存（返回空列表）。
    """
    if not beats:
        return []
    try:
        canonicalizer = _episode_canonicalizer(
            series_dir, beats, include_role_names_as_candidates=False
        )
        return [
            warning.to_dict()
            for warning in canonicalizer.inspect_script_beats(list(beats))
        ]
    except Exception as exc:  # noqa: BLE001 - 检查失败不应阻断保存
        print(f"[server] 角色名检查失败，跳过提醒：{exc}", file=sys.stderr)
        return []


def _merge_beats_for_check(
    existing: list[ScriptBeat], incoming: list[dict[str, Any]]
) -> list[ScriptBeat]:
    """检查用的宽松合并（Patch v4.23）。

    正常路径与 PUT 完全一致（继承前端没回传的只读字段）；前端草稿缺字段导致
    校验失败时，退化为「只取检查关心的字段」，不让预检接口因为一条草稿报错。
    """
    try:
        return _merge_script_beats(existing, incoming)
    except ValidationError:
        by_id = {beat.beat_id: beat for beat in existing}
        merged: list[ScriptBeat] = []
        for index, item in enumerate(incoming):
            if not isinstance(item, dict):
                continue
            raw = dict(item)
            old = by_id.get(str(raw.get("beat_id") or ""))
            base = old.model_dump() if old is not None else {}
            for key, value in raw.items():
                if value is not None or key not in base:
                    base[key] = value
            base.setdefault("beat_id", f"b{index + 1}")
            base.setdefault("narration_text", "（待填写）")
            base.setdefault("target_panel_ids", ["panel_001"])
            try:
                merged.append(ScriptBeat.model_validate(base))
            except ValidationError:
                continue
        return merged



def _migrate_character_voice(series_dir: Path, old_key: str, new_key: str) -> bool:
    """把 ``series_config.yaml`` 里的专属音色键从旧角色名迁到新角色名。

    Patch v4.25：用户把「道振」提升为规范名「南道振」后，专属音色还挂在
    旧键上会失效（``character_voices`` 的键就是角色名）。新键已经存在时
    **不覆盖**（用户手工配过的优先），只删旧键。
    """
    path = series_dir / "series_config.yaml"
    if not path.is_file() or not old_key or not new_key or old_key == new_key:
        return False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 档案同步失败不应阻断保存
        print(f"[server] 读取 series_config.yaml 失败：{exc}", file=sys.stderr)
        return False
    if not isinstance(data, dict):
        return False
    tts = data.get("tts")
    if not isinstance(tts, dict):
        return False
    voices = tts.get("character_voices")
    if not isinstance(voices, dict) or old_key not in voices:
        return False
    if new_key not in voices:
        voices[new_key] = voices[old_key]
    del voices[old_key]
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return True


def _apply_character_sync(
    series_dir: Path,
    updates: Sequence[dict[str, Any]],
    *,
    episode_no: int = 0,
) -> list[str]:
    """把用户勾选的角色名写法同步进作品档案（Patch v4.25）。

    **只有前端显式传了 ``sync_character_updates`` 才会调用**；默认一个都不传，
    绝不动档案（Patch v4.23「人工保存原样落盘」铁律）。

    支持三种动作：

    * ``add_alias``：把 ``found_name`` 加进 ``target_canonical`` 的 aliases；
    * ``promote_canonical``：把 ``target_canonical`` 改名为 ``found_name``
      （原名并入 aliases，并去掉重复的 found_name），同时迁移
      ``series_config.yaml`` 里 ``tts.character_voices`` 的旧键；
    * ``register_character``：把 ``found_name`` 登记成新角色。

    返回实际执行的动作列表（形如 ``add_alias:道振<-南道振``）。
    """
    applied: list[str] = []
    if not updates:
        return applied
    bible_path = series_dir / "story_bible.json"
    if not bible_path.is_file():
        print(
            "[server] 没有 story_bible.json，跳过角色档案同步", file=sys.stderr
        )
        return applied
    try:
        bible = load_model(StoryBibleV2, bible_path)
    except Exception as exc:  # noqa: BLE001 - 档案同步失败不应阻断保存
        print(
            f"[server] 读取 story_bible.json 失败，跳过角色档案同步：{exc}",
            file=sys.stderr,
        )
        return applied
    characters = list(bible.characters)
    for item in updates:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip()
        found = str(item.get("found_name") or "").strip()
        target = str(item.get("target_canonical") or "").strip()
        if not found:
            continue
        if action == "add_alias":
            for profile in characters:
                if profile.name != target:
                    continue
                if found != profile.name and found not in profile.aliases:
                    profile.aliases = [*profile.aliases, found]
                    applied.append(f"add_alias:{target}<-{found}")
                break
        elif action == "promote_canonical":
            for profile in characters:
                if profile.name != target:
                    continue
                aliases = [alias for alias in profile.aliases if alias != found]
                if target not in aliases:
                    aliases = [*aliases, target]
                profile.aliases = aliases
                profile.name = found
                _migrate_character_voice(series_dir, target, found)
                applied.append(f"promote_canonical:{target}->{found}")
                break
        elif action == "register_character":
            if any(profile.name == found for profile in characters):
                continue
            characters.append(
                CharacterProfile(
                    name=found,
                    aliases=[],
                    visual_traits="",
                    current_status="",
                    first_seen_ep=episode_no,
                    last_seen_ep=episode_no,
                )
            )
            applied.append(f"register_character:{found}")
    if not applied:
        return applied
    bible = bible.model_copy(update={"characters": characters})
    save_model(bible, bible_path)
    return applied


class PanelFlagRequest(BaseModel):
    """``PATCH /api/episodes/{ep_id}/panels/{panel_id}`` 请求体。"""

    is_transition_blank: bool = False


class VoiceSpecRequest(BaseModel):
    """作品级配音设置里的一项（音色 + 语速）。"""

    voice: str = "Elias"
    speed: float = 1.18


class SeriesTTSConfigRequest(BaseModel):
    """``PUT /api/series/{sid}/tts-config`` 请求体（字段均可选）。"""

    narrator: VoiceSpecRequest | None = None
    default_male: VoiceSpecRequest | None = None
    default_female: VoiceSpecRequest | None = None
    character_voices: dict[str, VoiceSpecRequest] | None = None


class ConfigUpdateRequest(BaseModel):
    """``PUT /api/config`` 请求体（字段均可选）。"""

    bailianBaseUrl: str | None = None
    visionModel: str | None = None
    scriptModel: str | None = None
    #: Patch v3.3 Patch 15：Stage 4A 说书体模型（qwen4-max / qwen3.8-max …）。
    storyModel: str | None = None
    ttsModel: str | None = None
    ttsVoice: str | None = None
    maxImageLongEdge: int | None = None
    aspectRatio: str | None = None
    bgmDuckingDb: float | None = None
    timeoutS: float | None = None
    maxRetries: int | None = None


class PronunciationRulesRequest(BaseModel):
    """``PUT /api/pronunciation-rules`` 请求体。"""

    scope: str = "global"
    rules: list[dict[str, Any]] = Field(default_factory=list)


# ---- Patch v4.5：作品级封页 / 双结尾片包 ----
class BrandingUpdateRequest(BaseModel):
    """``PUT /api/series/{sid}/branding`` 请求体（字段均可选，只改传入项）。"""

    cover_text: dict[str, Any] | None = None
    cover_duration_ms: int | None = None
    cover_fade_in_ms: int | None = None
    cover_fade_out_ms: int | None = None
    outro_duration_ms: int | None = None
    outro_fade_in_ms: int | None = None
    outro_fade_out_ms: int | None = None
    default_outro_mode: Literal["none", "continue", "final"] | None = None


class BrandingUploadRequest(BaseModel):
    """``POST /api/series/{sid}/branding/upload`` 请求体。"""

    slot: str
    content_base64: str
    filename: str | None = None


class CoverPreviewRequest(BaseModel):
    """``POST /api/series/{sid}/branding/cover-preview`` 请求体。"""

    episode_no: int | None = None
    cover_text: dict[str, Any] | None = None
    cover_text_override: str | None = None


class BgmUploadRequest(BaseModel):
    """``POST /api/series/{sid}/bgm-library/upload`` 请求体（JSON+Base64）。"""

    filename: str
    content_base64: str
    mood_slot: str | None = None


#: Patch v4.7 曲库上传限制：30MB、仅四种音频后缀（与 s7 ASSET_SUFFIX_PRIORITY 对齐）。
BGM_UPLOAD_MAX_BYTES = 30 * 1024 * 1024
BGM_ALLOWED_SUFFIXES = (".mp3", ".m4a", ".wav", ".ogg")


def _decode_base64_audio(content_base64: str) -> bytes:
    """兼容带/不带 ``data:audio/...;base64,`` 前缀的 Base64 音频解码。"""
    payload = content_base64.strip()
    if "," in payload and payload.lower().startswith("data:"):
        payload = payload.split(",", 1)[1]
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"音频 Base64 解码失败：{exc}") from exc
    if not raw:
        raise HTTPException(status_code=400, detail="音频内容为空")
    if len(raw) > BGM_UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=400, detail="音频体积超过 30MB 上限")
    return raw


# ---------------------------------------------------------------------------
# 工作区视图（磁盘状态 → 前端数据结构）
# ---------------------------------------------------------------------------


class WorkspaceView:
    """把工作区磁盘状态拼装成前端 ``SeriesItem`` 结构。"""

    def __init__(self, config: AppConfig, runner: JobRunner | None = None) -> None:
        self.config = config
        self.runner = runner
        self.registry = s0_catalog.CatalogRegistry(config.workspace_root())

    # ---- 作品级 ----
    def series_ids(self) -> list[str]:
        return [item.series_id for item in self.registry.load_index()]

    def catalog(self, series_id: str) -> SeriesCatalog | None:
        return self.registry.load_catalog(series_id)

    def series_item(self, series_id: str) -> dict[str, Any]:
        catalog = self.catalog(series_id)
        if catalog is None:
            raise HTTPException(status_code=404, detail=f"作品未登记：{series_id}")
        series_dir = self.registry.series_dir(series_id)
        series_config = load_series_config(series_dir)
        return {
            "series_id": catalog.series_id,
            "series_title": catalog.series_title,
            "list_url": catalog.list_url,
            "narration_voice_override": series_config.voice,
            "story_bible": self.story_bible(series_id),
            "episodes": [
                self.episode_item(series_id, episode.episode_no) for episode in catalog.episodes
            ],
        }

    # ---- 记忆体 ----
    def story_bible(self, series_id: str) -> dict[str, Any]:
        series_dir = self.registry.series_dir(series_id)
        path = series_dir / "story_bible.json"
        bible: StoryBibleV2 | None = None
        if path.is_file():
            try:
                bible = load_model(StoryBibleV2, path)
            except Exception:  # noqa: BLE001 - 记忆体损坏时退回空结构
                bible = None
        if bible is None:
            bible = StoryBibleV2(series_id=series_id, series_title="")
        return {
            "series_id": bible.series_id,
            "narration_style": bible.narration_style,
            "characters": [item.model_dump() for item in bible.characters],
            "plot_threads": [item.model_dump() for item in bible.plot_threads],
            "arc_summaries": [item.model_dump() for item in bible.arc_summaries],
            "recent_digests": [item.model_dump() for item in bible.episodes_index],
            "snapshots_ep_nos": list(bible.snapshots_ep_nos),
        }

    # ---- 单话 ----
    def episode_dir(self, series_id: str, episode_no: int) -> Path:
        return self.registry.series_dir(series_id) / "episodes" / f"ep_{episode_no:03d}"

    def episode_viewer_url(self, series_id: str, episode_no: int, episode: Any = None) -> str:
        """话次的「原漫链接」（Patch v4.13）。

        主来源是作品目录里的 ``catalog.json``（``episodes[].viewer_url``）；
        缺失时按顺序回退：

        1. ``episodes_catalog.json``（历史命名，部分作品目录里仍有）；
        2. 话次目录里落盘的 ``episode.json`` 的 ``source_url``（``EpisodeMeta``）。

        三处都没有就返回空串——前端据此隐藏「查看原漫」入口，而不是给出坏链接。
        """
        url = str(getattr(episode, "viewer_url", "") or "").strip()
        if url:
            return url
        series_dir = self.registry.series_dir(series_id)
        for filename in ("catalog.json", "episodes_catalog.json"):
            path = series_dir / filename
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            for item in payload.get("episodes") or []:
                if not isinstance(item, dict):
                    continue
                try:
                    item_no = int(item.get("episode_no") or 0)
                except (TypeError, ValueError):
                    continue
                if item_no != episode_no:
                    continue
                url = str(item.get("viewer_url") or "").strip()
                if url:
                    return url
        meta_path = self.episode_dir(series_id, episode_no) / "episode.json"
        if meta_path.is_file():
            try:
                payload = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                url = str(payload.get("source_url") or "").strip()
                if url:
                    return url
        return ""

    def episode_item(self, series_id: str, episode_no: int) -> dict[str, Any]:
        catalog = self.catalog(series_id)
        episode = next(
            (item for item in (catalog.episodes if catalog else []) if item.episode_no == episode_no),
            None,
        )
        episode_id = f"{series_id}_ep_{episode_no:03d}"
        state = self.runner.state(episode_id) if self.runner else None
        episode_dir = self.episode_dir(series_id, episode_no)

        stages = self.stage_progress(series_id, episode_no, state)
        panels = self.panels(series_id, episode_no)
        beats = self.beats(series_id, episode_no)
        status = self.episode_status(stages, state)
        manager = ManifestManager(
            self.registry.series_dir(series_id), series_id=series_id
        )
        manifest = manager.load(episode_no)
        archived = bool(manifest.archived)
        # Patch v4.11：归档后过程目录已清空，infer 会把 S1~S6 判成 pending；
        # 只要成片还在，话次状态就保持 done，试看/导出入口不退化成 idle。
        if archived and self._episode_video_path(series_id, episode_no, episode_dir):
            status = "done"
        disk_bytes, reclaimable_bytes = self.episode_disk_metrics(
            series_id, episode_no
        )
        current_stage = state.current_stage if state and state.current_stage else ""
        if not current_stage:
            for item in stages:
                if item["status"] == "running":
                    current_stage = item["id"]
                    break
                if item["status"] == "done":
                    current_stage = item["id"]
        logs = list(state.logs) if state else []

        return {
            "episode_no": episode_no,
            "episode_id": episode_id,
            "title": episode.title if episode else f"第 {episode_no} 话",
            "publish_date": episode.publish_date if episode else "",
            "viewer_url": self.episode_viewer_url(series_id, episode_no, episode),
            "is_locked": bool(episode.is_locked) if episode else False,
            "selected": False,
            "current_stage": current_stage or "s0_catalog",
            "status": status,
            "archived": archived,
            "disk_bytes": disk_bytes,
            "reclaimable_bytes": 0 if archived else reclaimable_bytes,
            "video_duration_ms": self.video_duration_ms(
                series_id, episode_no, episode_dir
            ),
            "stages": stages,
            "panels": panels,
            "beats": beats,
            "render_options": load_episode_render_options(episode_dir).model_dump(),
            "logs": logs,
        }

    def stage_progress(
        self, series_id: str, episode_no: int, state: EpisodeState | None
    ) -> list[dict[str, Any]]:
        """合并磁盘 manifest 与内存运行态，产出前端 ``StageProgress[]``。

        **三方合并**：manifest（执行记录）→ 内存运行态（本次会话）→ 磁盘产物（事实）。
        磁盘产物是最后一道兜底：早期由 CLI 直接产出的章节根本没有 manifest，
        若只看前两者，界面对着真实存在的成片显示「待执行」。
        """
        manager = ManifestManager(
            self.registry.series_dir(series_id), series_id=series_id
        )
        manifest = manager.load(episode_no)
        episode_dir = self.episode_dir(series_id, episode_no)
        inferred = infer_stage_status(episode_dir)
        inferred_artifacts = infer_stage_artifacts(episode_dir)
        result: list[dict[str, Any]] = []
        for definition in STAGE_DEFS:
            stage_id = str(definition["id"])
            status = "pending"
            duration: float | None = None
            summary = ""
            entry = manifest.stages.get(stage_id)
            if entry is not None:
                status = str(entry.status.value)
                if entry.artifact:
                    summary = Path(entry.artifact).name
                if entry.error:
                    summary = entry.error
            elif stage_id in inferred:
                # manifest 没记录 → 看磁盘上有没有产物（CLI 产出的章节走这条路）
                if inferred[stage_id] is StageStatus.DONE:
                    status = "done"
                    summary = inferred_artifacts.get(stage_id, "")
            if not summary:
                # 磁盘产物摘要兜底：manifest 只记了状态没记产物时，
                # 「事实」（磁盘上真有什么）比「记录」更可信。
                summary = inferred_artifacts.get(stage_id, "")
            if stage_id == "s0_catalog" and status == "pending":
                # S0 是作品级阶段（目录索引），判定看作品目录而不是话次目录。
                catalog_path = self.registry.series_dir(series_id) / "catalog.json"
                if catalog_path.is_file():
                    status = "done"
                    summary = "catalog.json"
            if state is not None:
                runtime = state.stages.get(stage_id)
                if runtime is not None:
                    status = str(runtime.get("status") or status)
                    if runtime.get("duration_sec") is not None:
                        duration = float(runtime["duration_sec"])
                    if status == "done" and not summary:
                        summary = f"{definition['label']}已完成"
            item: dict[str, Any] = {
                "id": stage_id,
                "code": definition["code"],
                "label": definition["label"],
                "phase": definition["phase"],
                "status": status,
            }
            if duration is not None:
                item["durationSec"] = duration
            if summary:
                item["summary"] = summary
            result.append(item)
        return result

    def episode_status(
        self, stages: list[dict[str, Any]], state: EpisodeState | None
    ) -> str:
        if state is not None and state.status in {"queued", "running", "failed", "done"}:
            return state.status
        statuses = [item["status"] for item in stages]
        if "failed" in statuses:
            return "failed"
        if "running" in statuses:
            return "running"
        if statuses and all(status == "done" for status in statuses[1:]):
            return "done"
        return "idle"

    def _episode_video_path(
        self, series_id: str, episode_no: int, episode_dir: Path | None = None
    ) -> Path | None:
        """定位本话规范成片：优先 ``07_out`` 下非空 mp4，其次 exports 导出。"""
        base = episode_dir or self.episode_dir(series_id, episode_no)
        out_dir = base / "07_out"
        candidates = (
            sorted(
                p
                for p in out_dir.glob("*.mp4")
                if p.is_file() and p.stat().st_size > 0
            )
            if out_dir.is_dir()
            else []
        )
        if candidates:
            return candidates[0]
        exports_dir = self.registry.series_dir(series_id) / "exports"
        if exports_dir.is_dir():
            exports = sorted(
                p
                for p in exports_dir.glob(f"EP{episode_no:03d}_*.mp4")
                if p.is_file() and p.stat().st_size > 0
            )
            if exports:
                return exports[0]
        return None

    def episode_disk_metrics(
        self, series_id: str, episode_no: int
    ) -> tuple[int, int]:
        """返回（当前占用字节、可回收字节）。

        可回收口径与 ``ManifestManager.archive_episode_keep_video`` 的删除
        范围严格一致：过程目录（含 ``03b_scenes``）、``07_out/_work``、
        本话 bible 快照；成片与 ``manifest.json`` 不算可回收。
        """
        episode_dir = self.episode_dir(series_id, episode_no)
        disk_bytes = dir_size(episode_dir)
        reclaimable = 0
        for dirname in ARCHIVE_INTERMEDIATE_DIRS:
            reclaimable += dir_size(episode_dir / dirname)
        reclaimable += dir_size(episode_dir / "07_out" / "_work")
        snapshot = (
            self.registry.series_dir(series_id)
            / "bible_snapshots"
            / f"ep_{episode_no:03d}.json"
        )
        if snapshot.is_file():
            try:
                reclaimable += snapshot.stat().st_size
            except OSError:  # pragma: no cover - 竞态删除不应阻断列表加载
                pass
        return disk_bytes, reclaimable

    def video_duration_ms(
        self, series_id: str, episode_no: int, episode_dir: Path
    ) -> int:
        """成片时长（毫秒），三级兜底。

        1. ``06_plan/timeline.json`` 的 ``total_duration_ms``（正常链路）；
        2. 归档后 timeline 已清，读 manifest 的 ``archived_duration_ms``；
        3. 都没有时直接 ffprobe 成片真实时长。
        """
        timeline_path = episode_dir / "06_plan" / "timeline.json"
        if timeline_path.is_file():
            try:
                data = json.loads(timeline_path.read_text(encoding="utf-8"))
                duration = int(data.get("total_duration_ms") or 0)
                if duration > 0:
                    return duration
            except Exception:  # noqa: BLE001 - 时间轴损坏时走下级兜底
                pass
        manager = ManifestManager(
            self.registry.series_dir(series_id), series_id=series_id
        )
        manifest = manager.load(episode_no)
        if int(manifest.archived_duration_ms or 0) > 0:
            return int(manifest.archived_duration_ms)
        video_path = self._episode_video_path(series_id, episode_no, episode_dir)
        if video_path is not None:
            try:
                return s7_renderer.probe_duration_ms(
                    video_path, ffmpeg_bin=self.config.render.ffmpeg_bin
                )
            except Exception:  # noqa: BLE001 - 探测失败按 0 处理，不阻断列表加载
                return 0
        return 0

    # ---- 面板 ----
    def panels(self, series_id: str, episode_no: int) -> list[dict[str, Any]]:
        episode_dir = self.episode_dir(series_id, episode_no)
        panels_dir = episode_dir / "02_panels"
        metas = s6_timeline.scan_panels(panels_dir)
        if not metas:
            return []
        vision_panels = self._vision_panels(episode_dir)
        used: set[int] = set()
        result: list[dict[str, Any]] = []
        for index, meta in enumerate(metas):
            stem = Path(meta.image_path).stem
            vision = self._match_vision(vision_panels, stem, index, used)
            if vision is not None:
                used.add(index)
            focus = list(vision.focus_region) if vision and vision.focus_region else [0.0, 0.0, 1.0, 1.0]
            result.append(
                {
                    "panel_id": stem,
                    "image_url": (
                        f"/workspace/{series_id}/episodes/ep_{episode_no:03d}"
                        f"/02_panels/{Path(meta.image_path).name}"
                    ),
                    "width": meta.width,
                    "height": meta.height,
                    "source_slice_range": list(meta.source_slice_range),
                    "is_tall_panel": bool(meta.is_tall_panel),
                    "is_transition_blank": bool(vision.is_transition_blank) if vision else False,
                    "scene_summary": vision.scene_summary if vision else "",
                    "dialogues": (
                        [item.model_dump() for item in vision.dialogues] if vision else []
                    ),
                    "sfx_list": list(vision.sfx_list) if vision else [],
                    "focus_region": focus,
                }
            )
        return result

    def _vision_panels(self, episode_dir: Path) -> list[Any]:
        path = episode_dir / "03_vision" / "vision.json"
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 视觉结果损坏时按无面板处理
            return []
        panels = data.get("panels") or []
        return [_VisionPayload.model_validate(item) for item in panels]

    @staticmethod
    def _match_vision(
        vision_panels: list[Any], stem: str, index: int, used: set[int]
    ) -> Any | None:
        """按 ID / 尾部数字 / 顺序三种方式关联 Stage 3 的面板信息。"""
        for item in vision_panels:
            if item.panel_id == stem:
                return item
        digits = "".join(ch for ch in stem if ch.isdigit())
        if digits:
            for item in vision_panels:
                item_digits = "".join(ch for ch in item.panel_id if ch.isdigit())
                if item_digits and item_digits == digits:
                    return item
        if index not in used and index < len(vision_panels):
            return vision_panels[index]
        return None

    # ---- 解说稿 ----
    def beats(self, series_id: str, episode_no: int) -> list[dict[str, Any]]:
        episode_dir = self.episode_dir(series_id, episode_no)
        script_path = episode_dir / "04_script" / "script.json"
        if not script_path.is_file():
            return []
        try:
            script = load_model(NarrationScript, script_path)
        except Exception:  # noqa: BLE001 - 剧本损坏时按无解说处理
            return []
        durations = self._audio_durations(episode_dir)
        assignment_ctx = self._voice_assignment_context(series_id)
        result: list[dict[str, Any]] = []
        for beat in script.beats:
            item: dict[str, Any] = {
                "beat_id": beat.beat_id,
                "narration_text": beat.narration_text,
                "tts_text": beat.tts_text or "",
                "emotion_instruction": beat.emotion_instruction,
                "target_panel_ids": list(beat.target_panel_ids),
                "motion": getattr(beat.motion, "value", beat.motion),
                "pause_after_ms": beat.pause_after_ms,
                "audio_duration_ms": durations.get(beat.beat_id, 0),
                "speaker_role": beat.speaker_role,
                "speaker_gender": str(getattr(beat, "speaker_gender", "") or ""),
                # Patch v4.2：逐句指定音色（空串 = 跟随角色解析）。
                "voice_override": str(getattr(beat, "voice_override", "") or ""),
                # Patch v4.4：单句语速微调（None = 跟随角色默认语速）。
                "speed_override": getattr(beat, "speed_override", None),
                "sfx_cue": beat.sfx_cue,
                "focus_hint": beat.focus_hint,
                # Patch v4.13：这条解说归属的镜头组（幕）；空 = 尚未归属。
                "scene_id": getattr(beat, "scene_id", None),
            }
            if assignment_ctx is not None:
                item["resolved_voice"] = self._resolved_voice(beat, assignment_ctx)
            result.append(item)
        return result

    def _voice_assignment_context(
        self, series_id: str
    ) -> tuple[SeriesTTSConfig, list[CharacterProfile], dict[str, str]] | None:
        """解析「这条 beat 用哪个音色」所需的上下文。

        与 Stage 5 保持同一份输入：作品级配音模板（合并全局）+ StoryBible 角色表
        + 旧槽位音色表。这样审核台显示的「本句配音」就是 Stage 5 真正会用的音色。
        """
        try:
            series_dir = self.registry.series_dir(series_id)
        except Exception:  # noqa: BLE001 - 作品目录缺失时按「无法解析」处理
            return None
        try:
            series_config = load_series_config(series_dir)
        except Exception:  # noqa: BLE001 - 作品级配置损坏时退回全局模板
            series_config = None
        merged = merge_series_tts(self.config.tts, series_config.tts)
        characters = s5_tts.load_bible_characters(series_dir)
        return merged, characters, dict(self.config.tts.voice_cast)

    def _resolved_voice(
        self,
        beat: ScriptBeat,
        assignment_ctx: tuple[SeriesTTSConfig, list[CharacterProfile], dict[str, str]],
    ) -> dict[str, Any]:
        """算出这条 beat 的「音色 + 语速 + 来源」，供审核台显示与核对。"""
        series_tts, characters, voice_cast = assignment_ctx
        assignment = s5_tts.resolve_beat_voice_assignment(
            beat,
            series_tts=series_tts,
            characters=characters,
            default_voice=self.config.tts.default_voice,
            voice_cast=voice_cast,
        )
        # Patch v4.4：角色默认语速始终一并下发（未微调时与 speed 相等；微调后
        # speed 是本句生效语速，role_speed 仍是按角色解析出的默认语速）。
        base = s5_tts.resolve_beat_voice_assignment_without_override(
            beat,
            series_tts=series_tts,
            characters=characters,
            default_voice=self.config.tts.default_voice,
            voice_cast=voice_cast,
        )
        payload: dict[str, Any] = {
            "voice": assignment.spec.voice,
            "speed": assignment.spec.speed,
            "role_speed": base.spec.speed,
            "source": assignment.source,
            "label": assignment.label,
            "role": assignment.role,
            "gender": assignment.gender,
        }
        if assignment.source == "voice_override":
            # 逐句指定音色时，同时给出「清空指定后按角色会用哪个音色」。
            payload["role_voice"] = {
                "voice": base.spec.voice,
                "speed": base.spec.speed,
                "source": base.source,
                "label": base.label,
                "role": base.role,
                "gender": base.gender,
            }
        return payload

    @staticmethod
    def _audio_durations(episode_dir: Path) -> dict[str, int]:
        manifest_path = episode_dir / "05_audio" / "audio_manifest.json"
        if not manifest_path.is_file():
            return {}
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 清单损坏时按无时长处理
            return {}
        items = data if isinstance(data, list) else (data.get("audios") or [])
        durations: dict[str, int] = {}
        for item in items:
            beat_id = str(item.get("beat_id") or "")
            if beat_id:
                durations[beat_id] = int(item.get("duration_ms") or 0)
        return durations

    # ---- 反查 ----
    def find_episode(self, episode_id: str) -> tuple[str, int] | None:
        """由 ``episode_id`` 反查 ``(series_id, episode_no)``。"""
        for series_id in self.series_ids():
            catalog = self.catalog(series_id)
            if catalog is None:
                continue
            for episode in catalog.episodes:
                if episode.episode_id == episode_id:
                    return series_id, episode.episode_no
            prefix = f"{series_id}_ep_"
            if episode_id.startswith(prefix):
                try:
                    return series_id, int(episode_id[len(prefix) :])
                except ValueError:
                    continue
        return None


# ---------------------------------------------------------------------------
# Patch v4.5：作品级封页 / 双结尾片包辅助
# ---------------------------------------------------------------------------
#: 上传槽位 → series_config.yaml 中的图片字段名。
_BRANDING_SLOT_FIELDS: dict[str, str] = {
    "cover": "cover_image",
    "outro_continue": "outro_continue_image",
    "outro_final": "outro_final_image",
}
#: 槽位 → GET payload 中的静态 URL 字段名。
_BRANDING_URL_FIELDS: dict[str, str] = {
    "cover": "cover_url",
    "outro_continue": "outro_continue_url",
    "outro_final": "outro_final_url",
}


def _branding_image_url(series_id: str, series_dir: Path, rel_path: str | None) -> str | None:
    """把 branding 相对路径拼成带 mtime 防缓存参数的静态 URL；文件缺失返回 ``None``。"""
    if not rel_path:
        return None
    image_path = series_dir / rel_path
    if not image_path.is_file():
        return None
    return f"/workspace/{series_id}/{rel_path}?v={image_path.stat().st_mtime_ns}"


def _series_branding_payload(series_id: str, series_dir: Path) -> dict[str, Any]:
    """组装 GET /branding 的完整响应（配置 + 三个图片 URL + 可用字体）。"""
    branding = load_series_config(series_dir).branding
    payload: dict[str, Any] = {"series_id": series_id}
    payload.update(branding.model_dump())
    for slot, url_field in _BRANDING_URL_FIELDS.items():
        rel_path = getattr(branding, _BRANDING_SLOT_FIELDS[slot])
        payload[url_field] = _branding_image_url(series_id, series_dir, rel_path)
    payload["font_options"] = [
        {"name": name, "path": path}
        for name, path in FONT_CANDIDATES
        if Path(path).is_file()
    ]
    return payload


def _persist_branding(series_dir: Path, branding: SeriesBrandingConfig) -> None:
    """把 branding 配置安全合并写回 ``series_config.yaml``，保留其它顶层键。"""
    series_dir.mkdir(parents=True, exist_ok=True)
    path = series_dir / SERIES_CONFIG_FILENAME
    data: dict[str, Any] = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    data["branding"] = branding.model_dump()
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _resolve_episode_dir(view: "WorkspaceView", episode_id: str) -> tuple[str, Path, int]:
    """把 ``{series_id}_ep_{no:03d}`` 复合 ID 反解为（作品 ID、单话目录、话次）。"""
    if "_ep_" not in episode_id:
        raise HTTPException(status_code=404, detail=f"无法识别的单话 ID：{episode_id}")
    series_id, suffix = episode_id.rsplit("_ep_", 1)
    if not suffix.isdigit():
        raise HTTPException(status_code=404, detail=f"无法识别的单话 ID：{episode_id}")
    episode_no = int(suffix)
    if series_id not in view.series_ids():
        raise HTTPException(status_code=404, detail=f"作品不存在：{series_id}")
    episode_dir = view.episode_dir(series_id, episode_no)
    if not episode_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"单话目录不存在：{episode_id}")
    return series_id, episode_dir, episode_no


#: audio_manifest.json 中可能承载音频条目的容器键（与 s6_timeline 解析口径一致）。
_MANIFEST_CONTAINER_KEYS = ("audios", "audio_clips", "clips", "items", "audio_manifest")


def _sync_pause_to_audio_manifest(
    episode_dir: Path, beats: Sequence[ScriptBeat] | Sequence[dict[str, Any]]
) -> int:
    """把剧本侧的 ``pause_after_ms`` 同步进 ``05_audio/audio_manifest.json``。

    Stage 6 的时间轴以 audio_manifest 的停顿为唯一来源，因此人工改稿或自动
    节奏留白后必须把新停顿回写到清单，用户才能只重跑 S6→S7 而无需重跑 S5。
    兼容清单为裸列表或 ``{"audios": [...]}`` 等容器结构；只更新匹配到的
    beat 的停顿字段，不动其它任何字段；原子写回。返回实际同步的条目数。
    """
    manifest_path = episode_dir / "05_audio" / "audio_manifest.json"
    if not manifest_path.is_file():
        return 0

    pause_map: dict[str, int] = {}
    for beat in beats:
        if isinstance(beat, dict):
            beat_id = str(beat.get("beat_id") or "")
            pause = int(beat.get("pause_after_ms") or 0)
        else:
            beat_id = str(getattr(beat, "beat_id", "") or "")
            pause = int(getattr(beat, "pause_after_ms", 0) or 0)
        if beat_id:
            pause_map[beat_id] = max(0, min(2000, pause))
    if not pause_map:
        return 0

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 清单损坏时不阻断剧本保存
        return 0

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = next(
            (
                candidate
                for key in _MANIFEST_CONTAINER_KEYS
                if isinstance((candidate := data.get(key)), list)
            ),
            None,
        )
        if items is None:
            return 0
    else:
        return 0

    synced = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        beat_id = str(item.get("beat_id") or "")
        if beat_id in pause_map:
            item["pause_after_ms"] = pause_map[beat_id]
            synced += 1
    if synced == 0:
        return 0

    tmp_path = manifest_path.with_name(f"{manifest_path.name}.tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp_path, manifest_path)
    return synced


class _VisionPayload(BaseModel):
    """Stage 3 面板结果的最小读取模型。"""

    panel_id: str
    scene_summary: str = ""
    dialogues: list[DialogueBubble] = Field(default_factory=list)
    sfx_list: list[str] = Field(default_factory=list)
    focus_region: list[float] | None = None
    is_transition_blank: bool = False


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------


def create_app(
    config: AppConfig | None = None,
    *,
    workspace: str | None = None,
    runner: JobRunner | None = None,
    client_factory: Any = None,
    tts_factory: Any = None,
) -> FastAPI:
    """构造 FastAPI 应用。

    ``workspace`` 覆盖配置里的工作区根目录（测试与多工作区场景）。
    """
    load_env_file()
    resolved = config or load_config()
    if workspace:
        resolved = resolved.model_copy(
            update={"workspace": WorkspaceConfig(root=Path(workspace))}
        )
    workspace_root = resolved.workspace_root()
    workspace_root.mkdir(parents=True, exist_ok=True)

    job_runner = runner or JobRunner(
        resolved, client_factory=client_factory, tts_factory=tts_factory
    )
    view = WorkspaceView(resolved, job_runner)

    app = FastAPI(title="Webtoon2Video 控制台", version="2.4")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.config = resolved
    app.state.runner = job_runner
    app.state.view = view
    # 配置文件位置：优先既有配置，否则落在工作区根（便于多工作区与测试隔离）。
    # Patch v3.4 Patch 16：显式传入配置（测试 / 嵌入式场景）时写工作区自己的
    # config.yaml，避免覆盖仓库里的真实部署配置。
    if config is None:
        app.state.config_path = find_config_path() or (workspace_root / "config.yaml")
    else:
        app.state.config_path = workspace_root / "config.yaml"

    def _reload_config() -> AppConfig:
        """重新读取磁盘配置并刷新内存态（工作区根目录保持不变）。

        Patch v4.17：热重载必须**同步给后台调度器与工作区视图**。
        之前只更新 ``app.state.config``，而 ``JobRunner`` 在进程启动时把配置
        拍了一份快照（``self.config``），后台任务每次取模型都读这份快照——
        于是出现「界面显示新模型、后台仍用启动时的旧模型」，
        改完配置必须重启后端才生效。
        这里把新配置同时写回 ``job_runner.config`` 与 ``view.config``，
        保存后下一个后台任务就会用上新模型，无需重启。
        """
        path = app.state.config_path
        updated = app.state.config
        if path.is_file():
            try:
                updated = load_config(path)
            except ConfigError:  # 配置损坏时保留旧值，避免把服务打挂
                updated = app.state.config
        updated = updated.model_copy(
            update={"workspace": WorkspaceConfig(root=workspace_root)}
        )
        app.state.config = updated
        # Patch v4.17：同步给后台调度器（模型选择）与工作区视图（音色/渲染设置）。
        app.state.runner.config = updated
        app.state.view.config = updated
        return updated

    # 静态资源：前端通过 /workspace/... 直接取图片、字幕与成片。
    app.mount(
        "/workspace",
        StaticFiles(directory=str(workspace_root)),
        name="workspace",
    )

    # ------------------------------------------------------------------
    # 书架
    # ------------------------------------------------------------------
    @app.get("/api/series")
    async def list_series() -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for series_id in view.series_ids():
            try:
                items.append(view.series_item(series_id))
            except HTTPException:
                continue
        return items

    @app.post("/api/series")
    async def create_series(payload: SeriesCreateRequest) -> dict[str, Any]:
        source = payload.url.strip()
        if not source:
            raise HTTPException(status_code=400, detail="请填写漫画目录 URL 或本地目录")
        try:
            catalog = await _resolve_catalog(source)
        except Exception as exc:  # noqa: BLE001 - 抓取失败要回给前端
            raise HTTPException(status_code=502, detail=f"目录抓取失败：{exc}") from exc
        if payload.title:
            catalog = catalog.model_copy(update={"series_title": payload.title})
        view.registry.register(catalog, done_episodes=0)
        view.registry.save_catalog(catalog)
        return view.series_item(catalog.series_id)

    @app.get("/api/series/{series_id}")
    async def get_series(series_id: str) -> dict[str, Any]:
        return view.series_item(series_id)

    # ------------------------------------------------------------------
    # 配音设置（Patch v3.2 Patch 13 C2）
    # ------------------------------------------------------------------
    @app.get("/api/tts/voices")
    async def list_tts_voices() -> dict[str, Any]:
        """百炼官方音色 + 试听样例是否就绪 + MiniMax 音色清单（Patch v3.4 Patch 16）。"""
        return {
            "voices": voice_options_payload(),
            "minimax_voices": minimax_voice_options_payload(),
        }

    @app.get("/api/tts/samples/{voice_id}")
    async def get_tts_sample(voice_id: str) -> FileResponse:
        """试听样例音频（静态文件，浏览器用 ``playbackRate`` 变速播放）。

        Patch v3.4 Patch 16：按百炼供应商解析（``voice_samples/qwen/`` 优先，
        旧的扁平目录回退），因此旧链接在分供应商布局下依然可用。
        """
        candidates = provider_voice_sample_candidates("qwen", voice_id)
        if not candidates:
            raise HTTPException(status_code=404, detail=f"未知音色：{voice_id}")
        for candidate in candidates:
            if candidate.is_file():
                # Patch v3.3 Patch 15：含空格的 ID（Eldric Sage）下载名也走安全文件名。
                return FileResponse(candidate, media_type="audio/wav", filename=candidate.name)
        raise HTTPException(
            status_code=404,
            detail=f"音色 {voice_id} 还没有试听样例，请先运行 backend/scripts/init_voice_samples.py",
        )

    @app.get("/api/tts/samples/{provider}/{voice_id}")
    async def get_tts_provider_sample(provider: str, voice_id: str) -> FileResponse:
        """按供应商试听（Patch v3.4 Patch 16）。

        路径形如 ``/api/tts/samples/minimax/Chinese%20(Mandarin)_Radio_Host``；
        样例按供应商隔离在 ``voice_samples/{provider}/``，旧的扁平目录仍可回退。
        """
        name = _safe_provider_dir(provider)
        if name not in VOICE_SAMPLE_PROVIDERS:
            raise HTTPException(status_code=404, detail=f"未知供应商：{provider}")
        candidates = provider_voice_sample_candidates(name, voice_id)
        if not candidates:
            raise HTTPException(status_code=404, detail=f"未知音色：{voice_id}")
        for candidate in candidates:
            if candidate.is_file():
                return FileResponse(candidate, media_type="audio/wav", filename=candidate.name)
        raise HTTPException(
            status_code=404,
            detail=f"音色 {voice_id} 还没有试听样例，请先运行 backend/scripts/init_voice_samples.py --provider {name}",
        )

    @app.get("/api/series/{series_id}/tts-config")
    async def get_series_tts_config(series_id: str) -> dict[str, Any]:
        """作品级配音设置 + 已识别角色清单。"""
        return _series_tts_payload(app.state.config, view, series_id)

    @app.put("/api/series/{series_id}/tts-config")
    async def put_series_tts_config(
        series_id: str, payload: SeriesTTSConfigRequest
    ) -> dict[str, Any]:
        """保存作品级配音设置（写 ``series_config.yaml``）。"""
        series_dir = view.registry.series_dir(series_id)
        series_dir.mkdir(parents=True, exist_ok=True)
        path = series_dir / "series_config.yaml"
        data: dict[str, Any] = {}
        if path.is_file():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        tts: dict[str, Any] = {}
        if payload.narrator is not None:
            tts["narrator"] = payload.narrator.model_dump()
        if payload.default_male is not None:
            tts["default_male"] = payload.default_male.model_dump()
        if payload.default_female is not None:
            tts["default_female"] = payload.default_female.model_dump()
        if payload.character_voices is not None:
            tts["character_voices"] = {
                str(name): spec.model_dump() for name, spec in payload.character_voices.items()
            }
        if tts:
            data["tts"] = tts
        path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return _series_tts_payload(app.state.config, view, series_id)

    # ---- Patch v4.5：作品级封页写字 + 双结尾图片包 ----
    @app.get("/api/series/{series_id}/branding")
    async def get_series_branding(series_id: str) -> dict[str, Any]:
        """读取作品片包配置 + 三张图片的静态 URL + 可用字体清单。"""
        if series_id not in view.series_ids():
            raise HTTPException(status_code=404, detail=f"作品不存在：{series_id}")
        series_dir = view.registry.series_dir(series_id)
        return _series_branding_payload(series_id, series_dir)

    @app.put("/api/series/{series_id}/branding")
    async def put_series_branding(
        series_id: str, payload: BrandingUpdateRequest
    ) -> dict[str, Any]:
        """更新片包配置（不含图片上传）；``*_image`` 字段不受影响。"""
        if series_id not in view.series_ids():
            raise HTTPException(status_code=404, detail=f"作品不存在：{series_id}")
        series_dir = view.registry.series_dir(series_id)
        branding = load_series_config(series_dir).branding
        updates = payload.model_dump(exclude_unset=True, exclude_none=True)
        cover_text_patch = updates.pop("cover_text", None)
        if cover_text_patch is not None:
            try:
                branding.cover_text = branding.cover_text.model_copy(
                    update=cover_text_patch
                )
            except ValidationError as exc:
                raise HTTPException(
                    status_code=400, detail=f"封页文字参数不合法：{exc.errors()}"
                ) from exc
        try:
            branding = branding.model_copy(update=updates)
        except ValidationError as exc:
            raise HTTPException(
                status_code=400, detail=f"片包配置不合法：{exc.errors()}"
            ) from exc
        _persist_branding(series_dir, branding)
        return _series_branding_payload(series_id, series_dir)

    @app.post("/api/series/{series_id}/branding/upload")
    async def upload_series_branding_image(
        series_id: str, payload: BrandingUploadRequest
    ) -> dict[str, Any]:
        """解码 Base64 图片，规范化为 PNG 存入 ``assets/branding/{slot}.png``。"""
        if series_id not in view.series_ids():
            raise HTTPException(status_code=404, detail=f"作品不存在：{series_id}")
        if payload.slot not in BRANDING_SLOTS:
            raise HTTPException(
                status_code=400,
                detail=f"非法图片槽位：{payload.slot}（可选：{', '.join(BRANDING_SLOTS)}）",
            )
        try:
            raw_bytes = decode_base64_image(payload.content_base64)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"图片解码失败：{exc}") from exc
        series_dir = view.registry.series_dir(series_id)
        branding_dir = series_dir / "assets" / "branding"
        branding_dir.mkdir(parents=True, exist_ok=True)
        out_path = branding_dir / f"{payload.slot}.png"
        try:
            save_branding_image(raw_bytes, out_path)
        except Exception as exc:  # noqa: BLE001 - Pillow 打开失败等统一转 400
            raise HTTPException(status_code=400, detail=f"图片无法识别：{exc}") from exc

        branding = load_series_config(series_dir).branding
        rel_path = f"assets/branding/{payload.slot}.png"
        setattr(branding, _BRANDING_SLOT_FIELDS[payload.slot], rel_path)
        _persist_branding(series_dir, branding)
        return _series_branding_payload(series_id, series_dir)

    @app.delete("/api/series/{series_id}/branding/{slot}")
    async def delete_series_branding_image(series_id: str, slot: str) -> dict[str, Any]:
        """删除槽位 PNG 并把配置中的对应字段置空。"""
        if series_id not in view.series_ids():
            raise HTTPException(status_code=404, detail=f"作品不存在：{series_id}")
        if slot not in BRANDING_SLOTS:
            raise HTTPException(
                status_code=400,
                detail=f"非法图片槽位：{slot}（可选：{', '.join(BRANDING_SLOTS)}）",
            )
        series_dir = view.registry.series_dir(series_id)
        image_path = series_dir / "assets" / "branding" / f"{slot}.png"
        image_path.unlink(missing_ok=True)
        branding = load_series_config(series_dir).branding
        setattr(branding, _BRANDING_SLOT_FIELDS[slot], None)
        _persist_branding(series_dir, branding)
        return _series_branding_payload(series_id, series_dir)

    @app.post("/api/series/{series_id}/branding/cover-preview")
    async def preview_series_cover(
        series_id: str, payload: CoverPreviewRequest
    ) -> Response:
        """用已上传封面底图 + Pillow 实时渲染一张含真实文字的 PNG。"""
        if series_id not in view.series_ids():
            raise HTTPException(status_code=404, detail=f"作品不存在：{series_id}")
        series_dir = view.registry.series_dir(series_id)
        branding = load_series_config(series_dir).branding
        if not branding.cover_image:
            raise HTTPException(status_code=404, detail="尚未上传通用封页底图")
        cover_path = series_dir / branding.cover_image
        if not cover_path.is_file():
            raise HTTPException(status_code=404, detail="封页底图文件缺失，请重新上传")

        cover_text = branding.cover_text
        if payload.cover_text is not None:
            try:
                cover_text = cover_text.model_copy(update=payload.cover_text)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=400, detail=f"封页文字参数不合法：{exc.errors()}"
                ) from exc

        with tempfile.NamedTemporaryFile(prefix="cover_preview_", suffix=".png", delete=False) as handle:
            tmp_path = Path(handle.name)
        try:
            render_cover_image(
                cover_path,
                tmp_path,
                episode_no=int(payload.episode_no or 1),
                cover_text=cover_text,
                override_text=payload.cover_text_override,
                width=app.state.config.video.width,
                height=app.state.config.video.height,
            )
            return Response(content=tmp_path.read_bytes(), media_type="image/png")
        finally:
            tmp_path.unlink(missing_ok=True)

    @app.put("/api/episodes/{episode_id}/render-options")
    async def put_episode_render_options(
        episode_id: str, options: EpisodeRenderOptions
    ) -> dict[str, Any]:
        """保存单话片包选项到 ``episodes/ep_XXX/render_options.json``。"""
        _series_id, episode_dir, _episode_no = _resolve_episode_dir(view, episode_id)
        save_episode_render_options(episode_dir, options)
        return {"episode_id": episode_id, "render_options": options.model_dump()}

    @app.put("/api/series/{series_id}/memory")
    async def update_memory(series_id: str, payload: MemoryUpdateRequest) -> dict[str, Any]:
        series_dir = view.registry.series_dir(series_id)
        path = series_dir / "story_bible.json"
        bible: StoryBibleV2
        if path.is_file():
            try:
                bible = load_model(StoryBibleV2, path)
            except Exception:  # noqa: BLE001 - 记忆体损坏时从空白重建
                bible = StoryBibleV2(series_id=series_id, series_title="")
        else:
            bible = StoryBibleV2(series_id=series_id, series_title="")
        if payload.narration_style is not None:
            bible.narration_style = payload.narration_style
        if payload.characters is not None:
            bible.characters = [CharacterProfile.model_validate(item) for item in payload.characters]
        if payload.plot_threads is not None:
            bible.plot_threads = [PlotThread.model_validate(item) for item in payload.plot_threads]
        if payload.arc_summaries is not None:
            bible.arc_summaries = [
                ArcSummary.model_validate(item) for item in payload.arc_summaries
            ]
        save_model(bible, path)
        return view.story_bible(series_id)

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------
    @app.post("/api/jobs/batch")
    async def submit_jobs(payload: JobBatchRequest) -> dict[str, Any]:
        if not payload.items:
            raise HTTPException(status_code=400, detail="任务列表为空")
        for item in payload.items:
            if "series_id" not in item or "episode_no" not in item:
                raise HTTPException(
                    status_code=400, detail="任务项需要 series_id 与 episode_no"
                )
        jobs = await job_runner.submit_batch(payload.items, mode=payload.mode)
        return {"jobs": jobs}

    @app.post("/api/episodes/{episode_id}/rerun")
    async def rerun_episode(episode_id: str, payload: RerunRequest) -> dict[str, Any]:
        located = view.find_episode(episode_id)
        if located is None:
            raise HTTPException(status_code=404, detail=f"找不到话次：{episode_id}")
        series_id, episode_no = located
        try:
            job = await job_runner.submit_episode(
                series_id,
                episode_no,
                from_stage=payload.from_stage,
                mode=payload.mode,
            )
        except JobError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return job

    @app.post("/api/episodes/{episode_id}/reset")
    async def reset_episode(
        episode_id: str, payload: EpisodeResetRequest
    ) -> dict[str, Any]:
        """清空该话指定阶段及其下游的产物与完成标记（Patch v4.1 Patch 21）。"""
        located = view.find_episode(episode_id)
        if located is None:
            raise HTTPException(status_code=404, detail=f"找不到话次：{episode_id}")
        series_id, episode_no = located
        if payload.from_stage not in STAGE_IDS or payload.from_stage == "s0_catalog":
            raise HTTPException(
                status_code=400, detail="只能从 S1 ~ S7 清理单话下游阶段"
            )
        state = job_runner.state_of(series_id, episode_no)
        if state.status in {"queued", "running"}:
            raise HTTPException(
                status_code=409, detail="该话正在后台执行任务，请等待完成后再清理"
            )
        try:
            summary = job_runner.reset_episode_from_stage(
                series_id,
                episode_no,
                payload.from_stage,
                include_human_storyboard=payload.include_human_storyboard,
            )
        except JobError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "episode_id": episode_id,
            "from_stage": payload.from_stage,
            "cleared_stages": summary.get("cleared_stages", []),
            "removed_count": summary.get("removed_count", 0),
            "preserved_human_storyboard": bool(
                summary.get("preserved_human_storyboard")
            ),
            "stages": view.stage_progress(series_id, episode_no, state),
            "logs": list(state.logs),
        }

    @app.post("/api/episodes/{episode_id}/cleanup-intermediates")
    async def cleanup_episode_intermediates(episode_id: str) -> dict[str, Any]:
        """一键清理本话过程文件，只保留最终成片（Patch v4.11）。"""
        located = view.find_episode(episode_id)
        if located is None:
            raise HTTPException(status_code=404, detail=f"找不到话次：{episode_id}")
        series_id, episode_no = located
        state = job_runner.state_of(series_id, episode_no)
        if state.status in {"queued", "running"}:
            raise HTTPException(
                status_code=409, detail="该话正在后台执行任务，请等待完成后再清理"
            )
        series_dir = view.registry.series_dir(series_id)
        manager = ManifestManager(series_dir, series_id=series_id)
        if manager.load(episode_no).archived:
            raise HTTPException(
                status_code=400, detail="该话已清理过过程文件（仅留成片）"
            )
        try:
            summary = manager.archive_episode_keep_video(episode_no)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "episode_id": episode_id,
            "freed_bytes": summary["freed_bytes"],
            "kept_bytes": summary["kept_bytes"],
            "archived": True,
            "episode": view.episode_item(series_id, episode_no),
        }

    @app.post("/api/series/{series_id}/cleanup-intermediates")
    async def cleanup_series_intermediates(series_id: str) -> dict[str, Any]:
        """批量清理作品下所有已出片话次的过程文件（Patch v4.11）。"""
        if series_id not in view.series_ids():
            raise HTTPException(status_code=404, detail=f"作品未登记：{series_id}")
        catalog = view.catalog(series_id)
        if catalog is None:
            raise HTTPException(status_code=404, detail=f"作品未登记：{series_id}")
        series_dir = view.registry.series_dir(series_id)
        manager = ManifestManager(series_dir, series_id=series_id)
        cleaned_count = 0
        freed_bytes = 0
        skipped_episode_ids: list[str] = []
        for episode in catalog.episodes:
            episode_id = f"{series_id}_ep_{episode.episode_no:03d}"
            state = job_runner.state_of(series_id, episode.episode_no)
            if state.status in {"queued", "running"}:
                skipped_episode_ids.append(episode_id)
                continue
            if manager.load(episode.episode_no).archived:
                skipped_episode_ids.append(episode_id)
                continue
            try:
                summary = manager.archive_episode_keep_video(episode.episode_no)
            except ValueError:
                # 没有成片的话次直接跳过，不阻断整批清理。
                skipped_episode_ids.append(episode_id)
                continue
            cleaned_count += 1
            freed_bytes += int(summary["freed_bytes"])
        return {
            "cleaned_count": cleaned_count,
            "freed_bytes": freed_bytes,
            "skipped_episode_ids": skipped_episode_ids,
        }

    @app.put("/api/episodes/{episode_id}/script")
    async def update_script(episode_id: str, payload: ScriptUpdateRequest) -> dict[str, Any]:
        located = view.find_episode(episode_id)
        if located is None:
            raise HTTPException(status_code=404, detail=f"找不到话次：{episode_id}")
        series_id, episode_no = located
        script_path = view.episode_dir(series_id, episode_no) / "04_script" / "script.json"
        if not script_path.is_file():
            raise HTTPException(status_code=404, detail="该话还没有 Stage 4 剧本")
        script = load_model(NarrationScript, script_path)
        beats = _merge_script_beats(script.beats, payload.beats)
        # Patch v4.13：人工保存也强制简体中文（字幕与送音文本一起转）。
        beats = [_to_simplified_beat(beat) for beat in beats]
        series_dir = view.registry.series_dir(series_id)
        # Patch v4.25：用户勾选的档案同步**先落地**，再算角色名告警——
        # 这样「记为规范名」之后同一句不会再被报成别名写法。
        if payload.sync_character_updates:
            applied = _apply_character_sync(
                series_dir,
                payload.sync_character_updates,
                episode_no=episode_no,
            )
            if applied:
                print(
                    f"[server] 角色档案同步：{applied}", file=sys.stderr
                )
                _reload_config()
        # Patch v4.23：人工保存**默认原样落盘**——用户写的「未来的丈夫」不该
        # 被悄悄改回角色本名。只有显式打开 auto_unify_characters 才归一。
        if payload.auto_unify_characters:
            beats = _canonicalize_script_beats(series_dir, beats)
        script = script.model_copy(update={"beats": beats})
        save_model(script, script_path)
        # Patch v4.6：人工调整停顿后同步进 audio_manifest，只重跑 S6→S7 即生效。
        _sync_pause_to_audio_manifest(view.episode_dir(series_id, episode_no), script.beats)
        readable = view.episode_dir(series_id, episode_no) / "04_script" / "script_readable.md"
        if readable.is_file():
            from stages.s4_scriptwriter import render_readable_script

            readable.write_text(render_readable_script(script), encoding="utf-8")
        # Patch v4.23：附上「角色名写法」只读检查结果，前端据此温和提示。
        character_warnings = _inspect_character_warnings(series_dir, script.beats)
        result: dict[str, Any] = {
            "script": script.model_dump(),
            "job": None,
            "character_warnings": character_warnings,
        }
        if payload.from_stage:
            job = await job_runner.submit_episode(
                series_id,
                episode_no,
                from_stage=payload.from_stage,
                mode=payload.mode,
            )
            result["job"] = job
        return result

    @app.post("/api/episodes/{episode_id}/script/check-characters")
    async def check_script_characters(
        episode_id: str, payload: CharacterCheckRequest
    ) -> dict[str, Any]:
        """只读检查剧本里的角色名写法（Patch v4.23）。

        **不落盘、不改写**：只返回结构化告警，前端据此弹应用内确认框，
        由用户决定「原样保存」还是「统一成规范名」。
        """
        located = view.find_episode(episode_id)
        if located is None:
            raise HTTPException(status_code=404, detail=f"找不到话次：{episode_id}")
        series_id, episode_no = located
        script_path = (
            view.episode_dir(series_id, episode_no) / "04_script" / "script.json"
        )
        if not script_path.is_file():
            raise HTTPException(status_code=404, detail="该话还没有 Stage 4 剧本")
        script = load_model(NarrationScript, script_path)
        if payload.beats:
            beats = _merge_beats_for_check(script.beats, payload.beats)
        else:
            beats = list(script.beats)
        beats = [_to_simplified_beat(beat) for beat in beats]
        warnings = _inspect_character_warnings(
            view.registry.series_dir(series_id), beats
        )
        return {"has_warnings": bool(warnings), "warnings": warnings}


    @app.post("/api/episodes/{episode_id}/script/auto-pacing")
    async def auto_pace_episode_script(episode_id: str) -> dict[str, Any]:
        """Patch v4.6：对已保存剧本执行叙事节奏自动留白，并同步 audio_manifest。"""
        located = view.find_episode(episode_id)
        if located is None:
            raise HTTPException(status_code=404, detail=f"找不到话次：{episode_id}")
        series_id, episode_no = located
        episode_dir = view.episode_dir(series_id, episode_no)
        script_dir = episode_dir / "04_script"
        script_path = script_dir / "script.json"
        if not script_path.is_file():
            raise HTTPException(status_code=404, detail="该话还没有 Stage 4 剧本")
        script = load_model(NarrationScript, script_path)

        # 启用幕：优先读已落盘 storyboard；缺失但有 vision 时内存自动编排，
        # 结果只用于本次节奏判定，绝不落盘覆盖用户的镜头编排。
        storyboard = s3b_scenes.load_storyboard(
            s3b_scenes.storyboard_path_for(episode_dir)
        )
        if storyboard is None:
            vision = _load_episode_vision(episode_dir)
            if vision is not None:
                try:
                    storyboard = s3b_scenes.auto_build_storyboard(episode_id, vision)
                except Exception:  # noqa: BLE001 - 自动编排失败退化为无幕模式，规则 B/C 仍可用
                    storyboard = None
        active_scenes = (
            [scene for scene in storyboard.scenes if scene.enabled]
            if storyboard is not None
            else []
        )

        from stages.s4_scriptwriter import (
            apply_narrative_pacing,
            render_readable_script,
        )

        beat_dicts = [beat.model_dump() for beat in script.beats]
        paced_beats, report = apply_narrative_pacing(
            beat_dicts,
            scenes=active_scenes or None,
            audio_durations_ms=view._audio_durations(episode_dir),
        )
        try:
            updated_script = NarrationScript.model_validate(
                {**script.model_dump(), "beats": paced_beats}
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=400, detail=f"自动节奏留白产出非法剧本：{exc}"
            ) from exc
        save_model(updated_script, script_path)
        readable_path = script_dir / "script_readable.md"
        readable_path.write_text(
            render_readable_script(updated_script), encoding="utf-8"
        )
        synced = _sync_pause_to_audio_manifest(episode_dir, updated_script.beats)
        return {
            "beats": view.beats(series_id, episode_no),
            "summary": {
                "updated_count": report.updated_count,
                "scene_end_count": report.scene_end_count,
                "turn_count": report.turn_count,
                "breath_count": report.breath_count,
                "panel_switch_count": report.panel_switch_count,
                "changed_beat_ids": report.changed_beat_ids,
                "audio_manifest_synced": synced,
            },
        }

    # ------------------------------------------------------------------
    # 单句重配 TTS（Patch v4.12）
    # ------------------------------------------------------------------
    @app.get("/api/episodes/{episode_id}/audio/{beat_id}")
    async def get_episode_beat_audio(episode_id: str, beat_id: str) -> FileResponse:
        """流式回源单句配音 WAV（真实试听）；找不到返回 404，响应不进浏览器缓存。"""
        located = view.find_episode(episode_id)
        if located is None:
            raise HTTPException(status_code=404, detail=f"找不到话次：{episode_id}")
        series_id, episode_no = located
        audio_dir = view.episode_dir(series_id, episode_no) / "05_audio"

        # 优先按音频清单里记录的实际文件名回源（兼容历史后缀 / 安全化文件名）。
        audio_path: Path | None = None
        manifest_path = audio_dir / s5_tts.AUDIO_MANIFEST_FILENAME
        if manifest_path.is_file():
            try:
                for item in s5_tts.load_audio_manifest(audio_dir):
                    if item.beat_id == beat_id:
                        candidate = audio_dir / Path(item.audio_path).name
                        if candidate.is_file():
                            audio_path = candidate
                        break
            except s5_tts.TTSStageError:
                audio_path = None
        if audio_path is None:
            fallback = audio_dir / f"beat_{s5_tts.safe_beat_id(beat_id)}.wav"
            if fallback.is_file():
                audio_path = fallback
        if audio_path is None:
            raise HTTPException(
                status_code=404, detail=f"找不到该句配音：{beat_id}（请先运行 Stage 5）"
            )

        suffix = audio_path.suffix.lower()
        media_type = {".wav": "audio/wav", ".mp3": "audio/mpeg"}.get(
            suffix, "application/octet-stream"
        )
        return FileResponse(
            audio_path,
            media_type=media_type,
            filename=audio_path.name,
            content_disposition_type="inline",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/episodes/{episode_id}/beats/{beat_id}/regenerate-tts")
    async def regenerate_beat_tts(
        episode_id: str, beat_id: str, payload: BeatRegenerateTTSRequest
    ) -> dict[str, Any]:
        """只重配一个 beat 的 TTS（Patch v4.12），不重跑整话 Stage 5。"""
        located = view.find_episode(episode_id)
        if located is None:
            raise HTTPException(status_code=404, detail=f"找不到话次：{episode_id}")
        series_id, episode_no = located
        state = job_runner.state_of(series_id, episode_no)
        if state.status in {"queued", "running"}:
            raise HTTPException(
                status_code=409, detail="该话正在后台执行任务，请等待完成后再重配"
            )
        episode_dir = view.episode_dir(series_id, episode_no)
        series_dir = view.registry.series_dir(series_id)
        manager = ManifestManager(series_dir, series_id=series_id)
        if manager.load(episode_no).archived:
            raise HTTPException(
                status_code=400,
                detail="该话已清理过程文件（仅留成片），无法单句重配，请先从 Stage 1 重跑",
            )
        script_dir = episode_dir / "04_script"
        script_path = script_dir / "script.json"
        if not script_path.is_file():
            raise HTTPException(status_code=400, detail="该话还没有 Stage 4 剧本，请先运行撰稿")
        audio_manifest_path = episode_dir / "05_audio" / s5_tts.AUDIO_MANIFEST_FILENAME
        if not audio_manifest_path.is_file():
            raise HTTPException(
                status_code=400,
                detail="该话还没跑过 Stage 5 配音，请先运行 Stage 5 后再使用单句重配",
            )

        # 可选：先把该句的表单草稿按 PUT /script 同款合并策略落盘并刷新可读稿。
        if payload.beat is not None:
            incoming = payload.beat
            if str(incoming.get("beat_id") or "") != beat_id:
                raise HTTPException(
                    status_code=400, detail="请求体 beat.beat_id 与路径 beat_id 不一致"
                )
            narration_text = str(incoming.get("narration_text") or "").strip()
            if len(narration_text) < 2:
                raise HTTPException(status_code=400, detail="字幕原文去空白后至少需要 2 个字")
            script = load_model(NarrationScript, script_path)
            old_beat = next((beat for beat in script.beats if beat.beat_id == beat_id), None)
            if old_beat is None:
                raise HTTPException(
                    status_code=404, detail=f"剧本里找不到该句：{beat_id}"
                )
            # 单句请求只带部分字段：用旧 beat 的完整快照打底，再让提交值覆盖，
            # 避免 target_panel_ids 等必填键缺失导致 _merge_script_beats 校验失败。
            # 注意 _merge_script_beats 是全量语义（输出跟随 incoming），这里只对
            # 单条合并，再原位替换，其余 beat 与顺序一个不动。
            merged_input = {**old_beat.model_dump(mode="json"), **incoming}
            replacement = _merge_script_beats([old_beat], [merged_input])[0]
            # Patch v4.13：单句重配前也强制简体中文，避免繁体送进 TTS。
            replacement = _to_simplified_beat(replacement)
            merged_beats = [
                replacement if beat.beat_id == beat_id else beat for beat in script.beats
            ]
            script = script.model_copy(update={"beats": merged_beats})
            save_model(script, script_path)
            readable_path = script_dir / "script_readable.md"
            if readable_path.is_file():
                from stages.s4_scriptwriter import render_readable_script

                readable_path.write_text(
                    render_readable_script(script), encoding="utf-8"
                )
            # 停顿改动同步进音频清单（与 PUT /script 同口径），S6 重建即生效。
            _sync_pause_to_audio_manifest(episode_dir, script.beats)

        # 与 JobRunner._stage_tts 完全相同的配置加载，保证单句重配与整话合成同源。
        series_config = load_series_config(series_dir)
        overrides = merge_pronunciation_overrides(
            dict(view.config.pronunciation_overrides),
            dict(series_config.pronunciation_overrides),
        )
        voice_cast = merge_voice_cast(
            view.config.tts.voice_cast, series_config.voice_cast
        )
        series_tts = merge_series_tts(view.config.tts, series_config.tts)
        characters = s5_tts.load_bible_characters(series_dir)

        client = job_runner.make_tts_client()
        try:
            try:
                clip = await s5_tts.resynthesize_single_beat(
                    client,
                    series_dir,
                    episode_no,
                    beat_id,
                    overrides=overrides,
                    default_voice=view.config.tts.default_voice,
                    voice_cast=voice_cast,
                    series_tts=series_tts,
                    characters=characters,
                    ffmpeg_bin=view.config.render.ffmpeg_bin,
                    tts_model=view.config.models.tts,
                )
            except s5_tts.TTSStageError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            close = getattr(client, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 - 关闭失败不影响重配结果
                    pass

        job = None
        if payload.rerun_from_s6:
            try:
                job = await job_runner.submit_episode(
                    series_id,
                    episode_no,
                    from_stage="s6_timeline",
                    mode="full_video",
                )
            except JobError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {
            "episode_id": episode_id,
            "beat_id": beat_id,
            "clip": clip.model_dump(mode="json"),
            "duration_ms": clip.duration_ms,
            "beats": view.beats(series_id, episode_no),
            "job": job,
        }

    # ------------------------------------------------------------------
    # 镜头编排（Storyboard，Patch v4.0 Patch 20）
    # ------------------------------------------------------------------
    def _storyboard_payload(
        storyboard: EpisodeStoryboard,
        episode_id: str,
        series_id: str,
        episode_no: int,
    ) -> dict[str, Any]:
        """组装镜头编排响应：storyboard 本体 + 每格缩略图/摘要/对白索引。"""
        panels_index: dict[str, dict[str, Any]] = {}
        file_by_id = {
            item["panel_id"]: Path(item["image_url"]).name
            for item in view.panels(series_id, episode_no)
        }
        for panel in view._vision_panels(view.episode_dir(series_id, episode_no)):
            filename = file_by_id.get(panel.panel_id, f"{panel.panel_id}.png")
            panels_index[panel.panel_id] = {
                "panel_id": panel.panel_id,
                "image_url": f"/api/episodes/{episode_id}/panels/{filename}",
                "scene_summary": panel.scene_summary,
                "dialogues": [item.model_dump() for item in panel.dialogues],
            }
        payload = storyboard.model_dump()
        payload["panels_index"] = panels_index
        return payload

    @app.get("/api/episodes/{episode_id}/storyboard")
    async def get_storyboard(episode_id: str) -> dict[str, Any]:
        located = view.find_episode(episode_id)
        if located is None:
            raise HTTPException(status_code=404, detail=f"找不到话次：{episode_id}")
        series_id, episode_no = located
        episode_dir = view.episode_dir(series_id, episode_no)
        vision = _load_episode_vision(episode_dir)
        if vision is None:
            raise HTTPException(
                status_code=404, detail="该话还没有 Stage 3 视觉结果，无法编排镜头组"
            )
        storyboard_path = s3b_scenes.storyboard_path_for(episode_dir)
        storyboard = s3b_scenes.load_storyboard(storyboard_path)
        if storyboard is None:
            # 首次打开：自动聚类并落盘，后续人工编辑与重置都基于这份文件。
            # Patch v4.21：这是**规则切幕的占位编排**，标记出来，Stage 4 拿到
            # 后会自动升级成语义切幕一次（前端预览不该把语义切幕绕过）。
            storyboard = s3b_scenes.auto_build_storyboard(episode_id, vision)
            storyboard = storyboard.model_copy(update={"auto_strategy": "rule"})
            s3b_scenes.save_storyboard(storyboard, storyboard_path)
        # Patch v4.20：分镜页不列出纯文字卡（章节标题条 / 字幕条）——它们不是
        # 剧情画面，留在幕里只会被 Stage 6 渲成成片里用户没配过的一帧。
        storyboard = s3b_scenes.strip_text_cards_from_storyboard(storyboard, vision)
        return _storyboard_payload(storyboard, episode_id, series_id, episode_no)

    @app.put("/api/episodes/{episode_id}/storyboard")
    async def save_storyboard(
        episode_id: str, payload: StoryboardUpdateRequest
    ) -> dict[str, Any]:
        located = view.find_episode(episode_id)
        if located is None:
            raise HTTPException(status_code=404, detail=f"找不到话次：{episode_id}")
        series_id, episode_no = located
        episode_dir = view.episode_dir(series_id, episode_no)
        vision = _load_episode_vision(episode_dir)
        if vision is None:
            raise HTTPException(status_code=404, detail="该话还没有 Stage 3 视觉结果")
        valid_panel_ids = {panel.panel_id for panel in vision.panels}
        storyboard_path = s3b_scenes.storyboard_path_for(episode_dir)
        existing_storyboard = s3b_scenes.load_storyboard(storyboard_path)
        scenes = _validate_storyboard_scenes(
            payload.scenes,
            valid_panel_ids,
            existing_scenes=existing_storyboard.scenes if existing_storyboard else None,
        )
        # 人工保存一律标记 source="human"，此后自动流程与 reset 之外绝不覆写。
        storyboard = EpisodeStoryboard(
            episode_id=episode_id, source="human", scenes=scenes
        )
        s3b_scenes.save_storyboard(storyboard, storyboard_path)
        return _storyboard_payload(storyboard, episode_id, series_id, episode_no)

    @app.post("/api/episodes/{episode_id}/storyboard/reset")
    async def reset_storyboard(episode_id: str) -> dict[str, Any]:
        located = view.find_episode(episode_id)
        if located is None:
            raise HTTPException(status_code=404, detail=f"找不到话次：{episode_id}")
        series_id, episode_no = located
        episode_dir = view.episode_dir(series_id, episode_no)
        vision = _load_episode_vision(episode_dir)
        if vision is None:
            raise HTTPException(status_code=404, detail="该话还没有 Stage 3 视觉结果")
        storyboard = s3b_scenes.auto_build_storyboard(episode_id, vision)
        # Patch v4.21：重置同样产出规则切幕的占位编排，标记后 Stage 4 会升级一次。
        storyboard = storyboard.model_copy(update={"auto_strategy": "rule"})
        storyboard_path = s3b_scenes.storyboard_path_for(episode_dir)
        s3b_scenes.save_storyboard(storyboard, storyboard_path)
        # Patch v4.21：重置返回前与 GET 保持同一口径——不列出纯文字卡。
        storyboard = s3b_scenes.strip_text_cards_from_storyboard(storyboard, vision)
        return _storyboard_payload(storyboard, episode_id, series_id, episode_no)

    @app.post("/api/episodes/{episode_id}/storyboard/generate")
    async def generate_from_storyboard(
        episode_id: str, payload: StoryboardGenerateRequest
    ) -> dict[str, Any]:
        located = view.find_episode(episode_id)
        if located is None:
            raise HTTPException(status_code=404, detail=f"找不到话次：{episode_id}")
        if payload.mode not in STORYBOARD_MODES:
            raise HTTPException(
                status_code=400, detail="mode 只支持 phase1_only / full_video"
            )
        series_id, episode_no = located
        episode_dir = view.episode_dir(series_id, episode_no)
        vision = _load_episode_vision(episode_dir)
        if vision is None:
            raise HTTPException(status_code=404, detail="该话还没有 Stage 3 视觉结果")
        storyboard_path = s3b_scenes.storyboard_path_for(episode_dir)
        if payload.scenes:
            # 带 scenes：先按人工编排保存（source="human"），再入队重跑。
            valid_panel_ids = {panel.panel_id for panel in vision.panels}
            existing_storyboard = s3b_scenes.load_storyboard(storyboard_path)
            scenes = _validate_storyboard_scenes(
                payload.scenes,
                valid_panel_ids,
                existing_scenes=existing_storyboard.scenes if existing_storyboard else None,
            )
            storyboard = EpisodeStoryboard(
                episode_id=episode_id, source="human", scenes=scenes
            )
            s3b_scenes.save_storyboard(storyboard, storyboard_path)
        else:
            storyboard = s3b_scenes.load_storyboard(storyboard_path)
            if storyboard is None:
                storyboard = s3b_scenes.auto_build_storyboard(episode_id, vision)
                # Patch v4.21：同上，标记为规则切幕占位，等 Stage 4 升级。
                storyboard = storyboard.model_copy(update={"auto_strategy": "rule"})
                s3b_scenes.save_storyboard(storyboard, storyboard_path)
        try:
            job = await job_runner.submit_episode(
                series_id,
                episode_no,
                from_stage="s4_scriptwriter",
                mode=payload.mode,
            )
        except JobError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "job": job,
            "storyboard": _storyboard_payload(
                storyboard, episode_id, series_id, episode_no
            ),
        }

    # ------------------------------------------------------------------
    # Patch v4.7：作品曲库管理 + 按幕 AI 配乐建议
    # ------------------------------------------------------------------
    BGM_MEDIA_TYPES = {
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
    }

    def _bgm_library_payload(series_id: str) -> dict[str, Any]:
        """组装曲库响应：五档情绪预设 + 作品/全局曲目（带试听 URL）。"""
        series_dir = view.registry.series_dir(series_id)
        tracks = []
        for item in s7_renderer.list_bgm_library(series_dir):
            tracks.append(
                {
                    "filename": item["filename"],
                    "scope": item["scope"],
                    "size_bytes": item["size_bytes"],
                    "url": (
                        f"/api/series/{series_id}/bgm-file/"
                        f"{item['scope']}/{quote(item['filename'])}"
                    ),
                }
            )
        moods = [
            {"mood": mood, **preset}
            for mood, preset in s3b_scenes.BGM_MOOD_PRESETS.items()
        ]
        return {"series_id": series_id, "moods": moods, "tracks": tracks}

    @app.get("/api/series/{series_id}/bgm-library")
    def get_bgm_library(series_id: str) -> dict[str, Any]:
        if series_id not in view.series_ids():
            raise HTTPException(status_code=404, detail=f"作品不存在：{series_id}")
        return _bgm_library_payload(series_id)

    @app.get("/api/series/{series_id}/bgm-file/{scope}/{filename}")
    def get_bgm_file(series_id: str, scope: str, filename: str) -> FileResponse:
        """流式回源一首曲库音频（试听用），scope 仅允许 series/global。"""
        if series_id not in view.series_ids():
            raise HTTPException(status_code=404, detail=f"作品不存在：{series_id}")
        if scope not in ("series", "global"):
            raise HTTPException(status_code=400, detail="scope 只支持 series / global")
        safe_name = Path(filename).name  # 防路径穿越
        if Path(safe_name).suffix.lower() not in BGM_ALLOWED_SUFFIXES:
            raise HTTPException(status_code=400, detail="不支持的音频格式")
        if scope == "series":
            path = (
                view.registry.series_dir(series_id)
                / s7_renderer.ASSETS_DIR_NAME
                / s7_renderer.BGM_DIR_NAME
                / safe_name
            )
        else:
            path = s7_renderer.assets_root() / s7_renderer.BGM_DIR_NAME / safe_name
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"找不到曲目：{safe_name}")
        return FileResponse(
            path,
            media_type=BGM_MEDIA_TYPES.get(
                Path(safe_name).suffix.lower(), "application/octet-stream"
            ),
            filename=safe_name,
            content_disposition_type="inline",
        )

    @app.post("/api/series/{series_id}/bgm-library/upload")
    def upload_bgm_track(series_id: str, payload: BgmUploadRequest) -> dict[str, Any]:
        if series_id not in view.series_ids():
            raise HTTPException(status_code=404, detail=f"作品不存在：{series_id}")
        raw_name = Path(payload.filename or "").name
        if not raw_name:
            raise HTTPException(status_code=400, detail="filename 不能为空")
        suffix = Path(raw_name).suffix.lower()
        if suffix not in BGM_ALLOWED_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"只支持 {', '.join(BGM_ALLOWED_SUFFIXES)} 音频",
            )
        if payload.mood_slot is not None:
            if payload.mood_slot not in s3b_scenes.BGM_MOOD_PRESETS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "mood_slot 必须是五档情绪之一："
                        f"{', '.join(s3b_scenes.BGM_MOOD_PRESETS)}"
                    ),
                )
            target_name = f"{payload.mood_slot}{suffix}"
        else:
            target_name = raw_name
        raw_bytes = _decode_base64_audio(payload.content_base64)
        bgm_dir = (
            view.registry.series_dir(series_id)
            / s7_renderer.ASSETS_DIR_NAME
            / s7_renderer.BGM_DIR_NAME
        )
        bgm_dir.mkdir(parents=True, exist_ok=True)
        (bgm_dir / target_name).write_bytes(raw_bytes)
        return _bgm_library_payload(series_id)

    @app.delete("/api/series/{series_id}/bgm-library/{filename}")
    def delete_bgm_track(series_id: str, filename: str) -> dict[str, Any]:
        """只允许删除作品专属曲目，全局曲库受保护。"""
        if series_id not in view.series_ids():
            raise HTTPException(status_code=404, detail=f"作品不存在：{series_id}")
        safe_name = Path(filename).name
        path = (
            view.registry.series_dir(series_id)
            / s7_renderer.ASSETS_DIR_NAME
            / s7_renderer.BGM_DIR_NAME
            / safe_name
        )
        if not path.is_file():
            raise HTTPException(
                status_code=404, detail=f"作品曲库中找不到曲目：{safe_name}"
            )
        path.unlink()
        return _bgm_library_payload(series_id)

    @app.post("/api/episodes/{episode_id}/storyboard/suggest-bgm")
    def suggest_episode_bgm(episode_id: str) -> dict[str, Any]:
        """对存量话次一键补全各幕 AI 配乐建议（只写 mood/suggestion，不动人工 bgm）。"""
        located = view.find_episode(episode_id)
        if located is None:
            raise HTTPException(status_code=404, detail=f"找不到话次：{episode_id}")
        series_id, episode_no = located
        episode_dir = view.episode_dir(series_id, episode_no)
        storyboard_path = s3b_scenes.storyboard_path_for(episode_dir)
        storyboard = s3b_scenes.load_storyboard(storyboard_path)
        if storyboard is None:
            vision = _load_episode_vision(episode_dir)
            if vision is None:
                raise HTTPException(
                    status_code=404, detail="该话还没有分镜，无法生成配乐建议"
                )
            storyboard = s3b_scenes.auto_build_storyboard(episode_id, vision)
        else:
            vision = _load_episode_vision(episode_dir)
        storyboard = s3b_scenes.suggest_storyboard_bgm(storyboard, vision)
        s3b_scenes.save_storyboard(storyboard, storyboard_path)
        return _storyboard_payload(storyboard, episode_id, series_id, episode_no)

    @app.get("/api/episodes/{episode_id}/panels/{filename}")
    async def get_episode_panel_file(
        episode_id: str,
        filename: str,
        w: int | None = Query(default=None, ge=64, le=800),
    ) -> FileResponse:
        """按文件名回源一话的分镜图（Storyboard 缩略图与识图复核共用）。

        Patch v4.25：可选查询参数 ``w``（64~800）——带上就返回按该宽度
        等比缩小的 JPEG 缩略图（只缩不放，缓存在 ``02_panels/.thumbs/``），
        不带则原样返回高清原图。
        """
        located = view.find_episode(episode_id)
        if located is None:
            raise HTTPException(status_code=404, detail=f"找不到话次：{episode_id}")
        series_id, episode_no = located
        safe_name = Path(filename).name  # 只取文件名，防路径穿越
        suffix = Path(safe_name).suffix.lower()
        if suffix not in PANEL_SUFFIXES:
            raise HTTPException(status_code=404, detail="不支持的图片格式")
        path = view.episode_dir(series_id, episode_no) / "02_panels" / safe_name
        if not path.is_file():
            raise HTTPException(
                status_code=404, detail=f"找不到分镜图片：{safe_name}"
            )
        if w is None:
            return FileResponse(
                path,
                media_type=PANEL_MEDIA_TYPES.get(suffix, "application/octet-stream"),
                filename=safe_name,
                content_disposition_type="inline",
            )
        thumb_path, thumb_type = _panel_thumbnail(path, w)
        return FileResponse(
            thumb_path,
            media_type=thumb_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.patch("/api/episodes/{episode_id}/panels/{panel_id}")
    async def flag_panel(
        episode_id: str, panel_id: str, payload: PanelFlagRequest
    ) -> dict[str, Any]:
        located = view.find_episode(episode_id)
        if located is None:
            raise HTTPException(status_code=404, detail=f"找不到话次：{episode_id}")
        series_id, episode_no = located
        vision_path = view.episode_dir(series_id, episode_no) / "03_vision" / "vision.json"
        if not vision_path.is_file():
            raise HTTPException(status_code=404, detail="该话还没有 Stage 3 视觉结果")
        data = json.loads(vision_path.read_text(encoding="utf-8"))
        panels = data.get("panels") or []
        matched = False
        for panel in panels:
            panel_id_value = str(panel.get("panel_id") or "")
            digits_panel = "".join(ch for ch in panel_id_value if ch.isdigit())
            digits_target = "".join(ch for ch in panel_id if ch.isdigit())
            if panel_id_value == panel_id or (
                digits_panel and digits_panel == digits_target
            ):
                panel["is_transition_blank"] = bool(payload.is_transition_blank)
                matched = True
                break
        if not matched:
            raise HTTPException(status_code=404, detail=f"找不到分镜：{panel_id}")
        vision_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"panel_id": panel_id, "is_transition_blank": bool(payload.is_transition_blank)}

    # ------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------
    @app.get("/api/episodes/{episode_id}/events")
    async def episode_events(episode_id: str) -> EventSourceResponse:
        async def _generator() -> Iterator[dict[str, Any]]:
            async for event in job_runner.bus.stream(episode_id):
                yield {
                    "event": str(event.get("type") or "message"),
                    "data": json.dumps(event, ensure_ascii=False),
                }

        return EventSourceResponse(_generator())

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    @app.get("/api/episodes/{episode_id}/export")
    async def export_episode(episode_id: str, format: str = Query("mp4")):
        located = view.find_episode(episode_id)
        if located is None:
            raise HTTPException(status_code=404, detail=f"找不到话次：{episode_id}")
        series_id, episode_no = located
        episode_dir = view.episode_dir(series_id, episode_no)
        if format == "mp4":
            path = episode_dir / "07_out" / f"{episode_id}.mp4"
            media_type = "video/mp4"
        elif format == "ass":
            path = episode_dir / "06_plan" / "subtitles.ass"
            media_type = "text/x-ssa"
        elif format == "script":
            path = episode_dir / "04_script" / "script_readable.md"
            media_type = "text/markdown; charset=utf-8"
        else:
            raise HTTPException(status_code=400, detail="format 只支持 mp4 / ass / script")
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"文件尚未生成：{path.name}")
        return FileResponse(
            path, media_type=media_type, filename=path.name, content_disposition_type="attachment"
        )

    @app.get("/api/series/{series_id}/export-zip")
    async def export_series_zip(series_id: str) -> StreamingResponse:
        catalog = view.catalog(series_id)
        if catalog is None:
            raise HTTPException(status_code=404, detail=f"作品未登记：{series_id}")
        series_dir = view.registry.series_dir(series_id)
        entries: list[tuple[str, Path]] = []
        for episode in catalog.episodes:
            path = s7_renderer.out_dir_for(series_dir, episode.episode_no) / (
                f"{episode.episode_id}.mp4"
            )
            if path.is_file():
                entries.append((f"{episode.episode_id}.mp4", path))
        if not entries:
            raise HTTPException(status_code=404, detail="该作品还没有已渲染的成片")

        def _stream() -> Iterator[bytes]:
            with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024) as buffer:
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
                    for arcname, path in entries:
                        archive.write(path, arcname)
                buffer.seek(0)
                while True:
                    chunk = buffer.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk

        return StreamingResponse(
            _stream(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{series_id}_videos.zip"'
            },
        )

    # ------------------------------------------------------------------
    # 配置与词典
    # ------------------------------------------------------------------
    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return _config_payload(app.state.config)

    @app.put("/api/config")
    async def put_config(payload: ConfigUpdateRequest) -> dict[str, Any]:
        path = app.state.config_path
        data: dict[str, Any] = {}
        if path.is_file():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        api = dict(data.get("api") or {})
        models = dict(data.get("models") or {})
        render = dict(data.get("render") or {})
        tts = dict(data.get("tts") or {})
        if payload.bailianBaseUrl:
            api["base_url"] = payload.bailianBaseUrl
        if payload.timeoutS is not None:
            api["timeout_s"] = float(payload.timeoutS)
        if payload.maxRetries is not None:
            api["max_retries"] = int(payload.maxRetries)
        if payload.visionModel:
            models["vision"] = payload.visionModel
        if payload.scriptModel:
            models["script"] = payload.scriptModel
        if payload.storyModel:
            # Patch v3.3 Patch 15：4A 故事模型可在 Web 端切换（留空则回退 script）。
            models["story_model"] = payload.storyModel
        if payload.ttsModel:
            models["tts"] = payload.ttsModel
            # Patch v3.4 Patch 16：切换 TTS provider 时原音色可能属于另一家
            # （百炼音色发给 MiniMax 会直接 2013 invalid voice_id），不兼容时
            # 自动换成新模型的推荐旁白音色。
            if not payload.ttsVoice:
                current_voice = str(tts.get("default_voice") or DEFAULT_TTS_VOICE).strip()
                if (
                    current_voice
                    and resolve_voice_for_tts_model(
                        current_voice, tts_model=payload.ttsModel
                    )
                    != current_voice
                ):
                    tts["default_voice"] = recommended_voice_for_tts_model(payload.ttsModel)
        if payload.bgmDuckingDb is not None:
            render["bgm_gain_db"] = float(payload.bgmDuckingDb)
        if payload.ttsVoice:
            # Patch v3.1 Patch 11 A2：默认音色落在 ``tts.default_voice``，
            # 与 ``GET /api/config`` 的回显口径保持一致（保留已有 voice_cast）。
            tts["default_voice"] = payload.ttsVoice
        if api:
            data["api"] = api
        if models:
            data["models"] = models
        if render:
            data["render"] = render
        if tts:
            data["tts"] = tts
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        updated = _reload_config()
        return _config_payload(updated)

    @app.get("/api/pronunciation-rules")
    async def get_pronunciation_rules() -> dict[str, Any]:
        return {"rules": _collect_rules(app.state.config, view)}

    @app.put("/api/pronunciation-rules")
    async def put_pronunciation_rules(payload: PronunciationRulesRequest) -> dict[str, Any]:
        mapping = {
            str(item.get("source") or ""): str(item.get("replacement") or "")
            for item in payload.rules
            if str(item.get("source") or "").strip()
        }
        if payload.scope == "global":
            path = app.state.config_path
            data: dict[str, Any] = {}
            if path.is_file():
                loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            data["pronunciation_overrides"] = mapping
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            # 全局词典写在配置文件里，刷新内存配置后才能立刻回显。
            _reload_config()
        else:
            series_dir = view.registry.series_dir(payload.scope)
            series_dir.mkdir(parents=True, exist_ok=True)
            path = series_dir / "series_config.yaml"
            data = {}
            if path.is_file():
                loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            data["pronunciation_overrides"] = mapping
            path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
        return {"rules": _collect_rules(app.state.config, view)}

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "workspace": str(app.state.config.workspace_root()),
            "series": len(view.series_ids()),
            "time": time.time(),
        }

    return app


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


async def _resolve_catalog(source: str) -> SeriesCatalog:
    """把 URL / ``local://`` / 本地目录解析成作品目录。"""
    from core.http_client import build_client, local_path_from_url

    if source.startswith("local://"):
        return s0_catalog.import_local_catalog(local_path_from_url(source))
    path = Path(source).expanduser()
    if path.exists():
        return s0_catalog.import_local_catalog(path)
    async with build_client() as client:
        return await s0_catalog.fetch_series_catalog(client, source)


def _config_payload(config: AppConfig) -> dict[str, Any]:
    """把后端配置映射成前端 ``StudioConfig``（不返回 API Key 明文）。"""
    return {
        "bailianBaseUrl": config.api.base_url,
        "visionModel": config.models.vision,
        # Patch v4.22：识图模型下拉推荐项（前端「视觉模型」下拉）。
        "visionModelOptions": list(VISION_MODEL_OPTIONS),
        "scriptModel": config.models.script,
        # Patch v3.3 Patch 15：4A 故事模型与 Stage 4 模型下拉推荐项。
        "storyModel": config.models.story_model,
        "storyModelOptions": list(STORY_MODEL_OPTIONS),
        "scriptModelOptions": list(SCRIPT_MODEL_OPTIONS),
        "ttsModel": config.models.tts,
        # Patch v3.4 Patch 16：TTS 模型下拉（百炼 / MiniMax / Edge）。
        "ttsModelOptions": list(TTS_MODEL_OPTIONS),
        # Patch v3.1 Patch 11 A1/A2：默认音色来自 ``config.tts.default_voice``，
        # 没有配置时退回 provider 的系统默认音色（``Elias``）。
        # Patch v3.4 Patch 16：回显按当前 TTS 模型做跨模型兜底（百炼音色发给
        # MiniMax 会直接 2013 invalid voice_id，这里提前换成 MiniMax 音色）。
        "ttsVoice": resolve_voice_for_tts_model(
            config.tts.default_voice or DEFAULT_TTS_VOICE,
            tts_model=config.models.tts,
        ),
        # Patch v3.1 Patch 11 A1：可选音色清单，供前端下拉展示。
        # Patch v3.4 Patch 16：改为完整条目（带 sample_url/sample_ready），
        # 前端才能在全局配置页直接试听百炼音色。
        "ttsVoiceOptions": voice_options_payload(),
        # Patch v3.4 Patch 16：MiniMax 音色清单单独下发，避免两套 provider 混用。
        "minimaxVoiceOptions": minimax_voice_options_payload(),
        "maxImageLongEdge": DEFAULT_MAX_IMAGE_LONG_EDGE,
        "aspectRatio": config.video.aspect_ratio,
        "bgmDuckingDb": config.render.bgm_gain_db,
        "apiKeyEnv": config.api.api_key_env,
        "timeoutS": config.api.timeout_s,
        "maxRetries": config.api.max_retries,
        "workspaceRoot": str(config.workspace_root()),
    }


def _series_tts_payload(
    config: AppConfig, view: WorkspaceView, series_id: str
) -> dict[str, Any]:
    """作品级配音设置 + 已识别角色清单（前端设置页用）。"""
    if view.catalog(series_id) is None:
        raise HTTPException(status_code=404, detail=f"作品未登记：{series_id}")
    series_dir = view.registry.series_dir(series_id)
    series_config = load_series_config(series_dir)
    merged = merge_series_tts(config.tts, series_config.tts)

    characters: list[dict[str, Any]] = []
    bible_path = series_dir / "story_bible.json"
    if bible_path.is_file():
        try:
            bible = load_model(StoryBibleV2, bible_path)
        except Exception:  # noqa: BLE001 - 记忆体损坏时按「没有角色」处理
            bible = None
        if bible is not None:
            for character in bible.characters:
                characters.append(
                    {
                        "name": character.name,
                        "aliases": list(character.aliases or []),
                        "gender": character.gender or "unknown",
                        "traits": character.visual_traits,
                    }
                )

    return {
        "series_id": series_id,
        "narrator": merged.narrator.model_dump(),
        "default_male": merged.default_male.model_dump(),
        "default_female": merged.default_female.model_dump(),
        "character_voices": {
            name: spec.model_dump() for name, spec in merged.character_voices.items()
        },
        "characters": characters,
        "voices": voice_options_payload(),
        # Patch v3.4 Patch 16：MiniMax 28 音色一并下发，前端作品配音面板
        # 才能选到 MiniMax 音色并直接试听（百炼音色发给 MiniMax 会 2013）。
        "minimax_voices": minimax_voice_options_payload(),
    }


def _collect_rules(config: AppConfig, view: WorkspaceView) -> list[dict[str, Any]]:
    """收集全局 + 各作品级的多音字词典。"""
    rules: list[dict[str, Any]] = []
    for source, replacement in (config.pronunciation_overrides or {}).items():
        rules.append(
            {
                "id": f"global_{source}",
                "series_id": None,
                "source": source,
                "replacement": replacement,
                "note": "全局规则",
            }
        )
    for series_id in view.series_ids():
        series_config = load_series_config(view.registry.series_dir(series_id))
        for source, replacement in (series_config.pronunciation_overrides or {}).items():
            rules.append(
                {
                    "id": f"{series_id}_{source}",
                    "series_id": series_id,
                    "source": source,
                    "replacement": replacement,
                    "note": f"{series_id} 专属规则",
                }
            )
    return rules


def main() -> None:  # pragma: no cover - 手动启动入口
    """``python -m web.server``：启动控制台后端。

    监听地址与端口可用环境变量覆盖（默认 127.0.0.1:8123）：
    ``W2V_HOST`` / ``W2V_PORT``。前端 ``vite.config.ts`` 的 proxy 默认指向同一端口。
    工作区根目录可用 ``W2V_WORKSPACE`` 覆盖（默认取 ``config.yaml`` 的 ``workspace.root``）。
    """
    import os

    import uvicorn

    host = os.environ.get("W2V_HOST", "127.0.0.1")
    port = int(os.environ.get("W2V_PORT", "8123"))
    workspace = os.environ.get("W2V_WORKSPACE") or None
    uvicorn.run(create_app(workspace=workspace), host=host, port=port)


if __name__ == "__main__":  # pragma: no cover - 手动执行入口
    main()
