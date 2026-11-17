"""Patch v3.1 Patch 9 回归测试。

覆盖三条验收标准：

1. **字符净化**：旁白 / TTS 文本里不再出现 ``♥`` ``★`` ``☒`` ``□`` 这类字体缺失
   的装饰符号（成片 03:46 的「☒」方块）；Emoji、私用区、控制字符一并剔除。
2. **sfx_only 整格判定**：尾部一个 ``sfx_only`` 切片不能把前面带对白的超长画格
   整格误杀；只有「所有有效切片都是 sfx_only 且合并后无对白」才整格标 sfx_only。
3. **超窄装饰横条 / 纯黑过渡页**：裁边后有效高度 < 150px 的暗色细条，以及纯黑
   过渡页，都必须标 ``is_transition_blank=True``。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from PIL import Image

from core.normalize import normalize_script_payload, sanitize_narration_and_tts
from core.schemas import StoryChapter
from core.subtitle_render import strip_render_unsafe_chars
from stages import s3_vision, s4_scriptwriter
from stages.s2_segmenter import (
    PANEL_WHITE_MARGIN_PX,
    imwrite_unicode,
    is_thin_decoration_strip,
    segment_episode,
)
from web import job_runner

WIDTH = 1080


# --------------------------------------------------------------------------
# 1. 字符净化
# --------------------------------------------------------------------------
def test_normalize_script_payload_strips_missing_glyph_symbols() -> None:
    """验收 (a)：``♥`` / ``★`` / ``☒`` 不得进入 narration_text 与 tts_text。"""
    payload = {
        "beats": [
            {
                "beat_id": "b1",
                "narration_text": "今天在别墅过如何♥★☒~",
                "tts_text": "今天在别墅过如何♥★☒~",
                "target_panel_ids": ["p_001"],
            }
        ]
    }
    script = normalize_script_payload(
        payload, episode_id="ep_001", episode_no=1, valid_panel_ids={"p_001"}
    )

    assert len(script["beats"]) == 1
    beat = script["beats"][0]
    for text in (beat["narration_text"], beat["tts_text"]):
        assert text is not None
        for symbol in ("♥", "♡", "★", "☆", "☒", "□"):
            assert symbol not in text
    assert beat["narration_text"].startswith("今天在别墅过如何")
    # 送音文本额外去掉尾部多余的 "~"（合成会读出怪音）。
    assert not beat["tts_text"].endswith("~")


def test_sanitize_removes_emoji_private_use_and_control_chars() -> None:
    """Emoji、私用区、控制字符一并剔除，正文不受影响。"""
    cleaned = sanitize_narration_and_tts("旁白😀\ue000文字\x00尾")
    assert "😀" not in cleaned
    assert "\ue000" not in cleaned
    assert "\x00" not in cleaned
    assert "旁白" in cleaned
    assert "文字" in cleaned
    assert "尾" in cleaned


def test_sanitize_collapses_repeated_wave_and_dash() -> None:
    """连续波浪号只留 1 个；连续破折号收敛成中文标准的一组。"""
    assert sanitize_narration_and_tts("语气~~~") == "语气~"
    assert sanitize_narration_and_tts("语气——") == "语气——"
    assert sanitize_narration_and_tts("语气————") == "语气——"
    # for_tts=True 时末尾多余的 "~" 直接去掉。
    assert sanitize_narration_and_tts("语气~", for_tts=True) == "语气"
    assert sanitize_narration_and_tts("语气~", for_tts=False) == "语气~"


def test_strip_render_unsafe_chars_is_the_render_side_safety_net() -> None:
    """字幕渲染侧的兜底：旧稿（未净化）也不会画出方块。"""
    text = "今天在别墅过如何♥★☒□\ue000😀"
    cleaned = strip_render_unsafe_chars(text)
    for symbol in ("♥", "★", "☒", "□", "😀"):
        assert symbol not in cleaned
    assert "\ue000" not in cleaned
    assert cleaned.startswith("今天在别墅过如何")
    # 幂等：净化两次结果一致。
    assert strip_render_unsafe_chars(cleaned) == cleaned


# --------------------------------------------------------------------------
# 2. sfx_only 整格判定
# --------------------------------------------------------------------------
def _tile(index: int, count: int, top: int, bottom: int) -> s3_vision.PanelTile:
    return s3_vision.PanelTile(
        image=Image.new("RGB", (WIDTH, bottom - top), "white"),
        top=top,
        bottom=bottom,
        total_height=1000,
        index=index,
        count=count,
    )


def _tile_payload(
    *,
    non_story_type: str = "none",
    blank: bool = False,
    dialogues: list[dict] | None = None,
    sfx: list[str] | None = None,
) -> dict:
    return {
        "panel_id": "p_001",
        "scene_summary": "画面内容",
        "dialogues": dialogues or [],
        "sfx_list": sfx if sfx is not None else ["咚"],
        "focus_region": [0.0, 0.0, 1.0, 1.0],
        "is_transition_blank": blank,
        "non_story_type": non_story_type,
    }


def test_merge_tile_payloads_keeps_dialogue_panel_when_tail_is_sfx_only() -> None:
    """验收 (b)：``[有对白的 none, 无对白的 sfx_only]`` → 整格仍是 none 且对白完整。"""
    tiles = [
        (
            _tile(0, 2, 0, 500),
            _tile_payload(
                non_story_type="none",
                dialogues=[{"speaker_guess": "小白", "text": "你终于来了"}],
            ),
        ),
        (_tile(1, 2, 500, 1000), _tile_payload(non_story_type="sfx_only", sfx=["轰"])),
    ]
    merged = s3_vision._merge_tile_payloads("p_001", tiles)

    assert merged["non_story_type"] == "none"
    assert merged["dialogues"]
    assert merged["dialogues"][0]["text"] == "你终于来了"


def test_merge_tile_payloads_marks_sfx_only_only_when_all_tiles_are_sfx() -> None:
    """所有有效切片都是 sfx_only 且无对白，整格才标 sfx_only。"""
    tiles = [
        (_tile(0, 2, 0, 500), _tile_payload(non_story_type="sfx_only")),
        (_tile(1, 2, 500, 1000), _tile_payload(non_story_type="sfx_only", sfx=["锵"])),
    ]
    merged = s3_vision._merge_tile_payloads("p_001", tiles)

    assert merged["non_story_type"] == "sfx_only"
    assert merged["dialogues"] == []


# --------------------------------------------------------------------------
# 3. 超窄装饰横条 / 纯黑过渡页
# --------------------------------------------------------------------------
def test_thin_strip_detector_flags_narrow_strip_only() -> None:
    """有效高度 < 150px 且细节稀薄 → 装饰窄条；正常高度的暗块不算。"""
    strip = np.full((60, WIDTH), 90, dtype=np.uint8)
    assert is_thin_decoration_strip(strip, width=WIDTH) is True

    tall_block = np.full((200, WIDTH), 90, dtype=np.uint8)
    assert is_thin_decoration_strip(tall_block, width=WIDTH) is False


def test_thin_strip_detector_keeps_black_text_bar() -> None:
    """黑底白字的旁白条 / 对话框不能被当成装饰条误杀。"""
    bar = np.full((60, WIDTH), 20, dtype=np.uint8)
    cv2.putText(
        bar, "hello", (20, 42), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 255, 2, cv2.LINE_AA
    )
    assert is_thin_decoration_strip(bar, width=WIDTH) is False


def _blank_canvas(height: int) -> np.ndarray:
    return np.full((height, WIDTH, 3), 255, dtype=np.uint8)


def _draw_circle(canvas: np.ndarray, top: int, height: int) -> None:
    cv2.circle(
        canvas,
        (WIDTH // 2, top + height // 2),
        max(20, height // 4),
        (0, 0, 0),
        -1,
    )


def _draw_horizontal_ramp(
    canvas: np.ndarray, top: int, height: int, low: int, high: int
) -> None:
    """画一段**横向**渐变块。

    必须横向渐变：``s2_segmenter`` 的内容切分靠「行内标准差 ≥ 8」判断一行
    是否画着东西（:func:`uniform_row_mask`），纯色块与竖向渐变都会被当成
    分隔带吃掉，根本不会成为 panel；横向渐变才能稳定落成独立画格。
    """
    ramp = np.tile(
        np.linspace(low, high, WIDTH, dtype=np.uint8), (height, 1)
    )
    canvas[top : top + height] = ramp[:, :, None]


def test_segment_episode_marks_blank_page_and_thin_strip_as_transition_blank(
    tmp_path,
) -> None:
    """验收 (c)：220px 纯黑过渡页与 80px 超窄暗条都要标 is_transition_blank。"""
    canvas = _blank_canvas(1540)
    _draw_circle(canvas, 0, 600)
    # 过渡页：200px 接近纯黑的横向渐变 + 上下各 10px 白边 → panel 高 220px。
    _draw_horizontal_ramp(canvas, 610, 200, 0, 30)
    # 超窄暗条：80px 横向渐变（灰阶跨度大，逃过「纯色无内容」判定，只能靠窄条规则拦）。
    _draw_horizontal_ramp(canvas, 860, 80, 0, 120)
    _draw_circle(canvas, 940, 600)

    raw_dir = tmp_path / "01_raw"
    raw_dir.mkdir()
    imwrite_unicode(raw_dir / "001.png", canvas)

    panels_dir = tmp_path / "02_panels"
    metas = segment_episode(
        raw_dir, panels_dir, episode_id="wt_x_ep_001", max_height=1200
    )
    assert metas

    manifest = json.loads(
        (panels_dir / "panels.json").read_text(encoding="utf-8")
    )
    flags = [panel["is_transition_blank"] for panel in manifest["panels"]]
    heights = [panel["height"] for panel in manifest["panels"]]

    # 正常画格 → 纯黑过渡页 → 超窄暗条 → 正常画格。
    panels = manifest["panels"]
    assert len(panels) == 4, (heights, flags)
    assert flags == [False, True, True, False], (heights, flags)
    # 纯黑过渡页：200px 内容 + 上下各 10px 安全边距 = 220px。
    assert panels[1]["height"] == 200 + 2 * PANEL_WHITE_MARGIN_PX, (heights, flags)
    # 超窄暗条：80px 内容 + 上下各 10px 边距，远小于 150px 阈值。
    assert panels[2]["height"] <= 120, (heights, flags)


# --------------------------------------------------------------------------
# 4. 章节分片与 story_model 透传
# --------------------------------------------------------------------------
def test_chapter_max_panels_is_raised_to_120() -> None:
    """Patch v3.1：单章候选分镜上限由 40 提到 120。"""
    assert s4_scriptwriter.CHAPTER_MAX_PANELS == 120


def test_slice_chapter_prose_splits_by_ratio() -> None:
    """超限拆片时每片只带自己那一段章节正文，避免整章复述。"""
    chapter = StoryChapter(
        chapter_index=1,
        chapter_title="第一段",
        panel_hint_range=("p_001", "p_240"),
        story_prose="段落一讲的是开场。\n\n段落二讲的是冲突。\n\n段落三讲的是收尾。",
    )
    head = s4_scriptwriter._slice_chapter_prose(chapter, 0.0, 0.5)
    tail = s4_scriptwriter._slice_chapter_prose(chapter, 0.5, 1.0)

    assert head.story_prose != chapter.story_prose
    assert "段落一" in head.story_prose
    assert "段落三" not in head.story_prose
    assert "段落三" in tail.story_prose
    assert "段落一" not in tail.story_prose
    # 其余字段保持不变。
    assert head.chapter_title == chapter.chapter_title
    assert head.chapter_index == chapter.chapter_index


def test_job_runner_stage_script_forwards_story_model(monkeypatch) -> None:
    """web 编排入口必须把 story_model 透传给 s4_scriptwriter.run_stage4。"""
    captured: dict = {}

    async def _fake_run_stage4(*args, **kwargs):
        captured.update(kwargs)
        return None, None

    monkeypatch.setattr(s4_scriptwriter, "build_arc_compressor", lambda *a, **k: object())
    monkeypatch.setattr(s4_scriptwriter, "run_stage4", _fake_run_stage4)

    class _Gate:
        async def run(self, episode_no: int, fn):
            await fn()

    class _FakeRunner:
        def gate(self, series_id: str) -> _Gate:
            return _Gate()

    episode = SimpleNamespace(episode_no=1, episode_id="wt_x_ep_001")
    runner = _FakeRunner()

    import asyncio

    asyncio.run(
        job_runner.JobRunner._stage_script(
            runner,  # type: ignore[arg-type]
            state=SimpleNamespace(series_id="wt_x"),
            series_dir=Path("/tmp"),
            episode=episode,
            manager=None,
            llm_client=None,
            script_model="model-script",
            story_model="model-story",
            series_title="测试作品",
        )
    )

    assert captured["story_model"] == "model-story"
    assert captured["model"] == "model-script"
