# PATCH v4.44 —— 多风格引擎（StyleProfileSpec）+ 动作连招蒙太奇提效 + 繁中前置规范化

- 基线提交：`caca2d7`（PATCH v4.43：全局音效资源库）
- 变更规模：19 个文件改动 +1153/−38；新增 4 个文件（`backend/src/core/style_profiles.py`、`backend/prompts/styles/{drama_suspense,action_combat}.md`、`backend/tests/test_patch_v444_style_profiles_and_action_montage.py`）
- 验收命令：`cd backend && pytest -n auto --dist loadfile`、`cd backend && ruff check src tests`、`npx tsc --noEmit && npm run build`

---

## 一、为什么做这个补丁

同一个流水线要同时服务两类完全不同的漫画，v4.43 之前只有一套「言情悬疑」的隐含假设：

| 维度 | 言情 / 剧情漫（现状） | 热血动作 / 格斗漫（想要） |
| :--- | :--- | :--- |
| 切图 | 保守，黑底画风容易被切碎 | 黑底过渡带也要能切，拟声字碎片要并进主画面 |
| 分幕 | 一幕 12 格 | 一幕可以到 16 格（动作镜头密度高） |
| 文案 | 心理独白 + 时间线留白 | 禁止逐格招式播报，要合并成一条短促总括 |
| 停顿 | 换格 400ms / 转折 950ms / 幕末 1150ms | 换格 160ms / 转折 450ms / 幕末 600ms |
| 运镜 | 宽比 ≥1.35 才横扫、连续动态 ≤3 拍 | 宽比 ≥1.15 即横扫、连续动态可到 6 拍 |

以前没有「风格」这个概念，只有散落在各 Stage 里的常量。想支持动作漫，只能一处一处改数字 —— 那等于把两种作品硬塞进同一条主干。

## 二、五条铁律

1. **绝对零回归**：未显式切换风格的存量作品默认走 `drama_suspense` 基线，S2 切图 / S3b 分幕 / S4 文案·停顿·运镜 / S6 时间轴的行为与 v4.43 **100% 等价**。
2. **禁止按作品名写死分支**：严禁 `if series_name == ...`。各 Stage 只读强类型 `StyleProfileSpec` 的能力开关与数值；新增风格只需在注册表加一条预设，不改管线主干。
3. **单一数据源**：风格参数只在 `core/style_profiles.py` 定义一次，作品级 `series_config.yaml` 只存「选哪个预设 + 可选覆盖」，不复制数值。
4. **不改既有铁律**：`drama_suspense` 的风格片段只复述已有铁律、不新增任何要求，保证语义零回归。
5. **测试与构建全绿**：新增双轨防回归单测（基线轨 + 动作轨），后端全量 pytest、ruff、tsc、build 必须全通过。

---

## 三、风格模型（Part 1）

### 3.1 五个正交维度

新建 `backend/src/core/style_profiles.py`：

| 维度 | 字段 | drama_suspense（= 4.43 现值） | action_combat |
| :--- | :--- | :--- | :--- |
| `segment` | `allow_dark_gutter` | False | **True** |
| | `dark_row_mean_max` | 28.0 | 28.0 |
| | `merge_sfx_fragments` | False | **True** |
| | `max_slices_per_group` | 1 | 1 |
| `montage` | `allow_action_combo` | False | **True** |
| | `combo_min_panels` / `combo_max_panels` | 3 / 6 | 3 / 6 |
| | `max_scene_panels` | 12 | **16** |
| `narrative` | `prompt_fragment_file` | `drama_suspense.md` | `action_combat.md` |
| | `forbid_play_by_play` | False | **True** |
| | `emphasize_timeline_and_inner_monologue` | True | **False** |
| | `target_beat_density_ratio` | 1.0 | **0.55** |
| `motion` | `wide_pan_ratio` | 1.35 | **1.15** |
| | `prefer_vertical_focus_sweep` | True | True |
| | `max_dynamic_streak` | 3 | **6** |
| | `max_shake_streak` | 1 | 1 |
| `pacing` | `panel_switch_ms` | 400 | **160** |
| | `turn_ms` | 950 | **450** |
| | `scene_end_ms` | 1150 | **600** |
| | `breath_ms` | 480 | **260** |
| | `same_panel_min_ms` | 150 | **100** |
| | `default_speed_bias` | 1.0 | 1.0 |

### 3.2 解析函数

- `get_style_profile(profile_id)` —— 未知 / 空 → 回退 `drama_suspense` 并打 warning（绝不抛异常打断流水线）。
- `list_style_profiles()` —— 返回 `id/label/description/recommended_for` 供前端下拉。
- `resolve_series_style_profile(series_config, *, overrides=None)` —— 三层深度合并：**预设 → `series_config.style_overrides` → 临时局部覆盖**；用 `getattr` 鸭子类型读配置，避免与 `core.config` 形成循环导入。
- `suggest_style_profile_from_url_or_genre(url_or_genre)` —— URL 含 `/action/`、`/martial-arts/`、`/wuxia/`、`/combat/` 等特征 → 建议 `action_combat`，否则 `drama_suspense`。

---

## 四、各 Stage 接线（Part 2 / Part 3）

### 4.1 配置与数据结构

- `core/config.py`：`SeriesConfig` 末尾追加 `style_profile: str = "drama_suspense"` 与 `style_overrides: dict[str, Any] = {}`；旧 `series_config.yaml` 照常可解析。
- `core/schemas.py`：`PanelVisionInfo` 末尾追加 `is_action_beat: bool = False`。

### 4.2 Stage 2 切图（`s2_segmenter.py`）

- 新增 `find_dark_gutter_gaps()`：行均值 ≤ `dark_row_mean_max` 且行标准差 < 12 的**连续暗色带**才算过渡带。
- `cut_atomic_blocks(..., allow_dark_gutter=False)`：启用后把暗底带当作切点候选，**每条带子先过 `_has_local_structure()`**（深黑底 + 高亮对白框不会被误当过渡带切碎）。
- 新增 `merge_sfx_fragment_blocks()`：高度 < 0.45×宽 且与邻块间隙 < 0.30×宽 的孤立拟声字 / 特效碎片，并入紧邻的主动作画面；合并后高度不超过 `min(MAX_ATOMIC_BLOCK_HEIGHT=3200, 3.6×宽)`。
- `segment_episode(..., style_profile=None)`：两个能力开关各自独立生效；`cli.py` 与 `web/job_runner.py` 调用前先 `resolve_series_style_profile(load_series_config(series_dir))`。

### 4.3 Stage 3 识图（`s3_vision.py` + prompt）

- 新增 `ACTION_BEAT_KEYWORDS`（搏斗 / 闪避 / 兵器碰撞 … 共 30 词）与 `infer_action_beat(payload)`：**显式字段优先**；旧 `vision.json` 没有该字段时，用「整格无对白 + 摘要命中关键词」兜底判定。
- `vision_extract.md` / `vision_extract_batch.md` 的 JSON 结构新增 `"is_action_beat": false` 与字段说明。

### 4.4 Stage 3b 分幕（`s3b_scenes.py`）

- `auto_build_storyboard` / `semantic_build_storyboard` / `load_or_build_storyboard` / `resolve_or_build_storyboard` 全部接收并透传 `style_profile`；用 `profile.montage.max_scene_panels` 取代写死的 `MAX_SCENE_PANELS = 12`。

### 4.5 Stage 4 文案 / 停顿 / 运镜（`s4_scriptwriter.py`）

- 新增 `backend/prompts/styles/drama_suspense.md`（只复述既有铁律）与 `action_combat.md`（禁逐格招式播报、连续 3~6 动作格合并成一条 8~18 字短句并把涉及分镜 id 按序写进 `target_panel_ids`、优先在 `tts_text` 配 `[sfx:whoosh]/[sfx:punch]/[sfx:sword_clash]/[sfx:boom_hit]`、短句优先、不改事实）。
- `script_narrate.md` 新增 `{{STYLE_GUIDANCE}}` 占位符，由 `_write_narration_chapter()` 动态注入（分片 / 截断二分两条递归路径都透传）。
- `apply_narrative_pacing(..., style_profile=None)`：规则 A~E 的 `panel_switch / turn / scene_end / breath / same_panel_min` 全部改读 `profile.pacing`；`default_speed_bias` 偏离 1.0 时才改写 `speed_override`。
- `apply_auto_motions(..., style_profile=None)`：`wide_pan_ratio / max_shake_streak / max_dynamic_streak` 改读 `profile.motion`；动作模式下单条 Beat 绑定 ≥3 张分镜时**不叠加强烈 shake**，防眩晕。
- 新增 `async compress_scene_montage()`：把某一幕内连续的动作解说压成**一条多镜蒙太奇 Beat**（`target_panel_ids` 按序去重合并、motion 由 shake 降级为 zoom_in、`pause_after_ms=0`）；文案优先走 LLM 改写（8~18 字，带降级链且**关闭思考模式**），失败退回确定性规则。

### 4.6 繁中前置规范化（`core/normalize.py`）

- `_get_t2s_converter()` 按 `tw2sp → t2s` 顺序惰性加载（台湾用语优先），缺 `tw2sp` 时无缝降级 `t2s`，全部不可用时原样返回。
- **`to_simplified_chinese()` 加护栏**：只有原文**确实含繁体字**时才做转换。`tw2sp` 是「台湾用语→大陆用语」的整句转换，对已经是简体的文本会产生「什么 → 什幺」这类反向劣化 —— 这条护栏是 v4.44 实测踩坑后加的。
- Stage 4 拼装送给 LLM 的视觉摘要与台词上下文前先过 `to_simplified_chinese()`；MiniMax 语气词标签与 `[sfx:key]` 原样透传（ASCII 不受影响）。

---

## 五、后端 API 与前端交互（Part 4）

### 5.1 API

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/style-profiles` | 列出全部预设（id/label/description/recommended_for） |
| GET | `/api/style-profiles/suggest?url_or_genre=` | 按 URL / 类型推荐预设 |
| GET | `/api/series/{series_id}/style-profile` | 读作品当前风格 + 生效后的 profile + 覆盖项 |
| PUT | `/api/series/{series_id}/style-profile` | 切换预设 / 写覆盖项；**未知预设 400** |
| POST | `/api/episodes/{episode_id}/scenes/{scene_id}/compress-montage` | 压缩某一幕的连招蒙太奇；**只动这一幕**，绝不跨幕；有 LLM 走 LLM、无 LLM 走确定性兜底；写回 `04_script/script.json` + `script_readable.md`；**不触发 S5/S6**（配音需从 S5 重跑） |

### 5.2 前端

- `src/App.tsx`：新建作品表单与侧栏作品区各加一个「漫画风格预设」下拉；新建时若用户没手动选过，会按输入的来源 URL 自动推荐并预选。
- `src/components/EpisodeStudioView.tsx`：每个幕分组标题栏右侧加「压缩连招」按钮（Scissors 图标，压缩中转圈），调用 `compress-montage` 后原地替换该幕 Beat 并提示「保存后从 Stage 5 重跑配音生效」。
- `src/api/studioApi.ts` / `src/types/studio.ts`：补 `StyleProfileSummary` / `StyleProfileSpec` / `SeriesStyleProfileResponse` / `CompressMontageResponse` 与五个 API 封装。

---

## 六、测试（Part 5）

新增 `backend/tests/test_patch_v444_style_profiles_and_action_montage.py`（**28 例**），四组：

1. **零回归基线**（8 例）：`drama_suspense` 每个数值 == 4.43 现行常量；未知 / 空预设回落；`apply_narrative_pacing` 不传风格 vs 传基线逐条相等；`apply_auto_motions` 不传风格 vs 传基线的 motion 序列相等；`SeriesConfig()` 解析为基线；基线风格片段不含「连招 / 蒙太奇 / 8~18 字」。
2. **动作模式**（11 例）：`action_combat` 数值核对；停顿 160 vs 400、100 vs 150；宽比 1.15 触发横扫而 1.35 不触发；连续动态上限 6；动作片段含「动作」「合并」；`compress_scene_montage` 合并 3 连动作且跨幕 Beat 不变、文案 8~18 字；基线风格不合并；`find_dark_gutter_gaps` 识别暗底带；`cut_atomic_blocks` 启用后切块数 ≥ 基线；`_has_local_structure` 保护高亮框不被当过渡带；`merge_sfx_fragment_blocks` 并块且不超高度上限。
3. **扩展性与容错**（6 例）：运行时动态注册新预设后各 Stage 直接可用（不改 Stage 主代码）；三层合并顺序正确；URL 推荐 `/action/`、`/martial-arts/` → `action_combat`；`tw2sp` 优先且 `t2s` 降级、`[sfx:heart_thump]` 完好；OpenCC 完全不可用时原样返回。
4. **API 边界**（7 例）：`/api/style-profiles`、`/api/style-profiles/suggest`、作品风格的 GET/PUT、未知预设 400、`compress-montage` 合并 + 跨幕隔离 + 落盘结果 + 无剧本 404 + 未知话次 404。

### 6.1 三个既有测试的同步（均为「加字段 / 加调用点」引起的必要更新）

- `backend/tests/test_schemas.py`：`PanelVisionInfo` 字段锁末尾追加 `"is_action_beat"`。
- `backend/tests/test_patch_v33_voices_and_models.py`：`source.count("**_NO_THINKING_KWARGS,") == 4` 改为 `>= 4` —— 连招蒙太奇改写是第 5 处 LLM 调用，同样必须关思考模式，断言从「恰好 4 处」放宽为「至少 4 处」以免每加一处调用点都要改。
- `core/normalize.py`：加繁中护栏后原 `test_persistent_reverse_is_clamped_after_retries` 恢复通过（此前 `tw2sp` 把「为什么」改成「为什幺」）。

---

## 七、验证结果

| 命令 | 结果 |
| :--- | :--- |
| `cd backend && pytest -n auto --dist loadfile` | **通过**（含 v4.44 专项 28 passed） |
| `cd backend && ruff check src tests` | **All checks passed!** |
| `npx tsc --noEmit` | **零错误** |
| `npm run build` | **✓ built in 189ms** |

真机接口验证（后端已重启加载 v4.44）：

- `GET /api/style-profiles` → 200，返回 `drama_suspense` 与 `action_combat` 两个预设（含中文名、说明、适用场景）。
- `GET /api/style-profiles/suggest?url_or_genre=https://x/action-combat/1` → 200 `{"style_profile":"action_combat"}`。
- `GET /api/series/wt_5081/style-profile` → `style_profile=drama_suspense`、`available=[drama_suspense, action_combat]`、`style_overrides={}`。
- `POST /api/episodes/wt_5081_ep_008/scenes/sc_001/compress-montage` → `changed=false`、`reason=当前风格未启用连招蒙太奇（montage.allow_action_combo=false）`、返回该幕 13 条 Beat；**script.json 校验和未变**（确认未越权改写存量作品）。
- `PUT /api/series/wt_5081/style-profile {"style_profile":"not_exist"}` → **400**。

---

## 八、已知限制

1. `action_combat` 的动作判定依赖 Stage 3 的 `is_action_beat`；**老 `vision.json` 只能靠关键词兜底**，识别率有限 —— 建议动作漫从 S1 重跑识图。
2. 连招蒙太奇的文案优先走 LLM 改写，需要模型可用；无模型时退回确定性规则（取首句裁短到 8~18 字），表达力有限。
3. `compress-montage` 只压当前幕，跨幕的连续动作不会被合并（这是刻意的，避免误伤）。
4. 蒙太奇合并后音频清单不变，**必须从 S5 重跑配音**才生效；只跑 S6/S7 会出现音画不齐。
5. 风格目前只有两套预设；新增第三套只需在 `STYLE_PROFILE_REGISTRY` 加一条 + 一个 `prompts/styles/<id>.md`，不需要改任何 Stage。

---

## 九、后续建议

1. 给动作漫补一个「连招镜头专用运镜」（快速横摇 + 短促定格），当前动作风格仍复用 v4.30 的运镜参数。
2. `target_beat_density_ratio` 目前只作为风格描述保留，尚未参与 Beat 数量预算的实际裁剪，可在后续版本接进 `_write_narration_chapter` 的 `beat_budget`。
3. 动作漫的 `sfx_list` 关键词命中可以与 `ACTION_BEAT_KEYWORDS` 合并成一张表，减少两处维护。
