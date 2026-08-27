"""P0-1 撮合假设文档结构检查（PRD A1.1：文档存在且 ≥14 条，每条含默认值与影响方向）。"""

from __future__ import annotations

import re
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "matching-assumptions.md"

VALID_DIRECTIONS = ("乐观", "保守", "中性")


def _parse_rows() -> list[tuple[str, str, str, str, str]]:
    """解析 markdown 表格行，返回 (ID, 内容, 默认值, 影响方向, 代码位置)。"""
    rows: list[tuple[str, str, str, str, str]] = []
    text = DOC.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        if re.match(r"^MA-\d{2,}$", cells[0]):
            rows.append((cells[0], cells[1], cells[2], cells[3], cells[4]))
    return rows


def test_doc_exists():
    assert DOC.exists(), f"缺少撮合假设文档 {DOC}"


def test_at_least_14_assumptions():
    rows = _parse_rows()
    assert len(rows) >= 14, f"撮合假设应 ≥14 条，实际 {len(rows)}"


def test_each_row_has_content_default_direction_and_location():
    rows = _parse_rows()
    assert rows, "文档中未解析到任何 MA-xx 行"
    for rid, content, default, direction, loc in rows:
        assert content, f"{rid} 缺少【内容】"
        assert default, f"{rid} 缺少【默认值】"
        assert direction, f"{rid} 缺少【影响方向】"
        assert loc, f"{rid} 缺少【代码位置】"
        assert direction in VALID_DIRECTIONS, f"{rid} 影响方向必须是 {'/'.join(VALID_DIRECTIONS)}，实际 {direction}"


def test_ids_unique_and_sequential():
    rows = _parse_rows()
    ids = [r[0] for r in rows]
    assert len(ids) == len(set(ids)), "MA id 重复"
    nums = sorted(int(i.split("-")[1]) for i in ids)
    assert nums == list(range(1, len(nums) + 1)), "MA id 应连续编号"


def test_covers_required_topics():
    """PRD F1.1 要求覆盖的 14 个主题关键词。"""
    rows = _parse_rows()
    blob = " ".join(r[1] for r in rows)
    topics = [
        "成交时点", "成交价", "滑点", "成交量", "部分成交", "整单拒绝",
        "跳空", "手续费", "保证金", "涨跌停", "平今", "复权", "数据对齐",
        "订单类型", "资金约束", "跨 bar",
    ]
    missing = [t for t in topics if t not in blob]
    assert not missing, f"撮合假设未覆盖主题: {missing}"
