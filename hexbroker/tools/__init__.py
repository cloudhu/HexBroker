"""P0-2 前视/递归依赖自检工具（静态分析，只读不污染）。

对特征/策略源码做 AST 依赖图分析，检出同一 bar 内使用未来信息的危险模式
（``shift(-n)`` / ``rolling(center=True)`` / ``ewm`` 时序错位 / ``asof`` 前缺
``reindex`` / ``iloc[i+1]`` / ``np.roll``）与跨 bar 递归依赖（A→B→A 回环）。

CLI 入口::

    python -m hexbroker.tools.lookahead_analysis [paths...] [--exemptions x.yaml]
    python -m hexbroker.tools.recursive_analysis [paths...]

发现未豁免缺陷 → 非零退出（CI 可拦截）。工具只读源码，不修改任何生产代码。
"""

from __future__ import annotations

from .dag import (
    AnalysisReport,
    Cycle,
    SuspiciousEdge,
    build_dependency_graph,
    find_suspicious_edges,
    norm_path,
)
from .exemptions import ExemptionRegistry

__all__ = [
    "AnalysisReport",
    "Cycle",
    "SuspiciousEdge",
    "ExemptionRegistry",
    "build_dependency_graph",
    "find_suspicious_edges",
    "norm_path",
]
