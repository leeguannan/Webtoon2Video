# Webtoon2Video 增量补丁规格书（Patch v4.16：角色名全链路自动统一 + Stage 5 音频增量检测与缓存复用）

> **基线提交**：`ba31f48`（Patch v4.15 之后）
> **落地提交**：`78e4894` `feat(script): 角色名全链路自动统一，Stage 5 音频增量检测与缓存复用`（10 文件 +2118/-33）
>
> **目标**：
> 1. **角色名全链路自动统一**：模型在同一话里把同一个角色写成「恩彗 / 恩慧 / 恩惠」时，剧本、故事稿、StoryBible、人工保存的解说词必须收敛到同一个规范名；同音错字由后端自动识别并归一，不再靠人工逐句修。
> 2. **Stage 5 音频增量检测与缓存复用**：改一句台词只重配这一句；没改的句子直接复用上次的成品音频（不再重新调 TTS、也不再重新裁剪），重跑整话 Stage 5 的成本从「全话重新合成」降到「只合成改动的句子」。
>
> **铁律**：
> - 严格遵守文件修改白名单（10 个），禁止全局重构；**不得改动** `s1_crawler.py` / `s2_segmenter.py` / `s3_vision.py` / `s3b_scenes.py` / `s6_timeline.py` / `s7_renderer.py` 与**任何前端文件**。
> - **禁止新增第三方 PyPI 依赖**（只用标准库 + 已在库内的 `opencc-python-reimplemented`）。
> - **禁止修改 `AudioClipMeta`（`backend/src/core/schemas.py`）字段集合** —— 缓存数据一律放 `05_audio/_cache/`，不进模型。
> - **禁止破坏 `clean_stale_audio(audio_dir)` 默认行为**：不传 `keep_names` 时仍删除全部 `beat_*.wav` / `beat_*.mp3`。
> - **禁止破坏 `ManifestManager.reset_from_stage(ep, "s5_tts")` 默认清理**：`preserve_tts_cache=False` 时仍清空整个 `05_audio`；`=True` 时仍删 `audio_manifest.json` 与 `beat_*.wav`，只保留 `05_audio/_cache/`。
> - **禁止破坏 `StoryBibleManager._merge_characters` 原「外貌称呼 → 正式名」归并**语义，只在其后**追加**同音变体兜底。
> - 正文替换**严禁**把外貌 / 泛称替换进解说词，**严禁**误伤「小恩小惠」「受人恩惠」等惯用词。
> - 提交 / 推送必须由用户显式下令。

---

## 一、文件修改白名单（10 个）

### 后端（9 个）
1. `backend/src/core/character_unify.py`（**新建**，621 行）— 同音变体识别、角色名归一器的唯一权威实现。
2. `backend/src/core/config.py` — `SeriesConfig.character_aliases`（向后兼容旧 YAML）。
3. `backend/src/core/normalize.py` — `normalize_script_payload(..., canonicalizer=...)` 收尾归一。
4. `backend/src/core/memory.py` — `_merge_characters` 的同音变体兜底。
5. `backend/src/core/manifest.py` — `reset_from_stage(..., preserve_tts_cache=False)`。
6. `backend/src/stages/s4_scriptwriter.py` — 4A / 4B 的角色锁名提示与归一接入。
7. `backend/src/stages/s5_tts.py` — 合成指纹、缓存读写、`use_cache` / `write_cache`。
8. `backend/src/web/job_runner.py` — 重跑保留 `_cache`；`_stage_tts` 输出复用 / 新合成计数。
9. `backend/src/web/server.py` — 人工保存解说词时统一角色名。

### 测试（1 个）
10. `backend/tests/test_patch_v416_char_unify_and_s5_cache.py`（**新建**，739 行 / 17 项）。

---

## 二、Part 1：角色名全链路自动统一

### 1. `backend/src/core/character_unify.py`（新建）

#### 同音字簇 `_HOMOPHONE_CLUSTERS`（:56，30 组）

覆盖创作中最常见的同音错字：

```
彗/慧/惠/蕙   振/真/珍/镇/祯   敏/珉/民/闵   宇/雨/羽/禹
贤/弦/娴/轩/宣/炫   熙/希/曦/熹/锡   雅/亚/娅   妍/研/延/颜
娜/呐   赫/贺/鹤   俊/峻/骏/浚   浩/昊/皓/豪   哲/喆/辙
琳/林/霖   智/志/致/知   静/净/靖   瑞/睿/锐   成/城/诚
泰/太   正/政/郑   秀/绣   珠/朱   灿/璨/粲   彬/宾/斌
勋/熏/薰   盛/胜/圣   仁/人/在/载/宰   彩/采
```

#### `are_homophone_name_variants(name_a, name_b) -> bool`（:102）

判定规则（全部满足才为同音变体）：

1. 先过 `to_simplified_chinese`（繁简同名不算不同）；
2. 两个名字**等长**且**长度 ≥ 2**（单字名不判同音，避免「恩」这类高频单字误伤）；
3. **恰好 1 个字不同**，且这一对字落在同一个同音簇内。

#### `CharacterNameCanonicalizer`（:233，frozen dataclass）

| 字段 | 含义 |
| :--- | :--- |
| `canonical_names` | 本话认定规范名集合 |
| `role_map` | **角色位用**：全名 / 简称 / 显式别名 / 同音变体 → 唯一规范角色名 |
| `text_rules` | **正文用**：`(变体, 规范名)` 元组，按变体长度降序 |
| `alias_notes` | 规范名 → 其被接受的别名集合（供 Prompt 提示与调试） |

方法：

| 方法 | 用途 |
| :--- | :--- |
| `canonicalize_role(role)` | 归一 `speaker_role` / `characters_present` / `character_updates[*].name`；**旁白（narrator / 旁白 / 解说）与空串原样保留** |
| `canonicalize_text(text)` | 归一 `narration_text` / `tts_text` / `story_prose` / `chapter_title` / `hook_title` / `one_line_hook` / `summary` |
| `canonicalize_story_draft(draft)` | 归一整份故事稿（4A 产物） |
| `canonicalize_script_dict(payload)` | 归一整份剧本字典（**返回新字典，不原地改**） |
| `prompt_lock_block()` | 生成给 LLM 看的「角色锁名」提示块 |

#### `build_character_canonicalizer(...)`（:456）

```python
def build_character_canonicalizer(
    *,
    characters: Sequence[Any] | None = None,
    series_tts: Any | None = None,
    series_aliases: Mapping[str, str | Sequence[str]] | None = None,
    extra_names: Iterable[str] | None = None,
    extra_texts: Iterable[str] | None = None,
) -> CharacterNameCanonicalizer:
```

**规范名优先级**（tier → rank → 频次 → 出现顺序）：

| tier | 含义 | rank |
| :--- | :--- | :--- |
| `_TIER_VOICE = 0` | 已配置专属音色（`series_tts.character_voices` 的键） | `_RANK_PRIMARY` |
| `_TIER_BIBLE = 1` | 已有 StoryBible / 作品级 `character_aliases` 的键 | `_RANK_PRIMARY` |
| `_TIER_DISCOVERED = 2` | 本话发现（`speaker_role` / 正文里出现） | `_RANK_PRIMARY` |

由 tier ≤ 1 的权威名派生的同音写法记为 `_RANK_DERIVED`，**永远不顶替权威原名**；tier 2 的派生写法与原名同层，由频次决胜。

**「正文出现即发现」**：`_homophone_substitutions(name)`（:412）把规范名的每个字按同音簇展开成候选写法，**只有当该写法真的出现在 `extra_texts` 里**才纳入候选 —— 于是「恩慧」不需要人工登记，只要正文写了、且「恩彗」已在角色表里，就会被自动发现并归一。第 1 话还没有 StoryBible 时，靠 `extra_names` + 频次自发现（「恩彗 / 恩慧 / 恩惠」谁出现得多谁当规范名）。

**正文替换的三条护栏**（`text_rules` 构造时过滤）：

- `_DESCRIPTIVE_MARKERS`（:150）：含「黑发 / 橙眼 / 少年 / 少女 / 男子 / 女主 / 男主 …」等外貌 / 身份泛词的描述**不进正文替换**；
- 变体长度必须 ≥ 2，且**不得短于**规范名（避免把 3 字全名硬拉成 2 字简称、避免叠字）；
- 惯用词保护：`_COMMON_PHRASE_PREFIXES = ("小", "受人", "施以", "给予", "报答", "感念", "莫大", "一点")` 与 `_COMMON_PHRASE_SUFFIXES = ("于", "之")`（:160/:162）命中的写法**跳过替换** —— 「小恩小惠」「受人恩惠」「施以恩惠于我」保持原文。

### 2. `backend/src/core/config.py` — 作品级别名表

```python
class SeriesConfig(...):
    character_aliases: dict[str, str | list[str]] = Field(default_factory=dict)
```

`{规范名: 别名或别名列表}`，旧 YAML 无此键时为空 dict，**向后兼容**。

### 3. `backend/src/core/normalize.py` — 剧本归一收口

```python
def normalize_script_payload(
    payload: Mapping[str, Any],
    *,
    episode_id: str,
    episode_no: int,
    valid_panel_ids: Sequence[str] | None = None,
    canonicalizer: Any | None = None,   # Patch v4.16
) -> dict[str, Any]:
    ...
    if canonicalizer is not None:
        result = canonicalizer.canonicalize_script_dict(result)   # 必须接住返回值
    return result
```

⚠️ `canonicalize_script_dict` **返回新字典**，不是原地修改 —— 必须写 `result = ...`，否则归一完全不生效（这是本补丁开发过程中真实踩过的坑）。

### 4. `backend/src/core/memory.py` — StoryBible 归并的同音兜底

`_merge_characters` 的 `_find(update)` 在原有「精确名 → 别名」匹配之后**追加**一轮：若 `update.name` 与 `character.name` 或 `character.aliases` 中任一项构成同音变体，则并入该角色。原「外貌称呼 → 正式名」归并逻辑**一字未改**。

### 5. `backend/src/stages/s4_scriptwriter.py` — 4A / 4B 联动

新增模块级符号：

| 符号 | 行 | 作用 |
| :--- | :--- | :--- |
| `build_episode_canonicalizer(series_dir, characters, *, extra_names, extra_texts)` | :2014 | 构建本话归一器 |
| `_story_draft_texts(draft)` | :2035 | 抽取故事稿正文（hook / full_story / arc / 各章标题与正文） |
| `_canonicalize_story_draft_with_text(draft, canonicalizer, series_dir, context)` | :2055 | **用故事稿自身文本**重建归一器后再归一（否则 4A 正文里的同音写法发现不了） |
| `_append_lock(memory_block, character_lock)` | :2072 | 把角色锁名块追加到 MEMORY 上下文 |
| `canonicalizer_lock(canonicalizer)` | :2079 | 生成锁名提示文本 |

- `_write_overview`（:2106）/ `_write_story_draft`（:2150）/ `_write_narration_chapter`（:2290）三处 Prompt 的 MEMORY 块追加角色锁名（4B 用 `MEMORY_MIN`）。
- `write_episode_script(..., canonicalizer=None, series_dir=None)`：4A 产出故事稿后立即归一并落盘；4B 汇聚 `merged_beats` 后**重建**含本话候选名与文本的归一器，再交给 `normalize_script_payload`。
- 人工改稿的 `story_draft.md` **不被覆盖**（只有自动生成的 json / md 走归一后落盘）。

### 6. `backend/src/web/server.py` — 人工保存也归一

```python
def _canonicalize_script_beats(series_dir: Path, beats: list[ScriptBeat]) -> list[ScriptBeat]:
```

用作品 StoryBible + `series_config` 构建归一器，归一 `speaker_role` / `narration_text` / `tts_text` 后回填 `ScriptBeat`；异常时向 stderr 打印并**原样返回**（不因归一失败阻断保存）。

`PUT /api/episodes/{episode_id}/script`（:1971）在 `_to_simplified_beat` 之后调用它。

---

## 三、Part 2：Stage 5 音频增量检测与缓存复用

### 1. `backend/src/stages/s5_tts.py` — 合成指纹

常量（:95-99）：

```python
AUDIO_CACHE_DIRNAME = "_cache"
AUDIO_CACHE_INDEX_FILENAME = "index.json"
AUDIO_CACHE_PIPELINE_VERSION = "v4.16-trim-lufs16"
```

`_resolve_beat_synth_plan(...) -> _BeatSynthPlan`（:1104）是**纯函数**：把「继承对白说话人 → build_spoken_text → 三分支音色解析 → speed_override → resolve_voice_for_tts_model → native_speed / trim_speed」全部算完，产出最终送音文本、最终音色、最终语速与指纹。

> **指纹与合成共用同一份解析结果**：只有这样「缓存命中」和「真实合成」才一定产出同样的音频 —— 否则改了音色解析逻辑却仍命中旧缓存，就会出现「界面上换了音色、听到的还是旧声音」。

指纹 payload（:1207-1218）：

```python
fingerprint = {
    "v": AUDIO_CACHE_PIPELINE_VERSION,
    "spoken_text": spoken,                    # 已含多音字替换与语气 / 停顿标签
    "instruction": str(beat.emotion_instruction or ""),
    "voice": str(beat_voice or ""),            # 已做继承 / series_tts / voice_override / 跨模型兜底
    "speed": round(float(speed), 4),
    "native_speed": bool(native_speed),
    "tts_model": str(tts_model or getattr(client, "model", "") or ""),
    "provider": str(getattr(client, "provider_name", "") or type(client).__name__),
    "sample_rate": str(getattr(client, "sample_rate", None) or ""),
    "trim_silence": bool(trim_silence),
}
digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
```

**明确不纳入指纹**（改了它们不影响音频，不该导致重配）：

```
beat_id   pause_after_ms   target_panel_ids   motion   focus_hint   sfx_cue   scene_id
```

### 2. 缓存读写与命中行为

| 组件 | 位置 | 行为 |
| :--- | :--- | :--- |
| `_cache_dir` / `_read_cache_index` / `_write_cache_index` | :971-1005 | 缓存目录 `05_audio/_cache/`，索引 `index.json` |
| `_cache_lookup` | :1008 | 命中判定：文件存在 + 大小 > 44 字节 + 探测时长 > 0 |
| `_cache_store` | :1036 | 真实合成且探测成功后才写入 |
| `_copy_cached_audio` | :1091 | 临时文件 + `os.replace` **原子复制**到 `05_audio/beat_{safe_beat_id}{suffix}` |
| `_synthesize_beat(..., use_cache=True, write_cache=True, ...)` | :1251 | 命中 → 复制文件、**不调 `client.synthesize`、不调 `_trim_silence`**、用当前 `beat.pause_after_ms` 构造 `AudioClipMeta`、`stats_out["reused"] += 1`；未命中 → 走原合成链路，**仅当 `write_cache and trim_ok and duration_ms > 0`** 才写缓存、`stats_out["synthesized"] += 1` |
| `clean_stale_audio(audio_dir, *, keep_names=None)` | :1415 | 传 `keep_names` 时保留本轮产出、只清多余旧 `beat_*`；**不传时行为与改造前完全一致**；`_cache/` 不在 `beat_*` 匹配范围，不受影响 |
| `synthesize_episode(..., use_cache=True, stats_out=None)` | :1454 | 不再开头无条件清理；轮末 `clean_stale_audio(target_dir, keep_names={meta.audio_path for meta in final_metas})` + 落盘索引 + 日志「音频缓存：复用 %d 条，新合成 %d 条」 |
| `resynthesize_single_beat` | :1755 | `use_cache=False`（用户明确要重合成）但 `write_cache=True` → 合成成功后**写回缓存**，下次整话重跑可复用 |
| `run_stage5(..., use_cache=True, stats_out=None)` | :1881 | 透传；返回值仍为 `Path` |

**静音占位绝不进缓存**：`_write_silence_placeholder` 产生的失败占位音频不满足「真实合成 + 探测成功」，因此不会污染 `_cache/`；修复后重跑会重新合成。

### 3. `backend/src/core/manifest.py` — 重跑保留缓存

```python
class ManifestManager:
    TTS_CACHE_DIRNAME = "_cache"

    def reset_from_stage(
        self,
        episode_no: int,
        from_stage: str,
        *,
        include_human_storyboard: bool = False,
        preserve_tts_cache: bool = False,
    ) -> dict[str, Any]:
```

清理到 Stage 5 时：

- `preserve_tts_cache=True` → `_clear_dir_keeping(stage_dir, (self.TTS_CACHE_DIRNAME,))`：删除 `05_audio` 下**除 `_cache/` 外**的全部文件（含 `audio_manifest.json`、`audio_issues.json`、`beat_*.wav`）；
- `preserve_tts_cache=False`（默认）→ `_clear_dir(stage_dir)`：清空整个 `05_audio`，**与改造前完全一致**。

> 类属性在方法内必须写 `self.TTS_CACHE_DIRNAME`（直接写类名会 NameError）。

### 4. `backend/src/web/job_runner.py`

- `_run_episode`（:423）调 `reset_from_stage(..., include_human_storyboard=False, preserve_tts_cache=True)` —— 后台重跑保留 `_cache/`。
- `_stage_tts`（:687-712）传 `stats_out: dict[str, int] = {}` + `use_cache=True`，结束后向前端日志输出：

  ```
  [配音缓存] 复用 {reused} 条缓存音频，新合成 {synthesized} 条
  ```

### 5. 角色名匹配兜底

`_match_character_by_variant`（:850）与 `_match_character_voice_by_variant`（:878）在精确 / 忽略大小写匹配失败后，追加**别名 + 同音变体**匹配；`_voice_role_kind` 命中变体时按「narrator」处理（不采信可能过期的旧 `speaker_gender`）。

---

## 四、契约保护清单（改动不得破坏）

| 契约 | 保护方式 |
| :--- | :--- |
| `AudioClipMeta` 字段集合 | 未改动；缓存数据全在 `05_audio/_cache/` |
| `clean_stale_audio(audio_dir)` 默认行为 | `keep_names` 为默认 `None` 时删全部 `beat_*`，与旧版一致 |
| `reset_from_stage(ep, "s5_tts")` 默认清理 | `preserve_tts_cache` 默认 `False` → 清空整个 `05_audio` |
| `_merge_characters` 原归并语义 | 只在 `_find` 末尾**追加**同音兜底，原分支不动 |
| 外貌 / 泛称不进正文 | `_DESCRIPTIVE_MARKERS` 过滤 |
| 惯用词不误伤 | `_COMMON_PHRASE_PREFIXES` / `_COMMON_PHRASE_SUFFIXES` 保护 |
| 依赖数量 | 未新增任何 PyPI 依赖 |

---

## 五、测试覆盖

`backend/tests/test_patch_v416_char_unify_and_s5_cache.py`（**新建**，739 行 / 17 项，全部离线）：

| 组 | 场景 |
| :--- | :--- |
| A 单元 | 同音错字统一（恩慧 / 恩惠 → 恩彗）；惯用词「小恩小惠」「施以恩惠于我」保持原文；外貌称呼「黑发橙眼少年」不进正文；第 1 话无 StoryBible 时同话高频自发现；`are_homophone_name_variants` 边界规则；StoryBible 归并接受同音变体 |
| B 联动 | 4A / 4B 联动：Stub LLM 混写人名 → `story_draft.json` / `.md`、`script.json` 与 StoryBible 均统一、且不把角色拆成两个 |
| C 缓存 | 连跑两次 `run_stage5` 第二次 FakeTTS 调用 == 0；只改一句 → 只重配该句；只改 `pause_after_ms` → 0 次 TTS 且清单停顿更新；改 `speed_override` / `series_tts` 音色 / `pronunciation_overrides` / `tts_model` → 仅受影响句重配；中间插入新句致 `beat_id` 顺延 → 未改旧句仍命中缓存、只新句重配；失败静音占位不进缓存；`reset_from_stage(preserve_tts_cache=True/False)` 行为；`POST /api/episodes/{id}/reset` 彻底清缓存；单句重配写回缓存 |
| D 接口 | `PUT /api/episodes/{id}/script` 人工保存自动统一角色名 |

**开发期真实踩到的坑（保留记录）**：

1. `CharacterProfile` 必填 `visual_traits` / `current_status` / `first_seen_ep` / `last_seen_ep`，测试构造需补齐；
2. `StoryBibleManager` 没有 `merge_episode`，实际 API 是 `apply_episode_result(script, *, base=None, arc_summary=None) -> StoryBibleV2`；
3. `normalize.py` 丢弃 `canonicalize_script_dict` 的返回值 → 归一完全不生效；
4. `use_cache=False` 时单句重配写不回缓存 → 拆出 `write_cache` 参数（`use_cache` 管读、`write_cache` 管写）。

---

## 六、验收命令与结果

```bash
cd backend
.venv/bin/python -m pytest tests/test_patch_v416_char_unify_and_s5_cache.py -q   # 17 passed
.venv/bin/python -m pytest -q                                                    # 876 条全绿
/opt/homebrew/bin/ruff check src tests                                           # All checks passed!
cd .. && npm run lint && npm run build                                            # 通过
```

白名单核查：只动白名单内 10 个文件，未触碰任何前端文件，未触碰 `s1` / `s2` / `s3` / `s3b` / `s6` / `s7`。

---

## 七、已知边界

1. **既有剧本不含新字段**：`scene_id` / `speed_override` 等字段要等重新跑 Stage 4 才会写入；`scene_id` 为空时前端回退按 `target_panel_ids[0]` 归幕，行为与改前一致。
2. **缓存是「合成结果级」而不是「片段级」**：改一处共享的发音词典会让受影响句子的指纹变化并重配，未受影响的句子照常复用。
3. **缓存不跨作品共享**：缓存位于各话次的 `05_audio/_cache/` 下，删除话次目录即清空。
4. **`_cache/` 与成片清理**：`POST /api/episodes/{id}/reset`（默认 `preserve_tts_cache=False`）与归档清理会连同 `_cache/` 一并删除，这是有意为之。
