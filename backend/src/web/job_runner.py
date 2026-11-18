"""Web 控制台的后台任务调度器与事件总线（规范第 5 节）。

调度模型对齐规范第 0 节铁律三与第 1.1 节并发法则：

* **跨作品并行**：不同 ``series_id`` 的任务互不阻塞；
* **部内 Stage 4 串行**：同一部漫画的 Stage 4 按 ``episode_no`` 升序排队，
  排队期间持部级互斥锁，保证 ``story_bible.json`` 写入无竞态，
  重跑第 K 话时强制回退读取 ``bible_snapshots/ep_{K-1}.json``（防剧透）；
* **其余阶段同部内并发**：Stage 1~3 与 Stage 5~7 在同一部内可并发执行。

事件总线为 ``GET /api/episodes/{ep_id}/events``（SSE）提供实时日志与阶段状态推送。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from core.config import (
    AppConfig,
    load_series_config,
    merge_pronunciation_overrides,
    merge_series_tts,
    merge_voice_cast,
)
from core.manifest import STAGE_DIR_NAMES, ManifestManager
from core.memory import StoryBibleManager
from core.schemas import NarrationScript, load_model
from stages import (
    s0_catalog,
    s1_crawler,
    s2_segmenter,
    s3_vision,
    s4_scriptwriter,
    s5_tts,
    s6_timeline,
    s7_renderer,
)

#: 阶段元数据（与前端 ``createInitialStages`` 的 code/label/phase 对齐）。
STAGE_DEFS: tuple[dict[str, Any], ...] = (
    {"id": "s0_catalog", "code": "S0", "label": "目录索引", "phase": 1},
    {"id": "s1_crawler", "code": "S1", "label": "本地下载", "phase": 1},
    {"id": "s2_segmenter", "code": "S2", "label": "智能切图", "phase": 1},
    {"id": "s3_vision", "code": "S3", "label": "百炼识图", "phase": 1},
    {"id": "s4_scriptwriter", "code": "S4", "label": "解说撰稿", "phase": 1},
    {"id": "s5_tts", "code": "S5", "label": "配音", "phase": 2},
    {"id": "s6_timeline", "code": "S6", "label": "时间轴字幕", "phase": 2},
    {"id": "s7_renderer", "code": "S7", "label": "FFmpeg合成", "phase": 2},
)

STAGE_IDS: tuple[str, ...] = tuple(str(item["id"]) for item in STAGE_DEFS)

#: 只跑到 Stage 4 的「第一步」模式终点。
PHASE1_LAST_STAGE = "s4_scriptwriter"

#: 全流程模式的终点。
FULL_LAST_STAGE = "s7_renderer"

#: Stage 4 排队等待上限（秒），避免话次永远等不到前序而挂死。
STAGE4_QUEUE_TIMEOUT_S = 6 * 3600.0


class JobError(RuntimeError):
    """后台任务执行失败。"""


@dataclass
class EpisodeState:
    """一话的执行状态（供前端轮询与 SSE 推送）。"""

    episode_id: str
    series_id: str
    episode_no: int
    job_id: str = ""
    status: str = "idle"  # idle | queued | running | done | failed
    current_stage: str = ""
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "series_id": self.series_id,
            "episode_no": self.episode_no,
            "job_id": self.job_id,
            "status": self.status,
            "current_stage": self.current_stage,
            "stages": self.stages,
            "logs": list(self.logs),
            "error": self.error,
        }


class EventBus:
    """按话次分发事件的发布/订阅总线。"""

    def __init__(self, *, history_limit: int = 200) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._history_limit = history_limit

    def publish(self, episode_id: str, event: dict[str, Any]) -> None:
        """广播事件，并留一份历史供新订阅者补发。"""
        payload = dict(event)
        payload.setdefault("timestamp", time.time())
        payload.setdefault("episode_id", episode_id)
        history = self._history.setdefault(episode_id, [])
        history.append(payload)
        if len(history) > self._history_limit:
            del history[: len(history) - self._history_limit]
        for queue in list(self._subscribers.get(episode_id, ())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:  # pragma: no cover - 队列满时丢弃最旧事件
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except Exception:  # noqa: BLE001 - 极端情况下忽略
                    pass

    def history(self, episode_id: str) -> list[dict[str, Any]]:
        return list(self._history.get(episode_id, ()))

    def subscribe(self, episode_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._subscribers.setdefault(episode_id, set()).add(queue)
        return queue

    def unsubscribe(self, episode_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        subscribers = self._subscribers.get(episode_id)
        if subscribers is not None:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(episode_id, None)

    async def stream(self, episode_id: str) -> AsyncIterator[dict[str, Any]]:
        """订阅一路事件流（先补发历史，再持续推送）。"""
        queue = self.subscribe(episode_id)
        try:
            for event in self.history(episode_id):
                yield event
            while True:
                event = await queue.get()
                yield event
        finally:
            self.unsubscribe(episode_id, queue)


class _SeriesGate:
    """部级 Stage 4 排队闸门：按 ``episode_no`` 升序、互斥执行。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition()
        self._pending: set[int] = set()
        self._finished: set[int] = set()

    def enqueue(self, episode_no: int) -> None:
        self._pending.add(episode_no)

    async def run(self, episode_no: int, factory: Callable[[], Any]) -> Any:
        """按升序等待轮到本话，再独占执行 Stage 4。"""
        self.enqueue(episode_no)
        # 让出一次控制权：同一批并发提交的话次能全部完成登记，
        # 否则先到的话次会以为自己是队首而抢先执行，破坏「按 episode_no 升序」。
        await asyncio.sleep(0)
        deadline = time.monotonic() + STAGE4_QUEUE_TIMEOUT_S
        async with self._condition:
            while True:
                head = min(self._pending) if self._pending else episode_no
                if head == episode_no:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise JobError("等待 Stage 4 排队超时")
                await asyncio.wait_for(self._condition.wait(), timeout=remaining)
        try:
            async with self._lock:
                return await factory()
        finally:
            async with self._condition:
                self._pending.discard(episode_no)
                self._finished.add(episode_no)
                self._condition.notify_all()


class JobRunner:
    """后台流水线调度器。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        client_factory: Callable[[], Any] | None = None,
        tts_factory: Callable[[], Any] | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.config = config
        self.client_factory = client_factory
        self.tts_factory = tts_factory
        self.bus = bus if bus is not None else EventBus()
        self.states: dict[str, EpisodeState] = {}
        self._gates: dict[str, _SeriesGate] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    @property
    def registry(self) -> s0_catalog.CatalogRegistry:
        return s0_catalog.CatalogRegistry(self.config.workspace_root())

    def series_dir(self, series_id: str) -> Path:
        return self.registry.series_dir(series_id)

    def episode_id(self, series_id: str, episode_no: int) -> str:
        return f"{series_id}_ep_{episode_no:03d}"

    def gate(self, series_id: str) -> _SeriesGate:
        gate = self._gates.get(series_id)
        if gate is None:
            gate = _SeriesGate()
            self._gates[series_id] = gate
        return gate

    def state(self, episode_id: str) -> EpisodeState | None:
        return self.states.get(episode_id)

    def state_of(self, series_id: str, episode_no: int) -> EpisodeState:
        episode_id = self.episode_id(series_id, episode_no)
        state = self.states.get(episode_id)
        if state is None:
            state = EpisodeState(
                episode_id=episode_id, series_id=series_id, episode_no=episode_no
            )
            self.states[episode_id] = state
        return state

    # ------------------------------------------------------------------
    # 提交任务
    # ------------------------------------------------------------------
    async def submit_episode(
        self,
        series_id: str,
        episode_no: int,
        *,
        from_stage: str = "s1_crawler",
        mode: str = "phase1_only",
    ) -> dict[str, Any]:
        """提交一话的重跑任务（后台执行）。"""
        if from_stage not in STAGE_IDS:
            raise JobError(f"未知阶段：{from_stage}")
        state = self.state_of(series_id, episode_no)
        state.job_id = uuid.uuid4().hex[:12]
        state.status = "queued"
        state.current_stage = ""
        state.error = ""
        state.finished_at = 0.0
        state.stages = {}
        self._log(state, f"[调度器] 已入队：{from_stage} 起，模式 {mode}")
        self._publish_state(state)

        task = asyncio.create_task(
            self._run_episode(series_id, episode_no, from_stage=from_stage, mode=mode)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return {
            "job_id": state.job_id,
            "episode_id": state.episode_id,
            "series_id": series_id,
            "episode_no": episode_no,
            "status": state.status,
        }

    async def submit_batch(
        self,
        items: list[dict[str, Any]],
        *,
        mode: str = "phase1_only",
    ) -> list[dict[str, Any]]:
        """按作品分组下发批量任务（跨作品并发，同作品内部保持入队顺序）。"""
        grouped: dict[str, list[int]] = {}
        modes: dict[str, str] = {}
        from_stages: dict[str, str] = {}
        for item in items:
            series_id = str(item["series_id"])
            episode_no = int(item["episode_no"])
            grouped.setdefault(series_id, []).append(episode_no)
            modes[series_id] = str(item.get("mode") or mode)
            from_stages[series_id] = str(item.get("from_stage") or "s1_crawler")

        submitted: list[dict[str, Any]] = []
        for series_id, episode_nos in grouped.items():
            for episode_no in sorted(episode_nos):
                submitted.append(
                    await self.submit_episode(
                        series_id,
                        episode_no,
                        from_stage=from_stages[series_id],
                        mode=modes[series_id],
                    )
                )
        return submitted

    async def wait_all(self) -> None:
        """等待所有后台任务结束（测试用）。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    async def _run_episode(
        self,
        series_id: str,
        episode_no: int,
        *,
        from_stage: str,
        mode: str,
    ) -> None:
        state = self.state_of(series_id, episode_no)
        state.status = "running"
        state.started_at = time.time()
        self._publish_state(state)

        start_index = STAGE_IDS.index(from_stage)
        last_stage = PHASE1_LAST_STAGE if mode == "phase1_only" else FULL_LAST_STAGE
        end_index = STAGE_IDS.index(last_stage)
        if start_index > end_index:
            # 「仅第一步」模式下请求 Stage 5~7：自动提升为全流程并留下记录。
            self._log(state, f"[调度器] {from_stage} 超出「仅第一步」范围，自动提升为全流程模式")
            end_index = STAGE_IDS.index(FULL_LAST_STAGE)

        catalog = self.registry.load_catalog(series_id)
        if catalog is None:
            raise JobError(f"作品未登记：{series_id}")
        episode = next(
            (item for item in catalog.episodes if item.episode_no == episode_no), None
        )
        if episode is None:
            raise JobError(f"目录中没有第 {episode_no} 话：{series_id}")

        series_dir = self.series_dir(series_id)
        manager = StoryBibleManager(
            series_dir, series_id=series_id, series_title=catalog.series_title
        )
        series_config = load_series_config(series_dir)
        vision_model = series_config.model_overrides.get("vision", self.config.models.vision)
        script_model = series_config.model_overrides.get("script", self.config.models.script)
        # 与 cli.py 同口径：4A 故事反写走 story_model（默认 qwen3.8-max），
        # 作品级覆盖缺省时回落到全局配置，再缺省回落到 script_model。
        story_model = (
            series_config.model_overrides.get("story", self.config.models.story_model)
            or script_model
        )

        llm_client: Any = None
        tts_client: Any = None
        try:
            llm_client = (self.client_factory or (lambda: _default_llm(self.config)))()
            if end_index >= STAGE_IDS.index("s5_tts"):
                tts_client = self._make_tts_client()
            for index in range(start_index, end_index + 1):
                stage = STAGE_IDS[index]
                if stage == "s0_catalog":
                    continue
                await self._run_stage(
                    state,
                    stage=stage,
                    series_id=series_id,
                    series_dir=series_dir,
                    episode=episode,
                    manager=manager,
                    llm_client=llm_client,
                    tts_client=tts_client,
                    vision_model=vision_model,
                    script_model=script_model,
                    story_model=story_model,
                    series_title=catalog.series_title,
                )
            state.status = "done"
            self._log(state, "[完成] 本轮流水线执行完毕")
        except Exception as exc:  # noqa: BLE001 - 后台任务需要把异常转成状态
            state.status = "failed"
            state.error = str(exc)
            self._log(state, f"[失败] {exc}")
        finally:
            state.finished_at = time.time()
            for client in (llm_client, tts_client):
                close = getattr(client, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:  # noqa: BLE001 - 关闭失败不影响结果
                        pass
            self._publish_state(state)

    async def _run_stage(
        self,
        state: EpisodeState,
        *,
        stage: str,
        series_id: str,
        series_dir: Path,
        episode: Any,
        manager: StoryBibleManager,
        llm_client: Any,
        tts_client: Any,
        vision_model: str,
        script_model: str,
        story_model: str,
        series_title: str,
    ) -> None:
        state.current_stage = stage
        state.stages[stage] = {"status": "running", "started_at": time.time()}
        self._publish_state(state)
        self._log(state, f"[执行中] 正在运行 {stage} ...")
        started = time.time()
        manifest = ManifestManager(series_dir, series_id=series_id)
        # 阶段开始即落盘：进程重启后仍能看到「跑到哪了」，而不是一片待执行。
        try:
            manifest.mark_running(episode.episode_no, stage)
        except Exception:  # noqa: BLE001 - manifest 落盘失败不应阻断流水线
            pass

        try:
            if stage == "s1_crawler":
                await self._stage_download(state, series_dir, episode)
            elif stage == "s2_segmenter":
                await asyncio.to_thread(
                    s2_segmenter.segment_episode,
                    s1_crawler.raw_dir_for(series_dir, episode.episode_no),
                    s2_segmenter.panels_dir_for(series_dir, episode.episode_no),
                    episode_id=episode.episode_id,
                )
            elif stage == "s3_vision":
                characters = manager.build_character_reference(episode.episode_no)
                await s3_vision.run_stage3(
                    series_dir,
                    episode.episode_no,
                    llm_client,
                    model=vision_model,
                    characters=characters,
                    series_title=series_title,
                    concurrency=self.config.concurrency.stage,
                    on_panel_error="skip",
                )
            elif stage == "s4_scriptwriter":
                await self._stage_script(
                    state,
                    series_dir,
                    episode,
                    manager,
                    llm_client,
                    script_model,
                    story_model,
                    series_title,
                )
            elif stage == "s5_tts":
                await self._stage_tts(state, series_dir, episode, tts_client)
            elif stage == "s6_timeline":
                await asyncio.to_thread(
                    s6_timeline.run_stage6,
                    series_dir,
                    episode.episode_no,
                    episode_id=episode.episode_id,
                    width=self.config.video.width,
                    height=self.config.video.height,
                    fps=self.config.video.fps,
                )
            elif stage == "s7_renderer":
                await asyncio.to_thread(
                    s7_renderer.render_episode,
                    series_dir,
                    episode.episode_no,
                    episode_id=episode.episode_id,
                    ffmpeg_bin=self.config.render.ffmpeg_bin,
                    config=self.config,
                    title=series_title,
                )
            else:  # pragma: no cover - STAGE_IDS 已穷举
                raise JobError(f"未知阶段：{stage}")
        except Exception as exc:  # noqa: BLE001 - 阶段失败必须落到状态里
            # 关键：把该阶段标成 failed。否则它会永远停在 running，
            # 界面既没有耗时（显示「待执行」）也看不出失败原因。
            state.stages[stage] = {
                "status": "failed",
                "error": str(exc),
                "failed_at": time.time(),
            }
            try:
                manifest.mark_failed(episode.episode_no, stage, error=str(exc))
            except Exception:  # noqa: BLE001
                pass
            self._publish_state(state)
            raise

        elapsed = round(time.time() - started, 3)
        state.stages[stage] = {
            "status": "done",
            "duration_sec": elapsed,
            "finished_at": time.time(),
        }
        try:
            manifest.mark_done(
                episode.episode_no, stage, artifact=self._stage_artifact(stage, series_dir, episode)
            )
        except Exception:  # noqa: BLE001
            pass
        self._log(state, f"[完成] {stage} 耗时 {elapsed}s")
        self._publish_state(state)

    @staticmethod
    def _stage_artifact(stage: str, series_dir: Path, episode: Any) -> str:
        """给出该阶段的关键产物路径（写进 manifest 供界面展示）。"""
        episode_no = int(episode.episode_no)
        episode_dir = series_dir / "episodes" / f"ep_{episode_no:03d}"
        if stage == "s1_crawler":
            return str(s1_crawler.raw_dir_for(series_dir, episode_no))
        if stage == "s2_segmenter":
            return str(s2_segmenter.panels_dir_for(series_dir, episode_no))
        if stage == "s7_renderer":
            return str(episode_dir / STAGE_DIR_NAMES[stage] / f"{episode.episode_id}.mp4")
        name = {
            "s3_vision": "vision.json",
            "s4_scriptwriter": "script.json",
            "s5_tts": "audio_manifest.json",
            "s6_timeline": "timeline.json",
        }.get(stage, "")
        return str(episode_dir / STAGE_DIR_NAMES[stage] / name) if name else ""

    # ------------------------------------------------------------------
    # 各阶段的具体实现
    # ------------------------------------------------------------------
    async def _stage_download(self, state: EpisodeState, series_dir: Path, episode: Any) -> None:
        from core.http_client import build_client

        raw_dir = s1_crawler.raw_dir_for(series_dir, episode.episode_no)
        async with build_client() as client:
            result = await s1_crawler.crawl_episode(
                client,
                episode,
                raw_dir,
                concurrency=self.config.concurrency.stage,
            )
        if result.failures:
            raise JobError(
                f"Stage 1 有 {len(result.failures)} 张图片下载失败：{result.failures[0]}"
            )
        self._log(state, f"[Stage 1] 新增 {len(result.saved)} 张，跳过 {len(result.skipped)} 张")

    async def _stage_script(
        self,
        state: EpisodeState,
        series_dir: Path,
        episode: Any,
        manager: StoryBibleManager,
        llm_client: Any,
        script_model: str,
        story_model: str,
        series_title: str,
    ) -> None:
        """Stage 4：必须按话升序 + 部级互斥锁执行（防剧透与写入竞态）。"""
        arc_compressor = s4_scriptwriter.build_arc_compressor(
            llm_client, model=script_model
        )

        async def _run() -> None:
            await s4_scriptwriter.run_stage4(
                series_dir,
                episode.episode_no,
                llm_client,
                model=script_model,
                story_model=story_model,
                episode_id=episode.episode_id,
                series_title=series_title,
                manager=manager,
                arc_compressor=arc_compressor,
            )

        await self.gate(state.series_id).run(episode.episode_no, _run)

    async def _stage_tts(
        self, state: EpisodeState, series_dir: Path, episode: Any, tts_client: Any
    ) -> None:
        series_config = load_series_config(series_dir)
        overrides = merge_pronunciation_overrides(
            dict(self.config.pronunciation_overrides),
            dict(series_config.pronunciation_overrides),
        )
        # Patch v3.1 Patch 11 A2/A3：作品级角色音色覆盖全局。
        voice_cast = merge_voice_cast(
            self.config.tts.voice_cast, series_config.voice_cast
        )
        # Patch v3.2 Patch 13 B1/B2：作品级配音模板（讲解者 / 通用男女声 / 角色专属）
        # + 角色性别表（未指定角色时按性别兜底）。
        series_tts = merge_series_tts(self.config.tts, series_config.tts)
        characters = s5_tts.load_bible_characters(series_dir)
        script_path = (
            series_dir
            / "episodes"
            / f"ep_{episode.episode_no:03d}"
            / "04_script"
            / "script.json"
        )
        script = load_model(NarrationScript, script_path) if script_path.is_file() else None
        await s5_tts.run_stage5(
            series_dir,
            episode.episode_no,
            tts_client,
            script=script,
            overrides=overrides,
            default_voice=self.config.tts.default_voice,
            voice_cast=voice_cast,
            series_tts=series_tts,
            characters=characters,
            concurrency=self.config.concurrency.stage,
            ffmpeg_bin=self.config.render.ffmpeg_bin,
            tts_model=self.config.models.tts,
        )

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _make_tts_client(self) -> Any:
        if self.tts_factory is not None:
            return self.tts_factory()
        # Patch v3.4 Patch 16：按 ``config.models.tts`` 路由到 MiniMax / 百炼 / Edge，
        # 并统一过跨模型音色兜底（避免把百炼音色发给 MiniMax 导致 2013）。
        from core.config import create_tts_client

        return create_tts_client(self.config)

    def _log(self, state: EpisodeState, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        state.logs.append(line)
        if len(state.logs) > 500:
            del state.logs[: len(state.logs) - 500]
        self.bus.publish(
            state.episode_id,
            {"type": "log", "message": line, "stage": state.current_stage},
        )

    def _publish_state(self, state: EpisodeState) -> None:
        self.bus.publish(
            state.episode_id,
            {
                "type": "state",
                "status": state.status,
                "current_stage": state.current_stage,
                "stages": state.stages,
                "error": state.error,
            },
        )


def _default_llm(config: AppConfig) -> Any:
    from providers.llm_bailian import BailianClient

    return BailianClient(config=config)
