"""Stage 2：长图纵拼与切图（规范第 4.2 节 + 铁律二）。

铁律二：禁止把几十张切片拼成超大图直接送视觉模型，必须先经本地 Stage 2 滑窗切图
（``height <= 2400px``）。本模块只做本地计算，不上传任何数据。

三件事：

1. **本地滑窗纵拼**：把 ``01_raw/`` 里的原始切片按文件名顺序纵拼成一张长图。
   相邻切片之间的重叠用**滑窗**逐像素比对确定：只有「像素级一致」且「重叠区域含有
   真实图像结构（非纯色平板）」的候选才会被采纳 —— 纯色留白板容易被误判成重叠而吞掉
   真实内容，所以宁可判 0 重叠也不要错切。
2. **留白切割**：在长图上逐行计算 ``row_std``（行像素标准差）与 ``row_diff``（与上一行
   的平均绝对差），满足 ``row_std < 3.0 且 row_diff < 2.0`` 的行视为留白行，作为切点
   候选；切点选在**尽量靠后**的位置以减少分块数量，并保证每块 ``height <= max_height``。
3. **Canny 超长兜底**：整段没有留白行可切时（整页密集画面、巨大跨页 splash），
   用 Canny 边缘投影找出**笔画最少**的那一行作为切点，避免把画面/气泡拦腰切断；
   连边缘信息都没有时退化为按 ``max_height`` 硬切。

「跨切片接缝的实心圆」不会被切破：切图始终在**纵拼后的长图**上进行，接缝不会成为切点；
留白切点也只会落在真正无笔画的行上。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from core.manifest import STAGE_DIR_NAMES
from core.schemas import PanelMeta

#: 铁律二：单块面板高度上限（送视觉模型前必须切到该高度以内）。
MAX_PANEL_HEIGHT = 2400

#: 留白行判定阈值（规范第 4.2 节）：``row_std < 3.0 且 row_diff < 2.0``。
ROW_STD_THRESHOLD = 3.0
ROW_DIFF_THRESHOLD = 2.0

#: 滑窗搜索重叠的最大行数。
MAX_OVERLAP = 64

#: 像素级一致性判定：单通道灰度差超过该值即视为不一致。
OVERLAP_PIXEL_TOLERANCE = 8

#: 重叠区域必须含有真实结构（行标准差下限），纯色平板不作为重叠依据。
MIN_OVERLAP_STRUCT_STD = 1.0

#: 面板最小高度（避免切出极窄的碎块）。
MIN_PANEL_HEIGHT = 200
BLANK_PANEL_INK_RATIO = 0.005   # 空白面板判据：中间调像素占比低于 0.5%
BLANK_PANEL_EDGE_RATIO = 0.001  # 空白面板判据：Canny 边缘密度低于 0.1%

#: Patch v4.0 Patch 18：暗黑高反差剧情格保护阈值。
#:
#: 深黑底 + 血红警示光 / 火光剪影的高潮格里常有**一条高亮的白色对白框**，
#: 整幅画面却几乎全是黑像素：全局中间调占比与全局边缘密度都低得能被
#: 判成「纯黑空白页」，于是这格高潮戏被整格切掉、故事断在半句。
#:
#: * ``DARK_SCENE_ROW_STD_MIN``：逐行像素标准差的峰值下限。黑底上的亮框
#:   会让穿框的那几行标准差暴涨（接近 127），纯黑页则接近 0。
#: * ``DARK_SCENE_LOCAL_EDGE_MIN``：局部（逐行）Canny 边缘密度下限。
#:   亮框的上下边线会在少数几行产生密集边缘；右侧补白那种整列竖边
#:   的密度只有 1/宽度 量级，达不到这个阈值。
DARK_SCENE_ROW_STD_MIN = 18.0
DARK_SCENE_LOCAL_EDGE_MIN = 0.02

#: Canny 边缘检测阈值。
CANNY_LOW = 100
CANNY_HIGH = 200

#: 高度 / 宽度 超过该倍数即认为是「高瘦面板」。
TALL_PANEL_RATIO = 2.0

# --------------------------------------------------------------------------
# Patch v2.6 Patch 4：全话无缝纵拼 + 语义感知二次裁剪
# --------------------------------------------------------------------------
#: 白带最小长度 = max(MIN_GAP_PX_FLOOR, 画幅宽度 * MIN_GAP_RATIO)。
#: Patch v3.0 Patch 6：阈值按规范收紧到 max(20, width * 0.025)。
MIN_GAP_RATIO = 0.025
MIN_GAP_PX_FLOOR = 20
#: 切块后每侧保留的纯色安全边距（像素）。Patch 6：每侧仅保留 10px。
PANEL_WHITE_MARGIN_PX = 10
#: 孤立独白框的高度上限（相对宽度）：height < 0.35 * img_width。
#: Patch 6 收紧：0.55 → 0.35，避免把正常场景图误判成独白框。
TEXT_BOX_MAX_HEIGHT_RATIO = 0.35
#: 悬浮小气泡的高度上限（相对宽度）：height < 0.35 * img_width。
SMALL_BUBBLE_MAX_HEIGHT_RATIO = 0.35
#: 单色背景像素占比阈值（> 78% 视为纯色独白框 / 纯白气泡外框）。
TEXT_BOX_MONO_RATIO = 0.78
#: 独白框向下吸附的最大白边间距（相对宽度）：< 0.25 * img_width。
TEXT_BOX_MERGE_GAP_RATIO = 0.25
#: 合并后总高度上限（相对宽度）：合并后 <= 1.65 * img_width。
#: Patch 6 铁律：超过该上限严禁合并，保证成片「一幕一画面」，绝不双景粘连。
TEXT_BOX_MERGE_MAX_HEIGHT_RATIO = 1.65
#: 判定「封闭漫画外框线」时，横向/纵向笔画需要覆盖整边的比例。
CLOSED_FRAME_COVERAGE = 0.9
#: 「纯白带」的亮度下限：行均值低于该值，即便完全平坦也不算白带。
#: 必须区分白与黑 —— 否则纯黑封面页、黑底内心独白框内部的大片纯黑会被误当成镜头分界，
#: 独白框会被拦腰截断（Patch 4 第 2 步要处理的对象正是这种黑底独白框）。
WHITE_ROW_MEAN_MIN = 200.0
# --------------------------------------------------------------------------
# Patch v2.6 补充：单色均匀带检测 + 极端长图安全阀
# --------------------------------------------------------------------------
#: 「单色均匀带」的横向标准差上限。规范第 4.2 节用 3.0，但 JPEG 压缩噪点会让
#: 纯色留白行的行标准差略微超标（实测黑底过渡带 5~8），判定会整段失效。
UNIFORM_ROW_STD_MAX = 8.0
#: 「单色均匀带」的相邻行平均绝对差上限（同样为 JPEG 噪点放宽）。
UNIFORM_ROW_DIFF_MAX = 5.0
#: 单个原子块的高度上限；超过则触发兜底切分（安全阀）。
#: Patch v3.0 Patch 6：4000 → 3200，彻底杜绝 45,871px 级别的巨型粘连块。
MAX_ATOMIC_BLOCK_HEIGHT = 3200
#: 兜底切分的搜索区间：块内 [top+1600, top+3200] 找边缘密度最低的波谷行。
OVERSIZED_CUT_MIN = 1600
OVERSIZED_CUT_MAX = 3200
#: 空镜判定：偏离主背景色（灰度差 > 15）的像素占比低于该值即视为「无内容页」
#: （全白过渡页、纯黑页）。Patch 6 要求这类页面标 is_transition_blank，
#: 而不是混进剧情池再在成片里「白屏空等 3 秒」。
BLANK_PANEL_CONTENT_RATIO = 0.06
#: 偏离主背景色的灰度差阈值。
BLANK_PANEL_COLOR_DELTA = 15
# --------------------------------------------------------------------------
# Patch v3.1 Patch 9：超窄装饰横条过滤
# --------------------------------------------------------------------------
#: 裁去上下边距后的「有效高度」上限（像素）：低于该值才可能是装饰窄条。
THIN_STRIP_MAX_HEIGHT_PX = 150
#: 装饰窄条的 Canny 边缘密度上限（0~1）：纯色窄条几乎没有线稿。
THIN_STRIP_MAX_EDGE_DENSITY = 0.015
#: 宽高比下限：超过该值即「极扁窄横条」。
THIN_STRIP_MIN_ASPECT_RATIO = 7.5
#: 近白像素占比下限：超过该值说明条子里有文字（旁白条 / 对话框），不能当装饰条。
THIN_STRIP_TEXT_BRIGHT_RATIO = 0.02
#: Stage 2 的切图清单文件名（Patch v3.0 Patch 6）：
#: 本次切分的唯一事实来源 —— Stage 3 只按这里列出的 panel_id 遍历，
#: 并继承其中的 is_transition_blank 标记。
PANELS_MANIFEST_FILENAME = "panels.json"
#: 参与面板清理的图片后缀（Patch 6 清缓存用）。
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})


class SegmenterError(RuntimeError):
    """Stage 2 拼接 / 切图失败。"""


@dataclass(frozen=True)
class PanelCut:
    """一块面板在长图中的行范围。"""

    top: int
    bottom: int
    method: str  # "blank" | "canny" | "tail"

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True)
class StitchResult:
    """纵拼结果。"""

    image: np.ndarray
    #: 每个切片在长图中的起始行。
    offsets: tuple[int, ...]
    #: 每个切片与前一个切片的重叠行数（第一个为 0）。
    overlaps: tuple[int, ...]
    #: 每个切片在长图中贡献的行范围 ``[top, bottom)``。
    slice_ranges: tuple[tuple[int, int], ...]

    @property
    def height(self) -> int:
        return int(self.image.shape[0])


# --------------------------------------------------------------------------
# 图像读写（对非 ASCII 路径安全）
# --------------------------------------------------------------------------
def imread_unicode(path: str | Path) -> np.ndarray:
    """读图（彩色 BGR）。"""
    target = Path(path)
    data = np.fromfile(str(target), dtype=np.uint8)
    if data.size == 0:
        raise SegmenterError(f"图片读取失败（空文件）：{target}")
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise SegmenterError(f"图片解码失败：{target}")
    return image


def imwrite_unicode(path: str | Path, image: np.ndarray) -> Path:
    """写图（按后缀选择编码格式）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = target.suffix or ".png"
    ok, buffer = cv2.imencode(suffix, image)
    if not ok:
        raise SegmenterError(f"图片编码失败：{target}")
    buffer.tofile(str(target))
    return target


def to_gray(image: np.ndarray) -> np.ndarray:
    """转灰度。"""
    if image.ndim == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


# --------------------------------------------------------------------------
# 纵拼
# --------------------------------------------------------------------------
def find_overlap(
    prev_gray: np.ndarray,
    cur_gray: np.ndarray,
    *,
    max_overlap: int = MAX_OVERLAP,
    tolerance: int = OVERLAP_PIXEL_TOLERANCE,
) -> int:
    """滑窗搜索相邻两图的重叠行数。

    从 ``prev_gray`` 的底部与 ``cur_gray`` 的顶部各取最多 ``max_overlap`` 行，
    逐个候选重叠行数做像素级比对。**只有完全一致（差异在 ``tolerance`` 内）且重叠
    区域含有真实图像结构**的候选才会被采纳；多个候选同时成立时取最大的一个
    （既然整段都一致，多出来的行是重复内容，丢弃不会损失信息）。
    """
    max_k = int(min(max_overlap, prev_gray.shape[0], cur_gray.shape[0]))
    if max_k <= 0:
        return 0
    # 真实下载的切片宽度可能不一致（如 700 与 800），按共同宽度比较
    width = int(min(prev_gray.shape[1], cur_gray.shape[1]))
    if width <= 0:
        return 0
    prev = prev_gray[-max_k:, :width].astype(np.int16)
    cur = cur_gray[:max_k, :width].astype(np.int16)

    best = 0
    for k in range(1, max_k + 1):
        a = prev[-k:]
        b = cur[:k]
        if bool(np.any(np.abs(a - b) > tolerance)):
            continue
        # 纯色平板（整段留白）不能作为重叠依据，否则会误吞真实内容
        if float(a.std()) <= MIN_OVERLAP_STRUCT_STD:
            continue
        best = k
    return best


def stitch_images(
    images: list[np.ndarray],
    *,
    max_overlap: int = MAX_OVERLAP,
    tolerance: int = OVERLAP_PIXEL_TOLERANCE,
) -> StitchResult:
    """把若干切片纵拼成一张长图。"""
    if not images:
        raise SegmenterError("没有可拼接的切片")

    width = max(int(image.shape[1]) for image in images)
    padded: list[np.ndarray] = []
    for image in images:
        if image.shape[1] < width:
            pad = width - int(image.shape[1])
            image = cv2.copyMakeBorder(
                image, 0, 0, 0, pad, cv2.BORDER_CONSTANT, value=(255, 255, 255)
            )
        padded.append(image)

    stitched = padded[0].copy()
    offsets: list[int] = [0]
    overlaps: list[int] = [0]
    ranges: list[tuple[int, int]] = [(0, int(stitched.shape[0]))]

    for index in range(1, len(padded)):
        overlap = find_overlap(
            to_gray(padded[index - 1]),
            to_gray(padded[index]),
            max_overlap=max_overlap,
            tolerance=tolerance,
        )
        top = int(stitched.shape[0]) - overlap
        stitched = np.vstack([stitched, padded[index][overlap:]])
        offsets.append(top)
        overlaps.append(overlap)
        ranges.append((top, int(stitched.shape[0])))

    return StitchResult(
        image=stitched,
        offsets=tuple(offsets),
        overlaps=tuple(overlaps),
        slice_ranges=tuple(ranges),
    )


def list_slice_paths(raw_dir: str | Path) -> list[Path]:
    """列出原始切片（按文件名排序，保证切片顺序稳定）。"""
    folder = Path(raw_dir)
    if not folder.is_dir():
        raise SegmenterError(f"原始切片目录不存在：{folder}")
    paths = sorted(
        (path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES),
        key=lambda path: path.name,
    )
    if not paths:
        raise SegmenterError(f"原始切片目录没有图片：{folder}")
    return paths


_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def stitch_slices(paths: list[str | Path], **kwargs) -> StitchResult:
    """读取切片文件并纵拼。"""
    images = [imread_unicode(path) for path in paths]
    return stitch_images(images, **kwargs)


# --------------------------------------------------------------------------
# 行统计与切点
# --------------------------------------------------------------------------
def row_statistics(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """逐行标准差与相邻行平均绝对差。"""
    rows = gray.astype(np.float32)
    row_std = rows.std(axis=1)
    row_diff = np.zeros(rows.shape[0], dtype=np.float32)
    if rows.shape[0] > 1:
        row_diff[1:] = np.abs(np.diff(rows, axis=0)).mean(axis=1)
    return row_std, row_diff


def blank_row_mask(
    gray: np.ndarray,
    *,
    row_std_threshold: float = ROW_STD_THRESHOLD,
    row_diff_threshold: float = ROW_DIFF_THRESHOLD,
) -> np.ndarray:
    """留白行掩码：``row_std < 3.0 且 row_diff < 2.0``。"""
    row_std, row_diff = row_statistics(gray)
    return (row_std < row_std_threshold) & (row_diff < row_diff_threshold)


def canny_edge_density(gray: np.ndarray) -> np.ndarray:
    """每行的 Canny 边缘密度（0~1），用于超长兜底时挑选笔画最少的行。"""
    edges = cv2.Canny(gray, CANNY_LOW, CANNY_HIGH)
    return edges.astype(np.float32).mean(axis=1) / 255.0


def _canny_cut(
    edge_density: np.ndarray | None,
    start: int,
    limit: int,
    min_height: int,
) -> int:
    """在 ``[start, limit]`` 内挑边缘密度最低的一行作为切点。"""
    if edge_density is None:
        return limit
    low = min(start + max(1, min_height // 2), limit)
    window = edge_density[low : limit + 1]
    if window.size == 0:
        return limit
    cut = low + int(np.argmin(window))
    if cut <= start:
        return limit
    return cut


def segment_rows(
    height: int,
    blank_mask: np.ndarray,
    edge_density: np.ndarray | None = None,
    *,
    max_height: int = MAX_PANEL_HEIGHT,
    min_height: int = MIN_PANEL_HEIGHT,
) -> list[PanelCut]:
    """按留白行把长图切成若干块，每块高度不超过 ``max_height``。

    贪心策略：从上界往下找**最靠后**的留白行切点，尽量减少分块数量；整段没有留白行
    可切时走 Canny 兜底；连边缘信息都没有则按 ``max_height`` 硬切（``method="forced"``）。
    """
    if height <= 0:
        return []
    cuts: list[PanelCut] = []
    start = 0
    while height - start > max_height:
        limit = start + max_height
        candidates = np.nonzero(blank_mask[start + min_height : limit + 1])[0]
        if candidates.size:
            cut = start + min_height + int(candidates[-1])
            method = "blank"
        else:
            cut = _canny_cut(edge_density, start, limit, min_height)
            method = "canny" if cut < limit else "forced"
        if cut <= start:
            cut = limit
            method = "forced"
        cuts.append(PanelCut(start, cut, method))
        start = cut
    cuts.append(PanelCut(start, height, "tail"))
    return cuts


def segment_image(
    image: np.ndarray,
    *,
    max_height: int = MAX_PANEL_HEIGHT,
    min_height: int = MIN_PANEL_HEIGHT,
) -> list[PanelCut]:
    """对长图做切分。"""
    gray = to_gray(image)
    mask = blank_row_mask(gray)
    density = canny_edge_density(gray)
    return segment_rows(
        int(image.shape[0]),
        mask,
        density,
        max_height=max_height,
        min_height=min_height,
    )


# --------------------------------------------------------------------------
# 单话切图
# --------------------------------------------------------------------------
def group_slices(
    heights: list[int],
    *,
    max_height: int = MAX_PANEL_HEIGHT,
    min_height: int = MIN_PANEL_HEIGHT,
    max_slices_per_group: int = 1,
) -> list[list[int]]:
    """把原始切片分组（0 起下标），每组纵拼后不超过 ``max_height``。

    真实漫画的一张切片本身就是**一屏画面**，切点应当落在切片边界上；
    若在页内按像素找切点，很容易把对白气泡拦腰切开。

    ``max_slices_per_group`` 默认 1：**一片一面板**。WEBTOON 自己的分页经常把
    一个画格切成两页，合并两页会在接缝处看到被腰斩的画格，视觉模型会误读；
    保持一屏一块与读者看到的一致。放宽到 2 时才合并（总高度仍受 2400px 上限约束）。
    单片自身超过 ``max_height`` 时单独成组，后续再走像素级滑窗兜底。
    """
    groups: list[list[int]] = []
    current: list[int] = []
    current_height = 0
    for index, height in enumerate(heights):
        if height > max_height:
            if current:
                groups.append(current)
                current, current_height = [], 0
            groups.append([index])
            continue
        too_tall = current_height + height > max_height
        too_many = len(current) >= max_slices_per_group
        if current and (too_tall or too_many):
            groups.append(current)
            current, current_height = [index], height
        else:
            current.append(index)
            current_height += height
    if current:
        groups.append(current)
    return groups


def _pad_to_width(image: np.ndarray, width: int) -> np.ndarray:
    """右侧补白到统一宽度（不裁剪内容）。"""
    if int(image.shape[1]) >= width:
        return image
    pad = width - int(image.shape[1])
    return cv2.copyMakeBorder(
        image, 0, 0, 0, pad, cv2.BORDER_CONSTANT, value=(255, 255, 255)
    )


def _has_local_structure(gray: np.ndarray) -> bool:
    """画面整体偏暗，但局部是否存在清晰结构（亮框 / 人脸轮廓 / 字幕边缘）。

    Patch v4.0 Patch 18：暗黑高潮格的全局指标（中间调占比、全局边缘密度）
    都很低，单看全局会把「深黑底 + 一条高亮白色对白框」判成纯黑空白页。
    这里改看**局部行高对比峰值**：穿框的那几行像素标准差会暴涨，
    同时该处必须真的有局部边缘（纯补白竖边达不到边缘密度阈值）。
    """
    if gray.size == 0 or gray.ndim != 2:
        return False
    row_std = gray.astype(np.float32).std(axis=1)
    if float(row_std.max()) < DARK_SCENE_ROW_STD_MIN:
        return False
    edge_rows = canny_edge_density(gray)
    return float(edge_rows.max()) >= DARK_SCENE_LOCAL_EDGE_MIN


def is_blank_panel(gray: np.ndarray) -> bool:
    """整块近乎无内容（纯白 / 纯黑）时判定为空白面板。

    判据取两项同时成立：中间调像素占比极低 **且** Canny 边缘密度极低。
    只看像素取值不行——白底黑线的漫画页几乎没有中间调像素，会被误判成空白；
    只看方差也不行——右侧补白边会把纯黑封面页的方差放大。

    Patch v4.0 Patch 18：全局指标之外再加一道**局部结构保护**——
    深黑底 + 高亮白色对白框的高潮剧情格同样没有中间调像素，
    但它有明显的局部行高对比与局部边缘，这类画面一律判为**有效画面**，
    绝不能当成纯黑空白页切掉。
    """
    mid = float(((gray > 40) & (gray < 215)).mean())
    edges = float(canny_edge_density(gray).mean())
    if mid >= BLANK_PANEL_INK_RATIO or edges >= BLANK_PANEL_EDGE_RATIO:
        return False
    return not _has_local_structure(gray)


def _detect_overlaps(images: list[np.ndarray]) -> tuple[int, ...]:
    """逐对检测切片重叠行数（第 i 片与第 i-1 片重叠的行数）。"""
    overlaps = [0]
    for index in range(1, len(images)):
        overlaps.append(
            find_overlap(to_gray(images[index - 1]), to_gray(images[index]))
        )
    return tuple(overlaps)


def build_episode_panels(
    images: list[np.ndarray],
    overlaps: tuple[int, ...],
    *,
    max_height: int = MAX_PANEL_HEIGHT,
    min_height: int = MIN_PANEL_HEIGHT,
    merge_slices: int = 1,
) -> list[tuple[list[int], np.ndarray]]:
    """把切片分组拼成面板，返回 ``(切片下标列表, 面板图像)``。"""
    heights = [int(image.shape[0]) for image in images]
    groups = group_slices(
        heights,
        max_height=max_height,
        min_height=min_height,
        max_slices_per_group=max(1, merge_slices),
    )
    panels: list[tuple[list[int], np.ndarray]] = []
    for group in groups:
        parts: list[np.ndarray] = []
        for index in group:
            image = images[index]
            overlap = overlaps[index] if index > 0 else 0
            if overlap > 0:
                image = image[overlap:]
            parts.append(image)
        if len(parts) == 1 and parts[0].shape[0] > max_height:
            # 超高单片：退回像素级滑窗
            for cut in segment_image(
                parts[0], max_height=max_height, min_height=min_height
            ):
                sub = parts[0][cut.top : cut.bottom]
                if sub.size:
                    panels.append((group, sub))
            continue
        width = max(int(part.shape[1]) for part in parts)
        parts = [_pad_to_width(part, width) for part in parts]
        panels.append((group, np.vstack(parts)))
    return panels


def segment_episode(
    raw_dir: str | Path,
    panels_dir: str | Path,
    *,
    episode_id: str = "",
    max_height: int = MAX_PANEL_HEIGHT,
    min_height: int = MIN_PANEL_HEIGHT,
    merge_slices: int = 1,
    blacklist_hashes: tuple[int, ...] | None = None,
) -> list[PanelMeta]:
    """把一话的原始切片切成分镜面板，落盘到 ``02_panels/``。

    **Patch v2.6 Patch 4 流程（先拼后切，顺序不可颠倒）**：

    0. **全话无缝纵拼**：把本话 ``01_raw/`` 的所有下载图统一宽度后拼成一张连续长卷，
       彻底消除「平台按固定高度机械切割」造成的「一个镜头跨两张下载图」断镜；
       片尾版权图（Patch v2.5-1 黑名单）在拼接前先剔除。
    1. **单色均匀带精细切分**：沿 ``min_gap_px = max(18, int(width * 0.022))`` 的
       **单色均匀带**切出原子块（白留白、黑底过渡带、灰底过渡带都算，判据
       ``row_std < 8.0 且 row_diff < 5.0``），再裁掉多余上下边距（每侧留 16px）；
       单块仍超过 ``MAX_ATOMIC_BLOCK_HEIGHT``(4000) 时由安全阀兜底切分，
       **绝不会出现上万像素的巨块**。
    2. **孤立独白框 / 悬浮小气泡向下吸附合并**：黑底内心独白框不单独成图，
       与紧邻下方主画格合并成同一张 Panel（带丰富透视线条的场景图不会被误合并）。
    3. **取消 Canny 硬切割**：没有断带的连续超长画格保留为 1 个完整 ``PanelMeta``
       （``is_tall_panel=True``），绝不为了满足高度上限把一个镜头劈成两半。

    ``max_height`` 仅用于标记 ``is_tall_panel``；``min_height`` 与 ``merge_slices``
    在 Patch 4 语义下不再参与切分（切分只由色带决定），保留参数只为向后兼容。
    面板文件名与 ``panel_id`` 统一为 ``p_001`` 形式（Stage 3 原样透传，不再改名）。
    返回每个面板的 ``PanelMeta``（规范第 3 节契约）。
    """
    paths = list_slice_paths(raw_dir)
    images = [imread_unicode(path) for path in paths]
    if blacklist_hashes is None:
        blacklist_hashes = load_blacklist_hashes()

    # 第 0 步前置：剔除片尾版权声明 / 平台推广尾图（Patch v2.5-1 第一重拦截）。
    # 必须在拼接**之前**剔除，否则版权图会被拼进长卷再切碎，既浪费 VLM 又拦不住。
    kept: list[tuple[int, np.ndarray]] = []
    for index, image in enumerate(images):
        if is_blacklisted_bumper(image, blacklist_hashes):
            print(
                f"[s2_segmenter] skipped_blacklist: episode={episode_id or '?'} "
                f"slice={index + 1}/{len(images)}",
                file=sys.stderr,
            )
            continue
        kept.append((index, image))
    if not kept:
        raise SegmenterError(f"本话所有切片都被黑名单命中：{raw_dir}")

    # 第 0 步：统一宽度 → 无缝纵拼成本话连续长卷。
    stitched = stitch_uniform_width([image for _, image in kept])
    canvas = stitched.image
    canvas_width = int(canvas.shape[1])

    # 第 1 步：按真实纯白带切出原子块（含 16px 白边裁剪）。
    blocks = cut_atomic_blocks(canvas, min_gap=min_gap_px(canvas_width))
    # 第 2 步：孤立独白框 / 悬浮小气泡向下吸附合并。
    blocks = merge_standalone_text_boxes(blocks, canvas, img_width=canvas_width)

    def slice_numbers(top: int, bottom: int) -> tuple[int, int]:
        """面板覆盖的原始切片区间（1 起）。"""
        numbers = [
            kept[slot][0] + 1
            for slot, (slice_top, slice_bottom) in enumerate(stitched.slice_ranges)
            if slice_top < bottom and top < slice_bottom
        ]
        if not numbers:
            return (1, 1)
        return (min(numbers), max(numbers))

    target_dir = Path(panels_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    # Patch v3.0 Patch 6 第 1 条：**原子清缓存**。
    # 旧实现把新面板直接写进 02_panels/，上一轮的 p_001.. 与上一版命名留下的
    # panel_001.. 会同时留在目录里 —— Stage 3 遍历目录时把两套都识图，
    # 同一话被播两遍（真实成片因此从 2 分多钟膨胀到 4 分 58 秒 / 223 条 beat）。
    # 现在：先写进 .new_<pid>/ 暂存目录 → 把旧产物挪进 .old_<pid>/ →
    # 新产物 move 进来 → 删除暂存与回收目录，任何一步失败都不会出现新旧混存。
    staging_dir = target_dir / f".new_{os.getpid()}"
    trash_dir = target_dir / f".old_{os.getpid()}"
    for temp_dir in (staging_dir, trash_dir):
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)
    trash_dir.mkdir(parents=True, exist_ok=True)

    metas: list[PanelMeta] = []
    _contentless_ids: set[str] = set()
    index = 0
    for top, bottom in blocks:
        panel = canvas[top:bottom]
        if panel.size == 0:
            continue
        gray = to_gray(panel)
        # 判定空白时排除我们自己保留的上下边距（PANEL_WHITE_MARGIN_PX）：
        # 否则纯黑封面页底部那条黑/白交界会被 Canny 当成「线稿」，
        # 让整块纯黑页躲过空白判定、被当成一个真面板送去识图。
        inner_bottom = int(gray.shape[0]) - PANEL_WHITE_MARGIN_PX
        inner = (
            gray[PANEL_WHITE_MARGIN_PX:inner_bottom]
            if inner_bottom > PANEL_WHITE_MARGIN_PX
            else gray
        )
        # Patch 6 第 4 条：纯白过渡页 / 纯黑页这类「无内容画面」不再直接丢弃，
        # 而是照常落盘并标记 is_transition_blank —— Stage 3 会跳过它们（不烧 VLM），
        # Stage 4 直接过滤，成片里就不会出现「白屏空等 3 秒」。
        # is_contentless_panel 也必须吃 inner：短黑页上下保留的 10px 安全边距
        # （PANEL_WHITE_MARGIN_PX）会改变主背景色统计，把本该判空的短黑页救回来。
        contentless = is_blank_panel(inner) or is_contentless_panel(inner)
        # Patch v3.1 Patch 9：超窄装饰横条（画面缝隙 / 纯色窄条）同样不算内容，
        # 标记 is_transition_blank，避免它单独占一幕成片。
        if is_thin_decoration_strip(inner, width=int(gray.shape[1])):
            contentless = True
        if is_blacklisted_bumper(panel, blacklist_hashes):
            # 片尾版权声明 / 推广尾图：跳过不写入 panels，日志留痕
            print(
                f"[s2_segmenter] skipped_blacklist: episode={episode_id or '?'} "
                f"rows={top}-{bottom}",
                file=sys.stderr,
            )
            continue
        index += 1
        # 文件名主干与 panel_id 统一为 p_XXX：Stage 3 直接透传文件名主干，
        # Stage 6 也以文件名主干为规范键 —— 两端 ID 命名从此同轨
        # （此前 Stage 2 写 panel_001.png 却把 panel_id 标成 {episode_id}_p001，
        #   Stage 3 又改名成 panel_001，三套 ID 互不对齐）。
        file_name = f"p_{index:03d}.png"
        imwrite_unicode(staging_dir / file_name, panel)
        height, width = int(panel.shape[0]), int(panel.shape[1])
        aspect_ratio = width / height if height else 0.0
        density = canny_edge_density(gray)
        metas.append(
            PanelMeta(
                panel_id=f"p_{index:03d}",
                image_path=str(target_dir / file_name),
                width=width,
                height=height,
                aspect_ratio=aspect_ratio,
                source_slice_range=slice_numbers(top, bottom),
                # 第 3 步：无白断带的超长画格保留为完整面板，并标记为高瘦面板，
                # 交给 Stage 3 缩放识图 / Stage 7 的 pan_down 匀速扫过。
                is_tall_panel=bool(
                    height >= TALL_PANEL_RATIO * width or height > max_height
                ),
                edge_density=float(density.mean()),
            )
        )
        if contentless:
            # 空镜：落盘但标记出来（Patch 6）
            _contentless_ids.add(f"p_{index:03d}")

    # 切换到新产物：旧文件先挪进回收目录，再把暂存目录里的文件 move 进来。
    for old_file in target_dir.iterdir():
        if old_file.is_file() and (
            old_file.suffix.lower() in _IMAGE_SUFFIXES or old_file.name == PANELS_MANIFEST_FILENAME
        ):
            shutil.move(str(old_file), str(trash_dir / old_file.name))
    for new_file in staging_dir.iterdir():
        shutil.move(str(new_file), str(target_dir / new_file.name))
    shutil.rmtree(staging_dir, ignore_errors=True)
    shutil.rmtree(trash_dir, ignore_errors=True)

    # panels.json：本次切图的唯一事实来源（Patch 6）。
    # Stage 3 只按这里列出的 panel_id 遍历，并把 is_transition_blank 标记带下去。
    manifest = {
        "episode_id": episode_id,
        "panel_count": len(metas),
        "panels": [
            {
                **meta.model_dump(),
                "source_slice_range": list(meta.source_slice_range),
                "is_transition_blank": meta.panel_id in _contentless_ids,
            }
            for meta in metas
        ],
    }
    (target_dir / PANELS_MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metas


def panels_dir_for(series_dir: str | Path, episode_no: int) -> Path:
    """某话的面板目录：``episodes/ep_XXX/02_panels/``。"""
    return (
        Path(series_dir)
        / "episodes"
        / f"ep_{episode_no:03d}"
        / STAGE_DIR_NAMES["s2_segmenter"]
    )


# --------------------------------------------------------------------------
# Patch v2.5-1 第一重拦截：片尾 LINE WEBTOON 版权声明 / 推广尾图本地指纹直筛
# --------------------------------------------------------------------------
#: 黑名单种子模板目录（backend/assets/blacklist_templates/）。
BLACKLIST_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "assets" / "blacklist_templates"

#: dHash Hamming 距离阈值：<= 6 视为同一张版权图（允许重编码/缩放带来的微小差异）。
BLACKLIST_HASH_THRESHOLD = 6

#: 启发式判据：BGR 三通道均低于该值视为纯黑像素。
BLACKLIST_DARK_CHANNEL_MAX = 20
#: 纯黑像素占比阈值（> 70%）。
BLACKLIST_DARK_RATIO = 0.70
#: WEBTOON 标志绿（#00DC64 邻近色）的 OpenCV HSV 区间。
#: 标准 HSV 口径 H∈[65,85]，OpenCV 的 H 折半（0~179）→ [33,43]。
BLACKLIST_GREEN_H_LOW = 33
BLACKLIST_GREEN_H_HIGH = 43
BLACKLIST_GREEN_S_MIN = 120
BLACKLIST_GREEN_V_MIN = 120
#: 中心区域绿色连通域最小像素数（挡散点噪声，要求是「连续区块」）。
BLACKLIST_GREEN_MIN_AREA = 80

#: 黑名单模板支持的图片后缀。
BLACKLIST_TEMPLATE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})


def dhash_64(image: np.ndarray) -> int:
    """计算 64-bit 差异哈希（dHash）：缩放到 9×8 灰度后逐行比较相邻像素。"""
    gray = to_gray(image)
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | int(small[row, col] > small[row, col + 1])
    return bits


def hamming_distance_64(left: int, right: int) -> int:
    """两个 64-bit 哈希的 Hamming 距离（不同位的数量）。"""
    return (left ^ right).bit_count()


def load_blacklist_hashes(
    template_dir: str | Path | None = None,
) -> tuple[int, ...]:
    """读取黑名单模板目录下所有图片的 64-bit dHash。

    目录缺失、为空或单张图解码失败都不抛异常（返回空元组），
    保证缺少种子资产时切图主流程仍可运行，只是退化为纯启发式判定。
    """
    folder = BLACKLIST_TEMPLATE_DIR if template_dir is None else Path(template_dir)
    if not folder.is_dir():
        return ()
    hashes: list[int] = []
    for path in sorted(folder.iterdir()):
        if not (path.is_file() and path.suffix.lower() in BLACKLIST_TEMPLATE_SUFFIXES):
            continue
        try:
            hashes.append(dhash_64(imread_unicode(path)))
        except SegmenterError:
            continue
    return tuple(hashes)


def _has_webtoon_green(img_bgr: np.ndarray) -> bool:
    """中心 50% 区域内是否存在足够大的 WEBTOON 标志绿连续区块。"""
    height, width = img_bgr.shape[:2]
    top, bottom = height // 4, height * 3 // 4
    left, right = width // 4, width * 3 // 4
    center = img_bgr[top:bottom, left:right]
    if center.size == 0:
        return False
    hsv = cv2.cvtColor(center, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(
        hsv,
        (BLACKLIST_GREEN_H_LOW, BLACKLIST_GREEN_S_MIN, BLACKLIST_GREEN_V_MIN),
        (BLACKLIST_GREEN_H_HIGH, 255, 255),
    )
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 1:
        return False
    # stats[0] 是背景，取其余连通域的最大面积。
    largest = int(stats[1:, cv2.CC_STAT_AREA].max())
    return largest >= BLACKLIST_GREEN_MIN_AREA


def is_blacklisted_bumper(img_bgr: np.ndarray, template_hashes: tuple[int, ...]) -> bool:
    """判定一块分镜是否为片尾版权声明 / 平台推广尾图。

    两条判据满足任一即命中：

    1. 与黑名单模板的 64-bit dHash Hamming 距离 ``<= BLACKLIST_HASH_THRESHOLD``；
    2. 纯黑像素（BGR 均 < 20）占比 > 70%，且居中区域存在 WEBTOON 标志绿
       （HSV H∈[65,85]、S>120、V>120 的 OpenCV 折半口径）连续区块。

    暗黑正片画面因为没有标志绿连通域，不会被启发式误杀。
    """
    if template_hashes:
        fingerprint = dhash_64(img_bgr)
        if any(
            hamming_distance_64(fingerprint, template) <= BLACKLIST_HASH_THRESHOLD
            for template in template_hashes
        ):
            return True
    dark_mask = (
        (img_bgr[:, :, 0] < BLACKLIST_DARK_CHANNEL_MAX)
        & (img_bgr[:, :, 1] < BLACKLIST_DARK_CHANNEL_MAX)
        & (img_bgr[:, :, 2] < BLACKLIST_DARK_CHANNEL_MAX)
    )
    dark_ratio = float(dark_mask.mean())
    if dark_ratio > BLACKLIST_DARK_RATIO and _has_webtoon_green(img_bgr):
        return True
    return False


# --------------------------------------------------------------------------
# Patch v2.6 Patch 4：全话无缝纵拼 + 语义感知二次裁剪
#
# 背景：WEBTOON 平台按固定高度把长条漫画机械切成若干下载图，
# 于是出现三类问题 —— ① 一个镜头被劈成两张下载图（断镜）；
# ② 一张下载图里塞了两个独立镜头（该拆没拆）；③ 黑底内心独白框被切碎成废图。
# 正确路线是：**先把整话拼成一张连续长卷（消灭下载切缝），再在长卷上做语义感知的二次裁剪**。
# --------------------------------------------------------------------------
def min_gap_px(image_width: int) -> int:
    """Patch 4 第 1 步：白带最小长度阈值 ``max(36, int(width * 0.045))``。

    下限 36px 保证「噪声级白行」不会被当成镜头分界；比例项保证窄画幅下
    白带仍然有足够的相对宽度才算真正的镜头间隔。
    """
    return max(MIN_GAP_PX_FLOOR, int(image_width * MIN_GAP_RATIO))


def dominant_width(images: list[np.ndarray]) -> int:
    """取众数宽度（并列时取较大值，避免把小图放大）。"""
    counts: dict[int, int] = {}
    for image in images:
        width = int(image.shape[1])
        counts[width] = counts.get(width, 0) + 1
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def resize_to_width(image: np.ndarray, width: int) -> np.ndarray:
    """等比缩放到指定宽度（宽度相同则原样返回，不做任何重采样）。"""
    source_width = int(image.shape[1])
    if source_width == width:
        return image
    source_height = int(image.shape[0])
    target_height = max(1, int(round(source_height * width / source_width)))
    return cv2.resize(image, (width, target_height), interpolation=cv2.INTER_AREA)


def stitch_uniform_width(
    images: list[np.ndarray], **kwargs
) -> StitchResult:
    """Patch 4 第 0 步：统一宽度后把本话所有切片无缝纵拼成一张连续长卷。

    平台切片的宽度基本一致（偶有 690 / 700 / 800 混用），这里按众数宽度等比缩放，
    而不是右侧补白 —— 补白会在长卷里制造假的竖直白条，干扰白带判定。
    """
    if not images:
        raise SegmenterError("没有可拼接的切片")
    width = dominant_width(images)
    resized = [resize_to_width(image, width) for image in images]
    return stitch_images(resized, **kwargs)


def uniform_row_mask(gray: np.ndarray) -> np.ndarray:
    """行是否属于「单色均匀带」：横向几乎同色，且与相邻行几乎没有变化。

    判据 ``row_std < UNIFORM_ROW_STD_MAX and row_diff < UNIFORM_ROW_DIFF_MAX``。
    相比规范第 4.2 节的 ``3.0 / 2.0`` 放宽了噪点容差（JPEG 压缩噪点会让纯色
    留白行的行标准差略微超标），同时**不再限定颜色必须是白色** —— 黑底画风的
    漫画用纯黑过渡带分隔画格，只认纯白会把几十个画格粘成一个上万像素的巨块。
    """
    row_std, row_diff = row_statistics(gray)
    return (row_std < UNIFORM_ROW_STD_MAX) & (row_diff < UNIFORM_ROW_DIFF_MAX)


def find_content_spans(gray: np.ndarray) -> list[tuple[int, int]]:
    """找出画面内容所在的行段（极大连续「非均匀行」区间）。

    「均匀行」= 整行几乎同色（纯白留白、纯黑过渡带、纯灰过渡带都算）；
    「非均匀行」= 行内有明暗变化，也就是真正画着东西的那些行。
    极大连续非均匀行区间就是候选的画面内容段。
    """
    content = ~uniform_row_mask(gray)
    if content.size == 0 or not bool(content.any()):
        return []
    padded = np.concatenate(([False], content, [False])).astype(np.int8)
    changes = np.diff(padded)
    starts = np.nonzero(changes == 1)[0]
    ends = np.nonzero(changes == -1)[0]
    return [(int(start), int(end)) for start, end in zip(starts, ends)]


def find_separator_gaps(gray: np.ndarray, *, min_gap: int) -> list[tuple[int, int]]:
    """找出**内容之间**的单色分隔带（两侧都存在画面内容）。

    与 :func:`find_uniform_gaps` 的区别：后者把所有单色带都算上，
    于是「整幅纯色页」会被当成一整条分隔带切碎；本函数只认**夹在两段
    内容之间**的带子——首尾的纯色边距不算分隔带（那是边距，由裁边处理）。
    """
    spans = find_content_spans(gray)
    if len(spans) < 2:
        return []
    gaps: list[tuple[int, int]] = []
    for index in range(len(spans) - 1):
        start = spans[index][1]
        end = spans[index + 1][0]
        if end - start >= min_gap:
            gaps.append((start, end))
    return gaps


def find_uniform_gaps(gray: np.ndarray, *, min_gap: int) -> list[tuple[int, int]]:
    """找出所有长度 ``>= min_gap`` 的**单色均匀带**（白 / 黑 / 灰都算）。

    均匀带 = 一整行几乎同色（行标准差低）且与上下行颜色一致（行间差低），
    因此纯白留白、纯黑过渡带、灰底过渡带都能被认出来。

    主流程用的是 :func:`find_separator_gaps`（只认内容之间的带子），
    本函数保留给需要「全部单色带」语义的调用方与测试。
    """
    mask = uniform_row_mask(gray)
    if mask.size == 0 or not bool(mask.any()):
        return []
    padded = np.concatenate(([False], mask, [False])).astype(np.int8)
    changes = np.diff(padded)
    starts = np.nonzero(changes == 1)[0]
    ends = np.nonzero(changes == -1)[0]
    return [
        (int(start), int(end))
        for start, end in zip(starts, ends)
        if int(end) - int(start) >= min_gap
    ]


def find_blank_gaps(gray: np.ndarray, *, min_gap: int) -> list[tuple[int, int]]:
    """（保留）只找**纯白**留白带：规范第 4.2 节判据 + 白度下限。

    主流程已改用 :func:`find_uniform_gaps`；本函数只留给需要「纯白带」语义的调用方。
    """
    mask = blank_row_mask(gray)
    if mask.size == 0 or not bool(mask.any()):
        return []
    row_mean = gray.astype(np.float32).mean(axis=1)
    mask = mask & (row_mean >= WHITE_ROW_MEAN_MIN)
    if not bool(mask.any()):
        return []
    padded = np.concatenate(([False], mask, [False])).astype(np.int8)
    changes = np.diff(padded)
    starts = np.nonzero(changes == 1)[0]
    ends = np.nonzero(changes == -1)[0]
    return [
        (int(start), int(end))
        for start, end in zip(starts, ends)
        if int(end) - int(start) >= min_gap
    ]


def _flat_row_mask(gray: np.ndarray) -> np.ndarray:
    """行是否「横向单色」（不要求与邻行一致），用于裁剪块首尾的纯色边。"""
    row_std, _ = row_statistics(gray)
    return row_std < UNIFORM_ROW_STD_MAX


def _trim_uniform_edges(
    gray: np.ndarray, *, margin: int = PANEL_WHITE_MARGIN_PX
) -> tuple[int, int]:
    """裁掉块首尾的单色均匀行（白 / 黑 / 灰都算），每侧保留 ``margin`` 像素。

    返回裁剪后的 ``(top, bottom)`` 偏移（相对块顶）。整块都是均匀色时返回
    ``(0, 0)`` —— 这块没有任何画面内容，会被后续的空白面板判定丢弃。
    """
    height = int(gray.shape[0])
    if height == 0:
        return (0, 0)
    mask = _flat_row_mask(gray)
    lead = 0
    while lead < height and bool(mask[lead]):
        lead += 1
    trail = 0
    while trail < height - lead and bool(mask[height - 1 - trail]):
        trail += 1
    if lead >= height:
        return (0, 0)
    top = max(0, lead - margin)
    bottom = height - max(0, trail - margin)
    if bottom <= top:
        return (0, 0)
    return (top, bottom)


def _row_edge_density(gray: np.ndarray) -> np.ndarray:
    """每行的 Canny 边缘密度（0~1），比 :func:`canny_edge_density` 更省内存。"""
    edges = cv2.Canny(gray, CANNY_LOW, CANNY_HIGH)
    width = max(1, int(gray.shape[1]))
    return edges.sum(axis=1, dtype=np.float64) / (255.0 * width)


def _split_oversized(
    gray: np.ndarray,
    top: int,
    bottom: int,
    *,
    max_height: int = MAX_ATOMIC_BLOCK_HEIGHT,
) -> list[tuple[int, int]]:
    """极端长图安全阀：单个原子块仍超过 ``max_height`` 时兜底切分。

    在块内 ``[top+1600, top+3200]`` 区间里找**水平边缘密度最低**的波谷行
    作为切点（那里通常是画格边界或空白），反复执行直到每块都不超过
    ``max_height``，确保不会再出现上万像素的巨块。
    """
    if bottom - top <= max_height:
        return [(top, bottom)]
    pieces: list[tuple[int, int]] = []
    pending = [(top, bottom)]
    while pending:
        start, end = pending.pop()
        if end - start <= max_height:
            pieces.append((start, end))
            continue
        density = _row_edge_density(gray[start:end])
        low = OVERSIZED_CUT_MIN
        high = min(OVERSIZED_CUT_MAX, (end - start) - OVERSIZED_CUT_MIN)
        if high <= low:
            middle = (start + end) // 2
            pending.append((middle, end))
            pending.append((start, middle))
            continue
        cut = start + low + int(np.argmin(density[low : high + 1]))
        if cut <= start or cut >= end:
            middle = (start + end) // 2
            pending.append((middle, end))
            pending.append((start, middle))
            continue
        pending.append((cut, end))
        pending.append((start, cut))
    pieces.sort()
    return pieces


def cut_atomic_blocks(
    image: np.ndarray,
    *,
    min_gap: int | None = None,
    margin: int = PANEL_WHITE_MARGIN_PX,
    max_height: int = MAX_ATOMIC_BLOCK_HEIGHT,
) -> list[tuple[int, int]]:
    """在长卷上按**单色均匀带**切出原子块，并裁掉多余边距。

    * 切点取「两块内容之间」的单色带中点（白留白、黑底过渡带、灰底过渡带都算）；
      长卷首尾的纯色边距不算分隔带——那是边距，交给裁边处理；
    * 每块首尾的纯色边距被裁掉（每侧保留 ``margin`` 像素）；
    * 单块仍超过 ``max_height`` 时由 :func:`_split_oversized` 兜底切分。

    退化兜底：整幅长卷若裁不出任何块（例如全幅纯色的合成图），返回整幅一块，
    交由空白面板判定决定去留——宁可留一块交给下游判空，也不要产出零面板。
    """
    gray = to_gray(image)
    height = int(gray.shape[0])
    gap = min_gap_px(int(image.shape[1])) if min_gap is None else int(min_gap)
    cuts = [
        (start + end) // 2 for start, end in find_separator_gaps(gray, min_gap=gap)
    ]
    bounds = [0, *cuts, height]
    blocks: list[tuple[int, int]] = []
    for index in range(len(bounds) - 1):
        top, bottom = bounds[index], bounds[index + 1]
        if bottom <= top:
            continue
        for piece_top, piece_bottom in _split_oversized(
            gray, top, bottom, max_height=max_height
        ):
            local_top, local_bottom = _trim_uniform_edges(
                gray[piece_top:piece_bottom], margin=margin
            )
            if local_bottom - local_top <= 0:
                continue
            blocks.append((piece_top + local_top, piece_top + local_bottom))
    if not blocks:
        # 整幅裁不出内容块：保留整幅（下游的空白面板判定会决定是否丢弃）。
        local_top, local_bottom = _trim_uniform_edges(gray, margin=margin)
        if local_bottom - local_top <= 0:
            return [(0, height)]
        return [(local_top, local_bottom)]
    return blocks


def is_contentless_panel(
    block_bgr: np.ndarray,
    *,
    ratio: float = BLANK_PANEL_CONTENT_RATIO,
    delta: int = BLANK_PANEL_COLOR_DELTA,
) -> bool:
    """判定一块画面是否「没有内容」（Patch v3.0 Patch 6 第 4 条）。

    判据：先取块内的**主背景色**（灰度量化众数），再统计偏离该颜色超过
    ``delta`` 的像素占比；占比低于 ``ratio``（默认 6%）即认为这是全白过渡页、
    纯黑页之类的空镜。

    这类页面过去会被当成正常分镜送进 VLM、再被写成一段「纯黑如墨吸走呼吸」
    的废话旁白，成片里表现为「白屏空等 3 秒」。现在它们会被标记
    ``is_transition_blank``，Stage 4 直接过滤，不再进剧情池。
    """
    if block_bgr.size == 0:
        return True
    gray = to_gray(block_bgr)
    levels = (gray.astype(np.uint16) // 16).ravel()
    counts = np.bincount(levels, minlength=16)
    mode_level = int(np.argmax(counts))
    mode_value = mode_level * 16 + 8
    deviation = np.abs(gray.astype(np.int16) - mode_value) > delta
    content_ratio = float(deviation.mean())
    return content_ratio < ratio


def is_thin_decoration_strip(
    inner_gray: np.ndarray,
    *,
    width: int,
    max_height: int = THIN_STRIP_MAX_HEIGHT_PX,
    max_edge_density: float = THIN_STRIP_MAX_EDGE_DENSITY,
    min_aspect_ratio: float = THIN_STRIP_MIN_ASPECT_RATIO,
    text_bright_ratio: float = THIN_STRIP_TEXT_BRIGHT_RATIO,
) -> bool:
    """判定一块画面是不是「超窄装饰横条」（画面缝隙 / 纯色窄条）。

    Patch v3.1 Patch 9：成片 03:00 处出现过一段 80px 左右的暗色细条单独占一幕，
    它既不是内容也不是过渡页，只是两张画格之间的缝隙被切成了一个面板。
    判据（四条同时满足，缺一不可）：

    1. 裁掉上下 10px 安全边距后的**有效高度** < ``max_height``（默认 150px）；
    2. **够扁窄**：宽高比（``width / 有效高度``）> ``min_aspect_ratio``——
       只是「矮」而不够扁的块，多半是漫画里的小画格，不能当装饰条；
    3. **几乎没有线稿**：Canny 边缘密度 < ``max_edge_density``；
    4. **不含文字**：整块近白像素占比 < ``text_bright_ratio``，且没有任何一行
       的近白像素占比达到 ``text_bright_ratio``——「黑底白字」的旁白条 /
       对话框、白底留白格都要保留，不能误杀。

    参数
    ----
    inner_gray:
        已裁掉上下边距的灰度图（2D）。
    width:
        该块的原始宽度（像素），用于计算宽高比。
    """
    if inner_gray is None or inner_gray.size == 0:
        return False
    effective_height = int(inner_gray.shape[0])
    if effective_height <= 0 or effective_height >= max_height:
        return False
    effective_width = int(inner_gray.shape[1]) if inner_gray.ndim >= 2 else 0
    aspect = float(width or effective_width) / float(effective_height)
    if aspect <= min_aspect_ratio:
        return False
    edge_density = float(canny_edge_density(inner_gray).mean())
    if edge_density >= max_edge_density:
        return False
    bright = inner_gray > 200
    if float(bright.mean()) >= text_bright_ratio:
        return False
    row_bright = bright.mean(axis=1)
    if bool((row_bright >= text_bright_ratio).any()):
        return False
    return True


def dominant_color_ratio(block_bgr: np.ndarray, *, quantize: int = 16) -> float:
    """块内「单一颜色」像素占比（灰度量化后取众数占比）。

    黑底白字的内心独白框、纯白气泡外框都会得到很高的占比；
    带丰富灰阶的场景图（走廊透视线、渐变）则明显偏低。
    """
    if block_bgr.size == 0:
        return 0.0
    gray = to_gray(block_bgr)
    levels = (gray.astype(np.uint16) // quantize).ravel()
    counts = np.bincount(levels, minlength=256 // quantize)
    return float(counts.max()) / float(levels.size)


def has_closed_border_frame(
    block_gray: np.ndarray, *, coverage: float = CLOSED_FRAME_COVERAGE
) -> bool:
    """块内是否存在贯穿四边的封闭漫画外框线。"""
    height, width = block_gray.shape[:2]
    if height < 4 or width < 4:
        return False
    edges = cv2.Canny(block_gray, CANNY_LOW, CANNY_HIGH)
    row_fill = edges.mean(axis=1) / 255.0
    column_fill = edges.mean(axis=0) / 255.0
    has_horizontal = bool((row_fill >= coverage).any())
    has_vertical = bool((column_fill >= coverage).any())
    return has_horizontal and has_vertical


def is_standalone_text_box(
    block_bgr: np.ndarray, orig_gap_below: int, img_width: int
) -> bool:
    """Patch 4 第 2 步：判定一块原子块是否为「孤立的内心独白框 / 悬浮小气泡」。

    特征 A（黑框 / 纯色独白框）：整体偏扁（``height < 0.55 * img_width``），
    且单色背景像素占比 ``> 75%``（大面积纯黑矩形内嵌少量文字，或纯白气泡外框），
    缺少四周漫画线稿与场景渐变。

    特征 B（小尺寸悬浮气泡）：``height < 0.35 * img_width`` 且没有形成封闭漫画外框线。

    ``orig_gap_below`` 是该块与下方主画格之间的白边间距（像素），
    合并决策在 :func:`merge_standalone_text_boxes` 中完成。
    """
    if block_bgr.size == 0 or img_width <= 0:
        return False
    height = int(block_bgr.shape[0])
    if height >= img_width * TEXT_BOX_MAX_HEIGHT_RATIO:
        # 太高就不是「偏扁的独白框」了，走主画格逻辑。
        return False
    if dominant_color_ratio(block_bgr) > TEXT_BOX_MONO_RATIO:
        return True  # 特征 A
    if height < img_width * SMALL_BUBBLE_MAX_HEIGHT_RATIO and not has_closed_border_frame(
        to_gray(block_bgr)
    ):
        return True  # 特征 B
    return False


def merge_standalone_text_boxes(
    blocks: list[tuple[int, int]],
    image: np.ndarray,
    *,
    img_width: int,
    gap_ratio: float = TEXT_BOX_MERGE_GAP_RATIO,
) -> list[tuple[int, int]]:
    """Patch 4 第 2 步：把孤立独白框 / 悬浮小气泡向下吸附并入主画格。

    合并条件：块被判定为文本框，且它与**紧邻下方非文本主画格**之间的白边间距
    ``< 0.25 * img_width``（Patch 6 收紧，原 0.50）。合并后两者连同中间留白
    合成同一张 Panel。

    **Patch 6 防双景粘连铁律**：合并后总高度必须 ``<= 1.65 * img_width``，
    否则严禁合并——宁可让独白框单独成块，也不能把两个独立镜头粘成「一幕两画面」。

    防误合并：像「医院走廊」这种带丰富建筑透视线条与灰阶色彩的场景图，
    单色背景占比远低于 78%，不会被判成文本框，因此仍然独立成块。
    """
    if not blocks:
        return []
    gray = to_gray(image)
    flags = [
        is_standalone_text_box(
            image[top:bottom],
            blocks[index + 1][0] - bottom if index + 1 < len(blocks) else 0,
            img_width,
        )
        for index, (top, bottom) in enumerate(blocks)
    ]
    limit = img_width * gap_ratio
    max_merged_height = img_width * TEXT_BOX_MERGE_MAX_HEIGHT_RATIO
    absorbed: dict[int, int] = {}
    for index in range(len(blocks)):
        if not flags[index]:
            continue
        total_gap = 0
        for target in range(index + 1, len(blocks)):
            total_gap += blocks[target][0] - blocks[target - 1][1]
            if not flags[target]:
                merged_height = blocks[target][1] - blocks[index][0]
                if total_gap < limit and merged_height <= max_merged_height:
                    absorbed[index] = target
                break
            if total_gap >= limit:
                break
    merged: list[tuple[int, int]] = []
    index = 0
    while index < len(blocks):
        target = absorbed.get(index)
        if target is None:
            merged.append(blocks[index])
            index += 1
            continue
        # 文本框并入下方主画格：整体保留（含中间留白）
        merged.append((blocks[index][0], blocks[target][1]))
        index = target + 1
    # 合并后可能跨越色带，再统一裁一次边距。
    # 整块都是单色（裁完为空）时**保留原块**：是不是空白由下游 is_blank_panel
    # 判定（它会看中间调像素占比与边缘密度），这里不能把整块丢掉，
    # 否则纯色合成图会导致 Stage 2 产出零面板。
    trimmed: list[tuple[int, int]] = []
    for top, bottom in merged:
        local_top, local_bottom = _trim_uniform_edges(gray[top:bottom])
        if local_bottom - local_top <= 0:
            trimmed.append((top, bottom))
            continue
        trimmed.append((top + local_top, top + local_bottom))
    return trimmed
