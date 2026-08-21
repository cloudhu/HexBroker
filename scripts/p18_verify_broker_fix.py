"""P18-P0 修复验证：SimBroker multiplier bug 修复前后 纯 B OOS 指标对比。

背景
----
P17 一致性验证发现：``SimBroker`` 实现/未实现盈亏使用全局 ``self.cost.multiplier``
（默认 10），未按品种级合约乘数记账（fee 却按品种级）→ ni0(×1) PnL 放大 10 倍、
jm0(×60) 缩小 0.167 倍 → P16/P3 回测 OOS 指标偏高。

修复（P18-P0，QA 指定最小方案）：broker.py 两处 ``self.cost.multiplier`` →
``self.cost._multiplier(symbol)``。

本脚本 A/B 验证：
  - **修复前（LegacyBroker 模拟）**：继承 SimBroker，PnL 仍用全局 multiplier
    （fee 保持品种级，忠实复刻旧行为）→ 应接近 P17 记录的修复前 BT 期末 1,905,743；
  - **修复后（当前 SimBroker）**：→ 应接近 ShadowAccount 准确记账
    （sa_samebar 期末 ~1,096,731 / sa_block 生产 ~1,090,232），而非 1,905,743。

输出：``artifacts/p18_broker_fix_verify.csv``（修复前后纯 B OOS 指标对比）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

import hexbroker.backtest.engine as bt_engine
from hexbroker.backtest.broker import Trade
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18

import scripts.p17_shadow_account as p17


class LegacyBroker(bt_engine.SimBroker):
    """忠实模拟修复前行为：PnL 用全局 multiplier=10（fee 仍品种级）。"""

    def unrealized(self, marks: dict[str, float]) -> float:
        total = 0.0
        for sym, pos in self.positions.items():
            if abs(pos) < 1e-12 or sym not in marks:
                continue
            total += pos * (marks[sym] - self.avg_entry[sym]) * self.cost.multiplier
        return float(total)

    def execute(self, symbol, target_qty, ref_price, is_today_close=False, timestamp=None):
        current = float(self.positions.get(symbol, 0.0))
        delta = float(target_qty) - current
        if abs(delta) < 1e-12:
            return None
        side = 1 if delta > 0 else -1
        is_open = (current == 0.0) or (np.sign(delta) == np.sign(current))
        fp, fee, _slip, _total = self.cost.trade_cost(
            ref_price, delta, is_open, is_today_close, symbol
        )
        if is_open:
            self.realized[symbol] = self.realized.get(symbol, 0.0) - fee
            abs_cur = abs(current)
            abs_del = abs(delta)
            if abs_cur < 1e-12:
                self.avg_entry[symbol] = fp
            else:
                self.avg_entry[symbol] = (
                    self.avg_entry[symbol] * abs_cur + fp * abs_del
                ) / (abs_cur + abs_del)
        else:
            closed = min(abs(delta), abs(current))
            direction = np.sign(current)
            self.realized[symbol] = (
                self.realized.get(symbol, 0.0)
                + direction * (fp - self.avg_entry[symbol]) * closed * self.cost.multiplier
                - fee
            )
            if abs(current + delta) < 1e-9:
                self.avg_entry[symbol] = 0.0
        self.positions[symbol] = current + delta
        trade = Trade(symbol, timestamp, delta, fp, fee, is_open, is_today_close)
        self.trades.append(trade)
        return trade


def _metrics(eq: pd.Series) -> dict:
    m = compute_metrics(eq, freq="daily")
    return {
        "final_equity": round(float(m.final_equity), 2),
        "total_return": round(float(m.total_return), 6),
        "annual_return": round(float(m.annual_return), 6),
        "max_drawdown": round(float(m.max_drawdown), 6),
        "sharpe": round(float(m.sharpe), 4),
        "n_days": int(m.n_bars) + 1,
    }


def main() -> None:
    print("P18-P0 修复验证：纯 B OOS（win252/thr0.7 + 180k + 费0.005% + 滑点1tick）")
    cfg = load_config("configs/base.yaml")
    cfg.backtest.contracts = CONTRACTS18
    prices = p17.load_prices_ohlc()
    basis = p17.load_basis_panel()

    # ---- 修复前（LegacyBroker 模拟旧行为）----
    orig_broker_cls = bt_engine.SimBroker
    bt_engine.SimBroker = LegacyBroker
    eq_pre = p17.run_backtest_oos(cfg, prices, basis)
    bt_engine.SimBroker = orig_broker_cls

    # ---- 修复后（当前 SimBroker）----
    eq_post = p17.run_backtest_oos(cfg, prices, basis)

    # ---- ShadowAccount 准确记账参考（P17 产物）----
    shadow = pd.read_csv(p17.ART_DIR / "consistency_compare.csv", index_col=0)
    shadow.index = pd.to_datetime(shadow.index)
    eq_samebar = shadow["sa_samebar"].dropna()
    eq_block = shadow["sa_block"].dropna()

    # 旧行为记录（P17 修复前 BT 期末，交叉校验）
    bt_old = pd.read_csv(p17.ART_DIR / "consistency_bt_equity.csv", index_col=0)
    bt_old.index = pd.to_datetime(bt_old.index)

    m_pre = _metrics(eq_pre)
    m_post = _metrics(eq_post)
    m_samebar = _metrics(eq_samebar)
    m_block = _metrics(eq_block)
    m_bt_old = _metrics(bt_old["equity"])

    # 日收益相关性：修复后 vs sa_samebar；修复前 vs 旧 BT 记录
    ret_post = eq_post.pct_change().dropna()
    ret_samebar = eq_samebar.pct_change().dropna()
    common = ret_post.index.intersection(ret_samebar.index)
    corr_post_samebar = float(ret_post[common].corr(ret_samebar[common]))

    ret_pre = eq_pre.pct_change().dropna()
    ret_old = bt_old["equity"].pct_change().dropna()
    common2 = ret_pre.index.intersection(ret_old.index)
    corr_pre_old = float(ret_pre[common2].corr(ret_old[common2]))

    rows = []
    for k in ("final_equity", "total_return", "annual_return", "max_drawdown", "sharpe", "n_days"):
        rows.append({
            "metric": k,
            "pre_fix": m_pre[k],
            "post_fix": m_post[k],
            "sa_samebar_ref": m_samebar[k],
            "sa_block_ref": m_block[k],
            "bt_old_recorded": m_bt_old[k],
        })
    rows.append({
        "metric": "corr_daily_ret",
        "pre_fix": corr_pre_old,
        "post_fix": corr_post_samebar,
        "sa_samebar_ref": np.nan,
        "sa_block_ref": np.nan,
        "bt_old_recorded": np.nan,
    })
    out = pd.DataFrame(rows)
    p17.ART_DIR.mkdir(parents=True, exist_ok=True)
    path = Path("artifacts/p18_broker_fix_verify.csv")
    out.to_csv(path, index=False, encoding="utf-8-sig")

    print()
    print(out.to_string(index=False))
    print(f"\n[OK] → {path}")
    print(f"  修复前 vs 旧记录(BT) 日收益相关: {corr_pre_old:.4f}（应≈1，模拟忠实）")
    print(f"  修复后 vs sa_samebar  日收益相关: {corr_post_samebar:.4f}（应≈1，口径一致）")


if __name__ == "__main__":
    main()
