"""前视依赖静态分析（P0-2）。

对源码做 AST 危险原语扫描（同 bar 未来函数），输出 DAG + 可疑边 + 证据
（文件/行号/模式）。CLI：``python -m hexbroker.tools.lookahead_analysis [paths...]``，
发现未豁免缺陷 → 非零退出（CI 可拦截）。

检出模式（对应 PRD F2.1/F2.2 危险原语）：
- ``shift(-n)``：未来 n 根数据参与当前计算；
- ``rolling(center=True)``：窗口中心化引入未来；
- ``ewm(...)``：时间衰减序列存在错位风险（默认豁免覆盖已知安全 EMA 用法）；
- ``asof`` 前缺 ``reindex``：跨时区对齐未锁列；
- ``iloc[i+1]``：直接索引未来行；
- ``np.roll``：数组未来搬移。
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
from typing import Optional

from .dag import (
    KNOWN_PATTERNS,
    AnalysisReport,
    build_dependency_graph,
    expand_paths,
    find_suspicious_edges,
    norm_path,
)
from .exemptions import ExemptionRegistry


def _has_negative_shift_arg(call: ast.Call) -> bool:
    """shift 参数为负：shift(-5) / shift(periods=-h) / shift(-horizon)。"""

    def _is_neg(v: ast.AST) -> bool:
        return isinstance(v, ast.UnaryOp) and isinstance(v.op, ast.USub)

    for a in call.args:
        if _is_neg(a):
            return True
    for kw in call.keywords:
        if kw.arg in ("periods", "freq") and _is_neg(kw.value):
            return True
    return False


def _has_kwarg_true(call: ast.Call, name: str) -> bool:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    return False


def _has_reindex_in_scope(fn: ast.AST) -> bool:
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call):
            name = _call_name(sub.func)
            if name == "reindex":
                return True
    return False


def _call_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_forward_index(slice_: ast.AST) -> bool:
    """``iloc[i+1]`` 形态：BinOp(Add, Name, Constant) 视为未来行索引。

    ``iloc[-1]``（UnaryOp）与 ``iloc[0]``（Constant）不误报；
    ``iloc[i-1]``（过去）不误报。
    """
    if not isinstance(slice_, ast.BinOp):
        return False
    if not isinstance(slice_.op, ast.Add):
        return False
    names = [v for v in (slice_.left, slice_.right) if isinstance(v, ast.Name)]
    consts = [v for v in (slice_.left, slice_.right) if isinstance(v, ast.Constant)]
    return bool(names and consts)


def _detect_in_function(fn: ast.FunctionDef, file: str, lines: list[str]) -> list[dict]:
    """在单个函数节点内匹配危险原语，返回检出列表。"""
    detections: list[dict] = []

    def _evidence(line: int) -> str:
        return lines[line - 1].strip() if 0 < line <= len(lines) else "<unknown>"

    for sub in ast.walk(fn):
        if not isinstance(sub, ast.Call):
            continue
        name = _call_name(sub.func)
        if name is None:
            continue
        line = getattr(sub, "lineno", 0)
        if name == "shift" and _has_negative_shift_arg(sub):
            detections.append({"pattern": "shift_negative", "line": line, "evidence": _evidence(line), "file": file})
        if name == "rolling" and _has_kwarg_true(sub, "center"):
            detections.append({"pattern": "rolling_center", "line": line, "evidence": _evidence(line), "file": file})
        if name == "ewm":
            detections.append({"pattern": "ewm", "line": line, "evidence": _evidence(line), "file": file})
        if name == "asof" and not _has_reindex_in_scope(fn):
            detections.append({"pattern": "asof_without_reindex", "line": line, "evidence": _evidence(line), "file": file})
        if name == "roll" and isinstance(sub.func, ast.Attribute) and isinstance(sub.func.value, ast.Name) \
                and sub.func.value.id in ("np", "numpy"):
            detections.append({"pattern": "np_roll", "line": line, "evidence": _evidence(line), "file": file})

    for sub in ast.walk(fn):
        if isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Attribute) and sub.value.attr == "iloc":
            if _is_forward_index(sub.slice):
                line = getattr(sub, "lineno", 0)
                detections.append({"pattern": "iloc_forward", "line": line, "evidence": _evidence(line), "file": file})
    return detections


def _scan_patterns(sources: list[str]) -> dict[str, list[dict]]:
    """对每个源码文件做 AST 危险原语扫描。

    返回 ``{node_key: [{"pattern", "line", "evidence", "file"}]}``。
    """
    ast_info: dict[str, list[dict]] = {}
    for src in sources:
        path = Path(src)
        file = norm_path(str(path))
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (OSError, SyntaxError):
            continue
        lines = text.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            node_key = f"{file}::{node.name}"
            detections = _detect_in_function(node, file, lines)
            if detections:
                ast_info.setdefault(node_key, []).extend(detections)
    return ast_info


def analyze(paths: list[str], exemptions: Optional[ExemptionRegistry] = None) -> AnalysisReport:
    """对路径集合做 lookahead 静态分析。

    返回 ``AnalysisReport``；``exemptions`` 为 None 时不做豁免（全部检出为 violation）。
    """
    sources = expand_paths(paths)
    graph = build_dependency_graph(sources)
    ast_info = _scan_patterns(sources)
    raw_edges = find_suspicious_edges(graph, ast_info)

    report = AnalysisReport(
        files=[norm_path(p) for p in sources],
        nodes=len(graph),
        edges=sum(len(v) for v in graph.values()),
    )
    for edge in raw_edges:
        key = f"{edge.pattern}@{edge.file}"
        if exemptions is not None and exemptions.is_exempt(key):
            report.exempted.append(key)
        else:
            report.suspicious.append(edge)
    return report


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 入口：``python -m hexbroker.tools.lookahead_analysis [paths...]``。

    发现未豁免缺陷 → 返回非零（CI 可拦截）。
    """
    ap = argparse.ArgumentParser(
        prog="python -m hexbroker.tools.lookahead_analysis",
        description="前视依赖静态分析（P0-2）：检出同 bar 未来函数危险模式",
    )
    ap.add_argument("paths", nargs="+", help="源码文件或目录")
    ap.add_argument("--exemptions", type=str, default=None, help="额外豁免 yaml（pattern@file: reason）")
    ap.add_argument("--no-default-exemptions", action="store_true", help="不使用内置默认豁免")
    args = ap.parse_args(argv)

    reg = ExemptionRegistry.default_exemptions() if not args.no_default_exemptions else ExemptionRegistry()
    if args.exemptions:
        reg.load_yaml(args.exemptions)

    report = analyze(args.paths, reg)
    for e in report.suspicious:
        print(f"[LOOKAHEAD] {e.pattern} @ {e.file}:{e.line} | {e.evidence}")
    for c in report.cycles:
        print(f"[RECURSIVE] 回环 {c.evidence} @ {c.file}")
    print(
        f"分析完成：{len(report.files)} 文件 / {report.nodes} 节点 / {report.edges} 边 / "
        f"未豁免缺陷 {report.violations} / 已豁免 {len(report.exempted)}"
    )
    return 1 if not report.ok else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
