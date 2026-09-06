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
import datetime as _dt
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


# --------------------------------------------------------------------------- #
# 6. 去重窗口（任务A：转真实前必须补 —— 避免每个 tick 写一条刷爆 jsonl）
# --------------------------------------------------------------------------- #
def _count_lines(path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def test_dedup_within_window_writes_only_one_row(tmp_path):
    """窗口内同一 (symbol, kind) 二次命中 → 不写第二条，jsonl 行数不变。"""
    mon = _monitor(tmp_path)  # 默认窗口 300s
    t0 = NOW
    # 第一次命中 → 写 1 行
    r1 = mon.scan(
        symbol="rb0", position=1.0, price=2890.0, stop=2900.0,
        take_profit=None, avg_entry=3000.0, quote_ts=t0, now=t0,
    )
    assert len(r1) == 1
    assert _count_lines(tmp_path / "shadow_stops.jsonl") == 1
    # 窗口内（+60s）价格继续在止损下方 → 不得写第二条
    r2 = mon.scan(
        symbol="rb0", position=1.0, price=2880.0, stop=2900.0,
        take_profit=None, avg_entry=3000.0, quote_ts=t0 + _dt.timedelta(seconds=60),
        now=t0 + _dt.timedelta(seconds=60),
    )
    assert r2 == [], "窗口内去重：不得返回新记录"
    assert _count_lines(tmp_path / "shadow_stops.jsonl") == 1, "窗口内不得写第二条"


def test_dedup_window_expiry_allows_second_emit(tmp_path):
    """窗口外（>300s）再次穿越阈值 → 重新 emit，写第二条（真·二次穿越要能触发）。"""
    mon = _monitor(tmp_path)
    t0 = NOW
    mon.scan(
        symbol="rb0", position=1.0, price=2890.0, stop=2900.0,
        take_profit=None, avg_entry=3000.0, quote_ts=t0, now=t0,
    )
    assert _count_lines(tmp_path / "shadow_stops.jsonl") == 1
    # 越过窗口（+301s），价格仍低于止损 → 应写第二条
    r2 = mon.scan(
        symbol="rb0", position=1.0, price=2850.0, stop=2900.0,
        take_profit=None, avg_entry=3000.0,
        quote_ts=t0 + _dt.timedelta(seconds=301),
        now=t0 + _dt.timedelta(seconds=301),
    )
    assert len(r2) == 1, "窗口外再次穿越必须重新 emit"
    assert _count_lines(tmp_path / "shadow_stops.jsonl") == 2


def test_dedup_is_per_symbol_and_kind(tmp_path):
    """去重键是 (symbol, kind)：不同 symbol / 不同 kind 互不影响。"""
    mon = _monitor(tmp_path)
    t0 = NOW
    # rb0 STOP
    mon.scan(symbol="rb0", position=1.0, price=2890.0, stop=2900.0,
             take_profit=None, avg_entry=3000.0, quote_ts=t0, now=t0)
    # 同 tick 不同 symbol 的 STOP → 不得被 rb0 去重挡住
    mon.scan(symbol="cu0", position=1.0, price=2890.0, stop=2900.0,
             take_profit=None, avg_entry=3000.0, quote_ts=t0, now=t0)
    # 同 symbol 但 kind=TP（stop 放到下方使本次只触发 TP）→ 不得被 (rb0,STOP) 挡住
    mon.scan(symbol="rb0", position=1.0, price=2890.0, stop=2800.0,
             take_profit=2850.0, avg_entry=3000.0, quote_ts=t0, now=t0)
    assert _count_lines(tmp_path / "shadow_stops.jsonl") == 3, "不同 symbol/kind 不得互相去重"


def test_dedup_disabled_when_window_nonpositive(tmp_path):
    """去重窗口 ≤ 0 → 关闭去重，始终 emit（每次命中都写）。"""
    mon = ShadowStopMonitor(
        path=tmp_path / "s.jsonl", enabled=True,
        multiplier_fn=lambda s: 1.0, dedup_window_sec=0.0,
    )
    t0 = NOW
    for i in range(3):
        r = mon.scan(symbol="rb0", position=1.0, price=2890.0, stop=2900.0,
                     take_profit=None, avg_entry=3000.0, quote_ts=t0,
                     now=t0 + _dt.timedelta(seconds=i))
        assert len(r) == 1
    assert _count_lines(tmp_path / "s.jsonl") == 3, "关闭去重应每次命中都写"


# --------------------------------------------------------------------------- #
# 7. P2-3 事件语义（2026-09-06）：event_id / first_hit
# --------------------------------------------------------------------------- #
# 背景：300s 去重只控制写盘节奏，一个持续 6h 的触发段仍会产生 ~72 行。
# 09-21「是否转真实平仓」的唯一决策依据就是这份 jsonl —— 若没有事件语义，
# 直接 count 行数回答「止损触发了几次」会高估一到两个数量级。
# 事件边界 = tick 级触发状态连续性：价格离开触发区哪怕一个 tick，旧事件结束。

def _scan_stop(mon, symbol="rb0", now=NOW, stop=2900.0, price=2890.0):
    return mon.scan(
        symbol=symbol, position=1.0, price=price, stop=stop,
        take_profit=None, avg_entry=3000.0, quote_ts=now, now=now,
    )


def _read_rows(tmp_path, name="shadow_stops.jsonl") -> list[dict]:
    path = tmp_path / name
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_continuous_trigger_yields_one_event_and_first_hit_only_once(tmp_path):
    """持续触发跨 3 个去重窗口 → 1 个 event_id，仅首条 first_hit=True。

    这是本次修复的主场景：修复前这 3 行会被当成 3 次「触发」，
    count 高估 3 倍（真实行情里一个段是 70+ 行 → 高估一到两个数量级）。
    """
    mon = _monitor(tmp_path)
    t0 = NOW
    for offset in (0, 301, 602):  # 每次都越过 300s 去重窗 → 都落盘
        rows = _scan_stop(mon, now=t0 + _dt.timedelta(seconds=offset))
        assert len(rows) == 1, f"offset={offset} 应落盘一条（窗口外重新 emit）"

    rows = _read_rows(tmp_path)
    assert len(rows) == 3
    ids = {r["event_id"] for r in rows}
    assert len(ids) == 1, f"同一连续触发段必须共用一个 event_id，实际 {ids}"
    assert sum(1 for r in rows if r["first_hit"]) == 1, "只有首条标 first_hit=True"
    assert rows[0]["first_hit"] is True
    assert rows[1]["first_hit"] is False and rows[2]["first_hit"] is False


def test_new_event_after_price_leaves_trigger_zone(tmp_path):
    """价格离开触发区（哪怕只隔一个 tick）→ 旧事件结束，再触发是新事件。

    时间线：t0 触发（事件 A 落盘）→ t0+301 回到止损上方（不触发，
    事件 A 结束）→ t0+602 再次跌破（新事件 B；距上次 emit 602s ≥ 300s
    窗口 → 放行落盘）。
    """
    mon = _monitor(tmp_path)
    t0 = NOW
    _scan_stop(mon, now=t0)                                              # A 落盘
    _scan_stop(mon, now=t0 + _dt.timedelta(seconds=301), price=3000.0)   # 离开触发区
    rows_b = _scan_stop(mon, now=t0 + _dt.timedelta(seconds=602))        # 再次跌破 → B
    assert len(rows_b) == 1, "602s ≥ 300s 窗口，必须重新 emit 落盘"

    rows = _read_rows(tmp_path)
    assert len(rows) == 2

    # ⛔ 关键断言：第二条必须属于**新事件**，不得并进第一段
    assert rows[1]["event_id"] != rows[0]["event_id"], (
        "价格离开过触发区（中间 tick 未触发），旧事件必须结束；"
        "并进同一段会把两次真实触发算成一次，低估事件数"
    )
    assert rows[1]["first_hit"] is True, "新事件的首条落盘记录必须标 first_hit=True"


def test_new_event_first_tick_deduped_still_flags_first_disk_row(tmp_path):
    """新事件首 tick 撞去重窗被拦 → 该段首条**落盘**记录仍必须标 first_hit=True。

    场景：t=0 触发（事件 A 落盘）→ t=60 短暂离开 → t=120 回到触发区
    （新事件 B！但距上次 emit 仅 120s < 300s → 去重拦截，不落盘）
    → t=440 仍触发（事件 B 延续，越过窗口 → 落盘）。
    分析侧按 event_id 分组时，B 段首条落盘记录必须可识别。
    """
    mon = _monitor(tmp_path)
    t0 = NOW
    _scan_stop(mon, now=t0)                                            # A 落盘 first_hit=True
    _scan_stop(mon, now=t0 + _dt.timedelta(seconds=60), price=3000.0)  # 离开
    r3 = _scan_stop(mon, now=t0 + _dt.timedelta(seconds=120))          # B 首tick，被 300s 窗拦截
    assert r3 == [], "距上次 emit 120s < 300s，必须被去重拦截（既有语义不动）"
    rows_b = _scan_stop(mon, now=t0 + _dt.timedelta(seconds=440))      # B 段越过窗口
    assert len(rows_b) == 1

    rows = _read_rows(tmp_path)
    assert len(rows) == 2
    assert rows[1]["event_id"] != rows[0]["event_id"], "t=120 起是价格离开后重新进入 → 新事件"
    assert rows[1]["first_hit"] is True, (
        "B 段首条落盘记录必须标 True——pending_first 只在真正落盘时消费"
    )


def test_simultaneous_stop_and_tp_get_independent_event_ids(tmp_path):
    """跳空同时命中止损+止盈 → 两条记录、两个独立 event_id（kind 不同）。"""
    mon = _monitor(tmp_path)
    t0 = NOW
    rows = mon.scan(
        symbol="rb0", position=1.0, price=3000.0, stop=3100.0,
        take_profit=2900.0, avg_entry=3000.0, quote_ts=t0, now=t0,
    )
    assert len(rows) == 2, "STOP 与 TP 必须都留痕（既有语义）"
    ids = {r["kind"]: r["event_id"] for r in rows}
    assert ids[KIND_STOP] != ids[KIND_TP], "不同 kind 的事件段必须独立编号"


def test_event_ids_isolated_across_symbols(tmp_path):
    """同 tick、同 kind、不同 symbol → 各自独立事件段，不得串扰。"""
    mon = _monitor(tmp_path)
    t0 = NOW
    r1 = _scan_stop(mon, symbol="rb0", now=t0)
    r2 = _scan_stop(mon, symbol="cu0", now=t0)
    assert len(r1) == 1 and len(r2) == 1
    assert r1[0]["event_id"] != r2[0]["event_id"], "event_id 必须含 symbol，防跨品种串扰"


def test_event_state_corruption_is_fail_open(tmp_path):
    """事件状态被人为破坏 → 不抛异常、退化为按新事件处理（R22）。"""
    mon = _monitor(tmp_path)
    t0 = NOW
    mon._event_state[("rb0", KIND_STOP)] = "garbage"   # 非 dict
    rows = _scan_stop(mon, now=t0)
    assert len(rows) == 1, "状态损坏不得中断判定/落盘"
    assert rows[0]["first_hit"] is True, "fail-open：退化为新事件"

    mon._in_trigger[("rb0", KIND_STOP)] = "not-a-bool"  # 再砸一处
    rows2 = _scan_stop(mon, now=t0 + _dt.timedelta(seconds=301))
    assert len(rows2) == 1, "状态损坏不得中断 tick（第二次）"


def test_reset_dedup_also_clears_event_state(tmp_path):
    """reset_dedup 必须同步清事件状态，否则复位后首段触发被误判成旧段延续。"""
    mon = _monitor(tmp_path)
    t0 = NOW
    _scan_stop(mon, now=t0)
    mon.reset_dedup()
    rows = _scan_stop(mon, now=t0 + _dt.timedelta(seconds=10))  # 窗口内（10s<300s）
    # 去重哨兵也被清了 → 放行落盘
    assert len(rows) == 1
    assert rows[0]["first_hit"] is True, "复位后第一段必须是全新事件"


def test_shadow_fields_order_carries_event_semantics():
    """SHADOW_FIELDS 常量必须携带新字段且位置稳定（落盘格式契约）。"""
    assert SHADOW_FIELDS[:3] == ("ts", "event_id", "first_hit"), (
        "event_id/first_hit 必须紧跟 ts，位置属于落盘契约的一部分"
    )
