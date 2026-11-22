# Webtoon2Video

把 LINE WEBTOON 条漫自动改编成 1080×1920 抖音竖屏解说视频的本地流水线：
抓取漫画 → 切图 → 视觉理解 → Story-First 故事撰稿 → TTS 配音 → 时间轴 → FFmpeg 合成，
带 FastAPI 控制台与 React 前端，全部可在单机离线（除模型 API 外）运行。

## 目录结构

```
.
├── src/                 # 前端原型（React 19 + Vite + Tailwind，已定稿，勿随意改动）
├── backend/             # Python 后端（src-layout，导入根为 backend/src）
│   ├── src/core/        # 配置、Schema、清单、记忆（Story Bible v2）、HTTP 客户端
│   ├── src/stages/      # s0~s7 八个阶段
│   ├── src/providers/   # 百炼 LLM / TTS 等模型适配
│   ├── src/web/         # FastAPI 服务与 SSE 任务编排
│   ├── prompts/         # 4A 故事稿 / 4B 解说稿等提示词模板
│   ├── tests/           # pytest 测试套件（880 条，分级跑法见 AGENTS.md）
│   ├── assets/          # 运行时素材（如片尾版权页 dHash 黑名单模板）
│   └── pyproject.toml
├── WEBTOON2VIDEO_DEV_SPEC.md   # 总开发规范（六条铁律与工作区拓扑）
├── PATCH_v2.5_OPTIMIZATION.md
├── PATCH_v2.6_ALIGNMENT_AND_STORY.md
├── PATCH_v3.0_DOUYIN_REBUILD.md
└── .env.example
```

后端各阶段与里程碑细节见 [`backend/README.md`](backend/README.md)。
补丁规格文档见仓库根 `PATCH_*.md`（共 11 份，最新为
[`PATCH_v4.12_SINGLE_BEAT_AND_VOICE_LABELS.md`](PATCH_v4.12_SINGLE_BEAT_AND_VOICE_LABELS.md)，
覆盖 Patch v4.12 / v4.13 / v4.14）。

## 环境要求

- Python ≥ 3.11（开发实测 3.13）
- Node.js ≥ 20
- 本机可用的 `ffmpeg`（需含 libass 之外的常用滤镜；ASS 字幕在无 libass 时自动改走 Pillow 烧录）
- 阿里云百炼（DashScope）API Key

## 快速开始

### 1. 配置密钥

```bash
cp .env.example .env
# 编辑 .env，填入 DASHSCOPE_API_KEY
```

### 2. 后端

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

pytest                      # 全量测试（串行 880 条，约 1 分钟）
pytest -n auto -m "not slow" # 快速回归：跳过重型 ffmpeg，约 12 秒
w2v --help                  # CLI：download / understand / run / export /
                            #       story-draft / narrate-from-story
```

测试分级跑法（快档 / 定向 / 全量）见 [`AGENTS.md`](AGENTS.md) 与
[`backend/README.md`](backend/README.md)。

启动 Web 控制台（默认 127.0.0.1:8123，可用 W2V_HOST / W2V_PORT / W2V_WORKSPACE 覆盖）：

```bash
W2V_WORKSPACE=/path/to/workspace python -m web.server
```

### 3. 前端

```bash
npm install
npm run dev                 # http://localhost:3000，/api 与 /workspace 代理到 8123
```

## Story-First 撰稿工作流（Patch v3.0-7 起）

1. `w2v story-draft --series <id> --ep <no>`：4A 用 `story_model`（默认 qwen3.8-max）通读整话，
   产出 `04_script/story_draft.md`，在此停下供人工改写。
2. 人工编辑 `story_draft.md`。
3. `w2v narrate-from-story --series <id> --ep <no>`：4B 用 script model（默认 qwen-plus）
   严格忠于人工稿拆拍解说，随后串起 Stage 5~7 直接出片。

## 版式安全区（Patch v3.0-8 起）

成片固定 1080×1920：主画面锁在 y=180~1440 的 1020×1220 安全框内（同图高斯模糊暗化背景），
字幕落在 y=1460~1700 纯暗区，y=1720~1920 留给抖音原生按钮，物理隔离对白气泡与遮挡区。

## 文档索引

- [总开发规范](WEBTOON2VIDEO_DEV_SPEC.md)
- [Patch v2.5：片尾过滤等优化](PATCH_v2.5_OPTIMIZATION.md)
- [Patch v2.6：切图对齐与故事反写](PATCH_v2.6_ALIGNMENT_AND_STORY.md)
- [Patch v3.0：抖音重建（Story-First + 安全分区）](PATCH_v3.0_DOUYIN_REBUILD.md)
- [后端详细说明](backend/README.md)
