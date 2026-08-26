"""P2 增量改动 —— QA 独立边界验证（第二层 fresh eyes，补工程师单测未覆盖路径）。

工程师 test_risk_gate_dedup.py / test_p2_incremental.py 已覆盖「happy path + 连续同条件」，
但以下边界/异常路径是本次增量验证的重点（尤其修复核心与 ratio 漂移）：

T1 去重核心：
  - 旧版 bug 是「非拒绝 tick 清空指纹」→ 同条件再拒绝又刷屏。工程师
    test_consecutive_same_condition_silent 只做了「连续拒绝」，未插入非拒绝 tick，
    故无法证明修复。本文件补：非拒绝 tick 之后同 fp 仍静默（核心证明）。

T2 报价时效：
  - 工程师只测了「过期(300s)跳过」与「age≈0 正常」。补：窗口内(ts=now-100s)不误拦、
    未来 ts(age 负)不误拦。

T3 今平门 ratio 换算漂移：
  - 工程师 test_min_hold_blocks_today_close_when_held_lt_min 只用了 1 手（被
    `max(1, int(raw))` 地板保护，恰好无漂移）。本文件补 1/2/3/5 手的回环，
    量化 maintain_ratio → PlanManager._size_qty 逆运算的漂移量级。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_scheduler_halt import (  # noqa: E402
    RecordingLog,
    StaleSignals,
    ValidQuotes,
    _q,
    _scheduler,
    _sig,
    scheduler_mod,
)

from omegaconf import OmegaConf  # noqa: E402

from hexbroker.backtest.cost import CostModel  # noqa: E402
from hexbroker.paper import risk_gate as rg_mod  # noqa: E402
from hexbroker.paper.risk_gate import RiskGate  # noqa: E402
from hexbroker.paper.planner import PlanManager  # noqa: E402
from hexbroker.paper.types import AccountSnapshot, PositionCtx, Quote, SignalFrame  # noqa: E402
from hexbroker.risk.types import RiskDecision  # noqa: E402

_RISK = OmegaConf.create(
    {
        "risk": {
            "vol_target": 0.5,
            "kelly_cap": 1.0,
            "max_position_pct": 0.5,
            "recovery_drawdown_r1": 0.05,
            "recovery_drawdown_r2": 0.10,
            "recovery_drawdown_r3": 0.15,
            "position_scalar_r1": 0.5,
            "position_scalar_r2": 0.0,
            "position_scalar_r3": 0.2,
            "position_scalar_r4": 1.0,
            "vol_low_q": 0.2,
            "vol_high_q": 0.8,
        }
    }
)


class _RecLog:
    """捕获 risk_gate 模块级 ``log`` 的 warning 文本。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str, *args: object, **kwargs: object) -> None:
        self.warnings.append(message.format(*args) if args else message)

    def info(self, message: str, *args: object, **kwargs: object) -> None:
        pass

    def debug(self, message: str, *args: object, **kwargs: object) -> None:
        pass

    def error(self, message: str, *args: object, **kwargs: object) -> None:
        self.warnings.append(message.format(*args) if args else message)

    def exception(self, message: str, *args: object, **kwargs: object) -> None:
        self.warnings.append(message.format(*args) if args else message)


def _cost() -> CostModel:
    return CostModel(
        fee_open=0.00005,
        fee_close=0.00005,
        fee_close_today=0.00010,
        slippage_ticks=1.0,
        margin_rate=0.12,
        multiplier=10.0,
        min_tick=10.0,
        contracts={"rb0": {"multiplier": 10.0, "min_tick": 1.0}},
    )


def _gate() -> RiskGate:
    return RiskGate(
        _RISK, default_intent=0.30, cost=_cost(), cost_gate_enabled=True, cost_gate_min_ratio=2.0
    )


def _acct() -> AccountSnapshot:
    return AccountSnapshot(
        ts=datetime(2026, 8, 25, 10, 0), equity=100_000.0, cash=100_000.0, margin_used=0.0,
        positions={}, avg_entry={}, realized={}, drawdown=0.0, peak_equity=100_000.0,
    )


def _pos(position: float = 0.0, entry: float = 3000.0) -> PositionCtx:
    return PositionCtx(symbol="rb0", position=position, entry_price=entry)


def _quote() -> Quote:
    return Quote(
        symbol="rb0", ts=datetime(2026, 8, 25, 10, 0), price=3000.0,
        open=3000.0, high=3001.0, low=2999.0, pre_settle=3000.0,
    )


def _sig(p_up: float = 0.7, exp_ret: float = 0.001) -> SignalFrame:
    """默认 exp_ret=0.001 → 成本门禁必拒（净期望收益不覆盖成本）。"""
    return SignalFrame(
        symbol="rb0", ts=datetime(2026, 8, 25, 10, 0), p_up=p_up, exp_ret=exp_ret,
        is_effective=True, source="test", freshness_days=1,
    )


def _cost_warnings(rec: _RecLog) -> list[str]:
    return [w for w in rec.warnings if "成本门禁拦截开仓" in w]


# ===========================================================================
# T1 去重核心：非拒绝 tick 不得清空指纹
# ===========================================================================
def test_dedup_non_reject_tick_keeps_fingerprint(monkeypatch):
    """修复核心证明：非拒绝 tick（如持仓中 evaluate）必须不清空指纹，
    否则同 fp 再拒绝又刷屏（旧版 bug）。"""
    rec = _RecLog()
    monkeypatch.setattr(rg_mod, "log", rec)
    rg = _gate()
    # ① 首拒（平今 + 弱信号 → 成本门禁拒）→ 打印 1 条，指纹落库
    rg.evaluate(_sig(), _quote(), _acct(), _pos(0.0))
    assert len(_cost_warnings(rec)) == 1
    # ② 非拒绝 tick：已持仓(position=1) → 成本门禁块被整体跳过，fp 不应被清空
    rg.evaluate(_sig(), _quote(), _acct(), _pos(1.0))
    assert len(_cost_warnings(rec)) == 1  # 仍只有 1 条，无新告警
    # ③ 同 fp 再拒绝（平今后）→ 因 fp 未清空，仍静默（不重打）
    rg.evaluate(_sig(), _quote(), _acct(), _pos(0.0))
    assert len(_cost_warnings(rec)) == 1


def test_dedup_fp_change_after_non_reject_still_reprints(monkeypatch):
    """反向证明：非拒绝 tick 之后，若 fp 真的变化（p_up 变），仍会重打——
    说明指纹逻辑完好，只是「未被清空」，而非「永远不更新」。"""
    rec = _RecLog()
    monkeypatch.setattr(rg_mod, "log", rec)
    rg = _gate()
    rg.evaluate(_sig(p_up=0.7), _quote(), _acct(), _pos(0.0))  # 首拒
    assert len(_cost_warnings(rec)) == 1
    rg.evaluate(_sig(), _quote(), _acct(), _pos(1.0))          # 非拒绝 tick
    assert len(_cost_warnings(rec)) == 1
    rg.evaluate(_sig(p_up=0.8), _quote(), _acct(), _pos(0.0))  # fp 变化 → 重打
    assert len(_cost_warnings(rec)) == 2


# ===========================================================================
# T2 报价时效：窗口内 / 未来 ts 不误拦
# ===========================================================================
def test_fresh_within_window_not_skipped(tmp_path, monkeypatch):
    """ts = now - 100s（窗口内，< 默认 180s）→ 不误拦，无「行情过期」告警。"""
    rec = RecordingLog()
    monkeypatch.setattr(scheduler_mod, "log", rec)
    sched = _scheduler(tmp_path, StaleSignals(_sig(0, True)), ValidQuotes())
    now = datetime(2026, 8, 24, 10, 0, 0)
    q = Quote(
        symbol="rb0", ts=now - timedelta(seconds=100), price=3038.0,
        open=3030.0, high=3040.0, low=3030.0, pre_settle=3030.0,
    )
    sched._process_symbol("rb0", now, q, {"rb0": 3038.0})
    assert not any("行情过期" in w for w in rec.warnings)


def test_future_ts_not_skipped(tmp_path, monkeypatch):
    """ts 在未来（age 负）→ 不误拦（age > max 为 False），无「行情过期」告警。"""
    rec = RecordingLog()
    monkeypatch.setattr(scheduler_mod, "log", rec)
    sched = _scheduler(tmp_path, StaleSignals(_sig(0, True)), ValidQuotes())
    now = datetime(2026, 8, 24, 10, 0, 0)
    q = Quote(
        symbol="rb0", ts=now + timedelta(seconds=30), price=3038.0,
        open=3030.0, high=3040.0, low=3030.0, pre_settle=3030.0,
    )
    sched._process_symbol("rb0", now, q, {"rb0": 3038.0})
    assert not any("行情过期" in w for w in rec.warnings)


# ===========================================================================
# T3 今平门 ratio 换算漂移量化
# ===========================================================================
class _FakeRG:
    def __init__(self, decision: RiskDecision) -> None:
        self._d = decision

    def evaluate(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self._d

    def set_cost(self, *args, **kwargs):  # noqa: ANN002, ANN003
        pass


def _build_min_hold_sched(tmp_path, min_hold: int) -> "object":
    sched = _scheduler(tmp_path, StaleSignals(_sig(0, True)), ValidQuotes())
    sched._min_hold_minutes = min_hold
    sched._risk_gate = _FakeRG(RiskDecision(target_position=0.0, liquidate=False))
    return sched


def _open_lots(sched, n: int, ts: datetime) -> None:
    """直接经 SimBroker 开 n 手（execute 目标为绝对手数），并记录开仓时刻。"""
    sched._broker._broker.execute("rb0", float(n), ref_price=3038.0, timestamp=ts)
    sched._broker._broker.open_dates["rb0"] = ts


def test_min_hold_ratio_roundtrip_through_pipeline(tmp_path):
    """经完整调度链路（scheduler 逆运算 → PlanManager._size_qty）验证回环：

    - cur=1 必须精确（实际手数不变、无成交）；
    - cur∈{2,3,5} 量化漂移：断言漂移 ≤ 1 手（int 截断放大浮点误差的上界），
      并打印实际回环手数供终裁判断「偏差量级是否可接受」。
    """
    sched = _build_min_hold_sched(tmp_path, 5)
    now = datetime(2026, 8, 24, 10, 0, 0)
    drift_report = {}
    for cur in (1, 2, 3, 5):
        sched2 = _build_min_hold_sched(tmp_path, 5)
        _open_lots(sched2, cur, now)  # 今开，held=0 < 5
        before = abs(sched2._broker.position("rb0"))
        sched2._process_symbol("rb0", now, _q(3038.0, now), {"rb0": 3038.0})
        after = abs(sched2._broker.position("rb0"))
        plan_qty = sched2._planner.get_plan("rb0").target_qty
        drift_report[cur] = {
            "before": before, "after": after,
            "plan_target_qty": plan_qty,
            "trades": len(sched2._all_trades),
            "drift_lots": after - before,
        }
        if cur == 1:
            # 1 手必须零漂移
            assert after == before and before > 0
            assert len(sched2._all_trades) == 0
    # 汇总漂移量级（供报告）；cur=1 必须为 0，其余应 ≤ 1 手（可被 int 截断上界约束）
    for cur, r in drift_report.items():
        print(f"[QA] min_hold 回环 cur={cur}: plan_target_qty={r['plan_target_qty']:.6f} "
              f"before={r['before']} after={r['after']} drift={r['drift_lots']:.6f} trades={r['trades']}")
        if cur >= 2:
            assert abs(r["drift_lots"]) <= 1.0 + 1e-9, f"cur={cur} 漂移超 1 手: {r['drift_lots']}"


def test_min_hold_ratio_inverse_pure_math():
    """纯 planner 逆运算回环（隔离浮点，不依赖调度）：maintain_ratio → _size_qty。

    验证公式本身一致（分母同为 equity、乘数同为 10）；并暴露 int(raw) 截断
    对多手持仓的放大效应。cur=1 必须精确回环。
    """
    pm = PlanManager(multipliers={"rb0": 10.0})
    price, equity = 3038.0, 300_000.0
    results = {}
    for cur in (1, 2, 3, 5, 10):
        maintain_ratio = cur * price * 10.0 / equity   # scheduler 逆运算公式
        qty = pm._size_qty("rb0", maintain_ratio, price, equity)  # planner 正运算
        results[cur] = qty
        print(f"[QA] 纯数学回环 cur={cur}: maintain_ratio={maintain_ratio:.10f} -> qty={qty}")
    assert results[1] == 1.0
    # 多手：回环手数可能因 int 截断下偏 ≤ 1 手（浮点误差被截断放大）
    for cur in (2, 3, 5, 10):
        assert abs(results[cur] - cur) <= 1.0 + 1e-9, f"cur={cur} 回环手数={results[cur]}"
