"""Patch v4.6：叙事节奏自动留白（auto-pacing）离线测试。

覆盖六组：
1. 引号引导词的短停顿任何情况下都不被拉长；
2. 多幕边界 / 全话收尾的跨幕留白 650ms；
3. 转折关键词 / 悬念短尾句 / sfx_cue 的 700ms 留白与最小间隔约束；
4. 连续长句累计超 11s 后在自然句末插 480ms 换气口；
5. write_episode_script 接入保护（<6 拍且非多幕不动显式停顿，>=6 拍才应用）；
6. Web API：PUT script 同步 audio_manifest 并传导到 S6 时间轴；
   POST auto-pacing 同时落盘 script.json 与 audio_manifest.json。

全部离线：纯函数 + respx mock LLM + TestClient，不触网、不消耗百炼额度。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from PIL import Image

from core.config import AppConfig, WorkspaceConfig
from core.memory import StoryBibleV2
from core.schemas import (
    CharacterProfile,
    EpisodeDigest,
    NarrationScript,
    PlotThread,
    ScriptBeat,
    StoryboardScene,
    VisionWindowOutput,
    load_model,
    save_model,
)
from providers.llm_bailian import BailianClient
from stages import s4_scriptwriter, s6_timeline
from stages.s4_scriptwriter import (
    PACE_BREATH_MS,
    PACE_LEAD_IN_MAX_MS,
    PACE_SCENE_END_MS,
    PACE_TURN_MS,
    PacingReport,
    apply_narrative_pacing,
)
from web.server import create_app
from fastapi.testclient import TestClient

CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


# --------------------------------------------------------------------------
# 造数助手
# --------------------------------------------------------------------------
def _beat(
    beat_id: str,
    text: str,
    *,
    pause: int = 150,
    role: str = "narrator",
    panels: list[str] | None = None,
    sfx: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "beat_id": beat_id,
        "narration_text": text,
        "tts_text": text,
        "speaker_role": role,
        "target_panel_ids": panels or ["panel_001"],
        "motion": "static",
        "pause_after_ms": pause,
    }
    if sfx is not None:
        payload["sfx_cue"] = sfx
    return payload


def _short_beats(count: int, *, pause: int = 150) -> list[dict[str, Any]]:
    """短句 beats：估算时长 ~900ms/句，累计不会触发 11s 换气规则。"""
    return [_beat(f"b{i}", f"短句{i}号。", pause=pause) for i in range(1, count + 1)]


def _long_beats(count: int) -> list[dict[str, Any]]:
    """长句 beats：每句 36 字 → 估算 7560ms，两句累计即破 11s 阈值。"""
    text = "这是一句足够长的叙述句子用来累积连续语音时长直到超过十一秒的阈值。"
    return [_beat(f"b{i}", text) for i in range(1, count + 1)]


def _two_scenes() -> list[StoryboardScene]:
    return [
        StoryboardScene(
            scene_id="s1",
            scene_index=0,
            title="第一幕",
            location="室内",
            panel_ids=["p1", "p2", "p3"],
            enabled=True,
        ),
        StoryboardScene(
            scene_id="s2",
            scene_index=1,
            title="第二幕",
            location="室外",
            panel_ids=["p4", "p5", "p6"],
            enabled=True,
        ),
    ]


# --------------------------------------------------------------------------
# 1. 引号引导词保护
# --------------------------------------------------------------------------
def test_lead_in_pause_never_extended() -> None:
    # 形态一：narrator 引导词以「：」结尾，即使后续满足转折/换气条件也保持 60ms。
    beats = _long_beats(6)
    beats[0] = _beat("b1", "道振平静说道：", pause=60)
    beats[1] = _beat("b2", "“没事的。”", pause=150, role="cherry")
    out, report = apply_narrative_pacing(beats)
    assert out[0]["pause_after_ms"] == 60
    assert "b1" not in report.changed_beat_ids
    assert out[0]["narration_text"] == "道振平静说道："

    # 形态二：短停顿（<=80）且下一条是角色台词（含中文引号），同样受保护。
    beats2 = _short_beats(6)
    beats2[0] = _beat("b1", "他接过话头", pause=PACE_LEAD_IN_MAX_MS)
    beats2[1] = _beat("b2", "“终于来了。”", role="narrator")
    out2, _ = apply_narrative_pacing(beats2)
    assert out2[0]["pause_after_ms"] == PACE_LEAD_IN_MAX_MS

    # 引导词即便带转折关键词也不允许被规则 B 拉长。
    beats3 = _short_beats(6)
    beats3[0] = _beat("b1", "然而他突然说道：", pause=40)
    beats3[1] = _beat("b2", "“走吧。”", role="ethan")
    out3, report3 = apply_narrative_pacing(beats3)
    assert out3[0]["pause_after_ms"] == 40
    assert "b1" not in report3.changed_beat_ids


# --------------------------------------------------------------------------
# 2. 跨幕留白 650ms
# --------------------------------------------------------------------------
def test_scene_boundary_pause_is_650() -> None:
    beats = [
        _beat(f"b{i}", f"短句{i}号。", panels=[f"p{i}"])
        for i in range(1, 7)
    ]
    out, report = apply_narrative_pacing(beats, scenes=_two_scenes())

    # b3 属幕 1、b4 属幕 2 → b3 跨幕留白；b6 是启用幕>=2 时的全话末条收尾留白。
    assert out[2]["pause_after_ms"] == PACE_SCENE_END_MS
    assert out[5]["pause_after_ms"] == PACE_SCENE_END_MS
    assert report.scene_end_count == 2
    assert set(report.changed_beat_ids) == {"b3", "b6"}

    # 只抬高不压低：原本更大的停顿保持原值。
    beats[2]["pause_after_ms"] = 800
    out2, report2 = apply_narrative_pacing(beats, scenes=_two_scenes())
    assert out2[2]["pause_after_ms"] == 800
    assert "b3" not in report2.changed_beat_ids


def test_disabled_scene_is_ignored() -> None:
    scenes = _two_scenes()
    scenes[1] = scenes[1].model_copy(update={"enabled": False})
    beats = [
        _beat(f"b{i}", f"短句{i}号。", panels=[f"p{i}"])
        for i in range(1, 7)
    ]
    _, report = apply_narrative_pacing(beats, scenes=scenes)
    # 只剩一个启用幕：无跨幕边界，末条也不满足「启用幕>=2」。
    assert report.scene_end_count == 0


# --------------------------------------------------------------------------
# 3. 转折 / 悬念 / 音效留白 700ms 与最小间隔约束
# --------------------------------------------------------------------------
def test_turn_signals_set_700() -> None:
    # 关键词
    beats = _short_beats(6)
    beats[3] = _beat("b4", "然而真相远比想象可怕。")
    out, report = apply_narrative_pacing(beats)
    assert out[3]["pause_after_ms"] == PACE_TURN_MS
    assert report.turn_count == 1

    # 悬念短尾句（去标点 <=16 字且以？！结尾）
    beats2 = _short_beats(6)
    beats2[3] = _beat("b4", "难道是他？！")
    out2, _ = apply_narrative_pacing(beats2)
    assert out2[3]["pause_after_ms"] == PACE_TURN_MS

    # 省略号悬念尾句
    beats3 = _short_beats(6)
    beats3[3] = _beat("b4", "门后竟然什么都没有……")
    out3, _ = apply_narrative_pacing(beats3)
    assert out3[3]["pause_after_ms"] == PACE_TURN_MS

    # sfx_cue 信号
    beats4 = _short_beats(6)
    beats4[3] = _beat("b4", "短句4号。", sfx="gasp")
    out4, report4 = apply_narrative_pacing(beats4)
    assert out4[3]["pause_after_ms"] == PACE_TURN_MS
    assert report4.changed_beat_ids == ["b4"]


def test_turn_pause_respects_min_beat_gap() -> None:
    # b4 命中转折后，b5 再出现转折信号，因间隔 < min_long_gap_beats=3 被抑制。
    beats = _short_beats(8)
    beats[3] = _beat("b4", "然而真相惊人。")
    beats[4] = _beat("b5", "突然灯灭了。")
    out, report = apply_narrative_pacing(beats)
    assert out[3]["pause_after_ms"] == PACE_TURN_MS
    assert out[4]["pause_after_ms"] == 150
    assert report.turn_count == 1
    assert report.changed_beat_ids == ["b4"]

    # 显式把 min_long_gap_beats 调为 1 后，相邻转折也允许命中。
    out2, report2 = apply_narrative_pacing(beats, min_long_gap_beats=1)
    assert out2[3]["pause_after_ms"] == PACE_TURN_MS
    assert out2[4]["pause_after_ms"] == PACE_TURN_MS
    assert report2.turn_count == 2


# --------------------------------------------------------------------------
# 4. 长连说换气口 480ms
# --------------------------------------------------------------------------
def test_long_streak_inserts_breath_pause() -> None:
    beats = _long_beats(6)
    out, report = apply_narrative_pacing(beats)
    assert report.breath_count >= 1
    first_changed = report.changed_beat_ids[0]
    idx = int(first_changed[1:]) - 1
    assert out[idx]["pause_after_ms"] == PACE_BREATH_MS
    # 换气口必须落在自然句末（。！？”……），长句均以「。」结尾。
    assert out[idx]["narration_text"].rstrip().endswith("。")

    # 真实 TTS 时长优先于字数估算：给足时长时两句即可破阈值。
    short = _short_beats(3)
    durations = {f"b{i}": 6000 for i in range(1, 4)}
    out2, report2 = apply_narrative_pacing(short, audio_durations_ms=durations)
    assert report2.breath_count == 1
    assert report2.changed_beat_ids == ["b2"]
    assert out2[1]["pause_after_ms"] == PACE_BREATH_MS


def test_pacing_is_pure_and_keeps_other_fields() -> None:
    beats = _long_beats(6)
    snapshot = json.loads(json.dumps(beats, ensure_ascii=False))
    out, report = apply_narrative_pacing(beats)

    # 入参不被修改
    assert beats == snapshot
    # 除 pause_after_ms 外字段逐条一致，beat 顺序不变
    for src, dst in zip(beats, out):
        for key in ("beat_id", "narration_text", "tts_text", "speaker_role", "motion"):
            assert src[key] == dst[key]
        assert src["target_panel_ids"] == dst["target_panel_ids"]
    assert [b["beat_id"] for b in out] == [b["beat_id"] for b in beats]
    assert isinstance(report, PacingReport)
    # 所有停顿夹在合法区间
    assert all(0 <= b["pause_after_ms"] <= 2000 for b in out)
    # 空列表安全早退
    empty_out, empty_report = apply_narrative_pacing([])
    assert empty_out == [] and empty_report.updated_count == 0


# --------------------------------------------------------------------------
# 5. write_episode_script / run_stage4 接入保护（mock LLM，不触网）
# --------------------------------------------------------------------------
async def _no_sleep(_seconds: float) -> None:
    return None


def _chat_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "qwen-plus",
            "choices": [
                {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        },
    )


def _script_envelope(beats: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "episode_id": "模型乱填的ID",
            "hook_title": "雨夜来客",
            "beats": beats,
            "episode_digest": {
                "episode_no": 999,
                "one_line_hook": "雨夜有人来访",
                "summary": "测试用摘要。",
                "keywords": ["雨夜"],
                "characters_present": ["小白"],
            },
            "character_updates": [],
            "new_plot_threads": [],
            "resolved_thread_ids": [],
        },
        ensure_ascii=False,
    )


def _two_beat_payload() -> str:
    return _script_envelope(
        [
            {
                "beat_id": "b1",
                "narration_text": "他把信纸攥成一团，指节发白。",
                "tts_text": "他把信纸攥成一团，指节发白。",
                "target_panel_ids": ["panel_001"],
                "motion": "zoom_in",
                "pause_after_ms": 320,
            },
            {
                "beat_id": "b2",
                "narration_text": "银行行长缓缓开口，声音很冷。",
                "tts_text": "银行行长缓缓开口，声音很冷。",
                "target_panel_ids": ["panel_002"],
                "motion": "static",
                "pause_after_ms": 200,
            },
        ]
    )


def _six_beat_payload() -> str:
    beats: list[dict[str, Any]] = []
    for i in range(1, 7):
        text = "然而真相就在这一封旧信里。" if i == 4 else f"第{i}句短解说。"
        beats.append(
            {
                "beat_id": f"b{i}",
                "narration_text": text,
                "tts_text": text,
                "target_panel_ids": ["panel_001" if i % 2 else "panel_002"],
                "motion": "static",
                "pause_after_ms": 200,
            }
        )
    return _script_envelope(beats)


def _overview_payload() -> dict[str, Any]:
    return {
        "hook_title": "整话通读钩子",
        "episode_digest": {
            "one_line_hook": "一句话钩子",
            "summary": "整话通读摘要",
            "keywords": ["雨夜"],
            "characters_present": ["小白"],
        },
        "character_updates": [],
        "new_plot_threads": [],
        "resolved_thread_ids": [],
    }


def _story_payload() -> dict[str, Any]:
    prose = "这一段讲的是雨夜里的相遇与试探，人物在沉默中交出底牌。"
    return {
        "episode_id": "wt_pace_ep_002",
        "hook_opening": "雨夜有人敲门。",
        "full_story_markdown": f"## 雨夜试探\n\n{prose}",
        "chapters": [
            {
                "chapter_index": 1,
                "chapter_title": "雨夜试探",
                "chapter_hint_range": ["panel_001", "panel_002"],
                "story_prose": prose,
            }
        ],
    }


def _dispatch(
    *,
    overview: httpx.Response,
    story: httpx.Response,
    beats: httpx.Response,
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content.decode("utf-8"))["messages"][-1]["content"]
        if "story_prose" in prompt:
            return story
        if "target_panel_ids" in prompt:
            return beats
        return overview

    return handler


def _make_series(tmp_path: Path) -> Path:
    series_dir = tmp_path / "workspace" / "wt_pace"
    series_dir.mkdir(parents=True)
    bible = StoryBibleV2(
        series_id="wt_pace",
        series_title="节奏测试漫画",
        characters=[
            CharacterProfile(
                name="小白",
                aliases=["阿白"],
                visual_traits="白发红瞳",
                current_status="神情紧张",
                first_seen_ep=1,
                last_seen_ep=1,
            )
        ],
        plot_threads=[
            PlotThread(
                thread_id="t_1",
                title="旧账本",
                description="账本缺页",
                introduced_ep=1,
            )
        ],
        episodes_index=[
            EpisodeDigest(
                episode_no=1,
                one_line_hook="雨夜里的信",
                summary="小白收到匿名信。",
                keywords=["雨夜"],
                characters_present=["小白"],
            )
        ],
    )
    save_model(bible, series_dir / "story_bible.json")
    snapshot_dir = series_dir / "bible_snapshots"
    snapshot_dir.mkdir(parents=True)
    save_model(bible, snapshot_dir / "ep_001.json")

    vision_dir = series_dir / "episodes" / "ep_002" / "03_vision"
    vision_dir.mkdir(parents=True)
    save_model(
        VisionWindowOutput.model_validate(
            {
                "panels": [
                    {
                        "panel_id": "panel_001",
                        "scene_summary": "雨夜街头，少年攥紧信纸",
                        "dialogues": [
                            {"speaker_guess": "小白", "text": "谁寄的？", "emotion": "tense"}
                        ],
                        "sfx_list": ["雨声"],
                    },
                    {
                        "panel_id": "panel_002",
                        "scene_summary": "黑衣人撑伞而立",
                        "dialogues": [],
                        "sfx_list": [],
                    },
                ]
            }
        ),
        vision_dir / "vision.json",
    )
    return series_dir


@pytest.mark.anyio
@respx.mock
async def test_run_stage4_short_script_keeps_explicit_pauses(tmp_path: Path) -> None:
    series_dir = _make_series(tmp_path)
    respx.post(CHAT_URL).mock(
        side_effect=_dispatch(
            overview=_chat_response(json.dumps(_overview_payload(), ensure_ascii=False)),
            story=_chat_response(json.dumps(_story_payload(), ensure_ascii=False)),
            beats=_chat_response(_two_beat_payload()),
        )
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, script_path = await s4_scriptwriter.run_stage4(
            series_dir,
            2,
            client,
            model="qwen-plus",
            episode_id="wt_pace_ep_002",
            series_title="节奏测试漫画",
        )

    # 仅 2 拍、非多幕：门槛不满足，显式停顿 320/200 原样保留。
    assert len(script.beats) == 2
    assert script.beats[0].pause_after_ms == 320
    assert script.beats[1].pause_after_ms == 200
    saved = load_model(NarrationScript, script_path)
    assert saved.beats[0].pause_after_ms == 320


@pytest.mark.anyio
@respx.mock
async def test_run_stage4_six_beat_script_applies_pacing(tmp_path: Path) -> None:
    series_dir = _make_series(tmp_path)
    respx.post(CHAT_URL).mock(
        side_effect=_dispatch(
            overview=_chat_response(json.dumps(_overview_payload(), ensure_ascii=False)),
            story=_chat_response(json.dumps(_story_payload(), ensure_ascii=False)),
            beats=_chat_response(_six_beat_payload()),
        )
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, _ = await s4_scriptwriter.run_stage4(
            series_dir,
            2,
            client,
            model="qwen-plus",
            episode_id="wt_pace_ep_002",
            series_title="节奏测试漫画",
        )

    # 6 拍满足门槛：b4 含「然而/真相」转折关键词，停顿被抬到 700ms。
    assert len(script.beats) == 6
    by_id = {beat.beat_id: beat for beat in script.beats}
    assert by_id["b4"].pause_after_ms == PACE_TURN_MS
    # 其它拍保持模型原值 200（短句不触发送气、无幕不触发跨幕）。
    assert by_id["b1"].pause_after_ms == 200


# --------------------------------------------------------------------------
# 6. Web API：audio_manifest 同步 + auto-pacing 路由
# --------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path / "ws"))
    return TestClient(create_app(config))


def _make_source(root: Path) -> Path:
    source = root / "src"
    folder = source / "ep_001"
    folder.mkdir(parents=True, exist_ok=True)
    for slice_index in (1, 2):
        image = Image.new("RGB", (400, 600), (30 * slice_index, 60, 90))
        image.save(folder / f"{slice_index:03d}.png")
    return source


def _seed_episode_artifacts(workspace: Path, series_id: str) -> Path:
    """铺出 auto-pacing 所需的 script.json / audio_manifest.json / 面板图。"""
    episode_dir = workspace / series_id / "episodes" / "ep_001"
    panels_dir = episode_dir / "02_panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (360, 640), (10, 20, 30)).save(panels_dir / "panel_001.png")
    Image.new("RGB", (360, 640), (20, 30, 40)).save(panels_dir / "panel_002.png")

    beats: list[ScriptBeat] = []
    for i in range(1, 7):
        text = "然而真相就在这一封旧信里。" if i == 4 else f"第{i}句短解说。"
        beats.append(
            ScriptBeat(
                beat_id=f"b{i}",
                narration_text=text,
                target_panel_ids=["panel_001" if i % 2 else "panel_002"],
                motion="static",
                pause_after_ms=200,
            )
        )
    script = NarrationScript(
        episode_id=f"{series_id}_ep_001",
        hook_title="节奏接口测试",
        beats=beats,
        episode_digest=EpisodeDigest(
            episode_no=1,
            one_line_hook="钩子",
            summary="摘要",
        ),
    )
    script_dir = episode_dir / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    save_model(script, script_dir / "script.json")
    (script_dir / "script_readable.md").write_text("# 解说文稿\n", encoding="utf-8")

    audio_dir = episode_dir / "05_audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    manifest = [
        {
            "beat_id": f"b{i}",
            "audio_path": f"05_audio/b{i}.wav",
            "duration_ms": 1000,
            "pause_after_ms": 200,
            "word_timestamps": [],
        }
        for i in range(1, 7)
    ]
    (audio_dir / "audio_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return episode_dir


def test_auto_pacing_404_without_script(client: TestClient, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    source = _make_source(workspace)
    series_id = client.post("/api/series", json={"url": str(source)}).json()["series_id"]
    response = client.post(f"/api/episodes/{series_id}_ep_001/script/auto-pacing")
    assert response.status_code == 404


def test_put_script_syncs_manifest_and_reaches_s6_timeline(
    client: TestClient, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    source = _make_source(workspace)
    series_id = client.post("/api/series", json={"url": str(source)}).json()["series_id"]
    episode_dir = _seed_episode_artifacts(workspace, series_id)
    episode_id = f"{series_id}_ep_001"

    baseline = s6_timeline.build_timeline_from_workspace(workspace / series_id, 1)

    # 人工把 b1 的停顿从 200 改成 800，PUT 全量 beats。
    script = load_model(NarrationScript, episode_dir / "04_script" / "script.json")
    edited = [beat.model_dump() for beat in script.beats]
    edited[0]["pause_after_ms"] = 800
    response = client.put(f"/api/episodes/{episode_id}/script", json={"beats": edited})
    assert response.status_code == 200, response.text

    manifest = json.loads(
        (episode_dir / "05_audio" / "audio_manifest.json").read_text(encoding="utf-8")
    )
    by_beat = {item["beat_id"]: item for item in manifest}
    assert by_beat["b1"]["pause_after_ms"] == 800
    # 其它字段不动
    assert by_beat["b1"]["duration_ms"] == 1000
    assert by_beat["b2"]["pause_after_ms"] == 200

    # S6 以 audio_manifest 为停顿唯一来源：新时间轴总时长随停顿增量变化。
    updated = s6_timeline.build_timeline_from_workspace(workspace / series_id, 1)
    delta = updated.total_duration_ms - baseline.total_duration_ms
    grid = s6_timeline.frame_ms(30)
    assert abs(delta - 600) <= grid + 1


def test_auto_pacing_endpoint_writes_script_and_manifest(
    client: TestClient, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    source = _make_source(workspace)
    series_id = client.post("/api/series", json={"url": str(source)}).json()["series_id"]
    episode_dir = _seed_episode_artifacts(workspace, series_id)
    episode_id = f"{series_id}_ep_001"

    response = client.post(f"/api/episodes/{episode_id}/script/auto-pacing")
    assert response.status_code == 200, response.text
    payload = response.json()
    summary = payload["summary"]
    assert set(summary) == {
        "updated_count",
        "scene_end_count",
        "turn_count",
        "breath_count",
        "changed_beat_ids",
        "audio_manifest_synced",
    }
    assert summary["turn_count"] >= 1
    assert "b4" in summary["changed_beat_ids"]
    assert summary["updated_count"] == len(summary["changed_beat_ids"])
    assert summary["audio_manifest_synced"] >= 1

    # script.json 与 audio_manifest.json 同时落盘且停顿一致。
    script = load_model(NarrationScript, episode_dir / "04_script" / "script.json")
    by_id = {beat.beat_id: beat for beat in script.beats}
    assert by_id["b4"].pause_after_ms == PACE_TURN_MS
    assert (episode_dir / "04_script" / "script_readable.md").is_file()

    manifest = json.loads(
        (episode_dir / "05_audio" / "audio_manifest.json").read_text(encoding="utf-8")
    )
    manifest_pause = {item["beat_id"]: item["pause_after_ms"] for item in manifest}
    assert manifest_pause["b4"] == PACE_TURN_MS
    for beat in script.beats:
        assert manifest_pause[beat.beat_id] == beat.pause_after_ms

    # 响应 beats 与 view.beats 同构（含 pause_after_ms），供前端直接刷新草稿。
    assert len(payload["beats"]) == 6
    assert all("pause_after_ms" in beat for beat in payload["beats"])


def test_sync_helper_supports_dict_container_and_missing_file(tmp_path: Path) -> None:
    from web.server import _sync_pause_to_audio_manifest

    episode_dir = tmp_path / "ep_001"

    # 清单不存在：安全返回 0，不报错。
    assert _sync_pause_to_audio_manifest(episode_dir, [_beat("b1", "短句1号。")]) == 0

    audio_dir = episode_dir / "05_audio"
    audio_dir.mkdir(parents=True)
    manifest_path = audio_dir / "audio_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "audios": [
                    {"beat_id": "b1", "duration_ms": 1000, "pause_after_ms": 100},
                    {"beat_id": "b2", "duration_ms": 1200, "pause_after_ms": 100},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    beats = [_beat("b1", "短句1号。", pause=650), _beat("b2", "短句2号。", pause=700)]
    synced = _sync_pause_to_audio_manifest(episode_dir, beats)
    assert synced == 2
    reloaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [item["pause_after_ms"] for item in reloaded["audios"]] == [650, 700]
    # 非停顿字段保持不变
    assert reloaded["audios"][0]["duration_ms"] == 1000
