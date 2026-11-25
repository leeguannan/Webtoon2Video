"""PATCH v4.41：句内内联音效与整句级音效**分档增益**（内联更轻）。

对应 ``PATCH_v4.40_SFX_INLINE_TIMING.md`` 第九节「后续建议」第 2 条。

**问题**：v4.40 落地了句内内联音效（``tts_text`` 里的 ``[sfx:key]`` 标签），
但渲染时它和整句级音效（``sfx_cue`` / 关键词兜底）用的是**同一档增益**
（``render.sfx_gain_db`` = -2.0dB）。内联音效落在解说正文**中间**，与人声同段共存，
这一声压过解说就听不清词了；而整句级音效落在句首/句末，前后是干净空隙，可以给足音量。

**解法（两条）**：

1. **分档增益**：``SfxEvent`` 增加 ``source`` 字段（``beat`` / ``inline``），
   Stage 7 按来源选增益档——内联走新的 ``render.inline_sfx_gain_db``（默认 -6.0dB），
   整句级仍走 ``render.sfx_gain_db``（-2.0dB）；事件显式写死 ``gain_db`` 时仍然最高优先。
2. **内联不再被挤掉**（顺带修掉的连带缺陷）：v4.40 的内联音效和整句级音效共用
   「6000ms 节流 + 8 条上限」同一条队列，内联落点又比同句整句级更晚，
   极易被前面的整句级音效挤掉、或因 8 条上限已满而整条被丢弃。
   现在内联走**独立的 1200ms 间隔**先占位，整句级再补位（落在内联 2000ms 内就让位），
   两者共享「全话 8 条」总额度。

本文件覆盖四层：

1. 数据模型：``SfxEvent.source`` 默认值与旧 JSON 兼容；
2. ``stages/s6_timeline.py``：``select_sfx_events`` 的来源标记、内联优先、让位规则；
3. ``stages/s6_timeline.py``：``build_timeline`` 给事件打上 ``source``；
4. ``stages/s7_renderer.py``：三级增益优先（显式 > 内联档 > 整句级档）与参数透传。

全部离线：纯函数 + 临时目录 + 假 ffmpeg。
"""

from __future__ import annotations

import inspect
import io
import math
import re
import struct
import wave
from pathlib import Path

import pytest
from PIL import Image

from core.config import AppConfig
from core.schemas import (
    AudioClipMeta,
    CameraMotion,
    EpisodeDigest,
    InlineSfxOffset,
    NarrationScript,
    PanelMeta,
    RenderTimeline,
    ScriptBeat,
    SfxEvent,
)
from stages import s6_timeline, s7_renderer
from stages.s6_timeline import build_timeline, select_sfx_events

_VOLUME_RE = re.compile(r"volume=(-?[\d.]+)dB")


# ---------------------------------------------------------------------------
# 造数工具
# ---------------------------------------------------------------------------
def _tone_wav_bytes(seconds: float, rate: int = 44100, freq: float = 440.0) -> bytes:
    """造一段带正弦波的真实 wav（供 ffmpeg 侧测量响度用）。"""
    total = max(1, int(rate * seconds))
    frames = bytearray()
    for index in range(total):
        value = 0.3 * math.sin(2 * math.pi * freq * index / rate)
        frames += struct.pack("<h", int(value * 32767))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return buffer.getvalue()


def _beat(
    beat_id: str,
    narration: str,
    *,
    sfx_cue: str | None = None,
    sfx_position: str = "start",
) -> ScriptBeat:
    return ScriptBeat(
        beat_id=beat_id,
        narration_text=narration,
        tts_text=None,
        target_panel_ids=["p_001"],
        motion=CameraMotion.STATIC,
        sfx_cue=sfx_cue,
        sfx_position=sfx_position,  # type: ignore[arg-type]
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
        hook_title="分档增益测试",
        beats=beats,
        episode_digest=EpisodeDigest(
            episode_no=1, one_line_hook="钩子", summary="摘要"
        ),
    )


def _timeline(
    beats: list[ScriptBeat], audios: list[AudioClipMeta], panels: list[PanelMeta]
) -> RenderTimeline:
    return build_timeline(
        episode_id="ep_001",
        script=_script(beats),
        audios=audios,
        panels=panels,
    )


# ---------------------------------------------------------------------------
# 1. 数据模型：SfxEvent.source
# ---------------------------------------------------------------------------
def test_sfx_event_source_defaults_to_beat() -> None:
    """默认来源是整句级——旧代码造出来的事件不需要改就能用。"""
    assert SfxEvent(sfx_key="heart_thump", start_ms=0).source == "beat"


def test_sfx_event_source_can_be_inline() -> None:
    """内联来源可显式标记。"""
    event = SfxEvent(sfx_key="slap", start_ms=600, source="inline")
    assert event.source == "inline"


def test_legacy_timeline_json_without_source_loads_as_beat() -> None:
    """旧 timeline.json 没有 source 字段 → 按整句级处理（向后兼容）。"""
    timeline = RenderTimeline.model_validate(
        {
            "episode_id": "ep_001",
            "total_duration_ms": 1000,
            "scenes": [],
            "audios": [],
            "subtitles": [],
            "sfx_events": [{"sfx_key": "heart_thump", "start_ms": 3000}],
        }
    )
    assert timeline.sfx_events[0].source == "beat"


# ---------------------------------------------------------------------------
# 2. Stage 6：select_sfx_events 的来源标记与内联优先
# ---------------------------------------------------------------------------
def test_select_sfx_events_keeps_legacy_beat_throttle() -> None:
    """纯整句级的行为必须和 v4.31 一致：6000ms 节流。"""
    events = select_sfx_events(
        [(0, "heart_thump"), (3000, "door_slam"), (6000, "whoosh"), (9000, "gasp")]
    )
    assert [event.start_ms for event in events] == [0, 6000]
    assert [event.sfx_key for event in events] == ["heart_thump", "whoosh"]
    assert [event.source for event in events] == ["beat", "beat"]


def test_select_sfx_events_keeps_legacy_max_events() -> None:
    """纯整句级仍然守 8 条上限。"""
    events = select_sfx_events([(index * 6000, f"key_{index}") for index in range(20)])
    assert len(events) == 8


def test_select_sfx_events_two_tuple_candidates_default_to_beat() -> None:
    """二元组候选（历史调用方式）一律按整句级处理。"""
    events = select_sfx_events([(0, "heart_thump"), (6000, "whoosh")])
    assert [event.source for event in events] == ["beat", "beat"]


def test_select_sfx_events_skips_empty_keys() -> None:
    """空 key 与 'none' 仍然跳过。"""
    events = select_sfx_events([(0, ""), (6000, "none"), (12000, "whoosh")])
    assert [event.sfx_key for event in events] == ["whoosh"]


def test_inline_sfx_survives_when_beat_queue_is_full() -> None:
    """内联音效不再被整句级队列挤掉（v4.41 修掉的连带缺陷）。

    历史实现把两种来源放进同一条队列：内联落点（3000ms）比前一条整句级
    （0ms）只晚 3 秒，被 6000ms 节流直接丢弃；而 8 条上限一旦占满，
    内联更是整条没机会进成片。
    """
    beats = [(index * 6000, f"key_{index}") for index in range(8)]
    events = select_sfx_events([*beats, (3000, "slap", "inline")])

    assert len(events) == 8, "内联与整句级共享「全话 8 条」总额度"
    inline = [event for event in events if event.source == "inline"]
    assert [event.sfx_key for event in inline] == ["slap"]
    assert inline[0].start_ms == 3000


def test_inline_sfx_uses_its_own_gap_threshold() -> None:
    """内联之间用 1200ms 间隔，不与整句级共用 6000ms。"""
    events = select_sfx_events(
        [(0, "a", "inline"), (500, "b", "inline"), (1500, "c", "inline")]
    )
    assert [event.sfx_key for event in events] == ["a", "c"]


def test_beat_sfx_yields_to_nearby_inline_sfx() -> None:
    """整句级音效落在内联附近时让位——内联是显式编排的点睛，优先级更高。"""
    events = select_sfx_events(
        [(0, "crash", "beat"), (500, "slap", "inline"), (30000, "door_slam", "beat")]
    )
    by_key = {event.sfx_key: event.source for event in events}
    assert by_key.get("slap") == "inline"
    assert "crash" not in by_key, "距离内联 500ms < 2000ms，应该让位"
    assert by_key.get("door_slam") == "beat"


def test_select_sfx_events_returns_events_sorted_by_time() -> None:
    """输出按时间升序（内联先选不代表先输出）。"""
    events = select_sfx_events(
        [(30000, "late", "beat"), (500, "early", "inline"), (12000, "mid", "beat")]
    )
    assert [event.start_ms for event in events] == sorted(
        event.start_ms for event in events
    )


# ---------------------------------------------------------------------------
# 3. Stage 6：build_timeline 给事件打上来源
# ---------------------------------------------------------------------------
def test_timeline_marks_inline_events_with_inline_source(tmp_path: Path) -> None:
    """内联音效产生的事件 source='inline'。"""
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
    assert timeline.sfx_events[0].sfx_key == "slap"
    assert timeline.sfx_events[0].source == "inline"


def test_timeline_marks_beat_events_with_beat_source(tmp_path: Path) -> None:
    """整句级音效产生的事件 source='beat'。"""
    panels = [_panel("p_001", tmp_path / "02_panels")]
    beats = [_beat("b1", "一句解说。", sfx_cue="crash")]
    audios = [
        AudioClipMeta(
            beat_id="b1",
            audio_path="05_audio/b1.wav",
            duration_ms=2000,
            pause_after_ms=0,
        )
    ]
    timeline = _timeline(beats, audios, panels)
    assert len(timeline.sfx_events) == 1
    assert timeline.sfx_events[0].sfx_key == "crash"
    assert timeline.sfx_events[0].source == "beat"


# ---------------------------------------------------------------------------
# 4. Stage 7：三级增益分档
# ---------------------------------------------------------------------------
class _RecordingFFmpeg:
    """替换 ``s7_renderer.run_ffmpeg``：记录每次命令并造占位产物。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: object) -> str:
        self.calls.append(list(args))
        target = Path(args[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake")
        return ""

    @property
    def joined(self) -> str:
        return " ".join(" ".join(call) for call in self.calls)


def _make_timeline(root: Path, *, sfx_events: list[SfxEvent]) -> tuple[Path, RenderTimeline]:
    episode_dir = root / "episodes" / "ep_001"
    (episode_dir / "05_audio").mkdir(parents=True, exist_ok=True)
    (episode_dir / "05_audio" / "a1.wav").write_bytes(_tone_wav_bytes(1.0, freq=300.0))
    timeline = RenderTimeline(
        episode_id="ep_001",
        total_duration_ms=2000,
        scenes=[],
        audios=[
            AudioClipMeta(
                beat_id="b1", audio_path="a1.wav", duration_ms=900, pause_after_ms=100
            )
        ],
        subtitles=[],
        sfx_events=sfx_events,
    )
    return episode_dir, timeline


def _prepare_sfx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sfx_events: list[SfxEvent],
    inline_sfx_gain_db: float | None = None,
) -> _RecordingFFmpeg:
    recorder = _RecordingFFmpeg()
    monkeypatch.setattr(s7_renderer, "run_ffmpeg", recorder)
    monkeypatch.setattr(s7_renderer, "probe_duration_ms", lambda *a, **k: 500)

    episode_dir, timeline = _make_timeline(tmp_path, sfx_events=sfx_events)
    assets = tmp_path / "assets"
    (assets / "sfx").mkdir(parents=True, exist_ok=True)
    (assets / "sfx" / "heart_thump.wav").write_bytes(_tone_wav_bytes(0.5))

    extra: dict[str, object] = {}
    if inline_sfx_gain_db is not None:
        extra["inline_sfx_gain_db"] = inline_sfx_gain_db

    s7_renderer._prepare_audio(
        timeline,
        episode_dir=episode_dir,
        work_dir=tmp_path / "work",
        total_s=2.0,
        bgm_path=None,
        bgm_gain_db=-8.0,
        ffmpeg_bin="ffmpeg",
        sfx_events=sfx_events,
        assets_dir=assets,
        **extra,
    )
    return recorder


def _sfx_gain_db(joined: str, marker: str) -> float:
    """取出某条音效链上的**最终增益**（链尾那一个 volume=）。"""
    for part in joined.split(";"):
        if marker in part:
            volumes = _VOLUME_RE.findall(part)
            assert volumes, f"音效链里没有 volume：{part}"
            return float(volumes[-1])
    raise AssertionError(f"没找到音效链：{marker}\n{joined}")


def test_inline_event_uses_lighter_inline_gain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """内联事件走内联档（-6.0dB），比整句级（-2.0dB）更轻。"""
    recorder = _prepare_sfx(
        tmp_path,
        monkeypatch,
        sfx_events=[
            SfxEvent(sfx_key="heart_thump", start_ms=200, source="inline")
        ],
    )
    assert _sfx_gain_db(recorder.joined, "adelay=200|200") == -6.0


def test_beat_event_keeps_configured_gain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整句级事件仍然走渲染配置 sfx_gain_db（-2.0dB）。"""
    recorder = _prepare_sfx(
        tmp_path,
        monkeypatch,
        sfx_events=[
            SfxEvent(sfx_key="heart_thump", start_ms=200, source="beat")
        ],
    )
    assert _sfx_gain_db(recorder.joined, "adelay=200|200") == -2.0


def test_explicit_event_gain_beats_inline_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """事件显式写死 gain_db 时最高优先（连内联事件也不例外）。"""
    recorder = _prepare_sfx(
        tmp_path,
        monkeypatch,
        sfx_events=[
            SfxEvent(
                sfx_key="heart_thump",
                start_ms=200,
                gain_db=-9.0,
                source="inline",
            )
        ],
    )
    assert _sfx_gain_db(recorder.joined, "adelay=200|200") == -9.0


def test_inline_gain_parameter_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """渲染层可以把内联档再调轻。"""
    recorder = _prepare_sfx(
        tmp_path,
        monkeypatch,
        sfx_events=[
            SfxEvent(sfx_key="heart_thump", start_ms=200, source="inline")
        ],
        inline_sfx_gain_db=-12.0,
    )
    assert _sfx_gain_db(recorder.joined, "adelay=200|200") == -12.0


def test_render_timeline_exposes_inline_gain_parameter() -> None:
    """``render_timeline`` / ``_prepare_audio`` 都必须带内联增益参数。"""
    assert "inline_sfx_gain_db" in inspect.signature(
        s7_renderer.render_timeline
    ).parameters
    assert "inline_sfx_gain_db" in inspect.signature(
        s7_renderer._prepare_audio
    ).parameters


def test_run_stage7_wires_render_config_into_renderer() -> None:
    """Stage 7 必须把渲染配置里的内联增益档传下去。"""
    source = inspect.getsource(s7_renderer.render_episode)
    assert "inline_sfx_gain_db=settings.render.inline_sfx_gain_db" in source


def test_mix_log_mentions_inline_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """混音日志要能一眼看出有内联音效被降档。"""
    _prepare_sfx(
        tmp_path,
        monkeypatch,
        sfx_events=[
            SfxEvent(sfx_key="heart_thump", start_ms=200, source="inline")
        ],
    )
    out = capsys.readouterr().out
    assert "内联 1 条" in out


# ---------------------------------------------------------------------------
# 5. 配置
# ---------------------------------------------------------------------------
def test_render_config_has_inline_gain_default() -> None:
    """内联档默认 -6.0dB，比整句级 -2.0dB 更轻。"""
    config = AppConfig()
    assert config.render.sfx_gain_db == -2.0
    assert config.render.inline_sfx_gain_db == -6.0
    assert config.render.inline_sfx_gain_db < config.render.sfx_gain_db
    assert s7_renderer.DEFAULT_INLINE_SFX_GAIN_DB == -6.0


def test_config_yaml_declares_inline_gain() -> None:
    """backend/config.yaml 的 render 段必须显式写出内联增益档。"""
    text = (Path(__file__).resolve().parents[1] / "config.yaml").read_text(
        encoding="utf-8"
    )
    assert "inline_sfx_gain_db" in text


def test_inline_gap_constants_are_exported() -> None:
    """两条内联相关常量要进 __all__（便于测试与外部引用）。"""
    assert s6_timeline.SFX_INLINE_MIN_GAP_MS == 1200
    assert s6_timeline.SFX_INLINE_CROSS_GAP_MS == 2000
    assert "SFX_INLINE_MIN_GAP_MS" in s6_timeline.__all__
    assert "SFX_INLINE_CROSS_GAP_MS" in s6_timeline.__all__
