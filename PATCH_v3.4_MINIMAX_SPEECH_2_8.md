# Webtoon2Video 增量重构补丁规格书（Patch v3.4：接入 MiniMax Speech 2.8 + 成片音频质检 5 大问题根治）

> **目标**：让流水线正式支持 **MiniMax Speech 2.8**（`speech-2.8-hd` 高保真旗舰 & `speech-2.8-turbo` 高性价比极速版）以及官方系统音色库 + **MiniMax 账户自定义克隆音色**；同时一并修掉上一版成片（`04:38`）暴露的**音量忽大忽小、闷糊失真、重复念台词、独白中途变性、旧 `Cherry` 配置泄漏** 5 个流水线根因，确保切到 MiniMax 后第一遍出片即达到抖音说书成片水准。
>
> **前提说明**：
> - 百炼（`DASHSCOPE_API_KEY`）仍负责 **Stage 3 识图**（`qwen3-vl-plus`）与 **Stage 4 撰稿**（`qwen4-max` / `qwen3.7-plus`）；
> - **Stage 5 配音**新增独立的 `MINIMAX_API_KEY`（放在项目根 `.env`），支持与百炼自由混搭；`config.yaml` 里把 `models.tts` 设为 `speech-2.8-hd`（或 `speech-2.8-turbo`）即自动走 MiniMax 链路，保留原有 `qwen3-tts-flash` 与 `edge-tts` 向后兼容。
>
> **给 `minimax-3.1-flash` 的铁律**：
> - **禁止全局重构**：严禁修改下载器（Stage 0/1）、`s2_segmenter.py` 切图算法、`s7_renderer.py` `1080×1920` 画面版式。
> - **分 2 轮增量落地，每轮结束必须跑通 `pytest`**：
>   - **Patch 16**：新增 `MiniMaxTTS` Provider（`t2a_v2` + `get_voice` + 原生 `speed` + 安全 `emotion` 枚举）+ `config/env/factory` 路由 + 音色兼容防错兜底 + 分供应商样例试听缓存与 Web 页面配置。
>   - **Patch 17**：根治上一版成片 4 大音频/拆分残留 Bug（单句 `-16 LUFS` `loudnorm`、`05_audio/` 脏缓存清理 + 相邻重复台词去重、内心独白不切声线 + 连续对白说话人继承防中途变性）。

---

## 一、为什么选 MiniMax Speech 2.8？（关键能力与工程适配）

| 维度 | 实测设计方案 | 相比旧方案的质变 |
| :--- | :--- | :--- |
| **模型选型** | `speech-2.8-hd`（精品漫评主推）<br>`speech-2.8-turbo`（批量低成本跑片） | 中文重音、断句呼吸、说书节奏感与情感张力显著强于 `qwen3-tts-flash`；`speech-2.8-hd` 在悬疑旁白与戏剧对白场景表现极稳 |
| **接口协议** | 国内端点 `POST https://api.minimaxi.com/v1/t2a_v2`<br>（国际端可配 `https://api.minimax.io/v1`） | 通过 `"output_format": "hex"` + `"audio_setting.format": "wav"` **一次请求直接拿回 WAV 字节**，无需二次下载 OSS 签名 URL |
| **语速调节** | 直接透传 `voice_setting.speed`（范围 `0.5 ~ 2.0`） | **原生韵律变速**，不再需要 FFmpeg `atempo` 硬拉音频导致金属感或齿音失真 |
| **情绪防失真** | 将剧本 `emotion_instruction` 映射为 MiniMax 离散安全枚举<br>（`calm` / `happy` / `sad` / `angry` / `fearful` / `surprised` / `fluent`） | **严禁传 `"whisper"` 或自由长提示词**（实测发闷/底噪暴涨的元凶）；低语/轻声统一安全降级为 `calm` |
| **音色与克隆** | 内置 28 个精选中文旁白/男/女声 + `POST /v1/get_voice` 拉取账户**已克隆自定义音色** | 既能直接用官方说书音色，也能在 MiniMax 控制台花 10 秒克隆任意喜欢的抖音博主演播声线直接填入 `voice_id` |

---

## 二、Patch 16：接入 MiniMax Speech 2.8 Provider、全链路路由与安全试听

**仅修改/新增以下文件**：
- `backend/src/providers/tts_minimax.py`（**新增**）
- `backend/src/core/config.py`
- `backend/config.example.yaml`
- `.env.example`
- `backend/src/stages/s5_tts.py`
- `backend/src/cli.py`
- `backend/src/web/job_runner.py`
- `backend/src/web/server.py`
- `backend/scripts/init_voice_samples.py`
- `backend/tests/test_patch_v34_minimax_tts.py`（**新增单测**）

### 精确实现规格

#### 1. `.env.example` 与 `config.py` 扩展
- `.env.example` 新增：
  ```dotenv
  # MiniMax Speech 2.8 TTS（与百炼 DASHSCOPE_API_KEY 独立共存）
  MINIMAX_API_KEY=
  # 可选：新版 Platform API Key 无需填；仅老版本企业/分组账号需填写 GroupId
  MINIMAX_GROUP_ID=
  ```
- `ApiConfig`（`backend/src/core/config.py`）新增字段（保持 `extra="forbid"`）：
  - `minimax_base_url: str = "https://api.minimaxi.com/v1"`
  - `minimax_api_key_env: str = "MINIMAX_API_KEY"`
  - `minimax_group_id_env: str = "MINIMAX_GROUP_ID"`
  - `minimax_group_id: str = ""`
- 新增 `resolve_minimax_api_key_with_env_file(config=None, env=None, env_file=None) -> str` 与 `resolve_minimax_group_id(config=None, env=None, env_file=None) -> str`（复用 `read_env_file` 的进程环境变量 $\rightarrow$ `.env` 读取顺序）。
- `ModelConfig` 默认或允许值说明：
  - `tts`: 支持 `"speech-2.8-hd"`、`"speech-2.8-turbo"`、`"speech-2.6-hd"`、`"speech-2.6-turbo"`、`"qwen3-tts-flash"`、`"qwen-audio-3.1-tts-plus"`、`"edge-tts"`；
  - 默认可设为 `"speech-2.8-hd"`（或在 `config.example.yaml` 中明确注释切换方式）。

#### 2. 新增 `backend/src/providers/tts_minimax.py`
- 常量与预设音色目录 `MINIMAX_VOICES`（每个条目包含 `id`, `name`, `gender`, `style`, `recommended_for`）：
  - **默认旁白音色**：`DEFAULT_VOICE = "Chinese (Mandarin)_Serene_Narrator"`
  - **推荐说书旁白（5 个）**：
    - `Chinese (Mandarin)_Serene_Narrator`（沉稳说书人 — 推荐悬疑/剧情长漫主旁白）
    - `Chinese (Mandarin)_Male_Announcer`（磁性男播音 — 节奏紧凑有力）
    - `Chinese (Mandarin)_Reliable_Executive`（稳重成熟男声 — 都市/商战/悬疑）
    - `Chinese (Mandarin)_News_Anchor`（干练女主播 — 清晰利落）
    - `Chinese (Mandarin)_Gentle_Senior`（温和叙事声线 — 抒情/回忆）
  - **男角色音色（11 个）**：
    - `male-qn-qingse`（青涩青年） / `male-qn-qingse-jingpin`（青涩青年-精品）
    - `male-qn-jingying`（精英青年） / `male-qn-jingying-jingpin`（精英青年-精品）
    - `male-qn-badao`（霸道青年） / `male-qn-badao-jingpin`（霸道青年-精品）
    - `male-qn-daxuesheng`（青年大学生）
    - `Chinese (Mandarin)_Gentleman`（温润绅士）
    - `Chinese (Mandarin)_Refreshing_Young_Man`（清爽青年）
    - `Chinese (Mandarin)_Unrestrained_Young_Man`（不羁青年）
    - `Chinese (Mandarin)_Humorous_Elder`（幽默长者/大叔）
  - **女角色音色（12 个）**：
    - `female-shaonv`（年轻少女） / `female-shaonv-jingpin`（少女-精品）
    - `female-yujie`（成熟御姐） / `female-yujie-jingpin`（御姐-精品）
    - `female-tianmei`（甜美女声） / `female-tianmei-jingpin`（甜美-精品）
    - `Chinese (Mandarin)_Warm_Girl`（温暖女生）
    - `Chinese (Mandarin)_Warm_Bestie`（温柔闺蜜）
    - `Chinese (Mandarin)_Mature_Woman`（成熟女性）
    - `Chinese (Mandarin)_Sweet_Lady`（甜美淑女）
    - `Chinese (Mandarin)_IntellectualGirl`（知性女生）
    - `Chinese (Mandarin)_Kind-hearted_Elder`（慈祥长辈）
- **关键协议细节（必须严格按以下契约实现）**：
  1. `supports_native_speed: bool = True`（供 `s5_tts.py` 判断，有原生语速能力时不走 `ffmpeg atempo`）。
  2. 请求端点：`{base_url.rstrip('/')}/t2a_v2`；仅当 `group_id` 非空时追加 `?GroupId={group_id}`；Headers 只带 `Authorization: Bearer {api_key}` 和 `Content-Type: application/json`。
  3. 请求体：
     ```python
     payload = {
         "model": self._model,  # e.g. "speech-2.8-hd"
         "text": text,
         "stream": False,
         "language_boost": "Chinese",
         "output_format": "hex",
         "voice_setting": {
             "voice_id": chosen_voice,
             "speed": round(max(0.5, min(2.0, float(speed))), 2),
             "vol": 1.0,
             "pitch": 0,
         },
         "audio_setting": {
             "sample_rate": 32000,
             "bitrate": 128000,
             "format": "wav",
             "channel": 1,
         },
     }
     ```
  4. **安全情绪映射 `_map_emotion(instruction: str) -> str | None`**：
     - 含 `怒/吼/斥/骂/暴躁` $\rightarrow$ `"angry"`
     - 含 `哭/悲/伤心/哽咽/绝望/凄凉` $\rightarrow$ `"sad"`
     - 含 `慌/吓/抖/恐惧/紧张/窒息/惊恐` $\rightarrow$ `"fearful"`
     - 含 `惊/震/瞪/错愕` $\rightarrow$ `"surprised"`
     - 含 `笑/喜/开心/激动/兴奋` $\rightarrow$ `"happy"`
     - 含 `厌/冷哼/鄙夷/恶心` $\rightarrow$ `"disgusted"`
     - 含 `沉稳/冷静/平稳/低语/轻声/压低` $\rightarrow$ `"calm"`（**严禁传 `"whisper"`，避免音频发闷失真**）
     - 未命中返回 `None`（不传 `emotion` 字段，由 Speech 2.8 自动根据上下文语境抑扬顿挫）。
  5. **`base_resp` 错误码与重试判定**（MiniMax 常在 HTTP 200 JSON 里返回错误码）：
     - `base_resp = data.get("base_resp") or {}`
     - `code = int(base_resp.get("status_code", 0) or 0)`
     - `code == 0`：取 `data["audio"]`（若以 `http` 开头则 GET 下载；否则 `bytes.fromhex(raw_audio)` 转 WAV 字节），返回 `TTSResult(audio=audio_bytes, content_type="audio/wav", voice=chosen_voice)`。
     - `code in {1000, 1002, 1024, 1039}` 或 HTTP `{408, 409, 425, 429, 500, 502, 503, 504}`：**可重试**（`TTSError` 触发退避重试）。
     - `code in {1004, 1008, 2013}` 或其余非 0 错码 / 4xx：**不重试直接抛出可读 `TTSRequestError`**（例如 `"MiniMax TTS 鉴权失败/余额不足/voice_id 无效 (status_code=2013): ..."`）。
  6. **动态账户音色拉取 `fetch_minimax_voices(...)`**：
     - `POST {base_url}/get_voice`，Body `{"voice_type": "all"}`；解析返回中的 `system_voice` 与 `voice_cloning` / `voice_generation`（用户克隆音色标记 `is_cloned=True`、`category="cloned"`），失败时优雅降级回静态 `MINIMAX_VOICES`。

#### 3. 统一 TTS 工厂与跨供应商音色防踩坑兜底
- 在 `backend/src/providers/tts_base.py` 或 `backend/src/core/config.py` 新增统一工厂 `create_tts_client(config: AppConfig) -> Any` 与音色兼容性检查 `resolve_voice_for_tts_model(voice: str | None, *, tts_model: str, role_kind: Literal["narrator", "male", "female"]) -> str`：
  - **路由规则**：
    - `tts_model.startswith("speech-")` $\rightarrow$ `MiniMaxTTS`
    - `tts_model == "edge-tts"` $\rightarrow$ `EdgeTTS`
    - 其余（`qwen*`）$\rightarrow$ `QwenTTS`
  - **跨模型切音色防报错护栏（极重要）**：
    - 如果用户刚把 `models.tts` 从 `qwen3-tts-flash` 改成 `speech-2.8-hd`，但作品目录 `workspace/wt_5081/series_config.yaml` 或旧配置里还留着 `Cherry`、`Elias`、`Ethan`、`Serena`、`Eldric Sage`、`zh-CN-*`：
    - `resolve_voice_for_tts_model` 自动识别「该音色属于 Qwen/Edge 而当前模型是 MiniMax」，打一条 WARNING 日志并平滑替换为对应的 MiniMax 默认音色（旁白 $\rightarrow$ `Chinese (Mandarin)_Serene_Narrator`，男配 $\rightarrow$ `male-qn-jingying`，女配 $\rightarrow$ `female-yujie`），**绝不把 `Cherry` 原样发给 MiniMax 导致整话报错 `2013 invalid voice_id`！**
    - 反之（若用户选了自定义克隆 ID 如 `my_cloned_voice_01` 且不在 Qwen/Edge 已知列表里），则原样透传给 MiniMax。
  - **清理 `server.py:71` 的 `DEFAULT_TTS_VOICE = "Cherry"` 硬编码**：StudioConfig 回显与更新联动 `config.models.tts`，默认返回当前活跃模型的推荐旁白音色。
  - **样例缓存路径按供应商隔离**：`backend/assets/voice_samples/{provider}/{safe_voice_id}.wav`（如 `minimax/Chinese__Mandarin__Serene_Narrator.wav`、`qwen/Elias.wav`），URL 编码安全解析，`init_voice_samples.py` 支持 `--provider minimax|qwen|all`。

---

## 三、Patch 17：根治上版成片（`04:38`）暴露的 4 项流水线音频/脚本 Bug

> **注**：以下 4 项问题在 `wt_5081_ep_001`（`04:38`）中已逐秒定位确认，与使用哪家 TTS 无关；若不修复，切到 MiniMax Speech 2.8 后同样会出现！

**仅修改以下文件**：
- `backend/src/core/normalize.py`
- `backend/src/stages/s4_scriptwriter.py`
- `backend/src/stages/s5_tts.py`
- `backend/tests/test_patch_v34_audio_hygiene.py`（**新增单测**）

### 精确修复清单

1. **单句级 `-16 LUFS` 响度归一 + 原生速度双路径（`backend/src/stages/s5_tts.py`）**：
   - 在 `_trim_silence` 同一条 `ffmpeg` 滤镜链末尾串联 `loudnorm=I=-16:TP=-1.5:LRA=7`，保证无论该句是平静旁白还是激昂对白，输出 WAV 响度稳定一致，彻底消灭“上一句震耳、下一句细若蚊呐”。
   - 若 `getattr(client, "supports_native_speed", False)` 为真（`MiniMaxTTS`），把解析出的 `speed` 传给 `client.synthesize(..., speed=speed)`，**不再叠加 `atempo`**；否则维持 `atempo={speed}`。

2. **清理 `05_audio/` 旧缓存防残余串音（`backend/src/stages/s5_tts.py`）**：
   - `synthesize_episode` 开始写新文件前，清理 `audio_dir` 内残留的 `beat_*.wav` / `beat_*.mp3`（若某条 beat 重跑撰稿后被删或编号变化，防止旧音频文件留在目录里干扰排查或被错误读取）。

3. **相邻重复对白确定性去重（消除 `03:17–03:21` “这个地方是用来做什么的呀？”连说两遍）**：
   - 在 `s4_scriptwriter.py` 的 `split_mixed_quote_beats` 与 `normalize_script_payload`（`normalize.py`）中新增 `deduplicate_adjacent_beats(beats)`：
     - 若相邻两条 Beat 去掉标点/引号/空白后的 `tts_text` 完全相同（或后一条被前一条完全包含且长度 $\ge 6$ 字、且共享相邻 `target_panel_ids`），自动丢弃重复的后一条并打 INFO 日志。

4. **仅“真正开口说话”才切角色声线 + 连续对白说话人继承（消除 `04:09` 男角色说到一半突然变女声）**：
   - **内心独白/短促拟声留在旁白**：在 `split_mixed_quote_beats` 中，若引号前导文本命中 `心想|暗想|默念|心里|想到|意识到|回响|脑海` 等心理活动关键词，或引号内纯字数 $< 6$ 字（如 `“嗯？”`、`“……！”`），保持 `speaker_role="narrator"`，不强行拆拍切走声线，保证说书节奏连贯不碎。
   - **连续对白状态机防变性**：Stage 4B 后处理及 `s5_tts.py` 维护当前话次内的 `last_dialogue_speaker` 与 `last_dialogue_gender`（仅在相邻 Beat 间隔不超过 1 个分镜且同属连续对话序列时生效）：当出现未写明“XX说：”的纯引号对白 Beat（且 `speaker_role == "narrator"` 或 `speaker_gender == "unknown"`）时，自动继承上一句对白的说话人与性别（`04:09` 被绑男人的连续台词不会突然退回旁白女声）。

---

## 四、验收单测与执行步骤（给用户与 `minimax-3.1-flash`）

### 给 `minimax-3.1-flash` 的分轮执行提示词
1. **第一轮（Patch 16）**：
   > 请严格阅读 `PATCH_v3.4_MINIMAX_SPEECH_2_8.md` 的「Patch 16」章节，仅修改允许清单内的文件：接入 `MiniMaxTTS`（`speech-2.8-hd` / `speech-2.8-turbo`）、`.env` 密钥读取、音色兼容防踩坑护栏、按供应商隔离的样例试听缓存与 `/api/tts/voices`（支持拉取克隆音色），并编写 `backend/tests/test_patch_v34_minimax_tts.py` 使用 `respx` mock 验证 `t2a_v2` hex 解码、`base_resp.status_code` 重试/报错、`speed`/`emotion` 映射与 `Cherry->MiniMax` 自动防报错映射，跑通全量 `pytest`。禁止全局重构。
2. **第二轮（Patch 17）**：
   > 在 Patch 16 测试全绿后，请严格阅读 `PATCH_v3.4_MINIMAX_SPEECH_2_8.md` 的「Patch 17」章节，仅修改 `normalize.py`、`s4_scriptwriter.py`、`s5_tts.py` 及新增单测 `test_patch_v34_audio_hygiene.py`：实现单句 `-16 LUFS` `loudnorm`、`05_audio/` 旧音频清理、相邻重复 Beat 去重、心理独白保留旁白、连续对白说话人/性别继承，跑通全量 `pytest`。

### 你本地配置与出片步骤
1. 在 MiniMax 开放平台（`https://platform.minimaxi.com`）创建 API Key，写入项目根目录 `.env`：
   ```dotenv
   DASHSCOPE_API_KEY=你的百炼Key
   MINIMAX_API_KEY=你的MiniMax_Key
   # 若为新版平台 Key 可留空；若接口提示 GroupId 校验再填：
   MINIMAX_GROUP_ID=
   ```
2. 在 `backend/config.yaml` 中切到 MiniMax Speech 2.8：
   ```yaml
   models:
     vision: qwen3-vl-plus
     script: qwen3.7-plus
     story_model: qwen4-max
     tts: speech-2.8-hd   # 或 speech-2.8-turbo（速度更快、成本更低）
   ```
3. 先生成一套静态样例（后续网页点击试听 **0 消耗 API**）：
   ```bash
   cd backend
   uv run python scripts/init_voice_samples.py --provider minimax
   ```
4. 在 Web 控制台选择作品（如 `wt_5081`），给旁白选 `Chinese (Mandarin)_Serene_Narrator`（语速 `1.12`），给男/女角色与男女通用兜底配好 MiniMax 音色，重新运行 **Stage 4B $\rightarrow$ Stage 5 $\rightarrow$ Stage 6 $\rightarrow$ Stage 7**（无需重下图片与重识图）即可输出新版成片。
