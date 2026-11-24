# Webtoon2Video 增量补丁规格书（Patch v4.27：单句音色覆盖 + 角色名写法确认弹窗修复）

> **目标**：
> 1. **单句直接选 MiniMax 音色**：解说审核台的「配音角色与语速」列、分镜大图弹窗（`PanelZoomModal`）都能为**单句**挑一个 MiniMax 音色（`voice_override`），用于一次性 / 龙套角色单独配音；选回「跟随角色」即清除指定。
> 2. **角色名写法确认弹窗不再错乱**：此前明细列表被下面的「档案同步」区压扁到 30px、底部三个操作按钮被挤出屏幕；现在明细与档案同步合并进同一个滚动容器，底栏固定可见。
> 3. **不再误报「未登记的角色」**：`female_antagonist` / `male_mature` 是 Stage 4 写出来的**通用槽位**（不是角色本名）；已经单独指定音色的句子也不需要作品角色表里有这个人。
>
> **铁律**：
> - 单句音色**只改这一句**的 `voice_override`，不动角色、不动语速；改角色仍然会清空单句指定（沿用旧规则），微调 / 重置语速**绝不清空**单句指定。
> - 后端只读检查**只读**：新增的豁免只影响「是否报告」，不改任何落盘文本。
> - 已知角色的别名 / 同音错字提醒**保持不变**——指定音色不掩盖「恩慧 / 恩彗」这类真错字。
> - 弹窗仍是**应用内**确认（禁止 `window.confirm` / `window.alert`），保存进行中不得被关闭。

---

## 一、文件修改白名单

### 后端（2 个）
1. `backend/src/core/character_unify.py` — 通用槽位豁免、单句音色豁免。
2. `backend/src/stages/s5_tts.py` — `_SLOT_GENDER` 补两个通用槽位。

### 前端（2 个）
3. `src/components/EpisodeStudioView.tsx` — 审核台行内「单句音色覆盖」控件、共享试听、弹窗布局修复。
4. `src/components/PanelZoomModal.tsx` — 弹窗内「单句指定 MiniMax 音色（可选）」下拉。

### 测试与文档（2 个）
5. `backend/tests/test_patch_v427_beat_voice_override.py`（**新建**）— 9 项离线测试。
6. `PATCH_v4.27_BEAT_VOICE_OVERRIDE.md`（本文件）。

---

## 二、后端实现

### 1. `backend/src/core/character_unify.py`

**（a）通用槽位不再当成「未登记的角色」**

```python
_GENERIC_ROLE_NAMES: frozenset[str] = frozenset({
    ...,
    "male_antagonist",
    # Patch v4.27：Stage 4 还会写这两个槽位，漏掉就会在保存时误报
    # 「未登记的角色」。
    "male_mature",
    "female_mature",
    "female_antagonist",
    ...
})
```

Stage 4 会把性别槽位（`female_antagonist` 等）写进 `speaker_role`，它们不是角色本名，
比对作品角色表只会误报，直接跳过。

**（b）单句音色豁免**

`_inspect_speaker_role()` 在「准备报 `unregistered_speaker`」这一段里先看单句音色：

```python
if self.canonical_names and simplified not in self.canonical_names:
    # Patch v4.27：这一句已经单独指定了 MiniMax 音色（voice_override），
    # Stage 5 会直接用这个音色合成，不会回退旁白、也不需要作品角色表里有
    # 这个人——一次性 / 龙套角色正是这么用的。这种情况不该再报
    # 「未登记的角色」。
    override = str(getattr(beat, "voice_override", "") or "").strip()
    if override:
        return []
    return [CharacterCheckWarning(... unregistered_speaker ...)]
```

豁免只对「未登记角色」生效；`canonicalize_role(role) != role` 的别名 / 同音分支在更早处
返回，因此「恩慧 → 恩彗」这类真错字**照旧提醒**。

### 2. `backend/src/stages/s5_tts.py`

```python
_SLOT_GENDER: dict[str, str] = {
    ...
    "male_mature": "male",
    "female_antagonist": "female",
}
```

Stage 5 解析角色性别时认这两个槽位，否则会按「未知性别」兜底、挑错音色。

---

## 三、前端实现

### 1. `src/components/EpisodeStudioView.tsx`

**（a）顶层共享音色索引（保护 200~300 分镜的渲染性能）**

```ts
const voiceGroups = useMemo(() => groupVoices(voiceOptions), [voiceOptions]);
const voiceOptionMap = useMemo(() => {
  const map = new Map<string, VoiceOption>();
  voiceOptions.forEach((option) => {
    if (option.voice_id) map.set(option.voice_id, option);
    if (option.id) map.set(option.id, option);
  });
  return map;
}, [voiceOptions]);
const { preview: previewVoiceSample, stop: stopVoiceSample } = useVoicePreview();
```

`useVoicePreview()` 挂在**组件顶层**（不是每一行一个），切换话次与卸载时 `stop()`；
`voiceGroups` / `voiceOptionMap` / 试听回调都以稳定引用传给 `React.memo(ScriptBeatRow)`，
行内不重复计算分组。

**（b）音色名友好显示**

`beatVoiceInfoOf()` 拆成原函数 `beatVoiceInfoBase` + 包装器，多返回一个 `voiceName`
（`voiceOptionMap.get(voice)?.display_name`，取不到就退回音色 ID）。

**（c）行内「单句音色覆盖」控件**

位于「配音角色与语速」列的角色下拉之后、语速行之前：

- 默认项 `跟随角色（{角色音色名}）`（值为空串 = 清除指定）；
- 用 `voiceGroups` 渲染 `<optgroup>` 分组展示全量 MiniMax 音色；
- 当前 `voice_override` 不在清单里时保留兜底 `<option>自定义：{id}</option>`，避免显示空白；
- `✎ 自定义音色 ID…`（`CUSTOM_VOICE`）切到行内输入框，回车 / 失焦提交（内容为空或与旧值相同则不提交），`Esc` 或「←」返回下拉；
- 旁配「试听样例」图标按钮：走共享 `useVoicePreview`，按当前生效语速播放**静态样例**（零 API 费用）；
- 已设置时给「清除」按钮，并高亮「已单独指定：{音色名}」。

**（d）弹窗布局修复（角色名写法确认）**

| 位置 | 改动 |
| --- | --- |
| 弹窗内卡 | 加 `overflow-hidden`，防止内容溢出圆角卡 |
| 顶栏 | 加 `shrink-0`，右上角补 `×` 关闭按钮（`charPrompt.busy` 时禁用） |
| 中部 | 明细列表 **与** 「要不要顺手把作品角色档案一起改掉？」合并进**同一个** `flex-1 min-h-0 overflow-y-auto` 滚动容器（中间用分割线分隔） |
| 底栏 | 加 `shrink-0 bg-white border-t border-slate-100`，三个按钮恒定可见 |

### 2. `src/components/PanelZoomModal.tsx`

- `PanelZoomModalProps` 新增 `onChangeVoiceOverride?: (beatId: string, voiceId: string) => void`（缺省时不渲染该控件）；
- 右侧「本句配音」区块、角色下拉下方新增「单句指定 MiniMax 音色（可选）」下拉
  （`id="zoom-voice-override-select"`、`aria-label="本句指定 MiniMax 音色"`、复用 `groupVoices(voiceOptions)` 分组）；
- 首项 `跟随角色（{角色音色名}）`，不在清单里时保留兜底项；
- 与「试听当前音色」实时联动：音色一改，上方「本句配音：…」与试听按钮立刻跟着变。

---

## 四、测试与验收

### 新增用例（`backend/tests/test_patch_v427_beat_voice_override.py`，9 条，全离线）

| 用例 | 断言 |
| --- | --- |
| `test_voice_override_skips_unregistered_speaker` | 指定音色的未登记角色不报 `unregistered_speaker` |
| `test_voice_override_still_reports_known_character_typo` | 指定音色不掩盖「恩慧 → 恩彗」同音告警 |
| `test_generic_role_slots_do_not_trigger_unregistered_speaker` | `female_antagonist` / `male_mature` 不误报 |
| `test_slot_gender_map_covers_generic_slots` | `_SLOT_GENDER` 认这两个槽位 |
| `test_unregistered_speaker_still_reported_without_override` | 没指定音色的未登记角色照旧报 |
| `test_put_script_saves_voice_override_and_switches_resolved_voice` | 保存后落盘带 `voice_override`，`resolved_voice.source == "voice_override"` 且带 `role_voice` |
| `test_put_script_clearing_voice_override_restores_role_voice` | 回传空串 = 清除，`resolved_voice.source` 恢复按角色解析 |
| `test_put_script_keeps_voice_override_when_not_returned` | 前端没回传时继承旧值，不被顺手抹掉 |
| `test_check_characters_endpoint_skips_unregistered_for_voice_override` | 预检接口对单句音色不再误报 |

### 拦截性验证

`git stash push -- backend/src/core/character_unify.py backend/src/stages/s5_tts.py` 后对旧代码跑新测试：
**4 failed / 5 passed**——失败的正是四条新行为护栏（单句音色豁免、通用槽位豁免、性别槽位表、预检接口豁免），
其余 5 条是「保存 / 清除 / 继承 / 同音照旧提醒」的回归护栏，旧代码本就满足。

### 验收命令与结果

```bash
cd backend && .venv/bin/python -m pytest -n auto --dist loadfile   # 1013 passed in 20.75s
cd backend && /opt/homebrew/bin/ruff check src tests               # All checks passed!
npx tsc --noEmit                                                   # 无错误
npm run build                                                      # ✓ built in 221ms
```

（v4.26 基线 1004 条 → v4.27 1013 条，+9 条新用例。）

### 实测数据

`GET /api/tts/voices` → `minimax_voices` 共 **28** 条（female 12 / male 11 / narrator 5），
示例 `voice_id = Chinese (Mandarin)_Radio_Host` / `display_name = 电台男主播` / `sample_ready = true`。
