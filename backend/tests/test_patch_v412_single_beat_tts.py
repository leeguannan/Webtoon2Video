"""Patch v4.12：单句重配 TTS + 真实试听 + 直接从 S6 重跑成片。

两条主线：

1. ``s5_tts.resynthesize_single_beat`` 的单元行为——只调 1 次 TTS、不删其它
   beat 的 WAV、清单顺序与其它条目不变、``duration_ms`` 精确更新、连续对白
   说话人继承、问题记录裁剪、缺清单 / 未知 beat 的拒绝路径；
2. 两个 HTTP API——``GET .../audio/{beat_id}`` 流式回源（``no-store`` + 404），
   ``POST .../beats/{beat_id}/regenerate-tts`` 的 200/400/404/409 守卫、局部
   剧本合并，以及 ``rerun_from_s6=true`` 只提交 S6 任务、不整话重跑 S5。

全部用假合成器（不触网），音频裁剪走本机真实 ffmpeg（与 test_s5_tts 同前提）。
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import struct
import wave
from pathlib import Path
from typing import Any, Sequence

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.config import AppConfig, WorkspaceConfig
from core.manifest import ManifestManager
from core.schemas import EpisodeDigest, NarrationScript, ScriptBeat, save_model
from providers.tts_base import TTSResult
from stages import s0_catalog, s5_tts
from web.job_runner import JobRunner
from web.server import create_app

SERIES_ID = "wt_v412"
SAMPLE_RATE = 24000


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------
def _tone(seconds: float, freq: float = 440.0, amp: float = 0.3) -> list[int]:
    count = int(SAMPLE_RATE * seconds)
    return [
        int(amp * 32767 * math.sin(2 * math.pi * freq * index / SAMPLE_RATE + math.pi / 2))
        for index in range(count)
    ]


def _wav_bytes(samples: Sequence[int], rate: int = SAMPLE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))
    return buffer.getvalue()


def _wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as handle:
        return round(handle.getnframes() * 1000 / handle.getframerate())


class FakeTTS:
    """假合成器：记录调用参数，返回真实 WAV 字节（绝不联网）。"""

    def __init__(self, *, samples: Sequence[int] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self._samples = list(samples) if samples is not None else _tone(0.3)

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str | None = None,
        language_type: str = "Chinese",
    ) -> TTSResult:
        self.calls.append(
            {"text": text, "instruction": instruction, "voice": voice, "language_type": language_type}
        )
        return TTSResult(audio=_wav_bytes(self._samples), content_type="audio/wav", voice=voice or "Fake")

    async def aclose(self) -> None:
        self.closed = True


def _beat(beat_id: str, narration_text: str, **kwargs: Any) -> ScriptBeat:
    payload: dict[str, Any] = {
        "beat_id": beat_id,
        "narration_text": narration_text,
        "target_panel_ids": ["panel_001"],
    }
    payload.update(kwargs)
    return ScriptBeat(**payload)


def _script(beats: Sequence[ScriptBeat], *, series_id: str = SERIES_ID) -> NarrationScript:
    return NarrationScript(
        episode_id=f"{series_id}_ep_001",
        hook_title="雨夜来客",
        beats=list(beats),
        episode_digest=EpisodeDigest(
            episode_no=1,
            one_line_hook="雨夜有人来访",
            summary="来客自称银行行长。",
        ),
    )


def _register(root: Path) -> tuple[str, Path, AppConfig]:
    """用 s0_catalog 登记作品，返回 (series_id, series_dir, config)。"""
    src = root / "src"
    ep_src = src / "ep_001"
    ep_src.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 600), (40, 80, 120)).save(ep_src / "001.png")
    catalog = s0_catalog.import_local_catalog(src)
    s0_catalog.CatalogRegistry(root).register(catalog)
    config = AppConfig(workspace=WorkspaceConfig(root=root))
    return catalog.series_id, root / catalog.series_id, config


async def _synth_full_episode(
    series_dir: Path, beats: Sequence[ScriptBeat], client: FakeTTS, **kwargs: Any
) -> Path:
    """写剧本并跑完整 Stage 5，返回 05_audio 目录。"""
    script_dir = series_dir / "episodes" / "ep_001" / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    save_model(_script(beats), script_dir / "script.json")
    await s5_tts.run_stage5(series_dir, 1, client, **kwargs)
    return series_dir / "episodes" / "ep_001" / "05_audio"


def _seed_episode(
    root: Path, beats: Sequence[ScriptBeat], *, client: FakeTTS | None = None, **kwargs: Any
) -> tuple[str, Path, AppConfig, Path, FakeTTS]:
    """同步测试装置：asyncio.run 跑完 Stage 5 造好整话音频。"""
    series_id, series_dir, config = _register(root)
    seed = client or FakeTTS()
    audio_dir = asyncio.run(_synth_full_episode(series_dir, beats, seed, **kwargs))
    return series_id, series_dir, config, audio_dir, seed


def _make_api_client(
    config: AppConfig, fake: FakeTTS
) -> tuple[TestClient, JobRunner]:
    runner = JobRunner(config, tts_factory=lambda: fake)
    return TestClient(create_app(config, runner=runner)), runner


def _three_narrator_beats() -> list[ScriptBeat]:
    return [
        _beat("b1", "银行行长推门而入。"),
        _beat("b2", "他摘下手套，目光扫过全场。"),
        _beat("b3", "雨声敲在玻璃窗上。"),
    ]


# ---------------------------------------------------------------------------
# A. s5_tts.resynthesize_single_beat 单元级
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_resynth_calls_tts_once_and_preserves_other_wavs(tmp_path: Path) -> None:
    series_id, series_dir, _config = _register(tmp_path)
    seed = FakeTTS()
    audio_dir = await _synth_full_episode(series_dir, _three_narrator_beats(), seed)

    untouched = {}
    for beat_id in ("b1", "b3"):
        wav = audio_dir / f"beat_{beat_id}.wav"
        stat = wav.stat()
        untouched[beat_id] = (wav, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    before_payload = json.loads((audio_dir / "audio_manifest.json").read_text(encoding="utf-8"))
    before_by_id = {item["beat_id"]: dict(item) for item in before_payload}

    fake = FakeTTS(samples=_tone(0.6))
    meta = await s5_tts.resynthesize_single_beat(fake, series_dir, 1, "b2")

    # 只调 1 次 TTS（client 生命周期由调用方/路由负责，单元层不关闭）。
    assert len(fake.calls) == 1

    # 其它 beat 的 WAV 一字节、inode、mtime 都不动。
    for beat_id, (wav, ino, size, mtime) in untouched.items():
        stat = wav.stat()
        assert (stat.st_ino, stat.st_size, stat.st_mtime_ns) == (ino, size, mtime)

    # 清单顺序不变，b1/b3 条目深度不变。
    after_payload = json.loads((audio_dir / "audio_manifest.json").read_text(encoding="utf-8"))
    assert [item["beat_id"] for item in after_payload] == ["b1", "b2", "b3"]
    after_by_id = {item["beat_id"]: item for item in after_payload}
    assert after_by_id["b1"] == before_by_id["b1"]
    assert after_by_id["b3"] == before_by_id["b3"]
    assert after_by_id["b2"]["audio_path"] == meta.audio_path
    assert (audio_dir / meta.audio_path).is_file()


@pytest.mark.anyio
async def test_resynth_updates_duration_ms_to_match_new_wav(tmp_path: Path) -> None:
    series_id, series_dir, _config = _register(tmp_path)
    seed = FakeTTS()
    audio_dir = await _synth_full_episode(series_dir, _three_narrator_beats(), seed)
    old_duration = next(
        item["duration_ms"]
        for item in json.loads((audio_dir / "audio_manifest.json").read_text(encoding="utf-8"))
        if item["beat_id"] == "b2"
    )

    fake = FakeTTS(samples=_tone(0.8))
    meta = await s5_tts.resynthesize_single_beat(fake, series_dir, 1, "b2")

    # 新时长与磁盘新 WAV 实测一致，并已写回清单；与旧时长不同。
    measured = _wav_duration_ms(audio_dir / meta.audio_path)
    assert abs(meta.duration_ms - measured) <= 2
    payload = json.loads((audio_dir / "audio_manifest.json").read_text(encoding="utf-8"))
    listed = next(item for item in payload if item["beat_id"] == "b2")
    assert listed["duration_ms"] == meta.duration_ms
    assert meta.duration_ms != old_duration


@pytest.mark.anyio
async def test_resynth_inherits_dialogue_speaker_voice(tmp_path: Path) -> None:
    """连续对白：b2 没写说话人，单句重配也必须继承 b1 的角色音色。"""
    beats = [
        _beat(
            "b1",
            "“你终于肯开口说话了对不对？”",
            speaker_role="恩彗",
            speaker_gender="female",
        ),
        _beat("b2", "“我只是不想再跟你吵下去而已。”"),
        _beat("b3", "夜色彻底沉了下来。", target_panel_ids=["panel_002"]),
    ]
    series_id, series_dir, _config = _register(tmp_path)
    seed = FakeTTS()
    await _synth_full_episode(series_dir, beats, seed, voice_cast={"恩彗": "Elias"})
    full_voice = next(
        call["voice"] for call in seed.calls if "我只是不想再跟你吵下去" in call["text"]
    )
    assert full_voice == "Elias"  # 整话合成时继承已生效

    fake = FakeTTS()
    await s5_tts.resynthesize_single_beat(
        fake, series_dir, 1, "b2", voice_cast={"恩彗": "Elias"}
    )

    assert len(fake.calls) == 1
    assert fake.calls[0]["voice"] == "Elias"  # 单句重配按整话剧本重算继承


@pytest.mark.anyio
async def test_resynth_without_manifest_raises_and_makes_no_tts_call(
    tmp_path: Path,
) -> None:
    series_id, series_dir, _config = _register(tmp_path)
    seed = FakeTTS()
    audio_dir = await _synth_full_episode(series_dir, _three_narrator_beats(), seed)
    (audio_dir / "audio_manifest.json").unlink()

    fake = FakeTTS()
    with pytest.raises(s5_tts.TTSStageError, match="Stage 5"):
        await s5_tts.resynthesize_single_beat(fake, series_dir, 1, "b2")
    assert fake.calls == []


@pytest.mark.anyio
async def test_resynth_unknown_beat_raises(tmp_path: Path) -> None:
    series_id, series_dir, _config = _register(tmp_path)
    seed = FakeTTS()
    await _synth_full_episode(series_dir, _three_narrator_beats(), seed)

    fake = FakeTTS()
    with pytest.raises(s5_tts.TTSStageError, match="beat"):
        await s5_tts.resynthesize_single_beat(fake, series_dir, 1, "b_ghost")
    assert fake.calls == []


@pytest.mark.anyio
async def test_resynth_inserts_missing_manifest_entry_in_script_order(
    tmp_path: Path,
) -> None:
    series_id, series_dir, _config = _register(tmp_path)
    seed = FakeTTS()
    audio_dir = await _synth_full_episode(series_dir, _three_narrator_beats(), seed)

    # 模拟历史清单缺 b2 的状态。
    manifest_path = audio_dir / "audio_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(
        json.dumps([item for item in payload if item["beat_id"] != "b2"], ensure_ascii=False),
        encoding="utf-8",
    )

    fake = FakeTTS()
    meta = await s5_tts.resynthesize_single_beat(fake, series_dir, 1, "b2")

    assert len(fake.calls) == 1
    restored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [item["beat_id"] for item in restored] == ["b1", "b2", "b3"]
    assert restored[1]["audio_path"] == meta.audio_path


@pytest.mark.anyio
async def test_resynth_prunes_issue_records_and_deletes_empty_file(
    tmp_path: Path,
) -> None:
    series_id, series_dir, _config = _register(tmp_path)
    seed = FakeTTS()
    audio_dir = await _synth_full_episode(series_dir, _three_narrator_beats(), seed)
    issues_path = audio_dir / "audio_issues.json"
    issues_path.write_text(
        json.dumps(
            [
                {"beat_id": "b2", "stage": "s5_tts", "message": "旧问题"},
                {"beat_id": "b3", "stage": "s5_tts", "message": "保留"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    fake = FakeTTS()
    await s5_tts.resynthesize_single_beat(fake, series_dir, 1, "b2")

    remaining = json.loads(issues_path.read_text(encoding="utf-8"))
    assert [item["beat_id"] for item in remaining] == ["b3"]

    await s5_tts.resynthesize_single_beat(fake, series_dir, 1, "b3")
    assert not issues_path.exists()  # 没有剩余问题时整文件删除


@pytest.mark.anyio
async def test_resynth_replaces_only_target_stale_audio_file(tmp_path: Path) -> None:
    """新合成换后缀时，只清该 beat 自己的旧文件，其它 beat 的文件不碰。"""
    series_id, series_dir, _config = _register(tmp_path)
    seed = FakeTTS()
    audio_dir = await _synth_full_episode(series_dir, _three_narrator_beats(), seed)
    b1_stat = (audio_dir / "beat_b1.wav").stat()

    manifest_path = audio_dir / "audio_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in payload:
        if item["beat_id"] == "b2":
            item["audio_path"] = "beat_b2.mp3"
    (audio_dir / "beat_b2.mp3").write_bytes(b"stale-mp3-bytes")
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    fake = FakeTTS()
    meta = await s5_tts.resynthesize_single_beat(fake, series_dir, 1, "b2")

    assert not (audio_dir / "beat_b2.mp3").exists()
    assert meta.audio_path == "beat_b2.wav"
    assert (audio_dir / "beat_b2.wav").is_file()
    # b1 原样保留。
    stat = (audio_dir / "beat_b1.wav").stat()
    assert (stat.st_ino, stat.st_size) == (b1_stat.st_ino, b1_stat.st_size)


# ---------------------------------------------------------------------------
# B. HTTP API 级（同步 TestClient；造数用 asyncio.run，请求走 portal）
# ---------------------------------------------------------------------------
def test_get_beat_audio_streams_wav_with_no_store(tmp_path: Path) -> None:
    series_id, series_dir, config, audio_dir, _seed = _seed_episode(
        tmp_path, _three_narrator_beats()
    )
    fake = FakeTTS()
    client, _runner = _make_api_client(config, fake)
    episode_id = f"{series_id}_ep_001"

    response = client.get(f"/api/episodes/{episode_id}/audio/b1")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.content == (audio_dir / "beat_b1.wav").read_bytes()


def test_get_beat_audio_404_when_missing(tmp_path: Path) -> None:
    series_id, _series_dir, config, _audio_dir, _seed = _seed_episode(
        tmp_path, _three_narrator_beats()
    )
    fake = FakeTTS()
    client, _runner = _make_api_client(config, fake)

    response = client.get(f"/api/episodes/{series_id}_ep_001/audio/b_ghost")

    assert response.status_code == 404


def test_regenerate_tts_success_returns_clip_beats_and_no_job(tmp_path: Path) -> None:
    series_id, _series_dir, config, _audio_dir, _seed = _seed_episode(
        tmp_path, _three_narrator_beats()
    )
    fake = FakeTTS(samples=_tone(0.5))
    client, _runner = _make_api_client(config, fake)

    response = client.post(f"/api/episodes/{series_id}_ep_001/beats/b2/regenerate-tts", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["episode_id"] == f"{series_id}_ep_001"
    assert payload["beat_id"] == "b2"
    assert payload["job"] is None
    assert payload["clip"]["beat_id"] == "b2"
    assert payload["clip"]["duration_ms"] > 0
    assert payload["duration_ms"] == payload["clip"]["duration_ms"]
    beats = {beat["beat_id"]: beat for beat in payload["beats"]}
    assert beats["b2"]["audio_duration_ms"] == payload["duration_ms"]
    assert len(fake.calls) == 1
    assert fake.closed is True


def test_regenerate_tts_merges_beat_draft_and_syncs_pause(tmp_path: Path) -> None:
    series_id, series_dir, config, _audio_dir, _seed = _seed_episode(
        tmp_path, _three_narrator_beats()
    )
    fake = FakeTTS()
    client, _runner = _make_api_client(config, fake)
    ep_dir = series_dir / "episodes" / "ep_001"
    readable = ep_dir / "04_script" / "script_readable.md"
    readable.write_text("# 旧可读稿\n", encoding="utf-8")

    response = client.post(
        f"/api/episodes/{series_id}_ep_001/beats/b2/regenerate-tts",
        json={
            "beat": {
                "beat_id": "b2",
                "narration_text": "他戴好手套，目光锁定柜台。",
                "pause_after_ms": 333,
            }
        },
    )

    assert response.status_code == 200
    # 剧本局部合并：b2 文案更新，b1/b3 不动。
    script = json.loads((ep_dir / "04_script" / "script.json").read_text(encoding="utf-8"))
    by_id = {beat["beat_id"]: beat for beat in script["beats"]}
    assert by_id["b2"]["narration_text"] == "他戴好手套，目光锁定柜台。"
    assert by_id["b1"]["narration_text"] == "银行行长推门而入。"
    assert by_id["b3"]["narration_text"] == "雨声敲在玻璃窗上。"
    # 可读稿已刷新；停顿同步进音频清单。
    assert "他戴好手套" in readable.read_text(encoding="utf-8")
    manifest = json.loads(
        (ep_dir / "05_audio" / "audio_manifest.json").read_text(encoding="utf-8")
    )
    assert next(item for item in manifest if item["beat_id"] == "b2")["pause_after_ms"] == 333
    # 新文案就是本次唯一一次 TTS 调用的送音文本。
    assert len(fake.calls) == 1
    assert "他戴好手套" in fake.calls[0]["text"]


def test_regenerate_tts_409_while_running(tmp_path: Path) -> None:
    series_id, _series_dir, config, _audio_dir, _seed = _seed_episode(
        tmp_path, _three_narrator_beats()
    )
    fake = FakeTTS()
    client, runner = _make_api_client(config, fake)
    runner.state_of(series_id, 1).status = "running"

    response = client.post(f"/api/episodes/{series_id}_ep_001/beats/b2/regenerate-tts", json={})

    assert response.status_code == 409
    assert "请等待完成后再重配" in response.json()["detail"]
    assert fake.calls == []


def test_regenerate_tts_400_when_archived(tmp_path: Path) -> None:
    series_id, series_dir, config, _audio_dir, _seed = _seed_episode(
        tmp_path, _three_narrator_beats()
    )
    fake = FakeTTS()
    client, _runner = _make_api_client(config, fake)
    manager = ManifestManager(series_dir, series_id=series_id)
    manager.save(manager.load(1).model_copy(update={"archived": True}))

    response = client.post(f"/api/episodes/{series_id}_ep_001/beats/b2/regenerate-tts", json={})

    assert response.status_code == 400
    assert "已清理过程文件" in response.json()["detail"]
    assert fake.calls == []


def test_regenerate_tts_400_without_stage5_manifest(tmp_path: Path) -> None:
    series_id, series_dir, config, audio_dir, _seed = _seed_episode(
        tmp_path, _three_narrator_beats()
    )
    (audio_dir / "audio_manifest.json").unlink()
    fake = FakeTTS()
    client, _runner = _make_api_client(config, fake)

    response = client.post(f"/api/episodes/{series_id}_ep_001/beats/b2/regenerate-tts", json={})

    assert response.status_code == 400
    assert "Stage 5" in response.json()["detail"]
    assert fake.calls == []


def test_regenerate_tts_400_short_text(tmp_path: Path) -> None:
    series_id, _series_dir, config, _audio_dir, _seed = _seed_episode(
        tmp_path, _three_narrator_beats()
    )
    fake = FakeTTS()
    client, _runner = _make_api_client(config, fake)

    response = client.post(
        f"/api/episodes/{series_id}_ep_001/beats/b2/regenerate-tts",
        json={"beat": {"beat_id": "b2", "narration_text": "x"}},
    )

    assert response.status_code == 400
    assert "2 个字" in response.json()["detail"]
    assert fake.calls == []


def test_regenerate_tts_rerun_from_s6_submits_timeline_only(tmp_path: Path) -> None:
    series_id, _series_dir, config, _audio_dir, _seed = _seed_episode(
        tmp_path, _three_narrator_beats()
    )
    fake = FakeTTS()
    client, runner = _make_api_client(config, fake)
    captured: dict[str, Any] = {}

    async def fake_submit(
        submitted_series_id: str, episode_no: int, *, from_stage: str, mode: str
    ) -> dict[str, Any]:
        captured.update(
            series_id=submitted_series_id,
            episode_no=episode_no,
            from_stage=from_stage,
            mode=mode,
        )
        return {
            "job_id": "job_v412",
            "episode_id": f"{submitted_series_id}_ep_{episode_no:03d}",
            "series_id": submitted_series_id,
            "episode_no": episode_no,
            "status": "queued",
        }

    runner.submit_episode = fake_submit  # type: ignore[method-assign]

    response = client.post(
        f"/api/episodes/{series_id}_ep_001/beats/b2/regenerate-tts",
        json={"rerun_from_s6": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["job"] == {
        "job_id": "job_v412",
        "episode_id": f"{series_id}_ep_001",
        "series_id": series_id,
        "episode_no": 1,
        "status": "queued",
    }
    # 只提交 S6 起的成片任务，S5 仅单句重配一次、没有整话重跑。
    assert captured["from_stage"] == "s6_timeline"
    assert captured["mode"] == "full_video"
    assert len(fake.calls) == 1


def test_regenerate_tts_unknown_episode_404(tmp_path: Path) -> None:
    _series_id, _series_dir, config, _audio_dir, _seed = _seed_episode(
        tmp_path, _three_narrator_beats()
    )
    fake = FakeTTS()
    client, _runner = _make_api_client(config, fake)

    response = client.post("/api/episodes/wt_ghost_ep_001/beats/b2/regenerate-tts", json={})

    assert response.status_code == 404
    assert fake.calls == []
