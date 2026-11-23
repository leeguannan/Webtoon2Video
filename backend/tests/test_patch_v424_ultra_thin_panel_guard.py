"""Patch v4.23 离线回归测试：出版方水印条（超扁窄条）不进成片。

实测 wt_5081 第 5 话：``p_088`` 是 690×61 的出版方水印白条（只有
"IAFDAM MEDIA" 角标）。Stage 2 的装饰条判据因为水印 logo 顶高边缘密度
（0.0353 ≥ 0.015）、而近乎全白又触发「含文字」豁免，所以漏判；Stage 3 的
多图批窗又把它「串味」成了相邻格 p_087 的翻版（摘要与对白完全相同），
于是它一路混进 b80 的分镜列表，在 b80 与 b81 之间渲染成整整一帧灰底画面。

覆盖两组断言：

1. Stage 3：超扁窄条**不烧 VLM**，直接按空镜处理（``is_transition_blank``），
   也不会被当成邻格参考图串进相邻格；
2. Stage 6：旧脚本里残留的超扁窄条引用，Stage 6 也要挡住（几何兜底），
   那一拍回退到上一张画面，不会多出那一帧。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from core.schemas import (
    EpisodeDigest,
    NarrationScript,
    PanelVisionInfo,
    ScriptBeat,
    VisionWindowOutput,
    save_model,
)
from providers.llm_base import ChatMessage, ChatResponse
from stages import s3_vision, s6_timeline

#: 正常漫画分镜尺寸（竖幅）。
_NORMAL_SIZE = (690, 827)
#: 出版方水印条尺寸（超扁窄条）。
_STRIP_SIZE = (690, 61)
#: 矮横幅但仍算正常画面的尺寸（宽高比 3.77 < 7.5，不该被误杀）。
_BANNER_SIZE = (690, 183)


class _FakeLLM:
    """记录每次调用的假客户端（离线，不联网）。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[list[ChatMessage]] = []

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        self.calls.append(messages)
        return ChatResponse(content=self.content, model=model)


def _text_of(message: ChatMessage) -> str:
    return "\n".join(part.text for part in message.content if part.type == "text")


def _panel_payload(panel_id: str) -> dict[str, Any]:
    return {
        "panel_id": panel_id,
        "scene_summary": f"{panel_id}：雨夜街头，少年握紧拳头抬头望向对面的人。",
        "dialogues": [{"speaker_guess": "小白", "text": "你终于来了", "emotion": "tense"}],
        "sfx_list": [],
        "is_transition_blank": False,
    }


def _write_manifest(panels_dir: Path, sizes: dict[str, tuple[int, int]]) -> None:
    """写出 Stage 2 的 ``panels.json``（几何信息是本次护栏的唯一依据）。"""
    panels = []
    for panel_id, (width, height) in sizes.items():
        panels.append(
            {
                "panel_id": panel_id,
                "image_path": str(panels_dir / f"{panel_id}.png"),
                "width": width,
                "height": height,
                "aspect_ratio": round(width / height, 2),
                "source_slice_range": [0, 1],
                "is_tall_panel": False,
                "is_transition_blank": False,
                "edge_density": 0.0353,
            }
        )
    (panels_dir / "panels.json").write_text(
        json.dumps({"panels": panels}, ensure_ascii=False), encoding="utf-8"
    )


def _make_episode(
    tmp_path: Path,
    sizes: dict[str, tuple[int, int]],
) -> tuple[Path, Path]:
    """造一个话次目录（面板图 + panels.json），返回 ``(series_dir, panels_dir)``。"""
    series_dir = tmp_path / "series"
    panels_dir = series_dir / "episodes" / "ep_001" / "02_panels"
    panels_dir.mkdir(parents=True)
    for panel_id, (width, height) in sizes.items():
        image = Image.new("RGB", (width, height), (245, 245, 245))
        image.save(panels_dir / f"{panel_id}.png")
    _write_manifest(panels_dir, sizes)
    return series_dir, panels_dir


# ---------------------------------------------------------------------------
# 断言 1：Stage 3 几何护栏
# ---------------------------------------------------------------------------
def test_ultra_thin_panel_ids_only_picks_flat_strips(tmp_path: Path) -> None:
    """只有「超扁窄条」入选；正常画格与矮横幅都不动。"""
    _, panels_dir = _make_episode(
        tmp_path,
        {
            "p_001": _NORMAL_SIZE,
            "p_002": _STRIP_SIZE,
            "p_003": _BANNER_SIZE,
        },
    )

    assert s3_vision.ultra_thin_panel_ids(panels_dir) == {"p_002"}
    assert s3_vision.is_ultra_thin_panel_geometry(*_STRIP_SIZE) is True
    assert s3_vision.is_ultra_thin_panel_geometry(*_NORMAL_SIZE) is False
    assert s3_vision.is_ultra_thin_panel_geometry(*_BANNER_SIZE) is False
    # 尺寸读不到时不能误杀。
    assert s3_vision.is_ultra_thin_panel_geometry(0, 0) is False


@pytest.mark.anyio
async def test_stage3_skips_ultra_thin_panel_and_breaks_neighbor_chain(
    tmp_path: Path,
) -> None:
    """超扁窄条不烧 VLM，也不当邻格参考图串进相邻格。"""
    _, panels_dir = _make_episode(
        tmp_path,
        {
            "p_001": _NORMAL_SIZE,
            "p_002": _STRIP_SIZE,
            "p_003": _NORMAL_SIZE,
        },
    )
    client = _FakeLLM(json.dumps({"panels": [_panel_payload("p_001")]}, ensure_ascii=False))

    output = await s3_vision.extract_episode_vision(
        client,
        panels_dir=panels_dir,
        model="qwen3-vl-plus",
        blank_ids=set(),
    )

    # 只有两格正常画面各调一次模型；水印条一次都没调。
    assert len(client.calls) == 2
    for messages in client.calls:
        prompt = _text_of(messages[0])
        assert "p_002" not in prompt, "水印条不能出现在任何提示词里"

    by_id = {panel.panel_id: panel for panel in output.panels}
    assert set(by_id) == {"p_001", "p_002", "p_003"}
    assert by_id["p_002"].is_transition_blank is True
    assert by_id["p_002"].non_story_type == "transition_blank"
    assert by_id["p_001"].is_transition_blank is False


# ---------------------------------------------------------------------------
# 断言 2：Stage 6 兜底
# ---------------------------------------------------------------------------
def test_stage6_drops_ultra_thin_panel_from_timeline(tmp_path: Path) -> None:
    """旧脚本残留的水印条引用，Stage 6 按几何挡住（哪怕模型把它描述成正常画面）。"""
    series_dir, panels_dir = _make_episode(
        tmp_path,
        {
            "p_001": _NORMAL_SIZE,
            "p_002": _STRIP_SIZE,
        },
    )
    episode_dir = series_dir / "episodes" / "ep_001"
    # 模拟 v4.22 多图批窗的「串味」：模型把水印条描述成了正常画面。
    save_model(
        VisionWindowOutput(
            panels=[
                PanelVisionInfo(
                    panel_id="p_001",
                    scene_summary="雨夜街头，少年握紧拳头抬头。",
                ),
                PanelVisionInfo(
                    panel_id="p_002",
                    scene_summary="雨夜街头，黑发男性撑伞低头凝视浅灰长发女性。",
                    dialogues=[],
                ),
            ]
        ),
        episode_dir / "03_vision" / "vision.json",
    )
    save_model(
        NarrationScript(
            episode_id="ep_001",
            hook_title="钩子标题",
            beats=[
                ScriptBeat(
                    beat_id="b80",
                    narration_text="恩彗扶着他的衣襟抬起头望向道振，心想：",
                    target_panel_ids=["p_001", "p_002"],
                ),
                ScriptBeat(
                    beat_id="b81",
                    narration_text="我们最终还是……",
                    target_panel_ids=["p_001"],
                ),
            ],
            episode_digest=EpisodeDigest(
                episode_no=1, one_line_hook="钩子标题", summary="这一话的主线摘要"
            ),
        ),
        episode_dir / "04_script" / "script.json",
    )
    audio_dir = episode_dir / "05_audio"
    audio_dir.mkdir(parents=True)
    (audio_dir / "audio_manifest.json").write_text(
        json.dumps(
            [
                {
                    "beat_id": "b80",
                    "audio_path": "05_audio/b80.wav",
                    "duration_ms": 1500,
                    "pause_after_ms": 150,
                },
                {
                    "beat_id": "b81",
                    "audio_path": "05_audio/b81.wav",
                    "duration_ms": 1000,
                    "pause_after_ms": 150,
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    timeline = s6_timeline.build_timeline_from_workspace(series_dir, 1)

    used_panels = [scene.panel_id for scene in timeline.scenes]
    assert "p_002" not in used_panels
    # b80 不再被拆成两帧（拆帧正是「b80 与 b81 之间多一帧」的成因）。
    assert len([scene for scene in timeline.scenes if scene.beat_id == "b80"]) == 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
