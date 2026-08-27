"""AST 依赖图与危险原语检测（P0-2 静态分析核心）。

节点 = 源码文件中的函数（含模块级代码 ``{file}::<module>``），
边 = 函数间调用依赖（A 调用 B → A→B）。
``find_suspicious_edges`` 把节点级危险原语检出转为 ``SuspiciousEdge`` 列表。

只读分析：不修改任何生产代码（PRD A2.5）。
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 危险原语 → 模式名（默认豁免按 模式@文件 键注册）
PATTERN_SHIFT_NEGATIVE = "shift_negative"
PATTERN_ROLLING_CENTER = "rolling_center"
PATTERN_EWM = "ewm"
PATTERN_ASOF = "asof_without_reindex"
PATTERN_ILOC_FORWARD = "iloc_forward"
PATTERN_NP_ROLL = "np_roll"

KNOWN_PATTERNS = (
    PATTERN_SHIFT_NEGATIVE,
    PATTERN_ROLLING_CENTER,
    PATTERN_EWM,
    PATTERN_ASOF,
    PATTERN_ILOC_FORWARD,
    PATTERN_NP_ROLL,
)


@dataclass
class SuspiciousEdge:
    """一条危险原语检出（source=节点，target 留空供 DAG 边标注）。"""

    source: str
    target: str
    pattern: str
    file: str
    line: int
    evidence: str


@dataclass
class Cycle:
    """一个有向环（A→B→A / 自依赖）。"""

    nodes: list[str]
    file: str
    evidence: str


@dataclass
class AnalysisReport:
    """一次静态分析的汇总报告。"""

    files: list[str]
    nodes: int
    edges: int
    suspicious: list[SuspiciousEdge] = field(default_factory=list)
    cycles: list[Cycle] = field(default_factory=list)
    exempted: list[str] = field(default_factory=list)  # 已豁免检出（供审计）

    @property
    def violations(self) -> int:
        return len(self.suspicious) + len(self.cycles)

    @property
    def ok(self) -> bool:
        return self.violations == 0


def norm_path(p: str) -> str:
    """归一化路径：正斜杠 + 相对 cwd（用于豁免键与报告）。

    无法相对化（跨盘符/越出 cwd）时返回绝对路径（正斜杠）。
    """
    s = str(p).replace("\\", "/")
    try:
        rel = os.path.relpath(s)
    except ValueError:
        return s
    rel = rel.replace("\\", "/")
    return rel if not rel.startswith("..") else s


def expand_paths(paths: list[str]) -> list[str]:
    """把文件/目录参数展开为 .py 文件列表（目录递归，跳过 __pycache__）。"""
    out: list[str] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            out.extend(
                str(f) for f in sorted(path.rglob("*.py"))
                if "__pycache__" not in str(f)
            )
        elif path.exists() and path.suffix == ".py":
            out.append(str(path))
    return out


def _iter_functions(tree: ast.AST, file: str) -> list[tuple[str, ast.FunctionDef]]:
    """收集文件中所有函数定义，节点名 = ``{file}::{name}``。"""
    nodes: list[tuple[str, ast.FunctionDef]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes.append((f"{file}::{node.name}", node))
    return nodes


def build_dependency_graph(sources: list[str]) -> dict[str, set[str]]:
    """AST 解析源码，返回调用依赖邻接表 ``{node: set[依赖节点]}``。

    依赖仅统计「直接函数调用」（``compute_b(df)`` 这类 ``Name`` 调用）且目标
    在被分析文件内已定义；``obj.method()`` 等 Attribute 调用不建边（方法归属
    无法静态确定，避免 ``eng.run()`` 误指到本地同名 ``run`` 函数的假环）。
    """
    graph: dict[str, set[str]] = {}
    defined: dict[str, str] = {}  # 函数名 → 节点名（跨文件同名后者覆盖，静态近似可接受）
    for src in sources:
        path = Path(src)
        file = norm_path(str(path))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        module_node = f"{file}::<module>"
        graph.setdefault(module_node, set())
        funcs = _iter_functions(tree, file)
        for node_key, fn in funcs:
            graph.setdefault(node_key, set())
            defined[fn.name] = node_key
        for node_key, fn in funcs:
            for call in _collect_calls(fn):
                if not isinstance(call.func, ast.Name):
                    continue  # 仅直接函数调用（Attribute 方法调用不建边）
                callee = call.func.id
                if callee in defined:
                    graph[node_key].add(defined[callee])
    return graph


def _collect_calls(node: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def find_suspicious_edges(graph: dict[str, set[str]], ast_info: dict) -> list[SuspiciousEdge]:
    """把节点级危险原语检出转为 SuspiciousEdge 列表。

    ``ast_info`` 形如 ``{node_key: [{"pattern", "line", "evidence", "file"}]}``
    （由 ``hexbroker.tools.lookahead_analysis._scan_patterns`` 生成）。
    """
    edges: list[SuspiciousEdge] = []
    for node, detections in ast_info.items():
        for d in detections:
            edges.append(
                SuspiciousEdge(
                    source=node,
                    target="",
                    pattern=str(d["pattern"]),
                    file=str(d["file"]),
                    line=int(d["line"]),
                    evidence=str(d["evidence"]),
                )
            )
    return edges
