"""Patch v3.1 Patch 11：音频链路（角色音色 / 默认 BGM / 音效事件）的离线测试。

三条验收（对应 ``PATCH_v3.1_AUDIO_STORY_POLISH.md`` 的 Patch 11 验收段）：

1. **角色音色路由**：5 条 beat（``narrator`` / ``female_lead`` / ``male_lead`` /
   ``female_mature`` / 未登记角色）依次拿到 ``Elias`` / ``Cherry`` / ``Ethan`` /
   ``Katerina`` / ``Elias``，并且 ``TTSResult.voice`` 与实际送进 TTS 的音色一致。
2. **零版权素材**：``init_audio_assets.py`` 生成的每个 wav 都能被 ``wave`` 读出
   正时长；同名素材存在 ``.mp3`` 时解析器优先返回 ``.mp3``。
3. **音效事件与混音**：间隔不足 6s 的触发点被 Stage 6 节流；Stage 7 在未指定
   BGM 时自动挂载默认素材，并把 ``sfx_events`` 混进音轨。

全部离线：不打网络，ffmpeg 调用用假的 ``run_ffmpeg`` 捕获命令行参数。
"""

from __future__ import annotations

import io
import json
import wave
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.config import AppConfig, TTSConfig, merge_voice_cast
from core.schemas import (
    AudioClipMeta,
    CameraMotion,
    EpisodeDigest,
    NarrationScript,
    PanelMeta,
    RenderTimeline,
    ScriptBeat,
    SfxEvent,
    SubtitleLine,
    TimelineScene,
    load_model,
    save_model,
)
from providers.tts_base import TTSResult
from providers.tts_qwen import DEFAULT_VOICE, QwenTTS
from scripts import init_audio_assets
from stages import s5_tts, s6_timeline, s7_renderer

TTS_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
AUDIO_URL = "https://oss.example.com/audio/result.wav"


# --------------------------------------------------------------------------
# 素材构造
# --------------------------------------------------------------------------
def _wav_bytes(seconds: float = 0.3, rate: int = 24000) -> bytes:
    """造一段真实可解析的 wav（Stage 5 会用 ``wave`` 直读时长）。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


class _RecordingTTS:
    """记录每次 ``synthesize`` 的音色，返回真实可解析的 wav。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str | None = None,
        language_type: str = "Chinese",
    ) -> TTSResult:
        self.calls.append(
            {
                "text": text,
                "instruction": instruction,
                "voice": voice,
                "language_type": language_type,
            }
        )
        return TTSResult(
            audio=_wav_bytes(),
            content_type="audio/x-wav",
            sample_rate=24000,
            voice=voice or "",
        )


def _beat(beat_id: str, role: str, panel: str = "p_001") -> ScriptBeat:
    return ScriptBeat(
        beat_id=beat_id,
        narration_text=f"第{beat_id}句解说",
        tts_text=f"第{beat_id}句解说",
        speaker_role=role,
        target_panel_ids=[panel],
        motion=CameraMotion.ZOOM_IN,
    )


def _script(beats: list[ScriptBeat]) -> NarrationScript:
    return NarrationScript(
        episode_id="ep_001",
        hook_title="钩子",
        beats=beats,
        episode_digest=EpisodeDigest(
            episode_no=1,
            one_line_hook="一句话钩子",
            summary="摘要",
        ),
    )


# --------------------------------------------------------------------------
# 1. 角色音色路由
# --------------------------------------------------------------------------
def test_default_voice_is_elias() -> None:
    """百炼默认音色换成 Elias（Patch 11 A1）。"""
    assert DEFAULT_VOICE == "Elias"
    assert TTSConfig().default_voice == "Elias"


def test_voice_cast_defaults_cover_five_roles() -> None:
    """默认音色表覆盖五个角色 + 两个通用性别键。"""
    cast = TTSConfig().voice_cast
    assert cast["narrator"] == "Elias"
    assert cast["female_lead"] == "Cherry"
    assert cast["male_lead"] == "Ethan"
    assert cast["male_antagonist"] == "Ryan"
    assert cast["female_mature"] == "Katerina"
    assert cast["male"] == "Ethan"
    assert cast["female"] == "Cherry"


def test_series_voice_cast_overrides_global() -> None:
    """作品级音色表优先于全局表（Patch 11 A2/A3）。"""
    merged = merge_voice_cast(
        {"narrator": "Elias", "male_lead": "Ethan"},
        {"male_lead": "Ryan"},
    )
    assert merged["narrator"] == "Elias"
    assert merged["male_lead"] == "Ryan"


def test_resolve_beat_voice_follows_speaker_role() -> None:
    """``resolve_beat_voice`` 的查表顺序：精确 → 小写 → narrator 兜底。"""
    cast = TTSConfig().voice_cast
    assert s5_tts.resolve_beat_voice(_beat("b1", "female_lead"), voice_cast=cast) == "Cherry"
    assert s5_tts.resolve_beat_voice(_beat("b2", "Male_Lead"), voice_cast=cast) == "Ethan"
    # 未登记角色回退到 narrator。
    assert s5_tts.resolve_beat_voice(_beat("b3", "unknown_role"), voice_cast=cast) == "Elias"
    # 没有音色表时只剩 default_voice。
    assert s5_tts.resolve_beat_voice(_beat("b4", "male_lead"), default_voice="Cherry") == "Cherry"


@pytest.mark.anyio
async def test_synthesize_episode_routes_voice_per_speaker_role(tmp_path: Path) -> None:
    """验收一：5 条 beat 依次拿到 Elias / Cherry / Ethan / Katerina / Elias。

    同时校验 ``TTSResult.voice`` 与送进 TTS 的音色一致（Stage 5 侧）。
    """
    client = _RecordingTTS()
    script = _script(
        [
            _beat("b1", "narrator"),
            _beat("b2", "female_lead"),
            _beat("b3", "male_lead"),
            _beat("b4", "female_mature"),
            _beat("b5", "unknown_role"),
        ]
    )

    await s5_tts.synthesize_episode(
        client,
        script=script,
        audio_dir=tmp_path / "audio",
        default_voice="Elias",
        voice_cast=TTSConfig().voice_cast,
        trim_silence=False,
    )

    assert [call["voice"] for call in client.calls] == [
        "Elias",
        "Cherry",
        "Ethan",
        "Katerina",
        "Elias",
    ]
    # 情绪指令仍然透传（Patch 11 要求保留 emotion_instruction）。
    assert all(call["instruction"] == "用自然、流畅且有代入感的漫评解说语气说出" for call in client.calls)


@respx.mock
def test_tts_result_voice_matches_requested_voice() -> None:
    """验收一后半段：``TTSResult.voice`` 必须是**实际生效**的音色。

    回归点是 ``_download_audio`` 之前硬写 ``self._voice``——
    显式指定音色时结果里会记成别的音色。
    """
    respx.post(TTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "output": {"audio": {"id": "audio-1", "url": AUDIO_URL, "expires_at": 1893456000}},
                "request_id": "req-1",
            },
        )
    )
    respx.get(AUDIO_URL).mock(
        return_value=httpx.Response(200, content=_wav_bytes(), headers={"content-type": "audio/x-wav"})
    )

    import asyncio

    async def run() -> tuple[TTSResult, TTSResult]:
        async with QwenTTS(api_key="sk-test", voice="Katerina") as client:
            explicit = await client.synthesize("你好", voice="Cherry")
            default = await client.synthesize("你好")
        return explicit, default

    explicit, default = asyncio.run(run())
    assert explicit.voice == "Cherry"
    assert default.voice == "Katerina"


# --------------------------------------------------------------------------
# 2. 零版权素材
# --------------------------------------------------------------------------
def test_init_audio_assets_generates_readable_wavs(tmp_path: Path) -> None:
    """验收二：全部素材生成成功，且每个 wav 都能被 wave 读出正时长。"""
    created = init_audio_assets.generate_audio_assets(tmp_path)
    assert created, "一个素材都没生成"
    for name in init_audio_assets.BGM_NAMES:
        assert f"bgm/{name}.wav" in created
    for name in init_audio_assets.SFX_NAMES:
        assert f"sfx/{name}.wav" in created

    for path in sorted(tmp_path.rglob("*.wav")):
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            assert frames > 0, f"{path} 没有可读帧"
            assert frames / handle.getframerate() > 0

    # 音效时长落在 0.3~1.8s；BGM 是 45s 循环垫底。
    with wave.open(str(tmp_path / "sfx" / "heart_thump.wav"), "rb") as handle:
        assert 0.3 <= handle.getnframes() / handle.getframerate() <= 1.8
    with wave.open(str(tmp_path / "bgm" / "suspense.wav"), "rb") as handle:
        assert handle.getnframes() / handle.getframerate() >= 45.0


def test_init_audio_assets_does_not_overwrite_existing(tmp_path: Path) -> None:
    """已存在的同名文件默认不覆盖（用户换过的素材不能被冲掉）。"""
    init_audio_assets.generate_audio_assets(tmp_path)
    target = tmp_path / "bgm" / "suspense.wav"
    original = target.read_bytes()

    created = init_audio_assets.generate_audio_assets(tmp_path)
    assert created == {}
    assert target.read_bytes() == original

    # --force 才覆盖。
    forced = init_audio_assets.generate_audio_assets(tmp_path, force=True)
    assert "bgm/suspense.wav" in forced


def test_resolve_audio_asset_prefers_mp3_over_wav(tmp_path: Path) -> None:
    """同名素材存在 ``.mp3`` 时优先返回 ``.mp3``（Patch 11 B）。"""
    bgm_dir = tmp_path / "bgm"
    bgm_dir.mkdir(parents=True, exist_ok=True)
    (bgm_dir / "suspense.wav").write_bytes(b"wav")
    (bgm_dir / "suspense.mp3").write_bytes(b"mp3")

    hit = s7_renderer.resolve_audio_asset(bgm_dir, "suspense")
    assert hit is not None
    assert hit.suffix == ".mp3"

    # 没有 mp3 时退回 wav。
    (bgm_dir / "suspense.mp3").unlink()
    fallback = s7_renderer.resolve_audio_asset(bgm_dir, "suspense")
    assert fallback is not None and fallback.suffix == ".wav"


def test_resolve_default_bgm_uses_configured_name(tmp_path: Path) -> None:
    """默认 BGM 按 ``render.default_bgm`` 找；找不到就退回目录首个音频。"""
    bgm_dir = tmp_path / "bgm"
    bgm_dir.mkdir(parents=True, exist_ok=True)
    (bgm_dir / "suspense.wav").write_bytes(b"wav")

    config = AppConfig()
    hit = s7_renderer.resolve_default_bgm(config, assets_dir=tmp_path)
    assert hit is not None and hit.name == "suspense.wav"

    other = AppConfig()
    other.render.default_bgm = "emotional"
    fallback = s7_renderer.resolve_default_bgm(other, assets_dir=tmp_path)
    assert fallback is not None and fallback.name == "suspense.wav"

    # auto_bgm=False 时不铺默认音乐。
    off = AppConfig()
    off.render.auto_bgm = False
    assert s7_renderer.resolve_default_bgm(off, assets_dir=tmp_path) is None


# --------------------------------------------------------------------------
# 3. 音效事件与混音
# --------------------------------------------------------------------------
def test_select_sfx_events_throttles_close_triggers() -> None:
    """验收三前半段：间隔不足 6s 的触发点被节流，最多保留 8 个。"""
    candidates = [(0, "heart_thump"), (3000, "door_slam"), (6000, "whoosh"), (9000, "gasp")]
    events = s6_timeline.select_sfx_events(candidates)
    assert [event.start_ms for event in events] == [0, 6000]
    assert [event.sfx_key for event in events] == ["heart_thump", "whoosh"]

    # 整话上限 8 条。
    many = [(index * 6000, "heart_thump") for index in range(20)]
    limited = s6_timeline.select_sfx_events(many)
    assert len(limited) == 8


def test_resolve_sfx_key_prefers_beat_cue_then_keywords() -> None:
    """主来源是 ``beat.sfx_cue``；没有才用画面 ``sfx_list`` 的关键词兜底。"""
    beat_with_cue = ScriptBeat(
        beat_id="b1",
        narration_text="一句解说",
        speaker_role="narrator",
        target_panel_ids=["p_001"],
        motion=CameraMotion.ZOOM_IN,
        sfx_cue="door_slam",
    )
    panel_map = {"p_001": ["心驚的声音"]}
    assert s6_timeline.resolve_sfx_key(beat_with_cue, panel_map) == "door_slam"

    beat_without_cue = _beat("b2", "narrator")
    assert s6_timeline.resolve_sfx_key(beat_without_cue, panel_map) == "heart_thump"
    assert s6_timeline.resolve_sfx_key(beat_without_cue, {}) is None


def test_build_timeline_fills_sfx_events() -> None:
    """Stage 6 产出时间轴时把音效事件写进 ``sfx_events``。"""
    script = _script(
        [
            ScriptBeat(
                beat_id="b1",
                narration_text="第一句",
                speaker_role="narrator",
                target_panel_ids=["p_001"],
                motion=CameraMotion.ZOOM_IN,
                sfx_cue="heart_thump",
            ),
            ScriptBeat(
                beat_id="b2",
                narration_text="第二句",
                speaker_role="narrator",
                target_panel_ids=["p_002"],
                motion=CameraMotion.ZOOM_IN,
                sfx_cue="door_slam",
            ),
        ]
    )
    audios = [
        # 第一句足够长（>6s），第二条音效才不会被节流掉。
        AudioClipMeta(beat_id="b1", audio_path="a1.wav", duration_ms=6100, pause_after_ms=0),
        AudioClipMeta(beat_id="b2", audio_path="a2.wav", duration_ms=2000, pause_after_ms=0),
    ]
    panels = [
        PanelMeta(
            panel_id="p_001",
            image_path="p1.png",
            width=800,
            height=1400,
            aspect_ratio=800 / 1400,
            source_slice_range=(0, 1400),
            is_tall_panel=True,
        ),
        PanelMeta(
            panel_id="p_002",
            image_path="p2.png",
            width=800,
            height=1400,
            aspect_ratio=800 / 1400,
            source_slice_range=(0, 1400),
            is_tall_panel=True,
        ),
    ]

    timeline = s6_timeline.build_timeline(
        episode_id="ep_001",
        script=script,
        audios=audios,
        panels=panels,
    )
    assert [event.sfx_key for event in timeline.sfx_events] == ["heart_thump", "door_slam"]
    assert timeline.sfx_events[0].start_ms == 0
    # 两条间隔必须 >= 6s。
    assert timeline.sfx_events[1].start_ms >= 6000


def _make_s7_workspace(root: Path) -> tuple[Path, RenderTimeline]:
    """造一话最小的 Stage 6 产物（时间轴 + 字幕 PNG + 面板 + 音频）。"""
    from PIL import Image

    episode_dir = root / "episodes" / "ep_001"
    plan_dir = episode_dir / "06_plan"
    (plan_dir / "subtitles").mkdir(parents=True, exist_ok=True)
    (episode_dir / "02_panels").mkdir(parents=True, exist_ok=True)
    (episode_dir / "05_audio").mkdir(parents=True, exist_ok=True)

    Image.new("RGB", (400, 700), (30, 40, 60)).save(episode_dir / "02_panels" / "p_001.png")
    Image.new("RGBA", (1080, 1920), (0, 0, 0, 0)).save(plan_dir / "subtitles" / "0001.png")
    (episode_dir / "05_audio" / "a1.wav").write_bytes(_wav_bytes(1.0, 44100))

    scenes = [
        TimelineScene(
            scene_index=0,
            beat_id="b1",
            panel_id="p_001",
            image_path="02_panels/p_001.png",
            start_ms=0,
            end_ms=1000,
            duration_ms=1000,
            motion=CameraMotion.ZOOM_IN,
        )
    ]
    audios = [AudioClipMeta(beat_id="b1", audio_path="a1.wav", duration_ms=900, pause_after_ms=100)]
    subtitles = [SubtitleLine(index=1, beat_id="b1", text="第一句解说", start_ms=0, end_ms=1000)]
    timeline = RenderTimeline(
        episode_id="ep_001",
        total_duration_ms=1000,
        scenes=scenes,
        audios=audios,
        subtitles=subtitles,
        sfx_events=[SfxEvent(sfx_key="heart_thump", start_ms=200, gain_db=-10.0)],
    )
    save_model(timeline, plan_dir / "timeline.json")
    return episode_dir, timeline


def test_render_timeline_mixes_sfx_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验收三后半段：``sfx_events`` 用 ``adelay`` + ``volume`` 混进音轨。"""
    calls: list[list[str]] = []

    def fake_run_ffmpeg(args: list[str], **kwargs: object) -> str:
        calls.append(list(args))
        target = Path(args[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake")
        return ""

    monkeypatch.setattr(s7_renderer, "run_ffmpeg", fake_run_ffmpeg)

    assets = tmp_path / "assets"
    (assets / "sfx").mkdir(parents=True, exist_ok=True)
    (assets / "sfx" / "heart_thump.wav").write_bytes(_wav_bytes(0.5, 44100))

    episode_dir, timeline = _make_s7_workspace(tmp_path)
    s7_renderer.render_timeline(
        timeline,
        episode_dir=episode_dir,
        out_path=tmp_path / "out" / "ep_001.mp4",
        ffmpeg_bin="ffmpeg",
        work_dir=tmp_path / "work",
        assets_dir=assets,
    )

    joined = " ".join(" ".join(call) for call in calls)
    # 音效轨：延迟到事件时间点 + 峰值归一 + 降音量，并单独成轨。
    # Patch 14 起 SfxEvent 的 schema 默认增益(-10.0)被视为「未显式指定」，
    # 沿用渲染配置的新默认 -3.5dB，且每条 SFX 链先过 dynaudnorm。
    assert "adelay=200|200" in joined
    assert "dynaudnorm" in joined
    assert "volume=-3.5dB" in joined
    assert "[sfx]" in joined
    # 成片混音：人声 + 音效两路。
    assert "amix=inputs=2" in joined


def test_render_episode_auto_loads_default_bgm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未传 ``--bgm`` 且有默认素材时，Stage 7 自动铺默认 BGM。"""
    calls: list[list[str]] = []

    def fake_run_ffmpeg(args: list[str], **kwargs: object) -> str:
        calls.append(list(args))
        target = Path(args[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake")
        return ""

    monkeypatch.setattr(s7_renderer, "run_ffmpeg", fake_run_ffmpeg)

    assets = tmp_path / "assets"
    (assets / "bgm").mkdir(parents=True, exist_ok=True)
    (assets / "bgm" / "suspense.wav").write_bytes(_wav_bytes(1.0, 44100))
    monkeypatch.setattr(s7_renderer, "assets_root", lambda: assets)

    episode_dir, _timeline = _make_s7_workspace(tmp_path)
    out_path = s7_renderer.render_episode(tmp_path, 1, ffmpeg_bin="ffmpeg", title="自动 BGM")

    assert out_path.is_file()
    joined = " ".join(" ".join(call) for call in calls)
    assert "suspense.wav" in joined
    # 默认 BGM 走闪避（sidechaincompress），人声始终是主控。
    assert "sidechaincompress=" in joined


def test_render_timeline_without_sfx_keeps_voice_only_mix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """没有音效事件时，混音链保持原来的两路（人声 + BGM）。"""
    calls: list[list[str]] = []

    def fake_run_ffmpeg(args: list[str], **kwargs: object) -> str:
        calls.append(list(args))
        target = Path(args[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake")
        return ""

    monkeypatch.setattr(s7_renderer, "run_ffmpeg", fake_run_ffmpeg)

    episode_dir, timeline = _make_s7_workspace(tmp_path)
    timeline.sfx_events = []
    s7_renderer.render_timeline(
        timeline,
        episode_dir=episode_dir,
        out_path=tmp_path / "out" / "ep_001.mp4",
        ffmpeg_bin="ffmpeg",
        work_dir=tmp_path / "work",
    )
    joined = " ".join(" ".join(call) for call in calls)
    assert "[sfx]" not in joined


def test_old_timeline_json_without_sfx_events_still_loads(tmp_path: Path) -> None:
    """旧时间轴 JSON（没有 ``sfx_events``）零报错反序列化。"""
    payload = {
        "episode_id": "ep_001",
        "total_duration_ms": 1000,
        "scenes": [],
        "audios": [],
        "subtitles": [],
    }
    path = tmp_path / "timeline.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    timeline = load_model(RenderTimeline, path)
    assert timeline.sfx_events == []
