"""Patch v4.16：角色名全链路自动统一 + Stage 5 音频增量检测与缓存复用。

两条主线：

1. **角色名统一**——同音异写（恩慧 / 恩惠 → 恩彗）在角色位与正文里统一；
   外貌称呼与「小恩小惠」「受人恩惠」这类惯用词不受影响；Stage 4A/4B 产物
   （故事稿、剧本、记忆体）与人工保存都走同一套归一。
2. **Stage 5 缓存**——同一段送音文本 + 同一套音色/语速只合成一次；改一句只重配
   一句；只改停顿不重新合成；失败的静音占位绝不进缓存；Stage 5 重跑保留缓存，
   手动「重置」彻底清空缓存。

全部离线：假合成器（不触网）、假 LLM（respx），音频裁剪走 ``trim_silence=False``
（不依赖本机 ffmpeg），时长探测走 ``wave`` 直读。
"""

from __future__ import annotations

import io
import json
import math
import struct
import wave
from pathlib import Path
from typing import Any, Sequence

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image

from core.character_unify import (
    are_homophone_name_variants,
    build_character_canonicalizer,
)
from core.config import AppConfig, WorkspaceConfig
from core.manifest import ManifestManager
from core.memory import StoryBibleManager
from core.schemas import (
    CharacterProfile,
    EpisodeDigest,
    NarrationScript,
    ScriptBeat,
    StoryBibleV2,
    VisionWindowOutput,
    load_model,
    save_model,
)
from providers.llm_bailian import BailianClient
from providers.tts_base import TTSResult
from stages import s0_catalog, s5_tts
from web.job_runner import JobRunner
from web.server import create_app

SERIES_ID = "wt_v416"
SAMPLE_RATE = 24000
CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------
def _tone(seconds: float, freq: float = 440.0, amp: float = 0.3) -> list[int]:
    count = int(SAMPLE_RATE * seconds)
    return [
        int(amp * 32767 * math.sin(2 * math.pi * freq * index / SAMPLE_RATE + math.pi / 2))
        for index in range(count)
    ]


def _wav_bytes(samples: Sequence[int], rate: int = SAMPLE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))
    return buffer.getvalue()


class FakeTTS:
    """假合成器：记录调用参数，返回真实 WAV 字节（绝不联网）。"""

    def __init__(self, *, samples: Sequence[int] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.fail_texts: set[str] = set()
        self._samples = list(samples) if samples is not None else _tone(0.3)

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str | None = None,
        language_type: str = "Chinese",
        **kwargs: Any,
    ) -> TTSResult:
        self.calls.append(
            {
                "text": text,
                "instruction": instruction,
                "voice": voice,
                "language_type": language_type,
                **kwargs,
            }
        )
        if text in self.fail_texts:
            raise RuntimeError("假合成器故意失败")
        return TTSResult(
            audio=_wav_bytes(self._samples), content_type="audio/wav", voice=voice or "Fake"
        )

    async def aclose(self) -> None:
        self.closed = True


def _beat(beat_id: str, narration_text: str, **kwargs: Any) -> ScriptBeat:
    payload: dict[str, Any] = {
        "beat_id": beat_id,
        "narration_text": narration_text,
        "target_panel_ids": ["panel_001"],
    }
    payload.update(kwargs)
    return ScriptBeat(**payload)


def _script(beats: Sequence[ScriptBeat], *, series_id: str = SERIES_ID) -> NarrationScript:
    return NarrationScript(
        episode_id=f"{series_id}_ep_001",
        hook_title="雨夜来客",
        beats=list(beats),
        episode_digest=EpisodeDigest(
            episode_no=1,
            one_line_hook="雨夜有人来访",
            summary="来客自称银行行长。",
        ),
    )


def _register(root: Path) -> tuple[str, Path, AppConfig]:
    """用 s0_catalog 登记作品，返回 (series_id, series_dir, config)。"""
    src = root / "src"
    ep_src = src / "ep_001"
    ep_src.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 600), (40, 80, 120)).save(ep_src / "001.png")
    catalog = s0_catalog.import_local_catalog(src)
    s0_catalog.CatalogRegistry(root).register(catalog)
    config = AppConfig(workspace=WorkspaceConfig(root=root))
    return catalog.series_id, root / catalog.series_id, config


def _write_script(series_dir: Path, beats: Sequence[ScriptBeat]) -> Path:
    script_dir = series_dir / "episodes" / "ep_001" / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    save_model(_script(beats), script_dir / "script.json")
    return script_dir / "script.json"


async def _run_stage5(
    series_dir: Path,
    beats: Sequence[ScriptBeat],
    client: FakeTTS,
    **kwargs: Any,
) -> dict[str, Any]:
    """写剧本并跑 Stage 5，返回统计信息。"""
    _write_script(series_dir, beats)
    stats: dict[str, Any] = {}
    await s5_tts.run_stage5(
        series_dir,
        1,
        client,
        trim_silence=False,
        stats_out=stats,
        **kwargs,
    )
    return stats


def _audio_dir(series_dir: Path) -> Path:
    return series_dir / "episodes" / "ep_001" / "05_audio"


def _cache_dir(series_dir: Path) -> Path:
    return _audio_dir(series_dir) / "_cache"


def _manifest(series_dir: Path) -> list[dict[str, Any]]:
    path = _audio_dir(series_dir) / "audio_manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _three_beats() -> list[ScriptBeat]:
    return [
        _beat("b1", "银行行长推门而入。"),
        _beat("b2", "他摘下手套，目光扫过全场。"),
        _beat("b3", "雨声敲在玻璃窗上。"),
    ]


# ---------------------------------------------------------------------------
# A. 角色名统一（单元级）
# ---------------------------------------------------------------------------
def _character(name: str, *, aliases: Sequence[str] = ()) -> CharacterProfile:
    """构造一个角色档案（``CharacterProfile`` 的必填字段都补齐）。"""
    return CharacterProfile(
        name=name,
        aliases=list(aliases),
        visual_traits="",
        current_status="",
        first_seen_ep=1,
        last_seen_ep=1,
    )


def test_homophone_variants_unify_roles_and_text() -> None:
    """「恩慧」是「恩彗」的同音异写：角色位与正文都要归一。"""
    canonicalizer = build_character_canonicalizer(
        characters=[_character("恩彗", aliases=["恩慧"])],
        series_tts=None,
        series_aliases=None,
    )
    assert canonicalizer.canonicalize_role("恩慧") == "恩彗"
    assert canonicalizer.canonicalize_role("恩彗") == "恩彗"
    # 旁白与空串原样保留
    assert canonicalizer.canonicalize_role("narrator") == "narrator"
    assert canonicalizer.canonicalize_role("旁白") == "旁白"
    assert canonicalizer.canonicalize_role("") == ""
    # 正文
    assert canonicalizer.canonicalize_text("恩慧推门而入") == "恩彗推门而入"
    # 提示词锁定块要列出规范名与「不要写成」的别名
    lock = canonicalizer.prompt_lock_block()
    assert "恩彗" in lock
    assert "恩慧" in lock


def test_common_phrases_and_descriptive_aliases_are_untouched() -> None:
    """惯用词与外貌称呼不能被角色名替换误伤。"""
    canonicalizer = build_character_canonicalizer(
        characters=[_character("恩彗", aliases=["恩惠"])],
        series_tts=None,
        series_aliases=None,
    )
    assert canonicalizer.canonicalize_text("他受了别人的恩惠。") == "他受了别人的恩彗。"
    assert canonicalizer.canonicalize_text("这不过是小恩小惠罢了。") == "这不过是小恩小惠罢了。"
    assert canonicalizer.canonicalize_text("施以恩惠于我。") == "施以恩惠于我。"
    # 真正的角色名仍然归一
    assert canonicalizer.canonicalize_text("恩惠推门而入") == "恩彗推门而入"

    descriptive = build_character_canonicalizer(
        characters=[_character("Bam", aliases=["黑发橙眼少年"])],
        series_tts=None,
        series_aliases=None,
    )
    # 角色位认得这个称呼
    assert descriptive.canonicalize_role("黑发橙眼少年") == "Bam"
    # 但正文里不把外貌称呼替换成正式名
    assert descriptive.canonicalize_text("黑发橙眼少年推门而入") == "黑发橙眼少年推门而入"
    assert ("黑发橙眼少年", "Bam") not in descriptive.text_rules


def test_first_episode_discovers_canonical_name_from_own_text() -> None:
    """第 1 话没有历史 StoryBible 时，靠本话高频自发现规范名。"""
    canonicalizer = build_character_canonicalizer(
        characters=None,
        series_tts=None,
        series_aliases=None,
        extra_names=["恩慧", "恩彗"],
        extra_texts=[
            "恩慧说：我来了。",
            "恩慧又说：别怕。",
            "恩慧点头。",
            "恩彗站在门口。",
        ],
    )
    # 频次更高的「恩慧」当规范名，「恩彗」作为同音变体归一过去
    assert canonicalizer.canonicalize_role("恩彗") == "恩慧"
    assert canonicalizer.canonicalize_text("恩彗推门而入") == "恩慧推门而入"


def test_same_homophone_variant_found_in_text_is_unified() -> None:
    """正文里出现同音写法时，即使角色表没登记也要归一。"""
    canonicalizer = build_character_canonicalizer(
        characters=[_character("恩彗")],
        series_tts=None,
        series_aliases=None,
        extra_texts=["恩慧推门而入，雨水顺着伞骨滑落。"],
    )
    assert canonicalizer.canonicalize_role("恩慧") == "恩彗"
    assert canonicalizer.canonicalize_text("恩慧推门而入") == "恩彗推门而入"


def test_are_homophone_name_variants_rules() -> None:
    assert are_homophone_name_variants("恩彗", "恩慧") is True
    assert are_homophone_name_variants("恩彗", "恩惠") is True
    # 长度不同 / 单字 / 不同簇 → 不是同音变体
    assert are_homophone_name_variants("恩彗", "小恩") is False
    assert are_homophone_name_variants("彗", "慧") is False
    assert are_homophone_name_variants("恩彗", "恩雨") is False


def test_memory_merge_characters_accepts_homophone_variant(tmp_path: Path) -> None:
    """StoryBible 归并：同音异写认作同一个角色，不新增角色条目。"""
    series_dir = tmp_path / "series"
    series_dir.mkdir(parents=True)
    bible = StoryBibleV2(
        series_id="wt_v416",
        series_title="测试",
        characters=[
            CharacterProfile(
                name="恩彗",
                aliases=[],
                visual_traits="黑发橙眼",
                current_status="平静",
                first_seen_ep=1,
                last_seen_ep=1,
            )
        ],
    )
    save_model(bible, series_dir / "story_bible.json")
    manager = StoryBibleManager(series_dir, series_title="测试")
    script = NarrationScript(
        episode_id="wt_v416_ep_002",
        hook_title="雨夜来客",
        beats=[_beat("b1", "恩慧推门而入。")],
        episode_digest=EpisodeDigest(
            episode_no=2,
            one_line_hook="雨夜来客",
            summary="恩慧推门而入。",
            characters_present=["恩慧"],
        ),
        character_updates=[
            CharacterProfile(
                name="恩慧",
                aliases=[],
                visual_traits="黑发橙眼",
                current_status="紧张",
                first_seen_ep=2,
                last_seen_ep=2,
            )
        ],
    )
    manager.apply_episode_result(script)
    merged = load_model(StoryBibleV2, series_dir / "story_bible.json")
    assert [item.name for item in merged.characters] == ["恩彗"]
    assert "恩慧" in merged.characters[0].aliases


# ---------------------------------------------------------------------------
# B. Stage 4A / 4B 联动
# ---------------------------------------------------------------------------
def _story_response(content: str) -> httpx.Response:
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "qwen-plus",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    return httpx.Response(200, json=payload)


def _story_payload(name: str) -> str:
    return json.dumps(
        {
            "hook_opening": f"{name}推门而入",
            "full_story_markdown": f"{name}在雨夜敲响了门。",
            "chapters": [
                {
                    "chapter_index": 1,
                    "chapter_title": f"{name}的夜访",
                    "panel_hint_range": ["panel_001", "panel_002"],
                    "story_prose": f"{name}自称银行行长。",
                }
            ],
            "overall_plot_arc": f"{name}来访引出旧账。",
        },
        ensure_ascii=False,
    )


def _beats_payload(name: str) -> str:
    return json.dumps(
        {
            "hook_title": "雨夜来客",
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": f"{name}推门而入，雨水顺着伞骨滑落。",
                    "tts_text": "",
                    "emotion_instruction": "克制",
                    "speaker_role": name,
                    "target_panel_ids": ["panel_001"],
                    "motion": "static",
                    "pause_after_ms": 200,
                }
            ],
            "episode_digest": {
                "episode_no": 1,
                "one_line_hook": "雨夜来客",
                "summary": f"{name}深夜来访。",
                "keywords": ["雨夜"],
                "characters_present": [name],
            },
        },
        ensure_ascii=False,
    )


def _overview_payload() -> str:
    return json.dumps(
        {
            "hook_title": "雨夜来客",
            "one_line_hook": "雨夜来客",
            "summary": "银行行长深夜来访。",
            "keywords": ["雨夜"],
            "characters_present": ["恩慧"],
        },
        ensure_ascii=False,
    )


def _make_series_with_bible(tmp_path: Path) -> Path:
    series_dir = tmp_path / "workspace" / "wt_v416"
    series_dir.mkdir(parents=True)
    bible = StoryBibleV2(
        series_id="wt_v416",
        series_title="测试漫画",
        characters=[
            CharacterProfile(
                name="恩彗",
                aliases=[],
                visual_traits="黑发橙眼",
                current_status="平静",
                first_seen_ep=1,
                last_seen_ep=1,
            )
        ],
    )
    save_model(bible, series_dir / "story_bible.json")
    vision_dir = series_dir / "episodes" / "ep_002" / "03_vision"
    vision_dir.mkdir(parents=True)
    save_model(
        VisionWindowOutput.model_validate(
            {
                "panels": [
                    {
                        "panel_id": "panel_001",
                        "scene_summary": "雨夜街头，少年推门",
                        "dialogues": [],
                        "sfx_list": ["雨声"],
                    },
                    {
                        "panel_id": "panel_002",
                        "scene_summary": "来客站在灯下",
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
async def test_stage4_story_draft_and_script_unify_character_names(tmp_path: Path) -> None:
    """4A 故事稿与 4B 剧本里的「恩慧」都要落成规范名「恩彗」。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        prompt = body["messages"][-1]["content"]
        if "story_prose" in prompt:
            return _story_response(_story_payload("恩慧"))
        if "target_panel_ids" in prompt:
            return _story_response(_beats_payload("恩慧"))
        return _story_response(_overview_payload())

    respx.post(CHAT_URL).mock(side_effect=handler)

    series_dir = _make_series_with_bible(tmp_path)
    async with BailianClient(api_key="sk-test") as client:
        script, script_path = await __import__(
            "stages.s4_scriptwriter", fromlist=["run_stage4"]
        ).run_stage4(series_dir, 2, client, model="qwen-plus", series_title="测试漫画")

    assert script_path.is_file()
    assert all("恩慧" not in beat.narration_text for beat in script.beats)
    assert all(beat.speaker_role != "恩慧" for beat in script.beats)

    draft_path = series_dir / "episodes" / "ep_002" / "04_script" / "story_draft.json"
    assert draft_path.is_file()
    draft_text = draft_path.read_text(encoding="utf-8")
    assert "恩慧" not in draft_text
    assert "恩彗" in draft_text

    # 记忆体里的角色表也保持单一角色，不因同音异写拆分
    bible = load_model(StoryBibleV2, series_dir / "story_bible.json")
    assert [item.name for item in bible.characters] == ["恩彗"]


# ---------------------------------------------------------------------------
# C. Stage 5 缓存
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_second_stage5_run_reuses_cache_without_tts_calls(tmp_path: Path) -> None:
    """同样的剧本跑第二遍：一条 TTS 都不调，全部复用缓存。"""
    _series_id, series_dir, _config = _register(tmp_path)
    client = FakeTTS()

    stats_first = await _run_stage5(series_dir, _three_beats(), client)
    assert len(client.calls) == 3
    assert stats_first.get("synthesized") == 3
    assert stats_first.get("reused", 0) == 0

    second = FakeTTS()
    stats_second = await _run_stage5(series_dir, _three_beats(), second)
    assert second.calls == []
    assert stats_second.get("reused") == 3
    assert stats_second.get("synthesized") == 0
    # 清单仍是三条、顺序不变
    assert [item["beat_id"] for item in _manifest(series_dir)] == ["b1", "b2", "b3"]


@pytest.mark.anyio
async def test_only_changed_beat_is_resynthesized(tmp_path: Path) -> None:
    """改一句台词：只有那一句重新合成。"""
    _series_id, series_dir, _config = _register(tmp_path)
    client = FakeTTS()
    await _run_stage5(series_dir, _three_beats(), client)
    assert len(client.calls) == 3

    changed = _three_beats()
    changed[1] = _beat("b2", "他摘下手套，目光扫过全场，声音很冷。")
    second = FakeTTS()
    stats = await _run_stage5(series_dir, changed, second)
    assert len(second.calls) == 1
    assert "声音很冷" in second.calls[0]["text"]
    assert stats.get("reused") == 2
    assert stats.get("synthesized") == 1


@pytest.mark.anyio
async def test_pause_change_does_not_resynthesize(tmp_path: Path) -> None:
    """只改停顿：不重新合成，但清单里的停顿要更新。"""
    _series_id, series_dir, _config = _register(tmp_path)
    client = FakeTTS()
    await _run_stage5(series_dir, _three_beats(), client)

    changed = _three_beats()
    changed[0] = _beat("b1", "银行行长推门而入。", pause_after_ms=520)
    second = FakeTTS()
    stats = await _run_stage5(series_dir, changed, second)
    assert second.calls == []
    assert stats.get("reused") == 3
    manifest = {item["beat_id"]: item for item in _manifest(series_dir)}
    assert manifest["b1"]["pause_after_ms"] == 520


@pytest.mark.anyio
async def test_speed_override_change_resynthesizes_only_that_beat(tmp_path: Path) -> None:
    """改单句语速：只有那一句的音频要重做。"""
    _series_id, series_dir, _config = _register(tmp_path)
    client = FakeTTS()
    await _run_stage5(series_dir, _three_beats(), client)

    changed = _three_beats()
    changed[2] = _beat("b3", "雨声敲在玻璃窗上。", speed_override=1.2)
    second = FakeTTS()
    stats = await _run_stage5(series_dir, changed, second)
    assert len(second.calls) == 1
    assert stats.get("reused") == 2
    assert stats.get("synthesized") == 1


@pytest.mark.anyio
async def test_inserted_beat_shifts_ids_but_old_beats_hit_cache(tmp_path: Path) -> None:
    """中间插入新句导致 beat_id 顺延：没改的旧句仍命中缓存，只有新句要合成。"""
    _series_id, series_dir, _config = _register(tmp_path)
    client = FakeTTS()
    await _run_stage5(series_dir, _three_beats(), client)

    shifted = [
        _beat("b1", "银行行长推门而入。"),
        _beat("b2", "他摘下手套，目光扫过全场。"),
        _beat("b3", "他身后的影子比人还高。"),
        _beat("b4", "雨声敲在玻璃窗上。"),
    ]
    second = FakeTTS()
    stats = await _run_stage5(series_dir, shifted, second)
    assert len(second.calls) == 1
    assert "影子比人还高" in second.calls[0]["text"]
    assert stats.get("reused") == 3
    assert [item["beat_id"] for item in _manifest(series_dir)] == ["b1", "b2", "b3", "b4"]


@pytest.mark.anyio
async def test_failed_placeholder_never_pollutes_cache(tmp_path: Path) -> None:
    """合成失败的静音占位不进缓存；修好后重跑会真的重新合成。"""
    _series_id, series_dir, _config = _register(tmp_path)
    client = FakeTTS()
    client.fail_texts = {"雨声敲在玻璃窗上。"}
    stats = await _run_stage5(
        series_dir, _three_beats(), client, placeholder_on_failure=True
    )
    assert stats.get("synthesized") == 2
    cache_files = sorted(p.name for p in _cache_dir(series_dir).glob("*"))
    # 缓存里只有两条成功合成 + 索引文件
    assert len([name for name in cache_files if name != "index.json"]) == 2

    # 修好后重跑：失败那句必须真的重新合成（不能拿占位音频当缓存）
    fixed = FakeTTS()
    stats_fixed = await _run_stage5(series_dir, _three_beats(), fixed)
    assert len(fixed.calls) == 1
    assert "雨声敲在玻璃窗上" in fixed.calls[0]["text"]
    assert stats_fixed.get("reused") == 2
    assert stats_fixed.get("synthesized") == 1


@pytest.mark.anyio
async def test_resynthesize_single_beat_writes_cache(tmp_path: Path) -> None:
    """单句重配：不读缓存（真合成），但成功后要写回缓存。"""
    _series_id, series_dir, _config = _register(tmp_path)
    client = FakeTTS()
    await _run_stage5(series_dir, _three_beats(), client)

    changed = _three_beats()
    changed[0] = _beat("b1", "银行行长把伞收了起来。")
    _write_script(series_dir, changed)
    single = FakeTTS()
    await s5_tts.resynthesize_single_beat(
        single, series_dir, 1, "b1", trim_silence=False
    )
    assert len(single.calls) == 1
    assert "把伞收了起来" in single.calls[0]["text"]

    # 单句重配写回缓存后，整话重跑应当一次 TTS 都不调
    full = FakeTTS()
    stats = await _run_stage5(series_dir, changed, full)
    assert full.calls == []
    assert stats.get("reused") == 3


@pytest.mark.anyio
async def test_reset_from_stage_preserves_cache_only_when_asked(tmp_path: Path) -> None:
    """Stage 5 重跑保留缓存；手动重置（默认）彻底清空缓存。"""
    _series_id, series_dir, _config = _register(tmp_path)
    client = FakeTTS()
    await _run_stage5(series_dir, _three_beats(), client)
    cache_dir = _cache_dir(series_dir)
    assert cache_dir.is_dir()
    assert any(cache_dir.glob("*.wav"))

    manager = ManifestManager(series_dir, series_id=SERIES_ID)
    manager.reset_from_stage(1, "s5_tts", preserve_tts_cache=True)
    assert cache_dir.is_dir()
    assert any(cache_dir.glob("*.wav"))
    # 清单与成品音频仍被清掉
    assert not (_audio_dir(series_dir) / "audio_manifest.json").exists()
    assert not list(_audio_dir(series_dir).glob("beat_*.wav"))

    manager.reset_from_stage(1, "s5_tts")
    assert not cache_dir.exists()


@pytest.mark.anyio
async def test_reset_api_clears_audio_cache(tmp_path: Path) -> None:
    """``POST /api/episodes/{id}/reset`` 彻底清掉音频缓存。"""
    series_id, series_dir, config = _register(tmp_path)
    client = FakeTTS()
    await _run_stage5(series_dir, _three_beats(), client)
    cache_dir = _cache_dir(series_dir)
    assert cache_dir.is_dir()

    runner = JobRunner(config, tts_factory=lambda: client)
    api = TestClient(create_app(config, runner=runner))
    response = api.post(
        f"/api/episodes/{series_id}_ep_001/reset",
        json={"from_stage": "s5_tts"},
    )
    assert response.status_code == 200
    assert not cache_dir.exists()


# ---------------------------------------------------------------------------
# D. Web API：人工保存的角色名处理
# ---------------------------------------------------------------------------
# Patch v4.23：人工保存**不再强制**把角色名改回规范名——默认原样落盘，
# 只在响应里回传只读告警；只有显式要求（auto_unify_characters=True）才归一。
@pytest.mark.anyio
async def test_put_script_keeps_character_names_by_default(tmp_path: Path) -> None:
    """人工保存时同音异写的角色名原样保留，并在响应里给出只读告警。"""
    series_id, series_dir, config = _register(tmp_path)
    bible = StoryBibleV2(
        series_id=series_id,
        series_title="测试漫画",
        characters=[
            CharacterProfile(
                name="恩彗",
                aliases=[],
                visual_traits="黑发橙眼",
                current_status="平静",
                first_seen_ep=1,
                last_seen_ep=1,
            )
        ],
    )
    save_model(bible, series_dir / "story_bible.json")
    _write_script(series_dir, [_beat("b1", "银行行长推门而入。")])

    runner = JobRunner(config, tts_factory=lambda: FakeTTS())
    api = TestClient(create_app(config, runner=runner))
    response = api.put(
        f"/api/episodes/{series_id}_ep_001/script",
        json={
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "恩慧推门而入，雨水顺着伞骨滑落。",
                    "speaker_role": "恩慧",
                    "target_panel_ids": ["panel_001"],
                    "pause_after_ms": 200,
                }
            ]
        },
    )
    assert response.status_code == 200
    payload = response.json()
    # Patch v4.23：默认原样落盘，不再强制改回规范名。
    saved = load_model(
        NarrationScript, series_dir / "episodes" / "ep_001" / "04_script" / "script.json"
    )
    assert saved.beats[0].speaker_role == "恩慧"
    assert saved.beats[0].narration_text == "恩慧推门而入，雨水顺着伞骨滑落。"
    # 但要给出只读告警，提示写法与规范名不一致。
    warnings = payload.get("character_warnings") or []
    assert warnings, "同音异写应当产生只读告警"
    reasons = {w["reason"] for w in warnings}
    assert "homophone_variant" in reasons
    assert any(w["suggested_canonical"] == "恩彗" for w in warnings)


@pytest.mark.anyio
async def test_put_script_unifies_character_names_when_requested(tmp_path: Path) -> None:
    """显式要求 auto_unify_characters=True 时，人工保存照旧归一角色名。"""
    series_id, series_dir, config = _register(tmp_path)
    bible = StoryBibleV2(
        series_id=series_id,
        series_title="测试漫画",
        characters=[
            CharacterProfile(
                name="恩彗",
                aliases=[],
                visual_traits="黑发橙眼",
                current_status="平静",
                first_seen_ep=1,
                last_seen_ep=1,
            )
        ],
    )
    save_model(bible, series_dir / "story_bible.json")
    _write_script(series_dir, [_beat("b1", "银行行长推门而入。")])

    runner = JobRunner(config, tts_factory=lambda: FakeTTS())
    api = TestClient(create_app(config, runner=runner))
    response = api.put(
        f"/api/episodes/{series_id}_ep_001/script",
        json={
            "auto_unify_characters": True,
            "beats": [
                {
                    "beat_id": "b1",
                    "narration_text": "恩慧推门而入，雨水顺着伞骨滑落。",
                    "speaker_role": "恩慧",
                    "target_panel_ids": ["panel_001"],
                    "pause_after_ms": 200,
                }
            ],
        },
    )
    assert response.status_code == 200
    saved = load_model(
        NarrationScript, series_dir / "episodes" / "ep_001" / "04_script" / "script.json"
    )
    assert saved.beats[0].speaker_role == "恩彗"
    assert saved.beats[0].narration_text == "恩彗推门而入，雨水顺着伞骨滑落。"
