"""M6-T1b：``w2v run`` 全链路离线端到端集成测试。

不联网、不花钱：视觉与撰稿用 stub LLM 客户端，语音用 stub TTS 客户端，
只有最后的 FFmpeg 编码是真跑（没有 ffmpeg 时整体跳过）。

三个测试共用同一个 module 级同步 fixture（``_e2e_run_result``）：整条
Stage 0~7 流水线（2 话）只跑一次真 FFmpeg 渲染，各测试在同一份产物上
做不同维度的断言，避免重复渲染拖慢用例。

覆盖的关键点：

- Stage 0~7 依次落盘（``01_raw`` → ``07_out``）；
- 成片是 **1080×1920 / H.264 / AAC**；
- **多音字只污染送音文本**：字幕（ASS）里仍是「银行」，送进 TTS 的却是注音；
- 时间轴是 Audio-First：画面时长跟着音频走。
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import pytest

from cli import run_series
from core.config import AppConfig, RenderConfig, WorkspaceConfig
from providers.llm_base import ChatResponse
from providers.tts_base import TTSResult

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None, reason="本机没有 ffmpeg，跳过端到端渲染测试"
    ),
]


def _write_png(path: Path, width: int = 700, height: int = 1100) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), (250, 248, 244))
    draw = ImageDraw.Draw(image)
    for index in range(6):
        top = 80 + index * 160
        draw.rectangle([40, top, width - 40, top + 120], fill=(60 + index * 20, 90, 160))
    image.save(path)


def _wav_bytes(seconds: float = 0.9, rate: int = 24000) -> bytes:
    """造一段带轻微噪声的 wav（避免被 silenceremove 整段裁掉）。"""
    import math
    import random

    buffer = io.BytesIO()
    rng = random.Random(7)
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        total = int(rate * seconds)
        for index in range(total):
            value = int(3000 * math.sin(2 * math.pi * 220 * index / rate)) + rng.randint(-200, 200)
            frames += max(-32768, min(32767, value)).to_bytes(2, "little", signed=True)
        handle.writeframes(bytes(frames))
    return buffer.getvalue()


class StubLLM:
    """按模型名区分「识图」与「撰稿」的假客户端。"""

    def __init__(self) -> None:
        self.vision_prompts: list[str] = []
        self.script_prompts: list[str] = []

    async def chat(self, messages, *, model: str = "", temperature=None, max_tokens=None, **kwargs):
        prompt = self._prompt_text(messages)
        if "vision" in model or "vl" in model:
            self.vision_prompts.append(prompt)
            panel_id = self._panel_id(prompt)
            payload = {
                "panels": [
                    {
                        "panel_id": panel_id,
                        "scene_summary": "少年站在高处俯瞰城市，画面偏冷。",
                        "dialogues": [
                            {"speaker_guess": "主角", "text": "我们必须往上爬。", "emotion": "tense"}
                        ],
                        "sfx_list": ["风声"],
                        "focus_region": None,
                        "is_transition_blank": False,
                    }
                ]
            }
            return ChatResponse(content=json.dumps(payload, ensure_ascii=False), model=model)

        self.script_prompts.append(prompt)
        payload = {
            "episode_id": "ep_001",
            "hook_title": "登塔者",
            "beats": [
                {
                    "beat_id": "beat_001",
                    # 含多音字：送音文本会被替换成注音，字幕必须保持原样
                    "narration_text": "他把最后的存款转进了银行，然后头也不回地走进了雨里。",
                    "tts_text": None,
                    "emotion_instruction": "语速平稳，带一点不安",
                    "speaker_role": "narrator",
                    "target_panel_ids": ["panel_001"],
                    "motion": "zoom_in",
                    "pause_after_ms": 250,
                },
                {
                    "beat_id": "beat_002",
                    "narration_text": "塔门在身后缓缓合拢，只留下一线光。",
                    "tts_text": None,
                    "emotion_instruction": "低沉，略带压迫感",
                    "speaker_role": "narrator",
                    "target_panel_ids": ["panel_002"],
                    "motion": "static",
                    "pause_after_ms": 250,
                },
            ],
            "episode_digest": {
                "episode_no": 1,
                "one_line_hook": "少年踏上登塔之路",
                "summary": "主角把存款转进银行后进入高塔。",
                "keywords": ["高塔", "登塔", "银行"],
                "characters_present": ["主角"],
            },
            "character_updates": [
                {
                    "name": "主角",
                    "aliases": [],
                    "visual_traits": "黑发橙瞳",
                    "current_status": "进入高塔",
                    "first_seen_ep": 1,
                    "last_seen_ep": 1,
                }
            ],
            "new_plot_threads": [
                {
                    "thread_id": "t1",
                    "title": "高塔的秘密",
                    "description": "塔门为何只开一次",
                    "introduced_ep": 1,
                    "status": "open",
                    "resolved_ep": None,
                }
            ],
            "resolved_thread_ids": [],
        }
        return ChatResponse(content=json.dumps(payload, ensure_ascii=False), model=model)

    @staticmethod
    def _prompt_text(messages) -> str:
        parts = []
        for message in messages:
            content = message.content
            if isinstance(content, str):
                parts.append(content)
            else:
                parts.extend(part.text for part in content if part.type == "text")
        return "\n".join(parts)

    @staticmethod
    def _panel_id(prompt: str) -> str:
        match = re.search(r"面板编号[：:]\s*(panel_\d+)", prompt)
        if match:
            return match.group(1)
        match = re.search(r"(panel_\d+)", prompt)
        return match.group(1) if match else "panel_001"


class StubTTS:
    """假 TTS 客户端：记录送进来的文本，吐出一段真 wav。"""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.instructions: list[str] = []

    async def synthesize(
        self, text: str, *, instruction: str = "", voice=None, language_type: str = "Chinese"
    ) -> TTSResult:
        self.texts.append(text)
        self.instructions.append(instruction)
        return TTSResult(
            audio=_wav_bytes(),
            content_type="audio/x-wav",
            sample_rate=24000,
            voice=voice or "stub",
        )

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True)
class _E2ERunResult:
    """一次共享流水线运行的产物句柄。"""

    result: dict
    workspace: Path
    out_dir: Path
    tts: StubTTS


def _make_source(root: Path, episodes: int = 2) -> Path:
    for episode_no in range(1, episodes + 1):
        folder = root / f"ep_{episode_no:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        for index in range(2):
            _write_png(folder / f"{index + 1:03d}.png")
    return root


def _probe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def _e2e_run_result(tmp_path_factory: pytest.TempPathFactory) -> _E2ERunResult:
    """整条 2 话流水线只跑一次。

    刻意写成同步 fixture + ``asyncio.run``：module 级 ``async def`` fixture
    会与 anyio 默认的函数级 event loop 撞 ``ScopeMismatch``。
    """
    base = tmp_path_factory.mktemp("pipeline_e2e")
    source = _make_source(base / "src")
    workspace = base / "ws"
    out_dir = base / "out"
    llm = StubLLM()
    tts = StubTTS()

    config = AppConfig(
        workspace=WorkspaceConfig(root=workspace),
        pronunciation_overrides={"银行": "yín háng"},
        # 真渲染统一用 ultrafast 预设（断言强度不变，只是不在测试里浪费时间）。
        render=RenderConfig(video_preset="ultrafast"),
    )

    result = asyncio.run(
        run_series(
            str(source),
            config=config,
            episodes_spec="1-2",
            concurrency=2,
            client_factory=lambda: llm,
            tts_factory=lambda: tts,
            out_dir=str(out_dir),
        )
    )
    return _E2ERunResult(result=result, workspace=workspace, out_dir=out_dir, tts=tts)


def test_w2v_run_produces_nine_by_sixteen_video(_e2e_run_result: _E2ERunResult) -> None:
    result = _e2e_run_result.result
    workspace = _e2e_run_result.workspace
    out_dir = _e2e_run_result.out_dir

    assert result["command"] == "run"
    assert result["series_id"].startswith("wt_local_")
    assert result["episodes"] == [1, 2]
    assert len(result["videos"]) == 2

    series_dir = workspace / result["series_id"]
    # Stage 0：注册表与作品目录
    assert (workspace / "index.json").is_file()
    assert (series_dir / "catalog.json").is_file()

    for episode_no, video in zip(result["episodes"], result["videos"], strict=True):
        episode_dir = series_dir / "episodes" / f"ep_{episode_no:03d}"
        # Stage 1~4
        assert list((episode_dir / "01_raw").glob("*.png"))
        assert list((episode_dir / "02_panels").glob("*.png"))
        assert (episode_dir / "03_vision" / "vision.json").is_file()
        assert (episode_dir / "04_script" / "script.json").is_file()
        # Stage 5
        assert (episode_dir / "05_audio" / "audio_manifest.json").is_file()
        # Stage 6
        assert (episode_dir / "06_plan" / "timeline.json").is_file()
        assert (episode_dir / "06_plan" / "subtitles.ass").is_file()
        # Stage 7
        out_path = Path(video["path"])
        assert out_path.is_file() and out_path.stat().st_size > 0
        assert out_path.parent.name == "07_out"

        streams = _probe(out_path)["streams"]
        video_stream = next(s for s in streams if s["codec_type"] == "video")
        audio_stream = next(s for s in streams if s["codec_type"] == "audio")
        assert (video_stream["width"], video_stream["height"]) == (1080, 1920)
        assert video_stream["codec_name"] == "h264"
        assert audio_stream["codec_name"] == "aac"

    # 成片同步复制到导出目录
    assert len(list(out_dir.glob("*.mp4"))) == 2


def test_pronunciation_override_never_pollutes_subtitles(
    _e2e_run_result: _E2ERunResult,
) -> None:
    """铁律：多音字替换只进送音文本，字幕保持原字。"""
    result = _e2e_run_result.result
    workspace = _e2e_run_result.workspace
    tts = _e2e_run_result.tts

    # 送进 TTS 的文本带注音
    assert any("yín háng" in text for text in tts.texts)
    # 字幕文件里仍是「银行」，且不含拼音
    series_dir = workspace / result["series_id"]
    ass_path = series_dir / "episodes" / "ep_001" / "06_plan" / "subtitles.ass"
    ass_text = ass_path.read_text(encoding="utf-8")
    assert "银行" in ass_text
    assert "yín háng" not in ass_text


def test_timeline_is_audio_first(_e2e_run_result: _E2ERunResult) -> None:
    """画面时长跟着音频走，而不是拍脑袋估。"""
    result = _e2e_run_result.result
    workspace = _e2e_run_result.workspace
    series_dir = workspace / result["series_id"]
    episode_dir = series_dir / "episodes" / "ep_001"

    manifest = json.loads((episode_dir / "05_audio" / "audio_manifest.json").read_text("utf-8"))
    timeline = json.loads((episode_dir / "06_plan" / "timeline.json").read_text("utf-8"))

    audio_total = sum(item["duration_ms"] for item in manifest)
    assert timeline["total_duration_ms"] >= audio_total
    # 每段字幕的时长不超过对应音频时长（画面不许比声音长）
    for subtitle in timeline["subtitles"]:
        matching = [item for item in manifest if item["beat_id"] == subtitle["beat_id"]]
        assert matching, f"字幕 {subtitle['beat_id']} 没有对应的音频"
        assert subtitle["end_ms"] - subtitle["start_ms"] <= matching[0]["duration_ms"] + 1
