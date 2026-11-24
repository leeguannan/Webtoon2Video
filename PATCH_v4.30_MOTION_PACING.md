# PATCH v4.30 · MOTION PACING —— Stage 7 运镜手感优化

> 版本：v4.30（承接 v4.28 / v4.28.1 / v4.29）
> 日期：2026-11-24
> 主题：让漫画长图的扫镜「慢得下来、接得顺、推得动」

---

## 一、目标与铁律

### 目标

解决四类真实成片里能看出来的运镜问题：

| # | 问题 | 表现 | 本补丁的解法 |
| --- | --- | --- | --- |
| 1 | **长图扫太快** | 1:5 的长竖格 2 秒扫完，镜头像「拉窗帘」 | 舒适扫速封顶（≤260 px/s 等效），宁可扫不完也不硬加速 |
| 2 | **起止生硬** | 第一帧就在动，最后一帧急停 | 起手 / 收尾各留一段定格（0.18s），中间走 Smoothstep 缓动 |
| 3 | **同格连拍割裂** | 同一张长竖格拆成 3 句，第 2 句就缩成中央细竖条 | 长竖格接力竖扫 + Stage 6 写接力区间，镜头一路往下接 |
| 4 | **推拉短句不动** | 短句 zoom 只挪动 0.0008/帧，几乎看不出推拉 | zoom 改为随句长自适应，1.0 ↔ 1.10 完整推到位 |

### 铁律（本补丁全程遵守）

1. **零压缩**：平移链路只用「等比缩放 → crop 滑动 → 缩回」，永不压扁原图（v4.28 铁律延续）。
2. **绝不硬加速**：宁可只扫完全长图的一段，也不为了「扫完全程」把速度拉爆。
3. **不越界**：任何 crop 表达式在 n=0…frames-1 全程都落在合法区间内。
4. **向后兼容**：`TimelineScene` 新增字段一律带默认值；不改字幕/读音双文本分离约定。
5. **Stage 之间不耦合**：Stage 6 只写 `pan_start_ratio/pan_end_ratio` 数值，Stage 7 只读，不互相 import。
6. **前端零改动**：本补丁只动 `backend/`，不碰 `src/`（v4.29 的前端改动另计）。

---

## 二、文件白名单

### 本补丁（v4.30）改动

| 文件 | 改动 |
| --- | --- |
| `backend/src/stages/s7_renderer.py` | 速度封顶常量、Smoothstep 缓动、定格、接力区间消费、zoom 自适应 |
| `backend/src/core/schemas.py` | `TimelineScene` 新增 `pan_start_ratio` / `pan_end_ratio` |
| `backend/src/stages/s6_timeline.py` | `_assign_pan_relay()`：同格多拍接力区间切分 |
| `backend/src/stages/s4_scriptwriter.py` | `apply_auto_motions()`：长竖格连续多拍保留 `pan_down` |
| `backend/tests/test_patch_v430_motion_pacing.py` | **新增**：16 条用例 |
| `backend/tests/test_s7_renderer.py` | 同步旧断言（平移表达式已重写） |
| `backend/tests/test_patch_v428_motion_sweep_and_auto.py` | 同步旧断言 + 自动动效语义变更 |
| `backend/tests/test_schemas.py` | 字段锁新增 `pan_start_ratio` / `pan_end_ratio` |

### 同批未提交（v4.29 遗留，见 `PATCH_v4.29_STORYBOARD_S4_ALIGNMENT.md`）

`backend/src/stages/s4_scriptwriter.py`、`backend/src/web/server.py`、
`src/components/StoryboardEditor.tsx`、`src/components/EpisodeStudioView.tsx`、`src/index.css`、
`backend/tests/test_patch_v429_storyboard_s4_alignment.py`、`PATCH_v4.29_STORYBOARD_S4_ALIGNMENT.md`。

---

## 三、问题定位

### 3.1 长图扫太快（速度无上限）

v4.28 的平移链路是**匀速全行程**：

```
crop=w=2040:h=2440:y='(ih-2440)*n/29'
```

`n` 从 0 线性走到 `span`，意味着**无论画布多高，都要在 `span` 帧内扫完全程**。
1:5 长竖格（800×2400 → 缩放到 2040 宽后高 6120，可动行程 3680 px）配 2 秒（60 帧），
等效速度高达 **3680/2 = 1840 px/s**（安全框等效 920 px/s）——远超「舒服」的 260 px/s。

**结论**：不能靠「把长图裁短」解决，必须给行程设**速度上限**，让镜头只扫它来得及扫的那一段。

### 3.2 起止生硬

线性 `n/span` 的首尾斜率最大 → 第一帧就有最大速度、最后一帧急停，视觉上像「机械滑轨」。
真实摄影机推拉摇移都是**缓入缓出**（ease-in-out），且开始/结束前会有短暂静止。

### 3.3 同格连拍割裂

`apply_auto_motions` 的规则 A 规定：超长竖格的**首拍**给 `pan_down`，
后续同格句子一律 `zoom_in`。于是同一张 1:5 长图：
第 1 句扫长图、第 2 句突然把整条长图缩成中央一根细条 —— 视觉上像「换了一张图」。

### 3.4 推拉短句不动

`_motion_expressions` 用写死的 `min(zoom+0.0008,1.12)`：每帧只涨 0.08%。
30 帧的句子只能走完 `30×0.0008 = 0.024` 的缩放，肉眼几乎看不出推拉；
而且 1.12 这个上限意味着**长句也只推到 1.12**，缓动曲线是折线而非平滑。

---

## 四、后端实现

### 4.1 `s7_renderer.py`：舒适扫速封顶

```python
PAN_MAX_SPEED_Y_SS = 520.0   # 竖扫：安全框等效 ≤ 260 px/s
PAN_MAX_SPEED_X_SS = 600.0   # 横扫：安全框等效 ≤ 300 px/s
PAN_HOLD_SECONDS = 0.18      # 起手 / 收尾定格
```

速度上限以 **2x 超采样画布（2040×2440）** 为基准，换算到最终安全框（1020×1220）正好减半。

### 4.2 `_pan_hold_frames()`：起止定格

```python
hold = max(0, min(int(round(0.18 * fps)), max(0, (frames - 2) // 6)))
```

- 正常时长（≥ 2s）：`hold = 5`（30fps × 0.18s ≈ 5 帧）；
- 极短句：`(frames - 2) // 6` 保证定格**不会吃掉整段画面**（最短句 hold = 0）。

### 4.3 `_pan_axis_expressions()`：行程封顶 + Smoothstep

核心公式（竖扫，横向同理）：

```
full_travel     = ih - 2440                 # 画布可动行程
active_frames   = max(1, frames - 2*hold)   # 有效运动帧数
active_sec      = active_frames / fps
max_travel      = PAN_MAX_SPEED_Y_SS * active_sec     # 速度封顶
travel          = min(full_travel, max_travel)        # 绝不硬加速
progress        = clip((n - hold) / (active_span), 0, 1)
eased           = progress * progress * (3 - 2 * progress)   # Smoothstep
position        = base + travel * eased          # 恒在 [0, full_travel]
```

- `base`：向下/向右扫时为 `0`；向上/向左扫时为 `full_travel`（从末端往回扫）；
  有取景带（`focus_band`）时改为**围绕焦点范围居中**。
- 有接力区间（`pan_start_ratio` / `pan_end_ratio`）时：
  `travel = min((end-start) * full_travel, max_travel)`，`base = start * full_travel`（正向）
  或 `end * full_travel`（反向）—— 镜头只在自己那一段里走。
- `clip(...)` 保证进度 ∈ [0,1]，因此位置**恒不越界**。

### 4.4 `_motion_expressions()`：zoom 随句长自适应

```python
span  = max(1, frames - 1)
eased = smoothstep(clip(on / span, 0, 1))
zoom_in  → "1.0+0.10*(eased)"
zoom_out → "1.10-0.10*(eased)"
```

无论 1 秒还是 5 秒，**都完整推到位**（1.0 ↔ 1.10），且始终是缓入缓出的曲线。
`shake` 保持 `1.12` + sin/cos 抖动不变。

### 4.5 `schemas.py`：`TimelineScene` 新增接力区间

```python
pan_start_ratio: float | None = None   # 接力扫镜：起点占比（0~1）
pan_end_ratio: float | None = None     # 接力扫镜：终点占比（0~1）
```

默认值 `None` = 不参与接力，**老数据与老流水线完全不受影响**。

### 4.6 `s6_timeline.py`：`_assign_pan_relay()`

在 `build_timeline()` 生成 scenes 之后调用：

- 找出「**连续共享同一 `panel_id` 且均为平移运镜**」的相邻场景组；
- 按各句 `duration_ms` 比例把 `[0.0, 1.0]` 切分成接力区间；
- 相邻段留 `0.05` 衔接余量（`pan_start_ratio = max(0, start - 0.025)`、
  `pan_end_ratio = min(1, end + 0.025)`），镜头衔接处不会硬跳；
- 换分镜 / 换成非平移运镜 → 断开，ratio 保持 `None`。

### 4.7 `s4_scriptwriter.py`：长竖格接力保留 `pan_down`

`apply_auto_motions` 的规则 A 改为：

| 情况 | 动效 |
| --- | --- |
| 该长格首拍 | `pan_down` |
| 同格第 2/3 拍且非冲击 | **`pan_down`（接力竖扫）** |
| 命中冲击动作/震撼情绪 | `shake`（最多连续 2 拍） |
| 同格第 4 拍及以后 | `zoom_in`（避免长图被缩成细竖条） |

新增常量 `MOTION_TALL_RELAY_MAX = 3`。防单调规则 D 也相应调整：**接力链不被打断换向**。

---

## 五、前端实现

本补丁**不涉及前端改动**（运镜参数全部由后端与 Stage 4/6 自动产生）。

---

## 六、测试

### 6.1 新增 `backend/tests/test_patch_v430_motion_pacing.py`（16 条）

| 分类 | 用例 |
| --- | --- |
| 速度封顶 | 1:5 长竖格竖扫行程受 `cap × 有效运动时长` 约束；横扫有独立上限；极短句不定格过头 |
| Smoothstep / 定格 | 表达式含 `clip((n-hold)/span,0,1)` 与 `3-2*(...)`；`pan_up` / `pan_left` 反向；取景带时居中 |
| 接力扫镜 | 同格 3 段按 1:2:1 切分（≈0.25 / 0.75）；换格断开；非平移忽略；Stage 7 正确消费 ratio |
| 推拉自适应 | `1.0+0.10*(...)` / `1.10-0.10*(...)`；进度按 `on/span`；不再出现 `0.0008` |
| 真实 ffmpeg | 1:5 长竖格 + 接力区间渲染成功、无 `Invalid too big`、抽帧尺寸 = **1080×1920**；zoom 场景同样渲染成功 |
| 自动动效 | 同格连续 4 句 → 前 3 句接力 `pan_down`、第 4 句不再竖扫；只改 motion，不动文本 / 停顿 / `voice_override` |

### 6.2 同步更新的旧断言

| 文件 | 说明 |
| --- | --- |
| `backend/tests/test_s7_renderer.py` | 平移表达式由 `(ih-2440)*n/29` 改为封顶 + Smoothstep 表达式 |
| `backend/tests/test_patch_v428_motion_sweep_and_auto.py` | 同上；`test_auto_motions_assigns_by_panel_geometry` 因接力语义把 b2 期望改为 `pan_down` |
| `backend/tests/test_schemas.py` | 字段锁新增 `pan_start_ratio` / `pan_end_ratio` |

### 6.3 真机验证（ffmpeg 9.0.1）

用真实 ffmpeg 渲染并反解镜头位移：

| 场景 | 实测位移 | 有效运动时长 | 实测速度 | 上限 |
| --- | --- | --- | --- | --- |
| 800×2400 长竖格 pan_down 2s | 839.7 SS px | 1.667s | **503.8 px/s** | 520 |
| 同上 3s | 1367.4 SS px | 2.667s | **512.8 px/s** | 520 |
| 全程可动行程 | 3748 SS px | — | 封顶行程仅 866.7 / 1386.7 | 证明「不硬加速」 |

- 起手 6 帧位移 = 0 → **定格生效**；
- 逐帧位移单调增大 → **Smoothstep 缓动生效**；
- 接力区间 `[0.25, 0.75]` 实测位移 349.7 SS px（理论 374）→ **接力区间正确生效**；
- `pan_up` 位移为负 → **反向正确**。

> 教训：8-bit 渐变量化（1 级 ≈ 12.5 SS px）会让「逐帧峰值」指标抖动失真，
> 速度评估应以「总位移 ÷ 有效运动时长」为准。

---

## 七、验证结果

| 命令 | 结果 |
| --- | --- |
| `cd backend && .venv/bin/python -m pytest tests/test_patch_v430_motion_pacing.py -q` | **16 passed** |
| `cd backend && .venv/bin/python -m pytest -n auto --dist loadfile` | **EXIT 0（全量通过）** |
| `cd backend && /opt/homebrew/bin/ruff check src tests` | **All checks passed!** |
| `npx tsc --noEmit` | **EXIT 0** |
| `npm run build` | **✓ built** |

---

## 八、已知限制

1. **长图扫不完是刻意的**：1:5 长竖格在 2 秒内只扫约 23% 的可动行程。
   这是「速度封顶」的直接结果；如果整段长图必须全部展示，请把该格拆得更细（更多句）
   或提高 `PAN_MAX_SPEED_Y_SS`。
2. **接力链最多 3 句**：第 4 句起回到 `zoom_in`，避免把超长图缩成中央细条。
3. **接力区间只对同格生效**：换分镜或换成非平移运镜即断开。
4. **加速上限是常量**：暂不支持按句长/情绪差异化调速；如需可另开补丁扩展
   `pan_speed_ratio` 字段。
5. **`zoom` 幅度固定 1.0 ↔ 1.10**：不再像 v4.28 那样最多只走到 1.12，
   视觉上更接近「推近」，但不会推得特别猛。
