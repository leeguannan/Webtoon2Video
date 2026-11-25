# PATCH v4.43 —— 全局音效资源库（增删改替换）+ 纯 `[sfx:key]` 单入口（方案 B）+ S4 上下文语义推荐

- **基线**：`d3819f1`（PATCH v4.42：Studio 音效资源管理器标签页）
- **性质**：音效能力从「单话内逐句选」升级为「全局资源库 + 单一口令」
- **涉及文件**：后端 7 个（改 6 + 改测试 1）、前端 4 个（改 3 + 删 1 + 新增 1）、新增测试 1、新增文档 2

---

## 一、为什么做

v4.38~v4.42 把音效从「没有」一路补到「能用」，但留下三个结构性断层：

| 编号 | 断层 | 用户感受 |
| :--- | :--- | :--- |
| ① | 音效只能选**内置那 24 条**，用户没法加自己的素材 | 想用一段自己的「刀锋划过」做不到 |
| ② | 音效入口**散在单话审核台里**（v4.42 的独立标签页） | 换个作品想统一管理素材，得逐个话次打开 |
| ③ | 整句级 `sfx_cue` 与句内 `[sfx:key]` **两套入口并存** | 不知道该用哪个；两个都填了行为还不一样 |

再加一条**隐藏断层**（v4.43 顺带根治）：`apply_auto_motions` / `apply_narrative_pacing`
这两套自动规则原本只读 `beat["sfx_cue"]`。一旦按新方案只写 `[sfx:key]`，
**高燃句不再震、转折句不再留白** —— 画面动效与叙事节奏会集体退化，而且不报错。

---

## 二、五条铁律

1. **音效只有一个入口**：送音文本里的 `[sfx:key]`。旧 `sfx_cue` / `sfx_position`
   保留为**兼容字段**，老剧本照旧能读能放，新稿不再输出。
2. **字幕绝不出现音效标签**：`[sfx:key]` 只写 `tts_text`，画面字幕仍读
   `narration_text`，两边永不混。
3. **曲库是全局的**：素材增删改在「音效资源库」这一个页面完成，
   所有话次、所有作品共用同一份曲库。
4. **内置素材永远可还原**：停用一个内置音效只是从生效曲库里摘掉，
   仓库里的默认音频**一个字节都不删**，随时一键还原。
5. **用户上传优先但不覆盖仓库**：用户上传的音频落在
   `assets/sfx/custom/<key>.<ext>`，与仓库默认素材**分目录存放**，
   删自定义 / 还原默认互不牵连。

---

## 三、文件白名单

### 后端

| 文件 | 改动 | 说明 |
| :--- | :--- | :--- |
| `backend/src/core/normalize.py` | +325 | 动态曲库：注册表读写 + 生效曲库合并 + 建议归一化 |
| `backend/src/core/schemas.py` | +32 | `SfxSuggestion` 模型 + `ScriptBeat.sfx_suggestions` |
| `backend/src/stages/s4_scriptwriter.py` | +192 | 上下文建议器 + 方案 B 联动（`_extract_beat_sfx_keys`） |
| `backend/src/stages/s7_renderer.py` | +25 | `resolve_sfx_asset` 改查动态曲库 + 自定义素材优先 |
| `backend/src/web/server.py` | +386 | 曲库 CRUD 路由 + 动态曲库 payload + 上传预处理 |
| `backend/prompts/script_narrate.md` | +39 | prompt 升级为方案 B 统一规范 |
| `backend/tests/test_schemas.py` | +2 | 字段锁补 `sfx_suggestions` |
| `.gitignore` | +3 | 运行时用户数据不入库：`backend/assets/sfx/custom_sfx.json`、`backend/assets/sfx/custom/` |

### 前端

| 文件 | 改动 | 说明 |
| :--- | :--- | :--- |
| `src/components/SfxLibraryView.tsx` | **新增 618 行** | 全局音效资源库页面 |
| `src/components/SfxExplorerTab.tsx` | **删除 490 行** | 单话内音效标签页被全局页取代 |
| `src/components/EpisodeStudioView.tsx` | +506/−506 | 收口：删逐句定位代码与整句级音效控件，新增 AI 音效建议栏 |
| `src/App.tsx` | +47 | 新增 `NavView='sfx'`、全局曲库 state、下发 `sfxLibraryItems` |
| `src/api/studioApi.ts` | +75 | 曲库 CRUD 四个 API 封装 |
| `src/types/studio.ts` | +28 | `SfxSuggestion` / `is_builtin` / `has_custom_audio` 等类型 |

### 测试与文档

| 文件 | 说明 |
| :--- | :--- |
| `backend/tests/test_patch_v443_sfx_repo_and_s4_recommender.py` | **新增 598 行 / 36 例** |
| `PATCH_v4.43_SFX_GLOBAL_REPO.md` | 本文件 |
| `PATCH_v4.42.1_SFX_EXPLORER_FIXES.md` | v4.42.1 的三处交互修复（见 §8 说明） |

---

## 四、实现明细

### Part 1｜动态曲库（`core/normalize.py`）

**注册表文件**：`assets/sfx/custom_sfx.json`

```json
{
  "custom":   [{"key": "knife_slice", "label": "刀锋划过", "group": "动作", "category": "击打", "fallback_key": ""}],
  "overrides": {"heart_thump": {"label": "咚咚咚"}},
  "hidden":   ["thunder"]
}
```

**新增 API**：

| 函数 | 行为 |
| :--- | :--- |
| `load_custom_sfx_config(assets_dir=None)` | 读注册表；**文件缺失 / 不是合法 JSON / 结构不是字典 → 一律回退成空配置**，绝不抛 |
| `save_custom_sfx_config(config, assets_dir=None)` | 原子写（先写 `.tmp` 再 `replace`），写完清缓存 |
| `get_effective_sfx_catalog(assets_dir=None)` | 内置 24 + overrides + custom − hidden，按 mtime 缓存 |
| `get_effective_sfx_keys(assets_dir=None)` | 生效曲库的全部 key |
| `is_valid_sfx_key(key, assets_dir=None)` | key 是否在生效曲库里 |
| `normalize_sfx_suggestions(raw, *, sfx_cue=None, assets_dir=None)` | 建议归一化：别名收敛 → 动态有效性校验 → 按 key 去重 → 最多 3 条；旧数据只填了 `sfx_cue` 时**自动转成一条建议** |

**内置 24 条一字未改**：`SFX_CUE_CATALOG` / `SFX_CUE_KEYS` 保持原样，
v4.38 的旧单测继续全绿。新增的 `sfx_group_of` / `sfx_fallback_key` / `normalize_inline_sfx_tags`
都改成**查动态曲库**（新增可选关键字参数 `assets_dir`，向后兼容旧调用）。

**关键设计**：`_sfx_assets_root()` 里 `from core.config import assets_root` 是**函数内局部导入** ——
`core.config` 不依赖 `core.normalize`，但顶层互相 import 在测试里容易踩循环导入。

### Part 2｜S4 上下文语义推荐

**模型**：

```python
class SfxSuggestion(BaseModel):
    key: str
    reason: str = ""
    source: Literal["llm", "context_rule"] = "llm"
    anchor_text: str = ""

class ScriptBeat(BaseModel):
    ...
    sfx_suggestions: list[SfxSuggestion] = Field(default_factory=list)
```

**规则侧补充器** `enrich_sfx_suggestions(beats, vision_panels=None, *, scenes=None)`：
模型给的建议是第一手判断，但会漏掉明显该配音效的句子。三条确定性规则补全：

1. **台词关键词** —— 22 组「关键词 → 音效键」映射（`一巴掌`→`slap`、`雷声`→`thunder` …）；
2. **画面拟声词** —— Stage 3 识图给出的该分镜 `sfx_list` 命中关键词；
3. **切幕语境** —— 新一幕的第一句配一个转场音效 `whoosh`。

规则：只从**当前生效曲库**里挑；句里已有的内联标签 / 旧 `sfx_cue` 不重复推荐；
按 key 去重、每句最多 3 条；规则补出来的 `source="context_rule"`。

**方案 B 联动（本次最关键的一处修复）**：

```python
def _extract_beat_sfx_keys(beat) -> list[str]:
    """取出这一句里出现的全部音效键（内联标签 + 旧整句级 sfx_cue）。"""
```

- `_beat_is_impact`（高燃 → `shake`）改用它；
- `apply_narrative_pacing` 规则 B 改判 `any(key in PACE_TURN_SFX for key in sfx_keys)`；
- `_beat_emotion` 的兜底文本也带上音效键文本。

→ **纯 `[sfx:key]` 模式下，画面动效与叙事节奏不再失效。**

**prompt 升级**（`backend/prompts/script_narrate.md`）：

- 【铁律二点九】改标题为「音效统一入口（Patch v4.43 方案 B）」，注明 `sfx_cue` /
  `sfx_position` 只是旧数据兼容字段、新稿不输出，24 key 表格保留，
  **自定义 key 没有清单不得臆造**；
- 【铁律三之一之二】改成方案 B 标签规范（去掉 `sfx_position` 推荐）；
- **新增【铁律三之一之三】** —— `sfx_suggestions` 输出规范：
  0~3 条、`reason` ≤ 20 字、`anchor_text` 取自 `narration_text`、
  已写标签的句子不再给同 key 建议、把握不大就别给。

### Part 3｜Server 曲库 CRUD

| 路由 | 行为 |
| :--- | :--- |
| `GET /api/sfx-library` | 动态生效曲库；每条带 `is_builtin` / `has_custom_audio` / `source_type`（`custom_upload` / `freesound_cc0` / `builtin_cc0`）/ `group` / `duration_ms` / `cc0_source`；顶层带 `hidden_builtin_keys` / `custom_keys` |
| `GET /api/sfx-file/{sfx_key}` | 校验改用 `is_valid_sfx_key`（动态曲库）；找不到 400 / 404 |
| `POST /api/sfx-library` | 新增自定义音效：key 须匹配 `^[a-z][a-z0-9_]{1,39}$` 且不与已有生效条目冲突；可同时上传音频 |
| `PATCH /api/sfx-library/{key}` | 改 `label` / `group` / `category` / `fallback_key`（自定义改条目、内置写 overrides） |
| `POST /api/sfx-library/upload` | 放开自定义与内置；替换前删同 key 旧扩展名文件；清 `_SFX_DURATION_CACHE` |
| `DELETE /api/sfx-library/{key}` | `mode=delete`（默认）删自定义条目连音频；`mode=restore_audio` 还原覆盖音频；`mode=disable` 加入 hidden 停用 |
| `POST /api/sfx-library/{key}/restore` | 把已停用的内置音效重新启用 |

**上传音频预处理**：`_sfx_preprocess_audio()` 走 FFmpeg
（`silenceremove` 去首尾静音 → `loudnorm=I=-16:TP=-1.5:LRA=11` → `-ar 44100 -ac 2`，
按后缀选 `libmp3lame` / `pcm_s16le` / `aac` / `libvorbis`）。
**预处理失败（没装 FFmpeg / 编码不支持 / 文件损坏）时安全直存原始文件** ——
音效差一点处理总比整条音效用不了强。

**素材查找顺序**（`s7_renderer.resolve_sfx_asset`）：

```
sfx/custom/<key>.*   ← 用户上传（优先）
sfx/<key>.*          ← 仓库默认 / 采编素材
catalog fallback_key ← 动态曲库的兜底（同样先找 custom 再找默认）
```

### Part 4｜前端

**新增全局页** `src/components/SfxLibraryView.tsx`：

- 一级大类筛选（全部 / 动作 / 环境 / 转场 / 道具，带计数）+ 关键词搜索 + 命中计数；
- 新增自定义音效表单：音效键（按后端同款正则前端先挡一道）、中文名、一级大类、细类，
  可选「仅登记」或「上传音频并新增」；
- 资源表 5 列（音效 / 分类 / 时长 / 来源 / 操作），操作含试听、编辑信息、上传替换音频、删除；
- 「已停用的内置音效」区可一键还原；
- 所有写操作成功后回传后端返回的最新曲库（`onLibraryChange`），App 据此同步给审核台与标签浮层。

**审核台收口** `src/components/EpisodeStudioView.tsx`：

- `StudioTab` 去掉 `'sfx'`（回到 `panels | storyboard | script | timeline | preview`）；
- 删除逐句定位相关代码（`activeTtsBeatId` / `sfxCaret` / `handleTtsActivate` /
  `handleApplySfxFromExplorer` / 全部 `handleSfxExplorer*`）；
- **移除解说台右侧栏整句级「音效下拉 + 试听 + 首/中/尾」控件**；
- `+ 语气 / 停顿 / 音效标签` 按钮改文案 `+ 标签 / 音效` 并加 `whitespace-nowrap`（修折行）；
- **新增「AI 音效建议栏」**：展示 `beat.sfx_suggestions` 与历史遗留未转的 `beat.sfx_cue`
  （标「历史音效（整句级）」），已插入的标签显示 `✓ [sfx:key]` 再点可一键移除，
  可试听，中文名从曲库查；
- 插入位置优先级：`ttsSelections` 光标 → `anchor_text` 之前 → 句末；
  底本 = `tts_text` 非空则用它，否则用 `narration_text`（字幕原文本身不被改动）；
- 曲库来源改为「外部优先」：`sfxLibraryItems` 有值就用它，否则用组件自己拉的。

**删除** `src/components/SfxExplorerTab.tsx`（单话内音效标签页）。

---

## 五、测试

`backend/tests/test_patch_v443_sfx_repo_and_s4_recommender.py`（36 例，六组）：

| 组 | 例数 | 覆盖 |
| :--- | :--- | :--- |
| 1 动态曲库纯函数 | 9 | 内置 24 条一字未改 / 注册表缺失回退 / 损坏回退 / 往返 / 合并自定义与 overrides / hidden 剔除 / keys 跟随 / `is_valid_sfx_key` / hidden 后失效 |
| 2 自定义 key 全链路 | 7 | 标签归一化认自定义键 / 未知键剥离 / 建议归一化认自定义键 / `resolve_sfx_asset` 找得到 / 用户素材优先于内置 / 自定义兜底链 / 未知键 400 |
| 3 S4 上下文推荐 | 5 | 台词关键词命中 / 已有内联标签不重复推荐 / 保留模型来源 / 最多 3 条 / 画面 `sfx_list` 命中 |
| 4 方案 B 联动 | 3 | 纯 `[sfx:key]` 下 `shake` 仍生效 / 无情绪词也生效 / 转折句仍留 950ms 白 |
| 5 Server CRUD | 11 | 归属字段 / 新增 / 非法键 400 / 重复键 400 / 改元数据 / 内置不可直接删 400 / 停用与还原 / 删自定义连音频 / 上传落到 custom 目录 / 还原覆盖音频 / 试听自定义键 |
| 6 剧本保存 | 1 | `PUT /script` 后 `sfx_suggestions` 完整落盘（key / reason / source / anchor_text） |

**过程中修掉的两个真问题**：

1. `web/server.py` 的 `_sfx_preprocess_audio` 里引用了**根本不存在的 `settings`** ——
   模块级函数拿不到 app 配置 → 改成显式传 `ffmpeg_bin` 参数，
   路由里传 `view.config.render.ffmpeg_bin`（原先误写成 `self.config`，路由函数不是方法）。
2. `test_put_script_keeps_sfx_suggestions` 起初返回 404「该话还没有 Stage 4 剧本」——
   `PUT /script` 要求话次已有剧本，测试改为先用 `save_model` 铺一份 `script.json`。

### 6.1 真机验证又抓出两个真 bug（都已修）

单测全绿不等于功能对 —— 这两个都是**真机浏览器点出来的**：

1. **自定义条目没上传音频时被误标成仓库内置**：
   `_sfx_library_payload()` 的来源判断只有「命中 custom 音频」和「其余」两分支，
   于是「已登记但没上传音频」的自定义条目落进 `else` → `source_type="builtin_cc0"`。
   修法：在 custom 音频分支后补 `elif key in custom_keys: source_type = "custom"`，
   并同步前端类型与 `SOURCE_LABELS`（新增「自定义（未上传音频）」），测试补断言。
2. **建议栏插入一次就整条消失、标签再也点不到**：
   建议栏的条目只来自 `beat.sfx_suggestions` + `beat.sfx_cue`，
   而「历史音效」被插入后会把 `sfx_cue` 置空 → 这条从列表里消失，
   于是**已经写进 `tts_text` 的标签没有对应的 chip**，
   用户既看不到「✓」也点不了「一键移除」（违反需求原文）。
   修法：`SfxSuggestionBar` 在整理完建议与历史条目之后，
   再把 `inlineSfxKeys(tts_text)` 里没被列出的 key 补成
   「已写入送音文本的音效标签 / present=true」的条目。

   真机复验（EP.008 · b2）：点插入 → `tts_text` 末尾出现 `[sfx:heart_thump]`、
   `narration_text` 不变、chip 变 `✓ [sfx:heart_thump]`（aria-label `b2 移除音效 heart_thump`）；
   再点一次 → 标签从 `tts_text` 移除、chip 消失。

---

## 六、验证结果

| 命令 | 结果 |
| :--- | :--- |
| `cd backend && .venv/bin/python -m pytest tests/test_patch_v443_sfx_repo_and_s4_recommender.py -q` | **36 passed** |
| `cd backend && .venv/bin/python -m pytest -n auto --dist loadfile -q` | **EXIT 0**（全量通过） |
| `cd backend && /opt/homebrew/bin/ruff check src tests` | **All checks passed!** |
| `npx tsc --noEmit` | **EXIT 0** |
| `npm run build` | **✓ built in 197ms** |

### 6.2 真机接口验证（curl）

| 请求 | 结果 |
| :--- | :--- |
| `GET /api/sfx-library` | items 24、`hidden_builtin_keys` []、`custom_keys` []、`source_type` {freesound_cc0:13, builtin_cc0:11}、`is_builtin` 全 True、`has_custom_audio` 全 False |
| `POST /api/sfx-library {"key":"knife_slice","label":"刀锋划过","group":"动作","category":"击打"}` | 200；items 25；`custom_keys` ["knife_slice"]；`source_type=="custom"`；注册表 `backend/assets/sfx/custom_sfx.json` 落盘 |
| `PATCH /api/sfx-library/knife_slice {"label":"刀锋划过身"}` | label 更新成功 |
| `DELETE /api/sfx-library/knife_slice` | items 回到 24、`custom_keys` 回到 []、注册表 `custom` 回到 [] |

验证结束后已 `rm -f backend/assets/sfx/custom_sfx.json`，盘面回到初始状态。

### 6.3 真机界面验证（Chromium 1440×860，wt_5081 · EP.008 · 97 句）

| 检查项 | 结果 |
| :--- | :--- |
| 左侧导航 | 新增「音效资源库」入口（Volume2 图标） |
| 全局音效资源库页 | 标题 + 副标题 + 24 条曲库 + 4 大类筛选（全部 24 / 动作 8 / 环境 5 / 转场 4 / 道具 7）+ 搜索 + 「命中 24 / 24」+ 5 列表格 + 试听/编辑/上传/删除 |
| 审核台 Tab | 「音效资源管理器」已消失，改由全局页承担 |
| 审核台「配音角色与语速」列 | 整句级音效下拉 / 试听 / 首中尾三档**全部移除** |
| 每句「音效建议」栏 | b2 显示 `[sfx:heart_thump] 心跳 🔊`（读历史遗留 `beat.sfx_cue`）；b1/b3/b4 无 `sfx_cue` → 不显示 |
| 插入 | 点 chip → `tts_text` 末尾出现 `[sfx:heart_thump]`，`narration_text` 不变 |
| 移除 | 再点 chip → 标签移除、chip 消失 |

验证中的浏览器草稿**未保存**（reload 时 beforeunload 被接受＝丢弃）；
复查磁盘 `workspace/wt_5081/episodes/ep_008/04_script/script.json` → 97 句、
含 `[sfx:` 的 `tts_text` 0 条、`b2.sfx_cue` 仍为 `heart_thump`（原值）、`custom_sfx.json` 不存在。

---

## 七、已知限制

1. **自定义音效的 `fallback_key` 只能指向生效曲库里的 key** —— 停用某个键之后，
   指向它的兜底关系会一起失效。
2. **上传的音频不会自动做「短素材响度补偿」** —— 只做峰值归一 + 限幅，
   像 v4.38.1 那样 <3s 的间歇素材均值可能偏轻，靠 S7 的 LUFS 校准兜底。
3. **内置音效停用是全局的** —— 停用后所有话次都写不了 `[sfx:thunder]`，
   旧剧本里的 `[sfx:thunder]` 标签会在归一化时被剥掉。
4. **`enrich_sfx_suggestions` 是规则补充，不是模型推理** —— 关键词表覆盖不到的
   语义（如同音梗、环境细节）仍要靠模型自己给建议。
5. **前端没有单元测试框架** —— 本次前端改动靠 `tsc` + `build` + 真机浏览器验证兜底。

---

## 八、后续建议

1. **建议栏接模型端在线重排**：现在建议是 S4 出稿时一次性生成的，
   可以在审核台加一个「按当前画面重算建议」的按钮。
2. **曲库导出 / 导入**：把 `custom_sfx.json` 与 `assets/sfx/custom/` 打包，
   让用户在不同机器间搬素材。
3. **音效使用统计**：记下每个 key 在成片里的实际使用次数，
   为「自动分配动效」提供依据。

---

## 九、关于 v4.42.1

v4.42.1 的三处交互修复（光标不跳末尾 / 跨话次清零 / 应用回执就地可见）
**改的是 `src/components/SfxExplorerTab.tsx`**。该文件在 v4.43 里**已整份删除**，
功能由全局「音效资源库」页 + 审核台的「AI 音效建议栏」承担，
因此那三处修复随之作废。

`PATCH_v4.42.1_SFX_EXPLORER_FIXES.md` 作为**评审记录**保留在仓库里，
说明 v4.42 交付后代码评审挑出了哪些问题、v4.43 又是怎么从根上解决它们的
（把「逐句插入音效」这件事从单话页挪到全局页，顺带消灭了跨话次残留的可能）。
