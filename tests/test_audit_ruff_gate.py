"""收工审核防线：ruff 作用域/语法门禁（F821 NameError 类 bug 单测跑不到，必须静态扫）。

背景：2026-08-28 审核发现 paper_trading_main.py 降级器初始化块位于
TradingScheduler 构造之后（degrader 未定义即引用），全量 pytest 639 绿仍漏网
（测试不执行 main()）。本测试把 ruff（含 F821/F811/E9 语法错误）纳入测试门禁。
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 今日治理批次新增/接线的文件（作用域/语法高危区）
GATED_FILES = [
    "scripts/paper_trading_main.py",
    "scripts/gov_scheme_signals.py",
    "hexbroker/diagnostics/signal_refresh.py",   # B+C 防再发批次
    "hexbroker/governance/degrade.py",
    "hexbroker/governance/ledger.py",
    "hexbroker/governance/scheme.py",
    "hexbroker/governance/interlock.py",
    "hexbroker/paper/scheduler.py",
]


def _ruff_cmd() -> list[str] | None:
    """定位可用的 ruff 可执行方式（PATH 二进制优先，回退 ``python -m ruff``）。

    2026-08-28 发现：托管 venv 装了 ruff 包但**未随包附带二进制**，
    ``python -m ruff`` 抛 RuffNotFound（exit 1），而 PATH 上的 ruff 正常。
    门禁若只认模块方式，会随解释器不同假失败——CI 信号必须稳定。
    """
    exe = shutil.which("ruff")
    if exe:
        return [exe]
    if importlib.util.find_spec("ruff") is not None:
        return [sys.executable, "-m", "ruff"]
    return None


def test_ruff_f821_gate(tmp_path: Path):
    """ruff 必须全过（重点 F821 未定义名 / F811 重复定义 / E9 语法错误）。"""
    cmd = _ruff_cmd()
    if cmd is None:
        pytest.skip("ruff 不可用（PATH 与当前解释器均无）")
    files = [str(ROOT / f) for f in GATED_FILES]
    r = subprocess.run(
        [*cmd, "check", "--select", "F821,F811,E9", "--output-format=concise", *files],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    if r.returncode != 0 and "RuffNotFound" in (r.stderr or ""):
        pytest.skip(f"ruff 二进制缺失（{' '.join(cmd)}）：{r.stderr.strip().splitlines()[-1]}")
    assert r.returncode == 0, f"ruff 门禁失败（作用域/语法）:\n{r.stdout}\n{r.stderr}"


def test_ruff_gate_is_interpreter_agnostic():
    """回归锁：门禁不得依赖单一解释器的 ruff 安装方式（至少一种可用）。"""
    assert _ruff_cmd() is not None, "当前环境无任何可用 ruff，门禁将静默失效"


def test_main_script_compiles():
    """paper_trading_main.py 语法可编译（最低防线）。"""
    src = (ROOT / "scripts" / "paper_trading_main.py").read_text(encoding="utf-8")
    compile(src, "paper_trading_main.py", "exec")


def test_degrader_block_precedes_scheduler_construction():
    """降级器初始化必须位于 TradingScheduler 构造之前（0828 审核回归锁）。"""
    src = (ROOT / "scripts" / "paper_trading_main.py").read_text(encoding="utf-8")
    init_pos = src.index("degrader = None")
    ctor_pos = src.index("scheduler = TradingScheduler(")
    assert init_pos < ctor_pos, "降级器初始化块不得位于 TradingScheduler 构造之后（F821 NameError）"
