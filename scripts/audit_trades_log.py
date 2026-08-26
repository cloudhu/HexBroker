# -*- coding: utf-8 -*-
"""交易记录审计：解析 data/paper/trades.log，量化异常并对照 paper.yaml 规则。

修订（2026-08-26，P1-2 根因修复）：
- 成交级统计（F5/F7/F8/F9/F10/F11/c0）改为以**权威结构化审计 JSON**（`TradeLogger.trade`
  中 `log_structured(EVT_TRADE)` 无条件写出的 ` | {json}` 行）为唯一口径，按 `trade_id`
  去重（keep-first）得到全量成交集；不再以受 `self._intent` 门控的 `[意图]` 可读子集为口径。
- 保留 `intent_lines_raw` / `intent_coverage` 用于暴露「可读翻译仅覆盖部分成交」的事实。
- 持仓时长改用 datetime 精确秒差（替换原字符串截取近似）。
"""
import re, json, statistics
from collections import defaultdict, Counter
from datetime import datetime

LOG = r"E:/Workspace/HexBroker/data/paper/trades.log"
OUT = r"E:/Workspace/HexBroker/data/paper/audit_result.json"

lines = open(LOG, encoding="utf-8").read().splitlines()

re_ts = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
re_start = re.compile(r"模拟盘交易系统已启动")
re_netfail = re.compile(r"拉取失败|getaddrinfo failed|实时行情拉取失败")
re_gate = re.compile(
    r"成本门禁拦截开仓 symbol=(\w+) price=([\d.]+) p_up=([\d.\-]+) exp_ret=([-\d.]+) "
    r"expected_pnl=([\d.]+) round_trip_cost=([\d.]+) notional=([\d.]+) min_ratio=([\d.]+)"
)
re_intent = re.compile(
    r"\[意图\]\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"(开多|开空|平多|平空|【今平】平多|【今平】平空)\s+(\d+)\s+手\s+(\w+)\((\w+)\)："
)
re_stat = re.compile(r"\[统计\] 盘中当日统计：(.+)")
re_snap = re.compile(r"账户快照已保存 equity=([\d.]+) cash=([\d.]+)")

# ----------------------------------------------------------------------------
# 1) 结构化审计 JSON 成交（权威源，按 trade_id 去重）→ 全量成交集
# ----------------------------------------------------------------------------
def _ts_dt(o):
    """结构化 JSON 的 ts → datetime（兼容 'T' 与空格分隔、带/不带微秒）。"""
    ts = str(o.get("ts", ""))
    if "T" in ts:
        ts = ts.replace("T", " ")
    if "." in ts:
        ts = ts.split(".")[0]
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _as_bool(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("true", "1", "yes", "y")


def _action(o):
    d = int(o.get("direction", 0))
    io = _as_bool(o.get("is_open"))
    base = ("开多" if d > 0 else "开空") if io else ("平多" if d < 0 else "平空")
    if (not io) and _as_bool(o.get("is_today_close")):
        return "【今平】" + base
    return base


def parse_structured_trades(lines):
    """解析 ` | {json}` 结构化审计行（event=='trade'），按 trade_id 去重（保留首副本）。"""
    trades = {}
    for ln in lines:
        if " | " not in ln:
            continue
        payload = ln.split(" | ", 1)[1]
        if not payload.startswith("{"):
            continue
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            continue
        if obj.get("event") != "trade":
            continue
        tid = obj.get("trade_id")
        if tid and tid not in trades:        # keep-first = 去重（消解多实例重放副本）
            trades[tid] = obj
    return list(trades.values())


trades = parse_structured_trades(lines)          # 全量成交（去重后）
intent_lines_raw = sum(1 for ln in lines if re_intent.search(ln))  # 受门控的可读子集行数

# ----------------------------------------------------------------------------
# 2) 其余日志计数（F1-F4、F6）保持不变
# ----------------------------------------------------------------------------
starts, netfails, gates, stats, snaps = [], [], [], [], []
for ln in lines:
    m = re_ts.match(ln)
    t = m.group(1) if m else None
    if re_start.search(ln):
        starts.append(t); continue
    if re_netfail.search(ln):
        netfails.append(ln); continue
    g = re_gate.search(ln)
    if g:
        gates.append(dict(symbol=g.group(1), price=float(g.group(2)), p_up=float(g.group(3)),
                          exp_ret=float(g.group(4)), expected_pnl=float(g.group(5)),
                          round_trip_cost=float(g.group(6)), notional=float(g.group(7)),
                          min_ratio=float(g.group(8)))); continue
    s = re_stat.search(ln)
    if s:
        stats.append(s.group(1)); continue
    sp = re_snap.search(ln)
    if sp:
        snaps.append((sp.group(1), float(sp.group(2)))); continue

# --- 配对持仓时长（开仓入栈，平仓弹栈；按 ts 排序后处理） ---
open_stack = defaultdict(list)
holds = []          # (symbol, hold_sec, open_stop, close_stop)
stop_err = []       # 平仓止损与最近开仓止损不一致
last_open_stop = {}
for o in sorted(trades, key=lambda x: (_ts_dt(x) or datetime.min)):
    sym = o["symbol"]
    if _as_bool(o.get("is_open")):
        open_stack[sym].append(o)
        last_open_stop[sym] = o.get("stop")
    else:  # 平仓
        if open_stack[sym]:
            op = open_stack[sym].pop()
            a, b = _ts_dt(op), _ts_dt(o)
            sec = (b - a).total_seconds() if (a and b) else 0
            holds.append((sym, sec, op.get("stop"), o.get("stop")))
            if (op.get("stop") is not None and o.get("stop") is not None
                    and abs(float(op["stop"]) - float(o["stop"])) > 1e-6):
                stop_err.append(dict(symbol=sym, open_stop=op.get("stop"),
                                     close_stop=o.get("stop"), ts=o.get("ts")))
        elif (last_open_stop.get(sym) is not None and o.get("stop") is not None
              and abs(float(last_open_stop[sym]) - float(o.get("stop"))) > 1e-6):
            stop_err.append(dict(symbol=sym, open_stop=last_open_stop[sym],
                                 close_stop=o.get("stop"), ts=o.get("ts")))

# --- 价格静止检测（全量） ---
price_by_sym = defaultdict(list)
for o in trades:
    if o.get("price") is not None:
        price_by_sym[o["symbol"]].append(float(o["price"]))

# --- 手续费比（全量） ---
fee_open, fee_close = defaultdict(list), defaultdict(list)
for o in trades:
    bucket = fee_open if _as_bool(o.get("is_open")) else fee_close
    if o.get("fee") is not None:
        bucket[o["symbol"]].append(float(o["fee"]))

# --- 统计行解析（F6，保持原逻辑） ---
stat_nums = []
for s in stats:
    d = {}
    for kv in ["去重成交", "量", "费", "今平", "毛利", "净", "账户净", "闭合"]:
        mm = re.search(kv + r"\s*([-\d.]+)", s)
        if mm:
            d[kv] = float(mm.group(1))
    stat_nums.append((s[:10], d))

# --- 日级聚合（全量成交） ---
fills_by_day = Counter(str(o.get("ts", ""))[:10] for o in trades)
gate_days = Counter()
for ln in lines:
    if "成本门禁拦截开仓" in ln:
        m = re_ts.match(ln)
        if m:
            gate_days[m.group(1)[:10]] += 1
netfail_days = Counter()
for ln in netfails:
    m = re_ts.match(ln)
    if m:
        netfail_days[m.group(1)[:10]] += 1
start_days = Counter(s[:10] for s in starts if s)

# ----------------------------------------------------------------------------
# 3) 结果组装
# ----------------------------------------------------------------------------
result = dict(
    total_lines=len(lines),
    starts=len(starts), start_by_day=dict(start_days),
    netfails=len(netfails), netfail_by_day=dict(netfail_days),
    gates=len(gates), gate_by_day=dict(gate_days),
    # —— 成交级统计（全量 trade_id 去重口径）——
    trades_unique=len(trades),
    intent_lines_raw=intent_lines_raw,
    intent_coverage=round(intent_lines_raw / len(trades), 4) if trades else None,
    fills_by_day=dict(fills_by_day),
    by_action=dict(Counter(_action(o) for o in trades)),
    by_symbol=dict(Counter(o["symbol"] for o in trades)),
    paired_holds=len(holds),
    hold_sec_min=min((h[1] for h in holds), default=None),
    hold_sec_max=max((h[1] for h in holds), default=None),
    hold_sec_mean=round(statistics.mean([h[1] for h in holds]), 1) if holds else None,
    hold_le60=sum(1 for h in holds if h[1] <= 60),
    hold_le120=sum(1 for h in holds if h[1] <= 120),
    stop_error_count=len(stop_err), stop_error_sample=stop_err[:5],
    price_distinct={s: sorted(set(round(p, 4) for p in ps))[:12] for s, ps in price_by_sym.items()},
    price_n_distinct={s: len(set(round(p, 4) for p in ps)) for s, ps in price_by_sym.items()},
    fee_open_mean={s: round(statistics.mean(v), 4) for s, v in fee_open.items()},
    fee_close_mean={s: round(statistics.mean(v), 4) for s, v in fee_close.items()},
    fee_ratio={s: round(statistics.mean(fee_close[s]) / statistics.mean(fee_open[s]), 3)
               for s in fee_open if fee_close.get(s)},
    stat_sample=stat_nums[:6],
    snap_first=snaps[0] if snaps else None,
    snap_last=snaps[-1] if snaps else None,
    c0_fills=sum(1 for o in trades if o.get("symbol") == "c0"),
)
json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# 文本摘要
print("==== 交易记录审计摘要（trade_id 去重全量口径） ====")
print(f"日志总行数: {result['total_lines']}")
print(f"系统启动(重启)次数: {result['starts']}  按天: {result['start_by_day']}")
print(f"网络拉取失败次数: {result['netfails']}  按天: {result['netfail_by_day']}")
print(f"成本门禁拦截次数: {result['gates']}  按天: {result['gate_by_day']}")
print(f"全量去重成交(trade_id): {result['trades_unique']}  "
      f"[意图]可读行: {result['intent_lines_raw']}  可读覆盖: {result['intent_coverage']}")
print(f"成交按天: {result['fills_by_day']}")
print(f"动作分布: {result['by_action']}")
print(f"品种分布: {result['by_symbol']}")
print(f"配对持仓数: {result['paired_holds']}")
print(f"持仓时长(秒) min/max/mean: {result['hold_sec_min']}/{result['hold_sec_max']}/{result['hold_sec_mean']}")
print(f"持仓<=60s 笔数: {result['hold_le60']}  <=120s: {result['hold_le120']}")
print(f"止损错乱次数: {result['stop_error_count']}  样本: {result['stop_error_sample']}")
print(f"各品种成交价不同值数量: {result['price_n_distinct']}")
print(f"各品种成交价样本: {result['price_distinct']}")
print(f"开仓手续费均值: {result['fee_open_mean']}")
print(f"平仓手续费均值: {result['fee_close_mean']}")
print(f"平仓/开仓手续费比: {result['fee_ratio']}")
print(f"统计行样本: {result['stat_sample']}")
print(f"快照首/末: {result['snap_first']} / {result['snap_last']}")
print(f"c0 成交笔数: {result['c0_fills']}")
print(f"\n结果已写入: {OUT}")
