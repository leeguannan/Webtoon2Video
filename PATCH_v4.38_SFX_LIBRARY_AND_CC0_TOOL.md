# PATCH v4.38：音效曲库扩到 24 键 + CC0 采编工具 + 试听选配

- 基线提交：`057fadb`（PATCH_v4.37）
- 上一版：v4.37（第 1 次音频升级：SFX 响度对齐 + 双源侧链 + -14 LUFS 两遍母带）
- 本补丁性质：后端音频链路 + 素材供给 + 前端选配交互

---

## 一、目标与铁律

### 1.1 目标

| # | 目标 | 判定 |
| --- | --- | --- |
| 1 | 修正 PATCH v4.37 的两处底层细节（侧链 attack/release 单位、响度测量空值语义） | `ffmpeg -h filter=sidechaincompress` 的 attack/release 单位是**毫秒**；`_measure_audio_lufs` 必须能表达「测不出」 |
| 2 | SFX 白名单从 7 键扩到 **24 键**，并与 Stage 4 提示词、Stage 6 关键词兜底联动 | 新键可用；**旧 7 键的关键词命中 100% 不变** |
| 3 | 提供**离线** CC0 音效采编工具，补齐盘面缺失的真实素材 | 严格只收 CC0；不接入线上出片流水线 |
| 4 | 后端提供音效曲库 / 试听 / 上传 API，前端解说台可逐句选配并试听 | 审核台能看见能听见，且能改 |
| 5 | 补齐单测 + 全量验证 | pytest / ruff / tsc / build 全绿 |

### 1.2 铁律

1. **不猜单位**：滤镜参数单位必须以 `ffmpeg -h filter=xxx` 的实测输出为准，不靠记忆推断。
2. **测不出 ≠ 0**：响度测不出来时返回 `None` 并跳过校准，绝不能把 0 当实测值去算偏置（那等于放大底噪）。
3. **旧命中不可回归**：新增关键词组只能「加长不能加短」，且必须排在旧组之后靠词条粒度区分，绝不能抢走旧组原有的命中。
4. **素材来源必须可追溯**：新增素材只收 CC0，且在 `SOURCES_CC0.json` 记录来源 URL / 作者 / 许可证。
5. **采编工具离线化**：`backend/scripts/` 下的工具不进在线流水线，不被任何 stage import。

---

## 二、文件白名单

### 2.1 修改（10 个）

| 文件 | 改动 |
| --- | --- |
| `backend/src/stages/s7_renderer.py` | Part 1 两处修正 + `resolve_sfx_asset` 回源 |
| `backend/src/core/normalize.py` | 24 键白名单 + 目录 + 别名表 + 兜底查询 |
| `backend/src/stages/s6_timeline.py` | `SFX_KEYWORD_MAP` 扩到 21 组 |
| `backend/src/stages/s4_scriptwriter.py` | 冲击 / 转折音效集合补 6 键 |
| `backend/src/web/server.py` | 3 个音效曲库 API |
| `backend/prompts/script_narrate.md` | 【铁律二点九】按分类列 24 键 |
| `backend/tests/test_patch_v437_audio_mastering.py` | 断言改为毫秒原值 + `None` 语义 |
| `backend/tests/test_s7_renderer.py` | 侧链参数断言改为毫秒原值 |
| `src/types/studio.ts` | `SfxLibraryItem` / `SfxLibraryResponse` 类型 |
| `src/components/EpisodeStudioView.tsx` | 逐句「音效」选择 + 试听；音效改动标记需重建时间轴 |

### 2.2 新增（3 个）

| 文件 | 说明 |
| --- | --- |
| `backend/scripts/fetch_cc0_sfx.py` | 离线 CC0 采编工具（842 行，纯标准库 + FFmpeg） |
| `backend/tests/test_patch_v438_sfx_library.py` | 本补丁 20 条单测（383 行） |
| `src/api/studioApi.ts` | 前端音效曲库 API 层（57 行） |

改动规模：`+488 / -46`（不含 3 个新文件 1282 行）。

---

## 三、问题定位

| # | 现象 | 根因 |
| --- | --- | --- |
| R1 | v4.37 的双源闪避实测「有 SFX / 无 SFX 窗口 RMS 完全相同」，闪避形同虚设 | `sidechaincompress` 的 attack/release 单位是**毫秒**，v4.37 误当成秒并除以 1000，实际传的是 0.025ms / 0.350ms——压缩器来不及起效 |
| R2 | 完全无声的素材被硬塞一档负偏置 | `_measure_audio_lufs` 用 `0.0` 同时表示「测到了 0 LUFS」和「测不出来」，调用方拿它当实测值算出 -16dB 偏置 |
| R3 | 音效种类只有 7 种，剧本写到「玻璃碎裂 / 雷声 / 掌声 / 耳光」只能硬套 `crash` | 白名单 `SFX_CUE_KEYS` 只列 7 键；盘面实际躺着 11 个文件，其中 4 个（phone_*）根本没挂进白名单 |
| R4 | 盘面缺 13 个新键的真实素材 | 没有任何素材补给手段，只能手抖拷文件 |
| R5 | 审核台看不到也听不到音效，只能回去改 JSON | 后端没有音效曲库 / 试听 API；前端没有音效控件 |

---

## 四、实现明细

### 4.1 Part 1：修正 PATCH v4.37 两处底层细节

**(1) 侧链 attack / release 改回毫秒原值**

```
实测：ffmpeg -hide_banner -h filter=sidechaincompress
  attack  <double> ..F.A....T. set attack (from 0.01 to 2000) (default 20)
  release <double> ..F.A....T. set release (from 0.01 to 9000) (default 250)
```

→ `_mix_and_master_audio` 里由
`attack={_DUCK_ATTACK_MS / 1000.0:.3f}:release={_DUCK_RELEASE_MS / 1000.0:.3f}`（0.025 / 0.350）
改回 `attack={_DUCK_ATTACK_MS}:release={_DUCK_RELEASE_MS}`（25 / 350）。

**(2) `_measure_audio_lufs` 返回 `float | None`**

- 完全静音 / 解析失败 / 文件缺失 → `None`；
- 缓存类型改 `dict[str, float | None]`；
- 调用方：
  - SFX 校准：`if measured is None: calibration = 0.0`（不校准）；
  - 人声总线校验：`if voice_lufs is not None and abs(voice_lufs - _VOICE_TARGET_LUFS) > _VOICE_LUFS_TOLERANCE`（跳过，不动原样）。

### 4.2 Part 2：24 键白名单与联动

**白名单（`core/normalize.py`）**——7 旧键 + 4 已存在未挂载键 + 13 新键 = 24：

```
heart_thump, door_slam, door_open, footstep, slap, punch, sword_clash,
cloth_rustle, whoosh, gasp, boom_hit, applause, crash, glass_shatter,
thunder, clock_tick, page_turn, phone_msg, phone_ringing, phone_vibrating,
phone_call, phone_unavailable, camera_shutter, car_horn
```

同批新增：

- `SFX_CUE_CATALOG`：24 条 `{key, label, category, fallback_key}`（category 分生理 / 门 / 脚步 / 击打 / 爆炸 / 碎裂 / 摩擦 / 人群 / 钟表 / 纸页 / 相机 / 交通 / 通讯）；
- `SFX_CUE_ALIASES`：`slap_face→slap`、`phone_ring→phone_ringing`、`vibrating→phone_vibrating`、`explosion→boom_hit`（只收敛明确同义）；
- `sfx_fallback_key(sfx_key)`：由目录派生的兜底查询。

归一化顺序：`小写 → 查别名 → 查白名单 → 都不中就置 None`。**`not_a_real_key` / `none` / `None` / `''` 依旧归一成「没有音效」**。

**Stage 6 关键词（`s6_timeline.py`）**——`SFX_KEYWORD_MAP` 从 7 组扩到 21 组。

兼容手法：新组**整体排在旧 7 组之后**，且新词条一律比旧词条长（`玻璃碎` vs `碎`、`铃响` vs `铃`、`嗡嗡震` vs `嗡`），因此旧文本仍按原顺序命中旧组：

| 新组 | 关键词（部分） |
| --- | --- |
| door_open | 推门 / 推開 / 推开 / 门推开 |
| slap | 耳光 / 巴掌 / 掌掴 |
| punch | 拳头 / 一拳 / 拳击 / 重击 |
| sword_clash | 刀剑 / 剑鸣 / 锵 / 铮 |
| cloth_rustle | 衣料 / 布料 / 窸窣 |
| boom_hit | 轰击 / 轰鸣 |
| applause | 掌声 / 鼓掌 / 喝彩 |
| glass_shatter | 玻璃碎 / 碎裂 / 玻璃破 |
| thunder | 雷声 / 雷鸣 / 霹雳 / 打雷 |
| clock_tick | 滴答 / 嘀嗒 / 钟表 / 秒针 |
| page_turn | 翻页 / 翻动书页 |
| camera_shutter | 快门 / 咔嚓 |
| car_horn | 鸣笛 / 喇叭声 |
| phone_ringing | 来电 / 铃响 / 电话铃 |
| phone_vibrating | 震动 / 嗡嗡震 |

**Stage 4 / 提示词**——`MOTION_IMPACT_SFX_CUES` 与 `PACE_TURN_SFX` 各补 6 键（slap / punch / sword_clash / glass_shatter / boom_hit / thunder）；`backend/prompts/script_narrate.md`【铁律二点九】改为按分类列出 24 个合法 key，仍保留「全话最多 4~8 处」「一条 beat 只能给 1 个 key」「宁缺毋滥」。

**素材回源（`s7_renderer.py`）**——`resolve_sfx_asset` 改为：

1. 真实文件优先（按 `.mp3 → .m4a → .wav → .ogg`）；
2. 没有就按目录里的 `fallback_key` 回源（如 `slap→door_slam`、`punch→crash`、`door_open→whoosh`）；
3. 回源链最多两级，不会成环；
4. 都没有返回 `None`。

### 4.3 Part 3：离线 CC0 采编工具 `backend/scripts/fetch_cc0_sfx.py`

纯 Python 标准库 + FFmpeg，**不被任何 stage import**，与线上出片流水线完全隔离。

| 能力 | 实现 |
| --- | --- |
| 搜索 | `search_freesound_cc0(query, *, pages=2)`：抓 `https://freesound.org/search/` HTML，多策略正则提取 sound-id / 标题 / 描述 / 作者 / 许可证 / 时长 / 下载量 / `-hq.mp3` 预览链接 |
| 许可证过滤 | `parse_freesound_html` **只保留 license 含「Creative Commons 0」的条目**；单条解析失败跳过、不抛异常；网络异常返回空列表 + 警告 |
| 排序 | `score_candidate`：下载量越高、时长越贴近 1.2s 得分越高 |
| 内置检索词 | `CURATED_SFX_QUERIES` 覆盖 13 个新键，每个 2~3 个英文检索词 |
| 预处理 | `preprocess_audio`：`silenceremove` 裁首尾静音 → 限制最长 3.0s（含尾部 afade）→ `loudnorm=I=-16:TP=-1.5:LRA=11` → `-ar 44100 -ac 2 -b:a 192k` |
| 落盘 | `backend/assets/sfx/<key>.mp3` |
| 溯源 | `backend/assets/sfx/SOURCES_CC0.json` 记录 Freesound URL / 作者 / 许可证 |
| CLI | `--missing-only` / `--only k1,k2` / `--dry-run` / `--overwrite` / `--assets-dir` / `--ffmpeg-bin` / `--max-duration` / `--pages` / `--timeout` |

### 4.4 Part 4：曲库 API 与前端选配

**后端（`web/server.py`）**

| 路由 | 行为 |
| --- | --- |
| `GET /api/sfx-library` | 返回 `{items, aliases, allowed_suffixes}`；每项含 `key/label/category/available/resolved_filename/fallback_key/url` |
| `GET /api/sfx-file/{sfx_key}` | key 必须属于 24 键白名单，否则 **400**；用 `resolve_sfx_asset` 取盘面文件，`FileResponse(..., content_disposition_type="inline")`；取不到 **404** |
| `POST /api/sfx-library/upload` | Base64 JSON；校验 key / 后缀（.mp3 .wav .m4a .ogg）/ 体积（≤ 30MB）；覆盖落盘 `backend/assets/sfx/<key><ext>`；返回刷新后的曲库 |

**前端**

- `src/api/studioApi.ts`（新建）：`fetchSfxLibrary()` / `uploadSfxAsset()` / `sfxFileUrl()`，统一错误信息提取 FastAPI `detail`；
- `src/types/studio.ts`：`SfxLibraryItem` / `SfxLibraryResponse`；
- `src/components/EpisodeStudioView.tsx`：
  - 每句解说「角色与音色」行下方新增紧凑一行「音效」：按 category 分组的 `<select>`（首项「无音效」→ 置空即写 `null`，命中后端 `NULLABLE_BEAT_FIELDS` 语义）+ 紧邻试听按钮（`Volume2` 图标，播放 `/api/sfx-file/{key}`）；
  - 走**通用兜底**的条目在标签后加「·通用兜底」，避免把兜底素材误当成真素材；
  - 全局单一 `HTMLAudioElement` 试听实例，播新的先停旧的；
  - 曲库拉取失败静默，不阻塞审核台；
  - 改 `sfx_cue` 会触发「未保存」标记，并置 `needsTimelineRebuild` → 从 S7 重跑时自动升级到 S6 重跑（音效事件在 S6 排、S7 取素材）。

### 4.5 Part 5：测试

新增 `backend/tests/test_patch_v438_sfx_library.py`（20 例）：

| 组 | 例数 | 覆盖 |
| --- | --- | --- |
| 白名单与目录 | 3 | 24 键集合相等且无重复 / 目录覆盖全部 key / `sfx_fallback_key` 取值 |
| 归一化与别名 | 3 | 4 条别名收敛 / 24 个标准键归一化不变 / `not_a_real_key`·`none`·`None`·`''` 仍为 None |
| Stage 6 关键词 | 4 | 旧 13 个关键词命中 100% 不变 / 安静描述不触发 / 12 个新关键词命中 / 旧 7 组仍在 map |
| 素材回源 | 3 | 真实文件优先 / 缺文件走 fallback / 全缺返回 None |
| Web API | 4 | `/api/sfx-library` 结构 / `/api/sfx-file/{key}` 回源 / 非法 key 400 / 缺素材 404 |
| 采编工具 | 3 | Mock HTML 严格 CC0 过滤 + 字段解析 / `CURATED_SFX_QUERIES` 覆盖 13 键 / 垃圾 HTML 不抛 |

既有测试同步：`backend/tests/test_patch_v437_audio_mastering.py` 与 `backend/tests/test_s7_renderer.py` 中侧链参数断言由 `0.025 / 0.350` 改为 `25 / 350`。

---

## 五、验证结果

### 5.1 静态与自动化

| 命令 | 结果 |
| --- | --- |
| `cd backend && .venv/bin/python -m pytest tests/test_patch_v438_sfx_library.py -q` | **20 passed** |
| `cd backend && .venv/bin/python -m pytest -n auto --dist loadfile -q` | **EXIT 0**（全量通过） |
| `cd backend && /opt/homebrew/bin/ruff check src tests` | **All checks passed!** |
| `npx tsc --noEmit` | **EXIT 0** |
| `npm run build` | **✓ built in 218ms** |

### 5.2 运行时自检

```
KEYS: 24   CATALOG: 24
ALIASES: {'slap_face': 'slap', 'phone_ring': 'phone_ringing',
          'vibrating': 'phone_vibrating', 'explosion': 'boom_hit'}
fallback slap -> door_slam
assets_root: <repo>/backend/assets
available(23/24): heart_thump.mp3 / door_slam.mp3 / door_open→whoosh.mp3 /
  footstep.wav / slap→door_slam.mp3 / punch→crash.wav / sword_clash→crash.wav /
  cloth_rustle→whoosh.mp3 / whoosh.mp3 / gasp.wav / boom_hit→crash.wav /
  applause→gasp.wav / crash.wav / glass_shatter→crash.wav / thunder→crash.wav /
  page_turn→whoosh.mp3 / phone_msg.mp3 / phone_ringing.mp3 / phone_vibrating.mp3 /
  phone_call.mp3 / phone_unavailable.mp3 / camera_shutter→whoosh.mp3 /
  car_horn→whoosh.mp3
missing: ['clock_tick']
```

### 5.3 真机接口验证（后端重启加载 v4.38 后）

```
GET /api/sfx-library → items: 24
  aliases: {'slap_face': 'slap', 'phone_ring': 'phone_ringing',
            'vibrating': 'phone_vibrating', 'explosion': 'boom_hit'}
  allowed_suffixes: ['.mp3', '.wav', '.m4a', '.ogg']
  available: 23 | 其中走通用兜底: 12
  sample: {"key": "footstep", "label": "脚步", "category": "脚步",
           "available": true, "resolved_filename": "footstep.wav",
           "fallback_key": "", "url": "/api/sfx-file/footstep"}

GET /api/sfx-file/not_a_real_key  → HTTP 400
GET /api/sfx-file/heart_thump     → HTTP 200（271,872 bytes）
GET /api/sfx-file/clock_tick      → HTTP 404（无真素材且刻意不配兜底）
```

### 5.4 真机界面验证（审核台，1440×779，真实话次 wt_3924 EP.001 / 121 句）

| 检查项 | 实测 |
| --- | --- |
| 音效下拉数量 | **121**（每句一个，aria-label 形如「b1 音效」） |
| 分类下拉组 | 13 组：生理(2) 门(2) 脚步(1) 击打(3) 爆炸(3) 碎裂(1) 摩擦(2) 人群(1) 钟表(1) 纸页(1) 相机(1) 交通(1) 通讯(5) |
| 首项 | 「无音效」（默认置空 → 落盘写 `null`） |
| 通用兜底标注 | 12 项带「·通用兜底」后缀（开门/耳光/拳击/刀剑交击/轰击/雷声/玻璃碎裂/衣料摩擦/掌声/翻页/快门/鸣笛） |
| 试听按钮 | 121 个（Volume2 图标），无禁用项（当前句均未设音效） |
| 布局 | 「音效」行位于「配音角色与语速」列内、语速行下方，1440px 无横向溢出 |
| 截图 | `/tmp/v438_sfx_row_1440.jpg` |

补充：后端未重启时（仍是 v4.37 代码），前端曲库请求静默失败 → 下拉只剩「无音效」，**不阻塞审核台**；重启后端加载 v4.38 后 24 项与 13 个分类全部出现。

### 5.5 采编工具自检（离线 Mock）

| 检查 | 结果 |
| --- | --- |
| `parse_freesound_html` 对「2 条 CC0 + 1 条 Attribution」HTML | 返回 **2** 条（非 CC0 被丢弃），sound_id / author / license / duration / downloads / preview_url 全部解析正确 |
| 打分 | 0.515 / 0.501（下载量 + 时长拟合度） |
| `preprocess_audio` 把 5s 正弦压到 | **3.000000s** 的 mp3（73KB） |
| `--help` | 正常打印全部 CLI 选项 |
| 垃圾 HTML | 返回空列表，不抛异常 |

---

## 六、已知限制

1. **盘面仍是 11 个真实素材**：24 键里 13 个新键没有真实素材，靠目录里的 `fallback_key` 兜底（如 `slap→door_slam`）。前端会标「·通用兜底」，但语义不是一一对应（`applause` 兜底是 `gasp`、`camera_shutter` / `car_horn` 兜底是 `whoosh`）。跑一次 `backend/scripts/fetch_cc0_sfx.py --missing-only` 补齐真实 CC0 素材后即自动优先命中。
2. **`clock_tick` 没有任何兜底**：目录里刻意留空，避免拿呼啸声冒充滴答——审核台里它是「暂不可用」状态。
3. **采编工具不自动下载**：默认需要显式执行才会联网；`--dry-run` 只列候选不下盘。许可证过滤是「只认 Creative Commons 0」的字面匹配，Freesound 改版页面结构时解析可能失效（已做多策略正则容错 + 单条跳过不抛）。
4. **音效改动要重跑 S6~S7**：音效事件在 S6 排、S7 取素材，只重跑 S7 拿不到新音效；审核台已内置「自动升级到 S6 重跑」提示。
5. **关键词兜底是单值命中**：一句同时含多个音效词时按 `SFX_KEYWORD_MAP` 的组顺序取第一个（爆炸类优先于击打类），不会同时触发多个音效。

---

## 七、后续

1. 跑 `python backend/scripts/fetch_cc0_sfx.py --missing-only` 把 13 个新键的真实 CC0 素材补齐，消灭兜底。
2. 后续可在审核台加「本话音效预览」总览（一键试听全话音效序列）。
3. 若要支持「一句多音效」，需把 `beat.sfx_cue` 升级为列表并同步 Stage 6 的 `select_sfx_events` 节流策略。
