"""Patch v3.2 Patch 13：作品级 TTS 配音 / 语速路由 + 静态试听样例 + Web API。

全部离线：不触网、不消耗百炼额度（TTS 用假客户端，ffmpeg 端到端用例在
本机没有 ffmpeg 时自动跳过）。
"""

from __future__ import annotations

import io
import json
import math
import shutil
import struct
import wave
from pathlib import Path
from typing import Any, Sequence

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.config import (
    AppConfig,
    SeriesTTSConfig,
    VoiceSpec,
    WorkspaceConfig,
    merge_series_tts,
)
from core.schemas import CharacterProfile, ScriptBeat, StoryBibleV2, save_model
from stages import s0_catalog, s5_tts
from web.server import create_app

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _wav_bytes(seconds: float = 0.3, rate: int = 24000, freq: int = 440) -> bytes:
    """造一段正弦波 WAV 字节。"""
    buf = io.BytesIO()
    total = int(rate * seconds)
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for index in range(total):
            sample = int(12000 * math.sin(2 * math.pi * freq * index / rate))
            frames.extend(struct.pack("<h", sample))
        handle.writeframes(bytes(frames))
    return buf.getvalue()


def _write_wav(path: Path, seconds: float = 2.0, rate: int = 24000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wav_bytes(seconds=seconds, rate=rate))
    return path


def _beat(beat_id: str = "b1", *, role: str = "narrator", gender: str | None = None) -> ScriptBeat:
    payload: dict[str, Any] = {
        "beat_id": beat_id,
        "narration_text": "这是一条用于测试音色路由的解说句。",
        "target_panel_ids": ["p_001"],
        "speaker_role": role,
    }
    if gender is not None:
        payload["speaker_gender"] = gender
    return ScriptBeat.model_validate(payload)


def _series_tts() -> SeriesTTSConfig:
    """规格验收①用的配置：Elias@1.2 / Ethan@1.15 / Serena@1.1 / 继母 Katerina@1.05。"""
    return SeriesTTSConfig.model_validate(
        {
            "narrator": {"voice": "Elias", "speed": 1.2},
            "default_male": {"voice": "Ethan", "speed": 1.15},
            "default_female": {"voice": "Serena", "speed": 1.1},
            "character_voices": {"继母": {"voice": "Katerina", "speed": 1.05}},
        }
    )


class _FakeTTS:
    """记录每次合成调用的假 TTS 客户端。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def synthesize(self, text: str, *, instruction: str, voice: str | None = None) -> Any:
        self.calls.append(voice or "")
        return type("Result", (), {"audio": _wav_bytes(0.3), "content_type": "audio/x-wav"})()

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# A. 配置默认值与合并
# ---------------------------------------------------------------------------


def test_series_tts_config_defaults() -> None:
    config = SeriesTTSConfig()
    assert config.narrator == VoiceSpec(voice="Elias", speed=1.18)
    assert config.default_male == VoiceSpec(voice="Ethan", speed=1.15)
    assert config.default_female == VoiceSpec(voice="Cherry", speed=1.15)
    assert config.character_voices == {}


def test_voice_spec_rejects_speed_out_of_range() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        VoiceSpec(voice="Elias", speed=2.0)
    with pytest.raises(ValidationError):
        VoiceSpec(voice="Elias", speed=0.5)


def test_merge_series_tts_series_overrides_global() -> None:
    global_cfg = AppConfig().tts
    series_cfg = SeriesTTSConfig.model_validate(
        {
            "narrator": {"voice": "Moon", "speed": 1.25},
            "character_voices": {"继母": {"voice": "Katerina", "speed": 1.05}},
        }
    )
    merged = merge_series_tts(global_cfg, series_cfg)
    # 作品级显式覆盖生效。
    assert merged.narrator.voice == "Moon"
    assert merged.narrator.speed == 1.25
    # 未覆盖的槽位沿用全局默认。
    assert merged.default_male.voice == "Ethan"
    # 角色音色逐键合并（全局表为空时只剩作品级条目）。
    assert "继母" in merged.character_voices
    # 作品没配过（None）时完全回退全局。
    fallback = merge_series_tts(global_cfg, None)
    assert fallback.narrator.voice == "Elias"


# ---------------------------------------------------------------------------
# B. 四级音色 / 语速路由（验收①）
# ---------------------------------------------------------------------------


def test_resolve_beat_voice_spec_four_levels() -> None:
    series_tts = _series_tts()

    # 1) 旁白 → narrator 槽。
    narrator_spec = s5_tts.resolve_beat_voice_spec(_beat(role="narrator"), series_tts=series_tts)
    assert (narrator_spec.voice, narrator_spec.speed) == ("Elias", 1.2)

    # 2) 角色专属音色（精确命中 character_voices）。
    stepmom = s5_tts.resolve_beat_voice_spec(_beat(role="继母"), series_tts=series_tts)
    assert (stepmom.voice, stepmom.speed) == ("Katerina", 1.05)

    # 3a) 未配置专属音色的男性角色，按 speaker_gender 走 default_male。
    bodyguard = s5_tts.resolve_beat_voice_spec(
        _beat(role="保镖", gender="male"), series_tts=series_tts
    )
    assert (bodyguard.voice, bodyguard.speed) == ("Ethan", 1.15)

    # 3b) 未配置专属音色的女性角色走 default_female。
    maid = s5_tts.resolve_beat_voice_spec(
        _beat(role="丫鬟", gender="female"), series_tts=series_tts
    )
    assert (maid.voice, maid.speed) == ("Serena", 1.1)

    # 4) 判不出性别 → 回退旁白槽。
    unknown = s5_tts.resolve_beat_voice_spec(
        _beat(role="神秘人", gender="unknown"), series_tts=series_tts
    )
    assert (unknown.voice, unknown.speed) == ("Elias", 1.2)


def test_resolve_beat_voice_spec_uses_character_registry_gender() -> None:
    """beat 没带 speaker_gender 时，从 StoryBible 角色表补性别。"""
    series_tts = _series_tts()
    characters = [
        CharacterProfile(
            name="保镖",
            visual_traits="黑西装",
            current_status="在职",
            first_seen_ep=1,
            last_seen_ep=1,
            gender="male",
        )
    ]
    spec = s5_tts.resolve_beat_voice_spec(
        _beat(role="保镖"), series_tts=series_tts, characters=characters
    )
    assert spec.voice == "Ethan"


def test_resolve_beat_voice_spec_character_voice_case_insensitive() -> None:
    series_tts = _series_tts()
    spec = s5_tts.resolve_beat_voice_spec(_beat(role="继母".upper()), series_tts=series_tts)
    # 中文无大小写之分；这里验证英文角色名的忽略大小写路径。
    series_tts.character_voices["Alice"] = VoiceSpec(voice="Maia", speed=1.0)
    spec = s5_tts.resolve_beat_voice_spec(_beat(role="alice"), series_tts=series_tts)
    assert (spec.voice, spec.speed) == ("Maia", 1.0)


def test_resolve_beat_voice_spec_legacy_fallback_without_series_tts() -> None:
    """series_tts=None 时退化为 Patch v3.1 旧行为：只查 voice_cast，语速恒 1.0。"""
    beat = _beat(role="female_lead", gender="female")
    spec = s5_tts.resolve_beat_voice_spec(
        beat,
        series_tts=None,
        default_voice="Elias",
        voice_cast={"female_lead": "Cherry"},
    )
    assert spec.voice == "Cherry"
    assert spec.speed == 1.0

    # 旧表也没命中 → default_voice。
    fallback = s5_tts.resolve_beat_voice_spec(
        _beat(role="nobody"), series_tts=None, default_voice="Elias", voice_cast={}
    )
    assert fallback.voice == "Elias"
    assert fallback.speed == 1.0


def test_normalize_speed_clamps_and_defaults() -> None:
    assert s5_tts.normalize_speed(None) == 1.0
    assert s5_tts.normalize_speed(0) == 1.0
    assert s5_tts.normalize_speed(-1.2) == 1.0
    assert s5_tts.normalize_speed(0.5) == s5_tts.MIN_SPEED  # 0.6
    assert s5_tts.normalize_speed(2.0) == s5_tts.MAX_SPEED  # 1.8
    assert s5_tts.normalize_speed(1.25) == 1.25


# ---------------------------------------------------------------------------
# C. atempo 不变调变速（验收②）
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_trim_silence_command_contains_atempo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: list[Sequence[str]] = []

    async def _fake_run_command(cmd: Sequence[str], *, timeout: float = 0.0) -> tuple[int, str, str]:
        captured.append(cmd)
        # 模拟 ffmpeg 产出：把输入复制到命令末尾的输出路径。
        output = Path(cmd[-1])
        output.write_bytes(_wav_bytes(0.2))
        return 0, "", ""

    monkeypatch.setattr(s5_tts, "_run_command", _fake_run_command)
    src = _write_wav(tmp_path / "beat.wav", seconds=1.0)

    path, issue = await s5_tts._trim_silence(src, speed=1.25)
    assert issue is None
    assert path is not None
    assert len(captured) == 1
    cmd = list(captured[0])
    filter_index = cmd.index("-af")
    audio_filter = cmd[filter_index + 1]
    assert "atempo=1.25" in audio_filter  # 1.2500 去尾零后是 1.25


@pytest.mark.anyio
async def test_trim_silence_skips_atempo_at_unit_speed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: list[Sequence[str]] = []

    async def _fake_run_command(cmd: Sequence[str], *, timeout: float = 0.0) -> tuple[int, str, str]:
        captured.append(cmd)
        Path(cmd[-1]).write_bytes(_wav_bytes(0.2))
        return 0, "", ""

    monkeypatch.setattr(s5_tts, "_run_command", _fake_run_command)
    src = _write_wav(tmp_path / "beat.wav", seconds=1.0)

    await s5_tts._trim_silence(src, speed=1.0)
    cmd = list(captured[0])
    audio_filter = cmd[cmd.index("-af") + 1]
    assert "atempo" not in audio_filter


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="本机没有 ffmpeg，跳过端到端变速时长校验")
@pytest.mark.slow
@pytest.mark.anyio
async def test_trim_silence_actually_shortens_duration(tmp_path: Path) -> None:
    """真实 ffmpeg：2s 音频按 1.5x 变速后时长约缩短到 1/1.5。"""
    src = _write_wav(tmp_path / "beat.wav", seconds=2.0)
    before = s5_tts._wav_duration_ms(src)
    assert before is not None and before > 1900

    path, issue = await s5_tts._trim_silence(src, ffmpeg_bin="ffmpeg", speed=1.5)
    assert issue is None
    assert path is not None
    after = s5_tts._wav_duration_ms(path)
    assert after is not None
    # 正弦波没有静音可裁，时长变化完全来自 atempo；放宽到 1.1~1.6 秒区间。
    assert 1100 < after < 1600, after
    assert after < before * 0.8


# ---------------------------------------------------------------------------
# D. init_voice_samples.py 静态样例（幂等）
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_generate_voice_samples_idempotent(tmp_path: Path) -> None:
    from providers.tts_qwen import QWEN3_VOICE_IDS
    from scripts import init_voice_samples

    client = _FakeTTS()
    out = tmp_path / "voice_samples"
    results = await init_voice_samples.generate_voice_samples(out, client=client)

    # Patch v3.3 Patch 15：音色表从 13 个扩到 34 个，默认全量生成。
    assert len(results) == len(QWEN3_VOICE_IDS)
    assert all(path.is_file() for path in results.values())
    assert len(client.calls) == len(QWEN3_VOICE_IDS)  # 首次：每个音色各合成一次
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["voices"]) == len(QWEN3_VOICE_IDS)
    assert manifest["voices"]["Elias"]["file"] == "Elias.wav"

    # 第二次运行：全部跳过，零新增合成。
    client.calls.clear()
    await init_voice_samples.generate_voice_samples(out, client=client)
    assert client.calls == []


@pytest.mark.anyio
async def test_generate_voice_samples_force_and_voice_filter(tmp_path: Path) -> None:
    from scripts import init_voice_samples

    client = _FakeTTS()
    out = tmp_path / "voice_samples"

    # --voice 限定：只处理一个音色。
    await init_voice_samples.generate_voice_samples(out, client=client, voices=["Elias"])
    assert client.calls == ["Elias"]
    assert (out / "Elias.wav").is_file()
    assert not (out / "Cherry.wav").exists()

    # 非 force 再跑同一个音色：跳过。
    client.calls.clear()
    await init_voice_samples.generate_voice_samples(out, client=client, voices=["Elias"])
    assert client.calls == []

    # force=True：强制重新合成。
    await init_voice_samples.generate_voice_samples(
        out, client=client, voices=["Elias"], force=True
    )
    assert client.calls == ["Elias"]


# ---------------------------------------------------------------------------
# E. Web API（验收③）
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "ws"


@pytest.fixture()
def client(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = create_app(AppConfig(workspace=WorkspaceConfig(root=workspace)))
    # 把试听样例目录隔离到 tmp，避免依赖本机是否跑过 init_voice_samples。
    monkeypatch.chdir(workspace)
    return TestClient(app)


def _register_series(workspace: Path) -> str:
    source = workspace / "src" / "ep_001"
    source.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 600), (40, 80, 120)).save(source / "001.png")
    catalog = s0_catalog.import_local_catalog(workspace / "src")
    s0_catalog.CatalogRegistry(workspace).register(catalog)
    return catalog.series_id


def test_list_tts_voices_endpoint(client: TestClient) -> None:
    from providers.tts_qwen import QWEN3_VOICE_IDS

    response = client.get("/api/tts/voices")
    assert response.status_code == 200
    voices = response.json()["voices"]
    # Patch v3.3 Patch 15：音色表已扩充到 34 个。
    assert len(voices) == len(QWEN3_VOICE_IDS)
    ids = {item["id"] for item in voices}
    assert {"Elias", "Ethan", "Moon", "Kai", "Ryan", "Marcus", "Cherry", "Serena", "Maia",
            "Katerina", "Vivian", "Chelsie", "Bella"} <= ids
    first = voices[0]
    assert first["sample_url"] == f"/api/tts/samples/{first['id']}"
    assert "sample_ready" in first


def test_tts_sample_unknown_voice_returns_404(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 未知音色 ID。
    response = client.get("/api/tts/samples/not_a_real_voice")
    assert response.status_code == 404

    # 合法 ID 但样例文件不存在（样例目录被隔离到空 tmp）。
    from web import server

    monkeypatch.setattr(server, "voice_sample_dir", lambda: tmp_path / "empty_voice_samples")
    response = client.get("/api/tts/samples/Elias")
    assert response.status_code == 404


def test_tts_sample_serves_file_when_present(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from web import server

    sample_dir = tmp_path / "voice_samples"
    sample_dir.mkdir(parents=True)
    wav_path = sample_dir / "Elias.wav"
    wav_path.write_bytes(_wav_bytes(0.2))
    monkeypatch.setattr(server, "voice_sample_dir", lambda: sample_dir)

    response = client.get("/api/tts/samples/Elias")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content == wav_path.read_bytes()


def test_series_tts_config_roundtrip(client: TestClient, workspace: Path) -> None:
    from providers.tts_qwen import QWEN3_VOICE_IDS

    series_id = _register_series(workspace)

    # GET：默认模板。
    response = client.get(f"/api/series/{series_id}/tts-config")
    assert response.status_code == 200
    payload = response.json()
    assert payload["narrator"] == {"voice": "Elias", "speed": 1.18}
    assert payload["default_male"]["voice"] == "Ethan"
    assert isinstance(payload["characters"], list)
    assert len(payload["voices"]) == len(QWEN3_VOICE_IDS)

    # PUT：只改 narrator + 一个角色音色。
    put_body = {
        "narrator": {"voice": "Moon", "speed": 1.25},
        "character_voices": {"继母": {"voice": "Katerina", "speed": 1.05}},
    }
    response = client.put(f"/api/series/{series_id}/tts-config", json=put_body)
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["narrator"] == {"voice": "Moon", "speed": 1.25}
    assert updated["character_voices"]["继母"] == {"voice": "Katerina", "speed": 1.05}
    # 未提交的槽位保留全局默认。
    assert updated["default_female"]["voice"] == "Cherry"

    # GET 完整回读（验收③）。
    again = client.get(f"/api/series/{series_id}/tts-config").json()
    assert again["narrator"]["voice"] == "Moon"
    assert again["character_voices"]["继母"]["voice"] == "Katerina"

    # 落盘文件是合法 YAML 且保留在作品目录。
    config_path = workspace / series_id / "series_config.yaml"
    assert config_path.is_file()
    saved = json.loads(json.dumps(__import__("yaml").safe_load(config_path.read_text(encoding="utf-8"))))
    assert saved["tts"]["narrator"]["voice"] == "Moon"


def test_series_tts_config_includes_bible_characters(client: TestClient, workspace: Path) -> None:
    series_id = _register_series(workspace)
    series_dir = workspace / series_id
    bible = StoryBibleV2(
        series_id=series_id,
        series_title="测试作品",
        characters=[
            CharacterProfile(
                name="继母",
                aliases=["岳母"],
                visual_traits="黑制服",
                current_status="在职",
                first_seen_ep=1,
                last_seen_ep=1,
                gender="female",
            )
        ],
    )
    save_model(bible, series_dir / "story_bible.json")

    payload = client.get(f"/api/series/{series_id}/tts-config").json()
    assert len(payload["characters"]) == 1
    character = payload["characters"][0]
    assert character["name"] == "继母"
    assert character["gender"] == "female"
    assert "岳母" in character["aliases"]


def test_series_tts_config_unknown_series_404(client: TestClient) -> None:
    response = client.get("/api/series/wt_not_exist/tts-config")
    assert response.status_code == 404
