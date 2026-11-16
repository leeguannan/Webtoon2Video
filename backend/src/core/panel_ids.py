"""分镜 ID 归一化（Stage 2 / Stage 3 / Stage 4 共用一处规则）。

**为什么需要这一层**：分镜 ID 历史上存在过三套写法——

* Stage 2 旧版把面板图写成 ``panel_001.png``，但 ``PanelMeta.panel_id`` 却是
  ``wt_5081_ep_001_p001``；
* Stage 3 旧版直接拿图片文件名当 ID（``panel_001``），与 Stage 2 的 ID 又对不上；
* Patch v2.6 之后 Stage 2 直接写 ``p_001.png``、``PanelMeta.panel_id`` 也是
  ``p_001``，Stage 3 原样透传。

三套写法让「历史产物」「外部测试脚本」「新流水线」互相认不出对方的分镜，
顺位校验 ``validate_beat_alignment`` 因此会把合法引用判成「引用了不存在的分镜」。

这里把归一化规则收敛到唯一入口，解析顺序：

1. **精确匹配**（``p_001``）；
2. **文件名主干**（``p_001.png`` → ``p_001``）；
3. **尾部数字**（``p_001`` / ``panel_001`` / ``ep_001_p001`` 都归到序号 1）。

Stage 6 的 ``PanelIndex`` 与 Stage 4 的 ``validate_beat_alignment`` 共用本模块，
保证「谁在什么时候解析分镜」得到的结果一致。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

#: 字符串尾部数字（``panel_001`` → ``001``）。
TRAILING_NUMBER = re.compile(r"(\d+)\s*$")


def trailing_number(value: str | None) -> int | None:
    """取字符串尾部数字（``panel_001`` → 1）；没有数字返回 ``None``。"""
    match = TRAILING_NUMBER.search(value or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:  # pragma: no cover - 正则已保证是数字
        return None


def panel_stem(value: str | Path | None) -> str:
    """把可能带路径/后缀的 ID 归到文件名主干（``a/b/p_001.png`` → ``p_001``）。"""
    if value is None:
        return ""
    return Path(str(value)).stem


def panel_sort_key(panel_id: str) -> tuple[str, int, str]:
    """分镜 ID 的自然序（``p_2`` 排在 ``p_10`` 前面）。"""
    match = re.search(r"(\d+)$", panel_id or "")
    if match:
        return (panel_id[: match.start()], int(match.group(1)), panel_id)
    return (panel_id, -1, panel_id)


class PanelIdResolver:
    """把各种写法的分镜 ID 解析成同一套「有效分镜 ID」。

    用法::

        resolver = PanelIdResolver(["p_001", "p_002"])
        resolver.resolve("panel_001")   # → "p_001"
        resolver.resolve("p_002.png")   # → "p_002"
        resolver.resolve("p_999")       # → None
    """

    def __init__(self, panel_ids: Iterable[str]) -> None:
        self.panel_ids: list[str] = [str(item) for item in panel_ids]
        self._by_exact: dict[str, str] = {}
        self._by_stem: dict[str, str] = {}
        self._by_number: dict[int, str] = {}
        for panel_id in self.panel_ids:
            if not panel_id:
                continue
            self._by_exact.setdefault(panel_id, panel_id)
            stem = panel_stem(panel_id)
            if stem:
                self._by_stem.setdefault(stem, panel_id)
            number = trailing_number(panel_id)
            if number is not None:
                self._by_number.setdefault(number, panel_id)

    def __bool__(self) -> bool:
        return bool(self.panel_ids)

    def __contains__(self, panel_id: object) -> bool:
        return self.resolve(str(panel_id)) is not None

    def resolve(self, panel_id: str | None) -> str | None:
        """解析成有效分镜 ID；认不出来返回 ``None``。"""
        if panel_id is None:
            return None
        key = str(panel_id).strip()
        if not key:
            return None
        if key in self._by_exact:
            return self._by_exact[key]
        stem = panel_stem(key)
        if stem in self._by_stem:
            return self._by_stem[stem]
        number = trailing_number(key)
        if number is not None:
            return self._by_number.get(number)
        return None

    def resolve_all(self, panel_ids: Iterable[str]) -> tuple[list[str], list[str]]:
        """批量解析，返回 ``(解析成功列表, 未识别列表)``。"""
        resolved: list[str] = []
        unknown: list[str] = []
        for panel_id in panel_ids:
            hit = self.resolve(panel_id)
            if hit is None:
                unknown.append(str(panel_id))
            else:
                resolved.append(hit)
        return resolved, unknown
