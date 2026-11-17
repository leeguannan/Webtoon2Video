"""Stage 2 单元测试：纵拼、留白切割、Canny 兜底，以及「跨切片接缝的实心圆不被切破」。"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from stages.s2_segmenter import (
    MAX_ATOMIC_BLOCK_HEIGHT,
    MAX_PANEL_HEIGHT,
    PANEL_WHITE_MARGIN_PX,
    SegmenterError,
    blank_row_mask,
    canny_edge_density,
    cut_atomic_blocks,
    dhash_64,
    find_overlap,
    group_slices,
    hamming_distance_64,
    imwrite_unicode,
    is_blacklisted_bumper,
    is_contentless_panel,
    is_standalone_text_box,
    is_blank_panel,
    list_slice_paths,
    load_blacklist_hashes,
    panels_dir_for,
    row_statistics,
    segment_episode,
    segment_image,
    segment_rows,
    stitch_images,
)

WIDTH = 320


def make_reference(
    height: int = 1200,
    *,
    center: tuple[int, int] = (WIDTH // 2, 600),
    radius: int = 150,
) -> np.ndarray:
    """白底 + 实心黑圆。"""
    image = np.full((height, WIDTH, 3), 255, dtype=np.uint8)
    cv2.circle(image, center, radius, (0, 0, 0), -1)
    return image


def make_striped(height: int = 1000) -> np.ndarray:
    """对角条纹图：每行都有笔画，不存在留白行。"""
    rows = np.arange(height)[:, None]
    cols = np.arange(WIDTH)[None, :]
    mask = ((rows + cols) % 40) < 20
    return np.where(mask, 255, 0).astype(np.uint8)[:, :, None].repeat(3, axis=2)


def ink_rows(image: np.ndarray) -> tuple[int, int]:
    """图像中含墨迹（暗像素）的首末行。"""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    rows = np.nonzero((gray < 128).any(axis=1))[0]
    assert rows.size, "测试图里应当有墨迹"
    return int(rows[0]), int(rows[-1])


def panel_of(cuts, row: int) -> int:
    for index, cut in enumerate(cuts):
        if cut.top <= row < cut.bottom:
            return index
    raise AssertionError(f"行 {row} 不属于任何面板")


# --------------------------------------------------------------------------
# 行统计与留白判定
# --------------------------------------------------------------------------
def test_row_statistics_and_blank_mask() -> None:
    gray = np.full((10, 8), 255, dtype=np.uint8)
    gray[4:6, 2:6] = 0

    row_std, row_diff = row_statistics(gray)
    assert row_std[0] == 0.0
    assert row_std[4] > 3.0
    assert row_diff[4] > 2.0  # 与上一行差异大

    mask = blank_row_mask(gray)
    assert not mask[4]
    assert not mask[5]
    assert mask[0]
    assert mask[9]


def test_blank_mask_uses_spec_thresholds() -> None:
    """阈值取自规范第 4.2 节：``row_std < 3.0 且 row_diff < 2.0``（严格小于）。"""
    gray = np.zeros((4, 4), dtype=np.uint8)
    gray[1] = 1  # std=0 < 3.0，diff=1 < 2.0 → 留白
    gray[2] = 3  # std=0 < 3.0，diff=2 不小于 2.0 → 不是留白
    gray[3] = 4  # std=0 < 3.0，diff=1 < 2.0 → 留白
    mask = blank_row_mask(gray)
    assert mask[0]
    assert mask[1]
    assert not mask[2]
    assert mask[3]


# --------------------------------------------------------------------------
# 纵拼
# --------------------------------------------------------------------------
def test_find_overlap_detects_identical_overlap() -> None:
    reference = cv2.cvtColor(make_reference(200, center=(160, 100), radius=60), cv2.COLOR_BGR2GRAY)
    prev = reference[0:150]
    cur = reference[120:200]
    assert find_overlap(prev, cur) == 30


def test_find_overlap_ignores_uniform_plateau() -> None:
    """纯色平板不得被当成重叠，否则会吞掉真实内容。"""
    blank = np.full((120, 40), 255, dtype=np.uint8)
    assert find_overlap(blank, blank) == 0


def test_find_overlap_returns_zero_without_overlap() -> None:
    prev = cv2.cvtColor(make_reference(200, center=(160, 100), radius=60), cv2.COLOR_BGR2GRAY)
    cur = cv2.cvtColor(make_reference(200, center=(160, 100), radius=60), cv2.COLOR_BGR2GRAY)
    assert find_overlap(prev, cur) == 0


def test_stitch_rebuilds_circle_across_seam() -> None:
    """无缝拼接：圆心正好压在两片切片的接缝上，拼回来必须与原图逐像素一致。"""
    reference = make_reference(1200, center=(WIDTH // 2, 600), radius=150)
    stitched = stitch_images([reference[0:600], reference[600:1200]])

    assert stitched.overlaps == (0, 0)
    assert stitched.height == 1200
    assert np.array_equal(stitched.image, reference)
    # 圆没有被接缝切成两半：墨迹首末行与原图一致
    assert ink_rows(stitched.image) == ink_rows(reference)


def test_stitch_detects_real_overlap() -> None:
    """带重叠的切片：重叠区含实心圆，滑窗必须精确定位重叠行数。"""
    reference = make_reference(1200, center=(WIDTH // 2, 530), radius=50)
    stitched = stitch_images([reference[0:560], reference[500:1200]])

    assert stitched.overlaps == (0, 60)
    assert stitched.height == 1200
    assert np.array_equal(stitched.image, reference)


def test_stitch_reports_slice_ranges() -> None:
    reference = make_reference(1200, center=(WIDTH // 2, 530), radius=50)
    stitched = stitch_images([reference[0:560], reference[500:1200]])
    assert stitched.slice_ranges == ((0, 560), (500, 1200))
    assert stitched.offsets == (0, 500)


# --------------------------------------------------------------------------
# 切割
# --------------------------------------------------------------------------
def test_segment_rows_respects_max_height() -> None:
    height = 5000
    mask = np.ones(height, dtype=bool)
    cuts = segment_rows(height, mask, None, max_height=MAX_PANEL_HEIGHT, min_height=200)

    assert len(cuts) == 3
    assert all(cut.height <= MAX_PANEL_HEIGHT for cut in cuts)
    assert cuts[0].top == 0
    assert cuts[-1].bottom == height


def test_segment_cuts_on_the_latest_blank_row() -> None:
    """黑块上方的留白行应被选作切点，而不是硬切。"""
    image = np.full((1000, WIDTH, 3), 255, dtype=np.uint8)
    for y in range(400, 600):  # 带笔画的黑色内容块，内部行不是留白
        image[y, ::2] = 0

    cuts = segment_image(image, max_height=500, min_height=200)
    assert cuts[0].method == "blank"
    assert cuts[0].bottom == 399  # 黑块上方的最后一行留白
    assert all(cut.height <= 500 for cut in cuts)


def test_segment_does_not_cut_the_circle() -> None:
    """核心验收：圆跨在两片原始切片的接缝上，切图不得把圆拦腰切断。"""
    reference = make_reference(1200, center=(WIDTH // 2, 600), radius=150)
    top, bottom = ink_rows(reference)

    cuts = segment_image(reference, max_height=700, min_height=200)

    # 切点不得落在圆的内部
    for cut in cuts[1:]:
        assert not (top < cut.top < bottom), f"切点 {cut.top} 落在圆内部"
    # 圆的上下沿必须在同一块面板里
    assert panel_of(cuts, top) == panel_of(cuts, bottom)
    # 面板高度受限，且所有像素都在（切片无像素丢失）
    assert all(cut.height <= 700 for cut in cuts)
    rebuilt = np.vstack([reference[cut.top : cut.bottom] for cut in cuts])
    assert np.array_equal(rebuilt, reference)
    # 圆所在的那块面板确实含有整圆
    holder = cuts[panel_of(cuts, top)]
    assert ink_rows(reference[holder.top : holder.bottom]) == (
        top - holder.top,
        bottom - holder.top,
    )


def test_segment_never_cuts_inside_ink() -> None:
    """任何切点都不得落在有笔画的行上。"""
    reference = make_reference(1200, center=(WIDTH // 2, 600), radius=150)
    cuts = segment_image(reference, max_height=700, min_height=200)

    gray = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    for cut in cuts[1:]:
        row = gray[cut.top]
        assert (row < 128).sum() == 0, f"切点 {cut.top} 落在有笔画的行上"


def test_canny_fallback_when_no_blank_row() -> None:
    """整段无留白行时走 Canny 兜底：切点落在笔画最少的行（灰色渐变带）上。"""
    image = make_striped(1000)
    for offset, y in enumerate(range(300, 341)):
        image[y, :] = 100 + offset * 3  # 竖向渐变：无留白行、边缘稀疏

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    assert not blank_row_mask(gray).any()  # 前提：确实没有留白行

    cuts = segment_image(image, max_height=400, min_height=200)
    assert cuts[0].method == "canny"
    assert 301 <= cuts[0].bottom <= 339  # 落在渐变带内部，而不是硬切在 400
    assert all(cut.height <= 400 for cut in cuts)

    density = canny_edge_density(gray)
    assert density[cuts[0].bottom] == pytest.approx(0.0)
    assert density[400] > 0.0


# --------------------------------------------------------------------------
# 单话切图落盘
# --------------------------------------------------------------------------
def test_segment_episode_writes_panels(tmp_path) -> None:
    reference = make_reference(1200, center=(WIDTH // 2, 600), radius=150)
    raw_dir = tmp_path / "01_raw"
    raw_dir.mkdir()
    imwrite_unicode(raw_dir / "001.png", reference[0:600])
    imwrite_unicode(raw_dir / "002.png", reference[600:1200])

    panels_dir = tmp_path / "02_panels"
    # Patch 4：先全话无缝纵拼再按白带切，跨接缝的镜头自动还原成一整块
    metas = segment_episode(
        raw_dir, panels_dir, episode_id="wt_95_ep_001", max_height=1200, merge_slices=2
    )

    assert metas
    assert len(metas) == 1
    assert all(meta.height <= 1200 for meta in metas)
    # 面板 ID 与文件名同轨（Stage 3 原样透传，不再改名）
    assert metas[0].panel_id == "p_001"
    for meta in metas:
        assert (panels_dir / f"{meta.panel_id}.png").is_file()

    # 圆跨在两片原始切片的接缝上，但成品面板里它是完整的一块
    panel = cv2.imread(str(metas[0].image_path))
    top, bottom = ink_rows(panel)
    assert bottom - top == 2 * 150
    assert metas[0].source_slice_range == (1, 2)


# --------------------------------------------------------------------------
# 切片边界优先（真实漫画一张切片 = 一屏）
# --------------------------------------------------------------------------
def test_group_slices_keeps_one_slice_per_group_by_default() -> None:
    """默认一片一面板：接缝处不会看到被 WEBTOON 自己腰斩的画格。"""
    groups = group_slices([1100] * 52, max_height=MAX_PANEL_HEIGHT)
    assert len(groups) == 52
    assert groups[0] == [0]
    assert groups[-1] == [51]


def test_group_slices_merges_until_limit() -> None:
    """显式允许合并两片时，按 2400px 上限每组 2 片。"""
    groups = group_slices(
        [1100] * 52, max_height=MAX_PANEL_HEIGHT, max_slices_per_group=2
    )
    assert len(groups) == 26
    assert groups[0] == [0, 1]
    assert groups[-1] == [50, 51]


def test_group_slices_isolates_oversized_slice() -> None:
    """单片自身超高时单独成组，后续走像素级滑窗。"""
    groups = group_slices([1100, 3000, 1100], max_height=MAX_PANEL_HEIGHT)
    assert groups == [[0], [1], [2]]


def test_segment_episode_cuts_on_slice_boundaries(tmp_path) -> None:
    """切点落在切片边界（漫画页边界），而不是页内把气泡切开。"""
    raw_dir = tmp_path / "01_raw"
    raw_dir.mkdir()
    for index in range(3):
        imwrite_unicode(
            raw_dir / f"{index + 1:03d}.png",
            make_reference(600, center=(WIDTH // 2, 300), radius=80),
        )

    metas = segment_episode(raw_dir, tmp_path / "02_panels", max_height=700)

    assert len(metas) == 3
    assert [meta.source_slice_range for meta in metas] == [(1, 1), (2, 2), (3, 3)]
    assert all(meta.height <= 700 for meta in metas)


def test_segment_episode_can_merge_slices(tmp_path) -> None:
    """Patch 4：切分由真实白带决定，``merge_slices`` 不再参与。

    旧语义（一片一面板 + ``merge_slices`` 合并下载图）已被「先拼后切」取代：
    四张下载图各自含一个圆，圆之间被纯白带隔开，于是切成 4 个独立面板，
    每块都完整包含自己的圆。
    """
    raw_dir = tmp_path / "01_raw"
    raw_dir.mkdir()
    for index in range(4):
        imwrite_unicode(
            raw_dir / f"{index + 1:03d}.png",
            make_reference(1100, center=(WIDTH // 2, 550), radius=80),
        )

    metas = segment_episode(
        raw_dir, tmp_path / "02_panels", max_height=2400, merge_slices=2
    )

    assert [meta.source_slice_range for meta in metas] == [(1, 1), (2, 2), (3, 3), (4, 4)]
    assert all(meta.height <= 2400 for meta in metas)
    for meta in metas:
        panel = cv2.imread(str(meta.image_path))
        top, bottom = ink_rows(panel)
        assert bottom - top == 2 * 80


def test_blank_panel_is_marked_as_transition_blank(tmp_path) -> None:
    """Patch 6：纯白过渡页（只有极少线条）落盘但标记 is_transition_blank。

    旧行为是直接丢弃，结果 Stage 3/4 拿不到任何线索，成片里反而会留下
    「白屏空等」；现在它进入 panels.json 并被显式标记，下游据此过滤。
    """
    raw_dir = tmp_path / "01_raw"
    raw_dir.mkdir()
    imwrite_unicode(
        raw_dir / "001.png",
        make_reference(600, center=(WIDTH // 2, 300), radius=80),
    )
    # 废格：整页接近纯白，只有一条极淡的竖线（偏离主色像素占比 ≈ 4.4% < 6%），
    # 顶部留 80px 平坦白作为与上一页的分隔带。
    waste = np.full((600, WIDTH, 3), 255, dtype=np.uint8)
    waste[80:, 150:165] = 210
    imwrite_unicode(raw_dir / "002.png", waste)

    panels_dir = tmp_path / "02_panels"
    metas = segment_episode(raw_dir, panels_dir, max_height=600)

    assert len(metas) == 2
    manifest = json.loads((panels_dir / "panels.json").read_text(encoding="utf-8"))
    flags = {item["panel_id"]: item["is_transition_blank"] for item in manifest["panels"]}
    assert flags["p_002"] is True
    assert flags["p_001"] is False


def test_contentless_short_black_panel_ignores_safe_margin() -> None:
    """短黑页判空必须吃 inner（裁掉 10px 安全边距），不能被边距救回来。

    旧表达式 ``is_contentless_panel(panel)`` 统计整图：切图阶段保留的上下
    10px 平坦边距会改变主背景色统计，把本该判空的短黑页救回来、当成正常分镜。
    修复后与 ``is_blank_panel(inner)`` 一样只统计 inner 区。
    端到端切分上游还有 ``_trim_uniform_edges`` 等步骤，这里在调用点的函数
    契约层面锁定回归（s2_segmenter.py 的 contentless 表达式）。
    """
    width, body_height = 320, 60
    body = np.full((body_height, width, 3), 12, dtype=np.uint8)
    # inner 区（10~50 行）放 4% 的深灰实心块（16x32=512 / 12800）：
    # is_blank_panel=False（中间调 4% > 0.5%，实心块带 Canny 边缘），
    # is_contentless_panel(inner)=True（偏离主色占比 4% < 6% 阈值）。
    body[20:36, 120:152] = 100
    # 底部安全边距内残留 4 行亮线（黑/白交界），旧逻辑统计整图时会抬高偏离占比。
    body[-4:, :] = 255
    inner = body[PANEL_WHITE_MARGIN_PX : body_height - PANEL_WHITE_MARGIN_PX]

    assert is_blank_panel(inner) is False
    assert is_contentless_panel(inner) is True
    # 回归锚点：带边距的整图确实逃过判空，证明修复点就在 inner/body 之差。
    assert is_contentless_panel(body) is False


def test_is_blank_panel() -> None:
    assert is_blank_panel(np.full((100, 100), 255, dtype=np.uint8)) is True
    assert is_blank_panel(np.zeros((100, 100), dtype=np.uint8)) is True
    content = np.full((100, 100), 255, dtype=np.uint8)
    content[40:60, 40:60] = 120
    assert is_blank_panel(content) is False


def test_segment_episode_rejects_missing_raw_dir(tmp_path) -> None:
    with pytest.raises(SegmenterError):
        segment_episode(tmp_path / "nope", tmp_path / "02_panels")


def test_list_slice_paths_sorted(tmp_path) -> None:
    for name in ("002.png", "001.png", "003.png"):
        imwrite_unicode(tmp_path / name, make_reference(20, radius=5))
    assert [path.name for path in list_slice_paths(tmp_path)] == [
        "001.png",
        "002.png",
        "003.png",
    ]


def test_panels_dir_for() -> None:
    assert panels_dir_for("/tmp/series", 3).as_posix() == (
        "/tmp/series/episodes/ep_003/02_panels"
    )


# --------------------------------------------------------------------------
# Patch v2.5-1 第一重拦截：片尾版权图本地指纹直筛
# --------------------------------------------------------------------------
def make_copyright_bumper(height: int = 600, width: int = WIDTH) -> np.ndarray:
    """黑底 + 居中 WEBTOON 标志绿矩形（OpenCV HSV H=38，落在 [33,43] 区间）。"""
    image = np.full((height, width, 3), 5, dtype=np.uint8)  # BGR 均 <20 的近黑底
    hsv = np.zeros((1, 1, 3), dtype=np.uint8)
    hsv[:] = (38, 200, 220)  # H∈[33,43]、S>120、V>120
    green = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0]
    cv2.rectangle(
        image,
        (width // 2 - 40, height // 2 - 150),
        (width // 2 + 40, height // 2 + 150),
        green.tolist(),
        -1,
    )
    return image


def test_dhash_and_hamming_basic() -> None:
    """dHash 是 64-bit 整数；同图距离 0，距离对称且不越界。"""
    image = make_reference()
    fingerprint = dhash_64(image)
    assert isinstance(fingerprint, int)
    assert 0 <= fingerprint < (1 << 64)
    assert hamming_distance_64(fingerprint, fingerprint) == 0
    other = dhash_64(make_striped())
    assert hamming_distance_64(fingerprint, other) == hamming_distance_64(
        other, fingerprint
    )
    assert 0 <= hamming_distance_64(fingerprint, other) <= 64


def test_load_blacklist_hashes(tmp_path) -> None:
    """模板目录读图算哈希；目录缺失/为空时安静返回空元组（退化为纯启发式）。"""
    assert load_blacklist_hashes(tmp_path / "missing") == ()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert load_blacklist_hashes(empty) == ()

    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    imwrite_unicode(template_dir / "copyright.png", make_copyright_bumper())
    imwrite_unicode(template_dir / "normal.png", make_reference(600))
    hashes = load_blacklist_hashes(template_dir)
    assert len(hashes) == 2
    assert all(isinstance(item, int) for item in hashes)


def test_is_blacklisted_bumper_heuristic() -> None:
    """无模板时靠「黑底 + 居中绿块」启发式；暗黑正片（无绿）不误杀。"""
    bumper = make_copyright_bumper()
    # 先确认它不是空白面板（绿块带来边缘与中间调），否则黑名单逻辑永远跑不到
    assert is_blank_panel(cv2.cvtColor(bumper, cv2.COLOR_BGR2GRAY)) is False
    assert is_blacklisted_bumper(bumper, ()) is True

    normal = make_reference(600, center=(WIDTH // 2, 300), radius=80)
    assert is_blacklisted_bumper(normal, ()) is False

    dark_story = np.full((600, WIDTH, 3), 5, dtype=np.uint8)  # 纯暗黑剧情格，无绿
    cv2.circle(dark_story, (WIDTH // 2, 300), 60, (30, 30, 30), -1)
    assert is_blacklisted_bumper(dark_story, ()) is False


def test_is_blacklisted_bumper_template_hash(tmp_path) -> None:
    """与种子模板 dHash 距离 <= 6 即命中，普通漫画图不命中。"""
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    bumper = make_copyright_bumper()
    imwrite_unicode(template_dir / "webtoon_copyright_zh.png", bumper)
    hashes = load_blacklist_hashes(template_dir)

    assert is_blacklisted_bumper(bumper, hashes) is True  # 同图距离 0
    normal = make_reference(600, center=(WIDTH // 2, 300), radius=80)
    assert is_blacklisted_bumper(normal, hashes) is False


def test_segment_episode_skips_blacklisted_bumper(tmp_path) -> None:
    """端到端：版权尾图（命中黑名单模板）不落盘到 02_panels，普通分镜保留。"""
    raw_dir = tmp_path / "01_raw"
    raw_dir.mkdir()
    imwrite_unicode(
        raw_dir / "001.png",
        make_reference(600, center=(WIDTH // 2, 300), radius=80),
    )
    # 用真实种子图，保证 dHash 命中（合成图只能命中启发式，稳定性不足）。
    template = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "blacklist_templates"
        / "webtoon_copyright_zh.png"
    )
    imwrite_unicode(raw_dir / "002.png", cv2.imread(str(template)))

    panels_dir = tmp_path / "02_panels"
    metas = segment_episode(
        raw_dir, panels_dir, max_height=600, blacklist_hashes=load_blacklist_hashes()
    )

    assert len(metas) == 1
    assert metas[0].source_slice_range == (1, 1)
    assert sorted(path.name for path in panels_dir.iterdir()) == [
        "p_001.png",
        "panels.json",
    ]


# --------------------------------------------------------------------------
# Patch 4：全话无缝纵拼 → 精细留白切分 → 独白框向下吸附 → 超长画格不硬切
# --------------------------------------------------------------------------
def make_texture(top: int, bottom: int, *, seed: int = 0) -> np.ndarray:
    """复杂灰度纹理（模拟走廊透视线 / 人物渐变）。

    单色占比远低于 75%、每行都有笔画，因此不会被误判成「孤立独白框」，
    也不会被当成留白带。
    """
    height = bottom - top
    rows = np.arange(height, dtype=np.int32)[:, None]
    cols = np.arange(WIDTH, dtype=np.int32)[None, :]
    value = (rows * 37 + cols * 53 + seed * 11) % 211
    value = value + (rows * cols) % 97
    gray = np.clip(value, 0, 255).astype(np.uint8)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def test_patch4_joins_a_scene_cut_across_two_downloads(tmp_path) -> None:
    """第 0 步：一个镜头被平台从中间劈成两张下载图，必须还原成 1 个完整 PanelMeta。"""
    reference = make_reference(600, center=(WIDTH // 2, 300), radius=100)
    raw_dir = tmp_path / "01_raw"
    raw_dir.mkdir()
    imwrite_unicode(raw_dir / "001.png", reference[0:300])
    imwrite_unicode(raw_dir / "002.png", reference[300:600])

    metas = segment_episode(raw_dir, tmp_path / "02_panels", episode_id="wt_x_ep_001")

    assert len(metas) == 1
    panel = cv2.imread(str(metas[0].image_path))
    top, bottom = ink_rows(panel)
    # 圆完整落在同一个面板里（没有被下载图的接缝腰斩）
    assert bottom - top == 2 * 100
    assert metas[0].source_slice_range == (1, 2)


def test_patch4_splits_one_download_at_a_wide_white_gap(tmp_path) -> None:
    """第 1 步：同一张下载图里上下两个镜头被 150px 纯白带隔开 → 切成 2 个 PanelMeta。"""
    image = np.full((1200, WIDTH, 3), 255, dtype=np.uint8)
    image[0:400] = make_texture(0, 400, seed=1)  # 上半：走廊
    image[550:1200] = make_texture(550, 1200, seed=2)  # 下半：病房
    raw_dir = tmp_path / "01_raw"
    raw_dir.mkdir()
    imwrite_unicode(raw_dir / "001.png", image)

    metas = segment_episode(raw_dir, tmp_path / "02_panels")

    assert len(metas) == 2
    # 两块都带着各自的纹理，不会被误合并
    assert metas[0].height > 100 and metas[1].height > 100
    assert all(meta.source_slice_range == (1, 1) for meta in metas)


def test_patch4_merges_standalone_narration_box_into_the_scene_below(tmp_path) -> None:
    """第 2 步：黑底白字独白框 + 60px 白边 + 下方人物画框 → 吸附合并成 1 个 PanelMeta。

    Patch 6 收紧后的阈值：独白框高度 < 0.35 * 宽、间距 < 0.25 * 宽、
    合并后总高 <= 1.65 * 宽。
    """
    image = np.full((600, WIDTH, 3), 255, dtype=np.uint8)
    image[100:190] = 0  # 纯黑独白框（90px < 0.35*320=112）
    cv2.putText(
        image, "心里话", (40, 160), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2
    )
    image[250:600] = make_texture(250, 600, seed=3)  # 下方主画格（复杂渐变纹理）
    raw_dir = tmp_path / "01_raw"
    raw_dir.mkdir()
    imwrite_unicode(raw_dir / "001.png", image)

    metas = segment_episode(raw_dir, tmp_path / "02_panels")

    assert len(metas) == 1
    # 合并后是一整块：独白框 + 主画格都在里面
    assert metas[0].height > 400


def test_is_standalone_text_box_detects_narration_box_only() -> None:
    """判定函数本身：黑底白字扁框是独白框，带复杂纹理的场景图不是。"""
    narration = np.zeros((100, WIDTH, 3), dtype=np.uint8)
    cv2.putText(
        narration, "独白", (30, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2
    )
    scene = make_texture(0, 400, seed=7)

    assert is_standalone_text_box(narration, 60, WIDTH) is True
    assert is_standalone_text_box(scene, 60, WIDTH) is False


# --------------------------------------------------------------------------
# Patch v2.6-6：任意单色分隔带（黑底/灰底过渡带）+ 超长块安全阀
# --------------------------------------------------------------------------
def test_dark_transition_band_splits_panels() -> None:
    """黑底画风：画格之间是纯黑过渡带，也必须切开（不再只认纯白带）。"""
    canvas = np.full((1200, WIDTH, 3), 255, dtype=np.uint8)
    canvas[0:400] = make_texture(0, 400, seed=1)
    canvas[400:700] = 0  # 纯黑过渡带 300px
    canvas[700:1200] = make_texture(700, 1200, seed=2)

    blocks = cut_atomic_blocks(canvas, min_gap=18)

    assert len(blocks) == 2
    assert blocks[0][0] == 0
    assert blocks[-1][1] == 1200


def test_oversized_block_is_split_by_safety_valve() -> None:
    """极端长块（>4000px）必须被安全阀切开，绝不留上万像素的巨块。"""
    canvas = np.full((5200, WIDTH, 3), 255, dtype=np.uint8)
    canvas[0:5200] = make_texture(0, 5200, seed=5)

    blocks = cut_atomic_blocks(canvas, min_gap=18)

    assert len(blocks) >= 2
    assert all(bottom - top <= MAX_ATOMIC_BLOCK_HEIGHT for top, bottom in blocks)
    assert blocks[0][0] == 0
    assert blocks[-1][1] == 5200


def test_uniform_page_is_not_shredded_into_pieces() -> None:
    """整幅纯色的合成页：退化兜底成一块，而不是被切碎成零面板。"""
    canvas = np.full((1200, WIDTH, 3), (20, 40, 60), dtype=np.uint8)

    blocks = cut_atomic_blocks(canvas, min_gap=18)

    assert len(blocks) == 1
    assert blocks[0] == (0, 1200)


# --------------------------------------------------------------------------
# Patch v3.0-6 验收用例（PATCH_v3.0_DOUYIN_REBUILD.md）
# --------------------------------------------------------------------------
def test_patch6_clears_stale_panels_before_writing(tmp_path) -> None:
    """① 重跑前清空旧的 02_panels 缓存：旧命名 panel_001.png 与旧清单都要消失。

    旧缓存 + 双轨 ID 会让同一话出现两套分镜，成片直接翻倍播两遍。
    """
    raw_dir = tmp_path / "01_raw"
    raw_dir.mkdir()
    imwrite_unicode(
        raw_dir / "001.png", make_reference(600, center=(WIDTH // 2, 300), radius=80)
    )
    panels_dir = tmp_path / "02_panels"
    panels_dir.mkdir()
    imwrite_unicode(panels_dir / "panel_001.png", np.zeros((10, 10, 3), dtype=np.uint8))
    (panels_dir / "panels.json").write_text(
        json.dumps({"panels": [{"panel_id": "panel_001"}]}), encoding="utf-8"
    )

    metas = segment_episode(raw_dir, panels_dir, max_height=600)

    assert [meta.panel_id for meta in metas] == ["p_001"]
    assert sorted(path.name for path in panels_dir.iterdir()) == [
        "p_001.png",
        "panels.json",
    ]
    manifest = json.loads((panels_dir / "panels.json").read_text(encoding="utf-8"))
    assert [item["panel_id"] for item in manifest["panels"]] == ["p_001"]


def test_patch6_splits_on_noisy_black_transition_band(tmp_path) -> None:
    """② 全色系均匀带切割：上下两个复杂画面 + 中间 30px 噪点纯黑带 → 拆成 2 块。

    旧判据只认纯白带（std < 3.0），遇到 JPEG 噪点白边/纯黑深灰隔离带就切不断，
    会把两个镜头粘成一块。
    """
    rng = np.random.default_rng(7)
    canvas = np.full((830, WIDTH, 3), 255, dtype=np.uint8)
    canvas[0:400] = make_texture(0, 400, seed=1)
    noise = rng.integers(-5, 6, size=(30, WIDTH, 1)).astype(np.int16)
    canvas[400:430] = np.clip(128 + noise, 0, 255).astype(np.uint8)
    canvas[430:830] = make_texture(430, 830, seed=2)

    raw_dir = tmp_path / "01_raw"
    raw_dir.mkdir()
    imwrite_unicode(raw_dir / "001.png", canvas)

    metas = segment_episode(raw_dir, tmp_path / "02_panels")

    assert len(metas) == 2
    assert metas[0].source_slice_range == (1, 1)
    assert metas[1].source_slice_range == (1, 1)


def test_patch6_keeps_cross_slice_scene_and_trims_padding(tmp_path) -> None:
    """③ 跨图拼接 + 去白边：跨片画格不被切破，且切出的块上下不留多余白边。"""
    reference = make_reference(1200, center=(WIDTH // 2, 600), radius=150)
    raw_dir = tmp_path / "01_raw"
    raw_dir.mkdir()
    imwrite_unicode(raw_dir / "001.png", reference[0:600])
    imwrite_unicode(raw_dir / "002.png", reference[600:1200])

    panels_dir = tmp_path / "02_panels"
    metas = segment_episode(raw_dir, panels_dir, episode_id="wt_95_ep_001", max_height=1200)

    assert len(metas) == 1
    assert metas[0].source_slice_range == (1, 2)
    panel = cv2.imread(str(metas[0].image_path))
    top, bottom = ink_rows(panel)
    # 圆跨在两片原始切片的接缝上，成品面板里它是完整的一块
    assert bottom - top == 2 * 150
    # 上下白边被裁掉（每侧只留 10px 安全边距），块高明显小于原始 1200px
    assert panel.shape[0] < 1200
    assert panel.shape[0] > 320
