"""Patch v4.0 Patch 20：Web 审核台「镜头编排（Storyboard）」接口回归测试。

覆盖规格第 3 节的五条硬断言：

1. ``GET /api/episodes/{id}/storyboard`` 在无 ``storyboard.json`` 但有
   ``vision.json`` 时自动聚类并落盘；
2. ``PUT`` 保存后 ``source`` 变为 ``"human"`` 且 ``user_note`` /
   ``locked_quotes`` 持久保留；
3. 非法（空 ``panel_ids`` / 跨幕重复 / 幕内乱序 / 未知分镜）返回 ``400``；
4. ``POST .../reset`` 重置回 ``source="auto"``；
5. ``POST .../generate`` 正确提交 ``from_stage="s4_scriptwriter"`` 任务。

另含分镜原图回源路由与未知话次 404 的保底测试。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.config import AppConfig, WorkspaceConfig
from core.schemas import EpisodeStoryboard, VisionWindowOutput, load_model
from core.schemas import save_model
from stages import s0_catalog
from web.server import create_app


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------
def _panel(panel_id: str, summary: str, *, blank: bool = False) -> dict[str, Any]:
    return {
        "panel_id": panel_id,
        "scene_summary": summary,
        "dialogues": (
            [{"speaker_guess": "小白", "text": f"{panel_id} 的台词。", "emotion": "tense"}]
            if not blank
            else []
        ),
        "sfx_list": [],
        "is_transition_blank": blank,
        "is_pure_text_card": False,
        "non_story_type": "none",
    }


def _vision() -> VisionWindowOutput:
    panels: list[dict[str, Any]] = [
        _panel(f"p_{index:03d}", f"病房里两人对峙，第 {index} 格。")
        for index in range(1, 9)
    ]
    panels.append(_panel("p_009", "过渡空页。", blank=True))
    panels.extend(
        _panel(f"p_{index:03d}", f"客厅中冲突升级，第 {index} 格。")
        for index in range(10, 13)
    )
    return VisionWindowOutput.model_validate({"panels": panels})


@pytest.fixture()
def workspace(tmp_path: Path) -> tuple[Path, str]:
    """登记作品 + 写 vision.json / 02_panels 图片，返回 (工作区根, series_id)。"""
    source = tmp_path / "src" / "ep_001"
    source.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 600), (40, 80, 120)).save(source / "001.png")
    catalog = s0_catalog.import_local_catalog(tmp_path / "src")
    s0_catalog.CatalogRegistry(tmp_path).register(catalog)
    series_id = catalog.series_id

    episode_dir = tmp_path / series_id / "episodes" / "ep_001"
    vision_dir = episode_dir / "03_vision"
    vision_dir.mkdir(parents=True, exist_ok=True)
    save_model(_vision(), vision_dir / "vision.json")

    panels_dir = episode_dir / "02_panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    for index in range(1, 13):
        Image.new("RGB", (360, 520), (200, index * 12, 90)).save(
            panels_dir / f"p_{index:03d}.png"
        )
    return tmp_path, series_id


class _FakeRunner:
    """记录 submit_episode 入参，不真正起后台任务。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def submit_episode(
        self,
        series_id: str,
        episode_no: int,
        *,
        from_stage: str = "s1_crawler",
        mode: str = "phase1_only",
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "series_id": series_id,
                "episode_no": episode_no,
                "from_stage": from_stage,
                "mode": mode,
            }
        )
        return {
            "job_id": "job_patch20",
            "episode_id": f"{series_id}_ep_{episode_no:03d}",
            "series_id": series_id,
            "episode_no": episode_no,
            "status": "queued",
        }


def _client(workspace_root: Path, runner: _FakeRunner | None = None) -> TestClient:
    config = AppConfig(workspace=WorkspaceConfig(root=workspace_root))
    return TestClient(create_app(config, runner=runner))


def _storyboard_path(workspace_root: Path, series_id: str) -> Path:
    return (
        workspace_root
        / series_id
        / "episodes"
        / "ep_001"
        / "04_script"
        / "storyboard.json"
    )


# ---------------------------------------------------------------------------
# 1. GET：无文件时自动聚类并落盘
# ---------------------------------------------------------------------------
def test_get_storyboard_auto_builds_when_missing(
    workspace: tuple[Path, str],
) -> None:
    workspace_root, series_id = workspace
    episode_id = f"{series_id}_ep_001"
    path = _storyboard_path(workspace_root, series_id)
    assert not path.exists()

    with _client(workspace_root) as client:
        response = client.get(f"/api/episodes/{episode_id}/storyboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["episode_id"] == episode_id
    assert payload["source"] == "auto"
    assert len(payload["scenes"]) >= 2  # 过渡页把病房 / 客厅切成至少两幕
    # 全部有效面板（过渡页除外）都被某个镜头组覆盖
    covered = {pid for scene in payload["scenes"] for pid in scene["panel_ids"]}
    assert {"p_001", "p_005", "p_010", "p_012"} <= covered
    assert "p_009" not in covered
    # 缩略图索引带新路由的 image_url
    panel_one = payload["panels_index"]["p_001"]
    assert panel_one["image_url"] == f"/api/episodes/{episode_id}/panels/p_001.png"
    assert panel_one["dialogues"][0]["text"] == "p_001 的台词。"
    # 已落盘，下次 GET 直接读文件
    assert path.is_file()
    on_disk = load_model(EpisodeStoryboard, path)
    assert on_disk.source == "auto"


# ---------------------------------------------------------------------------
# 2. PUT：人工保存 source=human 且编辑字段持久
# ---------------------------------------------------------------------------
def test_put_storyboard_persists_human_edits(
    workspace: tuple[Path, str],
) -> None:
    workspace_root, series_id = workspace
    episode_id = f"{series_id}_ep_001"
    body = {
        "scenes": [
            {
                "scene_id": "sc_001",
                "title": "第 1 幕 · 病房",
                "location": "病房",
                "panel_ids": ["p_001", "p_002", "p_003", "p_004", "p_005"],
                "user_note": "父亲在病床上说话，不是恩彗说的。",
                "locked_quotes": ["p_001 的台词。"],
            },
            {
                "scene_id": "sc_002",
                "title": "第 2 幕 · 客厅",
                "location": "客厅",
                "panel_ids": ["p_010", "p_011", "p_012"],
                "user_note": "",
                "locked_quotes": [],
            },
        ]
    }
    with _client(workspace_root) as client:
        response = client.put(f"/api/episodes/{episode_id}/storyboard", json=body)
        assert response.status_code == 200
        saved = response.json()
        assert saved["source"] == "human"
        # 与 s3b_scenes 约定对齐：scene_index 为 1-based，scene_id 前缀 sc_。
        assert saved["scenes"][0]["scene_index"] == 1
        assert saved["scenes"][0]["scene_id"] == "sc_001"
        assert saved["scenes"][1]["scene_index"] == 2
        assert saved["scenes"][1]["scene_id"] == "sc_002"
        assert saved["scenes"][0]["user_note"] == "父亲在病床上说话，不是恩彗说的。"
        assert saved["scenes"][0]["locked_quotes"] == ["p_001 的台词。"]

        # 再 GET 一次：磁盘回读，人工字段原样保留
        again = client.get(f"/api/episodes/{episode_id}/storyboard").json()
        assert again["source"] == "human"
        assert again["scenes"][0]["locked_quotes"] == ["p_001 的台词。"]

    on_disk = load_model(EpisodeStoryboard, _storyboard_path(workspace_root, series_id))
    assert on_disk.source == "human"
    assert on_disk.scenes[0].user_note.startswith("父亲在病床上")


def test_put_storyboard_fills_scene_id_and_1based_index(
    workspace: tuple[Path, str],
) -> None:
    """前端拆/合幕后可能省略 scene_id/scene_index：后端必须按 s3b_scenes 约定补全。"""
    workspace_root, series_id = workspace
    episode_id = f"{series_id}_ep_001"
    body = {
        "scenes": [
            {"title": "", "panel_ids": ["p_001", "p_002"]},
            {"title": "", "panel_ids": ["p_010"]},
        ]
    }
    with _client(workspace_root) as client:
        response = client.put(f"/api/episodes/{episode_id}/storyboard", json=body)
        assert response.status_code == 200
        saved = response.json()["scenes"]
    assert [(s["scene_id"], s["scene_index"]) for s in saved] == [
        ("sc_001", 1),
        ("sc_002", 2),
    ]


# ---------------------------------------------------------------------------
# 3. 非法请求一律 400
# ---------------------------------------------------------------------------
def test_put_storyboard_rejects_empty_panel_ids(
    workspace: tuple[Path, str],
) -> None:
    workspace_root, series_id = workspace
    episode_id = f"{series_id}_ep_001"
    with _client(workspace_root) as client:
        response = client.put(
            f"/api/episodes/{episode_id}/storyboard",
            json={"scenes": [{"panel_ids": []}]},
        )
    assert response.status_code == 400
    assert "panel_ids" in response.json()["detail"]


def test_put_storyboard_rejects_cross_scene_duplicate(
    workspace: tuple[Path, str],
) -> None:
    workspace_root, series_id = workspace
    episode_id = f"{series_id}_ep_001"
    body = {
        "scenes": [
            {"panel_ids": ["p_001", "p_002"]},
            {"panel_ids": ["p_002", "p_003"]},  # p_002 重复
        ]
    }
    with _client(workspace_root) as client:
        response = client.put(
            f"/api/episodes/{episode_id}/storyboard", json=body
        )
    assert response.status_code == 400
    assert "p_002" in response.json()["detail"]


def test_put_storyboard_rejects_unsorted_and_unknown_panel(
    workspace: tuple[Path, str],
) -> None:
    workspace_root, series_id = workspace
    episode_id = f"{series_id}_ep_001"
    with _client(workspace_root) as client:
        unsorted = client.put(
            f"/api/episodes/{episode_id}/storyboard",
            json={"scenes": [{"panel_ids": ["p_003", "p_001"]}]},
        )
        assert unsorted.status_code == 400
        assert "单调" in unsorted.json()["detail"]

        unknown = client.put(
            f"/api/episodes/{episode_id}/storyboard",
            json={"scenes": [{"panel_ids": ["p_999"]}]},
        )
        assert unknown.status_code == 400
        assert "p_999" in unknown.json()["detail"]


def test_put_storyboard_rejects_empty_scene_list(
    workspace: tuple[Path, str],
) -> None:
    workspace_root, series_id = workspace
    episode_id = f"{series_id}_ep_001"
    with _client(workspace_root) as client:
        response = client.put(
            f"/api/episodes/{episode_id}/storyboard", json={"scenes": []}
        )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 4. reset：回到自动分组
# ---------------------------------------------------------------------------
def test_reset_storyboard_returns_to_auto(workspace: tuple[Path, str]) -> None:
    workspace_root, series_id = workspace
    episode_id = f"{series_id}_ep_001"
    with _client(workspace_root) as client:
        client.put(
            f"/api/episodes/{episode_id}/storyboard",
            json={"scenes": [{"panel_ids": ["p_001", "p_002"]}]},
        )
        assert (
            client.get(f"/api/episodes/{episode_id}/storyboard").json()["source"]
            == "human"
        )

        response = client.post(f"/api/episodes/{episode_id}/storyboard/reset")
        assert response.status_code == 200
        payload = response.json()
        assert payload["source"] == "auto"
        assert len(payload["scenes"]) >= 2

    assert (
        load_model(EpisodeStoryboard, _storyboard_path(workspace_root, series_id)).source
        == "auto"
    )


# ---------------------------------------------------------------------------
# 5. generate：提交 s4_scriptwriter 后台任务
# ---------------------------------------------------------------------------
def test_generate_submits_s4_job_with_saved_scenes(
    workspace: tuple[Path, str],
) -> None:
    workspace_root, series_id = workspace
    episode_id = f"{series_id}_ep_001"
    runner = _FakeRunner()
    body = {
        "mode": "full_video",
        "scenes": [
            {"panel_ids": ["p_001", "p_002", "p_003"]},
            {"panel_ids": ["p_010", "p_011"]},
        ],
    }
    with _client(workspace_root, runner) as client:
        response = client.post(
            f"/api/episodes/{episode_id}/storyboard/generate", json=body
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["job"]["job_id"] == "job_patch20"
        # 带 scenes 时先落盘为人工编排
        assert payload["storyboard"]["source"] == "human"

    assert runner.calls == [
        {
            "series_id": series_id,
            "episode_no": 1,
            "from_stage": "s4_scriptwriter",
            "mode": "full_video",
        }
    ]
    assert (
        load_model(EpisodeStoryboard, _storyboard_path(workspace_root, series_id)).source
        == "human"
    )


def test_generate_without_scenes_auto_builds_and_submits_phase1(
    workspace: tuple[Path, str],
) -> None:
    workspace_root, series_id = workspace
    episode_id = f"{series_id}_ep_001"
    runner = _FakeRunner()
    with _client(workspace_root, runner) as client:
        response = client.post(
            f"/api/episodes/{episode_id}/storyboard/generate",
            json={"mode": "phase1_only"},
        )
        assert response.status_code == 200
        assert response.json()["storyboard"]["source"] == "auto"
        assert runner.calls[0]["from_stage"] == "s4_scriptwriter"
        assert runner.calls[0]["mode"] == "phase1_only"


def test_generate_rejects_unknown_mode(workspace: tuple[Path, str]) -> None:
    workspace_root, series_id = workspace
    episode_id = f"{series_id}_ep_001"
    runner = _FakeRunner()
    with _client(workspace_root, runner) as client:
        response = client.post(
            f"/api/episodes/{episode_id}/storyboard/generate",
            json={"mode": "sideways"},
        )
    assert response.status_code == 400
    assert runner.calls == []


# ---------------------------------------------------------------------------
# 附加：分镜原图回源 + 未知话次 404
# ---------------------------------------------------------------------------
def test_panel_file_route_serves_image_and_404(
    workspace: tuple[Path, str],
) -> None:
    workspace_root, series_id = workspace
    episode_id = f"{series_id}_ep_001"
    with _client(workspace_root) as client:
        ok = client.get(f"/api/episodes/{episode_id}/panels/p_001.png")
        assert ok.status_code == 200
        assert ok.headers["content-type"] == "image/png"
        assert ok.content[:8] == b"\x89PNG\r\n\x1a\n"

        missing = client.get(f"/api/episodes/{episode_id}/panels/p_999.png")
        assert missing.status_code == 404

        # 路径穿越被挡掉（Path(...).name 只取文件名）
        traversal = client.get(
            f"/api/episodes/{episode_id}/panels/..%2F..%2Fconfig.yaml"
        )
        assert traversal.status_code == 404


def test_storyboard_endpoints_404_for_unknown_episode(
    workspace: tuple[Path, str],
) -> None:
    workspace_root, _ = workspace
    with _client(workspace_root) as client:
        assert (
            client.get("/api/episodes/wt_missing_ep_009/storyboard").status_code
            == 404
        )
        assert (
            client.post(
                "/api/episodes/wt_missing_ep_009/storyboard/reset"
            ).status_code
            == 404
        )
