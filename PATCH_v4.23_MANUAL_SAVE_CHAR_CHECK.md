# Webtoon2Video 增量补丁规格书（Patch v4.23：人工保存取消强制恢复角色名，改为检查提醒 + 用户确认放行）

> **目标**：
> 1. **人工保存不再改写用户的角色名**：前端审核台里人工改过的「说话人 / 字幕原文 / 送音文本」保存时**原样落盘**，后端不再无条件把角色名强制改回规范名；改成「保存前只读检查 → 有告警就弹应用内确认框 → 用户三选一」。
> 2. **只读检查要准**：能区分「别名写法 / 同音异写 / 未登记的角色」三类问题，给出规范名建议与上下文片段；不误报「小恩小惠」「施以恩惠于」这类惯用词搭配，也不误报「未来的丈夫」这类关系称谓。
> 3. **Stage 4 AI 生成阶段照旧自动归一**：`canonicalize_script_dict` 与 4A/4B 的「角色名锁定」提示词**一行不改**，AI 生成的稿子仍然统一用规范名；本次只放开**人工编辑**这一段。
>
> **铁律**：
> - 只读检查**不得**修改任何文本（不落盘、不改内存里的 beat）。
> - 前端禁用 `window.alert` / `window.confirm`，确认框一律走应用内弹窗。
> - 大图弹窗里的自动保存（flushSave）**不阻断输入**，只做温和提示。
> - 关系 / 身份泛称（「丈夫」「岳母」…）继续豁免：既不进正文归一规则，也不触发告警。

---

## 一、文件修改白名单

### 后端（2 个）
1. `backend/src/core/character_unify.py` — 新增 `CharacterCheckWarning` 数据类与 `CharacterNameCanonicalizer.inspect_script_beats()` 只读检查。
2. `backend/src/web/server.py` — `ScriptUpdateRequest` 新增 `auto_unify_characters`；PUT 默认不归一并回传 `character_warnings`；新增 `POST /api/episodes/{id}/script/check-characters` 预检接口。

### 前端（3 个）
3. `src/types/studio.ts` — 新增 `CharacterWarningReason` / `CharacterCheckWarning` / `CharacterCheckResponse` / `ScriptUpdateResponse` / `ScriptSaveOutcome`。
4. `src/App.tsx` — `handleUpdateBeats` 增加 `autoUnifyCharacters` 参数、`handleAutoSaveBeats` 固定不归一、新增 `handleCheckCharacters`。
5. `src/components/EpisodeStudioView.tsx` — 保存前预检 + 应用内三按钮确认弹窗。

### 测试与文档（4 个）
6. `backend/tests/test_patch_v416_char_unify_and_s5_cache.py` — 旧断言改写（默认原样保留 / 显式要求才归一）。
7. `backend/tests/test_patch_v420_text_card_and_relation_names.py` — 旧断言改写（同上）。
8. `backend/tests/test_patch_v423_manual_save_char_check.py`（**新建**）— 17 项离线测试。
9. `PATCH_v4.23_MANUAL_SAVE_CHAR_CHECK.md`（本文件）。

### 严禁修改
`backend/src/stages/s4_scriptwriter.py`（Stage 4 的角色名锁定提示词与生成期归一逻辑**保持原样**）、`backend/src/core/schemas.py`、`backend/src/stages/s5_tts.py`、`backend/src/stages/s6_timeline.py`、`backend/pyproject.toml`。

---

## 二、后端实现

### 1. `backend/src/core/character_unify.py` — 只读检查

新增告警数据类：

```python
@dataclass(frozen=True)
class CharacterCheckWarning:
    beat_id: str
    beat_index: int
    field: str            # speaker_role / narration_text / tts_text
    found_name: str
    suggested_canonical: str | None
    reason: str           # alias_variant / homophone_variant / unregistered_speaker
    message: str
    snippet: str

    def to_dict(self) -> dict[str, Any]: ...
```

`CharacterNameCanonicalizer` 新增：

* `inspect_script_beats(beats)` → `list[CharacterCheckWarning]`：逐句检查，**不修改任何文本**；
* `_inspect_speaker_role(...)`：
  * 空串 / 通用说话人标记跳过（`_GENERIC_ROLE_NAMES`：`narrator` / `旁白` / `解说` / `独白` /
    `male` / `male_lead` / `male_antagonist` / `female` / `female_lead` / `female_mature` /
    `unknown` / `众人` / `音效` 等——流水线会把**性别槽位**写进 `speaker_role`，
    它们不是角色本名，拿去比对角色表只会得到误报）；
  * `canonicalize_role(role) != role` → 报 `alias_variant` 或 `homophone_variant`（`are_homophone_name_variants` 判定），给出 `suggested_canonical`；
  * 规范名表非空且该写法不在表里 → 报 `unregistered_speaker`，`suggested_canonical=None`；
* `_inspect_text_field(...)`：按 `text_rules` 扫描 `narration_text` / `tts_text`，
  * 用 `_protected_spans` 排除「小恩小惠」「施以恩惠于」等惯用词搭配；
  * 用 `_overlaps` 做位置去重，同一处只报一次；
* `_context_snippet(text, span, width=12)`：截出错位置前后各 12 字作为上下文。

### 2. `backend/src/web/server.py` — 保存与预检

* `ScriptUpdateRequest` 新增 `auto_unify_characters: bool = False`；
* 新增 `_episode_canonicalizer(series_dir, beats, *, include_role_names_as_candidates=True)`：
  * 归一（`_canonicalize_script_beats`）与只读检查（`_inspect_character_warnings`）**共用同一套口径**
    （作品专属音色 > StoryBible > 本话频次）；
  * **只读检查**用 `include_role_names_as_candidates=False`：不把用户自己写的说话人名字当成新候选名
    ——否则「用户写什么什么就成了规范名」，`unregistered_speaker` 永远判不出来；同时把角色名并进
    正文池，保证「恩慧」这类**同音写法**依然能被认出来；
  * 归一路径保持 Patch v4.16 的构造方式**一字不变**（`auto_unify_characters=true` 时行为零变化）；
* 新增 `_inspect_character_warnings(series_dir, beats)`：只读、失败返回空列表并打 stderr，**不阻断保存**；
* 新增 `_merge_beats_for_check(existing, incoming)`：预检用的宽松合并（草稿字段不全时退化为「只取检查关心的字段」）；
* `update_script` 流程改为：

  ```
  merge → _to_simplified_beat
        → 仅当 payload.auto_unify_characters 为真才 _canonicalize_script_beats
        → 落盘 → 同步 audio_manifest → 写 script_readable.md
        → character_warnings = _inspect_character_warnings(...)
        → 响应 {"script": ..., "job": ..., "character_warnings": [...]}
  ```

* 新增 `POST /api/episodes/{episode_id}/script/check-characters`：
  * 请求体 `{"beats": [...]}`（可省略，省略时检查磁盘上已落盘的 `script.json`）；
  * **只读**：不落盘、不改写；
  * 返回 `{"has_warnings": bool, "warnings": [...]}`；
  * 话次不存在 / 还没有 Stage 4 剧本 → 404。

---

## 三、前端实现

### 1. `src/types/studio.ts`

```ts
export type CharacterWarningReason =
  | 'alias_variant'
  | 'homophone_variant'
  | 'unregistered_speaker';

export interface CharacterCheckWarning {
  beat_id: string;
  beat_index: number;
  field: 'speaker_role' | 'narration_text' | 'tts_text';
  found_name: string;
  suggested_canonical: string | null;
  reason: CharacterWarningReason;
  message: string;
  snippet: string;
}

export interface CharacterCheckResponse {
  has_warnings: boolean;
  warnings: CharacterCheckWarning[];
}

export interface ScriptUpdateResponse {
  script: { beats: ScriptBeat[] };
  job?: unknown;
  character_warnings: CharacterCheckWarning[];
}

export interface ScriptSaveOutcome {
  ok: boolean;
  warnings: CharacterCheckWarning[];
}
```

### 2. `src/App.tsx`

* `handleUpdateBeats(episodeId, beats, triggerFromStage5, autoUnifyCharacters = false)`：
  PUT 时带 `auto_unify_characters`；保存成功后用**后端返回的** `res.script.beats` 更新 `patchEpisode`，
  并把 `res.character_warnings` 透传给调用方。
* `handleAutoSaveBeats(episodeId, beats)`：固定 `auto_unify_characters: false`（大图弹窗自动保存不归一）。
* `handleCheckCharacters(episodeId, beats)`：POST `.../script/check-characters`。

### 3. `src/components/EpisodeStudioView.tsx`

* 新增 `onCheckCharacters?: (episodeId, beats) => Promise<CharacterCheckResponse | null>`；
* 新增内部状态 `charPrompt`（告警 + 待保存的 beats + 触发来源 + `onDone` 回调 + busy 标记）；
* 新增 `checkCharacters(beats)`：预检失败（网络异常等）返回空数组，**不阻断保存**；
* 新增 `persistBeats(beats, triggerFromStage5, autoUnifyCharacters)`：真正落盘；
* 三条入口接预检：
  1. 「保存解说稿」「保存并从 Stage 5 重跑配音」；
  2. 带脏草稿点 S5 / S6 / S7 的「从此步重跑」（保存后再继续重跑）；
  3. 大图弹窗的 `flushSave` **不预检**，只把返回的告警做温和提示；
* 新增应用内确认弹窗（`role="dialog" aria-modal="true" aria-label="角色名写法确认"`），
  逐条列出「第几句（beat_id）/ 字段 / 原因 / 当前写法 → 建议规范名 / 上下文 / 说明」，
  三个按钮：
  * **返回继续修改**——不保存，保留草稿；
  * **仍按我的修改保存**——原样落盘（`auto_unify_characters=false`）；
  * **按建议统一为规范名并保存**——归一后落盘（`auto_unify_characters=true`）。

---

## 四、验收

```bash
cd backend && .venv/bin/python -m pytest -n auto --dist loadfile   # 969 passed
/opt/homebrew/bin/ruff check src tests                              # All checks passed!
cd .. && npx tsc --noEmit && npm run build                          # 无报错，✓ built
```

新增测试 `backend/tests/test_patch_v423_manual_save_char_check.py`（21 项）覆盖：

1. `inspect_script_beats`：同音异写说话人 / 正文别名写法 / 未登记角色 / 旁白与空角色跳过 /
   关系称谓豁免 / 惯用词豁免 / 送音文本也检查 / 性别槽位跳过 / **不修改任何文本**；
2. `POST /script/check-characters`：带 beats 检查、不带 beats 检查磁盘稿、规范写法无告警、
   未知话次 404、**预检不改磁盘**、**服务端路径能报出未登记角色**、**服务端路径能报出别名写法**、
   **性别槽位不误报**；
3. `PUT /script`：默认原样落盘 + 回传 `character_warnings`、`auto_unify_characters=true` 才归一、
   归一时关系泛称仍豁免、**Stage 4 生成期归一不受影响**。
