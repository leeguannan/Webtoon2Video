# backend —— WEBTOON2Video 后端

Python 后端独立目录，存放全部后端代码（规范 `WEBTOON2VIDEO_DEV_SPEC.md` 中的 `src/core`、`src/stages`、`src/providers`、`src/web`）。
根目录的 `src/`、`package.json`、`vite.config.ts` 是已定稿的前端原型，本目录不修改、不依赖。

## 目录结构

```text
backend/
├── pyproject.toml          # 工程与依赖声明（src-layout）
├── src/
│   ├── core/               # schemas / config / manifest / memory
│   ├── stages/             # Stage 0~7
│   ├── providers/          # 百炼 LLM / TTS 适配
│   └── web/                # FastAPI + SSE
├── tests/                  # pytest 单元测试
└── .venv/                  # 虚拟环境
```

## 使用

```bash
cd backend
source .venv/bin/activate          # 激活虚拟环境
python -m pip install -e ".[dev]"  # 安装依赖（开发依赖含 pytest / respx）
pytest                             # 运行测试
```

测试与运行期均以 `backend/src` 为导入根，例如：

```python
from core.schemas import NarrationScript
```

未激活虚拟环境时可直接使用解释器绝对路径：

```bash
backend/.venv/bin/python -m pytest backend/tests
```

### 启动控制台（前端 + 后端）

```bash
# 终端 A：FastAPI 控制台后端（默认 127.0.0.1:8123）
cd backend && ./.venv/bin/python -m web.server

# 终端 B：前端 dev server（Vite 已把 /api 与 /workspace 代理到同一后端端口）
npm run dev
```

后端端口与前端代理读取同一组环境变量，改端口只需改一处：

```bash
W2V_PORT=8200 W2V_HOST=127.0.0.1 \
  backend/.venv/bin/python -m web.server &
W2V_PORT=8200 npm run dev
```

| 环境变量 | 作用 | 默认值 |
| :--- | :--- | :--- |
| `W2V_HOST` | 后端监听地址（前端代理同步读取） | `127.0.0.1` |
| `W2V_PORT` | 后端监听端口（前端代理同步读取） | `8123` |
| `W2V_WORKSPACE` | 工作区根目录（覆盖 `config.yaml` 的 `workspace.root`） | `config.yaml` 里的值 |

## 环境变量

所有百炼调用统一读取 `DASHSCOPE_API_KEY`（根目录 `.env` 已配置）。

## 已落地模块（Milestone 1）

| 模块 | 文件 | 职责 |
| :--- | :--- | :--- |
| M1-T1 配置 | `src/core/config.py` | 全局配置 / 作品级覆盖 / 多音字词典 / API Key 解析 |
| M1-T2 契约 | `src/core/schemas.py` | 规范第 3 节 v2.4 锁定数据契约（字段名不可改） |
| M1-T3 清单 | `src/core/manifest.py` | 各话 Stage 1~7 状态清单 `manifest.json` |
| M1-T3 记忆 | `src/core/memory.py` | StoryBible v2 四层记忆体与 Stage 4 上下文组装 |

示例配置文件：`config.example.yaml`、`pronunciation_dict.example.yaml`（复制为 `config.yaml` 后生效，
配置查找优先级：显式路径 > `W2V_CONFIG` 环境变量 > 当前目录 `config.yaml` > `backend/config.yaml`）。

### 记忆体的快照回退语义

`StoryBibleManager.resolve_base_bible(k)` **强制优先**读取 `bible_snapshots/ep_{k-1}.json`（只读快照），
只有该快照不存在时才退化为「裁剪到第 k 话之前」的 live 记忆体（来源标签 `live:filtered`）。

该回退分支是规范第 4.1 节「强制加载 `ep_{episode_no - 1}` 只读快照（**若存在**）」的直接实现，
属于契约内行为而非临时兜底；当上游流水线保证第 1 话起快照必定落盘后，
可在 `backend/src/core/memory.py` 的 `resolve_base_bible` 中将回退分支收紧为显式报错。

## 已落地模块（Milestone 2：Stage 0 / Stage 1 采集）

| 模块 | 文件 | 职责 |
| :--- | :--- | :--- |
| M2-T0 目录 | `src/stages/s0_catalog.py` | 多作品 `workspace/index.json` 注册表、作品 `catalog.json`、目录页翻页抓取与话号区间解析、`local://` 本地目录导入 |
| M2-T1 采集 | `src/stages/s1_crawler.py` | 观看页图片直链提取 → 并发下载 → 图片头/内容验损 → 落 `episodes/ep_XXX/01_raw/`；支持 `local://` 本地图片导入 |
| 共用 HTTP | `src/core/http_client.py` | **Referer 铁律的唯一归属地**：统一注入 `Referer: https://www.webtoons.com/`、`User-Agent`、`Accept`；`local://` URL 工具 |

### Referer 铁律的落点

`core.http_client.build_client()` 是全项目**唯一**创建 `httpx.AsyncClient` 的位置，
`Referer: https://www.webtoons.com/` 由它统一注入；Stage 0 / Stage 1 只接受注入的 client，
不各自拼请求头，因此不存在「某条路径漏带 Referer」的可能。
测试 `test_fetch_series_catalog_sends_referer` 与 `test_download_images_always_sends_referer`
分别断言目录页请求与图片请求都带该头。

### 验损与断点续跑

`validate_image_bytes()` 两层校验：① 图片头魔数必须命中已知格式（挡 HTML 错误页 / 空响应）；
② `deep_verify=True` 时用 Pillow 解码校验（挡「头正常但内容被截断」的残图）。
下载先写 `.xxx.part` 再原子 `replace` 落盘；已存在且文件头合法的同序号切片会被跳过，
因此中断后重跑天然续传（`test_download_images_skips_existing_valid_files`）。
单个响应 `content-length` 与实际字节数不符按失败处理，不落盘。

### 目录与话号解析

目录页话号按优先级取：链接 query `episode_no` → 路径 `/ep-N` → 链接/条目文本 → 区间标题偏移回填
（`Episodes 11-20` 这类标题会按条目顺序回填 11、12…）。锁定话次由 class 含 `lock` 或
文案含 preview / locked / members only 判定，落到 `EpisodeCatalogItem.is_locked`。

### 已知限制

- WEBTOON 真实页面结构由**合成 HTML 夹具**验证（`tests/test_s0_catalog.py` / `tests/test_s1_crawler.py`），
  真实站点改版后选择器可能需要调整，集中在 `CatalogParser` / `parse_image_urls` 两处。
- `build_client(http2=True)` 已预留，但需额外安装 `h2` 包；当前默认 HTTP/1.1 以保证开箱即用。
- Stage 3 及以后（视觉 / 脚本 / TTS / 时间轴 / 渲染 / FastAPI）尚未实现。

## 已落地模块（Milestone 2：Stage 2 切图）

| 文件 | 职责 |
| :--- | :--- |
| `src/stages/s2_segmenter.py` | 切片纵拼 + 按切片边界分组切面板 + 空白面板丢弃，产出 `PanelMeta` |
| `tests/test_s2_segmenter.py` | 分组语义、空白面板丢弃、合并模式、像素级还原、跨接缝圆不被切破 |

### 铁律二的落点

规范铁律二要求「禁止把几十张切片拼成超大图直送视觉模型，必须先经本地 Stage 2 滑窗切图
（`height <= 2400px`）」。落点：

- `MAX_PANEL_HEIGHT = 2400` 是硬上限，默认**一片一切片 = 一块面板**，面板高度即切片高度
  （真实 WEBTOON 单屏 980~1170px），天然不越界；
- 单张切片本身超高（> 2400）时退回像素级滑窗（`segment_image`），保证不越界；
- `PanelMeta.edge_density` 给出该块的 Canny 边缘密度，便于排查误切。

### 切图语义：默认「一片一面板」

真实 WEBTOON 页面实测（Tower of God 第 2 话 52 屏）推翻了最初的「按像素留白找切点」方案：

- 页与页之间**没有纯白间隙**——整幅 57591px 拼图上 `blank_row_mask` 只找到 2 段白行
  （长 148 / 138），按留白切点会在页内乱切，一话切出 192 块面板并混进 100px 的空白碎片；
- 合并两屏会在接缝处露出**被腰斩的画格**（WEBTOON 自己的分页就常把一个画格切成两屏），
  视觉模型会把半张脸 / 半句话当完整画面读，反而误读。

因此改为按**切片边界**分组：`group_slices(heights, max_slices_per_group=1)` 默认一片一面板，
`merge_slices=2` 仍可显式开启（受 2400px 上限约束）。空白面板由 `is_blank_panel()` 判定
（中间调像素占比 < 0.005 **且** Canny 边缘密度 < 0.001，两者缺一不可，否则白底黑线的画面页会被误判）
并直接丢弃——不落盘、不进记忆体。

`blank_row_mask` / `canny_edge_density` / `segment_rows` 仍然保留，作为单片超高的兜底路径。

### 已知限制

- 纵拼假设同一话的所有切片宽度一致（WEBTOON 即如此）；宽度不一致时按最宽补白边，不做缩放变形。
- 切片边界切分假定「一屏 = 一个完整画面单元」；若站点在单屏内放多个画格，视觉模型需自行分辨。
- 留白判定沿用规范给的固定阈值，纯色大面积画面（如整页黑底）本身会被判为留白行。

## 已落地模块（Milestone 3：百炼接入 / Stage 3 / Stage 4 / CLI）

| 模块 | 文件 | 职责 |
| :--- | :--- | :--- |
| M3-T1 客户端 | `src/providers/llm_base.py` | `LLMClient` 抽象、`ChatMessage` / `ContentPart`（支持 `image_url` 多模态）、JSON 提取 |
| M3-T1 百炼 | `src/providers/llm_bailian.py` | compatible-mode/v1 `chat/completions`、超时/5xx 指数退避自愈重试、4xx 直接失败 |
| M3-T1 图片 | `src/providers/image_utils.py` | 长边 ≤ 1280 等比缩放、Base64 Data URL 编码与尺寸解码 |
| M3-T1 归一化 | `src/core/normalize.py` | 模型输出收敛到 v2.4 契约（枚举、类型、长度、面板引用） |
| M3-T2 识图 | `src/stages/s3_vision.py` | 逐面板调用 `qwen3-vl-plus`，注入 Layer 1 角色名单，落 `03_vision/vision.json` |
| M3-T3 撰稿 | `src/stages/s4_scriptwriter.py` | 两段式（整话通读概览 + 分批解说）调用 `qwen-plus`，落 `script.json` + `script_readable.md`，写新快照与篇章归档 |
| M6-T1a CLI | `src/cli.py` | `w2v download` / `w2v understand`，多部并发 + 部内 Stage 4 加锁串行 |
| 提示词 | `prompts/vision_extract.md`、`prompts/script_overview.md`、`prompts/script_beats.md` | 模板化提示词（`{{TOKEN}}` 占位符） |

### API Key 读取链路

百炼 Key 统一走 `DASHSCOPE_API_KEY`：

1. `BailianClient(api_key=...)` 显式传入；
2. 否则 `core.config.resolve_api_key_with_env_file()` 依次查 `env` 字典 → 进程环境变量 → **项目根 `.env`**（仓库根目录已有）；
3. 仍无则抛 `ConfigError`。

`w2v` 启动时先执行 `load_env_file()`（把根目录 `.env` 载入进程环境，默认不覆盖已有变量），
因此命令行直接可用，无需手动 export。

### Stage 3 的两个硬约束

- **长边 ≤ 1280px**：图片先经 `image_utils.resize_for_vision()` 缩放，再以
  `data:image/jpeg;base64,…` 内联进请求体；`test_stage3_sends_image_as_data_url_with_long_edge_1280`
  从请求体里反解出图片尺寸做断言。
- **注入 Layer 1 角色名单**：把 `name` + `aliases` + `visual_traits` 渲染进提示词，
  避免同一部作品里多个相似角色被认成路人（防脸盲）。名单按**该话的时间旅行基线**取
  （`build_character_reference(episode_no)` → 优先 `ep_{k-1}` 只读快照），既不把未来才登场的
  角色带进识图，也保证重跑时角色描述与当时一致。

### Stage 4 的记忆链路

`run_stage4()` 固定流程：`resolve_base_bible(k)`（强制读 `bible_snapshots/ep_{k-1}.json`）
→ `build_stage4_context()`（篇章摘要 + 近 3 话详摘 + open 伏笔 + 至多 2 条远期召回）
→ 出稿 → 写 `script.json` / `script_readable.md` → 每满 10 话用百炼压缩归档一条 `ArcSummary`
→ `apply_episode_result()` 合并角色/伏笔/摘要并写 `bible_snapshots/ep_{k:03d}.json`。

字幕文本与配音文本严格分离：`narration_text` 只进字幕，`tts_text` 只送 TTS，
`script_readable.md` 里两列分开呈现（`test_tts_text_never_pollutes_narration_text`）。

### Stage 4 两段式出稿（防 `max_tokens` 截断）

单段式要求一次调用吐完整话全部 beats + 摘要 + 角色/伏笔，长话（实测 77 格面板）会让
`completion_tokens` 撞 `max_tokens=8000`，百炼返回 `finish_reason="length"`、JSON 写一半，
表层却报「模型返回内容不是合法 JSON」（真因被掩盖）。现改为两段：

1. **整话通读**（`prompts/script_overview.md`，`max_tokens=4000`）：只产 `hook_title` /
   `episode_digest` / `character_updates` / `new_plot_threads` / `resolved_thread_ids`，不写解说句；
2. **分批解说**（`prompts/script_beats.md`，每批 ≤ `DEFAULT_BATCH_SIZE=15` 格，`max_tokens=4000`）：
   带上整话概览与上一批末句衔接，逐批产 `beats`；批次内若仍收到 `finish_reason=length`
   （`LLMTruncatedError`），自动对半拆成两子批递归重跑，单格失败才报错。

配套的 provider 层改动：`ChatResponse.finish_reason` 透传供应商结束原因；
`parse_json_response(text, finish_reason=...)` 在 `length` 时抛 `LLMTruncatedError`
（区别于普通格式错误）；`extract_json_text()` 容忍只有开头围栏、闭合围栏丢失的截断残片。
合并后 `beat_id` 全话重排（`b1..bN`）保证唯一。这同时落地「先完整读完整话再逐页讲述」。

### 并发模型（铁律三）

- `cli._SERIES_LOCKS: dict[str, asyncio.Lock]` 是部级锁仓库；
- 不同 `series_id` 的 Stage 0~7 **完全并行**（`run_download` / `run_understand` 用 `asyncio.gather`）；
- 同一部内：Stage 1~3 并发，**Stage 4 按 `episode_no` 升序 + 部级互斥锁串行**，
  保证 `story_bible.json` 写入无竞态；
- 锁与事件循环绑定，换事件循环（如同进程多次 `asyncio.run`）会自动重建，避免
  `RuntimeError: ... is bound to a different event loop`。

### 命令行

```bash
cd backend
.venv/bin/w2v download   --series https://www.webtoons.com/en/fantasy/xxx/list --series /local/dir \
                          [--episodes 1-3,5] [--workspace ./workspace] [--concurrency 8]
.venv/bin/w2v understand --series wt_88201 [--series wt_95] [--episodes 1-5] [--concurrency 4]
```

`download` = Stage 0（登记 `index.json` / `catalog.json`）+ Stage 1（图片落 `01_raw/`）；
`understand` = Stage 2（切图）+ Stage 3（识图）+ Stage 4（撰稿与记忆更新）。
两条命令都逐行输出 JSON 摘要；配置错误退出码 2，其他异常退出码 1。

### 已知限制

- Stage 3 目前按面板逐格调用识图（`qwen3-vl-plus`），视觉侧尚未做「整话先通读」的聚合调用；
  「先完整读完整话再逐页讲述」目前由 **Stage 4 的两段式出稿**承担（整话概览 + 分批解说），
  Stage 3 提示词仍只要求描述看得见的内容，不脑补剧情。
- `prompts/` 是纯 Markdown 模板，尚未做版本化与 A/B 对比。
- Stage 5（TTS）/ Stage 6（时间轴）/ Stage 7（渲染）/ FastAPI 与 SSE 尚未实现。

## 实机验收（Phase 1 门禁，真图 + 真实百炼）

用真实作品 [Tower of God](https://www.webtoons.com/en/fantasy/tower-of-god/list?title_no=95)
跑通 `w2v download` → `w2v understand`（第 1~2 话），验收工作区 `/tmp/w2v_live/ws`
（真实版权图不落仓库）。结论：

| 验收项 | 结果 |
| :--- | :--- |
| `01_raw/` 图片完整下载、无 403 | 60 张全部下载成功，PIL 逐张 `verify()` 0 损坏，0 失败 |
| 切图没把对白气泡拦腰切断 | 一片一面板，ep_001 8/8 与原图逐像素一致，ep_002 50/52 一致 |
| 面板高度 ≤ 2400 | 最大 1140px，远低于上限 |
| Stage 3 识图质量 | 52 面板 0 失败；场景描述能认出主角（Layer 1 注入生效），对白原文保留 |
| Stage 4 文风 | 热血悬疑、口语化、10~28 字短句，`tts_text` 与 `narration_text` 分列 |
| 人物记忆 | 主角只留一条档案（`黑发橙瞳少年`，别名含 `Bam`），伏笔跨话累积，快照 `ep_001/ep_002` 落盘 |
| 防剧透 | 第 2 话记忆基线 = `snapshot:ep_001`（第 1 话 = `live:filtered`） |

实跑暴露并修掉的真实缺陷：

1. `parse_image_urls` 不认真实页的 `data-url` 属性（真实原图在 `data-url`，`src` 是透明占位图）
   → 取值顺序改为 `data-url → data-src → data-original → data-lazy-src → src`，并丢弃占位图。
2. `parse_episodes` 误收侧栏「First episode」按钮 → 只接受条目容器（`li._episodeItem` 等）内的链接。
3. `fetch_series_catalog` 串行翻页且超时只抛空错误信息 → 改为并发翻页 + 指数退避重试，
   并把**作品内话号**重排为 1 起（站点内部 ID 保留在 `viewer_url` 里可回溯）。
4. Stage 2 在真实页上切得过碎（192 块 + 空白碎片）→ 改为按切片边界分组（见上一节）。
5. 模型 `emotion` 越界（`pleading`）会让整话崩 → 新增 `core/normalize.py` 收敛层。
6. Stage 4 生成 50 个 beat 超过 60s 超时 → `ApiConfig.timeout_s` 提到 **180s**，
   错误信息带上异常类型与超时值（原来只有一句「百炼请求超时」）。
7. 单个面板抽取失败会拖垮整话 → `on_panel_error="skip"`，失败明细写 `03_vision/vision_failures.json`。
8. 模型把角色从「黑发橙眼少年」改叫「Bam」会登记成两个人 → `_merge_characters` 支持按**别名**匹配合并。


## 已落地模块（Milestone 4：Stage 5 配音 / Stage 6 时间轴 / Stage 7 成片 / w2v run & export）

| 模块 | 文件 | 职责 |
| :--- | :--- | :--- |
| M4-T1 抽象 | `src/providers/tts_base.py` | `TTSSynthesizer` 协议、`TTSResult`（音频字节 + 采样率 + 词级时间戳占位） |
| M4-T1 百炼 | `src/providers/tts_qwen.py` | `qwen3-tts-flash`：`instruction` 情绪透传、OSS 音频回源、WAV 头修复、超时/5xx 自愈重试 |
| M4-T1 兜底 | `src/providers/tts_edge.py` | `edge-tts` 兜底（未安装则显式报错，不静默降级） |
| M4-T2 配音 | `src/stages/s5_tts.py` | 多音字词典替换、并发合成、静音裁剪、音频清单落盘 |
| M5-T1 时间轴 | `src/stages/s6_timeline.py` | Audio-First 权重分配、30fps 帧对齐、`subtitles.ass` + 字幕 PNG |
| M5-T1 字幕渲染 | `src/core/subtitle_render.py` | Pillow 渲染整帧透明字幕 PNG（供无 libass 的机器烧录） |
| M5-T2 成片 | `src/stages/s7_renderer.py` | FFmpeg 9:16 并行运镜、BGM 闪避、字幕烧录、双份落盘 |
| M6-T1b 命令行 | `src/cli.py` | `w2v run`（全链路）、`w2v export`（批量导出） |
| M6-T1b 端到端 | `tests/test_pipeline_e2e.py` | 离线集成：Stage 0~7 全通 + 铁律断言 |

### 铁律：字幕与读音双文本分离

送音文本的**唯一入口**是 `s5_tts.build_spoken_text(beat, overrides)`：

1. 有 `tts_text` 就用它，没有才退回 `narration_text`；
2. 再套 `apply_pronunciation()`——全局词典 + 作品级词典（作品级优先），按 key 长度从长到短替换；
3. `narration_text` 全程**只读**：不写回 `script.json`、不进 `audio_manifest.json`、不进 `subtitles.ass`。

`subtitles.ass` 与 `script.json` 的 `narration_text` 逐条比对一致（实测 8/8 一致），
而送进 TTS 的是注音版本——这就是「读音对、字幕不错字」的落地方式。

### 静音裁剪：为什么不用 `silenceremove` 的 stop_periods

规范写的是「用 FFmpeg silenceremove 裁静音」，但实测 `silenceremove=stop_periods=1` 的真实行为是
**只保留最后一段非静音**，句中停顿被整段丢弃：1.9s 的「静 + 音A + 静 + 音B + 静」被裁成 0.557s，
听起来像「吞了一半字」。因此改用 `areverse` 链式方案：

```text
正向裁头 (start_silence=0) → 反向裁尾 (start_silence=0.05) → areverse 回来
```

实测 1.5s → 1.25s，句中 0.2s 停顿完整保留。全静音输入会被裁成空文件（ffmpeg 退出码仍是 0），
所以加了一条「输出 ≤ 44 字节视为裁剪失败」的保护，失败时保留原音频并把明细写进 `audio_issues.json`。

### 无 libass 的机器怎么烧字幕

本机 ffmpeg 9.0.1（Homebrew）编译时缺 `--enable-libass` 与 `--enable-libfreetype`，
`subtitles` / `ass` / `drawtext` 滤镜**全部不可用**。因此走双轨：

- **外挂轨**：Stage 6 照常产出标准 `subtitles.ass`（平台上传、二次剪辑用，格式不打折）；
- **烧录轨**：Stage 6 用 Pillow 渲染整帧 1080×1920 RGBA 字幕 PNG（`06_plan/subtitles/0001.png`），
  Stage 7 用 `overlay` + `enable='between(t,start,end)'` 逐条烧进画面——不依赖 libass，效果等价。

字体按候选路径顺序探测（`Hiragino Sans GB` / `STHeiti` / `Arial Unicode`），全缺时直接报错，
**宁可失败也不画豆腐块**。

### Audio-First 时间轴

画面时长由音频决定，不由拍脑袋估：

- `frame_ms = 1000 // fps = 33`（30fps）；
- 只有**切点**（每句起点、每段画面边界）向上对齐到 33ms 栅格；
- 音频与字幕的**终点保留真实毫秒**（`start + 真实 duration`），字幕严格咬住语音不被栅格误差拖偏；
- 每个 beat 的最后一个场景延伸到下一句起点，停顿期间画面不断、无黑屏。

### Stage 7 的两次 FFmpeg 调用

`sidechaincompress` 的侧链输入属于 request-pad 机制，**不能同时接 filtergraph 内部的 label**
（会报 `Stream specifier 'voice' ... matches no streams`）。因此拆成两次：

- **调用 A「备音」**：各 beat 人声按时间轴 `adelay` + `amix` 拼成 `voice.wav`；
  BGM `volume=bgm_gain_db` 后归一成 `bgm.wav`（同一次调用双输出）；
- **调用 B「成片」**：视频链 + 字幕 overlay + `[bgm][voice]sidechaincompress` 闪避 + `amix` 混音 → mp4。

运镜还有个坑：`zoompan` 的 zoom 变量**每输入帧会重置为 1.0**，直接对循环图流用 `d=1`
会让运镜完全失效（实测首末帧 diff=0）。正确写法是先 `trim=end_frame=1,setpts=PTS-STARTPTS`
再接 `zoompan=d=帧数,s=1080x1920,fps=30`。

画面构图：背景 `scale` + `crop` + `gblur` + 压暗铺满，前景 `scale`（保持比例）+ 透明填充 +
`overlay` 居中——**原图完整可见，不裁切**。

### 命令行

```bash
cd backend
.venv/bin/w2v run    --series wt_95 --episodes 1-3 [--workspace ./workspace] \
                     [--out ./output_videos] [--voice Cherry] [--bgm ./bgm.mp3] [--skip-download]
.venv/bin/w2v export --series wt_95 --episodes 1-3 --out ./output_videos [--workspace ./workspace]
```

- `run` = Stage 0~7 全链路（未登记的 `--series` 会先跑 `download`）；
- `export` = 把 `07_out/{episode_id}.mp4` 批量复制到 `--out`（缺成片的话次会先重渲染）；
- 两条命令都逐行输出 JSON 摘要；配置错误退出码 2，其他异常退出码 1。

### 已知限制

- BGM 默认 `bgm_gain_db=-18`（规范要求压低）；若 BGM 源本身电平很低（实测 `-35dB` 的合成音），
  减 18dB 后基本听不见——真实音乐文件（峰值约 `-6dBFS`）没问题，但源电平建议先归一化。
  闪避阈值见 `_SIDECHAIN_*` 常量：`threshold=0.01` 必须低于人声 RMS（实测 -29.5dBFS），
  否则只有人声峰值触发压缩，闪避会**静默失效**（实测阈值 0.05 时停顿段只比人声段高 1.1dB，
  改成 0.01 后升到 3.7dB，可用 `tests/test_s7_renderer.py::test_sidechain_threshold_stays_below_speech_rms` 钉住）。
- 本机 ffmpeg 无 libass，成片字幕走 Pillow PNG overlay；若目标机器有 libass，
  可直接用 `subtitles.ass` 走 `subtitles` 滤镜（更快、更锐）。
- 字幕固定压在画面底部，会盖住原图最下方的内容（竖屏 9:16 下原图几乎填满，无处可放）；
  如需避让，得改成把原图再缩小一圈留出字幕带。
- Stage 5~7 尚未做「同一部多话并发渲染」的压力验证（Stage 4 的部级锁已覆盖 Stage 1~4）。

## 实机验收（Phase 2 门禁：真图 + 真实百炼 TTS + 真 FFmpeg）

作品仍是 [Tower of God](https://www.webtoons.com/en/fantasy/tower-of-god/list?title_no=95) 第 1 话，
工作区 `/tmp/w2v_live/ws`（真实版权图不落仓库）。

```bash
cd backend
.venv/bin/w2v run --series wt_95 --episodes 1 --workspace /tmp/w2v_live/ws \
                  --skip-download --out /tmp/w2v_live/out
```

| 验收项 | 结果 |
| :--- | :--- |
| 命令与耗时 | `w2v run` 5m03s（Stage 2~7），exit 0 |
| 成片规格 | 1080×1920 / H.264 / AAC / 30fps / 43.30s / 5.2MB |
| 9:16 构图 | 原图完整居中**未裁切**，背景高斯模糊铺底压暗，抽帧确认 |
| 字幕烧录 | 底部白字黑描边、逐字符折行、中文字体正常（无豆腐块） |
| 字幕不错字 | `subtitles.ass` 8 条与 `script.json` 的 `narration_text` **逐条一致（8/8）** |
| 配音与字幕分离 | 送 TTS 文本走 `build_spoken_text`（含词典替换），`narration_text` 全程只读 |
| 情绪透传 | 8 个 beat 的 `emotion_instruction`（如「压低声音，末尾略带沙哑」）原样送进 `instruction` |
| Audio-First | 成片 43.30s = 音频合计 40.6s + 8 段停顿 2.7s，画面长度跟着语音走 |
| 双份落盘 | `07_out/wt_95_ep_001.mp4` + `exports/wt_95_ep_001_Tower of God.mp4` + `--out` 目录三份一致 |
| `w2v export` | 单独执行正常复制（5.2MB，exit 0） |
| BGM 闪避 | 停顿段 BGM 比人声段高 **3.7dB**（阈值修正前只有 1.1dB，等于没闪） |

这一轮真机跑出来的问题与处理：

1. **BGM 闪避几乎无效**：`sidechaincompress` 的 `threshold=0.05`（-26dBFS）比人声 RMS（-29.5dBFS）
   还高，只有峰值才触发压缩 → 阈值降到 `0.01`、`release` 从 300ms 降到 200ms，
   闪避比从 1.14× 升到 1.54×；并加了一条断言防止阈值被调回去。
2. **百炼 TTS 的 WAV 头是占位值**：RIFF 总长与 data 块长都写成 `0x7FFFFFFF`，
   `wave` 读出来是 44739 秒 → `tts_qwen._fix_wav_size()` 按实际 payload 就地修正。
3. **回源 OSS 报 403 SignatureDoesNotMatch**：客户端默认挂了 `Content-Type: application/json`，
   与签名不一致 → 改为只在 POST 时按请求级设置，GET 下载不带该头。
4. **导出拿到 48 字节的半成品**：`w2v export` 在成片仍在渲染时复制到了写了一半的文件 →
   属并发时序，不是导出逻辑缺陷（渲染完成后复验正常）；如需更稳，可加「文件大小 + ffprobe」校验。

## 已落地模块（Milestone 5：FastAPI 控制台后端 + 前端对接）

| 模块 | 文件 | 职责 |
| :--- | :--- | :--- |
| M7-T1 调度器 | `src/web/job_runner.py` | 8 阶段状态机、部级 Stage 4 升序互斥锁、事件总线（SSE） |
| M7-T1 服务 | `src/web/server.py` | 规范 §5 全部 REST 端点、`/workspace` 静态挂载、SSE 流式日志 |
| M7-T2 前端 | `../src/App.tsx` | 去掉 Mock，改接真实 REST + SSE + 文件下载 |
| M7-T2 代理 | `../vite.config.ts` | `/api` 与 `/workspace` → `http://127.0.0.1:8123`（端口见下方「端口约定」） |

### 端点一览（对应规范 §5）

| 方法与路径 | 作用 |
| :--- | :--- |
| `GET /api/series` | 书架列表（读 `workspace/index.json`） |
| `POST /api/series` | 导入新漫画 URL/本地目录 → Stage 0 抓目录并登记 |
| `GET /api/series/{sid}` | 目录 + StoryBible + 各话 manifest/运行态 |
| `PUT /api/series/{sid}/memory` | 人工编辑角色图谱 / 伏笔状态 / 叙事基调（整体覆盖式） |
| `POST /api/jobs/batch` | 按作品分组下发任务，跨作品并发、同作品 Stage 4 串行 |
| `POST /api/episodes/{eid}/rerun` | 从指定阶段断点重跑 |
| `PUT /api/episodes/{eid}/script` | 保存人工修改的 beats（可选顺带从 Stage 5 重跑） |
| `PATCH /api/episodes/{eid}/panels/{pid}` | 标记/取消 `is_transition_blank` |
| `GET /api/episodes/{eid}/events` | SSE 实时日志与阶段状态 |
| `GET /api/episodes/{eid}/export?format=mp4\|ass\|script` | 单话成片/字幕/文稿下载 |
| `GET /api/series/{sid}/export-zip` | 已完成话次的 9:16 成片流式打包 |
| `GET/PUT /api/config` | 百炼模型与合成参数 |
| `GET/PUT /api/pronunciation-rules` | 全局/作品级多音字纠偏词典 |
| `GET /api/health` | 健康检查 |

### 并发模型（铁律三的落点）

- `JobRunner.submit_batch()` 按 `series_id` 分组，组内 `episode_no` 升序提交 → 不同作品完全并行。
- 同一部作品的 Stage 4 走 `_SeriesGate.run()`：`_pending` 集合 + `asyncio.Condition`，
  按 `min(_pending)` 排队，保证 **按话次升序** 且互斥；跨事件循环会重建锁（`asyncio.Lock` 不能跨 loop 复用）。
- 排队超时 6 小时（`STAGE4_QUEUE_TIMEOUT_S`）后抛 `JobError`。

### SSE 事件格式

```
event: log
data: {"type":"log","message":"[20:48:00] [执行中] 正在运行 s1_crawler","stage":"s1_crawler","timestamp":...,"episode_id":"wt_95_ep_001"}

event: state
data: {"type":"state","status":"running","current_stage":"s1_crawler","stages":{"s1_crawler":{"status":"running"}},"error":null,...}
```

`EventBus` 保留最近 200 条历史，新订阅者先补发历史再持续推送；每话日志上限 500 条。

### 前端对接（只改 `src/App.tsx`）

- `src/components/*.tsx` 四个组件**一行未改**，`src/types/studio.ts`、`src/data/mockStudioData.ts` 也未改
  （Mock 数据文件保留但 App.tsx 不再 import）。
- 数据流：`GET /api/series` + `/api/config` + `/api/pronunciation-rules` 并发拉取 → 前端类型映射；
  轮询 5s 一次（有任务在跑时每次刷，空闲时每 4 轮刷一次）+ `EventSource` 订阅当前话的事件流。
- `EpisodeItem.selected`（勾选态）后端恒返回 `false`，由前端 `selectedMap` 本地维护。
- 下载走 `fetch → blob → objectURL → a[download]`，文件名取后端 `Content-Disposition`；
  ZIP 走同一条链路，不在前端拼装。

### 已知限制

- `PUT /api/config` 目前只落盘 `api.base_url/timeout_s/max_retries`、`models.vision/script/tts`、
  `render.bgm_gain_db`；`ttsVoice`/`maxImageLongEdge`/`aspectRatio` 接收后会回显但**不落盘**
  （这两项目前是常量约束，后端尚未做成可配置项）。
- `PUT /api/pronunciation-rules` 按 `scope` **整体覆盖**：global 写 `config.yaml` 的
  `pronunciation_overrides`，作品级写 `workspace/{series_id}/series_config.yaml`。
- `GET /api/series` 每次都会重算全部话次的 `episode_item`（Tower of God 652 话约 0.33s、约 760KB），
  因此前端不做高频全量轮询，改用 SSE + 按需刷新。
- 后端未做鉴权，默认只监听 `127.0.0.1:8123`；CORS 全开仅适用于本地单用户。

### 端口约定（后端与前端代理必须一致）

| 位置 | 默认值 | 覆盖方式 |
| :--- | :--- | :--- |
| 后端 `python -m web.server` | `127.0.0.1:8123` | `W2V_HOST` / `W2V_PORT` 环境变量 |
| 前端 `vite.config.ts` proxy | `http://127.0.0.1:8123` | 同一组 `W2V_HOST` / `W2V_PORT` 环境变量 |

> 本机 `8000` 端口已被 `ai炒股.nosync` 的 uvicorn 占用，因此控制台后端统一改用 `8123`。
> 两端读同一个环境变量，改端口只需改一处环境变量，不必再分别改代码。

## 实机验收（M7 门禁：真实后端 + 真实工作区 + 浏览器代理）

后端起在 `127.0.0.1:8123`（最小工作区冒烟用 8124），真实工作区 `/tmp/w2v_live/ws`
（wt_95 / Tower of God / 652 话）。

| 验收项 | 结果 |
| :--- | :--- |
| `GET /api/health` | `{"ok":true,"workspace":"/tmp/w2v_live/ws","series":1}` |
| `GET /api/series` | 1 部作品 / 652 话；`story_bible` 字段为 `recent_digests`（与前端 `StoryBibleV2` 同名） |
| 单话结构 | `stages[0]={"id":"s0_catalog","code":"S0","label":"目录索引","phase":1,"status":"pending"}`；8 面板 / 8 beats |
| 面板 | `image_url="/workspace/wt_95/episodes/ep_001/02_panels/panel_001.png"`，`dialogues`/`sfx_list` 齐备 |
| beat | `tts_text:""` 与 `narration_text` 分列，`motion:"zoom_in"`，`audio_duration_ms:5676`（取自 05_audio 清单） |
| `PUT /api/series/{sid}/memory` | 角色/伏笔/基调写回成功，返回完整 `story_bible` |
| `PUT /api/config` | 回显 camelCase 字段（`bailianBaseUrl`/`visionModel`/…/`workspaceRoot`） |
| `PUT /api/pronunciation-rules` | 全局规则写入后 `GET` 返回 `id:"global_银行"`, `series_id:null` |
| `PATCH .../panels/{pid}` | `{"panel_id":"panel_001","is_transition_blank":true}`，回写 `03_vision/vision.json` |
| `PUT .../script` | 落盘 `script.json` 并重写 `script_readable.md`；不带 `from_stage` 时 `job:null` |
| `GET .../export?format=mp4` | 200 `video/mp4`，`Content-Disposition: attachment; filename="wt_smoke_ep_001.mp4"` |
| `GET .../export?format=script` | 200 `text/markdown`（文稿缺失时 404 `文件尚未生成：…`） |
| `GET .../export?format=xyz` | 400 `format 只支持 mp4 / ass / script` |
| `GET /api/series/{sid}/export-zip` | 200 `application/zip`，成员名 `{episode_id}.mp4` |
| SSE | `event: log` / `event: state` 两类帧，字段与前端 `addEventListener` 对应 |
| `/workspace` 静态资源 | 经 vite proxy 取面板图 200 `image/png`、成片 200 `video/mp4`（5.2MB）、`catalog.json` 200 |
| `npm run lint` | `tsc --noEmit` 无输出（通过） |
| `npm run build` | `vite build` 成功，1665 modules，`dist/assets/index-*.js` 307.20 kB |
| 后端回归 | `python -m pytest` → **352 passed**，exit 0 |
| 端口一致性 | 后端默认 8123，前端 proxy 同指 8123；经 proxy 复测 `/api/health`、`/api/series`、`/api/series/{sid}`、`/api/config`、`/api/pronunciation-rules`、`export?format=mp4|ass|script`、`export-zip` 全部 200，SSE 直连与经 proxy 均返回 `text/event-stream` |

前端原型保护边界核对：`src/components/*.tsx`（CatalogView / ConfigDictView / EpisodeStudioView /
MemoryView）、`src/types/studio.ts`、`src/data/mockStudioData.ts` 的 mtime 全部仍为 `Nov 14 10:14:40`
（本阶段只改 `src/App.tsx` 与 `vite.config.ts`）。

## 实机验收（Stage 4 截断修复：wt_5081《唯一的希望》第 1 话，77 格面板）

**故障现象**：前端日志报 `[失败] 模型返回内容不是合法 JSON`，残片以 ```` ```json ```` 开头、
在 `character_updates` 中段截断。真机复现脚本（`BailianClient` 原调用，`max_tokens=8000`）确认：
`finish_reason="length"`、`completion_tokens=8000`、已产 76 个 beat 后 JSON 写一半，闭合围栏丢失，
表层的「非法 JSON」掩盖了真因「输出被 max_tokens 截断」。

**修复后真机重跑**（同一工作区、同一话、真实百炼，不改任何输入数据）：

| 指标 | 结果 |
| :--- | :--- |
| 分段调用 | 7 次 = 1 次整话概览 + 6 批解说（15/15/15/15/15/2 格），全部 `finish_reason="stop"` |
| 截断调用数 | **0**（单次最大输出 4548 字符，远低于 `BEATS_MAX_TOKENS=4000` 的容量） |
| 成片解说 | 72 个 beats，覆盖全部 **77/77** 面板（`target_panel_ids` 引用全部有效） |
| 枚举合法性 | motion 仅含 6 个契约取值（static/zoom_in/zoom_out/pan_down/pan_up/shake） |
| 记忆产物 | `bible_snapshots/ep_001.json` 落盘：2 角色 / 3 伏笔，`episodes_index=[1]` |
| 文稿 | `script.json` + `script_readable.md`（22186 字节）均落盘，`episode_id` 以流水线为准 |
| 总耗时 | 189.2s（原单调用约 130s 必失败；分段后单次更短、可自愈） |

回归测试新增 7 条（`test_llm_bailian.py` 3 条 + `test_s4_scriptwriter.py` 4 条），覆盖
`finish_reason` 透传、未闭合围栏容忍、`LLMTruncatedError` 归因、面板分批、长话全局 beat_id
重排、截断批次对半自愈；全量 **352 passed**。

## 实机验收（界面「待执行」排查 + Stage 7 长话提速）

**用户报障**：「第七步 FFmpeg 合成执行了吗？为什么一直显示是待执行？」
wt_5081《唯一的希望》第 1 话（81 张原图 / 77 格面板 / 296 秒成片）在界面里 8 个阶段全显示「待执行」。

### 三条独立根因（互相独立，必须分别修）

| # | 根因 | 证据 | 修复 |
| :-- | :--- | :--- | :--- |
| 1 | **manifest 从来没被写过** | `ManifestManager.mark_running/mark_done/mark_failed/update_stage` 全仓只有定义、没有调用点；唯一调用是 `server.py` 的 `manager.load()` → `manifest.json` 永不生成 | `job_runner._run_stage` 与 `cli._manifest_stage` 在每个阶段开始/成功/失败时写 manifest |
| 2 | **阶段失败不置 failed** | `job_runner` 的 except 分支只设 `state.error` + 记日志，`state.stages[stage]` 永远停在 `running` | except 分支写 `{"status":"failed","error":...}`，并 `manifest.mark_failed` |
| 3 | **界面把「没有耗时」当「没执行」** | `EpisodeStudioView.tsx` 用 `st.durationSec !== undefined ? ... : '待执行'` | 改为按 `status` 兜底：done→「已完成」、failed→「失败」、running→「执行中」 |

### 状态判定改为三方合并

`server.stage_progress()` 现在合并 **manifest（执行记录）→ 内存运行态（本次会话）→ 磁盘产物（事实）**：

* `core/manifest.py` 新增 `STAGE_ARTIFACT_RULES` / `infer_stage_status()` / `infer_stage_artifacts()`：
  只做「有产物 ⇒ done」的单向判断，空目录不算数；
* S0 是作品级阶段，按 `series_dir/catalog.json` 判定；
* 历史工作区用 `python scripts/backfill_manifest.py --workspace <dir>` 一次性回填。

### Stage 7：长话从「渲染不出来」到「2.6 分钟出片」

| 指标 | 修复前 | 修复后 |
| :--- | :--- | :--- |
| 渲染速度 | **0.065x**（900s 只渲出 58 秒内容） | **1.93x**（实测 296 秒成片 153.7s） |
| 产物 | 9.4MB 残片（`moov atom not found`） | 43.79MB 完整成片（1080×1920 / 30fps / h264+aac / 296.26s） |
| 滤镜图输入数 | 222（2×74 场景 + 73 字幕 + 人声） | 每段 ≤ 26（`_MAX_SCENES_PER_SEGMENT = 8`） |

三处关键改动：

1. **背景预渲染**（`_prepare_background`）：原来 `gblur` 在帧循环里对同一张静态图逐帧重算
   1080×1920 高斯模糊，是慢 15 倍的主凶；现在一次性渲染成静态 PNG 再进滤镜图；
2. **字幕裁剪**（`_prepare_subtitle_png`）：字幕 PNG 原本是整帧 1080×1920 RGBA，文字只占 4.8%，
   overlay 等于每帧白做 95% 的 alpha 混合；现在按 `getbbox()` 裁成文字矩形再按偏移叠加；
3. **分段渲染**：整话一个 `filter_complex` 会因输入数过多把 swscale 内存打爆
   （`Failed initializing scaling graph (Resource temporarily unavailable)`，成片写出 0 字节），
   改成按场景切段、段产物用 concat demuxer `-c copy` 无损拼接，内存峰值与话长无关；
   同时 `run_ffmpeg` 默认超时 900s → **3600s**（长话渲染十几分钟，超时只会留下无 moov 的残片）。

### 本轮新增测试（7 条，全量 **368 passed**）

* `test_manifest.py` 4 条：`infer_stage_status` 的「有产物⇒done」「空目录不算」「成片也算」，
  `infer_stage_artifacts` 的摘要格式；
* `test_web_server.py` 1 条：阶段失败 → 内存状态 `failed` + manifest `failed` + 错误信息落盘；
* `test_s7_renderer.py` 2 条：20 场景真渲染出片（3 段 + 时长对齐）、分段后单段输入数有界。

## 实机验收（Patch v2.6-5：故事反写 + 顺位硬校验 + 镜头焦点偏移）

补丁来源：根目录 `PATCH_v2.6_ALIGNMENT_AND_STORY.md` 的 **Patch 5**（Stage 4 故事化）。
本轮只动 `src/core/schemas.py`、`prompts/`、`src/stages/s3_vision.py`、`s4_scriptwriter.py`、
`s6_timeline.py`、`s7_renderer.py` 与对应测试；下载器、记忆库、TTS、多音字词典、前端 UI 均未改。

### 新增契约（全部带默认值，向后兼容）

```python
class StoryChapter(BaseModel):
    chapter_index: int
    panel_range: tuple[str, str]     # 覆盖的连续起止分镜
    story_prose: str                 # 通顺完整的故事段落
    dramatic_intent: str             # 本段的冲突与情绪转折

class EpisodeStoryDraft(BaseModel):
    episode_id: str
    overall_plot_arc: str            # 全话一句话主线
    chapters: list[StoryChapter]
```

`ScriptBeat.focus_hint: Literal["full", "top", "bottom", "center"] = "full"`；
`TimelineScene.focus_band: bool = False`（True = `focus_region` 是 focus_hint 取景带，渲染端据此裁切）。

### Stage 4 改成三段式调用

```
概览（script_overview.md）→ 4A 故事反写（script_story_draft.md）→ 4B 分批拆拍（script_beats.md）
```

* **4A**：结合 StoryBible v2 与本话全部有效分镜（先硬过滤 `is_transition_blank` 与非剧情图）通读全话，
  产出 `EpisodeStoryDraft`，落盘 `04_script/story_draft.json`；章节 `panel_range` 必须单调递增、
  首尾相接覆盖全部分镜，严禁把后半话反转写进前半话。4A 失败时用概览兜底（`_fallback_story_draft`）
  并打 stderr 告警，不让整话作废。
* **4B**：每批 prompt 注入本批命中章节的 `story_prose` / `dramatic_intent` 作为锚点；
  允许**一格多句**（黑框内心独白 `focus_hint="top"` 在前、人物口头对白 `focus_hint="bottom"` 在后，
  同一 panel_id 拆成两条 beat）与**一句多镜**（连续 2~3 格绑定）。
* **顺位硬校验** `validate_beat_alignment(beats, valid_panel_ids, *, important_panel_ids=None)`：
  ① beat 内 `target_panel_ids` 必须全部有效且序号严格递增；② 全局展开序列单调不减；
  ③ **重要分镜**里连续 ≥2 个没有解说覆盖即报错。校验不过时把错误列表拼进
  `{{ALIGNMENT_FEEDBACK}}` 让 qwen-plus 修正，**最多重试 2 次**；耗尽仍不通过则采用最后一版并告警。
  「重要分镜」默认取**带对白的分镜**（`dialogues` 非空）——纯画面格跳过是正常叙述取舍，
  不该触发整话重写。

### 焦点偏移怎么落到画面上

* Stage 6：`FOCUS_BANDS = {top:(0,0,1,0.5), bottom:(0,0.5,1,1), center:(0,0.25,1,0.75)}`；
  `focus_hint != "full"` 时用取景带覆盖 Stage 3 的主体框（不做交集，避免交集为空），
  并置 `focus_band=True`。
* Stage 7：仅当 `focus_band=True` 时在前景链前加
  `crop=w='iw*{w}':h='ih*{h}':x='iw*{x}':y='ih*{y}'`，**默认不裁切**，
  保住实机验收确认过的「原图完整居中未裁切」构图。

### Stage 3 的两个增强

* `vision_extract.md` 要求区分**黑框内心独白**（`speaker_guess` 写 `角色名(内心独白)`，通常在画格上部）
  与**人物口头对白**（气泡台词，`speaker_guess` 写 `角色名`），两类都要抽、独白排前；
* 新增 `{{PANEL_SHAPE}}` 占位符：超高竖长画格（高 ≥ 2400 且高宽比 ≥ 3）会额外提示模型
  「把整幅图等比缩小后从上到下完整读完每一段」。

### 本轮新增测试（5 条，全量 **374 passed**）

* `test_s4_scriptwriter.py` 2 条：`validate_beat_alignment` 乱序/回头/漏格拦截 + 一镜多拍与顺位放行；
  4A→4B 双阶段 Mock（断言 `story_draft.json` 落盘、4B 请求含故事反写文本、focus_hint 分布
  `top/bottom/full`、同格两条 beat）；
* `test_s6_timeline.py` 1 条：focus_hint top/bottom/full → 取景带与 `focus_band`；
* `test_s7_renderer.py` 2 条：带 band 的场景含 crop 滤镜、不带 band 的场景保持整帧。

## 实机验收（Patch v2.6-6：巨块消除 + 超高画格切片 + 分镜 ID 同轨）

用户指出的两个遗留问题：**Stage 2 切出 45871px 的巨块**、**Stage 3 把超高画格缩到 1280 后只剩 19px 宽**，
以及**分镜 ID 三套命名互不对齐**。本轮一并修掉。

### 一、Stage 2：分隔带不再只认纯白

**根因（实测取证）**：wt_5081《唯一的希望》第 1 话的 81 张下载图纵拼成长卷 **690×121262**，
第 74701 行之后是**黑底画风**——画格之间是约 400~500px 的**纯黑过渡带**，没有任何白边。
旧规则只认 `row_mean >= 200` 的纯白带，于是 80 个画格粘成一个 45871px 的巨块。

**改法**：

| 项 | 旧 | 新 |
| :--- | :--- | :--- |
| 分隔带判据 | 纯白（`row_mean>=200`）+ `row_std<3.0` / `row_diff<2.0` | **任意单色均匀带** `row_std<8.0` / `row_diff<5.0`（白/黑/灰都算） |
| 最小连续行 | `max(36, int(W*0.045))` | `max(18, int(W*0.022))` |
| 切点来源 | 所有单色带 | **只认夹在两段内容之间**的单色带（`find_separator_gaps`） |
| 超长块 | 无兜底 | 块高 > 4000px 时在 `[1600,3200]` 区间搜 Canny 行边缘密度最低的波谷兜底切分 |

「只认内容之间的带子」是关键的第二处修正：整幅纯色的合成页若按「所有单色带」切，
会被当成一整条分隔带切碎、最终产出**零面板**（下游 `VisionError: 面板目录没有可用图片`）。
现在首尾纯色边距交给裁边处理，整幅纯色则退化兜底成一块，由空白面板判定决定去留。

真机结果（长卷 120572px，81 张下载图）：

| 作品 / 话次 | 下载图 | 长卷 | 切出块数 | 最大块高 | 超 4000px |
| :--- | ---: | ---: | ---: | ---: | ---: |
| wt_5081 ep_001 | 81 | 690×120572 | 128 → 合并独白框后 100 | **2595** | **0** |
| wt_95 ep_001 | 8 | 700×8718 | 20 → 11 | 1205 | 0 |
| wt_95 ep_002 | 52 | 700×57982 | 109 → 85 | 1479 | 0 |

### 二、Stage 3：超高画格纵向重叠切片喂 VLM

高度 > 1800px 的画格**不再**把长边缩到 1280（那会让 2595px 高的画格缩后只剩 19px 宽），
改为在**内存里**（`02_panels/` 原图文件一个字节都不动）按固定宽 ≤1024px、片高 1600px、
上下重叠 250px 拆成多片，逐片喂 `qwen3-vl-plus`，再把各片结果合并回**同一个 panel_id**：

* `scene_summary`：按段顺序拼接，合并相邻重复句；
* `dialogues`：按 `(speaker, text)` 去重；
* `focus_region`：切片内归一化坐标按片偏移映射回整格坐标；
* `is_transition_blank` 取全部为真，`non_story_type` 任一片是版权/推广即采用。

真机验证（wt_5081 ep_001，97 个面板，Stage 3 用时 80.3s）：3 个超高面板
（`p_058` h=2595 / `p_086` h=2239 / `p_093` h=1807）各切成 2 片、每片 1600px，
三者的摘要都成功合并了两段内容（如 `p_058` 同时描述了「从缝隙中窥视」与「背对观众抱头蹲坐」）。

### 三、分镜 ID 同轨（`p_001`）

| 环节 | 之前 | 现在 |
| :--- | :--- | :--- |
| Stage 2 落盘文件名 | `panel_001.png` | `p_001.png` |
| Stage 2 `PanelMeta.panel_id` | `wt_5081_ep_001_p001` | `p_001` |
| Stage 3 `vision.panel_id` | `panel_001`（**私自改名**） | `p_001`（**原样透传**） |
| Stage 6 规范键 | 文件名主干 | 文件名主干（同一套） |

新增 `backend/src/core/panel_ids.py` 作为**唯一的 ID 归一化入口**
（`PanelIdResolver.resolve()`：精确 → 文件名主干 → 尾部数字三级匹配），
Stage 4 的顺位校验、Stage 6 的面板索引、Stage 4 的 beat 归一化共用它。

**兼容旧写法**：模型、历史产物与外部脚本可能仍引用 `panel_001` / `ep_001_p001`。
`write_episode_script` 在归一化之前先跑 `_canonicalize_beat_panel_ids()` 把这些旧 ID
就地改写成规范 ID，因此旧引用既能通过顺位校验，也能通过 `normalize_script_payload`
的「只保留有效分镜」过滤——历史产物零改动即可通过。

### 本轮新增测试（6 条，全量 **385 passed**）

* `test_s2_segmenter.py` 3 条：黑底过渡带能切开、超长块被安全阀切开且每块 ≤4000px、整幅纯色页不被切碎；
* `test_s3_vision.py` 1 条：Stage 3 原样透传 `p_001`（不再改名）；
* `test_s4_scriptwriter.py` 2 条：旧写法 `panel_001` 能通过顺位校验（且仍受单调性约束）、
  `_canonicalize_beat_panel_ids` 就地改写。

## 实机验收（Patch v3.0-6：清缓存 + 全色系切割 + 空镜标记 + 拟声词剥离）

`PATCH_v3.0_DOUYIN_REBUILD.md` 的 Patch 6：修「同一话播两遍」「一幕两景」「白屏空等」
「拟声词被当人名」四个真实 Bug。**只改 `s2_segmenter.py` / `s3_vision.py` /
`schemas.py` / `vision_extract.md` / 三个测试文件**（下载器、记忆库、TTS 词典、前端 UI 一律未动）。

### 一、Stage 2：原子清缓存 + `panels.json` 清单

旧产物 `panel_001.png` 与新产物 `p_001.png` 并存时，Stage 3 会把两套都当成有效分镜，
同一话因此讲两遍。现在 Stage 2 落盘走**原子替换**：

1. 新图先写进 `02_panels/.new_<pid>/`；
2. 旧的 `*.png` 与 `panels.json` 移进 `02_panels/.old_<pid>/`；
3. 暂存目录的文件 move 回 `02_panels/`；
4. 两个临时目录 rmtree。

任何一步失败都不会出现新旧混存。**PanelMeta 不加字段**（`test_schemas.py` 有字段锁），
空镜标记走 `02_panels/panels.json`：

```json
{"episode_id": "...", "panel_count": 123,
 "panels": [{"panel_id": "p_001", "is_transition_blank": false, ...}]}
```

Stage 3 **只按这份清单遍历**（`load_panels_manifest()`），清单缺失时才回退目录扫描，
历史工作区照常可跑。

### 二、Stage 2：全色系均匀带 + 收紧独白框合并

| 项 | Patch v2.6-6 | Patch v3.0-6 |
| :--- | :--- | :--- |
| 最小连续行 | `max(18, int(W*0.022))` | `max(20, int(W*0.025))` |
| 裁边留白 | 16px | 10px |
| 独白框高度上限 | `< 0.55 * W` | `< 0.35 * W` |
| 独白框单色底占比 | `> 75%` | `> 78%` |
| 独白框与下格间距 | `< 0.50 * W` | `< 0.25 * W` |
| 合并后总高上限 | 无 | `<= 1.65 * W`（否则严禁合并） |

「合并后总高上限」是防「一幕两景」的关键：独白框只有又扁又窄、离下方主画格很近、
且合并后不会超过一个画幅高度时才允许吸附。

### 三、空镜不再丢弃，而是标记（消灭「白屏空等」）

旧逻辑把纯白/纯黑块直接丢弃，下游拿不到线索，成片里反而留下黑屏空等。
现在新增 `is_contentless_panel()`：**偏离主背景色（Δ>15）的像素占比 < 6%** 即判为空镜，
落盘并在 `panels.json` 标 `is_transition_blank=true`；Stage 3 见到这种面板**不调用视觉模型**，
直接产出占位结果（省下 VLM 费用），Stage 4 再按 `is_transition_blank` 过滤掉。

### 四、Stage 3：拟声词剥离

`non_story_type` 新增 `sfx_only`（整格只有拟声词/气氛特效、没有人物动作与对白）。
`vision_extract.md` 明确要求：`心驚`/`驚`/`啪`/`咚嗒` 等拟声词**一律进 `sfx_list`**，
绝不能当人名、不能写进 `dialogues`；「心驚」是「心里一惊」的写法，不是角色名。

### 真机数据（wt_5081《唯一的希望》第 1 话，81 张真实下载图）

| 环节 | 结果 |
| :--- | :--- |
| Stage 2 | 4.5s，123 个面板，高度 min=21 / 中位=467 / max=2583，panel_id 唯一且全为 `p_` 前缀 |
| 缓存清理 | 预置的 `panel_001.png` 与旧 `panels.json` 全部消失，目录只剩 `p_*.png` + `panels.json` |
| 空镜标记 | 31 个（`p_026`/`p_039`/`p_040`/…），Stage 3 继承后不再调模型 |
| Stage 3 | 80.1s，123 个面板，与 `panels.json` **完全同轨、无重复**；`non_story_type` 分布 `none 88 / transition_blank 31 / sfx_only 3 / copyright_notice 1`；拟声词被当人名 **0 处** |
| Stage 4 | 525.7s，90 条 beats；引用分镜 90 个（86 唯一，4 处一镜多句）；**全部引用都在 `panels.json` 内，越界 0**；`tts_text` 污染 `narration_text` **0 条**；`focus_hint` 分布 `full 60 / bottom 20 / top 9 / center 1`；顺位校验第 1 轮拦到 1 处「第 72 条把镜头拉回 p_080」并自动打回重写 |

### 本轮新增测试（3 条，全量 **388 passed**）

* `test_patch6_clears_stale_panels_before_writing`：预置 `panel_001.png` + 旧清单 → 重跑后只剩 `p_001.png` 与新清单；
* `test_patch6_splits_on_noisy_black_transition_band`：上下复杂画面 + 中间 30px 噪点纯黑带 → 拆成 2 个面板；
* `test_patch6_keeps_cross_slice_scene_and_trims_padding`：跨片画格不被切破（圆墨迹完整 300 行）且切出块上下不留多余白边。

另把两条旧用例改写到新语义（空镜改为「标记而非丢弃」、黑名单尾图改用真实种子图），
并给 `tests/test_cli.py` / `tests/test_web_server.py` 的合成漫画页补上可辨认内容块
（纯色页在新语义下会被判空镜，测试桩需像漫画页）。

## Patch v3.0-7：Story-First 抖音重建（故事为主角，分镜为 B-Roll）

### 为什么重建

旧流程强迫模型「不能跳过任何有效分镜」，解说被逐格画面牵着走，成片像看图写话。
本补丁把叙事权交还给故事：**先讲清一个盲听也成立的完整故事，再顺着故事去挑配图**，
允许跳过 30%~70% 的重复/低信息分镜。只改 `config.example.yaml`、`core/config.py`、
`core/schemas.py`、`prompts/`、`stages/s4_scriptwriter.py`、`cli.py` 与对应测试，
`web/` 与 `core/normalize.py` 未动（`run_stage4` 新参数全部带默认值，旧调用方零改动）。

### 两步撰稿（Stage 4 内部）

1. **4A 故事反写**（`prompts/story_draft.md`，模型取 `story_model`，默认 `qwen3.8-max`，
   留空回退 `script_model`）：整话通读后输出说书体章节稿 `EpisodeStoryDraft`
   （`hook_opening` + `full_story_markdown` + 3~8 个 `StoryChapter`，每章带
   `panel_hint_range` 提示区间与 `story_prose` 正文，全文 550~1100 字、不压 60 秒时长），
   落盘 `04_script/story_draft.json` 与**人工可编辑**的 `04_script/story_draft.md`。
2. **4B 章节拆拍**（`prompts/script_narrate.md`，模型取 `script_model`）：逐章串行调用，
   每章只能在 `assign_chapter_panels()` 算出的**互不重叠、顺序递增**的候选窗口里挑图，
   每条解说 10~45 字、绑定 1~3 张分镜；合并后 `beat_id` 全话重排，再过两道硬校验：
   * `validate_beat_alignment`：引用必须有效、句内严格递增、全局单调不减。
     **本补丁起移除覆盖率检查**——跳多少格都放行，回头讲前一格仍拦截，失败打回重写最多 2 轮；
   * `validate_tts_text_no_pinyin`：`tts_text` 出现拼音（如 `hùn xiǎo`）即报错打回，
     重写耗尽后用 `strip_pinyin()` 兜底剥除。**字幕 `narration_text` 全程只读，绝不被送音逻辑改动。**

### 人工审稿工作流（新增两个 CLI 子命令）

```bash
# 第一步：跑到 4A 停下，产出 story_draft.md 等人工改写（已存在时用 --force 覆盖）
w2v story-draft --series <series_id> --ep 1 [--force]

# 人工直接编辑 workspace/.../episodes/ep_001/04_script/story_draft.md（## 章节标题 + 正文）

# 第二步：读人工稿跑 4B + Stage 5~7 配音渲染出片（缺稿直接报错退出，不会偷偷重写）
w2v narrate-from-story --series <series_id> --ep 1
```

磁盘稿与自动渲染稿不一致即视为人工改稿，4B 直接采用、跳过 4A；`narrate-from-story`
缺故事稿时抛 `ConfigError`（退出码 2），提示先跑 `story-draft`。

### 验收测试（新增 3 条，全量 **401 passed**）

* `test_sparse_panel_selection_passes_but_reverse_is_blocked`：10 格只挑 3 格（跳过 70%）
  且单调递增 → 通过；同样三格倒序 → 「单调不减」拦截；
* `test_validate_tts_text_no_pinyin_rejects_pinyin_accepts_hanzi`：`hùn xiǎo` 报错、
  「混蛋」通过、白名单词（WEBTOON/BGM）不误伤；
* `test_human_revised_story_markdown_skips_4a_and_drives_4b`：磁盘人工稿存在时 4A 零调用
  （请求数从 3 降为 2），4B 请求体含人工稿独有文本，人工稿不会被自动稿覆写。

另含两条 CLI 工作流测试（`test_story_draft_mode_stops_after_stage_4a`、
`test_narrate_from_story_requires_and_uses_markdown`）、一条 thinking 透传测试
（`test_every_stage4_request_disables_thinking`），以及真机回归
`test_retry_feedback_is_routed_to_owning_chapter_only`、
`test_filter_feedback_for_window_routes_errors_by_panel`、
`test_chapter_beat_budget_splits_total_across_chapters`、
`test_split_budget_halves_text_budget`、`test_narrate_prompt_carries_beat_budget`，
以及顺位钳制兜底的两条测试
（`test_clamp_regressive_beats_anchors_to_current_main_panel`、
`test_persistent_reverse_is_clamped_after_retries`）。

### 真机修复：qwen3 系 thinking 撞穿 180s 超时

真机首跑 4A（88 个有效剧情格，prompt 仅 7728 字符）连续 4 次 `ReadTimeout`。
根因不是输入过大，而是 `qwen3.8-max` 默认开启长链思考（thinking），推理 token 撞穿
`api.timeout_s=180`。实测百炼 compatible-mode 统一接受 `enable_thinking=False`
（`qwen3.8-max` / `qwen-plus` 均兼容，参数经 `BailianClient.chat(**kwargs)` 透传，
无需改动 provider 层）：关闭后同一 4A 任务 **34~45s 完成**，产出 4~6 章、768~1432 字、
分镜锚点单调覆盖全话的合法章节稿。`s4_scriptwriter.py` 的全部 4 处 LLM 调用
（概览 / 4A / 4B / 篇章压缩）统一透传该开关，模块常量 `_NO_THINKING_KWARGS`。

### 真机修复：story-draft 模式的 manifest 静默丢状态

`w2v story-draft` 原先向 manifest 写不存在的 stage 名 `s4_story_draft`，
`ManifestManager.update_stage` 抛 `KeyError` 后被 `_manifest_stage` 吞掉，
Web 控制台会对着磁盘上真实存在的初稿显示「待执行」。修复为复用合法 stage
`s4_scriptwriter`，并给 `_manifest_stage` 增加 `artifact_name` 参数，
story-draft 模式记录产物 `story_draft.json`（普通撰稿仍记 `script.json`）。

### 真机修复：4B 反馈按章路由、条数配额与顺位钳制

真机首跑 4B 暴露两个系统性问题，修复后再补一道确定性兜底：

1. **反馈按章路由**：重试循环原先把全话校验错误原样广播给每一章，第 1 章会收到
   自己候选窗口里根本没有的 panel（如 `p_047`）的错误，模型无从修正。新增
   `_filter_feedback_for_window(errors, window_panel_ids)`：含 panel id 的错误只
   投给拥有该 panel 的章节；不含 panel 的错误（拼音 / 空绑定）仍全局广播。
2. **条数配额**：没有总量约束时模型狂写（真机首跑 81 条，超 30~65 规格）。新增
   `_chapter_beat_budget(chapter_count)` 按章平分全话 30~65 条预算（单章下限托底
   3 条），经 `script_narrate.md` 的 `{{BEAT_BUDGET}}` 占位符下发；章节切片与
   截断递归时由 `_split_budget` 同步平分预算。
3. **顺位钳制兜底**：模型在心理活动 / 回忆处常想给旁白配更早的表情格，与全局
   单调不减铁律冲突且三轮重写不收敛。新增 `_clamp_regressive_beats`：重写耗尽后，
   整条回看的 beat 锚定到当前主画面（符合 Prompt「多句可连续停留同一主画面」），
   部分回看 / 内部逆序的 beat 丢弃回看分镜、保留合规分镜；只改 `target_panel_ids`，
   解说与字幕文本一字不动，并向 stderr 逐条打印钳制记录。钳制后重新跑一遍
   `validate_beat_alignment`，保证落盘 `script.json` 100% 单调。

## Patch v3.0-8：抖音安全分区版式（主画面与字幕物理隔离）

### 为什么改

旧版式把分镜等比缩放后居中铺满 1080×1920，字幕只能压在画面底部：人物嘴唇、对白气泡
与字幕挤在同一区域，抖音原生按钮区（y≈1720~1920）还会再吃掉一块。本补丁把画面切成
两个互不重叠的物理分区，从版式上 100% 杜绝字幕压脸 / 压气泡 / 被按钮遮挡。

### 版式规格（`s7_renderer.py` 常量区）

- 画布固定 **1080×1920 / 30fps**。
- **底层背景**：同一张分镜放大裁切铺满全屏，再 `boxblur=30:4,eq=brightness=-0.38:saturation=0.75`
  高斯模糊 + 暗化降饱和，只承载氛围、不承载信息（旧的 `gblur`/`colorchannelmixer` 链路移除）。
- **主画面安全区 y=180~1440**：分镜等比缩放进 **1020×1220** 包围盒（`force_original_aspect_ratio=decrease`，
  不裁内容），`overlay=x=(W-w)/2:y=180+(1260-h)/2`；pad 后前景恒高 1220，叠加下沿
  固定为 **y=1420**，严格 <= 1450 / 1440 上限。
- **超长竖格**（源图 `高 > 1.5 × 宽`）的 `pan_down` 走专用链路：宽度铺满 1020
  （`scale=1020:-2`），`zoompan z=1.0` 在 1020×1220 视窗内 `y=(ih-1220)*on/(frames-1)`
  从顶匀速扫到底，替代旧链路「缩小 + pad + 1.12 倍放大」的假运镜。源尺寸由
  `_read_image_size`（PIL）在渲染分段时读取；`focus_band` 取景与普通比例场景不受影响。
- **底部字幕安全区 y≈1530~1670**：`s6_timeline.py` 的 ASS 与字幕 PNG 统一
  `MarginV=250`、左右边距 65、字号 56；ASS 改 `WrapStyle: 0`（智能均分换行）、
  `OutlineColour=&H00121216`。字幕锚区在主画面下沿 1440 之下、抖音遮挡区 1720 之上。

### 验收测试（新增 3 条，全量 **404 passed**）

1. `test_ass_subtitle_uses_douyin_safe_zone`：ASS 含 `PlayResX: 1080` / `PlayResY: 1920`
   / `WrapStyle: 0`，Style 行 MarginV=250（落在规范区间 220~280），字幕锚区 1440 < 1670 <= 1700。
2. `test_foreground_overlay_never_crosses_safe_bottom`：滤镜链含固定 overlay 公式，
   推算叠加起点 y=200、终点 y=1420，严格 <= 1450 与 `SAFE_BOTTOM_LIMIT=1440`。
3. `test_tall_panel_pan_down_scans_inside_safe_window`：800×2400 超长格触发
   `scale=1020:-2` + 视窗内扫底（无 1.12 倍放大、无 decrease/pad）；普通比例与
   static 场景保持原链路。
   另改写 `test_build_scene_filter_shape` 对齐新滤镜（boxblur/eq、`s=1020x1220`、新 overlay）。

### 真机验证（wt_5081《唯一的希望》第 1 话，从 Stage 2 全量重跑）

- **ffmpeg 滤镜冒烟**：普通格与超长竖格两条滤镜链真实编码均通过，输出 1080×1920。
- **端到端重跑**：81 张 raw → 123 个面板（35 个非剧情格过滤）→ 4A 故事稿 5 章 975 汉字
  → 4B 产出 **40 条 beats**（规格 30~65），顺位钳制修正 6 条后**顺位违规 0**，
  跳过率 **62%**（88 有效格引用 33，规格 30~70%），tts 拼音残留 0，narration 15~30 汉字。
- **成片**：`07_out/wt_5081_ep_001.mp4` 与 `exports/EP001_唯一的希望.mp4`，
  1080×1920 / 30fps / h264+aac / **215.9s** / 27.6 MB。
- **像素级抽帧**（t=12s、t=60s）：字幕文字行落在 y=1461~1650 / 1462~1582（安全区
  1460~1700 内）；抖音遮挡区 y=1720~1920 亮像素 **0.00%**；分区间隙 y=1440~1460
  亮像素 **0.00%**；主画面在 y≈1420 结束（与 overlay 公式终点 1420 实测吻合）。
- **超长竖格**：4 个 pan_down 场景中 p_065（690×2583，ratio 3.74）、p_115
  （690×1127，ratio 1.63）命中宽度铺满 + 视窗扫底链路，其余普通格保持通用链路。
