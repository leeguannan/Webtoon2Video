# Webtoon2Video 增量补丁规格书（Patch v4.17：配置热重载同步后台调度器 + Stage 4 降级链纳入百炼 DeepSeek）

> **基线提交**：`78e4894`（Patch v4.16 之后）
> **落地提交**：
> - `7d0c1f6` `fix(config): 配置热重载同步后台调度器，Stage 4 降级链纳入百炼 DeepSeek`（代码与测试）
> - `7e814d0` `chore(docs): 同步后端用例数（880 条）与分级跑法说明`（文档数字订正）
>
> **目标**：
> 1. **热重载要同步**：在界面保存配置后，下一个后台任务**立刻**用上新模型 —— 不再需要重启后端才能让配置生效。
> 2. **MODEL_FALLBACK_CHAIN 要包含 deepseek**：百炼 DeepSeek 置于降级链首，未开通该模型权限时按链有序退回 qwen 系。
> 3. **README / AGENTS 一并提交**：文档里的用例数必须与代码里的真实用例数一致。
>
> **铁律**：
> - 本补丁**不改任何前端文件**（设置页的降级提示文案对「主模型 = DeepSeek」场景已准确，无需改）。
> - 提交 / 推送必须由用户显式下令。

---

## 一、问题复盘（为什么界面改了配置后台不生效）

排查 `wt_3924/ep_001` 与 `wt_5081/ep_003` 两次撰稿时确认：用户在百炼后台看到 `qwen3.8-max` 的调用，而界面配置里写的是 `deepseek-v4.1-flash-1017`。逐项核对后确认**不是降级产物，而是「后台用的是后端启动那一刻的模型」**。

### 证据链

| # | 事实 | 位置 |
| :--- | :--- | :--- |
| 1 | `JobRunner` 在 `create_app` 时用当前配置构造，并把配置**存成实例属性** | `server.py:1481-1484` → `job_runner.py:207` `self.config = config`（全仓唯一赋值） |
| 2 | 每次后台任务取模型，读的都是这份实例属性 | `job_runner.py:399-405`：`series_config.model_overrides.get("script", self.config.models.script)` 等 |
| 3 | `PUT /api/config` 的热重载**只**更新 `app.state.config` | `server.py:1505-1530` `_reload_config()` |
| 4 | 全仓 grep `runner.config` 命中 0 —— 没有任何一处把新配置同步进 `JobRunner`（`view.config` 同样没同步） | — |
| 5 | 界面回显走 `_config_payload(updated)`，读的是**新**配置 → 界面显示新模型、后台跑旧模型 | `server.py:2606-2665` |
| 6 | 两份后端日志中「告警：… 模型 … 不可用 … 自动回退到」命中 **0 次** → 全程没有发生过任何降级 | 旧 job `bash-85bb7f5a4ede4fbf` / 新 job `bash-7afaf681148a47d9` 日志 |
| 7 | 用户保存配置的时刻**晚于**那两次撰稿（`backend/config.yaml` mtime 11-21 09:36，脚本 JSON 写于 11-20 22:41 与 11-21 01:39） | 文件系统时间戳 |

**结论**：`PUT /api/config` 的热重载只更新了「给人看的」那份状态，没更新「给后台任务用的」那份。

---

## 二、文件修改白名单

### 后端（2 个）
1. `backend/src/web/server.py` — `_reload_config()` 同步 `app.state.runner.config` 与 `app.state.view.config`。
2. `backend/src/stages/s4_scriptwriter.py` — `MODEL_FALLBACK_CHAIN` 链首加入 `deepseek-v4.1-flash-1017`。

### 测试（2 个）
3. `backend/tests/test_patch_v417_config_hot_reload_and_fallback.py`（**新建**，157 行 / 4 项）。
4. `backend/tests/test_patch_v33_voices_and_models.py` — **连带修改**：`test_fallback_candidates_follow_documented_chain` 硬断言了链的完整内容，链变更必然要同步该断言（原白名单未列，属需求必然的连带改动）。

### 文档（3 个）
5. `AGENTS.md` — 用例数 859 → **880**，快档条数说明同步。
6. `README.md` — 目录树注释与快速开始里的 pytest 条数同步。
7. `backend/README.md` — 「测试跑法（分级）」表格里的总数 / 快档 / 全量三处数字同步。

### 严禁修改
任何前端文件（`src/**`）、`backend/src/core/config.py`、`backend/src/providers/**`、`backend/src/stages/s1_crawler.py` / `s2_segmenter.py` / `s3_vision.py` / `s3b_scenes.py` / `s6_timeline.py` / `s7_renderer.py` / `s5_tts.py`。

---

## 三、后端实现

### 1. `backend/src/web/server.py` — `_reload_config()` 同步三处

```python
def _reload_config() -> AppConfig:
    """重新加载配置。

    Patch v4.17：此前这里只更新 ``app.state.config``，而 ``JobRunner`` 在进程
    启动时把配置拍了一份快照（``self.config``），后台任务每次取模型都读那份
    快照 —— 于是出现「界面显示新模型、后台仍用旧模型」，改完配置必须重启
    后端才生效。这里把新配置同步给调度器与工作区视图，保存即生效。
    """
    ...
    app.state.config = updated
    # Patch v4.17：同步给后台调度器（模型选择）与工作区视图（音色/渲染设置）。
    app.state.runner.config = updated
    app.state.view.config = updated
    return updated
```

- `app.state.runner` / `app.state.view` 在 `create_app` 里早已挂载（`server.py:1495-1496`），无需新增状态。
- `WorkspaceView.config` 用于音色 / 渲染设置回显，同步后一并保持最新。
- `JobRunner.config` 是**唯一**需要同步的对象：它没有其它派生缓存，替换整个配置对象即可，不会残留旧模型。

### 2. `backend/src/stages/s4_scriptwriter.py` — 降级链

```python
#: Patch v4.17：把百炼 DeepSeek（deepseek-v4.1-flash-1017）放到链首——
#: 它是控制台可选项里的高性价比主力，账号未开通该模型权限时按本链有序退回
#: qwen4-max → qwen3.8-max → qwen3.7-plus → qwen-plus；链中其余模型的行为不变
#: （主模型是 qwen 系时，候选依旧只取它之后的那些）。
MODEL_FALLBACK_CHAIN: tuple[str, ...] = (
    "deepseek-v4.1-flash-1017",
    "qwen4-max",
    "qwen3.8-max",
    "qwen3.7-plus",
    "qwen-plus",
)
```

候选语义（`_fallback_candidates`，`:150`）：

| 主模型 | 候选 |
| :--- | :--- |
| `deepseek-v4.1-flash-1017` | `qwen4-max` → `qwen3.8-max` → `qwen3.7-plus` → `qwen-plus` |
| `qwen4-max` | `qwen3.8-max` → `qwen3.7-plus` → `qwen-plus` |
| `qwen-plus` | （空，已是链尾） |
| 不在链里的自定义模型 | 整条链 |

降级触发条件不变：`is_model_access_error(exc)` 只认 `LLMError`（`LLMRequestError` 是其子类，故百炼 404 / 403 可触发降级），错误特征见 `_MODEL_ACCESS_ERROR_MARKERS`（modelnotfound / accessdenied / invalidparameter / permission denied / not found / no access / 404 / 403 / 未开通 / 无权限 / 没有权限）。降级时向 stderr 打：

```
[s4_scriptwriter] 告警：{label} 模型 {current} 不可用（{type}：{exc}），自动回退到 {next_model}
```

**与前端文案的一致性**：设置页提示「账号未开通该模型时自动降级：qwen4-max → qwen3.8-max → qwen3.7-plus → qwen-plus」在主模型为 DeepSeek 时同样成立（DeepSeek 之后的候选正是这四个），故本补丁**无需改前端**。

---

## 四、测试覆盖

`backend/tests/test_patch_v417_config_hot_reload_and_fallback.py`（**新建**，157 行 / 4 项，全部离线）：

| 用例 | 断言要点 |
| :--- | :--- |
| `test_put_config_hot_reloads_runner_and_view` | `PUT /api/config` 后，`runner.config.models.script` / `runner.config.models.story_model`、`app.state.config`、`app.state.view.config` **全部**为 `deepseek-v4.1-flash-1017`；`config.yaml` 落盘 |
| `test_hot_reloaded_model_is_used_by_next_background_run` | monkeypatch `runner._run_stage` 捕获 kwargs，`await runner._run_episode(series_id, 1, from_stage="s4_scriptwriter", mode="full_video")`，断言**后台任务实际拿到的** `script_model` / `story_model` 是热重载后的新值 |
| `test_fallback_chain_starts_with_deepseek` | 链首是 deepseek；三种入参的候选列表符合上表 |
| `test_fallback_degrades_from_deepseek_to_qwen4_max` | DeepSeek 抛 `LLMRequestError("百炼返回 404：ModelNotFound：The model deepseek-v4.1-flash-1017 does not exist")` → 降级到 `qwen4-max` 成功；断言调用序列 `[deepseek, qwen4-max]` |

配套修改 `backend/tests/test_patch_v33_voices_and_models.py::test_fallback_candidates_follow_documented_chain`：链断言加入 deepseek 首项，并补 `_fallback_candidates("deepseek-v4.1-flash-1017")` 断言。

---

## 五、验收命令与结果

```bash
cd backend
.venv/bin/python -m pytest tests/test_patch_v417_config_hot_reload_and_fallback.py -q   # 4 passed
.venv/bin/python -m pytest -q                                                          # 880 条全绿
.venv/bin/python -m pytest -n auto -m "not slow" -q                                    # 快档 11.086s 全绿
.venv/bin/python -m pytest --collect-only                                              # 880 collected（slow 22 / 快档 858）
/opt/homebrew/bin/ruff check src tests                                                 # All checks passed!
cd .. && npx tsc --noEmit && npm run build                                              # TSC_OK + 构建通过
```

> 首轮全量跑挂在 `test_patch_v33` 的链断言上（链内容变了），同步该断言后复跑全绿。

### 生效验证（重启后端）

```
GET /api/health              → {"ok": true, ...}
GET /openapi.json            → 37 paths（含 GET /api/episodes/{episode_id}/audio/{beat_id}
                               与 POST /api/episodes/{episode_id}/beats/{beat_id}/regenerate-tts）
GET /api/config              → visionModel=qwen3-vl-plus
                                scriptModel=storyModel=deepseek-v4.1-flash-1017
                                ttsModel=speech-2.8-hd
                                scriptModelOptions 含 deepseek、storyModelOptions 含 deepseek
离线只读复核                → chain=('deepseek-v4.1-flash-1017','qwen4-max','qwen3.8-max','qwen3.7-plus','qwen-plus')
                                candidates(deepseek)=['qwen4-max','qwen3.8-max','qwen3.7-plus','qwen-plus']
```

---

## 六、已知边界

1. **作品级覆盖优先于全局**：`series_config.model_overrides` 仍然优先于全局配置（`job_runner.py:399-405`），若某个作品单独设了 `script` / `story` / `vision`，改全局配置对它无效 —— 这是既有语义，本补丁未改。
2. **`PUT /api/config` 会重写 `config.yaml`**：写盘走 `yaml.safe_dump`，**文件里的注释不会被保留**（`backend/config.example.yaml` 的注释不受影响，那是模板文件）。这是既有行为。
3. **降级是静默的（但有日志）**：DeepSeek 未开通权限时，任务会成功但实际用的是 qwen 模型，只有 stderr 的「告警：… 不可用 … 自动回退到 …」能看出来；百炼后台也会看到 qwen 的调用记录。
4. **重启仍会让配置回到文件内容**：`_reload_config()` 只在 `PUT /api/config` 时触发；进程重启时依然从 `config.yaml` 读取（后端 cwd 为 `backend/`，命中 `backend/config.yaml`）。
