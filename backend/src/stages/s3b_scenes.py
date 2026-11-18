"""Stage 3b：镜头组（Scene）自动聚类 + 整幕视觉联看复核。

Patch v4.0 Patch 19「Scene-First 镜头组架构」。

背景：旧流程是「单格孤立识图 → 全话塞进一次 4A 压缩写死 500~900 字 →
4B 限死 35~50 条顺便跳图」，长话（60~80 格）必然出现「结尾高潮被砍、前半话
信息被压缩没了」。本模块把整话先聚类成 **5~10 个连续镜头组（幕）**：

* :func:`auto_build_storyboard`：按分镜顺序把有效面板切成连续镜头组，
  过渡空页是天然切幕边界、地点切换是软切幕边界、每幕最多 12 格；
* :func:`enrich_storyboard_with_vision`：把一幕的面板图拼成长条图交给视觉
  模型做一次「整幕联看」，补全跨格因果（谁打谁、结尾火光后站出来的是谁）；
* :func:`load_or_build_storyboard`：读写 ``04_script/storyboard.json``，
  **磁盘上已存在的编排（尤其 ``source == "human"``）绝不覆写**。

落盘格式见 :class:`core.schemas.EpisodeStoryboard`。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from core.panel_ids import panel_sort_key
from core.schemas import (
    EpisodeStoryboard,
    PanelVisionInfo,
    StoryboardScene,
    VisionWindowOutput,
    save_model,
)
from providers.image_utils import encode_pil_data_url, load_image
from providers.llm_base import ChatMessage, ContentPart, LLMClient

#: 镜头编排清单的文件名（落在 ``04_script/`` 下）。
STORYBOARD_FILENAME = "storyboard.json"
#: Stage 4 产物目录名（与 ``s4_scriptwriter.SCRIPT_DIR_NAME`` 一致）。
SCRIPT_DIR_NAME = "04_script"

#: 明确无效页（版权声明 / 推广尾页）：直接剔除，不参与聚类。
_INVALID_NON_STORY_TYPES = frozenset({"copyright_notice", "promo_banner"})
#: 只有拟声词特效字的格子：不承载剧情对白，不参与镜头组（与 Stage 4 的
#: 有效分镜过滤保持一致，否则覆盖率校验永远命中不到）。
_SFX_ONLY_NON_STORY_TYPES = frozenset({"sfx_only"})

#: 单幕最多容纳多少格。超过就强制切幕（并在最近一个对话结束格处断开）。
MAX_SCENE_PANELS = 12
#: 单幕最少容纳多少格。低于它时只在「末尾高潮」这种特殊情况下才独立成幕。
MIN_SCENE_PANELS = 3
#: 目标幕数区间（规格：5~10 幕）。
TARGET_SCENE_MIN = 5
TARGET_SCENE_MAX = 10

#: 常见空间地点词（用于给镜头组起标题）。
_LOCATION_WORDS: tuple[str, ...] = (
    "病房",
    "走廊",
    "客厅",
    "卧室",
    "书房",
    "别墅",
    "地下室",
    "花园",
    "玫瑰",
    "墓地",
    "车内",
    "餐厅",
    "玄关",
    "公司",
    "酒店",
)

#: 地点前缀抽取：第一个中文标点之前的短句。
_PREFIX_RE = re.compile(r"^([^，。；：！？、,.;:!?\s]{2,8})")
#: 明显是动作而不是地点的字（用于排除「她抬头看向窗外」这类前缀）。
_LOCATION_BLOCKERS = frozenset(
    "走跑站坐躺看说笑哭拿推拉转抬低抱抓倒伸挥冲退进出扔踢咬点摇摸捧端举挥抓"
)

#: 整幕联看长条图的宽度与高度上限（规格：等宽 640px、总高 <= 3200px）。
STRIP_WIDTH = 640
MAX_STRIP_HEIGHT = 3200

_RECAP_PROMPT = (
    "下面是同一部漫画里**同一个镜头组（连续一幕）**的若干格画面，"
    "请一次性通看后回答：\n"
    "1. 这一幕发生在哪里？有哪些人物在场？\n"
    "2. 跨格的连贯物理动作与因果是什么"
    "（谁打谁、谁抓住谁、谁在火中倒下、最后露脸的是谁）？\n"
    "3. 最关键的 1~2 句原话气泡分别归属谁？\n"
    "只回答事实，不要文学化描写，不要写「画面中」这类元词汇。"
)


def storyboard_path_for(episode_dir: str | Path) -> Path:
    """某话的镜头编排清单路径：``<episode_dir>/04_script/storyboard.json``。"""
    return Path(episode_dir) / SCRIPT_DIR_NAME / STORYBOARD_FILENAME


# --------------------------------------------------------------------------
# 地点标签
# --------------------------------------------------------------------------
def extract_location(summary: str) -> str:
    """从分镜摘要里抽取空间地点标签。

    先看常见地点词（``病房``/``走廊``/``玫瑰园``…），命中即用；否则取第一个
    中文标点之前的短前缀，且该前缀不能含明显动作动词（否则「她抬头看向窗外」
    会被当成地点）。
    """
    text = (summary or "").strip()
    if not text:
        return ""
    head = text[:20]
    for word in _LOCATION_WORDS:
        if word in head:
            return word
    match = _PREFIX_RE.match(text)
    if match:
        prefix = match.group(1).strip()
        # 太长的前缀多半是画面描述（「纯黑背景文字卡」）而不是地点，直接放弃。
        if 0 < len(prefix) <= 6 and not any(
            char in _LOCATION_BLOCKERS for char in prefix
        ):
            return prefix
    return ""


def _is_usable_panel(panel: PanelVisionInfo) -> bool:
    """该格是否进入镜头组（与 Stage 4 的有效分镜口径一致）。"""
    if panel.is_transition_blank:
        return False
    if panel.non_story_type in _INVALID_NON_STORY_TYPES:
        return False
    if panel.non_story_type in _SFX_ONLY_NON_STORY_TYPES:
        return False
    return True


def _is_boundary_panel(panel: PanelVisionInfo) -> bool:
    """该格是否是天然切幕边界（过渡空页）。"""
    return bool(panel.is_transition_blank) or panel.non_story_type == "transition_blank"


def _scene_title(index: int, location: str) -> str:
    """镜头组标题：``第 1 幕 · 病房``。"""
    if location:
        return f"第 {index} 幕 · {location}"
    return f"第 {index} 幕"


def _split_at_dialogue_end(panel_ids: list[str], panels: dict[str, PanelVisionInfo]) -> int:
    """在最近一个「有对白」的面板之后返回切点（返回 0 表示无处可切）。"""
    for index in range(len(panel_ids) - 1, -1, -1):
        if panels.get(panel_ids[index]) and panels[panel_ids[index]].dialogues:
            return index + 1
    return 0


def _build_scene(
    scene_index: int,
    panel_ids: list[str],
    panels: dict[str, PanelVisionInfo],
) -> StoryboardScene:
    """把一组分镜打包成 :class:`StoryboardScene`。"""
    location = ""
    for panel_id in panel_ids:
        panel = panels.get(panel_id)
        if panel is None:
            continue
        location = extract_location(panel.scene_summary)
        if location:
            break
    return StoryboardScene(
        scene_id=f"sc_{scene_index:03d}",
        scene_index=scene_index,
        title=_scene_title(scene_index, location),
        location=location,
        panel_ids=list(panel_ids),
        enabled=True,
        must_cover=True,
    )


def _rebalance_scenes(
    scene_panel_ids: list[list[str]],
    panels: dict[str, PanelVisionInfo],
) -> list[list[str]]:
    """把幕数拉进目标区间（5~10）。

    幕太少 → 在最长的一幕里按对白结束格再切一次；幕太多 → 把最短的一幕并进
    邻居。切分始终保证有序、连续、不丢格。
    """
    scenes = [list(item) for item in scene_panel_ids]

    while len(scenes) < TARGET_SCENE_MIN:
        # 找最长的、且能在对白结束格处再切一次的一幕。
        target = -1
        cut = 0
        for index, item in enumerate(scenes):
            if len(item) < MIN_SCENE_PANELS * 2:
                continue
            point = _split_at_dialogue_end(item[:-1], panels)
            if point <= 0 or point >= len(item):
                continue
            if target < 0 or len(item) > len(scenes[target]):
                target = index
                cut = point
        if target < 0:
            break
        scenes[target:target + 1] = [scenes[target][:cut], scenes[target][cut:]]

    while len(scenes) > TARGET_SCENE_MAX:
        # 幕太多：把最短的一幕并进邻居（不越过每幕 12 格上限），保证分镜连续。
        order = sorted(range(len(scenes)), key=lambda index: len(scenes[index]))
        merged = False
        for index in order:
            for neighbour in (index - 1, index + 1):
                if neighbour < 0 or neighbour >= len(scenes):
                    continue
                combined = scenes[min(index, neighbour)] + scenes[max(index, neighbour)]
                if len(combined) > MAX_SCENE_PANELS:
                    continue
                low, high = sorted((index, neighbour))
                scenes[low : high + 1] = [combined]
                merged = True
                break
            if merged:
                break
        if not merged:
            break
    return scenes


def auto_build_storyboard(
    episode_id: str,
    vision: VisionWindowOutput,
) -> EpisodeStoryboard:
    """把整话分镜自动聚类成连续镜头组（幕）。

    切分启发式：

    * 过渡空页（``is_transition_blank``）是**天然切幕边界**：当前幕已积累
      3 格以上时切新幕；
    * 地点标签明显切换且当前幕已有 4 格以上时切新幕；
    * 当前幕达到 12 格上限时，强制在最近一个对话结束格处切新幕；
    * 末尾残余不足 3 格时，若含对白或处于全话最后 5 格内，独立成收尾幕
      （**严禁丢弃**），否则并入前一幕。

    返回的镜头组默认 ``enabled=True`` / ``must_cover=True``。
    """
    all_panels = sorted(vision.panels, key=lambda item: panel_sort_key(item.panel_id))
    panels = {panel.panel_id: panel for panel in all_panels}

    scene_panel_ids: list[list[str]] = []
    current: list[str] = []
    current_location = ""
    pending_boundary = False

    for panel in all_panels:
        if _is_boundary_panel(panel):
            # 天然切幕边界：当前幕够长才切，避免每页过渡都切出一幕。
            if len(current) >= MIN_SCENE_PANELS:
                pending_boundary = True
            continue
        if not _is_usable_panel(panel):
            continue

        location = extract_location(panel.scene_summary)
        if pending_boundary and len(current) >= MIN_SCENE_PANELS:
            scene_panel_ids.append(current)
            current = []
            current_location = ""
        elif (
            current
            and location
            and current_location
            and location != current_location
            and len(current) >= MIN_SCENE_PANELS + 1
        ):
            scene_panel_ids.append(current)
            current = []
            current_location = ""
        pending_boundary = False

        current.append(panel.panel_id)
        if location and not current_location:
            current_location = location

        if len(current) >= MAX_SCENE_PANELS:
            point = _split_at_dialogue_end(current[:-1], panels)
            if point <= 0 or point >= len(current):
                point = len(current)
            scene_panel_ids.append(current[:point])
            current = current[point:]
            current_location = (
                extract_location(panels[current[0]].scene_summary) if current else ""
            )

    if current:
        if len(current) < MIN_SCENE_PANELS:
            # 末尾残余：含对白 / 至少 2 格 / 前一组已满 8 格 → 独立成收尾幕。
            #
            # Patch v4.0 Patch 20.1：原判据 ``usable_count - len(current) < 5``
            # 是笔误——usable_count 累加的是**全话**可用格数，减掉残余长度
            # 得到的是「前面所有幕的总格数」，与「是否在全话末尾」无关。
            # 循环结束处的 current 本身就是整话最后一组剩余格（距末尾 0 格），
            # 因此改用三个与位置无关的判据：含对白、格数 ≥ 2（前一组之外的
            # 纯动作反转 / 定格）、或前一组已满 8 格。
            has_dialogue = any(
                panels.get(panel_id) and panels[panel_id].dialogues
                for panel_id in current
            )
            keep_tail = (
                has_dialogue
                or len(current) >= 2
                or (bool(scene_panel_ids) and len(scene_panel_ids[-1]) >= 8)
            )
            if (
                not keep_tail
                and scene_panel_ids
                and len(scene_panel_ids[-1]) + len(current) <= MAX_SCENE_PANELS
            ):
                scene_panel_ids[-1].extend(current)
                current = []
        if current:
            scene_panel_ids.append(current)

    scene_panel_ids = _rebalance_scenes(scene_panel_ids, panels)

    scenes = [
        _build_scene(index, panel_ids, panels)
        for index, panel_ids in enumerate(scene_panel_ids, start=1)
        if panel_ids
    ]
    return EpisodeStoryboard(episode_id=episode_id, source="auto", scenes=scenes)


# --------------------------------------------------------------------------
# 整幕视觉联看复核
# --------------------------------------------------------------------------
def _panel_image_index(panels_dir: Path) -> dict[str, Path]:
    """扫描面板目录，建立 ``panel_id`` → 图片路径的索引。"""
    index: dict[str, Path] = {}
    if not panels_dir.is_dir():
        return index
    for path in sorted(panels_dir.iterdir()):
        if not path.is_file():
            continue
        stem = path.stem
        if stem.startswith("."):
            continue
        index.setdefault(stem, path)
    return index


def _find_panel_image(panel_id: str, image_index: dict[str, Path]) -> Path | None:
    """按 panel_id 找面板图（容忍 ``p_001`` / ``panel_001`` 两种写法）。"""
    if panel_id in image_index:
        return image_index[panel_id]
    tail = panel_id.rsplit("_", 1)[-1]
    if tail.isdigit():
        for stem, path in image_index.items():
            if stem.rsplit("_", 1)[-1] == tail:
                return path
    return None


def _build_strips(
    panel_paths: Sequence[Path],
    *,
    width: int = STRIP_WIDTH,
    max_height: int = MAX_STRIP_HEIGHT,
) -> list:
    """把若干面板图等宽竖向拼成长条图（高度超限则拆成多张）。"""
    from PIL import Image

    tiles: list = []
    for path in panel_paths:
        try:
            image = load_image(path)
        except Exception:  # noqa: BLE001 - 单张图读失败不阻断整幕
            continue
        ratio = width / max(1, image.width)
        tiles.append(image.resize((width, max(1, round(image.height * ratio)))))
    if not tiles:
        return []

    strips: list = []
    current: list = []
    height = 0
    for tile in tiles:
        if current and height + tile.height > max_height:
            strips.append(current)
            current = []
            height = 0
        current.append(tile)
        height += tile.height
    if current:
        strips.append(current)

    result: list = []
    for group in strips:
        canvas = Image.new("RGB", (width, sum(item.height for item in group)), "white")
        offset = 0
        for tile in group:
            canvas.paste(tile, (0, offset))
            offset += tile.height
        result.append(canvas)
    return result


def _scene_fallback_summary(
    scene: StoryboardScene, panels: dict[str, PanelVisionInfo]
) -> str:
    """视觉复核失败时的兜底：把该幕各格的 ``scene_summary`` 拼起来。"""
    parts: list[str] = []
    for panel_id in scene.panel_ids:
        panel = panels.get(panel_id)
        if panel is None:
            continue
        summary = (panel.scene_summary or "").strip()
        if summary:
            parts.append(summary)
    return " ".join(parts)


async def enrich_storyboard_with_vision(
    client: LLMClient | None,
    *,
    storyboard: EpisodeStoryboard,
    panels_dir: str | Path | None,
    model: str = "",
    force: bool = False,
    vision: VisionWindowOutput | None = None,
) -> EpisodeStoryboard:
    """给每个镜头组做一次「整幕视觉联看」，把结果写进 ``scene.visual_recap``。

    * 只有传了 ``client``、``panels_dir`` 存在、该幕还没有 ``visual_recap``
      （或 ``force=True``）时才会调用模型；
    * 该幕的面板图等宽缩至 640px 竖向拼成长条图（总高 <= 3200px），超出就拆
      成多张分别提问；
    * 单幕异常一律捕获并回退为「拼接各格 scene_summary」，**绝不阻断流水线**。
    """
    if client is None or not model or panels_dir is None:
        return storyboard
    panels_path = Path(panels_dir)
    if not panels_path.is_dir():
        return storyboard

    image_index = _panel_image_index(panels_path)
    panel_map: dict[str, PanelVisionInfo] = {
        panel.panel_id: panel for panel in (vision.panels if vision else [])
    }
    for scene in storyboard.scenes:
        if not scene.enabled:
            continue
        if scene.visual_recap and not force:
            continue
        panel_paths = [
            path
            for path in (
                _find_panel_image(panel_id, image_index) for panel_id in scene.panel_ids
            )
            if path is not None
        ]
        fallback = _scene_fallback_summary(scene, panel_map)
        if not panel_paths:
            scene.visual_recap = fallback
            continue
        try:
            strips = _build_strips(panel_paths)
            if not strips:
                scene.visual_recap = fallback
                continue
            parts: list[ContentPart] = [ContentPart(type="text", text=_RECAP_PROMPT)]
            for strip in strips:
                parts.append(
                    ContentPart(type="image_url", image_url=encode_pil_data_url(strip))
                )
            response = await client.chat(
                [ChatMessage(role="user", content=parts)],
                model=model,
                temperature=0.2,
                max_tokens=1200,
            )
            recap = (response.content or "").strip()
            scene.visual_recap = recap or fallback
        except Exception as exc:  # noqa: BLE001 - 单幕复核失败不阻断流水线
            print(f"[s3b_scenes] 第 {scene.scene_index} 幕视觉复核失败，回退摘要：{exc}")
            scene.visual_recap = fallback
    return storyboard


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------
def load_storyboard(path: str | Path) -> EpisodeStoryboard | None:
    """读取磁盘上的镜头编排清单（不存在或损坏时返回 ``None``）。"""
    from core.schemas import load_model

    file_path = Path(path)
    if not file_path.is_file():
        return None
    try:
        return load_model(EpisodeStoryboard, file_path)
    except Exception:  # noqa: BLE001 - 损坏的编排按「没有编排」处理
        return None


def save_storyboard(storyboard: EpisodeStoryboard, path: str | Path) -> Path:
    """把镜头编排清单落盘。"""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    save_model(storyboard, file_path)
    return file_path


def load_or_build_storyboard(
    episode_id: str,
    vision: VisionWindowOutput,
    storyboard_path: str | Path | None,
    *,
    reset: bool = False,
) -> EpisodeStoryboard:
    """加载或构建镜头编排清单。

    * 文件存在且 ``reset=False`` → **直接加载**，绝不覆写用户编排
      （``source == "human"`` 的 ``scenes`` / ``user_note`` / ``locked_quotes`` /
      ``enabled`` / ``must_cover`` 全部保留）；
    * ``source == "human"`` 的文件**任何情况下都不覆写**（连 ``reset=True``
      也不会重建），人工编排永远优先；
    * 其余情况 ``reset=True`` → 重新自动聚类并覆盖落盘；
    * 文件不存在 → 自动聚类并落盘。
    """
    path = Path(storyboard_path) if storyboard_path is not None else None
    existing = load_storyboard(path) if path is not None else None
    if existing is not None:
        if existing.source == "human":
            # 人工编排保护：永远不覆写用户的镜头组。
            return existing
        if not reset:
            return existing

    storyboard = auto_build_storyboard(episode_id, vision)
    if path is not None:
        save_storyboard(storyboard, path)
    return storyboard
