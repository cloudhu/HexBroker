"""P0-2 recursive 依赖环检测测试（PRD A2.3/A2.4）。

- A→B→A 回环检出；
- 干净调用链无环；
- CLI 退出码正确。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from _helpers import make_recursive_features

ROOT = Path(__file__).resolve().parents[1]


def _build(tmp_path, code: str):
    p = tmp_path / "sample.py"
    p.write_text(code, encoding="utf-8")
    from hexbroker.tools.dag import build_dependency_graph

    return str(p), build_dependency_graph([str(p)])


def test_a_b_a_cycle_detected(tmp_path):
    from hexbroker.tools.recursive_analysis import detect_cycles

    p, graph = _build(tmp_path, make_recursive_features()[0])
    cycles = detect_cycles(graph)
    assert len(cycles) >= 1
    c = cycles[0]
    names = {n.split("::")[-1] for n in c.nodes}
    assert {"compute_a", "compute_b"} <= names
    assert c.evidence.count("→") >= 2


def test_self_dependency_detected(tmp_path):
    from hexbroker.tools.recursive_analysis import detect_cycles

    _, graph = _build(
        tmp_path,
        'def recursive(df):\n    return recursive(df)\n',
    )
    cycles = detect_cycles(graph)
    assert len(cycles) >= 1
    assert any(n.split("::")[-1] == "recursive" for n in cycles[0].nodes)


def test_clean_call_chain_no_cycle(tmp_path):
    from hexbroker.tools.recursive_analysis import detect_cycles

    _, graph = _build(
        tmp_path,
        'def a(df):\n    return df\n\ndef b(df):\n    return a(df)\n',
    )
    assert detect_cycles(graph) == []


def test_cli_recursive_exit_code(tmp_path):
    p, _ = _build(tmp_path, make_recursive_features()[0])
    r = subprocess.run(
        [sys.executable, "-m", "hexbroker.tools.recursive_analysis", p],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert r.returncode != 0, "A→B→A 回环应非零退出"

    clean = tmp_path / "clean.py"
    clean.write_text('def a(df):\n    return df\n', encoding="utf-8")
    r2 = subprocess.run(
        [sys.executable, "-m", "hexbroker.tools.recursive_analysis", str(clean)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert r2.returncode == 0
