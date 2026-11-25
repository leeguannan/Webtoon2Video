#!/usr/bin/env python3
"""离线采编 CC0 音效素材（从 Freesound 检索 → 下载 → FFmpeg 预处理 → 落盘）。

**这个脚本不参与线上出片流水线**：它只是人工在有网环境里跑一次，
把 Stage 7 需要但盘面上缺失的音效补齐到 ``backend/assets/sfx/``。

背景：
    Stage 7 渲染时会按 ``sfx_cue`` 去 ``backend/assets/sfx/<key>.<ext>`` 取素材。
    盘面里已有的 11 个文件之外，本脚本负责补齐下面 13 个新 key
    （见 :data:`CURATED_SFX_QUERIES`）。

产物（默认写到 ``backend/assets/sfx/``）::

    <key>.mp3             # 预处理后的成片可用素材（44100Hz / 立体声 / 192kbps）
    SOURCES_CC0.json      # 溯源清单：每个 key 的 Freesound 来源与许可证

**规则**：

* 只收 **CC0**（``Creative Commons 0``）素材；其它许可证一律丢弃。
* 预览音频用 FFmpeg 统一处理：去首尾静音 → 裁到 ``--max-duration`` → 尾部淡出
  → 按实测峰值归一 + 限幅 → 44.1kHz 立体声 192kbps。
  （不用 ``loudnorm``：音效普遍短于 3 秒，EBU R128 的 400ms 积分窗口量不到瞬态。）
* **默认不覆盖已存在的文件**（``--overwrite`` 才覆盖）。
* 网络异常一律降级：打印警告、返回空候选，**不抛异常、不影响退出码**。

用法::

    # 只补盘面上缺的（人先看一眼要装什么）
    python backend/scripts/fetch_cc0_sfx.py --dry-run

    # 真正下载（需要联网 + 已安装 ffmpeg）
    python backend/scripts/fetch_cc0_sfx.py

    # 只补某一个
    python backend/scripts/fetch_cc0_sfx.py --only slap --overwrite
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

#: Freesound 搜索页地址。
FREESOUND_SEARCH_URL = "https://freesound.org/search/"
#: Freesound 站点根地址（用于把相对路径补成绝对地址）。
FREESOUND_BASE_URL = "https://freesound.org"
#: CC0 许可证的官方地址。
CC0_LICENSE_URL = "https://creativecommons.org/publicdomain/zero/1.0/"
#: 搜索时的许可证过滤表达式（Freesound 的 ``f`` 参数语法）。
FREESOUND_LICENSE_FILTER = 'license:"Creative Commons 0"'
#: 每个音效 key 的最长时长（秒），超出部分裁掉并做尾部淡出。
DEFAULT_MAX_DURATION_S = 3.0
#: 打分时的目标时长（秒）：短促音效最常用。
TARGET_DURATION_S = 1.2
#: 可用素材的时长上限（秒）：超过就是环境录音，当不了单音音效用。
_MAX_USABLE_DURATION_S = 8.0
#: 检索词里的通用词：几乎每个素材名都带，用它们做名称匹配毫无区分度。
_GENERIC_QUERY_WORDS = frozenset(
    {"sound", "sounds", "effect", "effects", "audio", "clip", "fx", "sfx"}
)
#: 素材落盘后的目标峰值电平（dBFS）：留 1.5dB 余量给限幅器。
_SFX_TARGET_PEAK_DBFS = -3.0
#: 峰值归一的增益上下限（dB）：太安静的素材别硬推，太响的必须压下来。
_SFX_MIN_GAIN_DB = -6.0
_SFX_MAX_GAIN_DB = 24.0
#: 限幅器系数：0.8413 == -1.5 dBFS。
_SFX_LIMITER_COEFF = 0.8413
#: 盘面上可能出现的音效文件后缀（判定某个 key 是否「已存在」）。
SFX_SUFFIXES: tuple[str, ...] = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac")
#: 溯源清单文件名。
SOURCES_FILENAME = "SOURCES_CC0.json"
#: 请求 Freesound 时的 User-Agent（带上项目名方便对方识别脚本）。
USER_AGENT = "webtoon2video-cc0-sfx-fetch/1.0 (offline asset tool)"
#: 下载预览音频的超时秒数（素材很小，但给足余量）。
DOWNLOAD_TIMEOUT_S = 60.0
#: 干跑时每个 key 最多打印几条候选。
DRY_RUN_CANDIDATE_LIMIT = 5

#: 新增 13 个音效 key 对应的推荐英文检索词（用于 Freesound 搜索）。
CURATED_SFX_QUERIES: dict[str, tuple[str, ...]] = {
    "door_open": ("door open", "opening door", "door creak open"),
    "slap": ("slap", "face slap", "slap sound"),
    "punch": ("punch", "punch impact", "boxing punch"),
    "sword_clash": ("sword clash", "swords fight", "metal clash"),
    "cloth_rustle": ("cloth rustle", "fabric rustle", "cloth movement"),
    "boom_hit": ("boom hit", "cinematic boom", "impact boom"),
    "applause": ("applause", "clapping crowd", "crowd applause"),
    "glass_shatter": ("glass shatter", "breaking glass", "glass break"),
    "thunder": ("thunder crack", "thunder clap", "thunder rumble", "thunder sound effect"),
    "clock_tick": ("clock tick", "clock ticking", "tick tock"),
    "page_turn": ("page turn", "turning pages", "paper page"),
    "camera_shutter": ("camera shutter", "shutter click", "photo camera"),
    "car_horn": ("car horn", "horn honk", "car honk"),
}


@dataclass(frozen=True, slots=True)
class FreesoundCandidate:
    """一条 Freesound 候选（严格 CC0）。"""

    sound_id: str
    name: str
    description: str
    author: str
    license_name: str
    license_url: str
    duration_s: float
    num_downloads: int
    preview_url: str
    page_url: str
    #: 预览音频的备用地址（首选地址下载失败时回退）。
    preview_fallback_url: str = ""
    #: 命中这条候选时用的检索词（打分时按它做名称相关性加权）。
    matched_query: str = ""


# ---------------------------------------------------------------------------
# HTML 解析
# ---------------------------------------------------------------------------

#: 容器 div 上的 sound id（首选策略）。
_SOUND_ID_RE = re.compile(r"id=[\"']sound-(\d+)[\"']", re.IGNORECASE)
#: 详情页链接里的 sound id（备用策略）。
_SOUNDS_LINK_RE = re.compile(r"/sounds/(\d+)/")
#: 新版搜索结果卡片容器（每个卡片 = 一条候选）。
_CARD_RE = re.compile(r'class="[^"]*\bbw-search__result\b[^"]*"', re.IGNORECASE)
#: 卡片里的播放器标签：新版把标题/时长/下载量/预览地址都放在它的 data-* 属性上。
_PLAYER_TAG_RE = re.compile(r"<div[^>]*\bbw-player\b[^>]*>", re.IGNORECASE)
#: 播放器标签上的 data-* 属性。
_PLAYER_ATTR_RE = re.compile(r"data-([a-z0-9-]+)=\"([^\"]*)\"", re.IGNORECASE)
#: 新版预览音频的 CDN 根地址（旧的 ``freesound.org/data/previews/...`` 已下线）。
_FREESOUND_CDN = "https://cdn.freesound.org/previews"
#: 时长文本（``0:03`` / ``0:03.21``）。
_CLOCK_RE = re.compile(r"(\d{1,3}):(\d{2}(?:\.\d+)?)")


class _TextExtractor(HTMLParser):
    """把一段 HTML 片段压成纯文本（忽略 ``script`` / ``style`` 内容）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.parts.append(data)

    @property
    def text(self) -> str:
        """片段的纯文本（空白已折叠）。"""
        return re.sub(r"\s+", " ", "".join(self.parts)).strip()


def _strip_tags(fragment: str) -> str:
    """把 HTML 片段转成纯文本；解析失败时退化为正则去标签。"""
    if not fragment:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(fragment)
        parser.close()
    except Exception:  # pragma: no cover - HTMLParser 极少抛异常
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()
    return parser.text


def _normalize_license(text: str) -> str:
    """把许可证文本归一化；``CC0`` / ``cc 0`` 统一成 ``Creative Commons 0``。"""
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return ""
    if re.fullmatch(r"(cc|creative\s*commons)[\s\-_]*0", cleaned, re.IGNORECASE):
        return "Creative Commons 0"
    return cleaned


def _is_cc0(license_name: str) -> bool:
    """许可证名里是否包含 ``Creative Commons 0``。"""
    return "creative commons 0" in license_name.lower()


def _extract_license(block: str) -> str:
    """从候选块里取许可证名；取不到返回空串（调用方按「非 CC0」丢弃）。"""
    # 1) 专门的许可证容器（class 里带 license / cc0）
    for pattern in (
        r'class="[^"]*\blicense\b[^"]*"[^>]*>(.*?)<',
        r'class="[^"]*\bcc0\b[^"]*"[^>]*>(.*?)<',
    ):
        found = re.search(pattern, block, re.IGNORECASE | re.DOTALL)
        if found:
            normalized = _normalize_license(_strip_tags(found.group(1)))
            if normalized:
                return normalized
    # 2) CC0 许可证链接
    if re.search(r"creativecommons\.org/publicdomain/zero", block, re.IGNORECASE):
        return "Creative Commons 0"
    # 3) title 属性
    found = re.search(r'title="([^"]*[Cc]reative[^"]*)"', block)
    if found:
        normalized = _normalize_license(_strip_tags(found.group(1)))
        if normalized:
            return normalized
    # 4) 整块纯文本里的许可证短语
    text = _strip_tags(block)
    found = re.search(r"Creative\s+Commons\s+0", text, re.IGNORECASE)
    if found:
        return "Creative Commons 0"
    found = re.search(r"(Attribution|Noncommercial|BY[\s-]NC)", text, re.IGNORECASE)
    if found:
        return found.group(1)
    return ""


def _parse_clock(text: str) -> float | None:
    """把 ``0:03`` / ``0:03.21`` 换算成秒；不匹配返回 ``None``。"""
    found = _CLOCK_RE.search(text)
    if not found:
        return None
    return float(found.group(1)) * 60 + float(found.group(2))


def _extract_duration(block: str) -> float:
    """从候选块里取时长（秒）；取不到返回 ``0.0``。"""
    attrs = _player_attrs(block)
    raw = attrs.get("duration", "")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    found = re.search(
        r'class="[^"]*\bduration\b[^"]*"[^>]*>(.*?)<',
        block,
        re.IGNORECASE | re.DOTALL,
    )
    if found:
        seconds = _parse_clock(_strip_tags(found.group(1)))
        if seconds is not None:
            return seconds
    seconds = _parse_clock(_strip_tags(block))
    return seconds if seconds is not None else 0.0


def _extract_downloads(block: str) -> int:
    """从候选块里取下载量；取不到返回 ``0``。"""
    attrs = _player_attrs(block)
    raw = attrs.get("num-downloads", "")
    if raw.isdigit():
        return int(raw)
    found = re.search(r"num-downloads[^>]*>(.*?)<", block, re.IGNORECASE | re.DOTALL)
    if found:
        digits = re.sub(r"\D", "", _strip_tags(found.group(1)))
        if digits:
            return int(digits)
    # 退化：找 "num-downloads" 后面最近的数字
    index = block.lower().find("num-downloads")
    if index >= 0:
        found = re.search(r"(\d[\d,\s]*)", _strip_tags(block[index:index + 300]))
        if found:
            digits = re.sub(r"\D", "", found.group(1))
            if digits:
                return int(digits)
    return 0


def _extract_author(block: str) -> tuple[str, str]:
    """从候选块里取作者，返回 ``(用户名 slug, 显示名)``。"""
    # 0) 新版播放器属性里直接带作者 slug
    attrs = _player_attrs(block)
    slug = attrs.get("username", "")
    if slug:
        # 显示名仍走作者主页链接的链接文本（拿不到就用 slug）
        found = re.search(
            r'<a[^>]+href="[^"]*?/people/' + re.escape(slug) + r'/"[^>]*>(.*?)</a>',
            block,
            re.IGNORECASE | re.DOTALL,
        )
        text = _strip_tags(found.group(1)) if found else ""
        return slug, text or slug
    # 1) 作者主页链接：``/people/<slug>/``（链接文本是显示名）
    found = re.search(
        r'<a[^>]+href="[^"]*?/people/([^/"?#]+)/"[^>]*>(.*?)</a>',
        block,
        re.IGNORECASE | re.DOTALL,
    )
    if found:
        slug = found.group(1)
        text = _strip_tags(found.group(2))
        return slug, text or slug
    # 2) 详情页链接：``/people/<slug>/sounds/<id>/``（链接文本是音效名，不能当作者名）
    found = re.search(
        r'<a[^>]+href="[^"]*?/people/([^/"?#]+)/sounds/[^"]*"[^>]*>(.*?)</a>',
        block,
        re.IGNORECASE | re.DOTALL,
    )
    if found:
        slug = found.group(1)
        return slug, slug
    # 3) 兜底：裸的 ``/people/<slug>/`` 字样
    found = re.search(r"/people/([^/\"'?#]+)/", block, re.IGNORECASE)
    if found:
        return found.group(1), found.group(1)
    return "", ""


def _extract_name(block: str, sound_id: str) -> str:
    """取音效名称：优先 ``data-title``，其次 ``/sounds/<id>/`` 链接文本。"""
    title = _player_attrs(block).get("title", "")
    if title:
        return title
    found = re.search(
        r'<a[^>]+href="[^"]*/sounds/' + re.escape(sound_id) + r'/[^"]*"[^>]*>(.*?)</a>',
        block,
        re.IGNORECASE | re.DOTALL,
    )
    if found:
        text = _strip_tags(found.group(1))
        if text:
            return text
    found = re.search(
        r'class="[^"]*\btitle\b[^"]*"[^>]*>(.*?)</div>',
        block,
        re.IGNORECASE | re.DOTALL,
    )
    if found:
        text = _strip_tags(found.group(1))
        if text:
            return text
    found = re.search(
        r'<a[^>]*class="[^"]*\bsound_title\b[^"]*"[^>]*>(.*?)</a>',
        block,
        re.IGNORECASE | re.DOTALL,
    )
    if found:
        return _strip_tags(found.group(1))
    return ""


def _extract_description(block: str) -> str:
    """取音效描述；取不到返回空串。"""
    found = re.search(
        r'class="[^"]*\bdescription\b[^"]*"[^>]*>(.*?)</div>',
        block,
        re.IGNORECASE | re.DOTALL,
    )
    if found:
        return _strip_tags(found.group(1))
    return ""


def _extract_preview_url(block: str, sound_id: str) -> str:
    """取预览音频地址（首选地址）。

    新版页面：``bw-player`` 的 ``data-mp3`` 直接给出 CDN 地址；已知作者 uid 时
    按 Freesound 的命名规则拼一个更高码率的 ``_hq.mp3``。
    旧版页面：``/data/previews/<id>/<id>_hq.mp3``。
    """
    attrs = _player_attrs(block)
    low = attrs.get("mp3", "")
    user_id = attrs.get("user-id", "")
    if user_id.isdigit():
        return f"{_FREESOUND_CDN}/{int(sound_id) // 1000}/{sound_id}_{user_id}-hq.mp3"
    if low:
        return low
    found = re.search(
        r"(https?://freesound\.org)?(/data/previews/[^\"'\s>]+_hq\.mp3)",
        block,
        re.IGNORECASE,
    )
    if not found:
        found = re.search(
            r"(https?://freesound\.org)?(/data/previews/[^\"'\s>]+)",
            block,
            re.IGNORECASE,
        )
    if found:
        if found.group(1):
            return found.group(1) + found.group(2)
        return f"{FREESOUND_BASE_URL}{found.group(2)}"
    return f"{FREESOUND_BASE_URL}/data/previews/{sound_id}/{sound_id}_hq.mp3"


def _extract_preview_fallback_url(block: str, sound_id: str) -> str:
    """取预览音频的备用地址（首选地址 404 时回退）。"""
    low = _player_attrs(block).get("mp3", "")
    if low:
        return low
    return ""


def _player_attrs(block: str) -> dict[str, str]:
    """取候选块里 ``bw-player`` 标签上的 ``data-*`` 属性。

    新版 Freesound 搜索页把标题、时长、下载量、预览地址、作者都放在这里，
    用普通标签文本正则一个都抓不到，必须直接读属性。
    """
    found = _PLAYER_TAG_RE.search(block)
    if not found:
        return {}
    return {key.lower(): value for key, value in _PLAYER_ATTR_RE.findall(found.group(0))}


def _split_sound_blocks(html: str) -> list[tuple[str, str]]:
    """把搜索结果 HTML 切成若干「候选块」。

    新版页面：按 ``class="bw-search__result"`` 卡片切分，sound id 取自卡片里
    ``bw-player`` 的 ``data-sound-id``。

    旧版页面：先试 ``id="sound-<id>"``（容器 div 上的 id），找不到再退到
    ``/sounds/<id>/`` 链接；切分点之间的原文就是一块。

    切分点会往前退到最近的 ``<``，免得把 ``<a href="...`` 之类的开标签切掉。
    """
    cards = list(_CARD_RE.finditer(html))
    if cards:
        blocks: list[tuple[str, str]] = []
        for index, card in enumerate(cards):
            end = cards[index + 1].start() if index + 1 < len(cards) else len(html)
            chunk = html[card.start() : end]
            attrs = _player_attrs(chunk)
            sound_id = attrs.get("sound-id", "")
            if sound_id:
                blocks.append((sound_id, chunk))
        if blocks:
            return blocks
    matches = list(_SOUND_ID_RE.finditer(html))
    if not matches:
        matches = list(_SOUNDS_LINK_RE.finditer(html))
    starts: list[int] = []
    for match in matches:
        head = html.rfind("<", 0, match.start())
        starts.append(head if head >= 0 else match.start())
    blocks = []
    for index, match in enumerate(matches):
        end = starts[index + 1] if index + 1 < len(starts) else len(html)
        blocks.append((match.group(1), html[starts[index] : end]))
    return blocks


def _parse_candidate(sound_id: str, block: str) -> FreesoundCandidate | None:
    """把一块 HTML 解析成候选；非 CC0 或缺关键字段时返回 ``None``。"""
    license_name = _extract_license(block)
    if not _is_cc0(license_name):
        return None
    author_slug, author_name = _extract_author(block)
    page_url = f"{FREESOUND_BASE_URL}/people/{author_slug}/sounds/{sound_id}/"
    return FreesoundCandidate(
        sound_id=sound_id,
        name=_extract_name(block, sound_id),
        description=_extract_description(block),
        author=author_name,
        license_name=license_name,
        license_url=CC0_LICENSE_URL,
        duration_s=_extract_duration(block),
        num_downloads=_extract_downloads(block),
        preview_url=_extract_preview_url(block, sound_id),
        page_url=page_url,
        preview_fallback_url=_extract_preview_fallback_url(block, sound_id),
    )


def parse_freesound_html(html: str) -> list[FreesoundCandidate]:
    """从一段 Freesound 搜索结果 HTML 里抽取候选条目。

    **只保留 CC0**（许可证名里包含 ``Creative Commons 0`` 的条目）；
    非 CC0 一律丢弃。解析不了的条目跳过，不得抛异常。
    """
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for sound_id, block in _split_sound_blocks(html or ""):
        if sound_id not in grouped:
            grouped[sound_id] = []
            order.append(sound_id)
        grouped[sound_id].append(block)

    candidates: list[FreesoundCandidate] = []
    for sound_id in order:
        try:
            candidate = _parse_candidate(sound_id, "\n".join(grouped[sound_id]))
        except Exception:  # pragma: no cover - 单条坏数据不该中断整体
            continue
        if candidate is not None:
            candidates.append(candidate)
    return candidates


# ---------------------------------------------------------------------------
# 联网检索
# ---------------------------------------------------------------------------


def _build_search_url(query: str, page: int) -> str:
    """拼一条 Freesound 搜索页 URL（只搜 CC0 许可证）。"""
    params = urllib.parse.urlencode(
        {"q": query, "f": FREESOUND_LICENSE_FILTER, "page": page}
    )
    return f"{FREESOUND_SEARCH_URL}?{params}"


def _http_get_text(url: str, *, timeout_s: float) -> str:
    """抓一个网页并按 UTF-8 解码（解码失败用替换字符兜底）。"""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        raw = response.read()
    return raw.decode("utf-8", errors="replace")


def score_candidate(candidate: FreesoundCandidate, *, target_duration_s: float = 1.2) -> float:
    """候选打分：下载量越高越好、时长越接近 target_duration_s 越好。

    超过 ``_MAX_USABLE_DURATION_S`` 的候选直接判 0 分——那是环境录音（雷雨、
    街道、整段掌声），裁到 3 秒只剩一段噪声，当不了音效用。
    """
    downloads = max(0, int(candidate.num_downloads))
    target = max(0.05, float(target_duration_s))
    duration = float(candidate.duration_s)
    if duration > _MAX_USABLE_DURATION_S:
        return 0.0
    # 下载量：取对数并压到 0~1（100 万次下载 ≈ 1.0）
    download_score = min(1.0, math.log10(downloads + 1) / 6.0)
    # 时长拟合：差一个目标长度就衰减到 1/e
    if duration <= 0.0:
        duration_fit = 0.3
    else:
        duration_fit = math.exp(-abs(duration - target) / target)
    score = download_score * 0.6 + duration_fit * 0.4
    # 名称相关性：Freesound 是全文检索，同一批结果里混着「手指弹响」这种
    # 名字完全不相干但被标签命中的素材。名字里带检索词才加分，不带就压分。
    query = str(getattr(candidate, "matched_query", "") or "")
    words = [
        word
        for word in re.split(r"\W+", query.lower())
        if len(word) >= 3 and word not in _GENERIC_QUERY_WORDS
    ]
    if words:
        name = candidate.name.lower()
        if any(word in name for word in words):
            score += 0.12
        else:
            score *= 0.55
    return score


def search_freesound_cc0(
    query: str,
    *,
    pages: int = 2,
    timeout_s: float = 20.0,
) -> list[FreesoundCandidate]:
    """联网搜索 Freesound 的 CC0 素材（走 HTML 搜索页，不是 API）。

    按下载量与「短促时长拟合度」综合打分后降序返回。
    任何网络异常都退化成返回空列表并打印警告，不得抛异常。
    """
    candidates: list[FreesoundCandidate] = []
    seen: set[str] = set()
    for page in range(1, max(1, int(pages)) + 1):
        url = _build_search_url(query, page)
        try:
            html = _http_get_text(url, timeout_s=float(timeout_s))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"警告：访问 Freesound 失败（{url}）：{exc}", file=sys.stderr)
            continue
        except Exception as exc:  # pragma: no cover - 兜底：绝不因网络问题崩掉
            print(f"警告：访问 Freesound 异常（{url}）：{exc}", file=sys.stderr)
            continue
        for candidate in parse_freesound_html(html):
            if candidate.sound_id in seen:
                continue
            seen.add(candidate.sound_id)
            candidates.append(candidate)
    candidates.sort(key=lambda item: score_candidate(item), reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# 下载与 FFmpeg 预处理
# ---------------------------------------------------------------------------


def _fmt_secs(value: float) -> str:
    """把秒数格式化成 FFmpeg 能吃的短小数（``3.0`` → ``"3.0"``）。"""
    text = f"{float(value):.3f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def _build_filter_chain(max_duration_s: float, gain_db: float, *, channels: int = 2) -> str:
    """拼 FFmpeg 滤镜链（顺序固定，不可调换）。

    ``gain_db`` 是按实测峰值算出的整体增益（见 :func:`_measure_peak_dbfs`）；
    ``channels`` 是源声道数（单声道时显式复制成双声道）。
    """
    silence = "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05"
    parts = [
        # 1) 去首静音；反向再走一遍去尾静音
        silence,
        f"areverse,{silence},areverse",
        # 2) 裁到最长时长 + 尾部淡出（太短就别淡出了）
        f"atrim=0:{_fmt_secs(max_duration_s)}",
    ]
    if max_duration_s >= 0.3:
        fade_start = max(0.0, max_duration_s - 0.15)
        parts.append(f"afade=t=out:st={_fmt_secs(fade_start)}:d=0.15")
    # 3) 峰值归一 + 限幅（不用 loudnorm：音效普遍短于 3 秒，
    #    EBU R128 的 400ms 积分窗口量不到瞬态，loudnorm 会失准甚至静音）
    if abs(gain_db) >= 0.05:
        parts.append(f"volume={gain_db:.2f}dB")
    # 4) 单声道显式复制成双声道：让 ffmpeg 的默认重矩阵（-3dB）不要生效，
    #    否则峰值归一的增益会被这 3dB 悄悄吃掉
    if channels == 1:
        parts.append("pan=stereo|c0=c0|c1=c0")
    elif channels and channels != 2:
        parts.append("aformat=channel_layouts=stereo")
    parts.append(f"alimiter=limit={_SFX_LIMITER_COEFF:.4f}:level=disabled")
    return ",".join(parts)


def _measure_peak_dbfs(src: Path, *, ffmpeg_bin: str = "ffmpeg") -> float | None:
    """量一份音频的峰值电平（dBFS）；量不到返回 ``None``。

    用 ``volumedetect`` 的 ``max_volume``（样本峰值），它对短瞬态也稳定，
    不像 EBU R128 的 LUFS 那样需要 400ms 积分窗口。
    """
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-nostats",
        "-nostdin",
        "-i",
        str(src),
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"找不到 ffmpeg 可执行文件（{ffmpeg_bin}）") from exc
    if result.returncode != 0:
        return None
    found = re.search(r"max_volume:\s*(-?[0-9.]+)\s*dB", result.stderr or "")
    if not found:
        return None
    try:
        return float(found.group(1))
    except ValueError:
        return None


def _probe_channels(src: Path, *, ffmpeg_bin: str = "ffmpeg") -> int:
    """探测源音频的声道数；探测不到返回 ``0``（表示未知）。"""
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-nostats",
        "-nostdin",
        "-i",
        str(src),
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return 0
    found = re.search(r"Hz,\s*([a-z0-9]+(?:\.[0-9])?)\s*,", result.stderr or "")
    if not found:
        return 0
    raw = found.group(1).lower()
    if raw == "mono":
        return 1
    if raw == "stereo":
        return 2
    try:
        return int(raw)
    except ValueError:
        return 0


def preprocess_audio(
    src: Path,
    dst: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
    max_duration_s: float = DEFAULT_MAX_DURATION_S,
) -> Path:
    """用 FFmpeg 把下载到的音轨预处理成成片可用素材并落盘。

    滤镜链（顺序固定）：
      1) ``silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05`` 去掉首静音
         并用 ``areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,areverse`` 去尾静音；
      2) ``atrim=0:{max_duration_s}`` + ``afade=t=out:st={max_duration_s - 0.15}:d=0.15``（尾部 0.15s 淡出，
         max_duration_s < 0.3 时只保留 atrim 不加 afade）；
      3) ``volume={gain}dB`` + ``alimiter=limit=0.8413:level=disabled``——按实测峰值把素材推到
         ``_SFX_TARGET_PEAK_DBFS``（-3 dBFS）并限幅，防止削波；
      4) 输出 ``-ar 44100 -ac 2 -b:a 192k``。

    为什么不 ``loudnorm``：音效素材普遍短于 3 秒（这里最短的只有 0.25 秒），
    EBU R128 的 400ms 积分窗口量不到这种瞬态，``loudnorm`` 的 ``input_i`` 会直接
    报 ``-inf``/``-70``，对瞬态还可能把增益压死导致成片里听不见。峰值归一对
    任意长度都成立，结果也可复现。
    返回 dst。
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    peak = _measure_peak_dbfs(src, ffmpeg_bin=ffmpeg_bin)
    if peak is None:
        gain_db = 0.0
    else:
        gain_db = max(_SFX_MIN_GAIN_DB, min(_SFX_MAX_GAIN_DB, _SFX_TARGET_PEAK_DBFS - peak))
    channels = _probe_channels(src, ffmpeg_bin=ffmpeg_bin)
    command = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(src),
        "-vn",
        "-af",
        _build_filter_chain(max_duration_s, gain_db, channels=channels),
        "-ar",
        "44100",
        "-ac",
        "2",
        "-b:a",
        "192k",
        str(dst),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"找不到 ffmpeg 可执行文件（{ffmpeg_bin}）") from exc
    if result.returncode != 0:
        lines = [line for line in (result.stderr or "").splitlines() if line.strip()]
        detail = lines[-1] if lines else "未知错误"
        raise RuntimeError(f"FFmpeg 预处理失败（{src.name} → {dst.name}）：{detail}")
    return dst


def _http_download(url: str, dst: Path, *, timeout_s: float) -> None:
    """把一个 URL 下载到本地文件；失败抛 ``RuntimeError``。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            data = response.read()
    except Exception as exc:
        raise RuntimeError(f"下载失败：{url}（{exc}）") from exc
    if not data:
        raise RuntimeError(f"下载失败：{url}（响应为空）")
    dst.write_bytes(data)


def download_candidate(
    candidate: FreesoundCandidate,
    dst: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
    max_duration_s: float = DEFAULT_MAX_DURATION_S,
) -> Path:
    """下载候选预览音频并预处理落盘。下载失败抛 RuntimeError。

    首选地址（通常是高码率 CDN 直链）失败时自动回退到备用地址。
    """
    urls = [candidate.preview_url]
    if candidate.preview_fallback_url and candidate.preview_fallback_url not in urls:
        urls.append(candidate.preview_fallback_url)
    urls = [url for url in urls if url]
    if not urls:
        raise RuntimeError(f"候选 {candidate.sound_id} 没有预览音频地址，无法下载")
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix="cc0_sfx_", suffix=".mp3")
    os.close(handle)
    temp_path = Path(temp_name)
    errors: list[str] = []
    try:
        for url in urls:
            try:
                _http_download(url, temp_path, timeout_s=DOWNLOAD_TIMEOUT_S)
            except RuntimeError as exc:
                errors.append(str(exc))
                continue
            preprocess_audio(temp_path, dst, ffmpeg_bin=ffmpeg_bin, max_duration_s=max_duration_s)
            return dst
        raise RuntimeError("；".join(errors))
    finally:
        temp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 溯源清单
# ---------------------------------------------------------------------------


def load_sources(path: Path) -> dict[str, dict[str, object]]:
    """读取 SOURCES_CC0.json（不存在时返回 {}）。"""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"警告：读取溯源清单失败（{path}）：{exc}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"警告：溯源清单格式异常（{path}），按空清单处理", file=sys.stderr)
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def save_sources(path: Path, sources: dict[str, dict[str, object]]) -> Path:
    """原子写 SOURCES_CC0.json（utf-8 + indent=2 + ensure_ascii=False）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".sources_", suffix=".json", dir=str(path.parent))
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        payload = json.dumps(sources, ensure_ascii=False, indent=2) + "\n"
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return path


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------


def default_assets_dir() -> Path:
    """默认音效盘面目录：``<仓库根>/backend/assets/sfx``。"""
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "backend" / "assets" / "sfx"


def _key_exists(assets_dir: Path, key: str) -> bool:
    """盘面上是否已有该 key 的音效文件（任意常见后缀）。"""
    for suffix in SFX_SUFFIXES:
        if (assets_dir / f"{key}{suffix}").is_file():
            return True
    return False


def _select_keys(args: argparse.Namespace, assets_dir: Path) -> list[str]:
    """按命令行参数挑出本次要处理的 key。"""
    only = [item.strip() for item in str(args.only or "").split(",") if item.strip()]
    if only:
        wanted = set(only)
        for key in only:
            if key not in CURATED_SFX_QUERIES:
                print(f"警告：未知音效 key「{key}」，已忽略", file=sys.stderr)
        return [key for key in CURATED_SFX_QUERIES if key in wanted]
    if args.overwrite:
        return list(CURATED_SFX_QUERIES)
    return [key for key in CURATED_SFX_QUERIES if not _key_exists(assets_dir, key)]


def _collect_candidates(
    key: str,
    *,
    pages: int,
    timeout_s: float,
) -> list[FreesoundCandidate]:
    """按 key 的检索词去 Freesound 抓候选（去重 + 按分数降序）。"""
    candidates: list[FreesoundCandidate] = []
    seen: set[str] = set()
    for query in CURATED_SFX_QUERIES[key]:
        for candidate in search_freesound_cc0(query, pages=pages, timeout_s=timeout_s):
            if candidate.sound_id in seen:
                continue
            seen.add(candidate.sound_id)
            candidates.append(replace(candidate, matched_query=query))
    candidates.sort(key=lambda item: score_candidate(item), reverse=True)
    return candidates


def _print_candidate(key: str, candidate: FreesoundCandidate, rank: int) -> None:
    """打印一条候选的关键信息（干跑与正式跑共用）。"""
    print(
        f"  [{key}] #{rank} {candidate.name} | 作者 {candidate.author} | "
        f"下载 {candidate.num_downloads} | 时长 {candidate.duration_s:.2f}s | "
        f"许可证 {candidate.license_name}"
    )
    print(f"      预览 {candidate.preview_url}")
    print(f"      页面 {candidate.page_url}")


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description="离线采编 CC0 音效素材（从 Freesound 检索 + 下载 + FFmpeg 预处理）",
    )
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help="只处理盘面上尚缺的 key（默认行为，显式写出来更直观）",
    )
    parser.add_argument(
        "--only",
        default="",
        help="只处理这些 key（逗号分隔），覆盖 missing 判定",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印候选，不下载",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="已存在的 key 也重新下载覆盖（默认跳过已存在的）",
    )
    parser.add_argument(
        "--assets-dir",
        default=None,
        help="音效盘面目录（默认 backend/assets/sfx）",
    )
    parser.add_argument(
        "--ffmpeg-bin",
        default="ffmpeg",
        help="ffmpeg 可执行名（默认 ffmpeg）",
    )
    parser.add_argument(
        "--max-duration",
        type=float,
        default=DEFAULT_MAX_DURATION_S,
        help="最长时长秒（默认 3.0）",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=2,
        help="搜索页数（默认 2）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="单次请求超时秒（默认 20）",
    )
    args = parser.parse_args(argv)

    assets_dir = (
        Path(args.assets_dir).expanduser() if args.assets_dir else default_assets_dir()
    )
    if not shutil.which(args.ffmpeg_bin):
        message = (
            f"错误：找不到 ffmpeg 可执行文件「{args.ffmpeg_bin}」，"
            "请先安装 ffmpeg 或用 --ffmpeg-bin 指定路径"
        )
        if args.dry_run:
            print(f"{message}（干跑模式不下载，先继续检索）", file=sys.stderr)
        else:
            print(message, file=sys.stderr)
            return 2

    if args.only and not any(key in CURATED_SFX_QUERIES for key in str(args.only).split(",")):
        print(f"错误：--only 指定的 key 都不在 CURATED_SFX_QUERIES 里：{args.only}", file=sys.stderr)
        return 1

    selected = _select_keys(args, assets_dir)
    if not selected:
        print("没有需要处理的音效 key（盘面上都已齐了）")
        return 0

    sources_path = assets_dir / SOURCES_FILENAME
    sources = load_sources(sources_path)
    changed = False

    for key in selected:
        dst = assets_dir / f"{key}.mp3"
        if dst.exists() and not args.overwrite:
            print(f"跳过 {key}：{dst.name} 已存在（需要覆盖请加 --overwrite）")
            continue
        candidates = _collect_candidates(
            key,
            pages=max(1, int(args.pages)),
            timeout_s=float(args.timeout),
        )
        if not candidates:
            print(
                f"警告：{key} 没有搜到 CC0 候选（检索词：{', '.join(CURATED_SFX_QUERIES[key])}）",
                file=sys.stderr,
            )
            continue
        if args.dry_run:
            print(f"[{key}] 候选 {len(candidates)} 条：")
            for rank, candidate in enumerate(
                candidates[:DRY_RUN_CANDIDATE_LIMIT], start=1
            ):
                _print_candidate(key, candidate, rank)
            continue

        best = candidates[0]
        print(f"[{key}] 采用：{best.name} | {best.author} | {best.license_name}")
        try:
            download_candidate(
                best,
                dst,
                ffmpeg_bin=args.ffmpeg_bin,
                max_duration_s=float(args.max_duration),
            )
        except RuntimeError as exc:
            print(f"错误：{key} 下载或预处理失败：{exc}", file=sys.stderr)
            continue
        sources[key] = {
            "sound_id": best.sound_id,
            "name": best.name,
            "author": best.author,
            "license": best.license_name,
            "license_url": best.license_url,
            "freesound_url": best.page_url,
            "preview_url": best.preview_url,
            "num_downloads": best.num_downloads,
            "duration_s": best.duration_s,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "max_duration_s": float(args.max_duration),
        }
        changed = True
        print(f"已写入 {dst}")

    if changed:
        save_sources(sources_path, sources)
        print(f"溯源清单已更新：{sources_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    sys.exit(main())
