"""PATCH v4.40：句内音效精准落点（``[sfx:key]`` 内联标签 + Beat 级 首/中/尾 锚点）。

**目标**：把「音效」从「整句随便一个时刻响」升级成「说到哪、响在哪」。

两条互补的通道：

* **句中内联标签**：送音文本（``tts_text``）里写 ``[sfx:slap]``，
  Stage 5 会把这句话**按标签切成子段**分别合成，再拼回一条完整音频，
  同时按**前置子段的真实耗时**算出音效落点（毫秒级）；
  标签**绝不会**被送进 TTS 引擎（引擎会把拉丁字母读成拼音）；
* **整句级落点锚点** ``sfx_position``（``start`` / ``mid`` / ``end``）：
  没有内联标签时的粗锚点，由 Stage 6 按这句的音频时长换算。

本文件覆盖四层：

1. ``core/normalize.py``：标签规范化 / 别名收敛 / 非法剥离 / 长度豁免 / 字幕净化；
2. ``stages/s5_tts.py``：句首句尾**不拆分**整句合成、句中**分段真实拼接**、
   落点毫秒精确、送入 TTS 的文本绝不含标签、缓存命中恢复落点、移动标签重算；
3. ``stages/s6_timeline.py``：内联落点**优先**、不再重复触发 beat 级兜底、
   ``sfx_position`` 的 mid / end 偏移；
4. ``web/server.py``：``PUT /script`` 保留 ``sfx_position``。

全部离线：纯函数 + 临时目录 + TestClient；S5 相关用例走**真实 ffmpeg**，
无 ffmpeg 时自动跳过。
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import shutil
import struct
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.config import AppConfig, WorkspaceConfig
from core.normalize import (
    normalize_inline_sfx_tags,
    normalize_script_payload,
    sanitize_narration_and_tts,
    spoken_length,
)
from core.schemas import (
    AudioClipMeta,
    CameraMotion,
    EpisodeDigest,
    InlineSfxOffset,
    NarrationScript,
    PanelMeta,
    ScriptBeat,
    save_model,
)
from providers.tts_base import TTSResult
from stages import s5_tts
from stages.s6_timeline import build_timeline
from web.server import create_app

FFMPEG_BIN = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="需要 ffmpeg 才能跑真实音频链路"
)


# ---------------------------------------------------------------------------
# 造数工具
# ---------------------------------------------------------------------------


def _make_wav(seconds: float, freq: float = 440.0, rate: int = 24000) -> bytes:
    """造一段单声道正弦波 WAV（无首尾静音，方便断言精确时长）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        count = max(1, int(seconds * rate))
        frames = bytearray()
        for index in range(count):
            sample = int(12000 * math.sin(2 * math.pi * freq * index / rate))
            frames += struct.pack("<h", sample)
        handle.writeframes(bytes(frames))
    return buf.getvalue()


class _FakeClient:
    """假 TTS 引擎：按文本长度生成对应时长的正弦波。"""

    model = "speech-2.8-hd"
    provider_name = "fake"
    sample_rate = 24000

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def synthesize(self, text: str, instruction: str = "", **kwargs) -> TTSResult:
        self.calls.append(text)
        seconds = 0.5 + 0.1 * len(text.strip())
        return TTSResult(audio=_make_wav(seconds), content_type="audio/wav")


def _beat(
    beat_id: str,
    narration: str,
    *,
    tts_text: str | None = None,
    panel_ids: list[str] | None = None,
    sfx_cue: str | None = None,
    sfx_position: str = "start",
) -> ScriptBeat:
    return ScriptBeat(
        beat_id=beat_id,
        narration_text=narration,
        tts_text=tts_text,
        target_panel_ids=panel_ids or ["p_001"],
        motion=CameraMotion.STATIC,
        sfx_cue=sfx_cue,
        sfx_position=sfx_position,  # type: ignore[arg-type]
    )


async def _synth(
    client: _FakeClient,
    beat: ScriptBeat,
    audio_dir: Path,
    *,
    use_cache: bool = True,
    write_cache: bool = True,
    cache_records: list | None = None,
):
    return await s5_tts._synthesize_beat(
        client,
        beat,
        audio_dir=audio_dir,
        overrides={},
        voice=None,
        ffmpeg_bin=FFMPEG_BIN,
        trim_silence=True,
        use_cache=use_cache,
        write_cache=write_cache,
        cache_records=cache_records if cache_records is not None else [],
        stats_out={},
    )


def _panel(panel_id: str, image_dir: Path) -> PanelMeta:
    image_dir.mkdir(parents=True, exist_ok=True)
    path = image_dir / f"{panel_id}.png"
    Image.new("RGB", (120, 200), (30, 60, 90)).save(path)
    return PanelMeta(
        panel_id=panel_id,
        image_path=str(path),
        width=120,
        height=200,
        aspect_ratio=120 / 200,
        source_slice_range=(0, 200),
        is_tall_panel=False,
    )


def _script(beats: list[ScriptBeat]) -> NarrationScript:
    return NarrationScript(
        episode_id="ep_001",
        hook_title="句内音效测试",
        beats=beats,
        episode_digest=EpisodeDigest(
            episode_no=1, one_line_hook="钩子", summary="摘要"
        ),
    )


# ---------------------------------------------------------------------------
# 1. 契约层：标签规范化 / 长度豁免 / 字幕净化
# ---------------------------------------------------------------------------


def test_normalize_inline_sfx_tags_canonicalizes_alias() -> None:
    """别名收敛到白名单里的标准键。"""
    assert (
        normalize_inline_sfx_tags("他[sfx:slap_face]一拳挥出")
        == "他[sfx:slap]一拳挥出"
    )


def test_normalize_inline_sfx_tags_strips_unknown_key() -> None:
    """非法 key 的标签直接剥离（不留残渣，也不阻断这一句）。"""
    assert normalize_inline_sfx_tags("他[sfx:not_a_real_key]一拳") == "他一拳"


def test_normalize_inline_sfx_tags_collapses_adjacent_duplicates() -> None:
    """相邻重复标签折叠成一个（别把音效叠两遍）。"""
    assert (
        normalize_inline_sfx_tags("他[sfx:slap][sfx:slap]一拳")
        == "他[sfx:slap]一拳"
    )


def test_spoken_length_excludes_inline_sfx_tag() -> None:
    """标签不占发音长度（否则会被当成「超长句」而腰斩）。"""
    assert spoken_length("他[sfx:slap]一拳挥出") == 5


def test_sanitize_narration_strips_tag_but_tts_keeps_it() -> None:
    """字幕绝不印出标签；送音文本保留合法标签。"""
    text = "他[sfx:slap]一拳挥出"
    assert sanitize_narration_and_tts(text, for_tts=False) == "他一拳挥出"
    assert sanitize_narration_and_tts(text, for_tts=True) == "他[sfx:slap]一拳挥出"


def test_normalize_script_payload_keeps_sfx_position() -> None:
    """sfx_position 合法值保留、非法值回落 start。"""
    payload = {
        "episode_id": "ep_001",
        "hook_title": "钩子",
        "beats": [
            {
                "beat_id": "b1",
                "narration_text": "一句解说。",
                "target_panel_ids": ["p_001"],
                "sfx_position": "mid",
            }
        ],
    }
    normalized = normalize_script_payload(payload, episode_id="ep_001", episode_no=1)
    assert normalized["beats"][0]["sfx_position"] == "mid"

    payload["beats"][0]["sfx_position"] = "nonsense"
    normalized = normalize_script_payload(payload, episode_id="ep_001", episode_no=1)
    assert normalized["beats"][0]["sfx_position"] == "start"


# ---------------------------------------------------------------------------
# 2. Stage 5：句首 / 句尾不拆分，句中分段真实拼接
# ---------------------------------------------------------------------------


@requires_ffmpeg
def test_start_inline_sfx_synthesizes_whole_sentence_once() -> None:
    """句首音效：整句只合成一次，落点在 0（配音一开口就响）。"""
    client = _FakeClient()
    beat = _beat(
        "b1",
        "门被猛地推开。",
        tts_text="[sfx:door_open]门被猛地推开。",
    )
    meta, issues = asyncio.run(_synth(client, beat, Path("/tmp/v440_test_start")))
    assert issues == []
    assert meta is not None
    assert len(client.calls) == 1, "句首音效不该把整句拆开"
    assert "[sfx:" not in client.calls[0]
    assert [(item.sfx_key, item.offset_ms) for item in meta.inline_sfx] == [
        ("door_open", 0)
    ]


@requires_ffmpeg
def test_end_inline_sfx_offset_sits_before_the_tail() -> None:
    """句尾音效：落点 = 音频结束前 120ms。"""
    client = _FakeClient()
    beat = _beat(
        "b1",
        "一切归于寂静。",
        tts_text="一切归于寂静。[sfx:thunder]",
    )
    meta, issues = asyncio.run(_synth(client, beat, Path("/tmp/v440_test_end")))
    assert issues == []
    assert meta is not None
    assert len(client.calls) == 1, "句尾音效不该把整句拆开"
    offset = meta.inline_sfx[0].offset_ms
    assert meta.inline_sfx[0].sfx_key == "thunder"
    assert abs(offset - (meta.duration_ms - s5_tts.INLINE_SFX_TAIL_OFFSET_MS)) <= 20


@requires_ffmpeg
def test_mid_inline_sfx_splits_and_computes_exact_offset() -> None:
    """句中音效：切成两段分别合成，落点 = 前一段的真实耗时。"""
    client = _FakeClient()
    beat = _beat(
        "b1",
        "他一拳挥出，鲜血飞溅。",
        tts_text="他[sfx:slap]一拳挥出，鲜血飞溅。",
    )
    meta, issues = asyncio.run(_synth(client, beat, Path("/tmp/v440_test_mid")))
    assert issues == []
    assert meta is not None
    # 两段文字 → 两次合成，且送进引擎的文本里绝不能有标签。
    assert len(client.calls) == 2
    for call in client.calls:
        assert "[sfx:" not in call
    # 第一段 "他" → 0.5 + 0.1 = 0.6s = 600ms。
    offset = meta.inline_sfx[0].offset_ms
    assert meta.inline_sfx[0].sfx_key == "slap"
    assert 520 <= offset <= 680, f"落点应≈第一段真实耗时 600ms，实际 {offset}"
    # 总时长 = 第一段 600ms + 默认间隙 80ms + 第二段 1500ms。
    assert 2100 <= meta.duration_ms <= 2260


@requires_ffmpeg
def test_inline_sfx_never_reaches_the_tts_engine() -> None:
    """铁律：``[sfx:...]`` 绝不能被送进 TTS 引擎（引擎会把拉丁字母读成拼音）。"""
    client = _FakeClient()
    beat = _beat(
        "b1",
        "他一拳挥出，鲜血飞溅。",
        tts_text="他[sfx:slap]一拳挥出，鲜血飞溅。",
    )
    asyncio.run(_synth(client, beat, Path("/tmp/v440_test_notts")))
    assert client.calls
    assert all("[sfx:" not in call for call in client.calls)


@requires_ffmpeg
def test_inline_sfx_cache_hit_restores_offsets() -> None:
    """命中缓存：不调 TTS，但落点要跟着缓存一起恢复。"""
    audio_dir = Path("/tmp/v440_test_cache")
    client = _FakeClient()
    beat = _beat(
        "b1",
        "他一拳挥出，鲜血飞溅。",
        tts_text="他[sfx:slap]一拳挥出，鲜血飞溅。",
    )
    records: list = []
    first, _ = asyncio.run(_synth(client, beat, audio_dir, cache_records=records))
    assert first is not None and first.inline_sfx
    # 缓存索引要落盘才会被下一次命中（整话流程由 synthesize_episode 统一 flush）。
    s5_tts._flush_cache_index(s5_tts._cache_dir(audio_dir), records)

    client2 = _FakeClient()
    second, _ = asyncio.run(_synth(client2, beat, audio_dir))
    assert client2.calls == [], "命中缓存时不该再调 TTS"
    assert second is not None
    assert [(i.sfx_key, i.offset_ms) for i in second.inline_sfx] == [
        (i.sfx_key, i.offset_ms) for i in first.inline_sfx
    ]


@requires_ffmpeg
def test_moving_inline_sfx_tag_recomputes_synth_and_offset() -> None:
    """标签挪位置 → 指纹变 → 重新合成，落点跟着变。"""
    audio_dir = Path("/tmp/v440_test_move")
    client = _FakeClient()
    beat_a = _beat(
        "b1",
        "他一拳挥出，鲜血飞溅。",
        tts_text="他[sfx:slap]一拳挥出，鲜血飞溅。",
    )
    first, _ = asyncio.run(_synth(client, beat_a, audio_dir))
    assert first is not None

    client2 = _FakeClient()
    beat_b = _beat(
        "b1",
        "他一拳挥出，鲜血飞溅。",
        tts_text="他一拳挥出，鲜血飞溅。[sfx:slap]",
    )
    second, _ = asyncio.run(_synth(client2, beat_b, audio_dir))
    assert client2.calls, "标签位置变了必须重新合成（不能吃旧缓存）"
    assert second is not None
    assert second.inline_sfx[0].offset_ms != first.inline_sfx[0].offset_ms


# ---------------------------------------------------------------------------
# 3. Stage 6：内联落点优先 + sfx_position 偏移
# ---------------------------------------------------------------------------


def _timeline(beats: list[ScriptBeat], audios: list[AudioClipMeta], panels: list[PanelMeta]):
    return build_timeline(
        episode_id="ep_001",
        script=_script(beats),
        audios=audios,
        panels=panels,
    )


def test_timeline_prefers_inline_sfx_offsets(tmp_path: Path) -> None:
    """内联落点按毫秒落位（= 该句起点 + 标签落点）。"""
    panels = [_panel("p_001", tmp_path / "02_panels")]
    beats = [_beat("b1", "一句解说。", sfx_cue="crash")]
    audios = [
        AudioClipMeta(
            beat_id="b1",
            audio_path="05_audio/b1.wav",
            duration_ms=2000,
            pause_after_ms=0,
            inline_sfx=[InlineSfxOffset(sfx_key="slap", offset_ms=600)],
        )
    ]
    timeline = _timeline(beats, audios, panels)
    assert len(timeline.sfx_events) == 1
    event = timeline.sfx_events[0]
    assert event.sfx_key == "slap"
    assert event.start_ms == 600


def test_timeline_skips_beat_sfx_cue_when_inline_sfx_present(tmp_path: Path) -> None:
    """一句里既有内联音效又有 beat 级 sfx_cue → 只响一次（内联优先）。"""
    panels = [_panel("p_001", tmp_path / "02_panels")]
    beats = [_beat("b1", "一句解说。", sfx_cue="crash")]
    audios = [
        AudioClipMeta(
            beat_id="b1",
            audio_path="05_audio/b1.wav",
            duration_ms=2000,
            pause_after_ms=0,
            inline_sfx=[InlineSfxOffset(sfx_key="slap", offset_ms=600)],
        )
    ]
    timeline = _timeline(beats, audios, panels)
    assert [event.sfx_key for event in timeline.sfx_events] == ["slap"]


def test_timeline_sfx_position_mid_and_end(tmp_path: Path) -> None:
    """没有内联标签时：mid = 句中，end = 结束前 150ms。"""
    panels = [_panel("p_001", tmp_path / "02_panels")]
    beats = [
        _beat("b1", "第一句。", sfx_cue="crash", sfx_position="mid"),
        _beat("b2", "第二句。", sfx_cue="whoosh", sfx_position="end"),
    ]
    audios = [
        AudioClipMeta(
            beat_id="b1",
            audio_path="05_audio/b1.wav",
            duration_ms=2000,
            # 停顿 5s：让两句的音效事件间隔超过 6s 节流阈值。
            pause_after_ms=5000,
        ),
        AudioClipMeta(
            beat_id="b2",
            audio_path="05_audio/b2.wav",
            duration_ms=3000,
            pause_after_ms=0,
        ),
    ]
    timeline = _timeline(beats, audios, panels)
    events = {event.sfx_key: event.start_ms for event in timeline.sfx_events}
    assert events["crash"] == 1000  # 2000 // 2（第一句起点 0）
    # 第二句的落点 = 该句起点（帧栅格对齐后由 S6 决定）+ 句尾提前量 2850ms。
    second_start = timeline.subtitles[1].start_ms
    assert events["whoosh"] == second_start + 2850


# ---------------------------------------------------------------------------
# 4. API 契约：PUT /script 保留 sfx_position
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path / "ws"))
    return TestClient(create_app(config))


def _seed_episode(workspace: Path, series_id: str) -> Path:
    episode_dir = workspace / series_id / "episodes" / "ep_001"
    panels_dir = episode_dir / "02_panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    for index in range(1, 3):
        Image.new("RGB", (360, 640), (index * 10, 20, 30)).save(
            panels_dir / f"p_{index:03d}.png"
        )

    beats = [_beat("b1", "第一句解说。", panel_ids=["p_001"])]
    script = NarrationScript(
        episode_id=f"{series_id}_ep_001",
        hook_title="音效落点接口测试",
        beats=beats,
        episode_digest=EpisodeDigest(episode_no=1, one_line_hook="钩子", summary="摘要"),
    )
    script_dir = episode_dir / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    save_model(script, script_dir / "script.json")
    (script_dir / "script_readable.md").write_text("# 解说文稿\n", encoding="utf-8")

    audio_dir = episode_dir / "05_audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / "audio_manifest.json").write_text(
        json.dumps(
            [
                {
                    "beat_id": "b1",
                    "audio_path": "05_audio/b1.wav",
                    "duration_ms": 1000,
                    "pause_after_ms": 300,
                    "word_timestamps": [],
                }
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return episode_dir


def test_put_script_preserves_sfx_position(
    client: TestClient, tmp_path: Path
) -> None:
    """保存解说稿时 sfx_position 必须落盘（否则改落点白改）。"""
    workspace = tmp_path / "ws"
    source = workspace / "src"
    source.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 600), (30, 60, 90)).save(source / "001.png")
    series_id = client.post("/api/series", json={"url": str(source)}).json()["series_id"]
    episode_dir = _seed_episode(workspace, series_id)
    episode_id = f"{series_id}_ep_001"

    response = client.put(
        f"/api/episodes/{episode_id}/script",
        json={
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "第一句解说。",
                    "tts_text": "第一句解说。",
                    "target_panel_ids": ["p_001"],
                    "sfx_cue": "crash",
                    "sfx_position": "end",
                    "pause_after_ms": 300,
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    payload = json.loads(
        (episode_dir / "04_script" / "script.json").read_text(encoding="utf-8")
    )
    assert payload["beats"][0]["sfx_position"] == "end"
