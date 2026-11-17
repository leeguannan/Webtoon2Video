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

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_API_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_API_KEY_ENV = "DASHSCOPE_API_KEY"
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


class ModelConfig(_StrictModel):
    """各阶段使用的模型名。"""

    vision: str = "qwen3-vl-plus"
    script: str = "qwen-plus"
    #: Patch v3.0-7：Step 4A（故事编剧）专用的「说书体」模型。
    #: 故事创作最吃文笔、逻辑与沉浸感，与 4B 的拆拍配图分开选型；
    #: 留空则回退到 ``script``（兼容单模型部署）。
    story_model: str = "qwen3.8-max"
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
    bgm_gain_db: float = -18.0
    #: Patch v3.1 Patch 11 B4：没有显式 BGM 时是否自动挂载默认氛围音乐。
    auto_bgm: bool = True
    #: Patch v3.1 Patch 11 B4：默认氛围音乐名（对应 ``assets/bgm/{name}.*``）。
    default_bgm: str = "suspense"
    #: Patch v3.1 Patch 11 B3/B4：是否把时间轴里的音效事件混进音轨。
    enable_sfx: bool = True
    #: Patch v3.1 Patch 11 B4：音效相对人声的增益（dB）。
    sfx_gain_db: float = -10.0


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


class SeriesConfig(_StrictModel):
    """作品级覆盖（``workspace/{series_id}/series_config.yaml``）。"""

    voice: str | None = None
    bgm: str | None = None
    model_overrides: dict[str, str] = Field(default_factory=dict)
    pronunciation_overrides: dict[str, str] = Field(default_factory=dict)
    #: Patch v3.1 Patch 11 A2：作品级角色音色覆盖（优先于全局 ``tts.voice_cast``）。
    voice_cast: dict[str, str] = Field(default_factory=dict)


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


def find_config_path(explicit: str | Path | None = None) -> Path | None:
    """按优先级定位全局配置文件；找不到返回 ``None``（使用内置默认值）。"""
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

    for candidate in (Path.cwd() / CONFIG_FILENAME, _package_root() / CONFIG_FILENAME):
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
