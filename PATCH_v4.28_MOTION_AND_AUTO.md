# Webtoon2Video 增量补丁规格书（Patch v4.28：扫镜不压扁 + 横向扫镜 + 全自动分配画面动效）

> **目标**：
> 1. **修复 Stage 7 扫镜压扁**：旧链路 `zoompan=z='1.0':x=0:y='(ih-1220)*on/span':s=1020x1220` 会把整张超长竖格**压扁**到 1020×1220，且 `y` 取样坐标越界后被**夹死**，镜头几乎不动。四个平移运镜（`pan_down` / `pan_up` / `pan_left` / `pan_right`）统一改为「等比缩放 + `setsar=1` → 单帧 loop 展开 → **2x 超采样（2040×2440）滑动 crop** → `scale=1020:1220:flags=lanczos,setsar=1`」，画面零压缩、滑动无抖动。
> 2. **新增真正的横向扫镜**：`pan_left` / `pan_right` 全链路打通（枚举、归一化、前端标签与 CSS 预览、Stage 7 渲染）。
> 3. **全自动分配画面动效**：Stage 4 出稿后自动按「分镜几何 + 情绪」分配运镜，审核台新增「自动分配动效」一键按钮，用户**无需手工调动效**；只改运镜，**不动配音**，仅需重跑 S6~S7。
>
> **铁律**：
> - 后端改动只在 `backend/`；前端只动 `src/`；提交 / 推送必须由用户显式下令。
> - 字幕与读音双文本必须分离（`narration_text` 与 `tts_text`），本补丁不触碰该语义。
> - 自动分配动效**只改 `motion` 字段**：`narration_text` / `tts_text` / `pause_after_ms` / `voice_override` / `speaker_role` 等一律原样保留。
> - 音频清单（`05_audio/audio_manifest.json`）与 S5 配音产物**完全不动**，不触发 S5。

---

## 一、文件修改白名单

### 后端（8 个）
1. `backend/src/core/schemas.py` — `CameraMotion` 新增 `PAN_LEFT` / `PAN_RIGHT`。
2. `backend/src/core/normalize.py` — `MOTION_VALUES` 纳入 `"pan_left"` / `"pan_right"`（不进这张表会被归一化回落成 `zoom_in`）。
3. `backend/src/stages/s7_renderer.py` — 平移运镜重写为 2x 超采样滑动 crop；缩放类运镜补 `setsar=1`。
4. `backend/src/stages/s4_scriptwriter.py` — 新增 `MotionAssignReport` / `apply_auto_motions` / `load_episode_panel_metas`；Stage 4 主流程末尾自动分配动效。
5. `backend/src/web/server.py` — 新增 `POST /api/episodes/{episode_id}/auto-motions`。
6. `backend/prompts/script_narrate.md` — 运镜可选值补 `pan_left` / `pan_right`。
7. `backend/tests/test_s7_renderer.py` — 原「长竖格 `pan_down`」断言改为新链路断言。
8. `backend/tests/test_schemas.py` — `CameraMotion` 取值集合补两项。

### 前端（3 个）
9. `src/types/studio.ts` — `CameraMotion` 联合类型补两项；新增 `AutoMotionSummary` / `AutoMotionResponse`。
10. `src/components/EpisodeStudioView.tsx` — 运镜标签与 CSS 预览补横扫；新增「自动分配动效」按钮、结果条、错误条。
11. `src/App.tsx` — 新增 `handleAutoMotions`，透传 `onAutoMotions`。

### 新增（2 个）
12. `backend/tests/test_patch_v428_motion_sweep_and_auto.py` — 12 条用例（S7 运镜 5 / 引擎 5 / API 2）。
13. `PATCH_v4.28_MOTION_AND_AUTO.md`（本文件）。

---

## 二、后端实现

### 1. 运镜枚举与归一化

`backend/src/core/schemas.py`：

```python
class CameraMotion(str, Enum):
    STATIC = "static"
    ZOOM_IN = "zoom_in"
    ZOOM_OUT = "zoom_out"
    PAN_DOWN = "pan_down"
    PAN_UP = "pan_up"
    #: Patch v4.28：真正的横向扫镜（左 / 右横扫），Stage 7 走 2x 超采样滑动 crop。
    PAN_LEFT = "pan_left"
    PAN_RIGHT = "pan_right"
    SHAKE = "shake"
```

`backend/src/core/normalize.py` 的 `MOTION_VALUES` 同步补 `"pan_left"` / `"pan_right"`
——`normalize_script_payload` 用 `normalize_enum(item.get("motion"), MOTION_VALUES, "zoom_in")`
收敛非法值，漏掉就会把新枚举回落成 `zoom_in`。

### 2. Stage 7 平移运镜（`backend/src/stages/s7_renderer.py`）

#### 根因

| 症状 | 原因 |
| --- | --- |
| 长图被压扁 | `zoompan` 用 sws 把输入帧**直接缩放到输出尺寸** `s=1020x1220`，源图 1020×3060 被强行拉成 1020×1220 |
| 镜头几乎不动 | `zoompan` 的 `x` / `y` 是**输入坐标**（`ih` ≫ 输出高），`(ih-1220)*on/span` 越界后被夹死在 0 |
| 抖动 / 锯齿 | 逐帧在缩放后取样，无超采样余量 |

#### 新链路

```
等比缩放（不压扁）+ setsar=1 → format=yuva420p → 单帧 loop 展开 → trim/setpts
→ crop=2040×2440 滑动取景 → scale=1020:1220:flags=lanczos,setsar=1
```

滑动窗口在 **2x 超采样画布（2040×2440）** 上移动，最后一次性 lanczos 缩回安全框
1020×1220，因此零压缩且滑动过程无锯齿。

新增常量（`backend/src/stages/s7_renderer.py:93-113`）：

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `SS_WIDTH` / `SS_HEIGHT` | 2040 / 2440 | 2x 超采样画布（安全框 1020×1220 的 2 倍） |
| `TALL_PAN_SS_MARGIN` | 1.06 | 长竖格判定余量（按宽 2040 缩放后高 > 视窗高 × 该系数） |
| `PAN_MARGIN` | 1.16 | 非长竖格的运镜余量（等比适配后额外放大，留出滑动空间） |
| `PAN_BANNER_RATIO` | 2.8 | 超窄横幅判定阈值（宽高比） |
| `PAN_MAX_BANNER_SCALE` | 2.2 | 超窄横幅放大倍率上限（防发糊） |

新增函数：

- `_even(value)` — 缩放尺寸对齐偶数（`yuva420p` 要求宽高为偶）。
- `_effective_source_size(scene, source_size)` — `focus_band=True` 时按 `focus_region` 折算裁切后的等效宽高，避免把取景带误判成长竖格。
- `_pan_plan(motion, source_size)` — 返回 `(scale 片段, pad 片段, 画布宽, 画布高)`，三种方案：
  1. **长竖格（纵向扫镜）**：按宽 2040 等比缩放（高度必然远超 2440），无 pad；
  2. **宽图（横向扫镜）**：按高 2440 等比缩放（宽度必然 > 2040）；宽高比 > 2.8 的超窄横幅放大倍率封顶 2.2x 并补透明边至 2440 高；
  3. **其余 / 尺寸未知**：等比适配安全框后放大 1.16x，`pad` 补透明边到安全框以上。
- `_pan_filter_chain(fg_label, motion, frames, fps, *, out_label, source_size)` — 拼装完整滤镜链。

滑动表达式（`span = max(1, frames - 1)`，取样序号 `n` 从 0 计）：

| 运镜 | crop 表达式 |
| --- | --- |
| `pan_down` | `crop=w=2040:h=2440:x='0':y='(ih-2440)*n/{span}'` |
| `pan_up` | `crop=w=2040:h=2440:x='0':y='(ih-2440)*(1-n/{span})'` |
| `pan_right` | `crop=w=2040:h=2440:x='(iw-2040)*n/{span}':y='0'` |
| `pan_left` | `crop=w=2040:h=2440:x='(iw-2040)*(1-n/{span})':y='0'` |

缩放类运镜（`zoom_in` / `zoom_out` / `shake` / `static`）的 `scale` 之后统一补 `setsar=1`，
避免非方形像素。`_motion_expressions` 不再返回 `pan_*` 的 zoompan 参数。

#### 真机验证（ffmpeg 9.0.1）

| 用例 | 输出 | 画面内方形标记（应为正方形） |
| --- | --- | --- |
| 长竖格 800×2400 `pan_down` / `pan_up` | 1020×1220 / 30 帧 / 30fps | 511×511（零压缩） |
| 宽图 1800×1000 `pan_left` / `pan_right` | 1020×1220 / 30 帧 / 30fps | 489×489 |
| 窄图 900×1200 `pan_left` | 1020×1220 / 30 帧 / 30fps | 472×472 |
| 横幅 3000×400（2.2x 封顶） | 1020×1220 / 30 帧 / 30fps | 441×330（源比例 400×300，等比） |

帧间位置变化确认镜头确实在滑动（临时验证脚本已在交付前删除）。

### 3. 全自动分配画面动效（`backend/src/stages/s4_scriptwriter.py`）

#### 数据结构

```python
@dataclass(slots=True)
class MotionAssignReport:
    updated_count: int = 0       # 被改动的 beat 数
    tall_pan_count: int = 0      # 竖扫（pan_down / pan_up）
    wide_pan_count: int = 0      # 横扫（pan_left / pan_right）
    shake_count: int = 0         # 震动
    push_pull_count: int = 0     # 推拉（zoom_in / zoom_out）
    static_count: int = 0        # 静止
    changed_beat_ids: list[str] = field(default_factory=list)
```

#### 函数

- `apply_auto_motions(beats, *, panel_metas=None, scenes=None) -> (list[dict], MotionAssignReport)`
  纯函数：入参拷贝后只改 `motion`。
- `load_episode_panel_metas(episode_dir) -> list[PanelMeta]`
  尺寸来源依次兜底：`02_panels/panels_meta.json` → `02_panels/panels.json` → 直接扫描 `02_panels/*.png`
  （Stage 2 的事实来源是 `panels.json`，本补丁按上述顺序保证各种历史产物都能跑）。
- `_index_panel_metas` / `_panel_geometry` / `_beat_emotion` / `_beat_is_impact` — 内部辅助。
- `episode_dir_for(series_dir, episode_no)` — 本话根目录 `episodes/ep_XXX/`。

#### 判定规则（自上而下）

| 规则 | 条件 | 分配 |
| --- | --- | --- |
| **A｜超长竖格** | `is_tall_panel` 或 `height / width >= 1.5` | 该格**首拍** `pan_down`；同一长格连续后续拍改 `zoom_in`（命中冲击且未连震 2 拍则 `shake`） |
| **B｜横向宽格** | `width / height >= 1.25` | `pan_right` / `pan_left`，**连续宽格交替左右横扫**；同一宽格连续后续拍改 `zoom_in` |
| **C｜高燃冲击** | `sfx_cue` 命中冲击词（impact / punch / explosion / slash / crash / boom / smash / thud / strike / 撞击 / 重击 / 爆炸 / 斩 / 轰 / 砰，或标准物理音 `crash` / `heart_thump` / `door_slam` / `whoosh` / `footstep`），**或** 情绪为 `angry` / `shocked` 且去标点后 ≤ 14 字 | `shake`（**最多连续 2 拍**，不得连震 3 拍以上） |
| **D｜定场与防单调** | 新幕首拍（非长 / 宽格）优先 `zoom_out`；`tense` / `whisper` / `sad` 倾向 `zoom_in`；其余默认 `zoom_in` | 连续 2 拍同动效时第 3 拍打散：竖扫改反向、震动改 `zoom_out`、`zoom_out` 改 `static`、其余改 `zoom_out` |

情绪来源：`ScriptBeat` 没有独立 `emotion` 字段，模型只给自由文本 `emotion_instruction`，
因此 `_beat_emotion` 先看 `emotion` 字段（外部直喂的 beat 可能带），再按中英关键词兜底识别。

尺寸缺失时按**普通竖格**兜底（`MOTION_FALLBACK_SIZE = (900, 1200)`，刻意避开长竖格 / 宽格阈值）。

#### Stage 4 接入

主流程在「叙事节奏自动留白」（`apply_narrative_pacing`）之后立即执行：

```python
merged_beats, motion_report = apply_auto_motions(
    merged_beats,
    panel_metas=load_episode_panel_metas(episode_dir_for(series_dir, context.episode_no)),
    scenes=active_scenes if scene_mode else None,
)
```

即：**新生成的话次自动带上高质量动效**，用户开箱即用。

### 4. Web API（`backend/src/web/server.py`）

`POST /api/episodes/{episode_id}/auto-motions`，完全对齐既有 `/script/auto-pacing` 的模式：

1. 定位话次 → 读 `04_script/script.json`（无剧本返回 404）；
2. 读已落盘 `storyboard`；缺失但有 vision 时**在内存里**自动编排（绝不落盘覆盖用户的镜头编排）；
3. 读分镜尺寸 → `apply_auto_motions`；
4. `NarrationScript.model_validate` → `save_model` 写回 `script.json`；
5. 重写 `script_readable.md`；
6. **不碰音频清单、不触发 S5**。

响应：

```json
{
  "beats": [ ... ],
  "summary": {
    "updated_count": 12, "tall_pan_count": 4, "wide_pan_count": 2,
    "shake_count": 3, "push_pull_count": 3, "static_count": 0,
    "changed_beat_ids": ["b1", "b2"], "panel_meta_count": 18
  }
}
```

---

## 三、前端实现

### `src/types/studio.ts`

- `CameraMotion` 补 `'pan_left' | 'pan_right'`。
- 新增 `AutoMotionSummary` / `AutoMotionResponse`（字段与后端 `MotionAssignReport` 一一对应）。

### `src/components/EpisodeStudioView.tsx`

- `MOTION_LABELS` 补 `pan_left: 'PAN_LEFT 左横扫'`、`pan_right: 'PAN_RIGHT 右横扫'`。
- `getMotionStyle` 补横向预览动画（与真实渲染方向一致）：
  - `pan_right`：`scale(1.12) translateX(-16 + 32p)`（画面向右移动）；
  - `pan_left`：`scale(1.12) translateX(16 - 32p)`（画面向左移动）。
- 解说审核工具栏「自动节奏留白」旁新增「自动分配动效」按钮（Wand2 图标）。
  - 草稿未保存（`tableDirty`）时**拦截**并内联提示「请先保存解说稿再执行自动分配动效」；
  - 成功后刷新 `draftBeats`、置 `needsTimelineRebuild`，并展示结果条：

    > 自动分配动效完成：共调整 X 处（横扫 A / 竖扫 B / 震动 C / 推拉 D），重跑 S6~S7 即可生效，无需重跑配音。

    并附「立即从 S6 重跑成片（0 TTS 费用）」按钮；
  - `updated_count == 0` 时提示「现有运镜已符合分配规则，未做调整」；
  - 失败展示红色错误条。

### `src/App.tsx`

新增 `handleAutoMotions`：POST `/episodes/{id}/auto-motions`，成功后 `patchEpisode` 同步本地
`beats`，失败走既有 `reportError`。

---

## 四、测试

### 新增 `backend/tests/test_patch_v428_motion_sweep_and_auto.py`（12 条）

| 组 | 用例 | 断言要点 |
| --- | --- | --- |
| S7 | `test_tall_panel_pan_down_uses_supersampled_crop` | 有 `scale=2040:-2` / `setsar=1` / `crop=w=2040:h=2440` / `y='(ih-2440)*n/29'`；**无** `zoompan=` / `z='1.0'` / `force_original_aspect_ratio=decrease` |
| S7 | `test_tall_panel_pan_up_scrolls_backwards` | `y='(ih-2440)*(1-n/29)'` |
| S7 | `test_wide_panel_uses_horizontal_sliding_crop` | `scale=-2:2440` + `x='(iw-2040)*n/29'`（右）/ `x='(iw-2040)*(1-n/29)'`（左），保留原比例 |
| S7 | `test_narrow_banner_limits_scale_factor` | 超窄横幅有 `2.2` 倍率上限 + `pad` 补边 |
| S7 | `test_zoom_motions_keep_setsar` | `zoom_in` / `zoom_out` / `shake` / `static` 均含 `setsar=1` |
| 引擎 | `test_auto_motions_assigns_by_panel_geometry` | 长竖格首拍 `pan_down`、同格后续 `zoom_in`；宽格 `pan_right` / `pan_left` 交替；`updated_count` / `changed_beat_ids` 准确 |
| 引擎 | `test_auto_motions_assigns_shake_on_impact` | 冲击音效 / 愤怒情绪配 `shake`；不得出现 3 连震 |
| 引擎 | `test_auto_motions_breaks_repeated_motion` | 连续 2 拍同动效时第 3 拍被打散 |
| 引擎 | `test_auto_motions_preserves_other_fields` | 只改 `motion`；`narration_text` / `tts_text` / `pause_after_ms` / `voice_override` 不动；入参不被就地修改 |
| 引擎 | `test_load_episode_panel_metas_falls_back_to_scan` | 无清单时按 `02_panels` 图片尺寸兜底 |
| API | `test_auto_motions_endpoint_writes_script` | 200 + `summary` 统计正确；`script.json` / `script_readable.md` 落盘；`voice_override` / `pause_after_ms` 不变；音频清单不变 |
| API | `test_auto_motions_endpoint_404_without_script` | 无剧本返回 404 |

### 既有测试同步更新

- `backend/tests/test_s7_renderer.py::test_tall_panel_pan_down_scans_inside_safe_window`：
  原断言 `zoompan=z='1.0':x=0:y='(ih-1220)*on/29'`（旧压扁链路）改为新链路断言。
- `backend/tests/test_schemas.py::test_camera_motion_values`：取值集合补 `pan_left` / `pan_right`。
- `backend/tests/test_s4_scriptwriter.py::test_run_stage4_writes_script_and_readable`：
  Stage 4 出稿后自动分配动效，模型给的 `static` 被自动分配结果覆盖（普通竖格兜底 → `zoom_in`）。

---

## 五、验证结果（2026-11-24）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 后端快档 | `cd backend && .venv/bin/python -m pytest -n auto -m "not slow"` | ✅ 全通过 |
| 后端全量 | `cd backend && .venv/bin/python -m pytest -n auto --dist loadfile` | ✅ 全通过（exit 0） |
| 后端 lint | `cd backend && /opt/homebrew/bin/ruff check src tests` | ✅ All checks passed |
| 前端类型 | `npx tsc --noEmit` | ✅ 无错误 |
| 前端构建 | `npm run build` | ✅ built（1675 modules） |
| 真机渲染 | ffmpeg 9.0.1 实跑四类画幅 | ✅ 零压缩、滑动正常、输出 1020×1220 |

---

## 六、已知限制与后续

- `updated_count` 统计的是**被改动的**分句数；模型原本就分配对的镜头不计入。
- 长竖格判定阈值（`height / width >= 1.5`）意味着常规 2:3 竖格也会走竖扫；
  若只想对「超长格」竖扫，需下调 `MOTION_TALL_RATIO`。
- 横向扫镜只在**宽图**（宽高比 > 安全框比 1020/1220）上产生可见位移；
  窄图选横扫时按 1.16x 余量放大后横移，位移幅度较小（这是「零压缩」与「位移幅度」的取舍）。
- 自动分配动效只改 `motion`；`focus_hint`（取景带）仍由模型决定，二者互不覆盖。
