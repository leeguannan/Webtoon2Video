"""``core.memory`` StoryBible v2 四层记忆体测试（规范第 4.1 节）。

覆盖四层行为：

* Layer 1 人物实体图谱（角色参考名单）；
* Layer 2 分层摘要（近 N 话详摘 + 每 N 话篇章归档）；
* Layer 3 伏笔追踪器（只注入 ``open`` 状态伏笔）；
* Layer 4 按话只读快照（重跑第 K 话强制回退 ``ep_{K-1}.json``，防剧透倒灌）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.memory import StoryBibleManager
from core.schemas import (
    CharacterProfile,
    EpisodeDigest,
    NarrationScript,
    PlotThread,
    ScriptBeat,
    StoryBibleV2,
)


def make_script(
    episode_no: int,
    *,
    hook: str = "本话钩子",
    summary: str = "本话概要",
    keywords: list[str] | None = None,
    characters_present: list[str] | None = None,
    character_updates: list[CharacterProfile] | None = None,
    new_plot_threads: list[PlotThread] | None = None,
    resolved_thread_ids: list[str] | None = None,
) -> NarrationScript:
    """构造一份最小的第 ``episode_no`` 话撰稿产出。"""
    return NarrationScript(
        episode_id=f"wt_1_ep_{episode_no:03d}",
        hook_title=hook,
        beats=[
            ScriptBeat(
                beat_id=f"b{episode_no}",
                narration_text=f"第{episode_no}话的解说正文",
                target_panel_ids=["p1"],
            )
        ],
        episode_digest=EpisodeDigest(
            episode_no=episode_no,
            one_line_hook=hook,
            summary=summary,
            keywords=list(keywords or []),
            characters_present=list(characters_present or []),
        ),
        character_updates=list(character_updates or []),
        new_plot_threads=list(new_plot_threads or []),
        resolved_thread_ids=list(resolved_thread_ids or []),
    )


def make_manager(series_dir: Path, **kwargs) -> StoryBibleManager:
    kwargs.setdefault("series_id", "wt_1")
    kwargs.setdefault("series_title", "测试漫画")
    return StoryBibleManager(series_dir, **kwargs)


# --------------------------------------------------------------------------
# 基础读写
# --------------------------------------------------------------------------
def test_empty_bible_defaults(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    bible = manager.bible
    assert bible.series_id == "wt_1"
    assert bible.characters == []
    assert bible.episodes_index == []


def test_save_bible_persists_to_disk(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.bible.characters.append(
        CharacterProfile(
            name="林墨",
            aliases=[],
            visual_traits="黑发红瞳",
            current_status="存活",
            first_seen_ep=1,
            last_seen_ep=1,
        )
    )
    path = manager.save_bible()
    assert path == tmp_path / "story_bible.json"
    assert path.is_file()

    reloaded = make_manager(tmp_path).bible
    assert [c.name for c in reloaded.characters] == ["林墨"]


def test_story_bible_is_valid_against_contract(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.save_bible()
    payload = (tmp_path / "story_bible.json").read_text(encoding="utf-8")
    assert StoryBibleV2.model_validate_json(payload).series_id == "wt_1"


# --------------------------------------------------------------------------
# Layer 4：按话只读快照与防剧透
# --------------------------------------------------------------------------
def test_apply_episode_result_writes_snapshot(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.apply_episode_result(make_script(1, hook="开场", summary="第一话概要"))

    snapshot_path = tmp_path / "bible_snapshots" / "ep_001.json"
    assert snapshot_path.is_file()

    snapshot = manager.load_snapshot(1)
    assert snapshot is not None
    assert snapshot.snapshots_ep_nos == [1]
    assert [d.episode_no for d in snapshot.episodes_index] == [1]


def test_resolve_base_bible_prefers_previous_episode_snapshot(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    for ep in (1, 2, 3):
        manager.apply_episode_result(make_script(ep, hook=f"钩子{ep}", summary=f"概要{ep}"))

    base, source = manager.resolve_base_bible(3)
    assert source == "snapshot:ep_002"
    assert [d.episode_no for d in base.episodes_index] == [1, 2]


def test_no_spoiler_leak_when_rerunning_episode(tmp_path: Path) -> None:
    """重跑第 3 话时，第 3 话及之后的设定不得进入上下文。"""
    manager = make_manager(tmp_path)
    manager.apply_episode_result(make_script(1, hook="钩子1", summary="平淡的开场"))
    manager.apply_episode_result(make_script(2, hook="钩子2", summary="线索浮现"))
    manager.apply_episode_result(
        make_script(3, hook="钩子3", summary="密室真相终于揭晓")
    )

    context = manager.build_stage4_context(3)
    prompt = context.prompt_block()
    assert "密室真相" not in prompt
    assert all(d.episode_no < 3 for d in context.recent_digests)
    assert all(d.episode_no < 3 for d in context.recalled_digests)

    # live 记忆体里确实已经存在未来信息，说明过滤来自快照回退而非数据缺失。
    assert "密室真相" in manager.bible.episodes_index[-1].summary


def test_future_episode_does_not_leak_into_earlier_context(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    for ep in (1, 2, 3):
        manager.apply_episode_result(make_script(ep, hook=f"钩子{ep}", summary=f"概要{ep}"))
    manager.apply_episode_result(make_script(4, hook="钩子4", summary="反派真实身份曝光"))

    context = manager.build_stage4_context(3)
    assert "反派真实身份" not in context.prompt_block()


def test_snapshot_content_is_immutable_across_later_episodes(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.apply_episode_result(make_script(1, hook="钩子1", summary="概要1"))
    before = manager.load_snapshot(1)
    assert before is not None
    digest_count = len(before.episodes_index)

    for ep in (2, 3, 4):
        manager.apply_episode_result(make_script(ep, hook=f"钩子{ep}", summary=f"概要{ep}"))

    after = manager.load_snapshot(1)
    assert after is not None
    assert len(after.episodes_index) == digest_count
    assert [d.episode_no for d in after.episodes_index] == [1]


def test_resolve_base_bible_falls_back_to_filtered_live(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.bible.episodes_index = [
        EpisodeDigest(episode_no=ep, one_line_hook=f"钩子{ep}", summary=f"概要{ep}")
        for ep in (1, 2, 3)
    ]
    manager.save_bible()

    base, source = manager.resolve_base_bible(3)
    assert source == "live:filtered"
    assert [d.episode_no for d in base.episodes_index] == [1, 2]


def test_resolve_base_bible_for_first_episode_uses_live(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    base, source = manager.resolve_base_bible(1)
    assert source == "live:filtered"
    assert base.episodes_index == []


# --------------------------------------------------------------------------
# Layer 1：人物实体图谱
# --------------------------------------------------------------------------
def test_character_reference_built_from_updates(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.apply_episode_result(
        make_script(
            1,
            character_updates=[
                CharacterProfile(
                    name="林墨",
                    aliases=["小林"],
                    visual_traits="黑发红瞳",
                    current_status="存活",
                    first_seen_ep=0,
                    last_seen_ep=0,
                )
            ],
        )
    )

    references = manager.build_character_reference()
    assert len(references) == 1
    reference = references[0]
    assert reference.name == "林墨"
    assert reference.aliases == ("小林",)
    assert reference.visual_traits == "黑发红瞳"
    assert "林墨" in reference.line
    assert "小林" in reference.line


def test_character_updates_merge_into_existing_profile(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.apply_episode_result(
        make_script(
            1,
            character_updates=[
                CharacterProfile(
                    name="林墨",
                    aliases=["小林"],
                    visual_traits="黑发红瞳",
                    current_status="存活",
                    first_seen_ep=1,
                    last_seen_ep=1,
                )
            ],
        )
    )
    manager.apply_episode_result(
        make_script(
            2,
            character_updates=[
                CharacterProfile(
                    name="林墨",
                    aliases=["墨哥"],
                    visual_traits="黑发红瞳，左颊有疤",
                    current_status="负伤",
                    first_seen_ep=0,
                    last_seen_ep=0,
                )
            ],
        )
    )

    profile = manager.bible.characters[0]
    assert profile.aliases == ["小林", "墨哥"]
    assert profile.visual_traits == "黑发红瞳，左颊有疤"
    assert profile.current_status == "负伤"
    assert profile.first_seen_ep == 1
    assert profile.last_seen_ep == 2


# --------------------------------------------------------------------------
# Layer 3：伏笔追踪器
# --------------------------------------------------------------------------
def test_only_open_threads_are_injected_into_context(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.apply_episode_result(
        make_script(
            1,
            new_plot_threads=[
                PlotThread(
                    thread_id="t_secret",
                    title="身世之谜",
                    description="主角身世未明",
                    introduced_ep=1,
                ),
                PlotThread(
                    thread_id="t_ring",
                    title="戒指线索",
                    description="反复出现的黑戒指",
                    introduced_ep=1,
                ),
            ],
        )
    )
    manager.apply_episode_result(make_script(2, resolved_thread_ids=["t_ring"]))

    context = manager.build_stage4_context(3)
    assert [thread.thread_id for thread in context.open_threads] == ["t_secret"]
    assert "t_ring" not in context.prompt_block()


def test_resolved_thread_is_marked_in_live_bible(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.apply_episode_result(
        make_script(
            1,
            new_plot_threads=[
                PlotThread(
                    thread_id="t_secret",
                    title="身世之谜",
                    description="主角身世未明",
                    introduced_ep=1,
                )
            ],
        )
    )
    manager.apply_episode_result(make_script(2, resolved_thread_ids=["t_secret"]))

    thread = manager.bible.plot_threads[0]
    assert thread.status == "resolved"
    assert thread.resolved_ep == 2


# --------------------------------------------------------------------------
# Stage 4 上下文组装与关键词召回
# --------------------------------------------------------------------------
def build_six_episodes(manager: StoryBibleManager) -> None:
    plan = [
        (1, "钩子1", "主角发现龙脉", ["龙脉"]),
        (2, "钩子2", "顺着龙脉追踪", ["龙脉", "宝藏"]),
        (3, "钩子3", "反派登场，局势紧张", ["反派"]),
        (4, "钩子4", "日常休整", ["日常"]),
        (5, "钩子5", "日常训练", ["日常"]),
        (6, "钩子6", "日常整备", ["日常"]),
    ]
    for episode_no, hook, summary, keywords in plan:
        manager.apply_episode_result(
            make_script(episode_no, hook=hook, summary=summary, keywords=keywords)
        )


def test_stage4_context_uses_recent_three_digests(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    build_six_episodes(manager)
    context = manager.build_stage4_context(7)
    assert [d.episode_no for d in context.recent_digests] == [4, 5, 6]
    assert context.base_source == "snapshot:ep_006"


def test_keyword_recall_returns_most_recent_matching_digests(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    build_six_episodes(manager)
    context = manager.build_stage4_context(7, vision_keywords=["龙脉"])
    assert [d.episode_no for d in context.recalled_digests] == [2, 1]
    assert len(context.recalled_digests) <= 2


def test_keyword_recall_respects_limit(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, recall_limit=1)
    build_six_episodes(manager)
    context = manager.build_stage4_context(7, vision_keywords=["龙脉"])
    assert [d.episode_no for d in context.recalled_digests] == [2]


def test_keyword_recall_excludes_recent_digests(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    build_six_episodes(manager)
    context = manager.build_stage4_context(7, vision_keywords=["日常"])
    assert context.recalled_digests == []


def test_keyword_recall_without_keywords_is_empty(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    build_six_episodes(manager)
    context = manager.build_stage4_context(7)
    assert context.recalled_digests == []


def test_prompt_block_contains_all_sections(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.apply_episode_result(
        make_script(
            1,
            hook="钩子1",
            summary="概要1",
            keywords=["龙脉"],
            character_updates=[
                CharacterProfile(
                    name="林墨",
                    aliases=[],
                    visual_traits="黑发红瞳",
                    current_status="存活",
                    first_seen_ep=1,
                    last_seen_ep=1,
                )
            ],
            new_plot_threads=[
                PlotThread(
                    thread_id="t_secret",
                    title="身世之谜",
                    description="主角身世未明",
                    introduced_ep=1,
                )
            ],
        )
    )
    context = manager.build_stage4_context(2, vision_keywords=["龙脉"])
    prompt = context.prompt_block()
    assert "【角色图谱】" in prompt
    assert "【近况详摘】" in prompt
    assert "【未回收伏笔】" in prompt
    # 第 1 话已落在「近 3 话详摘」窗口内，不再重复召回
    assert "【远期召回】" not in prompt


def test_prompt_block_includes_recalled_section(tmp_path: Path) -> None:
    """远期召回命中时，Prompt 追加「远期召回」段落。"""
    manager = make_manager(tmp_path)
    for episode_no in range(1, 5):
        manager.apply_episode_result(
            make_script(
                episode_no,
                hook=f"钩子{episode_no}",
                summary=f"概要{episode_no}",
                keywords=["龙脉"] if episode_no == 1 else [],
            )
        )

    context = manager.build_stage4_context(5, vision_keywords=["龙脉"])
    prompt = context.prompt_block()
    assert "【远期召回】" in prompt
    assert [d.episode_no for d in context.recalled_digests] == [1]


# --------------------------------------------------------------------------
# Layer 2：每 N 话篇章归档
# --------------------------------------------------------------------------
def test_arc_summary_archived_every_ten_episodes(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    for episode_no in range(1, 11):
        manager.apply_episode_result(
            make_script(episode_no, hook=f"钩子{episode_no}", summary=f"概要{episode_no}")
        )

    arcs = manager.bible.arc_summaries
    assert len(arcs) == 1
    assert arcs[0].ep_range == (1, 10)
    assert arcs[0].arc_name == "第1-10话篇章"

    manager.apply_episode_result(make_script(11, hook="钩子11", summary="概要11"))
    assert len(manager.bible.arc_summaries) == 1

    for episode_no in range(12, 21):
        manager.apply_episode_result(
            make_script(episode_no, hook=f"钩子{episode_no}", summary=f"概要{episode_no}")
        )
    arcs = manager.bible.arc_summaries
    assert len(arcs) == 2
    assert arcs[1].ep_range == (11, 20)


def test_arc_summary_not_duplicated_on_rerun(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    for episode_no in range(1, 11):
        manager.apply_episode_result(
            make_script(episode_no, hook=f"钩子{episode_no}", summary=f"概要{episode_no}")
        )
    manager.apply_episode_result(make_script(10, hook="钩子10", summary="概要10"))
    assert len(manager.bible.arc_summaries) == 1


def test_maybe_archive_arc_is_idempotent(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    for episode_no in range(1, 11):
        manager.apply_episode_result(
            make_script(episode_no, hook=f"钩子{episode_no}", summary=f"概要{episode_no}")
        )
    assert manager.maybe_archive_arc(10) is None
    assert manager.maybe_archive_arc(5) is None


def test_arc_summary_uses_injected_compressor(tmp_path: Path) -> None:
    captured: list[str] = []

    def compressor(raw: str) -> str:
        captured.append(raw)
        return "压缩后的篇章摘要"

    manager = make_manager(tmp_path, arc_summary_compressor=compressor)
    for episode_no in range(1, 11):
        manager.apply_episode_result(
            make_script(episode_no, hook=f"钩子{episode_no}", summary=f"概要{episode_no}")
        )

    assert manager.bible.arc_summaries[0].summary == "压缩后的篇章摘要"
    assert captured and "第1话" in captured[0]


def test_local_compressor_truncates_to_limit(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, max_summary_chars=40)
    for episode_no in range(1, 11):
        manager.apply_episode_result(
            make_script(
                episode_no,
                hook=f"钩子{episode_no}",
                summary=f"概要{episode_no}" * 10,
            )
        )
    summary = manager.bible.arc_summaries[0].summary
    assert len(summary) <= 40
    assert summary.endswith("…")


@pytest.mark.parametrize("episode_no", [1, 5, 9])
def test_no_archive_before_interval(tmp_path: Path, episode_no: int) -> None:
    manager = make_manager(tmp_path)
    for current in range(1, episode_no + 1):
        manager.apply_episode_result(
            make_script(current, hook=f"钩子{current}", summary=f"概要{current}")
        )
    assert manager.bible.arc_summaries == []


def test_character_merge_matches_by_alias(tmp_path) -> None:
    """模型把角色从描述性称呼改成正式名时，按别名合并成同一条档案。

    真实调用里第 1 话登记“ 黑发橙眼少年 ”，第 2 话模型改用 “Bam” 并把旧称呼
    放进 aliases——只按名字匹配会把同一个人拆成两条，Layer 1 角色表就会重名。
    """
    manager = make_manager(tmp_path)
    manager.apply_episode_result(
        make_script(
            1,
            character_updates=[
                CharacterProfile(
                    name="黑发橙眼少年",
                    aliases=["主角", "登塔者"],
                    visual_traits="黑发、橙色瞳孔",
                    current_status="静默登场",
                    first_seen_ep=1,
                    last_seen_ep=1,
                )
            ],
        )
    )
    manager.apply_episode_result(
        make_script(
            2,
            character_updates=[
                CharacterProfile(
                    name="Bam",
                    aliases=["黑发橙眼少年", "主角", "登塔者"],
                    visual_traits="黑发、橙色瞳孔、手持黑色长棍",
                    current_status="踏入神之塔第一层",
                    first_seen_ep=1,
                    last_seen_ep=2,
                )
            ],
        )
    )

    bible = manager.bible
    assert len(bible.characters) == 1
    character = bible.characters[0]
    assert character.name == "黑发橙眼少年"
    assert "Bam" in character.aliases
    assert character.aliases.count("主角") == 1
    assert character.name not in character.aliases
    assert character.first_seen_ep == 1
    assert character.last_seen_ep == 2
    assert "长棍" in character.visual_traits
    assert character.current_status == "踏入神之塔第一层"
