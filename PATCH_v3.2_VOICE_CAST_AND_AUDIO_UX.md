# Webtoon2Video 增量重构补丁规格书（Patch v3.2：作品级多角色配音与语速试听 + 引号自动切角色声线 + BGM/音效响度修复）

> **适用阶段**：基于最新成片 `wt_5081_ep_001`（`03:25`）逐帧逐声审核后制定。  
> **核心痛点解决**：
> 1. **彻底解决“讲解音不好听 + 语速太慢”**：支持按**单部漫画作品（`series_config.yaml`）**独立配置讲解者与角色音色/语速（默认讲解提速到 `1.18x`，停顿压缩），并在 Web 页面提供**零 TTS API 消耗的本地样例 + 实时倍速试听**（静态 `.wav` 样例 + 浏览器 `audio.playbackRate`）。
> 2. **已识别角色专属配音 + 未识别角色按男/女通用兜底**：结合 `StoryBibleV2` 人物表（新增 `gender` 字段），为已出场角色单独选音色与语速，未登录角色自动走 `default_male` / `default_female`。
> 3. **讲解者讲故事 + 双引号对白自动切角色声音**：先强制修复全部对话为中文双引号 `“...”`，再在 Stage 4B 落盘前**确定性拆分**「旁白引导 + 引号对白」为连续两条 `ScriptBeat`（衔接停顿 `60ms`），彻底根除“角色自己念‘XX说：’”或“旁白把台词念完”的问题。
> 4. **真正听得见 BGM 与音效**：修复 BGM 被 `-18dB` + 侧链压缩双重压没的 Bug，补齐响度归一（`loudnorm`）、合理的默认增益（BGM `-12dB` / SFX `-4dB`）与完整关键词触发链。
> 5. **清理新片残余画面/剧情 Bug**：禁止 `is_pure_text_card`（如 `01:54` 模糊字卡）出镜、杜绝 `02:43/02:49` 看图写话凑帧与 `03:02` 关键悬念台词被吞。
>
> **给 `minimax-3.1-flash` 的铁律**：  
> - **禁止全局重构**：严禁修改下载器（Stage 0/1）、`s2_segmenter.py` 核心切图与 `s7_renderer.py` 的 `1080×1920` 上下分区画面版式。  
> - **按顺序分 3 轮执行**：`Patch 12（双引号修复 + 旁白/引号对白确定性拆拍 + 残余看图写话/纯字卡硬拦截）` $\rightarrow$ `Patch 13（作品级 TTS 配音/语速/角色性别路由 + 静态试听样例 + Web API & 前端设置页）` $\rightarrow$ `Patch 14（BGM/SFX 响度归一与显性混音修复）`。

---

## 新成片（`03:25`）问题复盘（共 8 项）

| 来源 | 发现的问题 | 根因定位 | 归属补丁 |
| :--- | :--- | :--- | :--- |
| **用户反馈 1** | 主旁白仍为慢速 `Cherry` 女声，声音不合适且语速拖沓 | 作品配置/旧覆盖未切到新音色，且系统无独立 `speed` 调速，句间停顿偏长（250ms+） | Patch 13 |
| **用户反馈 1** | 需在页面按作品选音色+语速并**直接试听样例（不消耗 TTS 合成）** | 缺少静态样例库 `voice_samples/`、Web `/api/tts/*` 接口与浏览器 `playbackRate` 试听 UI | Patch 13 |
| **用户反馈 2** | 需支持已识别角色专属设置 + 未识别角色分男/女通用设置 | `CharacterProfile` 缺 `gender`，`SeriesConfig` 缺分角色/性别/语速配置结构 | Patch 13 |
| **用户反馈 3** | 引号中的内容要由对应角色配音，而不是讲解者一锅端 | Stage 4B 把「道振说：“……”」合在同一条 `ScriptBeat` 里，只能绑定 1 个音色 | Patch 12 |
| **用户反馈 4** | 第 4 步偶发把人物对话用单引号 `‘...’` 包裹 | Prompt 约束不够且缺少落盘前确定性引号归一化正则 | Patch 12 |
| **用户反馈 5** | 视频里几乎听不到 BGM 与音效 | BGM 未做 `loudnorm` 且 `-18dB` 再叠加 `sidechaincompress` 跌到 `-45dB` 以下；SFX 增益过小/触发偏少 | Patch 14 |
| **额外发现 6** | `01:54` 纯字卡 PPT（`這個地方有著…`）仍出镜 7 秒；`02:43`/`02:49` 还在数“金属柜/线缆/笔记本电脑” | 4B 候选池未硬过滤 `is_pure_text_card`，且 4A/4B 仍有静物描写惯性 | Patch 12 |
| **额外发现 7** | `03:02` 关键悬念对白 `“只要跟他说我知道你在哪的话，他立刻就会出现的！”` 被吞成仅两个字“混蛋” | 4A 对话精简过度，丢掉了决定剧情伏笔的核心台词 | Patch 12 |

---

## Patch 12：双引号统一 +「旁白 / 引号对白」确定性自动拆分 + 纯字卡/吞台词拦截

**仅修改**：
- `backend/src/core/schemas.py`
- `backend/src/core/normalize.py`
- `backend/src/stages/s4_scriptwriter.py`
- `backend/prompts/vision_extract.md`
- `backend/prompts/story_draft.md`
- `backend/prompts/script_narrate.md`
- `backend/tests/test_patch_v32_quotes_and_split.py`（新增单测）

### 精确改动清单

1. **Schema 补充人物性别与精确说话人字段（`backend/src/core/schemas.py`）**：
   - `CharacterProfile` 新增：
     ```python
     gender: Literal["male", "female", "unknown"] = "unknown"
     ```
   - `ScriptBeat` 新增可选字段（兼容旧 `script.json`）：
     ```python
     speaker_gender: Literal["male", "female", "unknown"] = "unknown"
     ```
     - 约定：`speaker_role` 对纯对话句优先填**具体人物姓名**（如 `"恩彗"`、`"道振"`、`"继母"`，或兼容 `"male_lead"` 等角色槽位），旁白固定填 `"narrator"`；同时带上 `speaker_gender`（`"male"` / `"female"` / `"unknown"`），便于未在作品词典中单独指定音色的角色自动落入通用男声或通用女声！

2. **确定性双引号归一化（`backend/src/core/normalize.py`）**：
   - 新增 `normalize_dialogue_quotes(text: str) -> str`：
     - 将模型误输出的对话单引号或英文引号规范为标准中文双引号 `“...”`：
       - `：‘([^’\n]{2,120})’` $\rightarrow$ `：“\1”`
       - `:'([^'\n]{2,120})'` / `：'([^'\n]{2,120})'` $\rightarrow$ `：“\1”`
       - `："([^"\n]{2,120})"` $\rightarrow$ `：“\1”`
       - 整段被 `‘...’` 或 `'...'` 包裹的独立台词 $\rightarrow$ `“...”`
     - 注意保护英文缩写（如 `I'm`、`don't`）不被误替换。
   - 在 `normalize_script_payload` 中对每条 `narration_text` 与 `tts_text` 强制调用 `normalize_dialogue_quotes`。

3. **确定性「旁白 + 引号对白」自动拆拍器 `split_mixed_quote_beats`（`backend/src/stages/s4_scriptwriter.py`）**：
   - **目的**：无论大模型输出是否把旁白引导词和人物引号对白混写在同一条 Beat 里（例如 `道振说：“只要在你心里我是个完美的丈夫就行了。”`），Python 在归一化阶段**100% 自动把它们拆成两条连续 `ScriptBeat`**，确保声线切换严丝合缝！
   - **拆分规则**：
     - 扫描每条 beat 的 `narration_text` 中的成对中文双引号 `“([^”]+)”`：
     - 若整条文本**同时包含引号外旁白叙述与引号内对白**（例如引号外长度 `>= 2` 字且引号内长度 `>= 2` 字）：
       1. 按引号边界切分为若干段 `[片段1, 片段2, ...]`；
       2. **非引号段**（如 `"道振说："`、`"恩彗咽下委屈，轻声道："`）：生成子 Beat，`speaker_role="narrator"`、`speaker_gender="unknown"`、`pause_after_ms=60`（极短停顿，念完引导词立刻接角色声音！）；
       3. **引号内对白段**（如 `"“麻烦给他们一点对话的空间，一下下就好。”"`）：
          - 字幕 `narration_text` 保留 `“...”` 双引号；
          - 送音 `tts_text`（若原无显式 `tts_text` 则基于引号内纯文本生成，**去掉外层双引号本身，避免 TTS 停顿怪异**，并保留句末语气标点）；
          - `speaker_role` 继承原 beat 的说话人（若原 beat 误标成 `"narrator"`，则从前文引导词或 `StoryBibleV2.characters` 自动推断说话人姓名与 `speaker_gender`，例：前文含 `"道振"` $\rightarrow$ `speaker_role="道振"`, `speaker_gender="male"`）；
          - `pause_after_ms` 取原 beat 的停顿（默认收紧为 `160`ms）。
       4. 所有拆分出来的子 Beat 共享同一组 `target_panel_ids`（保证画面稳住不乱跳），重新顺序编号 `b1, b2, ...`。
     - 若整条文本**本身就是纯引号对白**（例如 `“就算我死了，你也不用来参加我的葬礼。”`）：直接归属对应角色，仅在 `tts_text` 剥掉外层双引号并做角色/性别补全。

4. **Prompt 同步升级 + 残余画面/剧情 Bug 封杀**：
   - **`prompts/story_draft.md`**：
     - 强调：**所有人物对话、心声原话一律使用中文双引号 `“...”`，严禁使用单引号 `‘...’` 或英文引号**；
     - 强调：**禁止静态摆设描写**（如“桌上静静躺着一台笔记本电脑”“金属柜、箱子、散落的线缆处处透着异常”），直接讲人物做了什么、发现了什么；
     - 强调：**不得遗漏推进主线悬念的关键长对白**（如被绑人供出幕后黑手与威胁原话，必须完整保留核心信息，不能只剩一句“混蛋”）。
   - **`prompts/script_narrate.md` 与 `s4_scriptwriter.py`**：
     - 给 `ScriptBeat` 增加 `speaker_gender` 输出规范，并要求尽量把「旁白引导」与「角色引号原话」拆成相邻两条 Beat；
     - **硬过滤 `is_pure_text_card == True` 面板**：在传给 Stage 4B 的 `PANELS` 候选池中**直接剔除** `is_pure_text_card == True` 的分镜（仅当全章只剩纯字卡时才兜底保留），从源头彻底消灭 `01:54` 模糊字卡当 PPT 播放的问题；
     - 默默认整话默认停顿：`pause_after_ms` 默认值从 `250` 收紧为 **`150`**（旁白接台词为 `60`），提振整片节奏。

### 验收测试（`test_patch_v32_quotes_and_split.py`）
1. 输入 `"道振平静说道：‘别怕，有我在。’"` $\rightarrow$ 经 `normalize_dialogue_quotes` + `split_mixed_quote_beats` 后，必拆成：
   - Beat 1：`narration_text == "道振平静说道："`, `speaker_role == "narrator"`, `pause_after_ms == 60`
   - Beat 2：`narration_text == "“别怕，有我在。”"`, `tts_text == "别怕，有我在。"`, `speaker_role == "道振"`, `speaker_gender == "male"`
2. 候选面板池同时含普通画格与 `is_pure_text_card=True` 分镜时，传给 4B 的可用候选 ID 中不包含纯字卡 ID。

---

## Patch 13：作品级 TTS 配音与语速设置 + 角色/性别兜底 + 页面零耗费试听

**仅修改**：
- `backend/src/core/config.py`
- `backend/config.example.yaml`
- `backend/src/providers/tts_qwen.py`
- `backend/src/stages/s5_tts.py`
- `backend/src/cli.py`
- `backend/src/web/server.py`
- `backend/src/web/job_runner.py`
- `backend/scripts/init_voice_samples.py`（新增：一次性生成本地静态样例）
- （前端同步）`src/`（如后端仓库内包含对应前端目录 `frontend/` 一并更新作品 TTS 配置面板；AI Studio 工作区原型 `src/` 视需求同步）
- `backend/tests/test_patch_v32_tts_settings.py`（新增单测）

### 精确改动清单

#### A. 百炼系统音色清单（全部已在百炼 `qwen3-tts-flash` 实测/官方收录）

| `voice_id` | 官方名 | 性别 | 音色特点 | 适用场景 |
| :--- | :--- | :--- | :--- | :--- |
| **`Elias`** | 墨讲师 | 男 | 磁性沉稳、有故事讲述感 | 悬疑/剧情漫评主旁白（推荐默认） |
| **`Ethan`** | 晨煦 | 男 | 阳光温暖青年标准普通话 | 现代男主 / 青春男旁白 |
| **`Moon`** | 月白 | 男 | 清冷率性帅气男声 | 高冷/腹黑男主 |
| **`Kai`** | 凯 | 男 | 温柔低沉有质感男声 | 深情男主 / 情感故事旁白 |
| **`Ryan`** | 甜茶 | 男 | 富有戏剧张力与情绪起伏男声 | 冲突反派 / 激烈台词 |
| **`Marcus`** | 秦川 | 男 | 沉稳硬汉男声（略带地方硬朗韵味） | 霸道长辈 / 硬派反派 |
| **`Cherry`** | 芊悦 | 女 | 明亮亲切年轻女声 | 年轻女主 |
| **`Serena`** | 苏瑶 | 女 | 温柔细腻小姐姐声 | 温柔女主 / 抒情女旁白 |
| **`Maia`** | 四月 | 女 | 知性优雅女声 | 理性女配 / 叙事旁白 |
| **`Katerina`** | 卡捷琳娜 | 女 | 成熟富有韵味女声 | 继母 / 御姐反派 |
| **`Vivian`** | 十三 | 女 | 率性可爱略带小脾气 | 傲娇女配 / 闺蜜 |
| **`Chelsie`** | 千雪 | 女 | 甜美少女感 | 软萌/少女角色 |
| **`Bella`** | 萌宝 | 女 | 萝莉/孩童声线 | 幼年主角 / 儿童角色 |

#### B. 作品级 `SeriesTTSConfig` 结构与继承优先级

1. 在 `core/config.py` 新增数据结构（挂入 `SeriesConfig.tts` 与 `AppConfig.tts` 默认模板）：
   ```python
   class VoiceSpec(_StrictModel):
       voice: str = "Elias"
       speed: float = Field(default=1.18, ge=0.6, le=1.8)

   class SeriesTTSConfig(_StrictModel):
       narrator: VoiceSpec = Field(default_factory=lambda: VoiceSpec(voice="Elias", speed=1.18))
       default_male: VoiceSpec = Field(default_factory=lambda: VoiceSpec(voice="Ethan", speed=1.15))
       default_female: VoiceSpec = Field(default_factory=lambda: VoiceSpec(voice="Cherry", speed=1.15))
       character_voices: dict[str, VoiceSpec] = Field(default_factory=dict)
   ```
   - 同时保留旧字段（`SeriesConfig.voice`、`TTSConfig.voice_cast`）向后兼容；当 `SeriesConfig.tts` 存在时以作品级 `SeriesConfig.tts` 为最高优先级！

2. **音色与语速 4 级解析算法（`s5_tts.py`：`resolve_beat_voice_spec(beat, ...)`）**：
   - 若 `beat.speaker_role == "narrator"`（或 `"旁白"`）：返回 `series_tts.narrator`（`voice`, `speed`）；
   - 否则，按 `beat.speaker_role` 精确/别名匹配 `series_tts.character_voices`（已识别角色专属配置），命中则返回该角色的 `(voice, speed)`；
   - 否则，按 `beat.speaker_gender`（或根据 `StoryBibleV2.characters` 查到的性别、旧槽位如 `male_lead`/`female_mature` 推断性别）路由：
     - `"male"` $\rightarrow$ 返回 `series_tts.default_male`（通用男声 + 男生默认语速）
     - `"female"` $\rightarrow$ 返回 `series_tts.default_female`（通用女声 + 女生默认语速）
   - 仍无法判断性别 $\rightarrow$ 回退 `series_tts.narrator`。

3. **不变调高质量变速实现（`s5_tts.py` `_apply_speed`）**：
   - 当解析出的 `speed != 1.0`（范围 `0.6 ~ 1.8`）时，在 `s5_tts.py` 对 `qwen3-tts-flash` 合成的 `.wav` 执行 FFmpeg `atempo` 滤镜（例如 `atempo=1.18`，与静音裁剪合并为一次 ffmpeg 调用，**零额外子进程开销**），不改变音高、不发尖、**且与浏览器 `<audio>.playbackRate` 试听效果几乎完全一致**！

#### C. 零消耗 TTS 的静态样例 + 前端页面实时调节试听

1. **静态样例缓存脚本 `backend/scripts/init_voice_samples.py`**：
   - 为上述 13 个音色各准备一句 8 秒左右的代表性中文试听台词，保存在 `backend/assets/voice_samples/{voice_id}.wav`（并附 `manifest.json`）；
   - **幂等省额度原则**：检测 `{voice_id}.wav` 已存在时**直接跳过，绝不重复合成**；运行一次后，后续所有页面试听 **0 次 API 调用**！
2. **FastAPI 后端接口（`backend/src/web/server.py`）**：
   - `GET /api/tts/voices`：返回 13 个音色元数据及 `sample_url: "/api/tts/samples/{voice_id}"`、`sample_ready: bool`；
   - `GET /api/tts/samples/{voice_id}`：直接以 `audio/wav` 返回本地静态样例文件（支持 HTTP Range 拖拽）；
   - `GET /api/series/{series_id}/tts-config`：合并返回当前作品的 `SeriesTTSConfig` + 从 `story_bible.json` 读出的**已识别角色列表**（含姓名、外貌特征、性别）；
   - `PUT /api/series/{series_id}/tts-config`：持久化保存到 `workspace/{series_id}/series_config.yaml`。
   - `job_runner.py` 执行 Stage 5 时读取 `load_series_config(series_dir)` 并把完整 `series_tts`（配合 `story_bible` 角色性别映射）传给 `s5_tts.run_stage5`。
3. **前端页面交互（在作品详情 / Studio 配置区）**：
   - 展示 4 块清晰设置：
     1. **讲解者设置**：下拉选择音色（默认 `Elias`）+ 语速滑块 `0.75x ~ 1.50x`（默认 `1.18x`）+ **「▶ 试听样例」按钮**；
     2. **未识别男性通用设置**：音色 + 语速 + **「▶ 试听样例」**；
     3. **未识别女性通用设置**：音色 + 语速 + **「▶ 试听样例」**；
     4. **已识别角色专属列表**（自动带出 `恩彗`、`道振`、`继母`、`父亲` 等）：每个角色独立选音色、语速、性别标签 + **「▶ 试听样例」**。
   - **零合成试听机制**：点「试听样例」直接用前端 `new Audio(sampleUrl)` 设 `audio.playbackRate = speed` 播放本地静态样例，拖滑块秒切换语速，**绝不触发 TTS 合成接口**！

### 验收测试（`test_patch_v32_tts_settings.py`）
1. 构造 `SeriesTTSConfig`（讲解者 `Elias@1.2x`、通用男 `Ethan@1.15x`、通用女 `Serena@1.1x`、专属 `继母: Katerina@1.05x`），测试：
   - `narrator` $\rightarrow$ `Elias, 1.2`
   - `继母` $\rightarrow$ `Katerina, 1.05`
   - 未在 `character_voices` 里但 `speaker_gender="male"` 的新龙套（如 `"保镖"`）$\rightarrow$ `Ethan, 1.15`
   - 未在 `character_voices` 里但 `speaker_gender="female"` $\rightarrow$ `Serena, 1.1`
2. 测试 `s5_tts` 的 `atempo` 滤镜构造（`speed=1.25` 时真实缩短输出音频时长约 `1 / 1.25`）。
3. 测试 `PUT /api/series/{id}/tts-config` 后再 `GET` 能完整回读已保存的音色与语速。

---

## Patch 14：彻底解决 BGM 听不见 + 音效漏播 / 过小问题

**仅修改**：
- `backend/src/core/config.py`
- `backend/config.example.yaml`
- `backend/src/stages/s6_timeline.py`
- `backend/src/stages/s7_renderer.py`
- `backend/tests/test_patch_v32_audio_mix.py`（新增单测）

### 精确改动清单

1. **修复 BGM 被双重压没的 Bug（`core/config.py` + `s7_renderer.py`）**：
   - 将默认 `bgm_gain_db` 从 `-18.0` 提升至 **`-11.5`**（实测环境悬疑曲目在 `-18dB` 再经侧链闪避后会跌到 `-45dB` 几乎听不到）；
   - 在 `s7_renderer.py` `_prepare_audio` 处理 BGM 时，**先做响度归一化 `loudnorm=I=-16:TP=-1.5:LRA=11` 再施加 `volume={bgm_gain_db}dB`**，确保不管是从 Pixabay/Mixkit 下载的轻音乐还是重低音，音量基线统一稳定；
   - 调整 `_render_segment` 中的 `sidechaincompress` 参数为更柔和自然的漫评闪避：`threshold=0.05:ratio=2.8:attack=25:release=320`（说话时背景音乐温和退让 4~6dB，停顿时立即自然托底）。

2. **修复 SFX 没声音 / 漏触发（`s6_timeline.py` + `s7_renderer.py`）**：
   - 提高默认 `sfx_gain_db` 从 `-10.0` 到 **`-3.5`**，并在混入前对单个 SFX 做峰值归一化（如 `loudnorm=I=-14:TP=-1.0` 或 `dynaudnorm`），确保心跳、甩门、转场、手机提示清晰且不爆音；
   - 扩充 `s6_timeline.py` 对 Stage 3 `sfx_list` 与场景动作的保底触发词典（严格保持 6 秒最小间隔 + 单话上限 8 条）：
     - `心驚` / `驚` / `心跳` / `顫` / `抖` $\rightarrow$ `heart_thump`
     - `啪` / `甩開` / `關門` / `砰` / `撞` $\rightarrow$ `door_slam`
     - `嘎咿` / `吱` / `開門` / `刷` / `咻` $\rightarrow$ `whoosh`
     - `叮` / `嘟` / `嗡` / `訊息` / `手機` $\rightarrow$ `phone_msg`
     - `噠` / `踏` / `嗒` / `脚步` $\rightarrow$ `footstep`
     - `倒吸` / `喘` / `哈` $\rightarrow$ `gasp`
     - `碎` / `嘩啦` / `爆` $\rightarrow$ `crash`
   - 在 `s7_renderer.py` 增加明确运行日志打印：`[s7] 音频混合：bgm={resolved_bgm.name or 'NONE'} ({bgm_gain_db}dB), sfx={len(usable_sfx)}/{len(sfx_events)} 条`。

### 验收测试（`test_patch_v32_audio_mix.py`）
- 验证 `_prepare_audio` 生成的 ffmpeg 命令中，BGM 链包含 `loudnorm` 且默认增益为 `-11.5dB`；SFX 链包含 `adelay` 与 `-3.5dB` 并成功混入主音轨且非静音；
- 验证 `s6_timeline` 能把 `sfx_list: ["心驚", "嘎咿", "啪"]` 分别正确匹配为 `heart_thump` / `whoosh` / `door_slam` 且遵守 `6000ms` 节流。
