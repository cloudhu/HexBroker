"""T03 PaperBroker 测试：资金约束（A4）/ 预算 / 平今费与 SimBroker 口径一致 / 快照续跑。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from hexbroker.backtest.broker import SimBroker
from hexbroker.backtest.cost import CostModel
from hexbroker.paper.broker import PaperBroker
from hexbroker.paper.types import Plan, Quote


def _cost() -> CostModel:
    """品种级合约参数：ag0 ×15/0.01、rb0 ×10/1、c0 ×10/1（与 paper.yaml 一致）。"""
    return CostModel(
        fee_open=0.00005,
        fee_close=0.00005,
        fee_close_today=0.00010,
        slippage_ticks=1.0,
        margin_rate=0.12,
        multiplier=10.0,
        min_tick=10.0,
        contracts={
            "ag0": {"multiplier": 15.0, "min_tick": 0.01},
            "rb0": {"multiplier": 10.0, "min_tick": 1.0},
            "c0": {"multiplier": 10.0, "min_tick": 1.0},
        },
    )


def _broker(initial: float = 100_000.0, budget_ratio: float = 0.30) -> PaperBroker:
    return PaperBroker(_cost(), initial_capital=initial, budget_ratio=budget_ratio)


def _quote(symbol: str = "rb0", price: float = 3000.0) -> Quote:
    return Quote(symbol=symbol, ts=datetime(2026, 8, 24, 10, 0), price=price, open=2990, high=3010, low=2990, pre_settle=2990)


def _plan(symbol: str, qty: float, stop: float | None = 2900.0) -> Plan:
    return Plan(
        symbol=symbol,
        direction=1 if qty > 0 else -1,
        target_qty=qty,
        target_pos_pct=0.0,
        stop_price=stop,
        take_profit=3100.0,
        source="test",
    )


# ---------------------------------------------------------------------------
# 初始账户
# ---------------------------------------------------------------------------
def test_initial_snapshot():
    b = _broker()
    snap = b.snapshot()
    assert snap.equity == pytest.approx(100_000.0)
    assert snap.cash == pytest.approx(100_000.0)
    assert snap.margin_used == pytest.approx(0.0)
    assert snap.positions == {}


# ---------------------------------------------------------------------------
# 开仓/保证金/资金约束
# ---------------------------------------------------------------------------
def test_open_position_and_margin():
    b = _broker()
    ev = b.execute_plan(_plan("rb0", 1), _quote(), ts=datetime(2026, 8, 24, 10, 0))
    assert ev is not None
    assert ev.symbol == "rb0"
    assert ev.qty == pytest.approx(1.0)
    assert b.position("rb0") == pytest.approx(1.0)
    # 保证金 = 成交价(含滑点 3001) × 乘数 10 × 1 手 × 12%
    assert b.margin_used() == pytest.approx(3001 * 10 * 1 * 0.12, abs=1e-6)
    # 可用资金 = 权益 - 保证金 >= 0
    assert b.available_cash() >= 0.0


def test_budget_constraint_rejects_oversized_order():
    b = _broker(initial=100_000.0, budget_ratio=0.30)
    # 100 手 ag：保证金 ≈ 8000×15×100×12% = 144 万 >> 预算 3 万 → 拒绝
    ev = b.execute_plan(_plan("ag0", 100), _quote("ag0", 8000.0), ts=datetime(2026, 8, 24, 10, 0))
    assert ev is None
    assert b.position("ag0") == 0.0


def test_budget_ratio_040_allows_ag_one_lot():
    """P1-2：预算 0.40 下 10 万账户 ag 1 手（保证金≈30161 ≤ 40000）可通过预算防线。"""
    b = _broker(initial=100_000.0, budget_ratio=0.40)
    q = _quote("ag0", 16756.0)
    ev = b.execute_plan(_plan("ag0", 1), q, ts=datetime(2026, 8, 24, 10, 0))
    assert ev is not None
    assert b.position("ag0") == pytest.approx(1.0)
    assert b.margin_used() <= 0.40 * b.snapshot().equity + 1e-6
    # 预算防线仍生效：5 手 ag 保证金 ≈ 15 万 > 预算 4 万 → 拒绝
    ev2 = b.execute_plan(_plan("ag0", 5), q, ts=datetime(2026, 8, 24, 10, 1))
    assert ev2 is None
    assert b.position("ag0") == pytest.approx(1.0)


def test_cash_constraint_never_negative():
    b = _broker(initial=100_000.0, budget_ratio=0.30)
    q = _quote("rb0", 3000.0)
    # 目标 5 手：保证金 3001×10×5×12% = 18006 ≤ 预算 30000 → 可成交
    ev = b.execute_plan(_plan("rb0", 5), q, ts=datetime(2026, 8, 24, 10, 0))
    assert ev is not None
    assert b.available_cash() >= -1e-6
    assert b.margin_used() <= 0.30 * b.snapshot().equity + 1e-6
    # 加仓到 20 手：超预算 → 拒绝，持仓保持 5 手
    ev2 = b.execute_plan(_plan("rb0", 20), q, ts=datetime(2026, 8, 24, 10, 1))
    assert ev2 is None
    assert b.position("rb0") == pytest.approx(5.0)
    assert b.available_cash() >= -1e-6


def test_plan_with_same_target_no_trade():
    b = _broker()
    b.execute_plan(_plan("rb0", 1), _quote(), ts=datetime(2026, 8, 24, 10, 0))
    ev = b.execute_plan(_plan("rb0", 1), _quote(), ts=datetime(2026, 8, 24, 10, 1))
    assert ev is None


# ---------------------------------------------------------------------------
# 平今费与 SimBroker 口径一致（A3/R4）
# ---------------------------------------------------------------------------
def test_today_close_fee_matches_simbroker():
    """同日开平 → 平今费口径：fee_close_today（双倍），与直接 SimBroker 一致。"""
    cost = _cost()
    # PaperBroker 路径
    pb = _broker()
    open_ev = pb.execute_plan(_plan("rb0", 1), _quote("rb0", 3000.0), ts=datetime(2026, 8, 24, 10, 0))
    close_ev = pb.execute_plan(_plan("rb0", 0), _quote("rb0", 3050.0), ts=datetime(2026, 8, 24, 14, 0))
    assert open_ev is not None and close_ev is not None
    assert close_ev.is_today_close is True

    # 直接 SimBroker 路径（同参）
    sb = SimBroker(cost, initial_capital=100_000.0)
    t1 = sb.execute("rb0", 1, 3000.0, timestamp=datetime(2026, 8, 24, 10, 0))
    t2 = sb.execute("rb0", 0, 3050.0, timestamp=datetime(2026, 8, 24, 14, 0))
    assert t1.fee == pytest.approx(open_ev.fee, abs=1e-9)
    assert t2.fee == pytest.approx(close_ev.fee, abs=1e-9)
    assert t2.is_today_close is True
    # 平今费显式等于 CostModel.trade_cost(is_today_close=True)
    _, fee, _, _ = cost.trade_cost(3050.0, -1, is_open=False, is_today_close=True, symbol="rb0")
    assert close_ev.fee == pytest.approx(fee, abs=1e-9)


def test_next_day_close_uses_normal_fee():
    """隔日平仓 → 非平今费（fee_close），非双倍。"""
    pb = _broker()
    pb.execute_plan(_plan("rb0", 1), _quote("rb0", 3000.0), ts=datetime(2026, 8, 24, 10, 0))
    close_ev = pb.execute_plan(_plan("rb0", 0), _quote("rb0", 3050.0), ts=datetime(2026, 8, 25, 10, 0))
    assert close_ev is not None
    assert close_ev.is_today_close is False
    _, fee, _, _ = _cost().trade_cost(3050.0, -1, is_open=False, is_today_close=False, symbol="rb0")
    assert close_ev.fee == pytest.approx(fee, abs=1e-9)


# ---------------------------------------------------------------------------
# 已实现盈亏（乘数记账口径）
# ---------------------------------------------------------------------------
def test_close_position_realized_pnl():
    b = _broker()
    b.execute_plan(_plan("rb0", 1), _quote("rb0", 3000.0), ts=datetime(2026, 8, 24, 10, 0))
    b.execute_plan(_plan("rb0", 0), _quote("rb0", 3100.0), ts=datetime(2026, 8, 25, 10, 0))
    snap = b.snapshot()
    # 买 3001（含滑点）卖 3099：盈亏 = (3099-3001)×10 - 开平手续费
    expected = (3099 - 3001) * 10 - (3001 * 10 * 0.00005 + 3099 * 10 * 0.00005)
    assert snap.realized["rb0"] == pytest.approx(expected, abs=1e-6)
    assert b.position("rb0") == 0.0


# ---------------------------------------------------------------------------
# 快照续跑
# ---------------------------------------------------------------------------
def test_snapshot_roundtrip(tmp_path):
    b = _broker()
    b.execute_plan(_plan("rb0", 2), _quote("rb0", 3000.0), ts=datetime(2026, 8, 24, 10, 0))
    b.record_trading_day(date(2026, 8, 24))
    b._peak_equity = 100_500.0  # 模拟浮盈
    path = tmp_path / "account.json"
    b.save_snapshot(path)

    b2 = _broker()
    assert b2.load_snapshot(path) is True
    assert b2.position("rb0") == pytest.approx(2.0)
    assert b2.avg_entry("rb0") == pytest.approx(b.avg_entry("rb0"))
    assert b2.trading_day_count == 1
    assert b2.snapshot().peak_equity == pytest.approx(100_500.0)


def test_load_snapshot_missing_returns_false(tmp_path):
    b = _broker()
    assert b.load_snapshot(tmp_path / "nope.json") is False


def test_load_snapshot_corrupt_backs_up_and_resets(tmp_path):
    """P2-5：快照损坏 → 告警+重置初始资金+备份损坏文件，不崩溃。"""
    b = _broker(initial=100_000.0)
    path = tmp_path / "account.json"
    path.write_text("{ not valid json !!", encoding="utf-8")
    assert b.load_snapshot(path) is False
    # 损坏文件被备份为 account.json.corrupt.<ts>
    corrupts = list(tmp_path.glob("account.json.corrupt.*"))
    assert len(corrupts) == 1
    assert not path.exists()  # 原文件已被移走
    assert "not valid json" in corrupts[0].read_text(encoding="utf-8")
    # 账户重置为初始资金，可正常使用
    snap = b.snapshot()
    assert snap.equity == pytest.approx(100_000.0)
    assert snap.positions == {}
    assert b.trading_day_count == 0


def test_load_snapshot_non_dict_json_resets(tmp_path):
    """P2-5：JSON 合法但非对象结构（如列表）同样视为损坏并重置。"""
    b = _broker(initial=100_000.0)
    path = tmp_path / "account.json"
    path.write_text("[1,2,3]", encoding="utf-8")
    assert b.load_snapshot(path) is False
    assert b.snapshot().equity == pytest.approx(100_000.0)
    assert list(tmp_path.glob("account.json.corrupt.*"))


def test_save_snapshot_atomic_no_tmp_left(tmp_path):
    """P2-6：账户快照原子写，无残留 tmp 半文件。"""
    b = _broker()
    b.execute_plan(_plan("rb0", 1), _quote(), ts=datetime(2026, 8, 24, 10, 0))
    path = tmp_path / "account.json"
    b.save_snapshot(path)
    assert path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# P0-1：平仓事件止损/止盈必须读「持仓实际档位」，禁止复用 plan 派生值
# ---------------------------------------------------------------------------
def test_close_stop_uses_position_actual_stop_not_plan():
    """P0-1 回归：ag0 平仓事件 stop 必须读 ag0 开仓时设定的实际止损。

    即便平仓 plan 故意携带 rb0 的止损（模拟历史跨品种污染场景 86 次错乱），
    close_ev.stop 也必须等于 ag0 持仓实际止损 17307.07，而非 plan 的 2917.89。
    """
    b = _broker(budget_ratio=0.45)
    open_ag = b.execute_plan(
        _plan("ag0", -1, stop=17307.07), _quote("ag0", 17500.0), ts=datetime(2026, 8, 24, 10, 0)
    )
    open_rb = b.execute_plan(
        _plan("rb0", 1, stop=2917.89), _quote("rb0", 3000.0), ts=datetime(2026, 8, 24, 10, 0)
    )
    assert open_ag is not None and open_rb is not None
    assert open_ag.stop == pytest.approx(17307.07)
    assert open_rb.stop == pytest.approx(2917.89)

    close_ag = b.execute_plan(
        _plan("ag0", 0, stop=2917.89), _quote("ag0", 17400.0), ts=datetime(2026, 8, 24, 14, 0)
    )
    assert close_ag is not None
    assert close_ag.stop == pytest.approx(17307.07)
    assert close_ag.stop != pytest.approx(2917.89)
    assert close_ag.take_profit == pytest.approx(3100.0)


def test_close_stop_with_none_plan_falls_back_to_position():
    """P0-1：平仓 plan 未带 stop（None）时，仍读持仓实际止损而非 None。"""
    b = _broker(budget_ratio=0.45)
    b.execute_plan(
        _plan("ag0", -1, stop=17307.07), _quote("ag0", 17500.0), ts=datetime(2026, 8, 24, 10, 0)
    )
    close_ag = b.execute_plan(
        _plan("ag0", 0, stop=None), _quote("ag0", 17400.0), ts=datetime(2026, 8, 24, 14, 0)
    )
    assert close_ag is not None
    assert close_ag.stop == pytest.approx(17307.07)


def test_stops_persisted_across_snapshot(tmp_path):
    """P0-1 + P0-3：持仓实际止损随快照落盘/恢复，重启不丢档位（避免重复开仓误判）。"""
    b = _broker(budget_ratio=0.45)
    b.execute_plan(
        _plan("ag0", -1, stop=17307.07), _quote("ag0", 17500.0), ts=datetime(2026, 8, 24, 10, 0)
    )
    path = tmp_path / "account.json"
    b.save_snapshot(path)

    b2 = _broker(budget_ratio=0.45)
    assert b2.load_snapshot(path) is True
    assert b2._stops.get("ag0") == pytest.approx(17307.07)
    close_ag = b2.execute_plan(
        _plan("ag0", 0, stop=99999.0), _quote("ag0", 17400.0), ts=datetime(2026, 8, 25, 10, 0)
    )
    assert close_ag is not None
    assert close_ag.stop == pytest.approx(17307.07)
