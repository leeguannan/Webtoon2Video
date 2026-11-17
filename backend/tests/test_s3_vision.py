"""Stage 3（``stages/s3_vision.py``）离线测试。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from core.schemas import CharacterProfile, VisionWindowOutput, load_model
from providers.llm_bailian import BailianClient
from stages import s3_vision

CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


async def _no_sleep(_seconds: float) -> None:
    return None


def _panel_payload(
    panel_id: str = "panel_001",
    scene_summary: str = "雨夜街头，少年握紧拳头抬头",
) -> str:
    return json.dumps(
        {
            "panels": [
                {
                    "panel_id": panel_id,
                    "scene_summary": scene_summary,
                    "dialogues": [
                        {"speaker_guess": "小白", "text": "你终于来了", "emotion": "tense"}
                    ],
                    "sfx_list": ["哗啦啦"],
                    "focus_region": [0.1, 0.2, 0.8, 0.9],
                    "is_transition_blank": False,
                }
            ]
        },
        ensure_ascii=False,
    )


def _vision_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "qwen3-vl-plus",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        },
    )


def _make_series(tmp_path: Path, sizes: tuple[tuple[int, int], ...] = ((800, 1200),)) -> Path:
    series_dir = tmp_path / "workspace" / "wt_test"
    panels_dir = series_dir / "episodes" / "ep_001" / "02_panels"
    panels_dir.mkdir(parents=True)
    for index, size in enumerate(sizes, start=1):
        Image.new("RGB", size, (12, 34, 56)).save(panels_dir / f"panel_{index:03d}.png")
    return series_dir


def _characters() -> list[CharacterProfile]:
    return [
        CharacterProfile(
            name="小白",
            aliases=["阿白"],
            visual_traits="白发红瞳，左颊有疤",
            current_status="浑身湿透",
            first_seen_ep=1,
            last_seen_ep=1,
        )
    ]


def _image_part_size(payload_text: str) -> tuple[int, int]:
    """从请求体里取出 image_url 的 base64 并解码出尺寸。"""
    body = json.loads(payload_text)
    for message in body["messages"]:
        for part in message["content"]:
            if isinstance(part, dict) and part.get("type") == "image_url":
                data_url = part["image_url"]["url"]
                _, _, encoded = data_url.partition(",")
                raw = base64.b64decode(encoded)
                import io

                with Image.open(io.BytesIO(raw)) as image:
                    return image.size
    raise AssertionError("请求体中没有 image_url 分片")


@pytest.mark.anyio
@respx.mock
async def test_run_stage3_writes_vision_json(tmp_path) -> None:
    series_dir = _make_series(tmp_path)
    route = respx.post(CHAT_URL).mock(return_value=_vision_response(_panel_payload()))
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        path = await s3_vision.run_stage3(
            series_dir,
            1,
            client,
            model="qwen3-vl-plus",
            characters=_characters(),
            series_title="测试漫画",
        )
    assert path == series_dir / "episodes" / "ep_001" / "03_vision" / "vision.json"
    output = load_model(VisionWindowOutput, path)
    assert len(output.panels) == 1
    panel = output.panels[0]
    assert panel.panel_id == "panel_001"
    assert "雨夜" in panel.scene_summary
    assert panel.dialogues[0].speaker_guess == "小白"
    assert panel.dialogues[0].emotion == "tense"
    assert panel.sfx_list == ["哗啦啦"]
    assert panel.focus_region == (0.1, 0.2, 0.8, 0.9)
    assert route.call_count == 1


@pytest.mark.anyio
@respx.mock
async def test_stage3_injects_layer1_character_list(tmp_path) -> None:
    series_dir = _make_series(tmp_path)
    route = respx.post(CHAT_URL).mock(return_value=_vision_response(_panel_payload()))
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        await s3_vision.run_stage3(
            series_dir,
            1,
            client,
            model="qwen3-vl-plus",
            characters=_characters(),
            series_title="测试漫画",
        )
    payload = route.calls[0].request.content.decode("utf-8")
    assert "小白" in payload  # 名字
    assert "阿白" in payload  # 别名
    assert "白发红瞳" in payload  # 外貌特征
    assert "测试漫画" in payload


@pytest.mark.anyio
@respx.mock
async def test_stage3_sends_image_as_data_url_with_long_edge_1280(tmp_path) -> None:
    # 普通画格（高度未超过切片阈值）仍按规范缩到长边 ≤1280px
    series_dir = _make_series(tmp_path, sizes=((1000, 1500),))
    route = respx.post(CHAT_URL).mock(return_value=_vision_response(_panel_payload()))
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        await s3_vision.run_stage3(
            series_dir, 1, client, model="qwen3-vl-plus", characters=_characters()
        )
    payload = route.calls[0].request.content.decode("utf-8")
    assert "data:image/jpeg;base64," in payload
    width, height = _image_part_size(payload)
    assert max(width, height) == 1280


@pytest.mark.anyio
@respx.mock
async def test_tall_panel_is_sliced_vertically_and_merged(tmp_path) -> None:
    """超高画格：纵向重叠切片逐片喂模型，结果合并回同一个 panel_id。"""
    series_dir = _make_series(tmp_path, sizes=((600, 3000),))
    summaries = iter(
        [
            "上段：雨夜街头，少年握紧拳头",
            "中段：少年奔过长街，霓虹在积水里晃",
            "下段：少年冲进雨里，身后亮起车灯",
        ]
    )
    route = respx.post(CHAT_URL).mock(
        side_effect=lambda request: _vision_response(_panel_payload("p_001", next(summaries)))
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        path = await s3_vision.run_stage3(
            series_dir, 1, client, model="qwen3-vl-plus", characters=_characters()
        )
    # 3000px 高的画格按 1600px 片高 + 250px 重叠至少需要 3 片
    assert route.call_count == 3
    output = load_model(VisionWindowOutput, path)
    assert len(output.panels) == 1
    panel = output.panels[0]
    # 三段摘要合并
    assert "上段" in panel.scene_summary
    assert "中段" in panel.scene_summary
    assert "下段" in panel.scene_summary
    # 对白去重（各片返回同一条气泡）
    assert len(panel.dialogues) == 1
    # 切片宽度固定 ≤1024、片高 ≤1600
    for call in route.calls:
        width, height = _image_part_size(call.request.content.decode("utf-8"))
        assert width <= 1024
        assert height <= 1600


@pytest.mark.anyio
@respx.mock
async def test_tall_panel_keeps_original_file_untouched(tmp_path) -> None:
    """切片只在内存里做，02_panels/ 的原图文件不改动。"""
    series_dir = _make_series(tmp_path, sizes=((600, 3000),))
    panel_path = series_dir / "episodes" / "ep_001" / "02_panels" / "panel_001.png"
    before = panel_path.stat().st_mtime_ns
    route = respx.post(CHAT_URL).mock(return_value=_vision_response(_panel_payload()))
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        await s3_vision.run_stage3(
            series_dir, 1, client, model="qwen3-vl-plus", characters=_characters()
        )
    assert panel_path.stat().st_mtime_ns == before
    with Image.open(panel_path) as image:
        assert image.size == (600, 3000)
    assert route.call_count == 3


@pytest.mark.anyio
@respx.mock
async def test_stage3_analyses_every_panel(tmp_path) -> None:
    series_dir = _make_series(tmp_path, sizes=((600, 900), (600, 900)))
    route = respx.post(CHAT_URL).mock(
        side_effect=[
            _vision_response(_panel_payload("panel_001")),
            _vision_response(_panel_payload("panel_002")),
        ]
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        path = await s3_vision.run_stage3(
            series_dir, 1, client, model="qwen3-vl-plus", characters=_characters()
        )
    output = load_model(VisionWindowOutput, path)
    assert [panel.panel_id for panel in output.panels] == ["panel_001", "panel_002"]
    assert route.call_count == 2


@pytest.mark.anyio
async def test_stage3_raises_when_panels_dir_missing(tmp_path) -> None:
    series_dir = tmp_path / "workspace" / "wt_empty"
    series_dir.mkdir(parents=True)
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        with pytest.raises(s3_vision.VisionError):
            await s3_vision.run_stage3(
                series_dir, 1, client, model="qwen3-vl-plus", characters=_characters()
            )


@pytest.mark.anyio
@respx.mock
async def test_stage3_rejects_non_json_model_output(tmp_path) -> None:
    series_dir = _make_series(tmp_path)
    respx.post(CHAT_URL).mock(return_value=_vision_response("完全不是 JSON"))
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        with pytest.raises(Exception):
            await s3_vision.run_stage3(
                series_dir, 1, client, model="qwen3-vl-plus", characters=_characters()
            )


def test_prompt_template_contains_placeholders() -> None:
    template = s3_vision.load_prompt_template()
    for token in ("{{CHARACTERS}}", "{{PANEL_ID}}", "{{SERIES_TITLE}}", "{{EPISODE_NO}}"):
        assert token in template


@pytest.mark.anyio
@respx.mock
async def test_stage3_normalizes_out_of_range_emotion(tmp_path) -> None:
    """真实调用出现过 emotion="pleading"（不在契约 7 值内），必须收敛而不是崩掉。"""
    series_dir = _make_series(tmp_path)
    payload = json.dumps(
        {
            "panels": [
                {
                    "panel_id": "panel_001",
                    "scene_summary": "少年回头",
                    "dialogues": [
                        {"speaker_guess": "小白", "text": "别走", "emotion": "pleading"}
                    ],
                    "sfx_list": [],
                }
            ]
        },
        ensure_ascii=False,
    )
    respx.post(CHAT_URL).mock(return_value=_vision_response(payload))
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        path = await s3_vision.run_stage3(
            series_dir, 1, client, model="qwen3-vl-plus", characters=_characters()
        )
    output = load_model(VisionWindowOutput, path)
    assert output.panels[0].dialogues[0].emotion == "calm"


@pytest.mark.anyio
@respx.mock
async def test_stage3_skips_broken_panel_and_records_failures(tmp_path) -> None:
    """单个面板抽取失败不该让整话崩掉，失败明细单独落盘（不做静默丢弃）。"""
    series_dir = _make_series(tmp_path, sizes=((600, 900), (600, 900)))
    respx.post(CHAT_URL).mock(
        side_effect=[
            _vision_response("完全不是 JSON"),
            _vision_response(_panel_payload("panel_002")),
        ]
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        path = await s3_vision.run_stage3(
            series_dir,
            1,
            client,
            model="qwen3-vl-plus",
            characters=_characters(),
            on_panel_error="skip",
        )
    output = load_model(VisionWindowOutput, path)
    assert [panel.panel_id for panel in output.panels] == ["panel_002"]
    failures_path = path.parent / "vision_failures.json"
    assert failures_path.is_file()
    failures = json.loads(failures_path.read_text(encoding="utf-8"))
    assert failures and failures[0]["panel_id"] == "panel_001"
    assert failures[0]["error"]


@pytest.mark.anyio
@respx.mock
async def test_stage3_keeps_panel_id_from_filename(tmp_path) -> None:
    """Patch v2.6-6：Stage 3 原样透传 Stage 2 的 panel_id，不再私自改名。"""
    series_dir = tmp_path / "workspace" / "wt_test"
    panels_dir = series_dir / "episodes" / "ep_001" / "02_panels"
    panels_dir.mkdir(parents=True)
    Image.new("RGB", (600, 900), (12, 34, 56)).save(panels_dir / "p_001.png")
    route = respx.post(CHAT_URL).mock(
        return_value=_vision_response(_panel_payload("p_001"))
    )
    async with BailianClient(api_key="sk-test", sleep=_no_sleep) as client:
        path = await s3_vision.run_stage3(
            series_dir, 1, client, model="qwen3-vl-plus", characters=_characters()
        )
    output = load_model(VisionWindowOutput, path)
    assert [panel.panel_id for panel in output.panels] == ["p_001"]
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# Patch v3.0 小修：超高画格切片合并的 sfx_only 整格判定
# ---------------------------------------------------------------------------


def _tile(index: int, count: int, top: int, bottom: int, total: int = 1000) -> s3_vision.PanelTile:
    return s3_vision.PanelTile(
        image=Image.new("RGB", (320, bottom - top), (20, 20, 20)),
        top=top,
        bottom=bottom,
        total_height=total,
        index=index,
        count=count,
    )


def _tile_payload(
    *,
    non_story_type: str = "none",
    blank: bool = False,
    dialogues: list[dict] | None = None,
    sfx: list[str] | None = None,
    summary: str = "画面内容",
) -> dict:
    return {
        "panel_id": "p_001",
        "scene_summary": summary,
        "dialogues": dialogues or [],
        "sfx_list": sfx if sfx is not None else ["咚"],
        "focus_region": [0.0, 0.0, 1.0, 1.0],
        "is_transition_blank": blank,
        "non_story_type": non_story_type,
    }


def test_merge_tiles_sfx_only_tail_does_not_drop_dialogue_panel() -> None:
    """尾部一个 sfx_only 切片不能把前面带对白的超长画格整格误杀。"""
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
    assert merged["dialogues"] and merged["dialogues"][0]["text"] == "你终于来了"


def test_merge_tiles_all_sfx_and_no_dialogue_marks_sfx_only() -> None:
    """所有有效切片都只剩拟声词且无对白，整格才标 sfx_only。"""
    tiles = [
        (_tile(0, 2, 0, 500), _tile_payload(non_story_type="sfx_only")),
        (_tile(1, 2, 500, 1000), _tile_payload(non_story_type="sfx_only", sfx=["锵"])),
    ]
    merged = s3_vision._merge_tile_payloads("p_001", tiles)
    assert merged["non_story_type"] == "sfx_only"
    assert merged["dialogues"] == []


def test_merge_tiles_blank_sfx_slice_excluded_from_all_check() -> None:
    """空白切片不参与「全部 sfx_only」判定：一个空白 + 两个有效 sfx_only 仍是 sfx_only。"""
    tiles = [
        (_tile(0, 3, 0, 333), _tile_payload(non_story_type="sfx_only", blank=True)),
        (_tile(1, 3, 333, 666), _tile_payload(non_story_type="sfx_only")),
        (_tile(2, 3, 666, 1000), _tile_payload(non_story_type="sfx_only", sfx=["锵"])),
    ]
    merged = s3_vision._merge_tile_payloads("p_001", tiles)
    assert merged["non_story_type"] == "sfx_only"


def test_merge_tiles_sfx_label_with_dialogue_is_not_sfx_only() -> None:
    """切片虽自报 sfx_only 但抽出了对白：合并后 dialogues 非空，整格按剧情格处理。"""
    tiles = [
        (
            _tile(0, 1, 0, 1000),
            _tile_payload(
                non_story_type="sfx_only",
                dialogues=[{"speaker_guess": "黑衣人", "text": "别追了"}],
            ),
        ),
    ]
    merged = s3_vision._merge_tile_payloads("p_001", tiles)
    assert merged["non_story_type"] == "none"
    assert merged["dialogues"]


def test_merge_tiles_copyright_banner_still_wins() -> None:
    """版权/推广页的强判定不受本次收紧影响：任一切片命中即整格命中。"""
    tiles = [
        (_tile(0, 2, 0, 500), _tile_payload(non_story_type="copyright_notice")),
        (_tile(1, 2, 500, 1000), _tile_payload(non_story_type="sfx_only")),
    ]
    merged = s3_vision._merge_tile_payloads("p_001", tiles)
    assert merged["non_story_type"] == "copyright_notice"
