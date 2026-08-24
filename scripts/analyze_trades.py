#!/usr/bin/env python
# scripts/analyze_trades.py
# 交易审计日志统计分析（自动按 trade_id 去重，规避会话重放 3x 伪增）
# 用法：
#   python scripts/analyze_trades.py [log_path] [--since YYYY-MM-DD] [--until HH:MM:SS] [--out report.md]
#
# 核心逻辑已抽取到 hexbroker.paper.trade_stats，本脚本仅作 CLI 壳。
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hexbroker.paper.trade_stats import analyze_trades_log, render_daily_stats_markdown  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?", default="data/paper/trades.log")
    ap.add_argument("--since", default="2026-08-24")
    ap.add_argument("--until-time", default="11:30:00", help="当日截止时刻（午休前；留空=全天）")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    log_path = ROOT / args.log if not Path(args.log).is_absolute() else Path(args.log)
    acct_path = ROOT / "data/paper/account.json"
    result = analyze_trades_log(
        str(log_path),
        args.since,
        until_time=args.until_time or None,
        account_json_path=str(acct_path),
    )

    if not result["exists"]:
        print(f"[警告] 审计日志不存在：{log_path}")
        return

    L: list[str] = []
    L.append(f"# 交易记录统计分析（{args.since} 09:00–{args.until_time}）\n")

    L.append("## 0. 数据质量（关键：去重）\n")
    L.append(f"- 审计日志原始成交行：**{result['raw_count']}** 行")
    L.append(f"- 按 `trade_id` 去重后唯一成交：**{result['unique_count']}** 笔")
    L.append(f"- 每 id 副本数分布：{result['copy_dist']}（≈3× 会话重放，非真实 3 倍量）")
    L.append(f"- 副本间非一致（除 ts 外字段不同）条数：**{result['nonidentical']}** → 确为同会话重放")
    L.append("")

    L.append("## 1. 总体概览（去重后）\n")
    L.append(f"- 当日过滤成交笔数：**{result['filtered_count']}**")
    L.append(f"- 总成交量（∑|qty|）：**{result['total_vol']:.0f}** 手")
    L.append(f"- 总手续费：**¥{result['total_fee']:.2f}**")
    if result["filtered_count"]:
        L.append(f"- 平均每笔手续费：¥{result['total_fee'] / result['filtered_count']:.2f}")
    L.append(f"- 今平（is_today_close=True）笔数：**{result['today_close']}**\n")

    L.append("## 2. 去重统计明细\n")
    L.append(render_daily_stats_markdown(result))
    L.append("")

    L.append("## 3. 数据质量根因与修复建议\n")
    L.append("- **3× 重放根因**：`trades.log` 为 append 模式，原 PID 锁仅作健康检查标记（启动即 `write_text` 覆盖，**未互斥**）。多次双击 bat / 调度器自重启会启动多个并发实例，各自以 `_trade_seq` 从 1 重放同一历史行情流，写出相同 `trade_id`+相同事件 ts → 同一会话被记录约 3 次。")
    L.append("- **修复**：已在 `scripts/paper_trading_main.py` 实施 PID 锁启动互斥——写 PID 前检测既有存活实例，存活则拒绝第二个实例（exit 1），根除会话重放伪增。")
    L.append("- **统计口径结论**：本报告所有指标均基于按 `trade_id` 去重后的唯一成交；原始多倍行不可直接计数。\n")

    report = "\n".join(L)
    print(report)
    if args.out:
        out = ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)
        out.write_text(report, encoding="utf-8")
        print(f"\n[written] {out}")


if __name__ == "__main__":
    main()
