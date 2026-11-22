# AGENTS.md —— Webtoon2Video 仓库约定

漫画条漫 → 1080×1920 抖音竖屏解说视频流水线。前端 `src/`（React 19 + Vite），
后端 `backend/`（Python，src-layout，导入根 `backend/src`）。
总规范见 `WEBTOON2VIDEO_DEV_SPEC.md`，后端细节见 `backend/README.md`。

## 测试跑法（最重要）

后端共 **880** 条 pytest 用例，`pytest-xdist` 已装在 `backend/.venv`。
**开发期默认只跑快档，交付前再跑一次全量。**

```bash
cd backend

# 1) 快速回归（日常小改动）：纯单测 + API，跳过重型 ffmpeg，约 12 秒
.venv/bin/python -m pytest -n auto -m "not slow"

# 2) 定向验证（改了某个模块就跑它），例：
.venv/bin/python -m pytest tests/test_patch_v412_single_beat_tts.py

# 3) 全量并发兜底（改了 s5/s6/s7 媒体链路、或准备提交前必跑），约 30 秒
.venv/bin/python -m pytest -n auto --dist loadfile
```

- `slow` 标记只给「真拉起 ffmpeg/ffprobe 做**视频编码 / 长链路渲染 / 完整 E2E**」的用例
  （当前 22 条）；纯 mock / 纯 Python / 纯 TestClient 一律不标，所以快档覆盖率与全量一致。
- 快档**仍会调用 ffmpeg**，但只是轻量音频滤镜（`silenceremove`/`loudnorm` 裁剪，每条几十毫秒），
  视频编码 `libx264` 为 0 次——这些调用测的正是「裁剪后真实时长」，砍掉会丢断言，故保留。
- `--dist loadfile` 让同一文件的用例固定在一个 worker，module 级 fixture 不重复执行，
  全量档比默认 `--dist load` 快约 1/3（46s → 30s）。
- 默认 `pytest`（不带 `-n`）行为不变：串行跑全部 880 条，约 1 分钟。

前端检查：

```bash
npx tsc --noEmit        # 类型检查
npm run build           # 构建
```

后端 lint（本机 ruff 在全局，backend/.venv 内没有）：

```bash
cd backend && /opt/homebrew/bin/ruff check src tests
```

### 耗时对比（Apple M3 / 8 核）

| 场景 | 耗时 |
| :--- | :--- |
| 优化前全量串行 `pytest` | 5m59s |
| 优化后全量串行 `pytest` | 58s |
| 快档 `pytest -n auto -m "not slow"` | 12s |
| 全量并发 `pytest -n auto` | 46s |
| 全量并发 `pytest -n auto --dist loadfile` | 30s |

## 改动纪律

- 后端改动只在 `backend/`；前端 `src/` 已定稿，改动前先确认是否影响既有交互。
- 字幕与读音双文本必须分离（`narration_text` 与 `tts_text`），不得合并。
- 提交 / 推送必须由用户显式下令；不要自行 commit 或 push。
- 测试、ruff、tsc、build 任一不过，不算完成。
