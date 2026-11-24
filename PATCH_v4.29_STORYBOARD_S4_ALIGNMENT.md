# Webtoon2Video 增量补丁规格书（Patch v4.29：镜头编排重组后 Stage 4 与分镜头对齐）

> **目标**：用户在编排页做「拆分幕 / 合并幕 / 跨幕移格 / 删格 / 禁用幕 / 复用分镜」之后重跑 Stage 4，
> 解说与多音字审核页出现**分镜头乱序**、**大量解说与画面对不上**。本补丁把「编排页启用幕的分镜范围」
> 确立为 Stage 4 全程的**唯一权威范围**，并修掉四处具体的错位来源。
>
> **铁律**：
> - 后端改动只在 `backend/`；前端只动 `src/`；提交 / 推送必须由用户显式下令。
> - 字幕与读音双文本必须分离（`narration_text` 与 `tts_text`），本补丁不触碰该语义。
> - **人工编排（`source="human"`）的显式意图绝不覆盖**：`scenes` 结构、`panel_ids`、`enabled`、
>   `user_note`、`locked_quotes`、BGM 配置一律原样保留；只允许「补空」写入 `visual_recap`。
> - 本补丁**只影响 Stage 4 撰稿**，不触碰 S5 配音、S6 时间轴、S7 渲染的既有产物。

---

## 一、文件修改白名单

### 后端（2 个）
1. `backend/src/stages/s4_scriptwriter.py` — 编排范围收敛、章幕配对下标修复、4A 章数校验重试、
   跨幕残句剔除、跨幕锚定钳制、人工编排 `visual_recap` 回填。
2. `backend/src/web/server.py` — `_validate_storyboard_scenes`：分镜组合变更时强制失效过期 `visual_recap`。

### 前端（2 个）
3. `src/components/StoryboardEditor.tsx` — 六处重组操作自动清空受影响幕的 `visual_recap`。
4. `src/components/EpisodeStudioView.tsx` — 审核页在 `episode.beats` 变化且处于 script Tab 时同步拉取
   最新 `/storyboard`，保证分组渲染用的幕列表与新剧本一致。

### 新增（2 个）
5. `backend/tests/test_patch_v429_storyboard_s4_alignment.py` — 19 条用例，覆盖四类后端场景 + 端到端重试。
6. `PATCH_v4.29_STORYBOARD_S4_ALIGNMENT.md`（本文件）。

---

## 二、问题定位（四处错位来源）

### 来源 1：过期「画面复核」摘要没失效

`visual_recap` 是 4A 拿到的「这一幕画了什么」的摘要。用户在编排页重组后，摘要还挂着**旧分镜组合**的
描述，4A 拿着它去讲早就被改掉的画面。

### 来源 2：撰稿范围没收敛

原 `write_episode_script` 的顺序是：

```
概览(_write_overview) → 4A(_write_story_draft) → 解析编排(resolved_storyboard / active_scenes) → 4B
```

概览与 4A 看着**整话全部分镜**，4B 却只在**启用幕**的窗口里挑图——两边看到的世界不一致。
而且被禁用幕独有、以及「移除分镜」后残留的孤儿分镜，同样会进入概览与 4A 的上下文。

### 来源 3：章幕配对下标 bug（`backend/src/stages/s4_scriptwriter.py`）

章幕数量不等时，旧实现是：

```python
target = max(pairs)          # ← 这是「幕下标」
base = chapters[target]      # ← 却拿去索引「章节列表」
```

`pairs` 的键是**幕下标**、值是**章节下标**。章数比幕少时会把正文写进完全不相干的章节；
章数比幕多、且配对结果的幕下标超出章节数时直接 **IndexError**。

### 来源 4：跨幕残句与跨幕锚定

* `_splice_scene_beats` 剔除旧解说时只判 `target_panel_ids` 与本幕分镜的**交集**。
  之前被钳制到邻幕分镜的残句，`target_panel_ids` 已经落在别的幕上 → 交集为空 → **残句留存**，
  整幕重讲时新旧两句并排出现。
* `_clamp_regressive_beats` 强行钳制时把锚点固定为 `ordered_panel_ids[last_index]`
  （= 上一幕末尾分镜），等于把本幕的解说**挂到别的幕上**。

---

## 三、后端实现

### 1. 编排解析提前 + 分镜池收敛

#### 1.1 `_resolve_storyboard_context()`（新函数）

把原先排在概览 / 4A **之后**的编排解析块整体搬出，提到撰稿之前：

```python
async def _resolve_storyboard_context(
    *, client, model, story_model, context, episode_id, vision, storyboard,
    storyboard_path, vision_client, vision_model, panels_dir,
    reset_storyboard, storyboard_sink,
) -> tuple[EpisodeStoryboard | None, list[StoryboardScene], bool]:
```

内部保持原有三分支来源（直接传入 / 磁盘 `storyboard_path` → `resolve_or_build_storyboard` /
否则 `auto_build_storyboard`）、`enrich_storyboard_with_vision` 视觉复核、以及
`storyboard_sink.append(resolved_storyboard)`，**不改变任何既有行为**，只是把执行时机前移。

#### 1.2 `_converge_panel_pool()`（新函数）

```python
def _converge_panel_pool(
    *, panels, all_panels, resolved_storyboard, active_scenes, scene_mode,
) -> tuple[set[str] | None, list[str], dict[str, PanelVisionInfo], list[PanelVisionInfo]]:
```

返回 `(valid_panel_ids, ordered_panel_ids, panel_map, story_panels)`：

* **未启用逐幕模式**（`scene_mode` 为假或没有启用幕）→ 保持旧兼容行为，
  `story_panels = exclude_pure_text_cards(list(panels))`；
* **启用逐幕模式** →
  * **放行**：启用幕引用、但被过渡页 / 纯字卡过滤掉的普通分镜重新纳入
    （`copyright_notice` / `promo_banner` 永远不放行）；
  * **剔除禁用幕独有分镜**：`disabled_panel_ids - enabled_panel_ids`；
  * **剔除孤儿分镜**：`current_ids - enabled_panel_ids`
    （不被任何启用幕引用，含「移除分镜」后残留的）；
  * `panel_map` / `ordered_panel_ids` / `story_panels` 同步收敛。

> **实现注意**：`current_ids = set(valid_panel_ids or set())` 必须**拷贝**。
> 否则后面 `valid_panel_ids.update(extra_ids)` 会连带改掉这个快照，
> 导致「放行」的分镜又被当成孤儿剔除。

收敛后的 `story_panels` 同时喂给 `_write_overview` 与 `_write_story_draft`，
`valid_panel_ids` / `ordered_panel_ids` / `panel_map` 全程使用同一份，
保证「概览看到的」「4A 看到的」「4B 能选的」是同一个集合。

### 2. 章幕配对下标修复

抽出可单测的纯函数：

```python
def _merge_unpaired_chapters(
    chapters: Sequence[StoryChapter],
    pairs: dict[int, int],
    active_scenes: Sequence[StoryboardScene],
    ordered_panel_ids: Sequence[str],
) -> list[StoryChapter]:
```

规则：未配对章节按「章节覆盖的分镜区间中心」与「幕覆盖的分镜区间中心」的**距离就近归并**，
用 `pairs[target_scene_index]`（章节下标）索引章节列表——**不再把幕下标当章节下标用**，
也不会丢正文。

### 3. 4A 章数校验与重试（需求 3b）

* 新增 `_scene_count_feedback(active_scenes, chapter_count, ordered_panel_ids) -> str`，
  生成定向反馈：期望幕数、每幕标题、每幕分镜边界（`first ~ last`）、以及「请输出恰好 N 章」。
* `write_episode_script` 的 4A 调用改为 `draft_attempts = 2 if (scene_mode and active_scenes) else 1`
  的循环：4A 返回后若 `len(story_draft.chapters) != len(active_scenes)`，
  打印 stderr 并把反馈拼进 `storyboard_constraints` **重试一次** 4A；异常则 break 并走既有 fallback。

### 4. 4B 候选窗口去重（需求 3c）

抽出纯函数：

```python
def _scene_window(scene: StoryboardScene, valid_panel_ids: set[str] | None) -> list[str]:
```

对 `scene.panel_ids` 按首次出现**保序去重**（`dict.fromkeys`），
防止「再加一格」复用同格时把重复 `panel_id` 多次塞给模型诱发顺位冲突。

### 5. 跨幕残句与跨幕锚定（需求 4）

* `_splice_scene_beats`：剔除条件由 `if scene_panels and ids & scene_panels:`
  改为 `if (scene_panels and ids & scene_panels) or same_scene:`，
  其中 `same_scene` 要求 `beat.get("scene_id") == scene.scene_id`（两者都非空）。
* `_clamp_regressive_beats(beats, ordered_panel_ids, scenes=None)`：新增第三参 `scenes`，
  内部建立 `scene_id → panel_ids` 索引。强行钳制时若该 beat 有 `scene_id` 且本幕分镜都在池内，
  锚点**留在本幕**（取本幕中 `index_of <= last_index` 的最后一格，否则本幕第一格）；
  无幕信息时保持旧行为。两个调用点均传 `scenes=active_scenes if scene_mode else None`，
  且触发钳制时打印清晰日志（含幕 id、原锚点、钳制目标）。

### 6. 人工编排的 `visual_recap` 回填（需求 1 后端第三项）

新增：

```python
def _merge_visual_recap_into_human_storyboard(
    final_storyboard: EpisodeStoryboard, storyboard_path: str | Path,
) -> EpisodeStoryboard | None:
```

以**磁盘上的编排**为基底，只在「该幕磁盘 `visual_recap` 为空、且本次复核确实给出了摘要」时
`model_copy` 写入；其余字段（`source` / `scenes` 结构 / `panel_ids` / `enabled` /
`user_note` / `locked_quotes` / BGM）原样不动。无变化时返回 `None`，调用方不落盘。

`run_stage4` 的回写改为：`source != "human"` 时照旧整份保存；
`source == "human"` 时改走上述合并，**只有合并出变化才保存**。

### 7. 服务端 `visual_recap` 失效（需求 1 后端第二项）

`backend/src/web/server.py` 的 `_validate_storyboard_scenes`：

```python
visual_recap = str(raw.get("visual_recap") or "")
if existing is not None and list(existing.panel_ids or []) != panel_ids:
    if visual_recap == (existing.visual_recap or ""):
        visual_recap = ""
```

语义：**分镜组合变了、且提交值仍等于旧摘要 → 强制清空**；
用户主动重写过的新摘要（与旧值不同）照旧保留。两个调用点
（`PUT /api/episodes/{id}/storyboard` 与 `POST /api/episodes/{id}/storyboard/generate`）
原本就传了 `existing_scenes=existing_storyboard.scenes`，无需改动。

---

## 四、前端实现

### 1. `src/components/StoryboardEditor.tsx`

凡「`panel_ids` 发生变化或重新组合」的幕，自动把 `visual_recap` 置为 `''`：

| 操作 | 处理 |
| --- | --- |
| `splitSceneAt` | 左半幕 `visual_recap: ''`；右半新幕本来就是空 |
| `mergeIntoPrevious` | 合并后的幕 `visual_recap: ''` |
| `movePanel` | 移出幕与移入幕都 `visual_recap: ''` |
| `movePanelWithinScene` | 幕内换序同样清空（与后端「比较列表」规则同口径） |
| `addPanelToScene` | 新增格的幕 `visual_recap: ''` |
| `removePanelFromScene` / `handleRemovePanelAll` | 删格后的幕 `visual_recap: ''` |

### 2. `src/components/EpisodeStudioView.tsx`

新增 effect：`activeTab === 'script'` 且 `episode.beats` 非空时，拉取
`/api/episodes/{id}/storyboard` 并 `setSceneStoryboard`（带 `cancelled` 守卫），
依赖 `[activeTab, episode.episode_id, episode.beats]`。

作用：审核页按幕分组渲染时，用的幕列表必须与刚重跑出来的剧本一致，
否则会出现「剧本已换、幕列表还是旧的」导致的分组错位。

### 3. 审核台「停顿ms」输入框可见性（附带修复）

用户报障：解说审核表里「停顿ms」数字框太窄，**框内数字完全看不见**（只剩 Chrome
number input 的上下微调箭头）。根因有两条，缺一不可：

1. 框宽被 `w-full` 压到只剩十几像素，而 Chrome 的微调箭头固定占 ~16–18px，
   数字被箭头整段挤掉；
2. 表头 `运镜与停顿` 只有 `w-36`（144px），扣掉「停顿ms」标签与间距后放不下数字框。

修法（`src/components/EpisodeStudioView.tsx` + `src/index.css`）：

| 位置 | 改动 |
| --- | --- |
| 停顿 input | `w-full` → `flex-1 min-w-[76px] w-auto`，并加 `hide-spinner` |
| 表头 th | `w-36` → `w-44 min-w-[176px]` |
| `src/index.css` | 新增 `input[type='number'].hide-spinner` 规则：`::-webkit-inner-spin-button` / `::-webkit-outer-spin-button` 置 `appearance:none`，本体 `appearance:textfield` |

隐藏箭头后，数字恒完整可见；步进改用键盘上下键 / 滚轮（`step=50` 保持不变）。
真机复验（wt_3924 EP.001，121 句）截图确认 b1/b2/b3_new_120/b3 的
`450 / 60 / 150 / 480` 四项数值全部清晰可见。

---

## 五、测试

### 新增 `backend/tests/test_patch_v429_storyboard_s4_alignment.py`（19 条）

| 分组 | 用例 |
| --- | --- |
| 分镜池收敛 | 禁用幕剔除 + 孤儿剔除 / 人工放行过渡页 / 版权页与推广页永不放行 / 未启用逐幕模式保持旧行为 |
| 章幕配对 | 幕下标≠章节下标不越界 / 章多幕少不越界不丢正文 / 就近归并而非全塞末章 |
| 4B 窗口 | 保序去重 / 池外分镜过滤 |
| 跨幕残句 | `_splice_scene_beats` 按 `scene_id` 剔除残句 |
| 跨幕锚定 | `_clamp_regressive_beats` 锚点留在本幕 / 无幕信息保持旧行为 |
| `visual_recap` 失效 | 服务端清空过期摘要 / 保留用户重写 / 分镜未变则保留 |
| 人工编排回填 | 只补空、不覆盖用户摘要、不动其它字段 / 无可补时返回 `None` |
| 范围一致性 | 收敛后的分镜池与启用幕引用集合完全一致 |
| 4A 重试（端到端） | 首次章数与幕数不符 → 带「分幕数目校验失败」反馈重试一次 → 成稿每句都落在启用幕分镜上 |

### 既有测试

`backend/tests/test_s4_scriptwriter.py`、
`backend/tests/test_patch_v40_patch19_storyboard.py`、
`backend/tests/test_patch_v40_patch20_storyboard_api.py`、
`backend/tests/test_patch_v421_semantic_scene_activation.py` 全绿，无需修改。

---

## 六、验证结果（2026-11-24）

| 命令 | 结果 |
| --- | --- |
| `cd backend && .venv/bin/python -m pytest tests/test_patch_v429_storyboard_s4_alignment.py -q` | **19 passed** |
| `cd backend && .venv/bin/python -m pytest -n auto --dist loadfile` | **1047 passed in 25.31s** |
| `cd backend && /opt/homebrew/bin/ruff check src tests` | **All checks passed!** |
| `npx tsc --noEmit` | **exit 0** |
| `npm run build` | **✓ built in 248ms** |

---

## 七、已知限制与后续

* **只影响 Stage 4 出稿**：已产出的 `04_script/script.json` 不会自动重排，
  需要在审核台重跑 Stage 4 才会用上收敛后的范围。
* **`visual_recap` 回填只在「磁盘上为空」时生效**：如果用户手写过摘要，
  本次视觉复核的结果不会覆盖它（保护人工意图的取舍）。
* **幕内换序也会清空 `visual_recap`**：与后端「比较 `panel_ids` 列表」的规则同口径，
  换序后原摘要的「先后顺序」描述确实已经过期。
