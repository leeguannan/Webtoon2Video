"""``core.config`` 配置加载测试（规范 M1-T1）。

覆盖：全局配置读取与校验、配置文件定位优先级、作品级覆盖、发音词典、API Key 解析。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import (
    AppConfig,
    ConfigError,
    SeriesConfig,
    VideoConfig,
    find_config_path,
    has_api_key,
    load_config,
    load_pronunciation_dict,
    load_series_config,
    merge_pronunciation_overrides,
    resolve_api_key,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把工作目录挪到临时目录，避免误读到仓库里的真实 ``config.yaml``。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("W2V_CONFIG", raising=False)


# --------------------------------------------------------------------------
# 默认值
# --------------------------------------------------------------------------
def test_default_config_values() -> None:
    config = load_config()
    assert config.api.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert config.api.api_key_env == "DASHSCOPE_API_KEY"
    assert config.models.vision == "qwen3-vl-plus"
    assert config.models.script == "qwen-plus"
    assert config.models.tts == "qwen3-tts-flash"
    assert config.video.aspect_ratio == "9:16"
    assert (config.video.width, config.video.height, config.video.fps) == (1080, 1920, 30)
    assert config.concurrency.series == 8
    assert config.concurrency.stage == 6
    assert config.pronunciation_overrides == {}


def test_workspace_and_series_paths(tmp_path: Path) -> None:
    config = AppConfig()
    config.workspace.root = tmp_path / "workspace"
    assert config.workspace_root() == tmp_path / "workspace"
    assert config.series_dir("wt_1") == tmp_path / "workspace" / "wt_1"


# --------------------------------------------------------------------------
# 文件读取
# --------------------------------------------------------------------------
def test_load_config_from_file(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "workspace:\n"
        "  root: ./ws\n"
        "video:\n"
        "  aspect_ratio: '9:16'\n"
        "  width: 1080\n"
        "  height: 1920\n"
        "models:\n"
        "  vision: qwen3-vl-max\n"
        "pronunciation_overrides:\n"
        "  银行: 'yín háng'\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.workspace.root == Path("./ws")
    assert config.models.vision == "qwen3-vl-max"
    assert config.pronunciation_overrides == {"银行": "yín háng"}


def test_unknown_config_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("video:\n  aspect_ration: '9:16'\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_non_9x16_video_config_is_rejected() -> None:
    with pytest.raises(ValueError):
        VideoConfig(aspect_ratio="9:16", width=1920, height=1080)
    with pytest.raises(ValueError):
        VideoConfig(aspect_ratio="16:9")  # type: ignore[arg-type]


def test_config_path_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text("models:\n  vision: from-explicit\n", encoding="utf-8")
    env_path = tmp_path / "env.yaml"
    env_path.write_text("models:\n  vision: from-env\n", encoding="utf-8")

    assert find_config_path(explicit) == explicit

    monkeypatch.setenv("W2V_CONFIG", str(env_path))
    assert find_config_path() == env_path
    assert find_config_path(explicit) == explicit


def test_missing_explicit_config_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        find_config_path(tmp_path / "nope.yaml")


def test_config_path_env_pointing_to_missing_file_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("W2V_CONFIG", str(tmp_path / "nope.yaml"))
    with pytest.raises(ConfigError):
        find_config_path()


# --------------------------------------------------------------------------
# 作品级覆盖与发音词典
# --------------------------------------------------------------------------
def test_load_series_config(tmp_path: Path) -> None:
    assert load_series_config(tmp_path) == SeriesConfig()

    (tmp_path / "series_config.yaml").write_text(
        "voice: cherry\nbgm: theme.mp3\npronunciation_overrides:\n  重霄: 'chóng xiāo'\n",
        encoding="utf-8",
    )
    series = load_series_config(tmp_path)
    assert series.voice == "cherry"
    assert series.bgm == "theme.mp3"
    assert series.pronunciation_overrides == {"重霄": "chóng xiāo"}


def test_load_pronunciation_dict_supports_both_layouts(tmp_path: Path) -> None:
    nested = tmp_path / "nested.yaml"
    nested.write_text("overrides:\n  银行: 'yín háng'\n", encoding="utf-8")
    assert load_pronunciation_dict(nested) == {"银行": "yín háng"}

    flat = tmp_path / "flat.yaml"
    flat.write_text("银行: 'yín háng'\n重霄: 'chóng xiāo'\n", encoding="utf-8")
    assert load_pronunciation_dict(flat) == {"银行": "yín háng", "重霄": "chóng xiāo"}


def test_missing_pronunciation_dict_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_pronunciation_dict(tmp_path / "nope.yaml")


def test_series_overrides_take_precedence() -> None:
    merged = merge_pronunciation_overrides(
        {"银行": "yín háng", "重霄": "zhòng xiāo"},
        {"重霄": "chóng xiāo"},
    )
    assert merged == {"银行": "yín háng", "重霄": "chóng xiāo"}


# --------------------------------------------------------------------------
# API Key
# --------------------------------------------------------------------------
def test_resolve_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "  sk-test  ")
    assert resolve_api_key() == "sk-test"
    assert has_api_key() is True


def test_resolve_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        resolve_api_key()
    assert has_api_key() is False


def test_resolve_api_key_uses_configured_env_name(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AppConfig()
    config.api.api_key_env = "CUSTOM_KEY"
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("CUSTOM_KEY", "sk-custom")
    assert resolve_api_key(config) == "sk-custom"


# --------------------------------------------------------------------------
# 随仓库交付的示例文件必须始终可用
# --------------------------------------------------------------------------
def test_example_files_are_valid() -> None:
    example = BACKEND_ROOT / "config.example.yaml"
    assert example.is_file()
    config = load_config(example)
    assert config.video.aspect_ratio == "9:16"

    dict_example = BACKEND_ROOT / "pronunciation_dict.example.yaml"
    assert dict_example.is_file()
    overrides = load_pronunciation_dict(dict_example)
    assert overrides
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in overrides.items())
