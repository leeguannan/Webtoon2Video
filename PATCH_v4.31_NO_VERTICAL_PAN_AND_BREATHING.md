# PATCH v4.31 — 取消竖扫 + 「同画防跳变 / 呼吸节奏」自动动效

- 版本：v4.31
- 日期：2026-11-24
- 基线：HEAD `d8ccf27`（PATCH_v4.29 + v4.30）
- 一句话：**自动分配不再产出竖扫，并把运镜节奏从「句句有动作」改成「同画面第 1 动第 2 静、跨画面张弛有度」。**

---

## 一、目标与铁律

### 1.1 要解决的问题

| 现象 | 根因 |
| --- | --- |
| 漫画长图被「竖扫」甩得看不清，节奏很赶 | 自动分配把超长竖格一律判成 `pan_down`（v4.30 又加了接力竖扫），长图被裁着硬扫 |
| 同一张图连着两句都在动，画面像回弹跳帧 | 自动分配不区分「同图连拍」，两句可能拿到不同动效，切换时画面跳 |
| 整条片子句句有动作，看久了发累 | 没有呼吸机制，连续动态没有上限 |
| 推拉推得太满，和静帧对比生硬 | 推拉幅度固定 1.10（v4.30 自适应后暴露出来） |
| 横扫接力偶尔「跳回开头」 | 接力区间只按分镜分组，没有区分平移方向 |

### 1.2 铁律

1. **自动链路彻底取消竖扫**：`apply_auto_motions` 不再产出 `pan_down` / `pan_up`，`report.tall_pan_count` 恒为 0；老剧本里的竖扫在重新自动分配时会被替换成推拉 / 静止 / 横扫。
2. **底层能力保留**：`core/schemas.py` 的枚举、`core/normalize.py` 的 `MOTION_VALUES`、`s7_renderer.py` 的竖扫渲染**一律保留**，老工程 / 老剧本 / 手工指定竖扫不受影响。
3. **同画防跳变**：同一张分镜连拍时，「第 1 拍动、第 2 拍静」交替；同图第 3 拍在上一拍已静的前提下补一次不同向轻动。
4. **张弛有度**：连续动态最多 3 拍，第 4 拍强制插静止呼吸；禁止连震。
5. **只改 motion**：文本（`narration_text` / `tts_text`）、停顿（`pause_after_ms`）、音色（`voice_override`）一律不动。

---

## 二、文件白名单

### 修改（5 个）

| 文件 | 改动 |
| --- | --- |
| `backend/src/stages/s4_scriptwriter.py` | `apply_auto_motions` 重构为状态机；常量调整；新增 `_beat_core_text` / `_beat_is_guide_short` |
| `backend/src/stages/s7_renderer.py` | 推拉幅度 1.10 → 1.08 |
| `backend/src/stages/s6_timeline.py` | 接力只对同方向成组；反向运镜倒序切段 |
| `backend/prompts/script_narrate.md` | 运镜清单移除 `pan_down` / `pan_up` |
| `src/components/EpisodeStudioView.tsx` | 运镜下拉只展示推荐的 6 种；结果条改为「横扫 / 推拉 / 静止 / 震动」 |

### 新增（2 个）

| 文件 | 说明 |
| --- | --- |
| `backend/tests/test_patch_v431_no_vertical_pan_and_breathing.py` | v4.31 回归测试（17 例） |
| `PATCH_v4.31_NO_VERTICAL_PAN_AND_BREATHING.md` | 本文档 |

### 同步更新的既有测试（3 个）

| 文件 | 原因 |
| --- | --- |
| `backend/tests/test_patch_v428_motion_sweep_and_auto.py` | 自动动效断言按 v4.31 新语义重写（取消竖扫 / 同图强制静止） |
| `backend/tests/test_patch_v430_motion_pacing.py` | 推拉幅度断言 1.10 → 1.08；接力竖扫用例改为「同图动-静交替」 |
| `backend/tests/test_s7_renderer.py` | 竖扫渲染底层保留，断言不变（全量复跑通过） |

---

## 三、后端实现

### 3.1 `s4_scriptwriter.py` —— 自动动效状态机

#### 常量

| 常量 | v4.30 | v4.31 | 说明 |
| --- | --- | --- | --- |
| `MOTION_WIDE_RATIO` | 1.25 | **1.35** | 宽格门槛提高，只有真正的横幅才横扫 |
| `MOTION_MAX_SHAKE_STREAK` | 2 | **1** | 禁止连震 |
| `MOTION_MAX_DYNAMIC_STREAK` | — | **3** | 连续动态上限，第 4 拍必须呼吸 |
| `MOTION_GUIDE_SHORT_MAX` | — | **10** | 引导短句净字数上限 |
| `MOTION_GUIDE_PAUSE_MAX` | — | **80** | 引导短句停顿上限（ms） |
| `MOTION_CALM_EMOTIONS` | — | `{calm, happy}` | 情绪平稳 → 倾向静止 |
| `MOTION_TALL_RELAY_MAX` | 3 | **删除** | 竖扫下线，接力上限无意义 |

`MOTION_TALL_RATIO`（1.5）保留，仍用于「竖格 / 宽格」的形态区分。

#### 状态变量

逐拍维护：`same_panel_run`（同分镜连续拍数）、`motion_streak`（连续非 static 拍数，遇 static 清零）、`static_streak`（连续 static 拍数）、`prev_motion` / `prev_prev_motion`、`shake_streak`、`wide_flip`。

#### 判定顺序（自上而下，命中即止）

| # | 规则 | 条件 | 结果 |
| --- | --- | --- | --- |
| A1 | 同图防跳变 | 同图 且 上一拍非 static | `static`（除非命中冲击且允许震动 → `shake`） |
| B | 冲击高燃 | impact 且 上一拍不是 shake 且 未连震 | `shake` |
| A2 | 同图补轻动 | 同图第 ≥3 拍 且 上一拍已静 | `zoom_out` / `zoom_in`（与上一次动不同向） |
| B2 | 震后泄力 | 上一拍是 shake | 同图 → `static`，否则 `zoom_out` |
| C | 横向宽格 | 宽高比 ≥ 1.35 且是新分镜 | `pan_right` / `pan_left` 交替 |
| D1 | 天然静帧 | 非冲击 且（引导短句 或 情绪平稳且非新幕开篇） | `static` |
| D2 | 呼吸上限 | 连续动态 ≥ 3 且 非冲击非宽格 | `static` |
| D3 | 防死屏 | 连续 static ≥ 3 且 切入新分镜 | `zoom_out` |
| E1 | 新幕首拍 | 新幕开篇 | `zoom_out`（刚拉远过则 `zoom_in`） |
| E2 | 推进情绪 | 情绪 ∈ {tense, whisper, sad} | `zoom_in` |
| E3 | 兜底 | 其余 | `zoom_in` |
| E4 | 同向换向 | 本拍与上一拍同为推拉，且上两拍同动效 | `zoom_in` ⇄ `zoom_out` 换向 |

**引导短句**判定：净字数（去掉标点与空白）≤ 10，且（`pause_after_ms` ≤ 80 或 以 `:` / `：` 结尾）。

**impact** 判定不变：`sfx_cue` 命中冲击动作词，或愤怒 / 震惊情绪且句子净字数 ≤ 14。

### 3.2 `s7_renderer.py` —— 推拉幅度收敛

`_motion_expressions` 的自适应推拉幅度由 **1.10 → 1.08**：

- `zoom_in`：`1.0 + 0.08 * smoothstep(clip(on/span, 0, 1))`
- `zoom_out`：`1.08 - 0.08 * smoothstep(clip(on/span, 0, 1))`

推得太满会和静帧形成生硬对比，1.0 ↔ 1.08 更自然耐看。Smoothstep 缓动与随句长自适应的行为保持 v4.30 不变。

### 3.3 `s6_timeline.py` —— 接力分组收紧

`_assign_pan_relay` 的成组条件由「同 panel_id」收紧为「**同 panel_id 且同平移方向**」：

- `pan_right` 接 `pan_right` → 继续接力（正序 0 → 1 切段）；
- `pan_right` 接 `pan_left` → **断开**，各自从整段开始（这是折返，不是接力）；
- 反向运镜（`pan_left` / `pan_up`）按 `1.0 → 0.0` **倒序**切段，避免切句时镜头跳回开头。

---

## 四、前端实现

### 4.1 运镜下拉（`EpisodeStudioView.tsx`）

新增 `RECOMMENDED_MOTIONS` 与 `motionOptions()`：

```ts
const RECOMMENDED_MOTIONS: CameraMotion[] = [
  'static', 'zoom_in', 'zoom_out', 'pan_left', 'pan_right', 'shake',
];
```

- 审核表运镜下拉**默认只展示推荐的 6 种**；
- 当某句当前值恰好是已下线的旧版 `pan_down` / `pan_up` 时，**临时把该值保留在列表最前**，避免 `<select>` 找不到匹配项而显示空白；
- `MOTION_LABELS` 本身保留全部 8 个枚举（老剧本的竖扫仍要有可读标签）。

### 4.2 自动分配结果条

文案由「横扫 / 竖扫 / 震动 / 推拉」改为「**横扫 / 推拉 / 静止 / 震动**」，用 `static_count` 顶替原来的「竖扫」位。

---

## 五、测试

### 5.1 新增 `backend/tests/test_patch_v431_no_vertical_pan_and_breathing.py`（17 例）

| 组 | 用例 | 断言要点 |
| --- | --- | --- |
| 取消竖扫 | `test_tall_panel_never_gets_vertical_pan` | 长竖格 4 拍无 `pan_down` / `pan_up`，`tall_pan_count == 0` |
| | `test_legacy_vertical_pan_beats_are_replaced` | 老剧本的 `pan_down` / `pan_up` 被替换 |
| 同画防跳变 | `test_same_panel_second_beat_is_static` | 第 1 拍动、第 2 拍静 |
| | `test_same_panel_four_beats_alternate` | 动-静-动-静，且第 3 拍与第 1 拍不同向 |
| 呼吸节奏 | `test_dynamic_streak_limit_inserts_breath` | 第 4 拍强制静止，之后重新起动 |
| | `test_static_streak_avoids_dead_screen` | 连续静止后切入新图补推拉 |
| | `test_guide_short_sentence_becomes_static` | 引导短句静止 |
| | `test_shake_never_consecutive` | 任意相邻两拍不同时为 `shake` |
| | `test_shake_followed_by_release` | 震后下一拍泄力 |
| 横向宽格 | `test_wide_panel_gets_horizontal_sweep` | 宽格左右交替横扫 |
| | `test_narrow_wide_ratio_is_not_swept` | 1.3 比例不算宽格 |
| Stage 7 | `test_zoom_amplitude_tightened_to_1_08` | `1.0+0.08*(` / `1.08-0.08*(` |
| | `test_zoom_fragment_uses_1_08` | fragment 内无 `0.10*(` |
| Stage 6 | `test_pan_relay_groups_same_direction_only` | 同向成组、反向断开 |
| | `test_reverse_pan_relay_is_descending` | 反向倒序切段 |
| | `test_pan_relay_breaks_on_panel_change` | 换分镜断开 |
| | `test_pan_relay_ignores_non_pan_motion` | 非平移不接力 |

### 5.2 既有测试同步更新

| 用例 | 旧断言 | 新断言 |
| --- | --- | --- |
| `v428::test_auto_motions_assigns_by_panel_geometry` | `b1/b2 == pan_down` | `b1 == zoom_in`、`b2 == static`；`tall_pan_count == 0`；`updated_count == 3` |
| `v428::test_auto_motions_assigns_shake_on_impact` | `motions[1] == shake` | `motions[1] == static`（震后泄力）；改为断言「无相邻连震」 |
| `v428::test_auto_motions_preserves_other_fields` | `motion == pan_down` | `motion == zoom_in`；`changed_beat_ids == []` |
| `v428::test_auto_motions_endpoint_writes_script` | `b1 == pan_down` | `b1 == zoom_in`；`tall_pan_count == 0`；readable 含 `ZOOM_IN` |
| `v430::test_zoom_motions_adapt_to_duration` | `1.0+0.10*(` | `1.0+0.08*(` / `1.08-0.08*(` |
| `v430::test_zoom_motion_fragment_has_adaptive_expression` | `zoompan=z='1.0+0.10*(` | `zoompan=z='1.0+0.08*(`，并断言无 `0.10*(` |
| `v430::test_auto_motions_keeps_pan_down_for_relay_beats` | 3 连 `pan_down` | 改名为「取消竖扫 → 同图动-静交替」，断言无竖扫 + 动-静-动-静 |
| `v430::test_auto_motions_relay_keeps_panel_content_untouched` | `["pan_down", "pan_down"]` | `["zoom_in", "static"]` |

---

## 六、验证结果

| 命令 | 结果 |
| --- | --- |
| `cd backend && .venv/bin/python -m pytest tests/test_patch_v431_no_vertical_pan_and_breathing.py -q` | **17 passed** |
| `cd backend && .venv/bin/python -m pytest tests/test_patch_v428_motion_sweep_and_auto.py tests/test_patch_v430_motion_pacing.py tests/test_s7_renderer.py -q` | **55 passed** |
| `cd backend && .venv/bin/python -m pytest -n auto --dist loadfile` | **1080 passed in 24.58s**（EXIT 0） |
| `cd backend && /opt/homebrew/bin/ruff check src tests` | **All checks passed!**（EXIT 0） |
| `npx tsc --noEmit` | **EXIT 0** |
| `npm run build` | **✓ built in 222ms**（EXIT 0） |

---

## 七、已知限制

1. **竖扫仍可用于手工指定**：底层枚举与渲染保留，老工程 / 老剧本 / 审核台手工选竖扫照常工作；只是「自动分配」不再推荐和产出。
2. **老剧本不会自动被清洗**：只有重新跑一次「自动分配动效」时，旧的 `pan_down` / `pan_up` 才会被替换；未点该按钮的剧本保持原样。
3. **情绪识别依赖文本关键词**：`emotion_instruction` / `sfx_cue` 缺失时，情绪按兜底处理（既不判 calm 也不判 tense），节奏会偏「常规推拉」。
4. **`static_count` 统计口径**：只统计「本次被改动且结果为静止」的拍数，与 `updated_count` 同口径。
5. **同图第 3 拍的轻动方向**：按「与上一次动不同向」选取（`zoom_out` / `zoom_in` 交替），不是按内容语义挑选。
