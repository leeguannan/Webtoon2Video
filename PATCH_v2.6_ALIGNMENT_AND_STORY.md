# Webtoon2Video 增量优化补丁规格书（Patch v2.6：切图粒度修复 + 故事反写与音画严密对齐）

> **适用阶段**：流水线已大体跑通，专门用于解决**「分镜拆合不准（一图多镜/独白黑框被拆碎）」**与**「解说词和画面不对应、叙事断层」**两大核心体验问题。  
> **给 `minimax-3.1-flash` 的铁律**：  
> 1. **禁止全局重构**：严禁改动下载器（Stage 0/1）、记忆库读写接口、TTS 多音字词典逻辑及前端 UI。  
> 2. **逐项打补丁**：先完成 Patch 4（Stage 2 切图与单元测试），跑通后再做 Patch 5（Stage 4 两段式故事反写 + 单调对齐校验）。

---

## Patch 4：Stage 2 全话纵拼 + 语义感知二次裁剪（彻底解决“跨图断镜 / 一图多镜 / 独白合并”）

### 问题根因
1. **一个镜头被 CDN 拆成两张下载图**：WEBTOON 平台会按固定高度机械切割原始长条漫画，导致一张脸或一个气泡常被劈成两张下载切片（若不先拼接再切，必然出现断镜）。
2. **一张图里包含两个镜头（图 1）**：同一张切片内有明显纯白隔离带，若空白阈值过大就不会拆开。
3. **黑框内心独白被切碎（图 2）**：纯白像素切割会把上方的黑底独白框单独切出一张废图，破坏完整镜头。
4. **整话长图不能直接喂给大模型**：整话纵拼高达 `800×60,000+ px`，视觉模型会自动缩放压缩，文字气泡全糊成像素点。**正确路线是：本地纵拼消除 CDN 切缝 $\rightarrow$ 本地二次精确裁剪出独立分镜 $\rightarrow$ 再送给大模型。**

### 精确改动清单（仅修改 `backend/src/stages/s2_segmenter.py` 及对应测试）
1. **第 0 步：本地全话无缝纵向拼接（一次性消灭“一镜跨两张图”）**：
   - 按 `raw_images` 升序读取本话所有原始图片，统一缩放到主宽度（众数宽度，通常 `800px`），**直接沿垂直方向无缝拼接成完整的本话连续原画画布**（或流式只沿 `row_std < 3.0 & row_diff < 2.0` 的安全纯白带拼接缓冲）。
   - **铁律**：严禁逐张独立切割后再合并，必须先拼接再二次裁剪！同时对**黑名单版权尾图**在拼接后或尾部命中时直接剔除。
2. **第 1 步：精细白边切分 + 去除多余白边（解决图 1 一拆二）**：
   - 在拼接后的连续画布上，将连续空白行切割阈值设为 `min_gap_px = max(36, int(image_width * 0.045))`（满足 `row_std < 3.0` 且 `row_diff < 2.0`）。
   - 切出原子图像块（Atomic Blocks）后，自动裁剪上下纯白 Padding（每侧仅保留 `16px` 安全边距）。
   - 结果：图 1 中间巨大的纯白带会被精准切分为「走廊分镜」和「病房对峙分镜」两个独立块；跨越两张原图的镜头会被无缝还原成 1 个完整块。
3. **第 2 步：孤立“内心独白框 / 悬浮气泡”向下吸附合并（解决图 2 二合一）**：
   - 新增判定函数 `is_standalone_text_box(block_bgr, orig_gap_below, img_width) -> bool`：
     - **特征 A（黑框/纯色独白框）**：整体尺寸偏扁（`height < 0.55 * img_width`），且**单色背景像素占比 `> 75%`**（例如图 2 中的大面积纯黑矩形框内嵌少量文字，或纯白气泡外框）+ 内部边缘集中于文字区，**缺少四周漫画线稿/场景渐变**；
     - **特征 B（小尺寸悬浮气泡）**：`height < 0.35 * img_width` 且未形成封闭漫画外框线。
   - **合并规则**：若某个原子块被判定为 `is_standalone_text_box == True`，且它与紧邻**下一个非文本主画格**在原长图中的垂直白边间距 `< 0.50 * img_width`，则**不单独输出该小块，而是连同适当留白一起与下方主画格垂直合并为同一张 `Panel`**。
   - **防误合并保护**：图 1 上半部分的「医院走廊」包含丰富建筑透视线条与灰阶色彩（单色占比远低于 75%），不会被误合并。
4. **第 3 步：取消对连续无白边超长画格的“Canny 硬切割”（防止人工劈断长镜头）**：
   - 若某一个不含内部纯白隔离带的连续镜头本身很高（例如高 3000px 的全景或跌落镜头），**禁止用 Canny 强行把它一刀砍成两张分镜**，保持其为 1 个完整 `PanelMeta`（标记 `is_tall_panel=True`）；
   - Stage 3 识图时按长边等比缩放（`<= 1280px` 或对超高图重叠切片喂给 VLM 汇总同一 `panel_id` 的文字），Stage 7 渲染时自动配合 `pan_down` 从头匀速扫到尾。

### 验收测试
- 在 `backend/tests/test_s2_segmenter.py` 新增三个构造测试用例：
  1. **跨图断镜拼接测试**：将 1 个完整实心圆或长方形画格从正中间横向劈开存为 `slice_1.png` 和 `slice_2.png` $\rightarrow$ 断言经 `s2_segmenter` 先拼后切，输出 **1 个完整** `PanelMeta`（绝不拆成两张）。
  2. **模拟“图 1”**：上下两个有复杂灰度纹理的矩形框，中间隔 150px 纯白带 $\rightarrow$ 断言切出 **2 个** `PanelMeta`。
  3. **模拟“图 2”**：上方 1 个纯黑矩形白字小框 + 90px 白边 + 下方有复杂渐变纹理的人物画框 $\rightarrow$ 断言吸附合并后输出 **1 个** `PanelMeta`。

---

## Patch 5：Stage 4 升级为「全话通读反写故事（4A）→ 顺位分镜锚定配音（4B）」

### 为什么必须升级？
原方案让大模型一步到位边看散落分镜边写 `ScriptBeat`，模型容易为了行文顺畅跨格乱跳、遗漏关键镜头，或把「内心独白 + 表面台词」揉成错位解说。改为两步法：
**先通读整话理解全貌、反写完整连贯的本话故事大纲（4A）；再严格按分镜先后顺序把故事拆解锚定回每一帧画面，并允许“一镜多拍（特写切换）”与“一句跨连续多镜”（4B）。**

### 精确改动清单（仅修改 `backend/src/core/schemas.py`、`backend/prompts/`、`backend/src/stages/s4_scriptwriter.py`、`backend/src/stages/s6_timeline.py` 及对应测试）

1. **`backend/src/core/schemas.py`（新增 Stage 4A 中间产物契约与聚焦支持）**：
   - 新增（不改动已有 `ScriptBeat` 必填字段，仅可选增加焦点提示）：
     ```python
     class StoryChapter(BaseModel):
         chapter_index: int
         panel_range: tuple[str, str]   # 覆盖的连续起止分镜，如 ("p_003", "p_005")
         story_prose: str               # 通顺完整的故事段落（供 Stage 4B 拆拍参考）
         dramatic_intent: str           # 本段剧情冲突与情绪转折说明

     class EpisodeStoryDraft(BaseModel):
         episode_id: str
         overall_plot_arc: str          # 全话一句话主线脉络
         chapters: list[StoryChapter]   # 按分镜时间顺序单调排列
     ```
   - 在 `ScriptBeat` 新增可选字段（带默认值，向后兼容）：
     ```python
     focus_hint: Literal["full", "top", "bottom", "center"] = "full"
     ```
     *(说明：当“一镜多句”如图 2 时，第 1 句内心独白可配 `focus_hint="top"`/`zoom_in`，第 2 句对话可配 `focus_hint="bottom"`/`pan_down` 聚焦面部，消除同一画面久停的枯燥感！)*

2. **`backend/src/stages/s3_vision.py` & `prompts/vision_extract.md`（区分“内心独白黑框”与“口头对白”）**：
   - 在 `DialogueBubble` 的 `speaker_guess` 或抽取提示中要求：遇到**黑底矩形框/无指向框旁白**时，`speaker_guess` 明确标注为 `"女主(内心独白)"` 或 `"旁白"`（并标记处于画面上部 `top`），与下方口头对话区分开。

3. **`backend/src/stages/s4_scriptwriter.py`（拆分为 4A + 4B 两步调用 + 代码级单调校验）**：
   - **Step 4A（全话通读 + 故事反写，调用 `qwen-plus`）**：
     - 将 `StoryBible v2` 上下文 + **本话全部有效分镜（已过滤非剧情尾页）按 `panel_id` 顺序编号列出**，一次性送给模型。
     - 要求模型输出 `EpisodeStoryDraft`（落盘至 `04_script/story_draft.json` 便于人工审阅）：
       - 先写出全话完整故事脉络；
       - 按时间顺序切分为若干 `StoryChapter`，**铁律：每段 `panel_range` 必须严格单调递增、首尾相接覆盖全部有效分镜，严禁把后半话的反转提前写进前半话段落**。
   - **Step 4B（顺位分镜配对与口语化拆拍，调用 `qwen-plus`）**：
     - 输入 `EpisodeStoryDraft` + 各段对应的原始分镜详述，生成最终 `NarrationScript`（`beats`）。
     - **核心拆拍法则**：
       - **严格顺位**：`beats` 中出现的分镜编号顺序**绝对不允许倒退**（$idx(b_i) \le idx(b_{i+1})$）。
       - **一镜多句（如图 2）**：当同一格同时包含“黑框内心独白”和“口头台词/情绪转折”时，拆成 2 条连续 `ScriptBeat` 绑定同一 `panel_id`，分别用 `focus_hint="top"`（内心酸楚语气）和 `focus_hint="bottom"`（表面克制语气）。
       - **一句多镜（连续过场）**：仅当连续 2~3 格表达单一动作过渡（如 `p_001` 走廊 $\rightarrow$ `p_002` 推门）时，允许 `target_panel_ids: ["p_001", "p_002"]` 连续绑定。
   - **代码层单调性与覆盖率硬校验（关键防线！）**：
     - 写一个纯 Python 校验函数 `validate_beat_alignment(beats, valid_panel_ids) -> list[str]`：
       1. 检查每个 `beat.target_panel_ids` 必须为有效分镜且内部序号连续递增；
       2. 检查全局所有 `beat` 展开后的分镜索引序列必须**非递减（单调不减）**；
       3. 检查不能有连续 2 个以上重要有对白分镜被彻底跳过未绑定；
     - 若校验发现乱序/漏镜错误，将具体错误列表拼入 Retry Prompt 让 `qwen-plus` 修正 `target_panel_ids`（最多重试 2 次）。

4. **`backend/src/stages/s6_timeline.py` & `s7_renderer.py`（消费 `focus_hint` 与多镜均分）**：
   - 当一个 `ScriptBeat` 绑定多个连续 `target_panel_ids`（如 `["p_001", "p_002"]`）时，在 `s6_timeline.py` 中按各分镜视觉信息密度（或均匀分配）切分为多个 `TimelineScene` 依次播放，**音频保持连续不中断**。
   - 若 `beat.focus_hint == "top"` 或 `"bottom"`，在计算 `TimelineScene.focus_region` 时优先偏移至分镜上半区或下半区，让一镜多句时镜头随解说自然移动。

### 验收测试
- 在 `backend/tests/test_s4_scriptwriter.py` 新增：
  1. `validate_beat_alignment` 单元测试：验证倒序 `["p_003", "p_001"]` 必须报错被拦截、一镜多拍 `["p_002"], ["p_002"]` 与顺位连续 `["p_001", "p_002"], ["p_003"]` 必须通过。
  2. Mock 两次 LLM 调用（4A 产出 `EpisodeStoryDraft` $\rightarrow$ 4B 产出 `NarrationScript`），断言 `04_script/story_draft.json` 正常落盘且所有有效分镜顺序被完整覆盖。
