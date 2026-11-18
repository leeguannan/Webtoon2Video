# Webtoon2Video 增量补丁规格书（Patch v4.1：v4.0 评审修复 + 按步骤向后级联清理与状态重置）

> **目标**：
> 1. **Patch 20.1（v4.0 评审收尾）**：一次修清代码评审发现的 4 个边界缺陷（`cli.py` `w2v understand` 漏传 `storyboard_path`、`_splice_scene_beats` 缺失幕补写只插不删导致重复播报、4A 章数不等时下标错位、`s3b_scenes` 尾段判定笔误与跨幕顺位校验）。
> 2. **Patch 21（按步向后级联清理与状态重置）**：解决“重跑到一半或改完某一步后，下游旧产物和旧绿色完成状态混在一起分不清哪些重做了、哪些还是昨天的旧片”的核心痛点。
>    - 在 Web 审核台每一步（`S1` ~ `S7`）新增 **「向后清理重置」** 按钮（带内联确认弹窗，不用浏览器原生弹窗），按所选步骤把**当前步及之后所有下游阶段**的磁盘产物、`exports/` 导出成片、`manifest.json` 记录及内存状态彻底清空，重置为 **「待执行」**；
>    - 并在点击 **「从此步重跑」** / Storyboard 重生成时自动联动清理下游旧状态（如重跑 S4 `phase1_only` 时，S5/S6/S7 自动回到「待执行」，绝不挂着昨天的旧绿勾误导判断）。
>
> **给 `minimax-3.1-flash` 的铁律**：
> - **禁止全局重构**：严格遵守本规格书的文件修改白名单，禁止破坏 `7fa60cb`（本句配音展示/角色下拉/`+` 插入/MiniMax 试听）与 `9f710a0`（Patch 18/19/20）已有功能。
> - **分 2 轮（或 1 次按序）落地并跑通全量 `pytest` + `ruff` + `npm run lint && npm run build`**。

---

## 一、Patch 20.1：v4.0 代码评审 4 项修复

### 文件修改白名单
- `backend/src/cli.py`
- `backend/src/stages/s3b_scenes.py`
- `backend/src/stages/s4_scriptwriter.py`
- `backend/src/web/server.py`
- `src/components/StoryboardEditor.tsx`
- `backend/tests/test_patch_v40_patch19_storyboard.py`
- `backend/tests/test_patch_v40_patch20_storyboard_api.py`

### 精确修改点

1. **`backend/src/cli.py` — 打通 `w2v understand` 主链路的 Scene-First 路径**
   - 在 `produce_scripts` 内部调用 `s4_scriptwriter.run_stage4(...)`（约 L330–343）时，参照 `job_runner.py` 传入：
     - `storyboard_path=s3b_scenes.storyboard_path_for(episode_dir)`
     - `panels_dir=s2_segmenter.panels_dir_for(series_dir, episode.episode_no)`
     - `vision_client=client`
     - `vision_model=vision_model`
   - 注意兼容：`run_stage4` 内已有 `if vision is not None` 保护且 `enrich_storyboard_with_vision` 对单幕异常兜底；确认已有 `test_cli.py` 全部测试依然通过。

2. **`backend/src/stages/s4_scriptwriter.py` — 4 处细节修正**
   - **修正 `_splice_scene_beats`（L469–498）**：“只插不删”改为**“先替换旧残缺 beat，再插入新 beat”**：
     - 找出原 `beats` 中所有与该幕 `scene.panel_ids` 有交集（或目标分镜全部落在该幕区间内）的旧 beat；记录其中最靠前的插入下标 `insert_at`，**把这些属于本幕的旧 beat 从列表中剔除后**，再把 `new_beats` 插到 `insert_at`（注意索引偏移：剔除在 `insert_at` 之前的项数不会改变它自身相对于前文的位置）；
     - 若原本完全没有命中该幕的 beat，仍按 `ordered_panel_ids` 找到后一幕首次出现的 `later_index` 插入。
   - **修正缺失幕补写后的收尾校验（L2345–2356）**：
     - 在 `split_mixed_quote_beats` / `deduplicate_adjacent_beats` / `apply_dialogue_speaker_inheritance` 之后，补跑：
       - `_strip_pinyin_from_beats(merged_beats)`（若检测到拼音）
       - `_clamp_regressive_beats(merged_beats, ordered_panel_ids)`（若 `validate_beat_alignment` 仍有逆序错误，确保最终落盘的 `script.json` 100% 单调不减）。
   - **修正 `len(chapters) != len(active_scenes)` 时的章节错位（L2112–2149）**：
     - 当 `len(chapters) == len(active_scenes)` 时，按顺序一一对应；
     - 当数量不相等时，不再盲目 `chapters[index]` 按下标硬配对！改为对每个 `scene` 查找 `chapter.panel_hint_range` 与 `scene.panel_ids` 重叠格数最多的章节正文（多个 `scene` 若落入同一跨幕章，可保留共享章正文或按比例切片），完全匹配不到时才退回 `scene.visual_recap or scene.user_note`。
   - **修正禁用幕排除 & `auto` 版 `visual_recap` 回写（L1930–2086 & L2613）**：
     - 在 `scene_mode` 下，若用户把某些幕设为 `enabled=False`，先把被禁用幕独有的 `panel_ids` 从 `important_panel_ids` 与 `ordered_panel_ids` / `valid_panel_ids` 排除（仍保留所有 `enabled=True` 幕的 `panel_ids`）；若某格被人工编入启用幕且 `non_story_type not in NON_STORY_BANNER_TYPES`，放行进入 `valid_panel_ids`（即使旧 `vision.json` 曾标成 `sfx_only`）。
     - `run_stage4` 结束回写（L2613）：只要 `resolved_storyboard.source != "human"`（即 `"auto"`），即使磁盘上原本已有 `GET /storyboard` 生成的 `auto` 文件，也把刚补充完 `visual_recap` 的 `resolved_storyboard` 回写覆盖，避免下次打开 Tab 看不到摘要或重复耗费 VLM。

3. **`backend/src/stages/s3b_scenes.py` — 修正 L309 `near_tail` 笔误**
   - 原文 `near_tail = usable_count - len(current) < 5` 在长话里因为 `usable_count` 已累加全话总数而恒为 `False`；
   - 循环结束位置处的 `current` 本身就是整话的**最后一组剩余格**（距末尾就是 0 格！），改为：只要末尾残余包含对白（`has_dialogue`）**或** `len(current) >= 2`（例如 2 格纯动作反转/定格），或前一组已满 `>= 8` 格，就独立保留成收尾幕，不被强行吞进上一幕。

4. **`backend/src/web/server.py` — `_validate_storyboard_scenes` 增加跨幕单调性校验**
   - 在已有的“幕内单调不减”校验之上，追加跨幕顺序检查：后一幕的起始分镜 `panel_sort_key(panel_ids[0])` 必须 `>` 前一幕的末尾分镜 `panel_sort_key(prev_panel_ids[-1])`，否则抛出 HTTP `400`（提示“第 X 幕与前一幕分镜顺序交叉，请按剧情时间顺序排列”）。

5. **`src/components/StoryboardEditor.tsx` — 小体验优化**
   - 在“后台任务已提交”的绿色通知条右侧，加一个快捷按钮 **「前往剧本审核 →」** 调用 `onSwitchToScriptTab()`；
   - 点击一次「恢复自动分组」进入二次确认态后，在旁边补一个小「取消」按钮（或 4 秒后自动复位 `confirmReset=false`），防止误点卡在确认态。

---

## 二、Patch 21：基于当前步骤向后级联清理产物与重置状态（Cascade Clean & Reset）

### 文件修改白名单
- `backend/src/core/manifest.py`
- `backend/src/web/job_runner.py`
- `backend/src/web/server.py`
- `backend/src/cli.py`（新增 `w2v clean` 命令，复用同一个核心清理函数）
- `src/types/studio.ts`
- `src/App.tsx`
- `src/components/EpisodeStudioView.tsx`
- `backend/tests/test_patch_v41_cascade_reset.py`（**新增单测**）

### 精确实现规格

#### 1. `backend/src/core/manifest.py` — 核心级联清理方法
在 `ManifestManager` 上新增（并导出干净的数据类/字典返回结构）：
```python
def reset_from_stage(
    self,
    episode_no: int,
    from_stage: str,
    *,
    include_human_storyboard: bool = False,
) -> dict[str, Any]:
```
**精确语义**：
1. **阶段范围校验**：
   - `from_stage` 必须是 `STAGE_KEYS` 之一（`s1_crawler` ~ `s7_renderer`）；`"s0_catalog"` 是作品级阶段，禁止单话清理（若传入 `"s0_catalog"` 或未知阶段，抛出 `ValueError`）。
   - 受影响阶段列表：`affected = STAGE_KEYS[STAGE_KEYS.index(from_stage):]`（即从 `from_stage` 到 `s7_renderer` 的全部阶段）。
2. **安全清理各阶段磁盘产物（只清 `affected` 里的阶段，上游原样不动）**：
   - 清理目录时只删除目录内的文件/子目录（然后确保空阶段目录本身存在，避免其它模块 `mkdir` 假设出错）：
   - `s1_crawler` $\rightarrow$ 清空 `01_raw/`
   - `s2_segmenter` $\rightarrow$ 清空 `02_panels/`
   - `s3_vision` $\rightarrow$ 清空 `03_vision/`
   - `s4_scriptwriter` $\rightarrow$ 精细清理 `04_script/`：
     - 总是删除：`script.json`、`script_readable.md`、`story_draft.json`、`story_draft.md` 及其它临时产物；
     - **`storyboard.json` 保护策略**：
       - 如果 `from_stage` 在 `s1_crawler` / `s2_segmenter` / `s3_vision`（上游分镜/识图都清了，旧镜头编排已无意义）$\rightarrow$ **一并删除 `storyboard.json`**；
       - 如果 `from_stage == "s4_scriptwriter"`：检查 `04_script/storyboard.json` 的 `source` 字段，若是 `"human"` 且 `not include_human_storyboard` $\rightarrow$ **默认保留**（返回结果里标记 `preserved_human_storyboard: True`）；否则（`source == "auto"` 或 `include_human_storyboard=True`）一并删除；
     - 同时若 `{series_dir}/bible_snapshots/ep_{episode_no:03d}.json` 存在，一并删除（防本话旧快照残留；若只清理 `s5~s7` 则不删快照）。
   - `s5_tts` $\rightarrow$ 清空 `05_audio/`
   - `s6_timeline` $\rightarrow$ 清空 `06_plan/`
   - `s7_renderer` $\rightarrow$ 清空 `07_out/`，**同时扫描 `{series_dir}/exports/` 删除所有以 `EP{episode_no:03d}_` 开头的导出成片（如 `EP001_*.mp4` / `.ass`）**，防止导出区和预览残存旧成片！
3. **重置 `manifest.json`**：
   - 加载当前 `manifest = self.load(episode_no)`，把 `manifest.stages` 里属于 `affected` 的 key 全部移除（或确保状态恢复未执行）；如果 `manifest.stages` 发生变更或文件已存在，调用 `self.save(manifest)` 落盘。
   - **关键不变量**：清理完成后，紧接着调用 `infer_stage_status(episode_dir)` 对所有 `affected` 阶段必须 **100% 返回 `StageStatus.PENDING`**（因为真实文件已删），而 `from_stage` 之前已完成的上游阶段保持 `StageStatus.DONE` 不变！
4. 返回结构示例：
   ```python
   {
       "episode_id": self.episode_id(episode_no),
       "episode_no": episode_no,
       "from_stage": from_stage,
       "cleared_stages": list(affected),
       "removed_count": removed_count,
       "preserved_human_storyboard": preserved_human_storyboard,
   }
   ```

#### 2. `backend/src/web/job_runner.py` — 内存状态同步 + 重跑自动清理下游
1. **并发互斥保护**：
   - 若某话当前在 `self._states` 里 `status in ("queued", "running")`，禁止执行手动清理，抛出 `JobError("该话正在后台执行任务，请等待完成后再清理")`（接口转为 HTTP `409`）。
2. **新增方法 `reset_episode_from_stage(...) -> dict[str, Any]`**：
   - 在 `self.gate(series_id)` 或确认无进行中任务后，调用 `ManifestManager(series_dir, series_id=series_id).reset_from_stage(episode_no, from_stage, include_human_storyboard=include_human_storyboard)`；
   - 同步刷新内存中的 `EpisodeState`：
     - 将 `affected` 阶段全部从 `state.stages` 中移除（连带清除旧 `duration_sec` / `started_at`）；
     - 若 `state.current_stage in affected`，置为 `""`；
     - `state.status = "idle"`、`state.error = ""`；
     - 记录日志：`[清理重置] 已清空 {from_stage} 及下游 {len(affected)} 个阶段产物，状态恢复待执行`；
     - `self._publish_state(state)` 通过 SSE 广播最新状态。
3. **重跑自动清下游（关键防混状态闭环！）**：
   - 在 `submit_episode(series_id, episode_no, *, from_stage="s1_crawler", mode="phase1_only")` 中，当任务真正开始执行（或在入队前校验无其它正在运行的同话任务后、`await self._run_episode(...)` 真正动工前）：
     - 在真正执行阶段循环前，先用 `ManifestManager.reset_from_stage(episode_no, from_stage, include_human_storyboard=False)` 清理 `from_stage` 及全部下游旧产物与 `manifest` 记录，并同步清理 `state.stages` 中 `from_stage` 及下游的旧 `done` / `duration_sec`！
     - **效果**：即使你从 `S4` 点「从此步重跑」且当前全局模式是 `phase1_only`（只重跑 S4 不跑 S5~S7），重跑开始的一瞬间，S5/S6/S7 的旧音频/旧时间轴/旧成片就会被立刻清掉、卡片立刻回到灰白「待执行」；再也不会出现“新 S4 剧本配着昨天旧 S5/S7 的绿色已完成假象”。
     - 特别注意：`PUT /api/episodes/{id}/script` 带 `from_stage="s5_tts"` 触发只从 S5 重跑时，`reset_from_stage(..., "s5_tts")` 只清 `s5..s7`，**绝不会删 `s4` 刚刚保存的 `script.json`**，逻辑完全自洽！

#### 3. `backend/src/web/server.py` 与 `backend/src/cli.py`
1. **`server.py` 新增接口 `POST /api/episodes/{episode_id}/reset`**：
   - 请求体 `EpisodeResetRequest(BaseModel)`:
     - `from_stage: str`
     - `include_human_storyboard: bool = False`
   - 校验：
     - 找不到话次 $\rightarrow$ `404`；
     - `from_stage not in STAGE_IDS or from_stage == "s0_catalog"` $\rightarrow$ `400`（明确提示“只能从 S1 ~ S7 清理单话下游阶段”）；
     - 任务正在排队/运行中 $\rightarrow$ `409`；
   - 调用 `job_runner.reset_episode_from_stage(...)`，并组装返回最新话次刷新所需数据：
     ```json
     {
       "episode_id": "wt_5081_ep_001",
       "from_stage": "s4_scriptwriter",
       "cleared_stages": ["s4_scriptwriter", "s5_tts", "s6_timeline", "s7_renderer"],
       "removed_count": 12,
       "preserved_human_storyboard": true,
       "stages": [ ... ],
       "logs": [ ... ]
     }
     ```
     （`stages` 直接调用 `view._stages(series_id, episode_no, state)` 生成，保证与 `GET /api/series/{id}` 口径 100% 一致）。
2. **`cli.py` 新增子命令 `w2v clean`**：
   - `w2v clean --series <id> [--episode <no> | --episodes 1-3] --from-stage s4_scriptwriter [--include-human-storyboard]`
   - 直接复用 `ManifestManager.reset_from_stage(...)` 并输出 JSON 清理摘要。

#### 4. 前端类型、请求与交互 UI（`src/types/studio.ts`、`src/App.tsx`、`src/components/EpisodeStudioView.tsx`）
1. **`src/types/studio.ts`**：
   - 新增 `EpisodeResetResponse`：
     ```ts
     export interface EpisodeResetResponse {
       episode_id: string;
       from_stage: StageId;
       cleared_stages: StageId[];
       removed_count: number;
       preserved_human_storyboard: boolean;
       stages: StageProgress[];
       logs?: string[];
     }
     ```
2. **`src/App.tsx`**：
   - 新增 `handleResetFromStage(episodeId: string, stageId: StageId, includeHumanStoryboard?: boolean)`：
     - 调用 `POST /api/episodes/${encodeURIComponent(episodeId)}/reset`；
     - 成功后立即通过 `refreshSeries(seriesIdOf(episodeId))`（以及先乐观更新 `stages` / 清空已清理阶段对应的前端 `panels` / `beats` / `video_duration_ms`）刷新工作台状态并 `showToast(...)` 提示已清理范围（例如：`已清理 S4 → S7 的旧产物并重置为待执行`）；
     - 把 `onResetFromStage` 传给 `<EpisodeStudioView />`。
3. **`src/components/EpisodeStudioView.tsx`**：
   - Props 新增 `onResetFromStage?: (episodeId: string, stageId: StageId, includeHumanStoryboard?: boolean) => Promise<void> | void;`。
   - 顶部说明副文案更新为：`点击「从此步重跑」或「向后清理」都会先清空该步及全部下游旧产物与完成标记，保证状态与真实成片一致。`
   - 在 8 步流水线卡片（`episode.stages.map((st) => ...)`）中：
     - 保留原有的 **`从此步重跑`** 按钮；
     - 对 `st.id !== 's0_catalog'`（即 `S1` ~ `S7`）在卡片底部增加一个清晰紧凑的次按钮 **`清理重置`**（如用灰红描边/垃圾桶或回退图标，与主按钮区分，卡片正在运行任务时自动 `disabled`）；
     - 点击某步的 **`清理重置`** 后，**在卡片网格下方弹出一段清晰的内联确认横幅**（严禁用浏览器原生 `window.confirm` / `window.alert`）：
       - 标题明确列出受影响阶段范围，例如：`确认清理并重置「${st.code} ${st.label}」及之后全部阶段（${affectedCodes.join(' → ')}）？`
       - 列出即将清理的内容说明（如：`将删除旧音频、时间轴、渲染成片与 exports 导出文件，并把这些步骤恢复为「待执行」。`）；
       - 若 `st.id === 's4_scriptwriter'`，显示复选框：`[ ] 同时清空人工编排的镜头组（storyboard.json）（默认保留）`；若在 `S1~S3`，提示：`同时会清除依赖旧分镜的镜头编排（storyboard.json）与识图结果`；
       - 提供 **`确认清理`**（红色主按钮，提交期间显示 loading）与 **`取消`** 按钮。

#### 5. 单测 `backend/tests/test_patch_v41_cascade_reset.py`
至少覆盖以下 6 项核心用例：
1. `ManifestManager.reset_from_stage(1, "s5_tts")` 清空 `05_audio` / `06_plan` / `07_out` 与 `exports/EP001_*.mp4`，保留 `01_raw` ~ `04_script`，且 `infer_stage_status` 返回 `s1..s4 == DONE`、`s5..s7 == PENDING`；
2. `reset_from_stage(1, "s4_scriptwriter")` 默认保留 `source == "human"` 的 `storyboard.json` 并删除 `script.json` / `story_draft.json` / `bible_snapshots/ep_001.json`；传入 `include_human_storyboard=True` 时连同 `storyboard.json` 一并删除；
3. `reset_from_stage(1, "s3_vision")` 清理 `s3..s7` 时连同依赖旧识图的 `storyboard.json` 一并删除；
4. `reset_from_stage(1, "s0_catalog")` 抛出 `ValueError`；
5. `POST /api/episodes/{id}/reset` 正常返回 200 并更新 `stages`；传入 `s0_catalog` 返回 400；当话次正在运行任务时返回 409；
6. `JobRunner.submit_episode(..., from_stage="s4_scriptwriter", mode="phase1_only")` 开跑前自动清掉 `s5..s7` 的旧产物与旧 `done` 状态，跑完停在 S4 后 `s5..s7` 真实呈现 `pending` 而非假 `done`。

---

## 三、发给 `minimax-3.1-flash` 的即拷即用指令

可以直接将下面这段指令发给 `minimax-3.1-flash` 一次落地 **Patch 20.1 + Patch 21**：

```text
请严格按照项目根目录 `PATCH_v4.1_CASCADE_CLEAN_AND_FIXES.md` 实施两部分修改（遵守文件白名单，不要做无关全局重构）：

第一部分（Patch 20.1：v4.0 评审修复）：
1. `backend/src/cli.py`：在 `produce_scripts` 调用 `s4_scriptwriter.run_stage4` 时传入 `storyboard_path`、`panels_dir`、`vision_client=client`、`vision_model=vision_model`（保持现有测试通过）；
2. `backend/src/stages/s4_scriptwriter.py`：修复 `_splice_scene_beats` 先删本幕残缺旧 beat 再插入新 beat；补写后执行 `_strip_pinyin_from_beats` 与 `_clamp_regressive_beats`；`len(chapters) != len(active_scenes)` 时按 `panel_hint_range` 重叠匹配；`scene_mode` 下把禁用幕排除出 `important_panel_ids` 并允许回写 `auto` 版 `visual_recap`；
3. `backend/src/stages/s3b_scenes.py`：修正 L309 `near_tail` 恒假笔误；
4. `backend/src/web/server.py`：`_validate_storyboard_scenes` 增加跨幕分镜顺序单调校验；
5. `src/components/StoryboardEditor.tsx`：任务提交成功条加「前往剧本审核 →」按钮（调用 `onSwitchToScriptTab`），恢复确认加「取消」按钮。

第二部分（Patch 21：按步骤向后级联清理与状态重置）：
1. `backend/src/core/manifest.py`：实现 `ManifestManager.reset_from_stage(...)`，彻底清空所选阶段及全部下游目录、对应 `exports/EP{no:03d}_*` 与 `manifest.json` 状态，保护 `source == "human"` 的 `storyboard.json`（除非上游 S1~S3 被清或显式勾选）；
2. `backend/src/web/job_runner.py`：新增 `reset_episode_from_stage(...)`（运行中返回冲突），并在 `submit_episode` 开跑前自动清理 `from_stage` 及下游旧产物与状态，彻底消灭“假已完成”残留；
3. `backend/src/web/server.py` 与 `backend/src/cli.py`：新增 `POST /api/episodes/{episode_id}/reset` 与 `w2v clean` 命令；
4. `src/types/studio.ts`、`src/App.tsx`、`src/components/EpisodeStudioView.tsx`：在步骤条卡片 S1~S7 增加「清理重置」按钮与内联确认条（不用 window.confirm），点击后清掉下游旧状态与前端缓存并立刻同步为「待执行」。

完成后编写并通过 `backend/tests/test_patch_v41_cascade_reset.py` 及全部现有 `pytest`、`ruff check`、`npm run lint && npm run build`。
```
