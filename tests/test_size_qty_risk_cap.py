"""P0-4 回归：``PlanManager._size_qty`` 的仓位粒度放大与风险预算上限。

背景（2026-09-01 实证，deliverables/复盘_2026-09-01_上午.md §三 P0-4）：
期货最小交易单位是 1 手，风控批准 0.4454 手只能实开 1 手 → 名义敞口被放大：
    rb0 15% → 33.46% 权益（越过 max_position_pct=0.30 硬顶，但单笔亏损仅 0.86%）
    ag0 30% → **256.86%** 权益（硬顶的 8.6 倍，**单笔亏损 24.89% 权益**）
而 broker 第二道防线按**保证金**把关（ag0 保证金仅占 ~31% < 预算 40% → 放行），兜不住。

⛔ 危害窗口（2026-09-01 端到端实测修正，勿再写错）：ag0 **只在意图 ≥ ~25.8% 时**
才会被放大——意图 15% 时 raw_lots=0.058 < 0.10 阈值直接不开仓。而
``configs/paper.yaml::risk_default_intent = 0.30`` 恰好落在放大区间内。

修法：``size_by_risk=True`` 时叠加**风险预算**约束
    单笔亏损 = |price - stop| × multiplier × lots  ≤  equity × risk_per_trade
只减不增（final = min(名义口径手数, 风险口径手数)），默认关闭以保持历史行为。

止损一律用**生产实现** ``hexbroker.risk.stoploss.compute_stop`` 计算，不硬编码倍率。
"""
from __future__ import annotations

import pytest

from hexbroker.paper.planner import PlanManager
from hexbroker.paper.types import Quote, SignalFrame, dt_now
from hexbroker.risk.stoploss import compute_stop
from hexbroker.risk.types import ATRTier, RiskDecision

# 生产口径实测（scripts/forensics_prod_data.py → sina 名义价，2026-08-31 收盘）
EQUITY = 94868.28
SYMBOLS = {
    "ag0": dict(price=16293.0, mult=15.0, atr=644.8571),
    "rb0": dict(price=3197.0, mult=10.0, atr=30.9286),
    "c0": dict(price=2299.0, mult=10.0, atr=22.2857),
}
MULTIPLIERS = {s: v["mult"] for s, v in SYMBOLS.items()}


def _planner(**kw) -> PlanManager:
    base = dict(multipliers=MULTIPLIERS, risk_reward_ratio=1.5, plans_dir="trade_plans")
    base.update(kw)
    return PlanManager(**base)


def _stop(sym: str, tier: ATRTier = ATRTier.HIGH) -> float:
    v = SYMBOLS[sym]
    return float(compute_stop(v["price"], 1.0, v["atr"], tier))


# ----------------------------------------------------------------------
# ① 现状回归：size_by_risk=False 必须与修复前逐位一致
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "sym,intent,expected",
    [
        ("rb0", 0.30, 1.0),   # raw=0.8902 → 1 手（放大 1.12×，越 30% 硬顶）
        ("rb0", 0.15, 1.0),   # raw=0.4451 → 1 手（放大 2.24×，今日实况）
        ("ag0", 0.30, 1.0),   # raw=0.1165 → 1 手（放大 8.6×，257% 权益）
        ("ag0", 0.15, 0.0),   # raw=0.0582 < 0.10 阈值 → 不开仓
        ("c0", 0.30, 1.0),    # raw=1.2380 → 1 手
        ("c0", 0.15, 1.0),    # raw=0.6190 → 1 手
    ],
)
def test_legacy_behavior_unchanged_when_size_by_risk_off(sym, intent, expected):
    pm = _planner(size_by_risk=False)
    v = SYMBOLS[sym]
    got = pm._size_qty(sym, intent, v["price"], EQUITY, stop=_stop(sym), atr=v["atr"])
    assert got == pytest.approx(expected)


def test_negative_direction_preserves_sign():
    pm = _planner(size_by_risk=True)
    v = SYMBOLS["rb0"]
    got = pm._size_qty("rb0", -0.15, v["price"], EQUITY, stop=_stop("rb0"))
    assert got == pytest.approx(-1.0)


# ----------------------------------------------------------------------
# ② 风险预算法：只拦真正风险过大的品种（默认 1% risk_per_trade）
# ----------------------------------------------------------------------
def test_risk_cap_blocks_ag0_whose_one_lot_risk_is_25pct_of_equity():
    """ag0 1 手风险 25.49% 权益 ≫ 1% 上限 → 0 手（核心修复点）。"""
    pm = _planner(size_by_risk=True, risk_per_trade=0.01)
    v = SYMBOLS["ag0"]
    got = pm._size_qty("ag0", 0.30, v["price"], EQUITY, stop=_stop("ag0"), atr=v["atr"])
    assert got == pytest.approx(0.0)


@pytest.mark.parametrize("sym", ["rb0", "c0"])
def test_risk_cap_allows_rb0_and_c0_unchanged(sym):
    """rb0(0.82%) / c0(0.59%) 单笔风险在 1% 内 → 仍开 1 手，**零行为变化**。"""
    pm = _planner(size_by_risk=True, risk_per_trade=0.01)
    v = SYMBOLS[sym]
    got = pm._size_qty(sym, 0.15, v["price"], EQUITY, stop=_stop(sym), atr=v["atr"])
    assert got == pytest.approx(1.0)


def test_risk_cap_only_reduces_never_increases():
    """放宽到 20% risk_per_trade 也不得突破**名义敞口**上限（final = min 两者）。"""
    pm = _planner(size_by_risk=True, risk_per_trade=0.20)
    v = SYMBOLS["rb0"]
    got = pm._size_qty("rb0", 0.30, v["price"], EQUITY, stop=_stop("rb0"), atr=v["atr"])
    # 名义口径 raw=0.8902 → 1 手；风险口径可达 24 手；取 min → 仍 1 手
    assert got == pytest.approx(1.0)


def test_risk_cap_uses_notional_cap_when_it_is_tighter():
    """c0 名义口径 1.238 手 → 1 手；风险口径 1.70 手 → min = 1 手。"""
    pm = _planner(size_by_risk=True, risk_per_trade=0.01)
    v = SYMBOLS["c0"]
    got = pm._size_qty("c0", 0.30, v["price"], EQUITY, stop=_stop("c0"), atr=v["atr"])
    assert got == pytest.approx(1.0)


# ----------------------------------------------------------------------
# ③ 止损距离取值优先级：stop > ATR 兜底 > 无法定价退回名义口径
# ----------------------------------------------------------------------
def test_risk_distance_prefers_real_stop_over_atr():
    pm = _planner(size_by_risk=True)
    v = SYMBOLS["ag0"]
    tight_stop = v["price"] * 0.999  # 0.1% 距离，远小于 ATR 兜底
    d = pm.risk_distance(v["price"], stop=tight_stop, atr=v["atr"])
    assert d == pytest.approx(v["price"] * 0.001)
    # 窄止损 → 1 手风险骤降 → ag0 也可开 1 手（证明用的是真实 stop 而非 ATR）
    got = pm._size_qty("ag0", 0.30, v["price"], EQUITY, stop=tight_stop, atr=v["atr"])
    assert got == pytest.approx(1.0)


def test_risk_distance_falls_back_to_atr_when_stop_missing():
    pm = _planner(size_by_risk=True, risk_stop_atr_mult=2.5)
    v = SYMBOLS["ag0"]
    d = pm.risk_distance(v["price"], stop=None, atr=v["atr"])
    assert d == pytest.approx(2.5 * v["atr"])
    got = pm._size_qty("ag0", 0.30, v["price"], EQUITY, stop=None, atr=v["atr"])
    assert got == pytest.approx(0.0)


def test_without_stop_and_atr_falls_back_to_notional_only():
    """既无 stop 也无 ATR → 无法给风险定价，退回名义口径（不得因此拒开仓）。"""
    pm = _planner(size_by_risk=True)
    v = SYMBOLS["rb0"]
    got = pm._size_qty("rb0", 0.15, v["price"], EQUITY, stop=None, atr=None)
    assert got == pytest.approx(1.0)
    assert pm.size_metrics("rb0", 0.15, v["price"], EQUITY)["capped_by"] == (
        "notional_only(no_risk_data)"
    )


# ----------------------------------------------------------------------
# ④ 可观测性：size_metrics 把「名义敞口 / 单笔风险」一次算清
# ----------------------------------------------------------------------
def test_size_metrics_flags_ag0_notional_over_cap():
    pm = _planner(size_by_risk=True)
    v = SYMBOLS["ag0"]
    m = pm.size_metrics("ag0", 0.30, v["price"], EQUITY, stop=_stop("ag0"), atr=v["atr"])
    assert m["over_notional_cap"] is True
    assert m["notional_pct"] == pytest.approx(2.5762, abs=1e-3)
    assert m["risk_pct_1lot"] == pytest.approx(0.2549, abs=1e-3)
    assert m["lots_risk"] == 0
    assert m["final_lots"] == 0
    assert m["capped_by"] == "risk_budget"


def test_size_metrics_rb0_over_cap_but_risk_ok():
    pm = _planner(size_by_risk=True)
    v = SYMBOLS["rb0"]
    m = pm.size_metrics("rb0", 0.15, v["price"], EQUITY, stop=_stop("rb0"), atr=v["atr"])
    assert m["over_notional_cap"] is True          # 名义 33.7% > 30% 硬顶
    assert m["risk_pct_1lot"] == pytest.approx(0.0082, abs=1e-3)
    assert m["final_lots"] == 1                    # 但风险可控 → 放行
    assert m["capped_by"] == "notional"


def test_size_metrics_zero_when_below_min_lot_threshold():
    pm = _planner()
    v = SYMBOLS["ag0"]
    m = pm.size_metrics("ag0", 0.15, v["price"], EQUITY, stop=_stop("ag0"))
    assert m["raw_lots"] < 0.10
    assert m["final_lots"] == 0
    assert m["capped_by"] == "min_lot_threshold"


def test_min_lot_threshold_short_circuit_leaves_risk_unpriced():
    """⛔ 语义锁定：低于 0.10 手时**提前返回**，risk_pct_1lot 合法为 None。

    2026-09-01 端到端脚本在此崩溃（``None * 100``），当时误判为「无法给风险定价」，
    实为「连 1 手都开不起 → 单笔风险本就不适用」。两者必须分清：
    前者是数据缺失（应告警），后者是正常业务分支（不告警）。
    """
    pm = _planner(size_by_risk=True)
    v = SYMBOLS["ag0"]
    m = pm.size_metrics("ag0", 0.15, v["price"], EQUITY, stop=_stop("ag0"), atr=v["atr"])
    assert m["capped_by"] == "min_lot_threshold"
    assert m["risk_dist"] is None          # 压根没走到定价分支
    assert m["risk_pct_1lot"] is None
    assert m["notional_pct"] == 0.0        # 名义敞口同样未成形
    assert m["final_lots"] == 0


def test_ag0_amplification_only_kicks_in_above_min_lot_threshold():
    """危害窗口：ag0 意图 ≥ ~25.8% 才会被放大到 1 手（≈257% 权益）。

    ⛔ 只看 15%（今日实况）会**漏掉真危害** —— configs/paper.yaml 的
    ``risk_default_intent = 0.30`` 正落在放大区间内。
    """
    pm = _planner()
    v = SYMBOLS["ag0"]
    below = pm.size_metrics("ag0", 0.15, v["price"], EQUITY)
    above = pm.size_metrics("ag0", 0.30, v["price"], EQUITY)
    assert below["final_lots"] == 0                       # raw=0.058 → 不开仓
    assert above["final_lots"] == 1                       # raw=0.117 → 1 手
    assert above["notional_pct"] > 2.5                    # ≈257% 权益
    assert above["over_notional_cap"] is True


# ----------------------------------------------------------------------
# ⑤ 接线：update_from_signal 透传 stop / atr
# ----------------------------------------------------------------------
# ⛔ 一律用**生产 dataclass** 构造入参，不用手搓假对象：
# 假对象会与真实定义漂移（本次就差点漏掉 ``SignalFrame.source``），
# 而漂移会把「接线断了」伪装成「接线正常」。
def _signal(sym: str) -> SignalFrame:
    return SignalFrame(symbol=sym, ts=dt_now(), p_up=0.7, exp_ret=0.01, is_effective=True)


def _quote(sym: str) -> Quote:
    return Quote(symbol=sym, ts=dt_now(), price=SYMBOLS[sym]["price"])


def test_update_from_signal_passes_stop_into_sizing():
    """decision.stop_price 必须能影响手数（否则 P0-4 风险口径形同虚设）。"""
    v = SYMBOLS["ag0"]
    stop = _stop("ag0")

    pm_off = _planner(size_by_risk=False)
    pm_on = _planner(size_by_risk=True)

    sig = _signal("ag0")
    quote = _quote("ag0")
    dec = RiskDecision(target_position=0.30, stop_price=stop)

    q_off = pm_off.update_from_signal(sig, dec, quote=quote, equity=EQUITY)
    q_on = pm_on.update_from_signal(sig, dec, quote=quote, equity=EQUITY)
    assert q_off.target_qty == pytest.approx(1.0)   # 现状：放大到 257% 权益
    assert q_on.target_qty == pytest.approx(0.0)    # 风险口径：拦下


def test_update_from_signal_liquidate_yields_zero():
    pm = _planner(size_by_risk=True)
    dec = RiskDecision(target_position=0.30, stop_price=_stop("rb0"), liquidate=True)
    plan = pm.update_from_signal(_signal("rb0"), dec, quote=_quote("rb0"), equity=EQUITY)
    assert plan.target_qty == pytest.approx(0.0)


def test_update_from_signal_without_stop_price_uses_atr_fallback():
    """scheduler 传入的 atr 必须能在决策无 stop_price 时兜底定价风险。"""
    v = SYMBOLS["ag0"]
    pm = _planner(size_by_risk=True)
    dec = RiskDecision(target_position=0.30)   # stop_price=None
    plan = pm.update_from_signal(
        _signal("ag0"), dec, quote=_quote("ag0"), equity=EQUITY, atr=v["atr"]
    )
    assert plan.target_qty == pytest.approx(0.0)   # ATR 兜底同样拦下 ag0
    assert plan.stop_price is None                # 决策本就无止损，不得凭空造一个


# ----------------------------------------------------------------------
# ⑥ 配置接线：configs/paper.yaml 的新键必须真的被读进来
# 上面的单测参数由测试给定，验证不了接线；这里走生产入口构建。
# ----------------------------------------------------------------------
def _load_entry_module():
    """加载 scripts/paper_trading_main.py（文件名非法标识符，不能用 import）。"""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "_ptm_under_test", root / "scripts" / "paper_trading_main.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _production_planner(size_by_risk: bool):
    ptm = _load_entry_module()
    cfg = ptm._load_paper_config("configs/paper.yaml")
    cfg["size_by_risk"] = size_by_risk
    ok, planner = ptm.build_components_safe(cfg, offline=True)["planner"]
    assert ok, "生产入口构建 planner 失败"
    return cfg, planner


def test_config_keys_are_read_from_paper_yaml():
    """新键必须来自配置文件，而不是构造器的默认值兜底。"""
    _, pm = _production_planner(False)
    assert pm._risk_per_trade == pytest.approx(0.01)
    assert pm._risk_stop_atr_mult == pytest.approx(2.5)
    assert pm._size_by_risk is False


def test_max_position_pct_prefers_risk_overrides_over_risk_config():
    """⛔ 优先级：``risk_overrides``(0.50) > ``risk_config``(0.30)。

    生产 ``RiskGate`` 吃的是 overrides，只从 ``configs/risk/v4_atr.yaml`` 读会拿到
    0.30 → 告警判据比实际风控**严格**（rb0 的 33.46% 在 0.50 口径下并未越界）。
    """
    ptm = _load_entry_module()
    cfg = ptm._load_paper_config("configs/paper.yaml")
    assert float(cfg.risk_overrides.max_position_pct) == pytest.approx(0.50)
    assert ptm._max_position_pct(cfg) == pytest.approx(0.50)


def test_risk_per_trade_by_symbol_is_wired_from_config():
    """D2-C：``risk_per_trade_by_symbol`` 必须真的从 yaml 读进来。"""
    _, pm = _production_planner(True)
    assert pm.risk_budget_for("ag0") == pytest.approx(0.01)     # 显式覆盖
    assert pm.risk_budget_for("rb0") == pytest.approx(0.01)     # 未列出 → 继承全局
    assert pm.risk_budget_for("c0") == pytest.approx(0.01)


def test_config_default_keeps_legacy_behavior():
    """⛔ 硬约束：配置默认 size_by_risk=false —— 不得静默改变实盘行为。"""
    _, pm = _production_planner(False)
    v = SYMBOLS["ag0"]
    lots = pm._size_qty("ag0", 0.30, v["price"], EQUITY, stop=_stop("ag0"), atr=v["atr"])
    assert lots == pytest.approx(1.0)   # 现状照旧：放大到 ≈257% 权益


def test_config_flip_enables_risk_budget_end_to_end():
    """把配置翻成 true，ag0@30% 必须从 1 手变 0 手 —— 证明接线真的通到 _size_qty。"""
    _, pm = _production_planner(True)
    v = SYMBOLS["ag0"]
    lots = pm._size_qty("ag0", 0.30, v["price"], EQUITY, stop=_stop("ag0"), atr=v["atr"])
    assert lots == pytest.approx(0.0)


# ----------------------------------------------------------------------
# ⑦ D2-C（2026-09-01 主理人裁决）：按品种覆盖单笔风险预算
# ----------------------------------------------------------------------
def test_risk_budget_for_prefers_symbol_override():
    pm = _planner(
        size_by_risk=True, risk_per_trade=0.01, risk_per_trade_by_symbol={"ag0": 0.30}
    )
    assert pm.risk_budget_for("ag0") == pytest.approx(0.30)   # 覆盖生效
    assert pm.risk_budget_for("rb0") == pytest.approx(0.01)   # 其余继承全局


def test_per_symbol_override_can_rescue_a_blocked_symbol():
    """同一品种、同一行情，只改预算就能从「拦下」变「放行」——证明覆盖真的参与计算。"""
    v = SYMBOLS["ag0"]
    stop = _stop("ag0")
    blocked = _planner(size_by_risk=True, risk_per_trade=0.01)
    rescued = _planner(
        size_by_risk=True, risk_per_trade=0.01, risk_per_trade_by_symbol={"ag0": 0.30}
    )
    assert blocked._size_qty("ag0", 0.30, v["price"], EQUITY, stop=stop, atr=v["atr"]) == 0.0
    assert rescued._size_qty("ag0", 0.30, v["price"], EQUITY, stop=stop, atr=v["atr"]) == 1.0


def test_size_metrics_reports_effective_budget():
    """trace 归因需要知道**本品种实际生效**的预算，不然日志里的数字对不上配置。"""
    pm = _planner(
        size_by_risk=True, risk_per_trade=0.01, risk_per_trade_by_symbol={"ag0": 0.30}
    )
    v = SYMBOLS["ag0"]
    m = pm.size_metrics("ag0", 0.30, v["price"], EQUITY, stop=_stop("ag0"), atr=v["atr"])
    assert m["risk_budget"] == pytest.approx(0.30)
    assert m["capped_by"] == "notional"      # 预算放行后，由名义口径封顶
    assert m["final_lots"] == 1


def test_per_symbol_override_never_breaks_only_reduce_invariant():
    """放宽容许度也不能突破名义口径 —— final = min(名义, 风险) 恒成立。"""
    pm = _planner(
        size_by_risk=True, risk_per_trade=0.01, risk_per_trade_by_symbol={"rb0": 0.99}
    )
    v = SYMBOLS["rb0"]
    m = pm.size_metrics("rb0", 0.30, v["price"], EQUITY, stop=_stop("rb0"), atr=v["atr"])
    assert m["lots_risk"] > m["lots_notional"]      # 风险口径极宽
    assert m["final_lots"] == m["lots_notional"]    # 但仍被名义口径封顶
