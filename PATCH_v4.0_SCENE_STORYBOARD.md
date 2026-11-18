# Webtoon2Video 增量重构补丁规格书（Patch v4.0：识图纠偏 + Scene-First 镜头组架构 + 镜头编排审核台）

> **背景与诊断结论**：基于对真实成片 `wt_5081_ep_001 (4).mp4`（`01:55` / `115.96s`）逐帧对照原漫画《Only Hope》第 1 话全部 81 张原始切片，以及对已推送代码库（`7fa60cb` / `9ddd656`）的全面代码审计，确认当前流水线讲错/漏讲故事的 **6 个代码级根因**：
> 1. **气泡与独白框归属反转（`00:08~00:12`）**：`vision_extract.md` 按“画格上下位置”而非**气泡尾巴指向**认人，把病床上父亲的气泡台词错写成恩彗的心里冷笑。
> 2. **动作耦合拟声词丢失（`00:39~01:12`）**：`vision_extract.md` 让 VLM 把 `啪！` / `倒下` 等仅放进 `sfx_list`，而 `story_draft.md` 严禁读取 `sfx_list`，导致“继母扇巴掌”等关键动作被完全跳过，后续“脸颊红痕”失去因果。
> 3. **句中引号名词误拆分（`00:58~01:02`）**：`s4_scriptwriter.py` 的 `split_mixed_quote_beats` 对任意中文双引号一刀切，把 `“孤儿”` 这种句中宾语/定语硬拆成独立角色对话 Beat，听感支离破碎。
> 4. **死板篇幅与条数上限截断高密度剧集（`01:16~01:55`）**：`story_draft.md` 写死 `500~900` 字、`s4_scriptwriter.py` 写死 `BEAT_TOTAL_MAX = 50` 且 `script_narrate.md` 鼓励跳过 `30%~70%` 分镜；75+ 格的高密度首话被强行压缩，导致“备孕看诊＋玫瑰园传讯去别墅”整段转场被砍，结尾“绑匪提及金仁哲 $\rightarrow$ 倒下身亡 $\rightarrow$ 道振火光后露脸摸嘴角说‘早知道就别倒油了 / 让他死得太快了呢’ $\rightarrow$ 恩彗惊呼‘道振…？’”的高潮反转在 `01:55` 半句截断。
> 5. **暗黑高潮格易被误过滤**：`s2_segmenter.py` / `s3_vision.py` 缺乏对深黑/红光剪影高潮格的保护，容易把末尾暗底带框反转格误判为空镜/纯字卡并剔除出候选池。
> 6. **音频混音 BGM 仍过轻**：当前 `-11.5 dB` 叠 `sidechaincompress(ratio=2.8)` 在 `-18.8 dB` 人声底噪下背景配乐仍偏微弱。
>
> **给 `minimax-3.1-flash` 的铁律**：
> - **禁止全局重构**：严禁破坏已上线的 `7fa60cb` / `9ddd656` 核心成果（`EpisodeStudioView.tsx` 本句配音展示/角色下拉/插入新镜头、`VoicePicker.tsx`、`SeriesTtsPanel.tsx`、MiniMax Speech 2.8 `t2a_v2`、9:16 `1080×1920` 抖音安全区版式、Stage 0/1 下载器）。
> - **严格分 3 轮增量实施，每轮必须跑通 `pytest`（前端变更同步跑 `npm run lint && npm run build`）**：
>   - **Patch 18**：识图纠偏（气泡尾巴认人 + 动作拟声具象化 + 暗黑高潮护栏）+ 句中引号防误拆 + BGM/SFX 混音响度调优。
>   - **Patch 19**：Scene-First 镜头组架构（`storyboard.json` + `s3b_scenes.py` + 整幕视觉复核 + 动态篇幅预算 + 逐幕精写与覆盖率硬校验补写）。
>   - **Patch 20**：Web 审核台「镜头编排（Storyboard）」面板与 FastAPI 接口（支持人工分组、拆分/合并幕、填写本幕必讲要点与锁定必须出现的台词、一键按镜头组重跑解说/出片）。

---

## 一、Patch 18：识图纠偏、暗黑高潮护栏、句中引号防误拆与混音调优

### 文件修改白名单（严禁改动白名单外文件）
- `backend/prompts/vision_extract.md`
- `backend/src/stages/s2_segmenter.py`
- `backend/src/stages/s3_vision.py`
- `backend/src/stages/s4_scriptwriter.py`
- `backend/src/core/config.py`
- `backend/config.example.yaml`
- `backend/src/stages/s7_renderer.py`
- `backend/tests/test_patch_v40_patch18_fidelity.py`（**新增单测**）

### 精确实现规格

#### 1. `backend/prompts/vision_extract.md` 与 `backend/src/stages/s3_vision.py`
1. **气泡尾巴认人铁律（取代“上部黑框 / 下部气泡”机械假设）**：
   - 在 `vision_extract.md` 中明确区分：
     - **带尖角尾巴的对话气泡（Speech Bubble）**：必须**追踪气泡尖角尾巴（或位置朝向）指向哪位角色**来判定 `speaker_guess`；即使该角色只露出病床衣角或处于画外，也应结合上下文写为真实说话人（如 `父亲` / `病房中的男人`），**严禁因为画面主体是女主恩彗就把别人骂她的话写进恩彗名下**；
     - **无尾巴的矩形框（黑底白字框 / 白底黑字框）**：一律视为心理独白或旁白（`角色名(内心独白)` 或 `旁白`），**禁止**标成该角色当场说出口的口头台词。
2. **动作耦合拟声词必须写入 `scene_summary`**：
   - 凡表示物理动作或状态骤变的拟声字/特效字（如 `啪！` 扇耳光、`倒下` 倒地身亡、`撞`、`抓`、`摔碎`、`推门`），除了提取进 `sfx_list`，**必须在 `scene_summary` 里用一句话写清物理动作与受众**（例：“继母抬手扇了道振一耳光，道振脸颊瞬间泛起红印” / “被绑男子在火海中轰然倒下”），避免下游禁用 `sfx_list` 后整场冲突凭空消失。
3. **暗黑高潮格护栏**：
   - 明确提示 `vision_extract.md`：深黑底、血红警示光/火光下的剪影人物 + 六边形/黑底对白框属于高潮剧情格，**严禁标为 `is_transition_blank=true`、`is_pure_text_card=true` 或 `non_story_type="sfx_only"`**。
   - 在 `s3_vision.py` 新增确定性后处理函数 `protect_dramatic_panels(panels: list[PanelVisionInfo]) -> list[PanelVisionInfo]`，在输出落盘前调用：
     - 若某格 `dialogues` 含非纯拟声词的有效文本（`any(len(_strip_punct(d.text)) >= 3 for d in panel.dialogues)`）**或** `scene_summary` 含明确人物/躯体动作/空间词汇且并非 `copyright_notice`/`promo_banner`，则强制 `is_transition_blank = False`、`is_pure_text_card = False`、`non_story_type = "none"`。

#### 2. `backend/src/stages/s2_segmenter.py`
- 保护暗黑高反差剧情格不被 `is_blank_panel` 误杀：
  - 在 `is_blank_panel(gray: np.ndarray)` 中增加保护判据：不仅看全局均值 `mid` 与全局 `canny_edge_density.mean()`，还需检查**局部行高对比峰值**（`float(gray.std(axis=1).max())` 或任一纵向滑窗切片的最大边缘密度）；当画面虽整体偏暗但存在清晰亮框/人脸轮廓/字幕边缘（例如 `float(gray.std(axis=1).max()) >= 18.0` 且存在局部结构边缘）时，判定为有效画面（`return False`），绝不当成纯黑空白页切掉或标空。

#### 3. `backend/src/stages/s4_scriptwriter.py` — 修复 `split_mixed_quote_beats` 句中引号词误拆
- 目标：保留 `道振平静说道：“别怕，有我在。”`、`继母指着他骂：“立刻离开！”` 等真实引导拆分的全部既有行为，同时**禁止**把叙述句里作名词/代称/强调成分的引号短语（如 `继母恶毒地让他们带着这个“孤儿”立刻离开。`）劈成 3 条支离破碎的 Beat。
- 新增辅助函数 `_is_spoken_quote_segment(inner: str, lead_before: str, tail_after: str, *, full_lead_in_len: int) -> bool`：
  - 返回 `True`（应拆为独立对白 Beat）的**充分条件**（满足任一即可）：
    1. 本条原本几乎就是纯引号台词（`full_lead_in_len < 2`）；
    2. `lead_before` 去掉尾部空白后，以引导标点（`：` / `:` / `——` / `，` 且前文存在 `_SPEECH_VERBS` 或心理动词）结尾；
    3. `inner` 本身是一句**完整句子**（`quote_char_count(inner) >= 6` 且句末带有 `。！？…` 之一，或内部含完整主谓句式），而不是嵌在上下半句中间的名词词组；
  - 若 `inner` 不满足上述条件（例如 `len <= 5` 无句末独立语调标点，且后面紧跟着同一句的后续正文 `tail_after` 如 `立刻离开。`），判定为**句中嵌入引用词**：
    - 在 `split_mixed_quote_beats` 中，先扫描所有正则匹配，把判定为 False 的“句中引用词”直接并回周围的 `lead` 叙述文本中（保留原文里的 `“孤儿”` 双引号显示给字幕，不再单开子 Beat）；
    - 若整条 Beat 合并后已没有任何需要拆分的独立对话段，保持为单条 `narrator` Beat（只归属 `narrator` 声线，完整顺畅念出整句话）。

#### 4. `backend/src/core/config.py`、`backend/config.example.yaml`、`backend/src/stages/s7_renderer.py` — 混音响度微调
- `RenderConfig`（`config.py`）：
  - `bgm_gain_db: float = -8.0`（从 `-11.5` 提升到 `-8.0`）
  - `sfx_gain_db: float = -2.0`（从 `-3.5` 提升到 `-2.0`）
- `config.example.yaml` 同步更新 `bgm_gain_db: -8.0` 与 `sfx_gain_db: -2.0` 及注释。
- `s7_renderer.py`：
  - `DEFAULT_SFX_GAIN_DB = -2.0`
  - `_SIDECHAIN_THRESHOLD = 0.06`
  - `_SIDECHAIN_RATIO = 2.0`
  - `_SIDECHAIN_ATTACK_MS = 30`
  - `_SIDECHAIN_RELEASE_MS = 350`

#### 5. 单测 `backend/tests/test_patch_v40_patch18_fidelity.py`
- 覆盖至少 5 个核心断言：
  1. `split_mixed_quote_beats` 处理 `继母恶毒地让他们带着这个“孤儿”立刻离开。` 时 **保持为 1 条 `narrator` Beat**，不拆成 3 条；
  2. `split_mixed_quote_beats` 处理 `道振平静说道：“别怕，有我在。”` 与 `继母怒骂：“混账！”` 时 **正常拆分为旁白引导 + 角色台词两条**；
  3. `protect_dramatic_panels` 对“暗底红光 + 带有效台词”的画格强制恢复 `is_transition_blank=False, is_pure_text_card=False, non_story_type="none"`；
  4. `is_blank_panel` 对纯黑背景上含高亮白色对白框（高局部行方差）的画格返回 `False`；
  5. `RenderConfig()` 默认 `bgm_gain_db == -8.0` 且 `sfx_gain_db == -2.0`。

---

## 二、Patch 19：Scene-First 镜头组架构（`storyboard.json`）+ 逐幕视觉复核与覆盖率硬校验

### 核心设计思想
将原有的 **「单格孤立识图 $\rightarrow$ 全话塞入一次 4A 压缩写死 500~900 字 $\rightarrow$ 4B 限死 35~50 条顺便跳图」**，升级为 **Scene-First（镜头组先导）**：
1. 先将全话 60~80 格自动聚类成 **5~10 个连续「镜头组 / 幕（`StoryboardScene`）」**（保存为 `04_script/storyboard.json`）；
2. （可选/默认开启）使用 `models.vision`（`qwen3-vl-plus`）对**每个镜头组的拼接长条图做一次整幕联看复核**，补全跨格因果与黑化反转（如谁扇了谁、结尾火光后站出来的人是谁）；
3. Stage 4A 与 Stage 4B 严格**以 `storyboard.scenes`（`enabled=True`）为唯一分幕骨架**，带上用户填的 `user_note`（必讲要点）与 `locked_quotes`（必保原话），并采用**按幕数与分镜密度动态伸缩的字数/条数配额**；
4. 落地后执行 **`validate_storyboard_coverage` 硬校验**：任何一个 `must_cover=True` 的镜头组若漏讲或漏掉 `locked_quotes`，自动只针对该缺失幕单幕补写，**彻底根治“结尾高潮被砍/前半话信息被压缩没了”**。

### 文件修改白名单
- `backend/src/core/schemas.py`
- `backend/src/stages/s3b_scenes.py`（**新增模块**）
- `backend/src/stages/s4_scriptwriter.py`
- `backend/prompts/story_draft.md`
- `backend/prompts/script_narrate.md`
- `backend/src/cli.py`
- `backend/src/web/job_runner.py`
- `backend/tests/test_patch_v40_patch19_storyboard.py`（**新增单测**）

### 精确实现规格

#### 1. `backend/src/core/schemas.py`（只增不删，兼容旧数据）
新增两个 Pydantic 模型：
```python
class StoryboardScene(BaseModel):
    """Stage 3b/4 镜头组（一幕连续剧情）。"""
    scene_id: str
    scene_index: int
    title: str = ""
    location: str = ""
    panel_ids: list[str] = Field(..., min_length=1)
    enabled: bool = True
    must_cover: bool = True
    user_note: str = ""
    locked_quotes: list[str] = Field(default_factory=list)
    visual_recap: str = ""


class EpisodeStoryboard(BaseModel):
    """单话镜头编排清单（落盘于 04_script/storyboard.json）。"""
    episode_id: str
    source: Literal["auto", "human"] = "auto"
    scenes: list[StoryboardScene] = Field(default_factory=list)
```

#### 2. 新增 `backend/src/stages/s3b_scenes.py`
1. **常量与路径**：
   - `STORYBOARD_FILENAME = "storyboard.json"`
   - `storyboard_path_for(episode_dir: Path) -> Path` 返回 `episode_dir / "04_script" / "storyboard.json"`。
2. **自动聚类函数 `auto_build_storyboard(episode_id: str, vision: VisionWindowOutput) -> EpisodeStoryboard`**：
   - 按 `panel_sort_key` 遍历 `vision.panels`：
     - 先过滤掉明确的无效页（`non_story_type in {"copyright_notice", "promo_banner"}`），并记录 `is_transition_blank` 或 `non_story_type == "transition_blank"` 的位置作为**天然切幕边界**；
     - 从有效面板的 `scene_summary` 开头抽取空间地点标签（如按第一个中文标点 `，/。/：/ ` 之前且不含动作动词的短前缀，或匹配常见地点词 `病房|走廊|客厅|卧室|书房|别墅|地下室|花园|玫瑰|墓地|车内|餐厅|玄关|公司|酒店`）；
     - 切分启发式（保证有序、连续、不漏格）：
       - 遇 `transition_blank` 间隔且当前幕已积累 `>= 3` 格时切新幕；
       - 地点标签发生明显切换且当前幕已有 `>= 4` 格时切新幕；
       - 当前幕达到 `12` 格上限时强制寻最近一个对话结束格切新幕；
       - 末尾残余 `< 3` 格若非高潮反转（无对白且无重要动作）可并入前一幕；**若包含对白或处于全话最后 5 格内，宁可保持独立或与最后高潮合并成独立收尾幕，严禁丢弃**。
     - 目标控制在 **5 ~ 10 幕**（每幕自动填充简明 `title` 如 `"第 1 幕 · 病房"`、`location`、`enabled=True`、`must_cover=True`）。
3. **整幕视觉联看复核 `enrich_storyboard_with_vision(...) -> EpisodeStoryboard`**（异步函数）：
   - 仅当传入了 `client`、`panels_dir` 存在、且该幕尚未有 `visual_recap`（或 `force=True`）时，为每个 `enabled` 的 `StoryboardScene`：
     - 将该幕的面板图按顺序等宽缩至 `640px` 竖向拼成 1~2 张（高度控制在 `<= 3200px`）内存 JPEG/PNG Data URL；
     - 调用 `vision_model`（如 `qwen3-vl-plus`）询问：
       - 本幕发生在哪？有哪些人物在场？
       - 跨格连贯物理动作与因果（如“谁打谁、谁抓住谁、谁在火中倒下、最后露脸的是谁”）？
       - 最关键的 1~2 句原话气泡归属谁？
     - 将模型返回结果写入 `scene.visual_recap`（单幕异常时捕获并回退为拼接各格 `scene_summary`，**绝不阻断流水线**）。
4. **读写与人工覆盖保护**：
   - `load_or_build_storyboard(episode_id, vision, storyboard_path, *, reset=False) -> EpisodeStoryboard`：
     - 若文件存在且 `not reset`，直接加载；**特别保护**：只要磁盘文件已存在（尤其是 `source == "human"`），绝不覆写用户的 `scenes`、`user_note`、`locked_quotes`、`enabled`、`must_cover`。

#### 3. 升级 `backend/src/stages/s4_scriptwriter.py` 与 Prompt 模板
1. **动态预算（彻底淘汰写死的 `500~900` 字和 `BEAT_TOTAL_MAX = 50`）**：
   - 新增 `compute_episode_beat_budget(active_scene_count: int, valid_panel_count: int) -> tuple[int, int]`：
     - 每幕基础 `6 ~ 10` 条解说句；
     - `total_min = max(35, active_scene_count * 5)`
     - `total_max = min(90, max(55, active_scene_count * 10, round(valid_panel_count * 1.05)))`
     - 例如 8 幕 / 75 格首话 $\rightarrow$ 目标 `40 ~ 79` 条，彻底解决 75 格挤进 50 条被腰斩结尾的问题！
   - 新增 `compute_story_word_budget(active_scene_count: int, valid_panel_count: int) -> tuple[int, int]`：
     - 每幕约 `140 ~ 210` 字，整体控制在 `[max(600, active_scene_count * 130), min(1700, max(1000, active_scene_count * 210))]`。
2. **`backend/prompts/story_draft.md` 更新**：
   - 将写死的 `500 ~ 900 字` 替换为占位符 `{{STORY_WORD_BUDGET}}`；
   - 新增 `{{STORYBOARD_CONSTRAINTS}}` 区块：明确要求章节数量与顺序严格对齐 `storyboard` 中启用的镜头组（1 幕 = 1 章），每章必须完整落实对应镜头组的 `user_note`（编导要点）、`visual_recap`（整幕视觉事实）与 `locked_quotes`（必保台词）。
   - 修改“纯字卡不要复述”条款：明确**角色内心的黑框独白/标题过渡（如准备婚礼、去医院检查、玫瑰园传讯去别墅）属于叙事推进信息，必须转化为自然第三人称叙述融入对应章节正文，绝不可整段跳过。**
3. **`backend/prompts/script_narrate.md` 更新**：
   - 将“允许跳过 `30%~70%` 分镜 / 全话合计目标 `35~50` 条”修改为：
     - 严格遵守本章传入的 `{{BEAT_BUDGET}}`；
     - 新增 `{{SCENE_DIRECTIVES}}` 区块（传入本幕 `user_note` + `locked_quotes` + `visual_recap`）；
     - 强调：**本幕内每个关键动作转折与 `locked_quotes` 必保台词必须出现，只跳过纯粹重复的静态反应格**。
4. **Stage 4 接入 Storyboard 流程与硬校验补全**：
   - `write_episode_script(...)` 新增可选参数 `storyboard: EpisodeStoryboard | None = None`、`storyboard_path: Path | None = None`、`vision_client: LLMClient | None = None`、`vision_model: str = ""`、`panels_dir: Path | None = None`、`reset_storyboard: bool = False`。
   - 流程：
     1. 自动加载或构建 `EpisodeStoryboard`（若 `storyboard` 未显式传入且 `storyboard_path` 未提供，也在内存中按 `auto_build_storyboard` 生成）；若提供了 `vision_client` 与 `panels_dir` 则执行 `enrich_storyboard_with_vision`；
     2. 用启用的 `scenes`（`scene.enabled == True`）构造 4A 的按幕分镜摘要，让 4A 产出的每章严格一一对应每幕；
     3. 用每幕的 `(chapter, scene.panel_ids, scene)` 执行 4B 逐幕拆拍；
     4. 新增 **`validate_storyboard_coverage(beats, active_scenes) -> list[StoryboardScene]`**：
        - 检查每条 `scene.enabled and scene.must_cover` 的幕：
          - 命中该幕 `panel_ids` 的 beat 数量必须 `>= 1`（若该幕 `len(panel_ids) >= 3` 则至少 `>= 2` 条）；
          - 对每条 `quote in scene.locked_quotes`，去除标点后核心片段（或前 6 字核心短语）必须出现在命中该幕的 beat `narration_text` 中；
        - 若发现缺失幕列表 `missing_scenes`：
          - **仅针对 `missing_scenes` 逐幕重调 `_write_narration_chapter` 定向补写**（注入明确的缺失原因反馈，例如 `"上一稿漏掉了本幕必保台词「早知道就别倒油了」或未覆盖尾幕分镜，请完整讲述本幕冲突与反转"`），替换/插回对应位置，再重跑 `split_mixed_quote_beats`、去重与顺位校验！

#### 4. `backend/src/cli.py` 与 `backend/src/web/job_runner.py`
- `cli.py` 新增两个实用子命令：
  - `w2v storyboard --series <id> --episode <no> [--reset]`：根据 `03_vision/vision.json` 生成/重置 `04_script/storyboard.json`（可选执行整幕视觉复核）。
  - `w2v narrate-from-storyboard --series <id> --episode <no>`：读取已有 `04_script/storyboard.json` 跑 Stage 4（并保持 `human` 编辑不被覆盖）。
- `job_runner.py` 在执行 `s4_scriptwriter` 阶段时，自动把 `storyboard_path`（`04_script/storyboard.json`）、`panels_dir`（`02_panels`）与 `vision_model` 传给 `s4_scriptwriter.write_episode_script`（保留已有 `source == "human"` 编排）。

#### 5. 单测 `backend/tests/test_patch_v40_patch19_storyboard.py`
- 覆盖至少 5 个核心断言：
  1. `auto_build_storyboard` 能把模拟 30+ 格含 `transition_blank` 与地点切换的 `VisionWindowOutput` 稳定切成有序、覆盖全部有效面板、且末尾高潮独立成幕的 `EpisodeStoryboard`；
  2. `load_or_build_storyboard` 当磁盘存在 `source="human"` 时绝不被自动重建覆写；
  3. `compute_episode_beat_budget(8, 75)` 返回上限明显大于旧硬编码 `50`（落入 `60~85` 区间）；
  4. `validate_storyboard_coverage` 能精准检出“尾幕 `must_cover=True` 但产出 beats 为 0”或“漏掉 `locked_quotes`”的缺失幕；
  5. 端到端 mock `LLMClient` 验证：首次生成漏掉第 3 幕时，能自动只重调第 3 幕补写并最终通过全量覆盖校验。

---

## 三、Patch 20：Web 审核台「镜头编排（Storyboard）」面板与接口

### 文件修改白名单
- `backend/src/web/server.py`
- `backend/src/web/job_runner.py`
- `src/types/studio.ts`
- `src/components/StoryboardEditor.tsx`（**新增组件**）
- `src/components/EpisodeStudioView.tsx`
- `backend/tests/test_patch_v40_patch20_storyboard_api.py`（**新增单测**）

### 精确实现规格

#### 1. Backend FastAPI 接口（`backend/src/web/server.py`）
新增请求体模型与 4 个端点（沿用现有 `view.find_episode(episode_id)` 与错误处理风格）：
1. **`GET /api/episodes/{episode_id}/storyboard`**：
   - 定位话次，读 `03_vision/vision.json` 与 `04_script/storyboard.json`；
   - 若尚无 `storyboard.json` 但已有 `vision.json`，调用 `s3b_scenes.auto_build_storyboard` 生成并落盘后返回；
   - 返回结构：
     ```json
     {
       "episode_id": "wt_5081_ep_001",
       "source": "auto",
       "scenes": [ ... ],
       "panels_index": {
         "p_001": {
           "panel_id": "p_001",
           "image_url": "/api/episodes/wt_5081_ep_001/panels/p_001.png",
           "scene_summary": "...",
           "dialogues": [...]
         }
       }
     }
     ```
2. **`PUT /api/episodes/{episode_id}/storyboard`**：
   - 请求体 `{ "scenes": StoryboardScene[] }`；
   - 校验每幕 `panel_ids` 非空、无重复跨幕分镜且按单调顺序排列；
   - 自动将 `source` 置为 `"human"`，落盘至 `04_script/storyboard.json`，返回最新 storyboard。
3. **`POST /api/episodes/{episode_id}/storyboard/reset`**：
   - 重新根据 `03_vision/vision.json` 自动聚类并重置 `source = "auto"`，返回最新 storyboard。
4. **`POST /api/episodes/{episode_id}/storyboard/generate`**：
   - 请求体 `{ "scenes"?: StoryboardScene[], "mode": "phase1_only" | "full_video" }`；
   - 若请求体带了 `scenes`，先走保存校验落盘为 `source = "human"`；
   - 调用 `job_runner.submit_episode(series_id, episode_no, from_stage="s4_scriptwriter", mode=payload.mode)` 返回后台任务 `job` 对象。

#### 2. Frontend Studio 类型与组件（`src/types/studio.ts`、`StoryboardEditor.tsx`、`EpisodeStudioView.tsx`）
1. **`src/types/studio.ts`** 新增类型：
   ```ts
   export interface StoryboardScene {
     scene_id: string;
     scene_index: number;
     title: string;
     location: string;
     panel_ids: string[];
     enabled: boolean;
     must_cover: boolean;
     user_note: string;
     locked_quotes: string[];
     visual_recap?: string;
   }

   export interface EpisodeStoryboard {
     episode_id: string;
     source: 'auto' | 'human';
     scenes: StoryboardScene[];
     panels_index?: Record<string, PanelItem>;
   }
   ```
2. **新增 `src/components/StoryboardEditor.tsx`**：
   - 组件接收 `{ episode, onJobSubmitted, onSwitchToScriptTab }` props；
   - 挂载/切换 `episode.episode_id` 时请求 `GET /api/episodes/${episode.episode_id}/storyboard`；
   - **顶部操作栏**：
     - 展示当前模式标签（`自动分组` / `人工已编排`）与总幕数/启用幕数/覆盖格数统计；
     - 按钮 1：`恢复自动分组`（调 `/reset`，带二次确认防误点）；
     - 按钮 2：`保存镜头组`（调 `PUT /storyboard`）；
     - 按钮 3：`按镜头组重生成剧本 (S4)`（调 `POST /storyboard/generate` `mode="phase1_only"`）；
     - 按钮 4：`按镜头组一键制片 (S4→S7)`（调 `POST /storyboard/generate` `mode="full_video"`）；
   - **场景卡片流（每张卡片对应一幕 `StoryboardScene`）**：
     - 头部：幕序号徽章 + 可编辑标题框（`title`）+ 地点标签（`location`）+ 复选开关 `参与讲述 (enabled)` & `必讲不可漏 (must_cover)` + `合并到上一幕` 按钮；
     - 横向缩略图网格：逐格展示该幕 `panel_ids` 对应的面板缩略图、面板 ID、对话数量与 `scene_summary` 悬浮提示；
       - 每张非首格面板卡片右上角提供小按钮 **`从此处拆新幕`**（点击即可把本幕从该格一分为二，自动重编 `scene_index` 与 `scene_id`）；
       - 每张首格/尾格允许点击 `移到上一幕` / `移到下一幕` 进行 1 格微调；
     - **快捷锁定台词**：列出本幕面板识别出的 `dialogues[].text`，点击气泡标签一键加入或移出 `locked_quotes`（也可通过输入框手动回车新增关键台词，如 `早知道就别倒油了`）；
     - **本幕必讲要点（`user_note`）**：多行文本框，提示作者可写明因果或纠正识图误差（如：`父亲在病床上说话，不是恩彗说的；继母打道振一巴掌骂孤儿`）；
     - 若存在 `visual_recap`，折叠或浅色块展示“AI 整幕联看摘要”供参考。
3. **`src/components/EpisodeStudioView.tsx` 最小侵入集成**：
   - 在现有 Tab 状态联合类型里增加 `'storyboard'`，并在顶部 Tab 导航条里新增一个按钮：**`镜头编排 (${scenesCount})`**（放在“视觉抽取”与“剧本与多音字”之间，完全符合 `S3 识图 -> 镜头编排 -> S4 剧本 -> S5 配音` 工作流）；
   - **严禁改动现有 `script` Tab 里的 `BeatVoiceAssignment` 展示、说话人下拉、插入/删除 Beat 或 MiniMax 试听代码**。

#### 3. 单测 `backend/tests/test_patch_v40_patch20_storyboard_api.py`
- 使用 FastAPI `TestClient` 测试：
  1. `GET /api/episodes/{id}/storyboard` 在无 `storyboard.json` 但有 `vision.json` 时自动生成并落盘；
  2. `PUT /api/episodes/{id}/storyboard` 保存后 `source` 变为 `"human"` 且持久保留 `user_note` 和 `locked_quotes`；
  3. 传入非法重复或空 `panel_ids` 返回 `400`；
  4. `POST /api/episodes/{id}/storyboard/reset` 重置回 `source="auto"`；
  5. `POST /api/episodes/{id}/storyboard/generate` 正确提交 `from_stage="s4_scriptwriter"` 任务。

---

## 四、给用户的本地配置即时检查（在跑下一条视频前务必确认）

虽然你已经在全局切换了 MiniMax Speech 2.8，但上一次成片（`01:55`）里的旁白仍然是女声、且 BGM 仍偏轻，说明作品级覆盖或本地 `config.yaml` 里可能残留旧配置。请在 Mac 终端执行下面 2 行检查命令：

```bash
cat backend/config.yaml | grep -E "tts|narrator|bgm_gain_db|story_model|vision" -A 3
cat workspace/wt_5081/series_config.yaml 2>/dev/null || echo "无作品级 series_config.yaml"
```

建议确认以下值：
- `models.tts`: `"speech-2.8-hd"`
- `tts.narrator.voice`: `"Chinese (Mandarin)_Serene_Narrator"`（沉稳说书男声，悬疑/虐恋解说最佳）
- `tts.narrator.speed`: `1.12` ~ `1.16`
- `render.bgm_gain_db`: `-8.0`（Patch 18 会把代码默认值也升到 `-8.0`）

---

## 五、给 `minimax-3.1-flash` 的即拷即用执行指令（按顺序分 3 次发送）

> **每次只发一段！等它修改完成并确认 `pytest`（以及第 3 轮的 `npm run lint && npm run build`）全部通过后，再发下一段。**

### 第 1 段（Patch 18：识图纠偏 + 暗黑护栏 + 句中引号防误拆 + 混音调优）
```text
请严格按照项目根目录 `PATCH_v4.0_SCENE_STORYBOARD.md` 中的「一、Patch 18」规格实施修改，遵守文件修改白名单，禁止全局重构，也不要改动现有 7fa60cb/9ddd656 的前端审核台与 MiniMax 接入逻辑。
完成后请运行 `pytest`（重点通过 `backend/tests/test_patch_v40_patch18_fidelity.py` 及历史单测）与 `ruff check`（若已安装），汇报改了哪些文件及测试结果。
```

### 第 2 段（Patch 19：Scene-First 镜头组架构 `storyboard.json` + 逐幕精写与覆盖率硬校验）
```text
Patch 18 已通过，请继续严格按照 `PATCH_v4.0_SCENE_STORYBOARD.md` 中的「二、Patch 19」规格实施修改：
1. 在 `schemas.py` 只增不删地添加 `StoryboardScene` 与 `EpisodeStoryboard`；
2. 新增 `backend/src/stages/s3b_scenes.py` 实现自动聚类、整幕视觉联看复核 `enrich_storyboard_with_vision` 及人工配置保留保护；
3. 升级 `s4_scriptwriter.py` 与 `story_draft.md` / `script_narrate.md` 为 Scene-Grounded 逐幕撰稿 + 动态字数/条数配额 + `validate_storyboard_coverage` 缺失幕单幕补写，并在 `cli.py` 与 `job_runner.py` 接入；
4. 编写并通过 `backend/tests/test_patch_v40_patch19_storyboard.py` 及全量 `pytest`。严禁改动白名单外文件。
```

### 第 3 段（Patch 20：Web 审核台「镜头编排（Storyboard）」面板与 FastAPI 接口）
```text
Patch 19 已通过，请继续严格按照 `PATCH_v4.0_SCENE_STORYBOARD.md` 中的「三、Patch 20」规格实施修改：
1. 在 `backend/src/web/server.py` 与 `job_runner.py` 新增 `/api/episodes/{episode_id}/storyboard`（GET / PUT / reset / generate）接口与校验；
2. 在 `src/types/studio.ts`、`src/components/StoryboardEditor.tsx`（新增）、`src/components/EpisodeStudioView.tsx` 新增「镜头编排 (Storyboard)」Tab，支持查看每幕缩略图、一键拆分/合并幕、填写本幕必讲要点 (`user_note`)、锁定必保台词 (`locked_quotes`) 及一键按镜头组重生成解说/出片；
3. 严禁修改或破坏现有的「本句配音展示/角色下拉/插入镜头」与 MiniMax 试听功能；
4. 完成后运行 `pytest`（含 `test_patch_v40_patch20_storyboard_api.py`）以及前端 `npm run lint && npm run build` 验证零报错。
```
