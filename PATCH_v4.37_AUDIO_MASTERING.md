# PATCH v4.37 —— 第 1 次音频升级：SFX 瞬时响度对齐、双源侧链避让与整片 -14 LUFS 两遍母带

> 基线提交：`613401d`（PATCH_v4.36 切幕预备模式 + 4A/4B 双层解耦）
> 改动范围：`backend/src/stages/s7_renderer.py` ＋ 4 个测试文件
> 性质：后端音频链路改造（纯后端，前端零改动）

---

## 一、要解决的问题与三条铁律

### 1.1 现象

1. **音效电平随落点漂移**：同一条 SFX 素材被放在 3 秒处和 8 秒处，响度不一样；素材电平底子低的那条整体偏轻。
2. **音效盖不住人声**：默认 `sfx_gain_db=-2.0`，但素材本身响度不齐，实际有的盖过配音、有的听不见。
3. **BGM 不会被音效闪避**：一记重击落下时，背景音乐该让位却没让位，整段听感糊在一起。
4. **整片响度不统一**：分段混音各段自行处理，段与段之间动态跳变；整片没有统一的响度基准。

### 1.2 根因

| # | 根因 | 位置 |
| --- | --- | --- |
| R1 | SFX 链是「先 `adelay` 再 `dynaudnorm`」——**前导静音被 dynaudnorm 当成「素材本来就小」**，于是把增益抬起来；素材越长、落点越晚，抬得越离谱 | `_prepare_audio` 旧 SFX 链 |
| R2 | SFX 只叠了「配置增益」（默认 -2.0dB），**从未测过素材本身的响度** | `_prepare_audio` 旧 SFX 链 |
| R3 | `sidechaincompress` 的 `attack` / `release` 单位是**秒**，旧分段链路却传了毫秒值（`attack=30` → 30 秒、`release=350` → 350 秒），**压缩永远来不及起效，闪避事实上一直是关着的** | `_render_segment` 旧音频链 |
| R4 | 分段各自混音，段边界各自归一化 → 接缝处动态跳变；且整片没有统一响度目标 | `_render_segment` / `render_timeline` |

### 1.3 三条铁律

1. **先量后校**：任何音效素材在进入混音前，必须先量出「去掉前导静音后的有效响度」，再据此校准到统一目标。
2. **先整形后延时**：延时（`adelay`）必须放在整形（校准增益 / 淡入 / 限幅）**之后**，否则静音会污染测量结果。
3. **整片只母带一次**：响度对齐、闪避、限幅、归一全部在整片层面做一次；分段只做切片，不再各自混音。

---

## 二、文件白名单

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `backend/src/stages/s7_renderer.py` | 改 | 核心：SFX 校准、人声总线校验、双源侧链、两遍母带、分段接母带 |
| `backend/tests/test_patch_v437_audio_mastering.py` | 新增 | 13 例（5 纯逻辑 + 5 真机 + 3 mock） |
| `backend/tests/test_patch_v32_audio_mix.py` | 改 | 旧 `dynaudnorm` 断言改为新契约 |
| `backend/tests/test_patch_v31_audio.py` | 改 | 同上 |
| `backend/tests/test_s7_renderer.py` | 改 | 闪避参数断言改为「毫秒常量 → 秒」换算后的实参 |

**未改动**：`backend/src/core/`、`backend/src/web/`、`backend/src/stages/` 其余文件、`src/`（前端零改动）。

---

## 三、后端实现

### 3.1 新增常量（`s7_renderer.py`）

```
_SFX_TARGET_LUFS          = -16.0   # 音效校准目标
_SFX_CALIBRATION_LIMIT_DB = 12.0    # 校准增益上下限（夹取）
_VOICE_TARGET_LUFS        = -16.0   # 人声总线目标
_VOICE_LUFS_TOLERANCE     = 1.2     # 人声容差（超了就校）
_VOICE_CORRECTION_LIMIT_DB= 6.0     # 人声修正上下限
_MASTER_TARGET_LUFS       = -14.0   # 整片母带目标
_MASTER_TRUE_PEAK         = -1.5
_MASTER_LRA               = 9
_MASTER_LIMIT             = "-1.0dB"
_SFX_LIMIT                = "-1.5dB"
_DUCK_TRIGGER_SFX_BOOST_DB= 3.0     # 侧链触发轨里音效的额外增益
_DUCK_THRESHOLD           = 0.06
_DUCK_RATIO               = 2.4
_DUCK_ATTACK_MS           = 25
_DUCK_RELEASE_MS          = 350
```

### 3.2 `_measure_audio_lufs(audio_path, *, ffmpeg_bin, cache=None)`

用 FFmpeg 测「**去掉前导静音之后**」的有效响度：

```
ffmpeg -hide_banner -nostats -i <path> \
  -af "silenceremove=start_periods=1:start_threshold=-45dB,ebur128=peak=true" -f null -
```

- 优先取 **积分响度 I**（> -60 LUFS 才算有效）；
- 极短音频取**逐帧最大短期/瞬时响度 M**；
- 都测不出 → 回退 `0.0` 偏置（表示「不校准」，而不是当成 -0 dBFS）；
- 同一次 `_prepare_audio` 内按**文件路径缓存**，同一素材多条事件只测一次；
- 任何异常（文件缺失、ffmpeg 失败、解析失败）→ 返回 `0.0`，**绝不把渲染打断**。

### 3.3 `_prepare_audio` 的 SFX 链重写

旧链（问题源头）：

```
aformat → adelay → dynaudnorm → volume → apad → atrim
```

新链：

```
aformat
  → volume={校准增益}dB        # 依据测量结果，clamp(目标 - 实测, -12, +12)
  → volume={配置增益}dB        # 叠加原有 sfx_gain_db / 事件 gain_db
  → afade=t=in:st=0:d=0.005    # 素材 > 20ms 才加，消除咔哒
  → alimiter=limit=-1.5dB:attack=2:release=40
  → adelay={start}|{start}     # 延时放最后
  → apad → atrim=0:{total_s}
```

- **两级 `volume` 的用意**：`volume={配置增益}dB` 这一级保持语义独立、可被测试直接断言；校准级单独一级，避免和用户配置纠缠。
- 默认 `sfx_gain_db=-2.0` 叠加在校准之上 → 成品音效落在 **-18 LUFS**，与人声 `-16 LUFS - 2dB` 对齐。

### 3.4 人声总线偏差校验

`voice.wav` 生成后测一次响度：偏离 `-16.0 LUFS` 超过 `±1.2 LU` 才线性微调（夹取 `±6 dB`）并过限幅；否则**保持原样**，避免无谓重算。

### 3.5 `_mix_and_master_audio(...)`（新函数）

```
① 双源侧链闪避
   有 bgm 时：
     有 sfx → 先合成 duck_trigger.wav
               = amix( voice , sfx×(+3dB) )   duration=first, normalize=0
       无 sfx → duck_trigger = voice.wav
     再： [bgm][duck_trigger] sidechaincompress
            = threshold=0.06 : ratio=2.4 : attack=0.025 : release=0.350
       → bgm_ducked.wav
② 总线预混
   amix( voice , bgm_ducked , sfx )  normalize=0
     → alimiter=limit=-1.0dB:attack=5:release=60
     → pre_master.wav
③ 两遍母带
   Pass 1: loudnorm=I=-14:TP=-1.5:LRA=9:print_format=json -f null -
           解析 JSON（_parse_loudnorm_stats）
           input_i ≤ -60（全静音）或解析失败 → 降级：pre_master 直接复制成 master.wav
   Pass 2: loudnorm=...:linear=true  +  -ar 44100 -ac 2 -c:a pcm_s16le
           → master.wav
```

- **所有侧链输入都是真实 WAV 文件流**——FFmpeg 9 的 `sidechaincompress` 不接受图内 label。
- **Pass 2 必须显式 `-ar 44100 -ac 2 -c:a pcm_s16le`**：否则 loudnorm 会升到 192kHz，文件体积翻几倍。

### 3.6 分段渲染接入母带

`_render_segment(..., master_audio_path: Path | None = None)`：

- **传入 master**：分段只加一路 master 输入，按 `[audio_start_s, audio_start_s + audio_len_s]` 切片后 `aformat → apad → atrim → [aout]`，**不再跑 sidechaincompress / 多路 amix** → 分段接缝不再有动态跳变；
- **不传 master**：完整保留旧的 voice/bgm/sfx 实时混音分支（向后兼容旧单测与旧调用方）。

`render_timeline` 在 `_prepare_audio` 之后调用 `_mix_and_master_audio` 生成 `master.wav`，并把它传给每个分段。

---

## 四、真机验证（FFmpeg 9.0.1）

### 4.1 关键发现：sidechaincompress 的 attack/release 单位是秒

旧分段链路传 `attack=30`（= 30 秒）/ `release=350`（= 350 秒）——压缩器根本没时间起效，「闪避」一直是关着的。
新链路按 `attack={_DUCK_ATTACK_MS / 1000.0:.3f}`（0.025）/ `release={_DUCK_RELEASE_MS / 1000.0:.3f}`（0.350）传入。

### 4.2 响度测量

| 素材 | 幅度 | 实测 |
| --- | --- | --- |
| 正弦 | 0.30 | **-14.1 LUFS** |
| 正弦 | 0.03 | **-34.2 LUFS** |
| 差值 | | **20.1 dB** ✓ 与幅度比一致 |

### 4.3 双源闪避对比

人声静音、BGM 0.25 幅度、SFX 0.5 幅度，量 BGM 轨在音效窗口内的 RMS：

| | 修正前 | 修正后 |
| --- | --- | --- |
| 有 SFX | 4095.5 | **1522.9**（↓ 8.6 dB） |
| 无 SFX | 4095.5 | 4095.5 |
| 非 SFX 窗口（两者） | — | 4095.5 / 4095.5（瞬时闪避，非持续压缩）✓ |

### 4.4 端到端（真实 render_timeline 出片）

2 场景 / 6 秒 / 带 BGM 的真实渲染：

```
[s7] 母带完成：-14.0 LUFS （实测 -15.6 LUFS，修正 +0.00dB）
MASTER fmt: 44100 Hz / 2 ch / 16 bit / 6.0 s
MASTER LUFS = -14.00
MP4    LUFS = -14.00
E2E_RESULT: PASS
```

成片音轨与母带文件同为 **-14.00 LUFS**，采样率 44100 / 双声道 / 16bit。

---

## 五、测试

### 5.1 新增 `backend/tests/test_patch_v437_audio_mastering.py`（13 例）

| # | 用例 | 类型 | 断言要点 |
| --- | --- | --- | --- |
| 1 | `test_parse_loudnorm_stats_reads_json_block` | 纯逻辑 | Pass 1 JSON 能解析成浮点字典 |
| 2 | `test_parse_loudnorm_stats_returns_none_on_garbage` | 纯逻辑 | 解析失败返回 None（调用方据此降级） |
| 3 | `test_measure_audio_lufs_falls_back_to_zero_for_missing_file` | 纯逻辑 | 文件缺失回退 0.0，不抛异常 |
| 4 | `test_v437_constants_are_pinned` | 纯逻辑 | 13 个响度/闪避常量钉死 |
| 5 | `test_duck_attack_release_are_converted_to_seconds` | 纯逻辑 | 源码里必须是 `/1000.0` 换算 |
| 6 | `test_sfx_calibration_aligns_different_source_levels` | 真机 | 底电平差 8dB 的两条素材，校准后差 < 2dB |
| 7 | `test_sfx_calibration_respects_gain_clamp` | 真机 | 底电平差 20dB 超出 ±12dB 能力 → 残留 4~12dB（证明夹紧生效，不会无限放大噪声） |
| 8 | `test_measure_lufs_ignores_leading_silence` | 真机 | 2 秒前导静音素材与纯净素材响度差 < 1.0 LU |
| 9 | `test_sfx_triggers_extra_bgm_ducking` | 真机 | 有/无 SFX 的 BGM 窗口 RMS 比 < 0.75 |
| 10 | `test_master_wav_hits_target_loudness` | 真机 | 44100 / 双声道 / 16bit、-15.5..-12.5 LUFS、峰值 < 32700 |
| 11 | `test_master_degrades_gracefully_on_silence` | 真机 | 全静音不抛异常，master.wav 照常产出 |
| 12 | `test_render_segment_uses_master_without_sidechain` | mock | 传 master → 无 sidechaincompress / 无 amix，含 [aout] |
| 13 | `test_render_segment_keeps_legacy_mix_without_master` | mock | 不传 master → 保留 sidechaincompress + amix=inputs=2 |

### 5.2 既有测试同步更新

| 文件 | 改动 |
| --- | --- |
| `test_patch_v32_audio_mix.py` | `test_prepare_audio_sfx_chain_adelay_dynaudnorm_and_gain` → 更名 `..._calibrates_before_adelay`；断言「`dynaudnorm` 不再出现」+「链内顺序 volume < afade < alimiter < adelay」 |
| `test_patch_v31_audio.py` | `test_render_timeline_mixes_sfx_events` 的 `dynaudnorm` 断言改为 `alimiter=limit=-1.5dB:attack=2:release=40` |
| `test_s7_renderer.py` | `test_sidechain_uses_gentle_v32_ducking` 的参数断言改为「毫秒常量换算成秒后的实参」（0.025 / 0.350） |

---

## 六、验证结果

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 新测试 | `pytest tests/test_patch_v437_audio_mastering.py -q` | **13 passed** |
| 后端全量 | `pytest -n auto --dist loadfile` | **EXIT 0** |
| Lint | `ruff check src tests` | **All checks passed!** |
| 真机端到端 | 真实 render_timeline 出片 | master / MP4 均 **-14.00 LUFS** ✓ |

前端未改动（`src/` 零变更），本补丁不涉及 `tsc` / `build`。

---

## 七、已知限制与后续

1. **校准增益上限 ±12dB**：素材底电平相差超过 24dB 时无法完全拉平（用例 7 已钉死该行为）。这是有意的——无限放大会把底噪一起抬起来。
2. **人声总线只做一次整体校准**：不做逐句对齐；个别句子响度差异大时，仍需在审核台手动调语速。
3. **母带目标 -14 LUFS 是整片基准**：短片（< 30 秒）的 LUFS 积分样本偏少，实测值可能比 -14 高 1~2 LU，属正常范围。
4. **分段不再各自闪避**：老工程若依赖分段级混音（例如临时改过 `_SIDECHAIN_*` 常量），需要重新确认。
5. **后续（第 2 次音频升级）候选**：
   - 逐句人声响度对齐（把 `voice.wav` 拆成逐句再校准）；
   - 按段落（scene）而非整片做 BGM 情绪曲线；
   - 母带参数做成可配置（目前硬编码在常量区）。

---

*本补丁由 PATCH v4.37 实施记录生成；改动仅限 `backend/`，前端零变更。*
