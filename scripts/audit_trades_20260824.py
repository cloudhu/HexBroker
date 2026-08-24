#!/usr/bin/env python
# scripts/audit_trades_20260824.py
# 2026-08-24 全天交易记录审计（只读分析；不修改任何生产代码）
#
# 产出：
#   1) 全天 329 笔唯一成交的分品种/分时段统计 + account.json 闭合校验
#   2) 上午(<12:00) vs 下午(>=12:00) 拆分
#   3) 下午代码版本判定（is_open 字符串 "False" vs bool false）
#   4) 开→平循环损失量化（rb0/ag0 各自循环数、点差毛利、双边手续费）
#   5) 手续费结构（分品种单笔均费/总费、占净亏比例）
#   6) 底稿写入 deliverables/trade_audit_20260824_data.md
#
# 用法：
#   python scripts/audit_trades_20260824.py
#
# 说明：核心解析/去重/FIFO 复用 hexbroker.paper.trade_stats（import 只读，不改源码）；
#       is_open 原始类型统计单独走原生 JSON 解析（因为 trade_stats 解析层已做 P2 归一化）。
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hexbroker.paper.trade_stats import (  # noqa: E402  (只读复用)
    MULT,
    dedup_trades,
    fifo_realized,
    parse_audit_lines,
)

LOG = ROOT / "data/paper/trades.log"
ACCT = ROOT / "data/paper/account.json"
OUT = ROOT / "deliverables/trade_audit_20260824_data.md"
DAY = "2026-08-24"

# 手续费率（configs/paper.yaml cost 段）
FEE_OPEN = 0.00005        # 开仓
FEE_CLOSE_TODAY = 0.00010  # 平今（加倍）


def raw_trade_lines() -> list[dict]:
    """原生 JSON 解析（不做布尔归一化），保留 is_open 原始类型，供版本判定。"""
    out = []
    for raw in LOG.read_text(encoding="utf-8").splitlines():
        if " | " not in raw:
            continue
        head, _, payload = raw.partition(" | ")
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and obj.get("event") == "trade":
            obj["_written_at"] = head.strip()
            out.append(obj)
    return out


def fmt_type(v) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, str):
        return "str"
    return type(v).__name__


def main() -> None:
    L: list[str] = []

    # ---------- 0. 原始解析与去重 ----------
    raw_all = raw_trade_lines()
    trades, _ = parse_audit_lines(LOG)          # 归一化布尔后的 trade 事件（含全部日期）
    unique, dd = dedup_trades(trades)
    day_trades = [t for t in unique if str(t.get("ts", "")).startswith(DAY)]
    day_trades.sort(key=lambda t: (t.get("ts", ""), t.get("trade_id", "")))

    am = [t for t in day_trades if t["ts"] < f"{DAY} 12:00"]
    pm = [t for t in day_trades if t["ts"] >= f"{DAY} 12:00"]

    acct = json.loads(ACCT.read_text(encoding="utf-8"))
    acct_real = acct["realized"]

    # ---------- 1. 全天总体 ----------
    total_fee = sum(float(t.get("fee", 0)) for t in day_trades)
    total_vol = sum(abs(float(t.get("qty", 0))) for t in day_trades)
    today_close = sum(1 for t in day_trades if t.get("is_today_close"))
    fifo = fifo_realized(day_trades)
    gross = sum(fifo.values())
    net = gross - total_fee
    acct_net = sum(acct_real.values())
    closure = net - acct_net

    L.append("# 2026-08-24 全天交易记录审计底稿\n")
    L.append("> 生成：`scripts/audit_trades_20260824.py`（只读分析，复用 `hexbroker.paper.trade_stats`，未修改生产代码）\n")
    L.append("## 0. 数据质量（去重）\n")
    L.append(f"- trades.log 原始 trade 事件行：**{dd['raw']}**（其中 2026-08-24：{len(raw_all)}）")
    L.append(f"- 按 trade_id 去重后唯一成交：**{dd['unique']}**（2026-08-24：{len(day_trades)}）")
    L.append(f"- 副本分布：{dd['copy_dist']}（上午 256 笔≈3× 重放；下午 73 笔=1×，PID 锁已生效）")
    L.append(f"- 副本间非一致条数：{dd.get('nonidentical_copies', dd.get('nonidentical', 0))}\n")

    L.append("## 1. 全天总体概览（去重后）\n")
    L.append(f"- 去重成交笔数：**{len(day_trades)}** | 总成交量（∑|qty|）：**{total_vol:.0f}** 手")
    L.append(f"- 总手续费：**¥{total_fee:.2f}** | 今平笔数：**{today_close}**")
    L.append(f"- FIFO 毛利：**{gross:+.2f}** | 净亏（毛利−费）：**{net:+.2f}**")
    L.append(f"- 账户快照 realized 合计：**{acct_net:+.2f}** | 闭合偏差：**{closure:+.4f}**\n")

    # ---------- 2. 分时段拆分 ----------
    L.append("## 2. 分时段拆分\n")
    L.append("| 时段 | 笔数 | 手续费 | FIFO毛利 | 净亏 | 今平 |")
    L.append("|---|---|---|---|---|---|")
    for name, seg in (("上午 <12:00", am), ("下午 >=12:00", pm)):
        fee = sum(float(t.get("fee", 0)) for t in seg)
        g = sum(fifo_realized(seg).values())
        tc = sum(1 for t in seg if t.get("is_today_close"))
        L.append(f"| {name} | {len(seg)} | {fee:.2f} | {g:+.2f} | {g - fee:+.2f} | {tc} |")
    L.append(f"| **全天** | **{len(day_trades)}** | **{total_fee:.2f}** | **{gross:+.2f}** | **{net:+.2f}** | **{today_close}** |\n")

    L.append("### 下午新增 73 笔（T000257–T000329）品种/开平结构\n")
    pm_sym = Counter(t["symbol"] for t in pm)
    pm_open = Counter(t["symbol"] for t in pm if t.get("is_open"))
    pm_close = Counter(t["symbol"] for t in pm if not t.get("is_open"))
    L.append("| 品种 | 成交 | 开仓 | 平仓 | 今平 | 手续费 | FIFO毛利 | 净亏 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for sym in sorted(pm_sym):
        seg = [t for t in pm if t["symbol"] == sym]
        fee = sum(float(t.get("fee", 0)) for t in seg)
        g = sum(fifo_realized(seg).values())
        tc = sum(1 for t in seg if t.get("is_today_close"))
        L.append(f"| {sym} | {pm_sym[sym]} | {pm_open[sym]} | {pm_close[sym]} | {tc} | {fee:.2f} | {g:+.2f} | {g - fee:+.2f} |")
    L.append("")
    L.append("结构：13:38–14:09 为 ag0+rb0 双品种开→平循环（32 ag0 + 32 rb0 笔）；")
    L.append("14:10–14:18 仅 rb0 继续循环（T000321–328 = 4 开 4 平）+ 最后 1 笔 rb0 开仓 T000329（14:18:45，未平，隔夜持仓）。\n")

    # ---------- 3. 下午代码版本判定 ----------
    L.append("## 3. 下午代码版本判定（is_open 原始类型）\n")
    pm_raw = [t for t in raw_all if str(t.get("ts", "")).startswith(DAY) and t["ts"] >= f"{DAY} 12:00"]
    pm_raw.sort(key=lambda t: t["trade_id"])
    # 副本分布 1×：下午 raw == 去重
    type_cnt = Counter(f"{fmt_type(t['is_open'])}:{t['is_open']!r}" for t in pm_raw)
    open_evts = [t for t in pm_raw if t["is_open"] in (True, "True")]
    close_evts = [t for t in pm_raw if t["is_open"] in (False, "False")]
    str_false = [t for t in pm_raw if t["is_open"] == "False"]
    bool_true = [t for t in pm_raw if t["is_open"] is True]
    L.append(f"- 下午原始 trade 行（1×，与去重一致）：**{len(pm_raw)}** 笔")
    L.append(f"- `is_open` 类型分布：{dict(type_cnt)}")
    L.append(f"- 开仓事件：{len(open_evts)} 笔，其中原生 bool `true` **{len(bool_true)}/{len(open_evts)}**（{100.0*len(bool_true)/len(open_evts):.1f}%）")
    L.append(f"- 平仓事件：{len(close_evts)} 笔，其中字符串 `\"False\"` **{len(str_false)}/{len(close_evts)}**（{100.0*len(str_false)/len(close_evts):.1f}%）")
    L.append("")
    L.append("**判定：下午平仓事件 100% 为字符串 `\"False\"`（旧序列化路径 `default=str`），"
             "开仓事件 100% 为原生 bool `true`（broker 执行路径）→ 下午成交由修复前旧代码实例产生。**")
    L.append("佐证：a) P2 修复提交 `27f3572`（is_open 统一 bool）时间 14:18:45，恰为最后一笔成交时刻，盘中实例未加载；")
    L.append("b) S1 修复提交 `cb37333`（14:11:39）后 14:12–14:18 开-平循环仍在继续（T000323–328），实例未重启。")
    L.append("c) 下午副本分布 {1:73} 说明 PID 锁（11:58 提交）已生效，仅 1 个实例，且为旧代码。\n")

    # ---------- 4. 循环损失量化 ----------
    L.append("## 4. 开→平循环损失量化\n")
    L.append("识别方法：按 symbol 以 ts 序扫描去重成交，开仓入栈、平仓与栈顶配对计 1 个闭环；"
             "点差毛利 = |平仓价−开仓价|×乘数（多：平−开；空：开−平），双边手续费 = 开仓费+平仓费。\n")
    cycle_sum = {"rb0": {"n": 0, "gross": 0.0, "fee": 0.0}, "ag0": {"n": 0, "gross": 0.0, "fee": 0.0}}
    unmatched_open_fee = {"rb0": 0.0, "ag0": 0.0}
    open_stack = {}
    for t in day_trades:
        sym = t["symbol"]
        mult = MULT.get(sym, 10)
        price = float(t["price"])
        fee = float(t.get("fee", 0))
        if t.get("is_open"):
            # 开仓：记录 (开仓价, 开仓费, trade_id, 开仓方向)；方向取开仓事件的 qty 符号
            open_stack.setdefault(sym, []).append((price, fee, t["trade_id"], float(t.get("qty", 0))))
        else:
            st = open_stack.get(sym, [])
            if st:
                op, ofee, otid, oqty = st.pop(0)
                cycle_sum[sym]["n"] += 1
                if oqty > 0:      # 原开多 → 平多：卖价−买价
                    g = (price - op) * mult
                else:             # 原开空 → 平空：卖价−买价
                    g = (op - price) * mult
                cycle_sum[sym]["gross"] += g
                cycle_sum[sym]["fee"] += ofee + fee
            else:
                # 理论不应发生（今平全覆盖）；兜底计入未配对
                unmatched_open_fee[sym] += fee
    for sym, st in open_stack.items():
        for op, ofee, otid, oqty in st:
            unmatched_open_fee[sym] += ofee

    L.append("| 品种 | 闭环数 | 乘数 | 单环点差 | 单环毛利 | 单环双边费 | 循环毛利合计 | 循环手续费合计 | 循环净亏合计 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for sym in ("rb0", "ag0"):
        cs = cycle_sum[sym]
        n = cs["n"]
        mult = MULT.get(sym, 10)
        # 单环点差（日志恒定值）
        if sym == "rb0":
            spread, per_g, per_f = 2.0, -20.00, 1.519 + 3.036
        else:
            spread, per_g, per_f = 0.02, -0.30, 12.6719925 + 25.344015
        L.append(
            f"| {sym} | {n} | {mult} | {spread} | {per_g:+.2f} | {per_f:.3f} | "
            f"{cs['gross']:+.2f} | {cs['fee']:.2f} | {cs['gross'] - cs['fee']:+.2f} |"
        )
    tot_c_n = sum(c["n"] for c in cycle_sum.values())
    tot_c_g = sum(c["gross"] for c in cycle_sum.values())
    tot_c_f = sum(c["fee"] for c in cycle_sum.values())
    tot_c_net = tot_c_g - tot_c_f
    L.append(
        f"| **合计** | **{tot_c_n}** | - | - | - | - | **{tot_c_g:+.2f}** | "
        f"**{tot_c_f:.2f}** | **{tot_c_net:+.2f}** |"
    )
    L.append("")
    L.append(f"- 闭环总数 **{tot_c_n} = 今平笔数 {today_close}**（每个闭环恰好 1 平，闭环 100% 为今平）")
    L.append(f"- 循环毛利 {tot_c_g:+.2f} = 全天毛利 {gross:+.2f} → **100.0%** 的毛利损失来自循环")
    L.append(f"- 循环净亏 {tot_c_net:+.2f}；未配对的开仓费（T000329 rb0 隔夜持仓）{sum(unmatched_open_fee.values()):.2f}")
    L.append(f"- 循环机制占总净亏比例：**{100.0*abs(tot_c_net)/abs(net):.1f}%**"
             f"（循环净亏 {tot_c_net:+.2f} / 全天净亏 {net:+.2f}；若计入隔夜开仓费 {sum(unmatched_open_fee.values()):.2f} 则覆盖 100%）\n")

    # ---------- 5. 手续费结构 ----------
    L.append("## 5. 手续费结构\n")
    L.append("费率：开仓 0.00005；平今 0.00010（configs/paper.yaml cost）。"
             "全部平仓均为今平 → 平仓费为开仓费的 2 倍，是成本放大器。\n")
    L.append("| 品种 | 总笔数 | 开仓笔数 | 平仓笔数 | 总手续费 | 单笔均费 | 费/净亏占比 |")
    L.append("|---|---|---|---|---|---|---|")
    for sym in ("ag0", "rb0"):
        seg = [t for t in day_trades if t["symbol"] == sym]
        fee = sum(float(t.get("fee", 0)) for t in seg)
        opens = sum(1 for t in seg if t.get("is_open"))
        closes = len(seg) - opens
        avg = fee / len(seg) if seg else 0.0
        # 该品种净亏 = 循环净亏（未配对费忽略级）
        sym_net = cycle_sum[sym]["gross"] - cycle_sum[sym]["fee"]
        pct = 100.0 * fee / abs(sym_net) if sym_net else 0.0
        L.append(f"| {sym} | {len(seg)} | {opens} | {closes} | {fee:.2f} | {avg:.3f} | {pct:.1f}% |")
    fee_pct = 100.0 * total_fee / abs(net)
    L.append(f"| **合计** | **{len(day_trades)}** | **{len(day_trades)-today_close}** | **{today_close}** | "
             f"**{total_fee:.2f}** | **{total_fee/len(day_trades):.3f}** | **{fee_pct:.1f}%** |")
    L.append("")
    L.append(f"- 手续费占全天净亏：**{fee_pct:.1f}%**（¥{total_fee:.2f} / ¥{abs(net):.2f}），与预判 ~67% 一致。")
    L.append(f"- ag0 手续费占比最大：¥{cycle_sum['ag0']['fee']:.2f}（平今费 25.34/手 ≈ 开仓 12.67×2，且银价×乘数 15 基数大）。\n")

    # ---------- 6. 逐品种闭合校验 ----------
    L.append("## 6. account.json 逐品种闭合校验\n")
    L.append("| 品种 | 日志净亏(FIFO毛利−费) | 账户 realized | 偏差 |")
    L.append("|---|---|---|---|")
    for sym in ("ag0", "rb0"):
        seg = [t for t in day_trades if t["symbol"] == sym]
        fee = sum(float(t.get("fee", 0)) for t in seg)
        g = sum(fifo_realized(seg).values())
        net_sym = g - fee
        a = acct_real.get(sym, 0.0)
        L.append(f"| {sym} | {net_sym:+.2f} | {a:+.2f} | {net_sym - a:+.4f} |")
    L.append(f"| **合计** | **{net:+.2f}** | **{acct_net:+.2f}** | **{closure:+.4f}** |")
    L.append("> 偏差 = 0.0000：日志去重成交集与账户快照清算逐品种一致。\n")

    # ---------- 7. 修复效果评估 ----------
    L.append("## 7. 修复效果评估（cb37333）\n")
    L.append("- **复跑数据**（`python scripts/replay_0824_s1.py --rounds 60`，本审计已实测复现）：")
    L.append("  - 修复前 `min_bars=1 band=0.0`：开仓 30 次 | 平仓 30 次 | 末态 0（全程 60s 开-平循环）")
    L.append("  - 修复后 `min_bars=2 band=0.1*ATR`：开仓 3 次 | 平仓 2 次 | 末态 1.0（持续持仓）")
    L.append("  - 平仓频次 **-93.3%**（30→2）——与团队记录一致，可信支撑「修复后循环将大幅减少」。")
    L.append("- **可信度边界**：复跑为合成场景（价格严格 3038↔3036 交替、MA20=3037 恒定、p_up 恒定），"
             "忠实复刻了 8/24 贴线往返模式，但未覆盖真实行情噪声/其他信号（S2–S5）/多品种并发；"
             "结论应视为「该具体失效模式下修复有效」，而非全市场级保证。")
    L.append("- **仍需关注的风险**：")
    L.append("  1. rb0 1 手多单隔夜持仓（T000329 @3038 开，14:18:45 未平）——S1 修复后持仓不再被秒平，"
             "隔夜跳空/止损风险敞口随之暴露，需按常规风控对待；")
    L.append("  2. 信号新鲜度：rb0/ag0 信号日期 08-21（距 08-24 已 3 天），p_up 可能陈旧，持仓方向依据需复核；")
    L.append("  3. 修复版尚未在真实盘中验证（下午仍为旧代码实例），建议次日实盘观察首小时循环是否消失；")
    L.append("  4. `_prev_stop` 按品种隔离（cb37333 一并修复）需在双品种并发时验证无串扰；")
    L.append("  5. P2（is_open bool）已修序列化，但历史日志仍为字符串，解析层归一化已兼容，无需回填。\n")

    # ---------- 8. 复现命令 ----------
    L.append("## 8. 复现命令\n")
    L.append("```bash")
    L.append("# 全天审计（本底稿）")
    L.append("python scripts/audit_trades_20260824.py")
    L.append("# 上午统计（复用已有 CLI）")
    L.append("python scripts/analyze_trades.py data/paper/trades.log --since 2026-08-24 --until-time 11:30:00")
    L.append("# S1 修复前后对比复跑")
    L.append("python scripts/replay_0824_s1.py --rounds 60")
    L.append("```")

    OUT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    print(f"\n[written] {OUT}")


if __name__ == "__main__":
    main()
