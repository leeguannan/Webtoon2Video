"""PATCH v4.39：剧本顺序一致性（成片不再「回头看」前面的画面）。

**用户报障**（m04229）：「最新做的视频图中的画面和配音不应该出现在这里啊，
应该在 b_new_073 的后面，b_new_072 的前面才对啊！！！」

**取证结论**（workspace/wt_5081/episodes/ep_007）：

* 成片顺序 = ``04_script/script.json`` 的 ``beats`` 数组顺序
  （script.json / timeline.json / audio_manifest.json 三处 67 条顺序完全一致）；
* 落盘顺序里 ``b43``（目标分镜 ``p_027``、``scene_id=sc_002``）排在 idx39，
  前一句是 ``b36``（``p_038``）→ **分镜序列回退**（p_038 → p_027）；
* 而按「幕分组展平」看，``b43`` 应落在 idx29，前后正是
  ``b27, b_new_073, b43, b_new_072, b30`` —— 与用户期望逐字一致；
* 根因是 Stage 4 逐幕写作（4B 一章一章生成），合并进全话数组时把
  第 2 幕的句子挂到了第 3 幕的句子中间：**画面本身没错，位置错了**；
* 上游放大原因是 ``_clamp_regressive_beats`` 的本幕锚点取「本幕内
  index <= last_index 的最后一格」—— 那一格本身仍是回看格，钳制形同虚设。

本文件覆盖四层：

1. ``core/panel_scope.py``：**幕序**是成片播放顺序的唯一权威口径
   （``PanelScope.ordered_by_scene`` / ``order_beats_by_scene``）；
2. ``stages/s4_scriptwriter.py``：S4 出口按幕重排 + 钳制锚点不再选回看格；
3. ``web/server.py``：写回层按幕重排 + 顺位违规检测并回传；
4. API 契约：``PUT /script`` 自动纠正顺序、回传 ``order_violations``，
   且不破坏 ``voice_override`` / ``pause_after_ms`` / ``beat_id``。

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
    load_panel_scope,
    order_beats_by_scene,
    scope_from_storyboard,
)
from core.schemas import (
    CameraMotion,
    EpisodeDigest,
    EpisodeStoryboard,
    NarrationScript,
    ScriptBeat,
    StoryboardScene,
    load_model,
    save_model,
)
from stages.s4_scriptwriter import _clamp_regressive_beats, _order_beats_by_scene
from web.server import (
    _detect_order_violations,
    _reorder_beats_by_scene,
    create_app,
)

# ---------------------------------------------------------------------------
# 造数工具
# ---------------------------------------------------------------------------


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
    panel_ids: list[str],
    *,
    scene_id: str | None = None,
    text: str = "一句解说。",
    voice_override: str = "",
    pause_after_ms: int = 500,
) -> ScriptBeat:
    return ScriptBeat(
        beat_id=beat_id,
        narration_text=text,
        tts_text=text,
        target_panel_ids=panel_ids,
        motion=CameraMotion.STATIC,
        pause_after_ms=pause_after_ms,
        scene_id=scene_id,
        voice_override=voice_override,
    )


def _storyboard(scenes: list[StoryboardScene]) -> EpisodeStoryboard:
    return EpisodeStoryboard(episode_id="ep_001", source="human", scenes=scenes)


def _scope(scenes: list[StoryboardScene]) -> PanelScope:
    scope = scope_from_storyboard(_storyboard(scenes))
    assert scope is not None
    return scope


# ---------------------------------------------------------------------------
# 1. 权威口径：幕序 = 成片播放顺序
# ---------------------------------------------------------------------------


def test_ordered_by_scene_follows_scene_order_then_panel_order() -> None:
    """成片播放顺序 = 幕顺序 × 幕内自然序（不是分镜号自然序）。"""
    scope = _scope(
        [
            _scene("sc_001", ["p_001", "p_002"], index=1),
            _scene("sc_002", ["p_010", "p_011"], index=2),
        ]
    )
    assert scope.ordered_by_scene() == ["p_001", "p_002", "p_010", "p_011"]
    # 幕内按自然序（用户手工把 p_011 排在 p_010 前面也不打乱）。
    scope2 = _scope([_scene("sc_001", ["p_002", "p_001"], index=1)])
    assert scope2.ordered_by_scene() == ["p_001", "p_002"]


def test_ordered_by_scene_falls_back_to_natural_order_without_storyboard() -> None:
    """没有编排信息时退回自然序，旧行为不变。"""
    scope = PanelScope(allowed_panel_ids=frozenset({"p_002", "p_001"}))
    assert scope.ordered_by_scene() == ["p_001", "p_002"]


def test_order_beats_by_scene_moves_scene_sentences_back() -> None:
    """核心场景：第 1 幕的句子挂在第 2 幕句子中间 → 重排后回到第 1 幕末尾。"""
    scope = _scope(
        [
            _scene("sc_001", ["p_001", "p_002"], index=1),
            _scene("sc_002", ["p_003", "p_004"], index=2),
        ]
    )
    beats = [
        _beat("b1", ["p_001"], scene_id="sc_001"),
        _beat("b2", ["p_003"], scene_id="sc_002"),
        _beat("b3", ["p_002"], scene_id="sc_001"),  # 越位落在第 2 幕中间
        _beat("b4", ["p_004"], scene_id="sc_002"),
    ]
    ordered = order_beats_by_scene(
        beats,
        scene_order=scope.scene_order,
        panel_owner=scope.panel_owner_map(),
    )
    assert [b.beat_id for b in ordered] == ["b1", "b3", "b2", "b4"]


def test_order_beats_by_scene_infers_scene_from_first_panel() -> None:
    """没有 scene_id 的句子按第一张目标分镜反查所属幕。"""
    scope = _scope(
        [
            _scene("sc_001", ["p_001"], index=1),
            _scene("sc_002", ["p_002"], index=2),
        ]
    )
    beats = [
        _beat("b1", ["p_002"]),  # 无 scene_id，靠 p_002 反查 → sc_002
        _beat("b2", ["p_001"]),  # 无 scene_id，靠 p_001 反查 → sc_001
    ]
    ordered = order_beats_by_scene(
        beats,
        scene_order=scope.scene_order,
        panel_owner=scope.panel_owner_map(),
    )
    assert [b.beat_id for b in ordered] == ["b2", "b1"]


def test_order_beats_by_scene_keeps_orphans_last_and_is_idempotent() -> None:
    """认不出归属的句子按原序置末；重排幂等。"""
    scope = _scope(
        [
            _scene("sc_001", ["p_001"], index=1),
            _scene("sc_002", ["p_002"], index=2),
        ]
    )
    beats = [
        _beat("b1", ["p_002"], scene_id="sc_002"),
        _beat("b_orphan", ["p_099"]),
        _beat("b2", ["p_001"], scene_id="sc_001"),
    ]
    ordered = order_beats_by_scene(
        beats,
        scene_order=scope.scene_order,
        panel_owner=scope.panel_owner_map(),
    )
    assert [b.beat_id for b in ordered] == ["b2", "b1", "b_orphan"]
    again = order_beats_by_scene(
        ordered,
        scene_order=scope.scene_order,
        panel_owner=scope.panel_owner_map(),
    )
    assert [b.beat_id for b in again] == [b.beat_id for b in ordered]


def test_order_beats_by_scene_without_scenes_returns_input() -> None:
    """没有幕信息时原样返回（单幕/无编排在编旧工程不受影响）。"""
    beats = [_beat("b1", ["p_001"]), _beat("b2", ["p_002"])]
    assert order_beats_by_scene(beats, scene_order=[], panel_owner={}) == beats


# ---------------------------------------------------------------------------
# 2. Stage 4：出口按幕重排 + 钳制锚点不再选回看格
# ---------------------------------------------------------------------------


def test_s4_order_beats_by_scene_sorts_by_scene_index() -> None:
    """S4 的薄封装按 scene_index 排序（幕内保持原序）。"""
    scenes = [
        _scene("sc_002", ["p_003"], index=2),
        _scene("sc_001", ["p_001"], index=1),
    ]
    beats = [
        {"beat_id": "b1", "target_panel_ids": ["p_003"], "scene_id": "sc_002"},
        {"beat_id": "b2", "target_panel_ids": ["p_001"], "scene_id": "sc_001"},
    ]
    ordered = _order_beats_by_scene(beats, scenes)
    assert [b["beat_id"] for b in ordered] == ["b2", "b1"]


def test_clamp_regressive_beats_anchor_never_earlier_than_progress() -> None:
    """本幕分镜全部早于进度时，锚点必须落到当前主画面（而不是回看格）。

    旧实现取「本幕内 index <= last_index 的最后一格」—— 那一格本身仍是
    回看的画面，钳制形同虚设（真实案例 wt_5081/ep_007 的 b43）。
    """
    ordered = ["p_001", "p_002", "p_003", "p_004", "p_005"]
    scenes = [
        _scene("sc_001", ["p_001", "p_002"], index=1),
        _scene("sc_002", ["p_001", "p_002"], index=2),
    ]
    beats = [
        {"beat_id": "b1", "target_panel_ids": ["p_001"], "scene_id": "sc_001"},
        {"beat_id": "b2", "target_panel_ids": ["p_005"], "scene_id": "sc_002"},
        {"beat_id": "b3", "target_panel_ids": ["p_002"], "scene_id": "sc_002"},
    ]
    _clamp_regressive_beats(beats, ordered, scenes=scenes)
    # 进度已到 p_005（index 4），b3 回看 p_002 → 只能锚到当前主画面 p_005。
    assert beats[2]["target_panel_ids"] == ["p_005"]


def test_clamp_regressive_beats_advances_progress_to_forward_anchor() -> None:
    """锚点落在「本幕还没讲到的那格」时，进度必须跟着推进。

    场景：进度停在 ``p_002``，本幕（sc_002）是 ``p_004..p_006``，
    一条回看 ``p_001`` 的解说 → 锚到 ``p_004``，进度随之从 1 跳到 3。
    """
    ordered = ["p_001", "p_002", "p_003", "p_004", "p_005", "p_006"]
    scenes = [
        _scene("sc_001", ["p_001"], index=1),
        _scene("sc_002", ["p_004", "p_005", "p_006"], index=2),
    ]
    beats = [
        {"beat_id": "b1", "target_panel_ids": ["p_002"], "scene_id": "sc_001"},
        {"beat_id": "b2", "target_panel_ids": ["p_001"], "scene_id": "sc_002"},
        {"beat_id": "b3", "target_panel_ids": ["p_002"], "scene_id": "sc_002"},
    ]
    _clamp_regressive_beats(beats, ordered, scenes=scenes)
    # b2 回看 p_001 → 锚到本幕还没讲到的第一格 p_004。
    assert beats[1]["target_panel_ids"] == ["p_004"]
    # 进度推进到 p_004 之后，b3 回看 p_002 必须被钳制（不能原样保留）。
    assert beats[2]["target_panel_ids"] == ["p_004"]


def test_clamp_regressive_beats_keeps_legal_beat_untouched() -> None:
    """合规的句子一个字都不该动（只改 target，不动文本）。"""
    ordered = ["p_001", "p_002", "p_003"]
    beats = [
        {
            "beat_id": "b1",
            "target_panel_ids": ["p_001"],
            "scene_id": "sc_001",
            "narration_text": "原文不能动",
        },
        {
            "beat_id": "b2",
            "target_panel_ids": ["p_003"],
            "scene_id": "sc_001",
            "narration_text": "原文不能动",
        },
    ]
    _clamp_regressive_beats(beats, ordered, scenes=[_scene("sc_001", ordered, index=1)])
    assert [b["target_panel_ids"] for b in beats] == [["p_001"], ["p_003"]]
    assert beats[0]["narration_text"] == "原文不能动"


# ---------------------------------------------------------------------------
# 3. 写回层：顺位检测 + 按幕重排
# ---------------------------------------------------------------------------


def test_detect_order_violations_finds_backward_panel() -> None:
    """检测「画面回退」并给出人话说明。"""
    scope = _scope(
        [
            _scene("sc_001", ["p_001", "p_002"], index=1),
            _scene("sc_002", ["p_003"], index=2),
        ]
    )
    beats = [
        _beat("b1", ["p_001"], scene_id="sc_001"),
        _beat("b2", ["p_003"], scene_id="sc_002"),
        _beat("b3", ["p_002"], scene_id="sc_001"),
    ]
    violations = _detect_order_violations(beats, scope)
    assert len(violations) == 1
    assert violations[0]["beat_id"] == "b3"
    assert violations[0]["previous_beat_id"] == "b2"
    assert "p_002" in violations[0]["message"]


def test_detect_order_violations_empty_when_scope_missing() -> None:
    """没有编排文件时不做任何判断（旧工程行为不变）。"""
    beats = [_beat("b1", ["p_009"]), _beat("b2", ["p_001"])]
    assert _detect_order_violations(beats, None) == []
    assert _detect_order_violations(beats, PanelScope()) == []


def test_reorder_beats_by_scene_keeps_beat_ids() -> None:
    """重排不得改 beat_id（用户手加的 b_new_* 编号必须原样保留）。"""
    scope = _scope(
        [
            _scene("sc_001", ["p_001"], index=1),
            _scene("sc_002", ["p_002"], index=2),
        ]
    )
    beats = [
        _beat("b_new_073", ["p_001"], scene_id="sc_001"),
        _beat("b_new_072", ["p_002"], scene_id="sc_002"),
    ]
    reordered = _reorder_beats_by_scene(beats, scope)
    assert {b.beat_id for b in reordered} == {"b_new_073", "b_new_072"}


# ---------------------------------------------------------------------------
# 4. API 契约：PUT /script 自动纠正顺序并回传违规明细
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
    """铺出 3 幕编排 + 一份「第 1 幕句子落在第 2 幕中间」的剧本。"""
    episode_dir = workspace / series_id / "episodes" / "ep_001"
    panels_dir = episode_dir / "02_panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    for index in range(1, 7):
        Image.new("RGB", (360, 640), (index * 10, 20, 30)).save(
            panels_dir / f"p_{index:03d}.png"
        )

    beats = [
        _beat("b1", ["p_001"], scene_id="sc_001", voice_override="voice_custom", pause_after_ms=520),
        _beat("b2", ["p_003"], scene_id="sc_002", pause_after_ms=480),
        _beat("b_new_073", ["p_002"], scene_id="sc_001", pause_after_ms=470),
        _beat("b_new_072", ["p_004"], scene_id="sc_002", pause_after_ms=460),
    ]
    script = NarrationScript(
        episode_id=f"{series_id}_ep_001",
        hook_title="顺序一致性接口测试",
        beats=beats,
        episode_digest=EpisodeDigest(episode_no=1, one_line_hook="钩子", summary="摘要"),
    )
    script_dir = episode_dir / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    save_model(script, script_dir / "script.json")
    (script_dir / "script_readable.md").write_text("# 解说文稿\n", encoding="utf-8")
    save_model(
        _storyboard(
            [
                _scene("sc_001", ["p_001", "p_002"], index=1),
                _scene("sc_002", ["p_003", "p_004"], index=2),
            ]
        ),
        script_dir / "storyboard.json",
    )

    audio_dir = episode_dir / "05_audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    manifest = [
        {
            "beat_id": beat.beat_id,
            "audio_path": f"05_audio/{beat.beat_id}.wav",
            "duration_ms": 1000,
            "pause_after_ms": beat.pause_after_ms,
            "word_timestamps": [],
        }
        for beat in beats
    ]
    (audio_dir / "audio_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return episode_dir


def test_put_script_reorders_beats_by_scene(client: TestClient, tmp_path: Path) -> None:
    """保存时按幕重排：第 1 幕的句子回到第 1 幕末尾，成片顺序随之纠正。"""
    workspace = tmp_path / "ws"
    source = _make_source(workspace)
    series_id = client.post("/api/series", json={"url": str(source)}).json()["series_id"]
    episode_dir = _seed_episode_artifacts(workspace, series_id)
    episode_id = f"{series_id}_ep_001"

    script = load_model(NarrationScript, episode_dir / "04_script" / "script.json")
    edited = [beat.model_dump() for beat in script.beats]
    response = client.put(f"/api/episodes/{episode_id}/script", json={"beats": edited})
    assert response.status_code == 200, response.text

    saved = load_model(NarrationScript, episode_dir / "04_script" / "script.json")
    assert [b.beat_id for b in saved.beats] == ["b1", "b_new_073", "b2", "b_new_072"]
    # 重排后没有画面回退。
    scope = load_panel_scope(episode_dir)
    assert _detect_order_violations(saved.beats, scope) == []
    # beat_id 全部保留（b_new_* 是用户手加句的编号）。
    assert {b.beat_id for b in saved.beats} == {
        "b1",
        "b2",
        "b_new_073",
        "b_new_072",
    }


def test_put_script_keeps_voice_override_and_pause(client: TestClient, tmp_path: Path) -> None:
    """重排不得吞掉逐句音色与停顿。"""
    workspace = tmp_path / "ws"
    source = _make_source(workspace)
    series_id = client.post("/api/series", json={"url": str(source)}).json()["series_id"]
    episode_dir = _seed_episode_artifacts(workspace, series_id)
    episode_id = f"{series_id}_ep_001"

    script = load_model(NarrationScript, episode_dir / "04_script" / "script.json")
    edited = [beat.model_dump() for beat in script.beats]
    response = client.put(f"/api/episodes/{episode_id}/script", json={"beats": edited})
    assert response.status_code == 200, response.text

    saved = load_model(NarrationScript, episode_dir / "04_script" / "script.json")
    by_id = {b.beat_id: b for b in saved.beats}
    assert by_id["b1"].voice_override == "voice_custom"
    assert by_id["b1"].pause_after_ms == 520
    assert by_id["b_new_072"].pause_after_ms == 460


def test_put_script_reports_order_violations_key(client: TestClient, tmp_path: Path) -> None:
    """响应里带上 order_violations 字段（重排后为空数组）。"""
    workspace = tmp_path / "ws"
    source = _make_source(workspace)
    series_id = client.post("/api/series", json={"url": str(source)}).json()["series_id"]
    episode_dir = _seed_episode_artifacts(workspace, series_id)
    episode_id = f"{series_id}_ep_001"

    script = load_model(NarrationScript, episode_dir / "04_script" / "script.json")
    edited = [beat.model_dump() for beat in script.beats]
    payload = client.put(f"/api/episodes/{episode_id}/script", json={"beats": edited}).json()
    assert "order_violations" in payload
    assert isinstance(payload["order_violations"], list)
