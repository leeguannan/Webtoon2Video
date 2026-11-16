# Webtoon2Video 增量优化补丁规格书（Patch v2.5）

> **适用阶段**：后端与前端已基本开发完成，正在修 Bug / 稳定联调阶段。  
> **给 `minimax-3.1-flash` 的铁律**：  
> 1. **禁止全局重构**：严禁重新生成或推翻未提及的模块，严禁回退已修复的 Bug。  
> 2. **逐项打补丁**：每次只应用 **1 个 Patch**，改完立即运行对应 `pytest` 验收，全绿后再打下一个 Patch。  
> 3. **先修 Bug 再打补丁**：若当前存在未通过的测试用例，先修复现有 Bug 保持基线全绿。

---

## Patch 1：片尾 LINE WEBTOON 官方版权声明图三重过滤（最高优先）

### 改动目标
确保每一话在理解、配音与成片渲染时，**100% 剔除** LINE WEBTOON 片尾版权警告图（如 `public/assets/aistudio/image_3b1850df.png` 中的黑底绿标 Logo + 著作权提示）及平台推广尾图，同时不误杀漫画正片中的暗黑画面。

### 精确改动清单（仅修改以下 5 处）
1. **放置种子黑名单图**：
   - 创建目录 `backend/assets/blacklist_templates/`。
   - 若根目录存在 `public/assets/aistudio/image_3b1850df.png`，将其复制为 `backend/assets/blacklist_templates/webtoon_copyright_zh.png`。
2. **`backend/src/core/schemas.py`（向后兼容增量加字段）**：
   - 在 `PanelVisionInfo` 中新增（保留默认值，不破坏已有 JSON 解析）：
     ```python
     non_story_type: Literal[
         "none", "transition_blank", "copyright_notice", "promo_banner"
     ] = "none"
     ```
3. **`backend/src/stages/s2_segmenter.py`（第一重拦截：本地指纹直筛）**：
   - 新增辅助函数 `is_blacklisted_bumper(img_bgr, template_hashes)`：
     - 启动时读取 `backend/assets/blacklist_templates/*.{png,jpg,webp}` 计算 64-bit dHash（或 pHash）。
     - 对每张切出的分镜（尤其话尾图）计算 dHash：若与任一黑名单模板 Hamming 距离 `<= 6`，**或**同时满足「纯黑像素（RGB 均 `< 20`）占比 `> 70%` 且居中区域存在 WEBTOON 标志绿（HSV `H∈[65,85], S>120, V>120` 或 `#00DC64` 邻近色）连续区块」时，判定为片尾版权图。
   - 命中后**跳过不写入 `panels.json`**（可选移动或记录在日志 `skipped_blacklist`），避免浪费后续 VLM 费用。
4. **`backend/prompts/vision_extract.md` & `s3_vision.py`（第二重拦截：VLM 语义兜底）**：
   - 在 Prompt 中明确指令：若分镜为任何语言版本（中/英/韩/日）的「LINE WEBTOON / WEBTOON 官方版权声明、防盗版警告、休刊/求赞推广尾页」，必须将 `non_story_type` 设为 `"copyright_notice"` 或 `"promo_banner"`，并同步置 **`is_transition_blank = true`**。
5. **`backend/src/stages/s4_scriptwriter.py`（第三重拦截：撰稿代码硬过滤）**：
   - 在组装发送给 `qwen-plus` 的分镜输入前，强制执行代码过滤：
     ```python
     story_panels = [
         p for p in panels
         if not p.is_transition_blank and p.non_story_type == "none"
     ]
     ```
   - 校验模型返回的 `ScriptBeat.target_panel_ids`，严禁引用已被过滤的 `panel_id`。

### 验收测试
- 在 `backend/tests/test_s2_segmenter.py` 新增测试：喂入普通漫画图 + 版权声明样图（或模拟黑底绿块图），断言普通图正常保留、版权图被剔除。
- 在 `backend/tests/test_s4_scriptwriter.py` 新增测试：构造含 `non_story_type="copyright_notice"` 的分镜列表，断言传给 LLM 的上下文与生成剧本中均不包含该分镜。

---

## Patch 2：锁定 `9:16` 竖屏输出标准（移除 `16:9` 冗余逻辑）

### 改动目标
WEBTOON 条漫为竖向构图，强行兼容 `16:9` 会导致上下裁切丢失全貌。统一锁定 `9:16`（`1080×1920`, `30fps`）。

### 精确改动清单
1. **`backend/src/core/schemas.py`**：
   - 将 `RenderTimeline.aspect_ratio` 收窄为 `Literal["9:16"] = "9:16"`（默认 `width=1080, height=1920`）。
2. **`backend/src/stages/s6_timeline.py` & `s7_renderer.py`**：
   - 检查是否存在 `16:9` 分支逻辑；若有，保留 `9:16`（高斯模糊背景铺底 + 居中完整展示主分镜 + 底部安全区 `MarginV=240` ASS 字幕）即可，禁止对主分镜做横屏强裁切。
3. **前端原型同步**：
   - 若前端仍保留旧版 `16:9` 选项，参考当前最新版本 `src/types/studio.ts`、`ConfigDictView.tsx`、`EpisodeStudioView.tsx` 锁定为 `9:16`。

### 验收测试
- 运行 `pytest tests/test_s6_timeline.py tests/test_s7_renderer.py` 验证输出分辨率固定为 `1080×1920`。

---

## Patch 3：补齐成片归集目录、CLI 导出与 Web 下载端点

### 改动目标
解决“视频渲染完成后如何方便导出到电脑或剪映/发布平台”的最后一公里。

### 精确改动清单
1. **`backend/src/stages/s7_renderer.py`（汇总导出副本）**：
   - 成片落盘 `workspace/{series_id}/episodes/ep_{no:03d}/07_out/{episode_id}.mp4` 后，自动硬链接或复制一份可读文件名到 `workspace/{series_id}/exports/EP{no:03d}_{safe_title}.mp4`。
2. **`backend/src/cli.py`（新增 `w2v export`）**：
   - 新增子命令：`w2v export --series <series_id> --episodes 1-5 --out ./output_videos`，将已完成成片拷贝到指定本地目录。
3. **`backend/src/web/server.py`（新增 2 个只读下载接口）**：
   - `GET /api/episodes/{ep_id}/export?format=mp4|ass|script`：使用 FastAPI `FileResponse` 返回对应 `.mp4`、`.ass` 字幕或 `script_readable.md`（设置 `Content-Disposition: attachment`）。
   - `GET /api/series/{series_id}/export-zip`：将指定作品已完成 `s7_renderer` 的所有 `07_out/*.mp4` 内存流式或临时打包为 `.zip` 返回下载。

### 验收测试
- 在 `backend/tests/test_web_api.py` 使用 `TestClient` 断言 `/api/episodes/{ep_id}/export?format=mp4` 与 `/export-zip` 返回状态码 `200` 及正确响应头。
