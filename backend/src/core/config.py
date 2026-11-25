"""全局配置、作品级覆盖与多音字发音词典的加载。

配置优先级（全局配置 ``config.yaml``）：

1. 显式传入的路径；
2. 环境变量 ``W2V_CONFIG`` 指定的路径；
3. 当前工作目录下的 ``config.yaml``；
4. 内置默认值（无任何配置文件也能直接跑通）。

作品级覆盖位于 ``workspace/{series_id}/series_config.yaml``，其 ``pronunciation_overrides``
优先级高于全局词典。

所有百炼调用统一读取环境变量 ``DASHSCOPE_API_KEY``（见 :func:`resolve_api_key`）。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)

DEFAULT_API_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_API_KEY_ENV = "DASHSCOPE_API_KEY"
#: MiniMax Speech 2.8（Patch v3.4 Patch 16）：与百炼完全独立的第二套凭据。
DEFAULT_MINIMAX_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_MINIMAX_API_KEY_ENV = "MINIMAX_API_KEY"
DEFAULT_MINIMAX_GROUP_ID_ENV = "MINIMAX_GROUP_ID"
CONFIG_FILENAME = "config.yaml"
CONFIG_PATH_ENV = "W2V_CONFIG"
SERIES_CONFIG_FILENAME = "series_config.yaml"
PRONUNCIATION_DICT_FILENAME = "pronunciation_dict.yaml"


class ConfigError(RuntimeError):
    """配置文件缺失、格式非法或字段校验失败。"""


class _StrictModel(BaseModel):
    """未知配置键直接报错，避免拼写错误被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


class WorkspaceConfig(_StrictModel):
    """工作区根目录（多部漫画在此之下各自隔离）。"""

    root: Path = Path("workspace")


class ApiConfig(_StrictModel):
    """百炼兼容模式接口参数。"""

    base_url: str = DEFAULT_API_BASE_URL
    api_key_env: str = DEFAULT_API_KEY_ENV
    timeout_s: float = 180.0
    max_retries: int = 3
    retry_backoff_s: float = 1.0
    #: Patch v3.4 Patch 16：MiniMax Speech 2.8 接口根地址（与百炼互不影响）。
    minimax_base_url: str = DEFAULT_MINIMAX_BASE_URL
    #: MiniMax API Key 的环境变量名（值写在进程环境或项目根 ``.env``）。
    minimax_api_key_env: str = DEFAULT_MINIMAX_API_KEY_ENV
    #: MiniMax GroupId 的环境变量名（仅老版本企业/分组账号需要）。
    minimax_group_id_env: str = DEFAULT_MINIMAX_GROUP_ID_ENV
    #: 直接写死 GroupId 的兜底位置（留空表示不传 GroupId）。
    minimax_group_id: str = ""
    #: Patch v4.3：MiniMax t2a_v2 每分钟请求上限（RPM）。超限会返回
    #: ``1002 rate limit exceeded(RPM)``，整条 beat 会缺音频 → 成片少句。
    #: 实测单账号约 17~30 次/分钟即触发，这里默认留出余量；填 0 = 不限流。
    minimax_rate_limit_rpm: int = 20
    #: Patch v4.3：MiniMax 同一 client 的在途请求上限（1 = 串行）。
    minimax_max_concurrency: int = 1


class ModelConfig(_StrictModel):
    """各阶段使用的模型名。"""

    #: Patch v4.22：主力识图模型升级为 qwen3.7-plus——原生多图 + 长上下文，
    #: Stage 3 可按 2~4 格批窗抽取，请求数约为逐格模式的 1/3~1/4。
    vision: str = "qwen3.7-plus"
    #: Patch v3.3 Patch 15：Step 4B 拆拍配图升级新一代均衡主力 qwen3.7-plus。
    script: str = "qwen3.7-plus"
    #: Patch v3.0-7：Step 4A（故事编剧）专用的「说书体」模型。
    #: 故事创作最吃文笔、逻辑与沉浸感，与 4B 的拆拍配图分开选型；
    #: 留空则回退到 ``script``（兼容单模型部署）。
    #: Patch v3.3 Patch 15：默认升级百炼新旗舰 qwen4-max；账号未开通时
    #: s4_scriptwriter 按 qwen3.8-max → qwen3.7-plus → qwen-plus 自动降级。
    story_model: str = "qwen4-max"
    #: TTS 模型名（Patch v3.4 Patch 16 支持三套 provider）：
    #:
    #: - ``speech-2.8-hd`` / ``speech-2.8-turbo`` / ``speech-2.6-*`` → MiniMax；
    #: - ``qwen3-tts-flash`` / ``qwen-audio-3.1-tts-plus`` → 百炼；
    #: - ``edge-tts`` → Edge TTS。
    tts: str = "qwen3-tts-flash"


class VideoConfig(_StrictModel):
    """成片画幅：v2.4 锁定仅输出 9:16 竖屏。"""

    aspect_ratio: Literal["9:16"] = "9:16"
    width: int = 1080
    height: int = 1920
    fps: int = 30

    @model_validator(mode="after")
    def _check_aspect_ratio(self) -> "VideoConfig":
        # 9:16 → width / height == 9 / 16 → width * 16 == height * 9
        if self.width * 16 != self.height * 9:
            raise ValueError(
                f"画幅必须是 9:16（width * 16 == height * 9），当前为 {self.width}x{self.height}"
            )
        return self


class RenderConfig(_StrictModel):
    """Stage 6/7 渲染参数。"""

    ffmpeg_bin: str = "ffmpeg"
    background_blur: int = 40
    background_darken: float = 0.55
    #: Patch v3.2 Patch 14：BGM 增益从 -18dB 提升到 -11.5dB。
    #: 旧值在 loudnorm 之前再叠加侧链闪避会跌到 -45dB 以下，实测几乎听不到。
    #: Patch v4.0 Patch 18：再抬到 -8.0dB——侧链参数放宽后 BGM 不再被压死。
    bgm_gain_db: float = -8.0
    #: Patch v3.1 Patch 11 B4：没有显式 BGM 时是否自动挂载默认氛围音乐。
    auto_bgm: bool = True
    #: Patch v3.1 Patch 11 B4：默认氛围音乐名（对应 ``assets/bgm/{name}.*``）。
    default_bgm: str = "suspense"
    #: Patch v3.1 Patch 11 B3/B4：是否把时间轴里的音效事件混进音轨。
    enable_sfx: bool = True
    #: Patch v3.2 Patch 14：音效增益从 -10dB 提升到 -3.5dB（混前再做 dynaudnorm 峰值归一）。
    #: Patch v4.0 Patch 18：再抬到 -2.0dB，让扇耳光 / 摔碎这类关键音效听得见。
    sfx_gain_db: float = -2.0
    #: Patch v4.41：**句内内联音效**（``tts_text`` 里的 ``[sfx:key]`` 标签）的增益档。
    #: 比整句级音效（``sfx_gain_db``）更轻——内联音效落在解说正文中间，
    #: 与人声同段共存，音量压过解说会让人听不清词，所以单独降一档。
    inline_sfx_gain_db: float = -6.0
    #: Patch v4.10：x264 编码预设（medium 比 veryfast 画质更好，编码略慢）。
    video_preset: str = "medium"
    #: Patch v4.10：``video_bitrate`` 为空时回落到 CRF 模式的取值。
    video_crf: int = 17
    #: Patch v4.10：目标码率。旧链路是 ``-preset veryfast -crf 20``，
    #: 1080×1920 实测只有 ~800kbps，字幕边缘发糊；改为 3~4Mbps 明显更清晰。
    video_bitrate: str | None = "3500k"
    #: Patch v4.10：码率上限（maxrate）与 VBV 缓冲区大小。
    video_maxrate: str | None = "5500k"
    video_bufsize: str | None = "7000k"


class VoiceSpec(_StrictModel):
    """一个音色的「音色 ID + 语速」组合（Patch v3.2 Patch 13 B1）。"""

    #: 百炼系统音色 ID（见 ``providers.tts_qwen.QWEN3_TTS_VOICES``，也可手填目录外官方 ID）。
    voice: str = "Elias"
    #: 语速倍率（1.0 = 原速）；ffmpeg ``atempo`` 的安全区间是 0.6 ~ 1.8。
    speed: float = Field(default=1.18, ge=0.6, le=1.8)


class SeriesTTSConfig(_StrictModel):
    """作品级配音设置（Patch v3.2 Patch 13 B1）。

    四级解析顺序（见 :func:`stages.s5_tts.resolve_beat_voice_spec`）：

    1. ``narrator``：旁白（``beat.speaker_role == "narrator"``）；
    2. ``character_voices``：已识别角色的专属音色（键是角色名）；
    3. ``default_male`` / ``default_female``：按性别兜底的通用音；
    4. 回退 ``narrator``。
    """

    narrator: VoiceSpec = Field(default_factory=lambda: VoiceSpec(voice="Elias", speed=1.18))
    default_male: VoiceSpec = Field(
        default_factory=lambda: VoiceSpec(voice="Ethan", speed=1.15)
    )
    default_female: VoiceSpec = Field(
        default_factory=lambda: VoiceSpec(voice="Cherry", speed=1.15)
    )
    #: 角色名 → 专属音色（键是 ``beat.speaker_role``，如「继母」）。
    character_voices: dict[str, VoiceSpec] = Field(default_factory=dict)


class TTSConfig(_StrictModel):
    """Stage 5 配音参数（Patch v3.1 Patch 11 A2）。"""

    #: 默认旁白音色（百炼系统音色 ID）。
    default_voice: str = "Elias"
    #: 角色 → 音色的映射表；键是 ``beat.speaker_role``（见 ``script_narrate.md``）。
    voice_cast: dict[str, str] = Field(
        default_factory=lambda: {
            "narrator": "Elias",
            "male_lead": "Ethan",
            "male_antagonist": "Ryan",
            "female_lead": "Cherry",
            "female_mature": "Katerina",
            "male": "Ethan",
            "female": "Cherry",
        }
    )
    #: Patch v3.2 Patch 13 B1：全局配音模板（作品级 ``SeriesConfig.tts`` 可逐项覆盖）。
    narrator: VoiceSpec = Field(default_factory=lambda: VoiceSpec(voice="Elias", speed=1.18))
    default_male: VoiceSpec = Field(
        default_factory=lambda: VoiceSpec(voice="Ethan", speed=1.15)
    )
    default_female: VoiceSpec = Field(
        default_factory=lambda: VoiceSpec(voice="Cherry", speed=1.15)
    )
    character_voices: dict[str, VoiceSpec] = Field(default_factory=dict)
    #: Patch v4.3：合成失败的 beat 额外**串行**补齐几轮（0 = 不补齐）。
    #: 限流场景下并发补齐只会更快撞限，串行补齐才能把缺的句子补回来；
    #: 补齐仍失败的 beat 用等长静音占位，保证时间轴与字幕不错位。
    retry_failed_rounds: int = 1
    #: Patch v4.3：合成（及补齐）仍失败的 beat 是否写入等长静音占位，
    #: 让时间轴与字幕不错位；问题仍会写进 05_audio/audio_issues.json。
    silence_placeholder_on_failure: bool = False


class ConcurrencyConfig(_StrictModel):
    """并发上限：跨作品并发、同作品 Stage 4 串行加锁。"""

    series: int = 8
    stage: int = 6


class AppConfig(_StrictModel):
    """后端全局配置。"""

    workspace: WorkspaceConfig = WorkspaceConfig()
    api: ApiConfig = ApiConfig()
    models: ModelConfig = ModelConfig()
    video: VideoConfig = VideoConfig()
    render: RenderConfig = RenderConfig()
    tts: TTSConfig = TTSConfig()
    concurrency: ConcurrencyConfig = ConcurrencyConfig()
    pronunciation_overrides: dict[str, str] = Field(default_factory=dict)

    def workspace_root(self) -> Path:
        """工作区根目录（绝对化，``~`` 展开）。"""
        return self.workspace.root.expanduser()

    def series_dir(self, series_id: str) -> Path:
        """单部漫画的隔离工作区目录。"""
        return self.workspace_root() / series_id


class CoverTextSpec(_StrictModel):
    """封页话次文字规格（Patch v4.5：作品级通用封页自动写字）。

    坐标采用相对 1080×1920 画布的归一化比例，``anchor="mm"`` 居中绘制，
    因此换任意分辨率底图都能落在同一视觉位置。
    """

    #: 是否在封页自动写话次文字。
    enabled: bool = True
    #: 文字锚点 X 归一化坐标（0.05 ~ 0.95）。
    x_ratio: float = Field(default=0.5, ge=0.05, le=0.95)
    #: 文字锚点 Y 归一化坐标（0.05 ~ 0.95）。
    y_ratio: float = Field(default=0.78, ge=0.05, le=0.95)
    #: 字号（像素，按 1080×1920 画布标定）。
    font_size: int = Field(default=72, ge=24, le=160)
    #: 文字填充色（HEX，非法值由渲染层兜底为白色）。
    color: str = "#FFFFFF"
    #: 描边色（HEX，非法值由渲染层兜底为黑色）。
    stroke_color: str = "#000000"
    #: 描边宽度（0 = 不描边）。
    stroke_width: int = Field(default=5, ge=0, le=16)
    #: 字体名称，复用 ``core.subtitle_render`` 的字体解析与回退。
    font_name: str = "Hiragino Sans GB"
    #: 文案模板，占位符：``{ep}``（受中文数字开关影响）、``{ep_no}``（恒阿拉伯数字）。
    template: str = "第{ep}话"
    #: True 时 ``{ep}`` 渲染为中文数字（第一话），否则阿拉伯数字（第1话）。
    use_chinese_numeral: bool = True


class SeriesBrandingConfig(_StrictModel):
    """作品级封页 / 双结尾图片包配置（Patch v4.5）。

    三张图片路径都相对 ``series_dir``；时长单位均为毫秒。
    旧版 ``series_config.yaml`` 不含本字段时，pydantic 自动填充整套默认值。
    """

    #: 通用封页底图（相对作品目录，如 ``assets/branding/cover.png``）。
    cover_image: str | None = None
    #: 「待续」结尾图。
    outro_continue_image: str | None = None
    #: 「完结」结尾图。
    outro_final_image: str | None = None
    #: 封页引导总时长（含淡入、停顿与叠化转场）。
    cover_duration_ms: int = Field(default=2000, ge=500, le=8000)
    #: 封页从黑场柔和淡入时长。
    cover_fade_in_ms: int = Field(default=350, ge=0, le=2000)
    #: 封页向正文第 1 帧叠化（``xfade=fade``）时长。
    cover_fade_out_ms: int = Field(default=450, ge=0, le=2000)
    #: 结尾图总停留时长（含浮现叠化与淡出）。
    outro_duration_ms: int = Field(default=2500, ge=500, le=10000)
    #: 结尾图在正文末帧之上缓缓叠化浮现时长。
    outro_fade_in_ms: int = Field(default=700, ge=0, le=2500)
    #: 结尾图与 BGM 同步淡出至黑场/静音时长。
    outro_fade_out_ms: int = Field(default=600, ge=0, le=2500)
    #: 作品默认结尾模式（单话可在 render_options 里覆盖）。
    default_outro_mode: Literal["none", "continue", "final"] = "continue"
    #: 封页话次文字规格。
    cover_text: CoverTextSpec = Field(default_factory=CoverTextSpec)


class SeriesConfig(_StrictModel):
    """作品级覆盖（``workspace/{series_id}/series_config.yaml``）。"""

    voice: str | None = None
    bgm: str | None = None
    model_overrides: dict[str, str] = Field(default_factory=dict)
    pronunciation_overrides: dict[str, str] = Field(default_factory=dict)
    #: Patch v3.1 Patch 11 A2：作品级角色音色覆盖（优先于全局 ``tts.voice_cast``）。
    voice_cast: dict[str, str] = Field(default_factory=dict)
    #: Patch v3.2 Patch 13 B1：作品级配音设置（讲解者/通用男女声/角色专属）。
    #: ``None`` 表示作品没配置过 → 整体沿用全局模板 ``AppConfig.tts``。
    tts: SeriesTTSConfig | None = None
    #: Patch v4.5：作品级通用封页写字 + 双结尾图 + 平滑转场片包。
    branding: SeriesBrandingConfig = Field(default_factory=SeriesBrandingConfig)
    #: Patch v4.16：作品级角色别名表（角色名 → 规范名或别名列表）。
    #:
    #: 例：``{"恩彗": ["恩慧", "恩惠"]}`` 或 ``{"恩惠": "恩彗"}``。
    #: Stage 4 产出的角色名写法不一时，用这张表把别名归并到同一个规范名，
    #: 避免同一角色在解说词、角色表与音色表里被拆成好几个人。
    character_aliases: dict[str, str | list[str]] = Field(default_factory=dict)


def _package_root() -> Path:
    """backend 根目录（``backend/src/core/config.py`` → ``backend/``）。"""
    return Path(__file__).resolve().parents[2]


def assets_root() -> Path:
    """零版权素材目录（``backend/assets/``）。

    Patch v3.1 Patch 11 B：Stage 7 的默认 BGM 与音效都从这里找，
    ``backend/scripts/init_audio_assets.py`` 也往这里生成素材。
    """
    return _package_root() / "assets"


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    """读取 YAML 文件并要求根节点为映射。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - 依赖具体 YAML 报错文本
        raise ConfigError(f"YAML 解析失败（{path}）：{exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"配置文件根节点必须是映射（mapping），实际为 {type(raw).__name__}：{path}"
        )
    return raw


def _is_within(root: Path, target: Path) -> bool:
    """``target`` 是否在 ``root`` 之内（含相等）。"""
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


def find_config_path(explicit: str | Path | None = None) -> Path | None:
    """按优先级定位全局配置文件；找不到返回 ``None``（使用内置默认值）。

    优先级：显式路径 → ``W2V_CONFIG`` 环境变量 → 当前目录 → 包根目录。

    Patch v3.4 Patch 16：包根兜底只在**当前目录与包根同属一个项目目录树**时
    生效（cwd 在包根内，或 cwd 是包根的上级目录）。这样「从项目里任何位置
    运行都能读到仓库配置」依旧成立，而从无关目录（测试临时目录等）运行不会
    误读仓库里真实部署的 ``config.yaml``，默认值断言保持稳定。
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"配置文件不存在：{path}")
        return path

    env_path = os.environ.get(CONFIG_PATH_ENV)
    if env_path:
        path = Path(env_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"环境变量 {CONFIG_PATH_ENV} 指向的配置文件不存在：{path}")
        return path

    cwd = Path.cwd()
    package_root = _package_root()
    candidates = [cwd / CONFIG_FILENAME]
    if _is_within(package_root, cwd) or _is_within(cwd, package_root):
        candidates.append(package_root / CONFIG_FILENAME)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_config(path: str | Path | None = None) -> AppConfig:
    """加载全局配置；未提供任何配置文件时返回内置默认值。"""
    resolved = find_config_path(path)
    if resolved is None:
        return AppConfig()
    data = _load_yaml_mapping(resolved)
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"配置文件校验失败（{resolved}）：{exc}") from exc


def load_pronunciation_dict(path: str | Path) -> dict[str, str]:
    """加载多音字/同音字纠偏词典。

    文件支持两种写法：顶层 ``overrides:`` 映射，或直接是「词 → 读音」的扁平映射。
    词典仅可写入 ``tts_text`` / ``spoken_text``，严禁改写 ``narration_text``（字幕文本）。
    """
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise ConfigError(f"发音词典文件不存在：{resolved}")
    data = _load_yaml_mapping(resolved)
    overrides = data.get("overrides", data)
    if not isinstance(overrides, dict):
        raise ConfigError(f"发音词典 overrides 必须是映射：{resolved}")
    result: dict[str, str] = {}
    for key, value in overrides.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ConfigError(f"发音词典的键与值必须均为字符串：{resolved}")
        result[key] = value
    return result


def load_series_config(series_dir: str | Path) -> SeriesConfig:
    """加载作品级覆盖配置；文件不存在时返回空覆盖。"""
    path = Path(series_dir).expanduser() / SERIES_CONFIG_FILENAME
    if not path.is_file():
        return SeriesConfig()
    data = _load_yaml_mapping(path)
    try:
        return SeriesConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"作品级配置校验失败（{path}）：{exc}") from exc


def merge_pronunciation_overrides(
    global_overrides: Mapping[str, str],
    series_overrides: Mapping[str, str],
) -> dict[str, str]:
    """合并全局与作品级发音词典，作品级优先。"""
    merged = dict(global_overrides)
    merged.update(series_overrides)
    return merged


def merge_voice_cast(
    global_cast: Mapping[str, str],
    series_cast: Mapping[str, str],
) -> dict[str, str]:
    """合并全局与作品级角色音色表（Patch v3.1 Patch 11 A2/A3）。

    作品级优先：同一 ``speaker_role`` 在作品级出现时覆盖全局值。
    """
    merged = dict(global_cast)
    merged.update(series_cast)
    return merged


def merge_series_tts(
    global_tts: TTSConfig,
    series_tts: SeriesTTSConfig | None,
) -> SeriesTTSConfig:
    """合并全局配音模板与作品级配音设置（Patch v3.2 Patch 13 B1）。

    作品级优先：``narrator`` / ``default_male`` / ``default_female`` 直接采用
    作品级取值；``character_voices`` 逐键合并（同名角色以作品级为准）。
    作品级为 ``None``（没配过）时原样返回全局模板。
    """
    if series_tts is None:
        return SeriesTTSConfig(
            narrator=global_tts.narrator,
            default_male=global_tts.default_male,
            default_female=global_tts.default_female,
            character_voices=dict(global_tts.character_voices),
        )
    character_voices = dict(global_tts.character_voices)
    character_voices.update(series_tts.character_voices)
    return SeriesTTSConfig(
        narrator=series_tts.narrator,
        default_male=series_tts.default_male,
        default_female=series_tts.default_female,
        character_voices=character_voices,
    )


def resolve_api_key(
    config: AppConfig | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """读取百炼 API Key；缺失时抛出明确错误，避免静默失败。"""
    env_name = config.api.api_key_env if config is not None else DEFAULT_API_KEY_ENV
    source: Mapping[str, str] = env if env is not None else os.environ
    value = (source.get(env_name) or "").strip()
    if not value:
        raise ConfigError(f"未配置环境变量 {env_name}，无法调用百炼接口")
    return value


def has_api_key(config: AppConfig | None = None) -> bool:
    """检查 API Key 是否已配置（不抛异常）。"""
    env_name = config.api.api_key_env if config is not None else DEFAULT_API_KEY_ENV
    return bool((os.environ.get(env_name) or "").strip())


# ----------------------------------------------------------------------
# 项目根 .env：百炼 API Key 的第二读取来源
# ----------------------------------------------------------------------
ENV_FILENAME = ".env"


def project_root() -> Path:
    """项目根目录（``backend/`` 的上一级：存放 ``.env`` 与 ``workspace/``）。"""
    return _package_root().parent


def read_env_file(path: str | Path) -> dict[str, str]:
    """解析 ``.env`` 文件（``KEY=VALUE``，忽略空行与 ``#`` 注释，支持引号包裹）。

    文件不存在时返回空映射（不抛异常，便于「有没有 .env 都��能跑」）。
    """
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in resolved.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env_file(
    path: str | Path | None = None,
    *,
    override: bool = False,
) -> dict[str, str]:
    """把 ``.env`` 载入进程环境变量；已存在的键默认不覆盖。

    返回解析出的键值映射（便于调用方检查）。
    """
    target = Path(path).expanduser() if path is not None else project_root() / ENV_FILENAME
    values = read_env_file(target)
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values


def resolve_api_key_with_env_file(
    config: AppConfig | None = None,
    *,
    env: Mapping[str, str] | None = None,
    env_file: str | Path | None = None,
) -> str:
    """读取百炼 API Key：先进程环境变量，再项目根 ``.env`` 文件。

    铁律：所有百炼调用统一读取环境变量 ``DASHSCOPE_API_KEY``；
    ``.env`` 只是「让环境变量有值」的引导文件，不构成第二套配置体系。
    """
    env_name = config.api.api_key_env if config is not None else DEFAULT_API_KEY_ENV
    source: Mapping[str, str] = env if env is not None else os.environ
    value = (source.get(env_name) or "").strip()
    if value:
        return value
    target = Path(env_file).expanduser() if env_file is not None else project_root() / ENV_FILENAME
    value = (read_env_file(target).get(env_name) or "").strip()
    if value:
        return value
    raise ConfigError(f"未配置环境变量 {env_name}（{target} 中也没有），无法调用百炼接口")


# ---------------------------------------------------------------------------
# MiniMax Speech 2.8：凭据读取 + 统一 TTS 工厂 + 跨模型音色兜底
# ---------------------------------------------------------------------------


def resolve_minimax_api_key_with_env_file(
    config: AppConfig | None = None,
    env: Mapping[str, str] | None = None,
    env_file: str | Path | None = None,
    *,
    env_name: str | None = None,
) -> str:
    """读取 MiniMax API Key：先进程环境变量，再项目根 ``.env`` 文件。

    与百炼的 ``DASHSCOPE_API_KEY`` 完全独立（两套 provider 可以共存）；
    缺失时抛 :class:`ConfigError`，避免静默失败。
    """
    name = env_name or (
        config.api.minimax_api_key_env if config is not None else DEFAULT_MINIMAX_API_KEY_ENV
    )
    source: Mapping[str, str] = env if env is not None else os.environ
    value = (source.get(name) or "").strip()
    if value:
        return value
    target = Path(env_file).expanduser() if env_file is not None else project_root() / ENV_FILENAME
    value = (read_env_file(target).get(name) or "").strip()
    if value:
        return value
    raise ConfigError(f"未配置环境变量 {name}（{target} 中也没有），无法调用 MiniMax 接口")


def resolve_minimax_group_id(
    config: AppConfig | None = None,
    env: Mapping[str, str] | None = None,
    env_file: str | Path | None = None,
    *,
    env_name: str | None = None,
) -> str:
    """读取 MiniMax GroupId：进程环境变量 → ``.env`` → 配置内明文。

    新版 Platform API Key 不需要 GroupId，因此**缺失时返回空串**（不抛错），
    调用方据此决定要不要往 URL 上拼 ``?GroupId=``。
    """
    name = env_name or (
        config.api.minimax_group_id_env if config is not None else DEFAULT_MINIMAX_GROUP_ID_ENV
    )
    source: Mapping[str, str] = env if env is not None else os.environ
    value = (source.get(name) or "").strip()
    if value:
        return value
    target = Path(env_file).expanduser() if env_file is not None else project_root() / ENV_FILENAME
    value = (read_env_file(target).get(name) or "").strip()
    if value:
        return value
    if config is not None:
        return (config.api.minimax_group_id or "").strip()
    return ""


#: 各 provider 的推荐音色（兜底用；与 provider 内置默认值保持一致）。
_QWEN_DEFAULT_VOICE = "Elias"
_EDGE_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
#: MiniMax 默认旁白音色（真实存在于 MiniMax system_voice 清单）。
_MINIMAX_NARRATOR_VOICE = "Chinese (Mandarin)_Radio_Host"
_MINIMAX_MALE_VOICE = "male-qn-jingying"
_MINIMAX_FEMALE_VOICE = "female-yujie"

#: Edge TTS 音色的前缀（``zh-CN-XiaoxiaoNeural`` 之类）。
_EDGE_VOICE_PREFIXES = ("zh-CN-", "zh-TW-", "zh-HK-", "zh-MO-")


def is_minimax_tts_model(tts_model: str) -> bool:
    """判断 TTS 模型名是否走 MiniMax（``speech-2.8-hd`` / ``speech-2.6-turbo`` …）。"""
    return (tts_model or "").strip().startswith("speech-")


def recommended_voice_for_tts_model(
    tts_model: str,
    role_kind: Literal["narrator", "male", "female"] = "narrator",
) -> str:
    """当前 TTS 模型下推荐使用的音色 ID。"""
    model = (tts_model or "").strip()
    if is_minimax_tts_model(model):
        if role_kind == "male":
            return _MINIMAX_MALE_VOICE
        if role_kind == "female":
            return _MINIMAX_FEMALE_VOICE
        return _MINIMAX_NARRATOR_VOICE
    if model == "edge-tts":
        return _EDGE_DEFAULT_VOICE
    return _QWEN_DEFAULT_VOICE


def resolve_voice_for_tts_model(
    voice: str | None,
    *,
    tts_model: str,
    role_kind: Literal["narrator", "male", "female"] = "narrator",
) -> str:
    """跨供应商音色防踩坑兜底（Patch v3.4 Patch 16）。

    背景：用户把 ``models.tts`` 从 ``qwen3-tts-flash`` 改成 ``speech-2.8-hd`` 之后，
    作品目录 ``series_config.yaml`` 或全局配置里往往还留着百炼/Edge 的音色
    （``Cherry``、``Elias``、``Ethan``、``Serena``、``Eldric Sage``、``zh-CN-*``）。
    这些 ID 发给 MiniMax 会返回 ``2013 invalid voice_id`` 并让**整话**失败，
    因此这里统一做平滑替换：

    * 当前模型是 MiniMax 而音色属于 Qwen/Edge → WARNING + 换成对应的 MiniMax 音色
      （旁白 → ``Chinese (Mandarin)_Radio_Host``，男配 → ``male-qn-jingying``，
      女配 → ``female-yujie``）；
    * 反向（当前模型是百炼/Edge 而音色来自 MiniMax 目录）→ 同样平滑替换；
    * 自定义克隆 ID（如 ``my_cloned_voice_01``，不在任何已知列表里）→ 原样透传。
    """
    model = (tts_model or "").strip()
    chosen = (voice or "").strip()
    if not chosen:
        return recommended_voice_for_tts_model(model, role_kind)

    # 延迟导入：providers 反向依赖 core.config，顶层 import 会成环。
    from providers.tts_minimax import MINIMAX_VOICE_IDS
    from providers.tts_qwen import QWEN3_VOICE_IDS

    minimax_ids = set(MINIMAX_VOICE_IDS)
    qwen_ids = set(QWEN3_VOICE_IDS)
    is_edge_voice = chosen.startswith(_EDGE_VOICE_PREFIXES)

    if is_minimax_tts_model(model):
        if chosen in minimax_ids:
            return chosen
        if chosen in qwen_ids or is_edge_voice:
            replacement = recommended_voice_for_tts_model(model, role_kind)
            logger.warning(
                "[tts] 当前模型 %s 不认识音色 %s（百炼/Edge 音色），已替换为 %s",
                model,
                chosen,
                replacement,
            )
            return replacement
        # 自定义克隆音色：不认识就原样透传，让服务端自己判断。
        return chosen

    if chosen in minimax_ids:
        replacement = recommended_voice_for_tts_model(model, role_kind)
        logger.warning(
            "[tts] 当前模型 %s 不认识音色 %s（MiniMax 音色），已替换为 %s",
            model,
            chosen,
            replacement,
        )
        return replacement
    return chosen


def create_tts_client(config: AppConfig, *, voice: str | None = None) -> Any:
    """按 ``config.models.tts`` 路由到对应 TTS 客户端（Patch v3.4 Patch 16）。

    - ``speech-*`` → :class:`providers.tts_minimax.MiniMaxTTS`；
    - ``edge-tts`` → :class:`providers.tts_edge.EdgeTTS`；
    - 其余（``qwen*``）→ :class:`providers.tts_qwen.QwenTTS`。

    默认音色取 ``voice`` → ``config.tts.default_voice`` → provider 内置默认，
    并统一过一遍 :func:`resolve_voice_for_tts_model`（跨模型切换不再报
    ``2013 invalid voice_id``）。
    """
    model = (config.models.tts or "").strip()
    requested = voice or config.tts.default_voice or ""
    chosen = resolve_voice_for_tts_model(requested, tts_model=model, role_kind="narrator")

    if is_minimax_tts_model(model):
        from providers.tts_minimax import MiniMaxTTS

        try:
            api_key = resolve_minimax_api_key_with_env_file(config)
        except ConfigError:
            # 与百炼一致：不在构造期炸，延迟到真正合成时再报错。
            api_key = None
        return MiniMaxTTS(
            api_key=api_key,
            base_url=config.api.minimax_base_url,
            group_id=resolve_minimax_group_id(config),
            model=model,
            voice=chosen,
            timeout_s=config.api.timeout_s,
            max_retries=config.api.max_retries,
            retry_backoff_s=config.api.retry_backoff_s,
            # Patch v4.3：MiniMax 有 RPM 限流，交给 provider 内的节流闸门。
            rate_limit_rpm=config.api.minimax_rate_limit_rpm,
            max_concurrency=config.api.minimax_max_concurrency,
            api_key_env=config.api.minimax_api_key_env,
            group_id_env=config.api.minimax_group_id_env,
        )

    if model == "edge-tts":
        from providers.tts_edge import EdgeTTS

        return EdgeTTS(voice=chosen)

    from providers.tts_qwen import QwenTTS

    return QwenTTS(
        base_url=config.api.base_url,
        model=model,
        voice=chosen,
        timeout_s=config.api.timeout_s,
        max_retries=config.api.max_retries,
        retry_backoff_s=config.api.retry_backoff_s,
    )

