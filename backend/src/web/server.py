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
``GET /api/episodes/{eid}/events``            SSE 实时推送阶段状态与日志
``GET /api/episodes/{eid}/export``            下载成片 / 外挂字幕 / 文稿
``GET /api/series/{sid}/export-zip``          批量打包成片为 ZIP
``GET/PUT /api/config``                       全局配置读写
``GET/PUT /api/pronunciation-rules``          多音字词典读写（全局 + 作品级）
===========================  ==========================================
"""

from __future__ import annotations

import json
import re
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from core.config import (
    AppConfig,
    ConfigError,
    SeriesTTSConfig,
    WorkspaceConfig,
    assets_root,
    find_config_path,
    load_config,
    load_env_file,
    load_series_config,
    merge_series_tts,
    recommended_voice_for_tts_model,
    resolve_voice_for_tts_model,
)
from core.manifest import ManifestManager, infer_stage_artifacts, infer_stage_status
from core.schemas import (
    ArcSummary,
    CharacterProfile,
    DialogueBubble,
    NarrationScript,
    PlotThread,
    ScriptBeat,
    SeriesCatalog,
    StageStatus,
    StoryBibleV2,
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
from stages import s0_catalog, s5_tts, s6_timeline, s7_renderer
from web.job_runner import STAGE_DEFS, EpisodeState, JobError, JobRunner

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
STORY_MODEL_OPTIONS: tuple[str, ...] = (
    "qwen4-max",
    "qwen3.8-max",
    "qwen3.7-plus",
    "qwen-plus",
)
SCRIPT_MODEL_OPTIONS: tuple[str, ...] = (
    "qwen3.7-plus",
    "qwen-plus",
    "qwen3.8-max",
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


class ScriptUpdateRequest(BaseModel):
    """``PUT /api/episodes/{ep_id}/script`` 请求体。"""

    beats: list[dict[str, Any]] = Field(default_factory=list)
    from_stage: str | None = None
    mode: str = "full_video"


#: 前端保存脚本时不会回传这些字段（由 Stage 4 产出、前端只读展示）。
#: 客户端未提交时按 ``beat_id`` 从旧剧本继承，避免人工改一句字幕就把角色与音效标注抹掉。
PRESERVED_BEAT_FIELDS: tuple[str, ...] = (
    "speaker_role",
    "sfx_cue",
    "focus_hint",
    # Patch v3.2 Patch 12：说话人性别（前端还没回传该字段，别被前端保存抹掉）
    "speaker_gender",
)

#: 上面这些字段里允许显式置 ``None``（清空）的，其余字段 ``None`` 非法，按「未提供」处理。
NULLABLE_BEAT_FIELDS: frozenset[str] = frozenset({"sfx_cue"})

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
            "viewer_url": episode.viewer_url if episode else "",
            "is_locked": bool(episode.is_locked) if episode else False,
            "selected": False,
            "current_stage": current_stage or "s0_catalog",
            "status": status,
            "video_duration_ms": self.video_duration_ms(episode_dir),
            "stages": stages,
            "panels": panels,
            "beats": beats,
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

    def video_duration_ms(self, episode_dir: Path) -> int:
        timeline_path = episode_dir / "06_plan" / "timeline.json"
        if not timeline_path.is_file():
            return 0
        try:
            data = json.loads(timeline_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 时间轴损坏时按 0 处理
            return 0
        return int(data.get("total_duration_ms") or 0)

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
                "sfx_cue": beat.sfx_cue,
                "focus_hint": beat.focus_hint,
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
        return {
            "voice": assignment.spec.voice,
            "speed": assignment.spec.speed,
            "source": assignment.source,
            "label": assignment.label,
            "role": assignment.role,
            "gender": assignment.gender,
        }

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
        """重新读取磁盘配置并刷新内存态（工作区根目录保持不变）。"""
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
        script = script.model_copy(update={"beats": beats})
        save_model(script, script_path)
        readable = view.episode_dir(series_id, episode_no) / "04_script" / "script_readable.md"
        if readable.is_file():
            from stages.s4_scriptwriter import render_readable_script

            readable.write_text(render_readable_script(script), encoding="utf-8")
        result: dict[str, Any] = {"script": script.model_dump(), "job": None}
        if payload.from_stage:
            job = await job_runner.submit_episode(
                series_id,
                episode_no,
                from_stage=payload.from_stage,
                mode=payload.mode,
            )
            result["job"] = job
        return result

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
