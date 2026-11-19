# Webtoon2Video 增量补丁规格书（Patch v4.5：作品级通用封页写字 + 双结尾图选择 + 平滑淡入淡出转场）

> **目标**：
> 1. **作品级通用封页**：用户为作品上传 1 张通用封页底图，在作品配置里指定预留文字位置（x/y 归一化坐标、字号、颜色、描边、模板如 `第{ep}话` / 中文数字 `第一话`），每话渲染成片时自动在封面预留位置写上对应话次标题；前端支持可视化拖拽定位与实时预览。
> 2. **作品级双结尾图 + 单话自由选择**：作品可上传「待续（outro_continue）」与「完结（outro_final）」两张结尾图并设定作品默认模式；目录调度列表与单话审核台可为具体话次单独选择「跟随默认 / 待续 / 完结 / 不加结尾」，**无需重跑 S4~S6，仅重跑 S7 即可生效**。
> 3. **平滑进出不突兀**：封面黑场柔和淡入（默认 0.35s）→ 稳定停顿供观众看清话次（总引导时长默认 2.0s）→ `xfade=fade` 柔和叠化（默认 0.45s）到正文第 1 帧，此时才开始第一句配音与字幕；BGM 同步渐入并在开口时自然压低。结尾正文最后一句播完定格后，结尾图在末帧之上缓缓叠化浮现（默认 0.70s）→ 稳定停留（总结尾默认 2.5s）→ 结尾图与 BGM 同步缓缓淡出至黑场/静音（默认 0.60s）。
>
> **铁律**：
> - 严格遵守文件修改白名单，禁止全局重构；不传任何封页/结尾参数时，S7 渲染链路与历史行为 **100% 一致**（现有测试零修改通过）。
> - 不引入新依赖（禁止改 `backend/pyproject.toml`，不加 `python-multipart` 等）；图片上传走 JSON Base64，不依赖 multipart 表单。
> - 前端禁用 `window.alert` / `window.confirm`，全部反馈走内联状态与轻提示。

---

## 一、文件修改白名单

### 后端（5 个）
1. `backend/src/core/config.py` — 新增 `CoverTextSpec` / `SeriesBrandingConfig` 严格模型并挂到 `SeriesConfig`。
2. `backend/src/core/branding_render.py`（**新建**）— 图片规范化、中文数字、封面写字、画布铺图、单话渲染选项、片包解析。
3. `backend/src/stages/s7_renderer.py` — 封页/结尾滤镜链、音画偏移数学、BGM 淡入淡出、`render_episode` 片包集成。
4. `backend/src/web/server.py` — 6 个片包/单话选项接口 + episode JSON 附带 `render_options`。
5. `backend/tests/test_patch_v45_branding.py`（**新建**）— 15 项真实测试（含真 FFmpeg 合成与 TestClient 全流程）。

### 前端与文档（6 个）
6. `src/types/studio.ts` — 新增片包相关类型，`EpisodeItem` 加可选 `render_options`。
7. `src/components/SeriesBrandingPanel.tsx`（**新建**）— 作品片包折叠面板（三图上传 + 9:16 可视化封页编辑器 + 时长/默认策略）。
8. `src/components/CatalogView.tsx` — 挂载片包面板；每话操作列加紧凑「结尾图」下拉，切换即自动保存。
9. `src/components/EpisodeStudioView.tsx` — 顶部「本话封页与结尾片包（仅重跑 S7 生效）」配置条，变更自动保存。
10. `src/App.tsx` — `handleUpdateEpisodeRenderOptions` 调 `PUT /api/episodes/{id}/render-options` 并本地同步，透传两视图。
11. `PATCH_v4.5_COVER_AND_OUTRO.md`（本文件）。

### 严禁修改
`backend/pyproject.toml`、`backend/tests/test_schemas.py`、`backend/src/stages/s4_scriptwriter.py`、`backend/src/stages/s5_tts.py`、`backend/src/stages/s6_timeline.py`、`backend/src/core/subtitle_render.py`（仅允许 `import` 复用其 `resolve_font_path` / `load_font` / `DEFAULT_FONT_NAME` / `FONT_CANDIDATES`）。

---

## 二、后端实现

### 1. `backend/src/core/config.py` — 严格配置模型

两个新模型均继承 `_StrictModel`（`model_config = ConfigDict(extra="forbid")`），非法字段直接拒绝：

| 模型 | 字段 | 默认值 | 约束 |
| --- | --- | --- | --- |
| `CoverTextSpec` | `enabled` | `True` | 封页是否自动写话次 |
| | `x_ratio` / `y_ratio` | `0.5` / `0.78` | `ge=0.05, le=0.95` 归一化锚点（`anchor="mm"` 居中） |
| | `font_size` | `72` | `24~160` |
| | `color` / `stroke_color` | `#FFFFFF` / `#000000` | HEX，渲染层对非法值兜底 |
| | `stroke_width` | `5` | `0~16`，0 表示不描边 |
| | `font_name` | `Hiragino Sans GB` | 经 `resolve_font_path` 解析，失败自动回退默认中文字体 |
| | `template` | `第{ep}话` | 占位符 `{ep}`（随数字格式切换）、`{ep_no}`（恒阿拉伯） |
| | `use_chinese_numeral` | `True` | `第一话` ↔ `第1话` |
| `SeriesBrandingConfig` | `cover_image` / `outro_continue_image` / `outro_final_image` | `None` | 相对 `series_dir` 路径，如 `assets/branding/cover.png` |
| | `cover_duration_ms` | `2000` | `500~8000` |
| | `cover_fade_in_ms` / `cover_fade_out_ms` | `350` / `450` | `0~2000` |
| | `outro_duration_ms` | `2500` | `500~10000` |
| | `outro_fade_in_ms` / `outro_fade_out_ms` | `700` / `600` | `0~2500` |
| | `default_outro_mode` | `continue` | `Literal["none","continue","final"]` |
| | `cover_text` | `CoverTextSpec()` | `Field(default_factory=...)` |

`SeriesConfig` 新增 `branding: SeriesBrandingConfig = Field(default_factory=SeriesBrandingConfig)`。旧版不含 `branding` 的 `series_config.yaml` 反序列化时自动获得全套默认值，完全向后兼容。

### 2. `backend/src/core/branding_render.py`（新建）

**常量**：`BRANDING_DIR_NAME = "assets/branding"`、`RENDER_OPTIONS_FILENAME = "render_options.json"`、`BRANDING_SLOTS = ("cover", "outro_continue", "outro_final")`、`MAX_BRANDING_BYTES = 15MB`、画布 `1080×1920`。

**图片规范化**
- `decode_base64_image(content_base64: str) -> bytes`：兼容带/不带 `data:image/...;base64,` 前缀；`base64.b64decode(validate=True)`；解码后超 15MB 抛 `ValueError`。
- `save_branding_image(raw_bytes, out_path) -> Path`：Pillow 打开 → `ImageOps.exif_transpose` 纠偏 → 统一 `RGBA`/`RGB` → 标准 PNG 落盘（自动建父目录）。

**话次文案**
- `int_to_chinese(n) -> str`：覆盖 1~9999（`1→一`、`10→十`、`11→十一`、`20→二十`、`105→一百零五`，含「一十X → 十X」口语修正）；超出范围回退阿拉伯数字字符串。
- `format_cover_label(episode_no, spec, override_text=None) -> str`：非空 `override_text` 直接 `.strip()` 返回；否则 `template.format(ep=..., ep_no=...)`，`{ep}` 随 `use_chinese_numeral` 切换中文/阿拉伯，`{ep_no}` 恒阿拉伯。

**自适应画布**
- `fit_image_to_canvas(image_path, *, width=1080, height=1920) -> Image.Image`：源图比例与画布比例差 `<= 0.03` 时直接 Lanczos 缩放；否则先 `ImageOps.fit` cover-crop 放大裁切 + `GaussianBlur(radius=30)` + `Brightness.enhance(0.62)` 制柔暗底图，再把源图 `ImageOps.contain` 等比居中贴上——无生硬黑边，且用户预留的写字区域不被裁掉。
- `render_cover_image(...)`：在铺好的 1080×1920 底图上，`enabled=True` 时以 `(x_ratio*width, y_ratio*height)` 为 `anchor="mm"` 用 `ImageDraw.text` 写话次；字体经 `resolve_font_path(cover_text.font_name)` → 失败回退 `DEFAULT_FONT_NAME` → 再失败 `ImageFont.load_default()` 三级兜底；HEX 颜色正则校验失败回退白字/黑描边；输出 RGB PNG。
- `render_outro_image(...)`：仅铺图，输出 RGB PNG。

**单话渲染选项**
- `EpisodeRenderOptions(_StrictModel)`：`cover_enabled: bool = True`、`cover_text_override: str | None = None`（空白字符串经 validator 归一为 `None`）、`outro_mode: Literal["inherit","none","continue","final"] = "inherit"`。
- `load_episode_render_options(episode_dir)` / `save_episode_render_options(episode_dir, options)`：读写 `episode_dir/render_options.json`；文件不存在或 JSON/模型损坏时安全返回默认实例，不抛异常。
- `@dataclass(frozen=True) EffectiveBranding` + `resolve_effective_branding(series_config, options, series_dir)`：解析出最终封页开关与底图绝对路径、已解析掉 `inherit` 的结尾模式与结尾图路径、生效的全部时长/转场毫秒、封面文字规格与覆盖文案；`inherit` 回落到作品 `default_outro_mode`；对应图片文件缺失时优雅降级（封页关闭或结尾 `none`）。

### 3. `backend/src/stages/s7_renderer.py` — 严格音画与转场数学

全部新参数带默认值，缺省链路滤镜字符串与历史逐字一致，现有 20 项 S7 测试零修改通过。

**`_prepare_audio(...)` 新增参数**
- `audio_offset_ms=0`：每条 `usable` 人声与 `sfx_inputs` 的 `start` 先加偏移再 `adelay`，把正文音频整体后移到封面之后。
- `bgm_fade_in_ms=0` / `bgm_fade_out_ms=0`：`[bgm]` 链 `atrim` 后按总时长夹取追加 `afade=t=in` / `afade=t=out`；全 0 时不追加任何滤镜。

**`_render_segment(...)` 新增参数**
- 封页：`cover_png`、`cover_duration_ms`、`cover_fade_in_ms`、`cover_fade_out_ms`；结尾：`outro_png`、`outro_duration_ms`、`outro_fade_in_ms`、`outro_fade_out_ms`；音频/字幕：`seg_audio_start_ms`、`seg_audio_len_ms`、`subtitle_shift_ms`。
- 输入顺序：背景 N → 前景 N → 字幕 M → cover（`-loop 1 -framerate {fps} -t`）→ outro → bgm → voice → sfx。

**封页数学（仅第一段接入）**
```
eff_cover_ms   = max(200, int(cover_duration_ms))
eff_fade_out   = min(max(0, cover_fade_out_ms), eff_cover_ms-100, seg_len_ms-100)
eff_fade_in    = min(max(0, cover_fade_in_ms),  eff_cover_ms-eff_fade_out)
```
- 封面视频流：`fade=t=in:st=0:d={eff_fade_in}`（>0 时）后统一 `fps={fps},settb=AVTB,format=yuv420p[vcover]`；正文 `[vcat]` 同样规范 `fps/settb/format`。
- `eff_fade_out >= 50`：正文先 `tpad=start_duration={fade_out_s}:start_mode=clone`，再 `[vcover][vbody_pre]xfade=transition=fade:duration={fade_out_s}:offset={(eff_cover_ms-eff_fade_out)/1000}[vwithcover]`；否则 `concat=n=2:v=1:a=0`。
- 数学保证：**正文第 1 帧在 `t = eff_cover_ms` 精确完全显现**。
- 字幕 `enable='between(t, start+shift, end+shift)'`，第一段 `subtitle_shift_ms = eff_cover_ms`，封面停顿期间绝无字幕。

**结尾数学（仅最后一段接入；全片单段时封页与结尾先后作用于同一段）**
```
eff_outro_ms   = max(200, int(outro_duration_ms))
pre_outro_ms   = seg_len_ms + (eff_cover_ms if cover_active else 0)
eff_outro_in   = min(max(0, outro_fade_in_ms),  eff_outro_ms-100, seg_len_ms-100)
eff_outro_out  = min(max(0, outro_fade_out_ms), eff_outro_ms-eff_outro_in)
```
- 结尾视频流：`fade=t=out:st={(eff_outro_ms-eff_outro_out)/1000}:d={eff_outro_out}`（>0 时）后规范为 `[voutro]`。
- `eff_outro_in >= 50`：当前视频流先 `tpad=stop_duration={in_s}:stop_mode=clone`，再 `xfade=transition=fade:duration={in_s}:offset={pre_outro_ms/1000}[vfinal]`；否则 `concat=n=2`。

**音频切片与分段总长**
- `seg_total_ms = seg_len_ms + (eff_cover_ms if cover_active) + (eff_outro_ms if outro_active)`。
- `seg_audio_start_ms`：第一段 `0`，后续段 `eff_cover_ms + seg_begin_ms`；BGM/人声/SFX 输入按 `-ss/-t` 切片，输出 `-t seg_total_ms`。
- `render_timeline(...)` 新增同名可选参数并按首段/末段透传，备音总时长 `extended_total_ms = total_ms + eff_cover_ms + eff_outro_ms`；无片包时偏移恒 0、分段口径与历史完全一致。

**`render_episode(...)` 集成**
- 读 `load_series_config(series_dir).branding` 与 `load_episode_render_options(episode_dir)`，经 `resolve_effective_branding` 解析；有效底图时在 `work_dir` 生成 `cover_composed.png` / `outro_composed.png` 传给 `render_timeline`，BGM 淡入淡出仅在对应 composed 图存在时传值。
- 底图文件不存在或渲染异常只 `print` 警告并优雅跳过，不抛异常中断流水线；渲染结束后 `work_dir` 仍按原逻辑清理。

### 4. `backend/src/web/server.py` — HTTP 接口（共 7 个新增触点）

模块级辅助：`_BRANDING_SLOT_FIELDS` / `_BRANDING_URL_FIELDS`（槽位 ↔ 配置字段/URL 字段映射）、`_branding_image_url`（`/workspace/{series_id}/{rel}?v={st_mtime_ns}` 防缓存，缺文件返 `None`）、`_series_branding_payload`（组装完整 branding payload，含 `font_options` 仅列本机存在字体）、`_persist_branding`（读旧 yaml → 合并 `branding` 键 → `safe_dump(allow_unicode=True, sort_keys=False)`，保留其它顶层键与已有 `*_image`）、`_resolve_episode_dir`（从 `{series_id}_ep_{no:03d}` 复合 ID 反解目录，未知返 404）。

| 方法与路径 | 行为 |
| --- | --- |
| `GET /api/series/{series_id}/branding` | 返回 `{series_id, ...SeriesBrandingConfig, cover_url, outro_continue_url, outro_final_url, font_options}` |
| `PUT /api/series/{series_id}/branding` | 合并更新 `cover_text`、6 个毫秒字段、`default_outro_mode`；**不覆盖已有 `*_image`**；`ValidationError` → 400 |
| `POST /api/series/{series_id}/branding/upload` | JSON `{slot, content_base64, filename?}`；校验槽位与 Base64 后 `save_branding_image` 存 `assets/branding/{slot}.png` 并写 yaml，返回完整 payload |
| `DELETE /api/series/{series_id}/branding/{slot}` | `unlink(missing_ok=True)` 删 PNG，yaml 对应槽位置 `None` |
| `POST /api/series/{series_id}/branding/cover-preview` | 用已上传 `cover_image` + `render_cover_image` 实时生成含真实文字的 PNG，`Response(content=bytes, media_type="image/png")`；未上传封面返 404 |
| `PUT /api/episodes/{episode_id}/render-options` | Pydantic 直接收 `EpisodeRenderOptions`，存 `episodes/ep_XXX/render_options.json`，返回 `{episode_id, render_options}` |
| `WorkspaceView.episode_item()` | episode JSON 附带 `"render_options": load_episode_render_options(episode_dir).model_dump()`，`GET /api/series/{id}` 自动逐话带回 |

---

## 三、前端实现

### 1. `src/types/studio.ts`
新增 `OutroMode = 'inherit' | 'none' | 'continue' | 'final'`、`SeriesDefaultOutroMode = 'none' | 'continue' | 'final'`、`CoverTextSpec`、`SeriesBrandingConfig`、`SeriesBrandingResponse`（扩展配置并加 `series_id` / 三个 `*_url` / `font_options`）、`EpisodeRenderOptions`；`EpisodeItem` 加可选 `render_options?: EpisodeRenderOptions`。

### 2. `src/components/SeriesBrandingPanel.tsx`（新建）
默认折叠的圆角白卡片，样式与 `SeriesTtsPanel` 完全对齐（ChevronDown 旋转、红色错误条、蓝色主按钮、`accent-blue-600` 滑块），展开时才拉取数据。
- **三图上传区**：「通用封页底图 / 待续结尾图 / 完结结尾图」，隐藏 `<input type="file" accept="image/png,image/jpeg,image/webp">` → `FileReader.readAsDataURL` → JSON 上传；缩略图 + 重新上传 + Trash2 清除。
- **9:16 可视化封页编辑器**：封面图上按 `(x_ratio, y_ratio)` 实时渲染话次文字（内置与后端同口径的 `intToChinese` / `formatCoverLabel`）；指针按下/拖拽（`setPointerCapture`）移动文字，归一化夹在 5%~95%，与 X%/Y% 滑块双向同步；提供写字开关、中文/阿拉伯数字开关、模板输入（默认 `第{ep}话`）、字号 24~160、文字色/描边色/描边宽 0~16、预览话次号，以及「后端真实渲染校验」按钮（调 `/cover-preview` 拿 blob 内联展示 Pillow 落盘效果）。
- **时长与默认策略**：6 个毫秒滑块（封面总时长/淡入/叠化、结尾总时长/浮现/淡出）+ 默认结尾 segmented control（待续/完结/不加）+「保存片包配置」。

### 3. `src/components/CatalogView.tsx`
- `SeriesTtsPanel` 下方挂载 `<SeriesBrandingPanel seriesId={series.series_id} />`。
- 剧集表每行操作列左侧新增紧凑「结尾图」下拉（跟随默认/待续/完结/不加），切换即用当前 `render_options` 合并后调 `onUpdateEpisodeRenderOptions` 立即保存，批量出片前即可定好大结局用「完结」、其余用「待续」。

### 4. `src/components/EpisodeStudioView.tsx`
步骤条卡片下方新增「本话封页与结尾片包（仅重跑 S7 生效）」紧凑配置条：封页启用开关、封面文字覆盖输入（留空自动按作品模板生成；500ms 防抖自动保存，空白归一 `null`）、结尾四选下拉（即时保存）；右侧内联状态显示「正在保存… / 已保存 HH:MM:SS · 修改后仅需从 S7 合成成片重跑即可生效 / 错误文案」，全程无浏览器原生弹窗。

### 5. `src/App.tsx`
新增 `handleUpdateEpisodeRenderOptions(episodeId, options)`：`PUT /api/episodes/{id}/render-options` 成功后复用既有 `patchEpisode` 不可变更新本地 `seriesList` 中该话的 `render_options`，并透传给 `CatalogView` 与 `EpisodeStudioView`。

---

## 四、测试覆盖（`backend/tests/test_patch_v45_branding.py`，15 项，不触网）

1. `int_to_chinese`：1/10/11/23/105/9999 与超范围回退。
2. `format_cover_label`：模板四态、阿拉伯模式、`{ep_no}`、`override_text` strip、纯空白回退。
3. `decode_base64_image`：三种前缀形态 + 15MB 体积限制。
4. `save_branding_image`：WebP 输入规范化为标准 PNG。
5. `render_cover_image` / `render_outro_image`：9:16 与横图均输出严格 1080×1920 RGB；横图柔暗底四角非纯黑。
6. 写字有效性：`ImageChops.difference.getbbox()` 证明写字后像素确实变化；非法 HEX 兜底不报错；override 生效。
7. 旧版 `series_config.yaml`（无 `branding`）加载默认值正常。
8. branding 配置 yaml 往返字段不丢。
9. `EpisodeRenderOptions` 往返、损坏文件安全回退、严格枚举拒绝非法值。
10. `resolve_effective_branding`：`inherit → default_outro_mode`、完结图缺失降级 `none`、`cover_enabled=False`。
11. 真 FFmpeg `render_timeline`：封面 1200ms + 正文 2000ms + 结尾 1400ms + BGM，ffprobe 可解析、总时长 4600ms（±150ms）、音视频双流齐全；无片包基线仍为 2000ms。
12. 真 FFmpeg `render_episode` 端到端三态（4600 / 3200 / 2000ms），`work_dir` 渲染后清理。
13. 底图缺失优雅降级，不中断流水线。
14. TestClient 全流程：上传 3 图 → GET 返回 3 个带 `?v=` 的 URL → PUT 改坐标/模式/时长 → cover-preview 返 `200 image/png` → PUT render-options 切 `final` 且 GET series 回读 `outro_mode == "final"` → DELETE `outro_final` 文件消失且配置置 `None`。
15. 错误路径：未上传封面预览 404、非法槽位 400、非法 Base64 400、非法枚举 422、未知单话/作品 404。

---

## 五、验收命令与结果

```bash
# 1. 后端静态检查
ruff check backend/src backend/tests

# 2. 后端测试（本补丁 + 全部受影响回归集）
backend/.venv/bin/python -m pytest backend/tests/test_patch_v45_branding.py \
  backend/tests/test_s7_renderer.py backend/tests/test_web_server.py \
  backend/tests/test_config.py backend/tests/test_schemas.py

# 3. 前端类型检查与生产构建
npm run lint && npm run build
```

验收结果（真实执行回填）：

- `ruff check backend/src backend/tests`：**All checks passed**。
- `pytest`（五文件）：**127 项全部通过**（含本补丁 15 项、S7 渲染 20 项、Web 服务、配置与 schema 回归）。
- `npm run lint`（`tsc --noEmit`）：**零报错**；`npm run build`（vite build）：**构建成功**，1670 模块转换通过。
