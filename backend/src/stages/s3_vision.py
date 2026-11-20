"""Stage 3：调用百炼 ``qwen3-vl-plus`` 逐面板抽取画面信息。

规范第 2 节 / 第 4.1 节要点：

* 图片必须先在本地缩放到 **长边 ≤ 1280px**，再以 Base64 Data URL 内联发送；
* 必须注入 **Layer 1 角色参考名单**（``name`` + ``aliases`` + ``visual_traits``），
  否则老角色会被认成路人（脸盲重名）；
* 产物落到 ``episodes/ep_XXX/03_vision/vision.json``。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from core.normalize import coerce_str, normalize_panel_payload, to_simplified_chinese
from core.schemas import PanelVisionInfo, VisionWindowOutput, save_model
from providers.image_utils import encode_image_data_url, encode_pil_data_url
from providers.llm_base import ChatMessage, ContentPart, LLMClient, parse_json_response

VISION_DIR_NAME = "03_vision"
PROMPT_FILENAME = "vision_extract.md"

#: Patch v2.6-5：高度超过该值且长宽比超过 ``TALL_PANEL_RATIO`` 的画格视为
#: 「超高竖长画格」（平台把连续画面压在一张图里），需要模型等比缩放后完整阅读。
TALL_PANEL_MIN_HEIGHT = 2400
TALL_PANEL_RATIO = 3.0

# --------------------------------------------------------------------------
# Patch v2.6 补充：超高画格「纵向重叠切片喂 VLM」
# --------------------------------------------------------------------------
#: 画格高度超过该值就不再「长边缩到 1280」（缩完只剩一条细线，识图等于没做），
#: 改为在内存里纵向切成多片逐片喂模型。
TALL_PANEL_SLICE_THRESHOLD = 1800
#: 切片后的最大宽度（像素）。
TALL_PANEL_SLICE_WIDTH = 1024
#: 每片的高度（像素）。
TALL_PANEL_SLICE_HEIGHT = 1600
#: 相邻两片的纵向重叠（像素），避免切在画格中间丢内容。
TALL_PANEL_SLICE_OVERLAP = 250
#: 末片高度小于该值时并入上一片，避免出现一条没内容的薄片。
TALL_PANEL_SLICE_MIN_HEIGHT = 400

#: Patch v2.5-1：non_story_type 合法取值（与 schemas.PanelVisionInfo 对齐）。
#: Patch v3.0-6：新增 ``sfx_only``——整格只有拟声词/气氛特效、没有人物动作与对白。
NON_STORY_TYPES = frozenset(
    {"none", "transition_blank", "copyright_notice", "promo_banner", "sfx_only"}
)
#: 命中这两类即视为非剧情尾页，下游统一按过渡空页过滤。
NON_STORY_BANNER_TYPES = frozenset({"copyright_notice", "promo_banner"})
#: Patch v3.0-6：只含拟声词特效的画格（同样不进剧本）。
NON_STORY_SFX_TYPES = frozenset({"sfx_only"})

#: 参与视觉抽取的图片后缀。
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})

# --------------------------------------------------------------------------
# Patch v4.0 Patch 18：暗黑高潮格护栏
# --------------------------------------------------------------------------
#: 深黑底 + 血红警示光 / 火光剪影的高潮剧情格，极易被模型误判成过渡空页、
#: 纯字卡或纯拟声格，随后在 Stage 4 被整格剔除，结尾反转直接被砍。
#: 这里用**确定性关键词判据**把它们救回来（不依赖模型自觉）。

#: 去掉标点 / 空白后的净字符（判断「有效台词」用）。
_PUNCT_ONLY_RE = re.compile(r"[^\w]+", re.UNICODE)

#: 人物 / 躯体证据词（``scene_summary`` 里出现即说明画面里有人）。
_DRAMATIC_PERSON_KEYWORDS: tuple[str, ...] = (
    "男人", "女人", "男子", "女子", "少年", "少女", "男孩", "女孩", "老人", "孩子",
    "婴儿", "身影", "剪影", "人影", "面孔", "脸", "眼睛", "视线", "目光", "表情",
    "嘴角", "眉头", "头发", "手掌", "手指", "双腿", "肩膀", "后背", "胸膛",
    "倒地", "跪着", "躺着", "坐着", "站着",
)

#: 躯体动作证据词（打耳光、推搡、倒地这类高潮动作）。
_DRAMATIC_ACTION_KEYWORDS: tuple[str, ...] = (
    "挥", "扇", "抓", "推", "拉", "摔", "撞", "倒", "跌", "扑", "跪", "跑", "冲",
    "抱", "握", "攥", "举", "扔", "踢", "掐", "挣", "拖", "扶", "压", "抹", "撕",
    "打", "踹", "拽", "捂", "抬手", "转身", "低头", "抬头", "咬牙", "发抖",
)

#: 空间证据词（单有空间词还不够，必须再有人物指代才判定为剧情格）。
_DRAMATIC_SPACE_KEYWORDS: tuple[str, ...] = (
    "病房", "走廊", "客厅", "卧室", "厨房", "教室", "办公室", "地下室", "停车场",
    "街道", "巷子", "房间", "屋内", "门外", "窗边", "床边", "沙发", "车里", "院子",
    "花园", "楼道", "阳台", "医院", "大厅", "仓库", "天台", "桥上", "街口",
)

#: 人物指代词（与空间词同时出现才算「有人在此处发生了什么」）。
_DRAMATIC_PERSON_MARKERS: tuple[str, ...] = (
    "他", "她", "他们", "她们", "有人", "那人", "此人", "二人", "两人", "众人",
)

#: 有效台词的净字数下限（去掉标点后仍要够长，纯拟声词「啪！」不算）。
_DRAMATIC_DIALOGUE_MIN_CHARS = 3


class VisionError(RuntimeError):
    """Stage 3 视觉抽取失败。"""


def prompts_dir() -> Path:
    """提示词模板目录（``backend/prompts/``）。"""
    return Path(__file__).resolve().parents[2] / "prompts"


def load_prompt_template(name: str = PROMPT_FILENAME) -> str:
    """读取提示词模板。"""
    path = prompts_dir() / name
    if not path.is_file():
        raise VisionError(f"提示词模板缺失：{path}")
    return path.read_text(encoding="utf-8")


def render_prompt(template: str, mapping: dict[str, str]) -> str:
    """把模板中的 ``{{TOKEN}}`` 占位符替换为实际内容。"""
    rendered = template
    for key, value in mapping.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def format_character_block(characters: Any) -> str:
    """把角色名单渲染成 Prompt 片段（兼容 ``CharacterProfile`` 与 ``CharacterReference``）。"""
    lines: list[str] = []
    for character in characters or []:
        name = getattr(character, "name", "")
        if not name:
            continue
        aliases = tuple(getattr(character, "aliases", ()) or ())
        alias_text = f"（别名：{'、'.join(aliases)}）" if aliases else ""
        traits = getattr(character, "visual_traits", "") or ""
        status = getattr(character, "current_status", "") or ""
        status_text = f"；当前状态：{status}" if status else ""
        lines.append(f"- {name}{alias_text}：{traits}{status_text}")
    return "\n".join(lines)


def load_panels_manifest(panels_dir: str | Path) -> dict[str, dict[str, Any]]:
    """读取 Stage 2 产出的 ``panels.json``（不存在或损坏时返回空字典）。

    Patch v3.0-6：Stage 2 现在会写出本话的面板清单（含 ``is_transition_blank``
    标记）。Stage 3 只按清单遍历，**不再扫描目录里的所有图片**——否则历史
    遗留的 ``panel_001.png`` 会被再次识别，导致同一话出现两套 ID、播两遍。
    """
    path = Path(panels_dir) / "panels.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    items = data.get("panels") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        panel_id = str(item.get("panel_id") or "").strip()
        if panel_id:
            result[panel_id] = item
    return result


def list_panel_images(panels_dir: str | Path) -> list[Path]:
    """列出 Stage 2 本次产出的面板图。

    优先按 ``panels.json`` 的顺序取图（清单是唯一真相）；清单缺失时
    回退到目录扫描，保证历史工作区仍能跑。
    """
    root = Path(panels_dir)
    if not root.is_dir():
        raise VisionError(f"面板目录不存在：{root}")
    manifest = load_panels_manifest(root)
    if manifest:
        paths: list[Path] = []
        for panel_id in manifest:
            candidate = root / f"{panel_id}.png"
            if not candidate.is_file():
                # 清单里的 image_path 可能是绝对路径（生产工作区）
                raw = str(manifest[panel_id].get("image_path") or "")
                if raw and Path(raw).is_file():
                    candidate = Path(raw)
            if candidate.is_file():
                paths.append(candidate)
        if paths:
            return paths
    paths = sorted(
        path
        for path in root.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )
    if not paths:
        raise VisionError(f"面板目录没有可用图片：{root}")
    return paths


def blank_panel_ids(panels_dir: str | Path) -> set[str]:
    """``panels.json`` 中被标记为空镜（过渡页）的 panel_id 集合。"""
    manifest = load_panels_manifest(panels_dir)
    return {
        panel_id
        for panel_id, item in manifest.items()
        if bool(item.get("is_transition_blank"))
    }


def panel_id_for(path: str | Path) -> str:
    """面板编号 = 文件名主干（如 ``p_001``）。

    Stage 2 现在直接把面板写成 ``p_001.png``，``PanelMeta.panel_id`` 也是
    ``p_001``；Stage 3 **原样透传**这个编号，不再私自改名成 ``panel_001``，
    两端 ID 从此同轨（详见 Patch v2.6 的命名统一）。
    """
    return Path(path).stem


@dataclass(frozen=True)
class PanelTile:
    """超高画格纵向切片（仅存在于内存，不改动 ``02_panels/`` 原图）。"""

    image: Image.Image
    #: 该片在整格（已等比缩放后）中的纵向起止像素。
    top: int
    bottom: int
    #: 整格缩放后的总高度。
    total_height: int
    #: 本片序号（0 起）与总片数。
    index: int
    count: int


def _slice_tall_panel(image_path: str | Path) -> list[PanelTile]:
    """把超高画格在内存中纵向切成若干重叠的片。

    **为什么要切片**：平台会把连续画面压进一张很高的图。旧逻辑把这种图
    「长边缩到 1280px」再送模型——45871px 高的图缩完只剩 19px 宽的一条线，
    模型什么都看不见。现在改成固定宽度（≤1024px）、片高 1600px、上下重叠
    250px 逐片喂模型，再把各片结果合并回同一个 ``panel_id``。
    """
    try:
        with Image.open(image_path) as raw:
            image = ImageOps.exif_transpose(raw).convert("RGB")
    except Exception:  # pragma: no cover - 图片损坏时降级为普通路径
        return []
    width, height = image.size
    if not width or not height or height <= TALL_PANEL_SLICE_THRESHOLD:
        return []
    if width > TALL_PANEL_SLICE_WIDTH:
        scale = TALL_PANEL_SLICE_WIDTH / float(width)
        image = image.resize(
            (TALL_PANEL_SLICE_WIDTH, max(1, round(height * scale))),
            Image.Resampling.LANCZOS,
        )
        width, height = image.size
    # 切片数量：保证相邻两片至少重叠 TALL_PANEL_SLICE_OVERLAP，
    # 再把起点均匀铺开，使每一片的高度都恰好是 TALL_PANEL_SLICE_HEIGHT
    # （末片正好落在整格底部，不会有「薄尾巴」）。
    span = TALL_PANEL_SLICE_HEIGHT - TALL_PANEL_SLICE_OVERLAP
    count = max(1, math.ceil((height - TALL_PANEL_SLICE_OVERLAP) / span))
    if count == 1:
        ranges = [(0, height)]
    else:
        stride = (height - TALL_PANEL_SLICE_HEIGHT) / (count - 1)
        ranges = [
            (round(index * stride), min(height, round(index * stride) + TALL_PANEL_SLICE_HEIGHT))
            for index in range(count)
        ]
    return [
        PanelTile(
            image=image.crop((0, begin, width, end)),
            top=begin,
            bottom=end,
            total_height=height,
            index=index,
            count=len(ranges),
        )
        for index, (begin, end) in enumerate(ranges)
    ]


def _extract_panel_payload(data: Any, *, panel_id: str) -> dict[str, Any]:
    """从模型返回的 JSON 中取出面板对象（兼容单对象与 ``panels`` 数组两种写法）。"""
    if not isinstance(data, dict):
        raise VisionError(f"模型返回的 JSON 结构异常：{type(data).__name__}")
    if "panels" in data:
        panels = data.get("panels")
        if not isinstance(panels, list) or not panels:
            raise VisionError("模型返回的 panels 为空")
        first = panels[0]
        if not isinstance(first, dict):
            raise VisionError("模型返回的 panels[0] 不是对象")
        payload = dict(first)
    else:
        payload = dict(data)
    payload.setdefault("panel_id", panel_id)
    return payload


def describe_panel_shape(image_path: str | Path) -> str:
    """描述面板形状（Patch v2.6-5）。

    超高竖长画格（平台把连续画面压进一张图）在等比缩放到长边 1280px 之后
    会变得又窄又小，模型只看得见顶部一截；这里把尺寸与阅读要求显式告诉模型，
    让它把整条长画格从上到下读完。
    """
    try:
        with Image.open(image_path) as image:
            width, height = image.size
    except Exception:  # pragma: no cover - 图片损坏时降级
        return "（无法读取图片尺寸，按普通画格处理）"
    if not width or not height:
        return "（图片尺寸异常，按普通画格处理）"
    ratio = height / width
    if height >= TALL_PANEL_MIN_HEIGHT and ratio >= TALL_PANEL_RATIO:
        return (
            f"本格是**超高竖长画格**（{width}×{height}，高是宽的 {ratio:.0f} 倍，"
            "相当于多段画面上下排在一起）。请把整幅图等比缩小后**从上到下完整读完每一段**，"
            "不要只读顶部或只读底部；`scene_summary` 要概括这条长画格里的全部内容，"
            "对白也要全部抽出。"
        )
    return f"本格尺寸 {width}×{height}（比例正常），按常规方式阅读即可。"


def _tile_shape_text(tile: PanelTile) -> str:
    """描述当前切片在整格中的位置（提示词 ``{{PANEL_SHAPE}}`` 用）。"""
    ratio = (tile.bottom - tile.top) / max(1, tile.total_height)
    return (
        f"本格是**超高竖长画格**（总高 {tile.total_height}px）的第 "
        f"{tile.index + 1}/{tile.count} 段（这段覆盖整格的 "
        f"{ratio * 100:.0f}%）。"
        f"请只描述**本段里看得见**的内容，不要臆测本段之外的部分；"
        f"本段出现的对白气泡要全部抽出。"
    )


def _merge_tile_payloads(
    panel_id: str, tile_payloads: list[tuple[PanelTile, dict[str, Any]]]
) -> dict[str, Any]:
    """把多个切片的视觉结果合并成单个面板的视觉结果。

    * ``scene_summary``：按段顺序拼接（去重相邻重复）；
    * ``dialogues``：按 (说话人, 原文) 去重（切片重叠区会重复抽到同一条气泡）；
    * ``sfx_list``：去重保序；
    * ``focus_region``：把切片内的归一化坐标映射回整格坐标；
    * ``is_transition_blank``：全部切片都是空白才算空白；
    * ``non_story_type``：任一切片是版权/推广页即整体按该类型处理；
      ``sfx_only`` 则严格得多——只有**所有有效（非空白）切片都只剩拟声词**
      且合并后没有任何对白时，整格才标 ``sfx_only``。否则尾部一个空白/音效
      切片就会把前面带对白的超长画格整格误杀。
    * ``is_pure_text_card``：Patch v3.1 Patch 10——只有**所有有效切片都是纯字卡**
      且合并后没有任何对白时，整格才是纯字卡（任一切片有人物表情/动作即否）。
    """
    summaries: list[str] = []
    dialogues: list[dict[str, Any]] = []
    seen_dialogues: set[tuple[str, str]] = set()
    sfx_list: list[str] = []
    seen_sfx: set[str] = set()
    focus_region: tuple[float, float, float, float] | None = None
    non_story_type = "none"
    blank_flags: list[bool] = []
    #: 每个「有效切片」（非空白）是否自报 sfx_only。
    valid_sfx_flags: list[bool] = []
    #: 每个「有效切片」（非空白）是否自报纯字卡（Patch v3.1 Patch 10）。
    valid_text_card_flags: list[bool] = []

    for tile, payload in tile_payloads:
        # Patch v4.13：视觉抽取结果落盘前统一简体——模型偶尔会照抄画格里的
        # 繁体字，后续字幕 / 送音若繁简混排会破坏检索与对白匹配。
        summary = to_simplified_chinese(str(payload.get("scene_summary") or "").strip())
        if summary and (not summaries or summaries[-1] != summary):
            summaries.append(summary)
        for item in payload.get("dialogues") or []:
            if not isinstance(item, dict):
                continue
            text = to_simplified_chinese(str(item.get("text") or "").strip())
            if not text:
                continue
            speaker = to_simplified_chinese(str(item.get("speaker_guess") or "").strip())
            key = (speaker, text)
            if key in seen_dialogues:
                continue
            seen_dialogues.add(key)
            entry = dict(item)
            entry["text"] = text
            entry["speaker_guess"] = speaker
            dialogues.append(entry)
        for value in payload.get("sfx_list") or []:
            text = to_simplified_chinese(str(value).strip())
            if text and text not in seen_sfx:
                seen_sfx.add(text)
                sfx_list.append(text)
        if focus_region is None:
            region = payload.get("focus_region")
            if isinstance(region, (list, tuple)) and len(region) == 4:
                x1, y1, x2, y2 = (float(v) for v in region)
                span = max(1, tile.bottom - tile.top)
                # 切片内归一化坐标 → 整格归一化坐标
                gy1 = (tile.top + y1 * span) / max(1, tile.total_height)
                gy2 = (tile.top + y2 * span) / max(1, tile.total_height)
                focus_region = (x1, min(gy1, gy2), x2, max(gy1, gy2))
        raw_non_story = str(payload.get("non_story_type") or "none")
        if raw_non_story in NON_STORY_BANNER_TYPES:
            # 版权/推广页仍保持「任一切片命中即整格命中」的强判定。
            non_story_type = raw_non_story
        if not bool(payload.get("is_transition_blank")):
            # 空白切片不算有效切片，不参与 sfx_only 的「全部」判定。
            valid_sfx_flags.append(raw_non_story in NON_STORY_SFX_TYPES)
            valid_text_card_flags.append(bool(payload.get("is_pure_text_card")))
        blank_flags.append(bool(payload.get("is_transition_blank")))

    if (
        non_story_type == "none"
        and not dialogues
        and valid_sfx_flags
        and all(valid_sfx_flags)
    ):
        # 所有有效切片都自报 sfx_only 且整格确实没有对白，才标 sfx_only；
        # 只要有一个有效切片含剧情/对白，整格就按普通剧情格处理。
        non_story_type = "sfx_only"

    # Patch v3.1 Patch 10：纯字卡合并——所有有效切片都是纯字卡、且整格没有对白
    # 时才算纯字卡；只要有一片出现人物表情/动作或对白，就按普通剧情格处理。
    is_pure_text_card = bool(
        valid_text_card_flags
        and all(valid_text_card_flags)
        and not dialogues
        and non_story_type == "none"
    )

    merged: dict[str, Any] = {
        "panel_id": panel_id,
        "scene_summary": " ".join(summaries),
        "dialogues": dialogues,
        "sfx_list": sfx_list,
        "focus_region": list(focus_region) if focus_region else None,
        "is_transition_blank": bool(blank_flags) and all(blank_flags),
        "non_story_type": non_story_type,
        "is_pure_text_card": is_pure_text_card,
    }
    return merged


async def _analyze_tall_panel(
    client: LLMClient,
    *,
    tiles: list[PanelTile],
    panel_id: str,
    model: str,
    characters: Any = (),
    series_title: str = "",
    episode_no: int = 0,
    template: str | None = None,
    temperature: float = 0.2,
) -> PanelVisionInfo:
    """超高画格：逐片喂模型，再把各片结果合并成同一 ``panel_id``。"""
    prompt_template = template if template is not None else load_prompt_template()
    tile_payloads: list[tuple[PanelTile, dict[str, Any]]] = []
    for tile in tiles:
        data_url = encode_pil_data_url(tile.image)
        prompt = render_prompt(
            prompt_template,
            {
                "CHARACTERS": format_character_block(characters) or "（暂无已知角色）",
                "PANEL_ID": panel_id,
                "SERIES_TITLE": series_title or "未知作品",
                "EPISODE_NO": str(episode_no),
                "PANEL_SHAPE": _tile_shape_text(tile),
            },
        )
        message = ChatMessage(
            role="user",
            content=[
                ContentPart(type="text", text=prompt),
                ContentPart(type="image_url", image_url=data_url),
            ],
        )
        response = await client.chat(
            [message], model=model, temperature=temperature, max_tokens=2000
        )
        payload = _extract_panel_payload(
            parse_json_response(response.content, finish_reason=response.finish_reason),
            panel_id=panel_id,
        )
        tile_payloads.append((tile, payload))

    merged = _merge_tile_payloads(panel_id, tile_payloads)
    normalized = normalize_panel_payload(merged, panel_id=panel_id)
    raw_non_story = merged.get("non_story_type")
    non_story_type = raw_non_story if raw_non_story in NON_STORY_TYPES else "none"
    normalized["non_story_type"] = non_story_type
    if non_story_type in NON_STORY_BANNER_TYPES:
        normalized["is_transition_blank"] = True
    try:
        return PanelVisionInfo.model_validate(normalized)
    except Exception as exc:  # pydantic ValidationError
        raise VisionError(f"面板 {panel_id} 的视觉结果不合法：{exc}") from exc


async def analyze_panel(
    client: LLMClient,
    *,
    image_path: str | Path,
    panel_id: str,
    model: str,
    characters: Any = (),
    series_title: str = "",
    episode_no: int = 0,
    template: str | None = None,
    temperature: float = 0.2,
) -> PanelVisionInfo:
    """分析单个面板。"""
    path = Path(image_path)
    tiles = _slice_tall_panel(path)
    if tiles:
        return await _analyze_tall_panel(
            client,
            tiles=tiles,
            panel_id=panel_id,
            model=model,
            characters=characters,
            series_title=series_title,
            episode_no=episode_no,
            template=template,
            temperature=temperature,
        )
    data_url = encode_image_data_url(path)
    prompt = render_prompt(
        template if template is not None else load_prompt_template(),
        {
            "CHARACTERS": format_character_block(characters) or "（暂无已知角色）",
            "PANEL_ID": panel_id,
            "SERIES_TITLE": series_title or "未知作品",
            "EPISODE_NO": str(episode_no),
            "PANEL_SHAPE": describe_panel_shape(path),
        },
    )
    message = ChatMessage(
        role="user",
        content=[
            ContentPart(type="text", text=prompt),
            ContentPart(type="image_url", image_url=data_url),
        ],
    )
    response = await client.chat(
        [message], model=model, temperature=temperature, max_tokens=2000
    )
    payload = _extract_panel_payload(
        parse_json_response(response.content, finish_reason=response.finish_reason),
        panel_id=panel_id,
    )
    normalized = normalize_panel_payload(payload, panel_id=panel_id)
    # Patch v2.5-1 第二重：core.normalize 不在补丁清单内、不处理 non_story_type，
    # 在此做白名单收敛；模型给出的非法/缺失值一律回落 "none"。
    raw_non_story = payload.get("non_story_type")
    non_story_type = raw_non_story if raw_non_story in NON_STORY_TYPES else "none"
    normalized["non_story_type"] = non_story_type
    if non_story_type in NON_STORY_BANNER_TYPES:
        # 版权声明/推广尾页同步标记过渡空页，下游统一按 is_transition_blank 过滤
        normalized["is_transition_blank"] = True
    try:
        return PanelVisionInfo.model_validate(normalized)
    except Exception as exc:  # pydantic ValidationError
        raise VisionError(f"面板 {panel_id} 的视觉结果不合法：{exc}") from exc


async def extract_episode_vision(
    client: LLMClient,
    *,
    panels_dir: str | Path,
    model: str,
    characters: Any = (),
    series_title: str = "",
    episode_no: int = 0,
    template: str | None = None,
    concurrency: int = 4,
    on_panel_error: str = "raise",
    failures: list[dict[str, str]] | None = None,
    blank_ids: set[str] | None = None,
) -> VisionWindowOutput:
    """并发分析整话所有面板，返回合并结果。

    ``on_panel_error="skip"`` 时单个面板抽取失败不再让整话崩掉，失败明细写入
    调用方传入的 ``failures`` 容器（由调用方落盘，不做静默丢弃）。

    Patch v3.0-6：``blank_ids`` 里的面板是 Stage 2 判定出的空镜（纯白/纯黑
    过渡页），**不调用视觉模型**，直接产出占位结果并在下游按
    ``is_transition_blank`` 过滤，避免「白屏空等」。
    """
    paths = list_panel_images(panels_dir)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    sink: list[dict[str, str]] = failures if failures is not None else []
    blanks = blank_ids or set()

    async def worker(path: Path) -> PanelVisionInfo | None:
        panel_id = panel_id_for(path)
        if panel_id in blanks:
            # 空镜：不需要也不应该花钱调模型
            return PanelVisionInfo(
                panel_id=panel_id,
                scene_summary="（空白过渡画面，无剧情内容）",
                dialogues=[],
                sfx_list=[],
                focus_region=None,
                is_transition_blank=True,
                non_story_type="transition_blank",
            )
        async with semaphore:
            try:
                return await analyze_panel(
                    client,
                    image_path=path,
                    panel_id=panel_id,
                    model=model,
                    characters=characters,
                    series_title=series_title,
                    episode_no=episode_no,
                    template=template,
                )
            except Exception as exc:
                if on_panel_error != "skip":
                    raise
                sink.append({"panel_id": panel_id, "error": str(exc)})
                return None

    results = await asyncio.gather(*(worker(path) for path in paths))
    panels = [item for item in results if item is not None]
    return VisionWindowOutput(panels=panels)


def _strip_punct(text: str) -> str:
    """去掉标点 / 空白后的净字符（判断「有效台词」用）。"""
    return _PUNCT_ONLY_RE.sub("", coerce_str(text))


def _has_dramatic_evidence(summary: str) -> bool:
    """``scene_summary`` 里是否写明了人物 / 躯体动作 / 空间。

    纯空间词（"走廊""病房"）不足以判定这是剧情格——空镜也会写地点，
    必须同时出现人物指代；人物与动作词则可以直接判定。
    """
    text = coerce_str(summary)
    if not text:
        return False
    if any(keyword in text for keyword in _DRAMATIC_PERSON_KEYWORDS):
        return True
    if any(keyword in text for keyword in _DRAMATIC_ACTION_KEYWORDS):
        return True
    if any(keyword in text for keyword in _DRAMATIC_SPACE_KEYWORDS) and any(
        marker in text for marker in _DRAMATIC_PERSON_MARKERS
    ):
        return True
    return False


def protect_dramatic_panels(panels: list[PanelVisionInfo]) -> list[PanelVisionInfo]:
    """暗黑高潮格护栏：把被误判成空页 / 纯字卡 / 纯拟声的高潮格救回来。

    Patch v4.0 Patch 18：深黑底 + 血红警示光 / 火光剪影的高潮剧情格，
    常被模型标成 ``is_transition_blank=true`` / ``is_pure_text_card=true`` /
    ``non_story_type="sfx_only"``，Stage 4 就会整格剔除——结尾的反转
    （绑匪倒下 → 火光后有人露脸 → 惊呼）就是这样被砍掉的。

    判据（任一成立即恢复成普通剧情格）：

    1. 该格有**有效台词**：去掉标点后仍 ≥ 3 字（纯拟声词「啪！」不算）；
    2. ``scene_summary`` 写明了人物 / 躯体动作 / 空间。

    版权声明页与推广尾页（``copyright_notice`` / ``promo_banner``）不在此列。
    """
    protected: list[PanelVisionInfo] = []
    for panel in panels:
        if panel.non_story_type in NON_STORY_BANNER_TYPES:
            protected.append(panel)
            continue
        has_dialogue = any(
            len(_strip_punct(bubble.text)) >= _DRAMATIC_DIALOGUE_MIN_CHARS
            for bubble in panel.dialogues
        )
        if has_dialogue or _has_dramatic_evidence(panel.scene_summary):
            panel.is_transition_blank = False
            panel.is_pure_text_card = False
            panel.non_story_type = "none"
        protected.append(panel)
    return protected


def vision_dir_for(series_dir: str | Path, episode_no: int) -> Path:
    """``03_vision`` 目录路径。"""
    return Path(series_dir) / "episodes" / f"ep_{episode_no:03d}" / VISION_DIR_NAME


async def run_stage3(
    series_dir: str | Path,
    episode_no: int,
    client: LLMClient,
    *,
    model: str,
    characters: Any = (),
    series_title: str = "",
    template: str | None = None,
    concurrency: int = 4,
    on_panel_error: str = "raise",
) -> Path:
    """执行 Stage 3 并把结果写入 ``03_vision/vision.json``，返回该文件路径。"""
    from stages.s2_segmenter import panels_dir_for  # 局部导入避免循环依赖

    series_root = Path(series_dir)
    panels_dir = panels_dir_for(series_root, episode_no)
    failures: list[dict[str, str]] = []
    output = await extract_episode_vision(
        client,
        panels_dir=panels_dir,
        model=model,
        characters=characters,
        series_title=series_title,
        episode_no=episode_no,
        template=template,
        concurrency=concurrency,
        on_panel_error=on_panel_error,
        failures=failures,
        blank_ids=blank_panel_ids(panels_dir),
    )
    target_dir = vision_dir_for(series_root, episode_no)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "vision.json"
    # Patch v4.0 Patch 18：暗黑高潮格护栏——落盘前把被误判成空页 / 纯字卡 /
    # 纯拟声格的高潮剧情格救回来（深黑底 + 红光剪影格最容易被整格剔除）。
    protect_dramatic_panels(output.panels)
    # Patch v3.0-6：先清掉上一轮的 vision.json，避免新旧 ID 并存时下游
    # 读到两套分镜（那正是「同一话播两遍」的根因之一）。
    for stale in (path, target_dir / "vision_failures.json"):
        if stale.exists():
            stale.unlink()
    save_model(output, path)
    if failures:
        # 不静默丢弃：失败明细单独落盘，并在 stderr 提示
        (target_dir / "vision_failures.json").write_text(
            json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for item in failures:
            print(
                f"[w2v] 面板 {item['panel_id']} 视觉抽取失败：{item['error']}",
                file=sys.stderr,
            )
    return path
