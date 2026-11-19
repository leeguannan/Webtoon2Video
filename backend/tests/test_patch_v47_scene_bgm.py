"""Patch v4.7：按幕 AI 配乐建议 + 曲库管理 + Stage 7 多幕 BGM 平滑混音。

五组验收：

1. ``suggest_scene_bgm`` 关键词打分/回退、``suggest_storyboard_bgm`` 人工字段保护、
   ``enrich_storyboard_with_vision`` 对 VLM【配乐情绪】/【配乐建议】两行的提取与剥离；
2. ``resolve_scene_bgm_path`` 四分支（显式静音 / 显式文件作品优先 / mood 同名 / 回退默认）；
3. ``build_scene_bgm_spans`` 的拆幕、同曲合并与封页/结尾时间扩展；
4. ``synthesize_multi_bgm_wav`` 真 FFmpeg 合成：时长精确、各轨能量落点正确、静音区干净；
5. Web API：曲库上传/列表/试听/删除、PUT storyboard 的 bgm* 继承、suggest-bgm 补全。
"""

from __future__ import annotations

import base64
import math
import shutil
import struct
import wave
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.config import AppConfig, SeriesConfig, WorkspaceConfig
from core.schemas import (
    CameraMotion,
    EpisodeStoryboard,
    RenderTimeline,
    StoryboardScene,
    TimelineScene,
    VisionWindowOutput,
    save_model,
)
from stages import s0_catalog, s3b_scenes, s7_renderer
from web.server import create_app

FFMPEG_BIN = "ffmpeg"
needs_ffmpeg = pytest.mark.skipif(
    shutil.which(FFMPEG_BIN) is None, reason="本机未安装 ffmpeg，跳过真实合成测试"
)


# ---------------------------------------------------------------------------
# 公共装置
# ---------------------------------------------------------------------------
def _write_sine_wav(path: Path, *, frequency: float = 440.0, seconds: float = 1.0) -> Path:
    """写一条 44.1kHz 立体声正弦波 WAV（测试素材，不依赖外部音频）。"""
    rate = 44100
    frames = int(rate * seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        for index in range(frames):
            sample = int(0.25 * 32767 * math.sin(2 * math.pi * frequency * index / rate))
            wav.writeframes(struct.pack("<hh", sample, sample))
    return path


def _wav_base64(path: Path) -> str:
    return "data:audio/wav;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _scene(
    scene_id: str,
    index: int,
    panel_ids: list[str],
    *,
    title: str = "",
    location: str = "",
    visual_recap: str = "",
    user_note: str = "",
    **bgm_patch: Any,
) -> StoryboardScene:
    payload: dict[str, Any] = {
        "scene_id": scene_id,
        "scene_index": index,
        "title": title,
        "location": location,
        "panel_ids": panel_ids,
        "visual_recap": visual_recap,
        "user_note": user_note,
    }
    payload.update(bgm_patch)
    return StoryboardScene(**payload)


def _rms_at(wav_path: Path, at_s: float, window_s: float = 0.4) -> float:
    with wave.open(str(wav_path), "rb") as wav:
        rate = wav.getframerate()
        wav.setpos(int(at_s * rate))
        raw = wav.readframes(int(window_s * rate))
    values = struct.unpack("<%dh" % (len(raw) // 2), raw)
    if not values:
        return 0.0
    return math.sqrt(sum(value * value for value in values) / len(values))


# ---------------------------------------------------------------------------
# 组 1：AI 配乐建议（关键词打分 / 人工保护 / VLM 行提取）
# ---------------------------------------------------------------------------
def test_suggest_scene_bgm_keyword_scoring_and_fallback() -> None:
    sad_scene = _scene(
        "sc_001", 1, ["p_001"],
        title="病房告别", visual_recap="人物落泪哭泣，回忆温暖过往，钢琴声起。",
    )
    mood, suggestion = s3b_scenes.suggest_scene_bgm(sad_scene)
    assert mood == "sad_piano"
    assert isinstance(suggestion, str) and suggestion

    conflict_scene = _scene(
        "sc_002", 1, ["p_002"],
        title="正面冲突", visual_recap="两人争吵打斗，紧张对峙，弦乐拨奏加剧冲突。",
    )
    mood, _ = s3b_scenes.suggest_scene_bgm(conflict_scene)
    assert mood == "conflict_tension"

    # 无任何关键词命中时稳定回退默认档，同输入同输出。
    blank_scene = _scene("sc_003", 1, ["p_003"], title="普通场景")
    mood_a, suggestion_a = s3b_scenes.suggest_scene_bgm(blank_scene)
    mood_b, suggestion_b = s3b_scenes.suggest_scene_bgm(blank_scene)
    assert mood_a == mood_b == "suspense_lurk"
    assert suggestion_a == suggestion_b
    assert set(s3b_scenes.BGM_MOOD_PRESETS) == {
        "sad_piano",
        "conflict_tension",
        "warm_intimate",
        "suspense_lurk",
        "thriller_climax",
    }


def test_suggest_storyboard_bgm_preserves_manual_fields() -> None:
    storyboard = EpisodeStoryboard(
        episode_id="wt_test_ep_001",
        source="human",
        scenes=[
            _scene(
                "sc_001", 1, ["p_001"], title="病房告别",
                bgm_mood="sad_piano", bgm_suggestion="人工保留的建议",
                bgm="custom.wav", bgm_gain_db=-12.5,
            ),
            _scene("sc_002", 2, ["p_002"], title="突发惊吓", visual_recap="惊悚真相揭露。"),
        ],
    )
    result = s3b_scenes.suggest_storyboard_bgm(storyboard)
    first, second = result.scenes
    # 已有合法 mood 的幕不重算，人工 bgm / gain / suggestion 永不覆盖。
    assert first.bgm_mood == "sad_piano"
    assert first.bgm_suggestion == "人工保留的建议"
    assert first.bgm == "custom.wav"
    assert first.bgm_gain_db == -12.5
    # 空幕被补全为五档之一。
    assert second.bgm_mood in s3b_scenes.BGM_MOOD_PRESETS
    assert second.bgm is None and second.bgm_gain_db is None

    # overwrite=True 重算 mood，但人工曲目字段依旧不动。
    forced = s3b_scenes.suggest_storyboard_bgm(storyboard, overwrite=True)
    assert forced.scenes[0].bgm == "custom.wav"
    assert forced.scenes[0].bgm_gain_db == -12.5


class _FakeChatResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLM:
    """只实现 enrich 用到的 async chat。"""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls = 0

    async def chat(self, messages: Any, *, model: str = "", **kwargs: Any) -> _FakeChatResponse:
        self.calls += 1
        return _FakeChatResponse(self._content)


@pytest.mark.anyio
async def test_enrich_extracts_bgm_lines_and_strips_recap(tmp_path: Path) -> None:
    panels_dir = tmp_path / "02_panels"
    panels_dir.mkdir(parents=True)
    for panel_id in ("p_001", "p_002"):
        Image.new("RGB", (360, 520), (60, 90, 140)).save(panels_dir / f"{panel_id}.png")

    storyboard = EpisodeStoryboard(
        episode_id="wt_test_ep_001",
        source="auto",
        scenes=[_scene("sc_001", 1, ["p_001", "p_002"], title="病房告别")],
    )
    vision = VisionWindowOutput.model_validate(
        {
            "panels": [
                {
                    "panel_id": "p_001",
                    "scene_summary": "病房里两人沉默。",
                    "dialogues": [],
                    "sfx_list": [],
                    "is_transition_blank": False,
                    "is_pure_text_card": False,
                    "non_story_type": "none",
                }
            ]
        }
    )
    fake_client = _FakeLLM(
        "这是病房告别场景，人物落泪，情绪克制。\n"
        "【配乐情绪】sad_piano\n"
        "【配乐建议】60BPM 钢琴与大提琴铺底，sad solo piano cello ambient"
    )
    result = await s3b_scenes.enrich_storyboard_with_vision(
        fake_client,
        storyboard=storyboard,
        panels_dir=panels_dir,
        model="test-model",
        vision=vision,
    )
    scene = result.scenes[0]
    assert scene.bgm_mood == "sad_piano"
    assert "solo piano" in scene.bgm_suggestion
    # 配乐两行必须从 visual_recap 剥离干净，不污染 Stage 4 文案。
    assert "配乐" not in scene.visual_recap
    assert "病房告别" in scene.visual_recap
    assert fake_client.calls == 1

    # VLM 给了非法情绪档时本地兜底，不会写入脏 mood。
    bad_client = _FakeLLM("一段普通复核。\n【配乐情绪】not_a_mood\n【配乐建议】随便")
    storyboard_bad = EpisodeStoryboard(
        episode_id="wt_test_ep_002",
        source="auto",
        scenes=[_scene("sc_001", 1, ["p_001"], title="病房告别")],
    )
    bad_result = await s3b_scenes.enrich_storyboard_with_vision(
        bad_client,
        storyboard=storyboard_bad,
        panels_dir=panels_dir,
        model="test-model",
        vision=vision,
    )
    assert bad_result.scenes[0].bgm_mood in s3b_scenes.BGM_MOOD_PRESETS


# ---------------------------------------------------------------------------
# 组 2：resolve_scene_bgm_path 四分支
# ---------------------------------------------------------------------------
def test_resolve_scene_bgm_path_branches(tmp_path: Path) -> None:
    series_dir = tmp_path / "wt_series"
    assets_dir = tmp_path / "global_assets"
    series_bgm = series_dir / "assets" / "bgm"
    global_bgm = assets_dir / "bgm"
    series_bgm.mkdir(parents=True)
    global_bgm.mkdir(parents=True)
    # 同名曲目作品级与全局各一份（内容不同用于验证优先级）。
    (series_bgm / "theme.wav").write_bytes(b"series-version")
    (global_bgm / "theme.wav").write_bytes(b"global-version")
    (global_bgm / "emotional.wav").write_bytes(b"global-default")
    (series_bgm / "sad_piano.wav").write_bytes(b"mood-file")

    settings = AppConfig(workspace=WorkspaceConfig(root=tmp_path / "workspace"))
    series_config = SeriesConfig(bgm="emotional")
    default_gain = settings.render.bgm_gain_db

    # 分支 1：显式静音 → (None, gain)，gain 取幕级覆盖值。
    silent = _scene("sc_001", 1, ["p_001"], bgm="none", bgm_gain_db=-14.0)
    path, gain = s7_renderer.resolve_scene_bgm_path(
        silent, series_dir=series_dir, series_config=series_config,
        settings=settings, assets_dir=assets_dir,
    )
    assert path is None
    assert gain == -14.0

    # "silent" 与 "none" 等价。
    silent_alt = _scene("sc_002", 1, ["p_002"], bgm="silent")
    path, _ = s7_renderer.resolve_scene_bgm_path(
        silent_alt, series_dir=series_dir, series_config=series_config,
        settings=settings, assets_dir=assets_dir,
    )
    assert path is None

    # 分支 2：显式文件名 → 作品曲库优先于全局。
    explicit = _scene("sc_003", 1, ["p_003"], bgm="theme.wav")
    path, gain = s7_renderer.resolve_scene_bgm_path(
        explicit, series_dir=series_dir, series_config=series_config,
        settings=settings, assets_dir=assets_dir,
    )
    assert path == series_bgm / "theme.wav"
    assert path.read_bytes() == b"series-version"
    assert gain == default_gain  # 未指定幕级增益时回退作品默认

    # 作品曲库没有时回退全局。
    global_only = _scene("sc_004", 1, ["p_004"], bgm="emotional.wav")
    path, _ = s7_renderer.resolve_scene_bgm_path(
        global_only, series_dir=series_dir, series_config=series_config,
        settings=settings, assets_dir=assets_dir,
    )
    assert path == global_bgm / "emotional.wav"

    # 分支 3：自动选择 + mood 同名文件命中。
    mood_scene = _scene("sc_005", 1, ["p_005"], bgm_mood="sad_piano")
    path, _ = s7_renderer.resolve_scene_bgm_path(
        mood_scene, series_dir=series_dir, series_config=series_config,
        settings=settings, assets_dir=assets_dir,
    )
    assert path == series_bgm / "sad_piano.wav"

    # 分支 4：mood 无同名文件 → 回退作品配置 bgm（解析到全局 emotional）。
    fallback_scene = _scene("sc_006", 1, ["p_006"], bgm_mood="thriller_climax")
    path, _ = s7_renderer.resolve_scene_bgm_path(
        fallback_scene, series_dir=series_dir, series_config=series_config,
        settings=settings, assets_dir=assets_dir,
    )
    assert path == global_bgm / "emotional.wav"


# ---------------------------------------------------------------------------
# 组 3：build_scene_bgm_spans 拆幕 / 合并 / 封页结尾扩展
# ---------------------------------------------------------------------------
def _timeline_with_panels(total_ms: int, panel_ids: list[str], step_ms: int) -> RenderTimeline:
    scenes = [
        TimelineScene(
            scene_index=index,
            beat_id=f"b_{index + 1:03d}",
            panel_id=panel_id,
            image_path=f"02_panels/{panel_id}.png",
            start_ms=index * step_ms,
            end_ms=(index + 1) * step_ms,
            duration_ms=step_ms,
            motion=CameraMotion.STATIC,
        )
        for index, panel_id in enumerate(panel_ids)
    ]
    return RenderTimeline(
        episode_id="wt_test_ep_001",
        total_duration_ms=total_ms,
        scenes=scenes,
        audios=[],
        subtitles=[],
    )


def test_build_scene_bgm_spans_split_merge_and_branding_extension(tmp_path: Path) -> None:
    series_dir = tmp_path / "wt_series"
    assets_dir = tmp_path / "global_assets"
    series_bgm = series_dir / "assets" / "bgm"
    series_bgm.mkdir(parents=True)
    # 占位音频（resolve 只校验存在性）。
    (series_bgm / "theme.wav").write_bytes(b"a")
    (series_bgm / "thriller.wav").write_bytes(b"b")

    settings = AppConfig(workspace=WorkspaceConfig(root=tmp_path / "workspace"))
    series_config = SeriesConfig()
    storyboard = EpisodeStoryboard(
        episode_id="wt_test_ep_001",
        source="human",
        scenes=[
            _scene("sc_001", 1, ["p_001", "p_002"], bgm="theme.wav"),
            _scene("sc_002", 2, ["p_003", "p_004"], bgm="theme.wav"),  # 同曲同增益 → 合并
            _scene("sc_003", 3, ["p_005", "p_006"], bgm="thriller.wav", bgm_gain_db=-12.0),
        ],
    )
    timeline = _timeline_with_panels(6000, [f"p_{i:03d}" for i in range(1, 7)], 1000)

    spans = s7_renderer.build_scene_bgm_spans(
        storyboard,
        timeline,
        series_dir=series_dir,
        series_config=series_config,
        settings=settings,
        assets_dir=assets_dir,
        cover_duration_ms=350,
        outro_duration_ms=600,
    )
    # 幕 1/2 同曲合并 → 只剩两段。
    assert len(spans) == 2
    first, second = spans
    # 首段向前含封页（负起点），合并后覆盖正文 0~4000ms。
    assert first.start_ms == -350
    assert first.end_ms == 4000
    assert first.bgm_path == series_bgm / "theme.wav"
    # 末段向后含结尾时长。
    assert second.start_ms == 4000
    assert second.end_ms == 6600
    assert second.bgm_path == series_bgm / "thriller.wav"
    assert second.gain_db == -12.0

    # 无封页/结尾时严格夹在正文 [0, total] 内。
    plain = s7_renderer.build_scene_bgm_spans(
        storyboard,
        timeline,
        series_dir=series_dir,
        series_config=series_config,
        settings=settings,
        assets_dir=assets_dir,
    )
    assert plain[0].start_ms == 0
    assert plain[-1].end_ms == 6000


def test_build_scene_bgm_spans_silent_scene_kept(tmp_path: Path) -> None:
    series_dir = tmp_path / "wt_series"
    settings = AppConfig(workspace=WorkspaceConfig(root=tmp_path / "workspace"))
    storyboard = EpisodeStoryboard(
        episode_id="wt_test_ep_001",
        source="human",
        scenes=[
            _scene("sc_001", 1, ["p_001", "p_002"], bgm="theme.wav"),
            _scene("sc_002", 2, ["p_003", "p_004"], bgm="none"),
        ],
    )
    (series_dir / "assets" / "bgm").mkdir(parents=True)
    (series_dir / "assets" / "bgm" / "theme.wav").write_bytes(b"a")
    timeline = _timeline_with_panels(4000, [f"p_{i:03d}" for i in range(1, 5)], 1000)
    spans = s7_renderer.build_scene_bgm_spans(
        storyboard,
        timeline,
        series_dir=series_dir,
        series_config=SeriesConfig(),
        settings=settings,
    )
    assert len(spans) == 2
    assert spans[0].bgm_path is not None
    assert spans[1].bgm_path is None  # 静音幕保留为 None 段，混音时不出轨


# ---------------------------------------------------------------------------
# 组 4：真 FFmpeg 多轨合成
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_synthesize_multi_bgm_wav_timeline(tmp_path: Path) -> None:
    track_a = _write_sine_wav(tmp_path / "a.wav", frequency=440.0)
    track_b = _write_sine_wav(tmp_path / "b.wav", frequency=880.0)
    spans = [
        s7_renderer.BgmSpan(0, 3000, track_a, -8.0),
        s7_renderer.BgmSpan(3000, 5000, None, -8.0),
        s7_renderer.BgmSpan(5000, 8000, track_b, -8.0),
    ]
    out = s7_renderer.synthesize_multi_bgm_wav(
        spans,
        total_s=8.0,
        out_path=tmp_path / "multi_bgm.wav",
        ffmpeg_bin=FFMPEG_BIN,
    )
    assert out is not None and out.is_file()
    with wave.open(str(out), "rb") as wav:
        duration_s = wav.getnframes() / wav.getframerate()
        assert wav.getframerate() == 44100
        assert wav.getnchannels() == 2
    assert abs(duration_s - 8.0) < 0.05
    # A 轨区间有能量、中段静音、B 轨区间（历史 bug：loudnorm 导致 adelay 丢样本）必须有声。
    assert _rms_at(out, 1.5) > 500
    assert _rms_at(out, 4.0) < 50
    assert _rms_at(out, 6.5) > 500


@needs_ffmpeg
def test_synthesize_returns_none_when_all_silent(tmp_path: Path) -> None:
    spans = [s7_renderer.BgmSpan(0, 2000, None, -8.0)]
    result = s7_renderer.synthesize_multi_bgm_wav(
        spans,
        total_s=2.0,
        out_path=tmp_path / "silent.wav",
        ffmpeg_bin=FFMPEG_BIN,
    )
    assert result is None
    assert not (tmp_path / "silent.wav").exists()


# ---------------------------------------------------------------------------
# 组 5：Web API
# ---------------------------------------------------------------------------
@pytest.fixture()
def workspace(tmp_path: Path) -> tuple[Path, str]:
    """登记作品 + 铺 vision.json / 面板图，返回 (工作区根, series_id)。"""
    source = tmp_path / "src" / "ep_001"
    source.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 600), (40, 80, 120)).save(source / "001.png")
    catalog = s0_catalog.import_local_catalog(tmp_path / "src")
    s0_catalog.CatalogRegistry(tmp_path).register(catalog)
    series_id = catalog.series_id

    episode_dir = tmp_path / series_id / "episodes" / "ep_001"
    vision_dir = episode_dir / "03_vision"
    vision_dir.mkdir(parents=True, exist_ok=True)
    panels = [
        {
            "panel_id": f"p_{index:03d}",
            "scene_summary": f"病房里两人对峙，第 {index} 格。",
            "dialogues": [
                {"speaker_guess": "小白", "text": f"{index} 号台词。", "emotion": "tense"}
            ],
            "sfx_list": [],
            "is_transition_blank": False,
            "is_pure_text_card": False,
            "non_story_type": "none",
        }
        for index in range(1, 7)
    ]
    save_model(VisionWindowOutput.model_validate({"panels": panels}), vision_dir / "vision.json")
    panels_dir = episode_dir / "02_panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    for index in range(1, 7):
        Image.new("RGB", (360, 520), (200, index * 12, 90)).save(
            panels_dir / f"p_{index:03d}.png"
        )
    return tmp_path, series_id


@pytest.fixture()
def client(workspace: tuple[Path, str]) -> TestClient:
    workspace_root, _ = workspace
    return TestClient(create_app(AppConfig(workspace=WorkspaceConfig(root=workspace_root))))


def test_bgm_library_list_upload_and_delete(
    client: TestClient, workspace: tuple[Path, str], tmp_path: Path
) -> None:
    workspace_root, series_id = workspace
    library_base = f"/api/series/{series_id}/bgm-library"

    # 初始列表：五档情绪齐全，全局曲库（仓库自带 emotional/suspense）可被列出。
    response = client.get(library_base)
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["moods"]) == 5
    assert {mood["mood"] for mood in payload["moods"]} == set(
        s3b_scenes.BGM_MOOD_PRESETS
    )
    global_tracks = [track for track in payload["tracks"] if track["scope"] == "global"]
    assert any(track["filename"].startswith("emotional") for track in global_tracks)

    # 上传作品曲目并绑定 sad_piano 情绪档 → 落盘名为 sad_piano.<suffix>。
    wav_path = _write_sine_wav(tmp_path / "upload.wav", frequency=523.0, seconds=0.5)
    upload_res = client.post(
        f"{library_base}/upload",
        json={
            "filename": "my_track.wav",
            "content_base64": _wav_base64(wav_path),
            "mood_slot": "sad_piano",
        },
    )
    assert upload_res.status_code == 200
    series_tracks = [
        track for track in upload_res.json()["tracks"] if track["scope"] == "series"
    ]
    assert any(track["filename"] == "sad_piano.wav" for track in series_tracks)
    saved = workspace_root / series_id / "assets" / "bgm" / "sad_piano.wav"
    assert saved.is_file()

    # 试听路由能流回同一文件。
    file_base = f"/api/series/{series_id}/bgm-file"
    stream_res = client.get(f"{file_base}/series/sad_piano.wav")
    assert stream_res.status_code == 200
    assert stream_res.content == saved.read_bytes()

    # 删除作品曲目。
    delete_res = client.delete(f"{library_base}/sad_piano.wav")
    assert delete_res.status_code == 200
    assert not saved.exists()
    assert all(
        track["filename"] != "sad_piano.wav"
        for track in delete_res.json()["tracks"]
        if track["scope"] == "series"
    )


def test_bgm_library_guards(
    client: TestClient, workspace: tuple[Path, str], tmp_path: Path
) -> None:
    workspace_root, series_id = workspace
    library_base = f"/api/series/{series_id}/bgm-library"

    file_base = f"/api/series/{series_id}/bgm-file"
    # 非法 scope。
    assert client.get(f"{file_base}/evil/x.wav").status_code == 400
    # 路径穿越被文件名归一化挡掉。
    traversal = client.get(f"{file_base}/series/..%2F..%2Fsecret.wav")
    assert traversal.status_code in (400, 404)
    # 全局曲目不允许经此接口删除。
    assert client.delete(f"{library_base}/emotional.mp3").status_code == 404
    # 非法后缀与超体积（30MB 以上）拒绝。
    bad_ext = client.post(
        f"{library_base}/upload",
        json={"filename": "x.txt", "content_base64": _wav_base64(
            _write_sine_wav(tmp_path / "x.wav")
        )},
    )
    assert bad_ext.status_code == 400
    oversized = base64.b64encode(b"0" * (30 * 1024 * 1024 + 1)).decode("ascii")
    too_big = client.post(
        f"{library_base}/upload",
        json={"filename": "big.wav", "content_base64": oversized},
    )
    assert too_big.status_code == 400
    # 非法情绪档。
    bad_mood = client.post(
        f"{library_base}/upload",
        json={
            "filename": "y.wav",
            "content_base64": _wav_base64(_write_sine_wav(tmp_path / "y.wav")),
            "mood_slot": "not_a_mood",
        },
    )
    assert bad_mood.status_code == 400


def test_put_storyboard_preserves_bgm_fields(
    client: TestClient, workspace: tuple[Path, str]
) -> None:
    _, series_id = workspace
    episode_id = f"{series_id}_ep_001"
    storyboard_url = f"/api/episodes/{episode_id}/storyboard"

    # GET 自动聚类出幕编排。
    first = client.get(storyboard_url)
    assert first.status_code == 200
    scenes = first.json()["scenes"]
    assert len(scenes) >= 2
    scenes[0]["bgm_mood"] = "sad_piano"
    scenes[0]["bgm_suggestion"] = "人工建议文本"
    scenes[0]["bgm"] = "none"
    scenes[0]["bgm_gain_db"] = -14.0

    put_res = client.put(storyboard_url, json={"scenes": scenes})
    assert put_res.status_code == 200
    saved_first = put_res.json()["scenes"][0]
    assert saved_first["bgm_mood"] == "sad_piano"
    assert saved_first["bgm"] == "none"
    assert saved_first["bgm_gain_db"] == -14.0

    # 第二次 PUT 故意不带任何 bgm* 键（模拟其它面板保存）：字段必须按 scene_id 继承。
    stripped = [
        {
            "scene_id": scene["scene_id"],
            "title": scene["title"],
            "location": scene["location"],
            "panel_ids": scene["panel_ids"],
        }
        for scene in scenes
    ]
    again = client.put(storyboard_url, json={"scenes": stripped})
    assert again.status_code == 200
    inherited = again.json()["scenes"][0]
    assert inherited["bgm_mood"] == "sad_piano"
    assert inherited["bgm_suggestion"] == "人工建议文本"
    assert inherited["bgm"] == "none"
    assert inherited["bgm_gain_db"] == -14.0


def test_suggest_bgm_endpoint_fills_scenes(
    client: TestClient, workspace: tuple[Path, str]
) -> None:
    _, series_id = workspace
    episode_id = f"{series_id}_ep_001"
    response = client.post(f"/api/episodes/{episode_id}/storyboard/suggest-bgm")
    assert response.status_code == 200
    payload = response.json()
    active_scenes = [scene for scene in payload["scenes"] if scene["enabled"]]
    assert active_scenes
    for scene in active_scenes:
        assert scene["bgm_mood"] in s3b_scenes.BGM_MOOD_PRESETS
        assert scene["bgm_suggestion"]
    # 已落盘，后续 GET 能读回同一结果。
    saved = client.get(f"/api/episodes/{episode_id}/storyboard").json()
    assert saved["scenes"][0]["bgm_mood"] == payload["scenes"][0]["bgm_mood"]


def test_unknown_series_and_episode_404(client: TestClient, workspace: tuple[Path, str]) -> None:
    _, series_id = workspace
    assert client.get("/api/series/wt_not_exist/bgm-library").status_code == 404
    assert client.post(
        f"/api/episodes/{series_id}_ep_999/storyboard/suggest-bgm"
    ).status_code == 404
