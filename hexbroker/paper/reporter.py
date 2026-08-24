"""每日复盘（§3.1 ReviewReporter / A5 / R9）。

收盘后自动生成 ``deliverables/复盘_YYYY-MM-DD.md``（交易明细/持仓/盈亏/信号命中/
情报/计划变更/明日关注），并在命令窗口输出摘要；满 20 交易日输出评估摘要（Q5）。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Optional

from ..utils.logging import get_logger
from .trade_intent import plan_change_intent, trade_intent
from .trade_stats import analyze_trades_log, render_daily_stats_markdown
from .types import AccountSnapshot, NewsItem, Plan, PlanChange, SignalFrame, TradeEvent

log = get_logger("PAPER")


class ReviewReporter:
    """复盘报告生成器。"""

    def __init__(self, reports_dir: str | Path = "deliverables", symbols_display: Optional[dict[str, str]] = None) -> None:
        self._reports_dir = Path(reports_dir)
        self._symbols_display = symbols_display or {}

    def _display(self, symbol: str) -> str:
        return self._symbols_display.get(symbol, symbol)

    # ------------------------------------------------------------------
    # 每日复盘
    # ------------------------------------------------------------------
    def generate(
        self,
        day: date,
        acct: AccountSnapshot,
        trades: list[TradeEvent],
        plans: dict[str, Plan],
        signals: dict[str, list[SignalFrame]],
        news: list[NewsItem],
        changes: Optional[list[PlanChange]] = None,
        trades_log_path: str | Path = "data/paper/trades.log",
    ) -> Path:
        """生成复盘 md 并落盘；返回文件路径。

        ``trades_log_path``：审计日志路径，用于「八、当日交易统计（日志去重）」段
        （按 trade_id 去重后统计，规避会话重放 3× 伪增；并与账户快照做闭合校验）。
        """
        self._reports_dir.mkdir(parents=True, exist_ok=True)
        md = self._render_md(
            day, acct, trades, plans, signals, news, changes or [],
            trades_log_path=trades_log_path,
        )
        path = self._reports_dir / f"复盘_{day.isoformat()}.md"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(md, encoding="utf-8")
        tmp.replace(path)  # P2-6：tmp + os.replace 原子写，避免写入中断产生半文件
        log.info("复盘报告已生成 path={}", path)
        return path

    def _render_md(
        self,
        day: date,
        acct: AccountSnapshot,
        trades: list[TradeEvent],
        plans: dict[str, Plan],
        signals: dict[str, list[SignalFrame]],
        news: list[NewsItem],
        changes: list[PlanChange],
        trades_log_path: str | Path = "data/paper/trades.log",
    ) -> str:
        lines: list[str] = []
        lines.append(f"# 模拟盘复盘 {day.isoformat()}")
        lines.append("")
        lines.append(f"> 生成时间：{datetime.now().isoformat(timespec='seconds')} · 自动生成")
        lines.append("")
        # 账户
        lines.append("## 一、账户概览")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("|---|---|")
        lines.append(f"| 权益 | {acct.equity:,.2f} |")
        lines.append(f"| 可用资金 | {acct.cash:,.2f} |")
        lines.append(f"| 保证金占用 | {acct.margin_used:,.2f} |")
        lines.append(f"| 回撤 | {acct.drawdown * 100:.2f}% |")
        lines.append(f"| 峰值权益 | {acct.peak_equity:,.2f} |")
        lines.append("")
        # 持仓
        lines.append("## 二、当前持仓")
        lines.append("")
        if acct.positions:
            lines.append("| 品种 | 手数 | 均价 | 已实现盈亏 |")
            lines.append("|---|---|---|---|")
            for sym, pos in acct.positions.items():
                lines.append(
                    f"| {self._display(sym)} | {pos:g} | {acct.avg_entry.get(sym, 0.0):g} | "
                    f"{acct.realized.get(sym, 0.0):,.2f} |"
                )
        else:
            lines.append("（无持仓）")
        lines.append("")
        # 当日交易明细
        lines.append("## 三、当日交易明细")
        lines.append("")
        if trades:
            lines.append("| 时间 | 品种 | 方向 | 手数 | 成交价 | 手续费 | 止损 | 止盈 | 交易意图（中文） |")
            lines.append("|---|---|---|---|---|---|---|---|---|")
            for t in trades:
                intent = trade_intent(t.to_dict())
                lines.append(
                    f"| {t.ts.isoformat(timespec='seconds')} | {self._display(t.symbol)} | "
                    f"{t.direction_label()} | {t.qty:g} | {t.price:g} | {t.fee:.4f} | "
                    f"{t.stop if t.stop is not None else '-'} | {t.take_profit if t.take_profit is not None else '-'} | "
                    f"{intent} |"
                )
        else:
            lines.append("（无成交）")
        lines.append("")
        # 信号
        lines.append("## 四、信号命中")
        lines.append("")
        any_sig = any(v for v in signals.values())
        if any_sig:
            lines.append("| 品种 | 来源 | 时间 | p_up | exp_ret | 新鲜度(交易日) | 有效 |")
            lines.append("|---|---|---|---|---|---|---|")
            for sym, sigs in signals.items():
                for s in sigs:
                    lines.append(
                        f"| {self._display(sym)} | {s.source} | {s.ts.isoformat(timespec='seconds')} | "
                        f"{s.p_up:.3f} | {s.exp_ret:.4f} | {s.freshness_days} | {s.is_effective} |"
                    )
        else:
            lines.append("（今日无信号评估记录）")
        lines.append("")
        # 情报
        lines.append("## 五、情报事件")
        lines.append("")
        if news:
            for n in news:
                lines.append(f"- [{n.ts.isoformat(timespec='seconds')}] **{n.title}**（{n.source}）")
                if n.summary:
                    lines.append(f"  - {n.summary}")
        else:
            lines.append("（今日无情报事件）")
        lines.append("")
        # 计划变更
        lines.append("## 六、计划变更记录")
        lines.append("")
        if changes:
            for c in changes:
                lines.append(f"- {plan_change_intent(c.to_dict())}")
        else:
            lines.append("（今日无计划变更）")
        lines.append("")
        # 明日关注
        lines.append("## 七、明日关注 / 风险提示")
        lines.append("")
        risk_flags = [p.risk_flag for p in plans.values() if p.risk_flag]
        if risk_flags:
            for rf in risk_flags:
                lines.append(f"- ⚠️ {rf}")
        else:
            lines.append("- 按信号与风控正常跟踪；c0 处于日线积累期（accumulate），仅跟踪不开新仓。")
        lines.append("")

        # 当日交易统计（日志去重）：从审计日志按 trade_id 去重，与账户快照闭合校验
        lines.append("## 八、当日交易统计（日志去重）")
        lines.append("")
        try:
            stats = analyze_trades_log(str(trades_log_path), day)
            lines.append(render_daily_stats_markdown(stats, acct_realized=dict(acct.realized)))
        except Exception as exc:  # noqa: BLE001
            log.exception("当日交易统计（日志去重）生成失败（已隔离）")
            lines.append(f"（统计段生成失败：{type(exc).__name__}: {exc}）\n")
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 首轮评估（Q5：满 20 交易日）
    # ------------------------------------------------------------------
    def generate_evaluation(
        self,
        day: date,
        acct: AccountSnapshot,
        trades: list[TradeEvent],
        days_run: int,
        initial_capital: float = 100_000.0,
        trades_log_path: str | Path = "data/paper/trades.log",
    ) -> Path:
        """20 交易日评估摘要：收益率/已实现盈亏/回撤/成交笔数。

        ``trades_log_path``：审计日志路径，用于「当日交易统计（日志去重）」段
        （按 trade_id 去重，规避会话重放 3× 伪增；与账户快照闭合校验，口径同复盘「八」段）。
        """
        self._reports_dir.mkdir(parents=True, exist_ok=True)
        total_pnl = sum(acct.realized.values())
        ret_pct = (acct.equity - initial_capital) / initial_capital * 100 if initial_capital > 0 else 0.0
        lines: list[str] = []
        lines.append(f"# 模拟盘首轮评估（第 {days_run} 个交易日）")
        lines.append("")
        lines.append(f"> 生成时间：{datetime.now().isoformat(timespec='seconds')} · 评估日 {day.isoformat()}")
        lines.append("")
        lines.append("## 账户总结")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("|---|---|")
        lines.append(f"| 权益 | {acct.equity:,.2f} |")
        lines.append(f"| 已实现盈亏 | {total_pnl:,.2f} |")
        lines.append(f"| 收益率（vs 初始 {initial_capital:,.0f}） | {ret_pct:.2f}% |")
        lines.append(f"| 回撤 | {acct.drawdown * 100:.2f}% |")
        lines.append(f"| 成交笔数 | {len(trades)} |")
        lines.append("")

        # 当日交易统计（日志去重）：口径同复盘「八」段（按 trade_id 去重 + 账户快照闭合校验）
        lines.append("## 当日交易统计（日志去重）")
        lines.append("")
        try:
            stats = analyze_trades_log(str(trades_log_path), day)
            lines.append(render_daily_stats_markdown(stats, acct_realized=dict(acct.realized)))
        except Exception as exc:  # noqa: BLE001
            log.exception("评估报告交易统计段生成失败（已隔离）")
            lines.append(f"（统计段生成失败：{type(exc).__name__}: {exc}）\n")
        lines.append("")

        lines.append("## 说明")
        lines.append("")
        lines.append("- 胜率/盈亏曲线/信号命中率在积累更多交易日数据后于后续评估中展开。")
        lines.append("- 首轮重点：验证 报价→信号→风控→计划→撮合→日志→情报→复盘 管道闭环。")
        lines.append("- 交易统计段基于审计日志按 trade_id 去重（规避会话重放 3× 伪增），与账户快照闭合校验。")
        lines.append("")
        path = self._reports_dir / f"模拟盘评估_第{days_run}交易日.md"
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        tmp.replace(path)  # P2-6：tmp + os.replace 原子写（与 generate 同口径）
        log.info("首轮评估摘要已生成 path={}", path)
        return path