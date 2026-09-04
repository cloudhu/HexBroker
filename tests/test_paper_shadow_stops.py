"""P0-C 止损止盈影子模式回归锁（2026-09-04 取证确认：计划止损/止盈从未参与执行）。

取证结论（P0-2）：``PaperBroker`` 维护了每品种 ``_stops`` / ``_take_profits`` 档位，
但全仓**从未有任何一处拿现价与它们比较过** —— 止损止盈只被写进日志与
``account.json`` 展示，从未触发。

本文件锁定影子模式的三条契约：

1. **判定语义**：多头 ``price <= stop`` / ``price >= tp``，空头反向，
   价格**等于**阈值即触发（用 ``<=`` / ``>=`` 而非 ``<`` / ``>``）；
2. **不触发条件**：持仓为 0、阈值为 None/非正/NaN → 一律不触发；
3. ⛔ **账户零变更**：影子模式下命中只告警 + 落盘 jsonl，
   **不调用任何平仓路径** —— 持仓/现金/权益/峰值/成交数全部逐字段不变。

第 3 条是 P0-C 的命门：影子模式一旦偷偷平仓，观察数据就被污染，
「要不要转真实生效」的决策将建立在错误事实上。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from hexbroker.paper.quotes import RealTimeQuoteClient
from hexbroker.paper.shadow_stops import (
    DEFAULT_PATH,
    KIND_STOP,
    KIND_TP,
    SHADOW_FIELDS,
    ShadowStopMonitor,
    evaluate_trigger,
)
from hexbroker.paper.types import Plan, Quote

from test_paper_pipeline import MON, _eff_signal, _scheduler

NOW = datetime(2026, 8, 24, 10, 0)


def _monitor(tmp_path, **kw) -> ShadowStopMonitor:
    return ShadowStopMonitor(path=tmp_path / "shadow_stops.jsonl", **kw)


def _quote(price: float, symbol: str = "rb0") -> Quote:
    return Quote(symbol=symbol, ts=NOW, price=price, open=price, high=price, low=price)


def _open_long(
    sched,
    qty: float = 1.0,
    price: float = 3000.0,
    stop: float = 2900.0,
    tp: float = 3200.0,
    symbol: str = "rb0",
) -> None:
    """直接建仓（绕开信号/风控，专注止损止盈判定）。"""
    plan = Plan(
        symbol=symbol,
        direction=1 if qty > 0 else -1,
        target_qty=qty,
        target_pos_pct=0.1,
        stop_price=stop,
        take_profit=tp,
    )
    sched._broker.execute_plan(plan, _quote(price, symbol), NOW)


def _account_state(sched) -> dict:
    """账户状态指纹：影子模式前后必须逐字段相等。"""
    snap = sched._broker.snapshot()
    return {
        "positions": dict(snap.positions),
        "avg_entry": dict(snap.avg_entry),
        "realized": dict(snap.realized),
        "equity": snap.equity,
        "cash": snap.cash,
        "margin_used": snap.margin_used,
        "peak_equity": snap.peak_equity,
        "drawdown": snap.drawdown,
        "trades": len(sched._broker.trades()),
        "stops": dict(sched._broker._stops),
        "take_profits": dict(sched._broker._take_profits),
        "trade_seq": sched._broker._trade_seq,
    }


# --------------------------------------------------------------------------- #
# 1. 判定语义（纯函数，无 IO）
# --------------------------------------------------------------------------- #
def test_long_stop_fires_when_price_at_or_below_threshold():
    """多头：price == stop 必须触发（等于即触发，不是严格小于）。"""
    assert evaluate_trigger(1.0, 2900.0, 2900.0, None) == [KIND_STOP], "等于阈值应触发"
    assert evaluate_trigger(1.0, 2899.9, 2900.0, None) == [KIND_STOP]
    assert evaluate_trigger(1.0, 2900.1, 2900.0, None) == [], "高于止损不得触发"


def test_long_tp_fires_when_price_at_or_above_threshold():
    """多头：price == tp 必须触发。"""
    assert evaluate_trigger(1.0, 3200.0, None, 3200.0) == [KIND_TP], "等于阈值应触发"
    assert evaluate_trigger(1.0, 3200.1, None, 3200.0) == [KIND_TP]
    assert evaluate_trigger(1.0, 3199.9, None, 3200.0) == [], "低于止盈不得触发"


def test_short_direction_is_reversed():
    """空头：止损在上方（price >= stop），止盈在下方（price <= tp）。"""
    assert evaluate_trigger(-1.0, 3100.0, 3100.0, None) == [KIND_STOP]
    assert evaluate_trigger(-1.0, 3099.9, 3100.0, None) == []
    assert evaluate_trigger(-1.0, 2800.0, None, 2800.0) == [KIND_TP]
    assert evaluate_trigger(-1.0, 2800.1, None, 2800.0) == []


def test_flat_position_never_triggers():
    """持仓为 0 → 无论如何都不触发（含荒谬阈值）。"""
    for pos in (0.0, 1e-13, -1e-13):
        assert evaluate_trigger(pos, 1.0, 1.0, 1.0) == [], f"pos={pos} 不应触发"


def test_missing_or_invalid_threshold_never_triggers():
    """阈值 None / 0 / 负数 / NaN → 该侧跳过，不得用垃圾值误触发。"""
    for bad in (None, 0.0, -1.0, float("nan")):
        assert evaluate_trigger(1.0, 3000.0, bad, None) == []
        assert evaluate_trigger(1.0, 3000.0, None, bad) == []


def test_both_stop_and_tp_recorded_on_gap_through():
    """跳空同时穿越两个阈值 → 两条都留痕，不静默丢弃任一条。"""
    hits = evaluate_trigger(1.0, 3000.0, 3100.0, 2900.0)
    assert set(hits) == {KIND_STOP, KIND_TP}


def test_nonpositive_or_nan_price_never_triggers():
    """价格非正 / NaN → 不触发（报价本身已不可信）。"""
    assert evaluate_trigger(1.0, 0.0, 2900.0, 3200.0) == []
    assert evaluate_trigger(1.0, float("nan"), 2900.0, 3200.0) == []


# --------------------------------------------------------------------------- #
# 2. 落盘格式
# --------------------------------------------------------------------------- #
def test_jsonl_record_has_exactly_the_agreed_fields(tmp_path):
    """落盘字段必须与约定完全一致，不多不少。"""
    mon = _monitor(tmp_path)
    records = mon.scan(
        symbol="rb0",
        position=1.0,
        price=2890.0,
        stop=2900.0,
        take_profit=3200.0,
        avg_entry=3000.0,
        quote_ts=NOW,
        now=NOW,
    )
    assert len(records) == 1

    lines = (tmp_path / "shadow_stops.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert tuple(payload.keys()) == SHADOW_FIELDS, "字段集合与顺序必须与约定一致"
    assert payload["kind"] == KIND_STOP
    assert payload["symbol"] == "rb0"
    assert payload["position"] == pytest.approx(1.0)
    assert payload["threshold"] == pytest.approx(2900.0)
    assert payload["trigger_price"] == pytest.approx(2890.0)
    assert payload["avg_entry"] == pytest.approx(3000.0)
    assert payload["quote_ts"] == NOW.isoformat(timespec="seconds")


def test_unrealized_pnl_uses_multiplier(tmp_path):
    """浮动盈亏按「(价差 × 持仓 × 乘数)」计金额，乘数缺失时退回 1.0。"""
    plain = _monitor(tmp_path)
    rec = plain.scan(
        symbol="rb0", position=1.0, price=2890.0, stop=2900.0,
        take_profit=None, avg_entry=3000.0, quote_ts=NOW, now=NOW,
    )[0]
    assert rec["unrealized_pnl"] == pytest.approx(-110.0), "乘数缺省 1.0 → 名义价差"

    scaled = ShadowStopMonitor(path=tmp_path / "b.jsonl", multiplier_fn=lambda s: 10.0)
    rec2 = scaled.scan(
        symbol="rb0", position=1.0, price=2890.0, stop=2900.0,
        take_profit=None, avg_entry=3000.0, quote_ts=NOW, now=NOW,
    )[0]
    assert rec2["unrealized_pnl"] == pytest.approx(-1100.0), "rb0 乘数 10 → 金额口径"


def test_no_trigger_writes_nothing(tmp_path):
    """未命中不得产生空行/空文件。"""
    mon = _monitor(tmp_path)
    assert mon.scan(
        symbol="rb0", position=1.0, price=3000.0, stop=2900.0,
        take_profit=3200.0, avg_entry=3000.0, quote_ts=NOW, now=NOW,
    ) == []
    assert not (tmp_path / "shadow_stops.jsonl").exists()


def test_write_failure_never_propagates(tmp_path, monkeypatch):
    """落盘失败只记日志、绝不向上抛（R22：不新增停摆模式）。"""
    import hexbroker.paper.shadow_stops as mod

    monkeypatch.setattr(mod, "json", None)  # 强制 json.dumps 抛 AttributeError
    mon = _monitor(tmp_path)
    records = mon.scan(
        symbol="rb0", position=1.0, price=2890.0, stop=2900.0,
        take_profit=None, avg_entry=3000.0, quote_ts=NOW, now=NOW,
    )
    assert len(records) == 1, "落盘失败不得影响判定结果本身"


def test_default_path_points_to_data_paper():
    assert str(DEFAULT_PATH).replace("\\", "/") == "data/paper/shadow_stops.jsonl"


# --------------------------------------------------------------------------- #
# 3. ⛔ 账户零变更（P0-C 命门）
# --------------------------------------------------------------------------- #
def test_shadow_mode_leaves_account_completely_untouched(tmp_path):
    """最关键断言：影子模式命中后，账户任何字段都不得变化。

    命中场景是「多头 1 手 @3000，止损 2900，现价 2890」—— 真实止损会立刻
    平仓并兑现 -110 元亏损。影子模式下必须纹丝不动。
    """
    sched, _ = _scheduler(tmp_path, sig=_eff_signal())
    _open_long(sched, qty=1.0, price=3000.0, stop=2900.0, tp=3200.0)
    assert sched._broker.position("rb0") == pytest.approx(1.0), "前置：建仓成功"

    mon = ShadowStopMonitor(
        path=tmp_path / "shadow.jsonl", enabled=True, multiplier_fn=lambda s: 10.0
    )
    sched._shadow_stops = mon

    before = _account_state(sched)
    sched._shadow_stop_sweep({"rb0": _quote(2890.0)}, NOW, MON)
    after = _account_state(sched)

    assert after == before, "影子模式命中后账户状态必须逐字段不变"
    # 显式复述关键项，避免 dict 比较掩盖语义
    assert after["trades"] == before["trades"] == 1, "不得产生新成交"
    assert after["positions"]["rb0"] == pytest.approx(1.0), "持仓不得被平掉"
    assert after["stops"]["rb0"] == pytest.approx(2900.0), "止损档位不得被清除"
    # 但记录必须落盘 —— 影子模式是要留下证据的
    lines = (tmp_path / "shadow.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["kind"] == KIND_STOP


def test_shadow_mode_records_take_profit_without_closing(tmp_path):
    """止盈命中同样不平仓。"""
    sched, _ = _scheduler(tmp_path, sig=_eff_signal())
    _open_long(sched, qty=1.0, price=3000.0, stop=2900.0, tp=3200.0)
    sched._shadow_stops = ShadowStopMonitor(
        path=tmp_path / "s.jsonl", enabled=True, multiplier_fn=lambda s: 10.0
    )

    before = _account_state(sched)
    sched._shadow_stop_sweep({"rb0": _quote(3210.0)}, NOW, MON)

    assert _account_state(sched) == before
    rec = json.loads((tmp_path / "s.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["kind"] == KIND_TP
    # 开仓均价含滑点（3000 → 3001），故按实际 avg_entry 反算，不硬编码 3000
    entry = sched._broker.avg_entry("rb0")
    assert entry == pytest.approx(3001.0), "前置：建仓均价含 1 跳滑点"
    assert rec["unrealized_pnl"] == pytest.approx((3210.0 - entry) * 1.0 * 10.0)


def test_shadow_mode_skips_stale_quotes(tmp_path):
    """陈旧报价不参与判定 —— 冻结价会产生幻影触发，污染观察数据集。"""
    sched, _ = _scheduler(tmp_path, sig=_eff_signal())
    _open_long(sched, qty=1.0, price=3000.0, stop=2900.0, tp=3200.0)
    sched._shadow_stops = ShadowStopMonitor(path=tmp_path / "s.jsonl", enabled=True)

    stale = Quote(symbol="rb0", ts=NOW, price=1000.0, stale=True)  # 远低于止损
    sched._shadow_stop_sweep({"rb0": stale}, NOW, MON)
    assert not (tmp_path / "s.jsonl").exists(), "陈旧报价不得判定"
    assert sched._broker.position("rb0") == pytest.approx(1.0)


def test_shadow_mode_skips_flat_positions(tmp_path):
    """空仓品种不参与扫描（_stops 残留旧档位也不得触发）。"""
    sched, _ = _scheduler(tmp_path, sig=_eff_signal())
    sched._broker._stops["rb0"] = 2900.0  # 空仓但残留旧止损
    sched._shadow_stops = ShadowStopMonitor(path=tmp_path / "s.jsonl", enabled=True)
    sched._shadow_stop_sweep({"rb0": _quote(1000.0)}, NOW, MON)
    assert not (tmp_path / "s.jsonl").exists()


def test_monitor_none_disables_sweep_entirely(tmp_path):
    """shadow_stops=None → 完全不启用（零行为变更，向后兼容）。"""
    sched, _ = _scheduler(tmp_path, sig=_eff_signal())
    _open_long(sched, qty=1.0, price=3000.0, stop=2900.0, tp=3200.0)
    sched._shadow_stops = None
    before = _account_state(sched)
    sched._shadow_stop_sweep({"rb0": _quote(2890.0)}, NOW, MON)
    assert _account_state(sched) == before


def test_sweep_exception_is_isolated(tmp_path):
    """扫描内部异常不得冒泡打断主 tick（R22）。"""
    sched, _ = _scheduler(tmp_path, sig=_eff_signal())
    _open_long(sched, qty=1.0, price=3000.0, stop=2900.0, tp=3200.0)
    sched._shadow_stops = object()  # 没有 .enabled / .scan → 必抛
    sched._shadow_stop_sweep({"rb0": _quote(2890.0)}, NOW, MON)  # 不得 raise
    assert sched._broker.position("rb0") == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# 4. 真实平仓路径（仅 risk.shadow_stops: false 时启用）
# --------------------------------------------------------------------------- #
def test_real_close_path_actually_flattens_when_disabled(tmp_path):
    """enabled=False → 转真实平仓（团队裁决：只改配置不改代码）。

    默认配置 true 时该路径永不执行，但必须实现且可测，否则「置 false」是空话。
    """
    sched, _ = _scheduler(tmp_path, sig=_eff_signal())
    _open_long(sched, qty=1.0, price=3000.0, stop=2900.0, tp=3200.0)
    sched._shadow_stops = ShadowStopMonitor(
        path=tmp_path / "s.jsonl", enabled=False, multiplier_fn=lambda s: 10.0
    )

    sched._shadow_stop_sweep({"rb0": _quote(2890.0)}, NOW, MON)

    assert sched._broker.position("rb0") == pytest.approx(0.0), "非影子模式必须平至 0"
    assert len(sched._broker.trades()) == 2, "应多出一笔平仓成交"
    # 影子记录照写（保留「为什么平」的证据）
    assert (tmp_path / "s.jsonl").exists()


def test_shadow_vs_real_is_purely_config_driven():
    """配置键 risk.shadow_stops 默认 true —— 防止误开真实平仓。"""
    from omegaconf import OmegaConf

    cfg = OmegaConf.load("configs/paper.yaml")
    paper = cfg.get("paper", cfg)
    enabled = dict(paper.get("risk", {}) or {}).get(
        "shadow_stops", paper.get("risk_shadow_stops", True)
    )
    assert bool(enabled) is True, "默认必须是影子模式，转真实平仓需显式置 false"


# --------------------------------------------------------------------------- #
# 5. 与实时报价管线对接（字段取自真实报文）
# --------------------------------------------------------------------------- #
def test_scan_uses_quotes_layer_parse_output():
    """端到端：用真实报文解析结果驱动一次判定（验证 price 取的是 field[8]）。"""
    line = (
        'var hq_str_nf_RB0="螺纹钢连续,150000,3145.000,3180.000,3137.000,'
        '3166.000,3165.000,3166.000,3166.000,3160.000,3137.000,3,524,'
        '1497791.000,877156,沪,螺纹钢,2026-09-04";'
    )
    client = RealTimeQuoteClient(offline=True)
    q = client._parse_line(line)
    assert q is not None and q.price == pytest.approx(3166.0)

    # 持仓 1 手 @3200，止损 3170 → 最新价 3166 已跌破，应触发
    hits = evaluate_trigger(1.0, q.price, 3170.0, 3300.0)
    assert hits == [KIND_STOP], "必须用 field[8] 最新价判定，而非 field[2] 开盘价 3145"
