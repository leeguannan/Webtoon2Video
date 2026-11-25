# PATCH v4.40 —— 句内音效精准落点（`[sfx:key]` 内联标签 + Beat 级 首/中/尾 锚点）

> 基线：`36bfd64`（PATCH v4.38.1 + PATCH v4.39）
> 目标：把「音效」从「整句随便一个时刻响」升级成「**说到哪、响在哪**」。

---

## 一、背景与动机

v4.38 落地了 24 键音效曲库 + 审核台试听选配，但落点一直是**粗锚点**：
一条 beat 挂一个 `sfx_cue`，音效统一落在**这句的开头**（`sfx_position="start"`）。
观众听到的效果是「解说念到第三句时，拳头撞击的声音在第一句就响了」——
音效与画面/台词**对不上**，是廉价感的直接来源。

本补丁给出**两条互补通道**：

| 通道 | 载体 | 落点精度 | 适用 |
| --- | --- | --- | --- |
| **句中内联标签** | 送音文本 `tts_text` 里写 `[sfx:slap]` | **毫秒级**（按子段真实耗时算） | 音效必须卡在某个词上（「他**[sfx:slap]**一拳挥出」） |
| **整句级锚点** | `beat.sfx_position` = `start`/`mid`/`end` | 句级（三档之一） | 没有内联标签时的粗锚点 |

---

## 二、五条铁律

1. **`[sfx:...]` 绝不能送进 TTS 引擎** —— 引擎会把拉丁字母读成拼音（`slap` → 「斯拉普」）。
   标签只能在**切段之后**被剥掉，送进引擎的每一段文本都不含标签。
2. **字幕绝不印出标签** —— `narration_text` 与字幕链路一律剥除 `[sfx:...]`；
   标签只允许出现在送音文本 `tts_text`。
3. **内联音效不进入配音音频** —— 它是时间轴上的**独立事件**（`AudioClipMeta.inline_sfx`），
   由 Stage 6 排到时间轴上、Stage 7 混进成片；配音音频里只有人声 + 静音间隙。
4. **一句只要带了内联音效，就不再重复触发 beat 级 `sfx_cue` 兜底** ——
   否则同一声效会在同一句里响两次。
5. **分段合成失败绝不丢整条配音** —— 自动回退「剥掉全部标签整句合成」，
   落点按字符比例估算，宁可落点不准也不能没配音。

---

## 三、文件白名单

**后端（改 7）**

| 文件 | 改动 |
| --- | --- |
| `backend/src/utils/tts_tags.py` | 新增 `INLINE_SFX_TAG_PATTERN`；纳入切分/遮蔽/剥离三处 |
| `backend/src/core/normalize.py` | 新增 `INLINE_SFX_TAG_RE` / `normalize_inline_sfx_tags()` / `SFX_POSITIONS`；标签豁免长度；字幕净化 |
| `backend/src/core/schemas.py` | 新增 `InlineSfxOffset`；`AudioClipMeta.inline_sfx`；`ScriptBeat.sfx_position` |
| `backend/src/stages/s5_tts.py` | 句内音效合成路径（切段/拼接/落点/缓存） |
| `backend/src/stages/s6_timeline.py` | 内联落点优先 + `sfx_position` 三档偏移 |
| `backend/src/web/server.py` | `PRESERVED_BEAT_FIELDS` 增加 `sfx_position` |
| `backend/prompts/script_narrate.md` | 新增【铁律三之一之二】标签与落点写法 |

**后端（测试，改 2 + 新增 1）**

| 文件 | 改动 |
| --- | --- |
| `backend/tests/test_schemas.py` | 字段锁补 `sfx_position` / `inline_sfx` |
| `backend/tests/test_s5_tts.py` | 音频清单键名补 `inline_sfx` |
| `backend/tests/test_patch_v440_inline_sfx_timing.py` | **新增**：16 例（契约层 6 + S5 6 + S6 3 + API 1） |

**前端（改 3）**

| 文件 | 改动 |
| --- | --- |
| `src/types/studio.ts` | 新增 `SfxPosition`；`ScriptBeat.sfx_position?` |
| `src/components/TtsTagPicker.tsx` | 扩为 3 Tab（新增「句中音效」） |
| `src/components/EpisodeStudioView.tsx` | 音效行加 首/中/尾 切换；传曲库与试听给标签面板 |

改动规模：`+1085 / −19`（不含新增测试与本文档）。

---

## 四、实现明细

### Part 1 · 契约层

**`backend/src/utils/tts_tags.py`**

```python
INLINE_SFX_TAG_PATTERN = re.compile(r"\[\s*sfx\s*:\s*([a-zA-Z0-9_\-]+)\s*\]")
```

* `MINIMAX_TAG_SPLIT_PATTERN` 末尾追加该模式 → `strip_pinyin` / `apply_pronunciation`
  **不会**把标签里的字母当成拼音剥掉或改写；
* `strip_minimax_tts_tags` 追加剥除 → `spoken_length` 天然把标签长度排除；
* `mask_minimax_tts_tags` 追加**等长遮蔽** → `validate_tts_text_no_pinyin`
  扫描时看不见标签（否则 S4 全线报错）。

**`backend/src/core/normalize.py`**

| 函数 | 行为 |
| --- | --- |
| `normalize_inline_sfx_tags(text)` | 别名收敛（`slap_face`→`slap`）→ 白名单校验 → 非法标签**直接剥离** → 规范写法 `[sfx:<key>]` → 相邻重复折叠成一个 |
| `sanitize_narration_and_tts(text, for_tts=…)` | `for_tts=False` 剥除标签（字幕链路）；`for_tts=True` 保留并规范化 |
| `normalize_script_payload(…)` | `sfx_position` 收敛：合法值保留，非法值回落 `"start"` |

> `_MINIMAX_TAG_SPAN_PATTERN` 也追加了标签模式 → `clip_text` / `clip_text_by_spoken_length`
> 不会把标签从中间腰斩。

**`backend/src/core/schemas.py`**

```python
class InlineSfxOffset(BaseModel):
    sfx_key: str
    offset_ms: int = Field(default=0, ge=0)

class AudioClipMeta(BaseModel):
    ...
    inline_sfx: list[InlineSfxOffset] = Field(default_factory=list)

class ScriptBeat(BaseModel):
    ...
    sfx_position: Literal["start", "mid", "end"] = "start"
```

### Part 2 · Stage 5：句内音效合成

`s5_tts.py` 新增常量：
`INLINE_SFX_DEFAULT_GAP_MS = 80`、`INLINE_SFX_TAIL_OFFSET_MS = 120`、
`INLINE_SFX_TARGET_LUFS = -16.0`、`INLINE_SFX_SAMPLE_RATE = 24000`。

**切段**（`split_inline_sfx`）按 `[sfx:key]` 把送音文本切成「文字段 / 音效段」交替序列；
紧贴标签的 `<#x#>` 停顿标签换算成该音效**前 / 后**的静音间隙，没有显式停顿时用 80ms 兜底。

**落点模式**（`_inline_sfx_edge_kind`）：

| 标签位置 | 模式 | 做法 |
| --- | --- | --- |
| 全在正文**之前** | `start` | **不拆整句**：剥标签整句合成一次，落点 `0` |
| 全在正文**之后** | `end` | **不拆整句**：落点 `duration_ms − 120` |
| 落在**句中** | 分段 | 切子段 → 各子段分别合成 → 插静音间隙 → FFmpeg 拼接 → 统一 -16 LUFS / 24kHz |

**句中落点算法**（毫秒级）：

```
offset = Σ(前置文字段真实耗时) + Σ(前置静音间隙时长)
```

即「**紧跟其后的那段台词说完的那一刻**」——音效卡在动作词被念出的瞬间。

**子段失败回退**：任一子段合成/拼接失败 → 剥掉全部标签整句合成，
落点按**字符比例**估算（`_estimate_inline_offsets`），保证不丢配音。

**缓存**：`plan.fingerprint["spoken_text"]` 保留完整标签 → 指纹天然区分有无标签；
`_cache_store(..., inline_sfx=…)` 把落点写进 `_cache/index.json`，
`_cache_lookup` 命中时原样恢复（旧缓存缺该字段按「无内联音效」处理，不影响命中）。
`AUDIO_CACHE_PIPELINE_VERSION` **不 bump**（标签进指纹即天然隔离新旧缓存）。

**标签挪位置 → 指纹变 → 重算**（测试用例覆盖）。

### Part 3 · Stage 6 / 写回层 / prompt

`build_timeline` 收集音效候选时**先读 `audio.inline_sfx`**：

```python
inline_sfx = list(getattr(audio, "inline_sfx", None) or [])
if inline_sfx:
    for item in inline_sfx:
        sfx_candidates.append((int(plan["start"]) + item.offset_ms, item.sfx_key))
    continue          # 这一句不再触发 beat 级 sfx_cue 兜底
```

没有内联标签时才回退到 beat 级：

| `sfx_position` | 偏移 |
| --- | --- |
| `start` | `0` |
| `mid` | `audio_duration_ms // 2` |
| `end` | `max(0, audio_duration_ms − 150)`（`SFX_POSITION_TAIL_OFFSET_MS`） |

候选仍统一走 `select_sfx_events`（排序 + 6000ms 节流 + 8 条上限）——
音效是「点睛」，不是背景。

`web/server.py`：`PRESERVED_BEAT_FIELDS` 增加 `"sfx_position"`
（**不进** `NULLABLE_BEAT_FIELDS`：它有默认值，不允许被 null 清空）。

`prompts/script_narrate.md` 新增【铁律三之一之二】：动作发生在句中/句末时，
可以在 `tts_text` 写 `[sfx:key]`（**严禁**写进 `narration_text`），一条最多 1 个，
标签位置即落点，全话仍守 4~8 处上限。

### Part 4 · 前端

**`TtsTagPicker.tsx`** 扩为 3 Tab：**语气词 / 句中停顿 / 句中音效**。
「句中音效」Tab 按分类分组展示 24 个标准音效的卡片（中文名 + `[sfx:key]` + 试听按钮，
仅 `available` 时显示试听），点击卡片即在 `tts_text` 光标处插入 `[sfx:key]`。
该 Tab **不弹** Speech 2.8 不兼容提醒（内联标签与语气词标签机制不同）；
底部提示：「只作用于配音句内落点、不印字幕，保存后重跑 S5 生效」。

**`EpisodeStudioView.tsx`**：

* 音效行试听按钮旁新增紧凑 **首 / 中 / 尾** 三档切换（未选音效时禁用）；
* `DRAFT_EDITABLE_FIELDS` 增加 `sfx_position`（进脏状态对比）；
* `updateBeatField` 中 `sfx_position` 与 `pause_after_ms` / `sfx_cue` 一样触发
  `needsTimelineRebuild`（**只需重跑 S6→S7，不用重跑配音**）；
* 标签面板挂载处传入 `sfxLibrary` 与试听回调。

---

## 五、数据流

```
S4 写剧本   tts_text = "他[sfx:slap]一拳挥出，鲜血飞溅。"
   │
S5 合成     切成 ["他", sfx:slap, "一拳挥出，鲜血飞溅。"]
             ├─ 段1 合成 600ms ──┐
             ├─ 静音间隙 80ms    │  拼接 → beat_b1.wav（2180ms）
             └─ 段2 合成 1500ms ─┘
             落点 = 600ms → inline_sfx = [{sfx_key: "slap", offset_ms: 600}]
   │
S6 编排     事件时间 = 该句起点 + 600ms
   │
S7 渲染     assets/sfx/slap.mp3 在 600ms 处混进成片
```

---

## 六、测试

`backend/tests/test_patch_v440_inline_sfx_timing.py`（16 例）：

| 组 | 用例 | 断言要点 |
| --- | --- | --- |
| 契约层 | `test_normalize_inline_sfx_tags_canonicalizes_alias` | `slap_face` → `slap` |
| | `test_normalize_inline_sfx_tags_strips_unknown_key` | 非法 key 直接剥离 |
| | `test_normalize_inline_sfx_tags_collapses_adjacent_duplicates` | 相邻重复折叠成一个 |
| | `test_spoken_length_excludes_inline_sfx_tag` | 标签不占发音长度（`5`） |
| | `test_sanitize_narration_strips_tag_but_tts_keeps_it` | 字幕剥除 / 送音保留 |
| | `test_normalize_script_payload_keeps_sfx_position` | `mid` 保留、非法回落 `start` |
| S5（真 ffmpeg） | `test_start_inline_sfx_synthesizes_whole_sentence_once` | 只合成 1 次、落点 `0` |
| | `test_end_inline_sfx_offset_sits_before_the_tail` | 落点 = 末尾前 120ms |
| | `test_mid_inline_sfx_splits_and_computes_exact_offset` | 2 次合成、落点 ≈600ms、总时长 ≈2180ms |
| | `test_inline_sfx_never_reaches_the_tts_engine` | 送进引擎的文本不含 `[sfx:` |
| | `test_inline_sfx_cache_hit_restores_offsets` | 命中缓存 0 次 TTS、落点恢复 |
| | `test_moving_inline_sfx_tag_recomputes_synth_and_offset` | 挪标签 → 重算、落点变化 |
| S6 | `test_timeline_prefers_inline_sfx_offsets` | 事件 = 句起点 + 内联落点 |
| | `test_timeline_skips_beat_sfx_cue_when_inline_sfx_present` | 内联优先、不重复响 |
| | `test_timeline_sfx_position_mid_and_end` | `mid` = 句中；`end` = 末尾前 150ms |
| API | `test_put_script_preserves_sfx_position` | `PUT /script` 后 `sfx_position` 落盘 |

旧测试同步：`test_schemas.py`（字段锁 ×2）、`test_s5_tts.py`（清单键名 +1）。

---

## 七、验证结果

| 命令 | 结果 |
| --- | --- |
| `cd backend && .venv/bin/python -m pytest -n auto --dist loadfile -q` | **EXIT 0**（全量通过） |
| `cd backend && .venv/bin/python -m pytest tests/test_patch_v440_inline_sfx_timing.py -q` | **16 passed** |
| `cd backend && /opt/homebrew/bin/ruff check src tests` | **All checks passed!** |
| `npx tsc --noEmit` | **EXIT 0** |
| `npm run build` | **✓ built in 218ms** |

真机自检（`/tmp/v440_check.py`，假引擎按文本长度生成正弦波）：

* 句中：`client.calls == ['他', '一拳挥出，鲜血飞溅。']`（**都不含标签**），
  `duration_ms == 2180`，`offset_ms == 600`（= 第一段真实耗时）；
* 句首：`offset_ms == 0`；句尾：`offset_ms == 1080`（`1200 − 120`）；
* 缓存：二次调用 **0 次 TTS**，`inline_sfx` 随缓存恢复。

### 真机界面验证（Chromium 1440×860，话次 `wt_5081_ep_008`，97 句解说）

| 检查项 | 实测结果 |
| --- | --- |
| 音效下拉 | **97 个**（`aria-label="b1 音效"` … `"b97 音效"`），每行一个 |
| 下拉分类 | **13 个 optgroup**：生理 / 门 / 脚步 / 击打 / 爆炸 / 碎裂 / 摩擦 / 人群 / 钟表 / 纸页 / 相机 / 交通 / 通讯 |
| 下拉首项 | 「无音效」；后续为中文名 +（兜底时）`·通用兜底` |
| 试听按钮 | **97 个**（`aria-label="bN 试听当前音效"`） |
| 首/中/尾三档 | **291 个**（97 × 3，`aria-label="bN 音效落点首/中/尾"`）；未选音效时 `disabled` 且提示「先给这一句选一个音效」 |
| 选音效后 | 三档立即 `enabled`（title 变「音效落在句首/中/尾」），默认「首」高亮 |
| 切换落点 | 点「中」后 active 由「首」移到「中」（`bg-blue-50 text-blue-700`） |
| 脏状态 | 改动后命令栏出现「1 处未保存」；标签入口文案同步为「+ 语气 / 停顿 / 音效标签」 |
| 标签浮层 | 三个 Tab：**语气词 / 句中停顿 / 句中音效** |
| 句中音效 Tab | **24 张卡片**（中文名 + `[sfx:key]`，如「摔门[sfx:door_slam]」），**不显示** Speech 2.8 不兼容提醒 |
| 底部提示 | 「只作用于配音句内落点、不印字幕，保存后重跑 S5 生效」 |
| 插入标签 | 点「摔门」卡片 → 该句送音文本变为 `[sfx:door_slam]恩彗从昏迷中醒来，一双手正死死掐住她的脖子。` |

截图：`/tmp/v440_sfx_position.jpg`（音效行 + 首/中/尾三档 + 试听按钮）。

---

## 八、已知限制

1. **一条 beat 建议只放 1 个内联标签**：多个标签会切成多段、拼接误差累加，
   落点精度随之下降；prompt 已写明「一条最多 1 个」。
2. **内联音效不参与配音混音**：它是独立事件，在时间轴上叠加；
   若该句本身有 BGM，音效与 BGM 的相对响度仍由 Stage 7 的母带链统一控制。
3. **缓存版本号未 bump**：`inline_sfx` 随缓存索引持久化，
   旧缓存条目缺该字段时按「无内联音效」处理（命中但没有落点），
   改动标签后指纹会变、必然重新合成。
4. **前端改动需整页刷新浏览器**：`EpisodeStudioView.tsx` 有历史遗留的非组件导出
   （`panelThumbUrl`），Vite Fast Refresh 会降级，改动不整页刷新看不到。
5. **落点仍是「整句级」的下限**：内联标签解决的是「精确到词」，
   `sfx_position` 解决的是「粗到句」，两者互不替代。

---

## 九、后续建议

1. 审核台给 `sfx_position` 三档加一个「预览落点」提示（显示预计毫秒）；
2. S7 渲染时把内联音效与 beat 级音效的增益区分开（内联更轻一点）；
3. 若后续要「字幕同步高亮音效词」，需要额外记录标签在 `narration_text` 中的字符位置。
