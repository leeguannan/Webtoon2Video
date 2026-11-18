# Webtoon2Video 增量重构补丁规格书（Patch v3.3：方案 A 全量 34+ 音色库扩充 + Stage 4 升级 `qwen4-max` / `qwen3.7-plus`）

> **前置依赖**：建立在 `PATCH_v3.2_VOICE_CAST_AND_AUDIO_UX.md`（Patch 12~14）之上，作为 **`Patch 15`** 增量叠加；**不改 TTS 底层接口协议（继续稳定跑 `qwen3-tts-flash`）**，仅把已验证的官方音色库扩充至全量中文/方言/戏剧角色，并将 Stage 4 模型配置与下拉选项升级至百炼最新 **`qwen4-max`** / **`qwen3.7-plus`**。
>
> **给 `minimax-3.1-flash` 的铁律**：
> - **禁止全局重构**：严禁修改下载器（Stage 0/1）、`s2_segmenter.py`、`s7_renderer.py` 画面版式，以及 Patch 12~14 已验证逻辑。
> - 本补丁只包含 **1 个聚焦模块 `Patch 15`**，可在打完 Patch 12~14 后直接一轮落地并跑通 `pytest`。

---

## 一、Stage 4 解说撰稿升级百炼新旗舰模型的可行性与配置分工

经核实阿里云百炼最新模型列表，**完全支持**使用 **`qwen4-max`** 与 **`qwen3.7-plus`**，且对 **Story-First 说书编剧** 提升明显：

| 环节 | 推荐模型 ID | 职责与优势 | 关键工程保护 |
| :--- | :--- | :--- | :--- |
| **Step 4A（说书体故事编剧）** | **`qwen4-max`**（备选 `qwen3.8-max`） | 最新一代旗舰，人物关系逻辑、长距离伏笔防剧透、悬疑铺陈文笔明显强于 `qwen-plus` / `qwen3.8-max` | **必须透传 `enable_thinking=False`**（防止默认长链思考撞穿 `180s` 请求超时） |
| **Step 4B（拆拍配图 + 引号标注）** | **`qwen3.7-plus`**（备选 `qwen-plus`） | 新一代均衡主力，JSON Schema 遵循度、长列表分镜顺位对齐与 `speaker_gender` 标注更稳 | 单次调用快、成本远低于 `max` 旗舰，性价比最高 |

> **安全回退机制（防部分账号未开通新模型权限报错）**：若配置了 `qwen4-max` 但接口返回 `404 ModelNotFound` / `InvalidParameter` / `AccessDenied`，自动记录告警并允许按配置回退链路 `qwen4-max -> qwen3.8-max -> qwen-plus`（或按配置明确报错提示去百炼控制台开通对应模型授权）。

---

## 二、Patch 15：全量 `qwen3-tts-flash` 中文/方言音色目录 + Stage 4 模型升级

**仅修改**：
- `backend/config.example.yaml`
- `backend/src/core/config.py`
- `backend/src/providers/tts_qwen.py`
- `backend/src/stages/s4_scriptwriter.py`
- `backend/src/web/server.py`
- `backend/scripts/init_voice_samples.py`
- （前端对应页面，如仓库内包含 `frontend/`）更新模型下拉与分组音色选择器
- `backend/tests/test_patch_v33_voices_and_models.py`（新增单测）

### 精确改动清单

#### 1. `qwen3-tts-flash` 全量中文/方言/戏剧音色目录（34 个官方实名音色）

在 `backend/src/providers/tts_qwen.py`（并由 `web/server.py` `GET /api/tts/voices` 统一读取导出）中定义结构化常量 `QWEN3_TTS_VOICES`，每项包含：`voice_id`、`display_name`、`gender`（`male`/`female`）、`age_group`（`child`/`youth`/`middle`/`elder`）、`category`（`narrator`/`character`/`dialect`/`drama`）、`description`：

- **男声 — 普通话主旁白与青年/大叔/老者（11 个）**：
  1. `Elias`（墨讲师｜男｜中年｜`narrator`）—— 磁性沉稳，适合悬疑/剧情漫评主旁白
  2. `Neil`（阿闻｜男｜青年/中年｜`narrator`）—— 标准主播腔，字正腔圆、语流利落
  3. `Kai`（凯｜男｜青年｜`narrator`）—— 温和磁性“耳朵SPA”，适合情感/治愈线旁白
  4. `Ethan`（晨煦｜男｜青年｜`character`）—— 阳光标准普通话，适合年轻男主
  5. `Moon`（月白｜男｜青年｜`character`）—— 清冷帅气，适合高冷/腹黑男主
  6. `Vincent`（田叔｜男｜中年/大叔｜`character`）—— 沙哑烟嗓大叔，极适合父亲/boss/反派
  7. `Eldric Sage`（沧明子｜男｜老年｜`character`）—— 沉稳睿智老者、师尊/长辈
  8. `Arthur`（徐大爷｜男｜老年｜`narrator`）—— 沧桑质朴说书老人
  9. `Nofish`（不吃鱼｜男｜青年｜`character`）—— 幽默个性男配角
  10. `Mochi`（沙小弥｜男｜少年/童声｜`character`）—— 机灵聪明少年音
  11. `Pip`（调皮小新｜男｜男童｜`character`）—— 活泼调皮男童声

- **女声 — 普通话女主/御姐/少女/老妇（15 个）**：
  12. `Cherry`（芊悦｜女｜青年｜`character`）—— 阳光明亮亲切女声，适合现代女主
  13. `Serena`（苏瑶｜女｜青年｜`narrator`）—— 温柔小姐姐，适合言情女旁白
  14. `Seren`（小婉｜女｜青年｜`narrator`）—— 舒缓柔声讲故事
  15. `Maia`（四月｜女｜青年/熟女｜`narrator`）—— 知性优雅女声
  16. `Mia`（乖小妹｜女｜青年｜`character`）—— 温顺乖巧甜美少女
  17. `Bellona`（燕铮莺｜女｜青年/中年｜`character`）—— 洪亮正统有气场，适合大女主/女反派
  18. `Katerina`（卡捷琳娜｜女｜熟女｜`character`）—— 成熟有韵味女声，适合继母/反派女
  19. `Vivian`（十三｜女｜青年｜`character`）—— 自信可爱带小脾气
  20. `Momo`（茉兔｜女｜青年｜`character`）—— 俏皮搞怪闺蜜
  21. `Chelsie`（千雪｜女｜少女｜`character`）—— 二次元软萌少女
  22. `Bella`（萌宝｜女｜少女/萝莉｜`character`）—— 古灵精怪萌系少女
  23. `Bunny`（萌小姬｜女｜萝莉｜`character`）—— 可爱小萝莉
  24. `Nini`（邻家妹妹｜女｜少女｜`character`）—— 自然邻家妹妹
  25. `Stella`（美少女阿月｜女｜少女｜`character`）—— 甜美二次元美少女
  26. `Ebona`（诡婆婆｜女｜老年｜`character`）—— 悬疑故事老年女声

- **中国方言与剧场张力特色音色（8 个，供特殊人设与搞笑/悬疑反派选用）**：
  27. `Dylan`（北京晓东｜男｜方言）—— 北京胡同青年
  28. `Jada`（上海阿珍｜女｜方言）—— 上海话特色女声
  29. `Sunny`（杭州阿晴｜女｜方言）—— 温柔江南口音
  30. `Eric`（四川程川｜男｜方言）—— 四川话特色男声
  31. `Marcus`（陕西秦川｜男｜方言）—— 陕西硬汉男声
  32. `Li`（南京老李｜男｜方言）—— 南京话大叔
  33. `Ryan`（甜茶｜男｜剧场张力）—— 强烈戏剧节奏与爆发张力男声
  34. `Jennifer`（詹妮弗｜女｜电影质感）—— 电影叙事质感音色

> **额外兼容性**：`SeriesConfig` / `VoiceSpec.voice` **不做死枚举限制**，只要是阿里云官方支持的其余外语/新上线 `qwen3-tts-flash` 音色 ID（共 49 种）均可直接手填透传使用！

#### 2. `init_voice_samples.py` 分级生成静态试听样例
- 支持 CLI 参数：
  - 默认 / `--all`：依次为上述 **34 个音色** 生成一次静态样例 `backend/assets/voice_samples/{voice_id}.wav`（含 4~6 秒自然试听文本，**文件已存在则跳过**，失败单条不中断整体）；
  - `--tier core`：仅生成高频 14 个核心音色（快速起步）；
  - 并在样例文件名安全化上做处理：注意 `Eldric Sage` 含空格，接口路径 `/api/tts/samples/{voice_id}` 与本地缓存文件名统一安全映射（或保持 `voice_id` 原样 URL 编码，如 `Eldric%20Sage` $\rightarrow$ 文件 `Eldric_Sage.wav`，解析函数同时兼容两种写法！）。

#### 3. Stage 4 模型升级与下拉更新
- **`config.example.yaml` & `core/config.py`**：
  ```yaml
  models:
    vision: qwen3-vl-plus
    # Step 4A 说书体故事编剧（推荐旗舰 qwen4-max；若账号暂未开通可填 qwen3.8-max）
    story_model: qwen4-max
    # Step 4B 拆拍配图与记忆压缩（推荐新一代均衡主力 qwen3.7-plus，或填 qwen-plus）
    script: qwen3.7-plus
    tts: qwen3-tts-flash
  ```
- **`stages/s4_scriptwriter.py`**：
  - 所有 `qwen4-*` / `qwen3.8-*` / `qwen3.7-*` 模型调用统一携带 `_NO_THINKING_KWARGS = {"enable_thinking": False}`；
  - 若 `story_model` 调用遇到 `404` / 模型未开通 / 权限报错，打印清晰警告并自动按配置回退链 `("qwen3.8-max", "qwen3.7-plus", "qwen-plus")` 降级重试（仅限配置未锁死时，保证不因新模型未开通直接挂死整话！）；单测验证回退逻辑与默认模型配置。
- **`web/server.py`（及对应前端页面）**：
  - `GET /api/config` / 可选模型元数据列表中，`story_model` 推荐选项包含：`qwen4-max`、`qwen3.8-max`、`qwen3.7-plus`、`qwen-plus`；
  - `script_model` 推荐选项包含：`qwen3.7-plus`、`qwen-plus`、`qwen3.8-max`。

### 验收测试（`test_patch_v33_voices_and_models.py`）
1. 验证 `QWEN3_TTS_VOICES` 至少包含 **34 个**不重复 `voice_id`，且 `Neil`、`Vincent`、`Eldric Sage`、`Mia`、`Seren`、`Bellona`、`Elias`、`Katerina`、`Cherry` 等字段完整。
2. 验证含空格音色 `Eldric Sage` 的样例文件名映射（`Eldric_Sage.wav`）与 `/api/tts/samples/Eldric%20Sage` 解析一致。
3. 验证 `ModelConfig` 默认 `story_model == "qwen4-max"`、`script == "qwen3.7-plus"`，且当 mock LLM 对 `qwen4-max` 抛出模型未开通异常时，Stage 4A 能日志告警并平滑回退到 `qwen3.8-max` / `qwen-plus` 完成产出。
