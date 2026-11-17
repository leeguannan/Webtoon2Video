"""Stage 4（``stages/s4_scriptwriter.py``）离线测试。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from core.memory import StoryBibleManager
from core.normalize import normalize_script_payload
from core.schemas import (
    CameraMotion,
    CharacterProfile,
    EpisodeDigest,
    EpisodeStoryDraft,
    NarrationScript,
    PanelVisionInfo,
    PlotThread,
    RenderTimeline,
    ScriptBeat,
    SfxEvent,
    StoryBibleV2,
    VisionWindowOutput,
    load_model,
    save_model,
)
from providers.llm_bailian import BailianClient
from stages import s3_vision, s4_scriptwriter

CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


async def _no_sleep(_seconds: float) -> None:
    return None


def _script_payload() -> str:
    return json.dumps(
        {
            "episode_id": "模型乱填的ID",
            "hook_title": "雨夜来客",
            "beats": [
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
                    "tts_text": "银行行长缓缓开口，声音十分冰冷。",
                    "target_panel_ids": ["panel_002"],
                    "motion": "static",
                    "pause_after_ms": 200,
                },
            ],
            "episode_digest": {
                "episode_no": 999,
                "one_line_hook": "雨夜有人来访",
                "summary": "小白收到一封匿名信，来客自称银行行长。",
                "keywords": ["雨夜", "匿名信"],
                "characters_present": ["小白"],
            },
            "character_updates": [
                {
                    "name": "小白",
                    "aliases": ["阿白"],
                    "visual_traits": "白发红瞳，左颊有疤",
                    "current_status": "神情紧张",
                    "first_seen_ep": 1,
                    "last_seen_ep": 2,
                }
            ],
            "new_plot_threads": [
                {
                    "thread_id": "t_2",
                    "title": "黑色信封",
                    "description": "信封上有一枚奇怪印章",
                    "introduced_ep": 2,
                }
            ],
            "resolved_thread_ids": [],
        },
        ensure_ascii=False,
    )


def _script_response(content: str, *, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "qwen-plus",
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        },
    )


def _overview_payload() -> dict:
    return {
        "hook_title": "整话通读钩子",
        "episode_digest": {
            "one_line_hook": "一句话钩子",
            "summary": "整话通读后得到的剧情摘要",
            "keywords": ["雨夜"],
            "characters_present": ["小白"],
        },
        "character_updates": [],
        "new_plot_threads": [],
        "resolved_thread_ids": [],
    }


def _beats_payload(panel_ids: list[str]) -> dict:
    return {
        "beats": [
            {
                "beat_id": "b1",
                "narration_text": f"{panel_id} 的解说句要写够十个字以上。",
                "target_panel_ids": [panel_id],
                "motion": "static",
                "pause_after_ms": 200,
            }
            for panel_id in panel_ids
        ]
    }


def _story_payload(panel_ids: list[str]) -> dict:
    """Stage 4A 的故事反写稿（章节稿，Patch v3.0-7 新 Schema）。"""
    first = panel_ids[0] if panel_ids else "panel_001"
    last = panel_ids[-1] if panel_ids else "panel_001"
    prose = "这一段讲的是雨夜里的相遇与试探，人物在沉默中一点点交出底牌。"
    return {
        "episode_id": "wt_test_ep_002",
        "hook_opening": "雨夜有人敲门，来者不善。",
        "full_story_markdown": f"## 雨夜试探\n\n{prose}",
        "chapters": [
            {
                "chapter_index": 1,
                "chapter_title": "雨夜试探",
                "panel_hint_range": [first, last],
                "story_prose": prose,
            }
        ],
    }


def _dispatch(
    *,
    overview: list[httpx.Response] | None = None,
    story: list[httpx.Response] | None = None,
    beats: list[httpx.Response] | None = None,
):
    """按请求内容把响应分派到「概览 / 故事反写 / 分批解说」三段。

    Patch v2.6-5 之后一次 Stage 4 会发三种请求：
    概览（含 ``hook_title``）、故事反写（含 ``story_prose``）、分批解说（含 ``target_panel_ids``）。
    """
    buckets = {
        "overview": list(overview or []),
        "story": list(story or []),
        "beats": list(beats or []),
    }
    counters = {"overview": 0, "story": 0, "beats": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        prompt = body["messages"][-1]["content"]
        if "story_prose" in prompt:
            key = "story"
        elif "target_panel_ids" in prompt:
            key = "beats"
        else:
            key = "overview"
        items = buckets[key]
        if not items:
            raise AssertionError(f"没有为 {key} 段准备响应")
        index = counters[key]
        counters[key] += 1
        return items[index] if index < len(items) else items[-1]

    return handler


def _make_series(
    tmp_path: Path,
    *,
    episode_no: int = 2,
    with_snapshot: bool = True,
    digests: list[EpisodeDigest] | None = None,
) -> Path:
    """构造一部带记忆体的漫画目录。"""
    series_dir = tmp_path / "workspace" / "wt_test"
    series_dir.mkdir(parents=True)
    bible = StoryBibleV2(
        series_id="wt_test",
        series_title="测试漫画",
        characters=[
            CharacterProfile(
                name="小白",
                aliases=["阿白"],
                visual_traits="白发红瞳，左颊有疤",
                current_status="浑身湿透",
                first_seen_ep=1,
                last_seen_ep=1,
            )
        ],
        plot_threads=[
            PlotThread(
                thread_id="t_1",
                title="母亲的旧账本",
                description="账本最后一页被撕掉了",
                introduced_ep=1,
            )
        ],
        episodes_index=digests
        if digests is not None
        else [
            EpisodeDigest(
                episode_no=1,
                one_line_hook="雨夜里的信",
                summary="小白在雨夜收到一封匿名信。",
                keywords=["雨夜", "匿名信"],
                characters_present=["小白"],
            )
        ],
    )
    save_model(bible, series_dir / "story_bible.json")
    if with_snapshot:
        snapshot_dir = series_dir / "bible_snapshots"
        snapshot_dir.mkdir(parents=True)
        save_model(bible, snapshot_dir / "ep_001.json")

    vision_dir = series_dir / "episodes" / f"ep_{episode_no:03d}" / "03_vision"
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
async def test_run_stage4_writes_script_and_readable(tmp_path) -> None:
    series_dir = _make_series(tmp_path)
    respx.post(CHAT_URL).mock(return_value=_script_response(_script_payload()))
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, script_path = await s4_scriptwriter.run_stage4(
            series_dir,
            2,
            client,
            model="qwen-plus",
            episode_id="wt_test_ep_002",
            series_title="测试漫画",
        )
    assert script_path == series_dir / "episodes" / "ep_002" / "04_script" / "script.json"
    assert script_path.is_file()
    readable_path = script_path.parent / "script_readable.md"
    assert readable_path.is_file()

    saved = load_model(NarrationScript, script_path)
    assert saved.episode_id == "wt_test_ep_002"  # 以流水线为准，覆盖模型乱填的值
    assert saved.episode_digest.episode_no == 2  # 同理
    assert len(saved.beats) == 2
    assert saved.beats[0].pause_after_ms == 320
    assert saved.beats[1].motion.value == "static"

    readable = readable_path.read_text(encoding="utf-8")
    assert "旁白（字幕）" in readable
    assert "配音文本" in readable
    assert "雨夜来客" in readable


@pytest.mark.anyio
@respx.mock
async def test_tts_text_never_pollutes_narration_text(tmp_path) -> None:
    """Patch v3.0-7：模型在 tts_text 写拼音时，字幕 narration_text 原样保留，
    拼音只在送音文本里被剥离兜底，两条文本始终分离。"""
    series_dir = _make_series(tmp_path)
    payload = json.loads(_script_payload())
    # 模拟模型违规在配音文本里写拼音（脱敏字的错误写法）。
    payload["beats"][1]["tts_text"] = "银行 hùn xiǎo 缓缓开口，声音很冷。"
    respx.post(CHAT_URL).mock(
        return_value=_script_response(json.dumps(payload, ensure_ascii=False))
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, script_path = await s4_scriptwriter.run_stage4(
            series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
        )
    # 字幕文本一个字都不能被送音逻辑改动。
    assert script.beats[1].narration_text == "银行行长缓缓开口，声音很冷。"
    # 拼音被兜底剥离（hùn/xiǎo 消失），剩下的汉字保留；不会反过来污染字幕。
    assert script.beats[1].tts_text is not None
    assert "hùn" not in script.beats[1].tts_text and "xiǎo" not in script.beats[1].tts_text
    assert "银行" in script.beats[1].tts_text

    raw = json.loads(script_path.read_text(encoding="utf-8"))
    assert raw["beats"][1]["narration_text"] == "银行行长缓缓开口，声音很冷。"
    readable = (script_path.parent / "script_readable.md").read_text(encoding="utf-8")
    assert "旁白（字幕）**：银行行长缓缓开口，声音很冷。" in readable


@pytest.mark.anyio
@respx.mock
async def test_stage4_prompt_uses_snapshot_base_and_memory_layers(tmp_path) -> None:
    series_dir = _make_series(tmp_path)
    route = respx.post(CHAT_URL).mock(return_value=_script_response(_script_payload()))
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        await s4_scriptwriter.run_stage4(
            series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
        )
    payload = route.calls[0].request.content.decode("utf-8")
    assert "snapshot:ep_001" in payload  # 强制读取上一话只读快照
    assert "【角色图谱】" in payload and "小白" in payload
    assert "【近况详摘】" in payload
    assert "【未回收伏笔】" in payload and "t_1" in payload
    assert "panel_001" in payload  # Stage 3 画面信息进入撰稿上下文
    assert '"model": "qwen-plus"' in payload or '"model":"qwen-plus"' in payload


@pytest.mark.anyio
@respx.mock
async def test_stage4_falls_back_to_live_filtered_without_snapshot(tmp_path) -> None:
    series_dir = _make_series(tmp_path, episode_no=1, with_snapshot=False)
    route = respx.post(CHAT_URL).mock(return_value=_script_response(_script_payload()))
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        await s4_scriptwriter.run_stage4(
            series_dir, 1, client, model="qwen-plus", episode_id="wt_test_ep_001"
        )
    payload = route.calls[0].request.content.decode("utf-8")
    assert "live:filtered" in payload


@pytest.mark.anyio
@respx.mock
async def test_stage4_updates_memory_and_writes_snapshot(tmp_path) -> None:
    series_dir = _make_series(tmp_path)
    respx.post(CHAT_URL).mock(return_value=_script_response(_script_payload()))
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        await s4_scriptwriter.run_stage4(
            series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
        )
    snapshot_path = series_dir / "bible_snapshots" / "ep_002.json"
    assert snapshot_path.is_file()
    snapshot = load_model(StoryBibleV2, snapshot_path)
    assert [item.episode_no for item in snapshot.episodes_index] == [1, 2]
    assert any(item.name == "小白" and item.last_seen_ep == 2 for item in snapshot.characters)
    assert any(item.thread_id == "t_2" for item in snapshot.plot_threads)

    live = load_model(StoryBibleV2, series_dir / "story_bible.json")
    assert [item.episode_no for item in live.episodes_index] == [1, 2]
    assert 2 in live.snapshots_ep_nos


@pytest.mark.anyio
@respx.mock
async def test_stage4_marks_resolved_thread(tmp_path) -> None:
    series_dir = _make_series(tmp_path)
    payload = json.loads(_script_payload())
    payload["resolved_thread_ids"] = ["t_1"]
    respx.post(CHAT_URL).mock(return_value=_script_response(json.dumps(payload, ensure_ascii=False)))
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        await s4_scriptwriter.run_stage4(
            series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
        )
    snapshot = load_model(StoryBibleV2, series_dir / "bible_snapshots" / "ep_002.json")
    thread = next(item for item in snapshot.plot_threads if item.thread_id == "t_1")
    assert thread.status == "resolved"
    assert thread.resolved_ep == 2


@pytest.mark.anyio
@respx.mock
async def test_arc_compressor_runs_only_on_archive_episode(tmp_path) -> None:
    digests = [
        EpisodeDigest(
            episode_no=index,
            one_line_hook=f"第{index}话钩子",
            summary=f"第{index}话摘要",
            characters_present=["小白"],
        )
        for index in range(1, 10)
    ]
    series_dir = _make_series(tmp_path, episode_no=10, with_snapshot=False, digests=digests)
    respx.post(CHAT_URL).mock(return_value=_script_response(_script_payload()))
    captured: list[str] = []

    async def compressor(raw: str) -> str:
        captured.append(raw)
        return "压缩后的阶段剧情"

    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        await s4_scriptwriter.run_stage4(
            series_dir,
            10,
            client,
            model="qwen-plus",
            episode_id="wt_test_ep_010",
            arc_compressor=compressor,
        )
    assert captured, "第 10 话应触发篇章归档"
    assert "第1话钩子" in captured[0]
    snapshot = load_model(StoryBibleV2, series_dir / "bible_snapshots" / "ep_010.json")
    arc = snapshot.arc_summaries[0]
    assert arc.ep_range == (1, 10)
    assert arc.arc_name == "第1-10话篇章"
    assert arc.summary == "压缩后的阶段剧情"


@pytest.mark.anyio
@respx.mock
async def test_arc_compressor_not_called_on_regular_episode(tmp_path) -> None:
    series_dir = _make_series(tmp_path, episode_no=3, with_snapshot=False)
    respx.post(CHAT_URL).mock(return_value=_script_response(_script_payload()))
    calls: list[str] = []

    async def compressor(raw: str) -> str:
        calls.append(raw)
        return "不该被调用"

    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        await s4_scriptwriter.run_stage4(
            series_dir,
            3,
            client,
            model="qwen-plus",
            episode_id="wt_test_ep_003",
            arc_compressor=compressor,
        )
    assert calls == []


@pytest.mark.anyio
@respx.mock
async def test_invalid_script_payload_raises(tmp_path) -> None:
    series_dir = _make_series(tmp_path)
    respx.post(CHAT_URL).mock(
        return_value=_script_response(json.dumps({"episode_id": "x", "hook_title": "y"}))
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        with pytest.raises(s4_scriptwriter.ScriptWriterError):
            await s4_scriptwriter.run_stage4(
                series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
            )


def test_render_readable_script_lists_memory_baseline() -> None:
    script = NarrationScript.model_validate(json.loads(_script_payload()))
    readable = s4_scriptwriter.render_readable_script(script, base_source="snapshot:ep_001")
    assert "记忆基线" in readable
    assert "snapshot:ep_001" in readable
    assert "旁白（字幕）" in readable


def test_render_readable_script_shows_plain_motion_value() -> None:
    """可读版给人工审稿看，运镜要写 `zoom_in` 而不是枚举 repr。"""
    script = NarrationScript.model_validate(json.loads(_script_payload()))
    readable = s4_scriptwriter.render_readable_script(script)
    assert "CameraMotion." not in readable
    assert "运镜 `zoom_in`" in readable


def test_manager_snapshot_round_trip(tmp_path) -> None:
    series_dir = _make_series(tmp_path)
    manager = StoryBibleManager(series_dir, series_id="wt_test", series_title="测试漫画")
    base, source = manager.resolve_base_bible(2)
    assert source == "snapshot:ep_001"
    assert base.series_id == "wt_test"


@pytest.mark.anyio
@respx.mock
async def test_stage4_normalizes_out_of_range_motion_and_panel_refs(tmp_path) -> None:
    """模型给的 motion 大写、target_panel_ids 指向不存在面板时应收敛而不是崩掉。"""
    series_dir = _make_series(tmp_path)
    payload = {
        "episode_id": "模型乱填",
        "hook_title": "塔门初启",
        "beats": [
            {
                "narration_text": "少年握棍突进，蓝光迸射",
                "target_panel_ids": ["panel_001", "panel_999"],
                "motion": "ZOOM_IN",
                "pause_after_ms": 9999,
            }
        ],
        "episode_digest": {"one_line_hook": "钩子", "summary": "概要"},
    }
    respx.post(CHAT_URL).mock(
        return_value=_script_response(json.dumps(payload, ensure_ascii=False))
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, _path = await s4_scriptwriter.run_stage4(
            series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
        )
    assert script.beats[0].motion == CameraMotion.ZOOM_IN
    assert script.beats[0].pause_after_ms == 2000
    # 不存在的面板被过滤
    assert script.beats[0].target_panel_ids == ["panel_001"]
    # 话次 ID 以流水线为准
    assert script.episode_id == "wt_test_ep_002"


def test_chunk_panels_splits_by_fixed_size() -> None:
    panels = [
        PanelVisionInfo.model_validate(
            {"panel_id": f"panel_{index:03d}", "scene_summary": "画面", "dialogues": [], "sfx_list": []}
        )
        for index in range(1, 33)
    ]
    batches = s4_scriptwriter.chunk_panels(panels, 15)
    assert [len(batch) for batch in batches] == [15, 15, 2]
    assert batches[0][0].panel_id == "panel_001"
    assert batches[-1][-1].panel_id == "panel_032"
    with pytest.raises(ValueError):
        s4_scriptwriter.chunk_panels(panels, 0)


def test_chapter_beat_budget_splits_total_across_chapters() -> None:
    """Patch v3.1 Patch 10：全话条数预算收紧到 35~50 条，按章平分，单章至少 3 条。"""
    assert s4_scriptwriter._chapter_beat_budget(1) == (35, 50)
    # 6 章：下限 35//6=5，上限 ceil(50/6)=9，合计上限 54 ≈ 全话 50。
    assert s4_scriptwriter._chapter_beat_budget(6) == (5, 9)
    # 章数极多时下限被托底为 3。
    low, high = s4_scriptwriter._chapter_beat_budget(20)
    assert low == 3 and high >= low


def test_split_budget_halves_text_budget() -> None:
    assert s4_scriptwriter._split_budget("5~11 条", 2) == ["2~6 条", "2~6 条"]
    # 解析不出数字时首片保留原文，其余给空串。
    assert s4_scriptwriter._split_budget("自行决定", 2) == ["自行决定", ""]


def test_filter_feedback_for_window_routes_errors_by_panel() -> None:
    """顺位错误按 panel 归属章节；拼音/空绑定类无 panel 错误全局广播。"""
    errors = [
        "第 12 条解说把镜头拉回了前面的画面（p_005），分镜顺序必须单调不减",
        "第 58 条解说把镜头拉回了前面的画面（p_047），分镜顺序必须单调不减",
        "第 3 条解说的配音文本含拼音「hùn」，请改写成标准汉字同音词",
    ]
    first_chapter = s4_scriptwriter._filter_feedback_for_window(
        errors, ["p_001", "p_005", "p_010"]
    )
    # 第 1 章只收到自己窗口里的 p_005 错误；p_047 被过滤；拼音错误无 panel，保留。
    assert any("p_005" in item for item in first_chapter)
    assert not any("p_047" in item for item in first_chapter)
    assert any("拼音" in item for item in first_chapter)

    second_chapter = s4_scriptwriter._filter_feedback_for_window(
        errors, ["p_047", "p_050"]
    )
    assert any("p_047" in item for item in second_chapter)
    assert not any("p_005" in item for item in second_chapter)
    # 拼音错误同样广播给第 2 章。
    assert any("拼音" in item for item in second_chapter)


@pytest.mark.anyio
@respx.mock
async def test_narrate_prompt_carries_beat_budget(tmp_path) -> None:
    """4B 请求体必须带上本章条数预算，约束模型不要超 30~65 条总量。"""
    series_dir = _make_series(tmp_path)
    route = respx.post(CHAT_URL).mock(
        side_effect=_dispatch(
            overview=[_script_response(json.dumps(_overview_payload(), ensure_ascii=False))],
            story=[
                _script_response(
                    json.dumps(_story_payload(["panel_001", "panel_002"]), ensure_ascii=False)
                )
            ],
            beats=[
                _script_response(
                    json.dumps(
                        _beats_payload(["panel_001", "panel_002"]), ensure_ascii=False
                    )
                )
            ],
        )
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        await s4_scriptwriter.run_stage4(
            series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
        )
    narrate_request = route.calls[2].request.content.decode("utf-8")
    # 单章预算即全话预算 35~50（Patch v3.1 Patch 10 收紧）。
    assert "35~50 条" in narrate_request
    assert "本章解说条数预算" in narrate_request


@pytest.mark.anyio
@respx.mock
async def test_two_stage_overview_then_batched_beats(tmp_path) -> None:
    """第一段整话通读只出元数据，第二段故事反写，第三段按章节出解说句。"""
    series_dir = _make_series(tmp_path)
    route = respx.post(CHAT_URL).mock(
        side_effect=_dispatch(
            overview=[
                _script_response(json.dumps(_overview_payload(), ensure_ascii=False))
            ],
            story=[
                _script_response(
                    json.dumps(_story_payload(["panel_001", "panel_002"]), ensure_ascii=False)
                )
            ],
            beats=[
                _script_response(
                    json.dumps(
                        _beats_payload(["panel_001", "panel_002"]), ensure_ascii=False
                    )
                )
            ],
        )
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, _path = await s4_scriptwriter.run_stage4(
            series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
        )
    assert route.call_count == 3  # 概览 + 故事反写 + 章节拆拍（单章一次）
    first_request = route.calls[0].request.content.decode("utf-8")
    story_request = route.calls[1].request.content.decode("utf-8")
    narrate_request = route.calls[2].request.content.decode("utf-8")
    # 概览段：完整记忆基线 + 全部面板
    assert "snapshot:ep_001" in first_request
    assert "panel_001" in first_request and "panel_002" in first_request
    # 故事反写段：整话通读，要求章节覆盖全部分镜
    assert "panel_001" in story_request and "panel_002" in story_request
    # 章节拆拍段：以故事稿章节为锚点，候选分镜只给本章窗口（不再有 PANEL_RANGE 批头）
    assert "雨夜试探" in narrate_request  # CHAPTER_TITLE
    assert "雨夜里的相遇与试探" in narrate_request  # CHAPTER_STORY
    assert "panel_001" in narrate_request and "panel_002" in narrate_request
    assert "panel_001 ~ panel_002" not in narrate_request  # 旧批次区间头已移除
    assert script.hook_title == "整话通读钩子"
    assert script.episode_digest.summary == "整话通读后得到的剧情摘要"
    assert len(script.beats) == 2


@pytest.mark.anyio
@respx.mock
async def test_long_episode_is_split_into_batches_with_global_beat_ids(tmp_path) -> None:
    """32 格面板 → 1 次概览 + 1 次故事反写（3 章）+ 3 次章节拆拍；beat_id 全局唯一重排。

    Story-First 下不再按固定 15 格分批，而是 4A 把整话切成若干章节，4B 逐章
    在自己的候选窗口里挑图（允许跳过窗口内大部分分镜）。
    """
    series_dir = _make_series(tmp_path)
    vision_dir = series_dir / "episodes" / "ep_002" / "03_vision"
    panel_ids = [f"panel_{index:03d}" for index in range(1, 33)]
    save_model(
        VisionWindowOutput.model_validate(
            {
                "panels": [
                    {
                        "panel_id": panel_id,
                        "scene_summary": f"画面{index}",
                        "dialogues": [],
                        "sfx_list": [],
                    }
                    for index, panel_id in enumerate(panel_ids, start=1)
                ]
            }
        ),
        vision_dir / "vision.json",
    )
    story_payload = {
        "episode_id": "wt_test_ep_002",
        "hook_opening": "雨夜有人敲门，来者不善。",
        "full_story_markdown": "## 第一章\n\n起\n\n## 第二章\n\n承\n\n## 第三章\n\n合",
        "chapters": [
            {
                "chapter_index": 1,
                "chapter_title": "第一章 · 起",
                "panel_hint_range": ["panel_001", "panel_010"],
                "story_prose": "雨夜试探的开端，主角在沉默中观察来人。",
            },
            {
                "chapter_index": 2,
                "chapter_title": "第二章 · 承",
                "panel_hint_range": ["panel_011", "panel_021"],
                "story_prose": "对话逐渐收紧，底牌被一点点逼出来。",
            },
            {
                "chapter_index": 3,
                "chapter_title": "第三章 · 合",
                "panel_hint_range": ["panel_022", "panel_032"],
                "story_prose": "真相揭开，主角做出抉择。",
            },
        ],
    }
    route = respx.post(CHAT_URL).mock(
        side_effect=_dispatch(
            overview=[
                _script_response(json.dumps(_overview_payload(), ensure_ascii=False))
            ],
            story=[
                _script_response(json.dumps(story_payload, ensure_ascii=False))
            ],
            beats=[
                # 每章只挑窗口里的一个代表格，显式覆盖「允许跳过 30%~70% 分镜」。
                _script_response(json.dumps(_beats_payload(["panel_001"]), ensure_ascii=False)),
                _script_response(json.dumps(_beats_payload(["panel_011"]), ensure_ascii=False)),
                _script_response(json.dumps(_beats_payload(["panel_022"]), ensure_ascii=False)),
            ],
        )
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, _path = await s4_scriptwriter.run_stage4(
            series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
        )
    assert route.call_count == 5  # 1 概览 + 1 故事反写 + 3 章拆拍
    narrate_requests = [call.request.content.decode("utf-8") for call in route.calls[2:]]
    # 章节窗口互不重叠、顺序递增：第一章看不到后两章的分镜。
    assert "第一章 · 起" in narrate_requests[0]
    assert "panel_001" in narrate_requests[0] and "panel_011" not in narrate_requests[0]
    assert "第二章 · 承" in narrate_requests[1] and "panel_011" in narrate_requests[1]
    assert "第三章 · 合" in narrate_requests[2] and "panel_032" in narrate_requests[2]
    # 每章模型都返回 b1，合并后必须全局重排为 b1..b3，不能重复。
    assert [beat.beat_id for beat in script.beats] == ["b1", "b2", "b3"]
    # 三条解说分别锚定三个递增的分镜，全局单调不减。
    assert [beat.target_panel_ids[0] for beat in script.beats] == [
        "panel_001",
        "panel_011",
        "panel_022",
    ]


@pytest.mark.anyio
@respx.mock
async def test_chapter_with_45_panels_is_not_split_under_new_limit(tmp_path) -> None:
    """Patch v3.0 小修：CHAPTER_MAX_PANELS 40→120。

    旧上限下 45 格的单章被切成两批 4B，而两批 prompt 都带着同一份
    chapter.story_prose，等于把同一段故事讲两遍。新上限内必须一次调用讲完。
    """
    assert s4_scriptwriter.CHAPTER_MAX_PANELS == 120
    series_dir = _make_series(tmp_path)
    vision_dir = series_dir / "episodes" / "ep_002" / "03_vision"
    panel_ids = [f"panel_{index:03d}" for index in range(1, 46)]
    save_model(
        VisionWindowOutput.model_validate(
            {
                "panels": [
                    {
                        "panel_id": panel_id,
                        "scene_summary": f"画面{index}",
                        "dialogues": [],
                        "sfx_list": [],
                    }
                    for index, panel_id in enumerate(panel_ids, start=1)
                ]
            }
        ),
        vision_dir / "vision.json",
    )
    story_payload = {
        "episode_id": "wt_test_ep_002",
        "hook_opening": "雨夜有人敲门，来者不善。",
        "full_story_markdown": "## 第一章 · 整章\n\n一整章的故事。",
        "chapters": [
            {
                "chapter_index": 1,
                "chapter_title": "第一章 · 整章",
                "panel_hint_range": ["panel_001", "panel_045"],
                "story_prose": "四十五格只讲这一遍的完整故事。",
            }
        ],
    }
    route = respx.post(CHAT_URL).mock(
        side_effect=_dispatch(
            overview=[
                _script_response(json.dumps(_overview_payload(), ensure_ascii=False))
            ],
            story=[
                _script_response(json.dumps(story_payload, ensure_ascii=False))
            ],
            # 只准备一个 4B 响应：若被错误切片，第二次调用会取同一个响应，
            # 但 call_count 与请求内容断言仍会抓住切片行为。
            beats=[
                _script_response(
                    json.dumps(
                        _beats_payload(["panel_001", "panel_045"]), ensure_ascii=False
                    )
                )
            ],
        )
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        await s4_scriptwriter.run_stage4(
            series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
        )
    # 1 概览 + 1 故事反写 + 1 次章节拆拍（旧逻辑会是 4 次：拆拍被切成两片）。
    assert route.call_count == 3
    narrate_request = route.calls[2].request.content.decode("utf-8")
    # 唯一一次 4B 调用必须看得到整章首尾分镜，故事正文也只出现这一次。
    assert "panel_001" in narrate_request and "panel_045" in narrate_request
    assert narrate_request.count("四十五格只讲这一遍的完整故事") == 1


@pytest.mark.anyio
@respx.mock
async def test_retry_feedback_is_routed_to_owning_chapter_only(tmp_path) -> None:
    """真机回归：第 2 章的顺位错误在重写轮只能进入第 2 章请求，
    不能广播给候选窗口里根本没有该 panel 的其他章。"""
    series_dir = _make_series(tmp_path)
    vision_dir = series_dir / "episodes" / "ep_002" / "03_vision"
    panel_ids = [f"panel_{index:03d}" for index in range(1, 33)]
    save_model(
        VisionWindowOutput.model_validate(
            {
                "panels": [
                    {
                        "panel_id": panel_id,
                        "scene_summary": f"画面{index}",
                        "dialogues": [],
                        "sfx_list": [],
                    }
                    for index, panel_id in enumerate(panel_ids, start=1)
                ]
            }
        ),
        vision_dir / "vision.json",
    )
    story_payload = {
        "episode_id": "wt_test_ep_002",
        "hook_opening": "雨夜有人敲门，来者不善。",
        "full_story_markdown": "## 第一章\n\n起\n\n## 第二章\n\n承\n\n## 第三章\n\n合",
        "chapters": [
            {"chapter_index": 1, "chapter_title": "第一章 · 起",
             "panel_hint_range": ["panel_001", "panel_010"],
             "story_prose": "开端。"},
            {"chapter_index": 2, "chapter_title": "第二章 · 承",
             "panel_hint_range": ["panel_011", "panel_021"],
             "story_prose": "对峙。"},
            {"chapter_index": 3, "chapter_title": "第三章 · 合",
             "panel_hint_range": ["panel_022", "panel_032"],
             "story_prose": "结局。"},
        ],
    }
    route = respx.post(CHAT_URL).mock(
        side_effect=_dispatch(
            overview=[_script_response(json.dumps(_overview_payload(), ensure_ascii=False))],
            story=[_script_response(json.dumps(story_payload, ensure_ascii=False))],
            beats=[
                # 第 1 轮：章 1 合法、章 2 回头违规（先 015 再 011）、章 3 合法。
                _script_response(json.dumps(_beats_payload(["panel_001"]), ensure_ascii=False)),
                _script_response(
                    json.dumps(_beats_payload(["panel_015", "panel_011"]), ensure_ascii=False)
                ),
                _script_response(json.dumps(_beats_payload(["panel_022"]), ensure_ascii=False)),
                # 第 2 轮：三章全部合法，错误清零后结束重试。
                _script_response(json.dumps(_beats_payload(["panel_001"]), ensure_ascii=False)),
                _script_response(json.dumps(_beats_payload(["panel_011"]), ensure_ascii=False)),
                _script_response(json.dumps(_beats_payload(["panel_022"]), ensure_ascii=False)),
            ],
        )
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        await s4_scriptwriter.run_stage4(
            series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
        )
    # calls: 0=overview 1=story 2..4=第1轮三章 5..7=第2轮三章
    assert route.call_count == 8
    round_two = [call.request.content.decode("utf-8") for call in route.calls[5:]]
    chapter_one_retry, chapter_two_retry, _chapter_three_retry = round_two
    # 章 1 的候选窗口没有 panel_011，反馈必须被过滤掉。
    assert "把镜头拉回" not in chapter_one_retry
    # 章 2 拥有 panel_011，必须收到这条它能修的错误。
    assert "把镜头拉回" in chapter_two_retry and "panel_011" in chapter_two_retry

@pytest.mark.anyio
@respx.mock
async def test_truncated_batch_is_halved_and_retried(tmp_path) -> None:
    """批次响应 finish_reason=length（半截 JSON）时自动对半拆分重跑，不拖垮整话。"""
    series_dir = _make_series(tmp_path)
    truncated = _script_response(
        '```json\n{"beats": [{"narration_text": "写到一半被截断',
        finish_reason="length",
    )
    route = respx.post(CHAT_URL).mock(
        side_effect=_dispatch(
            overview=[
                _script_response(json.dumps(_overview_payload(), ensure_ascii=False))
            ],
            story=[
                _script_response(
                    json.dumps(_story_payload(["panel_001", "panel_002"]), ensure_ascii=False)
                )
            ],
            beats=[
                truncated,  # 整批（2 格）被截断
                _script_response(
                    json.dumps(_beats_payload(["panel_001"]), ensure_ascii=False)
                ),  # 左半批
                _script_response(
                    json.dumps(_beats_payload(["panel_002"]), ensure_ascii=False)
                ),  # 右半批
            ],
        )
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, _path = await s4_scriptwriter.run_stage4(
            series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
        )
    assert route.call_count == 5  # 概览 + 故事反写 + 整章截断 + 左半 + 右半
    left_request = route.calls[3].request.content.decode("utf-8")
    right_request = route.calls[4].request.content.decode("utf-8")
    # 截断后按候选分镜对半拆：左半只能看到 panel_001，右半只能看到 panel_002。
    # 用候选清单行（"- panel_xxx："）判定，避免与 PREVIOUS_TAIL 里的解说文案混淆。
    assert "- panel_001：" in left_request and "- panel_002：" not in left_request
    assert "- panel_002：" in right_request and "- panel_001：" not in right_request
    assert {beat.target_panel_ids[0] for beat in script.beats} == {"panel_001", "panel_002"}


def _write_vision_with_copyright(series_dir: Path, episode_no: int = 2) -> Path:
    """铺一份 3 格 vision.json：panel_003 是片尾版权声明图。"""
    vision_dir = series_dir / "episodes" / f"ep_{episode_no:03d}" / "03_vision"
    vision_dir.mkdir(parents=True, exist_ok=True)
    vision_path = vision_dir / "vision.json"
    save_model(
        VisionWindowOutput.model_validate(
            {
                "panels": [
                    {
                        "panel_id": "panel_001",
                        "scene_summary": "雨夜街头，少年攥紧信纸",
                        "dialogues": [],
                        "sfx_list": [],
                    },
                    {
                        "panel_id": "panel_002",
                        "scene_summary": "黑衣人撑伞而立",
                        "dialogues": [],
                        "sfx_list": [],
                    },
                    {
                        "panel_id": "panel_003",
                        "scene_summary": "LINE WEBTOON 版权声明与防盗版警告",
                        "dialogues": [],
                        "sfx_list": [],
                        "is_transition_blank": True,
                        "non_story_type": "copyright_notice",
                    },
                ]
            }
        ),
        vision_path,
    )
    return vision_path


@pytest.mark.anyio
@respx.mock
async def test_copyright_panel_never_enters_llm_context(tmp_path) -> None:
    """Patch v2.5-1 第三重：版权尾图不进 overview/分批上下文，也不进剧本。"""
    series_dir = _make_series(tmp_path)
    _write_vision_with_copyright(series_dir)
    route = respx.post(CHAT_URL).mock(
        side_effect=_dispatch(
            overview=[
                _script_response(json.dumps(_overview_payload(), ensure_ascii=False))
            ],
            story=[
                _script_response(
                    json.dumps(_story_payload(["panel_001", "panel_002"]), ensure_ascii=False)
                )
            ],
            beats=[
                _script_response(
                    json.dumps(
                        _beats_payload(["panel_001", "panel_002"]), ensure_ascii=False
                    )
                )
            ],
        )
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, _path = await s4_scriptwriter.run_stage4(
            series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
        )
    assert route.call_count == 3
    for call in route.calls:
        body = call.request.content.decode("utf-8")
        assert "panel_003" not in body  # 三段都看不到版权格
    referenced = {
        panel_id for beat in script.beats for panel_id in beat.target_panel_ids
    }
    assert "panel_003" not in referenced
    assert {beat.target_panel_ids[0] for beat in script.beats} == {"panel_001", "panel_002"}


@pytest.mark.anyio
@respx.mock
async def test_beat_referencing_filtered_panel_is_dropped(tmp_path) -> None:
    """模型若违规给版权格写解说，target_panel_ids 校验会把该 beat 整条剔除。"""
    series_dir = _make_series(tmp_path)
    _write_vision_with_copyright(series_dir)
    beats_payload = {
        "beats": [
            {
                "beat_id": "b1",
                "narration_text": "panel_001 的解说句要写够十个字以上。",
                "target_panel_ids": ["panel_001"],
                "motion": "static",
                "pause_after_ms": 200,
            },
            {
                "beat_id": "b2",
                "narration_text": "版权页本不该有解说，模型违规写了一句。",
                "target_panel_ids": ["panel_003"],
                "motion": "static",
                "pause_after_ms": 200,
            },
        ]
    }
    respx.post(CHAT_URL).mock(
        side_effect=_dispatch(
            overview=[
                _script_response(json.dumps(_overview_payload(), ensure_ascii=False))
            ],
            story=[
                _script_response(
                    json.dumps(_story_payload(["panel_001", "panel_002"]), ensure_ascii=False)
                )
            ],
            beats=[_script_response(json.dumps(beats_payload, ensure_ascii=False))],
        )
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, _path = await s4_scriptwriter.run_stage4(
            series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
        )
    referenced = {
        panel_id for beat in script.beats for panel_id in beat.target_panel_ids
    }
    assert "panel_003" not in referenced
    assert len(script.beats) == 1
    assert script.beats[0].target_panel_ids == ["panel_001"]


def test_validate_beat_alignment_rejects_out_of_order_beats() -> None:
    """Patch v2.6-5：倒序与「回头讲解」必须被拦住；一镜多拍与顺位必须放行。"""
    valid = ["p_001", "p_002", "p_003"]

    # 倒序：同一句里把镜头拉回前一格。
    errors = s4_scriptwriter.validate_beat_alignment(
        [{"target_panel_ids": ["p_003", "p_001"]}], valid
    )
    assert errors
    assert any("严格递增" in item for item in errors)

    # 回头讲解：先讲第三格再讲第一格。
    errors = s4_scriptwriter.validate_beat_alignment(
        [{"target_panel_ids": ["p_003"]}, {"target_panel_ids": ["p_001"]}], valid
    )
    assert errors
    assert any("单调不减" in item for item in errors)

    # 一镜多拍：同一格拆成两条 beat（内心独白 + 口头台词）是合法的。
    assert (
        s4_scriptwriter.validate_beat_alignment(
            [{"target_panel_ids": ["p_002"]}, {"target_panel_ids": ["p_002"]}], valid
        )
        == []
    )

    # 顺位：一句跨两格 + 下一格。
    assert (
        s4_scriptwriter.validate_beat_alignment(
            [{"target_panel_ids": ["p_001", "p_002"]}, {"target_panel_ids": ["p_003"]}],
            valid,
        )
        == []
    )

    # Patch v3.0-7 起不再检查覆盖率：跳过任意数量的分镜（哪怕只剩一格）都放行，
    # 只要分镜引用有效、内部严格递增、全局单调不减即可。
    assert (
        s4_scriptwriter.validate_beat_alignment(
            [{"target_panel_ids": ["p_001"]}], valid
        )
        == []
    )
    assert (
        s4_scriptwriter.validate_beat_alignment(
            [{"target_panel_ids": ["p_001", "p_003"]}], valid
        )
        == []
    )


def test_clamp_regressive_beats_anchors_to_current_main_panel() -> None:
    """真机回归（wt_5081 ep_001 残留 4 处顽固回看）：重写耗尽后的确定性钳制。"""
    ordered = [f"p_{index:03d}" for index in range(1, 21)]

    # 1) 单图整条回看：p_001 -> p_008 -> 回看 p_005，钳制到当前主画面 p_008。
    beats = [
        {"target_panel_ids": ["p_001"]},
        {"target_panel_ids": ["p_008"]},
        {"target_panel_ids": ["p_005"]},
    ]
    logs = s4_scriptwriter._clamp_regressive_beats(beats, ordered)
    assert [beat["target_panel_ids"] for beat in beats] == [
        ["p_001"],
        ["p_008"],
        ["p_008"],
    ]
    assert len(logs) == 1 and "p_005" in logs[0] and "p_008" in logs[0]
    assert s4_scriptwriter.validate_beat_alignment(beats, ordered) == []

    # 2) 多图 beat 部分回看：p_005 丢弃，合规的 p_012 保留（当前进度停在 p_010）。
    beats = [
        {"target_panel_ids": ["p_001"]},
        {"target_panel_ids": ["p_008", "p_010"]},
        {"target_panel_ids": ["p_005", "p_012"]},
    ]
    logs = s4_scriptwriter._clamp_regressive_beats(beats, ordered)
    assert beats[2]["target_panel_ids"] == ["p_012"]
    assert any("p_005" in line for line in logs)
    assert s4_scriptwriter.validate_beat_alignment(beats, ordered) == []

    # 3) 首条 beat 内部逆序：逆序的 p_002 被滤掉，保留更靠后的 p_005。
    beats = [{"target_panel_ids": ["p_005", "p_002"]}]
    s4_scriptwriter._clamp_regressive_beats(beats, ordered)
    assert beats[0]["target_panel_ids"] == ["p_005"]
    assert s4_scriptwriter.validate_beat_alignment(beats, ordered) == []

    # 4) 合规序列（同格停留 + 稀疏跳格）原样不动、无日志。
    beats = [
        {"target_panel_ids": ["p_001"]},
        {"target_panel_ids": ["p_001"]},
        {"target_panel_ids": ["p_009"]},
    ]
    logs = s4_scriptwriter._clamp_regressive_beats(beats, ordered)
    assert logs == []
    assert [beat["target_panel_ids"] for beat in beats] == [
        ["p_001"],
        ["p_001"],
        ["p_009"],
    ]


@pytest.mark.anyio
@respx.mock
async def test_story_draft_is_written_and_beats_follow_it(tmp_path) -> None:
    """Patch v2.6-5：4A 故事反写稿落盘 story_draft.json，4B 顺位拆拍并带上 focus_hint。"""
    series_dir = _make_series(tmp_path)
    beats_payload = {
        "beats": [
            {
                "beat_id": "b1",
                "narration_text": f"{panel_id} 上半格的内心独白先讲一遍。",
                "target_panel_ids": [panel_id],
                "focus_hint": "top",
                "motion": "zoom_in",
                "pause_after_ms": 200,
            }
            for panel_id in ["panel_001"]
        ]
        + [
            {
                "beat_id": "b2",
                "narration_text": f"{panel_id} 下半格的人物口头台词再讲一遍。",
                "target_panel_ids": [panel_id],
                "focus_hint": "bottom",
                "motion": "pan_down",
                "pause_after_ms": 200,
            }
            for panel_id in ["panel_001"]
        ]
        + [
            {
                "beat_id": "b3",
                "narration_text": "panel_002 的画面把气氛压了下去。",
                "target_panel_ids": ["panel_002"],
                "focus_hint": "full",
                "motion": "static",
                "pause_after_ms": 200,
            }
        ]
    }
    route = respx.post(CHAT_URL).mock(
        side_effect=_dispatch(
            overview=[
                _script_response(json.dumps(_overview_payload(), ensure_ascii=False))
            ],
            story=[
                _script_response(
                    json.dumps(_story_payload(["panel_001", "panel_002"]), ensure_ascii=False)
                )
            ],
            beats=[_script_response(json.dumps(beats_payload, ensure_ascii=False))],
        )
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, script_path = await s4_scriptwriter.run_stage4(
            series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
        )

    # 4A 的故事反写稿必须落盘。
    draft_path = script_path.parent / "story_draft.json"
    assert draft_path.is_file()
    draft = load_model(EpisodeStoryDraft, draft_path)
    assert draft.episode_id == "wt_test_ep_002"
    assert draft.chapters and draft.chapters[0].panel_hint_range == (
        "panel_001",
        "panel_002",
    )

    # 4B 的请求里必须带上了故事反写稿（锚点）。
    beats_request = route.calls[2].request.content.decode("utf-8")
    assert "雨夜里的相遇与试探" in beats_request

    # 4B 拆拍：同格两条 beat（top/bottom）+ 后一格一条，且 focus_hint 保留下来。
    assert [beat.focus_hint for beat in script.beats] == ["top", "bottom", "full"]
    assert [beat.target_panel_ids[0] for beat in script.beats] == [
        "panel_001",
        "panel_001",
        "panel_002",
    ]


def test_validate_beat_alignment_accepts_legacy_panel_ids() -> None:
    """Patch v2.6-6：历史写法的分镜 ID（panel_001）要能通过顺位校验。"""
    valid = ["p_001", "p_002", "p_003"]

    # 旧写法 panel_001 → 解析到 p_001，顺位正确时放行。
    assert (
        s4_scriptwriter.validate_beat_alignment(
            [{"target_panel_ids": ["panel_001"]}, {"target_panel_ids": ["p_002"]}], valid
        )
        == []
    )

    # 旧写法也必须遵守单调性：panel_002 再 panel_001 仍然报错。
    errors = s4_scriptwriter.validate_beat_alignment(
        [{"target_panel_ids": ["panel_002"]}, {"target_panel_ids": ["panel_001"]}], valid
    )
    assert errors
    assert any("单调不减" in item for item in errors)


def test_canonicalize_beat_panel_ids_maps_legacy_names() -> None:
    """Patch v2.6-6：beat 引用的旧 ID 会被就地改写成规范 ID。"""
    beats = [
        {"target_panel_ids": ["panel_001"]},
        {"target_panel_ids": ["ep_001_p002", "p_003"]},
    ]
    s4_scriptwriter._canonicalize_beat_panel_ids(beats, {"p_001", "p_002", "p_003"})

    assert beats[0]["target_panel_ids"] == ["p_001"]
    assert beats[1]["target_panel_ids"] == ["p_002", "p_003"]


# ---------------------------------------------------------------------------
# Patch v3.0-7（抖音重建 / Story-First）验收测试
# ---------------------------------------------------------------------------


def test_sparse_panel_selection_passes_but_reverse_is_blocked() -> None:
    """验收①：跳过 70% 分镜只要全局单调递增就放行；倒序讲解必须被拦截。"""
    valid = [f"p_{index:03d}" for index in range(1, 11)]  # 10 格有效分镜

    # 只挑 3 格（跳过 70%），严格递增 → 顺位校验通过。
    sparse = [
        {"target_panel_ids": ["p_001"]},
        {"target_panel_ids": ["p_005"]},
        {"target_panel_ids": ["p_010"]},
    ]
    assert s4_scriptwriter.validate_beat_alignment(sparse, valid) == []

    # 同样三格倒着讲 → 全局单调不减失败。
    reversed_beats = [
        {"target_panel_ids": ["p_010"]},
        {"target_panel_ids": ["p_005"]},
        {"target_panel_ids": ["p_001"]},
    ]
    errors = s4_scriptwriter.validate_beat_alignment(reversed_beats, valid)
    assert errors
    assert any("单调不减" in item for item in errors)


def test_validate_tts_text_no_pinyin_rejects_pinyin_accepts_hanzi() -> None:
    """验收②：tts_text 写拼音（hùn xiǎo）报错；改写成标准汉字（混蛋）通过。"""
    bad = [
        {
            "narration_text": "银行行长缓缓开口，声音很冷。",
            "tts_text": "银行 hùn xiǎo 缓缓开口，声音很冷。",
            "target_panel_ids": ["p_001"],
        }
    ]
    errors = s4_scriptwriter.validate_tts_text_no_pinyin(bad)
    assert errors
    assert "hùn" in "\n".join(errors)

    # 标准汉字同音词不触发；narration_text 与 tts_text 都保持汉字。
    good = [
        {
            "narration_text": "他低声骂了一句混蛋。",
            "tts_text": "他低声骂了一句混蛋。",
            "target_panel_ids": ["p_001"],
        }
    ]
    assert s4_scriptwriter.validate_tts_text_no_pinyin(good) == []

    # 白名单专有名词（WEBTOON / BGM）不算拼音。
    allowed = [
        {
            "narration_text": "片尾出现 WEBTOON 标识与 BGM 渐弱。",
            "tts_text": "片尾出现 WEBTOON 标识与 BGM 渐弱。",
            "target_panel_ids": ["p_001"],
        }
    ]
    assert s4_scriptwriter.validate_tts_text_no_pinyin(allowed) == []


@pytest.mark.anyio
@respx.mock
async def test_human_revised_story_markdown_skips_4a_and_drives_4b(tmp_path) -> None:
    """验收③：人工改稿后 4B 严格忠于改稿，且 4A 故事反写不再被调用。

    走真实 CLI 工作流：人工把 ``04_script/story_draft.md`` 写到磁盘（此时还没有
    story_draft.json），``run_stage4`` 应把磁盘稿判定为人工稿，跳过 4A，
    直接解析人工章节驱动 4B 拆拍。
    """
    series_dir = _make_series(tmp_path)
    script_dir = s4_scriptwriter.script_dir_for(series_dir, 2)
    script_dir.mkdir(parents=True, exist_ok=True)
    human_markdown = (
        "# 第 2 话 · 故事初稿\n\n"
        "## 人工改写的雨夜对峙\n\n"
        "人工改稿独有桥段：黑衣人缓缓摘下兜帽，露出一张与主角一模一样的脸。\n"
    )
    (script_dir / s4_scriptwriter.STORY_DRAFT_MD_FILENAME).write_text(
        human_markdown, encoding="utf-8"
    )

    route = respx.post(CHAT_URL).mock(
        side_effect=_dispatch(
            overview=[
                _script_response(json.dumps(_overview_payload(), ensure_ascii=False))
            ],
            # 故意不准备 story 桶响应：一旦误调 4A，_dispatch 会立刻抛 AssertionError。
            beats=[
                _script_response(
                    json.dumps(_beats_payload(["panel_001", "panel_002"]), ensure_ascii=False)
                )
            ],
        )
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, script_path = await s4_scriptwriter.run_stage4(
            series_dir,
            2,
            client,
            model="qwen-plus",
            episode_id="wt_test_ep_002",
            require_story_source=True,
        )

    # 只有 1 次概览 + 1 次章节拆拍，4A 故事反写从未发出。
    assert route.call_count == 2
    prompts = [call.request.content.decode("utf-8") for call in route.calls]
    assert all("story_prose" not in body for body in prompts)
    narrate_request = prompts[1]
    # 4B 请求体必须带上人工稿独有文本（忠于改稿，而非模型自动稿）。
    assert "人工改稿独有桥段" in narrate_request
    assert "黑衣人缓缓摘下兜帽" in narrate_request
    assert "人工改写的雨夜对峙" in narrate_request  # CHAPTER_TITLE
    assert script.beats  # 正常产出解说句
    # 人工稿原样保留在磁盘上，没有被自动稿覆写。
    on_disk = (script_path.parent / s4_scriptwriter.STORY_DRAFT_MD_FILENAME).read_text(
        encoding="utf-8"
    )
    assert "人工改稿独有桥段" in on_disk
    # 解析人工稿得到的章节稿也落盘到 story_draft.json。
    draft = load_model(
        EpisodeStoryDraft, script_path.parent / s4_scriptwriter.STORY_DRAFT_FILENAME
    )
    assert draft.chapters[0].chapter_title == "人工改写的雨夜对峙"


@pytest.mark.anyio
@respx.mock
async def test_every_stage4_request_disables_thinking(tmp_path) -> None:
    """Patch v3.0-7 真机修复：qwen3 系默认 thinking 会撞穿 180s 超时，
    概览 / 4A / 4B 三类调用都必须透传 ``enable_thinking=False``。"""
    series_dir = _make_series(tmp_path)
    route = respx.post(CHAT_URL).mock(
        side_effect=_dispatch(
            overview=[
                _script_response(json.dumps(_overview_payload(), ensure_ascii=False))
            ],
            story=[
                _script_response(
                    json.dumps(_story_payload(["panel_001", "panel_002"]), ensure_ascii=False)
                )
            ],
            beats=[
                _script_response(
                    json.dumps(
                        _beats_payload(["panel_001", "panel_002"]), ensure_ascii=False
                    )
                )
            ],
        )
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        await s4_scriptwriter.run_stage4(
            series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
        )
    assert route.call_count == 3
    for call in route.calls:
        body = json.loads(call.request.content.decode("utf-8"))
        assert body.get("enable_thinking") is False


@pytest.mark.anyio
@respx.mock
async def test_persistent_reverse_is_clamped_after_retries(tmp_path, capsys) -> None:
    """真机回归：模型三轮重写都坚持倒序时，确定性钳制保证落盘剧本 100% 单调。"""
    series_dir = _make_series(tmp_path)
    reverse_beats = {
        "beats": [
            {
                "beat_id": "b1",
                "narration_text": "她转身面向房门，低声应道知道了这件事。",
                "target_panel_ids": ["panel_002"],
                "motion": "static",
                "pause_after_ms": 200,
            },
            {
                "beat_id": "b2",
                "narration_text": "她在心里问自己为什么还要再来一趟这里。",
                "target_panel_ids": ["panel_001"],
                "motion": "static",
                "pause_after_ms": 200,
            },
        ]
    }
    route = respx.post(CHAT_URL).mock(
        side_effect=_dispatch(
            overview=[
                _script_response(json.dumps(_overview_payload(), ensure_ascii=False))
            ],
            story=[
                _script_response(
                    json.dumps(
                        _story_payload(["panel_001", "panel_002"]), ensure_ascii=False
                    )
                )
            ],
            # 桶耗尽后 _dispatch 会一直复用最后一个响应：三轮重写都倒序。
            beats=[_script_response(json.dumps(reverse_beats, ensure_ascii=False))],
        )
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, _path = await s4_scriptwriter.run_stage4(
            series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
        )
    # 1 概览 + 1 故事反写 + 3 轮章节拆拍（ALIGNMENT_MAX_RETRIES + 1）。
    assert route.call_count == 5
    # 回看的第二条被钳制到当前主画面 panel_002，字幕文本一字不动。
    assert [beat.target_panel_ids for beat in script.beats] == [
        ["panel_002"],
        ["panel_002"],
    ]
    assert script.beats[1].narration_text == "她在心里问自己为什么还要再来一趟这里。"
    assert (
        s4_scriptwriter.validate_beat_alignment(
            [
                {"target_panel_ids": beat.target_panel_ids}
                for beat in script.beats
            ],
            ["panel_001", "panel_002"],
        )
        == []
    )
    assert "顺位钳制" in capsys.readouterr().err


# ===========================================================================
# Patch v3.1 Patch 10：纯字卡标记 / speaker_role / sfx_cue / 旧 JSON 兼容
# ===========================================================================


def _prompt_of(call) -> str:
    """从一次 LLM 请求体里取出最后一条消息的 prompt 文本（真实换行）。"""
    body = json.loads(call.request.content.decode("utf-8"))
    return body["messages"][-1]["content"]


def _write_vision_with_pure_text_card(series_dir: Path, episode_no: int = 2) -> Path:
    """铺一份 3 格 vision.json。

    * ``panel_001``：普通剧情格；
    * ``panel_002``：纯字卡（白底黑字、无人物动作），``is_pure_text_card=True``；
    * ``panel_003``：超窄过渡条，``is_transition_blank=True`` 且 ``non_story_type``
      仍是 ``none``——必须靠 ``is_transition_blank`` 这一个标记被滤掉。
    """
    vision_dir = series_dir / "episodes" / f"ep_{episode_no:03d}" / "03_vision"
    vision_dir.mkdir(parents=True, exist_ok=True)
    vision_path = vision_dir / "vision.json"
    save_model(
        VisionWindowOutput.model_validate(
            {
                "panels": [
                    {
                        "panel_id": "panel_001",
                        "scene_summary": "雨夜街头，少年攥紧信纸",
                        "dialogues": [],
                        "sfx_list": [],
                    },
                    {
                        "panel_id": "panel_002",
                        "scene_summary": "白底黑字卡：倒计时三分钟",
                        "dialogues": [],
                        "sfx_list": [],
                        "is_pure_text_card": True,
                    },
                    {
                        "panel_id": "panel_003",
                        "scene_summary": "细窄黑色过渡条",
                        "dialogues": [],
                        "sfx_list": [],
                        "is_transition_blank": True,
                        "non_story_type": "none",
                    },
                ]
            }
        ),
        vision_path,
    )
    return vision_path


@pytest.mark.anyio
@respx.mock
async def test_pure_text_card_is_marked_and_transition_blank_filtered(tmp_path) -> None:
    """Patch v3.1 Patch 10 验收一。

    给 4B 的候选面板摘要必须给 ``is_pure_text_card=True`` 的分镜标上
    ``[纯字卡]``（提示模型跳过这类白底黑字 PPT 格），同时
    ``is_transition_blank=True`` 的窄条不得出现在任何一段请求里。
    """
    series_dir = _make_series(tmp_path)
    _write_vision_with_pure_text_card(series_dir)
    route = respx.post(CHAT_URL).mock(
        side_effect=_dispatch(
            overview=[
                _script_response(json.dumps(_overview_payload(), ensure_ascii=False))
            ],
            story=[
                _script_response(
                    json.dumps(_story_payload(["panel_001", "panel_002"]), ensure_ascii=False)
                )
            ],
            beats=[
                _script_response(
                    json.dumps(
                        _beats_payload(["panel_001", "panel_002"]), ensure_ascii=False
                    )
                )
            ],
        )
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, _path = await s4_scriptwriter.run_stage4(
            series_dir, 2, client, model="qwen-plus", episode_id="wt_test_ep_002"
        )
    assert route.call_count == 3
    # 过渡窄条三段都看不到（write_episode_script 的硬过滤 + _format_panel_ids 兜底）。
    prompts = [_prompt_of(call) for call in route.calls]
    for prompt in prompts:
        assert "panel_003" not in prompt
    # 4B 的候选清单里，纯字卡分镜带 [纯字卡] 标记，普通格不带。
    narrate_prompt = prompts[2]
    card_line = next(
        line for line in narrate_prompt.splitlines() if line.startswith("- panel_002：")
    )
    assert "[纯字卡]" in card_line
    plain_line = next(
        line for line in narrate_prompt.splitlines() if line.startswith("- panel_001：")
    )
    assert "[纯字卡]" not in plain_line
    # 剧本里也不会引用被滤掉的窄条。
    referenced = {
        panel_id for beat in script.beats for panel_id in beat.target_panel_ids
    }
    assert "panel_003" not in referenced


def test_format_panel_ids_filters_transition_blank_and_marks_text_card() -> None:
    """``_format_panel_ids`` 的兜底过滤：窄条剔除、纯字卡打标。"""
    panel_map = {
        "panel_001": PanelVisionInfo.model_validate(
            {"panel_id": "panel_001", "scene_summary": "雨夜街头", "dialogues": []}
        ),
        "panel_002": PanelVisionInfo.model_validate(
            {
                "panel_id": "panel_002",
                "scene_summary": "白底黑字卡",
                "dialogues": [],
                "is_pure_text_card": True,
            }
        ),
        "panel_003": PanelVisionInfo.model_validate(
            {
                "panel_id": "panel_003",
                "scene_summary": "细窄过渡条",
                "dialogues": [],
                "is_transition_blank": True,
            }
        ),
    }
    rendered = s4_scriptwriter._format_panel_ids(
        ["panel_001", "panel_002", "panel_003"], panel_map
    )
    assert "- panel_001：" in rendered
    assert "- panel_002： [纯字卡] 白底黑字卡" in rendered
    assert "panel_003" not in rendered


def test_script_beat_parses_speaker_role_and_sfx_cue() -> None:
    """Patch v3.1 Patch 10 验收二：新字段能正常解析。"""
    beat = ScriptBeat.model_validate(
        {
            "beat_id": "b1",
            "narration_text": "道玄把伞递过去，声音压得很低。",
            "target_panel_ids": ["panel_001"],
            "speaker_role": "male_lead",
            "sfx_cue": "heart_thump",
        }
    )
    assert beat.speaker_role == "male_lead"
    assert beat.sfx_cue == "heart_thump"


def test_legacy_script_json_without_sfx_cue_deserializes(tmp_path) -> None:
    """Patch v3.1 Patch 10 验收二后半：旧版 script.json 零报错反序列化。"""
    legacy_path = tmp_path / "script.json"
    legacy_path.write_text(_script_payload(), encoding="utf-8")
    script = load_model(NarrationScript, legacy_path)
    assert len(script.beats) == 2
    # 新字段走默认值，缺失不报错。
    assert all(beat.sfx_cue is None for beat in script.beats)
    assert all(beat.speaker_role == "narrator" for beat in script.beats)


def test_normalize_script_payload_converges_sfx_cue_and_speaker_role() -> None:
    """sfx_cue 走白名单收敛（大小写不敏感），speaker_role 缺省 narrator。"""
    payload = {
        "hook_title": "雨夜来客",
        "beats": [
            {
                "narration_text": "他攥紧信纸，指节发白。",
                "target_panel_ids": ["panel_001"],
                "sfx_cue": "heart_thump",
            },
            {
                "narration_text": "门被猛地推开，她后退半步。",
                "target_panel_ids": ["panel_002"],
                "sfx_cue": "DOOR_SLAM",
                "speaker_role": "female_lead",
            },
            {
                "narration_text": "远处传来一阵脚步声。",
                "target_panel_ids": ["panel_003"],
                "sfx_cue": "not_a_real_key",
            },
            {
                "narration_text": "雨声渐渐停了。",
                "target_panel_ids": ["panel_001"],
                "sfx_cue": "none",
            },
        ],
        "episode_digest": {"one_line_hook": "钩子", "summary": "概要"},
    }
    normalized = normalize_script_payload(
        payload,
        episode_id="wt_test_ep_002",
        episode_no=2,
        valid_panel_ids={"panel_001", "panel_002", "panel_003"},
    )
    cues = [beat["sfx_cue"] for beat in normalized["beats"]]
    assert cues == ["heart_thump", "door_slam", None, None]
    roles = [beat["speaker_role"] for beat in normalized["beats"]]
    assert roles == ["narrator", "female_lead", "narrator", "narrator"]


def test_render_timeline_legacy_json_stays_compatible() -> None:
    """RenderTimeline 新增 sfx_events 后，旧 timeline.json 照常反序列化。"""
    legacy = {
        "episode_id": "wt_test_ep_002",
        "total_duration_ms": 12000,
        "scenes": [],
        "audios": [],
        "subtitles": [],
    }
    timeline = RenderTimeline.model_validate(legacy)
    assert timeline.sfx_events == []
    with_events = RenderTimeline.model_validate(
        {
            **legacy,
            "sfx_events": [{"sfx_key": "heart_thump", "start_ms": 3000}],
        }
    )
    assert with_events.sfx_events == [
        SfxEvent(sfx_key="heart_thump", start_ms=3000, gain_db=-10.0)
    ]


def _tile_payload_v31(
    *,
    non_story_type: str = "none",
    blank: bool = False,
    dialogues: list[dict] | None = None,
    pure_text_card: bool = False,
    summary: str = "画面内容",
) -> dict:
    """构造一份切片视觉结果（Stage 3 合并用的最小载荷）。"""
    return {
        "scene_summary": summary,
        "dialogues": dialogues or [],
        "sfx_list": [],
        "focus_region": None,
        "is_transition_blank": blank,
        "non_story_type": non_story_type,
        "is_pure_text_card": pure_text_card,
    }


def _tile_v31(index: int, count: int, top: int, bottom: int, total: int = 1000):
    return s3_vision.PanelTile(
        image=Image.new("RGB", (320, bottom - top), (20, 20, 20)),
        top=top,
        bottom=bottom,
        total_height=total,
        index=index,
        count=count,
    )


def test_merge_tile_payloads_marks_pure_text_card() -> None:
    """Patch v3.1 Patch 10：切片合并时只有「全部纯字卡且无对白」才算纯字卡。

    （Patch 10 的「仅修改」清单里只有 ``test_s4_scriptwriter.py``，
    所以 Stage 3 的合并用例也放在这里。）
    """
    tile_a = _tile_v31(0, 2, 0, 500)
    tile_b = _tile_v31(1, 2, 500, 1000)

    merged = s3_vision._merge_tile_payloads(
        "panel_002",
        [
            (tile_a, _tile_payload_v31(pure_text_card=True, summary="白底黑字卡上半")),
            (tile_b, _tile_payload_v31(pure_text_card=True, summary="白底黑字卡下半")),
        ],
    )
    assert merged["is_pure_text_card"] is True
    assert merged["non_story_type"] == "none"

    # 只要有一片不是纯字卡，整格就不算纯字卡。
    mixed = s3_vision._merge_tile_payloads(
        "panel_002",
        [
            (tile_a, _tile_payload_v31(pure_text_card=True)),
            (tile_b, _tile_payload_v31(pure_text_card=False)),
        ],
    )
    assert mixed["is_pure_text_card"] is False

    # 有对白就不算纯字卡（纯字卡必然没有气泡）。
    with_dialogue = s3_vision._merge_tile_payloads(
        "panel_002",
        [
            (
                tile_a,
                _tile_payload_v31(
                    pure_text_card=True,
                    dialogues=[{"speaker_guess": "小白", "text": "谁寄的？"}],
                ),
            ),
            (tile_b, _tile_payload_v31(pure_text_card=True)),
        ],
    )
    assert with_dialogue["is_pure_text_card"] is False
