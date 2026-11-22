# Webtoon2Video 增量补丁规格书（Patch v4.12 / v4.13 / v4.14：单句重配 TTS + 幕与分句解耦 + 全链路简体 + MiniMax 语气词与停顿标签）

> **基线提交**：`80b9dac`（v4.11 之后、提速工程化之后）
> **落地提交**：
> - `56a9f2a` `feat(tts): 新增单句重配语音与真实试听，可直接从 Stage 6 重跑成片`（v4.12，7 文件 +1264/-2）
> - `8617fe4` `feat(script): 新增原漫外链、幕与分句画面解耦、单句语速与全链路简体`（v4.13 + v4.14，19 文件 +2452/-103）
> - `dce6716` `fix(script): 修正停顿标签越界清理顺序，补句中停顿插入与归属幕指定`（收尾修复，3 文件 +93/-23）
> - `cb9adc2` `fix(script): 评审修复归属幕节奏留白、幕内换序守卫与停顿标签残留`（评审修复，7 文件 +207/-32）
>
> **目标**：
> 1. **v4.12 单句重配 TTS**：改一句台词 / 语气 / 音色 / 语速后，只重配这一句的语音，**不重跑整话 Stage 5**；审核台能直接试听该句的**真实配音**（不是提示音），并可「重配完直接从 Stage 6 重跑出新成片」。
> 2. **v4.13 幕与分句画面解耦**：镜头编排（幕）不再靠 `target_panel_ids` 反推——剧本显式记录 `beat.scene_id`；同一格漫画可以被多句解说复用；新增「查看原漫」外链、单句语速 `speed_override`、全链路强制简体中文。
> 3. **v4.14 MiniMax 语气词与停顿标签**：送音文本支持 19 个官方语气词标签与 `<#x#>` 句中停顿标签，模型侧按规范产出、后端侧规范化与保护、前端侧可视化插入。
>
> **铁律**：
> - 严格遵守文件修改白名单，禁止全局重构；不传新参数时既有链路行为 **100% 一致**。
> - 字幕与送音文本**必须分离**：`narration_text`（屏幕字幕）与 `tts_text`（送入 TTS）不得合并；语气词/停顿标签**只允许写在 `tts_text`**。
> - 前端禁用 `window.alert` / `window.confirm`，全部反馈走内联状态与轻提示。
> - 标签**绝不腰斩**：`<#0.5#>` / `(laughs)` 这类标签在长度裁剪、拼音剥除、繁简转换中必须整体保留。
> - 提交 / 推送必须由用户显式下令。

---

## 一、文件修改白名单

### 后端（10 个）
1. `backend/src/stages/s5_tts.py` — 新增 `resynthesize_single_beat()`；送音文本构造（`build_spoken_text`）接入简体与标签；发音词典替换按标签切段；模型能力提示。
2. `backend/src/stages/s4_scriptwriter.py` — 按幕写 `scene_id`；钳制 `speed_override`；拼音校验/剥除保护标签；跨幕留白改按 `scene_id` 判定。
3. `backend/src/stages/s3_vision.py` — 视觉抽取结果落盘前转简体。
4. `backend/src/stages/s7_renderer.py` — BGM 归属幕改按 `beat.scene_id` 优先。
5. `backend/src/core/normalize.py` — 繁简转换、标签剥离、可发音长度、语速钳位、字幕/送音双文本净化链路。
6. `backend/src/core/schemas.py` — `ScriptBeat` 新增 `scene_id`。
7. `backend/src/utils/tts_tags.py`（**新建**）— MiniMax 标签的唯一权威定义与规范化。
8. `backend/src/web/server.py` — 单句配音回源、单句重配接口、原漫链接透传、字段合并与简体兜底。
9. `backend/src/web/job_runner.py` — `_make_tts_client` 改为公开 `make_tts_client()` 供 server 复用。
10. `backend/pyproject.toml` — 新增 `opencc-python-reimplemented`（繁简转换）。

### 前端与文档（7 个）
11. `src/types/studio.ts` — `ScriptBeat.scene_id`、`AudioClipMetaItem`、`BeatRegenerateTTSResponse`。
12. `src/App.tsx` — `handleRegenerateBeatTts`；只替换被重配那一句的基准状态。
13. `src/components/EpisodeStudioView.tsx` — 真实试听、单句重配（含居中确认弹窗）、归属幕下拉、行内语速微调、标签浮层、「查看原漫」入口。
14. `src/components/StoryboardEditor.tsx` — 幕内添加 / 移除 / 换序分镜（与后端顺序校验对齐）。
15. `src/components/TtsTagPicker.tsx`（**新建**）— 语气词 / 停顿标签选择浮层。
16. `src/constants/ttsTags.ts`（**新建**）— 标签常量与格式化（与后端 `tts_tags.py` 对齐）。
17. `PATCH_v4.12_SINGLE_BEAT_AND_VOICE_LABELS.md`（本文件）。

### 测试（4 个）
18. `backend/tests/test_patch_v412_single_beat_tts.py`（**新建**，18 项）— 单句重配与两个 HTTP 接口。
19. `backend/tests/test_patch_v413_v414.py`（**新建**，44 项）— `scene_id` / 简体 / 语速 / 标签 / 原漫链接。
20. `backend/tests/test_patch_v46_auto_pacing.py` — 新增 1 项跨幕留白优先级用例。
21. `backend/tests/test_schemas.py` — `FIELD_LOCKS` 追加 `scene_id`（字段锁白名单）。

### 严禁修改
`backend/src/stages/s6_timeline.py`、`backend/src/core/subtitle_render.py`、`backend/src/providers/tts_minimax.py`、`backend/src/providers/tts_bailian.py`、`backend/src/web/job_runner.py` 的调度与并发逻辑（仅允许把 `_make_tts_client` 改名为 `make_tts_client`）。

---

## 二、Patch v4.12：单句重配 TTS + 真实试听 + 直接从 S6 重跑

### 1. `backend/src/stages/s5_tts.py` — 新增 `resynthesize_single_beat()`

```python
async def resynthesize_single_beat(
    client: Any,
    series_dir: str | Path,
    episode_no: int,
    beat_id: str,
    *,
    overrides: Mapping[str, str] | None = None,
    voice: str | None = None,
    default_voice: str | None = None,
    voice_cast: Mapping[str, str] | None = None,
    series_tts: SeriesTTSConfig | None = None,
    characters: Sequence[CharacterProfile] | None = None,
    ffmpeg_bin: str = "ffmpeg",
    trim_silence: bool = True,
    tts_model: str | None = None,
) -> AudioClipMeta:
```

行为规范（与整话 `synthesize_episode` 的**关键差异**）：

| 维度 | 整话 `synthesize_episode` | 单句 `resynthesize_single_beat` |
| --- | --- | --- |
| 清理旧音频 | 调 `clean_stale_audio()` | **绝不调用**，不删任何其它 beat 的 WAV |
| TTS 调用次数 | 每句一次 | **只调一次**（仅目标 beat） |
| 清单写入 | 全量重写 | **原位替换**目标条目；原本缺该句时按 `script.beats` 顺序插回 |
| 说话人继承 | 整话计算 | 仍按整话剧本算（`resolve_dialogue_speaker_inheritance`），保证继承前句声线 |
| 问题清单 | 全量重写 | 只移除该 beat 的旧记录、追加新记录；无剩余问题则删除 `audio_issues.json` |

- 缺 `04_script/script.json` → `TTSStageError("缺少 Stage 4 剧本文件：…")`；剧本里没有该 `beat_id` → `TTSStageError("剧本里找不到 beat：…")`。
- 缺 `05_audio/audio_manifest.json` → `TTSStageError("该话还没跑过 Stage 5，缺少音频清单：…，请先运行 Stage 5 配音")`。
- 静音裁剪 / `-16 LUFS` 响度归一 / `-40dB` 尾端保留 / 时长探测与主链路**完全一致**（同一个 `_synthesize_beat`）。
- 仅当新旧 `audio_path` **文件名不同**时，才删除该 beat 自己的旧文件（`OSError` 只 warning，不阻断）。
- 清单写回格式：`json.dumps([item.model_dump(mode="json") for item in metas], ensure_ascii=False, indent=2)`。
- 导出到 `__all__`。

### 2. `backend/src/web/job_runner.py` — 暴露 TTS 客户端工厂

`_make_tts_client` 改名为公开的 `make_tts_client()`（docstring 注明「测试可经 `tts_factory` 注入假合成器」），让 web 层能构造与 `JobRunner._run_stage("s5_tts")` **完全同源**的客户端（同样的模型路由与跨模型音色兜底）。内部调用点不变。

### 3. `backend/src/web/server.py` — 两个新接口

**`GET /api/episodes/{episode_id}/audio/{beat_id}`**（真实试听）

- 回源顺序：① `05_audio/audio_manifest.json` 里该 beat 记录的**实际文件名**（兼容历史后缀与安全化文件名）→ ② 回退 `05_audio/beat_{safe_beat_id(beat_id)}.wav`。
- 找不到 → `404 找不到该句配音：{beat_id}（请先运行 Stage 5）`；话次不存在 → `404 找不到话次：{episode_id}`。
- 返回 `FileResponse(media_type=audio/wav | audio/mpeg, content_disposition_type="inline", headers={"Cache-Control": "no-store"})`。

**`POST /api/episodes/{episode_id}/beats/{beat_id}/regenerate-tts`**

请求体 `BeatRegenerateTTSRequest`：`beat: dict[str, Any] | None = None`、`rerun_from_s6: bool = False`。

守卫与错误码：

| 场景 | 状态码 | 提示 |
| --- | --- | --- |
| 话次不存在 | 404 | 找不到话次 |
| 该话 `queued` / `running` | 409 | 该话正在后台执行任务，请等待完成后再重配 |
| 该话已 `archived`（过程文件已清理） | 400 | 该话已清理过程文件（仅留成片），无法单句重配，请先从 Stage 1 重跑 |
| 缺 `04_script/script.json` | 400 | 该话还没有 Stage 4 剧本，请先运行撰稿 |
| 缺 `05_audio/audio_manifest.json` | 400 | 该话还没跑过 Stage 5 配音，请先运行 Stage 5 后再使用单句重配 |
| `payload.beat.beat_id` 与路径不一致 | 400 | beat_id 不匹配 |
| `narration_text` 去空白后 < 2 字 | 400 | 字幕原文去空白后至少需要 2 个字 |
| 剧本里没有该句 | 404 | — |
| TTS 合成失败（`TTSStageError`） | 400 | — |
| 提交后台任务失败（`JobError`） | 400 | — |

流程：校验 → （可选）用 `_merge_script_beats` 同款合并策略更新 `script.json` 中**该句**并刷新 `script_readable.md`、同步 `pause_after_ms` 到音频清单 → 加载与 `_stage_tts` 同源配置（作品配置 + 全局/作品发音词典 + `voice_cast` + `series_tts` + `bible` 角色）→ `make_tts_client()` → `resynthesize_single_beat(...)` → **`finally` 中 `await client.aclose()`**（异常吞掉，不能因关连接失败而 500）→ `rerun_from_s6=True` 时 `submit_episode(..., from_stage="s6_timeline", mode="full_video")`。

返回：`{ episode_id, beat_id, clip, duration_ms, beats: view.beats(series_id, episode_no), job }`。

### 4. 前端

- `src/types/studio.ts`：`AudioClipMetaItem`（beat_id / audio_path / duration_ms / pause_after_ms）、`BeatRegenerateTTSResponse`（含 `beats: ScriptBeat[]`、`job | null`）。
- `src/App.tsx`：`handleRegenerateBeatTts(episodeId, beatId, beat, rerunFromS6)` 调 POST；成功后**只替换该 `beat_id` 条目**的基准状态（`ep.beats.map(...)`），其余句子保持原样，避免冲掉用户对其它句子的未保存草稿。
- `src/components/EpisodeStudioView.tsx`：
  - `handlePreviewAudio(beatId)`：`isTtsReady || 该 beat 的 audio_duration_ms > 0` 时用 `<audio>` 播放 `/api/episodes/{id}/audio/{beat_id}?t={audioRevision}`（时间戳防缓存），再次点击同句 = 停止，点别句 = 换源；播放失败 / 取不到文件时回退 `playPreviewTone()` 并给内联提示。
  - `handleConfirmRegenerate(rerunFromS6)`：成功后**只把目标 beat 合并进本地草稿**，其余 beat 保留本地草稿；若其它 beat 仍有未落盘改动（`hasDraftChanges(draft, saved, skipBeatId)`）则保持 dirty 标记，使 `hasVoiceRelevantDraftChanges()` 仍为 true。
  - 重配确认从 v4.12 的内联确认条改为 **v4.13 的居中模态**（见下节）。

---

## 三、Patch v4.13：幕与分句解耦 + 原漫外链 + 单句语速 + 全链路简体

### 1. 幕与分句画面解耦（`scene_id`）

问题：同一格漫画可以被多句解说复用，靠 `target_panel_ids` 反推归属幕必然串幕。

| 层 | 改动 |
| --- | --- |
| `backend/src/core/schemas.py` | `ScriptBeat` 新增 `scene_id: str | None = None`（默认 `None` = 尚未归属）。 |
| `backend/src/stages/s4_scriptwriter.py` | 新增 `_assign_scene_id(beats, scene)`：逐幕拍摄时天然知道当前幕，直接写 `beat["scene_id"] = scene.scene_id`；非逐幕模式保持为空。 |
| `backend/src/core/normalize.py` | `normalize_script_payload` 读入并保留 `scene_id`。 |
| `backend/src/web/server.py` | `PRESERVED_BEAT_FIELDS` 增加 `"scene_id"`；`NULLABLE_BEAT_FIELDS = {"sfx_cue", "speed_override", "scene_id"}`（前端未回传 = 继承旧值；显式 `null` = 解除归属）。 |
| `backend/src/stages/s7_renderer.py` | 新增 `load_beat_scene_map(episode_dir) -> dict[str, str]`（读 `04_script/script.json`，异常返回 `{}`）；`build_scene_bgm_spans` 新增 `beat_scene_map` 参数，归属判定由 `panel_owner[tl_scene.panel_id]` 改为 **`beat_owner[tl_scene.beat_id] or panel_owner[...]`**。 |
| `backend/src/stages/s4_scriptwriter.py` | `apply_narrative_pacing` 的跨幕留白改按 `scene_id` 判定：新增 `_scene_ordinal_index(scenes)`（启用且 `panel_ids` 非空的幕 → 序号，1 起递增）与 `_beat_scene_ordinal(beat, panel_to_scene, scene_ordinals=None)`（优先 `beat["scene_id"]`，未命中才回退按 `target_panel_ids`）；`active_scene_count` 口径同步。 |
| `src/types/studio.ts` | `ScriptBeat` 增加 `scene_id?: string | null`。 |
| `src/components/EpisodeStudioView.tsx` | `beatSceneGroups` 优先按 `beat.scene_id` 归幕，为空才回退 `target_panel_ids[0]`；`DRAFT_EDITABLE_FIELDS` 增加 `'scene_id'`。 |
| `backend/tests/test_schemas.py` | `FIELD_LOCKS` 为 `ScriptBeat` 追加 `scene_id`。 |

**跨幕留白口径**（`apply_narrative_pacing`）：

- 规则 A 跨幕留白 = `PACE_SCENE_END_MS = 1150`；判定条件从「下一句的第一张分镜属于另一幕」改为「下一句的 `scene_id` 属于另一幕」。
- `active_scene_count` = 启用幕数（启用且 `panel_ids` 非空），用于决定末条是否也留白。
- 回归用例见 `backend/tests/test_patch_v46_auto_pacing.py::test_scene_id_takes_priority_over_panel_lookup_for_scene_boundary`：两条 beat 引用**同一张分镜**但 `scene_id` 分属 s1 / s2 → 两条都拿 1150ms；去掉 `scene_id` 则回退同分镜保底。

### 2. 「查看原漫」外链

- `backend/src/web/server.py` 新增 `episode_viewer_url(series_id, episode_no, episode=None)`：主来源 `episode.viewer_url`；缺失时依次回退 `catalog.json` → `episodes_catalog.json` → 话次目录 `episode.json` 的 `source_url`；三处都没有返回空串。
- `episode_item` 的 `viewer_url` 改用该方法。
- 前端：顶部信息栏加「查看原漫」入口 —— **仅 `http/https` 渲染** `<a target="_blank" rel="noopener noreferrer">`；`local://` 等其它协议只显示「本地目录导入」标签；空链接不渲染。**禁用 `window.open`**。

### 3. 单句语速 `speed_override`

| 层 | 改动 |
| --- | --- |
| `backend/prompts/script_narrate.md` | 【铁律三之二】普通旁白省略或 `null`；仅明显节奏/情绪变化点给值，紧张/争吵/惊呼 `1.08~1.20`，低沉/悲伤/回忆 `0.85~0.94`，全话约 `4~10` 处。输出 Schema 新增 `"speed_override": null`。 |
| `backend/src/core/normalize.py` | `MIN_SPEED_OVERRIDE = 0.5` / `MAX_SPEED_OVERRIDE = 2.0`；`coerce_float()` 容错（字符串 / 越界 / `0`）；落盘前钳位。 |
| `backend/src/stages/s4_scriptwriter.py` | `_clamp_beat_speed_overrides(beats)`：统一成「`None` = 跟随角色默认语速」或落在 `[0.5, 2.0]` 的浮点数。 |
| 前端 | 审核台表格行配音区加紧凑语速显示与微调：`nudgeBeatSpeed(beat, delta)`（步进 ±0.02，基于 `speed_override ?? 角色默认` 并钳位）、`−` / 数字 / `+`、以及「恢复默认」（写回 `null`）。 |

### 4. 全链路强制简体中文

| 层 | 改动 |
| --- | --- |
| `backend/pyproject.toml` | 新增 `opencc-python-reimplemented`。 |
| `backend/src/core/normalize.py` | 新增 `to_simplified_chinese(text)`（OpenCC `t2s`，**惰性创建**，缺依赖或转换异常时降级为「不转换」并打 warning，不阻断流水线）；ASCII、标点与 MiniMax 标签原样透传。 |
| `backend/src/stages/s3_vision.py` | `scene_summary` / `dialogues[].text` / `dialogues[].speaker_guess` / `sfx_list` 落盘前转简体。 |
| `backend/src/core/normalize.py`（脚本链路） | `narration_text`：剥离标签 → 转简体 → 引号规范化 → 净化 → 长度校验 → 裁剪；`tts_text`：转简体 → 引号规范化 → 净化（`for_tts=True`）→ 停顿标签规范化 → 按可发音长度裁剪。 |
| `backend/src/web/server.py` | 新增 `_to_simplified_beat(beat)`：人工保存 `PUT /api/episodes/{id}/script` 与单句重配落盘前调用（只动 `narration_text` / `tts_text`）。 |
| `backend/src/stages/s5_tts.py` | `build_spoken_text` 送音前再兜底转一次简体。 |
| `backend/prompts/vision_extract.md` | 新增「## 硬约束零：一律写简体中文（Patch v4.13）」。 |
| `backend/prompts/script_narrate.md` | 新增【铁律三之三：全链路简体中文】。 |

### 5. 单句重配改为居中确认弹窗

`regenerateTarget` 有值时渲染 `fixed inset-0 z-50` 居中模态（遮罩 `bg-slate-900/50 backdrop-blur-[1px]`）：点遮罩或 `Esc` 关闭（`regenerateBusy` 时锁定），弹窗内展示 `beat_id`、待合成文本预览、配音角色与音色、语速、停顿毫秒，提供「只重配此句」「重配并从 S6 重新出片」「取消」。**禁用 `window.confirm` / `window.alert`**。

### 6. `StoryboardEditor` 幕内分镜编排

| 能力 | 规则 |
| --- | --- |
| 添加分镜 | 幕头「添加分镜」→ 勾选面板 → 插入该幕；同一格可被多幕引用（复用同一格）。已在其它幕使用的分镜**置灰不可选**。 |
| 移除分镜 | 缩略图「移除」；**保持 `panel_ids.length >= 1`**，最后一格禁用。 |
| 幕内换序 | 仅当相邻两格 `panel_id` **相同**（同一分镜的副本）才允许「上移 / 下移」；不同分镜互换会破坏后端 `_validate_storyboard_scenes` 的「幕内 `panel_sort_key` 单调不减」校验，故按钮不渲染。 |
| 提示 | 常驻灰字「同一幕内按剧情时间序排列；跨句复用同一格请通过『添加分镜』插入。」；`findSceneOrderIssues()` 给出幕内逆序与跨幕交叉的行内提示。 |
| 保存 | 沿用 `PUT /api/episodes/{id}/storyboard`，`source="human"`。 |

后端 `_validate_storyboard_scenes` **不修改**：幕内非空、同一分镜不跨幕重复、分镜属本话、幕内顺序单调不减、后幕首格必须大于前幕末格。

---

## 四、Patch v4.14：MiniMax 语气词与停顿标签

### 1. `backend/src/utils/tts_tags.py`（新建，标签唯一权威）

```python
MINIMAX_INTERJECTION_TAGS: tuple[str, ...] = (
    "(laughs)", "(chuckle)", "(coughs)", "(clear-throat)", "(groans)",
    "(breath)", "(pant)", "(inhale)", "(exhale)", "(gasps)", "(sniffs)",
    "(sighs)", "(snorts)", "(burps)", "(lip-smacking)", "(humming)",
    "(hissing)", "(emm)", "(sneezes)",
)                                                  # 19 个官方标签

MINIMAX_PAUSE_TAG_PATTERN = re.compile(r"<#(\d+(?:\.\d{1,2})?)#>")  # 秒数 x ∈ [0.01, 99.99]
MINIMAX_PAUSE_MIN_SECONDS = 0.01
MINIMAX_PAUSE_MAX_SECONDS = 99.99
MINIMAX_TAG_SPLIT_PATTERN = re.compile(<19 个标签> | r"|<#\d+(?:\.\d{1,2})?#>")
```

函数：

| 函数 | 语义 |
| --- | --- |
| `normalize_minimax_pause_tags(text)` | 规范化停顿标签，四步：**① 清理越界写法**（先 `_LOOSE_NUMERIC_PAUSE_TAG_PATTERN` 四舍五入到两位小数保留，如 `<#1.234#>` → `<#1.23#>`；**再** `_LOOSE_PAUSE_TAG_PATTERN` 删除不合规写法如 `<#abc#>`。顺序不可颠倒，否则 `<#1.234#>` 会在四舍五入前被整段删掉）→ **② 合并**相邻或仅隔空白的多个停顿（秒数相加，`<#0.5#><#0.8#>` → `<#1.3#>`）→ **③ 钳位**到 `[0.01, 99.99]` → **④ 剥除贴首 / 贴尾的孤立停顿**（停顿只能夹在两段可发音文字之间；剥除后整句会空则保留；剥除时同步 `.lstrip()` / `.rstrip()`，不留残边空白）。 |
| `strip_minimax_tts_tags(text)` | 抹掉全部标签（用于字幕轨净化），并收敛多余空白与「空格 + 标点」空洞。 |
| `mask_minimax_tts_tags(text)` | 把**完整匹配**的标签换成等长非字母占位符 `〇`，**不改变任何字符下标**；裸单词（如 `coughs`）不遮蔽。 |
| `has_minimax_pause_tag(text)` / `count_minimax_interjection_tags(text)` | 存在性 / 计数。 |

### 2. 后端保护链

| 层 | 改动 |
| --- | --- |
| `backend/src/stages/s4_scriptwriter.py` | `validate_tts_text_no_pinyin()`：扫描前先 `mask_minimax_tts_tags()`，因此 `(coughs)` / `(clear-throat)` 不再被当成拼音误杀，而**真拼音与漏括号的裸英文仍被拦下**；`strip_pinyin()` 按标签切段，只剥标签之外的拉丁字母（`_strip_pinyin_plain`）。 |
| `backend/src/core/normalize.py` | `narration_text` 经 `strip_minimax_tts_tags()`（字幕轨不带标签）；`tts_text` 经 `normalize_minimax_pause_tags()`；`clip_text()` 改为**标签豁免额度**（标签整段保留或整段丢弃，正文按 limit 截断，**绝不从标签中间腰斩**）；新增 `spoken_length()` / `clip_text_by_spoken_length()`（按剥离标签后的可发音正文计长）；`dedupe_adjacent_beats` 比较键时忽略标签但**保留胜出句的标签**。 |
| `backend/src/stages/s5_tts.py` | `apply_pronunciation()` 按标签切段替换多音字（`_apply_pronunciation_plain`），**不改标签内部**；`build_spoken_text()` 送音前调 `normalize_minimax_pause_tags()`，且整句判定基于**剥离标签后的正文长度**（`TTS_FRAGMENT_RATIO = 0.6`），避免 `(laughs)` 把整句判成「碎片」而回退字幕原句；新增 `_warn_if_tag_model_unsupported()`：模型不是 `speech-2.8-*` 时打 warning 提示标签可能不会被读出来（避免静默失败）。 |
| `backend/prompts/script_narrate.md` | 【铁律三之一】标签只写 `tts_text`；每集语气词约 `2~6` 处、停顿约 `1~4` 处；`<#x#>` 必须夹在两段可发音文字之间、不可连用、不可句首句尾；写了 `tts_text` 必须写**含标签的完整整句**。 |

### 3. 前端标签浮层

- `src/constants/ttsTags.ts`：`MINIMAX_INTERJECTION_TAGS`（19 个，按 4 组：笑与冷哼 / 叹息与呼吸 / 咳与哭痛 / 其它拟声）、`INTERJECTION_TAG_GROUPS`、`PAUSE_QUICK_SECONDS = [0.3, 0.5, 0.8, 1.2, 2.0]`、`PAUSE_TAG_MIN_SECONDS = 0.01` / `MAX = 99.99`、`formatPauseTag()`、`isSpeech28Model()`。
- `src/components/TtsTagPicker.tsx`：受控浮层内容，Tab A 语气词 / Tab B 句中停顿；Esc 关闭；模型非 `speech-2.8-*` 时顶部温和提醒；**无 `alert` / `confirm`**。
- `src/components/EpisodeStudioView.tsx`：
  - 每个送音文本 textarea 记 `ref` 与选区 `{start, end}`（`onSelect` / `onClick` / `onKeyUp` / `onBlur` 同步），工具栏加「+ 语气 / 停顿标签」按钮展开浮层。
  - `insertTtsTag(beat, snippet)`：
    - 送音文本**为空** → 用字幕原文铺底。语气词插句首（`{tag} {narration_text}`）；停顿标签若原文含「前后都有文字」的句中标点（`，` `、` `；` `……` `——`）则插在该标点之后（如「别怕，<#0.5#>有我在。」）；没有句中标点则先铺底、光标停在句中并提示用户确认位置后再点一次插入（**绝不空手返回**）。
    - 送音文本**非空** → 按选区替换（无选区则按光标处插入）；相邻已有停顿标签时先摘掉旧的，避免连用（正则 `/<#\d+(?:\.\d+)?#>/`，与后端一致）。
  - `focusAfterTag(beatId, caret)`：插入后还焦并把光标停在插入内容正后方。

---

## 五、测试覆盖

### `backend/tests/test_patch_v412_single_beat_tts.py`（18 项）

- 只调 **1 次** TTS；其它 beat 的 WAV `inode` / `size` / `mtime` **全部未动**；清单顺序不变。
- `duration_ms` 更新为新 WAV 的实测值。
- 继承前句对白说话人音色（`voice_cast={"恩彗": "Elias"}`）。
- 缺清单 / 未知 beat → `TTSStageError` 且**不调 TTS**。
- 清单缺该句 → 按 `script.beats` 顺序插回。
- `audio_issues.json` 裁剪；无剩余问题时删除该文件。
- 只清目标 beat 的旧文件（mp3 → wav 场景）。
- HTTP：`GET /audio/{beat_id}` 返回 200 + `no-store` + 字节与磁盘一致；不存在 → 404。
- HTTP：`POST regenerate-tts` 成功返回 `clip` / `beats` / `job`；局部 beat 合并 + 停顿同步进清单 + 可读稿刷新；409（running）/ 400（archived）/ 400（缺 S5 清单）/ 400（短文本）；`rerun_from_s6=true` 只提交 `s6_timeline` + `mode=full_video`；未知话次 404。

### `backend/tests/test_patch_v413_v414.py`（44 项）

覆盖 `scene_id` 读写、S4 按幕写入、S7 BGM 按 `beat.scene_id` 优先映射、`to_simplified_chinese`、19 个语气词与 `<#0.5#>` 通过拼音校验而真拼音 / 裸英文仍被拦、`clip_text` 不腰斩标签、`normalize_minimax_pause_tags` 的合并与贴首贴尾剥除、截断后末尾不残留 `<#0.5#>`、贴首贴尾剥除不留残边空白、`narration` 洗标签而 `tts_text` 保留、`speed_override` 范围、`viewer_url` 透传、前端关键产物在位。

### `backend/tests/test_patch_v46_auto_pacing.py`（新增 1 项）

`test_scene_id_takes_priority_over_panel_lookup_for_scene_boundary`：`scene_id` 优先于分镜反查判定跨幕边界。

---

## 六、验收命令与结果

```bash
# 1. 后端静态检查（本机 ruff 在全局，backend/.venv 内没有）
cd backend && /opt/homebrew/bin/ruff check src tests
# → All checks passed!

# 2. 定向验证
cd backend && .venv/bin/python -m pytest tests/test_patch_v412_single_beat_tts.py tests/test_patch_v413_v414.py
# → 62 passed

# 3. 快档回归（纯单测 + API，跳过重型 ffmpeg）
cd backend && .venv/bin/python -m pytest -n auto -m "not slow"
# → 全绿

# 4. 全量并发兜底（准备提交前必跑）
cd backend && .venv/bin/python -m pytest -n auto --dist loadfile
# → 全绿

# 5. 前端类型检查与生产构建
npx tsc --noEmit && npm run build
# → 无报错；产物 ~450 kB
```

**用例总数**：`--collect-only` → **853** 条（其中 `slow` 22 条）。分级跑法与耗时对比见仓库根 `AGENTS.md` 与 `backend/README.md`「测试跑法（分级）」。

---

## 七、已知边界与后续

1. **既有剧本不含新字段**：`scene_id` / `speed_override` 只在**重新跑 Stage 4（撰稿）**后才写入 `script.json`；旧剧本里为 `None` / 缺省。前端在 `scene_id` 为空时回退按 `target_panel_ids[0]` 归幕，行为与改前一致。
2. **旧成片不受影响**：v4.13 的 `scene_id` 只影响后续生成的剧本与渲染；既有 `script.json` 不会自动补写。
3. **标签能力依赖模型**：语气词与停顿标签是 MiniMax Speech 2.8 的能力，换模型时后端会打 warning、前端会显示提醒，但**不阻止**写入。
4. **快档仍会调 ffmpeg**：保留的是轻量音频滤镜裁剪（`silenceremove` / `loudnorm`），视频编码 `libx264` 为 0 次——这些调用测的正是「裁剪后真实时长」，砍掉会丢断言。
