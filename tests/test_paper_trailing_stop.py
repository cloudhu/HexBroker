"""移动止损接线（P0-C 续接）回归锁（2026-09-05）。

盲区根因：``RiskManager.evaluate`` 每 tick 算出 ``decision.stop_price``（ATR trailing stop），
但 ``PaperBroker._stops`` 仅在开仓/反手时写入，持仓期间**永不更新** → 名义「移动止损」
实为固定止损（被冻结在开仓价），完全不随价移动。

本文件锁定三件事：
1. ``PaperBroker.set_trailing_stop`` 仅对有持仓品种更新实际档位，空仓/无效值幂等 no-op；
2. ``TradingScheduler._process_symbol`` 每 tick 把 ``decision.stop_price`` 刷进 ``_stops``
   （真实风控引擎驱动，证明接线生效）；
3. 刷新后的 trailing stop 被影子监视器正确识别为触发（影子模式仅记录，验证观察链路打通）。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from hexbroker.paper.shadow_stops import KIND_STOP, ShadowStopMonitor
from hexbroker.paper.types import Plan, Quote

from test_paper_pipeline import MON, _eff_signal, _scheduler

NOW = datetime(2026, 8, 24, 10, 0)


def _quote(price: float, symbol: str = "rb0") -> Quote:
    return Quote(symbol=symbol, ts=NOW, price=price, open=price, high=price, low=price)


def _open_long(sched, qty=1.0, price=3000.0, stop=2900.0, tp=3200.0, symbol="rb0"):
    plan = Plan(
        symbol=symbol,
        direction=1 if qty > 0 else -1,
        target_qty=qty,
        target_pos_pct=0.1,
        stop_price=stop,
        take_profit=tp,
    )
    sched._broker.execute_plan(plan, _quote(price, symbol), NOW)


def _monitor(tmp_path, **kw):
    return ShadowStopMonitor(path=tmp_path / "shadow_stops.jsonl", **kw)


# --------------------------------------------------------------------------- #
# 1. set_trailing_stop 单元行为
# --------------------------------------------------------------------------- #
def test_set_trailing_stop_updates_open_position(tmp_path):
    sched, _ = _scheduler(tmp_path, sig=_eff_signal())
    _open_long(sched, qty=1.0, price=3000.0, stop=2900.0, tp=3200.0)
    assert sched._broker._stops["rb0"] == pytest.approx(2900.0)
    # 价格上行 → trailing stop 应随之上移
    sched._broker.set_trailing_stop("rb0", 2950.0)
    assert sched._broker._stops["rb0"] == pytest.approx(2950.0)


def test_set_trailing_stop_is_idempotent_on_flat_or_invalid(tmp_path):
    sched, _ = _scheduler(tmp_path, sig=_eff_signal())
    _open_long(sched, qty=1.0, price=3000.0, stop=2900.0, tp=3200.0)
    sched._broker.set_trailing_stop("rb0", 2950.0)
    # None / <=0 / NaN → 不覆盖既有档位
    sched._broker.set_trailing_stop("rb0", None)
    assert sched._broker._stops["rb0"] == pytest.approx(2950.0)
    sched._broker.set_trailing_stop("rb0", 0.0)
    assert sched._broker._stops["rb0"] == pytest.approx(2950.0)
    sched._broker.set_trailing_stop("rb0", float("nan"))
    assert sched._broker._stops["rb0"] == pytest.approx(2950.0)
    # 平仓至 0 后：空仓 no-op，不得残留幽灵档位污染 account.json 与影子扫描
    sched._broker.execute_plan(
        Plan(symbol="rb0", direction=0, target_qty=0.0, target_pos_pct=0.0),
        _quote(2950.0),
        NOW,
    )
    assert abs(sched._broker.position("rb0")) <= 1e-12
    sched._broker.set_trailing_stop("rb0", 2950.0)
    assert "rb0" not in sched._broker._stops, "空仓不得注入止损档位"


# --------------------------------------------------------------------------- #
# 2. 调度器接线：每 tick 把 decision.stop_price 刷入 _stops（真实引擎驱动）
# --------------------------------------------------------------------------- #
def test_process_symbol_refreshes_trailing_stop_every_tick(tmp_path):
    sched, _ = _scheduler(tmp_path, sig=_eff_signal())
    mk = {"rb0": 3000.0}
    sched._process_symbol("rb0", NOW, _quote(3000.0), mk)
    assert sched._broker.position("rb0") > 0, "前置：真实引擎开仓"
    stop0 = sched._broker._stops["rb0"]
    assert stop0 is not None and 0 < stop0 < 3000.0, "多头止损应低于开仓价"

    # 价格上行到 3100 → trailing stop 应单调上移（不回缩）
    sched._process_symbol("rb0", NOW, _quote(3100.0), {"rb0": 3100.0})
    stop1 = sched._broker._stops["rb0"]
    assert stop1 is not None, "持仓期间 stop_price 不得被清空"
    assert stop1 >= stop0, f"trailing stop 应单调不减：{stop0} -> {stop1}"
    assert sched._broker.position("rb0") > 0


# --------------------------------------------------------------------------- #
# 3. trailing 值被影子监视器识别（影子模式仅记录 → 观察链路打通）
# --------------------------------------------------------------------------- #
def test_trailing_stop_value_drives_shadow_detection(tmp_path):
    """关键断言：刷新后的 trailing 档位（2950）若被监视为触发，则证明「接线」生效。

    对照：若 stop 仍冻结在开仓价 2900，则 2940 > 2900 不会触发；
    只有 trailing 值（2950）真正进入 ``_stops``，2940 <= 2950 才会记 [SHADOW-STOP]。
    """
    sched, _ = _scheduler(tmp_path, sig=_eff_signal())
    _open_long(sched, qty=1.0, price=3000.0, stop=2900.0, tp=3200.0)
    # 模拟价格上行后 trailing stop 被刷新到 2950（即 _process_symbol 每 tick 所做的事）
    sched._broker.set_trailing_stop("rb0", 2950.0)
    sched._shadow_stops = _monitor(tmp_path, enabled=True, multiplier_fn=lambda s: 10.0)

    before = {
        "pos": sched._broker.position("rb0"),
        "trades": len(sched._broker.trades()),
    }
    # 现价回落至 2940，恰好击穿 trailing 档位 2950
    sched._shadow_stop_sweep({"rb0": _quote(2940.0)}, NOW, MON)

    # 影子模式：账户零变更（P0-C 命门）
    assert sched._broker.position("rb0") == pytest.approx(before["pos"])
    assert len(sched._broker.trades()) == before["trades"]
    # 但 trailing 击穿被记录
    lines = (tmp_path / "shadow_stops.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == KIND_STOP
    assert rec["threshold"] == pytest.approx(2950.0)
