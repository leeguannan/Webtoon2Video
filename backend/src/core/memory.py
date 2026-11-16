"""StoryBible v2 四层记忆体管理（规范第 4.1 节）。

四层结构：

* **Layer 1 人物实体图谱**（``CharacterProfile``）：为 Stage 3 视觉抽取注入角色参考名单，防脸盲重名。
* **Layer 2 分层摘要**（``ArcSummary`` + 近 N 话详摘）：短记忆看最近几话，长记忆看每 N 话一条不可覆盖的篇章摘要。
* **Layer 3 伏笔追踪器**（``PlotThread``）：只向 Stage 4 注入 ``open`` 状态的未回收悬念，回收后释放上下文。
* **Layer 4 按话只读快照**（``bible_snapshots/ep_{no:03d}.json``）：重跑第 K 话时强制回退到第 K-1 话快照，防剧透倒灌。

Stage 4 上下文组装（Token 预算恒定）：``arc_summaries``（全部历史篇章摘要）
+ ``episodes_index[-3:]``（近 3 话详摘）+ ``open`` 状态伏笔
+ 按当前话视觉关键词召回的至多 2 条远期历史 ``EpisodeDigest``。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from core.schemas import (
    ArcSummary,
    CharacterProfile,
    EpisodeDigest,
    NarrationScript,
    PlotThread,
    StoryBibleV2,
    save_model,
)

#: 篇章摘要压缩器：接收待压缩原文，返回归档摘要文本。
#: Stage 4 落地时注入百炼 ``qwen-plus`` 调用；未注入时退化为本地确定性压缩（可离线测试）。
ArcSummaryCompressor = Callable[[str], str]

MAX_TRAIT_CHARS = 200
MAX_ARC_CHARS = 300
MAX_DIGEST_CHARS = 240
MAX_THREAD_CHARS = 200


def _clip(text: str, limit: int) -> str:
    """按字符上限裁剪文本，保证注入 Prompt 的上下文长度恒定。"""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


@dataclass(frozen=True)
class CharacterReference:
    """注入 Stage 3 视觉 Prompt 的角色参考条目。"""

    name: str
    aliases: tuple[str, ...]
    visual_traits: str
    current_status: str

    @property
    def line(self) -> str:
        """单行角色描述（``名字（别名：A、B）：外貌特征``）。"""
        alias_text = f"（别名：{'、'.join(self.aliases)}）" if self.aliases else ""
        return f"{self.name}{alias_text}：{self.visual_traits}"


class Stage4Context(BaseModel):
    """Stage 4 撰稿用的紧凑记忆上下文（Token 预算恒定）。"""

    episode_no: int
    base_source: str
    characters: list[CharacterProfile] = Field(default_factory=list)
    arc_summaries: list[ArcSummary] = Field(default_factory=list)
    recent_digests: list[EpisodeDigest] = Field(default_factory=list)
    open_threads: list[PlotThread] = Field(default_factory=list)
    recalled_digests: list[EpisodeDigest] = Field(default_factory=list)

    def prompt_block(self) -> str:
        """渲染为可直接拼进撰稿 Prompt 的文本块。"""
        lines: list[str] = [f"【记忆基线】{self.base_source}"]

        if self.characters:
            lines.append("【角色图谱】")
            for character in self.characters:
                alias_text = f"（别名：{'、'.join(character.aliases)}）" if character.aliases else ""
                lines.append(
                    f"- {character.name}{alias_text}：{_clip(character.visual_traits, MAX_TRAIT_CHARS)}"
                )

        if self.arc_summaries:
            lines.append("【篇章摘要】")
            for arc in self.arc_summaries:
                lines.append(
                    f"- {arc.arc_name} [{arc.ep_range[0]}-{arc.ep_range[1]}]：{_clip(arc.summary, MAX_ARC_CHARS)}"
                )

        if self.recent_digests:
            lines.append("【近况详摘】")
            for digest in self.recent_digests:
                lines.append(
                    f"- 第{digest.episode_no}话：{_clip(digest.one_line_hook, MAX_DIGEST_CHARS)}"
                    f"｜{_clip(digest.summary, MAX_DIGEST_CHARS)}"
                )

        if self.open_threads:
            lines.append("【未回收伏笔】")
            for thread in self.open_threads:
                lines.append(
                    f"- {thread.thread_id} {thread.title}：{_clip(thread.description, MAX_THREAD_CHARS)}"
                )

        if self.recalled_digests:
            lines.append("【远期召回】")
            for digest in self.recalled_digests:
                lines.append(
                    f"- 第{digest.episode_no}话：{_clip(digest.one_line_hook, MAX_DIGEST_CHARS)}"
                    f"｜{_clip(digest.summary, MAX_DIGEST_CHARS)}"
                )

        return "\n".join(lines)


class StoryBibleManager:
    """单部漫画的 StoryBible v2 记忆体管理。"""

    def __init__(
        self,
        series_dir: str | Path,
        *,
        series_id: str = "",
        series_title: str = "",
        arc_summary_compressor: ArcSummaryCompressor | None = None,
        arc_summary_interval: int = 10,
        recent_digest_window: int = 3,
        recall_limit: int = 2,
        max_summary_chars: int = 600,
    ) -> None:
        self.series_dir = Path(series_dir).expanduser()
        self.series_id = series_id
        self.series_title = series_title
        self.arc_summary_compressor = arc_summary_compressor
        self.arc_summary_interval = arc_summary_interval
        self.recent_digest_window = recent_digest_window
        self.recall_limit = recall_limit
        self.max_summary_chars = max_summary_chars
        self._bible: StoryBibleV2 | None = None

    # ------------------------------------------------------------------
    # 基础读写
    # ------------------------------------------------------------------
    @property
    def story_bible_path(self) -> Path:
        return self.series_dir / "story_bible.json"

    @property
    def snapshot_dir(self) -> Path:
        return self.series_dir / "bible_snapshots"

    @property
    def bible(self) -> StoryBibleV2:
        """当前 live 记忆体（首次访问时从磁盘读取并缓存）。"""
        if self._bible is None:
            self._bible = self._read_bible()
        return self._bible

    def _read_bible(self) -> StoryBibleV2:
        path = self.story_bible_path
        if path.is_file():
            return StoryBibleV2.model_validate_json(path.read_text(encoding="utf-8"))
        return StoryBibleV2(series_id=self.series_id, series_title=self.series_title)

    def reload(self) -> StoryBibleV2:
        """丢弃缓存并重新从磁盘读取 live 记忆体。"""
        self._bible = self._read_bible()
        return self._bible

    def save_bible(self, bible: StoryBibleV2 | None = None) -> Path:
        """写回 live 记忆体（``story_bible.json``）。"""
        target = bible if bible is not None else self.bible
        if not target.series_id:
            target.series_id = self.series_id
        if not target.series_title:
            target.series_title = self.series_title
        save_model(target, self.story_bible_path)
        self._bible = target
        return self.story_bible_path

    # ------------------------------------------------------------------
    # Layer 1：人物实体图谱
    # ------------------------------------------------------------------
    def build_character_reference(self, episode_no: int | None = None) -> list[CharacterReference]:
        """生成 Stage 3 视觉抽取的角色参考名单。

        传入 ``episode_no`` 时按该话的时间旅行基线取角色，避免把未来才登场的角色带进识图。
        """
        bible = self.resolve_base_bible(episode_no)[0] if episode_no is not None else self.bible
        return [
            CharacterReference(
                name=character.name,
                aliases=tuple(character.aliases),
                visual_traits=character.visual_traits,
                current_status=character.current_status,
            )
            for character in bible.characters
        ]

    # ------------------------------------------------------------------
    # Layer 4：按话只读快照
    # ------------------------------------------------------------------
    def snapshot_path(self, episode_no: int) -> Path:
        return self.snapshot_dir / f"ep_{episode_no:03d}.json"

    def save_snapshot(self, episode_no: int, bible: StoryBibleV2) -> Path:
        """落盘该话的只读快照 ``bible_snapshots/ep_{no:03d}.json``。"""
        snapshot = bible.model_copy(deep=True)
        if episode_no not in snapshot.snapshots_ep_nos:
            snapshot.snapshots_ep_nos.append(episode_no)
            snapshot.snapshots_ep_nos.sort()
        path = self.snapshot_path(episode_no)
        save_model(snapshot, path)
        return path

    def load_snapshot(self, episode_no: int) -> StoryBibleV2 | None:
        """读取某话快照；不存在返回 ``None``。"""
        path = self.snapshot_path(episode_no)
        if not path.is_file():
            return None
        return StoryBibleV2.model_validate_json(path.read_text(encoding="utf-8"))

    def resolve_base_bible(self, episode_no: int) -> tuple[StoryBibleV2, str]:
        """解析第 ``episode_no`` 话的基线记忆体。

        铁律：强制回退读取 ``ep_{K-1}`` 只读快照；快照缺失时退化为「裁剪到该话之前」的 live 记忆体。
        返回 ``(记忆体, 来源标签)``，来源标签形如 ``snapshot:ep_002`` / ``live:filtered``。
        """
        if episode_no > 1:
            snapshot = self.load_snapshot(episode_no - 1)
            if snapshot is not None:
                return snapshot, f"snapshot:ep_{episode_no - 1:03d}"
        return self._filtered_live(episode_no), "live:filtered"

    def _filtered_live(self, episode_no: int) -> StoryBibleV2:
        """把 live 记忆体裁剪到「第 ``episode_no`` 话之前」的状态，防止剧透倒灌。"""
        live = self.bible.model_copy(deep=True)
        live.episodes_index = [d for d in live.episodes_index if d.episode_no < episode_no]
        live.arc_summaries = [a for a in live.arc_summaries if a.ep_range[1] < episode_no]
        live.plot_threads = [t for t in live.plot_threads if t.introduced_ep < episode_no]
        live.characters = [c for c in live.characters if c.first_seen_ep < episode_no]
        live.snapshots_ep_nos = [n for n in live.snapshots_ep_nos if n < episode_no]
        return live

    # ------------------------------------------------------------------
    # Stage 4 上下文组装
    # ------------------------------------------------------------------
    def build_stage4_context(
        self,
        episode_no: int,
        *,
        vision_keywords: Sequence[str] = (),
    ) -> Stage4Context:
        """组装 Stage 4 撰稿所需的紧凑上下文（Token 预算恒定）。"""
        base, source = self.resolve_base_bible(episode_no)
        window = max(self.recent_digest_window, 0)
        recent = list(base.episodes_index[-window:]) if window else []
        excluded = {digest.episode_no for digest in recent} | {episode_no}
        open_threads = [thread for thread in base.plot_threads if thread.status == "open"]
        recalled = self._recall_digests(base, vision_keywords, exclude=excluded, limit=self.recall_limit)
        return Stage4Context(
            episode_no=episode_no,
            base_source=source,
            characters=list(base.characters),
            arc_summaries=list(base.arc_summaries),
            recent_digests=recent,
            open_threads=open_threads,
            recalled_digests=recalled,
        )

    def _recall_digests(
        self,
        base: StoryBibleV2,
        keywords: Sequence[str],
        *,
        exclude: set[int],
        limit: int,
    ) -> list[EpisodeDigest]:
        """按当前话视觉关键词召回远期历史详摘（至多 ``limit`` 条）。"""
        normalized = [keyword.strip().lower() for keyword in keywords if keyword and keyword.strip()]
        if not normalized or limit <= 0:
            return []
        scored: list[tuple[int, int, EpisodeDigest]] = []
        for digest in base.episodes_index:
            if digest.episode_no in exclude:
                continue
            blob = " ".join(
                [digest.one_line_hook, digest.summary, *digest.keywords, *digest.characters_present]
            ).lower()
            score = sum(1 for keyword in normalized if keyword in blob)
            if score > 0:
                scored.append((score, digest.episode_no, digest))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        return [digest for _score, _ep_no, digest in scored[:limit]]

    # ------------------------------------------------------------------
    # 产出合并与快照落盘
    # ------------------------------------------------------------------
    def apply_episode_result(
        self,
        script: NarrationScript,
        *,
        base: StoryBibleV2 | None = None,
        arc_summary: ArcSummary | None = None,
    ) -> StoryBibleV2:
        """合并第 K 话的撰稿产出，返回「截至第 K 话」的只读快照内容。

        快照由 ``ep_{K-1}`` 基线 + 本话产出推导，**绝不会**把第 K+1 话之后的设定倒灌进来。
        ``arc_summary`` 用于传入「已由百炼压缩好的篇章摘要」，避免本地确定性压缩占位。
        """
        episode_no = script.episode_digest.episode_no
        base_bible = base if base is not None else self.resolve_base_bible(episode_no)[0]

        as_of = base_bible.model_copy(deep=True)
        self._merge_characters(as_of, script.character_updates, episode_no)
        self._merge_threads(as_of, script.new_plot_threads, script.resolved_thread_ids, episode_no)
        self._merge_digest(as_of, script.episode_digest)

        arc = arc_summary if arc_summary is not None else self._build_arc_summary(as_of, episode_no)
        if arc is not None:
            self._append_arc(as_of, arc)

        self.save_snapshot(episode_no, as_of)

        live = self.bible
        self._merge_characters(live, script.character_updates, episode_no)
        self._merge_threads(live, script.new_plot_threads, script.resolved_thread_ids, episode_no)
        self._merge_digest(live, script.episode_digest)
        if arc is not None:
            self._append_arc(live, arc)
        if episode_no not in live.snapshots_ep_nos:
            live.snapshots_ep_nos.append(episode_no)
            live.snapshots_ep_nos.sort()
        self.save_bible(live)
        return as_of

    def _merge_characters(
        self,
        bible: StoryBibleV2,
        updates: Sequence[CharacterProfile],
        episode_no: int,
    ) -> None:
        index = {character.name: character for character in bible.characters}

        def _find(update: CharacterProfile) -> CharacterProfile | None:
            """按名字或别名找同一个人。

            模型可能把角色从描述性称呼改成正式名（“黑发橙眼少年” → “Bam”），
            只按名字匹配会把同一个人拆成两条档案，Layer 1 角色表就会出现重名。
            """
            existing = index.get(update.name)
            if existing is not None:
                return existing
            new_aliases = {alias for alias in update.aliases if alias}
            for character in bible.characters:
                if update.name and update.name in character.aliases:
                    return character
                if character.name and character.name in new_aliases:
                    return character
                if new_aliases & {alias for alias in character.aliases if alias}:
                    return character
            return None

        for update in updates:
            existing = _find(update)
            if existing is None:
                profile = update.model_copy(deep=True)
                if profile.first_seen_ep <= 0:
                    profile.first_seen_ep = episode_no
                profile.last_seen_ep = episode_no
                bible.characters.append(profile)
                index[profile.name] = profile
                continue
            aliases = [*existing.aliases, *update.aliases, update.name]
            existing.aliases = list(
                dict.fromkeys(alias for alias in aliases if alias and alias != existing.name)
            )
            if update.visual_traits:
                existing.visual_traits = update.visual_traits
            if update.current_status:
                existing.current_status = update.current_status
            if update.first_seen_ep > 0:
                existing.first_seen_ep = min(existing.first_seen_ep, update.first_seen_ep)
            existing.last_seen_ep = episode_no

    def _merge_threads(
        self,
        bible: StoryBibleV2,
        new_threads: Sequence[PlotThread],
        resolved_ids: Sequence[str],
        episode_no: int,
    ) -> None:
        index = {thread.thread_id: thread for thread in bible.plot_threads}
        for thread in new_threads:
            existing = index.get(thread.thread_id)
            if existing is None:
                item = thread.model_copy(deep=True)
                if item.introduced_ep <= 0:
                    item.introduced_ep = episode_no
                bible.plot_threads.append(item)
                index[item.thread_id] = item
                continue
            if thread.title:
                existing.title = thread.title
            if thread.description:
                existing.description = thread.description
            if thread.status == "resolved":
                existing.status = "resolved"
                existing.resolved_ep = thread.resolved_ep or episode_no
        for thread_id in resolved_ids:
            thread = index.get(thread_id)
            if thread is None:
                thread = PlotThread(
                    thread_id=thread_id,
                    title=thread_id,
                    description="",
                    introduced_ep=episode_no,
                    status="resolved",
                    resolved_ep=episode_no,
                )
                bible.plot_threads.append(thread)
                index[thread_id] = thread
                continue
            thread.status = "resolved"
            thread.resolved_ep = thread.resolved_ep or episode_no

    def _merge_digest(self, bible: StoryBibleV2, digest: EpisodeDigest) -> None:
        bible.episodes_index = [
            item for item in bible.episodes_index if item.episode_no != digest.episode_no
        ]
        bible.episodes_index.append(digest.model_copy(deep=True))
        bible.episodes_index.sort(key=lambda item: item.episode_no)

    # ------------------------------------------------------------------
    # Layer 2：每 N 话篇章归档
    # ------------------------------------------------------------------
    def maybe_archive_arc(self, episode_no: int) -> ArcSummary | None:
        """每满 N 话（默认 10）归档一条篇章摘要；同区间已归档则不重复写入。"""
        arc = self._build_arc_summary(self.bible, episode_no)
        if arc is None:
            return None
        if any(tuple(item.ep_range) == tuple(arc.ep_range) for item in self.bible.arc_summaries):
            return None
        self._append_arc(self.bible, arc)
        self.save_bible(self.bible)
        return arc

    def build_arc_raw_text(
        self,
        episode_no: int,
        *,
        extra_digest: EpisodeDigest | None = None,
    ) -> str | None:
        """构造待归档的篇章原文（未压缩）。

        非归档话次（未满 N 话）或窗口内没有任何详摘时返回 ``None``。
        ``extra_digest`` 用于把「本话刚产出、尚未写进 live 记忆体」的详摘并入窗口。
        """
        if self.arc_summary_interval <= 0 or episode_no % self.arc_summary_interval != 0:
            return None
        start = episode_no - self.arc_summary_interval + 1
        window = [item for item in self.bible.episodes_index if start <= item.episode_no <= episode_no]
        if extra_digest is not None and extra_digest.episode_no == episode_no:
            window = [item for item in window if item.episode_no != extra_digest.episode_no]
            window.append(extra_digest)
            window.sort(key=lambda item: item.episode_no)
        if not window:
            return None
        return "\n".join(
            f"第{item.episode_no}话：{item.one_line_hook}｜{item.summary}" for item in window
        )

    def _build_arc_summary(self, bible: StoryBibleV2, episode_no: int) -> ArcSummary | None:
        """构造归档摘要对象（不落盘）。"""
        if self.arc_summary_interval <= 0 or episode_no % self.arc_summary_interval != 0:
            return None
        start = episode_no - self.arc_summary_interval + 1
        window = [item for item in bible.episodes_index if start <= item.episode_no <= episode_no]
        if not window:
            return None
        raw = "\n".join(
            f"第{item.episode_no}话：{item.one_line_hook}｜{item.summary}" for item in window
        )
        return ArcSummary(
            arc_name=f"第{start}-{episode_no}话篇章",
            ep_range=(start, episode_no),
            summary=self._compress(raw),
        )

    def _compress(self, raw: str) -> str:
        """压缩待归档原文：优先使用注入的百炼压缩器，否则本地确定性压缩。"""
        if self.arc_summary_compressor is not None:
            return self.arc_summary_compressor(raw)
        collapsed = " ".join(line.strip() for line in raw.splitlines() if line.strip())
        return _clip(collapsed, self.max_summary_chars)

    def _append_arc(self, bible: StoryBibleV2, arc: ArcSummary) -> bool:
        """追加篇章摘要；区间重复时跳过。"""
        if any(tuple(item.ep_range) == tuple(arc.ep_range) for item in bible.arc_summaries):
            return False
        bible.arc_summaries.append(arc)
        return True
