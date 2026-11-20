"""Patch v4.13 + v4.14：幕与分句解耦、全链路简体、单句语速、语气词/停顿标签。

覆盖清单：

1. ``ScriptBeat.scene_id`` 的读写与 ``normalize_script_payload`` 的透传；
2. Stage 4 按幕生成时显式写入 ``scene_id``；
3. Stage 7 的 ``build_scene_bgm_spans`` 优先按 ``beat.scene_id`` 归属幕
   （改 ``target_panel_ids`` 也不会串幕）；
4. ``to_simplified_chinese`` 常见繁体转简体，且完整保留 ``(coughs)`` 与 ``<#0.5#>``；
5. 19 个语气词标签与 ``<#0.5#>`` 能通过 ``validate_tts_text_no_pinyin()`` /
   ``strip_pinyin()``，而真拼音与漏括号的裸英文仍被拦截；
6. ``normalize.py`` 的 ``clip_text`` / 清洗绝不腰斩 ``(clear-throat)`` 与 ``<#1.25#>``；
7. ``normalize_minimax_pause_tags`` 合并 ``<#0.5#><#0.8#>`` → ``<#1.3#>``，
   并剥除贴首 / 贴尾的孤立停顿；
8. ``narration_text`` 中误混入的 ``(sighs)`` 与 ``<#0.5#>`` 被洗净，
   而 ``tts_text`` 完整保留；
9. ``speed_override`` 的合法范围读写；
10. Episode 详情的 ``viewer_url`` 透传（http 链接与 local:// 回退）；
11. 前端关键产物（StoryboardEditor / EpisodeStudioView / TtsTagPicker）在位。

全部离线：不联网、不拉起真实 TTS。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pytest
from fastapi.testclient import TestClient

from core.config import AppConfig, SeriesConfig, WorkspaceConfig
from core.normalize import (
    clip_text,
    normalize_script_payload,
    to_simplified_chinese,
)
from core.schemas import (
    CameraMotion,
    EpisodeDigest,
    EpisodeStoryboard,
    NarrationScript,
    RenderTimeline,
    ScriptBeat,
    StoryboardScene,
    TimelineScene,
    save_model,
)
from stages import s4_scriptwriter, s7_renderer
from utils.tts_tags import (
    MINIMAX_INTERJECTION_TAGS,
    mask_minimax_tts_tags,
    normalize_minimax_pause_tags,
    strip_minimax_tts_tags,
)
from web.job_runner import JobRunner
from web.server import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------
def _beat(beat_id: str, narration_text: str, **kwargs: Any) -> ScriptBeat:
    payload: dict[str, Any] = {
        "beat_id": beat_id,
        "narration_text": narration_text,
        "target_panel_ids": ["p_001"],
    }
    payload.update(kwargs)
    return ScriptBeat(**payload)


def _scene(scene_id: str, scene_index: int, panel_ids: Sequence[str], **kwargs: Any) -> StoryboardScene:
    payload: dict[str, Any] = {
        "scene_id": scene_id,
        "scene_index": scene_index,
        "panel_ids": list(panel_ids),
    }
    payload.update(kwargs)
    return StoryboardScene(**payload)


def _timeline_with_panels(panel_ids: Sequence[str], step_ms: int = 1000) -> RenderTimeline:
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
        total_duration_ms=len(panel_ids) * step_ms,
        scenes=scenes,
        audios=[],
        subtitles=[],
    )


# ---------------------------------------------------------------------------
# 组 1：scene_id 读写
# ---------------------------------------------------------------------------
def test_script_beat_scene_id_roundtrip(tmp_path: Path) -> None:
    """ScriptBeat.scene_id 可读写，落盘后能原样读回。"""
    beat = _beat("b1", "他推开了那扇门。", scene_id="sc_002")
    assert beat.scene_id == "sc_002"

    script = NarrationScript(
        episode_id="wt_test_ep_001",
        hook_title="雨夜来客",
        beats=[beat],
        episode_digest=EpisodeDigest(
            episode_no=1, one_line_hook="雨夜有人来访", summary="来客自称银行行长。"
        ),
    )
    path = tmp_path / "script.json"
    save_model(script, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["beats"][0]["scene_id"] == "sc_002"

    # 缺省时为 None（向后兼容旧剧本）。
    assert _beat("b2", "没有人应门。").scene_id is None


def test_normalize_script_payload_keeps_scene_id_and_speed_override() -> None:
    """normalize_script_payload 透传 scene_id，并读取/钳位 speed_override。"""
    payload = {
        "episode_id": "wt_test_ep_001",
        "hook_title": "雨夜来客",
        "beats": [
            {
                "beat_id": "b1",
                "narration_text": "他推开了那扇门。",
                "target_panel_ids": ["p_001"],
                "scene_id": "sc_003",
                "speed_override": 1.15,
            },
            {
                "beat_id": "b2",
                "narration_text": "没有人应门。",
                "target_panel_ids": ["p_002"],
            },
        ],
    }
    result = normalize_script_payload(payload, episode_id="wt_test_ep_001", episode_no=1)
    assert result["beats"][0]["scene_id"] == "sc_003"
    assert result["beats"][0]["speed_override"] == pytest.approx(1.15)
    assert result["beats"][1]["scene_id"] is None
    assert result["beats"][1]["speed_override"] is None


def test_normalize_script_payload_clamps_speed_override_range() -> None:
    """speed_override 越界时被钳位到 [0.5, 2.0]。"""
    payload = {
        "episode_id": "wt_test_ep_001",
        "hook_title": "雨夜来客",
        "beats": [
            {
                "beat_id": "b1",
                "narration_text": "他推开了那扇门。",
                "target_panel_ids": ["p_001"],
                "speed_override": 9.0,
            },
            {
                "beat_id": "b2",
                "narration_text": "没有人应门。",
                "target_panel_ids": ["p_002"],
                "speed_override": 0.01,
            },
        ],
    }
    result = normalize_script_payload(payload, episode_id="wt_test_ep_001", episode_no=1)
    assert result["beats"][0]["speed_override"] == pytest.approx(2.0)
    assert result["beats"][1]["speed_override"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 组 2：Stage 4 按幕写入 scene_id
# ---------------------------------------------------------------------------
def test_stage4_assigns_scene_id_per_scene() -> None:
    """Stage 4 逐幕拍摄时把该幕的 scene_id 写进每条解说。"""
    scene = _scene("sc_007", 3, ["p_001", "p_002"])
    beats: list[dict[str, Any]] = [
        {"beat_id": "b1", "narration_text": "他推开了那扇门。"},
        {"beat_id": "b2", "narration_text": "没有人应门。"},
    ]
    s4_scriptwriter._assign_scene_id(beats, scene)
    assert [beat["scene_id"] for beat in beats] == ["sc_007", "sc_007"]


def test_stage4_clamps_speed_overrides() -> None:
    """Stage 4 落盘前把 speed_override 钳位到合法区间。"""
    beats: list[dict[str, Any]] = [
        {"beat_id": "b1", "narration_text": "他推开了那扇门。", "speed_override": 4.0},
        {"beat_id": "b2", "narration_text": "没有人应门。", "speed_override": 0.2},
        {"beat_id": "b3", "narration_text": "脚步声远了。"},
    ]
    s4_scriptwriter._clamp_beat_speed_overrides(beats)
    assert beats[0]["speed_override"] == pytest.approx(2.0)
    assert beats[1]["speed_override"] == pytest.approx(0.5)
    assert beats[2].get("speed_override") is None


# ---------------------------------------------------------------------------
# 组 3：Stage 7 按 beat.scene_id 归属幕（改 target_panel_ids 不串幕）
# ---------------------------------------------------------------------------
def test_stage7_bgm_spans_follow_beat_scene_id(tmp_path: Path) -> None:
    """beat.scene_id 优先于分镜归属决定 BGM 段落。"""
    series_dir = tmp_path / "wt_series"
    bgm_dir = series_dir / "assets" / "bgm"
    bgm_dir.mkdir(parents=True)
    (bgm_dir / "theme.wav").write_bytes(b"a")
    (bgm_dir / "thriller.wav").write_bytes(b"b")

    settings = AppConfig(workspace=WorkspaceConfig(root=tmp_path / "workspace"))
    storyboard = EpisodeStoryboard(
        episode_id="wt_test_ep_001",
        source="human",
        scenes=[
            _scene("sc_001", 1, ["p_001", "p_002"], bgm="theme.wav"),
            _scene("sc_002", 2, ["p_003", "p_004"], bgm="thriller.wav"),
        ],
    )
    timeline = _timeline_with_panels(["p_001", "p_002", "p_003", "p_004"])

    # 关键：第一条 beat 的分镜属于 sc_001，但 scene_id 指向 sc_002；
    # 归属必须跟着 scene_id 走，不能串到 sc_001。
    beat_scene_map = {"b_001": "sc_002", "b_002": "sc_002", "b_003": "sc_002", "b_004": "sc_002"}
    spans = s7_renderer.build_scene_bgm_spans(
        storyboard,
        timeline,
        series_dir=series_dir,
        series_config=SeriesConfig(),
        settings=settings,
        beat_scene_map=beat_scene_map,
    )
    assert len(spans) == 1
    assert spans[0].bgm_path == bgm_dir / "thriller.wav"
    assert spans[0].start_ms == 0
    assert spans[0].end_ms == 4000

    # 不传 beat_scene_map 时回退按分镜归属 → 两段。
    fallback = s7_renderer.build_scene_bgm_spans(
        storyboard,
        timeline,
        series_dir=series_dir,
        series_config=SeriesConfig(),
        settings=settings,
    )
    assert len(fallback) == 2
    assert fallback[0].bgm_path == bgm_dir / "theme.wav"
    assert fallback[1].bgm_path == bgm_dir / "thriller.wav"


def test_load_beat_scene_map_reads_script_json(tmp_path: Path) -> None:
    """load_beat_scene_map 从 04_script/script.json 读出 beat → scene 映射。"""
    episode_dir = tmp_path / "ep_001"
    script_dir = episode_dir / "04_script"
    script_dir.mkdir(parents=True)
    script = NarrationScript(
        episode_id="wt_test_ep_001",
        hook_title="雨夜来客",
        beats=[
            _beat("b1", "他推开了那扇门。", scene_id="sc_001"),
            _beat("b2", "没有人应门。", scene_id="sc_002"),
            _beat("b3", "脚步声远了。"),
        ],
        episode_digest=EpisodeDigest(
            episode_no=1, one_line_hook="雨夜有人来访", summary="来客自称银行行长。"
        ),
    )
    save_model(script, script_dir / "script.json")
    mapping = s7_renderer.load_beat_scene_map(episode_dir)
    assert mapping == {"b1": "sc_001", "b2": "sc_002"}

    # 缺文件时返回空映射而不是抛错。
    assert s7_renderer.load_beat_scene_map(tmp_path / "missing") == {}


# ---------------------------------------------------------------------------
# 组 4：全链路简体中文
# ---------------------------------------------------------------------------
def test_to_simplified_chinese_converts_traditional() -> None:
    """常见繁体转简体。"""
    assert to_simplified_chinese("這是一個測試") == "这是一个测试"
    assert to_simplified_chinese("後臺開發") == "后台开发"
    assert to_simplified_chinese("銀行帳號") == "银行帐号"
    # 已经简体的文本保持不变。
    assert to_simplified_chinese("这是简体中文") == "这是简体中文"


def test_to_simplified_chinese_preserves_tts_tags() -> None:
    """简体转换不得破坏 MiniMax 语气词与停顿标签。"""
    text = "Hello (coughs) <#0.5#> 世界"
    assert to_simplified_chinese(text) == text
    assert to_simplified_chinese("(laughs) 這很好笑") == "(laughs) 这很好笑"


# ---------------------------------------------------------------------------
# 组 5：拼音校验放行官方标签
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tag", [f"({name})" for name in MINIMAX_INTERJECTION_TAGS])
def test_interjection_tags_pass_pinyin_validation(tag: str) -> None:
    """19 个官方语气词标签都能通过拼音校验。"""
    beats = [
        {
            "beat_id": "b1",
            "narration_text": "他推开了那扇门。",
            "tts_text": f"{tag} 他推开了那扇门。",
            "target_panel_ids": ["p_001"],
        }
    ]
    s4_scriptwriter.validate_tts_text_no_pinyin(beats)
    stripped = s4_scriptwriter.strip_pinyin(beats[0]["tts_text"])
    assert tag in stripped


def test_pause_tag_and_interjection_pass_pinyin_validation() -> None:
    """<#0.5#> 与 (clear-throat) 等组合能通过校验并保留。"""
    text = "(clear-throat) 再来一次 <#0.5#> 结束"
    beats = [
        {
            "beat_id": "b1",
            "narration_text": "他推开了那扇门。",
            "tts_text": text,
            "target_panel_ids": ["p_001"],
        }
    ]
    s4_scriptwriter.validate_tts_text_no_pinyin(beats)
    assert s4_scriptwriter.strip_pinyin(text) == text


def test_bare_english_and_real_pinyin_still_blocked() -> None:
    """漏括号的裸英文与真拼音仍被拦截。"""
    bare = [
        {
            "beat_id": "b1",
            "narration_text": "他推开了那扇门。",
            "tts_text": "coughs 他推开了那扇门。",
            "target_panel_ids": ["p_001"],
        }
    ]
    errors = s4_scriptwriter.validate_tts_text_no_pinyin(bare)
    assert errors and "coughs" in errors[0]

    pinyin = [
        {
            "beat_id": "b1",
            "narration_text": "他推开了那扇门。",
            "tts_text": "hùn xiǎo 你好",
            "target_panel_ids": ["p_001"],
        }
    ]
    errors = s4_scriptwriter.validate_tts_text_no_pinyin(pinyin)
    assert errors and "拼音" in errors[0]


def test_strip_pinyin_keeps_tags_only() -> None:
    """剥拼音时标签原样保留。"""
    assert s4_scriptwriter.strip_pinyin("(coughs) hùn xiǎo 你好") == "(coughs) 你好"
    assert s4_scriptwriter.strip_pinyin("hùn xiǎo") == ""
    masked = mask_minimax_tts_tags("(laughs) 你好")
    assert len(masked) == len("(laughs) 你好")
    assert "laughs" not in masked


# ---------------------------------------------------------------------------
# 组 6：标签不被腰斩
# ---------------------------------------------------------------------------
def test_clip_text_never_cuts_tag_in_half() -> None:
    """clip_text 不得把 (clear-throat) / <#1.25#> 从中间截断。"""
    assert clip_text("(clear-throat)你好", 8) == "(clear-throat)你好"
    # 标签完整保留（绝不出现半截标签），正文按额度截断。
    kept = clip_text("(clear-throat)你好世界再见", 8)
    assert kept.startswith("(clear-throat)")
    assert not kept[len("(clear-throat)") :].startswith(")")
    clipped = clip_text("你好<#1.25#>世界", 2)
    assert clipped == "你好<#1.25#>"
    # 绝不会出现被腰斩的停顿标签（缺右尖括号）。
    assert "<#0." not in clipped and clipped.count("<#") == clipped.count("#>")


def test_clip_text_keeps_whole_tag_or_drops_it() -> None:
    """标签要么完整保留、要么整段丢弃。"""
    assert clip_text("(laughs)你好世界", 2) == "(laughs)你好"
    assert clip_text("你好世界", 2) == "你好"


# ---------------------------------------------------------------------------
# 组 7：停顿标签归一
# ---------------------------------------------------------------------------
def test_normalize_pause_tags_merges_adjacent() -> None:
    """相邻 / 仅隔空白的多个停顿合并成一个并求和。"""
    assert normalize_minimax_pause_tags("<#0.5#><#0.8#>") == "<#1.3#>"
    assert normalize_minimax_pause_tags("<#0.5#> <#0.8#>") == "<#1.3#>"
    assert normalize_minimax_pause_tags("你好<#0.5#>世界<#0.8#>再见") == "你好<#0.5#>世界<#0.8#>再见"


def test_normalize_pause_tags_strips_edge_isolated_tags() -> None:
    """贴首 / 贴尾的孤立停顿被剥除。"""
    assert normalize_minimax_pause_tags("<#0.5#>你好") == "你好"
    assert normalize_minimax_pause_tags("你好<#0.5#>") == "你好"
    assert normalize_minimax_pause_tags("你好<#0.5#>世界<#1.25#>") == "你好<#0.5#>世界"


def test_normalize_pause_tags_fixes_out_of_range() -> None:
    """越界 / 非法写法的停顿被清理或钳位。"""
    assert normalize_minimax_pause_tags("<#1.234#>你好") == "你好"
    assert normalize_minimax_pause_tags("<#abc#>你好") == "你好"
    assert normalize_minimax_pause_tags("你好<#0#>世界") == "你好<#0.01#>世界"
    assert normalize_minimax_pause_tags("你好<#120#>世界") == "你好<#99.99#>世界"


def test_strip_minimax_tts_tags_removes_all() -> None:
    """strip 移除全部语气词与停顿标签并收敛空格。"""
    assert strip_minimax_tts_tags("(sighs)你好<#0.5#>世界") == "你好世界"


# ---------------------------------------------------------------------------
# 组 8：字幕轨与送音轨分离
# ---------------------------------------------------------------------------
def test_narration_is_cleaned_but_tts_text_keeps_tags() -> None:
    """narration_text 里的标签被洗净，tts_text 完整保留。"""
    payload = {
        "episode_id": "wt_test_ep_001",
        "hook_title": "雨夜来客",
        "beats": [
            {
                "beat_id": "b1",
                "narration_text": "(sighs) 他推开了那扇门。<#0.5#>没有人应门。",
                "tts_text": "(sighs) 他推开了那扇门。<#0.5#>没有人应门。",
                "target_panel_ids": ["p_001"],
            }
        ],
    }
    result = normalize_script_payload(payload, episode_id="wt_test_ep_001", episode_no=1)
    beat = result["beats"][0]
    assert "(sighs)" not in beat["narration_text"]
    assert "<#0.5#>" not in beat["narration_text"]
    assert "(sighs)" in (beat["tts_text"] or "")
    assert "<#0.5#>" in (beat["tts_text"] or "")


# ---------------------------------------------------------------------------
# 组 9：Episode 详情 viewer_url 透传
# ---------------------------------------------------------------------------
def _seed_series(tmp_path: Path) -> tuple[str, Path, AppConfig]:
    """登记一部只有一话的作品，返回 (series_id, series_dir, config)。"""
    from stages import s0_catalog

    src = tmp_path / "src"
    ep_src = src / "ep_001"
    ep_src.mkdir(parents=True, exist_ok=True)
    from PIL import Image

    Image.new("RGB", (40, 60), (10, 20, 30)).save(ep_src / "001.png")
    catalog = s0_catalog.import_local_catalog(src)
    s0_catalog.CatalogRegistry(tmp_path).register(catalog)
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path))
    return catalog.series_id, tmp_path / catalog.series_id, config


def test_episode_detail_returns_viewer_url(tmp_path: Path) -> None:
    """作品详情里的 viewer_url 可透传到前端。"""
    series_id, series_dir, config = _seed_series(tmp_path)
    catalog_path = series_dir / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["episodes"][0]["viewer_url"] = "https://www.webtoons.com/zh-hant/romance/only-hope/ep-1"
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    client = TestClient(create_app(config, runner=JobRunner(config)))
    response = client.get(f"/api/series/{series_id}")
    assert response.status_code == 200
    episodes = response.json()["episodes"]
    assert episodes[0]["viewer_url"] == "https://www.webtoons.com/zh-hant/romance/only-hope/ep-1"


def test_episode_detail_viewer_url_falls_back_to_local(tmp_path: Path) -> None:
    """本地目录导入的 viewer_url 保持 local:// 前缀（前端只显示标签）。"""
    series_id, _series_dir, config = _seed_series(tmp_path)
    client = TestClient(create_app(config, runner=JobRunner(config)))
    response = client.get(f"/api/series/{series_id}")
    assert response.status_code == 200
    viewer_url = response.json()["episodes"][0]["viewer_url"]
    assert viewer_url.startswith("local://")


# ---------------------------------------------------------------------------
# 组 10：前端产物在位
# ---------------------------------------------------------------------------
def test_frontend_artifacts_present() -> None:
    """Patch v4.13 / v4.14 的前端关键产物必须在位。"""
    tag_picker = (REPO_ROOT / "src" / "components" / "TtsTagPicker.tsx").read_text(encoding="utf-8")
    assert "INTERJECTION_TAG_GROUPS" in tag_picker or "语气词" in tag_picker
    assert "停顿" in tag_picker
    assert "window.alert" not in tag_picker
    assert "window.confirm" not in tag_picker

    tags = (REPO_ROOT / "src" / "constants" / "ttsTags.ts").read_text(encoding="utf-8")
    assert "MINIMAX_INTERJECTION_TAGS" in tags
    assert "clear-throat" in tags
    assert "lip-smacking" in tags

    studio = (REPO_ROOT / "src" / "components" / "EpisodeStudioView.tsx").read_text(encoding="utf-8")
    assert "TtsTagPicker" in studio
    assert "查看原漫" in studio
    assert "noopener noreferrer" in studio
    assert "scene_id" in studio
    assert "window.confirm(" not in studio
    assert "window.alert(" not in studio

    storyboard = (REPO_ROOT / "src" / "components" / "StoryboardEditor.tsx").read_text(encoding="utf-8")
    assert "addPanelToScene" in storyboard
    assert "removePanelFromScene" in storyboard

    types = (REPO_ROOT / "src" / "types" / "studio.ts").read_text(encoding="utf-8")
    assert "scene_id" in types


def test_prompts_document_new_rules() -> None:
    """提示词里必须写清简体、语速波动、语气词与停顿标签的规则。"""
    narrate = (REPO_ROOT / "backend" / "prompts" / "script_narrate.md").read_text(encoding="utf-8")
    assert "speed_override" in narrate
    assert "(laughs)" in narrate
    assert "<#x#>" in narrate or "<#0.5#>" in narrate
    assert "简体中文" in narrate

    vision = (REPO_ROOT / "backend" / "prompts" / "vision_extract.md").read_text(encoding="utf-8")
    assert "简体中文" in vision
