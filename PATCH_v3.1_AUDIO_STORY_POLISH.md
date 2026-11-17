# Webtoon2Video 增量优化补丁规格书（Patch v3.1：男声说书多角色配音 + 无版权 BGM/音效混音 + 剧情精修与 Bug 修复）

> **适用阶段**：基于 GitHub 仓库（commit `7d1ddb9`）实查 + `wt_5081_ep_001` 最新成片（`06:10`）逐帧验收后制定。  
> **核心目标**：
> 1. **修掉 4 个已定位的真实工程 Bug + 字幕缺字方块（`☒`）**。
> 2. **把单人女声平铺朗读升级为「男声磁性主旁白（`Elias`）+ 多角色差异化配音（`voice_cast`）」**。
> 3. **打通「零版权现成可用」的 BGM 与拟声音效自动装配管线**（自带 CC0 程序化素材生成脚本 + 无缝支持 Pixabay/Mixkit 无版权 `.mp3` 替换 + Stage 7 自动侧链闪避混音）。
> 4. **彻底消灭“他/她”指代混乱、人物误认、纯字卡当 PPT 播、慢动作凑帧与元词汇看图写话**，将单话节奏收紧到 **2.5 ~ 3.5 分钟（35 ~ 50 条 Beat）**。
>
> **给 `minimax-3.1-flash` 的铁律**：
> - **禁止全局重构**：严禁修改下载器（Stage 0/1）、`StoryBibleManager` 记忆架构、前端 `src/` 文件，以及 Patch v3.0 已验证通过的 `1080×1920` 上下分区画面版式与 `pre-rendered background + 8-scene segment` 渲染架构。
> - **按顺序分 3 轮打补丁**：`Patch 9（4 处代码 Bug 修复 + 字幕/TTS 缺字净化 + 废帧拦截）` $\rightarrow$ `Patch 10（Story-First 2.0：防错认、防他/她混乱、纯字卡跳过、4B 角色与音效标定）` $\rightarrow$ `Patch 11（男声多角色配音路由 + 无版权 BGM/SFX 自动混音）`，每轮结束跑通对应 `pytest` 验收。

---

## 问题清单总览（实查代码 + 成片复盘）

| 类别 | 发现的问题 | 根因定位 | 归属补丁 |
| :--- | :--- | :--- | :--- |
| **工程 Bug 1** | Web UI 跑 Stage 4A 静默回退 `qwen-plus` | `web/job_runner.py` 未透传 `config.models.story_model` | Patch 9 |
| **工程 Bug 2** | 超长竖格尾部留白会把整格误标 `sfx_only` 整块丢掉 | `s3_vision.py` `_merge_tile_payloads` 任一切片 `sfx_only` 即覆盖全格 | Patch 9 |
| **工程 Bug 3** | 章节超过 40 格时切块喂同一份 `story_prose` 导致复读 | `s4_scriptwriter.py` `CHAPTER_MAX_PANELS = 40` 过小 | Patch 9 |
| **工程 Bug 4** | 200px 左右纯黑过渡页 / 窄缝黑条漏进成片（如 `03:00`） | `s2_segmenter.py` 对带边距 `panel` 调 `is_contentless_panel`，且未过滤窄缝图 | Patch 9 |
| **字幕/TTS Bug** | `03:46` 出现 **`☒`** 方块且 TTS 跳过该字符 | 台词带 `♥` 等字体缺失符号，未在进字幕/TTS 前清洗 | Patch 9 |
| **剧情质量 1** | “他/她”过多分不清谁对谁 | `story_draft.md` 缺少禁止连续代词开头与强制点名规范 | Patch 10 |
| **剧情质量 2** | `05:53` 红光地下室把灰发女主「恩彗」错认成「继母」 | `vision_extract.md` 缺发色/光照/空间隔离约束；4A 未校验同场人物 | Patch 10 |
| **剧情质量 3** | 纯黑/白字卡当 PPT 播；吻戏连拖 6 帧；念出「手写体/二字/枯叶」 | 缺少 `is_pure_text_card` 标记与元词汇禁令；4B 条数预算偏松 | Patch 10 |
| **音频体验 1** | 全程单女声 `Cherry` 平铺，故事代入感弱、角色台词不区分 | `tts_qwen.py` 默认 `Cherry`，`s5_tts.py` 未按 `speaker_role` 路由音色 | Patch 11 |
| **音频体验 2** | 成片只有干声人声，缺乏 BGM 与关键动作音效 | `assets/` 素材库缺失，未传 `--bgm` 时未启用默认 BGM，缺 SFX 混音链 | Patch 11 |

---

## Patch 9：4 个实查工程 Bug 修复 + 废帧过滤 + 字幕/TTS 字符净化

**仅修改**：
- `backend/src/web/job_runner.py`
- `backend/src/stages/s2_segmenter.py`
- `backend/src/stages/s3_vision.py`
- `backend/src/stages/s4_scriptwriter.py`
- `backend/src/core/normalize.py`
- `backend/src/core/subtitle_render.py`
- （删除废弃文件）`backend/prompts/script_beats.md`
- `backend/tests/test_patch_v31_hygiene.py`（新增单测）

### 精确改动清单

1. **修复 `web/job_runner.py` 漏传 `story_model`**：
   - 在 `_stage_script` 与调用链（约 L350~352、L540~566）中读取 `story_model = (series_cfg.model_overrides.get("story_model") or self.config.models.story_model or script_model)`，并将其作为 `story_model=story_model` 传入 `s4_scriptwriter.run_stage4(...)`（与 `cli.py` 行为完全对齐）。

2. **修复 `s3_vision.py` `_merge_tile_payloads` 误标 `sfx_only`（约 L357~363）**：
   - 将当前「任一切片 `sfx_only` 就整格标 `sfx_only`」逻辑改为：
     - 收集所有切片的 `non_story_type` 到 `tile_types`；
     - 若存在 `NON_STORY_BANNER_TYPES`（版权页/引流横幅），仍按 banner 处理；
     - **仅当 `not dialogues` 且 `all(t in NON_STORY_SFX_TYPES for t in tile_types)` 时，整格才标 `sfx_only`**；只要任一切片含真实人物动作或对白，`non_story_type` 必须保持 `"none"`。

3. **修复 `s4_scriptwriter.py` `CHAPTER_MAX_PANELS` 切块重复播报（L70）**：
   - 将 `CHAPTER_MAX_PANELS` 从 `40` 提升到 **`120`**（并保持若万一超限拆片时把 `chapter.story_prose` 按比例切片，避免每片复述全章）。

4. **修复 `s2_segmenter.py` 过渡暗块漏网与超窄装饰条（L625 附近）**：
   - 将 `is_contentless_panel(panel)` 改为 **`is_contentless_panel(inner)`**（使用已经裁去上下 `PANEL_WHITE_MARGIN_PX=10` 边界的 `inner`，与 `is_blank_panel(inner)` 一致）。
   - 新增**超窄装饰横条过滤**：若某块裁边后有效高度 `< 150` 像素 **且** 边缘密度（`canny_edge_density(inner)`）`< 0.015`（或宽高比 `width / height > 7.5` 且不含文字框特征），直接标为 `is_transition_blank=True`（不再让类似 `03:00` 的细窄色条单独成一幕）。
   - 删除未被引用的旧残留模板 `backend/prompts/script_beats.md`（先用 `grep` 确认测试与代码均未引用后删除）。

5. **彻底修复字幕方块 `☒` 与 TTS 吞字（`core/normalize.py` + `core/subtitle_render.py`）**：
   - 在 `core/normalize.py` 新增统一净化函数 `sanitize_narration_and_tts(text: str, *, for_tts: bool = False) -> str`：
     - 把常见情绪/排版符号映射或清除：
       - 删除或替换中文字体大概率缺失、且无法发音的符号：`♥`、`♡`、`❤`、`❥`、`★`、`☆`、`♪`、`♫`、`♬`、`✦`、`✧`、`※`、`☒`、`☐`、`□`、`■`、`◆`、`◇`、`●`、`○`；
       - 过滤 Emoji 区间（`U+1F300–U+1FAFF`、`U+2600–U+27BF`、`U+FE0F` 变体选择符）；
       - 把非常见波浪/破折号异常组合归一化（如连续 `~` 保留 1 个；`for_tts=True` 时去掉末尾多余 `~` 以免合成怪音）。
     - 在 `normalize_script_payload` 中对每条 beat 的 `narration_text` 与 `tts_text` 均执行净化。
   - 在 `core/subtitle_render.py` 渲染 PNG 前再做一次兜底过滤：遍历字符串字符，若包含会触发方块的控制符/私用区字符（`U+E000–U+F8FF`）或未覆盖符号，先剔除再 `wrap_text` / `draw.text`，保证永远不画出 `☒`。

### 验收测试（`test_patch_v31_hygiene.py`）
- 传入含 `"今天在别墅过如何♥★☒~"` 的脚本经过 `normalize_script_payload` 后，`narration_text` 与 `tts_text` 均不含 `♥`/`★`/`☒` 等缺字符号；
- `_merge_tile_payloads` 收到 `[tile1(有对白, "none"), tile2(无对白, "sfx_only")]` 时，合并结果 `non_story_type == "none"` 且 `dialogues` 完整保留；
- `s2_segmenter` 对带 `10px` 边距的 `220px` 纯黑过渡页及 `80px` 超窄暗条，必须标记 `is_transition_blank=True`。

---

## Patch 10：Story-First 2.0 —— 消灭“他/她”混乱、人物错认、纯字卡 PPT 与元词汇

**仅修改**：
- `backend/src/core/schemas.py`
- `backend/src/core/normalize.py`
- `backend/src/stages/s3_vision.py`
- `backend/src/stages/s4_scriptwriter.py`
- `backend/prompts/vision_extract.md`
- `backend/prompts/story_draft.md`
- `backend/prompts/script_narrate.md`
- `backend/tests/test_s4_scriptwriter.py`

### 精确改动清单

1. **Schema 扩展（`backend/src/core/schemas.py`）—— 保持完全向后兼容**：
   - `PanelVisionInfo` 新增：
     ```python
     is_pure_text_card: bool = False  # 仅包含大段黑白/纯色旁白字卡、无角色表情/动作
     ```
   - `ScriptBeat` 新增可选字段：
     ```python
     sfx_cue: str | None = None  # 可选音效触发标签，如 heart_thump / door_slam / whoosh / gasp / phone_msg / footstep / crash
     ```
   - `SfxEvent` 与 `RenderTimeline.sfx_events`：
     ```python
     class SfxEvent(BaseModel):
         sfx_key: str
         start_ms: int
         gain_db: float = -10.0

     # RenderTimeline 新增：
     sfx_events: list[SfxEvent] = Field(default_factory=list)
     ```

2. **Stage 3 强化角色外观/空间识别 + 纯字卡标定（`prompts/vision_extract.md` + `s3_vision.py`）**：
   - 在 `prompts/vision_extract.md` 新增硬约束：
     - **人物发色与光照剥离**：描述画面人物时，必须区分「固有发色/发型/衣着」与「环境打光（如红光/蓝光/暗影）」，例如浅灰长发少女在红色警示灯下依然是浅灰发，**绝不能因为环境偏红就把她当成红发成年女性（如继母）**。
     - **所处空间必填**：`scene_summary` 开头需简短点明场景地点（如「病房内」「医院走廊」「红光地下室」「客厅沙发」），帮助下游保持空间连贯。
     - **`is_pure_text_card` 判定**：若该格主体只是一块文字框/内心独白卡片（无人物面部特写或肢体动作），`is_pure_text_card` 设为 `true`。
   - `s3_vision.py` 在构造输出时保留并合并 `is_pure_text_card`（切片场景仅当全部切片均为纯字卡且无动作时才为 `True`）。

3. **Stage 4A Prompt 升级（`prompts/story_draft.md`）—— 解决代词混乱、人物错认、元词汇**：
   - 新增/强化 **4 条写作红线**：
     1. **严禁“他/她”连环糊涂账**：
        - 绝不允许连续两句都以“他”或“她”开头；
        - 同一段落涉及 2 位以上角色时，**优先使用人物姓名或清晰身份称谓**（如“恩彗”“继母”“道玄”“父亲”），确保观众闭眼听 100% 知道“谁对谁做了什么”。
     2. **同场人物空间一致性校验**：
        - 严格核对前序分镜与当前格地点！未明确到场的角色（例如一直留在病房的继母）**绝不能**突然出现在另一个独立空间（如主角独处的地下室）里。
     3. **绝对禁用元词汇与无中生有**：
        - **禁用黑名单词**：`手写体`、`二字`、`字幕`、`幕布`、`画面中`、`画面上`、`这一格`、`镜头`、`气泡`、`文字框`、`对话框`；
        - 禁止把画面的字/标点拆出来当实体念（如“‘倒下’二字砸进沉默”、“手写体‘停住’悬在眼前”）；
        - 禁止对纯黑/暗色衣物或背景纹理脑补无关物体（如把暗色褶皱/光斑脑补成“枯叶”）。
     4. **字数与节奏自检**：
        - 单话完整故事控制在 **500 ~ 900 字**，不拖沓、不重复描写同一瞬间（比如一个拥吻动作最多 1~2 句推进关系，不要连写 6 句拆解手指、嘴唇、呼吸）。

4. **Stage 4B Prompt 与配额收紧（`prompts/script_narrate.md` + `s4_scriptwriter.py`）**：
   - **`s4_scriptwriter.py` 条数预算收紧**：
     - 全话总 Beat 目标收紧为 **35 ~ 50 条**（每章按剧情占比动态分配，使成品自然落在 **2.5 ~ 3.5 分钟**）；
   - **`prompts/script_narrate.md` 升级**：
     - **纯字卡避让规则**：`PANELS` 清单中带有 `[纯字卡]` 标记的分镜，**尽量跳过不选**；把内心独白或故事旁白优先挂到相邻的人物表情/动作格上，彻底消灭“白底黑字 PPT 朗读”；
     - **多角色 `speaker_role` 标定**：
       - 旁白叙述一律填 `"narrator"`；
       - 直接以角色第一人称/口吻说出的独立对白句，按角色属性准确标注为：
         - `"female_lead"`（女主角，如恩彗）
         - `"male_lead"`（男主角，如道玄）
         - `"female_mature"`（成熟女性/继母/反派女）
         - `"male_antagonist"`（反派男/威严男性长辈）
       - 注意：只有**纯角色原话台词**才用角色声线，带“恩彗心想：……”或第三人称转述的句子仍用 `"narrator"`（避免人称与声线冲突）。
     - **可选音效提示 `sfx_cue`**：仅在剧情高潮或明显物理动作点输出 1 个标准 key（`heart_thump` / `door_slam` / `whoosh` / `gasp` / `phone_msg` / `footstep` / `crash`），普通对话与叙述留空或省略（**全话最多 4~8 处，切忌每句都加**）。

5. **代码级拦截黑名单词（`s4_scriptwriter.py`）**：
   - 新增轻量文本自检：若 4A 产物中出现明显元词汇（如 `"手写体"`、`"二字砸"`、`"幕布"`、`"画面中"`）或连续 3 句以上以 `"他"`/`"她"` 开头，在校验日志告警并可附带反馈重试 1 次（非阻断兜底）。

### 验收测试（`test_s4_scriptwriter.py`）
- 模拟包含 `is_pure_text_card=True` 的输入清单，验证构建给 4B 的候选面板摘要明确标记 `[纯字卡]` 且过滤掉 `is_transition_blank=True` 的窄条；
- 验证 `ScriptBeat` 能解析 `speaker_role="male_lead"` 与 `sfx_cue="heart_thump"`，且旧版无 `sfx_cue` 的 `script.json` 仍能零报错反序列化。

---

## Patch 11：男声说书多角色配音路由 + 无版权现成 BGM & 音效自动混音

**仅修改**：
- `backend/config.example.yaml`
- `backend/src/core/config.py`
- `backend/src/providers/tts_qwen.py`
- `backend/src/stages/s5_tts.py`
- `backend/src/stages/s6_timeline.py`
- `backend/src/stages/s7_renderer.py`
- `backend/src/cli.py`
- `backend/src/web/job_runner.py`
- `backend/scripts/init_audio_assets.py`（新增无版权音频生成脚本）
- `backend/tests/test_patch_v31_audio.py`（新增单测）

### 精确改动清单

#### A. 配音音色：默认男声说书 + `voice_cast` 多角色路由

1. **百炼 `qwen3-tts-flash` 推荐音色映射（全部为阿里云百炼官方系统音色 ID）**：
   - **`Elias`**（墨讲师）：磁性沉稳男声，带强叙事讲故事感 —— **主旁白 `narrator` 默认音色**；
   - **`Ethan`**（晨煦）：标准普通话阳光青年男声 —— **年轻男主 `male_lead`**；
   - **`Ryan`**（甜茶）：戏剧张力与节奏感强男声 —— **反派男 / 冲突男声 `male_antagonist`**；
   - **`Cherry`**（芊悦）：明亮自然的年轻女声 —— **女主 `female_lead`**；
   - **`Katerina`**（卡捷琳娜）：成熟知性带余韵女声 —— **继母 / 成熟女性角色 `female_mature`**。
   - *(注：如需方言硬汉反派，用户也可在配置里手工填 `Marcus`，但默认组合全部选用纯净标准普通话音色。)*

2. **配置与模型改动**：
   - `providers/tts_qwen.py`：
     - 将 `DEFAULT_VOICE` 从 `"Cherry"` 改为 **`"Elias"`**；
     - 修复 `_download_audio`（L209、L223）返回的 `TTSResult(voice=...)`：传入实际合成时使用的 `effective_voice = voice or self._voice`，而不是写死 `self._voice`。
   - `core/config.py` & `config.example.yaml`：
     - 新增 `TTSConfig`（挂在 `AppConfig.tts` 下）：
       ```python
       class TTSConfig(_StrictModel):
           default_voice: str = "Elias"
           voice_cast: dict[str, str] = Field(default_factory=lambda: {
               "narrator": "Elias",
               "male_lead": "Ethan",
               "male_antagonist": "Ryan",
               "female_lead": "Cherry",
               "female_mature": "Katerina",
               "male": "Ethan",
               "female": "Cherry",
           })
       ```
     - `SeriesConfig` 同步新增 `voice_cast: dict[str, str] = Field(default_factory=dict)`（作品级覆盖全局音色表，如特定角色 `恩彗: Cherry`）。
     - `RenderConfig` 新增：
       ```python
       auto_bgm: bool = True
       default_bgm: str = "suspense"   # 对应 assets/bgm/suspense.(mp3|wav)
       enable_sfx: bool = True
       sfx_gain_db: float = -10.0
       ```
   - `stages/s5_tts.py`：
     - 新增 `resolve_beat_voice(beat: ScriptBeat, *, default_voice: str | None = None, voice_cast: Mapping[str, str] | None = None) -> str | None`：
       - 优先级：命令行显式 `--voice`（若用户强行单音色锁死）？**注意**：建议若传入 `voice_cast`，默认按 `beat.speaker_role` 查 `voice_cast`（先精确匹配，再小写匹配，未命中回退 `voice_cast.get("narrator") or default_voice`）；若命令行/Web 显式指定了单一覆盖开关则尊重显式设置。
     - `_synthesize_beat`、`synthesize_episode`、`run_stage5` 透传 `default_voice` 与 `voice_cast`，并将 `beat` 实际所用音色写入 `AudioClipMeta`（或在日志/issues 留痕）。
   - `cli.py` 与 `web/job_runner.py`：
     - `_make_tts_client()` 与 Stage 5 调用处合并 `config.tts.voice_cast` + `series_config.voice_cast`，默认值使用 `config.tts.default_voice`（`"Elias"`）。

#### B. 无版权 / 现成 BGM 与音效自动装配管线

1. **双源即插即用目录规范（位于 `backend/assets/`，完全独立于代码）**：
   - **`backend/assets/bgm/`**：存放循环铺底音乐，按优先级匹配 `.mp3` / `.m4a` / `.wav` / `.ogg`：
     - `suspense`（悬疑/暗黑漫评默认）
     - `emotional`（情感/回忆）
     - `action`（爽文/冲突）
     - `default`（兜底通用）
   - **`backend/assets/sfx/`**：存放 0.3s ~ 1.8s 短音效：
     - `heart_thump`（心惊重拍/心跳低鸣）
     - `door_slam`（摔门/重响）
     - `whoosh`（转场划过）
     - `gasp`（倒吸冷气/惊喘氛围音）
     - `phone_msg`（手机短信提示叮咚）
     - `footstep`（脚步声）
     - `crash`（碎裂/撞击）

2. **自带零版权「现成生成脚本」`backend/scripts/init_audio_assets.py`（100% CC0，跑完即有声）**：
   - 使用 Python 标准库 + `numpy` + 本地 `ffmpeg`（不拉任何外网链接，**100% 无版权争议**）一键生成可直接商用的**柔和环境氛围 BGM + 7 种基础物理合成音效**到 `backend/assets/bgm/` 与 `backend/assets/sfx/`：
     - `bgm/suspense.wav`（约 45 秒可无缝循环：温暖低频 Sub-bass 脉冲 + 轻微空气感泛音 Pad，自带淡入淡出循环边界，**绝无刺耳正弦波鸣叫**）；
     - `bgm/emotional.wav`（柔和电钢和弦分解氛围 Pad，适合情感/家庭线）；
     - `sfx/heart_thump.wav`（双连低频心跳重击 `lub-dub`，60Hz→35Hz 包络衰减）；
     - `sfx/door_slam.wav`（短脉冲瞬态 + 滤波白噪闷响）；
     - `sfx/whoosh.wav`（带通扫频白噪转场气声）；
     - `sfx/phone_msg.wav`（清脆双音阶提示音）；
     - `sfx/footstep.wav`（短木质闷击脚步声）；
     - `sfx/gasp.wav`（短促高频气流扫频转折提示）；
     - `sfx/crash.wav`（高频碎裂噪点衰减包络）。
   - **重要设计**：脚本默认**不覆盖用户自己放入的同名文件（如用户放了 `suspense.mp3`）**；且解析器按 `.mp3 > .m4a > .wav` 优先，用户只要从 **Pixabay / Mixkit / Freesound (CC0)** 下载自己喜欢的无版权 `.mp3` 拖进 `backend/assets/bgm/` 或 `backend/assets/sfx/`，无需改一行代码立即生效！

3. **Stage 6 时间轴触发音效事件（`s6_timeline.py`）**：
   - 在 `build_timeline(...)` 中生成 `sfx_events: list[SfxEvent]`：
     - 主来源：`beat.sfx_cue`（当不为 `None`/`"none"` 时触发，时间落在 `beat_start`）；
     - 辅助来源（兜底）：若 `beat.sfx_cue` 为空，可按该 beat 绑定的主 `panel_id` 在 `vision.json` 中的 `sfx_list` 做保守关键词映射（例如含 `心驚`/`驚` → `heart_thump`，`叮`/`短信` → `phone_msg`，`砰`/`啪`/`关门` → `door_slam`，`咚`/`踏` → `footstep`）；
     - **防吵闹节流铁律**：全局任意两条 `sfx_events` 间距 **`>= 6000 ms`（6 秒）**，且整话最多 **8 个音效**，宁缺毋滥。

4. **Stage 7 BGM 自动兜底 + SFX 混音（`s7_renderer.py`）**：
   - **BGM 解析优先级**：
     1. 显式参数 `--bgm` / API `bgm_path`；
     2. 作品配置 `series_config.yaml` 的 `bgm`；
     3. 若 `config.render.auto_bgm == True`，自动去 `backend/assets/bgm/` 查找 `config.render.default_bgm`（依次找 `.mp3/.m4a/.wav/.ogg`，再回退目录下任意首个音频文件）。
   - **SFX 混入 `_prepare_audio`**：
     - 当 `timeline.sfx_events` 非空且 `config.render.enable_sfx == True` 时，解析存在的音效文件，按 `event.start_ms` 用 `adelay={start}|{start},volume={sfx_gain_db}dB` 对齐，先与 BGM 或独立总线混合输出 `[sfx]`，再与 `[voice]` / `[bgmv]` 一起输出（**人声始终做 `sidechaincompress` 侧链主控，确保旁白永远压过 BGM 与音效**）。

### 验收测试（`test_patch_v31_audio.py`）
1. **多角色音色路由测试**：构造含 `speaker_role` 分别为 `narrator`、`female_lead`、`male_lead`、`female_mature`、`unknown_role` 的 5 条 beat，断言 `s5_tts` 传给 TTS Client 的 `voice` 分别为 `Elias`、`Cherry`、`Ethan`、`Katerina`、`Elias`（回退默认男声），且 `TTSResult.voice` 与传入一致。
2. **默认素材生成与防覆盖测试**：运行 `init_audio_assets.py` 生成全部 BGM/SFX `.wav` 文件且均可被 `wave` 正常读取时长 `> 0`；提前写入自定义 `suspense.mp3` 占坑后，资源解析器优先返回 `.mp3`。
3. **SFX 节流与混音命令测试**：构造间隔小于 `6000ms` 的 4 个触发点，验证 `s6_timeline` 节流后按 `>= 6000ms` 间隔保留合理数量；验证 `s7_renderer` 在未显式传入 `bgm_path` 且存在默认素材时自动挂载 BGM 与 `sfx_events` 混音链。
