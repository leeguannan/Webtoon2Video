"""Patch v4.25：分镜缩略图接口 + 角色档案同步闭环 + 只读检查去重与繁简比对。

背景（用户实测）：
* 一话 200~300 个分镜时，前端把 690px 宽的原图整张拉进列表，页面卡死；
* 人工保存时若想把「南道振」登记成规范名 / 把「丈夫」记为合法别名，
  后端此前没有任何入口，只能改 story_bible.json 手改；
* 繁简体说话人（作品角色表存简体、用户写繁体）会被误报成「未登记的角色」。

本文件锁死 v4.25 的新契约：

* ``GET /api/episodes/{id}/panels/{file}?w=<像素>`` 返回等比缩小的 JPEG 缩略图，
  缓存到 ``02_panels/.thumbs/``，原图 mtime 变化后缓存必须失效；
* ``PUT /api/episodes/{id}/script`` 支持 ``sync_character_updates``：
  ``add_alias`` / ``promote_canonical`` / ``register_character``，
  **默认不传就绝不动档案**；
* ``inspect_script_beats`` 对「送音文本 == 字幕原文」不重复告警，
  说话人比对前先转简体。

全部离线：不触网、不依赖 ffmpeg。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Sequence

import yaml
from fastapi.testclient import TestClient
from PIL import Image

from core.character_unify import build_character_canonicalizer
from core.config import AppConfig, WorkspaceConfig
from core.schemas import (
    CharacterProfile,
    EpisodeDigest,
    NarrationScript,
    ScriptBeat,
    StoryBibleV2,
    load_model,
    save_model,
)
from stages import s0_catalog
from web.job_runner import JobRunner
from web.server import create_app

SERIES_ID = "wt_v425"


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------
def _register(root: Path) -> tuple[str, Path, AppConfig]:
    src = root / "src"
    ep_src = src / "ep_001"
    ep_src.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 600), (40, 80, 120)).save(ep_src / "001.png")
    catalog = s0_catalog.import_local_catalog(src)
    s0_catalog.CatalogRegistry(root).register(catalog)
    config = AppConfig(workspace=WorkspaceConfig(root=root))
    return catalog.series_id, root / catalog.series_id, config


def _character(name: str, *, aliases: Sequence[str] = ()) -> CharacterProfile:
    return CharacterProfile(
        name=name,
        aliases=list(aliases),
        visual_traits="",
        current_status="",
        first_seen_ep=1,
        last_seen_ep=1,
    )


def _beat(beat_id: str, narration_text: str, **kwargs: Any) -> ScriptBeat:
    payload: dict[str, Any] = {
        "beat_id": beat_id,
        "narration_text": narration_text,
        "target_panel_ids": ["p_001"],
    }
    payload.update(kwargs)
    return ScriptBeat(**payload)


def _write_script(series_dir: Path, beats: Sequence[ScriptBeat]) -> Path:
    script_dir = series_dir / "episodes" / "ep_001" / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    script = NarrationScript(
        episode_id=f"{series_dir.name}_ep_001",
        hook_title="雨夜来客",
        beats=list(beats),
        episode_digest=EpisodeDigest(
            episode_no=1,
            one_line_hook="雨夜有人来访",
            summary="来客自称银行行长。",
        ),
    )
    save_model(script, script_dir / "script.json")
    return script_dir / "script.json"


def _write_bible(series_dir: Path, series_id: str) -> Path:
    path = series_dir / "story_bible.json"
    save_model(
        StoryBibleV2(
            series_id=series_id,
            series_title="测试漫画",
            characters=[
                _character("恩彗"),
                _character("道振", aliases=["丈夫", "南道振"]),
                _character("云飞"),
            ],
        ),
        path,
    )
    return path


def _write_series_config(series_dir: Path, voices: dict[str, Any]) -> Path:
    path = series_dir / "series_config.yaml"
    path.write_text(
        yaml.safe_dump({"tts": {"character_voices": voices}}, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def _api(config: AppConfig) -> TestClient:
    runner = JobRunner(config, tts_factory=lambda: None)
    return TestClient(create_app(config, runner=runner))


def _panel_path(series_dir: Path, name: str = "p_001.png") -> Path:
    panels_dir = series_dir / "episodes" / "ep_001" / "02_panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    path = panels_dir / name
    Image.new("RGB", (690, 1200), (30, 90, 160)).save(path)
    return path


def _bible_characters(series_dir: Path) -> list[CharacterProfile]:
    return list(load_model(StoryBibleV2, series_dir / "story_bible.json").characters)


# ---------------------------------------------------------------------------
# A. 缩略图接口
# ---------------------------------------------------------------------------
def test_panel_endpoint_returns_original_without_width(tmp_path) -> None:
    """不带 ``w`` 时必须原样回源原图（内容与字节数都不变）。"""
    series_id, series_dir, config = _register(tmp_path)
    panel = _panel_path(series_dir)
    client = _api(config)

    response = client.get(f"/api/episodes/{series_id}_ep_001/panels/p_001.png")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert len(response.content) == panel.stat().st_size


def test_panel_endpoint_generates_and_caches_thumbnail(tmp_path) -> None:
    """带 ``w`` 时返回等比缩小的 JPEG 缩略图，并落缓存。"""
    series_id, series_dir, config = _register(tmp_path)
    _panel_path(series_dir)
    client = _api(config)

    response = client.get(f"/api/episodes/{series_id}_ep_001/panels/p_001.png?w=180")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert "max-age=86400" in response.headers.get("cache-control", "")

    cached = series_dir / "episodes" / "ep_001" / "02_panels" / ".thumbs" / "p_001_w180.jpg"
    assert cached.is_file()
    with Image.open(cached) as image:
        assert image.width == 180
        # 690x1200 等比缩到 180 宽 → 高度约 313
        assert 300 <= image.height <= 330


def test_panel_thumbnail_cache_is_invalidated_when_source_changes(tmp_path) -> None:
    """原图 mtime 变化后缩略图必须重建（否则预览图与分镜对不上）。"""
    series_id, series_dir, config = _register(tmp_path)
    panel = _panel_path(series_dir)
    client = _api(config)

    first = client.get(f"/api/episodes/{series_id}_ep_001/panels/p_001.png?w=180")
    cached = series_dir / "episodes" / "ep_001" / "02_panels" / ".thumbs" / "p_001_w180.jpg"
    assert first.status_code == 200
    first_mtime = cached.stat().st_mtime

    # 换一张内容不同的原图，并把 mtime 推到更晚
    Image.new("RGB", (690, 1200), (200, 40, 40)).save(panel)
    future = time.time() + 10
    os.utime(panel, (future, future))

    second = client.get(f"/api/episodes/{series_id}_ep_001/panels/p_001.png?w=180")
    assert second.status_code == 200
    assert cached.stat().st_mtime > first_mtime


def test_panel_thumbnail_rejects_out_of_range_width(tmp_path) -> None:
    """宽度越界（<64 或 >800）必须被拒。"""
    series_id, series_dir, config = _register(tmp_path)
    _panel_path(series_dir)
    client = _api(config)

    assert (
        client.get(f"/api/episodes/{series_id}_ep_001/panels/p_001.png?w=32").status_code
        == 422
    )
    assert (
        client.get(
            f"/api/episodes/{series_id}_ep_001/panels/p_001.png?w=2000"
        ).status_code
        == 422
    )


# ---------------------------------------------------------------------------
# B. 角色档案同步
# ---------------------------------------------------------------------------
def test_put_script_without_sync_updates_keeps_bible_untouched(tmp_path) -> None:
    """默认不传 sync_character_updates 时绝不能动角色档案。"""
    series_id, series_dir, config = _register(tmp_path)
    bible_path = _write_bible(series_dir, series_id)
    _write_script(series_dir, [_beat("b1", "道振走进病房。", speaker_role="道振")])
    before = bible_path.read_bytes()
    client = _api(config)

    response = client.put(
        f"/api/episodes/{series_id}_ep_001/script",
        json={
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "南道振走进病房。",
                    "target_panel_ids": ["p_001"],
                    "speaker_role": "南道振",
                }
            ]
        },
    )

    assert response.status_code == 200
    assert bible_path.read_bytes() == before
    assert [c.name for c in _bible_characters(series_dir)] == ["恩彗", "道振", "云飞"]


def test_put_script_sync_add_alias(tmp_path) -> None:
    """add_alias：把 found_name 加进对应角色的 aliases（去重）。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(series_dir, series_id)
    _write_script(series_dir, [_beat("b1", "南道振走进病房。", speaker_role="南道振")])
    client = _api(config)

    response = client.put(
        f"/api/episodes/{series_id}_ep_001/script",
        json={
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "南道振走进病房。",
                    "target_panel_ids": ["p_001"],
                    "speaker_role": "南道振",
                }
            ],
            "sync_character_updates": [
                {
                    "action": "add_alias",
                    "found_name": "南道振",
                    "target_canonical": "道振",
                }
            ],
        },
    )

    assert response.status_code == 200
    characters = {c.name: c for c in _bible_characters(series_dir)}
    assert "南道振" in characters["道振"].aliases

    # 再同步一次不会重复添加
    client.put(
        f"/api/episodes/{series_id}_ep_001/script",
        json={
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "南道振走进病房。",
                    "target_panel_ids": ["p_001"],
                    "speaker_role": "南道振",
                }
            ],
            "sync_character_updates": [
                {
                    "action": "add_alias",
                    "found_name": "南道振",
                    "target_canonical": "道振",
                }
            ],
        },
    )
    characters = {c.name: c for c in _bible_characters(series_dir)}
    assert characters["道振"].aliases.count("南道振") == 1


def test_put_script_sync_promote_canonical_migrates_voice_key(tmp_path) -> None:
    """promote_canonical：角色改名 + 原名进 aliases + 专属音色键迁移。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(series_dir, series_id)
    _write_series_config(series_dir, {"道振": {"voice": "Elias", "speed": 1.2}})
    _write_script(series_dir, [_beat("b1", "南道振走进病房。", speaker_role="南道振")])
    client = _api(config)

    response = client.put(
        f"/api/episodes/{series_id}_ep_001/script",
        json={
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "南道振走进病房。",
                    "target_panel_ids": ["p_001"],
                    "speaker_role": "南道振",
                }
            ],
            "sync_character_updates": [
                {
                    "action": "promote_canonical",
                    "found_name": "南道振",
                    "target_canonical": "道振",
                }
            ],
        },
    )

    assert response.status_code == 200
    characters = {c.name: c for c in _bible_characters(series_dir)}
    assert "南道振" in characters
    assert "道振" in characters["南道振"].aliases

    config_data = yaml.safe_load(
        (series_dir / "series_config.yaml").read_text(encoding="utf-8")
    )
    voices = config_data["tts"]["character_voices"]
    assert "南道振" in voices
    assert "道振" not in voices
    assert voices["南道振"]["voice"] == "Elias"


def test_put_script_sync_promote_canonical_keeps_existing_voice_key(tmp_path) -> None:
    """新键已存在时不覆盖（用户手工配过的音色优先）。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(series_dir, series_id)
    _write_series_config(
        series_dir,
        {
            "道振": {"voice": "Elias", "speed": 1.2},
            "南道振": {"voice": "Katerina", "speed": 1.0},
        },
    )
    _write_script(series_dir, [_beat("b1", "南道振走进病房。", speaker_role="南道振")])
    client = _api(config)

    response = client.put(
        f"/api/episodes/{series_id}_ep_001/script",
        json={
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "南道振走进病房。",
                    "target_panel_ids": ["p_001"],
                    "speaker_role": "南道振",
                }
            ],
            "sync_character_updates": [
                {
                    "action": "promote_canonical",
                    "found_name": "南道振",
                    "target_canonical": "道振",
                }
            ],
        },
    )

    assert response.status_code == 200
    config_data = yaml.safe_load(
        (series_dir / "series_config.yaml").read_text(encoding="utf-8")
    )
    voices = config_data["tts"]["character_voices"]
    assert voices["南道振"]["voice"] == "Katerina"
    assert "道振" not in voices


def test_put_script_sync_register_character(tmp_path) -> None:
    """register_character：把未登记的说话人写进作品角色档案。"""
    series_id, series_dir, config = _register(tmp_path)
    _write_bible(series_dir, series_id)
    _write_script(series_dir, [_beat("b1", "陌生人推门而入。", speaker_role="陌生人")])
    client = _api(config)

    response = client.put(
        f"/api/episodes/{series_id}_ep_001/script",
        json={
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "陌生人推门而入。",
                    "target_panel_ids": ["p_001"],
                    "speaker_role": "陌生人",
                }
            ],
            "sync_character_updates": [
                {"action": "register_character", "found_name": "陌生人"}
            ],
        },
    )

    assert response.status_code == 200
    assert "陌生人" in [c.name for c in _bible_characters(series_dir)]
    # 登记之后同一句不再报「未登记的角色」
    warnings = response.json()["character_warnings"]
    assert not [w for w in warnings if w["reason"] == "unregistered_speaker"]


# ---------------------------------------------------------------------------
# C. 只读检查：去重 + 繁简说话人
# ---------------------------------------------------------------------------
def test_inspect_skips_duplicate_tts_text_warning() -> None:
    """送音文本与字幕原文去空白后相同时，只报一次。"""
    canonicalizer = build_character_canonicalizer(
        characters=[_character("恩彗")],
        extra_names=["恩慧"],
        extra_texts=["恩慧推门而入。"],
    )
    beat = _beat(
        "b1",
        "恩慧推门而入。",
        tts_text="恩慧推门而入。",
        speaker_role="恩慧",
    )

    warnings = canonicalizer.inspect_script_beats([beat])

    assert {w.field for w in warnings} == {"speaker_role", "narration_text"}


def test_inspect_accepts_traditional_speaker_name() -> None:
    """说话人写繁体时，先转简体再比对（不能误报未登记角色）。"""
    canonicalizer = build_character_canonicalizer(
        characters=[_character("云飞")],
        extra_names=[],
        extra_texts=[],
    )
    beat = _beat("b1", "雲飛推门而入。", speaker_role="雲飛")

    warnings = canonicalizer.inspect_script_beats([beat])

    assert warnings == []
