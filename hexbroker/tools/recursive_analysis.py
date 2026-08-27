"""递归依赖环检测（P0-2）。

在特征列依赖图（函数调用图）上做环检测：A→B→A 回环、特征依赖自身未来值、
自依赖均检出。CLI：``python -m hexbroker.tools.recursive_analysis [paths...]``，
发现回环 → 非零退出（CI 可拦截）。
"""

from __future__ import annotations

import argparse
from typing import Optional

from .dag import Cycle, build_dependency_graph, expand_paths, norm_path


def detect_cycles(graph: dict[str, set[str]]) -> list[Cycle]:
    """DFS 找有向环；A→B→A 与自依赖均检出。返回去重后的 Cycle 列表。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    stack: list[str] = []
    found: list[Cycle] = []

    def _file_of(node: str) -> str:
        return node.split("::", 1)[0] if "::" in node else ""

    def dfs(u: str) -> None:
        color[u] = GRAY
        stack.append(u)
        for v in sorted(graph.get(u, set())):
            if color.get(v, WHITE) == WHITE:
                dfs(v)
            elif color.get(v) == GRAY:
                try:
                    start = stack.index(v)
                except ValueError:
                    start = 0
                cycle_nodes = stack[start:] + [v]
                found.append(
                    Cycle(nodes=cycle_nodes, file=_file_of(u), evidence=" → ".join(cycle_nodes))
                )
        stack.pop()
        color[u] = BLACK

    for u in sorted(graph.keys()):
        if color.get(u, WHITE) == WHITE:
            dfs(u)

    seen: set[tuple[str, ...]] = set()
    uniq: list[Cycle] = []
    for c in found:
        key = tuple(c.nodes)
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 入口：``python -m hexbroker.tools.recursive_analysis [paths...]``。"""
    ap = argparse.ArgumentParser(
        prog="python -m hexbroker.tools.recursive_analysis",
        description="递归依赖环检测（P0-2）：检出 A→B→A 回环与特征自依赖",
    )
    ap.add_argument("paths", nargs="+", help="源码文件或目录")
    args = ap.parse_args(argv)

    sources = expand_paths(args.paths)
    graph = build_dependency_graph(sources)
    cycles = detect_cycles(graph)
    for c in cycles:
        print(f"[RECURSIVE] 回环 {c.evidence} @ {c.file}")
    print(f"分析完成：{len(sources)} 文件 / {len(cycles)} 个回环")
    return 1 if cycles else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
