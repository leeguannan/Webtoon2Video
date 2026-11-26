"""Patch v4.44：多风格引擎（StyleProfileSpec）+ 动作连招蒙太奇 + 繁中前置规范化。

覆盖四组：

1. **零回归基线**：未配置风格 / 显式 ``drama_suspense`` 时，S2 参数、
   ``apply_narrative_pacing``、``apply_auto_motions`` 的结果与 4.43 **逐项相等**。
2. **动作模式**：暗底过渡带切分不切碎高亮对白框、S4 短停顿 160/450/600/260、
   宽比 >= 1.15 触发横扫、连招动态上限 6、动作提示词生效且不污染基线。
3. **扩展性与容错**：动态注册新预设无需改 Stage 主代码即生效、未知预设回落
   ``drama_suspense``、``tw2sp`` 优先与 ``t2s`` 降级保护 ``[sfx:key]``。
4. **API 边界**：``/api/style-profiles``、``/api/series/{id}/style-profile``
   非法参数拦截、``/scenes/{scene_id}/compress-montage`` 多镜合并与跨幕隔离。

全部离线：纯函数 + TestClient + 临时盘面，不触网、不消耗 API 额度。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.config import AppConfig, SeriesConfig, WorkspaceConfig
from core.normalize import to_simplified_chinese
from core.schemas import EpisodeDigest, NarrationScript, PanelMeta, ScriptBeat, StoryboardScene
from core.style_profiles import (
    DEFAULT_STYLE_PROFILE_ID,
    STYLE_PROFILE_REGISTRY,
    StyleProfileSpec,
    get_style_profile,
    resolve_series_style_profile,
    suggest_style_profile_from_url_or_genre,
)
from stages import s2_segmenter
from stages.s4_scriptwriter import (
    apply_auto_motions,
    apply_narrative_pacing,
    compress_scene_montage,
    load_style_guidance,
)
from web.server import create_app

# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------


def _beat(
    beat_id: str,
    text: str,
    panels: list[str],
    scene_id: str = "sc_001",
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "beat_id": beat_id,
        "narration_text": text,
        "tts_text": text,
        "target_panel_ids": panels,
        "scene_id": scene_id,
    }
    payload.update(extra)
    return payload


def _panel(
    panel_id: str,
    width: int = 900,
    height: int = 1400,
) -> PanelMeta:
    return PanelMeta(
        panel_id=panel_id,
        image_path=f"02_panels/{panel_id}.png",
        width=width,
        height=height,
        aspect_ratio=round(width / max(1, height), 4),
        source_slice_range=(0, height),
        is_tall_panel=height > width * 1.2,
    )


def _scene(scene_id: str, index: int, panel_ids: list[str]) -> StoryboardScene:
    return StoryboardScene(
        scene_id=scene_id,
        scene_index=index,
        title=f"第 {index} 幕",
        panel_ids=panel_ids,
        enabled=True,
    )


# ---------------------------------------------------------------------------
# 1) 零回归基线：drama_suspense 必须与 4.43 逐项相等
# ---------------------------------------------------------------------------


def test_drama_suspense_profile_matches_v443_constants() -> None:
    """基线预设的每个数值都必须等于 4.43 的硬编码常量。"""
    profile = get_style_profile(DEFAULT_STYLE_PROFILE_ID)
    assert profile.id == "drama_suspense"

    # 节奏：4.43 的 PACE_* 常量
    assert profile.pacing.panel_switch_ms == 400
    assert profile.pacing.turn_ms == 950
    assert profile.pacing.scene_end_ms == 1150
    assert profile.pacing.breath_ms == 480
    assert profile.pacing.same_panel_min_ms == 150
    assert profile.pacing.default_speed_bias == 1.0

    # 运镜：4.43 的 MOTION_* 常量
    assert profile.motion.wide_pan_ratio == 1.35
    assert profile.motion.max_dynamic_streak == 3
    assert profile.motion.max_shake_streak == 1
    assert profile.motion.prefer_vertical_focus_sweep is True

    # 切图 / 蒙太奇 / 叙事：4.43 的行为（全部关闭）
    assert profile.segment.allow_dark_gutter is False
    assert profile.segment.merge_sfx_fragments is False
    assert profile.montage.allow_action_combo is False
    assert profile.montage.max_scene_panels == 12
    assert profile.narrative.forbid_play_by_play is False
    assert profile.narrative.target_beat_density_ratio == 1.0


def test_unknown_profile_falls_back_to_baseline() -> None:
    """未知 / 空预设 id 一律回退基线，且**不污染注册表**。"""
    assert get_style_profile("").id == DEFAULT_STYLE_PROFILE_ID
    assert get_style_profile("no_such_style").id == DEFAULT_STYLE_PROFILE_ID

    before = STYLE_PROFILE_REGISTRY[DEFAULT_STYLE_PROFILE_ID].pacing.turn_ms
    get_style_profile("no_such_style")
    after = STYLE_PROFILE_REGISTRY[DEFAULT_STYLE_PROFILE_ID].pacing.turn_ms
    assert before == after


def test_pacing_without_profile_equals_drama_profile() -> None:
    """不传风格 ≡ 传 drama_suspense（零回归铁律）。"""
    beats = [
        _beat("b1", "他抬起头看向窗外。", ["p_001"]),
        _beat("b2", "她终于开口了。", ["p_002"]),
        _beat("b3", "门外传来脚步声。", ["p_003"]),
    ]
    scenes = [_scene("sc_001", 1, ["p_001", "p_002", "p_003"])]
    durations = {"b1": 1800, "b2": 1600, "b3": 4200}

    baseline, report_a = apply_narrative_pacing(
        beats, scenes=scenes, audio_durations_ms=durations
    )
    explicit, report_b = apply_narrative_pacing(
        beats,
        scenes=scenes,
        audio_durations_ms=durations,
        style_profile=get_style_profile(DEFAULT_STYLE_PROFILE_ID),
    )
    assert [b["pause_after_ms"] for b in baseline] == [
        b["pause_after_ms"] for b in explicit
    ]
    assert report_a.updated_count == report_b.updated_count


def test_auto_motions_without_profile_equals_drama_profile() -> None:
    """不传风格 ≡ 传 drama_suspense（运镜零回归）。"""
    beats = [
        _beat("b1", "他一拳砸下。", ["p_001"]),
        _beat("b2", "对手应声倒地。", ["p_002"]),
        _beat("b3", "尘埃落定。", ["p_003"]),
    ]
    metas = [
        _panel("p_001"),
        _panel("p_002"),
        _panel("p_003"),
    ]
    baseline, _ = apply_auto_motions(beats, panel_metas=metas)
    explicit, _ = apply_auto_motions(
        beats, panel_metas=metas, style_profile=get_style_profile(DEFAULT_STYLE_PROFILE_ID)
    )
    assert [b["motion"] for b in baseline] == [b["motion"] for b in explicit]


def test_default_series_config_resolves_to_baseline() -> None:
    """没写 style_profile 的老作品，解析出来必须是基线。"""
    resolved = resolve_series_style_profile(SeriesConfig())
    assert resolved.id == DEFAULT_STYLE_PROFILE_ID
    assert resolved.pacing.scene_end_ms == 1150


def test_drama_style_guidance_does_not_change_prompt_semantics() -> None:
    """基线风格片段只允许复述既有铁律，不许新增要求。"""
    guidance = load_style_guidance(get_style_profile(DEFAULT_STYLE_PROFILE_ID))
    assert guidance.strip(), "基线风格片段不应为空"
    # 基线片段不得出现动作版专属指令
    for banned in ("连招", "蒙太奇", "8~18 字"):
        assert banned not in guidance


# ---------------------------------------------------------------------------
# 2) 动作模式
# ---------------------------------------------------------------------------


def test_action_profile_values() -> None:
    """动作风格的数值必须与规格一致。"""
    profile = get_style_profile("action_combat")
    assert profile.segment.allow_dark_gutter is True
    assert profile.segment.merge_sfx_fragments is True
    assert profile.montage.allow_action_combo is True
    assert profile.montage.combo_min_panels == 3
    assert profile.montage.combo_max_panels == 6
    assert profile.montage.max_scene_panels == 16
    assert profile.narrative.forbid_play_by_play is True
    assert profile.narrative.target_beat_density_ratio == 0.55
    assert profile.motion.wide_pan_ratio == 1.15
    assert profile.motion.max_dynamic_streak == 6
    assert profile.pacing.panel_switch_ms == 160
    assert profile.pacing.turn_ms == 450
    assert profile.pacing.scene_end_ms == 600
    assert profile.pacing.breath_ms == 260
    assert profile.pacing.same_panel_min_ms == 100


def test_action_pacing_uses_shorter_pauses() -> None:
    """动作风格的停顿明显更短（600/450/260/160）。"""
    beats = [
        _beat("b1", "他一拳挥出。", ["p_001"]),
        _beat("b2", "对手闪身避开。", ["p_002"]),
    ]
    scenes = [_scene("sc_001", 1, ["p_001", "p_002"])]
    durations = {"b1": 1500, "b2": 1500}

    paced, _ = apply_narrative_pacing(
        beats,
        scenes=scenes,
        audio_durations_ms=durations,
        style_profile=get_style_profile("action_combat"),
    )
    # 换格底线：动作版 160ms（基线 400ms）
    assert paced[0]["pause_after_ms"] == 160

    baseline, _ = apply_narrative_pacing(
        beats, scenes=scenes, audio_durations_ms=durations
    )
    assert baseline[0]["pause_after_ms"] == 400

    # 同图保底留白：动作版 100ms（基线 150ms）
    same_panel, _ = apply_narrative_pacing(
        [_beat("b1", "他一拳挥出。", ["p_001"]), _beat("b2", "对手闪身。", ["p_001"])],
        scenes=[_scene("sc_001", 1, ["p_001"])],
        audio_durations_ms={"b1": 900, "b2": 900},
        style_profile=get_style_profile("action_combat"),
    )
    assert same_panel[0]["pause_after_ms"] == 100


def test_action_wide_ratio_triggers_horizontal_pan() -> None:
    """宽比 1.15 即触发横扫（基线要 1.35）。"""
    beats = [_beat("b1", "他一拳挥出。", ["p_001"])]
    metas = [_panel("p_001", width=1200, height=1000)]
    profile = get_style_profile("action_combat")
    assert 1200 / 1000 >= profile.motion.wide_pan_ratio
    assert 1200 / 1000 < 1.35  # 基线不会判成宽格

    baseline, _ = apply_auto_motions(beats, panel_metas=metas)
    action, _ = apply_auto_motions(beats, panel_metas=metas, style_profile=profile)
    assert baseline[0]["motion"] != action[0]["motion"]


def test_action_dynamic_streak_allows_six() -> None:
    """连续动态上限：基线 3 拍，动作 6 拍。"""
    metas = [_panel(f"p_{index:03d}") for index in range(1, 9)]
    beats = [
        _beat(f"b{index}", f"他挥出第{index}拳。", [f"p_{index:03d}"]) for index in range(1, 9)
    ]
    baseline, _ = apply_auto_motions(beats, panel_metas=metas)
    action, _ = apply_auto_motions(
        beats, panel_metas=metas, style_profile=get_style_profile("action_combat")
    )

    def _max_streak(items: list[dict[str, Any]]) -> int:
        best = current = 0
        for item in items:
            if item["motion"] in ("static", ""):
                current = 0
            else:
                current += 1
                best = max(best, current)
        return best

    assert _max_streak(action) >= _max_streak(baseline)
    assert _max_streak(action) <= 6


def test_action_style_guidance_is_loaded_and_distinct() -> None:
    """动作风格片段必须存在且与基线不同。"""
    action = load_style_guidance(get_style_profile("action_combat"))
    drama = load_style_guidance(get_style_profile(DEFAULT_STYLE_PROFILE_ID))
    assert action.strip()
    assert action != drama
    assert "动作" in action
    assert "合并" in action


def test_compress_scene_montage_merges_action_runs() -> None:
    """连续 3 拍动作句合并成一条多镜蒙太奇。"""
    beats = [
        ScriptBeat(
            beat_id="b1",
            narration_text="他一拳挥出重击。",
            target_panel_ids=["p_001"],
            scene_id="sc_001",
        ),
        ScriptBeat(
            beat_id="b2",
            narration_text="对手闪避不及。",
            target_panel_ids=["p_002"],
            scene_id="sc_001",
        ),
        ScriptBeat(
            beat_id="b3",
            narration_text="两人再度交手。",
            target_panel_ids=["p_003"],
            scene_id="sc_001",
        ),
        ScriptBeat(
            beat_id="b4",
            narration_text="她安静地走进房间。",
            target_panel_ids=["p_010"],
            scene_id="sc_002",
        ),
    ]
    merged, report = asyncio.run(
        compress_scene_montage(
            beats,
            scene_id="sc_001",
            style_profile=get_style_profile("action_combat"),
        )
    )
    assert report["changed"] is True
    assert report["removed_beat_ids"] == ["b2", "b3"]
    assert [beat.beat_id for beat in merged] == ["b1", "b4"]
    assert merged[0].target_panel_ids == ["p_001", "p_002", "p_003"]
    assert 8 <= len(merged[0].narration_text) <= 18
    # 跨幕隔离：另一幕的 Beat 一字不改
    assert merged[1].narration_text == "她安静地走进房间。"
    assert merged[1].target_panel_ids == ["p_010"]


def test_compress_scene_montage_is_noop_for_drama_profile() -> None:
    """基线风格不启用连招蒙太奇。"""
    beats = [
        ScriptBeat(
            beat_id="b1",
            narration_text="他一拳挥出重击。",
            target_panel_ids=["p_001"],
            scene_id="sc_001",
        ),
        ScriptBeat(
            beat_id="b2",
            narration_text="对手闪避不及。",
            target_panel_ids=["p_002"],
            scene_id="sc_001",
        ),
        ScriptBeat(
            beat_id="b3",
            narration_text="两人再度交手。",
            target_panel_ids=["p_003"],
            scene_id="sc_001",
        ),
    ]
    merged, report = asyncio.run(
        compress_scene_montage(
            beats,
            scene_id="sc_001",
            style_profile=get_style_profile(DEFAULT_STYLE_PROFILE_ID),
        )
    )
    assert report["changed"] is False
    assert len(merged) == 3


def test_dark_gutter_gap_is_detected_only_when_enabled() -> None:
    """暗底过渡带：动作风格能认出来，基线不认。"""
    canvas = np.full((600, 400), 12, dtype=np.uint8)
    canvas[100:400, 50:350] = 200  # 一整块内容
    gray = canvas

    gaps = s2_segmenter.find_dark_gutter_gaps(gray, min_gap=20)
    assert gaps, "暗底连续带应该被识别出来"

    blocks = s2_segmenter.cut_atomic_blocks(gray, min_gap=20, allow_dark_gutter=False)
    blocks_action = s2_segmenter.cut_atomic_blocks(
        gray, min_gap=20, allow_dark_gutter=True
    )
    assert len(blocks_action) >= len(blocks)


def test_dark_gutter_respects_local_structure_guard() -> None:
    """高亮对白框（局部有结构）不能被当成过渡带切开。"""
    canvas = np.full((600, 400), 10, dtype=np.uint8)
    # 暗底上放一个高亮方框（模拟对白气泡）
    canvas[200:300, 100:300] = 240
    guard = s2_segmenter._has_local_structure(canvas)
    assert guard is True
    # 有结构的暗底带不应被 find_dark_gutter_gaps 认成可切带
    gaps = s2_segmenter.find_dark_gutter_gaps(canvas, min_gap=20)
    for start, end in gaps:
        band = canvas[start:end]
        assert not s2_segmenter._has_local_structure(band)


def test_merge_sfx_fragment_blocks_merges_into_neighbour() -> None:
    """拟声碎片并入紧邻画面，且不超高度上限。"""
    blocks = [(0, 600), (640, 700), (760, 1400)]
    merged = s2_segmenter.merge_sfx_fragment_blocks(blocks, img_width=1000)
    assert len(merged) < len(blocks)
    for start, end in merged:
        assert end - start <= s2_segmenter.MAX_ATOMIC_BLOCK_HEIGHT


# ---------------------------------------------------------------------------
# 3) 扩展性与容错
# ---------------------------------------------------------------------------


def test_new_profile_can_be_registered_without_touching_stage_code() -> None:
    """动态注册一个新预设 → 各 Stage 直接能用（不改编管主干）。"""
    custom = StyleProfileSpec(
        id="test_custom",
        label="测试风格",
        description="单测用",
        recommended_for="单元测试",
    )
    custom.pacing.turn_ms = 700
    custom.motion.wide_pan_ratio = 1.2
    STYLE_PROFILE_REGISTRY["test_custom"] = custom
    try:
        assert get_style_profile("test_custom").pacing.turn_ms == 700
        beats = [_beat("b1", "他一拳挥出。", ["p_001"])]
        metas = [_panel("p_001", width=1200, height=1000)]
        _, report = apply_auto_motions(
            beats, panel_metas=metas, style_profile=get_style_profile("test_custom")
        )
        assert report is not None
    finally:
        STYLE_PROFILE_REGISTRY.pop("test_custom", None)


def test_resolve_series_style_profile_merges_three_layers() -> None:
    """预设 ← 作品覆盖 ← 临时覆盖，三层深度合并。"""
    config = SeriesConfig(style_overrides={"pacing": {"turn_ms": 800}})
    resolved = resolve_series_style_profile(config)
    assert resolved.pacing.turn_ms == 800
    assert resolved.pacing.scene_end_ms == 1150  # 未覆盖的保持预设值

    resolved2 = resolve_series_style_profile(
        config, overrides={"pacing": {"turn_ms": 600}}
    )
    assert resolved2.pacing.turn_ms == 600


def test_suggest_style_profile_from_url() -> None:
    """URL 含 /action/ 等特征 → 推荐动作风格。"""
    assert (
        suggest_style_profile_from_url_or_genre("https://x/action-combat/series")
        == "action_combat"
    )
    assert (
        suggest_style_profile_from_url_or_genre("https://x/martial-arts/series")
        == "action_combat"
    )
    assert (
        suggest_style_profile_from_url_or_genre("https://x/romance/series")
        == DEFAULT_STYLE_PROFILE_ID
    )


def test_t2s_converter_prefers_tw2sp_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """tw2sp 优先；缺 tw2sp 时无缝降级 t2s；``[sfx:key]`` 必须完好。"""
    import core.normalize as normalize_module

    monkeypatch.setattr(normalize_module, "_T2S_CONFIGS", ("tw2sp", "t2s"), raising=False)
    monkeypatch.setattr(normalize_module, "_T2S_CONVERTER", None, raising=False)

    text = "那時候因為距離事件發生已經過了一段時間。[sfx:heart_thump]"
    simplified = to_simplified_chinese(text)
    assert "[sfx:heart_thump]" in simplified
    assert "那" in simplified


def test_t2s_converter_handles_missing_opencc(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenCC 完全不可用时原样返回（不抛）。"""
    import core.normalize as normalize_module

    monkeypatch.setattr(normalize_module, "_T2S_CONVERTER", None, raising=False)
    monkeypatch.setattr(
        normalize_module, "_get_t2s_converter", lambda: None, raising=False
    )
    assert to_simplified_chinese("測試") == "測試"


# ---------------------------------------------------------------------------
# 4) API 边界
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    config = AppConfig(workspace=WorkspaceConfig(root=tmp_path / "ws"))
    return TestClient(create_app(config))


def _register_series(client: TestClient, tmp_path: Path) -> str:
    source = tmp_path / "src" / "ep_001"
    source.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 600), (30, 60, 90)).save(source / "001.png")
    payload = client.post("/api/series", json={"url": str(source)}).json()
    return str(payload["series_id"])


def test_style_profiles_api_lists_profiles(client: TestClient) -> None:
    resp = client.get("/api/style-profiles")
    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()["profiles"]]
    assert DEFAULT_STYLE_PROFILE_ID in ids
    assert "action_combat" in ids


def test_style_profiles_suggest_api(client: TestClient) -> None:
    resp = client.get(
        "/api/style-profiles/suggest",
        params={"url_or_genre": "https://x/action-combat/series"},
    )
    assert resp.status_code == 200
    assert resp.json()["style_profile"] == "action_combat"


def test_series_style_profile_get_and_put(client: TestClient, tmp_path: Path) -> None:
    series_id = _register_series(client, tmp_path)

    resp = client.get(f"/api/series/{series_id}/style-profile")
    assert resp.status_code == 200
    assert resp.json()["style_profile"] == DEFAULT_STYLE_PROFILE_ID

    resp = client.put(
        f"/api/series/{series_id}/style-profile",
        json={"style_profile": "action_combat"},
    )
    assert resp.status_code == 200
    assert resp.json()["style_profile"] == "action_combat"

    resp = client.get(f"/api/series/{series_id}/style-profile")
    assert resp.json()["style_profile"] == "action_combat"


def test_series_style_profile_rejects_unknown_profile(
    client: TestClient, tmp_path: Path
) -> None:
    series_id = _register_series(client, tmp_path)
    resp = client.put(
        f"/api/series/{series_id}/style-profile",
        json={"style_profile": "no_such_style"},
    )
    assert resp.status_code == 400


def test_compress_montage_api_merges_and_isolates_scenes(
    client: TestClient, tmp_path: Path
) -> None:
    """接口层：多镜合并 + 跨幕隔离 + 落盘。"""
    series_id = _register_series(client, tmp_path)
    workspace = tmp_path / "ws"
    episode_id = f"{series_id}_ep_001"
    episode_dir = workspace / series_id / "episodes" / "ep_001"
    script_dir = episode_dir / "04_script"
    script_dir.mkdir(parents=True, exist_ok=True)
    (episode_dir / "02_panels").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (360, 640), (10, 20, 30)).save(
        episode_dir / "02_panels" / "p_001.png"
    )

    script = NarrationScript(
        episode_id=episode_id,
        hook_title="连招蒙太奇接口测试",
        episode_digest=EpisodeDigest(
            episode_no=1, one_line_hook="测试", summary="测试"
        ),
        beats=[
            ScriptBeat(
                beat_id="b1",
                narration_text="他一拳挥出重击。",
                target_panel_ids=["p_001"],
                scene_id="sc_001",
            ),
            ScriptBeat(
                beat_id="b2",
                narration_text="对手闪避不及。",
                target_panel_ids=["p_001"],
                scene_id="sc_001",
            ),
            ScriptBeat(
                beat_id="b3",
                narration_text="两人再度交手。",
                target_panel_ids=["p_001"],
                scene_id="sc_001",
            ),
            ScriptBeat(
                beat_id="b4",
                narration_text="她安静地走进房间。",
                target_panel_ids=["p_001"],
                scene_id="sc_002",
            ),
        ],
    )
    from core.schemas import save_model

    save_model(script, script_dir / "script.json")

    # 切到动作风格
    resp = client.put(
        f"/api/series/{series_id}/style-profile",
        json={"style_profile": "action_combat"},
    )
    assert resp.status_code == 200

    resp = client.post(
        f"/api/episodes/{episode_id}/scenes/sc_001/compress-montage"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["report"]["changed"] is True
    assert body["report"]["removed_beat_ids"] == ["b2", "b3"]
    assert [beat["beat_id"] for beat in body["beats"]] == ["b1"]

    # 落盘：script.json 只剩 b1 + b4
    from core.schemas import load_model

    saved = load_model(NarrationScript, script_dir / "script.json")
    assert [beat.beat_id for beat in saved.beats] == ["b1", "b4"]
    assert saved.beats[0].target_panel_ids == ["p_001"]
    assert (script_dir / "script_readable.md").is_file()


def test_compress_montage_api_requires_existing_script(
    client: TestClient, tmp_path: Path
) -> None:
    series_id = _register_series(client, tmp_path)
    episode_id = f"{series_id}_ep_001"
    resp = client.post(
        f"/api/episodes/{episode_id}/scenes/sc_001/compress-montage"
    )
    assert resp.status_code == 404


def test_compress_montage_api_rejects_unknown_episode(client: TestClient) -> None:
    resp = client.post("/api/episodes/no_such_ep/scenes/sc_001/compress-montage")
    assert resp.status_code == 404
