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
from typing import Any, Sequence

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

#: Patch v4.19：邻格参考图的缩略边长（当前格主图仍是 1280px）。
#: 邻格只是「上下文滑窗」，给 640px 足够模型看清说话人与动作延续，
#: 又不至于把每格的请求体积翻倍。
NEIGHBOR_MAX_EDGE = 640

# --------------------------------------------------------------------------
# Patch v4.22：qwen3.7-plus 原生多图「批窗」抽取
# --------------------------------------------------------------------------
#: 多图批窗抽取用的提示词文件名（单格 ``vision_extract.md`` 保持原样不动，
#: 以确保既有单格单测 100% 兼容）。
BATCH_PROMPT_FILENAME = "vision_extract_batch.md"

#: 关闭思考链：视觉抽取要的是结构化 JSON，不是长篇推理。百炼 qwen3 系默认
#: 开启 thinking，推理 token 会挤占 ``max_tokens``（Stage 4 早已统一关闭）。
_VISION_NO_THINKING_KWARGS: dict[str, object] = {"enable_thinking": False}

#: Patch v4.22：视觉模型降级链（模型 ID 下线 / 未开通时按顺序降级）。
#: 实测（2026-11-22）：``qwen-vl-max`` 这个 ID 在百炼已下线（404 Model not exist），
#: ``qwen3.7-plus`` 识图与多图均实测可用，故放在链首。
VISION_MODEL_FALLBACK_CHAIN: tuple[str, ...] = (
    "qwen3.7-plus",
    "qwen-vl-max",
    "qwen3-vl-plus",
)

#: 原生多图模型前缀：一次请求可以看多张图 → 可以按「格」批窗抽取。
_MULTI_IMAGE_MODEL_PREFIXES: tuple[str, ...] = (
    "qwen3.7",
    "qwen3.8",
    "qwen4",
    "deepseek-v4",
)

#: 经典单图视觉模型前缀：一次只吃一张图 → 保持逐格抽取（行为与旧版完全一致）。
_SINGLE_IMAGE_MODEL_PREFIXES: tuple[str, ...] = ("qwen3-vl-plus", "qwen-vl-max")

#: 多图模型的默认批窗大小（一次最多看 4 格）。
DEFAULT_VISION_BATCH_SIZE = 4

#: 批窗里每格的 max_tokens 预算（4 格 → 3600；下限 2000）。
VISION_BATCH_TOKENS_PER_PANEL = 900

#: Patch v2.5-1：non_story_type 合法取值（与 schemas.PanelVisionInfo 对齐）。
#: Patch v3.0-6：新增 ``sfx_only``——整格只有拟声词/气氛特效、没有人物动作与对白。
NON_STORY_TYPES = frozenset(
    {"none", "transition_blank", "copyright_notice", "promo_banner", "sfx_only"}
)
#: 命中这两类即视为非剧情尾页，下游统一按过渡空页过滤。
NON_STORY_BANNER_TYPES = frozenset({"copyright_notice", "promo_banner"})
#: Patch v3.0-6：只含拟声词特效的画格（同样不进剧本）。
NON_STORY_SFX_TYPES = frozenset({"sfx_only"})

# --------------------------------------------------------------------------
# Patch v4.44：动作向分镜标记（连招蒙太奇的输入）
# --------------------------------------------------------------------------
#: 画面摘要里出现这些词 → 判定为动作向分镜（模型没给 is_action_beat 时的兜底）。
#:
#: 只在**画面没有任何对白**时才生效：动作格与对白格天然互斥。
ACTION_BEAT_KEYWORDS: tuple[str, ...] = (
    "搏斗",
    "格斗",
    "厮杀",
    "打斗",
    "交手",
    "过招",
    "缠斗",
    "对打",
    "连击",
    "出拳",
    "一拳",
    "挥拳",
    "格挡",
    "闪避",
    "躲闪",
    "飞踢",
    "侧踢",
    "重击",
    "横扫",
    "劈砍",
    "挥刀",
    "兵器",
    "刀剑",
    "长剑",
    "扑向",
    "撞向",
    "揪住",
    "掐住",
)


def infer_action_beat(payload: dict[str, Any]) -> bool:
    """判定一格是不是「动作向分镜」（Patch v4.44）。

    * 模型显式给了 ``is_action_beat`` → 直接采信（并收敛为 bool）；
    * 模型没给（旧 vision.json / 老模型输出）→ **兼容兜底**：
      画面**没有任何对白**、且摘要里命中 :data:`ACTION_BEAT_KEYWORDS` 时判 True。
    """
    raw = payload.get("is_action_beat")
    if raw is not None:
        return bool(raw)
    dialogues = payload.get("dialogues")
    if isinstance(dialogues, list):
        has_dialogue = any(
            str((item or {}).get("text") or "").strip()
            for item in dialogues
            if isinstance(item, dict)
        )
        if has_dialogue:
            return False
    summary = str(payload.get("scene_summary") or payload.get("summary") or "")
    return any(keyword in summary for keyword in ACTION_BEAT_KEYWORDS)

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


# --------------------------------------------------------------------------
# Patch v4.20：纯文字卡判定（画面上只有字，不是剧情画面）
# --------------------------------------------------------------------------
#: 章节标题条 / 字幕条 / 纯文字 PPT 这类格子一旦留在候选池里，Stage 6 会
#: 把它当成一帧画面渲进成片（实测：b9 与 b10 之间多出一条粉色标题条）。
#: 模型自己常常不标 ``is_pure_text_card``，所以这里用确定性判据再认一遍。

#: ``scene_summary`` 里「画面上写着字」的描述词。
_TEXT_CARD_SUMMARY_KEYWORDS: tuple[str, ...] = (
    "文字", "标题", "字幕", "标语", "文本", "标识", "字样", "问句",
    "行字", "行文字",
)

#: 更强的信号：摘要明说「这就是一张文字卡」。
_TEXT_CARD_EXPLICIT_MARKERS: tuple[str, ...] = (
    "纯文字", "文字卡片", "文字卡", "纯字卡", "纯文字卡", "整幅都是文字",
    "只有文字", "全是文字", "文字条", "标题条", "字幕条", "满屏文字", "整幅文字",
)

#: 画了**对话气泡**（圆/椭圆、带尾巴的气球）的格子是这一话的正式画面，
#: 哪怕整格只有气泡没有人物，也不能按纯文字卡剔除——那会把这一话真正的
#: 剧情画面砍掉（实测 wt_3924 有一话大量「纯白背景 + 对话气泡」的画风）。
_TEXT_CARD_SPEECH_BALLOON_MARKERS: tuple[str, ...] = (
    "气泡", "带尾巴", "椭圆形", "圆形对话", "圆形气泡", "尖角尾巴", "尖刺边缘",
)

#: 「方框 + 文字」的引用卡（内心独白卡 / 叙述框），不是对话气泡 → 算纯字卡。
_TEXT_CARD_BOX_MARKERS: tuple[str, ...] = (
    "矩形框", "矩形文字框", "矩形对话框", "文字框", "文本框", "框内",
)

#: 摘要里明说「没有气泡」——这时不能因为出现「气泡」二字就当成对话气泡。
_NO_SPEECH_BALLOON_MARKERS: tuple[str, ...] = (
    "无气泡", "没有气泡", "不含气泡", "无对话框", "没有对话框", "无对白框",
    "未画气泡", "并非气泡",
)

#: 「一整块底色 + 上面写着字」的平面文字帧（章节标题条 / 字幕条 / 出版方标识）。
_FLAT_BACKGROUND_MARKERS: tuple[str, ...] = (
    "纯黑背景", "纯白背景", "粉色背景", "浅粉背景", "白色背景", "黑色背景",
    "白底", "黑底", "纯色背景", "单色背景", "底色",
    "背景上显示", "背景中央显示", "背景中央", "背景上写着", "背景右上角",
    "空白色背景",
)

#: 摘要明说「画面里什么都没有」（纯字卡的典型描述）。
_TEXT_CARD_EMPTY_MARKERS: tuple[str, ...] = (
    "无任何人物", "无任何角色", "没有任何人物", "没有人物", "无角色", "无人物",
    "无动作", "无气泡", "无背景", "无任何背景", "无环境元素", "无任何环境元素",
    "无背景细节", "无任何背景细节", "无任何人物、动作或环境元素",
)

#: 「画面上的字」类说话人：模型把画中文字当成台词时用的说话人。
_TEXT_CARD_SPEAKERS: frozenset[str] = frozenset({
    "旁白", "字幕", "文字", "标题", "文案", "画面", "画面文字", "旁白文字",
    "说明", "说明文字", "narration", "narrator", "caption", "subtitle", "text",
})


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


# --------------------------------------------------------------------------
# Patch v4.23：超扁窄条（出版方水印条 / 画格缝隙）不是漫画画面
# --------------------------------------------------------------------------
#: 超扁窄条的最大高度（像素）。与 Stage 2 ``THIN_STRIP_MAX_HEIGHT_PX`` 对齐。
ULTRA_THIN_PANEL_MAX_HEIGHT_PX = 150
#: 超扁窄条的最小宽高比。与 Stage 2 ``THIN_STRIP_MIN_ASPECT_RATIO`` 对齐。
ULTRA_THIN_PANEL_MIN_ASPECT_RATIO = 7.5


def is_ultra_thin_panel_geometry(width: int, height: int) -> bool:
    """几何判据：这块面板是不是「超扁窄条」（出版方水印条 / 画格缝隙）。

    实测 wt_5081 第 5 话：``p_088`` 是 690×61 的出版方水印白条（只有
    "IAFDAM MEDIA" 角标）。Stage 2 之所以没把它判成装饰条，是因为水印
    logo 把 Canny 边缘密度顶高（0.0353 ≥ 0.015），而近乎全白又触发了
    「含文字」的豁免——于是它被当成普通分镜，一路混进分镜编排，在 b80 与
    b81 之间渲染成整整一帧灰底画面。

    这里**只看几何不看像素**：水印条的高度与宽高比本身就是「不是漫画画面」
    的可靠信号，不该依赖像素内容判断。
    """
    if width <= 0 or height <= 0:
        return False
    if height > ULTRA_THIN_PANEL_MAX_HEIGHT_PX:
        return False
    return (width / height) >= ULTRA_THIN_PANEL_MIN_ASPECT_RATIO


def ultra_thin_panel_ids(panels_dir: str | Path) -> set[str]:
    """``panels.json`` 里属于「超扁窄条」的 panel_id 集合。

    读不到清单 / 尺寸缺失时返回空集合——宁可放过，也不要因为旧产物格式
    变化把整话判废。
    """
    result: set[str] = set()
    for panel_id, item in load_panels_manifest(panels_dir).items():
        try:
            width = int(item.get("width") or 0)
            height = int(item.get("height") or 0)
        except (TypeError, ValueError):
            continue
        if is_ultra_thin_panel_geometry(width, height):
            result.add(panel_id)
    return result


def panel_id_for(path: str | Path) -> str:
    """面板编号 = 文件名主干（如 ``p_001``）。

    Stage 2 现在直接把面板写成 ``p_001.png``，``PanelMeta.panel_id`` 也是
    ``p_001``；Stage 3 **原样透传**这个编号，不再私自改名成 ``panel_001``，
    两端 ID 从此同轨（详见 Patch v2.6 的命名统一）。
    """
    return Path(path).stem


# --------------------------------------------------------------------------
# Patch v4.19：邻格上下文滑窗
# --------------------------------------------------------------------------
#: 邻格标注里反复出现的核心约束（提示词与图片分片标注共用同一句）。
NEIGHBOR_RULE_TEXT = (
    "仅用于追踪说话人延续与动作因果，禁止将邻格画面或台词写入当前格 JSON。"
)


def _last_sentence(text: str) -> str:
    """取最后一句（按中文/英文句末标点切分）。"""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    parts = [item.strip() for item in re.split(r"[。！？!?\n]+", cleaned) if item.strip()]
    if not parts:
        return cleaned
    return parts[-1]


def _neighbor_reference_label(kind: str, panel_id: str) -> str:
    """邻格缩略图前面的文字标注（紧贴图片，避免模型张冠李戴）。"""
    label = "前一格" if kind == "prev" else "后一格"
    return (
        f"【{label}参考图 · {panel_id}】下面这张缩略图是本格的{label}，"
        f"{NEIGHBOR_RULE_TEXT}"
    )


def _neighbor_context_block(
    *,
    prev_panel_id: str = "",
    next_panel_id: str = "",
    extra: str = "",
) -> str:
    """渲染 ``{{NEIGHBOR_CONTEXT}}``：邻格参考说明（没有邻格时给兜底文案）。"""
    lines: list[str] = ["【邻格参考（仅作上下文，不属于本格）】"]
    if prev_panel_id:
        lines.append(f"- 第 2 张图：前一格（{prev_panel_id}）的缩略图。")
    if next_panel_id:
        index = 3 if prev_panel_id else 2
        lines.append(f"- 第 {index} 张图：后一格（{next_panel_id}）的缩略图。")
    if prev_panel_id or next_panel_id:
        lines.append(f"- 邻格{NEIGHBOR_RULE_TEXT}")
    else:
        lines.append(
            "- 本格没有可用的邻格参考图（相邻是过渡空页，或本格是话首/话尾），"
            "请只依据当前画面作答。"
        )
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def _neighbor_reference_parts(
    prev_image_path: str | Path | None,
    next_image_path: str | Path | None,
) -> list[ContentPart]:
    """邻格参考图的内容分片（文字标注 + 640px 缩略图）。

    Patch v4.19：当前格主图永远是第 1 张图片，邻格缩略图追加在后面——
    既不新增 LLM 调用次数，也不改变主图分辨率。
    """
    parts: list[ContentPart] = []
    for kind, neighbor in (("prev", prev_image_path), ("next", next_image_path)):
        if not neighbor:
            continue
        path = Path(neighbor)
        try:
            data_url = encode_image_data_url(path, max_edge=NEIGHBOR_MAX_EDGE)
        except Exception:  # noqa: BLE001 - 邻格读失败不阻断本格抽取
            continue
        parts.append(
            ContentPart(type="text", text=_neighbor_reference_label(kind, path.stem))
        )
        parts.append(ContentPart(type="image_url", image_url=data_url))
    return parts


def _neighbor_paths(
    paths: Sequence[Path],
    blanks: set[str],
) -> dict[str, tuple[Path | None, Path | None]]:
    """每格的（前一格路径, 后一格路径）。

    Patch v4.19：**相邻是过渡空页时置空**，不跨空页取邻格——空页意味着
    场景已经切换，跨过去会把上一段剧情的说话人带进本格。
    """
    ids = [panel_id_for(path) for path in paths]
    result: dict[str, tuple[Path | None, Path | None]] = {}
    for index, path in enumerate(paths):
        prev_path: Path | None = None
        next_path: Path | None = None
        if index > 0 and ids[index - 1] not in blanks:
            prev_path = paths[index - 1]
        if index + 1 < len(paths) and ids[index + 1] not in blanks:
            next_path = paths[index + 1]
        result[ids[index]] = (prev_path, next_path)
    return result


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


def _vision_fallback_candidates(primary_model: str) -> list[str]:
    """视觉模型降级候选（按 :data:`VISION_MODEL_FALLBACK_CHAIN` 的顺序）。"""
    return [item for item in VISION_MODEL_FALLBACK_CHAIN if item != primary_model]


async def _call_vision_chat(
    client: LLMClient,
    *,
    messages: list[ChatMessage],
    model: str,
    temperature: float = 0.2,
    max_tokens: int = 2000,
) -> Any:
    """统一封装视觉模型调用（Patch v4.22）。

    * 每次调用都透传 ``enable_thinking=False``——视觉抽取要的是结构化 JSON，
      思考链只会挤占 ``max_tokens``（Stage 4 早已统一关闭，Stage 3 此前漏了）。
    * 模型 ID 下线 / 未开通（复用 :func:`stages.s4_scriptwriter.is_model_access_error`）
      时按 :data:`VISION_MODEL_FALLBACK_CHAIN` 自动降级；其余异常原样抛出。
    """
    from stages.s4_scriptwriter import is_model_access_error  # 局部导入避免循环依赖

    last_error: Exception | None = None
    for candidate in [model, *_vision_fallback_candidates(model)]:
        try:
            return await client.chat(
                messages,
                model=candidate,
                temperature=temperature,
                max_tokens=max_tokens,
                **_VISION_NO_THINKING_KWARGS,
            )
        except Exception as exc:  # noqa: BLE001 - 按错误类型决定是否降级
            if not is_model_access_error(exc):
                raise
            last_error = exc
            print(
                f"[s3_vision] 视觉模型 {candidate} 不可用（{exc}），按降级链尝试下一个模型。",
                file=sys.stderr,
            )
    assert last_error is not None  # pragma: no cover - 循环至少执行一次
    raise last_error


def resolve_vision_batch_size(model: str, batch_size: int | None = None) -> int:
    """这个视觉模型一次能看几张图（Patch v4.22）。

    * 原生多图模型（``qwen3.7`` / ``qwen3.8`` / ``qwen4`` / ``deepseek-v4`` 前缀）
      默认按 :data:`DEFAULT_VISION_BATCH_SIZE`（4 格）批窗抽取。
    * 经典单图视觉模型（``qwen3-vl-plus`` / ``qwen-vl-max``）默认 1——保持既有
      单格调用行为不变（既有单测 100% 兼容）。
    * 显式传入 ``batch_size`` 时以传入值为准（小于 1 视为未传）。
    """
    if batch_size is not None:
        try:
            explicit = int(batch_size)
        except (TypeError, ValueError):
            explicit = 0
        if explicit >= 1:
            return explicit
    name = str(model or "").strip().lower()
    if any(name.startswith(prefix) for prefix in _SINGLE_IMAGE_MODEL_PREFIXES):
        return 1
    if any(name.startswith(prefix) for prefix in _MULTI_IMAGE_MODEL_PREFIXES):
        return DEFAULT_VISION_BATCH_SIZE
    return 1


def _panel_info_from_payload(payload: dict[str, Any], *, panel_id: str) -> PanelVisionInfo:
    """把模型返回的原始 JSON 规整成 :class:`PanelVisionInfo`。

    单格抽取（:func:`analyze_panel`）与批窗抽取（:func:`_analyze_panel_batch`）
    共用同一套规整逻辑，避免两条路径的字段口径漂移。
    """
    normalized = normalize_panel_payload(payload, panel_id=panel_id)
    # Patch v4.44：动作向分镜标记（连招蒙太奇的输入；旧数据走关键词兜底）。
    normalized["is_action_beat"] = infer_action_beat(payload)
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


def _ensure_vision_content(response: Any, *, model: str) -> str:
    """确保视觉模型真的回了内容（Patch v4.22）。

    有些「能聊天但不看图」的模型（例如纯文本模型被误选成视觉模型）会返回
    HTTP 200 + 空字符串内容。若放任它继续，后面的 JSON 解析只会报
    「模型返回内容不是合法 JSON：''」，完全指不出真正的原因。这里提前拦下，
    直接把「模型不支持图片输入」这件事讲清楚。
    """
    content = str(getattr(response, "content", "") or "").strip()
    if not content:
        raise VisionError(
            f"视觉模型 {model} 返回空内容（该模型很可能不支持图片输入，"
            "请改用多模态视觉模型）"
        )
    return content


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
    #: 每个「有效切片」（非空白）是否自报动作向分镜（Patch v4.44）。
    valid_action_flags: list[bool] = []

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
            valid_action_flags.append(infer_action_beat(payload))
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
        # Patch v4.44：任一有效切片是动作向分镜、且整格没有对白 → 整格算动作向。
        "is_action_beat": bool(valid_action_flags)
        and any(valid_action_flags)
        and not dialogues,
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
    prev_image_path: str | Path | None = None,
    next_image_path: str | Path | None = None,
) -> PanelVisionInfo:
    """超高画格：逐片喂模型，再把各片结果合并成同一 ``panel_id``。

    Patch v4.19：从第 2 片起，把上一片的**最后一句摘要**与**最后一位说话人**
    一起带进提示词——同一格被纵向切开后，模型默认「每片都从零开始看」，
    容易把同一个人认成不同角色、把连续动作断成两截。
    """
    prompt_template = template if template is not None else load_prompt_template()
    prev_id = Path(prev_image_path).stem if prev_image_path else ""
    next_id = Path(next_image_path).stem if next_image_path else ""
    neighbor_parts = _neighbor_reference_parts(prev_image_path, next_image_path)
    tile_payloads: list[tuple[PanelTile, dict[str, Any]]] = []
    carry_summary = ""
    carry_speaker = ""
    for tile in tiles:
        extra = ""
        if tile.index > 0:
            extra = (
                f"- 上一片（同一格第 {tile.index} 片）末尾摘要：{carry_summary or '（无）'}\n"
                f"- 上一片最后一位说话人：{carry_speaker or '（无）'}"
            )
        data_url = encode_pil_data_url(tile.image)
        prompt = render_prompt(
            prompt_template,
            {
                "CHARACTERS": format_character_block(characters) or "（暂无已知角色）",
                "PANEL_ID": panel_id,
                "SERIES_TITLE": series_title or "未知作品",
                "EPISODE_NO": str(episode_no),
                "PANEL_SHAPE": _tile_shape_text(tile),
                "NEIGHBOR_CONTEXT": _neighbor_context_block(
                    prev_panel_id=prev_id,
                    next_panel_id=next_id,
                    extra=extra,
                ),
            },
        )
        message = ChatMessage(
            role="user",
            content=[
                ContentPart(type="text", text=prompt),
                ContentPart(type="image_url", image_url=data_url),
                *neighbor_parts,
            ],
        )
        response = await _call_vision_chat(
            client,
            messages=[message],
            model=model,
            temperature=temperature,
            max_tokens=2000,
        )
        payload = _extract_panel_payload(
            parse_json_response(
                _ensure_vision_content(response, model=model),
                finish_reason=response.finish_reason,
            ),
            panel_id=panel_id,
        )
        tile_payloads.append((tile, payload))
        # 下一片携带本片的「最后一句摘要 + 最后一位说话人」。
        carry_summary = _last_sentence(str(payload.get("scene_summary") or ""))
        dialogues = payload.get("dialogues") or []
        last_dialogue = dialogues[-1] if dialogues else {}
        carry_speaker = str(last_dialogue.get("speaker_guess") or "").strip()

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
    prev_image_path: str | Path | None = None,
    next_image_path: str | Path | None = None,
) -> PanelVisionInfo:
    """分析单个面板。

    Patch v4.19：可选带上**紧邻的前一格 / 后一格**缩略图作为上下文滑窗。
    第 1 张图片永远是当前格（1280px 主图），邻格缩略图（640px）追加在后面，
    因此单格调用行为与既有单测保持完全兼容。
    """
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
            prev_image_path=prev_image_path,
            next_image_path=next_image_path,
        )
    data_url = encode_image_data_url(path)
    prev_id = Path(prev_image_path).stem if prev_image_path else ""
    next_id = Path(next_image_path).stem if next_image_path else ""
    prompt = render_prompt(
        template if template is not None else load_prompt_template(),
        {
            "CHARACTERS": format_character_block(characters) or "（暂无已知角色）",
            "PANEL_ID": panel_id,
            "SERIES_TITLE": series_title or "未知作品",
            "EPISODE_NO": str(episode_no),
            "PANEL_SHAPE": describe_panel_shape(path),
            "NEIGHBOR_CONTEXT": _neighbor_context_block(
                prev_panel_id=prev_id,
                next_panel_id=next_id,
            ),
        },
    )
    message = ChatMessage(
        role="user",
        content=[
            ContentPart(type="text", text=prompt),
            ContentPart(type="image_url", image_url=data_url),
            *_neighbor_reference_parts(prev_image_path, next_image_path),
        ],
    )
    response = await _call_vision_chat(
        client,
        messages=[message],
        model=model,
        temperature=temperature,
        max_tokens=2000,
    )
    payload = _extract_panel_payload(
        parse_json_response(
            _ensure_vision_content(response, model=model),
            finish_reason=response.finish_reason,
        ),
        panel_id=panel_id,
    )
    return _panel_info_from_payload(payload, panel_id=panel_id)


def _is_tall_panel(image_path: str | Path) -> bool:
    """该画格是否是超高竖长画格（要纵向切片，不能进批窗）。

    只读图片尺寸，不做解码切片——分批规划阶段每格都要问一次。
    """
    try:
        with Image.open(image_path) as raw:
            width, height = raw.size
    except Exception:  # pragma: no cover - 图片损坏时按普通格处理
        return False
    return bool(width and height and height > TALL_PANEL_SLICE_THRESHOLD)


async def _analyze_panel_batch(
    client: LLMClient,
    *,
    paths: list[Path],
    panel_ids: list[str],
    model: str,
    characters: Any = (),
    series_title: str = "",
    episode_no: int = 0,
    template: str | None = None,
    temperature: float = 0.2,
    prev_context_path: str | Path | None = None,
) -> dict[str, PanelVisionInfo]:
    """一次请求抽取 2~4 格（Patch v4.22，qwen3.7-plus 原生多图）。

    请求体 = 1 份提示词 + 至多 1 张**前格 640px 上下文参考图** + N 张待抽
    **1280px 主图**（顺序与 ``panel_ids`` 一一对应）。

    返回 ``{panel_id: PanelVisionInfo}``：模型少返回 / 返回错的 panel_id 不会
    出现在结果里，由调用方**逐格回退**补抽，确保 100% 不丢格。
    """
    if not paths or len(paths) != len(panel_ids):
        raise VisionError("批量抽取参数不一致（图片数与面板 ID 数不等）")
    prompt_template = (
        template if template is not None else load_prompt_template(BATCH_PROMPT_FILENAME)
    )
    prev_id = Path(prev_context_path).stem if prev_context_path else ""
    if prev_id:
        prev_note = (
            f"- 第 1 张图是**参考前格 `{prev_id}` 的缩略图**，只用来认人与延续上一格的"
            f"说话人 / 动作，**禁止**把它的画面或台词写进下面任何一个面板对象。\n"
            f"- 第 2~{len(paths) + 1} 张图依次对应待抽面板：{', '.join(panel_ids)}。"
        )
    else:
        prev_note = (
            f"- 本次**没有**参考前格；第 1~{len(paths)} 张图依次对应待抽面板："
            f"{', '.join(panel_ids)}。"
        )
    prompt = render_prompt(
        prompt_template,
        {
            "CHARACTERS": format_character_block(characters) or "（暂无已知角色）",
            "SERIES_TITLE": series_title or "未知作品",
            "EPISODE_NO": str(episode_no),
            "TARGET_PANEL_IDS": ", ".join(panel_ids),
            "PANEL_COUNT": str(len(panel_ids)),
            "PREV_CONTEXT_NOTE": prev_note,
        },
    )
    parts: list[ContentPart] = [ContentPart(type="text", text=prompt)]
    if prev_context_path:
        try:
            parts.append(
                ContentPart(
                    type="image_url",
                    image_url=encode_image_data_url(
                        prev_context_path, max_edge=NEIGHBOR_MAX_EDGE
                    ),
                )
            )
        except Exception:  # pragma: no cover - 前格读不到就当没有，不阻断
            pass
    for path in paths:
        parts.append(ContentPart(type="image_url", image_url=encode_image_data_url(path)))
    message = ChatMessage(role="user", content=parts)
    response = await _call_vision_chat(
        client,
        messages=[message],
        model=model,
        temperature=temperature,
        max_tokens=max(2000, len(panel_ids) * VISION_BATCH_TOKENS_PER_PANEL),
    )
    data = parse_json_response(
        _ensure_vision_content(response, model=model),
        finish_reason=response.finish_reason,
    )
    if not isinstance(data, dict):
        raise VisionError(f"模型返回的 JSON 结构异常：{type(data).__name__}")
    panels = data.get("panels")
    if not isinstance(panels, list) or not panels:
        raise VisionError("模型返回的 panels 为空")
    wanted = set(panel_ids)
    returned_ids: list[str] = []
    for item in panels:
        if isinstance(item, dict):
            returned_ids.append(str(item.get("panel_id") or "").strip())
        else:
            returned_ids.append("")
    # 模型给的 panel_id 全部合法且不重复 → 按 id 归档（能容忍「只回了几格」）；
    # 否则退回「按数组顺序对应」（提示词要求顺序严格一一对应）。
    by_id = (
        len(returned_ids) == len(set(returned_ids))
        and all(pid in wanted for pid in returned_ids)
    )
    found: dict[str, PanelVisionInfo] = {}
    for index, item in enumerate(panels):
        if not isinstance(item, dict):
            continue
        if by_id:
            panel_id = returned_ids[index]
        elif index < len(panel_ids):
            panel_id = panel_ids[index]
        else:
            continue
        if panel_id in found:
            continue
        payload = dict(item)
        payload["panel_id"] = panel_id
        found[panel_id] = _panel_info_from_payload(payload, panel_id=panel_id)
    return found


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
    batch_size: int | None = None,
    batch_template: str | None = None,
) -> VisionWindowOutput:
    """并发分析整话所有面板，返回合并结果。

    ``on_panel_error="skip"`` 时单个面板抽取失败不再让整话崩掉，失败明细写入
    调用方传入的 ``failures`` 容器（由调用方落盘，不做静默丢弃）。

    Patch v3.0-6：``blank_ids`` 里的面板是 Stage 2 判定出的空镜（纯白/纯黑
    过渡页），**不调用视觉模型**，直接产出占位结果并在下游按
    ``is_transition_blank`` 过滤，避免「白屏空等」。

    Patch v4.19：为每格附带紧邻的**前一格 / 后一格**缩略图（上下文滑窗），
    用于跨格追踪说话人延续与动作因果；相邻是过渡空页时不跨空页取引用。
    邻格不新增 LLM 调用次数——每格仍然只调用模型一次。

    Patch v4.22：原生多图模型（``qwen3.7-plus`` 等）走**顺序批窗抽取**——
    连续 2~4 个普通格合成一次请求（1 份提示词 + 至多 1 张前格 640px 参考图
    + N 张待抽 1280px 主图），请求次数约为逐格模式的 1/3~1/4。
    断批规则：遇到过渡空页 / 超高竖长画格（要走纵向切片）就断开；
    批次异常或模型少返回某几格时，**只对缺失的格子逐格回退**补抽，不丢格。

    Patch v4.23：出版方水印条这类**超扁窄条**直接当空镜处理（不烧 VLM），
    否则它会被多图批窗「串味」成相邻格的翻版，一路混进成片。
    """
    paths = list_panel_images(panels_dir)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    sink: list[dict[str, str]] = failures if failures is not None else []
    blanks = set(blank_ids or set()) | ultra_thin_panel_ids(panels_dir)
    # Patch v4.19：为每格算出紧邻的前一格 / 后一格参考图（跨过渡空页不串格）。
    neighbors = _neighbor_paths(paths, blanks)
    # Patch v4.22：多图模型按批窗抽取，单图模型保持逐格（行为与旧版完全一致）。
    effective_batch = resolve_vision_batch_size(model, batch_size)
    results: dict[str, PanelVisionInfo | None] = {}

    async def analyze_one(path: Path) -> PanelVisionInfo | None:
        """单格抽取（失败处置与旧版一致）。"""
        panel_id = panel_id_for(path)
        prev_path, next_path = neighbors.get(panel_id, (None, None))
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
                prev_image_path=prev_path,
                next_image_path=next_path,
            )
        except Exception as exc:
            if on_panel_error != "skip":
                raise
            sink.append({"panel_id": panel_id, "error": str(exc)})
            return None

    async def analyze_group(group: list[Path]) -> None:
        """处理一组面板：1 格走单格，2~4 格走批窗。"""
        if len(group) == 1:
            path = group[0]
            async with semaphore:
                results[panel_id_for(path)] = await analyze_one(path)
            return
        panel_ids = [panel_id_for(path) for path in group]
        prev_path = neighbors.get(panel_ids[0], (None, None))[0]
        found: dict[str, PanelVisionInfo] = {}
        try:
            async with semaphore:
                found = await _analyze_panel_batch(
                    client,
                    paths=group,
                    panel_ids=panel_ids,
                    model=model,
                    characters=characters,
                    series_title=series_title,
                    episode_no=episode_no,
                    template=batch_template,
                    prev_context_path=prev_path,
                )
        except Exception:
            # 整批失败 → 逐格回退（单格再失败时按 on_panel_error 处置）。
            if on_panel_error != "skip":
                for path in group:
                    async with semaphore:
                        results[panel_id_for(path)] = await analyze_one(path)
                return
            found = {}
        # 缺失的格子逐格回退补抽（模型只回了一部分 / 批次异常后的恢复）。
        for path in group:
            panel_id = panel_id_for(path)
            if panel_id in found:
                results[panel_id] = found[panel_id]
                continue
            async with semaphore:
                results[panel_id] = await analyze_one(path)

    # 分组：空镜直接出占位；超高画格独立成组（要纵向切片，不能进批窗）。
    groups: list[list[Path]] = []
    current: list[Path] = []
    for path in paths:
        panel_id = panel_id_for(path)
        if panel_id in blanks:
            # 空镜：不需要也不应该花钱调模型
            results[panel_id] = PanelVisionInfo(
                panel_id=panel_id,
                scene_summary="（空白过渡画面，无剧情内容）",
                dialogues=[],
                sfx_list=[],
                focus_region=None,
                is_transition_blank=True,
                non_story_type="transition_blank",
            )
            continue
        if effective_batch > 1 and _is_tall_panel(path):
            if current:
                groups.append(current)
                current = []
            groups.append([path])
            continue
        current.append(path)
        if len(current) >= effective_batch:
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    await asyncio.gather(*(analyze_group(group) for group in groups))
    panels = [
        results[panel_id_for(path)]
        for path in paths
        if panel_id_for(path) in results
    ]
    return VisionWindowOutput(panels=[item for item in panels if item is not None])


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


def is_on_screen_text_speaker(speaker_guess: str) -> bool:
    """这个说话人是不是「画面上的字」而不是角色。

    模型把画中文字（章节标题条、字幕条）当台词时，说话人常写成「旁白」
    「字幕」「文字」「标题」等；这些「台词」不是角色说的话，不能拿来判断
    这一格是不是剧情格。
    """
    text = coerce_str(speaker_guess).strip().lower()
    if not text:
        return False
    if text in _TEXT_CARD_SPEAKERS:
        return True
    return any(marker in text for marker in ("字幕", "文字", "标题", "文案", "旁白"))


def is_text_only_card(panel: Any) -> bool:
    """这一格是不是「画面上只有文字」的纯文字卡（Patch v4.20）。

    判定只依据 ``scene_summary``，**不看**模型给的 ``is_pure_text_card``
    （那个标记正是「暗黑高潮格护栏」要推翻的，拿它当输入会误伤深黑底火光格）。

    判定顺序（任一条命中即判定为纯文字卡）：

    1. 摘要明说「纯文字卡片 / 只有文字 / 文字条」这类平面文字帧；
    2. 摘要写着画面里有字，**且**画面是「一整块底色 + 字」
       （纯黑底、纯白底、粉底标题条……），或明说画面里空无一物
       （无人物 / 无动作 / 无气泡……），或就是一个「方框 + 字」的引用卡。

    两种情况**不算**纯文字卡（必须保留，否则会砍掉真正的剧情画面）：

    - 画面上画了对话气泡（圆/椭圆/带尾巴的气球）——哪怕整格只有气泡；
    - 画面上的字有明确角色归属（角色台词、角色内心独白）。
    """
    summary = coerce_str(getattr(panel, "scene_summary", ""))
    if not summary:
        return False
    # 1) 强信号：摘要明说这就是一张文字卡。
    if any(marker in summary for marker in _TEXT_CARD_EXPLICIT_MARKERS):
        return True
    # 2) 摘要压根没提「文字 / 标题 / 字幕……」→ 不是字卡。
    if not any(marker in summary for marker in _TEXT_CARD_SUMMARY_KEYWORDS):
        return False
    # 3) 画了对话气泡 → 这一话的正式画面，保留（摘要明说「无气泡」时不算）。
    if (
        any(marker in summary for marker in _TEXT_CARD_SPEECH_BALLOON_MARKERS)
        and not any(marker in summary for marker in _NO_SPEECH_BALLOON_MARKERS)
    ):
        return False
    # 4) 画面里有真实的人 / 动作 / 场景 → 是插画，不是字卡（哪怕角落有个文字框）。
    if _has_dramatic_evidence(summary):
        return False
    # 5) 画面上的字有角色归属（角色台词 / 角色内心独白）→ 有角色信息，保留。
    dialogues = list(getattr(panel, "dialogues", ()) or ())
    if any(
        not is_on_screen_text_speaker(getattr(bubble, "speaker_guess", ""))
        for bubble in dialogues
    ):
        return False
    # 6) 其余：文字 + 平面底色 / 空无一物 / 方框引用卡 → 判定为纯文字卡。
    return (
        any(marker in summary for marker in _FLAT_BACKGROUND_MARKERS)
        or any(marker in summary for marker in _TEXT_CARD_EMPTY_MARKERS)
        or any(marker in summary for marker in _TEXT_CARD_BOX_MARKERS)
    )


def protect_dramatic_panels(panels: list[PanelVisionInfo]) -> list[PanelVisionInfo]:
    """暗黑高潮格护栏：把被误判成空页 / 纯字卡 / 纯拟声的高潮格救回来。

    Patch v4.0 Patch 18：深黑底 + 血红警示光 / 火光剪影的高潮剧情格，
    常被模型标成 ``is_transition_blank=true`` / ``is_pure_text_card=true`` /
    ``non_story_type="sfx_only"``，Stage 4 就会整格剔除——结尾的反转
    （绑匪倒下 → 火光后有人露脸 → 惊呼）就是这样被砍掉的。

    判据（任一成立即恢复成普通剧情格）：

    1. 该格有**有效台词**：去掉标点后仍 ≥ 3 字（纯拟声词「啪！」不算），
       且说话人不是「画面上的字」（旁白 / 字幕 / 文案……）；
    2. ``scene_summary`` 写明了人物 / 躯体动作 / 空间。

    版权声明页与推广尾页（``copyright_notice`` / ``promo_banner``）不在此列。

    Patch v4.20：画面上只有文字的格子（章节标题条 / 字幕条）**不救回**——
    它不是剧情格，救回来只会让 Stage 6 把它当一帧画面渲进成片。
    """
    protected: list[PanelVisionInfo] = []
    for panel in panels:
        if panel.non_story_type in NON_STORY_BANNER_TYPES:
            protected.append(panel)
            continue
        if is_text_only_card(panel):
            # 纯文字卡：标记成纯字卡后跳过护栏，绝不救回成剧情格。
            panel.is_pure_text_card = True
            protected.append(panel)
            continue
        has_dialogue = any(
            len(_strip_punct(bubble.text)) >= _DRAMATIC_DIALOGUE_MIN_CHARS
            and not is_on_screen_text_speaker(bubble.speaker_guess)
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
    # Patch v4.22：视觉抽取**全军覆没**时立刻中止，不再落盘一份空 vision。
    #
    # 以前的行为是「失败也照常落盘」：模型 ID 下线（比如百炼 404 Model not
    # exist）会让整话面板全部抽取失败，磁盘上却只剩几张空白过渡页占位，
    # 下游 Stage 4 拿着空候选池白跑三轮 LLM，最后报一个完全误导人的
    # 「模型没有产出任何可用的解说句（beats 为空）」。更糟的是旧的那份
    # 成功视觉结果会被这份空文件覆盖掉。
    blanks = blank_panel_ids(panels_dir) | ultra_thin_panel_ids(panels_dir)
    expected = sum(1 for path_ in list_panel_images(panels_dir) if panel_id_for(path_) not in blanks)
    produced = sum(1 for panel in output.panels if panel.panel_id not in blanks)
    if expected > 0 and produced == 0:
        first_error = str(failures[0].get("error") or "未知原因") if failures else "未知原因"
        # 失败明细照旧落盘，方便排查（但不覆盖 vision.json）。
        if failures:
            (target_dir / "vision_failures.json").write_text(
                json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        raise VisionError(
            f"视觉模型 {model} 抽取失败：{expected} 个面板全部失败（成功 0 个）。"
            f"首个错误：{first_error}。"
            "请检查视觉模型名称是否仍然可用（模型 ID 下线会返回 404 Model not exist，"
            "无权限会返回 403 Access denied），改好后重新运行本阶段。"
        )
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
