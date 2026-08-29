"""P2 处置：p43 raw_close 单日口径残留定点修复单测。

夹具 = 单品种湖内 parquet + 注入已知毛刺日；验证修复值数学精确性
（new_raw = adj/k）、安全门（G2 adj 跳变拦截）、dry-run 零写盘、
apply 内置复扫归零与幂等、sidecar 留痕。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_p43():
    spec = importlib.util.spec_from_file_location(
        "p43_raw_close_spike_repair",
        Path(__file__).resolve().parents[1] /
        "scripts" / "p43_raw_close_spike_repair.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["p43_raw_close_spike_repair"] = mod
    spec.loader.exec_module(mod)
    return mod


def _mk_lake(root: Path, sym0: str, raw: list[float], adj: list[float],
             start: str = "2023-01-02") -> None:
    dates = pd.bdate_range(start, periods=len(raw))
    df = pd.DataFrame({
        "symbol": [sym0.upper()] * len(dates),
        "datetime": dates,
        "raw_close": raw,
        "adj_close": adj,
    })
    d = root / "processed" / sym0 / "1d"
    d.mkdir(parents=True, exist_ok=True)
    df.to_parquet(d / f"{dates[0].year}.parquet")


# 基线：比值 1.5（adj=150, raw=100），01-05 毛刺（raw 误含复权价 150），
# 01-10 起因子 1.5→1.65（真换月，不得触碰）
BASE_ADJ = [150.0] * 6 + [165.0] * 4
GLITCH_RAW = [100.0] * 3 + [150.0] + [100.0] * 6


def test_plan_exact_repair_value(tmp_path):
    mod = _load_p43()
    _mk_lake(tmp_path, "cu0", GLITCH_RAW, BASE_ADJ)
    df = mod._load_processed(tmp_path, "cu0", "1d")
    plan = mod.build_plan(tmp_path, "cu0", df, tol=1e-3)
    assert len(plan["repairs"]) == 1
    r = plan["repairs"][0]
    assert r["date"] == "2023-01-05"
    assert r["k"] == 1.5
    assert r["old_raw"] == 150.0
    assert r["new_raw"] == 100.0  # = adj/k = 150/1.5，数学精确
    assert plan["gated"] == []


def test_gate_blocks_adj_glitch(tmp_path):
    """adj 本身跳变（毛刺在 adj 而非 raw）→ G2 拦截，不得自动修。"""
    mod = _load_p43()
    dates = pd.bdate_range("2023-01-02", periods=10)
    raw = [100.0] * 10
    # adj 在 01-05 单日 +50% 后回落 —— 毛刺在 adj，修复工具必须拒绝
    adj = [150.0] * 3 + [225.0] + [150.0] * 6
    _mk_lake(tmp_path, "cu0", raw, adj)
    df = mod._load_processed(tmp_path, "cu0", "1d")
    plan = mod.build_plan(tmp_path, "cu0", df, tol=1e-3)
    assert plan["repairs"] == []
    assert len(plan["gated"]) == 1
    assert plan["gated"][0]["gated_out"] == "G2_adj_jump"


def test_true_rollover_untouched(tmp_path):
    mod = _load_p43()
    _mk_lake(tmp_path, "cu0", GLITCH_RAW, BASE_ADJ)
    df = mod._load_processed(tmp_path, "cu0", "1d")
    plan = mod.build_plan(tmp_path, "cu0", df, tol=1e-3)
    touched_dates = {r["date"] for r in plan["repairs"]}
    assert "2023-01-10" not in touched_dates  # 真换月持续阶梯 ≠ 毛刺


def test_dry_run_zero_write_then_apply(tmp_path):
    mod = _load_p43()
    _mk_lake(tmp_path, "cu0", GLITCH_RAW, BASE_ADJ)
    part = tmp_path / "processed" / "cu0" / "1d" / "2023.parquet"
    before = part.read_bytes()

    rc = mod.main(["--data-root", str(tmp_path)])  # dry-run
    assert rc == 0
    assert part.read_bytes() == before  # 零写盘

    rc = mod.main(["--data-root", str(tmp_path), "--apply"])
    assert rc == 0
    df = pd.read_parquet(part)
    df["datetime"] = pd.to_datetime(df["datetime"])
    row = df.loc[df["datetime"] == pd.Timestamp("2023-01-05")].iloc[0]
    assert row["raw_close"] == 100.0  # 精确修复
    # 真换月段（01-10 起）不触碰
    row2 = df.loc[df["datetime"] == pd.Timestamp("2023-01-10")].iloc[0]
    assert row2["raw_close"] == 100.0 and row2["adj_close"] == 165.0


def test_apply_rescans_zero_and_idempotent(tmp_path):
    mod = _load_p43()
    _mk_lake(tmp_path, "cu0", GLITCH_RAW, BASE_ADJ)
    assert mod.main(["--data-root", str(tmp_path), "--apply"]) == 0
    # 幂等复跑：无事件 → no-op，不追加 sidecar 条目
    rc = mod.main(["--data-root", str(tmp_path), "--apply"])
    assert rc == 0
    # sidecar 留痕仅一次（实际写盘的那次）
    sidecar = tmp_path / "processed" / mod.SIDECAR_NAME
    log = json.loads(sidecar.read_text(encoding="utf-8"))
    assert len(log) == 1
    assert log[0]["residual_events_after"] == 0
    assert log[0]["n_rows"] == 1


def test_main_json_report(tmp_path):
    mod = _load_p43()
    _mk_lake(tmp_path, "cu0", GLITCH_RAW, BASE_ADJ)
    out = tmp_path / "plan.json"
    rc = mod.main(["--data-root", str(tmp_path), "--json", str(out)])
    assert rc == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep[0]["sym0"] == "cu0"
    assert rep[0]["repairs"][0]["new_raw"] == 100.0
