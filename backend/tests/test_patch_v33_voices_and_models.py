"""Patch v3.3 Patch 15：34 音色目录 + 带空格 ID 映射 + 模型升级与降级兜底。

全部离线：不触网、不消耗百炼额度（TTS / LLM 都用假客户端）。
"""

from __future__ import annotations

import io
import json
import math
import struct
import wave
from pathlib import Path
from typing import Any, Sequence

import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image

from core.config import AppConfig, ModelConfig, WorkspaceConfig
from core.memory import Stage4Context
from core.schemas import PanelVisionInfo
from providers.llm_base import ChatResponse, LLMError, LLMRequestError, LLMTruncatedError
from providers.tts_qwen import (
    CORE_VOICE_IDS,
    QWEN3_TTS_VOICES,
    QWEN3_VOICE_IDS,
    voice_sample_filename,
)
from stages import s0_catalog, s4_scriptwriter
from web.server import create_app

#: 后端包根目录（``backend/``），用于读 ``config.example.yaml``。
BACKEND_ROOT = Path(__file__).resolve().parents[1]

#: 规格要求的音色条目字段（缺一不可）。
REQUIRED_VOICE_FIELDS = {
    "voice_id",
    "display_name",
    "gender",
    "age_group",
    "category",
    "description",
}

#: 验收①点名要覆盖的音色。
REQUIRED_VOICE_IDS = (
    "Elias",
    "Neil",
    "Vincent",
    "Eldric Sage",
    "Mia",
    "Seren",
    "Bellona",
    "Katerina",
    "Cherry",
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _wav_bytes(seconds: float = 0.2, rate: int = 24000, freq: int = 440) -> bytes:
    """造一段正弦波 WAV 字节。"""
    buf = io.BytesIO()
    total = int(rate * seconds)
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for index in range(total):
            sample = int(12000 * math.sin(2 * math.pi * freq * index / rate))
            frames.extend(struct.pack("<h", sample))
        handle.writeframes(bytes(frames))
    return buf.getvalue()


class _FakeTTS:
    """记录每次合成调用的假 TTS 客户端。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def synthesize(self, text: str, *, instruction: str, voice: str | None = None) -> Any:
        self.calls.append(voice or "")
        return type("Result", (), {"audio": _wav_bytes(0.2), "content_type": "audio/x-wav"})()

    async def aclose(self) -> None:
        return None


class _FakeLLM:
    """按模型名决定「未开通」还是「正常返回」的假 LLM 客户端。"""

    def __init__(self, blocked: Sequence[str], payload: dict[str, Any]) -> None:
        self.blocked = set(blocked)
        self.payload = payload
        self.calls: list[str] = []
        self.thinking_flags: list[Any] = []

    async def chat(
        self,
        messages: Any,
        *,
        model: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        self.calls.append(model)
        self.thinking_flags.append(kwargs.get("enable_thinking"))
        if model in self.blocked:
            raise LLMRequestError(f"百炼返回 404：ModelNotFound: {model}")
        return ChatResponse(
            content=json.dumps(self.payload, ensure_ascii=False),
            model=model,
        )


# ---------------------------------------------------------------------------
# A. 音色目录（验收①）
# ---------------------------------------------------------------------------


def test_voice_catalog_has_at_least_34_unique_entries() -> None:
    assert len(QWEN3_TTS_VOICES) >= 34
    ids = [str(item["voice_id"]) for item in QWEN3_TTS_VOICES]
    assert len(set(ids)) == len(ids), "voice_id 不能重复"
    assert list(QWEN3_VOICE_IDS) == ids


def test_every_voice_entry_carries_complete_metadata() -> None:
    for item in QWEN3_TTS_VOICES:
        voice_id = str(item.get("voice_id"))
        assert set(item) == REQUIRED_VOICE_FIELDS, voice_id
        assert item["gender"] in {"male", "female"}, voice_id
        assert item["age_group"] in {"child", "youth", "middle", "elder"}, voice_id
        assert item["category"] in {"narrator", "character", "dialect", "drama"}, voice_id
        assert str(item["display_name"]).strip(), voice_id
        assert str(item["description"]).strip(), voice_id


def test_required_voice_ids_are_present_and_complete() -> None:
    by_id = {str(item["voice_id"]): item for item in QWEN3_TTS_VOICES}
    for voice_id in REQUIRED_VOICE_IDS:
        item = by_id.get(voice_id)
        assert item is not None, f"缺少音色：{voice_id}"
        assert set(item) == REQUIRED_VOICE_FIELDS, voice_id
        assert item["display_name"], voice_id
        assert item["description"], voice_id


def test_core_tier_is_high_frequency_subset() -> None:
    assert len(CORE_VOICE_IDS) == 14
    assert set(CORE_VOICE_IDS) <= set(QWEN3_VOICE_IDS)
    assert "Neil" in CORE_VOICE_IDS


# ---------------------------------------------------------------------------
# B. 带空格音色 ID 的文件名映射（验收②）
# ---------------------------------------------------------------------------


def test_voice_sample_filename_maps_space_to_underscore() -> None:
    assert voice_sample_filename("Eldric Sage") == "Eldric_Sage.wav"
    assert voice_sample_filename("Eldric_Sage") == "Eldric_Sage.wav"


def test_voice_sample_filename_is_path_safe() -> None:
    assert voice_sample_filename("../evil") == "evil.wav"
    assert voice_sample_filename("a/b") == "ab.wav"
    assert "/" not in voice_sample_filename("../../etc/passwd")
    assert voice_sample_filename("") == "unknown.wav"


# ---------------------------------------------------------------------------
# C. 模型默认值升级（验收③）
# ---------------------------------------------------------------------------


def test_model_config_defaults_are_upgraded() -> None:
    config = ModelConfig()
    assert config.story_model == "qwen4-max"
    assert config.script == "qwen3.7-plus"


def test_config_example_yaml_matches_new_defaults() -> None:
    example = yaml.safe_load(
        (BACKEND_ROOT / "config.example.yaml").read_text(encoding="utf-8")
    )
    assert example["models"]["story_model"] == "qwen4-max"
    assert example["models"]["script"] == "qwen3.7-plus"


def test_stage4_call_sites_keep_thinking_disabled() -> None:
    """所有 qwen4-*/qwen3.7-* 调用继续透传 ``enable_thinking=False``。"""
    source = (BACKEND_ROOT / "src" / "stages" / "s4_scriptwriter.py").read_text(
        encoding="utf-8"
    )
    assert source.count("**_NO_THINKING_KWARGS,") == 4
    # 四处 LLM 调用都必须包在降级兜底里。
    assert source.count("run_with_model_fallback(") >= 5  # 1 处定义 + 4 处调用


# ---------------------------------------------------------------------------
# D. 模型未开通时的降级兜底
# ---------------------------------------------------------------------------


def test_is_model_access_error_recognizes_unavailable_models() -> None:
    assert s4_scriptwriter.is_model_access_error(LLMRequestError("百炼返回 404：ModelNotFound"))
    assert s4_scriptwriter.is_model_access_error(LLMRequestError("百炼返回 403：AccessDenied"))
    assert s4_scriptwriter.is_model_access_error(LLMError("该模型未开通"))
    # 其它错误不触发降级（截断、5xx 等）。
    assert not s4_scriptwriter.is_model_access_error(LLMError("百炼返回 500：服务器开小差"))
    assert not s4_scriptwriter.is_model_access_error(LLMTruncatedError("输出被截断"))
    assert not s4_scriptwriter.is_model_access_error(RuntimeError("boom"))


def test_fallback_candidates_follow_documented_chain() -> None:
    # Patch v4.17：降级链以百炼 DeepSeek 起头（未开通该模型时按链退回 qwen 系）。
    assert s4_scriptwriter.MODEL_FALLBACK_CHAIN == (
        "deepseek-v4.1-flash-1017",
        "qwen4-max",
        "qwen3.8-max",
        "qwen3.7-plus",
        "qwen-plus",
    )
    assert s4_scriptwriter._fallback_candidates("deepseek-v4.1-flash-1017") == [
        "qwen4-max",
        "qwen3.8-max",
        "qwen3.7-plus",
        "qwen-plus",
    ]
    assert s4_scriptwriter._fallback_candidates("qwen4-max") == [
        "qwen3.8-max",
        "qwen3.7-plus",
        "qwen-plus",
    ]
    assert s4_scriptwriter._fallback_candidates("qwen-plus") == []
    # 主模型不在链里（自定义模型）→ 整条链都是候选。
    assert s4_scriptwriter._fallback_candidates("my-custom-model") == list(
        s4_scriptwriter.MODEL_FALLBACK_CHAIN
    )


@pytest.mark.anyio
async def test_run_with_model_fallback_switches_model_and_warns(capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []

    async def call(model: str) -> str:
        calls.append(model)
        if model in {"qwen4-max", "qwen3.8-max"}:
            raise LLMRequestError(f"百炼返回 404：ModelNotFound: {model}")
        return f"ok:{model}"

    result = await s4_scriptwriter.run_with_model_fallback(
        "qwen4-max", call, label="Stage 4A 故事反写"
    )
    assert result == "ok:qwen3.7-plus"
    assert calls == ["qwen4-max", "qwen3.8-max", "qwen3.7-plus"]
    captured = capsys.readouterr()
    assert "自动回退到" in captured.err
    assert "qwen3.8-max" in captured.err


@pytest.mark.anyio
async def test_run_with_model_fallback_does_not_swallow_other_errors() -> None:
    async def call(model: str) -> str:
        raise LLMTruncatedError("输出被截断")

    with pytest.raises(LLMTruncatedError):
        await s4_scriptwriter.run_with_model_fallback("qwen4-max", call)


@pytest.mark.anyio
async def test_run_with_model_fallback_raises_when_chain_exhausted() -> None:
    async def call(model: str) -> str:
        raise LLMRequestError("百炼返回 404：ModelNotFound")

    with pytest.raises(LLMRequestError):
        await s4_scriptwriter.run_with_model_fallback("qwen4-max", call)


@pytest.mark.anyio
async def test_stage4a_story_draft_falls_back_when_model_not_open(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """验收③：4A 故事反写在 qwen4-max 未开通时告警并平滑回退完成产出。"""
    payload = {
        "episode_id": "wt_test_ep_002",
        "hook_opening": "雨夜有人敲门。",
        "full_story_markdown": "## 雨夜试探\n\n这一段讲的是雨夜里的相遇与试探。",
        "chapters": [
            {
                "chapter_index": 1,
                "chapter_title": "雨夜试探",
                "panel_hint_range": ["panel_001", "panel_001"],
                "story_prose": "这一段讲的是雨夜里的相遇与试探。",
            }
        ],
    }
    client = _FakeLLM(blocked={"qwen4-max", "qwen3.8-max"}, payload=payload)
    context = Stage4Context(episode_no=2, base_source="snapshot:ep_001")
    panels = [
        PanelVisionInfo(panel_id="panel_001", scene_summary="雨夜街头，少年攥紧信纸")
    ]

    draft = await s4_scriptwriter._write_story_draft(
        client,
        model="qwen4-max",
        context=context,
        panels=panels,
        episode_id="wt_test_ep_002",
        series_title="测试漫画",
        template=None,
        temperature=0.7,
    )

    assert client.calls == ["qwen4-max", "qwen3.8-max", "qwen3.7-plus"]
    assert draft.hook_opening == "雨夜有人敲门。"
    assert len(draft.chapters) == 1
    # 每次调用都关掉了思考模式。
    assert client.thinking_flags == [False, False, False]
    captured = capsys.readouterr()
    assert "自动回退到" in captured.err


# ---------------------------------------------------------------------------
# E. init_voice_samples 分层 / 幂等 / 失败隔离
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_generate_voice_samples_full_tier_and_idempotent(tmp_path: Path) -> None:
    from scripts import init_voice_samples

    client = _FakeTTS()
    out = tmp_path / "voice_samples"
    results = await init_voice_samples.generate_voice_samples(out, client=client)

    assert len(results) == 34
    assert all(path.is_file() for path in results.values())
    assert len(client.calls) == 34
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["voices"]) == 34
    assert manifest["voices"]["Elias"]["file"] == "Elias.wav"
    # 带空格的音色 ID 落成下划线文件名。
    assert manifest["voices"]["Eldric Sage"]["file"] == "Eldric_Sage.wav"

    # 第二次运行：全部跳过，零新增合成。
    client.calls.clear()
    await init_voice_samples.generate_voice_samples(out, client=client)
    assert client.calls == []


@pytest.mark.anyio
async def test_generate_voice_samples_core_tier_only(tmp_path: Path) -> None:
    from scripts import init_voice_samples

    client = _FakeTTS()
    out = tmp_path / "voice_samples"
    results = await init_voice_samples.generate_voice_samples(out, client=client, tier="core")

    assert len(results) == 14
    assert set(results) == set(CORE_VOICE_IDS)
    assert len(client.calls) == 14


@pytest.mark.anyio
async def test_generate_voice_samples_isolates_single_voice_failure(tmp_path: Path) -> None:
    from scripts import init_voice_samples

    class _OneFailureTTS(_FakeTTS):
        async def synthesize(self, text: str, *, instruction: str, voice: str | None = None) -> Any:
            if voice == "Dylan":
                raise RuntimeError("音色未开通")
            return await super().synthesize(text, instruction=instruction, voice=voice)

    client = _OneFailureTTS()
    out = tmp_path / "voice_samples"
    results = await init_voice_samples.generate_voice_samples(
        out, client=client, voices=["Dylan", "Eldric Sage"]
    )

    # 单条失败不中断整批，另一个音色照常产出。
    assert "Dylan" not in results
    assert "Eldric Sage" in results
    assert (out / "Eldric_Sage.wav").is_file()
    assert not (out / "Dylan.wav").exists()


@pytest.mark.anyio
async def test_generate_voice_samples_rejects_unknown_tier(tmp_path: Path) -> None:
    from scripts import init_voice_samples

    with pytest.raises(ValueError):
        await init_voice_samples.generate_voice_samples(
            tmp_path / "voice_samples", client=_FakeTTS(), tier="ultra"
        )


# ---------------------------------------------------------------------------
# F. Web API
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "ws"


@pytest.fixture()
def client(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = create_app(AppConfig(workspace=WorkspaceConfig(root=workspace)))
    # 把试听样例目录隔离到 tmp，避免依赖本机是否跑过 init_voice_samples。
    monkeypatch.chdir(workspace)
    return TestClient(app)


def test_list_tts_voices_endpoint_lists_full_catalog(client: TestClient) -> None:
    response = client.get("/api/tts/voices")
    assert response.status_code == 200
    voices = response.json()["voices"]
    assert len(voices) == 34
    ids = {item["voice_id"] for item in voices}
    assert set(REQUIRED_VOICE_IDS) <= ids
    first = voices[0]
    assert first["sample_url"] == f"/api/tts/samples/{first['voice_id']}"
    assert first["category"] in {"narrator", "character", "dialect", "drama"}
    assert first["age_group"] in {"child", "youth", "middle", "elder"}
    assert "sample_ready" in first
    # 带空格的音色 ID 做 URL 编码。
    spaced = next(item for item in voices if item["voice_id"] == "Eldric Sage")
    assert spaced["sample_url"] == "/api/tts/samples/Eldric%20Sage"


def test_tts_sample_route_accepts_space_and_underscore(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收②：``Eldric%20Sage`` 与 ``Eldric_Sage`` 解析到同一个样例文件。"""
    from web import server

    sample_dir = tmp_path / "voice_samples"
    sample_dir.mkdir(parents=True)
    wav_path = sample_dir / "Eldric_Sage.wav"
    wav_path.write_bytes(_wav_bytes(0.2))
    monkeypatch.setattr(server, "voice_sample_dir", lambda: sample_dir)

    spaced = client.get("/api/tts/samples/Eldric%20Sage")
    underscored = client.get("/api/tts/samples/Eldric_Sage")
    assert spaced.status_code == 200
    assert underscored.status_code == 200
    assert spaced.content == underscored.content == wav_path.read_bytes()
    assert spaced.headers["content-type"] == "audio/wav"


def test_config_endpoint_exposes_new_model_options(client: TestClient) -> None:
    payload = client.get("/api/config").json()
    assert payload["storyModel"] == "qwen4-max"
    assert payload["scriptModel"] == "qwen3.7-plus"
    assert "qwen4-max" in payload["storyModelOptions"]
    assert "qwen3.7-plus" in payload["scriptModelOptions"]


def test_put_config_persists_story_model(client: TestClient, workspace: Path) -> None:
    response = client.put("/api/config", json={"storyModel": "qwen3.8-max"})
    assert response.status_code == 200, response.text
    assert response.json()["storyModel"] == "qwen3.8-max"

    saved = yaml.safe_load((workspace / "config.yaml").read_text(encoding="utf-8"))
    assert saved["models"]["story_model"] == "qwen3.8-max"


def test_series_tts_config_still_roundtrips(client: TestClient, workspace: Path) -> None:
    """Patch 13 的作品级配音接口在音色表扩容后依旧可用。"""
    source = workspace / "src" / "ep_001"
    source.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 600), (40, 80, 120)).save(source / "001.png")
    catalog = s0_catalog.import_local_catalog(workspace / "src")
    s0_catalog.CatalogRegistry(workspace).register(catalog)

    payload = client.get(f"/api/series/{catalog.series_id}/tts-config").json()
    assert len(payload["voices"]) == 34
    assert payload["narrator"] == {"voice": "Elias", "speed": 1.18}
