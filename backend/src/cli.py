"""``w2v`` 命令行入口（Phase 1：``download`` 与 ``understand``）。

规范第 6 节 M6-T1a：实现 ``w2v download`` 与 ``w2v understand``，
**多部漫画并发** + **部内 Stage 4 按话升序加部级互斥锁串行**（规范第 0 节铁律三、第 1.1 节调度器并发法则）。

命令形态::

    w2v download   --series <目录页URL|本地目录> [--series ...] [--episodes 1-3,5]
    w2v understand --series <series_id> [--series ...] [--episodes 1-5]

``download`` 执行 Stage 0（登记目录）+ Stage 1（下载原图到 ``01_raw/``）；
``understand`` 执行 Stage 2（切图）+ Stage 3（识图）+ Stage 4（撰稿并更新记忆）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from core.config import (
    AppConfig,
    ConfigError,
    WorkspaceConfig,
    load_config,
    load_env_file,
    load_series_config,
)
from core.http_client import build_client, local_path_from_url
from core.manifest import ManifestManager
from core.memory import StoryBibleManager
from core.schemas import (
    EpisodeCatalogItem,
    NarrationScript,
    SeriesCatalog,
    VisionWindowOutput,
    load_model,
)
from providers.llm_base import LLMClient
from providers.llm_bailian import BailianClient
from stages import s0_catalog, s1_crawler, s2_segmenter, s3_vision, s4_scriptwriter

#: 部级互斥锁仓库（规范第 1.1 节：``series_locks: dict[str, asyncio.Lock]``）。
_SERIES_LOCKS: dict[str, asyncio.Lock] = {}

#: 锁所属的事件循环；同一进程内多次 ``asyncio.run`` 时需要重建锁，
#: 否则会抛 ``RuntimeError: ... is bound to a different event loop``。
_SERIES_LOCK_LOOPS: dict[str, asyncio.AbstractEventLoop] = {}

#: 各阶段 manifest 里记录的产物文件名（与 ``core.manifest.STAGE_DIR_NAMES`` 对应）。
_STAGE_ARTIFACT_FILES: dict[str, str] = {
    "s2_segmenter": "",
    "s3_vision": "vision.json",
    "s4_scriptwriter": "script.json",
    "s5_tts": "audio_manifest.json",
    "s6_timeline": "timeline.json",
    "s7_renderer": "{episode_id}.mp4",
}


@contextmanager
def _manifest_stage(
    manager: ManifestManager,
    episode_no: int,
    stage: str,
    *,
    episode_id: str = "",
    artifact_name: str | None = None,
) -> Iterator[None]:
    """把一个阶段包起来，实时把状态写进单话 manifest。

    **为什么 CLI 也要写**：Web 控制台的阶段状态读的就是 manifest；
    若 CLI 产出的章节没有 manifest，界面会对着磁盘上真实存在的成片显示「待执行」。
    manifest 落盘失败不得影响流水线本身，因此所有写入都吞异常。

    ``artifact_name`` 用于同一 stage 产出不同产物的场景（如 ``s4_scriptwriter``
    在 story-draft 模式只产出 ``story_draft.json`` 而非 ``script.json``）；
    不传则按 ``_STAGE_ARTIFACT_FILES`` 的默认产物名记录。
    """
    try:
        manager.mark_running(episode_no, stage)
    except Exception:  # noqa: BLE001 - manifest 是旁路记录，不是主流程依赖
        pass
    try:
        yield
    except Exception as exc:  # noqa: BLE001
        try:
            manager.mark_failed(episode_no, stage, error=str(exc))
        except Exception:  # noqa: BLE001
            pass
        raise
    else:
        artifact = ""
        name = (
            artifact_name
            if artifact_name is not None
            else _STAGE_ARTIFACT_FILES.get(stage, "")
        )
        if name:
            artifact = str(
                manager.stage_dir(episode_no, stage) / name.format(episode_id=episode_id)
            )
        elif stage == "s2_segmenter":
            artifact = str(manager.stage_dir(episode_no, stage))
        try:
            manager.mark_done(episode_no, stage, artifact=artifact or None)
        except Exception:  # noqa: BLE001
            pass


def get_series_lock(series_id: str) -> asyncio.Lock:
    """取得（并惰性创建）某部漫画的 Stage 4 互斥锁。

    锁与创建它的事件循环绑定；换了事件循环就重建一把，
    避免跨事件循环复用同一把锁时抛 ``RuntimeError``。
    """
    loop = asyncio.get_running_loop()
    lock = _SERIES_LOCKS.get(series_id)
    if lock is None or _SERIES_LOCK_LOOPS.get(series_id) is not loop:
        lock = asyncio.Lock()
        _SERIES_LOCKS[series_id] = lock
        _SERIES_LOCK_LOOPS[series_id] = loop
    return lock


def select_episodes(catalog: SeriesCatalog, spec: str = "") -> list[EpisodeCatalogItem]:
    """按 ``1-3,5`` 这样的选择串过滤话次；空串表示全部。"""
    if not spec:
        return list(catalog.episodes)
    wanted = set(s0_catalog.parse_episode_selection(spec))
    return [episode for episode in catalog.episodes if episode.episode_no in wanted]


async def resolve_catalog(source: str) -> SeriesCatalog:
    """把命令行来源解析成目录：本地目录 / ``local://`` URI / 目录页 URL。"""
    if source.startswith("local://"):
        return s0_catalog.import_local_catalog(local_path_from_url(source))
    path = Path(source).expanduser()
    if path.exists():
        return s0_catalog.import_local_catalog(path)
    async with build_client() as client:
        return await s0_catalog.fetch_series_catalog(client, source)


async def download_series(
    source: str,
    *,
    config: AppConfig,
    episodes_spec: str = "",
    concurrency: int = 8,
) -> dict[str, Any]:
    """Stage 0 + Stage 1：登记作品并把原图下载到 ``01_raw/``。"""
    registry = s0_catalog.CatalogRegistry(config.workspace_root())
    catalog = await resolve_catalog(source)
    registry.register(catalog, done_episodes=0)
    registry.save_catalog(catalog)

    episodes = select_episodes(catalog, episodes_spec)
    series_dir = registry.series_dir(catalog.series_id)
    async with build_client() as client:
        results = await s1_crawler.crawl_episodes(
            client, episodes, series_dir, concurrency=concurrency
        )
    saved = sum(len(result.saved) for result in results)
    skipped = sum(len(result.skipped) for result in results)
    failed = sum(len(result.failures) for result in results)
    registry.update_progress(catalog.series_id, len(results))
    return {
        "command": "download",
        "series_id": catalog.series_id,
        "series_title": catalog.series_title,
        "episodes": [result.episode_no for result in results],
        "saved": saved,
        "skipped": skipped,
        "failed": failed,
        "failures": [failure for result in results for failure in result.failures],
    }


async def produce_scripts(
    series_id: str,
    *,
    config: AppConfig,
    episodes_spec: str = "",
    concurrency: int = 4,
    client_factory: Any = None,
    mode: str = "full",
    force_story_markdown: bool = False,
) -> tuple[SeriesCatalog, list[EpisodeCatalogItem], dict[int, Path]]:
    """Stage 2~4：切图 → 识图 → 撰稿（同部 Stage 4 升序串行加锁）。

    返回 ``(目录, 选定话次, {episode_no: script.json 路径})``，
    供 ``understand`` 与 ``run`` 复用。

    ``mode`` 支持三种：

    * ``full``：完整流程（4A 故事反写 + 4B 逐章拆拍）；
    * ``story_draft``：只跑 4A，产出 ``story_draft.json`` / ``story_draft.md``，
      停下等待人工审阅；
    * ``narrate_from_story``：跳过 4A，直接采用磁盘上的
      ``story_draft.md``（人工改定稿）跑 4B。
    """
    registry = s0_catalog.CatalogRegistry(config.workspace_root())
    catalog = registry.load_catalog(series_id)
    if catalog is None:
        raise ConfigError(f"作品未登记：{series_id}（先执行 w2v download）")

    episodes = select_episodes(catalog, episodes_spec)
    episodes.sort(key=lambda episode: episode.episode_no)
    series_dir = registry.series_dir(series_id)
    series_config = load_series_config(series_dir)
    vision_model = series_config.model_overrides.get("vision", config.models.vision)
    script_model = series_config.model_overrides.get("script", config.models.script)
    # Patch v3.0-7：4A 故事编剧用专门的模型（默认 qwen3.8-max），未配置则回退 script 模型。
    story_model = (
        series_config.model_overrides.get("story", config.models.story_model) or script_model
    )

    manager = StoryBibleManager(
        series_dir, series_id=series_id, series_title=catalog.series_title
    )

    factory = client_factory or (lambda: BailianClient(config=config))
    client: LLMClient = factory()
    manifests = ManifestManager(series_dir, series_id=series_id)
    try:
        # Stage 2：CPU 密集，放线程池；同部内并发。
        def _segment(episode: EpisodeCatalogItem) -> int:
            panels_dir = s2_segmenter.panels_dir_for(series_dir, episode.episode_no)
            raw_dir = s1_crawler.raw_dir_for(series_dir, episode.episode_no)
            with _manifest_stage(
                manifests, episode.episode_no, "s2_segmenter", episode_id=episode.episode_id
            ):
                s2_segmenter.segment_episode(
                    raw_dir, panels_dir, episode_id=episode.episode_id
                )
            return episode.episode_no

        await asyncio.gather(*(asyncio.to_thread(_segment, episode) for episode in episodes))

        # Stage 3：视觉抽取，同部内并发。
        async def _vision(episode: EpisodeCatalogItem) -> Path:
            # 角色名单按该话的时间旅行基线取（优先 ep_{K-1} 快照），
            # 既防剧透，也避免把未来才登场的角色带进识图提示词。
            characters = manager.build_character_reference(episode.episode_no)
            with _manifest_stage(
                manifests, episode.episode_no, "s3_vision", episode_id=episode.episode_id
            ):
                return await s3_vision.run_stage3(
                    series_dir,
                    episode.episode_no,
                    client,
                    model=vision_model,
                    characters=characters,
                    series_title=catalog.series_title,
                    concurrency=concurrency,
                    on_panel_error="skip",
                )

        vision_paths = await asyncio.gather(*(_vision(episode) for episode in episodes))
        vision_map = {
            episode.episode_no: path
            for episode, path in zip(episodes, vision_paths, strict=True)
        }

        # Stage 4：撰稿 + 记忆更新。按 episode_no 升序 + 部级互斥锁串行。
        lock = get_series_lock(series_id)
        arc_compressor = s4_scriptwriter.build_arc_compressor(client, model=script_model)

        async def _script(episode: EpisodeCatalogItem) -> Path:
            async with lock:
                vision_path = vision_map.get(episode.episode_no)
                vision = (
                    load_model(VisionWindowOutput, vision_path)
                    if vision_path is not None and Path(vision_path).is_file()
                    else None
                )
                if mode == "story_draft":
                    with _manifest_stage(
                        manifests,
                        episode.episode_no,
                        "s4_scriptwriter",
                        episode_id=episode.episode_id,
                        artifact_name=s4_scriptwriter.STORY_DRAFT_FILENAME,
                    ):
                        _draft, json_path, _md_path = (
                            await s4_scriptwriter.write_story_draft_document(
                                series_dir,
                                episode.episode_no,
                                client,
                                model=story_model,
                                vision=vision,
                                manager=manager,
                                episode_id=episode.episode_id,
                                series_title=catalog.series_title,
                                force=force_story_markdown,
                            )
                        )
                    return json_path

                story_source_markdown: str | None = None
                if mode == "narrate_from_story":
                    markdown_path = (
                        s4_scriptwriter.script_dir_for(series_dir, episode.episode_no)
                        / s4_scriptwriter.STORY_DRAFT_MD_FILENAME
                    )
                    if not markdown_path.is_file():
                        raise ConfigError(
                            f"第 {episode.episode_no} 话还没有故事初稿：{markdown_path}"
                            "（先执行 w2v story-draft 生成初稿并人工审阅）"
                        )
                    story_source_markdown = markdown_path.read_text(encoding="utf-8")

                with _manifest_stage(
                    manifests, episode.episode_no, "s4_scriptwriter", episode_id=episode.episode_id
                ):
                    _script_obj, path = await s4_scriptwriter.run_stage4(
                        series_dir,
                        episode.episode_no,
                        client,
                        model=script_model,
                        episode_id=episode.episode_id,
                        series_title=catalog.series_title,
                        vision=vision,
                        manager=manager,
                        arc_compressor=arc_compressor,
                        story_model=story_model,
                        story_source_markdown=story_source_markdown,
                        require_story_source=mode == "narrate_from_story",
                    )
                return path

        script_paths = await asyncio.gather(*(_script(episode) for episode in episodes))
    finally:
        close = getattr(client, "aclose", None)
        if close is not None:
            await close()

    script_map = {
        episode.episode_no: path for episode, path in zip(episodes, script_paths, strict=True)
    }
    return catalog, episodes, script_map


async def understand_series(
    series_id: str,
    *,
    config: AppConfig,
    episodes_spec: str = "",
    concurrency: int = 4,
    client_factory: Any = None,
) -> dict[str, Any]:
    """Stage 2~4：切图 → 识图 → 撰稿（同部 Stage 4 升序串行加锁）。"""
    catalog, episodes, script_map = await produce_scripts(
        series_id,
        config=config,
        episodes_spec=episodes_spec,
        concurrency=concurrency,
        client_factory=client_factory,
    )
    return {
        "command": "understand",
        "series_id": series_id,
        "series_title": catalog.series_title,
        "episodes": [episode.episode_no for episode in episodes],
        "scripts": [str(path) for path in script_map.values()],
    }


async def run_download(
    sources: list[str],
    *,
    config: AppConfig,
    episodes_spec: str = "",
    concurrency: int = 8,
) -> list[dict[str, Any]]:
    """多部漫画并发下载。"""
    return list(
        await asyncio.gather(
            *(
                download_series(
                    source,
                    config=config,
                    episodes_spec=episodes_spec,
                    concurrency=concurrency,
                )
                for source in sources
            )
        )
    )


async def run_understand(
    series_ids: list[str],
    *,
    config: AppConfig,
    episodes_spec: str = "",
    concurrency: int = 4,
    client_factory: Any = None,
) -> list[dict[str, Any]]:
    """多部漫画并发理解（部内 Stage 4 仍按话升序串行）。"""
    return list(
        await asyncio.gather(
            *(
                understand_series(
                    series_id,
                    config=config,
                    episodes_spec=episodes_spec,
                    concurrency=concurrency,
                    client_factory=client_factory,
                )
                for series_id in series_ids
            )
        )
    )


# ---------------------------------------------------------------------------
# Phase 2：Stage 5~7（配音 / 时间轴 / 成片渲染）
# ---------------------------------------------------------------------------


def _tts_factory(config: AppConfig, voice: str | None = None) -> Any:
    """构造百炼 TTS 客户端工厂（Stage 5 用）。"""

    def factory():
        from providers.tts_qwen import DEFAULT_VOICE, QwenTTS

        return QwenTTS(
            base_url=config.api.base_url,
            model=config.models.tts,
            voice=voice or DEFAULT_VOICE,
            timeout_s=config.api.timeout_s,
        )

    return factory


def _pronunciation_overrides(config: AppConfig, series_dir: Path) -> dict[str, str]:
    """合并全局与作品级多音字词典（作品级优先）。"""
    from core.config import merge_pronunciation_overrides

    series_config = load_series_config(series_dir)
    return merge_pronunciation_overrides(
        dict(config.pronunciation_overrides), dict(series_config.pronunciation_overrides)
    )


async def produce_media(
    series_id: str,
    *,
    config: AppConfig,
    episodes_spec: str = "",
    concurrency: int = 4,
    client_factory: Any = None,
    tts_factory: Any = None,
    voice: str | None = None,
    bgm_path: str | None = None,
    render: bool = True,
    out_dir: str | None = None,
) -> dict[str, Any]:
    """Stage 2~7：切图 → 识图 → 撰稿 → 配音 → 时间轴 → 成片。

    Stage 5~7 的模块在函数内部导入：Phase 1 的 ``download``/``understand``
    不应该因为渲染链路尚未就绪而无法使用。
    """
    catalog, episodes, script_map = await produce_scripts(
        series_id,
        config=config,
        episodes_spec=episodes_spec,
        concurrency=concurrency,
        client_factory=client_factory,
    )
    return await _produce_media_from_scripts(
        series_id,
        config=config,
        catalog=catalog,
        episodes=episodes,
        script_map=script_map,
        tts_factory=tts_factory,
        voice=voice,
        bgm_path=bgm_path,
        render=render,
        out_dir=out_dir,
        command="run",
        concurrency=concurrency,
    )


async def _produce_media_from_scripts(
    series_id: str,
    *,
    config: AppConfig,
    catalog: SeriesCatalog,
    episodes: list[EpisodeCatalogItem],
    script_map: dict[int, Path],
    tts_factory: Any = None,
    voice: str | None = None,
    bgm_path: str | None = None,
    render: bool = True,
    out_dir: str | None = None,
    command: str = "run",
    concurrency: int = 4,
) -> dict[str, Any]:
    """Stage 5~7：配音 → 时间轴 → 成片（复用已有 script.json）。"""
    from stages import s5_tts, s6_timeline, s7_renderer

    registry = s0_catalog.CatalogRegistry(config.workspace_root())
    series_dir = registry.series_dir(series_id)
    overrides = _pronunciation_overrides(config, series_dir)
    series_config = load_series_config(series_dir)

    # Stage 5：配音（每话独立合成，同部内并发）。
    factory = tts_factory or _tts_factory(config, voice)
    tts_client = factory()
    manifests = ManifestManager(series_dir, series_id=series_id)
    try:

        async def _speak(episode: EpisodeCatalogItem) -> Path:
            script_path = script_map[episode.episode_no]
            script = load_model(NarrationScript, script_path)
            with _manifest_stage(
                manifests, episode.episode_no, "s5_tts", episode_id=episode.episode_id
            ):
                return await s5_tts.run_stage5(
                    series_dir,
                    episode.episode_no,
                    tts_client,
                    script=script,
                    overrides=overrides,
                    voice=voice,
                    concurrency=concurrency,
                    ffmpeg_bin=config.render.ffmpeg_bin,
                )

        audio_manifests = await asyncio.gather(*(_speak(episode) for episode in episodes))
    finally:
        close = getattr(tts_client, "aclose", None)
        if close is not None:
            await close()

    # Stage 6：Audio-First 时间轴 + 字幕。
    timelines = []
    for episode in episodes:
        with _manifest_stage(
            manifests, episode.episode_no, "s6_timeline", episode_id=episode.episode_id
        ):
            timelines.append(
                s6_timeline.run_stage6(
                    series_dir,
                    episode.episode_no,
                    episode_id=episode.episode_id,
                    width=config.video.width,
                    height=config.video.height,
                    fps=config.video.fps,
                )
            )

    # Stage 7：FFmpeg 成片（CPU 密集，放线程池串行，避免多进程抢 CPU）。
    videos: list[dict[str, Any]] = []
    bgm = bgm_path or series_config.bgm
    if render:
        for episode, timeline in zip(episodes, timelines, strict=True):
            with _manifest_stage(
                manifests, episode.episode_no, "s7_renderer", episode_id=episode.episode_id
            ):
                path = await asyncio.to_thread(
                    s7_renderer.render_episode,
                    series_dir,
                    episode.episode_no,
                    episode_id=episode.episode_id,
                    bgm_path=bgm,
                    ffmpeg_bin=config.render.ffmpeg_bin,
                    config=config,
                    title=catalog.series_title,
                )
            videos.append(
                {
                    "episode_no": episode.episode_no,
                    "episode_id": episode.episode_id,
                    "path": str(path),
                    "duration_ms": timeline.total_duration_ms,
                }
            )

    if out_dir:
        target = Path(out_dir).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        for video in videos:
            source = Path(video["path"])
            if source.is_file():
                destination = target / source.name
                destination.write_bytes(source.read_bytes())
                video["exported"] = str(destination)

    return {
        "command": command,
        "series_id": series_id,
        "series_title": catalog.series_title,
        "episodes": [episode.episode_no for episode in episodes],
        "audio_manifests": [str(path) for path in audio_manifests],
        "videos": videos,
    }


async def run_series(
    source: str,
    *,
    config: AppConfig,
    episodes_spec: str = "",
    concurrency: int = 8,
    client_factory: Any = None,
    tts_factory: Any = None,
    voice: str | None = None,
    bgm_path: str | None = None,
    skip_download: bool = False,
    out_dir: str | None = None,
) -> dict[str, Any]:
    """``w2v run``：一条命令跑完 Stage 0~7。

    ``source`` 可以是目录页 URL、本地目录，或**已登记的 series_id**
    （后者需要配合 ``skip_download=True``，避免重复下载）。
    """
    registry = s0_catalog.CatalogRegistry(config.workspace_root())
    series_id = source
    downloaded: dict[str, Any] | None = None

    known = registry.get_series(source) is not None
    if not (skip_download or known):
        downloaded = await download_series(
            source,
            config=config,
            episodes_spec=episodes_spec,
            concurrency=concurrency,
        )
        series_id = str(downloaded["series_id"])

    result = await produce_media(
        series_id,
        config=config,
        episodes_spec=episodes_spec,
        concurrency=concurrency,
        client_factory=client_factory,
        tts_factory=tts_factory,
        voice=voice,
        bgm_path=bgm_path,
        out_dir=out_dir,
    )
    if downloaded is not None:
        result["download"] = downloaded
    return result


async def export_series(
    series_id: str,
    *,
    config: AppConfig,
    episodes_spec: str = "",
    out_dir: str,
    bgm_path: str | None = None,
    voice: str | None = None,
    tts_factory: Any = None,
) -> dict[str, Any]:
    """``w2v export``：批量导出成片到指定目录。

    已渲染过的成片直接复制；缺成片的 话次 重新走 Stage 5~7。
    """
    registry = s0_catalog.CatalogRegistry(config.workspace_root())
    catalog = registry.load_catalog(series_id)
    if catalog is None:
        raise ConfigError(f"作品未登记：{series_id}（先执行 w2v download）")
    episodes = select_episodes(catalog, episodes_spec)
    series_dir = registry.series_dir(series_id)
    target = Path(out_dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)

    from stages import s7_renderer

    exported: list[dict[str, Any]] = []
    pending: list[EpisodeCatalogItem] = []
    for episode in episodes:
        existing = s7_renderer.out_dir_for(series_dir, episode.episode_no) / f"{episode.episode_id}.mp4"
        if existing.is_file():
            destination = target / existing.name
            destination.write_bytes(existing.read_bytes())
            exported.append({"episode_no": episode.episode_no, "path": str(destination)})
        else:
            pending.append(episode)

    if pending:
        rendered = await produce_media(
            series_id,
            config=config,
            episodes_spec=",".join(str(episode.episode_no) for episode in pending),
            bgm_path=bgm_path,
            voice=voice,
            tts_factory=tts_factory,
            out_dir=out_dir,
        )
        exported.extend(rendered.get("videos", []))

    return {
        "command": "export",
        "series_id": series_id,
        "series_title": catalog.series_title,
        "out_dir": str(target),
        "exported": exported,
    }


async def _run_many(
    sources: list[str],
    *,
    config: AppConfig,
    episodes_spec: str = "",
    concurrency: int = 8,
    voice: str | None = None,
    bgm_path: str | None = None,
    skip_download: bool = False,
    out_dir: str | None = None,
) -> list[dict[str, Any]]:
    """多部漫画并发跑全链路（部内 Stage 4 仍按话升序串行）。"""
    return list(
        await asyncio.gather(
            *(
                run_series(
                    source,
                    config=config,
                    episodes_spec=episodes_spec,
                    concurrency=concurrency,
                    voice=voice,
                    bgm_path=bgm_path,
                    skip_download=skip_download,
                    out_dir=out_dir,
                )
                for source in sources
            )
        )
    )


async def _export_many(
    series_ids: list[str],
    *,
    config: AppConfig,
    episodes_spec: str = "",
    out_dir: str = "",
    voice: str | None = None,
    bgm_path: str | None = None,
) -> list[dict[str, Any]]:
    """多部漫画并发导出。"""
    return list(
        await asyncio.gather(
            *(
                export_series(
                    series_id,
                    config=config,
                    episodes_spec=episodes_spec,
                    out_dir=out_dir,
                    voice=voice,
                    bgm_path=bgm_path,
                )
                for series_id in series_ids
            )
        )
    )


async def run_story_draft_series(
    series_id: str,
    *,
    config: AppConfig,
    episodes_spec: str = "",
    concurrency: int = 4,
    client_factory: Any = None,
    force: bool = False,
) -> dict[str, Any]:
    """``w2v story-draft``：只跑到 Stage 4A，产出人工可编辑的故事初稿。

    每话落盘 ``04_script/story_draft.json`` 与 ``04_script/story_draft.md``，
    然后停下等待人工审阅（不会触发 4B 拆拍与配音渲染）。
    """
    catalog, episodes, draft_map = await produce_scripts(
        series_id,
        config=config,
        episodes_spec=episodes_spec,
        concurrency=concurrency,
        client_factory=client_factory,
        mode="story_draft",
        force_story_markdown=force,
    )
    registry = s0_catalog.CatalogRegistry(config.workspace_root())
    series_dir = registry.series_dir(series_id)
    markdown_paths = [
        str(
            s4_scriptwriter.script_dir_for(series_dir, episode.episode_no)
            / s4_scriptwriter.STORY_DRAFT_MD_FILENAME
        )
        for episode in episodes
    ]
    return {
        "command": "story-draft",
        "series_id": series_id,
        "series_title": catalog.series_title,
        "episodes": [episode.episode_no for episode in episodes],
        "story_drafts": [str(path) for path in draft_map.values()],
        "story_markdown": markdown_paths,
    }


async def run_narrate_from_story(
    series_id: str,
    *,
    config: AppConfig,
    episodes_spec: str = "",
    concurrency: int = 4,
    client_factory: Any = None,
    tts_factory: Any = None,
    voice: str | None = None,
    bgm_path: str | None = None,
    render: bool = True,
    out_dir: str | None = None,
) -> dict[str, Any]:
    """``w2v narrate-from-story``：读人工改定的故事初稿跑 4B + Stage 5~7 出片。"""
    catalog, episodes, script_map = await produce_scripts(
        series_id,
        config=config,
        episodes_spec=episodes_spec,
        concurrency=concurrency,
        client_factory=client_factory,
        mode="narrate_from_story",
    )
    return await _produce_media_from_scripts(
        series_id,
        config=config,
        catalog=catalog,
        episodes=episodes,
        script_map=script_map,
        tts_factory=tts_factory,
        voice=voice,
        bgm_path=bgm_path,
        render=render,
        out_dir=out_dir,
        command="narrate-from-story",
        concurrency=concurrency,
    )


async def _story_draft_many(
    series_ids: list[str],
    *,
    config: AppConfig,
    episodes_spec: str = "",
    concurrency: int = 4,
    force: bool = False,
) -> list[dict[str, Any]]:
    """多部漫画并发生成故事初稿。"""
    return list(
        await asyncio.gather(
            *(
                run_story_draft_series(
                    series_id,
                    config=config,
                    episodes_spec=episodes_spec,
                    concurrency=concurrency,
                    force=force,
                )
                for series_id in series_ids
            )
        )
    )


async def _narrate_many(
    series_ids: list[str],
    *,
    config: AppConfig,
    episodes_spec: str = "",
    concurrency: int = 4,
    voice: str | None = None,
    bgm_path: str | None = None,
    out_dir: str | None = None,
) -> list[dict[str, Any]]:
    """多部漫画并发从人工故事稿出片。"""
    return list(
        await asyncio.gather(
            *(
                run_narrate_from_story(
                    series_id,
                    config=config,
                    episodes_spec=episodes_spec,
                    concurrency=concurrency,
                    voice=voice,
                    bgm_path=bgm_path,
                    out_dir=out_dir,
                )
                for series_id in series_ids
            )
        )
    )


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="w2v", description="WEBTOON 漫画 → 解说视频 流水线（Phase 1）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    download = sub.add_parser("download", help="Stage 0 + Stage 1：登记作品并下载原图")
    download.add_argument(
        "--series",
        action="append",
        required=True,
        help="目录页 URL 或本地目录（可重复传入，多部漫画并发）",
    )
    download.add_argument("--episodes", default="", help="话次选择，如 1-3,5")
    download.add_argument("--workspace", default=None, help="工作区根目录（默认取配置文件）")
    download.add_argument("--concurrency", type=int, default=8, help="单部漫画内的下载并发数")

    understand = sub.add_parser("understand", help="Stage 2~4：切图、识图、撰稿")
    understand.add_argument(
        "--series", action="append", required=True, help="已登记的 series_id（可重复传入）"
    )
    understand.add_argument("--episodes", default="", help="话次选择，如 1-5")
    understand.add_argument("--workspace", default=None, help="工作区根目录（默认取配置文件）")
    understand.add_argument("--concurrency", type=int, default=4, help="单部漫画内的识图并发数")

    run = sub.add_parser("run", help="Stage 0~7：一条命令跑完全链路并出片")
    run.add_argument(
        "--series",
        action="append",
        required=True,
        help="目录页 URL / 本地目录 / 已登记的 series_id（可重复传入）",
    )
    run.add_argument("--episodes", default="", help="话次选择，如 1-3,5")
    run.add_argument("--workspace", default=None, help="工作区根目录（默认取配置文件）")
    run.add_argument("--concurrency", type=int, default=8, help="单部漫画内的并发数")
    run.add_argument("--out", default=None, help="成片额外复制到该目录")
    run.add_argument("--voice", default=None, help="TTS 音色（如 Cherry）")
    run.add_argument("--bgm", default=None, help="BGM 音频路径")
    run.add_argument(
        "--skip-download", action="store_true", help="来源是已登记 series_id 时跳过 Stage 0/1"
    )

    export = sub.add_parser("export", help="批量导出成片到指定目录")
    export.add_argument("--series", action="append", required=True, help="已登记的 series_id")
    export.add_argument("--episodes", default="", help="话次选择，如 1-5")
    export.add_argument("--out", required=True, help="导出目录")
    export.add_argument("--workspace", default=None, help="工作区根目录（默认取配置文件）")
    export.add_argument("--voice", default=None, help="TTS 音色")
    export.add_argument("--bgm", default=None, help="BGM 音频路径")

    story_draft = sub.add_parser(
        "story-draft",
        help="Stage 2~4A：切图、识图并生成故事初稿（story_draft.md），停下供人工审阅",
    )
    story_draft.add_argument(
        "--series", action="append", required=True, help="已登记的 series_id（可重复传入）"
    )
    story_draft.add_argument("--episodes", default="", help="话次选择，如 1-5")
    story_draft.add_argument("--workspace", default=None, help="工作区根目录（默认取配置文件）")
    story_draft.add_argument("--concurrency", type=int, default=4, help="单部漫画内的识图并发数")
    story_draft.add_argument(
        "--force", action="store_true", help="已存在故事初稿时强制重新生成（覆盖 story_draft.md）"
    )

    narrate = sub.add_parser(
        "narrate-from-story",
        help="读人工改定的 story_draft.md 跑 4B 拆拍 + Stage 5~7 配音渲染出片",
    )
    narrate.add_argument(
        "--series", action="append", required=True, help="已登记的 series_id（可重复传入）"
    )
    narrate.add_argument("--episodes", default="", help="话次选择，如 1-5")
    narrate.add_argument("--workspace", default=None, help="工作区根目录（默认取配置文件）")
    narrate.add_argument("--concurrency", type=int, default=4, help="单部漫画内的并发数")
    narrate.add_argument("--out", default=None, help="成片额外复制到该目录")
    narrate.add_argument("--voice", default=None, help="TTS 音色")
    narrate.add_argument("--bgm", default=None, help="BGM 音频路径")
    return parser


def _apply_workspace(config: AppConfig, workspace: str | None) -> AppConfig:
    if not workspace:
        return config
    return config.model_copy(update={"workspace": WorkspaceConfig(root=Path(workspace))})


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    args = build_parser().parse_args(argv)
    load_env_file()
    try:
        config = _apply_workspace(load_config(), args.workspace)
        if args.command == "download":
            results = asyncio.run(
                run_download(
                    args.series,
                    config=config,
                    episodes_spec=args.episodes,
                    concurrency=args.concurrency,
                )
            )
        elif args.command == "understand":
            results = asyncio.run(
                run_understand(
                    args.series,
                    config=config,
                    episodes_spec=args.episodes,
                    concurrency=args.concurrency,
                )
            )
        elif args.command == "run":
            results = asyncio.run(
                _run_many(
                    args.series,
                    config=config,
                    episodes_spec=args.episodes,
                    concurrency=args.concurrency,
                    voice=args.voice,
                    bgm_path=args.bgm,
                    skip_download=args.skip_download,
                    out_dir=args.out,
                )
            )
        elif args.command == "story-draft":
            results = asyncio.run(
                _story_draft_many(
                    args.series,
                    config=config,
                    episodes_spec=args.episodes,
                    concurrency=args.concurrency,
                    force=args.force,
                )
            )
        elif args.command == "narrate-from-story":
            results = asyncio.run(
                _narrate_many(
                    args.series,
                    config=config,
                    episodes_spec=args.episodes,
                    concurrency=args.concurrency,
                    voice=args.voice,
                    bgm_path=args.bgm,
                    out_dir=args.out,
                )
            )
        else:
            results = asyncio.run(
                _export_many(
                    args.series,
                    config=config,
                    episodes_spec=args.episodes,
                    out_dir=args.out,
                    voice=args.voice,
                    bgm_path=args.bgm,
                )
            )
    except ConfigError as exc:
        print(f"[w2v] 配置错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI 顶层需要把异常转成退出码
        print(f"[w2v] 执行失败：{exc}", file=sys.stderr)
        return 1
    for item in results:
        print(json.dumps(item, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - 手动执行入口
    sys.exit(main())
