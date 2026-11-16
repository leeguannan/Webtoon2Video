# Webtoon2Video 增量重构补丁规格书（Patch v3.0：Story-First 说书体叙事 + 一幕一镜去重 + 抖音不挡脸竖屏版式）

> **适用阶段**：基于真实产出的 `Only Hope` 第 1 话成片（04:58）与 `script_readable.md`（223 条 Beat）深度诊断后制定。  
> **核心产品原则调整（用户最新确认）**：
> 1. **不强行压缩到 60~90 秒**：单话可按剧情自然展开（通常 **2 ~ 4.5 分钟** / 约 **550 ~ 1,100 字** / **30 ~ 65 条 `ScriptBeat`**），前提是**每一句话都在推故事、零“看图写话”废话**。
> 2. **故事为主，画面配合（Story-First, Panels-as-B-Roll）**：先写出“闭眼纯听也能 100% 听懂、人物关系明确、悬念起伏清晰”的完整有声故事正文，再顺位挑最贴合的漫画单帧做视觉配合；**允许跳过 30%~70% 冗余空镜/纯音效格**。
> 3. **一幕严格只出现一个干净画面 + 字幕绝不压脸/压气泡**。
>
> **给 `minimax-3.1-flash` 的铁律**：  
> - **禁止全局重构**：严禁修改下载器（Stage 0/1）、`StoryBibleManager` 记忆读写接口、TTS 多音字主词典架构与前端文件。  
> - **按顺序逐个打补丁**：`Patch 6（切图清缓存+全色去白/黑缝）` $\rightarrow$ `Patch 7（4A 纯故事正文 -> 4B 视觉挂接 + 去拼音校验）` $\rightarrow$ `Patch 8（Stage 7 抖音上下分区防遮挡渲染）`，每完成一个即运行对应 `pytest` 验收。

---

## 问题诊断总览（本次要彻底消灭的 5 个真实 Bug）

1. **旧缓存未清理 + 双轨 ID 导致同一话被播两遍（223 条 Beat）**：`script_readable.md` 前半（`b1..b115`）绑定 `p_001..p_096`，后半（`b116..b223`）又从病房重新绑定 `panel_001..panel_075`，导致成片翻倍到 4 分 58 秒。
2. **一幕同时出现上下两个镜头（`00:00` 走廊+病房、`03:18`/`03:27` 双黑底画面）**：`s2_segmenter.py` 只认纯白带且阈值过窄（`std < 3.0`），遇到 JPEG 噪点白边、纯黑/深灰隔离带就切不断，甚至粘出 4 万像素巨块。
3. **“看图写话”导致不知所云**：原方案强制覆盖全部分镜，模型被迫为每一格写描写（“指尖触碰触控板”、“纯黑如墨吸走呼吸”），开篇不交代人物是谁就直接甩台词，观众完全听不懂在讲什么。
4. **幻觉与拼音泄漏**：把画面装饰拟声词 `心驚` 误读成人名（`“心——敬馬？这名字碎得不成形”`）；`tts_text` 直接写入拼音字母（`hùn xiǎo`、`jié zuò`、`kào xiǎo`），`qwen3-tts-flash` 会报错或逐字母朗读。
5. **字幕直接压在人物嘴唇与原始气泡上（`01:03`、`01:17`）**：缺少独立底部字幕暗区，观感杂乱不适合发抖音。

---

## Patch 6：Stage 2 原子清缓存 + 全色系隔离带精切 + 废帧剔除

**仅修改**：`backend/src/stages/s2_segmenter.py`、`backend/src/stages/s3_vision.py`、`backend/tests/test_s2_segmenter.py`

### 精确改动清单
1. **重跑前强制原子清缓存与统一 `panel_id` 命名（根治 223 条重复）**：
   - `s2_segmenter.py` 启动时，**必须先清空**当前话次的 `02_panels/panels.json` 与 `02_panels/images/` 下的所有旧图片，杜绝新旧分镜混存。
   - **唯一合法 ID 规范**：全局只允许生成 `p_{idx:03d}`（如 `p_001`, `p_002`），彻底禁止产出 `panel_001` 格式；`s3_vision.py` 启动时清空旧 `03_vision/vision.json`，且仅遍历本次 `02_panels/panels.json` 中的 `panel_id`。
2. **全色系均匀隔离带切割（同时切开白带、黑带、灰带与 JPEG 噪点带）**：
   - 保留 Patch 4 第 0 步“先整话统一宽度无缝纵拼，消除 CDN 跨图断镜”。
   - 切割判定不再局限于“纯白”，升级为**任意单色低方差带**：
     ```python
     # 满足以下条件视为有效场景分隔行（兼容白底、黑底、灰底及 JPEG 轻微压缩噪点）
     is_uniform_row = (row_std < 8.0) and (row_diff < 5.0)
     min_gap_px = max(20, int(image_width * 0.025))
     ```
   - 切出原子块后，紧致裁掉上下纯色 Padding（每侧仅保留 `10px` 安全边），消除自带巨型白边/黑边。
   - **巨块强制能量谷兜底**：若单个原子块因无明显纯色带导致高度仍 `> 3200px`，按水平灰度梯度能量最小值（Energy Valley）切开，彻底杜绝 `45,871px` 巨型粘连块。
3. **收紧“独白框向下吸附合并”防双景粘连**：
   - 仅当上方块满足 `is_standalone_text_box == True`（`height < 0.35 * img_width` 且纯黑/纯白底占比 `> 78%`），与下方主画格间距 `< 0.25 * img_width`，**且合并后总高度 `<= 1.65 * img_width`** 时，才允许向下合并；
   - **铁律**：一旦合并后超过 `1.65 * img_width` 或两块之间是长留白/纯黑过渡带，**严禁合并**，保证成片绝对“一幕一画面”。
4. **纯白/纯黑无内容空镜直接过滤 + 超高图切片喂 VLM**：
   - 对切出块计算非背景主像素占比（偏离主背景色 `> 15` 的像素比例）：若 `< 6%`（如全白过渡页、纯黑页），直接标为 `is_transition_blank=True`（或跳过不写入有效剧情池），消灭成片里“白屏空等 3 秒”现象。
   - 对少数真正连续的长画格（`height > 1800px`），Stage 3 送 `qwen3-vl-plus` 时采用 **纵向重叠切片（`tile_height=1600, overlap=250`）** 分别识图后合并同一 `panel_id` 摘要，禁止长边压成 `1280px` 糊图。
5. **Stage 3 明确剥离“拟声词特效字”**：
   - 在 `prompts/vision_extract.md` 增加硬约束：
     - 将漫画内的巨大拟声词/情绪特效字（如 `心驚`、`驚`、`啪`、`咚嗒`）统一收入 `sfx_list`，**绝不允许当作人物姓名或角色台词写入 `dialogues`**；
     - 若某一帧画面**只有纯拟声词/纯背景气氛无人物动作或剧情对话**，标为 `non_story_type="sfx_only"`（或新增枚举值 `"sfx_only"`，需同步更新 `schemas.py` Literal）。

### 验收测试（`test_s2_segmenter.py`）
- 构造 4 个单测并全部通过：
  1. **旧缓存清理测试**：预先在 `02_panels/` 放入 `panel_001.png` 垃圾文件，运行一次 `s2_segmenter` 后断言仅存在 `p_001.png` 等且无 `panel_*` 残留。
  2. **黑底双景切割测试**：上下两个复杂画面中间隔 `30px` 轻微噪点纯黑带（`std=5.0`） $\rightarrow$ 断言拆成 **2 个** `PanelMeta`。
  3. **跨图拼接 + 去白边测试**：保持不切破跨片画格且切出块上下无多余白边。
  4. **纯白空白页过滤测试**：一张纯白中间带极少线条的废格 $\rightarrow$ 断言被标记 `is_transition_blank=True`。

---

## Patch 7：Stage 4 范式反转 ——「4A 纯故事说书正文 $\rightarrow$ 4B 画面顺位配图 + 人工可介入工作流」

**仅修改**：`backend/config.example.yaml`、`backend/src/core/config.py`、`backend/src/core/schemas.py`、`backend/prompts/`、`backend/src/stages/s4_scriptwriter.py`、`backend/src/cli.py`、`backend/tests/test_s4_scriptwriter.py`

### 核心思想变革
以前之所以“像看图写话”，是因为大模型被强迫“不能跳过任何有效分镜”，于是只能对着每一格描细节、凑句子。  
现在改为：**好故事是主角，分镜只是配合讲故事的插图（B-Roll）！**

### 精确改动清单
1. **模型选型与任务分工**：
   - 在 `config.example.yaml` 与 `src/core/config.py` 中新增可选配置项 `story_model: "qwen3.8-max"`（默认设为 **`qwen3.8-max`**，未配置或单话回退时兼容 `script_model` / `qwen-plus`）。
   - `s4_scriptwriter.py` 中：Step 4A（故事编剧，最吃文笔、逻辑与说书沉浸感）默认调用 **`qwen3.8-max`**；Step 4B（把已定稿故事拆拍并与分镜顺位对齐）仍使用 **`qwen-plus`**，兼顾速度与性价比。
2. **Schema 调整（`backend/src/core/schemas.py`）**：
   - `PanelVisionInfo.non_story_type` 扩展为：`Literal["none", "transition_blank", "copyright_notice", "promo_banner", "sfx_only"] = "none"`。
   - `EpisodeStoryDraft` 升级为以**完整可读故事正文**为核心：
     ```python
     class StoryChapter(BaseModel):
         chapter_index: int
         chapter_title: str            # 如 "第一幕：医院病房的冰冷诀别"
         panel_hint_range: tuple[str, str]  # 仅供 4B 缩小检索范围，如 ("p_001", "p_011")
         story_prose: str              # 连贯好听的说书体正文（非看图描述！）

     class EpisodeStoryDraft(BaseModel):
         episode_id: str
         hook_opening: str             # 开篇前 15 秒抓人钩子
         full_story_markdown: str      # 汇总全文，同步落盘为 04_script/story_draft.md 方便人工审阅修改
         chapters: list[StoryChapter]
     ```
3. **Step 4A Prompt 重写（`prompts/story_draft.md` —— 纯粹“说书体”写故事）**：
   - 输入：`StoryBibleV2` + 过滤掉 `non_story_type != "none"` 的全话 `vision` 事实按序列表。
   - **明确写作铁律（写进 Prompt）**：
     1. **盲听可读原则**：假设观众**完全不看屏幕只听音频**，也要 100% 听懂人物是谁、什么关系、为何冲突。开篇第一段必须明确交待登场人物身份与关系（例如：“恩彗来到医院，病床上躺着的是从没给过她好脸色的亲生父亲……”），**严禁无主语直接丢台词**。
     2. **禁止看图写话与凑字数描写**：禁止写“画面中红发女人……”、“指尖触碰触控板发出嗒一声”、“纯黑如墨吸走呼吸”等无意义画外音；只写**推进剧情的行动、情绪张力、因果动机与最点睛的关键对白**。
     3. **时长与篇幅放开但要言之有物**：不强压 60 秒！根据本话剧情密度，写出 **550 ~ 1,100 字**（约 2~4.5 分钟有声故事）的饱满叙事，节奏有铺垫、有冲突、有结尾钩子。
     4. **严禁拟声词幻觉**：明确禁止将 `sfx_list` 中的漫画大字（如 `心驚`、`咚嗒`）当成人名、地名或台词念出来。
   - 同时落盘 `04_script/story_draft.json` 与人类可直接用记事本编辑的 `04_script/story_draft.md`。
4. **Step 4B Prompt 重写（`prompts/script_narrate.md` —— 故事定死，顺位选图配合）**：
   - 输入：`EpisodeStoryDraft`（若用户修改过 `story_draft.md`，优先读取人工改定后的文本！）+ 有效分镜摘要清单。
   - **核心拆拍铁律**：
     1. **严禁篡改故事去凑分镜**：必须以 4A 的连贯故事文本为唯一主干，按自然口播停顿切分为 `ScriptBeat`（每条 10~45 字，通常 **30 ~ 65 条**）。
     2. **按剧情顺位“挑图配合”**：每一条 `ScriptBeat` 从时间顺序往后挑选 `1 ~ 3` 张最贴合当下解说内容的 `panel_id`（`target_panel_ids`）。
        - **强烈鼓励跳过无关/重复/低信息格（允许跳过 30%~70% 分镜）！** 只有最能烘托当前句子情绪或动作的关键画面才出镜；讲一段连续心理或背景介绍时，可以多句连续停留或缓推同一张核心主画面，也可以配一组连续动作快切，**绝不为了“消耗分镜”而硬插废话**！
     3. **`tts_text` 禁拼音硬铁律**：
        - 若字幕为脱敏字或特殊口语（如 `混X`、`靠X`），`tts_text` **必须写成标准汉字同音替代**（如 `"混蛋"`、`"混账"`、`"靠北"`），**严禁出现 `hùn xiǎo`、`jié zuò` 等任何带声调或 ASCII 拼音字母！**
5. **重写代码级校验（`validate_beat_alignment` + `validate_tts_text_no_pinyin`）**：
   - **`validate_beat_alignment(beats, valid_panel_ids)`**：
     - **保留**：① `target_panel_ids` 必须全部存在于有效池 `valid_panel_ids` 中且内部单调递增；② 全局所有 beat 展开后的分镜下标**必须单调非递减**（保证时间线从头到尾顺着走、绝不倒叙穿帮）。
     - **移除**：**彻底删除 Patch 5 中“不能跳过连续 N 个分镜”的覆盖率报错！** 跳过非关键分镜现在是 Story-First 模式的正常核心行为。
   - **`validate_tts_text_no_pinyin(beats)`**：
     - 正则扫描所有 `beat.tts_text`（匹配 `[a-zA-Zāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]{2,}`，排除已知专有名词如 `WEBTOON`、`BGM` 等白名单），一旦命中直接触发重试修正（或自动兜底清除非法拼音）。
6. **支持「大模型出故事初稿 $\rightarrow$ 人工精修文字 $\rightarrow$ 一键从 4B 续跑合成」工作流**：
   - CLI 新增命令（并在 FastAPI 复用该能力）：
     - `w2v story-draft --series <id> --ep <no>`：只跑到 4A，生成 `04_script/story_draft.md` 停下供人工审阅/删改。
     - `w2v narrate-from-story --series <id> --ep <no>`：直接读取本地（人工编辑后的）`04_script/story_draft.md`，执行 Step 4B 顺位配图 + Stage 5~7 配音渲染出片！

### 验收测试（`test_s4_scriptwriter.py`）
1. 验证 `validate_beat_alignment`：**跳过 70% 分镜但保持单调递增（如只选 `p_001, p_005, p_018, p_042`）必须通过**；倒序（如 `p_018 -> p_005`）必须被拦截。
2. 验证 `validate_tts_text_no_pinyin`：出现 `"hùn xiǎo"` 必须报错拦截，出现 `"混蛋"` 必须通过。
3. 验证 `narrate-from-story`：人工修改 `story_draft.md` 内容后运行 4B，输出的 `ScriptBeat` 文本严格忠于人工修改稿。

---

## Patch 8：Stage 7 抖音 `9:16` 安全分区版式（100% 杜绝字幕压脸与气泡遮挡）

**仅修改**：`backend/src/stages/s6_timeline.py`、`backend/src/stages/s7_renderer.py`、`backend/tests/test_s7_renderer.py`

### 精确改动清单
1. **画布分区规范（固定 `1080 × 1920`, `30fps`）**：
   - **底层背景**：当前单帧分镜放大铺满 `1080×1920` 并施加高斯模糊 + 暗化（`boxblur=30:4, eq=brightness=-0.38:saturation=0.75`），营造沉浸暗调质感，彻底消除黑边或白边生硬感。
   - **主画面安全区（严格限制在 `y = 180 ~ 1440`，即居中偏上 `1020 × 1220` 包围盒内）**：
     - 当前单张有效分镜等比缩放进 `1020 × 1220` 安全框内；
     - 运镜（`zoom_in` / `zoom_out` / `pan_down` / `shake`）严格在安全框内先做裁剪再叠加（`overlay=x=(W-w)/2:y=180+(1260-h)/2`），**画面下沿绝不越过 `y = 1440`！**
     - 对超长竖格（`height > 1.5 * width`），`pan_down` 在 `1020 × 1220` 视窗内从顶部匀速平稳扫到底部，绝不在同一屏幕同时露出上一帧的尾巴或下一帧的头。
   - **底部专享字幕安全区（`y = 1460 ~ 1700`）**：
     - 处于主画面底端之下、且位于抖音原生底部标题/头像遮挡区（`y = 1720 ~ 1920`）之上！
     - 更新 `s6_timeline.py` 中生成的 `subtitles.ass` `[V4+ Styles]`：
       - `PlayResX: 1080`, `PlayResY: 1920`
       - `Alignment: 2`（底部居中）
       - `MarginL: 65`, `MarginR: 65`, **`MarginV: 250`**（精确将单行/双行字幕锚定在 `y ≈ 1550 ~ 1670` 的纯暗色区域！）
       - `Fontsize: 56`, `PrimaryColour: &H00FFFFFF`, `OutlineColour: &H00121216`, `BorderStyle: 1`, `Outline: 4`, `Shadow: 2`
       - `WrapStyle: 0`（智能均分换行，尽量控制每行 `<= 16` 字、最多 2 行）。

### 验收测试（`test_s7_renderer.py`）
- 校验生成的 `subtitles.ass` 包含 `PlayResX: 1080`、`PlayResY: 1920`、`MarginV` 在 `220~280` 区间，且 FFmpeg 滤镜链中主画面 `overlay` 的纵向终点坐标严格 `<= 1450`。
