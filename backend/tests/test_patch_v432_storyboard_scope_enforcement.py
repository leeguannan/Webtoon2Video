"""PATCH v4.32：镜头编排范围强制收口（成片不再出现用户没选过的画面）。

**用户报障**：视频里出现了一帧「我在分镜里从没选过」的画面，而且同样的问题
已经复发第二次。取证结论（workspace/wt_3924/episodes/ep_001）：

* 成片 26 秒处显示的是 ``p_017``，而 timeline 说该处应是 ``p_014``；
* ``b4`` 的 ``target_panel_ids`` 是 ``["p_014", "p_017"]``，Stage 6 按格数均分
  时长轮播两格，**但审核台的分镜下拉只暴露第 1 格**（``target_panel_ids[0]``），
  第 2 格在审核台完全不可见 → 用户观感就是「这帧我没选过」；
* 更硬的一条：``b_new_142`` 指向 ``p_094``，而 ``p_094`` **不在任何启用幕的
  panel_ids 里**（用户从没在镜头编排页选过它）。这句是用户在审核台手工新增的
  （``b_new_*`` 只有前端 ``uniqueBeatId()`` 会生成），而保存链路
  ``PUT /api/episodes/{id}/script`` **完全不校验** target 是否在编排范围内，
  Stage 6 又原样渲染 → 成片里出现了用户没选过的画面。

本文件覆盖四层收口：

1. **canonical owner 模块** ``core/panel_scope.py``：启用幕范围是唯一权威；
2. **Stage 6 渲染层**：越界 target 不渲染；整句越界时回退到编排范围内**序位
   最近**的一格（而不是上一句的画面）；无编排文件时行为完全不变；
3. **后端写回层**：``PUT /script`` / ``auto-pacing`` / ``auto-motions`` 写回前
   收口，并把收口明细回传给前端；
4. **API 契约**：``panel_scope_violations`` 随响应返回；不破坏
   ``voice_override`` / ``pause_after_ms`` / 音频清单。

全部离线：纯函数 + 临时目录 + TestClient，不触网、不消耗百炼额度。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.config import AppConfig, WorkspaceConfig
from core.panel_scope import (
    PanelScope,
    clamp_beats_to_scope,
    load_panel_scope,
    scope_from_storyboard,
    storyboard_path_for,
    violations_payload,
)
from core.schemas import (
    AudioClipMeta,
    CameraMotion,
    EpisodeDigest,
    EpisodeStoryboard,
    NarrationScript,
    PanelMeta,
    ScriptBeat,
    StoryboardScene,
    load_model,
    save_model,
)
from stages import s6_timeline
from web.server import create_app

# ---------------------------------------------------------------------------
# 造数工具
# ---------------------------------------------------------------------------


def _storyboard(
    scenes: list[StoryboardScene], *, source: str = "human"
) -> EpisodeStoryboard:
    return EpisodeStoryboard(episode_id="ep_001", source=source, scenes=scenes)


def _scene(
    scene_id: str,
    panel_ids: list[str],
    *,
    index: int = 1,
    enabled: bool = True,
) -> StoryboardScene:
    return StoryboardScene(
        scene_id=scene_id,
        scene_index=index,
        title=f"第 {index} 幕",
        panel_ids=panel_ids,
        enabled=enabled,
    )


def _beat(
    beat_id: str,
    text: str,
    panel_ids: list[str],
    *,
    scene_id: str | None = None,
    motion: str = "static",
    pause: int = 200,
    voice_override: str = "",
) -> ScriptBeat:
    return ScriptBeat(
        beat_id=beat_id,
        narration_text=text,
        tts_text=text,
        target_panel_ids=panel_ids,
        motion=CameraMotion(motion),
        pause_after_ms=pause,
        scene_id=scene_id,
        voice_override=voice_override,
    )


def _panel(panel_id: str, image_dir: Path, width: int = 360, height: int = 640) -> PanelMeta:
    image_dir.mkdir(parents=True, exist_ok=True)
    path = image_dir / f"{panel_id}.png"
    Image.new("RGB", (width, height), (30, 60, 90)).save(path)
    return PanelMeta(
        panel_id=panel_id,
        image_path=str(path),
        width=width,
        height=height,
        aspect_ratio=width / height,
        source_slice_range=(0, height),
        is_tall_panel=False,
    )


def _audio(beat_id: str, duration_ms: int = 1000, pause_after_ms: int = 200) -> AudioClipMeta:
    return AudioClipMeta(
        beat_id=beat_id,
        audio_path=f"05_audio/{beat_id}.wav",
        duration_ms=duration_ms,
        pause_after_ms=pause_after_ms,
    )


def _script(beats: list[ScriptBeat]) -> NarrationScript:
    return NarrationScript(
        episode_id="ep_001",
        hook_title="编排范围测试",
        beats=beats,
        episode_digest=EpisodeDigest(episode_no=1, one_line_hook="钩子", summary="摘要"),
    )


# ---------------------------------------------------------------------------
# 1. canonical owner：启用幕范围是唯一权威
# ---------------------------------------------------------------------------


def test_scope_only_contains_enabled_scenes() -> None:
    """禁用的幕引用的分镜不在范围内。"""
    storyboard = _storyboard(
        [
            _scene("sc_001", ["p_001", "p_002"], index=1),
            _scene("sc_002", ["p_003"], index=2, enabled=False),
        ]
    )
    scope = scope_from_storyboard(storyboard)
    assert scope.allowed_panel_ids == frozenset({"p_001", "p_002"})
    assert scope.source == "storyboard"
    assert scope.allows("p_001")
    assert not scope.allows("p_003")


def test_scope_without_enabled_scene_is_empty_and_not_constraining() -> None:
    """没有启用幕 → 空范围 → 本层不约束（旧工程行为不变）。"""
    scope = scope_from_storyboard(_storyboard([_scene("sc_001", ["p_001"], enabled=False)]))
    assert scope.is_empty
    assert not scope
    assert scope_from_storyboard(None).is_empty


def test_load_panel_scope_returns_none_without_storyboard(tmp_path: Path) -> None:
    """没有编排文件 → None（不约束），损坏文件同样按不约束处理。"""
    assert load_panel_scope(tmp_path) is None

    broken = storyboard_path_for(tmp_path)
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{ 不是合法 JSON", encoding="utf-8")
    assert load_panel_scope(tmp_path) is None


def test_load_panel_scope_reads_enabled_scenes(tmp_path: Path) -> None:
    path = storyboard_path_for(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    save_model(
        _storyboard(
            [
                _scene("sc_001", ["p_001", "p_002"], index=1),
                _scene("sc_002", ["p_009"], index=2, enabled=False),
            ]
        ),
        path,
    )
    scope = load_panel_scope(tmp_path)
    assert scope is not None
    assert scope.allowed_panel_ids == frozenset({"p_001", "p_002"})


# ---------------------------------------------------------------------------
# 2. 收口：越界 target 剔除 / 整句越界时替换
# ---------------------------------------------------------------------------


def test_clamp_drops_out_of_scope_targets_but_keeps_valid_ones() -> None:
    """一半越界 → 只剔掉越界那一格，范围内的原样保留。"""
    scope = scope_from_storyboard(_storyboard([_scene("sc_001", ["p_001", "p_002"])]))
    beats = [_beat("b1", "这一句配两格。", ["p_001", "p_094"])]
    clamped, violations = clamp_beats_to_scope(beats, scope)
    assert clamped[0].target_panel_ids == ["p_001"]
    assert len(violations) == 1
    assert violations[0].beat_id == "b1"
    assert list(violations[0].removed) == ["p_094"]
    # 部分保留时不算「整体替换」。
    assert violations[0].replacement is None


def test_clamp_replaces_when_every_target_is_out_of_scope() -> None:
    """整句越界 → 换成编排范围内的一格，且 target_panel_ids 永远非空。"""
    scope = scope_from_storyboard(_storyboard([_scene("sc_001", ["p_001", "p_002"])]))
    beats = [_beat("b1", "这一句指向编排外的画面。", ["p_094"])]
    clamped, violations = clamp_beats_to_scope(beats, scope)
    assert len(clamped[0].target_panel_ids) == 1
    assert clamped[0].target_panel_ids[0] in scope.allowed_panel_ids
    assert violations[0].replacement == clamped[0].target_panel_ids[0]


def test_clamp_prefers_same_scene_panel_before_nearest_allowed() -> None:
    """整句越界时优先用「本句归属幕内」的分镜。"""
    scope = scope_from_storyboard(
        _storyboard(
            [
                _scene("sc_001", ["p_001"], index=1),
                _scene("sc_002", ["p_002"], index=2),
            ]
        )
    )
    beats = [_beat("b1", "归属第二幕却指向第一幕之外。", ["p_094"], scene_id="sc_002")]
    clamped, _ = clamp_beats_to_scope(beats, scope)
    assert clamped[0].target_panel_ids == ["p_002"]


def test_clamp_is_noop_without_scope() -> None:
    """没有编排 / 没有启用幕 → 原样返回，不改任何东西。"""
    beats = [_beat("b1", "任意分镜。", ["p_094"])]
    for scope in (None, PanelScope()):
        clamped, violations = clamp_beats_to_scope(beats, scope)
        assert clamped[0].target_panel_ids == ["p_094"]
        assert violations == []


def test_violations_payload_shape() -> None:
    """回传给前端的结构固定：beat_id / removed / replacement。"""
    scope = scope_from_storyboard(_storyboard([_scene("sc_001", ["p_001"])]))
    _clamped, violations = clamp_beats_to_scope([_beat("b1", "越界。", ["p_094"])], scope)
    payload = violations_payload(violations)
    assert payload == [
        {"beat_id": "b1", "removed": ["p_094"], "replacement": "p_001"}
    ]


# ---------------------------------------------------------------------------
# 3. Stage 6 渲染层收口
# ---------------------------------------------------------------------------


def test_build_timeline_drops_out_of_scope_panels(tmp_path: Path) -> None:
    """Stage 6 不再渲染编排范围外的分镜。"""
    panels = [
        _panel("p_001", tmp_path / "02_panels"),
        _panel("p_002", tmp_path / "02_panels"),
        _panel("p_094", tmp_path / "02_panels"),
    ]
    scope = scope_from_storyboard(_storyboard([_scene("sc_001", ["p_001", "p_002"])]))
    timeline = s6_timeline.build_timeline(
        episode_id="ep_001",
        script=_script([_beat("b1", "一句配两格。", ["p_001", "p_094"])]),
        audios=[_audio("b1")],
        panels=panels,
        panel_scope=scope,
    )
    assert [scene.panel_id for scene in timeline.scenes] == ["p_001"]


def test_build_timeline_falls_back_to_nearest_in_scope_panel(tmp_path: Path) -> None:
    """整句越界 → 回退到编排范围内序位最近的一格，而不是上一句的画面。"""
    panels = [
        _panel("p_001", tmp_path / "02_panels"),
        _panel("p_002", tmp_path / "02_panels"),
        _panel("p_050", tmp_path / "02_panels"),
    ]
    scope = scope_from_storyboard(_storyboard([_scene("sc_001", ["p_001", "p_002"])]))
    timeline = s6_timeline.build_timeline(
        episode_id="ep_001",
        script=_script([_beat("b1", "指向 p_050。", ["p_050"])]),
        audios=[_audio("b1")],
        panels=panels,
        panel_scope=scope,
    )
    # p_050 越界 → 就近换成 p_002（p_001 序位差 49，p_002 差 48）。
    assert [scene.panel_id for scene in timeline.scenes] == ["p_002"]


def test_build_timeline_falls_back_to_same_scene_panel_first(tmp_path: Path) -> None:
    """渲染层与写回层同口径：整句越界时优先用这句所属那一幕里的分镜。"""
    panels = [
        _panel("p_001", tmp_path / "02_panels"),
        _panel("p_002", tmp_path / "02_panels"),
        _panel("p_050", tmp_path / "02_panels"),
    ]
    scope = scope_from_storyboard(
        _storyboard(
            [
                _scene("sc_001", ["p_001"], index=1),
                _scene("sc_002", ["p_002"], index=2),
            ]
        )
    )
    timeline = s6_timeline.build_timeline(
        episode_id="ep_001",
        script=_script([_beat("b1", "归属第二幕。", ["p_050"], scene_id="sc_002")]),
        audios=[_audio("b1")],
        panels=panels,
        panel_scope=scope,
    )
    assert [scene.panel_id for scene in timeline.scenes] == ["p_002"]


def test_build_timeline_without_scope_keeps_previous_behaviour(tmp_path: Path) -> None:
    """没有编排范围时行为与 Patch v4.32 之前完全一致（向后兼容）。"""
    panels = [
        _panel("p_001", tmp_path / "02_panels"),
        _panel("p_094", tmp_path / "02_panels"),
    ]
    timeline = s6_timeline.build_timeline(
        episode_id="ep_001",
        script=_script([_beat("b1", "指向 p_094。", ["p_094"])]),
        audios=[_audio("b1")],
        panels=panels,
    )
    assert [scene.panel_id for scene in timeline.scenes] == ["p_094"]


def test_build_timeline_from_workspace_reads_storyboard_scope(tmp_path: Path) -> None:
    """从工作区构建时间轴时自动读编排文件并收口。"""
    series_dir = tmp_path / "wt_test"
    episode_dir = series_dir / "episodes" / "ep_001"
    panels_dir = episode_dir / "02_panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (360, 640), (10, 20, 30)).save(panels_dir / "p_001.png")
    Image.new("RGB", (360, 640), (20, 30, 40)).save(panels_dir / "p_002.png")
    Image.new("RGB", (360, 640), (30, 40, 50)).save(panels_dir / "p_094.png")

    script_dir = episode_dir / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    save_model(_script([_beat("b1", "指向编排外的 p_094。", ["p_094"])]), script_dir / "script.json")
    save_model(
        _storyboard([_scene("sc_001", ["p_001", "p_002"])]),
        script_dir / "storyboard.json",
    )

    audio_dir = episode_dir / "05_audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / "audio_manifest.json").write_text(
        json.dumps([_audio("b1").model_dump(mode="json")], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    timeline = s6_timeline.build_timeline_from_workspace(series_dir, 1)
    assert [scene.panel_id for scene in timeline.scenes] == ["p_002"]


# ---------------------------------------------------------------------------
# 4. Web API：写回层收口
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path / "ws"))
    return TestClient(create_app(config))


def _make_source(root: Path) -> Path:
    source = root / "src"
    folder = source / "ep_001"
    folder.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 600), (30, 60, 90)).save(folder / "001.png")
    return source


def _seed_episode_artifacts(workspace: Path, series_id: str) -> Path:
    """铺出 script.json / audio_manifest.json / 面板图 / 镜头编排。"""
    episode_dir = workspace / series_id / "episodes" / "ep_001"
    panels_dir = episode_dir / "02_panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (360, 640), (10, 20, 30)).save(panels_dir / "p_001.png")
    Image.new("RGB", (360, 640), (20, 30, 40)).save(panels_dir / "p_002.png")
    Image.new("RGB", (360, 640), (30, 40, 50)).save(panels_dir / "p_094.png")

    beats = [
        ScriptBeat(
            beat_id="b1",
            narration_text="编排内的一句解说。",
            tts_text="编排内的一句解说。",
            target_panel_ids=["p_001"],
            motion="static",
            pause_after_ms=520,
            voice_override="voice_custom",
        ),
        ScriptBeat(
            beat_id="b2",
            narration_text="指向编排外的一句解说。",
            tts_text="指向编排外的一句解说。",
            target_panel_ids=["p_094"],
            motion="static",
            pause_after_ms=480,
        ),
    ]
    script = NarrationScript(
        episode_id=f"{series_id}_ep_001",
        hook_title="编排范围接口测试",
        beats=beats,
        episode_digest=EpisodeDigest(episode_no=1, one_line_hook="钩子", summary="摘要"),
    )
    script_dir = episode_dir / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    save_model(script, script_dir / "script.json")
    (script_dir / "script_readable.md").write_text("# 解说文稿\n", encoding="utf-8")
    # 镜头编排：只有 p_001 / p_002 在启用幕里，p_094 从来没被用户选过。
    save_model(
        _storyboard([_scene("sc_001", ["p_001", "p_002"])]),
        script_dir / "storyboard.json",
    )

    audio_dir = episode_dir / "05_audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    manifest = [
        {
            "beat_id": "b1",
            "audio_path": "05_audio/b1.wav",
            "duration_ms": 1000,
            "pause_after_ms": 520,
            "word_timestamps": [],
        },
        {
            "beat_id": "b2",
            "audio_path": "05_audio/b2.wav",
            "duration_ms": 1000,
            "pause_after_ms": 480,
            "word_timestamps": [],
        },
    ]
    (audio_dir / "audio_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return episode_dir


def test_put_script_clamps_out_of_scope_and_reports(client: TestClient, tmp_path: Path) -> None:
    """保存时越界引用被收口，并把明细回传给前端。"""
    workspace = tmp_path / "ws"
    source = _make_source(workspace)
    series_id = client.post("/api/series", json={"url": str(source)}).json()["series_id"]
    episode_dir = _seed_episode_artifacts(workspace, series_id)
    episode_id = f"{series_id}_ep_001"

    script = load_model(NarrationScript, episode_dir / "04_script" / "script.json")
    edited = [beat.model_dump() for beat in script.beats]
    response = client.put(f"/api/episodes/{episode_id}/script", json={"beats": edited})
    assert response.status_code == 200, response.text
    payload = response.json()

    violations = payload["panel_scope_violations"]
    assert len(violations) == 1
    assert violations[0]["beat_id"] == "b2"
    assert violations[0]["removed"] == ["p_094"]
    assert violations[0]["replacement"] in {"p_001", "p_002"}

    # 落盘结果：越界引用已经不在剧本里。
    saved = load_model(NarrationScript, episode_dir / "04_script" / "script.json")
    by_id = {beat.beat_id: beat for beat in saved.beats}
    assert "p_094" not in by_id["b2"].target_panel_ids
    assert len(by_id["b2"].target_panel_ids) == 1

    # 不破坏 voice_override 与 pause_after_ms。
    assert by_id["b1"].voice_override == "voice_custom"
    assert by_id["b1"].pause_after_ms == 520
    assert by_id["b2"].pause_after_ms == 480


def test_auto_motions_endpoint_clamps_out_of_scope(client: TestClient, tmp_path: Path) -> None:
    """自动分配动效写回时同样收口（不能把越界引用重新落盘）。"""
    workspace = tmp_path / "ws"
    source = _make_source(workspace)
    series_id = client.post("/api/series", json={"url": str(source)}).json()["series_id"]
    episode_dir = _seed_episode_artifacts(workspace, series_id)
    episode_id = f"{series_id}_ep_001"

    response = client.post(f"/api/episodes/{episode_id}/auto-motions")
    assert response.status_code == 200, response.text

    saved = load_model(NarrationScript, episode_dir / "04_script" / "script.json")
    by_id = {beat.beat_id: beat for beat in saved.beats}
    assert "p_094" not in by_id["b2"].target_panel_ids
    assert by_id["b1"].voice_override == "voice_custom"
    assert by_id["b1"].pause_after_ms == 520


def test_auto_pacing_endpoint_clamps_out_of_scope(client: TestClient, tmp_path: Path) -> None:
    """自动节奏留白写回时同样收口。"""
    workspace = tmp_path / "ws"
    source = _make_source(workspace)
    series_id = client.post("/api/series", json={"url": str(source)}).json()["series_id"]
    episode_dir = _seed_episode_artifacts(workspace, series_id)
    episode_id = f"{series_id}_ep_001"

    response = client.post(f"/api/episodes/{episode_id}/script/auto-pacing")
    assert response.status_code == 200, response.text

    saved = load_model(NarrationScript, episode_dir / "04_script" / "script.json")
    by_id = {beat.beat_id: beat for beat in saved.beats}
    assert "p_094" not in by_id["b2"].target_panel_ids


def test_put_script_without_storyboard_keeps_everything(client: TestClient, tmp_path: Path) -> None:
    """没有编排文件 → 不约束，旧工程行为不变。"""
    workspace = tmp_path / "ws"
    source = _make_source(workspace)
    series_id = client.post("/api/series", json={"url": str(source)}).json()["series_id"]
    episode_dir = _seed_episode_artifacts(workspace, series_id)
    episode_id = f"{series_id}_ep_001"
    (episode_dir / "04_script" / "storyboard.json").unlink()

    script = load_model(NarrationScript, episode_dir / "04_script" / "script.json")
    edited = [beat.model_dump() for beat in script.beats]
    response = client.put(f"/api/episodes/{episode_id}/script", json={"beats": edited})
    assert response.status_code == 200, response.text
    assert response.json()["panel_scope_violations"] == []

    saved = load_model(NarrationScript, episode_dir / "04_script" / "script.json")
    by_id = {beat.beat_id: beat for beat in saved.beats}
    assert by_id["b2"].target_panel_ids == ["p_094"]
