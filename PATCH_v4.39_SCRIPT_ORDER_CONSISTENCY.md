# PATCH v4.39 — 剧本顺序一致性（成片不再「回头看」前面的画面）

> 基线：`607eff5`（PATCH v4.38）｜分支：`main`｜日期：2026-11-25

---

## 1. 用户报障与取证结论

**用户原话**（m04229）：

> 最新做的视频图中的画面和配音不应该出现在这里啊，应该在 b_new_073 的后面，b_new_072 的前面才对啊！！！请帮我排查问题

附图为 `wt_5081_ep_007.mp4` 03:15 处画面（女孩躺在床上，漫画气泡「那时因為距離事件發生已經過了一段時間…但是從現在開始調查的話還有機會。」）。

### 1.1 三处顺序完全一致 → 成片顺序 = 剧本数组顺序

| 文件 | 句数 | 顺序 |
| --- | --- | --- |
| `04_script/script.json` → `beats[]` | 67 | 落盘原序 |
| `06_plan/timeline.json` → `scenes[]` | 67 | 与 script.json **逐条一致** |
| `05_audio/audio_manifest.json` → `clips[]` | 67 | 与 script.json **逐条一致** |

- Stage 5 不重排（`backend/src/stages/s5_tts.py:1490` 注释「顺序与剧本 beats 一致」）；
- 写回层 `_merge_script_beats`（`backend/src/web/server.py:680`）保序不重排。

**结论：成片的画面/配音顺序 = `script.json` 的 `beats` 数组顺序。** 只要剧本数组顺序错了，成片必然错。

### 1.2 落盘原序里存在唯一一处「画面回退」

```
idx27  b27        p_026   (sc_002)
idx28  b_new_073  p_026   (sc_002)   「在那之后的2年…警方没找到线索」
idx29  b_new_072  p_028   (scene_id=None) 「第二天雨夜的便利店。」   ← 属于 sc_003
idx30  b30 … b36  p_029 … p_038                                  ← sc_003
idx39  b43        p_027   (sc_002)  「那时是因为距离事件发生已经过了一段时间…」  ★ 回退 p_038 → p_027
idx40  b44        p_051
```

- `storyboard.json`（11-25 10:24 用户手工改过编排）：`sc_001=p_001..p_012`、`sc_002=p_013..p_027`、`sc_003=p_028..p_039`、`sc_004=p_042..p_059`、`sc_005=p_060..p_081`（全部 enabled）。
- `story_draft.json` 的 **CH3**（`panel_hint_range=[p_023,p_028]`）正文结尾原文含 `b43` 那句 → **`b43` 语义上属于 CH3（sc_002）**，理应排在 `b_new_073` 之后、`b_new_072` 之前。

### 1.3 按「幕分组展平」看，顺序完全正确

```
b27 → b_new_073 → b43 → b_new_072 → b30
```

**与用户期望逐字一致。** 也就是说：画面本身没错，**位置错了** —— `b43` 被挂到了 sc_003 的句子中间。

### 1.4 根因链

| # | 位置 | 问题 |
| --- | --- | --- |
| R1 | Stage 4 逐幕写作 | 4B 一章一章生成，合并进全话数组时**按章顺序追加**，但模型给出的句子归属幕与追加顺序不一致时，A 幕的句子会落在 B 幕句子中间 |
| R2 | `_clamp_regressive_beats`（`s4_scriptwriter.py:3319`） | 本幕锚点取「本幕内 `index <= last_index` 的最后一格」——**那一格本身仍是回看格**，钳制形同虚设（v4.29「锚点留在本幕内」改动引入的回归；v4.29 前锚点是「当前主画面」，合法） |
| R3 | 钳制后仍异常只打日志 | `s4_scriptwriter.py:3913-3922` 校验仍不通过时**只 print 到 stderr，不阻止落盘** → 静默产出错序剧本 |
| R4 | 审核台界面 ≠ 落盘顺序 | `EpisodeStudioView.tsx:1861-1892` `beatSceneGroups` 按幕分组渲染，`:2819` `flushSave` 提交的却是 `draftBeatsRef.current`（**原序**）→ 用户在界面上看到的顺序与真正落盘的顺序不是一回事 |

---

## 2. 铁律

1. **幕序 = 成片播放顺序的唯一权威口径**：「幕顺序 × 幕内自然序」，不按分镜号自然序、不按落盘原序。
2. **写回层必须自愈**：任何写回路径（保存 / auto-pacing / auto-motions）落盘前，按幕重排一次；**不改动 `beat_id`**（用户手加的 `b_new_*` 编号必须原样保留）。
3. **钳制锚点不得早于当前进度**：锚点必须满足 `index >= last_index`；本幕内取「还没讲到（或正好讲到）的第一格」，本幕已讲完才退回当前主画面。
4. **顺序纠正不得越权**：只动 `target_panel_ids` 与数组顺序，**字幕文本（`narration_text`）、送音文本（`tts_text`）、逐句音色（`voice_override`）、停顿（`pause_after_ms`）一律不动**。

---

## 3. 文件白名单

| 文件 | 改动 | 说明 |
| --- | --- | --- |
| `backend/src/core/panel_scope.py` | 改 | canonical owner：新增 `scene_order` / `ordered_by_scene()` / `panel_owner_map()` / 模块级 `order_beats_by_scene()` |
| `backend/src/stages/s4_scriptwriter.py` | 改 | S4 出口按幕重排 + 锚点策略重写 |
| `backend/src/web/server.py` | 改 | 写回层顺位检测 + 按幕重排 + 回传 `order_violations` |
| `src/types/studio.ts` | 改 | 新增 `OrderViolation` 类型 |
| `src/App.tsx` | 改 | 透传 `order_violations` |
| `src/components/EpisodeStudioView.tsx` | 改 | 前端同口径重排 + 顺位告警 |
| `backend/tests/test_patch_v439_script_order_consistency.py` | 新增 | 16 例回归测试 |
| `PATCH_v4.39_SCRIPT_ORDER_CONSISTENCY.md` | 新增 | 本文档 |

**禁改**：`core/normalize.py`、`stages/s5_tts.py`、`stages/s6_timeline.py`、`stages/s7_renderer.py`、`core/schemas.py`（本补丁不需要动它们的既有语义）。

---

## 4. 后端实现

### 4.1 `core/panel_scope.py` —— 唯一权威口径

```python
@dataclass(frozen=True)
class PanelScope:
    ...
    scene_order: tuple[str, ...] = ()          # 启用幕的先后顺序

    def ordered_by_scene(self) -> list[str]:
        """成片逻辑播放顺序 = 幕顺序 × 幕内自然序。"""
        if not self.scene_order:
            return self.ordered_allowed()
        ordered: list[str] = []
        for scene_id in self.scene_order:
            ordered.extend(self.scene_panels(scene_id))
        return ordered

    def panel_owner_map(self) -> dict[str, str]:
        """分镜 → 所属幕 反查表。"""
```

- `scope_from_storyboard` 按 `storyboard.scenes` 原顺序、只取 `enabled` 的幕填充 `scene_order`。
- 新增模块级共享函数（前后端同口径的落点）：

```python
def order_beats_by_scene(
    beats: Sequence[Any],
    *,
    scene_order: Sequence[str],
    panel_owner: Mapping[str, str],
) -> list[Any]:
    """按幕归属重排解说句：幕内保序、认不出的置末、幂等。"""
```

- 句子归属幕的判定顺序：① `beat.scene_id`；② 缺失时按**第一张目标分镜**反查 `panel_owner`；③ 仍认不出 → 孤儿，置末。

### 4.2 `stages/s4_scriptwriter.py`

| 新增 | 说明 |
| --- | --- |
| `_order_beats_by_scene(beats, scenes)` | 薄封装：按 `scene_index` 排序取 `scene_order` + `panel_owner`，再调共享函数 |
| `_renumber_beats(beats)` | 按当前顺序重编 `beat_id` 为 `b1..bN`（S4 出口铁律） |

**重排插入三处**（保证「校验 → 落盘 → 动效」三步看到的是同一份顺序）：

1. 重试循环内 `errors = chapter_errors` 之后、`validate_beat_alignment` 之前；
2. 补写流程内 `apply_dialogue_speaker_inheritance` 之后（替换原 `for/enumerate` 编号块）；
3. 落盘 payload 前、`apply_auto_motions` 之前（保证 motion report 的 `beat_id` 与最终一致）。

**`_clamp_regressive_beats` 锚点策略重写**（R2 的根治）：

```python
forward = [pid for pid in scene_panels if index_of[pid] >= last_index]
if forward:
    anchor_id = min(forward, key=lambda pid: index_of[pid])   # 本幕还没讲到的第一格
else:
    anchor_id = fallback_anchor                                # 本幕已讲完 → 当前主画面
...
last_index = max(last_index, index_of.get(anchor_id, last_index))  # 进度随锚点推进
```

> 与旧实现的差别：旧实现取本幕内 `index <= last_index` 的**最后一格**，那一格本身就是回看画面 → 钳制等于没做。新实现取 `index >= last_index` 的**第一格**，必然不早于进度。

### 4.3 `web/server.py`

```python
def _detect_order_violations(beats, scope) -> list[dict[str, Any]]:
    """按 scope.ordered_by_scene() 口径检测「画面回退」。"""
    # 返回 beat_id / previous_beat_id / panel_ids / message

def _reorder_beats_by_scene(beats, scope) -> list[ScriptBeat]:
    """按幕重排（不改 beat_id）。"""
```

`PUT /api/episodes/{id}/script` 流程：

```
clamp（v4.32 范围收口）
  → _detect_order_violations
  → 有违规则 _reorder_beats_by_scene 重排
  → 再检测：只有违规数变少才采纳这次重排
  → 落盘 + 响应附带 order_violations
```

`POST /script/auto-pacing` 与 `POST /auto-motions` 写回前同样按 `scope.scene_order` + `panel_owner_map()` 重排。

> **采纳条件「违规数变少才采纳」**：避免重排本身把顺序改坏（重排只按幕归属分组，幕内相对顺序不动，理论上只会变好；但仍加这道保险）。

---

## 5. 前端实现

| 文件 | 改动 |
| --- | --- |
| `src/types/studio.ts` | 新增 `OrderViolation`（`beat_id` / `previous_beat_id` / `panel_ids` / `message`）；`ScriptUpdateResponse`、`ScriptSaveOutcome` 增加可选 `order_violations?: OrderViolation[]` |
| `src/App.tsx` | `handleUpdateBeats` / `handleAutoSaveBeats` 返回值带上 `order_violations` |
| `src/components/EpisodeStudioView.tsx` | ① 新增模块级 `orderBeatsByScene(beats, scenes)`（与后端同口径：启用幕过滤 → panel→scene 反查 → 孤儿置末 → 幂等）；② 新增 useEffect（依赖 `[sceneStoryboard, episode.episode_id, episode.beats]`）：镜头编排到位后按幕重排草稿（**用户有未保存改动时跳过**，不覆盖正在编辑的内容）；③ `flushSave` 提交前按幕重排；④ 响应里仍有 `order_violations` 时提示「已保存，但仍有 N 处画面顺序回退（成片会回头看前面的画面），请改一下该句对应的画面。」 |

> **前端重排 + 后端重排双保险**：前端保证「界面所见 = 提交内容」，后端保证「落盘内容 = 成片顺序」。任何一端被绕过，另一端仍然自愈。

---

## 6. 测试

### 6.1 新增 `backend/tests/test_patch_v439_script_order_consistency.py`（16 例，全离线）

| # | 用例 | 覆盖点 |
| --- | --- | --- |
| 1 | `test_ordered_by_scene_follows_scene_order_then_panel_order` | 幕序 × 幕内自然序；幕内顺序被打乱时仍按自然序 |
| 2 | `test_ordered_by_scene_falls_back_to_natural_order_without_storyboard` | 无编排 → 退回自然序（旧行为不变） |
| 3 | `test_order_beats_by_scene_moves_scene_sentences_back` | **核心**：A 幕句子落在 B 幕中间 → 重排回 A 幕末尾 |
| 4 | `test_order_beats_by_scene_infers_scene_from_first_panel` | 无 `scene_id` → 按第一张目标分镜反查 |
| 5 | `test_order_beats_by_scene_keeps_orphans_last_and_is_idempotent` | 孤儿置末 + 幂等 |
| 6 | `test_order_beats_by_scene_without_scenes_returns_input` | 无幕信息 → 原样返回 |
| 7 | `test_s4_order_beats_by_scene_sorts_by_scene_index` | S4 薄封装按 `scene_index` 排序 |
| 8 | `test_clamp_regressive_beats_anchor_never_earlier_than_progress` | 本幕已讲完 → 锚当前主画面（**R2 回归**） |
| 9 | `test_clamp_regressive_beats_advances_progress_to_forward_anchor` | forward 锚点 + 进度推进 |
| 10 | `test_clamp_regressive_beats_keeps_legal_beat_untouched` | 合规句一字不动 |
| 11 | `test_detect_order_violations_finds_backward_panel` | 违规检测 + 人话 message |
| 12 | `test_detect_order_violations_empty_when_scope_missing` | 无编排 → 不判断 |
| 13 | `test_reorder_beats_by_scene_keeps_beat_ids` | 重排不改 `beat_id`（`b_new_*` 保留） |
| 14 | `test_put_script_reorders_beats_by_scene` | **API**：落盘顺序 = `b1, b_new_073, b2, b_new_072`，违规清零 |
| 15 | `test_put_script_keeps_voice_override_and_pause` | 不破坏 `voice_override` / `pause_after_ms` |
| 16 | `test_put_script_reports_order_violations_key` | 响应含 `order_violations` 字段 |

### 6.2 真实数据自测（`/tmp/v439_check.py`，读 `workspace/wt_5081/ep_007`）

```
scope allowed=75   scenes=('sc_001'..'sc_005')
原序回退   = [('b43', ['p_027'], 'b36')]
重排后回退 = []
重排后顺序 = b27(27) → b_new_073(28) → b43(29) → b_new_072(30) → b30(31)
幂等       = True
```

**与用户期望逐字一致。**

### 6.3 真机数据修复验证（`workspace/wt_5081/episodes/ep_007`）

**重排**（`PUT /api/episodes/wt_5081_ep_007/script` 走新写回层）：

```
server 日志：剧本顺序存在画面回退，已按镜头编排的幕顺序重排：剩余 0 处
响应：order_violations=[]   panel_scope_violations=[]
落盘顺序：idx29=b_new_073 → idx30=b43 → idx31=b_new_072   ← 与用户期望逐字一致
重排后画面回退：[]   （重排前：[('b43', ['p_027'], 'b36')]）
```

**重跑 S5→S6→S7**（`POST /rerun {from_stage: "s5_tts", mode: "full_video"}`）：

- S5 全部命中 `05_audio/_cache/`（74 个缓存文件，文本未改 → **零 TTS 消耗**）；
- S6 日志：`镜头编排范围生效：允许 75 格分镜`；剔除超扁窄条面板 8 格；
- S7 母带：`-14.0 LUFS（实测 -15.3 LUFS，修正 +0.16dB）`；
- 新成片 `07_out/wt_5081_ep_007.mp4` = 110,147,754 bytes。

**成片抽帧对比**（旧片已备份至 `/tmp/wt_5081_ep_007.old.mp4`）：

| | 旧成片 | 新成片 |
| --- | --- | --- |
| `panel_id` 序列 | **非单调**（idx29 直接跳到 p_028，跳过 p_027） | **单调不减，逆序对 0** |
| b43 的位置 | idx39（p_038 → p_027 回退） | **idx29**（p_027），前一句 p_026、后一句 p_028 |
| b43 的时间 | 03:15（195s）← 用户报错的那一帧 | **142.8s**（137.7s~147.9s） |
| 195s 处画面 | 女孩躺床上 +「那时是因為距離事件發生已經過了一段時間…」 | 「"怎麼會這麼巧？朴敏赫竟然出現在這裡……？"」← 已是该位置该有的画面 |

新成片 142.8s 抽帧确认：画面 = p_027（女孩躺床上），字幕 =「那时是因为距离事件发生已经过了一段时间，所以很难找到证据。但是从现在开始调查的话还有机会。」—— **画面与配音严丝合缝，且位置在 `b_new_073` 之后、`b_new_072` 之前**。

### 6.4 验证命令与结果

| 命令 | 结果 |
| --- | --- |
| `cd backend && .venv/bin/python -m pytest tests/test_patch_v439_script_order_consistency.py -q` | **16 passed** |
| `cd backend && .venv/bin/python -m pytest -n auto --dist loadfile -q` | **全量通过** |
| `cd backend && /opt/homebrew/bin/ruff check src tests` | **All checks passed!** |
| `npx tsc --noEmit` | **零错误** |
| `npm run build` | **✓ built in 198ms** |
| `pytest tests/test_patch_v432_storyboard_scope_enforcement.py tests/test_patch_v429_storyboard_s4_alignment.py tests/test_s4_scriptwriter.py -q` | **79 passed**（v4.32 / v4.29 / S4 旧行为零回归） |

---

## 7. 已知限制

1. **只纠正「幕归属」层面的错位**：幕内两句的相对先后（同一格里换画面）不在纠正范围内——那属于叙事顺序，本补丁不动。
2. **孤儿句（分镜不在任何启用幕里）**：按原序置末，不删；它们仍会被 v4.32 的范围收口剔掉越界分镜。
3. **已落盘的旧剧本不会自动重排**：重排发生在写回时。要修正历史话次，需要在审核台点一次「保存草稿」（或直接调用写回接口）。
4. **`b_new_*` 编号不重排**：用户手加句的编号保持原样，因此落盘 `beat_id` 仍可能有空洞——这是有意的（编号连续化只发生在 Stage 4 出口）。

---

## 8. 后续动作

1. **数据修复已完成**：`workspace/wt_5081/episodes/ep_007` 已按幕重排并重跑 S5→S6→S7（见 §6.3），旧成片备份在 `/tmp/wt_5081_ep_007.old.mp4`、旧时间线备份在 `/tmp/wt_5081_ep_007.old.timeline.json`，剧本原文件备份在 `04_script/script.json.bak-v439`。
2. **其他历史话次**：需要同样修正的话次，在审核台点一次「保存草稿」即可（写回层会自动按幕重排）。
3. **提交推送**：待用户显式下令。
