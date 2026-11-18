"""Patch v4.1：Patch 20.1 边界修复 + Patch 21 按步向后级联清理。

本文件覆盖两部分：

**Patch 20.1（代码审查发现的 4 个边界缺陷）**

1. ``backend/src/cli.py`` 的 ``produce_scripts`` 必须把镜头组上下文
   （``storyboard_path`` / ``panels_dir`` / ``vision_client`` / ``vision_model``）
   透传给 ``s4_scriptwriter.run_stage4``，让 ``w2v understand`` 也走
   Scene-Grounded 流程；
2. ``_splice_scene_beats`` 插入补写内容前先剔除「与本幕分镜有交集」的旧 beat，
   避免整幕重播两遍与顺位倒挂；
3. 4A 章节数与镜头组数不等时，按 ``panel_hint_range`` 与 ``scene.panel_ids``
   的区间重叠配对，不再按下标错位；
4. ``important_panel_ids`` 在 Scene-Grounded 下只统计**启用幕**里的分镜；
   用户手动编入启用幕的非版权分镜放行进入有效分镜集合；
5. ``s3b_scenes.auto_build_storyboard`` 末尾残余格的收尾幕判据修正。

**Patch 21（按步向后级联清理与状态重置）**

6. ``ManifestManager.reset_from_stage`` 清空该步及下游产物，Stage 4 默认保留
   人工编排的镜头组，Stage 7 额外清 ``exports/EP{话次}_*``；
7. ``JobRunner.reset_episode_from_stage`` 同步清掉内存里的旧完成标记，运行中
   的话拒绝清理；
8. ``POST /api/episodes/{id}/reset`` 的 404 / 400 / 409 语义；
9. ``submit_episode`` 真正开跑前自动清空该步及下游旧产物与旧 ``done``。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from cli import download_series, understand_series
from core.config import AppConfig, WorkspaceConfig
from core.manifest import ManifestManager, infer_stage_status
from core.schemas import (
    EpisodeStoryboard,
    StageStatus,
    StoryboardScene,
    VisionWindowOutput,
    save_model,
)
from providers.llm_base import ChatResponse
from providers.llm_bailian import BailianClient
from stages import s0_catalog, s3b_scenes, s4_scriptwriter
from web.job_runner import JobError, JobRunner
from web.server import create_app

CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

_PANEL_RE = re.compile(r"^- (p_\d{3})：", re.MULTILINE)


async def _no_sleep(_seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------
# 公共装置
# ---------------------------------------------------------------------------
def _panel(
    panel_id: str,
    summary: str,
    *,
    blank: bool = False,
    non_story_type: str = "none",
    dialogue: str | None = "一句台词。",
) -> dict:
    return {
        "panel_id": panel_id,
        "scene_summary": summary,
        "dialogues": (
            [{"speaker_guess": "小白", "text": dialogue, "emotion": "tense"}]
            if dialogue
            else []
        ),
        "sfx_list": [],
        "is_transition_blank": blank,
        "is_pure_text_card": False,
        "non_story_type": non_story_type,
    }


def _response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "qwen-plus",
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        },
    )


def _overview_payload() -> str:
    return json.dumps(
        {
            "hook_title": "雨夜来客",
            "episode_digest": {
                "one_line_hook": "一句话钩子",
                "summary": "整话通读后得到的剧情摘要",
                "keywords": ["雨夜"],
                "characters_present": ["小白"],
            },
            "character_updates": [],
            "new_plot_threads": [],
            "resolved_thread_ids": [],
        },
        ensure_ascii=False,
    )


def _story_payload(chapters: list[tuple[str, str, str, str]]) -> str:
    prose = "这一段讲的是雨夜里的相遇与试探，人物在沉默中一点点交出底牌。"
    return json.dumps(
        {
            "episode_id": "wt_fix_ep_002",
            "hook_opening": "雨夜有人敲门，来者不善。",
            "full_story_markdown": f"## 雨夜试探\n\n{prose}",
            "chapters": [
                {
                    "chapter_index": index,
                    "chapter_title": title,
                    "panel_hint_range": [first, last],
                    "story_prose": body,
                }
                for index, (title, first, last, body) in enumerate(chapters, start=1)
            ],
        },
        ensure_ascii=False,
    )


def _beats_payload(panel_ids: list[str], *, quote: str = "") -> str:
    narration = f"{panel_ids[0]} 的解说句要写够十个字以上。"
    if quote:
        narration = f"{quote}，{narration}"
    return json.dumps(
        {
            "beats": [
                {
                    "beat_id": f"b{index}",
                    "narration_text": (
                        f"{panel_id} 的解说句要写够十个字以上。"
                        + (f"（{quote}）" if quote else "")
                    ),
                    "target_panel_ids": [panel_id],
                    "motion": "static",
                    "pause_after_ms": 200,
                }
                for index, panel_id in enumerate(panel_ids, start=1)
            ]
        },
        ensure_ascii=False,
    )


def _make_series(tmp_path: Path, *, episode_no: int = 2, panels: list[dict] | None = None) -> Path:
    series_dir = tmp_path / "workspace" / "wt_fix"
    series_dir.mkdir(parents=True)
    vision_dir = series_dir / "episodes" / f"ep_{episode_no:03d}" / "03_vision"
    vision_dir.mkdir(parents=True)
    default_panels = [
        _panel("p_001", "病房里，小白攥紧信纸。"),
        _panel("p_002", "病房里，走廊灯忽明忽暗。"),
        _panel("p_003", "客厅里，两人隔着茶几对视。"),
        _panel("p_004", "客厅里，恩彗攥紧了拳头。"),
        _panel("p_005", "墓地里，恩彗站在墓碑前。"),
        _panel("p_006", "墓碑上刻着旧名字。"),
    ]
    save_model(
        VisionWindowOutput.model_validate({"panels": panels or default_panels}),
        vision_dir / "vision.json",
    )
    return series_dir


# ---------------------------------------------------------------------------
# 缺陷 1：CLI 透传镜头组上下文
# ---------------------------------------------------------------------------
_VISION_JSON = json.dumps(
    {
        "panels": [
            {
                "panel_id": "panel_001",
                "scene_summary": "雨夜街头，少年攥紧信纸抬头望天",
                "dialogues": [
                    {"speaker_guess": "小白", "text": "谁寄的？", "emotion": "tense"}
                ],
                "sfx_list": ["雨声"],
                "focus_region": None,
                "is_transition_blank": False,
            }
        ]
    },
    ensure_ascii=False,
)

_SCRIPT_JSON = json.dumps(
    {
        "episode_id": "模型乱填",
        "hook_title": "雨夜来客",
        "beats": [
            {
                "beat_id": "b1",
                "narration_text": "他把信纸攥成一团，指节发白。",
                "tts_text": None,
                "target_panel_ids": ["panel_001"],
                "motion": "zoom_in",
                "pause_after_ms": 300,
            }
        ],
        "episode_digest": {
            "episode_no": 1,
            "one_line_hook": "雨夜有人来访",
            "summary": "小白在雨夜收到一封匿名信。",
            "keywords": ["雨夜"],
            "characters_present": ["小白"],
        },
        "character_updates": [],
        "new_plot_threads": [],
        "resolved_thread_ids": [],
    },
    ensure_ascii=False,
)

_STORY_JSON = json.dumps(
    {
        "episode_id": "模型乱填",
        "hook_opening": "雨夜有人敲门。",
        "full_story_markdown": "## 雨夜试探\n\n少年在雨夜收到一封匿名信，来人身份成谜。",
        "chapters": [
            {
                "chapter_index": 1,
                "chapter_title": "雨夜试探",
                "panel_hint_range": ["panel_001", "panel_001"],
                "story_prose": "少年在雨夜收到一封匿名信，来人身份成谜。",
            }
        ],
    },
    ensure_ascii=False,
)


class _StubClient:
    """离线 LLM 替身：视觉 / 故事稿 / 拆拍三种响应。"""

    async def chat(self, messages, *, model, temperature=None, max_tokens=None, **kwargs):
        content = messages[0].content
        prompt = content if isinstance(content, str) else "\n".join(
            part.text or "" for part in content
        )
        if "vl" in model:
            return ChatResponse(content=_VISION_JSON, model=model)
        if "story_prose" in prompt:
            return ChatResponse(content=_STORY_JSON, model=model)
        return ChatResponse(content=_SCRIPT_JSON, model=model)

    async def aclose(self) -> None:
        return None


def _make_local_series(root: Path, name: str = "comic_a") -> Path:
    series_root = root / name
    episode_dir = series_root / "ep_001"
    episode_dir.mkdir(parents=True, exist_ok=True)
    for index in range(1, 3):
        image = Image.new("RGB", (800, 1200), (20 * index, 40, 60))
        draw = ImageDraw.Draw(image)
        for row in range(4):
            for col in range(3):
                draw.rectangle(
                    [
                        60 + col * 240,
                        80 + row * 280,
                        60 + col * 240 + 200,
                        80 + row * 280 + 240,
                    ],
                    fill=(200 - 30 * row, 180 - 20 * col, 160),
                )
        image.save(episode_dir / f"page_{index:03d}.png")
    return series_root


@pytest.mark.anyio
async def test_cli_passes_storyboard_context_to_stage4(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    local = _make_local_series(tmp_path / "src")
    config = AppConfig(workspace=WorkspaceConfig(root=workspace))
    await download_series(str(local), config=config)

    captured: list[dict] = []
    original = s4_scriptwriter.run_stage4

    async def spy(*args, **kwargs):
        captured.append(kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr(s4_scriptwriter, "run_stage4", spy)

    await understand_series(
        "wt_local_comic_a",
        config=config,
        client_factory=lambda: _StubClient(),
    )

    assert captured, "Stage 4 没有被调用"
    kwargs = captured[0]
    assert "storyboard_path" in kwargs
    assert kwargs["storyboard_path"] is not None
    assert kwargs["storyboard_path"].name == s3b_scenes.STORYBOARD_FILENAME
    assert kwargs["storyboard_path"].parent.name == "04_script"
    assert kwargs["panels_dir"] is not None
    assert kwargs["panels_dir"].name == "02_panels"
    assert kwargs["vision_client"] is not None
    assert "vision_model" in kwargs


# ---------------------------------------------------------------------------
# 缺陷 2：补写时剔除旧残句
# ---------------------------------------------------------------------------
def test_splice_scene_beats_drops_stale_partial_beat() -> None:
    scene = StoryboardScene(
        scene_id="sc_003",
        scene_index=3,
        title="第 3 幕 · 墓地",
        panel_ids=["p_005", "p_006"],
    )
    beats = [
        {"beat_id": "b1", "target_panel_ids": ["p_001"], "narration_text": "开场。"},
        {"beat_id": "b2", "target_panel_ids": ["p_005"], "narration_text": "旧残句。"},
        {"beat_id": "b3", "target_panel_ids": ["p_007"], "narration_text": "收尾。"},
    ]
    new_beats = [
        {
            "beat_id": "x1",
            "target_panel_ids": ["p_005", "p_006"],
            "narration_text": "整幕补写。",
        }
    ]
    ordered = ["p_001", "p_003", "p_005", "p_006", "p_007"]

    merged = s4_scriptwriter._splice_scene_beats(beats, scene, new_beats, ordered)

    texts = [item["narration_text"] for item in merged]
    # 旧残句（挂在本幕分镜上）被剔除；补写内容插在本幕应在的位置，不重播。
    assert texts == ["开场。", "整幕补写。", "收尾。"]
    assert "旧残句。" not in texts


def test_splice_scene_beats_drops_beat_overlapping_scene() -> None:
    """与本幕分镜**有交集**的旧 beat 也要被剔除（避免半句重复）。"""
    scene = StoryboardScene(
        scene_id="sc_002",
        scene_index=2,
        panel_ids=["p_005", "p_006"],
    )
    beats = [
        {"beat_id": "b1", "target_panel_ids": ["p_001"], "narration_text": "开场。"},
        {"beat_id": "b2", "target_panel_ids": ["p_004", "p_005"], "narration_text": "跨幕残句。"},
        {"beat_id": "b3", "target_panel_ids": ["p_007"], "narration_text": "收尾。"},
    ]
    new_beats = [
        {
            "beat_id": "x1",
            "target_panel_ids": ["p_005", "p_006"],
            "narration_text": "整幕补写。",
        }
    ]
    ordered = ["p_001", "p_004", "p_005", "p_006", "p_007"]

    merged = s4_scriptwriter._splice_scene_beats(beats, scene, new_beats, ordered)

    texts = [item["narration_text"] for item in merged]
    assert texts == ["开场。", "整幕补写。", "收尾。"]


# ---------------------------------------------------------------------------
# 缺陷 3：章节与镜头组按区间重叠配对
# ---------------------------------------------------------------------------
def test_pair_chapters_with_scenes_matches_by_overlap() -> None:
    from core.schemas import StoryChapter

    ordered = [f"p_{index:03d}" for index in range(1, 11)]
    chapters = [
        StoryChapter(
            chapter_index=1,
            chapter_title="病房",
            panel_hint_range=("p_001", "p_002"),
            story_prose="甲章",
        ),
        StoryChapter(
            chapter_index=2,
            chapter_title="客厅",
            panel_hint_range=("p_003", "p_004"),
            story_prose="乙章",
        ),
        StoryChapter(
            chapter_index=3,
            chapter_title="墓地",
            panel_hint_range=("p_009", "p_010"),
            story_prose="丙章",
        ),
    ]
    scenes = [
        StoryboardScene(
            scene_id="sc_001",
            scene_index=1,
            panel_ids=["p_005", "p_006", "p_007"],
        ),
        StoryboardScene(
            scene_id="sc_002",
            scene_index=2,
            panel_ids=["p_009", "p_010"],
        ),
    ]

    pairs = s4_scriptwriter._pair_chapters_with_scenes(chapters, scenes, ordered)

    # 第 1 幕与任何章节区间都不重叠 → 不配对（改用幕自身信息合成章节）；
    # 第 2 幕与「墓地」章区间重叠 → 配到丙章，而不是按下标拿到乙章。
    assert pairs == {1: 2}


def test_pair_chapters_with_scenes_falls_back_to_index_order() -> None:
    from core.schemas import StoryChapter

    ordered = ["p_001", "p_002", "p_003"]
    chapters = [
        StoryChapter(
            chapter_index=1,
            chapter_title="整话",
            panel_hint_range=("p_001", "p_003"),
            story_prose="正文",
        )
    ]
    scenes = [
        StoryboardScene(scene_id="sc_001", scene_index=1, panel_ids=["p_001"]),
    ]

    pairs = s4_scriptwriter._pair_chapters_with_scenes(chapters, scenes, ordered)

    # 区间完全对不上时退化成按下标配对，保持旧行为兜底。
    assert pairs == {0: 0}


# ---------------------------------------------------------------------------
# 缺陷 4：重要分镜只统计启用幕
# ---------------------------------------------------------------------------
def test_important_panel_ids_only_counts_enabled_scenes() -> None:
    panels = [
        _panel("p_001", "病房。"),
        _panel("p_002", "客厅。"),
        _panel("p_003", "墓地。"),
    ]
    parsed = VisionWindowOutput.model_validate({"panels": panels}).panels
    scenes = [
        StoryboardScene(
            scene_id="sc_001",
            scene_index=1,
            panel_ids=["p_001", "p_002"],
            enabled=True,
        ),
        StoryboardScene(
            scene_id="sc_002",
            scene_index=2,
            panel_ids=["p_003"],
            enabled=False,
        ),
    ]

    legacy = s4_scriptwriter._important_panel_ids(parsed, scenes, scene_mode=False)
    scoped = s4_scriptwriter._important_panel_ids(parsed, scenes, scene_mode=True)

    assert legacy == ["p_001", "p_002", "p_003"]
    assert scoped == ["p_001", "p_002"]


# ---------------------------------------------------------------------------
# 缺陷 5：末尾残余格的收尾幕判据
# ---------------------------------------------------------------------------
def test_auto_build_storyboard_keeps_action_only_tail_scene() -> None:
    panels = [
        _panel(f"p_{index:03d}", f"病房里，小白攥紧信纸，第 {index} 格。")
        for index in range(1, 6)
    ]
    # 末尾两格：没有对白，但构成完整的高潮动作（≥ 2 格 ⇒ 独立收尾幕）。
    panels.append(_panel("p_006", "道振抓起地上的刀，猛地站起身。", dialogue=None))
    panels.append(_panel("p_007", "他一把推开病房门冲了出去。", dialogue=None))
    vision = VisionWindowOutput.model_validate({"panels": panels})

    board = s3b_scenes.auto_build_storyboard("wt_fix_ep_001", vision)

    tail = board.scenes[-1]
    assert tail.panel_ids == ["p_006", "p_007"]
    # 前面的大幕没有被尾巴污染。
    assert board.scenes[0].panel_ids == [
        "p_001",
        "p_002",
        "p_003",
        "p_004",
        "p_005",
    ]


# ---------------------------------------------------------------------------
# 缺陷 4（续）：用户手动编入启用幕的非版权分镜放行
# ---------------------------------------------------------------------------
@pytest.mark.anyio
@respx.mock
async def test_user_edited_panel_in_enabled_scene_is_allowed(tmp_path) -> None:
    series_dir = _make_series(
        tmp_path,
        panels=[
            _panel("p_001", "病房里，小白攥紧信纸。"),
            _panel("p_002", "病房里，走廊灯忽明忽暗。"),
            _panel("p_003", "过渡空页。", blank=True, dialogue=None),
            _panel("p_004", "客厅里，两人隔着茶几对视。"),
            _panel("p_005", "客厅里，恩彗攥紧了拳头。"),
            _panel("p_006", "墓地里，恩彗站在墓碑前。"),
        ],
    )
    script_dir = series_dir / "episodes" / "ep_002" / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    board_path = script_dir / s3b_scenes.STORYBOARD_FILENAME
    # 用户手动把过渡空页 p_003 编进了第 1 幕（自动聚类会跳过它）。
    s3b_scenes.save_storyboard(
        EpisodeStoryboard(
            episode_id="wt_fix_ep_002",
            source="human",
            scenes=[
                StoryboardScene(
                    scene_id="sc_001",
                    scene_index=1,
                    title="第 1 幕 · 病房",
                    panel_ids=["p_001", "p_002", "p_003"],
                    enabled=True,
                    must_cover=True,
                ),
                StoryboardScene(
                    scene_id="sc_002",
                    scene_index=2,
                    title="第 2 幕 · 客厅",
                    panel_ids=["p_004", "p_005"],
                    enabled=True,
                    must_cover=True,
                ),
                StoryboardScene(
                    scene_id="sc_003",
                    scene_index=3,
                    title="第 3 幕 · 墓地",
                    panel_ids=["p_006"],
                    enabled=True,
                    must_cover=True,
                ),
            ],
        ),
        board_path,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        prompt = body["messages"][-1]["content"]
        if "story_prose" in prompt:
            return _response(
                _story_payload(
                    [
                        ("第 1 幕", "p_001", "p_003", "甲章"),
                        ("第 2 幕", "p_004", "p_005", "乙章"),
                        ("第 3 幕", "p_006", "p_006", "丙章"),
                    ]
                )
            )
        if "target_panel_ids" in prompt:
            panel_ids = _PANEL_RE.findall(prompt)
            if "p_001" in panel_ids:
                # 模型引用了用户手动编入启用幕的过渡页 p_003。
                panel_ids = [*panel_ids, "p_003"]
            return _response(_beats_payload(panel_ids))
        return _response(_overview_payload())

    respx.post(CHAT_URL).mock(side_effect=handler)
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        script, _ = await s4_scriptwriter.run_stage4(
            series_dir,
            2,
            client,
            model="qwen-plus",
            episode_id="wt_fix_ep_002",
            series_title="补丁测试",
            storyboard_path=board_path,
        )

    referenced = {
        panel_id for beat in script.beats for panel_id in beat.target_panel_ids
    }
    # 用户手动编入启用幕的过渡页被放行，可以出现在成稿里。
    assert "p_003" in referenced


# ===========================================================================
# Patch 21：按步向后级联清理与状态重置
# ===========================================================================
def _register_series(tmp_path: Path) -> tuple[str, Path]:
    """登记一个作品，返回 (series_id, series_dir)。"""
    source = tmp_path / "src" / "ep_001"
    source.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 600), (40, 80, 120)).save(source / "001.png")
    catalog = s0_catalog.import_local_catalog(tmp_path / "src")
    s0_catalog.CatalogRegistry(tmp_path).register(catalog)
    return catalog.series_id, tmp_path / catalog.series_id


def _build_episode(
    series_dir: Path,
    episode_no: int = 1,
    *,
    storyboard_source: str = "human",
) -> Path:
    """造一个「七步全都有产物」的话次目录。"""
    episode_dir = series_dir / "episodes" / f"ep_{episode_no:03d}"

    raw = episode_dir / "01_raw"
    raw.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 80), (10, 20, 30)).save(raw / "page_001.png")

    panels_dir = episode_dir / "02_panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 80), (40, 50, 60)).save(panels_dir / "p_001.png")

    vision_dir = episode_dir / "03_vision"
    vision_dir.mkdir(parents=True, exist_ok=True)
    save_model(
        VisionWindowOutput.model_validate(
            {"panels": [_panel("p_001", "病房里，小白攥紧信纸。")]}
        ),
        vision_dir / "vision.json",
    )

    script_dir = episode_dir / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    (script_dir / "script.json").write_text("{}", encoding="utf-8")
    (script_dir / "script_readable.md").write_text("# 剧本\n", encoding="utf-8")
    (script_dir / "story_draft.json").write_text("{}", encoding="utf-8")
    (script_dir / "story_draft.md").write_text("# 故事\n", encoding="utf-8")
    (script_dir / "storyboard.json").write_text(
        json.dumps(
            {
                "episode_id": f"ep_{episode_no:03d}",
                "source": storyboard_source,
                "scenes": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    audio_dir = episode_dir / "05_audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / "audio_manifest.json").write_text("{}", encoding="utf-8")

    plan_dir = episode_dir / "06_plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "timeline.json").write_text("{}", encoding="utf-8")

    out_dir = episode_dir / "07_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"ep_{episode_no:03d}.mp4").write_bytes(b"\x00\x01")

    exports = series_dir / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    (exports / f"EP{episode_no:03d}_final.mp4").write_bytes(b"\x00\x01")
    (exports / "EP999_final.mp4").write_bytes(b"\x00\x01")

    snapshots = series_dir / "bible_snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    (snapshots / f"ep_{episode_no:03d}.json").write_text("{}", encoding="utf-8")
    return episode_dir


def test_reset_from_stage_clears_downstream_and_keeps_upstream(tmp_path) -> None:
    series_id, series_dir = _register_series(tmp_path)
    episode_dir = _build_episode(series_dir)
    manager = ManifestManager(series_dir, series_id=series_id)

    summary = manager.reset_from_stage(1, "s5_tts")

    assert summary["cleared_stages"] == ["s5_tts", "s6_timeline", "s7_renderer"]
    assert summary["removed_count"] > 0
    assert summary["preserved_human_storyboard"] is False

    # 上游产物保留。
    assert (episode_dir / "01_raw" / "page_001.png").is_file()
    assert (episode_dir / "02_panels" / "p_001.png").is_file()
    assert (episode_dir / "03_vision" / "vision.json").is_file()
    assert (episode_dir / "04_script" / "script.json").is_file()

    # 下游产物清空（目录保留）。
    assert not (episode_dir / "05_audio" / "audio_manifest.json").exists()
    assert (episode_dir / "05_audio").is_dir()
    assert not (episode_dir / "06_plan" / "timeline.json").exists()
    assert not (episode_dir / "07_out" / "ep_001.mp4").exists()

    # 作品级导出只删本话。
    assert not (series_dir / "exports" / "EP001_final.mp4").exists()
    assert (series_dir / "exports" / "EP999_final.mp4").is_file()

    # 磁盘推断：上游 done，下游 pending。
    status = infer_stage_status(episode_dir)
    assert status["s1_crawler"] == StageStatus.DONE
    assert status["s2_segmenter"] == StageStatus.DONE
    assert status["s3_vision"] == StageStatus.DONE
    assert status["s4_scriptwriter"] == StageStatus.DONE
    assert status["s5_tts"] == StageStatus.PENDING
    assert status["s6_timeline"] == StageStatus.PENDING
    assert status["s7_renderer"] == StageStatus.PENDING


def test_reset_from_stage_s4_preserves_human_storyboard(tmp_path) -> None:
    series_id, series_dir = _register_series(tmp_path)
    episode_dir = _build_episode(series_dir, storyboard_source="human")
    manager = ManifestManager(series_dir, series_id=series_id)

    summary = manager.reset_from_stage(1, "s4_scriptwriter")

    assert summary["preserved_human_storyboard"] is True
    # 人工编排的镜头组保留。
    assert (episode_dir / "04_script" / "storyboard.json").is_file()
    # 剧本 / 故事稿删除。
    assert not (episode_dir / "04_script" / "script.json").exists()
    assert not (episode_dir / "04_script" / "script_readable.md").exists()
    assert not (episode_dir / "04_script" / "story_draft.json").exists()
    assert not (episode_dir / "04_script" / "story_draft.md").exists()
    # 圣经快照删除。
    assert not (series_dir / "bible_snapshots" / "ep_001.json").exists()


def test_reset_from_stage_s4_can_drop_human_storyboard(tmp_path) -> None:
    series_id, series_dir = _register_series(tmp_path)
    episode_dir = _build_episode(series_dir, storyboard_source="human")
    manager = ManifestManager(series_dir, series_id=series_id)

    summary = manager.reset_from_stage(
        1, "s4_scriptwriter", include_human_storyboard=True
    )

    assert summary["preserved_human_storyboard"] is False
    assert not (episode_dir / "04_script" / "storyboard.json").exists()


def test_reset_from_stage_s3_drops_storyboard(tmp_path) -> None:
    series_id, series_dir = _register_series(tmp_path)
    episode_dir = _build_episode(series_dir, storyboard_source="human")
    manager = ManifestManager(series_dir, series_id=series_id)

    summary = manager.reset_from_stage(1, "s3_vision")

    assert "s3_vision" in summary["cleared_stages"]
    assert not (episode_dir / "03_vision" / "vision.json").exists()
    # 人工镜头组在 S3 重来时也不保留（下游产物全部清空）。
    assert not (episode_dir / "04_script" / "storyboard.json").exists()
    assert not (episode_dir / "04_script" / "script.json").exists()


def test_reset_from_stage_rejects_s0_catalog(tmp_path) -> None:
    series_id, series_dir = _register_series(tmp_path)
    _build_episode(series_dir)
    manager = ManifestManager(series_dir, series_id=series_id)

    with pytest.raises(ValueError) as excinfo:
        manager.reset_from_stage(1, "s0_catalog")
    assert "只能从 S1 ~ S7 清理单话下游阶段" in str(excinfo.value)


def test_reset_episode_api_returns_summary(tmp_path) -> None:
    series_id, series_dir = _register_series(tmp_path)
    _build_episode(series_dir)
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    client = TestClient(create_app(config))

    episode_id = f"{series_id}_ep_001"
    response = client.post(
        f"/api/episodes/{episode_id}/reset",
        json={"from_stage": "s5_tts"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["episode_id"] == episode_id
    assert payload["from_stage"] == "s5_tts"
    assert payload["cleared_stages"] == ["s5_tts", "s6_timeline", "s7_renderer"]
    assert payload["removed_count"] > 0
    assert payload["preserved_human_storyboard"] is False
    assert isinstance(payload["stages"], list)


def test_reset_episode_api_rejects_s0_catalog(tmp_path) -> None:
    series_id, series_dir = _register_series(tmp_path)
    _build_episode(series_dir)
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    client = TestClient(create_app(config))

    response = client.post(
        f"/api/episodes/{series_id}_ep_001/reset",
        json={"from_stage": "s0_catalog"},
    )

    assert response.status_code == 400
    assert "只能从 S1 ~ S7 清理单话下游阶段" in response.json()["detail"]


def test_reset_episode_api_conflicts_while_running(tmp_path) -> None:
    series_id, series_dir = _register_series(tmp_path)
    _build_episode(series_dir)
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    runner = JobRunner(config)
    client = TestClient(create_app(config, runner=runner))

    state = runner.state_of(series_id, 1)
    state.status = "running"

    response = client.post(
        f"/api/episodes/{series_id}_ep_001/reset",
        json={"from_stage": "s5_tts"},
    )

    assert response.status_code == 409
    assert "请等待完成后再清理" in response.json()["detail"]


def test_job_runner_reset_clears_memory_state(tmp_path) -> None:
    series_id, series_dir = _register_series(tmp_path)
    episode_dir = _build_episode(series_dir)
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    runner = JobRunner(config)

    state = runner.state_of(series_id, 1)
    state.stages = {
        "s4_scriptwriter": {"status": "done"},
        "s5_tts": {"status": "done"},
        "s6_timeline": {"status": "done"},
    }
    state.current_stage = "s6_timeline"
    state.status = "failed"
    state.error = "boom"

    summary = runner.reset_episode_from_stage(series_id, 1, "s5_tts")

    assert summary["cleared_stages"] == ["s5_tts", "s6_timeline", "s7_renderer"]
    assert "s5_tts" not in state.stages
    assert "s6_timeline" not in state.stages
    assert "s4_scriptwriter" in state.stages
    assert state.current_stage == ""
    assert state.status == "idle"
    assert state.error == ""
    assert not (episode_dir / "05_audio" / "audio_manifest.json").exists()


def test_job_runner_reset_rejects_running_episode(tmp_path) -> None:
    series_id, series_dir = _register_series(tmp_path)
    _build_episode(series_dir)
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    runner = JobRunner(config)

    state = runner.state_of(series_id, 1)
    state.status = "running"

    with pytest.raises(JobError) as excinfo:
        runner.reset_episode_from_stage(series_id, 1, "s5_tts")
    assert "请等待完成后再清理" in str(excinfo.value)


@pytest.mark.anyio
async def test_submit_episode_clears_downstream_before_running(
    tmp_path, monkeypatch
) -> None:
    series_id, series_dir = _register_series(tmp_path)
    episode_dir = _build_episode(series_dir)
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    runner = JobRunner(config, client_factory=lambda: _StubClient())

    seen: dict[str, dict[str, bool]] = {}

    async def fake_run_stage(self, state, *, stage, **kwargs):
        seen[stage] = {
            "audio": (episode_dir / "05_audio" / "audio_manifest.json").exists(),
            "timeline": (episode_dir / "06_plan" / "timeline.json").exists(),
            "out": (episode_dir / "07_out" / "ep_001.mp4").exists(),
            "export": (series_dir / "exports" / "EP001_final.mp4").exists(),
        }
        state.stages[stage] = {"status": "done", "duration_sec": 0.1}

    monkeypatch.setattr(JobRunner, "_run_stage", fake_run_stage)

    await runner.submit_episode(
        series_id, 1, from_stage="s4_scriptwriter", mode="phase1_only"
    )
    await runner.wait_all()

    # 开跑 Stage 4 之前，S5~S7 的旧产物与导出成片已经被清空。
    assert "s4_scriptwriter" in seen
    assert seen["s4_scriptwriter"] == {
        "audio": False,
        "timeline": False,
        "out": False,
        "export": False,
    }

    # manifest 里的旧完成记录也被移除，状态回到待执行。
    manifest = ManifestManager(series_dir, series_id=series_id).load(1)
    assert "s4_scriptwriter" not in manifest.stages
    assert "s5_tts" not in manifest.stages
    assert "s6_timeline" not in manifest.stages
    assert "s7_renderer" not in manifest.stages


def test_cli_clean_command_reports_summary(tmp_path, capsys) -> None:
    from cli import run_clean_series

    series_id, series_dir = _register_series(tmp_path)
    _build_episode(series_dir)
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))

    result = run_clean_series(series_id, config=config, from_stage="s5_tts")

    assert result["command"] == "clean"
    assert result["from_stage"] == "s5_tts"
    assert len(result["episodes"]) == 1
    entry = result["episodes"][0]
    assert entry["episode_no"] == 1
    assert entry["cleared_stages"] == ["s5_tts", "s6_timeline", "s7_renderer"]
    assert not (series_dir / "episodes" / "ep_001" / "05_audio" / "audio_manifest.json").exists()
