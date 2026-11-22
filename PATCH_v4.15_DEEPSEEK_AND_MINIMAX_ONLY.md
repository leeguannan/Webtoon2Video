# Webtoon2Video 增量补丁规格书（Patch v4.15：接入百炼 DeepSeek + 音色选择仅保留 MiniMax）

> **基线提交**：`cb9adc2`（v4.13 / v4.14 代码评审修复之后）
> **落地提交**：`ba31f48` `feat(model): 接入百炼 deepseek-v4.1-flash-1017，音色选择仅保留 MiniMax`（13 文件 +679/-28）
>
> **目标**：
> 1. **接入百炼 DeepSeek**：Stage 4 的「解说撰稿（4B）」与「说书体（4A）」模型下拉新增 `deepseek-v4.1-flash-1017`，可与 qwen 系同台使用；DeepSeek 系模型不接受 `enable_thinking` 参数，需在百炼客户端层**原地自愈**，不能被上层当成模型失败而静默降级。
> 2. **音色选择仅保留 MiniMax**：前端所有音色下拉（设置页、作品 TTS 面板、审核台）只展示 MiniMax Speech 音色；百炼 / 阿里系（qwen）音色的剔除统一收口在音色选择层，后端接口契约保持不变。
>
> **铁律**：
> - 严禁修改 `backend/src/core/config.py` 的 `ModelConfig.story_model`（保持 `"qwen4-max"`）与 `ModelConfig.script`（保持 `"qwen3.7-plus"`）默认值——本补丁只扩展**下拉可选项**，默认值是既有契约。
> - 严禁修改 `backend/config.example.yaml` 的任何默认键值（只允许补注释说明新模型）。
> - 严禁修改 `backend/src/stages/s4_scriptwriter.py` 的 `**_NO_THINKING_KWARGS,` 与 `MODEL_FALLBACK_CHAIN`（降级链的调整属于另一个补丁，见 `PATCH_v4.17_CONFIG_HOT_RELOAD_AND_FALLBACK.md`）。
> - 严禁修改 `backend/src/providers/tts_qwen.py` 的 `QWEN3_TTS_VOICES` 与后端 `/api/config` 的 `ttsVoiceOptions`（`test_patch_v33` / `test_patch_v34` / `test_web_server` 对其有强断言）。
> - 严禁改动 S2 / S3 / S3b / S7 / TTS 合成主链路。
> - 提交 / 推送必须由用户显式下令。

---

## 一、文件修改白名单

### 后端（4 个）
1. `backend/src/web/server.py` — `STORY_MODEL_OPTIONS` / `SCRIPT_MODEL_OPTIONS` 扩展 deepseek 选项与说明注释。
2. `backend/src/providers/llm_bailian.py` — 新增 `_should_retry_without_thinking()` 与「剥离 `enable_thinking` 后原地重试一次」的自愈分支。
3. `backend/src/providers/llm_base.py` — `extract_json_text()` 先剥离 `<think>…</think>` 再提取 JSON 围栏。
4. `backend/config.example.yaml` — 仅在 `models.script` / `models.story_model` 上方补一行 deepseek 注释（默认值未动）。

### 前端（4 个）
5. `src/components/ConfigDictView.tsx` — 模型下拉选项 / 标签 / 降级提示文案；音色下拉只取 `minimaxVoiceOptions`。
6. `src/components/SeriesTtsPanel.tsx` — 音色下拉只取 `minimax_voices`。
7. `src/components/EpisodeStudioView.tsx` — 音色下拉只取 `minimax_voices`。
8. `src/components/VoicePicker.tsx` — `PROVIDER_LABELS` / `PROVIDER_ORDER` 只保留 minimax；`groupVoices` 入口过滤非 minimax 条目。

### 测试与文档（2 个）
9. `backend/tests/test_patch_v415_deepseek_and_voices.py`（**新建**，6 项）— 模型选项、热重试、不可重试场景、`<think>` 剥离。
10. `PATCH_v4.15_DEEPSEEK_AND_MINIMAX_ONLY.md`（本文件）。

### 严禁修改
`backend/src/core/config.py`、`backend/src/stages/s1_crawler.py`、`s2_segmenter.py`、`s3_vision.py`、`s3b_scenes.py`、`s6_timeline.py`、`s7_renderer.py`、`s5_tts.py`、`backend/src/providers/tts_qwen.py`、`backend/src/providers/tts_minimax.py`、`backend/src/stages/s4_scriptwriter.py`。

---

## 二、后端实现

### 1. `backend/src/web/server.py` — 模型下拉扩展

```python
#: Stage 4 模型下拉推荐项（Patch v3.3 Patch 15：新增 qwen4-max / qwen3.7-plus）。
#: Patch v4.15：新增 deepseek-v4.1-flash-1017（百炼 · DeepSeek 高性价比），
#: 需先在百炼控制台开通该模型的调用权限，未开通时按 MODEL_FALLBACK_CHAIN 降级。
STORY_MODEL_OPTIONS: tuple[str, ...] = (
    "qwen4-max",
    "qwen3.8-max",
    "qwen3.7-plus",
    "qwen-plus",
    "deepseek-v4.1-flash-1017",
)
SCRIPT_MODEL_OPTIONS: tuple[str, ...] = (
    "qwen3.7-plus",
    "qwen-plus",
    "deepseek-v4.1-flash-1017",
    "qwen3.8-max",
)
```

- 说书体（4A）下拉第 1 项仍是 `qwen4-max`（既有默认不变，deepseek 追加在末位）。
- 解说撰稿（4B）下拉第 1 项仍是 `qwen3.7-plus`，deepseek 插在第 3 位。
- 两个列表由 `GET /api/config` 原样回传（`server.py:2801-2802`），前端下拉直接消费。

### 2. `backend/src/providers/llm_bailian.py` — `enable_thinking` 原地自愈

**问题**：Stage 4 的请求统一带 `enable_thinking=False`（`s4_scriptwriter._NO_THINKING_KWARGS`）。DeepSeek 系模型不支持该参数，百炼直接返回 `400 InvalidParameter`。若不在客户端层消化，上层 `run_with_model_fallback()` 会把它当成「模型不可用」，静默降级到 `MODEL_FALLBACK_CHAIN` 的下一个模型，用户既看不到真实原因，DeepSeek 也等于白配。

**新增判定函数**（`llm_bailian.py:86`）：

```python
def _should_retry_without_thinking(
    status_code: int,
    body_text: str,
    payload: dict[str, Any],
    model: str,
) -> bool:
```

触发条件（**全部满足**才剥离重试）：

| 条件 | 说明 |
| :--- | :--- |
| 状态码 = 400 | 404 / 403 一律不触发 |
| 请求体含 `enable_thinking` | 没带这个参数就没得剥 |
| 响应体不含「模型不存在」特征 | `_MODEL_MISSING_MARKERS = ("modelnotfound", "model not found", "does not exist")` |
| 响应体不含「鉴权 / 权限不足」特征 | `_ACCESS_DENIED_MARKERS = ("accessdenied", "access denied", "forbidden")` |
| 且满足其一 | (a) 响应体点名了 `enable_thinking`；(b) 模型名**不是** `qwen` 开头（DeepSeek 等）且响应体命中 `_INVALID_PARAMETER_MARKERS = ("invalidparameter", "unsupported", "unknown parameter", "extra inputs are not permitted")` |

**原地重试**（`llm_bailian.py:236-275`）：命中后剥掉 `enable_thinking`、**用同一个 model 再打一次**（不是换模型），成功即返回；失败按原有分支处理——重试响应属 `RETRYABLE_STATUS` 记为 `LLMRequestError`，否则照抛 `LLMRequestError`；重试请求抛 `httpx.HTTPError` 记为 `LLMError`。整段用 `stripped_thinking_retried` 标志位保护，**最多剥离重试一次**，不会变成无限循环。

同时向 stderr 打一行可观测日志：

```
[llm_bailian] 模型 {model} 不支持 enable_thinking 参数，已剥离该参数并原地重试一次。
```

**明确不自愈的场景**：404 ModelNotFound、403 AccessDenied、400 AccessDenied —— 这些是账号 / 权限问题，剥参数没有意义，必须暴露给上层走降级链或报错，避免掩盖真实故障。

### 3. `backend/src/providers/llm_base.py` — 剥离 `<think>` 思考块

```python
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)

def extract_json_text(text: str) -> str:
    stripped = _THINK_BLOCK_RE.sub("", text).strip()
    ...
```

部分模型（尤其是带思考模式的 DeepSeek 系）会在 JSON 围栏之外额外吐一段 `<think>…</think>` 推理文本，原实现会在围栏提取时把它当成正文，导致 JSON 解析失败。剥离发生在围栏提取**之前**，且大小写不敏感 + DOTALL（跨行）。

### 4. `backend/config.example.yaml` — 仅补注释

```yaml
models:
  # 可选：deepseek-v4.1-flash-1017（百炼 · DeepSeek 高性价比，需先在百炼控制台开通权限）
  script: qwen3.7-plus
  # 可选：deepseek-v4.1-flash-1017（百炼 · DeepSeek 高性价比，需先在百炼控制台开通权限）
  story_model: qwen4-max
```

默认值一字未改。

---

## 三、前端实现

### 1. `src/components/ConfigDictView.tsx`
- `FALLBACK_SCRIPT_MODEL_OPTIONS` / `FALLBACK_STORY_MODEL_OPTIONS` 数组化并加入 `'deepseek-v4.1-flash-1017'`（:20 / :29）。
- `SCRIPT_MODEL_LABELS` / `STORY_MODEL_LABELS` 各加一条显示名（:47 / :55）：

  ```ts
  'deepseek-v4.1-flash-1017': 'deepseek-v4.1-flash-1017 (百炼 · DeepSeek 高性价比)',
  ```
- 说书体模型提示文案（:229）改为：
  > 账号未开通该模型时自动降级：qwen4-max → qwen3.8-max → qwen3.7-plus → qwen-plus；选用 deepseek-v4.1-flash-1017 前需先在百炼控制台开通该模型的调用权限
- 音色下拉只取 MiniMax（:134）：

  ```ts
  () => [...(draft.minimaxVoiceOptions ?? [])],
  ```

### 2. `src/components/SeriesTtsPanel.tsx:132` / `src/components/EpisodeStudioView.tsx:590`

```ts
() => [...(data?.minimax_voices ?? [])],          // SeriesTtsPanel
setVoiceOptions([...(data.minimax_voices ?? [])]);  // EpisodeStudioView
```

### 3. `src/components/VoicePicker.tsx` — 音色 provider 收口

```ts
export const PROVIDER_LABELS: Record<string, string> = {
  minimax: 'MiniMax Speech',
};
const PROVIDER_ORDER = ['minimax'];
```

`groupVoices` 入口过滤：

```ts
if ((item.provider ?? 'qwen') !== 'minimax') continue;
const provider = item.provider ?? 'minimax';
```

**设计取舍**：剔除动作只在**选择层**做。`/api/config` 的 `ttsVoiceOptions`（34 条百炼音色）与 `/api/tts/voices` 的 `voices` 字段保持原样返回，既不改后端契约，也不破坏 `test_patch_v33` / `test_patch_v34` / `test_web_server` 的既有断言。

---

## 四、测试覆盖

`backend/tests/test_patch_v415_deepseek_and_voices.py`（**新建**，209 行 / 6 项，全部离线，`respx` 拦截百炼 HTTP）：

| 用例 | 断言要点 |
| :--- | :--- |
| `test_config_endpoint_offers_deepseek_model` | 两个 options 都含 deepseek；`scriptModelOptions[0] == "qwen3.7-plus"`、`storyModelOptions[0] == "qwen4-max"`（默认值语义未变） |
| `test_put_config_persists_deepseek_model` | `PUT /api/config` 后回显为 deepseek，且落盘到 `workspace/config.yaml` 的 `models.script` / `models.story_model` |
| `test_invalid_enable_thinking_is_stripped_and_retried_in_place` | 400 InvalidParameter → 剥离后成功；`call_count == 2`；第一次带 `enable_thinking`、第二次不带；**模型名不变** |
| `test_model_not_found_is_not_retried` | 404 ModelNotFound → `call_count == 1`，抛 `LLMRequestError` |
| `test_access_denied_is_not_retried` | 400 AccessDenied（且 body 含 `enable_thinking`）→ `call_count == 1`，抛 `LLMRequestError` |
| `test_parse_json_response_strips_think_block` | `"<think>分析</think>\n```json\n{\"ok\": true}\n```"` → `{"ok": True}` |

---

## 五、验收命令与结果

```bash
cd backend
.venv/bin/python -m pytest tests/test_patch_v415_deepseek_and_voices.py -q   # 6 passed
.venv/bin/python -m pytest -q                                               # 全量全绿
.venv/bin/python -m pytest -n auto -m "not slow" -q                         # 快档全绿
/opt/homebrew/bin/ruff check src tests                                      # All checks passed!
cd .. && npx tsc --noEmit && npm run build                                   # TSC_OK + 构建通过
```

- 白名单核查：`git diff --name-only -- backend/src/core/config.py backend/src/stages/s4_scriptwriter.py backend/src/providers/tts_qwen.py backend/src/stages/s2_segmenter.py backend/src/stages/s3_vision.py backend/src/stages/s7_renderer.py backend/src/stages/s5_tts.py` → **空**（禁改模块均未动）；`config.example.yaml` 实际改动仅 2 行注释。
- 后端音色 provider 标记核查：minimax 28 条全部 `provider=minimax`、qwen 34 条全部 `provider=qwen`，无缺 `provider` 的条目，故前端过滤不会误伤 MiniMax 音色。

---

## 六、已知边界

1. **DeepSeek 需先在百炼控制台开通权限**，否则 404 / 403 不属于自愈范围，会按降级链回退（降级链的调整见 `PATCH_v4.17_CONFIG_HOT_RELOAD_AND_FALLBACK.md`）。
2. **配置热重载不同步后台调度器**：`PUT /api/config` 只刷新 `app.state.config`，而 `JobRunner` 持有进程启动快照 —— 该问题在 v4.17 才修复，见 `PATCH_v4.17_CONFIG_HOT_RELOAD_AND_FALLBACK.md`。
3. **后端仍返回百炼音色**：音色剔除只发生在前端选择层；`/api/config` 的 `ttsVoiceOptions` 与 `/api/tts/voices` 的 `voices` 字段保持原样（属有意为之，避免破坏既有断言与后端契约）。
