#!/usr/bin/env python
# scripts/qa_review_20260824.py
# QA fresh-eyes 独立复核（2026-08-24 全天交易审计）
# ------------------------------------------------------------------
# 原则：
#   1) 不导入工程师脚本/不依赖其输出；核心数字用本文件独立实现的
#      解析/去重/FIFO/闭环配对逻辑复算（仅对照 trade_stats.MULT 的
#      乘数常量——该常量由手续费反推，属业务配置，非工程师推断）。
#   2) 只读分析：不修改 data/、hexbroker/ 任何文件。
#   3) 输出证据链到 stdout，供 QA 报告引用。
# ------------------------------------------------------------------
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data/paper/trades.log"
ACCT = ROOT / "data/paper/account.json"
DAY = "2026-08-24"

# 合约乘数（trade_stats 注释：由手续费反推，业务常量）
MULT = {"ag0": 15, "rb0": 10}


# ---------------- 独立解析（不依赖 trade_stats） ----------------
def parse_raw_trades(path: Path) -> list[dict]:
    """原生 JSON 解析 trade 事件；保留 is_open 原始类型（bool/str）。"""
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
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


def is_open_bool(v) -> bool:
    """与生产解析层一致的布尔归一化（用于统计；原始类型另查）。"""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("true", "1")


def dedup(trades: list[dict]) -> tuple[list[dict], dict]:
    """按 trade_id 去重（保留首个副本）。"""
    seen, copy_count, nonidentical = {}, Counter(), 0
    for t in trades:
        tid = t.get("trade_id")
        copy_count[tid] += 1
        if tid not in seen:
            seen[tid] = t
        else:
            a = {k: v for k, v in seen[tid].items() if k not in ("ts", "_written_at")}
            b = {k: v for k, v in t.items() if k not in ("ts", "_written_at")}
            if a != b:
                nonidentical += 1
    return list(seen.values()), {
        "raw": len(trades),
        "unique": len(seen),
        "copy_dist": dict(Counter(copy_count.values())),
        "nonidentical": nonidentical,
    }


def fifo_realized(unique: list[dict]) -> dict[str, float]:
    """逐品种 FIFO 已实现毛利（独立实现，逻辑同 trade_stats 但独立书写）。"""
    stacks: dict[str, list] = defaultdict(list)
    realized: dict[str, float] = defaultdict(float)
    for ev in unique:
        sym = ev["symbol"]
        mult = MULT.get(sym, 10)
        price = float(ev["price"])
        signed_qty = float(ev["qty"])
        if is_open_bool(ev.get("is_open")):
            stacks[sym].append([price, signed_qty])
        else:
            remaining = abs(signed_qty)
            while remaining > 1e-9 and stacks[sym]:
                lot = stacks[sym][0]
                open_price, open_qty = lot
                m = min(remaining, abs(open_qty))
                if open_qty > 0:
                    realized[sym] += (price - open_price) * m * mult
                else:
                    realized[sym] += (open_price - price) * m * mult
                open_qty -= (m if open_qty > 0 else -m)
                remaining -= m
                if abs(open_qty) < 1e-9:
                    stacks[sym].pop(0)
    return dict(realized)


# ---------------- 闭环配对（独立实现） ----------------
def cycle_pairs(day_trades: list[dict]) -> dict:
    """按 symbol 以 ts/trade_id 序扫描：开仓入栈（FIFO），平仓与栈顶配对=1 闭环。"""
    res = {"rb0": {"n": 0, "gross": 0.0, "fee": 0.0, "spreads": []},
           "ag0": {"n": 0, "gross": 0.0, "fee": 0.0, "spreads": []}}
    unmatched_open_fee = defaultdict(float)
    open_stack: dict[str, list] = {}
    for t in day_trades:
        sym = t["symbol"]
        mult = MULT.get(sym, 10)
        price = float(t["price"])
        fee = float(t.get("fee", 0))
        if is_open_bool(t.get("is_open")):
            open_stack.setdefault(sym, []).append((price, fee, t["trade_id"], float(t.get("qty", 0))))
        else:
            st = open_stack.get(sym, [])
            if st:
                op, ofee, otid, oqty = st.pop(0)
                res[sym]["n"] += 1
                g = (price - op) * mult if oqty > 0 else (op - price) * mult
                res[sym]["gross"] += g
                res[sym]["fee"] += ofee + fee
                res[sym]["spreads"].append(g / mult)
            else:
                unmatched_open_fee[sym] += fee
    for sym, st in open_stack.items():
        for op, ofee, otid, oqty in st:
            unmatched_open_fee[sym] += ofee
    return {"cycle": res, "unmatched_open_fee": dict(unmatched_open_fee),
            "open_stack_left": {s: len(v) for s, v in open_stack.items()}}


# ---------------- 下午 stop 串扰检查 ----------------
def stop_crosscheck(pm_trades: list[dict]) -> dict:
    """检查平仓事件 stop 是否带其它品种止损值（P1 _prev_stop 串扰特征）。"""
    rows = []
    for t in pm_trades:
        sym = t["symbol"]
        stop = t.get("stop")
        # rb0 合理止损 ≈ 2917.89（开多 3038 - ATR40*2.5*... 实际日志值）
        # ag0 合理止损 ≈ 17307.07（开空 16895.99 + ...）
        if not is_open_bool(t.get("is_open")) and stop is not None:
            suspicious = (sym == "rb0" and abs(float(stop) - 17307.07) < 0.01) or \
                         (sym == "ag0" and abs(float(stop) - 2917.89) < 0.01)
            rows.append((t["trade_id"], sym, t["ts"], stop, "CONTAMINATED" if suspicious else "ok"))
    return {"total_close": len(rows), "contaminated": [r for r in rows if r[4] == "CONTAMINATED"]}


def main() -> None:
    L: list[str] = []

    raw_all = parse_raw_trades(LOG)
    unique, dd = dedup(raw_all)
    day_trades = [t for t in unique if str(t.get("ts", "")).startswith(DAY)]
    day_trades.sort(key=lambda t: (t.get("ts", ""), t.get("trade_id", "")))
    am = [t for t in day_trades if t["ts"] < f"{DAY} 12:00"]
    pm = [t for t in day_trades if t["ts"] >= f"{DAY} 12:00"]

    acct = json.loads(ACCT.read_text(encoding="utf-8"))
    acct_real = acct["realized"]

    def seg_stat(seg):
        fee = sum(float(t.get("fee", 0)) for t in seg)
        vol = sum(abs(float(t.get("qty", 0))) for t in seg)
        tc = sum(1 for t in seg if t.get("is_today_close"))
        g = sum(fifo_realized(seg).values())
        return {"n": len(seg), "vol": vol, "fee": fee, "today_close": tc,
                "gross": g, "net": g - fee}

    full = seg_stat(day_trades)
    am_s, pm_s = seg_stat(am), seg_stat(pm)

    L.append("==== 1. 全局去重（QA 独立） ====")
    L.append(f"原始 trade 事件行: {dd['raw']} | 2026-08-24 事件行: {len(raw_all)}")
    L.append(f"去重唯一: {dd['unique']} | 2026-08-24 唯一: {len(day_trades)}")
    L.append(f"副本分布: {dd['copy_dist']} | 副本间不一致: {dd['nonidentical']}")
    L.append(f"全天: 笔数 {full['n']} | 量 {full['vol']:.0f} 手 | 费 {full['fee']:.2f} | "
             f"今平 {full['today_close']} | 毛利 {full['gross']:+.2f} | 净 {full['net']:+.2f}")

    L.append("\n==== 2. 分品种闭合（QA 独立 FIFO vs account.json） ====")
    for sym in ("ag0", "rb0"):
        seg = [t for t in day_trades if t["symbol"] == sym]
        s = seg_stat(seg)
        a = acct_real.get(sym, 0.0)
        L.append(f"{sym}: 笔 {s['n']} | 费 {s['fee']:.2f} | 毛利 {s['gross']:+.2f} | 净 {s['net']:+.2f} | "
                 f"账户 realized {a:+.2f} | 偏差 {s['net'] - a:+.6f}")
    tot_a = sum(acct_real.values())
    L.append(f"合计: 净 {full['net']:+.2f} | 账户 realized {tot_a:+.2f} | 偏差 {full['net'] - tot_a:+.6f}")

    L.append("\n==== 3. 分时段（QA 独立） ====")
    L.append(f"上午<12:00: 笔 {am_s['n']} | 费 {am_s['fee']:.2f} | 毛利 {am_s['gross']:+.2f} | "
             f"净 {am_s['net']:+.2f} | 今平 {am_s['today_close']}")
    L.append(f"下午>=12:00: 笔 {pm_s['n']} | 费 {pm_s['fee']:.2f} | 毛利 {pm_s['gross']:+.2f} | "
             f"净 {pm_s['net']:+.2f} | 今平 {pm_s['today_close']}")
    for sym in ("ag0", "rb0"):
        seg = [t for t in pm if t["symbol"] == sym]
        s = seg_stat(seg)
        opens = sum(1 for t in seg if is_open_bool(t.get("is_open")))
        L.append(f"下午 {sym}: 笔 {s['n']} | 开 {opens} | 平 {s['n'] - opens} | 今平 {s['today_close']} | "
                 f"费 {s['fee']:.2f} | 毛利 {s['gross']:+.2f} | 净 {s['net']:+.2f}")

    L.append("\n==== 4. 下午 is_open 原始类型（QA 独立原生解析） ====")
    pm_raw = [t for t in raw_all if str(t.get("ts", "")).startswith(DAY) and t["ts"] >= f"{DAY} 12:00"]
    type_cnt = Counter(f"{'bool' if isinstance(t['is_open'], bool) else 'str'}:{t['is_open']!r}"
                       for t in pm_raw)
    L.append(f"下午原始行: {len(pm_raw)} | 类型分布: {dict(type_cnt)}")
    bool_false = sum(1 for t in pm_raw if t["is_open"] is False)
    str_false = sum(1 for t in pm_raw if t["is_open"] == "False")
    bool_true = sum(1 for t in pm_raw if t["is_open"] is True)
    close_n = sum(1 for t in pm_raw if t["is_open"] in (False, "False"))
    open_n = sum(1 for t in pm_raw if t["is_open"] in (True, "True"))
    L.append(f"开仓 {open_n}（bool true {bool_true}）| 平仓 {close_n}（str 'False' {str_false} / bool false {bool_false}）")

    L.append("\n==== 5. 下午 stop 跨品种串扰（QA 独立抽查） ====")
    sc = stop_crosscheck(pm)
    L.append(f"下午平仓事件总数: {sc['total_close']} | 疑似串扰(带对方品种 stop 值): {len(sc['contaminated'])}")
    for r in sc["contaminated"][:10]:
        L.append(f"  {r[0]} {r[1]} {r[2]} stop={r[3]} -> {r[4]}")

    L.append("\n==== 6. 闭环配对（QA 独立） ====")
    cp = cycle_pairs(day_trades)
    for sym in ("rb0", "ag0"):
        c = cp["cycle"][sym]
        mult = MULT.get(sym, 10)
        spread = sum(c["spreads"]) / len(c["spreads"]) if c["spreads"] else 0.0
        L.append(f"{sym}: 闭环 {c['n']} | 单环均点差 {spread:.4f} | 循环毛利 {c['gross']:+.2f} | "
                 f"循环费 {c['fee']:.2f} | 循环净 {c['gross'] - c['fee']:+.2f}")
    tot_n = sum(cp["cycle"][s]["n"] for s in ("rb0", "ag0"))
    tot_g = sum(cp["cycle"][s]["gross"] for s in ("rb0", "ag0"))
    tot_f = sum(cp["cycle"][s]["fee"] for s in ("rb0", "ag0"))
    tot_net = tot_g - tot_f
    L.append(f"合计: 闭环 {tot_n} | 循环毛利 {tot_g:+.2f} | 循环费 {tot_f:.2f} | 循环净 {tot_net:+.2f}")
    L.append(f"闭环 vs 今平: {tot_n} vs {full['today_close']} | 循环毛利/全天毛利: {tot_g/full['gross']*100:.1f}%")
    L.append(f"未配对开仓费: {cp['unmatched_open_fee']} | 剩余未平开仓栈: {cp['open_stack_left']}")
    L.append(f"循环净+隔夜开仓费 vs 全天净: {tot_net + sum(cp['unmatched_open_fee'].values()):+.2f} vs {full['net']:+.2f}")

    L.append("\n==== 7. 手续费占比（QA 独立） ====")
    L.append(f"费/净亏: {full['fee']/abs(full['net'])*100:.1f}%")
    for sym in ("ag0", "rb0"):
        seg = [t for t in day_trades if t["symbol"] == sym]
        s = seg_stat(seg)
        L.append(f"{sym}: 费 {s['fee']:.2f} / 净亏 {abs(s['net']):.2f} = {s['fee']/abs(s['net'])*100:.1f}%")

    L.append("\n==== 8. 尾部/其他风险（QA 独立） ====")
    last = day_trades[-1]
    L.append(f"日志最后一笔: {last['trade_id']} {last['symbol']} ts={last['ts']} price={last['price']} "
             f"qty={last['qty']} is_open={last['is_open']!r} is_today_close={last['is_today_close']}")
    syms = Counter(t["symbol"] for t in day_trades)
    L.append(f"全天品种分布: {dict(syms)} (c0 有无成交: {'c0' in syms})")
    rb_open_end = [t for t in day_trades if t["symbol"] == "rb0" and is_open_bool(t.get("is_open"))][-3:]
    for t in rb_open_end:
        L.append(f"  rb0 末段开仓: {t['trade_id']} ts={t['ts']} price={t['price']} stop={t.get('stop')}")

    txt = "\n".join(L)
    print(txt)
    (ROOT / "deliverables" / "_qa_review_stdout.txt").write_text(txt, encoding="utf-8")


if __name__ == "__main__":
    main()
