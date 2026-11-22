"""M7：FastAPI 控制台后端与后台调度的离线测试。

全部用本地文件系统 + 假 LLM/TTS 客户端，不触网、不消耗百炼额度。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from core.config import AppConfig, TTSConfig, WorkspaceConfig
from core.schemas import NarrationScript, load_model, save_model
from providers.llm_base import ChatResponse
from web.job_runner import JobRunner
from web.server import DEFAULT_TTS_VOICE, create_app

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "ws"


@pytest.fixture()
def config(workspace: Path) -> AppConfig:
    return AppConfig(workspace=WorkspaceConfig(root=workspace))


@pytest.fixture()
def app(config: AppConfig):
    return create_app(config)


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


def _make_source(root: Path, episodes: int = 2) -> Path:
    """造一个本地漫画目录（每个子目录一话）。"""
    source = root / "src"
    for index in range(1, episodes + 1):
        folder = source / f"ep_{index:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        for slice_index in (1, 2):
            # 画出可辨认的内容块：Patch 6 起，纯色页会被判为「空镜」而跳过视觉模型
            image = Image.new("RGB", (400, 600), (30 * slice_index, 60, 90))
            draw = ImageDraw.Draw(image)
            for row in range(3):
                for col in range(2):
                    draw.rectangle(
                        [30 + col * 180, 40 + row * 180, 30 + col * 180 + 150, 40 + row * 180 + 150],
                        fill=(200 - 30 * row, 180 - 20 * col, 160),
                    )
            image.save(folder / f"{slice_index:03d}.png")
    return source


def _register(client: TestClient, workspace: Path, source: Path) -> str:
    response = client.post("/api/series", json={"url": str(source)})
    assert response.status_code == 200, response.text
    return str(response.json()["series_id"])


def _write_stage_artifacts(workspace: Path, series_id: str, episode_no: int) -> Path:
    """手工铺出 Stage 2~6 的产物，供接口组装测试使用。"""
    episode_dir = workspace / series_id / "episodes" / f"ep_{episode_no:03d}"
    panels_dir = episode_dir / "02_panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (360, 640), (10, 20, 30)).save(panels_dir / "panel_001.png")

    vision_dir = episode_dir / "03_vision"
    vision_dir.mkdir(parents=True, exist_ok=True)
    (vision_dir / "vision.json").write_text(
        json.dumps(
            {
                "panels": [
                    {
                        "panel_id": "panel_001",
                        "scene_summary": "主角推开铁门走进长廊。",
                        "dialogues": [
                            {
                                "speaker_guess": "主角",
                                "text": "这里到底是哪。",
                                "emotion": "tense",
                            }
                        ],
                        "sfx_list": ["吱呀"],
                        "focus_region": [0.1, 0.2, 0.9, 0.8],
                        "is_transition_blank": False,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    script_dir = episode_dir / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    script = NarrationScript.model_validate(
        {
            "episode_id": f"{series_id}_ep_{episode_no:03d}",
            "hook_title": "铁门之后",
            "beats": [
                {
                    "beat_id": "b_001",
                    "narration_text": "主角推开那扇锈迹斑斑的铁门。",
                    "tts_text": None,
                    "emotion_instruction": "用悬疑的语气低声说",
                    "target_panel_ids": ["panel_001"],
                    "motion": "zoom_in",
                    "pause_after_ms": 250,
                }
            ],
            "episode_digest": {
                "episode_no": episode_no,
                "one_line_hook": "推开铁门",
                "summary": "主角进入长廊",
                "keywords": ["铁门"],
                "characters_present": ["主角"],
            },
            "character_updates": [],
            "new_plot_threads": [],
            "resolved_thread_ids": [],
        }
    )
    save_model(script, script_dir / "script.json")
    (script_dir / "script_readable.md").write_text("# 解说文稿\n", encoding="utf-8")

    audio_dir = episode_dir / "05_audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / "audio_manifest.json").write_text(
        json.dumps(
            [
                {
                    "beat_id": "b_001",
                    "audio_path": "b_001.wav",
                    "duration_ms": 4200,
                    "pause_after_ms": 250,
                    "word_timestamps": [],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    plan_dir = episode_dir / "06_plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "subtitles.ass").write_text("[Script Info]\n", encoding="utf-8")
    (plan_dir / "timeline.json").write_text(
        json.dumps({"episode_id": script.episode_id, "total_duration_ms": 4450}),
        encoding="utf-8",
    )

    out_dir = episode_dir / "07_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{series_id}_ep_{episode_no:03d}.mp4").write_bytes(b"fake-mp4-bytes")
    return episode_dir


# ---------------------------------------------------------------------------
# 书架
# ---------------------------------------------------------------------------


def test_series_list_is_empty_initially(client: TestClient) -> None:
    assert client.get("/api/series").json() == []


def test_create_series_registers_local_catalog(client: TestClient, workspace: Path) -> None:
    source = _make_source(workspace)
    response = client.post("/api/series", json={"url": str(source)})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["series_id"] == "wt_local_src"
    assert [item["episode_no"] for item in payload["episodes"]] == [1, 2]
    assert (workspace / "index.json").is_file()
    assert (workspace / "wt_local_src" / "catalog.json").is_file()


def test_create_series_requires_url(client: TestClient) -> None:
    assert client.post("/api/series", json={"url": "  "}).status_code == 400


def test_get_series_returns_detail_with_stage_slots(
    client: TestClient, workspace: Path
) -> None:
    source = _make_source(workspace)
    series_id = _register(client, workspace, source)
    payload = client.get(f"/api/series/{series_id}").json()
    assert payload["series_id"] == series_id
    episode = payload["episodes"][0]
    assert [stage["id"] for stage in episode["stages"]] == [
        "s0_catalog",
        "s1_crawler",
        "s2_segmenter",
        "s3_vision",
        "s4_scriptwriter",
        "s5_tts",
        "s6_timeline",
        "s7_renderer",
    ]
    assert episode["stages"][0]["code"] == "S0"
    assert episode["stages"][0]["phase"] == 1
    assert episode["stages"][7]["phase"] == 2


def test_get_unknown_series_returns_404(client: TestClient) -> None:
    assert client.get("/api/series/wt_missing").status_code == 404


# ---------------------------------------------------------------------------
# 记忆体
# ---------------------------------------------------------------------------


def test_update_memory_persists_character_and_thread(
    client: TestClient, workspace: Path
) -> None:
    source = _make_source(workspace)
    series_id = _register(client, workspace, source)
    response = client.put(
        f"/api/series/{series_id}/memory",
        json={
            "narration_style": "冷峻克制的漫评风格",
            "characters": [
                {
                    "name": "黑发少年",
                    "aliases": ["小巴"],
                    "visual_traits": "黑发橙瞳",
                    "current_status": "迷路了",
                    "first_seen_ep": 1,
                    "last_seen_ep": 1,
                }
            ],
            "plot_threads": [
                {
                    "thread_id": "t1",
                    "title": "塔顶的秘密",
                    "description": "他一定要爬到塔顶",
                    "introduced_ep": 1,
                    "status": "open",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["narration_style"] == "冷峻克制的漫评风格"
    assert payload["characters"][0]["name"] == "黑发少年"
    assert payload["plot_threads"][0]["status"] == "open"

    saved = json.loads((workspace / series_id / "story_bible.json").read_text("utf-8"))
    assert saved["characters"][0]["aliases"] == ["小巴"]
    assert saved["plot_threads"][0]["thread_id"] == "t1"


# ---------------------------------------------------------------------------
# 面板与解说稿
# ---------------------------------------------------------------------------


def test_series_detail_contains_panels_and_beats(
    client: TestClient, workspace: Path
) -> None:
    source = _make_source(workspace)
    series_id = _register(client, workspace, source)
    _write_stage_artifacts(workspace, series_id, 1)

    payload = client.get(f"/api/series/{series_id}").json()
    episode = payload["episodes"][0]
    assert episode["panels"][0]["panel_id"] == "panel_001"
    assert episode["panels"][0]["image_url"].startswith(
        f"/workspace/{series_id}/episodes/ep_001/02_panels/"
    )
    assert episode["panels"][0]["scene_summary"] == "主角推开铁门走进长廊。"
    assert episode["panels"][0]["dialogues"][0]["emotion"] == "tense"
    assert episode["beats"][0]["narration_text"] == "主角推开那扇锈迹斑斑的铁门。"
    assert episode["beats"][0]["audio_duration_ms"] == 4200
    assert episode["beats"][0]["motion"] == "zoom_in"
    assert episode["video_duration_ms"] == 4450


def test_static_workspace_assets_are_served(
    client: TestClient, workspace: Path
) -> None:
    source = _make_source(workspace)
    series_id = _register(client, workspace, source)
    _write_stage_artifacts(workspace, series_id, 1)
    response = client.get(f"/workspace/{series_id}/episodes/ep_001/02_panels/panel_001.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_patch_panel_transition_blank_flag(client: TestClient, workspace: Path) -> None:
    source = _make_source(workspace)
    series_id = _register(client, workspace, source)
    _write_stage_artifacts(workspace, series_id, 1)

    response = client.patch(
        f"/api/episodes/{series_id}_ep_001/panels/panel_001",
        json={"is_transition_blank": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["is_transition_blank"] is True

    vision = json.loads(
        (workspace / series_id / "episodes" / "ep_001" / "03_vision" / "vision.json").read_text(
            "utf-8"
        )
    )
    assert vision["panels"][0]["is_transition_blank"] is True


def test_patch_panel_unknown_returns_404(client: TestClient, workspace: Path) -> None:
    source = _make_source(workspace)
    series_id = _register(client, workspace, source)
    _write_stage_artifacts(workspace, series_id, 1)
    response = client.patch(
        f"/api/episodes/{series_id}_ep_001/panels/panel_999",
        json={"is_transition_blank": True},
    )
    assert response.status_code == 404


def test_put_script_saves_beats(client: TestClient, workspace: Path) -> None:
    source = _make_source(workspace)
    series_id = _register(client, workspace, source)
    _write_stage_artifacts(workspace, series_id, 1)

    response = client.put(
        f"/api/episodes/{series_id}_ep_001/script",
        json={
            "beats": [
                {
                    "beat_id": "b_001",
                    "narration_text": "人工改写后的解说词。",
                    "tts_text": "人工改写后的解说词",
                    "emotion_instruction": "压低声音",
                    "target_panel_ids": ["panel_001"],
                    "motion": "pan_down",
                    "pause_after_ms": 300,
                    "audio_duration_ms": 4200,
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    saved = json.loads(
        (workspace / series_id / "episodes" / "ep_001" / "04_script" / "script.json").read_text(
            "utf-8"
        )
    )
    assert saved["beats"][0]["narration_text"] == "人工改写后的解说词。"
    assert saved["beats"][0]["motion"] == "pan_down"


def test_put_script_keeps_readonly_beat_fields_not_sent_by_client(
    client: TestClient, workspace: Path
) -> None:
    """前端只回传旧字段时，speaker_role / sfx_cue / focus_hint 不能被抹掉。"""
    source = _make_source(workspace)
    series_id = _register(client, workspace, source)
    _write_stage_artifacts(workspace, series_id, 1)

    script_path = (
        workspace / series_id / "episodes" / "ep_001" / "04_script" / "script.json"
    )
    script = load_model(NarrationScript, script_path)
    script = script.model_copy(
        update={
            "beats": [
                beat.model_copy(
                    update={
                        "speaker_role": "male_lead",
                        "sfx_cue": "heart_thump",
                        "focus_hint": "top",
                    }
                )
                for beat in script.beats
            ]
        }
    )
    save_model(script, script_path)

    legacy_beat = {
        "beat_id": "b_001",
        "narration_text": "人工改写后的解说词。",
        "tts_text": "",
        "emotion_instruction": "压低声音",
        "target_panel_ids": ["panel_001"],
        "motion": "pan_down",
        "pause_after_ms": 300,
        "audio_duration_ms": 4200,
    }
    response = client.put(
        f"/api/episodes/{series_id}_ep_001/script",
        json={"beats": [legacy_beat]},
    )
    assert response.status_code == 200, response.text
    saved = json.loads(script_path.read_text("utf-8"))
    assert saved["beats"][0]["speaker_role"] == "male_lead"
    assert saved["beats"][0]["sfx_cue"] == "heart_thump"
    assert saved["beats"][0]["focus_hint"] == "top"

    # sfx_cue 允许显式清空（null）；focus_hint 不允许 null，非法 null 按「未提供」继承旧值
    response = client.put(
        f"/api/episodes/{series_id}_ep_001/script",
        json={"beats": [{**legacy_beat, "sfx_cue": None, "focus_hint": None}]},
    )
    assert response.status_code == 200, response.text
    saved = json.loads(script_path.read_text("utf-8"))
    assert saved["beats"][0]["sfx_cue"] is None
    assert saved["beats"][0]["focus_hint"] == "top"
    assert saved["beats"][0]["speaker_role"] == "male_lead"


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


def test_export_episode_formats(client: TestClient, workspace: Path) -> None:
    source = _make_source(workspace)
    series_id = _register(client, workspace, source)
    _write_stage_artifacts(workspace, series_id, 1)
    episode_id = f"{series_id}_ep_001"

    mp4 = client.get(f"/api/episodes/{episode_id}/export?format=mp4")
    assert mp4.status_code == 200
    assert mp4.content == b"fake-mp4-bytes"

    ass = client.get(f"/api/episodes/{episode_id}/export?format=ass")
    assert ass.status_code == 200
    assert "[Script Info]" in ass.text

    script = client.get(f"/api/episodes/{episode_id}/export?format=script")
    assert script.status_code == 200
    assert "解说文稿" in script.text


def test_export_rejects_unknown_format(client: TestClient, workspace: Path) -> None:
    source = _make_source(workspace)
    series_id = _register(client, workspace, source)
    _write_stage_artifacts(workspace, series_id, 1)
    response = client.get(f"/api/episodes/{series_id}_ep_001/export?format=gif")
    assert response.status_code == 400


def test_export_zip_contains_rendered_videos(client: TestClient, workspace: Path) -> None:
    source = _make_source(workspace)
    series_id = _register(client, workspace, source)
    _write_stage_artifacts(workspace, series_id, 1)
    response = client.get(f"/api/series/{series_id}/export-zip")
    assert response.status_code == 200, response.text
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert names == [f"{series_id}_ep_001.mp4"]
        assert archive.read(names[0]) == b"fake-mp4-bytes"


def test_export_zip_without_videos_returns_404(client: TestClient, workspace: Path) -> None:
    source = _make_source(workspace)
    series_id = _register(client, workspace, source)
    assert client.get(f"/api/series/{series_id}/export-zip").status_code == 404


# ---------------------------------------------------------------------------
# 配置与词典
# ---------------------------------------------------------------------------


def test_get_config_returns_studio_shape(client: TestClient) -> None:
    payload = client.get("/api/config").json()
    assert payload["aspectRatio"] == "9:16"
    # Patch v4.22：默认识图模型升级为 qwen3.7-plus，下拉候选一并回显。
    assert payload["visionModel"] == "qwen3.7-plus"
    assert "qwen3.7-plus" in payload["visionModelOptions"]
    assert payload["scriptModel"] == "qwen3.7-plus"
    # Patch v3.3 Patch 15：4A 说书体模型与下拉候选一并回显。
    assert payload["storyModel"] == "qwen4-max"
    assert "qwen4-max" in payload["storyModelOptions"]
    assert payload["ttsModel"] == "qwen3-tts-flash"
    assert "apiKey" not in payload


def test_get_config_uses_tts_default_voice(client: TestClient) -> None:
    """默认音色来自 ``config.tts.default_voice``（不再写死旧值）。"""
    payload = client.get("/api/config").json()
    assert payload["ttsVoice"] == "Elias"
    assert DEFAULT_TTS_VOICE == "Elias"


def test_get_config_prefers_configured_default_voice(workspace: Path) -> None:
    """配置里改过 ``tts.default_voice`` 时，回显跟着变。"""
    config = AppConfig(
        workspace=WorkspaceConfig(root=workspace),
        tts=TTSConfig(default_voice="Katerina"),
    )
    with TestClient(create_app(config)) as client:
        payload = client.get("/api/config").json()
    assert payload["ttsVoice"] == "Katerina"


def test_get_config_lists_voice_options(client: TestClient) -> None:
    """可选音色清单包含百炼官方音色（Patch v3.3 Patch 15 扩到 34 个，前端下拉用）。"""
    from providers.tts_qwen import QWEN3_VOICE_IDS

    payload = client.get("/api/config").json()
    options = payload["ttsVoiceOptions"]
    assert [item["id"] for item in options] == list(QWEN3_VOICE_IDS)
    assert len(options) == len(QWEN3_VOICE_IDS)
    assert all(item["label"] for item in options)


def test_put_config_writes_file(client: TestClient, workspace: Path) -> None:
    response = client.put(
        "/api/config", json={"visionModel": "qwen3-vl-max", "bgmDuckingDb": -12.0}
    )
    assert response.status_code == 200, response.text
    assert response.json()["visionModel"] == "qwen3-vl-max"
    saved = (workspace / "config.yaml").read_text("utf-8")
    assert "qwen3-vl-max" in saved
    assert "-12.0" in saved


def test_put_config_persists_tts_voice(client: TestClient, workspace: Path) -> None:
    """保存音色要落到 ``tts.default_voice``，回显随之变化。"""
    response = client.put("/api/config", json={"ttsVoice": "Cherry"})
    assert response.status_code == 200, response.text
    assert response.json()["ttsVoice"] == "Cherry"
    saved = (workspace / "config.yaml").read_text("utf-8")
    assert "default_voice: Cherry" in saved


def test_pronunciation_rules_round_trip(client: TestClient, workspace: Path) -> None:
    source = _make_source(workspace)
    series_id = _register(client, workspace, source)

    response = client.put(
        "/api/pronunciation-rules",
        json={
            "scope": "global",
            "rules": [{"source": "银行", "replacement": "yín háng"}],
        },
    )
    assert response.status_code == 200, response.text
    rules = response.json()["rules"]
    assert any(rule["source"] == "银行" for rule in rules)

    response = client.put(
        "/api/pronunciation-rules",
        json={
            "scope": series_id,
            "rules": [{"source": "重霄", "replacement": "chóng xiāo"}],
        },
    )
    rules = response.json()["rules"]
    assert any(rule["series_id"] == series_id for rule in rules)
    series_config = (workspace / series_id / "series_config.yaml").read_text("utf-8")
    assert "chóng xiāo" in series_config


# ---------------------------------------------------------------------------
# 调度端点
# ---------------------------------------------------------------------------


def test_rerun_unknown_episode_returns_404(client: TestClient) -> None:
    response = client.post("/api/episodes/wt_missing_ep_001/rerun", json={})
    assert response.status_code == 404


def test_jobs_batch_requires_items(client: TestClient) -> None:
    assert client.post("/api/jobs/batch", json={"items": []}).status_code == 400


def test_health_endpoint(client: TestClient) -> None:
    payload = client.get("/api/health").json()
    assert payload["ok"] is True
    assert payload["series"] == 0


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


def test_events_stream_pushes_published_log(client: TestClient, app) -> None:
    episode_id = "wt_local_src_ep_001"
    app.state.runner.bus.publish(episode_id, {"type": "log", "message": "流水线已启动"})
    with client.stream("GET", f"/api/episodes/{episode_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        collected = ""
        for line in response.iter_lines():
            collected += line + "\n"
            if "流水线已启动" in line:
                break
    assert "流水线已启动" in collected


# ---------------------------------------------------------------------------
# 后台调度器（铁律三：跨作品并发 + 部内 Stage 4 升序串行）
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stage4_gate_runs_in_episode_order(config: AppConfig) -> None:
    runner = JobRunner(config)
    gate = runner.gate("wt_x")
    order: list[int] = []

    def make(number: int):
        async def _run() -> None:
            await asyncio.sleep(0.01)
            order.append(number)

        return _run

    await asyncio.gather(gate.run(2, make(2)), gate.run(1, make(1)))
    assert order == [1, 2]


@pytest.mark.anyio
async def test_different_series_gates_run_in_parallel(config: AppConfig) -> None:
    runner = JobRunner(config)
    intervals: list[tuple[float, float]] = []

    def make():
        async def _run() -> None:
            started = time.monotonic()
            await asyncio.sleep(0.05)
            intervals.append((started, time.monotonic()))

        return _run

    await asyncio.gather(
        runner.gate("wt_a").run(1, make()),
        runner.gate("wt_b").run(1, make()),
    )
    assert len(intervals) == 2
    first, second = sorted(intervals)
    assert second[0] < first[1]  # 两部漫画的 Stage 4 时间窗相交


class _StubLLM:
    """假百炼客户端：按模型名返回视觉 JSON 或撰稿 JSON。"""

    def __init__(self) -> None:
        self.models: list[str] = []

    async def chat(
        self,
        messages: Any,
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        self.models.append(model)
        if "vl" in model or "vision" in model:
            payload = {
                "panels": [
                    {
                        "panel_id": "panel_001",
                        "scene_summary": "昏暗通道里有人在奔跑。",
                        "dialogues": [],
                        "sfx_list": [],
                        "focus_region": [0.0, 0.0, 1.0, 1.0],
                        "is_transition_blank": False,
                    }
                ]
            }
        else:
            payload = {
                "episode_id": "ep_001",
                "hook_title": "开始登塔",
                "beats": [
                    {
                        "beat_id": "b_001",
                        "narration_text": "少年在昏暗的通道里飞奔。",
                        "tts_text": None,
                        "emotion_instruction": "用急促的语气说",
                        "target_panel_ids": ["panel_001"],
                        "motion": "zoom_in",
                        "pause_after_ms": 250,
                    }
                ],
                "episode_digest": {
                    "episode_no": 1,
                    "one_line_hook": "登塔开始",
                    "summary": "少年进入通道",
                    "keywords": ["通道"],
                    "characters_present": ["少年"],
                },
                "character_updates": [],
                "new_plot_threads": [],
                "resolved_thread_ids": [],
            }
        return ChatResponse(content=json.dumps(payload, ensure_ascii=False), model=model)

    async def aclose(self) -> None:
        return None


@pytest.mark.anyio
async def test_job_runner_executes_stage1_to_stage4(
    config: AppConfig, workspace: Path
) -> None:
    source = _make_source(workspace, episodes=1)
    from stages import s0_catalog

    catalog = s0_catalog.import_local_catalog(source)
    registry = s0_catalog.CatalogRegistry(workspace)
    registry.register(catalog)

    stub = _StubLLM()
    runner = JobRunner(config, client_factory=lambda: stub)
    job = await runner.submit_episode(
        catalog.series_id, 1, from_stage="s1_crawler", mode="phase1_only"
    )
    assert job["episode_id"] == f"{catalog.series_id}_ep_001"
    await runner.wait_all()

    state = runner.state(job["episode_id"])
    assert state is not None
    assert state.status == "done", state.error
    episode_dir = workspace / catalog.series_id / "episodes" / "ep_001"
    assert len(list((episode_dir / "01_raw").glob("*.png"))) == 2
    assert (episode_dir / "03_vision" / "vision.json").is_file()
    assert (episode_dir / "04_script" / "script.json").is_file()
    # Patch v4.22：默认识图模型升级为 qwen3.7-plus。
    assert any("qwen3.7-plus" in model for model in stub.models)
    # Patch v3.3 Patch 15：默认撰稿模型已升级为 qwen3.7-plus。
    assert any("qwen3.7-plus" in model for model in stub.models)


@pytest.mark.anyio
async def test_failed_stage_is_marked_failed_in_state_and_manifest(
    config: AppConfig, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """阶段失败必须落到状态里：否则它永远停在 running，界面只显示「待执行」。"""
    source = _make_source(workspace, episodes=1)
    from stages import s0_catalog, s3_vision

    catalog = s0_catalog.import_local_catalog(source)
    registry = s0_catalog.CatalogRegistry(workspace)
    registry.register(catalog)

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("视觉模型炸了")

    monkeypatch.setattr(s3_vision, "run_stage3", boom)

    stub = _StubLLM()
    runner = JobRunner(config, client_factory=lambda: stub)
    job = await runner.submit_episode(
        catalog.series_id, 1, from_stage="s1_crawler", mode="phase1_only"
    )
    await runner.wait_all()

    state = runner.state(job["episode_id"])
    assert state is not None
    assert state.status == "failed"
    assert state.stages["s3_vision"]["status"] == "failed"
    assert "视觉模型炸了" in state.stages["s3_vision"]["error"]
    # 失败阶段没有耗时，前端必须显示「失败」而不是「待执行」。
    assert "duration_sec" not in state.stages["s3_vision"]

    manifest_path = (
        workspace / catalog.series_id / "episodes" / "ep_001" / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["stages"]["s3_vision"]["status"] == "failed"
    assert "视觉模型炸了" in manifest["stages"]["s3_vision"]["error"]
    # 前一个阶段成功过，也要留下记录。
    assert manifest["stages"]["s1_crawler"]["status"] == "done"


# ---------------------------------------------------------------------------
# Patch v3.0 小修：Web 控制台 Stage 4A 也必须走 story_model
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_job_runner_passes_default_story_model_to_stage4(
    config: AppConfig, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无作品级覆盖时，4A 故事反写透传全局 config.models.story_model。"""
    from stages import s0_catalog, s4_scriptwriter

    source = _make_source(workspace, episodes=1)
    catalog = s0_catalog.import_local_catalog(source)
    s0_catalog.CatalogRegistry(workspace).register(catalog)

    captured: dict[str, Any] = {}

    async def fake_run_stage4(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return None, None

    monkeypatch.setattr(s4_scriptwriter, "run_stage4", fake_run_stage4)

    runner = JobRunner(config, client_factory=lambda: _StubLLM())
    job = await runner.submit_episode(
        catalog.series_id, 1, from_stage="s4_scriptwriter", mode="phase1_only"
    )
    await runner.wait_all()

    state = runner.state(job["episode_id"])
    assert state is not None and state.status == "done", state.error
    assert captured["model"] == config.models.script
    assert captured["story_model"] == config.models.story_model
    assert captured["story_model"] == "qwen4-max"


@pytest.mark.anyio
async def test_job_runner_story_model_respects_series_override(
    config: AppConfig, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """series_config.yaml 的 model_overrides.story 优先于全局配置。"""
    from stages import s0_catalog, s4_scriptwriter

    source = _make_source(workspace, episodes=1)
    catalog = s0_catalog.import_local_catalog(source)
    registry = s0_catalog.CatalogRegistry(workspace)
    registry.register(catalog)
    (workspace / catalog.series_id / "series_config.yaml").write_text(
        "model_overrides:\n  story: custom-story-model\n", encoding="utf-8"
    )

    captured: dict[str, Any] = {}

    async def fake_run_stage4(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return None, None

    monkeypatch.setattr(s4_scriptwriter, "run_stage4", fake_run_stage4)

    runner = JobRunner(config, client_factory=lambda: _StubLLM())
    job = await runner.submit_episode(
        catalog.series_id, 1, from_stage="s4_scriptwriter", mode="phase1_only"
    )
    await runner.wait_all()

    state = runner.state(job["episode_id"])
    assert state is not None and state.status == "done", state.error
    assert captured["story_model"] == "custom-story-model"
    # script model 不受 story 覆盖影响。
    assert captured["model"] == config.models.script
