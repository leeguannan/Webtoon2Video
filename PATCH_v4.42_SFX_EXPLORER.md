# PATCH v4.42：Studio 音效资源管理器标签页

> 目标：把 v4.40 引入的「句中音效标签 `[sfx:key]`」从"藏在浮层里的卡片"升级成
> **一个可以挑、可以比、可以查来源的独立标签页** —— 用户不必逐句点开浮层，
> 就能看清「这个音效到底是什么、多长、从哪来的（CC0 溯源）」，
> 然后在**光标所在的位置**一键插入音效标签。

- 基线提交：`da1dace`（PATCH v4.41 句内音效与整句级音效分档增益）
- 落地提交：见文末「提交与推送」
- 影响面：后端 2 个文件、前端 3 个文件（其中 1 个新建）、新增测试 1 个文件

---

## 一、为什么要做这个补丁

v4.40 交付之后，`[sfx:key]` 标签的**插入**能力已经有了（解说台每句的送音浮层里
新增了「句中音效」Tab），但用起来的实际体验有三个断层：

| 断层 | 现象 | 后果 |
| --- | --- | --- |
| **信息断层** | 浮层里只有「中文名 + 标签」两行，用户不知道这个音效**多长**、**是不是自己下的**、**来自哪个作者** | 选音效纯靠猜，命中全凭记忆 |
| **对比断层** | 24 个音效散在一堆卡片里，想在「雷声」和「轰击」之间比较时无从下手 | 挑音效要来回翻 |
| **定位断层** | 标签要插到句子中间某个位置时，只能在**解说台**的文本框里手动敲 `[sfx:xxx]` | 手敲容易写错 key、大小写、方括号 |

而曲库里**其实已经有一整套 CC0 溯源信息**（v4.38.1 的采编工具
`backend/scripts/fetch_cc0_sfx.py` 写下的 `backend/assets/sfx/SOURCES_CC0.json`），
但后端 `/api/sfx-library` 接口从来没把它读出来 —— 信息就在盘面上躺着，只是没接上。

本补丁就是把这些信息**接上、露出来、并做成一个独立可操作的工作台**。

---

## 二、四条铁律

1. **不碰字幕原文**：`[sfx:key]` 只允许写进 `tts_text`（送音文本）。
   `narration_text` 一个字都不能动 —— 画面字幕永远读 `narration_text`，
   所以音效标签**绝不会印在画面上**。
2. **送音文本留空时以字幕原文为底本**：直接往空串里插标签会让整句正文消失。
   资源管理器显示的是「送音文本（有则用之）否则字幕原文」，并在界面上明确提示
   「当前 `tts_text` 留空 · 应用时将基于字幕原文写入 `[sfx:key]`」。
3. **溯源信息是锦上添花，绝不能拖垮曲库**：`SOURCES_CC0.json` 缺失 / 损坏 /
   结构不对时，`GET /api/sfx-library` 仍必须 200，24 条条目一项不少。
4. **既有契约不动**：v4.38 起就有的 `key / label / category / available /
   resolved_filename / fallback_key / url` 字段一个都不改、不能删，
   新字段只能往后加。

---

## 三、文件白名单

| 文件 | 动作 | 改动规模 | 说明 |
| --- | --- | --- | --- |
| `backend/src/core/normalize.py` | 改 | +31 / −0 | `SFX_CUE_CATALOG` 每条加 `group`；新增 `SFX_GROUP_CATEGORIES` 与 `sfx_group_of()` |
| `backend/src/web/server.py` | 改 | +92 / −8 | 曲库接口读 CC0 溯源清单、补 `duration_ms / source_type / cc0_source` |
| `src/types/studio.ts` | 改 | +26 / −0 | `SfxGroupCategory`、`SfxCc0SourceMeta`，`SfxLibraryItem` 补 4 个可选字段 |
| `src/components/SfxExplorerTab.tsx` | **新增** | +490 | 音效资源管理器标签页（3 块结构） |
| `src/components/EpisodeStudioView.tsx` | 改 | +126 / −4 | `StudioTab` 加 `'sfx'`、光标追踪、8 个新 handler、标签页渲染 |
| `backend/tests/test_patch_v442_sfx_explorer.py` | **新增** | +311 | 24 条离线用例 |
| `PATCH_v4.42_SFX_EXPLORER.md` | **新增** | 本文 | 本文档 |

合计 **6 个文件（含 2 个新建），+1076 / −12**。

---

## 四、实现明细

### Part 1 · 后端：曲库接口长出"分类 / 时长 / 溯源"三根新触角

#### 4.1.1 一级大类（`backend/src/core/normalize.py`）

`category`（细类，如「击打」「爆炸」）之上再补一层 `group`（一级大类）：

| 一级大类 | 条数 | 成员 |
| --- | --- | --- |
| 动作 | 8 | `heart_thump` `door_slam` `door_open` `footstep` `slap` `punch` `sword_clash` `cloth_rustle` |
| 转场 | 4 | `whoosh` `gasp` `boom_hit` `applause` |
| 环境 | 5 | `crash` `glass_shatter` `thunder` `clock_tick` `page_turn` |
| 道具 | 7 | `phone_msg` `phone_ringing` `phone_vibrating` `phone_call` `phone_unavailable` `camera_shutter` `car_horn` |

```python
SFX_GROUP_CATEGORIES: tuple[str, ...] = ("动作", "环境", "转场", "道具")

def sfx_group_of(sfx_key: str) -> str:
    """查某个音效键的一级大类；未知键返回空串。"""
```

`SFX_CUE_CATALOG` 每条**直接内联** `"group"` 字段，`key / label / category /
fallback_key` 四个既有字段一个字没动。

#### 4.1.2 溯源清单读取（`backend/src/web/server.py`）

```python
SFX_SOURCES_FILENAME = "SOURCES_CC0.json"
_SFX_DURATION_CACHE: dict[tuple[str, float, int], int] = {}   # (路径, mtime, size) → 毫秒

def _sfx_sources_index() -> dict[str, dict[str, Any]]:
    """读 CC0 采编工具写的溯源清单。文件缺失 / 非法 JSON / 结构不是字典
    —— 一律回退成空字典，绝不抛异常。"""

def _sfx_duration_ms(path: Path | None) -> int | None:
    """读音效文件时长（毫秒）；读不出来返回 None。"""
```

**时长探测为什么要缓存**：曲库接口一次要报 24 条素材的时长，每条都要起一次
`ffprobe` 进程 —— 24 次进程启动会让接口慢到不可用。缓存键取
`(路径, mtime, size)`，素材被覆盖写时 `mtime` 会变，自动失效。

**时长取值的优先级**：

1. 盘面真实素材的实测时长（`_sfx_duration_ms`）；
2. 实测拿不到（文件不存在 / 探测失败）→ 回退溯源清单里的 `duration_s × 1000`；
3. 都没有 → `None`（前端显示 `—`）。

#### 4.1.3 曲库条目新增字段

```jsonc
{
  "key": "thunder",
  "label": "雷声",
  "category": "爆炸",          // 既有
  "group": "环境",             // 新增：一级大类
  "available": true,           // 既有
  "resolved_filename": "thunder.mp3",
  "fallback_key": "crash",
  "url": "/api/sfx-file/thunder",
  "duration_ms": 1794,         // 新增：生效素材的实测时长
  "source_type": "freesound_cc0",  // 新增：freesound_cc0 | builtin_cc0
  "cc0_source": {              // 新增：溯源明细（内置素材为 null）
    "sound_id": "484852",
    "name": "Thunder.",
    "author": "PappaBert",
    "license": "License: Creative Commons 0",
    "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
    "freesound_url": "https://freesound.org/people/PappaBert/sounds/484852/",
    "fetched_at": "2026-11-25T...",
    "max_duration_s": 3.0
  }
}
```

`source_type` 的判定很简单：**在溯源清单里就是 `freesound_cc0`（采编工具下的），
不在就是 `builtin_cc0`（仓库内置资产）**。

---

### Part 2 · 前端类型（`src/types/studio.ts`）

```ts
export type SfxGroupCategory = '动作' | '环境' | '转场' | '道具';

export interface SfxCc0SourceMeta {
  sound_id?: string; name?: string; author?: string; license?: string;
  license_url?: string; freesound_url?: string; fetched_at?: string;
  max_duration_s?: number;
}

export interface SfxLibraryItem {
  // ...既有字段不动
  group?: SfxGroupCategory;
  duration_ms?: number | null;
  source_type?: 'freesound_cc0' | 'builtin_cc0';
  cc0_source?: SfxCc0SourceMeta | null;
}
```

全部标成**可选**，旧版后端返回的响应依然能直接用（向后兼容）。

---

### Part 3 · 前端新组件（`src/components/SfxExplorerTab.tsx`，490 行）

标签页由三块结构组成，从上到下就是一条工作流：

```
┌──────────────────────────────────────────────────────────┐
│ ① 插入目标与光标预览条                                      │
│   [插入目标 ▾ b5 · "道振……他的手怎么会缠着绷带……！"]       │
│   b5 · 第 17 字符处        [返回解说台查看] [保存脚本并从 Stage 5 重跑配音] │
│   ┌───────────────────────────────────────────────────┐  │
│   │ 送音文本（可编辑，点击即可调整光标位置）                │  │
│   │ 道振……他的手怎么会缠着绷带……！                      │  │
│   └───────────────────────────────────────────────────┘  │
│   …道振……他的手怎么会缠着绷带……！▏光标…                  │
│                                  插入位置＝第 17 字符      │
├──────────────────────────────────────────────────────────┤
│ ② 分类筛选与关键词检索                                      │
│   [全部 (24)] [动作 (8)] [环境 (5)] [转场 (4)] [道具 (7)]     │
│   [🔍 搜索音效键 / 中文名 / 分类 / 文件名 / CC0 作者]  命中 5/24 │
├──────────────────────────────────────────────────────────┤
│ ③ CC0 预处理音效资源表                                      │
│   音效        标签          分类        时长   CC0 来源    操作  │
│   心跳        [sfx:heart_thump] 动作·生理 8.50s 仓库内置CC0资产 ♪ 应用 │
│   雷声        [sfx:thunder]     环境·爆炸 1.79s Freesound #484852·PappaBert │
│   thunder.mp3                             查看原始素材页           │
└──────────────────────────────────────────────────────────┘
```

**几个关键设计**：

- **底本口径**：`baseText = tts_text 非空 ? tts_text : narration_text`。
  送音文本留空时用字幕原文兜底（铁律二），并挂一条琥珀色提示说明
  「当前 `tts_text` 留空 · 应用时将基于字幕原文写入 `[sfx:key]`
  （字幕原文本身不会被改动，画面字幕仍读 narration_text）」。
- **光标是父组件持有的**：`caret` / `onChangeCaret` 由 `EpisodeStudioView` 掌控，
  所以解说台和资源管理器**指向同一个位置**（见 Part 4）。
- **锚点预览**：光标前后各取 18 个字符，中间插一个 `▏光标` 标记，
  肉眼看得出「要插在哪儿」。
- **兜底提示**：素材走了 `fallback_key` 回源时（文件名不以 key 开头），
  额外标一行 `兜底 → door_slam`。
- **本地编辑即时回写**：资源管理器里的文本框直接可改，改动通过
  `onChangeTargetText` 回写父组件的草稿（和改送音文本是同一条路径）。
- **筛选维度**：一级大类分段按钮 + 关键词搜索（key / 中文名 / category /
  文件名 / CC0 素材名 / CC0 作者），空态给「重置筛选」。

---

### Part 4 · 前端接线（`src/components/EpisodeStudioView.tsx`）

#### 4.4.1 标签页入口

```ts
type StudioTab = 'panels' | 'storyboard' | 'script' | 'sfx' | 'timeline' | 'preview';
```

标签栏在「解说与多音字审核」**右边**插入
`音效资源管理器 (${sfxLibrary.length})` —— 放这个位置是因为
「给这一句加音效」本来就是解说审核的配套动作。

#### 4.4.2 光标追踪：谁是「当前目标句」

音效资源管理器要知道 `[sfx:key]` 插到哪一句的哪个位置，
**唯一可靠来源就是用户在解说台最后一次点开的句子与光标位置**。

因此送音文本框新增了一条 `onTtsActivate` 回调：

```ts
// ScriptBeatRowProps
onTtsActivate?: (beatId: string, start: number, end: number) => void;
```

触发点覆盖：`onFocus`（新加）、`onSelect` / `onClick` / `onKeyUp` / `onBlur`
（原有的 `rememberSelection` 里顺带调用），以及折叠态
「展开送音文本编辑」按钮的 onClick（按 0 位置激活）。

父组件侧：

```ts
const [activeTtsBeatId, setActiveTtsBeatId] = useState<string | null>(null);
const [sfxCaret, setSfxCaret] = useState(0);

const handleTtsActivate = useCallback((beatId, start, end) => {
  setActiveTtsBeatId(beatId);
  setSfxCaret(start);
  ttsSelections.current[beatId] = { start, end };
}, []);
```

#### 4.4.3 七个新 handler

| handler | 职责 |
| --- | --- |
| `handleTtsActivate` | 记录最后激活的送音文本框与光标 |
| `handleApplySfxFromExplorer` | **核心**：把 `[sfx:key]` 插到目标句光标处 |
| `handleSfxExplorerChangeTarget` | 换目标句时，光标回到该句上次的位置 |
| `handleSfxExplorerChangeTargetText` | 资源管理器里直接改送音文本 |
| `handleSfxExplorerBackToScript` | 回解说台并定位到那一句 |
| `handleSfxExplorerSaveAndRerunTts` | 保存脚本并从 Stage 5 重跑配音 |
| `handleSfxExplorerPreview` | 试听音效（复用全局单一 Audio 实例） |

插入逻辑（`handleApplySfxFromExplorer`）：

```ts
const current = (beat.tts_text ?? '').trim();
const base = current !== '' ? (beat.tts_text ?? '') : (beat.narration_text ?? '');
if (base.trim() === '') { setSaveHint('这一句既没有送音文本也没有字幕原文，插不了音效标签。'); return; }
const at = Math.max(0, Math.min(sfxCaret, base.length));
const next = `${base.slice(0, at)}${snippet}${base.slice(at)}`;
updateBeatField(targetBeatId, 'tts_text', next);
ttsSelections.current[targetBeatId] = { start: at + snippet.length, end: at + snippet.length };
setSfxCaret(at + snippet.length);
setSaveHint(`已在 ${targetBeatId} 插入 ${snippet}；保存后从 Stage 5 重跑配音才会生效。`);
```

插入后光标**自动落到标签末尾**，可以连续插多个音效。

`updateBeatField` 走的是既有的草稿通道，因此**自动触发未保存标记**，
命令栏会显示「N 处未保存」。

#### 4.4.4 「保存脚本并从 Stage 5 重跑配音」

复用既有的 `handleSaveBeats(true)` —— 它内部走 `persistBeats(beats, true, ...)`，
App 侧会自动带上 `from_stage: 's5_tts'` + `mode: 'full_video'`，
**不需要重跑 S1~S4，也不需要重新出图**。

---

### Part 5 · 测试（`backend/tests/test_patch_v442_sfx_explorer.py`，24 条）

| 组 | 条数 | 覆盖内容 |
| --- | --- | --- |
| 1. 一级大类 | 8 | 四大类齐全；24 条都有 group；分组与规格逐一一致；条数 8/4/5/7；分组恰好划分全部 key；**未改动既有字段**；未知键返回空串；接口逐条给 group；接口分组计数一致 |
| 2. 新字段 | 6 | 三件套字段齐全；无溯源文件时全是 `builtin_cc0` + `cc0_source=None`；有溯源时 thunder 是 `freesound_cc0` 且明细 8 字段齐全；探测失败回退 `duration_s×1000`；两处都没有时 `duration_ms=None`；有真实 wav 时报实测时长；溯源字段不全时空串兜底不炸；**时长缓存命中**（同素材不重复探测） |
| 3. 健壮性 | 5 | 溯源文件缺失 → 200；文件损坏（非法 JSON）→ 200；结构不是字典 → 200；只覆盖部分 key → 其余照常 `builtin_cc0`；**v4.38 的既有字段一项不少** |
| 4. 兜底素材 | 2 | 盘面没有 `slap.mp3` 但有 `door_slam.mp3` → `slap` 指向 `door_slam.mp3` 且 `available=true`；盘面什么都没有 → `available=false`、`resolved_filename=""` |

全部离线：纯函数 + `TestClient` + 临时盘面，不触网、不消耗 API 额度。

**一个写测试时踩到的坑**（值得记下来）：`web/server.py` 里
`assets_root` 是 `from core.config import assets_root` 直接绑定的**模块属性**，
所以 `monkeypatch.setattr(s7_renderer, "assets_root", ...)` **改不动它** ——
必须同时改 `web.server.assets_root`，否则测试会读到仓库真实的
`backend/assets/sfx/`，导致「没有溯源文件」的用例莫名其妙拿到 13 条 `freesound_cc0`。

---

## 五、验证结果

### 5.1 静态与自动化

| 命令 | 结果 |
| --- | --- |
| `cd backend && .venv/bin/python -m pytest tests/test_patch_v442_sfx_explorer.py -q` | **24 passed** |
| `cd backend && .venv/bin/python -m pytest -n auto --dist loadfile -q` | **全量通过（EXIT 0）** |
| `cd backend && /opt/homebrew/bin/ruff check src tests` | **All checks passed!** |
| `npx tsc --noEmit` | **零错误（EXIT 0）** |
| `npm run build` | **✓ built in 200ms** |

### 5.2 接口实测（后端重启加载 v4.42 后）

```
GET /api/sfx-library
  items: 24
  group:       {'动作': 8, '转场': 4, '环境': 5, '道具': 7}
  source_type: {'builtin_cc0': 11, 'freesound_cc0': 13}
  with duration: 24
  thunder → {"group":"环境","duration_ms":1794,"source_type":"freesound_cc0",
             "cc0_source":{"sound_id":"484852","name":"Thunder.","author":"PappaBert",
                           "license":"License: Creative Commons 0", ...}}
```

### 5.3 真机界面验证（Chromium 1440×860，话次 wt_5081_ep_008，97 句）

| 验证项 | 结果 |
| --- | --- |
| 标签栏出现「音效资源管理器 (24)」 | ✓ |
| 一级大类分段按钮 | 全部 (24) / 动作 (8) / 环境 (5) / 转场 (4) / 道具 (7)，点击「环境」→ 表格只剩 5 行、命中 5/24 |
| 关键词搜索 | 搜「雷」→ 命中 1/24，唯一命中 `[sfx:thunder]` |
| 资源表 6 列 | 中文名 / 素材文件 / `[sfx:key]` / 一级大类·细类 / 时长 / CC0 来源 / 试听+应用，24 行齐全 |
| CC0 来源展示 | 采编素材显示 `Freesound #564230 · niijiiiii... / Huge Slap in the Face / 查看原始素材页`；内置素材显示「仓库内置 CC0 资产」 |
| 应用（插入） | b1 送音文本 → `[sfx:slap]恩彗从昏迷中醒来…`；光标自动落到标签末尾 |
| 应用（连续） | 再插 `[sfx:door_slam]` → 光标自动推进，标签落在新位置 |
| 切换目标句 | 下拉切到 b3 → 文本框换成 b3 的正文、光标归 0 |
| 解说台 → 资源管理器联动 | 在解说台点 b5 的送音文本框（光标落在第 17 字符）→ 切到资源管理器，目标自动变成 **b5 · 第 17 字符处** |
| 未保存标记 | 插入标签后解说台命令栏显示「**1 处未保存**」 |
| 「返回解说台查看」 | 切回解说台并定位到该句 |
| 「保存脚本并从 Stage 5 重跑配音」 | 按钮存在且可用（未在验证中点击，避免触发真实 TTS 重跑） |
| 试听按钮 | 每行一个（Volume2 图标），素材缺失时禁用 |

**验证纪律**：验证过程中的插入操作**只停留在浏览器草稿**，未点保存；
关闭会话后复查磁盘 `workspace/wt_5081/episodes/ep_008/04_script/script.json`
—— 97 句、`tts_text` 无任何 `[sfx:` 标签，**磁盘数据未被污染**。

---

## 六、已知限制

1. **时长口径不统一**：内置素材是仓库原始资产（未做 v4.38.1 的峰值归一化），
   所以 `heart_thump` 显示 8.50s —— 真实入库时它会被 S7 的 LUFS 校准拉响，
   但用户看到的是「8.5 秒的心跳」这种偏长的数字，可能误判。采编工具下的
   13 条素材都经过了 3.0s 截断 + 峰值归一化，时长更贴近实际使用感。
2. **资源管理器不是音效编辑器**：只能试听与插入，不能裁剪、改音量、换素材。
   增益分档由 v4.41 的 `inline_sfx_gain_db`（默认 -6dB）在 S7 统一处理。
3. **试听用的是原素材**：不带 v4.41 的内联降档，也不带 S7 的 LUFS 校准，
   听感会比成片略响一点。
4. **目标句只有一句**：资源管理器一次只对一句生效；批量给整话加音效
   仍需回到解说台逐句操作。
5. **搜索是子串匹配**：中文按子串搜（搜「雷」会命中「雷声」），
   不支持拼音或拼音首字母。

---

## 七、后续建议

1. **把「素材实际时长」和「入库时长」分开显示**：内置素材可以补一次
   v4.38.1 同款的峰值归一化预处理（3.0s 截断 + -3dBFS），让 24 条素材
   在时长与响度两个维度上口径一致。
2. **资源管理器支持多选批量插入**：现在一次只能对一句，
   可以扩展成「选中 N 句 → 统一插入同一个标签」。
3. **给资源管理器加一个「本话已用音效」视图**：把当前话次里已经插入的
   `[sfx:key]` 汇总出来，避免同一个音效在一话里重复十几次。

---

## 八、提交与推送

| 项 | 值 |
| --- | --- |
| 基线 | `da1dace`（PATCH v4.41） |
| 落地提交 | `PATCH_v4.42: Studio 音效资源管理器标签页` |
| 推送 | `git -c http.version=HTTP/1.1 push origin main` |
| 提交序列 | `PATCH_v4.42` → `da1dace`(v4.41) → `1de4819`(v4.40) → `36bfd64`(v4.38.1) → `12532a3`(v4.39) → `607eff5`(v4.38) → `a40c4bb`(v4.31) → `0f02bb2`(v4.32) → `af76551`(v4.35) → `613401d`(v4.36) → `057fadb`(v4.37) |
