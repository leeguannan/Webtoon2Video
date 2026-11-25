# PATCH v4.38.1 — 离线 CC0 音效采编工具修复 + 13 个音效落盘

> 基线提交：`607eff5`（PATCH v4.38）
> 上游任务：执行 `backend/scripts/fetch_cc0_sfx.py` 实下载 13 个新增音效键
> 涉及文件：`backend/scripts/fetch_cc0_sfx.py`（改）、`backend/tests/test_patch_v438_sfx_library.py`（改）、
> `backend/assets/sfx/*.mp3` + `backend/assets/sfx/SOURCES_CC0.json`（新增素材）

---

## 一、背景

PATCH v4.38 交付了 `backend/scripts/fetch_cc0_sfx.py` 这把离线采编工具，但从未联网实跑过。
本次获得授权后首次实跑，暴露出**五个互相独立的问题**，其中三个会让工具完全不可用，
另两个会让落盘素材在成片里「听不见」或「不是那个声音」。

---

## 二、五个根因

### R1 Freesound 预览音频已迁 CDN，旧路径整批 404

第一次执行，13 个 key 全部失败：

```
HTTP Error 404: Not Found
https://freesound.org/data/previews/15419/15419_hq.mp3
```

Freesound 把预览音频迁到了独立 CDN，旧路径 `freesound.org/data/previews/<id>/<id>_hq.mp3` 已下线。
新路径带作者 uid：

```
https://cdn.freesound.org/previews/<id // 1000>/<id>_<user_id>-hq.mp3
https://cdn.freesound.org/previews/<id // 1000>/<id>_<user_id>-lq.mp3
```

> 注意 `<id // 1000>` 是**千分桶目录**（406464 → `406/`），不是 id 原值。

### R2 新版搜索页把元数据搬进了 `bw-player` 的 data-* 属性

旧版页面是「div.title > a.sound_title」「span.num-downloads」「span.duration」。
新版每页 15 张卡片 `class="bw-search__result"`，卡片里只有一个播放器标签承载全部元数据：

```html
<div class="bw-player" data-sound-id="406464" data-username="kretopi" data-user-id="2825490"
     data-mp3="https://cdn.freesound.org/previews/406/406464_2825490-lq.mp3"
     data-title="Punching-005.wav" data-duration="0.518083"
     data-samplerate="48000.0" data-num-downloads="750"></div>
```

脚本原来那套正则**一个字段都抓不到**，后果是候选时长恒为 `0.00`，
时长拟合度打分整体失效、排序退化成纯下载量排序。
另外新版搜索页**不再输出 mm:ss 格式的时长**，只有 `data-duration` 浮点秒。

### R3 `loudnorm` 对短于 3 秒的音效素材根本失效

素材落盘后逐个量响度，发现两个关键异常：

| 素材 | loudnorm `input_i` | 现象 |
| --- | --- | --- |
| `punch`（0.25s） | **测不到** | 峰值被压到 -4.8dBFS，均值 -21.8dB |
| `door_open`（0.72s） | -25.57 | 目标是 -16，实测 -25.6 |

原因：EBU R128 的积分窗口是 **400ms**，`loudnorm` 的动态模式在素材短于 3 秒时
既量不准 `input_i`（短瞬态直接报 `-inf`/`-70`），又会因为真峰保护把增益压死。
音效素材恰恰全是短瞬态 —— 这条路走不通。

### R4 单声道转双声道时 ffmpeg 默认重矩阵吃掉 3dB

改用峰值归一后，实测落盘峰值是 -6.2dBFS，而目标是 -3.0dBFS，**整整差 3dB**。
逐环节拆解后定位到 `-ac 2`：

```
MP3 → +15.5dB → WAV(-ac 1) : peak -3.0   ✓
MP3 → +15.5dB → WAV(-ac 2) : peak -6.0   ✗ 差 3dB
MP3 → +15.5dB → WAV(-ac 2, pan=stereo|c0=c0|c1=c0) : peak -3.3  ✓
```

`-ac 2` 对单声道源走的是 rematrix（0.707 系数），把电平整体压了 3dB ——
**峰值归一的增益被这一步悄悄吃掉了**。显式 `pan=stereo|c0=c0|c1=c0` 是等功率复制，不衰减。

### R5 Freesound 是全文检索，混进大量「名字完全不相干」的素材

`thunder` 这一 key 实跑选出来的是 **"Magic - Finger snap"（手指弹响）**。
原因：Freesound 全文检索会把标签命中的素材一起返回，而候选排序只看「下载量 × 时长拟合」，
名字里含不含检索词完全不影响排名，于是下载量高的怪东西排在前面。

同批还有两个素材因此选错：`applause` 选成 "Mini_Clap_ses1"、`page_turn` 选成 "Paper Rustle"。

---

## 三、修复明细

全部改动集中在 `backend/scripts/fetch_cc0_sfx.py`。

### 3.1 解析层

| 改动 | 说明 |
| --- | --- |
| 新增 `_CARD_RE` / `_PLAYER_TAG_RE` / `_PLAYER_ATTR_RE` / `_FREESOUND_CDN` | 新版卡片与播放器标签的正则与 CDN 根地址 |
| 新增 `_player_attrs(block)` | 从 `bw-player` 标签读出全部 `data-*` |
| `_split_sound_blocks` | **卡片切分优先**，页面没有卡片结构时回退旧逻辑（旧的 mock HTML 测试不破） |
| `_extract_duration` | 优先 `data-duration`（浮点秒），回退旧的 mm:ss 解析 |
| `_extract_downloads` | 优先 `data-num-downloads`，回退旧的 `num-downloads` 文本 |
| `_extract_name` | 优先 `data-title` |
| `_extract_author` | 优先 `data-username` |
| `_extract_preview_url` | 有 `data-user-id` → 拼 `cdn.freesound.org/previews/<id//1000>/<id>_<uid>-hq.mp3`；否则用 `data-mp3` |
| 新增 `_extract_preview_fallback_url` | 返回 `data-mp3`（低码率）作为备用直链 |
| `FreesoundCandidate` | 新增 `preview_fallback_url` 与 `matched_query` 两个字段 |
| `download_candidate` | 首选地址失败**自动回退备用地址**，两次都失败才抛错（错误信息用「；」拼接） |

### 3.2 排序层

| 改动 | 说明 |
| --- | --- |
| 新增 `_MAX_USABLE_DURATION_S = 8.0` | 超过 8 秒的是环境录音（整段雷雨、街道、掌声），裁到 3 秒只剩噪声；这类候选**直接判 0 分** |
| 新增 `_GENERIC_QUERY_WORDS` | `sound/effect/audio/clip/fx/sfx` 这类几乎每个名字都带的通用词，不参与名称匹配 |
| `score_candidate` | 新增**名称相关性加权**：名字含检索词 `+0.12`，完全不含 `×0.55`；名称匹配时排除通用词 |
| `_collect_candidates` | 记录每条候选来自哪个检索词（`dataclasses.replace`），供打分使用 |
| `CURATED_SFX_QUERIES["thunder"]` | 从 `("thunder", "thunderstorm", "thunder crack")` 收紧为 `("thunder crack", "thunder clap", "thunder rumble", "thunder sound effect")` |

### 3.3 响度层（本补丁最关键的一处）

放弃 `loudnorm`，改为**实测峰值 → 确定性增益 → 限幅**：

```
silenceremove → areverse+silenceremove+areverse → atrim → afade
  → volume={gain}dB
  → [单声道] pan=stereo|c0=c0|c1=c0
  → alimiter=limit=0.8413:level=disabled
  → -ar 44100 -ac 2 -b:a 192k
```

- 新增 `_measure_peak_dbfs(src)`：用 `volumedetect` 的 `max_volume`（样本峰值），
  对短瞬态也稳定，不需要 400ms 积分窗口。
- 新增 `_probe_channels(src)`：解析 ffmpeg 输出的 `Audio: …, mono|stereo, …`，
  单声道时显式复制成双声道，绕开 R4 的 -3dB 重矩阵。
- 增益：`gain = clamp(_SFX_TARGET_PEAK_DBFS(-3.0) - peak, -6, +24)` dB。
- 限幅兜底 `-1.5 dBFS`，任何素材都不会削波。

> 为什么不留 `loudnorm`：成片里的最终响度由 S7 的 `_prepare_audio` 统一校准到
> -16 LUFS（`backend/src/stages/s7_renderer.py`），盘面素材只需要「不静音、不削波、电平确定」。
> 峰值归一对任意长度素材都成立，且结果可复现。

---

## 四、落盘结果

13 个音效全部来自 Freesound 的 **CC0（Creative Commons 0）** 素材，
溯源信息在 `backend/assets/sfx/SOURCES_CC0.json`（每条含 `sound_id` / `name` / `author` /
`license` / `license_url` / `freesound_url` / `preview_url` / `num_downloads` / `duration_s` /
`fetched_at` / `max_duration_s`）。

| key | 采用素材 | 作者 | 时长 | 峰值 | 均值 |
| --- | --- | --- | ---: | ---: | ---: |
| door_open | door - open 01.wav | Anthousai | 0.720s | -3.6 dBFS | -32.9 dB |
| slap | Huge Slap in the Face | njjjjjjjjjjj… | 0.987s | -2.9 | -17.2 |
| punch | Punch #1.mp3 | lewisisminted | 0.252s | -3.2 | -20.2 |
| sword_clash | Sword_Clash (7).wav | JohnBuhr | 0.284s | -3.1 | -21.4 |
| cloth_rustle | Movement.wav | 2create | 0.741s | -3.2 | -21.9 |
| boom_hit | strong hit | axilirate | 1.053s | -3.2 | -18.6 |
| applause | smallcrowd.wav | moxobna | 3.000s | -3.3 | -24.5 |
| glass_shatter | Breaking Glass #1.wav | abstraktgene… | 0.660s | -3.1 | -19.0 |
| thunder | **Thunder.** | PappaBert | 1.794s | -3.4 | -20.3 |
| clock_tick | tick tock | keerotic | 0.842s | -3.2 | -32.1 |
| page_turn | Game UI Screen Transition Book Page Turn | el_boss | 0.531s | -3.2 | -28.4 |
| camera_shutter | DigitalCameraClick.wav | rachinc | 1.209s | -3.2 | -25.0 |
| car_horn | Car Honking | MicktheMicGuy | 0.533s | -3.3 | -17.9 |

峰值全部落在 -2.9 ~ -3.6 dBFS（目标 -3.0，MP3 编码舍入误差），无削波。

**曲库状态变化**：`GET /api/sfx-library` 从「24 项 / 23 可用 / 12 项走通用兜底」
变成 **「24 项 / 24 可用 / 0 项兜底」** —— `clock_tick` 之前刻意没配兜底，现在有了真素材。

---

## 五、测试

`backend/tests/test_patch_v438_sfx_library.py` 由 20 例增至 **29 例**，新增 9 例：

| 用例 | 覆盖点 |
| --- | --- |
| `test_fetch_tool_reads_new_freesound_card_attributes` | 新版卡片解析：名称/作者/时长/下载量/hq 直链/lq 回退 |
| `test_fetch_tool_scores_long_recordings_as_unusable` | 超 8 秒的环境录音判 0 分 |
| `test_fetch_tool_prefers_candidates_whose_name_matches_the_query` | 名称相关性加权（"Thunder crack" > "Magic - Finger snap"） |
| `test_fetch_tool_uses_peak_normalization_instead_of_loudnorm` | 链里有 `volume` + `alimiter`、没有 `loudnorm` |
| `test_fetch_tool_copies_mono_to_stereo_without_attenuating` | 单声道显式 `pan` 复制、双声道不转 |
| `test_fetch_tool_preprocess_lands_near_target_peak` | 真 ffmpeg：落盘峰值落在 -4.5 ~ -1.0 dBFS |
| `test_fetch_tool_download_falls_back_to_second_url` | 首选地址 404 → 自动回退备用地址 |
| `test_fetch_tool_curated_thunder_query_avoids_storm_recordings` | thunder 检索词不再含 "thunderstorm" |

---

## 六、验证

```bash
cd backend && .venv/bin/python -m pytest -n auto --dist loadfile -q   # 全量通过
cd backend && /opt/homebrew/bin/ruff check src tests scripts           # All checks passed!
cd backend && .venv/bin/python -m pytest tests/test_patch_v438_sfx_library.py -q   # 29 passed
curl -s http://127.0.0.1:8123/api/sfx-library                          # 24 项 / 24 可用 / 0 兜底
```

---

## 七、已知限制

1. **名称相关性只是加权，不是硬过滤**：名字不含检索词但确实是目标声音的素材可能被压分；
   反之名字含检索词的怪东西仍可能胜出。人工过一眼 `--dry-run` 输出是必要的。
2. **8 秒门槛是经验值**：确实存在「5 秒的完整掌声」这类合理素材，门槛会把它们压低。
3. **均值电平差异较大**（-17 ~ -33 dB）：峰值归一对间歇性素材（滴答、开门）拉不动均值，
   最终响度仍依赖 S7 的 LUFS 校准。
4. **素材内容未经人工试听验收**：本补丁只做了时长/峰值/名称/许可证的自动化校验。
5. 工具仍然**不接入线上出片流水线**，只往 `backend/assets/sfx/` 落盘。
