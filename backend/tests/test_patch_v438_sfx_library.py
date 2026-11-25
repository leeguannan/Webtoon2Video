"""Patch v4.38：音效曲库扩到 24 键 + CC0 采编工具 + 试听选配（离线测试）。

覆盖五组：

1. **白名单与目录**：24 个标准 key 齐全、目录 key 与白名单一致、兜底键可查；
2. **归一化与别名**：同义写法收敛到标准键；``not_a_real_key`` / ``none`` 仍归一
   成「没有音效」（既有契约不能被别名表破坏）；
3. **Stage 6 关键词兜底**：既有 7 组关键词命中 100% 保持原样，新关键词按新语义
   命中（耳光→slap、推门→door_open、玻璃碎裂→glass_shatter …）；
4. **素材回源** ``resolve_sfx_asset``：真实文件优先命中，缺文件时按 ``fallback_key``
   回源复用；
5. **Web API**：``GET /api/sfx-library`` 目录结构、``GET /api/sfx-file/{key}``
   试听与非法 key 拦截；
6. **离线采编工具** ``backend/scripts/fetch_cc0_sfx.py``：Mock 的 Freesound HTML
   解析正确，且**严格只保留 CC0**。

全部离线：纯函数 + TestClient + Mock HTML，不触网、不消耗 API 额度。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.config import AppConfig, WorkspaceConfig
from core.normalize import (
    SFX_CUE_ALIASES,
    SFX_CUE_CATALOG,
    SFX_CUE_KEYS,
    normalize_script_payload,
    sfx_fallback_key,
)
from stages import s6_timeline, s7_renderer
from web.server import create_app

#: Patch v4.38 规格里的 24 个标准音效键（顺序即规格书写顺序）。
EXPECTED_24_KEYS = (
    "heart_thump",
    "door_slam",
    "door_open",
    "footstep",
    "slap",
    "punch",
    "sword_clash",
    "cloth_rustle",
    "whoosh",
    "gasp",
    "boom_hit",
    "applause",
    "crash",
    "glass_shatter",
    "thunder",
    "clock_tick",
    "page_turn",
    "phone_msg",
    "phone_ringing",
    "phone_vibrating",
    "phone_call",
    "phone_unavailable",
    "camera_shutter",
    "car_horn",
)


def _normalize_sfx(raw: str | None) -> str | None:
    """跑一遍 Stage 4 的剧本归一化，取出这一句最终落盘的 sfx_cue。"""
    payload = {
        "episode_id": "ep_001",
        "hook_title": "测试",
        "beats": [
            {
                "beat_id": "b1",
                "narration_text": "这是一句足够长的测试解说词。",
                "target_panel_ids": ["p_001"],
                "sfx_cue": raw,
            }
        ],
    }
    result = normalize_script_payload(
        payload, episode_id="ep_001", episode_no=1, valid_panel_ids={"p_001"}
    )
    beats = result.get("beats") or []
    assert beats, "归一化后应保留这一句"
    return beats[0].get("sfx_cue")


# ---------------------------------------------------------------------------
# 1) 白名单与目录
# ---------------------------------------------------------------------------
def test_sfx_cue_keys_are_exactly_the_24_spec_keys() -> None:
    assert len(SFX_CUE_KEYS) == 24
    assert set(SFX_CUE_KEYS) == set(EXPECTED_24_KEYS)
    assert len(set(SFX_CUE_KEYS)) == len(SFX_CUE_KEYS), "白名单不允许重复"


def test_sfx_cue_catalog_covers_every_key() -> None:
    keys = {entry["key"] for entry in SFX_CUE_CATALOG}
    assert keys == set(SFX_CUE_KEYS)
    for entry in SFX_CUE_CATALOG:
        assert entry["label"], f"{entry['key']} 缺中文名"
        assert entry["category"], f"{entry['key']} 缺分类"
        fallback = entry.get("fallback_key") or ""
        assert fallback == "" or fallback in SFX_CUE_KEYS


def test_sfx_fallback_key_lookup() -> None:
    assert sfx_fallback_key("slap") == "door_slam"
    assert sfx_fallback_key("punch") == "crash"
    assert sfx_fallback_key("door_open") == "whoosh"
    assert sfx_fallback_key("heart_thump") == ""
    assert sfx_fallback_key("not_a_real_key") == ""


# ---------------------------------------------------------------------------
# 2) 归一化与别名
# ---------------------------------------------------------------------------
def test_aliases_collapse_to_standard_keys() -> None:
    assert SFX_CUE_ALIASES == {
        "slap_face": "slap",
        "phone_ring": "phone_ringing",
        "vibrating": "phone_vibrating",
        "explosion": "boom_hit",
    }
    assert _normalize_sfx("slap_face") == "slap"
    assert _normalize_sfx("phone_ring") == "phone_ringing"
    assert _normalize_sfx("vibrating") == "phone_vibrating"
    assert _normalize_sfx("explosion") == "boom_hit"


def test_standard_keys_survive_normalization() -> None:
    for key in EXPECTED_24_KEYS:
        assert _normalize_sfx(key) == key


def test_unknown_and_none_sfx_cue_stay_empty() -> None:
    """既有契约不能被别名表破坏：不是音效的东西照旧丢掉。"""
    assert _normalize_sfx("not_a_real_key") is None
    assert _normalize_sfx("none") is None
    assert _normalize_sfx(None) is None
    assert _normalize_sfx("") is None


# ---------------------------------------------------------------------------
# 3) Stage 6 关键词兜底
# ---------------------------------------------------------------------------
def _resolve(text: str) -> str | None:
    """用 Stage 6 的关键词兜底解析一句话里的音效键。"""
    beat = SimpleNamespace(sfx_cue=None, target_panel_ids=["p_001"], sfx_list=[text])
    return s6_timeline.resolve_sfx_key(beat, {"p_001": [text]})


def test_legacy_keywords_still_resolve_to_same_keys() -> None:
    """v3.2 起的既有断言必须 100% 保持。"""
    assert _resolve("心驚") == "heart_thump"
    assert _resolve("颤") == "heart_thump"
    assert _resolve("嘎咿") == "whoosh"
    assert _resolve("吱") == "whoosh"
    assert _resolve("啪") == "door_slam"
    assert _resolve("甩開") == "door_slam"
    assert _resolve("砰") == "door_slam"
    assert _resolve("手机") == "phone_msg"
    assert _resolve("鈴") == "phone_msg"
    assert _resolve("噠") == "footstep"
    assert _resolve("倒吸") == "gasp"
    assert _resolve("碎") == "crash"
    assert _resolve("哗啦") == "crash"


def test_plain_description_does_not_trigger_sfx() -> None:
    assert _resolve("安静的走廊，空无一人") is None


def test_new_keywords_resolve_to_new_keys() -> None:
    assert _resolve("一记响亮的耳光") == "slap"
    assert _resolve("重重一拳") == "punch"
    assert _resolve("刀剑交击") == "sword_clash"
    assert _resolve("玻璃碎裂") == "glass_shatter"
    assert _resolve("一阵雷鸣") == "thunder"
    assert _resolve("推门进来") == "door_open"
    assert _resolve("钟表滴答") == "clock_tick"
    assert _resolve("翻页声") == "page_turn"
    assert _resolve("快门咔嚓") == "camera_shutter"
    assert _resolve("汽车鸣笛") == "car_horn"
    assert _resolve("电话铃响") == "phone_ringing"
    assert _resolve("手机震动") == "phone_vibrating"


def test_keyword_map_still_covers_legacy_groups() -> None:
    """既有 7 组一个都不能少。"""
    for key in (
        "heart_thump",
        "door_slam",
        "whoosh",
        "phone_msg",
        "footstep",
        "gasp",
        "crash",
    ):
        assert key in s6_timeline.SFX_KEYWORD_MAP


# ---------------------------------------------------------------------------
# 4) 素材回源
# ---------------------------------------------------------------------------
def test_resolve_sfx_asset_prefers_real_file(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    sfx_dir = assets / s7_renderer.SFX_DIR_NAME
    sfx_dir.mkdir(parents=True)
    (sfx_dir / "phone_ringing.mp3").write_bytes(b"fake")
    (sfx_dir / "door_slam.mp3").write_bytes(b"fake")

    hit = s7_renderer.resolve_sfx_asset("phone_ringing", assets_dir=assets)
    assert hit is not None
    assert hit.name == "phone_ringing.mp3"


def test_resolve_sfx_asset_falls_back_to_catalog_key(tmp_path: Path) -> None:
    """盘面没有 slap 时按 fallback_key 回源到 door_slam。"""
    assets = tmp_path / "assets"
    sfx_dir = assets / s7_renderer.SFX_DIR_NAME
    sfx_dir.mkdir(parents=True)
    (sfx_dir / "door_slam.mp3").write_bytes(b"fake")

    hit = s7_renderer.resolve_sfx_asset("slap", assets_dir=assets)
    assert hit is not None
    assert hit.name == "door_slam.mp3"


def test_resolve_sfx_asset_returns_none_when_nothing_available(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    (assets / s7_renderer.SFX_DIR_NAME).mkdir(parents=True)
    # clap 的兜底是 door_slam，door_slam 也没有 → 真缺。
    assert s7_renderer.resolve_sfx_asset("clap", assets_dir=assets) is None


# ---------------------------------------------------------------------------
# 5) Web API
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path / "ws"))
    return TestClient(create_app(config))


def test_sfx_library_endpoint_lists_all_keys(client: TestClient) -> None:
    resp = client.get("/api/sfx-library")
    assert resp.status_code == 200
    data = resp.json()
    keys = [item["key"] for item in data["items"]]
    assert set(keys) == set(SFX_CUE_KEYS)
    assert len(keys) == 24
    for item in data["items"]:
        assert item["label"]
        assert item["category"]
        assert isinstance(item["available"], bool)
        assert item["url"] == f"/api/sfx-file/{item['key']}"


def test_sfx_file_endpoint_serves_real_asset(client: TestClient, tmp_path: Path) -> None:
    """临时往盘面塞一个素材，试听接口要能把它回出来。"""
    assets = tmp_path / "assets"
    sfx_dir = assets / s7_renderer.SFX_DIR_NAME
    sfx_dir.mkdir(parents=True, exist_ok=True)
    (sfx_dir / "heart_thump.mp3").write_bytes(b"ID3fake")

    original = s7_renderer.assets_root
    try:
        s7_renderer.assets_root = lambda: assets  # type: ignore[assignment]
        resp = client.get("/api/sfx-file/heart_thump")
        assert resp.status_code == 200
        assert resp.content == b"ID3fake"
    finally:
        s7_renderer.assets_root = original  # type: ignore[assignment]


def test_sfx_file_endpoint_rejects_illegal_key(client: TestClient) -> None:
    resp = client.get("/api/sfx-file/not_a_real_key")
    assert resp.status_code == 400
    assert "音效键" in resp.json()["detail"]


def test_sfx_file_endpoint_404_when_missing(client: TestClient, tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    (assets / s7_renderer.SFX_DIR_NAME).mkdir(parents=True, exist_ok=True)
    original = s7_renderer.assets_root
    try:
        s7_renderer.assets_root = lambda: assets  # type: ignore[assignment]
        resp = client.get("/api/sfx-file/heart_thump")
        assert resp.status_code == 404
    finally:
        s7_renderer.assets_root = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 6) 离线采编工具（Mock HTML）
# ---------------------------------------------------------------------------
def _load_fetch_module() -> Any:
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import fetch_cc0_sfx  # noqa: PLC0415

    return fetch_cc0_sfx


MOCK_FREESOUND_HTML = """
<html><body>
  <div class="sound" id="sound-1111111">
    <div class="title"><a class="sound_title">Slap SFX</a></div>
    <div class="description">A crisp slap</div>
    <span class="license">Creative Commons 0</span>
    <span class="num-downloads">1,234</span>
    <span class="duration">0:02</span>
    <a href="/people/alice/sounds/1111111/">alice</a>
    <audio src="https://freesound.org/data/previews/1111111/1111111_hq.mp3"></audio>
  </div>
  <div class="sound" id="sound-2222222">
    <div class="title"><a class="sound_title">Attribution only</a></div>
    <div class="description">Not CC0</div>
    <span class="license">Attribution</span>
    <span class="num-downloads">9999</span>
    <span class="duration">0:05</span>
    <a href="/people/bob/sounds/2222222/">bob</a>
  </div>
  <div class="sound" id="sound-3333333">
    <div class="title">Crash</div>
    <div class="description">loud</div>
    <span class="license">Creative Commons 0</span>
    <span class="num-downloads">77</span>
    <span class="duration">0:01.50</span>
    <a href="/people/carol/sounds/3333333/">carol</a>
  </div>
</body></html>
"""


def test_fetch_tool_parses_and_filters_strictly_cc0() -> None:
    module = _load_fetch_module()
    candidates = module.parse_freesound_html(MOCK_FREESOUND_HTML)
    assert len(candidates) == 2, "非 CC0 条目必须被丢掉"
    assert {c.sound_id for c in candidates} == {"1111111", "3333333"}
    for candidate in candidates:
        assert "Creative Commons 0" in candidate.license_name
        assert candidate.preview_url.endswith("_hq.mp3")
    by_id = {c.sound_id: c for c in candidates}
    assert by_id["1111111"].author == "alice"
    assert by_id["1111111"].num_downloads == 1234
    assert by_id["1111111"].duration_s == pytest.approx(2.0)
    assert by_id["3333333"].duration_s == pytest.approx(1.5)


def test_fetch_tool_curated_queries_cover_the_13_new_keys() -> None:
    module = _load_fetch_module()
    expected = {
        "door_open",
        "slap",
        "punch",
        "sword_clash",
        "cloth_rustle",
        "boom_hit",
        "applause",
        "glass_shatter",
        "thunder",
        "clock_tick",
        "page_turn",
        "camera_shutter",
        "car_horn",
    }
    assert set(module.CURATED_SFX_QUERIES) == expected
    for key, queries in module.CURATED_SFX_QUERIES.items():
        assert queries, f"{key} 缺检索词"
        assert all(isinstance(q, str) and q.strip() for q in queries)


def test_fetch_tool_survives_garbage_html() -> None:
    module = _load_fetch_module()
    assert module.parse_freesound_html("") == []
    assert module.parse_freesound_html("<html><body>没有声音</body></html>") == []


# ---------------------------------------------------------------------------
# 7) 采编工具：Freesound 新版页面解析 / 排序 / 响度链 / 下载回退
# ---------------------------------------------------------------------------
NEW_FREESOUND_HTML = """
<html><body>
  <div class="bw-search__result">
    <div class="bw-player" data-sound-id="406464" data-username="kretopi"
         data-user-id="2825490" data-mp3="https://cdn.freesound.org/previews/406/406464_2825490-lq.mp3"
         data-title="Punching-005.wav" data-duration="0.518083"
         data-num-downloads="750"></div>
    <a class="bw-link--black" href="/people/kretopi/sounds/406464/">Punching-005.wav</a>
    <div class="bw-icon-zero" title="License: Creative Commons 0"></div>
  </div>
  <div class="bw-search__result">
    <div class="bw-player" data-sound-id="777001" data-username="longrec"
         data-user-id="9999" data-mp3="https://cdn.freesound.org/previews/777/777001_9999-lq.mp3"
         data-title="Rain and Thunder" data-duration="99.5"
         data-num-downloads="24360"></div>
    <div class="bw-icon-zero" title="License: Creative Commons 0"></div>
  </div>
</body></html>
"""


def test_fetch_tool_reads_new_freesound_card_attributes() -> None:
    module = _load_fetch_module()
    candidates = module.parse_freesound_html(NEW_FREESOUND_HTML)
    assert len(candidates) == 2
    first = candidates[0]
    assert first.sound_id == "406464"
    assert first.name == "Punching-005.wav"
    assert first.author == "kretopi"
    assert first.duration_s == pytest.approx(0.518083, abs=1e-6)
    assert first.num_downloads == 750
    # 预览地址走新版 CDN（带 user id 的高码率直链）
    assert first.preview_url == (
        "https://cdn.freesound.org/previews/406/406464_2825490-hq.mp3"
    )
    # 备用地址是页面上直接给的低码率直链
    assert first.preview_fallback_url == (
        "https://cdn.freesound.org/previews/406/406464_2825490-lq.mp3"
    )


def test_fetch_tool_scores_long_recordings_as_unusable() -> None:
    module = _load_fetch_module()
    long_one = module.parse_freesound_html(NEW_FREESOUND_HTML)[1]
    assert module.score_candidate(long_one) == 0.0


def test_fetch_tool_prefers_candidates_whose_name_matches_the_query() -> None:
    module = _load_fetch_module()
    base = module.parse_freesound_html(NEW_FREESOUND_HTML)[0]
    matching = module.replace(base, name="Thunder crack", matched_query="thunder crack")
    unrelated = module.replace(base, name="Magic - Finger snap", matched_query="thunder crack")
    assert module.score_candidate(matching) > module.score_candidate(unrelated)


def test_fetch_tool_uses_peak_normalization_instead_of_loudnorm() -> None:
    module = _load_fetch_module()
    chain = module._build_filter_chain(3.0, 2.5)
    assert "loudnorm" not in chain
    assert "volume=2.50dB" in chain
    assert "alimiter=limit=0.8413:level=disabled" in chain


def test_fetch_tool_copies_mono_to_stereo_without_attenuating() -> None:
    module = _load_fetch_module()
    mono_chain = module._build_filter_chain(3.0, 2.5, channels=1)
    stereo_chain = module._build_filter_chain(3.0, 2.5, channels=2)
    # 单声道必须显式复制，否则 ffmpeg 默认重矩阵会吃掉 3dB 峰值归一的增益
    assert "pan=stereo|c0=c0|c1=c0" in mono_chain
    assert "pan=stereo" not in stereo_chain


def test_fetch_tool_preprocess_lands_near_target_peak(tmp_path: Path) -> None:
    module = _load_fetch_module()
    source = tmp_path / "raw.mp3"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=1.2:sample_rate=44100",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    target = tmp_path / "out.mp3"
    module.preprocess_audio(source, target)
    peak = module._measure_peak_dbfs(target)
    assert peak is not None
    # 目标峰值 -3 dBFS，限幅器兜底 -1.5 dBFS
    assert -4.5 <= peak <= -1.0


def test_fetch_tool_download_falls_back_to_second_url(tmp_path: Path, monkeypatch: Any) -> None:
    module = _load_fetch_module()
    candidate = module.parse_freesound_html(NEW_FREESOUND_HTML)[0]
    seen: list[str] = []

    def fake_download(url: str, dst: Path, *, timeout_s: float) -> None:
        seen.append(url)
        if url.endswith("-hq.mp3"):
            raise RuntimeError("下载失败：404")
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                "-i", "sine=frequency=440:duration=0.5:sample_rate=44100",
                str(dst),
            ],
            check=True,
            capture_output=True,
        )

    monkeypatch.setattr(module, "_http_download", fake_download)
    target = tmp_path / "punch.mp3"
    module.download_candidate(candidate, target)
    assert target.exists()
    assert len(seen) == 2
    assert seen[0].endswith("-hq.mp3")
    assert seen[1].endswith("-lq.mp3")


def test_fetch_tool_curated_thunder_query_avoids_storm_recordings() -> None:
    module = _load_fetch_module()
    queries = module.CURATED_SFX_QUERIES["thunder"]
    assert queries
    # 泛词会把整段雷雨录音搜出来，门槛只能拦时长，检索词也要收紧
    assert "thunderstorm" not in queries
