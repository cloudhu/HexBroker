"""任务B 回归锁：PaperBroker 跨日滚动极值（名实不符语义债修复，默认关闭）。

背景
----
``PositionCtx.highest_since_entry / lowest_since_entry`` 原实现取**当日**
``quote.high / quote.low``（新浪 field[3]/field[4]，盘中高/低，每日重置），
不是「持仓以来」的滚动极值 —— 跨日持仓时昨日极值丢失，变量名却叫
``*_since_entry`` → **名实不符**。

修复（2026-09-04，任务B）：新增配置位 ``paper.risk.cross_day_rolling_extremes``
（**默认 false** = 维持现状当日口径，行为零变化；true = 启用持仓以来跨日滚动极值，
状态随 ``account.json`` 的 additive 键 ``ext_hi/ext_lo`` 持久化）。

⚠️ 关键实证（决定本任务的「触发点漂移」评估）：
risk 引擎 S1–S5 / trailing / ratchet / 硬止损当前**均不读取** ``highest_since_entry``
``lowest_since_entry``（hexbroker/risk/ 下无任何消费者；仅 broker→risk_gate 拷贝 + 观察用）。
故开启本开关在当下**不改变任何风控触发点** —— 本任务是"让观察/审计字段变得准确"，
而非"改变判定"。留开关是为将来某规则真消费该字段时能闸住语义，默认保持今日行为。

锁定契约：
1. 默认 off → 跨日极值**不维护**，``position_ctx`` 返回当日口径（= 修复前行为）；
2. on → 建仓 seed=entry，当日 high/low 抬升/下压，**跨日保留昨日极值**；
3. on → 重启（save→load）后跨日极值**存活**；
4. on → 平仓至 0 → 极值清除；重新开仓 seed 重置；
5. on → 单调性：hi 只升不降、lo 只降不升；
6. 配置键默认 false。
"""
from __future__ import annotations

from datetime import datetime

from hexbroker.backtest.cost import CostModel
from hexbroker.paper.broker import PaperBroker
from hexbroker.paper.types import Plan, Quote

INITIAL = 1_000_000.0  # 大额初始资金，确保 execute_plan 预算/现金校验通过


def _pb(cross_day: bool = False) -> PaperBroker:
    return PaperBroker(
        CostModel(fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
                  slippage_ticks=1.0, margin_rate=0.12, multiplier=10.0, min_tick=1.0),
        initial_capital=INITIAL,
        data_dir="data/paper_test_cross_day_extremes",
        cross_day_rolling_extremes=cross_day,
    )


def _quote(price: float, high: float, low: float, ts=None) -> Quote:
    return Quote(
        symbol="rb0",
        ts=ts or datetime(2026, 9, 1, 10, 0),
        price=price,
        open=price,
        high=high,
        low=low,
        pre_settle=price,
    )


def _open_long(pb: PaperBroker, price: float = 3000.0) -> None:
    """经 execute_plan 真实建仓（触发 seed 初始化 / _ext_* 写入）。"""
    plan = Plan(symbol="rb0", direction=1, target_qty=1.0, target_pos_pct=0.1,
                stop_price=2900.0, take_profit=3200.0)
    pb.execute_plan(plan, _quote(price, price, price), datetime(2026, 9, 1, 9, 5))


def _close_long(pb: PaperBroker) -> None:
    plan = Plan(symbol="rb0", direction=-1, target_qty=0.0, target_pos_pct=0.0)
    pb.execute_plan(plan, _quote(3100.0, 3100.0, 3100.0), datetime(2026, 9, 3, 10, 0))


# --------------------------------------------------------------------------- #
# 1. 默认 off → 不维护跨日极值（行为 = 修复前）
# --------------------------------------------------------------------------- #
def test_off_mode_does_not_maintain_rolling_extremes():
    """默认关闭：_ext_* 不写入，position_ctx 返回当日口径（昨日极值不保留）。"""
    pb = _pb(cross_day=False)
    _open_long(pb)
    assert pb._cross_day_extremes is False
    assert pb._ext_hi == {} and pb._ext_lo == {}, "off 模式不得维护滚动极值状态"

    # 当日 high=3200 → hi=3200
    ctx1 = pb.position_ctx("rb0", _quote(3000.0, 3200.0, 2990.0))
    assert ctx1.highest_since_entry == 3200.0
    assert pb._ext_hi == {}, "off 模式即使观测到新高也不写入滚动状态"

    # 模拟"新一日"（high 只到 3100，未破昨日 3200）→ 当日口径会回落
    ctx2 = pb.position_ctx("rb0", _quote(3000.0, 3100.0, 2980.0))
    assert ctx2.highest_since_entry == 3100.0, "off=当日口径，昨日 3200 不保留"


# --------------------------------------------------------------------------- #
# 2. on → 跨日保留昨日极值
# --------------------------------------------------------------------------- #
def test_on_mode_carries_yesterday_extreme_across_days():
    """on 模式：昨日 high=3200 在次日 high 只到 3100 时仍保留（最高点=3200）。"""
    pb = _pb(cross_day=True)
    _open_long(pb)  # seed ext_hi = ext_lo = avg_entry(~3000)
    assert pb._ext_hi["rb0"] > 0 and pb._ext_lo["rb0"] > 0

    # 当日 high=3200, low=2990
    ctx1 = pb.position_ctx("rb0", _quote(3000.0, 3200.0, 2990.0))
    assert ctx1.highest_since_entry == 3200.0
    assert ctx1.lowest_since_entry == 2990.0

    # 新一日 high 只到 3100（<3200），low 到 2980（<2990）
    ctx2 = pb.position_ctx("rb0", _quote(3000.0, 3100.0, 2980.0))
    assert ctx2.highest_since_entry == 3200.0, "跨日最高点必须保留昨日 3200"
    assert ctx2.lowest_since_entry == 2980.0, "今日更低点 2980 应继续下压"


def test_on_mode_hi_floor_is_entry_and_lo_never_above_entry():
    """on 模式 seed=entry：极值不会低于 entry（多头 hi≥entry）逻辑由 seed 保证。"""
    pb = _pb(cross_day=True)
    _open_long(pb, price=3000.0)
    entry = pb._broker.avg_entry["rb0"]  # 含 1 跳滑点 ≈ 3001
    assert pb._ext_hi["rb0"] == entry and pb._ext_lo["rb0"] == entry

    # 报价高/低都高于 entry 一个方向（多头上涨）→ hi 抬、lo 保持 entry
    ctx = pb.position_ctx("rb0", _quote(3050.0, 3100.0, 3040.0))
    assert ctx.highest_since_entry == 3100.0
    assert ctx.lowest_since_entry == entry, "low 从未低于 entry → lo 停在上界=entry"


# --------------------------------------------------------------------------- #
# 3. on → 重启存活（save → load）
# --------------------------------------------------------------------------- #
def test_on_mode_extremes_survive_restart(tmp_path):
    """on 模式：save_snapshot → 新 broker load_snapshot → 跨日极值存活。"""
    pb1 = PaperBroker(
        CostModel(fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
                  slippage_ticks=1.0, margin_rate=0.12, multiplier=10.0, min_tick=1.0),
        initial_capital=INITIAL, data_dir=str(tmp_path), cross_day_rolling_extremes=True,
    )
    _open_long(pb1)
    pb1.position_ctx("rb0", _quote(3000.0, 3200.0, 2990.0))  # 抬到 hi=3200
    snap_path = pb1.save_snapshot(tmp_path / "account.json")

    # 新进程（重启）
    pb2 = PaperBroker(
        CostModel(fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
                  slippage_ticks=1.0, margin_rate=0.12, multiplier=10.0, min_tick=1.0),
        initial_capital=INITIAL, data_dir=str(tmp_path), cross_day_rolling_extremes=True,
    )
    assert pb2.load_snapshot(snap_path) is True
    assert pb2._ext_hi["rb0"] == 3200.0, "重启后昨日极值必须从快照恢复"

    # 新一日 high 只到 3100 → 仍返回 3200（跨日 + 重启双重保留）
    ctx = pb2.position_ctx("rb0", _quote(3000.0, 3100.0, 2995.0))
    assert ctx.highest_since_entry == 3200.0


def test_off_mode_snapshot_roundtrip_keeps_no_extremes(tmp_path):
    """off 模式快照不含极值键污染；on 模式 load 缺键 → 冷启动到 entry（兼容旧快照）。"""
    pb = _pb(cross_day=False)
    _open_long(pb)
    snap_path = pb.save_snapshot(tmp_path / "a.json")
    payload_has_ext = "ext_hi" in __import__("json").loads(
        (tmp_path / "a.json").read_text(encoding="utf-8")
    )
    assert payload_has_ext is False or pb._ext_hi == {}, "off 模式 ext_hi 应为空"
    assert snap_path.exists()


# --------------------------------------------------------------------------- #
# 4. on → 平仓清除、重新开仓 seed 重置
# --------------------------------------------------------------------------- #
def test_on_mode_flat_clears_extremes_and_reopen_reseeds():
    """平仓至 0 → 极值清除；重新开仓 seed 重置（不留旧仓数据）。"""
    pb = _pb(cross_day=True)
    _open_long(pb)
    pb.position_ctx("rb0", _quote(3000.0, 3200.0, 2990.0))
    assert pb._ext_hi["rb0"] == 3200.0

    _close_long(pb)
    assert pb._ext_hi == {} and pb._ext_lo == {}, "平仓必须清空滚动极值"

    # 重新开仓 → seed 重置为新 entry，而非沿用旧 3200
    _open_long(pb, price=3300.0)
    entry2 = pb._broker.avg_entry["rb0"]
    assert pb._ext_hi["rb0"] == entry2, "重开仓 seed 应等于新 entry"
    assert pb._ext_lo["rb0"] == entry2


# --------------------------------------------------------------------------- #
# 5. on → 单调性
# --------------------------------------------------------------------------- #
def test_on_mode_hi_monotonic_lo_monotonic():
    """hi 只升不降、lo 只降不升 —— 即便报价高低往复跳动。"""
    pb = _pb(cross_day=True)
    _open_long(pb)
    hi_seen, lo_seen = pb._ext_hi["rb0"], pb._ext_lo["rb0"]
    # 人为制造高低往复：先抬再压再抬
    for (hi, lo) in [(3200.0, 3001.0), (3150.0, 2980.0), (3300.0, 3000.0), (3250.0, 2950.0)]:
        ctx = pb.position_ctx("rb0", _quote(3000.0, hi, lo))
        hi_seen = max(hi_seen, ctx.highest_since_entry)
        lo_seen = min(lo_seen, ctx.lowest_since_entry)
        assert pb._ext_hi["rb0"] == hi_seen, "hi 不得回落"
        assert pb._ext_lo["rb0"] == lo_seen, "lo 不得回抬"


# --------------------------------------------------------------------------- #
# 6. 配置键默认 false
# --------------------------------------------------------------------------- #
def test_config_cross_day_rolling_extremes_defaults_false():
    """configs/paper.yaml 的 paper.risk.cross_day_rolling_extremes 默认必须 false。"""
    from omegaconf import OmegaConf

    cfg = OmegaConf.load("configs/paper.yaml")
    paper = cfg.get("paper", cfg)
    val = dict(paper.get("risk", {}) or {}).get(
        "cross_day_rolling_extremes",
        paper.get("risk_cross_day_rolling_extremes", False),
    )
    assert bool(val) is False, "跨日滚动极值默认必须关闭（维持当日口径现状）"


def test_default_constructor_keeps_cross_day_off():
    """不传参构造 PaperBroker → 默认关闭跨日极值（所有存量调用点零变化）。"""
    pb = _pb()  # cross_day 缺省 False
    assert pb._cross_day_extremes is False
