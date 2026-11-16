# Webtoon2Video 自动化漫画解说视频生成系统 — 技术路线与工程开发实施方案（v2.4 竖屏定型与导出版）

> **文档用途**：本规格书用于指导 **`minimax-3.1-flash`** 进行端到端开发。  
> **核心升级（v2.4）**：在 v2.3 多作品隔离与 `StoryBible v2` 四层记忆体基础上，**锁定仅输出 `9:16`（`1080×1920`）竖屏短视频**以完整保留条漫构图，并补齐**成片本地集中归档、浏览器单话/打包导出及 CLI 导出规范**。

---

## 0. 给 `minimax-3.1-flash` 的开发执行总则（Agent Execution Protocol）

1. **分期聚焦，严禁跳步**：先完成 **Phase 1（`M1-T1 ~ M3-T3` + `M6-T1a`）** 跑通本地下载 + 百炼视觉/撰稿出稿；再推进 **Phase 2（`qwen3-tts-flash` + FFmpeg + FastAPI 对接现有 React 控制台原型）**。
2. **一次只执行一个原子任务（Single-Task Rule）**：每次只编写 **1 个核心模块 + 1 个单元测试文件**。
3. **契约绝对锁定（Immutable Schemas）**：`src/core/schemas.py` 锁定后，后续严禁私自改动字段名。
4. **六条工程铁律**：
   - 下载 WEBTOON 图片必须携带 `Referer: https://www.webtoons.com/`。
   - 禁止把几十张切片拼成超大图直接送给视觉模型，必须先经本地 Stage 2 滑窗切图（`height <= 2400px`）。
   - **多部漫画并行 + 部内串行**：不同 `series_id` 之间彻底并发互不阻塞；同一部漫画内 Stage 1~3 并发，**Stage 4 必须按 `episode_no` 升序加部级互斥锁串行**。
   - **重跑时间旅行隔离**：重跑第 $K$ 话 Stage 4 时，**强制读取第 $K-1$ 话的只读快照 `bible_snapshots/ep_{K-1}.json`**，严禁把第 $K+1$ 话的后续剧透混入。
   - **字幕与读音双文本分离**：多音字同音字替换只能进入 `tts_text` / `spoken_text`，**严禁改写用于屏幕 ASS 字幕的 `narration_text`**。
   - 所有百炼调用统一读取环境变量 `DASHSCOPE_API_KEY`。

---

## 1. 多部漫画隔离架构与四层增强跨话记忆库（v2.3 核心设计）

### 1.1 多部漫画（Multi-Series）文件隔离与调度拓扑

```text
workspace/
├── index.json                                # 全局作品索引表（列出所有已导入漫画）
├── wt_88201/                                 # 作品 A 独立工作区
│   ├── catalog.json                          # 作品 A 目录
│   ├── series_config.yaml                    # 作品 A 专属覆盖（专属声线/BGM/多音字词典）
│   ├── story_bible.json                      # 作品 A 当前最新四层记忆体
│   ├── bible_snapshots/                      # 只读按话历史快照（防重跑剧透）
│   │   ├── ep_001.json
│   │   └── ep_002.json
│   └── episodes/
│       ├── ep_001/{01_raw,02_panels,03_vision,04_script,05_audio,06_plan,07_out}
│       └── ep_002/...
└── wt_99104/                                 # 作品 B 独立工作区（与作品 A 完全并发隔离）
    ├── catalog.json
    ├── story_bible.json
    └── ...
```

- **调度器并发法则**：后台维护 `series_locks: dict[str, asyncio.Lock]`。作品 A 与作品 B 的 Stage 0~7 **完全并行**；仅在执行同一部漫画的 Stage 4 时获取该作品的 `series_locks[series_id]`，保证 `StoryBible` 写入无竞态。

### 1.2 为什么需要增强记忆体？四层记忆架构（`StoryBible v2`）

单靠一个 `400 字 rolling_summary` 在连载到第 30~100 话时会出现**“传话筒雪崩遗忘”**（第 1 话的关键伏笔被不断压缩抹掉）和**“角色重跑污染”**。本方案升级为四层结构：

| 记忆层级 | 解决的痛点 | 工程机制 |
| :--- | :--- | :--- |
| **Layer 1：人物实体图谱 (`CharacterProfile`)** | 角色改名、戴面具、外貌成长导致 Stage 3 脸盲重名 | 记录 `name`、`aliases`（别名列表）、`visual_traits`、`current_status`。**同时注入 Stage 3（`qwen3-vl-plus`）与 Stage 4（`qwen-plus`）**，让识图阶段就能直接认出老角色。 |
| **Layer 2：分层摘要 (`ArcSummary` + `recent_digests`)** | 滚动摘要越滚越丢细节，全塞进 Prompt 又爆 Token | 保留 **最近 3 话完整详摘**（滑动窗口）+ **每 10 话/每篇章归档一条不可覆盖的 `ArcSummary`**，长短记忆兼顾且 Token 恒定。 |
| **Layer 3：伏笔追踪器 (`PlotThread`)** | 第 2 话埋的坑到第 60 话才揭晓，滚动摘要早已忘光 | 单独记录 `open` 状态的未回收悬念；Stage 4 撰稿时注入所有 `open` 伏笔，一旦当话回收则标记 `resolved` 并释放上下文。 |
| **Layer 4：按话快照时间旅行 (`bible_snapshots/`)** | 重跑第 3 话时误用了第 10 话才产生的新设定（剧透倒灌） | 每话 Stage 4 结束落盘只读快照 `ep_{no:03d}.json`；重跑第 $K$ 话强制回退读取 `ep_{K-1}.json`；辅以 `episodes_index` 关键词按需召回久未出场的旧角色历史。 |

---

## 2. 核心技术选型矩阵（阿里云百炼 Qwen 全栈）

| 阶段 | 选定方案 | 核心理由与工程细节 |
| :--- | :--- | :--- |
| **Stage 0 & 1 采集** | `httpx` (HTTP/2 异步) + `BeautifulSoup4` / `lxml` | 服务端直出 HTML；`Referer` 防盗链请求头并发拉取；支持 `local://` 本地目录导入。 |
| **Stage 2 切图** | OpenCV/NumPy 滑窗拼接 + 行标准差/差分投影 + Canny 兜底 | 零 GPU 依赖；避开跨切片气泡被拦腰切断，切图后大幅提升百炼视觉准确率。 |
| **Stage 3 识图** | 百炼 **`qwen3-vl-plus`**（密集文字可选 `qwen-vl-max`） | `compatible-mode/v1` + Base64 Data URL（长边 `<= 1280px`），**注入 Layer 1 角色表**防止人脸识别混淆。 |
| **Stage 4 撰稿** | 百炼 **`qwen-plus`**（或 `qwen3-max`） | 结合 `StoryBible v2` 产出短句 `ScriptBeat`（10~28 字）与自然语言 `emotion_instruction`。 |
| **Stage 5 TTS** | 百炼 **`qwen3-tts-flash`**（进阶 `qwen-audio-3.1-tts-plus`） | 原生支持 `input.instruction` 情绪控制；叠加全局+作品级 `pronunciation_overrides` 双文本替换，**零字幕错字**。 |
| **Stage 6 & 7 视频** | `FFmpeg` `filter_complex` + `ASS` 字幕 | **锁定 `9:16`（`1080×1920`, 30fps）**：底层原图高斯模糊铺底、顶层居中保留完整分镜运镜；Audio-First 对齐 + BGM 闪避输出 MP4（并同步复制至 `exports/`）。 |
| **Web 控制台** | **React 19 + Tailwind v4（原型已定稿）** + FastAPI (SSE) | 原型已覆盖**多作品书架切换、四层记忆库可视化、分镜/多音字审核、9:16 成片预览与一键导出（MP4 / ASS / 批量 ZIP）**。 |

---

## 3. 核心数据契约（`src/core/schemas.py` v2.3 锁定版）

```python
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field


class CameraMotion(str, Enum):
    STATIC = "static"
    ZOOM_IN = "zoom_in"
    ZOOM_OUT = "zoom_out"
    PAN_DOWN = "pan_down"
    PAN_UP = "pan_up"
    SHAKE = "shake"


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class SeriesIndexItem(BaseModel):
    series_id: str                        # "wt_88201"
    series_title: str
    list_url: str
    total_episodes: int = 0
    done_episodes: int = 0


class EpisodeCatalogItem(BaseModel):
    episode_no: int
    episode_id: str                       # "wt_88201_ep_001"
    title: str
    viewer_url: str
    publish_date: str = ""
    is_locked: bool = False


class SeriesCatalog(BaseModel):
    series_id: str
    series_title: str
    list_url: str
    episodes: list[EpisodeCatalogItem]


class EpisodeMeta(BaseModel):
    episode_id: str
    source_url: str | None = None
    title_name: str = ""
    episode_title: str = ""
    raw_images: list[str]
    image_width: int
    total_raw_slices: int


class PanelMeta(BaseModel):
    panel_id: str
    image_path: str
    width: int
    height: int
    aspect_ratio: float
    source_slice_range: tuple[int, int]
    is_tall_panel: bool
    edge_density: float = 0.0


class DialogueBubble(BaseModel):
    speaker_guess: str
    text: str
    emotion: Literal["calm", "happy", "angry", "sad", "shocked", "whisper", "tense"] = "calm"


class PanelVisionInfo(BaseModel):
    panel_id: str
    scene_summary: str
    dialogues: list[DialogueBubble] = Field(default_factory=list)
    sfx_list: list[str] = Field(default_factory=list)
    focus_region: tuple[float, float, float, float] | None = None
    is_transition_blank: bool = False


class VisionWindowOutput(BaseModel):
    panels: list[PanelVisionInfo]


class CharacterProfile(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    visual_traits: str
    current_status: str
    first_seen_ep: int
    last_seen_ep: int


class PlotThread(BaseModel):
    thread_id: str
    title: str
    description: str
    introduced_ep: int
    status: Literal["open", "resolved"] = "open"
    resolved_ep: int | None = None


class ArcSummary(BaseModel):
    arc_name: str
    ep_range: tuple[int, int]
    summary: str


class EpisodeDigest(BaseModel):
    episode_no: int
    one_line_hook: str
    summary: str
    keywords: list[str] = Field(default_factory=list)
    characters_present: list[str] = Field(default_factory=list)


class StoryBibleV2(BaseModel):
    series_id: str
    series_title: str
    narration_style: str = "热血悬疑漫评风格，节奏紧凑，口语化有代入感"
    characters: list[CharacterProfile] = Field(default_factory=list)
    plot_threads: list[PlotThread] = Field(default_factory=list)
    arc_summaries: list[ArcSummary] = Field(default_factory=list)
    episodes_index: list[EpisodeDigest] = Field(default_factory=list)
    pronunciation_overrides: dict[str, str] = Field(default_factory=dict)
    snapshots_ep_nos: list[int] = Field(default_factory=list)


class ScriptBeat(BaseModel):
    beat_id: str
    narration_text: str = Field(..., min_length=2, max_length=120)
    tts_text: str | None = None
    emotion_instruction: str = "用自然、流畅且有代入感的漫评解说语气说出"
    speaker_role: str = "narrator"
    target_panel_ids: list[str] = Field(..., min_length=1)
    motion: CameraMotion = CameraMotion.ZOOM_IN
    pause_after_ms: int = Field(default=250, ge=0, le=2000)


class NarrationScript(BaseModel):
    episode_id: str
    hook_title: str
    beats: list[ScriptBeat]
    episode_digest: EpisodeDigest
    character_updates: list[CharacterProfile] = Field(default_factory=list)
    new_plot_threads: list[PlotThread] = Field(default_factory=list)
    resolved_thread_ids: list[str] = Field(default_factory=list)


class WordTimestamp(BaseModel):
    text: str
    start_ms: int
    end_ms: int


class AudioClipMeta(BaseModel):
    beat_id: str
    audio_path: str
    duration_ms: int
    pause_after_ms: int
    word_timestamps: list[WordTimestamp] = Field(default_factory=list)


class TimelineScene(BaseModel):
    scene_index: int
    beat_id: str
    panel_id: str
    image_path: str
    start_ms: int
    end_ms: int
    duration_ms: int
    motion: CameraMotion
    focus_region: tuple[float, float, float, float] | None = None


class SubtitleLine(BaseModel):
    index: int
    beat_id: str
    text: str
    start_ms: int
    end_ms: int


class RenderTimeline(BaseModel):
    episode_id: str
    aspect_ratio: Literal["9:16"] = "9:16"
    width: int = 1080
    height: int = 1920
    fps: int = 30
    total_duration_ms: int
    scenes: list[TimelineScene]
    audios: list[AudioClipMeta]
    subtitles: list[SubtitleLine]


def save_model(model: BaseModel, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")


def load_model(model_cls: type[BaseModel], path: Path):
    return model_cls.model_validate_json(path.read_text(encoding="utf-8"))
```

---

## 4. 核心算法与记忆管理实现规范

### 4.1 `StoryBibleManager` 记忆体组装与压缩算法（`src/core/memory.py`）
1. **为 Stage 3 视觉抽取提供角色上下文**：提取 `story_bible.characters` 中 `name + aliases + visual_traits` 作为参考名单注入 `vision_extract.md`，让 `qwen3-vl-plus` 准确识别老面孔。
2. **为 Stage 4 撰稿组装紧凑 Prompt 上下文（Token 预算恒定）**：
   - 强制加载 `ep_{episode_no - 1}` 只读快照（若存在）；
   - 注入：`arc_summaries`（全部历史篇章摘要）+ `episodes_index[-3:]`（近 3 话详摘）+ `open` 状态的 `plot_threads` + 按当前话视觉关键词召回的至多 2 条远期历史 `EpisodeDigest`；
   - Stage 4 结束后按 `character_updates`、`new_plot_threads`、`resolved_thread_ids` 合并更新并保存新快照 `bible_snapshots/ep_{no:03d}.json`；每满 10 话自动调用 `qwen-plus` 压缩归档一条 `ArcSummary`。

### 4.2 Stage 0~7 流水线要点
- **Stage 0 & 1**：`s0_catalog.py` 维护全局 `workspace/index.json` 与作品 `catalog.json`；`s1_crawler.py` 带 `Referer` 并发下载图片至 `episodes/ep_XXX/01_raw/`。
- **Stage 2**：滑窗拼接 + `(row_std < 3.0) & (row_diff < 2.0)` 切割 + `Canny` 超长兜底。
- **Stage 3 & 4**：百炼 `qwen3-vl-plus`（长边 `<= 1280px`）+ `qwen-plus` 生成 `NarrationScript` 与 `script_readable.md`。
- **Stage 5~7**：`qwen3-tts-flash` 透传 `emotion_instruction` 与多音字双文本替换 $\rightarrow$ FFmpeg `silenceremove` $\rightarrow$ Audio-First ASS 编译 $\rightarrow$ **固定 `9:16` (`1080×1920`)** 并行运镜渲染与 BGM 闪避输出。

### 4.3 成片画幅锁定与三种导出方式（v2.4 新增）
- **为何仅兼容 `9:16`**：WEBTOON 为竖向条漫，若强行适配 `16:9` 横屏会导致上下严重裁切（丢失画面全貌与对话气泡）或左右出现大面积无效黑边/虚化。锁定 `1080×1920` 可原生贴合抖音/快手/B 站竖屏/YouTube Shorts 并保证全格构图完整。
- **成片导出三条路径**：
  1. **本地双份落盘**：Stage 7 完成后自动输出 `workspace/{series_id}/episodes/ep_{no:03d}/07_out/{episode_id}.mp4`，并可选同步复制一份到汇总目录 `workspace/{series_id}/exports/{episode_id}_{title}.mp4`，方便直接拖入剪辑软件或上传平台。
  2. **Web 控制台一键下载**：单话审核台与目录页提供「导出 MP4 / 外挂字幕 (.ass) / 解说稿 (.md)」与「打包导出成片 (.zip)」按钮，通过 FastAPI `FileResponse` / 流式 ZIP 下载到浏览器默认下载目录。
  3. **CLI 导出命令**：`w2v export --series wt_88201 --episodes 1-5 --out ./output_videos` 将指定话次成片按可读文件名批量拷贝到目标文件夹。

---

## 5. Web 控制台原型与 FastAPI 接口契约（Milestone 7）

前端原型（`src/App.tsx`, `CatalogView.tsx`, `EpisodeStudioView.tsx`, `MemoryView.tsx`, `ConfigDictView.tsx`）已定稿。FastAPI 后端（`src/web/server.py`）只需补齐以下端点即可与原型无缝对接：

| 方法与路径 | 功能说明 |
| :--- | :--- |
| `GET /api/series` & `POST /api/series` | 列出全局书架 `workspace/index.json` / 导入新漫画 URL 并运行 Stage 0 |
| `GET /api/series/{series_id}` | 返回目录、`StoryBibleV2` 及各话 `manifest` 状态 |
| `PUT /api/series/{series_id}/memory` | 人工编辑保存角色图谱、伏笔状态（open/resolved）与叙事基调 |
| `POST /api/jobs/batch` | 按作品独立下发并发任务（跨作品并发，同作品 Stage 4 串行加锁） |
| `POST /api/episodes/{ep_id}/rerun` | 从指定步骤断点重跑（若重跑 Stage 4 自动回退读取前序快照） |
| `PUT /api/episodes/{ep_id}/script` | 保存人工修改的 `beats`（含 `tts_text` 与 `emotion_instruction`）并可选从 Stage 5 重跑 |
| `PATCH /api/episodes/{ep_id}/panels/{panel_id}` | 标记/取消 `is_transition_blank` |
| `GET /api/episodes/{ep_id}/events` | SSE 实时推送 Stage 状态与终端日志 |
| `GET /api/episodes/{ep_id}/export` | 查询参数 `format=mp4|ass|script`，通过 `FileResponse` 下载成片、外挂字幕或文稿 |
| `GET /api/series/{series_id}/export-zip` | 将该作品已完成 `s7_renderer` 的所有 `9:16` 成片流式打包为 `.zip` 下载 |
| `GET/PUT /api/config` & `/api/pronunciation-rules` | 读写全局/作品级配置与多音字纠偏词典 |

---

## 6. WBS 分期开发任务拆解表

### 第一步必做清单（Phase 1：多作品本地下载 + 百炼理解 + 四层记忆体）

| Task ID | 前置依赖 | 目标文件 | 核心任务与验收标准 |
| :--- | :--- | :--- | :--- |
| **M1-T1** | 无 | `pyproject.toml`, `config.example.yaml`, `pronunciation_dict.example.yaml`, `src/core/config.py` | 初始化后端骨架与配置加载。 |
| **M1-T2** | M1-T1 | `src/core/schemas.py`, `tests/test_schemas.py` | 落盘第 3 节 v2.3 锁定版 Pydantic 模型。 |
| **M1-T3** | M1-T2 | `src/core/manifest.py`, `src/core/memory.py`, `tests/test_memory.py` | 实现 `ManifestManager` 与 `StoryBibleManager`（四层记忆、快照回退、关键词召回、每 10 话归档）。 |
| **M2-T0** | M1-T2 | `src/stages/s0_catalog.py`, `tests/test_s0_catalog.py` | 多作品 `workspace/index.json` 注册表、翻页抓取与区间解析。 |
| **M2-T1** | M1-T2 | `src/stages/s1_crawler.py`, `tests/test_s1_crawler.py` | **仅本地下载图片**：带 `Referer` 并发拉取至 `01_raw/` 并验损。 |
| **M2-T2** | M2-T1 | `src/stages/s2_segmenter.py`, `tests/test_s2_segmenter.py` | 本地滑窗纵拼、行方差切割、Canny 兜底。 |
| **M3-T1** | M1-T1 | `src/providers/llm_base.py`, `src/providers/llm_bailian.py` | 阿里云百炼 `OpenAI` 兼容客户端（Base64 缩放、自愈重试）。 |
| **M3-T2** | M3-T1, M2-T2, M1-T3 | `src/stages/s3_vision.py`, `prompts/vision_extract.md` | 调用 `qwen3-vl-plus`（注入 Layer 1 角色列表防脸盲）执行滑窗抽取。 |
| **M3-T3** | M3-T1, M3-T2 | `src/stages/s4_scriptwriter.py`, `prompts/script_narrate.md` | 调用 `qwen-plus` 读取快照记忆生成剧本，更新角色/伏笔/摘要并写快照。 |
| **M6-T1a** | M2~M3 | `src/cli.py`（Phase 1 命令） | 实现 `w2v download` 与 `w2v understand`（支持多部漫画并发 + 部内 Stage 4 串行锁）。 |

### 第二步扩展清单（Phase 2：Qwen TTS + 视频渲染 + 对接 Web 原型）

| Task ID | 前置依赖 | 目标文件 | 核心任务描述 |
| :--- | :--- | :--- | :--- |
| **M4-T1** | M1-T2 | `src/providers/tts_base.py`, `tts_qwen.py`, `tts_edge.py` | 封装百炼 `qwen3-tts-flash`（`instruction` 透传 + SSML）与 `edge-tts` 兜底。 |
| **M4-T2** | M4-T1, M3-T3 | `src/stages/s5_tts.py`, `tests/test_s5_tts.py` | 全局+作品级多音字词典替换（不污染字幕）、并发合成与静音裁剪。 |
| **M5-T1** | M4-T2 | `src/stages/s6_timeline.py`, `tests/test_s6_timeline.py` | Audio-First 权重分配、30fps 帧对齐与 `subtitles.ass`。 |
| **M5-T2** | M5-T1 | `src/stages/s7_renderer.py`, `tests/test_s7_renderer.py` | FFmpeg 并行运镜、`concat`、BGM 闪避与 ASS 烧录。 |
| **M6-T1b** | M5-T2 | `src/cli.py`, `tests/test_pipeline_e2e.py` | `w2v run` 全链路命令、`w2v export` 批量导出与端到端离线集成测试。 |
| **M7-T1 & T2** | M6-T1b | `src/web/server.py`, `src/web/job_runner.py` & `src/App.tsx` 对接 | FastAPI 多作品并发调度 + SSE 推送 + MP4/ASS/ZIP 导出端点对接已定稿前端原型。 |

---

## 7. 场景一（Coding Agent + `minimax-3.1-flash`）逐轮实操步骤

1. **第 1 轮（骨架、契约与四层记忆体：`M1-T1 ~ M1-T3`）**：
   > 「请阅读根目录 `WEBTOON2VIDEO_DEV_SPEC.md`。当前只开发 Phase 1，不要修改现有前端原型文件。请先完成 `M1-T1`、`M1-T2`、`M1-T3`（含 `src/core/memory.py` 的四层记忆组装、按话快照回退与关键词召回逻辑）及对应单元测试，运行 `pytest` 全部通过。」
2. **第 2 轮（多作品目录与本地图片下载：`M2-T0 ~ M2-T1`）**：
   > 「完成 `M2-T0` 和 `M2-T1`（多作品 `workspace/index.json` 注册、`s0_catalog.py` 与 `s1_crawler.py` 带 `Referer` 仅本地图片下载），使用 `respx` 编写离线测试并运行 `pytest` 通过。」
3. **第 3 轮（本地防切破智能分镜：`M2-T2`）**：
   > 「完成 `M2-T2`（实现 `s2_segmenter.py`），在 `tests/test_s2_segmenter.py` 中构造跨切片实心圆测试图断言不被切破，运行 `pytest` 通过。」
4. **第 4 轮（百炼 Qwen 识图、撰稿与跨话记忆联动：`M3-T1 ~ M3-T3` & `M6-T1a`）**：
   > 「完成 `M3-T1`、`M3-T2`（注入 Layer 1 角色列表防脸盲）、`M3-T3`（快照回退与伏笔追踪更新）和 `M6-T1a`（支持多作品并发+部内串行锁的 CLI），用 Mock 测试跑通 `pytest`。」
5. **第 5 轮（Qwen TTS 情绪/多音字与 FFmpeg 合成：`M4-T1 ~ M6-T1b`）**：
   > 「Phase 1 出稿验收通过。现在开始 Phase 2，请依次完成 `M4-T1`、`M4-T2`（`qwen3-tts-flash` 情绪指令与双文本多音字替换）、`M5-T1`、`M5-T2` 和 `M6-T1b`，跑通 `pytest tests/test_pipeline_e2e.py`。」
6. **第 6 轮（FastAPI 后端对接现有 Web 原型：`M7-T1 & M7-T2`）**：
   > 「完成 `M7-T1` 与 `M7-T2`：按 `WEBTOON2VIDEO_DEV_SPEC.md` 第 5 节实现 `src/web/server.py` 与 `job_runner.py`（跨作品并发 + 部内 Stage 4 串行锁），并将现有前端原型 `src/App.tsx` 中的 Mock 数据替换为真实 API 与 SSE 对接。」
