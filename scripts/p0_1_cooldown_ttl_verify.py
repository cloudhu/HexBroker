"""P0-1 冷却 TTL + 指纹生命周期 —— 端到端验证 harness（近实盘，不碰实盘状态）。

目的
----
以「当前源码 + **真实 configs/paper.yaml** + 受控时钟」驱动真实 ``TradingScheduler``，
验证 P0-1 的三项修复（以及 P0-2 原保证未被破坏）：

  A. 配置接线：``signal_cooldown.{ttl_minutes,max_reentries_per_day,persist}`` 与
     ``cooldown_file`` 确实被 Scheduler 读到（防止「写了 yaml 但代码没消费」）。
  B. P0-2 保证不回退：平仓后 +1min 同信号 → 仍被拦截（60s 开-平-开-平循环不复活）。
  C. TTL 到期（+31min）→ 放行重开。**这是 P0-1 的核心**：修复前冷却永不过期，
     信号缓存日内恒定 → 平仓后当日停摆（取证：rb0 08-31 停摆 1.94h / 09-01 停摆 2.36h）。
  D. 当日重开预算：重开次数达 max_reentries_per_day 后 → 拦截
     （reason=signal_cooldown_budget，与 TTL 未到期区分开）。
  E. 跨重启持久化：新 Scheduler 实例从同一 cooldown_file 恢复指纹 / 开仓时刻 /
     交易日 / 重开计数，且 TTL 起算点沿用（重启不重置冷却）。
  F. 换交易日 → 重开计数归零。
  G. 决策 trace 携带 ``cooldown_remaining_min`` / ``cooldown_reentries``。

隔离
----
- 全部落盘路径（account / trades_log / cooldown / plans / reports）重定向到临时目录，
  **不加载也不改写实盘 data/paper 状态**，不申请实盘 pid 锁。
- 行情/信号用**确定性 fake**（离线、固定价 3038 / MA20 3037 / ATR 40），
  唯一目的是让 TTL 的时间轴可控；风控、成本门禁、撮合、冷却全部走**生产实现**。
- ⛔ 本脚本不含任何冷却判定的本地副本：所有断言只读生产状态
  （``sched._last_sig_fp`` / ``sched._broker.position`` / ``sched._block_reasons``）。

用法
----
    python scripts/p0_1_cooldown_ttl_verify.py
"""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
from omegaconf import OmegaConf  # noqa: E402

LOG = logging.getLogger("p0_1_verify")

SYMBOL = "rb0"
TRADING_DAY = date(2026, 8, 24)      # 周一，非节假日（holidays_2026 未含）
NEXT_DAY = date(2026, 8, 25)
PRICE = 3038.0
MA20 = 3037.0


# ---------------------------------------------------------------------------
# 确定性 fixture（仅替代网络行情与信号缓存；风控/成本门禁/冷却均走生产实现）
# ---------------------------------------------------------------------------
class FixedQuotes:
    def fetch_quotes(self, symbols):
        from hexbroker.paper.types import Quote

        now = datetime.combine(TRADING_DAY, datetime.min.time()).replace(hour=9, minute=10)
        return {
            s: Quote(symbol=s, ts=now, price=PRICE, open=PRICE, high=PRICE + 1,
                     low=PRICE - 1, pre_settle=PRICE)
            for s in symbols
        }

    def fetch_bars(self, symbol, freq="1d", days=120):
        """22 根日线：close 恒定 3037 → MA20=3037；high-low=40 → ATR≈40。"""
        n = 30
        closes = [MA20] * n
        idx = pd.date_range("2026-07-01", periods=n, freq="D")
        return pd.DataFrame(
            {
                "open": closes,
                "high": [c + 20.0 for c in closes],
                "low": [c - 20.0 for c in closes],
                "close": closes,
                "volume": [1000.0] * n,
            },
            index=idx,
        )


class FixedSignals:
    """固定多头信号：p_up=0.7 / exp_ret=+0.5（与方向同号，否则成本门禁方向校验会拦截）。"""

    def __init__(self) -> None:
        self._sig = None

    def _make(self, symbol, asof):
        from hexbroker.paper.types import SignalFrame

        # freshness_days=1：默认阈值下为「新鲜」，不会走技术兜底降为 is_effective=False
        return SignalFrame(symbol=symbol, ts=asof, p_up=0.7, exp_ret=0.5,
                           is_effective=True, source="engine_a", freshness_days=1)

    def latest_signal(self, symbol, asof):
        return self._make(symbol, asof)

    def technical_fallback(self, symbol, bars):
        return None

    def neutral_signal(self, symbol, asof=None):
        from hexbroker.paper.types import SignalFrame

        return SignalFrame(symbol=symbol, ts=asof or datetime.now(), p_up=0.5,
                           exp_ret=0.0, is_effective=False, source="neutral")


# ---------------------------------------------------------------------------
def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "✅" if ok else "⛔"
    LOG.warning("%s %s %s", mark, label, detail)
    return ok


def _build(cfg, tmp: Path):
    """用真实组件工厂组装，仅替换行情/信号为确定性 fixture。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "paper_trading_main_mod", str(ROOT / "scripts" / "paper_trading_main.py")
    )
    ptm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ptm)

    comp = ptm.build_components(cfg, offline=True)
    comp["quotes"] = FixedQuotes()
    comp["signals"] = FixedSignals()

    from hexbroker.paper.scheduler import TradingScheduler

    sched = TradingScheduler(
        cfg=cfg,
        session=comp["session"],
        quotes=comp["quotes"],
        signals=comp["signals"],
        risk_gate=comp["risk_gate"],
        planner=comp["planner"],
        broker=comp["broker"],
        intel=comp["intel"],
        logger=comp["logger"],
        reporter=comp["reporter"],
        stop_event=None,
        run_days=None,
    )
    return sched, comp


def _tick(sched, when: datetime):
    from hexbroker.paper.types import Quote

    q = Quote(symbol=SYMBOL, ts=when, price=PRICE, open=PRICE,
              high=PRICE + 1, low=PRICE - 1, pre_settle=PRICE)
    sched._process_symbol(SYMBOL, when, q, {SYMBOL: PRICE})


def _flat(sched, when: datetime) -> None:
    """强制回到空仓（模拟止损/风控平仓）。直接调 broker，不经 _record_trade。"""
    from hexbroker.paper.types import Quote, Plan

    q = Quote(symbol=SYMBOL, ts=when, price=PRICE, open=PRICE,
              high=PRICE + 1, low=PRICE - 1, pre_settle=PRICE)
    sched._broker.execute_plan(
        Plan(symbol=SYMBOL, direction=0, target_qty=0.0, target_pos_pct=0.0), q, when
    )


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    from hexbroker.config import load_config

    full = load_config(str(ROOT / "configs" / "paper.yaml"))
    paper_dict = getattr(full, "paper", None)
    if paper_dict is None:
        LOG.error("配置缺少 paper 段")
        return 2
    cfg = OmegaConf.create(paper_dict)

    # ---- 隔离：全部落盘重定向到临时目录 ----
    tmp = Path(tempfile.mkdtemp(prefix="p0_1_verify_"))
    cfg.data_dir = str(tmp / "data")
    cfg.account_file = str(tmp / "data" / "account.json")
    cfg.trades_log = str(tmp / "trades.log")
    cfg.c0_daily_csv = str(tmp / "c0_daily.csv")
    cfg.plans_dir = str(tmp / "plans")
    cfg.reports_dir = str(tmp / "reports")
    cfg.intel_static_file = str(tmp / "news_static.json")
    # P0-1 冷却状态：重定向到 tmp（生产路径 data/paper/cooldown.json 不碰）
    cfg.cooldown_file = str(tmp / "cooldown.json")

    results: list[bool] = []
    try:
        sched, comp = _build(cfg, tmp)
        ttl = sched._signal_cooldown_ttl_min
        maxre = sched._signal_cooldown_max_reentries
        LOG.warning("=" * 78)
        LOG.warning("P0-1 端到端验证 | TTL=%.0fmin 预算=%d persist=%s", ttl, maxre,
                    sched._signal_cooldown_persist)
        LOG.warning("=" * 78)

        # ---- A. 配置接线 ----
        results.append(_check("A1 TTL 取自生产配置", ttl == 30.0, f"ttl_minutes={ttl}"))
        results.append(_check("A2 当日重开预算取自生产配置", maxre == 3, f"max_reentries_per_day={maxre}"))
        results.append(_check("A3 持久化已开启", sched._signal_cooldown_persist is True))
        results.append(_check(
            "A4 冷却文件路径已接线", sched._cooldown_file.name == "cooldown.json",
            f"path={sched._cooldown_file}",
        ))

        t0 = datetime(2026, 8, 24, 9, 10)
        steps = [t0 + timedelta(minutes=off) for off in (0, 1, 31, 62, 93, 124)]

        # ---- B. 首开 + P0-2 保证不回退 ----
        _tick(sched, steps[0])
        opened1 = abs(sched._broker.position(SYMBOL)) > 1e-12
        rec1 = sched._last_sig_fp.get(SYMBOL)
        results.append(_check("B1 首次开仓成功（无指纹 → 放行）", opened1,
                              f"position={sched._broker.position(SYMBOL)}"))
        results.append(_check("B2 首开重开计数为 0", rec1 is not None and rec1.reentries == 0))
        results.append(_check("B3 交易日标签正确（非自然日）",
                              rec1 is not None and rec1.day == "2026-08-24"))
        _flat(sched, steps[0])

        _tick(sched, steps[1])           # +1min < TTL
        results.append(_check("B4 平仓后 +1min 同信号 → 仍拦截（P0-2 保证不回退）",
                              abs(sched._broker.position(SYMBOL)) < 1e-12))

        # ---- C. TTL 到期放行（P0-1 核心） ----
        _tick(sched, steps[2])           # +31min ≥ TTL
        rec2 = sched._last_sig_fp.get(SYMBOL)
        ok_c = abs(sched._broker.position(SYMBOL)) > 1e-12
        results.append(_check("C1 TTL 到期 → 放行重开（修复当日停摆）", ok_c,
                              f"position={sched._broker.position(SYMBOL)}"))
        results.append(_check("C2 重开计数 +1", rec2 is not None and rec2.reentries == 1))
        _flat(sched, steps[2])

        # ---- D. 当日重开预算 ----
        _tick(sched, steps[3])           # +62min → 第 3 次开仓，reentries=2
        results.append(_check("D1 TTL 再次到期 → 放行（reentries=2）",
                              abs(sched._broker.position(SYMBOL)) > 1e-12))
        _flat(sched, steps[3])
        _tick(sched, steps[4])           # +93min → 第 4 次开仓，reentries=3（预算用尽）
        results.append(_check("D2 第 4 次开仓放行（reentries=3 = 预算上限）",
                              abs(sched._broker.position(SYMBOL)) > 1e-12))
        _flat(sched, steps[4])

        _tick(sched, steps[5])           # +124min：TTL 已到期但预算耗尽
        bucket = sched._block_reasons.get(TRADING_DAY, {})
        ok_d = abs(sched._broker.position(SYMBOL)) < 1e-12
        results.append(_check("D3 预算耗尽 → 拦截重开", ok_d))
        results.append(_check(
            "D4 拦截原因与 TTL 未到期区分（signal_cooldown_budget）",
            bucket.get("signal_cooldown_budget") == 1, f"bucket={bucket}",
        ))

        # ---- G. 决策 trace 携带冷却字段 ----
        fields = sched._cooldown_trace_fields(SYMBOL, steps[5], TRADING_DAY)
        results.append(_check("G1 trace 冷却字段已接线",
                              fields["cooldown_remaining_min"] == 0.0
                              and fields["cooldown_reentries"] == 3, f"{fields}"))

        # ---- E. 跨重启持久化 ----
        cooldown_file = tmp / "cooldown.json"
        results.append(_check("E1 开仓后立即原子落盘", cooldown_file.exists()))
        sched2, _ = _build(cfg, tmp)     # 模拟三窗口重启：全新进程 + 全新组件
        rec_r = sched2._last_sig_fp.get(SYMBOL)
        results.append(_check("E2 重启后指纹已恢复", rec_r is not None and rec_r.fp[0] == 0.7))
        results.append(_check("E3 重启后重开计数已恢复",
                              rec_r is not None and rec_r.reentries == 3,
                              f"reentries={rec_r.reentries if rec_r else None}"))
        results.append(_check("E4 重启后 TTL 起算点沿用（仍处预算耗尽态）",
                              rec_r is not None and rec_r.day == "2026-08-24"))
        # 重启后仍被预算拦截 → 证明「一天三次重启 ≠ 三次免费重开」
        _tick(sched2, steps[5] + timedelta(minutes=1))
        results.append(_check("E5 重启后未被重置为可开仓",
                              abs(sched2._broker.position(SYMBOL)) < 1e-12))

        # ---- F. 换交易日 → 计数归零 ----
        t_next = datetime(2026, 8, 25, 9, 10)
        _tick(sched2, t_next)
        rec_n = sched2._last_sig_fp.get(SYMBOL)
        results.append(_check("F1 新交易日 → 重开计数归零",
                              rec_n is not None and rec_n.reentries == 0
                              and rec_n.day == "2026-08-25",
                              f"day={rec_n.day if rec_n else None} reentries={rec_n.reentries if rec_n else None}"))
        results.append(_check("F2 新交易日首开成功",
                              abs(sched2._broker.position(SYMBOL)) > 1e-12))

        # ---- 落盘格式可读回 ----
        import json
        raw = json.loads(cooldown_file.read_text(encoding="utf-8"))
        results.append(_check("E6 落盘 schema 自洽",
                              raw.get("schema_version") == "1.0"
                              and list(raw["records"]["rb0"]["fp"]) == [0.7, 0.5, "engine_a"]))

        LOG.warning("=" * 78)
        passed = sum(1 for r in results if r)
        LOG.warning("结果：%d/%d 项通过", passed, len(results))
        LOG.warning("=" * 78)
        return 0 if passed == len(results) else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
