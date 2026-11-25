# PATCH v4.41 —— 句内音效与整句级音效**分档增益**（内联更轻）

> 承接 `PATCH_v4.40_SFX_INLINE_TIMING.md` 第九节「后续建议」第 2 条：
> *「S7 渲染时把内联音效与 beat 级音效的增益区分开（内联更轻一点）」。*

---

## 一、背景与动机

v4.40 落地了**句内内联音效**（`tts_text` 里的 `[sfx:key]` 标签），
落点精确到毫秒；但渲染时它和**整句级音效**（`sfx_cue` / 关键词兜底）用的是
**同一档增益**（`render.sfx_gain_db` = `-2.0dB`）。

两条通道的听感条件完全不同：

| 通道 | 落点 | 前后是什么 | 结论 |
| --- | --- | --- | --- |
| 整句级 `sfx_cue` | 句首 / 句中 / 句末（`sfx_position`） | 前后基本是干净空隙 | 可以给足音量 |
| 句内内联 `[sfx:key]` | 解说正文**中间**（说到哪响在哪） | 与这一句的**人声同段共存** | 必须更轻 |

同一档增益下，内联那一声会压住正在念的字，听感就是「听不清词」。

### 顺带修掉的连带缺陷

勘察时发现一个更严重的问题：**内联音效和整句级音效共用同一条队列**
（`select_sfx_events` 的 6000ms 节流 + 8 条上限）。

- 内联落点是 `句起点 + 标签偏移`，比同一句的整句级落点**更晚**；
- 于是内联极易被**前面那条整句级音效**按 6000ms 节流直接挤掉；
- 更糟的是：真实话次的 `timeline.json` 里 8 条 `sfx_events` 已经**占满上限**，
  内联根本挤不进去 → **整条被丢弃，用户在 `tts_text` 里写的标签等于白写**。

只做增益分档而不修队列，等于给一条永远不会被播到的音效调音量。

---

## 二、四条铁律

1. **来源必须显式标记**：任何音效事件都要能回答「这是句内内联，还是整句级兜底」。
   用 `SfxEvent.source` 字段（`"beat"` / `"inline"`）落盘到 `timeline.json`，
   **不用增益哨兵值区分**（隐晦、易错）。
2. **显式指定最高优先**：事件自己写死 `gain_db` 时，一切来源分档让位。
3. **内联不被挤掉**：内联是「作者明确要的点睛」，先用独立的 1200ms 间隔占位，
   整句级再补位（落在内联 2000ms 内就让位）；两者共享「全话 8 条」总额度。
4. **向后兼容**：旧 `timeline.json` 没有 `source` 字段 → 按整句级处理，
   渲染结果与 v4.40 完全一致。

---

## 三、文件白名单

| 文件 | 改动 | 说明 |
| --- | --- | --- |
| `backend/src/core/schemas.py` | +7 | `SfxEvent.source` 字段 |
| `backend/src/core/config.py` | +4 | `RenderConfig.inline_sfx_gain_db` |
| `backend/config.example.yaml` | +3 | render 段同步新配置项 |
| `backend/src/stages/s6_timeline.py` | +94/−17 | 来源标记 + 内联优先队列 |
| `backend/src/stages/s7_renderer.py` | +24 | 三级增益优先 + 参数透传 |
| `src/components/TtsTagPicker.tsx` | +11 | 句中音效 Tab 补充增益提示 |
| `backend/tests/test_patch_v441_inline_sfx_gain.py` | 新增 484 行 | 23 例回归 |

> `backend/config.yaml` 是本机私有文件（`.gitignore` 第 29 行），已同步写入
> `inline_sfx_gain_db: -6.0`，但不入库；入库版本是 `backend/config.example.yaml`。

---

## 四、实现明细

### Part 1 · 数据模型：`SfxEvent.source`

```python
class SfxEvent(BaseModel):
    sfx_key: str
    start_ms: int
    gain_db: float = -10.0
    source: Literal["beat", "inline"] = "beat"   # Patch v4.41 新增
```

- `beat`：整句级 `sfx_cue` / 关键词兜底；
- `inline`：句内 `[sfx:key]` 标签的精确落点。

`schemas.py` 顶部铁律「字段名/类型/默认值不得私自改动，只能扩展新模型或末尾追加」已遵守。
`SfxEvent` **不在** `backend/tests/test_schemas.py` 的 `FIELD_LOCKS` 内 → 不撞字段锁测试。

### Part 2 · Stage 6：来源标记与内联优先

```python
# backend/src/stages/s6_timeline.py
SFX_INLINE_MIN_GAP_MS = 1200     # 两条内联音效之间的最小间隔
SFX_INLINE_CROSS_GAP_MS = 2000   # 整句级音效给内联「让位」的距离
```

`select_sfx_events` 重写为**两轮**：

| 轮次 | 来源 | 间隔规则 | 让位规则 | 产出 |
| --- | --- | --- | --- | --- |
| 第一轮 | `inline` | `>= 1200ms` | 不让位（受保护） | `SfxEvent(source="inline")` |
| 第二轮 | `beat` | `>= 6000ms`（原规则） | 落在任一内联 2000ms 内 → 丢弃 | `SfxEvent(source="beat")` |

两轮**共享 `max_events=8` 总额度**，输出按 `(start_ms, sfx_key)` 升序。

候选可以是 `(start_ms, key)`（历史二元组，等价 `source="beat"`）或
`(start_ms, key, source)`；`build_timeline` 现在分别产出
`(start, key, "inline")` 与 `(start, key, "beat")`。

**兼容性**：纯整句级输入时，两轮算法退化成原来的单队列贪心，
`test_patch_v31_audio.py` / `test_patch_v32_audio_mix.py` 的节流与上限断言**零修改全绿**。

### Part 3 · Stage 7：三级增益优先

```python
DEFAULT_SFX_GAIN_DB = -2.0          # 整句级
DEFAULT_INLINE_SFX_GAIN_DB = -6.0   # 句内内联（新增）
```

```python
gain = sfx_gain_db                                   # ① 整句级默认档
if event.gain_db is not None and event.gain_db != schema 默认(-10.0):
    gain = event.gain_db                             # ② 事件显式指定（最高优先）
elif event.source == "inline":
    gain = inline_sfx_gain_db                        # ③ 内联档（更轻）
```

参数透传链：
`render.inline_sfx_gain_db`（配置）→ `render_episode` → `render_timeline` → `_prepare_audio`
→ 音效滤镜链 `volume={calibration}dB,volume={gain}dB,`。

混音日志追加内联条数与降档值，一眼可查：

```
[s7] 音频混合：bgm=NONE (-8.0dB), sfx=1/1 条（其中内联 1 条按 -6.0dB 降档）
```

### Part 4 · 配置

```yaml
# backend/config.example.yaml（backend/config.yaml 同步）
render:
  sfx_gain_db: -2.0
  inline_sfx_gain_db: -6.0   # Patch v4.41：句中内联音效单独一档、更轻
```

`RenderConfig` 是 `extra="forbid"`，新增键必须同步 yaml，否则启动直接报错。

### Part 5 · 前端

`src/components/TtsTagPicker.tsx` 的「句中音效」Tab 底部追加一行提示：

> 句中音效比整句音效轻一档（默认 -6dB），避免压住正在念的词

> 前端不消费 `sfx_events`（`src/` 内无 `SfxEvent` 引用）→ 无需改类型。
> 注意：`EpisodeStudioView.tsx` 有历史遗留非组件导出 `panelThumbUrl`，Vite Fast Refresh 会降级，
> **该文件任何改动都必须整页刷新浏览器才生效**；本次未改该文件。

---

## 五、测试

新增 `backend/tests/test_patch_v441_inline_sfx_gain.py`，**23 例**：

| 组 | 用例 | 断言要点 |
| --- | --- | --- |
| 数据模型 | 3 | `source` 默认 `"beat"`；可显式 `"inline"`；旧 JSON 无该字段按 beat |
| Stage 6 队列 | 7 | 纯 beat 复现 v4.31 节流与 8 条上限；二元组默认 beat；空 key 跳过；**内联在队列满时仍存活**；内联用 1200ms 间隔；整句级给内联让位；输出按时间升序 |
| Stage 6 标记 | 2 | `build_timeline` 给内联事件打 `inline`、给整句级打 `beat` |
| Stage 7 增益 | 6 | 内联 → -6.0dB；整句级 → -2.0dB；显式 `gain_db` 最高优先；渲染参数可覆盖；`render_timeline`/`_prepare_audio` 都带参数；配置接线 |
| 配置 | 3 | `RenderConfig.inline_sfx_gain_db` 默认 -6.0 且轻于整句级；`config.yaml` 有该键；两个常量进 `__all__` |

关键回归用例「内联在队列满时仍存活」：

```python
beats = [(index * 6000, f"key_{index}") for index in range(8)]
events = select_sfx_events([*beats, (3000, "slap", "inline")])
assert len(events) == 8                                  # 共享总额度
assert [e.sfx_key for e in events if e.source == "inline"] == ["slap"]   # 内联没被挤掉
```

旧实现下这条断言会失败——内联会被前一条整句级按 6000ms 节流吃掉。

---

## 六、验证结果

| 命令 | 结果 |
| --- | --- |
| `cd backend && .venv/bin/python -m pytest tests/test_patch_v441_inline_sfx_gain.py -q` | **23 passed** |
| `cd backend && .venv/bin/python -m pytest -n auto --dist loadfile -q` | **EXIT 0**（全量通过） |
| `cd backend && /opt/homebrew/bin/ruff check src tests` | **All checks passed!** |
| `npx tsc --noEmit` | **EXIT 0** |
| `npm run build` | **✓ built in 202ms** |

既有断言零修改全绿：`test_patch_v31_audio.py`、`test_patch_v32_audio_mix.py`、
`test_patch_v440_inline_sfx_timing.py`、`test_s4_scriptwriter.py`（旧 JSON 兼容）。

---

## 七、已知限制

1. **真实话次里还没有内联音效**：v4.40 刚交付，全库 `audio_manifest.json` 里
   `inline_sfx` 命中数为 **0**，所以本补丁的增益分档**只能靠测试覆盖**，
   无法用现成话次做端到端听感验证；需要等有话次真正用上 `[sfx:key]` 标签。
2. **内联档是全局配置**：`render.inline_sfx_gain_db` 对整部作品生效，
   不能按话次/按句单独调。
3. **让位距离是固定值**：`SFX_INLINE_CROSS_GAP_MS = 2000` 为常量，
   「多近算太近」没有做自适应（长句与短句的语速差异未纳入）。
4. **总量上限仍是 8 条**：内联与整句级共享。内联占位多时整句级会变少——
   这是「全话 4~8 处音效」铁律的直接结果，不是 bug，但用户可能预期是「各 8 条」。
5. **`gain_db` 哨兵值的老问题仍在**：`SfxEvent.gain_db` 默认 `-10.0` 被当作
   「未显式指定」，只有写别的值才算显式。本次未改这个约定（属历史兼容面）。

---

## 八、后续建议

1. **做一次带内联音效的真机端到端渲染**，验证 -6dB 档在成片里的听感是否合适
   （需要先在某个话次的 `tts_text` 里写入 `[sfx:key]` 标签并重跑 S5~S7）。
2. **让内联档可按句覆盖**：给 `ScriptBeat` 加一个可选的 `sfx_gain_db`，
   让「这一句的音效我想大声点」也能表达。
3. **考虑把让位距离做成可配置项**，与 `SFX_INLINE_MIN_GAP_MS` 一起放进 `render` 段。
