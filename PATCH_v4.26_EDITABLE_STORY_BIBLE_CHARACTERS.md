# Webtoon2Video 增量补丁规格书（Patch v4.26：跨话记忆库 Layer 1 人物实体图谱可编辑 + 快照与专属音色联动同步）

> **目标**：
> 1. **人物实体图谱可编辑**：「四层跨话记忆库」页的 Layer 1 此前只能新增角色，人物主名、性别、别名、出场话次、外貌特征、最新状态全都要手改 `story_bible.json`；现在每张角色卡片都能行内编辑，改名 / 改字段 / 合并到别的角色 / 删除，全部在页面上完成。
> 2. **专属音色跟着改名走**：角色主名一改，`series_config.yaml` 里 `tts.character_voices` 的同名键就会失配、专属音色静默失效；现在改名会同步迁移音色键。
> 3. **按话快照同步**：按话只读快照（`bible_snapshots/ep_*.json`）是「重跑第 N 话」的基线（防剧透）；改了 live 档案不同步快照，重跑时角色信息会前后不一致。现在保存角色档案会同步历史快照，且严守防跨话剧透。
>
> **铁律**：
> - **防剧透优先**：首现话次晚于快照话次的角色绝不进该快照；快照里的最近出场话次一律钳位到本话以内。
> - **历史状态不倒灌**：用户没改某个角色的「最新状态」时，历史快照里的状态**保持原样**，只对齐别名 / 外貌 / 性别 / 出场话次。
> - **改名必须迁音色**：`tts.character_voices` 旧键平滑迁移到新名；新键已存在时**不覆盖**（用户手工配过的优先）。
> - **删除必须清快照**：被删角色（以及被合并掉的角色）必须从历史快照里一并剔除，否则重跑旧话时它会诈尸。
> - 前端一律用**应用内**交互（行内编辑 / 行内二次确认），**禁止** `window.confirm` / `window.alert`。

---

## 一、文件修改白名单

1. `backend/src/web/server.py` — 角色档案校验、改名/删除同步、按话快照同步、专属音色键迁移。
2. `src/types/studio.ts` — 新增 `CharacterRenameSync`、`CharacterSaveOptions`。
3. `src/App.tsx` — 新增 `handleSaveCharacters`，把改名/删除动作透传给后端。
4. `src/components/MemoryView.tsx` — 角色卡片行内编辑、性别徽标、合并、删除二次确认、收录表单改造。
5. `backend/tests/test_patch_v426_story_bible_edit.py`（**新建**）— 16 项离线测试。
6. `PATCH_v4.26_EDITABLE_STORY_BIBLE_CHARACTERS.md`（本文件）。

---

## 二、后端实现（`backend/src/web/server.py`）

### 1. 请求体扩展

```python
class CharacterRenameSync(BaseModel):
    from_name: str
    to_name: str


class MemoryUpdateRequest(BaseModel):
    ...
    character_renames: list[CharacterRenameSync] = Field(default_factory=list)
    deleted_character_names: list[str] = Field(default_factory=list)
    sync_snapshots: bool = True
```

### 2. 角色校验 `_normalize_characters()`

提交的角色列表统一过一遍校验，失败一律 `400` + 中文提示：

| 规则 | 提示语 |
| :--- | :--- |
| 主名去首尾空白后为空 | `角色主名不能为空` |
| 同一次提交里主名重复 | `角色主名重复：{name}` |
| 别名去重、剔除主名、剔除空串 | — |
| `first_seen_ep < 1` | `角色「{name}」首现话次必须 ≥ 1` |
| `last_seen_ep < first_seen_ep` | `角色「{name}」最近出场话次不能早于首现话次` |

### 3. 专属音色键迁移

复用 v4.25 已有的 `_migrate_character_voice(series_dir, old_key, new_key)`：把 `series_config.yaml` 的 `tts.character_voices[old_key]` 迁到 `new_key`；**新键已存在时不覆盖**，只删旧键。迁移成功后调用 `_reload_config()` 让运行期配置立即生效。

### 4. 按话快照同步 `_sync_bible_snapshots()`

快照 `bible_snapshots/ep_{N}.json` 表示「截至第 N 话结束」的 live 状态（`memory.resolve_base_bible()` 读第 N−1 话快照作基线），因此同步必须严守防剧透：

1. **删除**：被删角色从快照剔除（若该名字是某次改名的源，交给改名分支处理）。
2. **改名 / 合并**：快照里同时存在旧名与新名 → 合并成一个，旧名降为别名，`first_seen_ep` 取小、`last_seen_ep` 取大；只有旧名 → 直接改名。
3. **对齐 live 字段**：别名（live 的 + 合并补充的）、外貌特征、性别、首现话次；最近出场话次取 `min(max(live, 快照原值), 本话)`——**快照是历史记录，不能因为 live 的全局值偏小就把「这一话里出现过」的事实抹掉**，同时钳位防剧透。
4. **防剧透剔除**：`first_seen_ep > 本话` 的角色直接踢出快照。
5. **历史状态保护**：只有「本次确实改了 `current_status`」或「发生改名合并」时才覆盖快照里的状态；否则保留历史状态。
6. **补录**：live 里首现不晚于本话、但快照缺失的角色补录进去（最近出场话次同样钳位）。

---

## 三、前端实现

### 1. `src/types/studio.ts`

```ts
export interface CharacterRenameSync { from_name: string; to_name: string; }
export interface CharacterSaveOptions {
  character_renames?: CharacterRenameSync[];
  deleted_character_names?: string[];
}
```

### 2. `src/App.tsx`

新增 `handleSaveCharacters(seriesId, characters, options)`：把角色列表与改名/删除动作一起 PUT 到 `/series/{id}/memory`，`sync_snapshots` 固定为 `true`；成功后 `saveMemory` 会用后端返回的 bible 合并回 `seriesList`。

### 3. `src/components/MemoryView.tsx`

- **性别徽标**：每张角色卡片显示「男 / 女 / 未标注」徽标。
- **行内编辑**：「编辑」按钮展开行内表单，可改主名 / 性别 / 别名 / 首现话次 / 最近出场话次 / 外貌特征 / 最新状态；`Cmd/Ctrl+Enter` 保存、`Esc` 取消。
- **改名选项**：主名变更后出现「原主名『X』保留为别名」复选框，**默认不勾选**。
- **合并到**：下拉选择另一个角色把当前角色合并过去，「原角色收为别名」复选框**默认不勾选**；合并后原角色退出档案（后端按 `deleted_character_names` 清快照）。
- **删除**：点「删除」进入**行内二次确认**（「确认删除？」+ 删除 / 取消），不用 `window.confirm`。
- **收录表单**：新增性别选择与**独立的角色登场话次输入**（不再复用伏笔的 `threadEp`）；主名与既有角色重复时禁用「收录」并给出提示。
- **切换作品 / 叙事风格更新**：`useEffect` 同步 `styleDraft` 并收起编辑态，避免草稿串台。

---

## 四、测试与验收

### 新增 `backend/tests/test_patch_v426_story_bible_edit.py`（16 项，全离线）

| 分组 | 用例 |
| :--- | :--- |
| 400 校验 | 空名 / 重名 / 首现话次 < 1 / 最近出场早于首现 / 别名去重剔主名 |
| 专属音色 | 改名迁移 `character_voices` 键（其它键不动）/ 新键已存在不覆盖 |
| 快照同步 | 改名同步快照角色名 / 删除剔除快照角色 / 合并保留旧名为别名且首末取并集 / `sync_snapshots=False` 不动快照 |
| 防剧透 | 未来角色不进早期快照 / 最近出场话次钳位 / 缺失角色补录 |
| 状态保护 | 未改状态时历史快照状态不被倒灌（外貌仍对齐）/ 改了状态则跟随更新 |

### 验收命令

```bash
cd backend && ./.venv/bin/python -m pytest -n auto --dist loadfile   # 1004 passed
/opt/homebrew/bin/ruff check src tests                               # All checks passed!
cd .. && npx tsc --noEmit && npm run build                           # ✓ built
```
