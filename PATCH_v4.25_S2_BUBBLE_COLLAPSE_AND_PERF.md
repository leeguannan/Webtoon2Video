# Webtoon2Video 增量补丁规格书（Patch v4.25：Stage 2 气泡合并修复 + 解说审核按幕折叠 + 大分镜性能优化 + 快捷保存 + 角色档案同步闭环）

> **目标**：
> 1. **Stage 2 别把高瘦气泡当画格**：690×1732 这类高瘦页面里，页面顶部的独立气泡（纯背景、无外框）此前会被切成独立分镜（一个气泡 = 一格），画面被切碎；现在这类气泡要并回它下面的主画格，且**不能**把「白底黑框 / 黑底白字」的稀疏文字气泡误判成空白过渡页。
> 2. **解说审核台默认按幕折叠**：一话 200~300 分镜时整张表铺开会卡死；现在每一幕默认折叠，点开展开，折叠态**不挂载** table/tr DOM。
> 3. **大分镜性能**：分镜列表 / 审核表 / 时间轴 / 分镜编辑器的图片全部走缩略图接口 `?w=`，懒加载 + `content-visibility:auto`，并用 `useMemo` 的 `panelMap` 取代每次渲染的 `panels.find()`。
> 4. **快捷保存**：`Cmd/Ctrl+S` 保存解说稿、`Cmd/Ctrl+Shift+S` 保存并从 Stage 5 重跑配音；大图校对弹窗内 `Cmd/Ctrl+S` 原地保存、`Cmd/Ctrl+Enter` 保存并跳下一句；脏稿时注册 `beforeunload` 拦截丢稿。
> 5. **v4.23 档案同步闭环**：只读检查里点「记为规范名 / 记为合法别名 / 登记进角色档案」时，能真正写进 `story_bible.json`（并平滑迁移专属音色键），而不是只口头提醒。
>
> **铁律**：
> - Stage 2 的**切分判据**只做「合并气泡 + 保护稀疏文字气泡」两件事，**不得**整体改动切分算法（会位移 panel 编号、打乱已有人工编排的 `script.json`）。
> - 缩略图接口**不改原图**：`?w=` 缺省时仍原样返回原图；缩略图只缩不放，缓存随原图 mtime 失效。
> - 角色档案**默认绝不动**：`sync_character_updates` 不传就一个字节都不许改 `story_bible.json`。
> - 只读检查仍然**只读**：繁简比对只在检查口径内转简体，落盘文本保持用户原样。

---

## 一、文件修改白名单

### 后端（3 个）
1. `backend/src/stages/s2_segmenter.py` — 气泡识别与合并阈值、稀疏高对比文字保护、`.thumbs` 清理。
2. `backend/src/core/character_unify.py` — 说话人繁简比对、送音文本去重复告警。
3. `backend/src/web/server.py` — `?w=` 缩略图参数、角色档案同步（`sync_character_updates`）。

### 前端（5 个）
4. `src/components/EpisodeStudioView.tsx` — 按幕折叠、缩略图、快捷键、脏稿拦截、角色档案同步选项。
5. `src/components/PanelZoomModal.tsx` — 弹窗内快捷保存 / 保存并跳下一句。
6. `src/components/StoryboardEditor.tsx` — 幕卡片缩略图与懒渲染。
7. `src/App.tsx` — 透传 `syncCharacterUpdates`、刷新 `seriesTts`。
8. `src/types/studio.ts` — 新增档案同步相关类型。

### 测试与文档（4 个）
9. `backend/tests/test_s2_segmenter.py` — 新增 4 条 S2 回归测试。
10. `backend/tests/test_patch_v423_manual_save_char_check.py` — 送音文本去重规则的旧断言适配。
11. `backend/tests/test_patch_v425_s2_and_studio_perf.py`（**新建**）— 11 项离线测试。
12. `PATCH_v4.25_S2_BUBBLE_COLLAPSE_AND_PERF.md`（本文件）。

---

## 二、后端实现

### 1. `backend/src/stages/s2_segmenter.py`

**（a）高瘦纯背景气泡识别**

```python
MONO_BUBBLE_MAX_HEIGHT_RATIO = 0.80   # 气泡高度上限：img_width * 0.80
MONO_BUBBLE_MIN_RATIO = 0.82          # 单一主色占比下限
```

`is_standalone_text_box()` 在保留原有「特征 A / B」（高度 < `img_width * 0.35`）之外，新增一条高瘦判据：

> 高度 < `img_width * 0.80` **且** `dominant_color_ratio(block_bgr) > 0.82` **且** `not has_closed_border_frame(to_gray(block_bgr))`

即「纯背景、没有外框框线」的高瘦块算气泡（气泡描边也算外框，故排除）。

**（b）合并高度解耦**

```python
TEXT_BOX_MERGE_MAX_HEIGHT_RATIO = 3.6   # 原 1.65
TEXT_BOX_STACK_MAX_HEIGHT_RATIO = 1.0   # 新增：连续串并的文本块累计高度上限
```

- 单个气泡并入主画格后的合并上限：`min(MAX_ATOMIC_BLOCK_HEIGHT, int(img_width * 3.6))` —— 让 690×1732 的高瘦人物格能向下吸附上方约 400px 的气泡。
- 连续向后吸收多个纯文本框时，**被吸收文本块自身**的累计高度超过 `img_width * 1.0` 即停止串并，避免一列气泡把整页吞掉。
- 间距上限仍是 `< 0.25 * img_width`；合并方向不变：只把 `flags[index] == True` 的气泡并入**紧邻下方第一个** `flags[target] == False` 的主画格。

**（c）稀疏高对比文字保护**

新增 `_has_sparse_high_contrast_text(gray, mode_value) -> bool`：

- 块高 ≥ 48；
- 亮底（`mode_value >= 200`）看 `gray <= 128`，暗底（`mode_value <= 50`）看 `gray >= 190`，其余底色返回 `False`；
- 有效墨迹行数 `int(ink_mask.any(axis=1).sum()) >= 12`；
- `canny_edge_density(gray) >= DARK_SCENE_LOCAL_EDGE_MIN (0.02)` 的行数 ≥ 4。

`is_contentless_panel()` 在 `content_ratio < ratio` 时，若该函数为 `True` 则**不**判为空白页（返回 `False`）。

> 注意：**不能**直接复用 `_has_local_structure()`——它会把「白底黑框 / 黑底白字」的稀疏文字气泡连同短黑面板一起豁免，破坏 `test_contentless_short_black_panel_ignores_safe_margin` 与 `test_blank_panel_is_marked_as_transition_blank`。

**（d）清理旧缩略图**

`segment_episode()` 清理旧产物时追加 `shutil.rmtree(target_dir / ".thumbs", ignore_errors=True)`，避免原图更新后残留过期缩略图。

### 2. `backend/src/core/character_unify.py`

- `inspect_script_beats()`：当某 beat 的 `tts_text` 与 `narration_text` 去空白后完全相同时，**跳过** `tts_text` 的重复告警（同一句话不该报两次）。
- `_inspect_speaker_role()`：比对前先 `to_simplified_chinese(role)` 得到 `simplified`，用 `simplified` 走 `canonicalize_role()` 与 `are_homophone_name_variants()`；`found_name` 仍报用户原样写法；未登记判据改为 `simplified not in self.canonical_names`。**原字段不被改写。**

### 3. `backend/src/web/server.py`

**（a）缩略图接口**

`GET /api/episodes/{episode_id}/panels/{filename}` 新增查询参数：

```python
w: int | None = Query(default=None, ge=64, le=800)
```

- 不传 `w`：原样返回原图。
- 传 `w`：按原图宽高比等比缩到宽 `w`（**只缩不放**），JPEG `quality=82`，缓存到 `02_panels/.thumbs/{stem}_w{w}.jpg`；缓存不存在或原图 mtime 更新时重建。
- 返回 `FileResponse` 并附 `Cache-Control: public, max-age=86400`。
- 缩略图生成异常时回退原图（不阻断请求）。

**（b）角色档案同步**

`ScriptUpdateRequest` 新增：

```python
sync_character_updates: list[dict[str, Any]] = Field(default_factory=list)
```

每项：

| action | 语义 |
| :--- | :--- |
| `add_alias` | 把 `found_name` 加入 `target_canonical` 的 `aliases`（去重） |
| `promote_canonical` | 把 `target_canonical` 改名为 `found_name`，原名并入 `aliases` 并去掉 `found_name`；同时把 `series_config.yaml` 的 `tts.character_voices` 旧键平滑迁移到新键（**新键已存在则不覆盖**） |
| `register_character` | 新增 `CharacterProfile(name=found_name)` |

`PUT /api/episodes/{episode_id}/script` 仅当 `sync_character_updates` **非空**时先更新档案（默认不传绝不动档案），再计算并返回 `character_warnings`。

---

## 三、前端实现

### 1. `src/components/EpisodeStudioView.tsx` — 按幕折叠 + 缩略图 + 快捷键

- 新增 `expandedSceneKeys: Set<string>`（默认空集 = **全部幕折叠**），切换话次时重置为全折叠；工具栏新增「展开全部 / 全部折叠」两个入口。
- **折叠态不挂载 DOM**：幕内容用 `{isExpanded && (...)}` 条件渲染（不是 `hidden`），折叠时 `<table>` / `<tr>` 完全不在文档里，200~300 分镜的一话打开审核台才不会卡死。
- 幕头部（折叠时唯一渲染的一行）展示：展开箭头（`ChevronRight` / `ChevronDown`）、`第 N 幕` 徽标、标题 + 地点、句数与配音总时长、该幕**未保存草稿**小标记、BGM 情绪标签与**选曲 + 试听**控件（`onClick` / `onKeyDown` 都 `stopPropagation`，点控件不会顺手把幕折叠掉）。
- 新增解说句 / 插入句时自动 `expandSceneKey(...)` 展开所在幕。
- 拆出 `const ScriptBeatRow = React.memo(function ScriptBeatRow(...))`，逐句行组件化，减少整表重渲染。
- `panelMap = useMemo(() => new Map(...))` 取代渲染期反复的 `episode.panels.find(...)`；角色下拉选项也预计算成稳定数组。
- 新增 `panelThumbUrl(url, width)`：在原 URL 上追加 `?w=` / `&w=`；左侧分镜列表 `?w=180`、审核表缩略图 `?w=140`、时间轴 `?w=140`、StoryboardEditor 幕内卡片 `?w=360`、添加分镜勾选列表 `?w=140`；**大图预览与 PanelZoomModal 保持原图 URL**。
- 所有缩略图 `<img>` 加 `loading="lazy" decoding="async"`；左侧分镜列表加 `[content-visibility:auto] [contain-intrinsic-size:88px]`。
- 快捷键（仅 `activeTab === 'script'`）：`(e.metaKey || e.ctrlKey) && !e.altKey && key === 's'` 一律 `preventDefault`（焦点在输入框里也拦，否则 Cmd+S 变成「保存网页」）；无弹窗遮挡且未在保存 / 预检中时，普通 `Cmd/Ctrl+S` → `handleSaveBeats(false)`，`Cmd/Ctrl+Shift+S` → `handleSaveBeats(true)`；大图弹窗打开时让开（由 PanelZoomModal 自己处理，避免保存两次）；`Esc` 关闭角色名确认弹窗。
- `characterCheckBusy` 期间禁用保存按钮，防连点重复提交；`flushSave` 无告警时清掉上一轮遗留的「角色名写法」提示条。
- `tableDirty === true` 时注册 `window.beforeunload` 拦截，刷新 / 关页面前提醒一次，防丢稿。
- v4.23 弹窗闭环：`charPrompt` 内按 `(found_name, suggested_canonical)` 聚合成组，每组给可选同步动作——`alias_variant` / `homophone_variant` 可选「记为规范名（原名降为别名）」= `promote_canonical` 或「记为合法别名（不再提醒）」= `add_alias`；`unregistered_speaker` 可选「登记进作品角色档案」= `register_character`；**默认全选「仅本次放行，不改档案」**。点「仍按我的修改保存（放行）」时把勾选项作为 `syncCharacterUpdates` 透传给 PUT；执行了同步则 `reloadSeriesTts()` 重拉一次作品配音配置，让「本句配音」下拉立刻反映新档案。

### 2. `src/components/PanelZoomModal.tsx` — 弹窗内快捷校对

新增可选 `onQuickSave?: () => Promise<boolean>`：`Cmd/Ctrl+S` 原地保存；`Cmd/Ctrl+Enter` 保存当前格并跳到下一句（最后一句只原地保存、不跳）。`Cmd/Ctrl+Enter` 在 textarea 里本来是「换行」，这里必须拦掉；保存期间不允许重复触发。底部快捷键提示同步更新文案。

### 3. `src/components/StoryboardEditor.tsx` — 幕卡片懒渲染

幕卡片拆成 `React.memo` 的 `SceneCard`，卡片容器加 `[content-visibility:auto] [contain-intrinsic-size:auto_600px]`；幕内分镜缩略图 `?w=360`、添加分镜勾选列表 `?w=140`，均 `loading="lazy"`。

### 4. `src/App.tsx` / `src/types/studio.ts`

- 新增 `CharacterSyncAction` / `CharacterSyncUpdate` 类型（与后端 `sync_character_updates` 一一对应）。
- `handleUpdateBeats` 增加第 5 个参数 `syncCharacterUpdates?: CharacterSyncUpdate[]`，PUT 时透传 `sync_character_updates`（空数组 = 不改档案）。
- `handleAutoSaveBeats` 仍固定 `auto_unify_characters: false`（弹窗逐字微调原样落盘）。

---

## 四、测试与验收

### 新增/更新测试

- `backend/tests/test_s2_segmenter.py`：4 条回归测试
  - `test_patch425_tall_mono_bubble_is_recognized_as_text_box` —— 160/320=0.5 高的纯背景气泡判为气泡；纹理场景不是；带封闭外框不是。
  - `test_patch425_tall_bubble_merges_into_scene_below` —— 切分后只剩 1 个面板且高度 > 400。
  - `test_patch425_sparse_text_bubble_is_not_contentless` —— 白底黑框、黑底白字都不算空白页；纯白才算。
  - `test_patch425_merge_stops_when_text_stack_too_tall` —— 3 个 200px 气泡串并到上限即停。
- `backend/tests/test_patch_v425_s2_and_studio_perf.py`：11 条离线测试（缩略图 4 + 档案同步 5 + 只读检查 2）。
- `backend/tests/test_patch_v423_manual_save_char_check.py`：送音文本去重规则下调整旧用例并新增一条去重用例。

### 验收命令

```bash
cd backend && .venv/bin/python -m pytest -n auto --dist loadfile
/opt/homebrew/bin/ruff check src tests
npx tsc --noEmit
npm run build
```

四项全绿后方可提交。

### 真机验证（只读副本，不动生产工作区）

用 `workspace/wt_3924/episodes/ep_001/01_raw`（211 张原图）的**副本**跑 `s2_segmenter.segment_episode`：

| 逻辑 | 面板总数 | 其中超扁（高 < 200px） |
| :--- | :--- | :--- |
| 旧逻辑 | 284 | 52 |
| 新逻辑 | **245** | **40** |

即 39 个独立气泡被并回它下面的主画格，碎条再减 12 条，画面没有被切碎（生产工作区的 `02_panels` 未被覆盖，仍是旧的 284 格）。
