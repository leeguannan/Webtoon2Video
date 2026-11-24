# PATCH v4.32 —— 镜头编排范围强制收口（成片不再出现用户没选过的画面）

> 基线提交：`a40c4bb`（PATCH_v4.31：取消竖扫 + 「同画防跳变 / 呼吸节奏」自动动效）
> 上一版相关：`PATCH_v4.29_STORYBOARD_S4_ALIGNMENT.md`（编排重组后 Stage 4 对齐）

---

## 一、用户报障与铁律

**用户原话**：

> 「这一帧画面我并没有在分镜中选择过，为什么在视频中有？这个问题已经出现了两次了，为什么没有根治呢？」

截图：`wt_3924_ep_001.mp4` 播放头 `00:26`，字幕「老师则站在一旁大声的斥责道:」，
画面是白底气泡「政治人脉也不是盖的…」+ 西装男 + 「您聽過「朱英建設」吧?」的竖条漫画。

### 铁律（Patch v4.32 的全部行为都必须服从这三条）

1. **成片里出现的每一格画面，都必须能追溯到用户在镜头编排页勾选过的分镜。**
   「用户在分镜里选过」的唯一权威口径 = `04_script/storyboard.json` 里**启用幕**
   （`enabled=True`）引用的 `panel_ids`。
2. **同一口径必须在三个层面同时生效**：写回层（保存剧本时收口）、渲染层
   （Stage 6 生成时间轴时再兜底一次）、审核台（前端不让用户选到范围外的格）。
   任何一层单独生效都不够 —— 手工改盘、历史文件、其它工具写过的剧本都要兜住。
3. **向后兼容**：没有编排文件 / 编排文件损坏 / 编排里没有任何启用幕时，
   本层**不约束任何东西**，旧工程的成片行为一个字都不变。

---

## 二、取证与根因

### 2.1 取证（`workspace/wt_3924/episodes/ep_001`）

| 项 | 数据 |
| --- | --- |
| 成片 | `07_out/wt_3924_ep_001.mp4`（150,712,198 字节，时长 415.29s） |
| 时间轴 | `06_plan/timeline.json`，126 scenes，fps=30，total_duration_ms=410,378 |
| 剧本 | `04_script/script.json` |
| 镜头编排 | `04_script/storyboard.json`，`source="human"`，12 幕全部 enabled，共引用 155 格分镜 |

用「等比 fit+pad 后灰度降采样 MSE」把成片第 26 秒那一帧与全部 285 格里逐一比对：

```
frame26.jpg → p_017 (MSE 5151) → p_121 (5749) → p_172 (5882)   ← p_017 显著胜出
```

而时间轴上 26 秒落在 `scene_index=10`（`24816-30261ms`，`b_new_125` → `p_014`）。
`p_017.png` 与截图目视完全一致。

**结论：画面序列本身没有乱序**。成片比时间轴长 4.9s，差额来自 `s7_renderer` 的
`eff_cover_ms`（封面）整体偏移 —— 所以 00:26 处看到的是时间轴 23~25s 的内容。
问题不在时间轴，而在**「审核台所见 ≠ 成片所播」**。

### 2.2 根因一：多目标 beat 的第 2..N 格在审核台完全不可见

`b4` 的 `target_panel_ids` 是 `["p_014", "p_017"]`。Stage 6 按格数**均分时长**
轮播两格（`s6_timeline.build_timeline` 的 `divmod(total_frames, count)`），
但审核台每行的分镜下拉写死是：

```tsx
// src/components/EpisodeStudioView.tsx（旧）
<select value={beat.target_panel_ids[0]} onChange={e => onChangePanel(beat.beat_id, e.target.value)}>
```

用户看到的是 `p_014`，成片播的是 `p_017` → 观感就是「这帧我没选过」。
全话共 5 处多目标：`b4` / `b46` / `b63` / `b64` / `b72`。

### 2.3 根因二：编排范围外的分镜引用能一路写进剧本并渲染

`b_new_142`（文案「血瞬间流淌开来。」）指向 `p_094`，而 **`p_094` 不在任何启用幕的
`panel_ids` 里** —— 用户从没在镜头编排页勾选过它。

- `b_new_*` 这个 beat_id 前缀**只有前端会生成**（`EpisodeStudioView.tsx` 的
  `uniqueBeatId()`），后端 Stage 4 一律重排成 `b{index}` → 说明这句是**用户在
  审核台手工新增/插入**的；
- 新增句的默认分镜是 `episode.panels[0]`（**全部分镜**的第一格），下拉选项也是
  全部分镜，不区分编排内外；
- 保存链路 `PUT /api/episodes/{id}/script` **完全不校验** `target_panel_ids`
  是否在编排范围内；
- Stage 6 又原样渲染 → 成片里出现了用户没选过的画面。

全话 126 帧的画面来源分类：**编排外 1 帧（p_094）** / 仅作非首目标出现 4 帧 /
审核台首目标 121 帧。

### 2.4 为什么 v4.29 没根治

v4.29 的 `_converge_panel_pool` 收敛的是**喂给 Stage 4 模型的池**，只约束
「Stage 4 生成时能看见哪些分镜」；它**不约束模型反写的结果**，更不约束
**用户在审核台手改/手加的 beat**。而 v4.31 的 `apply_auto_motions` / v4.28 的
`auto-motions` 接口同样只改 `motion`，不碰 `target_panel_ids`。

---

## 三、修复方案

### 3.1 新增 canonical owner 模块 `backend/src/core/panel_scope.py`

把「镜头编排页启用幕范围」确立为唯一权威：

| 符号 | 作用 |
| --- | --- |
| `PanelScope` | 不可变数据类：`allowed_panel_ids`（启用幕并集）/ `scene_panel_ids` / `source` |
| `PanelScope.allows(panel_id)` | 该分镜是否在范围内（经 `PanelIdResolver` 归一，支持 `p_1` / `p_001` 各种写法） |
| `PanelScope.scene_panels(scene_id)` | 某幕引用的分镜（自然序） |
| `PanelScope.nearest_allowed(panel_id)` | 范围内序位最接近的一格（按尾部数字距离） |
| `scope_from_storyboard(storyboard)` | 从编排对象提取范围（**只取 enabled 幕**） |
| `load_panel_scope(episode_dir)` | 读 `04_script/storyboard.json`；**文件缺失/损坏 → None（不约束）** |
| `clamp_beats_to_scope(beats, scope)` | 越界 target 剔除；整句越界时按「本句归属幕内的分镜 → 序位最近的一格」顶上，保证 `target_panel_ids` 永不为空 |
| `violations_payload(violations)` | 转成回给前端的 JSON 结构 |

### 3.2 Stage 6 渲染层收口（`backend/src/stages/s6_timeline.py`）

- `build_timeline(...)` 新增关键字参数 `panel_scope: PanelScope | None = None`
  —— **默认值 None，行为与 Patch v4.32 之前完全一致**（向后兼容）；
- 选画面时：指向范围外分镜的 target **直接剔除**并打印
  `[s6_timeline] xxx 的目标分镜 [...] 不在镜头编排范围内，已剔除`；
- 一句的 target 全被剔除时，回退到**同幕**（`beat.scene_id`）的分镜，
  同幕没有则回退到**序位最近**的一格 —— **不是上一句的画面**（画面与解说对不上）；
- `build_timeline_from_workspace(...)` 自动读编排文件并传入（打印
  `[s6_timeline] 镜头编排范围生效：允许 N 格分镜`）。

### 3.3 后端写回层收口（`backend/src/web/server.py`）

- `PUT /api/episodes/{id}/script`：`load_panel_scope` → `clamp_beats_to_scope`，
  越界时打印 `[server] 分镜引用超出镜头编排范围，已收口：…`；
  **响应体新增 `panel_scope_violations`**，把改了哪几句、去掉哪些格、改用了哪一格
  如实回传；
- `POST /api/episodes/{id}/script/auto-pacing`、`POST /api/episodes/{id}/auto-motions`：
  写回前同样过一遍 `_clamp_beat_dicts(...)`，不把越界引用重新落盘。

### 3.4 前端审核台（`src/components/EpisodeStudioView.tsx`）

- 新增 `scopePanelIds`（启用幕 `panel_ids` 并集；**空集合 = 不限制**，与后端同口径）；
- 分镜单元格改为**逐格渲染 select**：第 1 格沿用原交互，第 2..N 格可单独替换
  （`onChangePanelAt`）或删除（`onRemovePanelAt`，至少保留一格）；
  —— 从此「一句配了几格」在审核台**看得见、改得动**；
- 下拉选项：编排非空时**只列编排内的分镜**；本句当前用到的编排外分镜仍保留在
  列表里并标注「（编排外）」，让用户能看清问题而不是被悄悄改掉；
- 新增句 / 插入句：默认分镜改为「**编排范围内第一格**」，继承的分镜先按范围过滤；
- 改分镜不再把多目标压成一格；
- 保存提示：后端回传 `panel_scope_violations` 时提示
  「已保存，但有 N 处分镜引用不在镜头编排范围内，已自动改回编排内（明细…）」。

### 3.5 前端大图弹窗（`src/components/PanelZoomModal.tsx`）

- 以前只显示 `target_panel_ids[0]`；现在按格渲染缩略图条，点击可切换查看每一格
  —— 「这句到底会播哪几张」在弹窗里也一目了然。

---

## 四、涉及文件

| 文件 | 改动 |
| --- | --- |
| `backend/src/core/panel_scope.py` | **新增**（canonical owner 模块） |
| `backend/src/stages/s6_timeline.py` | 渲染层收口（`panel_scope` 参数 + 剔除 + 回退） |
| `backend/src/web/server.py` | 写回层收口（`PUT /script` / `auto-pacing` / `auto-motions`） |
| `backend/tests/test_patch_v432_storyboard_scope_enforcement.py` | **新增**（18 例） |
| `src/types/studio.ts` | `PanelScopeViolation` + `ScriptUpdateResponse` / `ScriptSaveOutcome` 字段 |
| `src/components/EpisodeStudioView.tsx` | 逐格分镜编辑 + 范围约束下拉 + 提示条 |
| `src/components/PanelZoomModal.tsx` | 多格缩略图条 |
| `src/App.tsx` | 回传 `scope_violations` |

---

## 五、测试

`backend/tests/test_patch_v432_storyboard_scope_enforcement.py`（18 例，全离线）：

| 组 | 用例 | 断言要点 |
| --- | --- | --- |
| canonical owner | `test_scope_only_contains_enabled_scenes` | 禁用幕的分镜不在范围内 |
| | `test_scope_without_enabled_scene_is_empty_and_not_constraining` | 无启用幕 → 空范围 → 不约束 |
| | `test_load_panel_scope_returns_none_without_storyboard` | 文件缺失 / 损坏 → `None` |
| | `test_load_panel_scope_reads_enabled_scenes` | 正常读出启用幕并集 |
| 收口 | `test_clamp_drops_out_of_scope_targets_but_keeps_valid_ones` | 剔越界、留范围内、`replacement is None` |
| | `test_clamp_replaces_when_every_target_is_out_of_scope` | 整句越界 → 换成范围内一格，target 永不为空 |
| | `test_clamp_prefers_same_scene_panel_before_nearest_allowed` | 优先用本句归属幕内的分镜 |
| | `test_clamp_is_noop_without_scope` | 无范围 → 原样返回 |
| | `test_violations_payload_shape` | 回传结构固定 |
| Stage 6 | `test_build_timeline_drops_out_of_scope_panels` | 越界格不渲染 |
| | `test_build_timeline_falls_back_to_nearest_in_scope_panel` | 整句越界 → 就近换格 |
| | `test_build_timeline_falls_back_to_same_scene_panel_first` | 同幕优先（与写回层同口径） |
| | `test_build_timeline_without_scope_keeps_previous_behaviour` | 无范围 → 行为不变（向后兼容） |
| | `test_build_timeline_from_workspace_reads_storyboard_scope` | 工作区构建自动读编排 |
| Web API | `test_put_script_clamps_out_of_scope_and_reports` | 收口 + 回传 `panel_scope_violations` + 不破坏 `voice_override` / `pause_after_ms` |
| | `test_auto_motions_endpoint_clamps_out_of_scope` | auto-motions 写回也收口 |
| | `test_auto_pacing_endpoint_clamps_out_of_scope` | auto-pacing 写回也收口 |
| | `test_put_script_without_storyboard_keeps_everything` | 无编排 → 不约束（向后兼容） |

### 真机验证（真实话次 `workspace/wt_3924/ep_001`）

```
scope source: storyboard allowed: 155
violations:  [('b_new_142', ['p_094'], 'p_060')]

OLD p_094 次数: 1        ← Patch v4.32 之前：编排外的分镜真的进了成片
NEW p_094 次数: 0        ← Patch v4.32 之后：成片里不再出现
画面变化: [(50, 'p_094', 'p_060')]
[s6_timeline] b_new_142 的目标分镜 ['p_094'] 不在镜头编排范围内，已剔除
```

即：出问题的 `b_new_142` 那一帧，画面从「用户没选过的 `p_094`」换成
「用户选过的、且属于同一幕的 `p_060`」，其余 125 帧**一帧都没变**。

---

## 六、验证结果

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `cd backend && .venv/bin/python -m pytest -n auto --dist loadfile` | ✅ EXIT 0 |
| 后端 lint | `cd backend && /opt/homebrew/bin/ruff check src tests` | ✅ All checks passed! |
| 前端类型 | `npx tsc --noEmit` | ✅ EXIT 0 |
| 前端构建 | `npm run build` | ✅ ✓ built |

---

## 七、已知限制

1. **兜底替换是启发式的**：整句越界时优先用「本句归属幕内的分镜」，同幕没有
   可用格才退回「序位最近」的一格。序位最近 ≠ 叙事最贴切 —— 属于兜底，
   正常情况下不会有越界引用走到这一步（写回层已经先收口过一次）。
2. **审核台的下拉仍保留「本句当前用到的编排外分镜」**（标注「（编排外）」）：
   目的是让用户**看得见**问题并自己决定改不改，而不是被系统悄悄改掉；保存时
   仍会被后端收口。
3. **编排范围只认「启用幕」**：被禁用的幕里的分镜不算「用户选过的画面」，
   引用它们同样会被收口。
4. **不追溯历史成片**：本补丁只保证**之后**重跑 S6~S7 的成片不再出现越界画面；
   已经渲染好的旧成片不会自动修。
5. **前端没有自动化测试框架**（`package.json` 只有 `dev/build/lint/preview`），
   本次前端改动只经过 `tsc --noEmit` + `npm run build` 的静态验证，
   交互层仍建议在浏览器里人工过一遍审核台。
