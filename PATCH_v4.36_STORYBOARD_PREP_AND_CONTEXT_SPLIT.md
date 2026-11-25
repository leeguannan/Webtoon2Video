# PATCH v4.36 —— 切幕预备模式 + 4A/4B 双层解耦

- 基线提交：`af76551`（PATCH_v4.35 审核台 / 分镜台 / 放大审校的 UI-UX 重构）
- 补丁号：`v4.36`
- 日期：2026-11-25
- 状态：实现完成，pytest 全量 / ruff / tsc / build 全绿

---

## 一、要解决的两个问题

### 问题 1：S4 白跑一遍（写稿与调幕顺序颠倒）

旧流程是「**一路跑到 S4 出稿 → 打开镜头编排页调幕 → 从 S4 重跑**」。
这意味着：

1. 用户还没看过镜头编排，模型就已经按「默认切幕」写完了整话解说；
2. 调完幕后必须从 S4 重跑 —— 上一轮写稿的算力（一次完整的 4A + 4B 模型调用）**全部作废**；
3. 重跑还会把 TTS 之前的产物连带清掉一次。

### 问题 2：跳过一格分镜 = 剪断一段剧情（4A/4B 混为一谈）

v4.29 起，「启用幕以外的分镜」被从**撰稿范围**彻底剔除。这个收口修好了
「审核台看到的分镜与成片不一致」，但副作用是：

用户在编排页把一格**纯对话 / 空镜**标记为「移出」或「过渡废格(跳过)」，
S4 写稿时就**再也看不到这一格的台词** —— 于是：

- 4A（整话剧情规划）认为这段剧情根本不存在，不给它排章节；
- 4B（逐句写稿）更不可能引用它。

结果：**用户只是想让这一格不出镜，结果整段剧情被剪断了。**

---

## 二、三条铁律

1. **「不出镜」≠「不看剧情」**：被移出 / 跳过的分镜，4A 必须看得见它的画面与台词，
   只是标记为「仅参考剧情·不出镜」；4B 的候选窗口仍然严格只认启用幕。
2. **只有「整段被关掉的镜头组」和版权 / 推广页才真正从 4A 视野消失**：
   禁用幕独有的分镜、以及 `non_story_type != "none"`（版权页 / 推广尾页 / 过渡帧）
   依旧不进 4A 上下文。
3. **S4 只跑一遍**：切幕预备模式把「切幕」从 S4 里拆出来单独交付，
   用户确认编排后再跑一次 S4，S4 全程只执行一次。

---

## 三、文件白名单

| 文件 | 动作 | 说明 |
| --- | --- | --- |
| `backend/src/web/job_runner.py` | 改 | 新增 `STORYBOARD_PREP_LAST_STAGE` 与 `_stage_prepare_storyboard` |
| `backend/src/core/manifest.py` | 改 | `reset_from_stage` 保留语义切幕成果 |
| `backend/src/web/server.py` | 改 | `RUN_MODES` 与 mode 校验 |
| `backend/src/stages/s4_scriptwriter.py` | 改 | 4A/4B 双层解耦（`_build_story_context_panels` 等） |
| `src/types/studio.ts` | 改 | `RunMode` 扩三档 |
| `src/App.tsx` | 改 | 模式 localStorage 记忆 + 重跑提升 + toast 文案 |
| `src/components/CatalogView.tsx` | 改 | 三档模式切换 |
| `src/components/EpisodeStudioView.tsx` | 改 | 默认落到「镜头编排」Tab + 两处说明行 |
| `src/components/StoryboardEditor.tsx` | 改 | 未出稿时的按钮文案 |
| `backend/tests/test_patch_v436_storyboard_prep_and_context_split.py` | 新增 | 20 条回归测试 |
| `PATCH_v4.36_STORYBOARD_PREP_AND_CONTEXT_SPLIT.md` | 新增 | 本文档 |

未改动的既有契约：

- S1~S7 的 7 阶段 `STAGE_KEYS` 保持不变（`s1_crawler` / `s2_segmenter` /
  `s3_vision` / `s4_scriptwriter` / `s5_tts` / `s6_timeline` / `s7_renderer`）；
- 后端 API 的 `mode` 默认值仍是 `"phase1_only"`；
- `/api/episodes/{id}/storyboard/generate` 的 `STORYBOARD_MODES` 仍是
  `{"phase1_only", "full_video"}`（切幕预备是**批量任务**的模式，不是单话生成模式）；
- `_converge_panel_pool` 的 4 元组签名未变（既有单测直接解包）。

---

## 四、后端实现

### 4.1 (A) 「切幕预备」模式 `mode="storyboard_prep"`

**`backend/src/web/job_runner.py`**

- 新增常量：

  ```python
  STORYBOARD_PREP_LAST_STAGE = "s3_vision"   # Patch v4.36
  ```

- `_run_episode(...)` 按 mode 选终点：

  | mode | 终点 | 含义 |
  | --- | --- | --- |
  | `storyboard_prep` | `s3_vision` | 只跑 S1~S3，顺带切幕，然后停下 |
  | `phase1_only` | `s4_scriptwriter` | 跑到出稿为止 |
  | `full_video` | `s7_renderer` | 一路到成片 |

- 阶段循环里，S3 结束的瞬间插入切幕预备：

  ```python
  if stage == "s3_vision" and mode == "storyboard_prep":
      await self._stage_prepare_storyboard(state, series_dir, episode, llm_client, story_model)
  ```

- 新增私有方法 `_stage_prepare_storyboard(...)`：

  1. 读 `episodes/ep_XXX/03_vision/vision.json`；
  2. 调 `s3b_scenes.resolve_or_build_storyboard(..., reset=False)`
     → 走语义切幕并落盘 `04_script/storyboard.json`（`auto_strategy="semantic"`）；
  3. 打日志：`[Stage 3b] 已完成语义切幕（共 N 幕），等待人工确认镜头编排后再执行 S4`。

  **刻意不做**：不调 `enrich_storyboard_with_vision`、不拿 `_SeriesGate` 锁、
  不跑 S4、不写 StoryBible。缺 `vision.json` / 解析失败 / 切幕失败一律
  **只记日志不抛异常**，让流水线正常收尾。

- 「从此步重跑」的提升规则（`start_index > end_index` 时）：

  | 当前 mode | from_stage | 结果 |
  | --- | --- | --- |
  | `storyboard_prep` | `s4_scriptwriter` | 提升为 `phase1_only`（S1~S4），日志「S4 超出「切幕预备」范围，自动提升为「出稿止步」模式（S1~S4）」 |
  | `storyboard_prep` | `s5_tts` 及以后 | 提升为 `full_video`（S1~S7） |
  | 其它 | 任意 | 维持原「仅第一步 → 全流程」提升逻辑 |

**`backend/src/core/manifest.py`**

`reset_from_stage` 里判断 `04_script/storyboard.json` 是否保留，条件从
「只有 `source == "human"` 才保留」放宽为：

```python
keep_board = from_stage == "s4_scriptwriter" and not include_human_storyboard
if keep_board and (source == "human" or auto_strategy == "semantic"):
    preserved_human_storyboard = True
else:
    _remove_file(board_path)
```

- `source == "human"`：人工编排，照旧保留（v4.29 行为不变）；
- `auto_strategy == "semantic"`：**新增** —— 语义切幕的成果也是真成果，
  从 S4 重跑时不该被清掉，否则用户确认完编排重跑 S4 会丢掉刚切好的幕；
- `auto_strategy == "rule"`：规则切幕占位（还没真切），照旧清掉。

从 S1 / S2 / S3 重跑，或 `include_human_storyboard=True` 时，照旧清理。

**`backend/src/web/server.py`**

```python
RUN_MODES: frozenset[str] = frozenset({"storyboard_prep", "phase1_only", "full_video"})
```

`POST /api/jobs/batch` 与 `POST /api/episodes/{id}/rerun` 各加 mode 校验，
非法值返回 400「mode 只支持 storyboard_prep / phase1_only / full_video」。
两个请求模型的 `mode` 默认值仍是 `"phase1_only"`（不破坏老客户端）。

### 4.2 (B) 4A / 4B 双层解耦

**`backend/src/stages/s4_scriptwriter.py`**

- **新增 `_build_story_context_panels(...)`**（独立函数，**不改**
  `_converge_panel_pool` 的 4 元组签名）：

  ```
  scene_mode 关闭 → return exclude_pure_text_cards(list(panels)), set()   # 旧行为
  scene_mode 开启 →
      剔除：non_story_type != "none"（版权页 / 推广尾页 / 过渡帧 / 纯音效）
             is_transition_blank
             属于「禁用幕独有」且不在启用幕并集里的分镜
      其余全部进 story_context_panels
      其中不在 valid_panel_ids 的 → 进 reference_only_ids
      最后再过一遍 exclude_pure_text_cards（纯字卡不进上下文）
  ```

- **`format_panel_list(panels, reference_only_ids=None)`**：参考分镜在
  `- {panel_id}：` 之后加 `[仅参考剧情·不出镜]` 标记（在 `[纯字卡]` 之前），
  **画面描述、对白、音效一字不少** —— 台词不能因为「不出镜」而丢失。

- **`_storyboard_constraints_block(active_scenes, reference_only_ids=None)`**：
  末尾追加约束段：

  > 【仅参考分镜】以下分镜被移出或跳过，不在任何启用镜头组内，不会出镜；
  > 但它们的台词属于真实剧情，必须结合理解并把必要台词融入相邻启用镜头所在章节，
  > 不得为其单独开章、也不得引用其编号：…

- **`_write_overview` / `_write_story_draft`**：新增 `reference_only_ids` 形参，
  透传给 `format_panel_list`。

- **`_fallback_story_draft`**：新增 `panel_range` 形参，模型 4A 失败兜底时
  用启用幕的首尾分镜作章节锚点。

- **`write_episode_script`** 的分工（改动核心）：

  | 环节 | 吃哪份分镜 |
  | --- | --- |
  | 概览 + 4A（整话剧情规划） | `story_context_panels` + `reference_only_ids` |
  | 4B（逐句写稿 / 出图） | `_scene_window(scene, valid_panel_ids)` — **未变** |

  章节切分与字数 / 句数预算（`compute_episode_beat_budget` /
  `compute_story_word_budget`）**仍只按启用幕与 `ordered_panel_ids` 计算** ——
  参考分镜不占预算，不会凭空多出章节。

---

## 五、前端实现

| 文件 | 改动 |
| --- | --- |
| `src/types/studio.ts` | `RunMode` 扩为 `'storyboard_prep' \| 'phase1_only' \| 'full_video'`（带三档语义注释） |
| `src/App.tsx` | 模块级 `RUN_MODE_LABELS`（切幕预备 / 出稿止步 / 全流程）；`runMode` 用 `localStorage['w2v.runMode']` 记忆，**默认 `storyboard_prep`**；`handleRerunFromStage` 里点 S4 且当前是 `storyboard_prep` → 提升 `phase1_only`，点 S5 及以后 → `full_video`；toast 用 `RUN_MODE_LABELS` |
| `src/components/CatalogView.tsx` | 两档改**三档单行**（`whitespace-nowrap shrink-0`）：「准备分镜+切幕 (S1–S3b)」（title 说明「只跑下载→切图→视觉抽取→语义切幕，然后停下等你调镜头编排；确认后再单独跑一次 S4 写稿（S4 只跑一遍）」）/「出稿止步 (S1–S4)」/「全流程制片 (S1–S7)」；选中切幕预备时右侧显示「推荐：先调幕再写稿，S4 只跑一次」 |
| `src/components/EpisodeStudioView.tsx` | `episode.panels.length > 0 && episode.beats.length === 0` 时**默认落到「镜头编排」Tab**（同一话次只判一次，`storyboardDefaultAppliedRef`）；「分镜与视觉抽取」的跳过按钮旁 + 「镜头编排」顶部各加一行说明：「移出/跳过纯对话与空镜仅不出镜，S4 写稿仍会参考其台词全貌；需整段跳过请关闭整幕开关。」 |
| `src/components/StoryboardEditor.tsx` | 未出稿时按钮文案为「按镜头组生成剧本 (S4)」/「按镜头组一键制片 (S4→S7)」，已出稿时为「重生成剧本 (S4)」/「按镜头组一键制片 (S4→S7)」 |

---

## 六、测试

`backend/tests/test_patch_v436_storyboard_prep_and_context_split.py` —— **20 条**：

**4A / 4B 双层解耦（10 条）**

1. `test_build_story_context_panels_keeps_removed_panels_as_reference_only`
   —— 被移出的 p_004 留在 4A 上下文里，且进 `reference_only_ids`；
2. `test_build_story_context_panels_drops_disabled_scene_only_panels`
   —— 禁用幕独有的 p_002 连参考都不给；
3. `test_build_story_context_panels_drops_copyright_and_promo`
   —— 版权页 / 推广页剔除；
4. `test_format_panel_list_marks_reference_only_panels`
   —— 参考分镜带 `[仅参考剧情·不出镜]`，台词与画面描述一字不少；
5. `test_format_panel_list_without_reference_marks_unchanged`
   —— 不传参考集合时输出与旧行为一致；
6. `test_storyboard_constraints_block_mentions_reference_panels`
   —— 约束块写清「不得为其单独开章」；
7. `test_storyboard_constraints_block_without_reference_panels_unchanged`
   —— 无参考分镜时约束块不变；
8. `test_stage4_context_split_keeps_reference_panels_out_of_beats`（端到端）
   —— 4A 的 prompt 里能看到 p_004 且带标记，最终剧本一格参考分镜都没引用；
9. `test_stage4_drops_reference_panel_even_if_model_picks_it`（端到端）
   —— 模型违规挑了参考分镜，落盘前必须被剔掉。

**切幕预备模式（8 条）**

10. `test_storyboard_prep_last_stage_constant` —— 三个终点常量；
11. `test_manifest_preserves_semantic_storyboard_on_s4_rerun`
    —— 语义切幕成果在 S4 重跑时保留（`preserved_human_storyboard is True`）；
12. `test_manifest_preserves_human_storyboard_on_s4_rerun` —— 人工编排照旧保留；
13. `test_manifest_removes_rule_placeholder_storyboard_on_s4_rerun`
    —— `auto_strategy="rule"` 占位照旧清掉；
14. `test_storyboard_prep_mode_stops_after_s3`
    —— 切幕预备只跑 S1/S2/S3，**S4 执行 0 次**，切幕预备被调用 1 次；
15. `test_storyboard_prep_mode_promotes_s4_rerun_to_phase1`
    —— 点「S4 从此步重跑」→ 提升为出稿止步，S4 真的会跑；
16. `test_storyboard_prep_mode_promotes_s5_rerun_to_full_video`
    —— 点「S5 从此步重跑」→ 提升为全流程，一路到 S7；
17. `test_storyboard_prep_then_confirm_runs_stage4_exactly_once`
    —— **核心场景**：切幕预备（S4 跑 0 次）→ 人工确认 → 跑一次 S4，
    `executed.count("s4_scriptwriter") == 1`；
18. `test_storyboard_prep_then_confirm_preserves_semantic_board`
    —— 确认后跑 S4，磁盘上的语义切幕成果还在。

**API 层（2 条）**

19. `test_api_rejects_unknown_run_mode` —— 非法 mode → 400；
20. `test_api_accepts_storyboard_prep_mode` —— 切幕预备是合法 mode。

---

## 七、验证结果

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量单测 | `cd backend && .venv/bin/python -m pytest -n auto --dist loadfile -q` | ✅ EXIT 0（**全绿，无回归**） |
| 后端 lint | `cd backend && /opt/homebrew/bin/ruff check src tests` | ✅ All checks passed! |
| 前端类型 | `npx tsc --noEmit` | ✅ EXIT 0 |
| 前端构建 | `npm run build` | ✅ EXIT 0 |

补丁文件实测：`tests/test_patch_v436_storyboard_prep_and_context_split.py` → **20 passed**。

---

## 八、已知限制

1. **切幕预备依赖 S3 成功**：若 S3 视觉抽取没产出 `vision.json`，切幕预备会跳过
   并只记日志（不报错），流水线正常收尾，用户需要先补跑 S3。
2. **`auto_strategy` 只有三种取值**：本补丁只区分 `human` / `semantic` / 其它。
   将来若新增切幕策略，需要同步更新 `manifest.reset_from_stage` 的保留条件。
3. **参考分镜只进 4A，不进 4B**：被移出 / 跳过的分镜**永远不会出镜**，
   但它的台词需要模型自行「融入相邻章节」—— 若相邻幕内容与它毫无关联，
   仍可能被略过。这是刻意的取舍（宁可少讲，也不让用户没选过的画面出镜）。
4. **参考分镜不占预算**：章节数与字数预算只按启用幕计算，因此「移出一格」
   不会让 S4 多写一章；但也意味着移出过多会让剧情更紧凑。
5. **前端默认档位变了**：首次进入（或清过 localStorage 的用户）默认选中
   「准备分镜+切幕 (S1–S3b)」，与旧的「仅第一步」不同 —— 这是 Patch v4.36
   的设计意图，但**老用户会看到默认档位变化**。

---

## 九、后续建议

1. 「切幕预备」跑完后，镜头编排页顶部可以给一条更强的引导
   （例如「编排确认后点『按镜头组生成剧本 (S4)』，S4 只跑一遍」）。
2. 若后续要做「编排页改动 → 自动重跑 S4」的闭环，需要补一条
   「编排变更指纹」比对，避免每次都重跑。
3. `auto-motions` 与 `auto-pacing` 两个接口目前不校验分镜是否在启用幕内
   （v4.32 的 `clamp_beats_to_scope` 已覆盖 `PUT /script` 路径），
   可以把这层收口也补到这两个接口上。
