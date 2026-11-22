"""``w2v`` 命令行（``src/cli.py``）离线测试。

覆盖：Stage 0 登记 + Stage 1 下载、Stage 2~4 串联、**多部漫画并发**、
**部内 Stage 4 按话升序串行**（规范铁律三）。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from cli import (
    download_series,
    export_series,
    main,
    produce_scripts,
    run_download,
    run_understand,
    select_episodes,
    understand_series,
)
from core.config import AppConfig, ConfigError, WorkspaceConfig
from core.manifest import ManifestManager
from core.schemas import EpisodeCatalogItem, SeriesCatalog
from providers.llm_base import ChatResponse

_VISION_JSON = json.dumps(
    {
        "panels": [
            {
                "panel_id": "panel_001",
                "scene_summary": "雨夜街头，少年攥紧信纸抬头望天",
                "dialogues": [{"speaker_guess": "小白", "text": "谁寄的？", "emotion": "tense"}],
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
        "character_updates": [
            {
                "name": "小白",
                "aliases": ["阿白"],
                "visual_traits": "白发红瞳",
                "current_status": "神情紧张",
                "first_seen_ep": 0,
                "last_seen_ep": 0,
            }
        ],
        "new_plot_threads": [],
        "resolved_thread_ids": [],
    },
    ensure_ascii=False,
)

_VISION_RE = re.compile(r"作品《(.+?)》第 (\d+) 话")
_SCRIPT_RE = re.compile(r"作品《(.+?)》第 (\d+) 话（`([^`]+)`）")

# Patch v3.0-7：Stage 4A 故事反写桩（合法 EpisodeStoryDraft，单章覆盖唯一面板）。
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


class Tracker:
    """记录 Stub 客户端的调用时间线。"""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.script_calls: list[dict] = []
        self.vision_calls: list[dict] = []
        self.vision_prompts: list[str] = []

    def enter(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)

    def leave(self) -> None:
        self.active -= 1


class StubClient:
    """离线 LLM 替身：按模型区分视觉 / 撰稿两种 JSON。"""

    def __init__(self, tracker: Tracker, *, delay: float = 0.05) -> None:
        self.tracker = tracker
        self.delay = delay

    @staticmethod
    def _prompt_text(messages) -> str:
        content = messages[0].content
        if isinstance(content, str):
            return content
        return "\n".join(part.text or "" for part in content)

    async def chat(self, messages, *, model, temperature=None, max_tokens=None, **kwargs):
        prompt = self._prompt_text(messages)
        start = time.monotonic()
        self.tracker.enter()
        try:
            await asyncio.sleep(self.delay)
            # Patch v4.22：默认识图模型从 qwen-vl-max 换成 qwen3.7-plus（模型名里
            # 不再带 "vl"），所以除了看模型名，还要按提示词特征判定这是不是视觉调用
            # （单格/批窗模板首句都是「你是漫画画面解析助手」，整幕复核提示词以
            # 「同一个镜头组（连续一幕）」开头）。
            is_vision_call = (
                "vl" in model
                or "漫画画面解析助手" in prompt
                or "同一个镜头组（连续一幕）" in prompt
            )
            if is_vision_call:
                match = _VISION_RE.search(prompt)
                title, episode_no = (match.group(1), int(match.group(2))) if match else ("?", 0)
                self.tracker.vision_calls.append(
                    {"series": title, "episode": episode_no, "start": start, "end": time.monotonic()}
                )
                self.tracker.vision_prompts.append(prompt)
                content = _VISION_JSON
            else:
                match = _SCRIPT_RE.search(prompt)
                title, episode_no, episode_id = (
                    (match.group(1), int(match.group(2)), match.group(3)) if match else ("?", 0, "")
                )
                self.tracker.script_calls.append(
                    {
                        "series": title,
                        "episode": episode_no,
                        "episode_id": episode_id,
                        "start": start,
                        "end": time.monotonic(),
                    }
                )
                # Patch v3.0-7：4A 故事反写 prompt 含 story_prose，返回合法章节稿；
                # 概览与 4B 拆拍继续复用 _SCRIPT_JSON（后者只取 beats 字段）。
                content = _STORY_JSON if "story_prose" in prompt else _SCRIPT_JSON
            return ChatResponse(content=content, model=model)
        finally:
            self.tracker.leave()

    async def aclose(self) -> None:
        return None


def _make_local_series(root: Path, name: str, episodes: tuple[int, ...] = (1,)) -> Path:
    """构造本地漫画目录（每个子目录 = 一话）。"""
    series_root = root / name
    for episode_no in episodes:
        episode_dir = series_root / f"ep_{episode_no:03d}"
        episode_dir.mkdir(parents=True, exist_ok=True)
        for index in range(1, 3):
            # 画出可辨认的内容块：Patch 6 起，纯色页会被判为「空镜」而跳过视觉模型
            image = Image.new("RGB", (800, 1200), (20 * index, 40, 60))
            draw = ImageDraw.Draw(image)
            for row in range(4):
                for col in range(3):
                    draw.rectangle(
                        [60 + col * 240, 80 + row * 280, 60 + col * 240 + 200, 80 + row * 280 + 240],
                        fill=(200 - 30 * row, 180 - 20 * col, 160),
                    )
            image.save(episode_dir / f"page_{index:03d}.png")
    return series_root


def _config(workspace: Path) -> AppConfig:
    return AppConfig(workspace=WorkspaceConfig(root=workspace))


def _overlaps(left: list[dict], right: list[dict]) -> bool:
    """两组调用区间是否存在时间重叠。"""
    for first in left:
        for second in right:
            if first["start"] < second["end"] and second["start"] < first["end"]:
                return True
    return False


@pytest.mark.anyio
async def test_download_registers_series_and_downloads_images(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    local = _make_local_series(tmp_path / "src", "comic_a", episodes=(1, 2))
    result = await download_series(str(local), config=_config(workspace))
    assert result["command"] == "download"
    assert result["series_id"] == "wt_local_comic_a"
    assert result["episodes"] == [1, 2]
    assert result["saved"] == 4
    assert result["failed"] == 0

    index = json.loads((workspace / "index.json").read_text(encoding="utf-8"))
    assert isinstance(index, list)
    assert index[0]["series_id"] == "wt_local_comic_a"
    assert index[0]["total_episodes"] == 2
    catalog = json.loads(
        (workspace / "wt_local_comic_a" / "catalog.json").read_text(encoding="utf-8")
    )
    assert [item["episode_no"] for item in catalog["episodes"]] == [1, 2]
    raw_files = sorted(
        path.name for path in (workspace / "wt_local_comic_a" / "episodes" / "ep_001" / "01_raw").iterdir()
    )
    assert raw_files == ["001.png", "002.png"]


@pytest.mark.anyio
async def test_download_supports_episode_selection(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    local = _make_local_series(tmp_path / "src", "comic_a", episodes=(1, 2, 3))
    result = await download_series(str(local), config=_config(workspace), episodes_spec="1,3")
    assert result["episodes"] == [1, 3]
    assert result["saved"] == 4


@pytest.mark.anyio
async def test_two_series_are_downloaded_concurrently(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    local_a = _make_local_series(tmp_path / "src", "comic_a")
    local_b = _make_local_series(tmp_path / "src", "comic_b")
    results = await run_download(
        [str(local_a), str(local_b)], config=_config(workspace)
    )
    assert {item["series_id"] for item in results} == {"wt_local_comic_a", "wt_local_comic_b"}
    index = json.loads((workspace / "index.json").read_text(encoding="utf-8"))
    assert len(index) == 2


@pytest.mark.anyio
async def test_understand_runs_stage2_to_stage4(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    local = _make_local_series(tmp_path / "src", "comic_a", episodes=(1,))
    await download_series(str(local), config=_config(workspace))
    tracker = Tracker()

    result = await understand_series(
        "wt_local_comic_a",
        config=_config(workspace),
        client_factory=lambda: StubClient(tracker),
    )
    assert result["command"] == "understand"
    assert result["episodes"] == [1]

    series_dir = workspace / "wt_local_comic_a"
    episode_dir = series_dir / "episodes" / "ep_001"
    assert (episode_dir / "02_panels" / "p_001.png").is_file()
    assert (episode_dir / "03_vision" / "vision.json").is_file()
    assert (episode_dir / "04_script" / "script.json").is_file()
    assert (episode_dir / "04_script" / "script_readable.md").is_file()
    assert (series_dir / "bible_snapshots" / "ep_001.json").is_file()


@pytest.mark.anyio
async def test_stage4_is_serialized_per_series_in_ascending_order(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    local = _make_local_series(tmp_path / "src", "comic_a", episodes=(1, 2, 3))
    await download_series(str(local), config=_config(workspace))
    tracker = Tracker()

    await understand_series(
        "wt_local_comic_a",
        config=_config(workspace),
        client_factory=lambda: StubClient(tracker),
    )
    script_calls = [call for call in tracker.script_calls if call["series"] == "comic_a"]
    # 两段式后每话有「整话概览 + 分批撰稿」多次调用，但同话的调用必须连续成块、
    # 话次整体仍按 1→2→3 升序，且不同话的调用区间互不重叠（部级串行锁）。
    episodes = [call["episode"] for call in script_calls]
    # 同话连续：groupby 后每个话号只出现一个连续块
    grouped = [episode for episode, _group in itertools.groupby(episodes)]
    assert grouped == [1, 2, 3]
    # 不同话的任意调用区间不重叠
    for earlier in script_calls:
        for later in script_calls:
            if earlier["episode"] < later["episode"]:
                assert earlier["end"] <= later["start"], "同一部的 Stage 4 必须按话串行"


@pytest.mark.anyio
async def test_two_series_are_understood_concurrently(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    local_a = _make_local_series(tmp_path / "src", "comic_a", episodes=(1, 2))
    local_b = _make_local_series(tmp_path / "src", "comic_b", episodes=(1, 2))
    await run_download([str(local_a), str(local_b)], config=_config(workspace))
    tracker = Tracker()

    results = await run_understand(
        ["wt_local_comic_a", "wt_local_comic_b"],
        config=_config(workspace),
        client_factory=lambda: StubClient(tracker),
    )
    assert len(results) == 2
    # 跨部完全并发：某一时刻至少有两个请求同时在飞
    assert tracker.max_active >= 2
    series_a = [call for call in tracker.script_calls if call["series"] == "comic_a"]
    series_b = [call for call in tracker.script_calls if call["series"] == "comic_b"]
    # 两段式后每话多次撰稿调用，按出现顺序去重仍须保持话次升序
    assert list(dict.fromkeys(call["episode"] for call in series_a)) == [1, 2]
    assert list(dict.fromkeys(call["episode"] for call in series_b)) == [1, 2]
    # 两部的请求在时间上交错（说明不是整体串行）
    calls_a = [
        call
        for call in tracker.vision_calls + tracker.script_calls
        if call["series"] == "comic_a"
    ]
    calls_b = [
        call
        for call in tracker.vision_calls + tracker.script_calls
        if call["series"] == "comic_b"
    ]
    assert _overlaps(calls_a, calls_b), "两部漫画的请求应当存在时间交错"


@pytest.mark.anyio
async def test_stage3_gets_character_list_from_previous_episode(tmp_path) -> None:
    """第 1 话沉淀的角色名单必须进入第 2 话的识图提示词（Layer 1 防脸盲）。"""
    workspace = tmp_path / "workspace"
    local = _make_local_series(tmp_path / "src", "comic_a", episodes=(1, 2))
    await download_series(str(local), config=_config(workspace))

    first = Tracker()
    await understand_series(
        "wt_local_comic_a",
        config=_config(workspace),
        episodes_spec="1",
        client_factory=lambda: StubClient(first),
    )
    assert not any("小白" in prompt for prompt in first.vision_prompts), "第 1 话尚无角色名单"

    second = Tracker()
    await understand_series(
        "wt_local_comic_a",
        config=_config(workspace),
        episodes_spec="2",
        client_factory=lambda: StubClient(second),
    )
    assert any("小白" in prompt for prompt in second.vision_prompts)


@pytest.mark.anyio
async def test_understand_requires_registered_series(tmp_path) -> None:
    with pytest.raises(ConfigError):
        await understand_series(
            "wt_missing",
            config=_config(tmp_path / "workspace"),
            client_factory=lambda: StubClient(Tracker()),
        )


def test_select_episodes_parses_spec() -> None:
    catalog = SeriesCatalog(
        series_id="wt_test",
        series_title="测试",
        list_url="local:///tmp/x",
        episodes=[
            EpisodeCatalogItem(
                episode_no=no,
                episode_id=f"wt_test_ep_{no:03d}",
                title=f"第{no}话",
                viewer_url=f"local:///tmp/x/ep_{no:03d}",
            )
            for no in (1, 2, 3)
        ],
    )
    assert [item.episode_no for item in select_episodes(catalog, "")] == [1, 2, 3]
    assert [item.episode_no for item in select_episodes(catalog, "1,3")] == [1, 3]
    assert [item.episode_no for item in select_episodes(catalog, "2-3")] == [2, 3]


def test_cli_main_download_prints_json(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    local = _make_local_series(tmp_path / "src", "comic_a", episodes=(1,))
    code = main(
        [
            "download",
            "--series",
            str(local),
            "--workspace",
            str(workspace),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out.strip().splitlines()
    payload = json.loads(out[-1])
    assert payload["command"] == "download"
    assert payload["series_id"] == "wt_local_comic_a"


def test_cli_main_returns_2_on_config_error(tmp_path, capsys) -> None:
    code = main(
        [
            "understand",
            "--series",
            "wt_missing",
            "--workspace",
            str(tmp_path / "workspace"),
        ]
    )
    assert code == 2
    assert "配置错误" in capsys.readouterr().err


@pytest.mark.anyio
async def test_export_series_copies_existing_rendered_videos(tmp_path) -> None:
    """Patch v2.5-3：``w2v export`` 对已有 07_out 成片直接复制，不重渲染。"""
    workspace = tmp_path / "workspace"
    local = _make_local_series(tmp_path / "src", "comic_a", episodes=(1, 2))
    await download_series(str(local), config=_config(workspace))

    series_dir = workspace / "wt_local_comic_a"
    # 模拟 Stage 7 已产出的成片（不跑真实 ffmpeg 渲染链路）
    for episode_no in (1, 2):
        out_dir = series_dir / "episodes" / f"ep_{episode_no:03d}" / "07_out"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"wt_local_comic_a_ep_{episode_no:03d}.mp4").write_bytes(
            f"video-{episode_no}".encode()
        )

    target = tmp_path / "output_videos"
    result = await export_series(
        "wt_local_comic_a",
        config=_config(workspace),
        episodes_spec="1-2",
        out_dir=str(target),
    )
    assert result["command"] == "export"
    assert result["series_id"] == "wt_local_comic_a"
    exported = sorted(Path(item["path"]).name for item in result["exported"])
    assert exported == [
        "wt_local_comic_a_ep_001.mp4",
        "wt_local_comic_a_ep_002.mp4",
    ]
    assert (target / "wt_local_comic_a_ep_001.mp4").read_bytes() == b"video-1"
    assert (target / "wt_local_comic_a_ep_002.mp4").read_bytes() == b"video-2"


@pytest.mark.anyio
async def test_export_series_rejects_unregistered_series(tmp_path) -> None:
    with pytest.raises(ConfigError):
        await export_series(
            "wt_not_registered",
            config=_config(tmp_path / "workspace"),
            out_dir=str(tmp_path / "out"),
        )


@pytest.mark.anyio
async def test_story_draft_mode_stops_after_stage_4a(tmp_path) -> None:
    """Patch v3.0-7：``story-draft`` 模式只产出故事初稿，不生成 script.json，
    且 manifest 必须记下 s4_scriptwriter 阶段（artifact 指向 story_draft.json）。"""
    workspace = tmp_path / "workspace"
    local = _make_local_series(tmp_path / "src", "comic_a", episodes=(1,))
    await download_series(str(local), config=_config(workspace))
    tracker = Tracker()

    _catalog, episodes, script_map = await produce_scripts(
        "wt_local_comic_a",
        config=_config(workspace),
        client_factory=lambda: StubClient(tracker),
        mode="story_draft",
    )
    assert [item.episode_no for item in episodes] == [1]

    script_dir = workspace / "wt_local_comic_a" / "episodes" / "ep_001" / "04_script"
    draft_json = script_dir / "story_draft.json"
    draft_md = script_dir / "story_draft.md"
    assert draft_json.is_file() and draft_md.is_file()
    # 4A 停下：不应出现 4B 才有的剧本与可读稿。
    assert not (script_dir / "script.json").exists()
    assert not (script_dir / "script_readable.md").exists()
    assert Path(script_map[1]).name == "story_draft.json"

    # manifest 不能再因非法 stage 名被静默吞掉（修复前 Web 控制台会显示「待执行」）。
    manifest = ManifestManager(workspace / "wt_local_comic_a").load(1)
    entry = manifest.stages["s4_scriptwriter"]
    assert entry.status == "done"
    assert entry.artifact is not None and entry.artifact.endswith("story_draft.json")


@pytest.mark.anyio
async def test_narrate_from_story_requires_and_uses_markdown(tmp_path) -> None:
    """``narrate-from-story`` 缺故事稿时抛 ConfigError；补稿后跳过 4A 直接出剧本。"""
    workspace = tmp_path / "workspace"
    local = _make_local_series(tmp_path / "src", "comic_a", episodes=(1,))
    await download_series(str(local), config=_config(workspace))

    # 还没有 story_draft.md → 明确报错，不允许偷偷重写。
    with pytest.raises(ConfigError, match="故事初稿"):
        await produce_scripts(
            "wt_local_comic_a",
            config=_config(workspace),
            client_factory=lambda: StubClient(Tracker()),
            mode="narrate_from_story",
        )

    # 先跑 4A 产出人工稿，再从人工稿跑 4B。
    await produce_scripts(
        "wt_local_comic_a",
        config=_config(workspace),
        client_factory=lambda: StubClient(Tracker()),
        mode="story_draft",
    )
    _catalog, _episodes, script_map = await produce_scripts(
        "wt_local_comic_a",
        config=_config(workspace),
        client_factory=lambda: StubClient(Tracker()),
        mode="narrate_from_story",
    )
    script_path = Path(script_map[1])
    assert script_path.name == "script.json" and script_path.is_file()
    assert (script_path.parent / "script_readable.md").is_file()
