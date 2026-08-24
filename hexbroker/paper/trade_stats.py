"""交易审计日志统计（去重 + FIFO 已实现盈亏重算）。

被两处复用：

- ``scripts/analyze_trades.py``：CLI 离线分析（默认上午 / 可全日的统计报告）。
- ``hexbroker.paper.reporter.ReviewReporter``：每日复盘报告「八、当日交易统计（日志去重）」段。

设计要点：

- 解析 ``trades.log`` 的结构化审计行（``" | "`` 切分后 json.loads）。
- 按 ``trade_id`` 去重，规避**会话重放 3× 伪增**（多实例并发 append 同一历史行情流）。
- FIFO 重算各品种已实现盈亏（毛利），总手续费单独累加；``净亏 = 毛利 − 手续费``，
  可与账户快照 ``realized``（净盈亏）做逐品种闭合校验。
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, date
from pathlib import Path
from typing import Any, Optional

# 合约乘数（由手续费反推：ag0 平今 25.344 = 16896.01*15*0.0001；rb0 开多 1.519 = 3038*10*0.00005）
MULT = {"ag0": 15, "rb0": 10, "au0": 1000, "m0": 10, "cu0": 5, "i0": 100, "hc0": 10}
_AUDIT = {"trade", "plan_change"}


def parse_audit_lines(path: str | Path) -> tuple[list[dict], list[dict]]:
    """稳健解析：按 ' | ' 切分，取后半段 json.loads；返回 (trades, plan_changes)。"""
    trades: list[dict] = []
    plans: list[dict] = []
    p = Path(path)
    if not p.exists():
        return trades, plans
    for raw in p.read_text(encoding="utf-8").splitlines():
        if " | " not in raw:
            continue
        head, _, payload = raw.partition(" | ")
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict) or obj.get("event") not in _AUDIT:
            continue
        # P2：解析层归一化布尔字段（历史日志 is_open 偶发字符串化 "False"/"True"，
        # 统一为 bool，保证下游类型一致）
        obj["is_open"] = _norm_bool(obj.get("is_open"))
        obj["is_today_close"] = _norm_bool(obj.get("is_today_close"))
        # 行首 head 即日志写出时刻，作为第三时间戳参考
        obj["_written_at"] = head.strip()
        if obj["event"] == "trade":
            trades.append(obj)
        else:
            plans.append(obj)
    return trades, plans


def dedup_trades(trades: list[dict]) -> tuple[list[dict], dict]:
    """按 trade_id 去重（保留首个副本）；返回 (unique, stats)。"""
    seen: dict[Any, dict] = {}
    copy_count: Counter = Counter()
    nonidentical = 0
    for t in trades:
        tid = t.get("trade_id")
        copy_count[tid] += 1
        if tid not in seen:
            seen[tid] = t
        else:
            # 除 ts / _written_at 外应逐字节一致
            a = {k: v for k, v in seen[tid].items() if k not in ("ts", "_written_at")}
            b = {k: v for k, v in t.items() if k not in ("ts", "_written_at")}
            if a != b:
                nonidentical += 1
    return list(seen.values()), {
        "raw": len(trades),
        "unique": len(seen),
        "copy_dist": dict(Counter(copy_count.values())),
        "nonidentical_copies": nonidentical,
    }


def _norm_bool(v: Any) -> bool:
    """归一化为 bool：原生 bool 原样；字符串/数字按 truthy 规则解析（历史兼容）。"""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("true", "1")


def _is_open(ev: dict) -> bool:
    v = ev.get("is_open")
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1")


def action_label(ev: dict) -> str:
    d = int(ev.get("direction", 0))
    if _is_open(ev):
        return "开多" if d > 0 else "开空"
    return "平多" if d < 0 else "平空"


def fifo_realized(unique: list[dict]) -> dict[str, float]:
    """逐品种 FIFO 重算已实现盈亏（毛利），交叉验证 account.json。"""
    stacks: dict[str, list] = defaultdict(list)  # symbol -> [[open_price, signed_qty], ...]
    realized: dict[str, float] = defaultdict(float)
    for ev in unique:
        sym = ev["symbol"]
        mult = MULT.get(sym, 10)
        price = float(ev["price"])
        signed_qty = float(ev["qty"])
        if _is_open(ev):
            stacks[sym].append([price, signed_qty])
        else:
            # 平仓：signed_qty 与开仓相反符号
            remaining = abs(signed_qty)
            while remaining > 1e-9 and stacks[sym]:
                lot = stacks[sym][0]
                open_price, open_qty = lot
                m = min(remaining, abs(open_qty))
                if open_qty > 0:  # 平多：卖出
                    realized[sym] += (price - open_price) * m * mult
                else:  # 平空：买回
                    realized[sym] += (open_price - price) * m * mult
                open_qty -= (m if open_qty > 0 else -m)
                remaining -= m
                if abs(open_qty) < 1e-9:
                    stacks[sym].pop(0)
    return dict(realized)


def _parse_ts(ev: dict) -> Optional[datetime]:
    ts = str(ev.get("ts", "")).split(".")[0]
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _load_acct_realized(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        r = data.get("realized")
        return dict(r) if isinstance(r, dict) else None
    except Exception:
        return None


def analyze_trades_log(
    log_path: str | Path,
    day: date | str,
    until_time: Optional[str] = None,
    account_json_path: Optional[str] = None,
) -> dict[str, Any]:
    """解析并去重统计某日成交，返回结构化结果（供 CLI / 复盘报告共用）。

    ``day`` 形如 ``date(...)`` 或 ``"2026-08-24"``；``until_time`` 形如 ``"11:30:00"``
    （默认当日 23:59:59，即全天空窗）。``account_json_path`` 提供则做闭合校验。
    """
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    d = day if isinstance(day, date) else datetime.strptime(day_s, "%Y-%m-%d").date()
    since_dt = datetime(d.year, d.month, d.day, 0, 0, 0)
    if until_time:
        until_dt = datetime.strptime(day_s + " " + until_time, "%Y-%m-%d %H:%M:%S")
    else:
        until_dt = datetime(d.year, d.month, d.day, 23, 59, 59)

    out: dict[str, Any] = {
        "exists": Path(log_path).exists(),
        "day": day_s,
        "raw_count": 0,
        "unique_count": 0,
        "copy_dist": {},
        "nonidentical": 0,
        "trades": [],
        "filtered_count": 0,
        "sym_counter": {},
        "act_counter": {},
        "today_close": 0,
        "total_fee": 0.0,
        "total_vol": 0.0,
        "hourly": {},
        "fifo_gross": {},
        "fee_by_sym": {},
        "acct_realized": None,
    }
    if not out["exists"]:
        return out

    raw_trades, _ = parse_audit_lines(log_path)
    unique, dd = dedup_trades(raw_trades)
    out["raw_count"] = dd["raw"]
    out["unique_count"] = dd["unique"]
    out["copy_dist"] = dd["copy_dist"]
    out["nonidentical"] = dd["nonidentical_copies"]

    am = [t for t in unique if (lambda dt: dt is not None and since_dt <= dt <= until_dt)(_parse_ts(t))]
    out["trades"] = am
    out["filtered_count"] = len(am)

    sym_counter = Counter(t["symbol"] for t in am)
    act_counter = Counter(action_label(t) for t in am)
    today_close = sum(1 for t in am if t.get("is_today_close"))
    total_fee = sum(float(t.get("fee", 0)) for t in am)
    total_vol = sum(abs(float(t.get("qty", 0))) for t in am)
    hourly = Counter(str(t.get("ts", ""))[11:13] for t in am)
    fifo = fifo_realized(am)
    fee_by_sym: dict[str, float] = defaultdict(float)
    for t in am:
        fee_by_sym[t["symbol"]] += float(t.get("fee", 0))

    out.update({
        "sym_counter": dict(sym_counter),
        "act_counter": dict(act_counter),
        "today_close": today_close,
        "total_fee": total_fee,
        "total_vol": total_vol,
        "hourly": dict(hourly),
        "fifo_gross": fifo,
        "fee_by_sym": dict(fee_by_sym),
        "acct_realized": _load_acct_realized(account_json_path),
    })
    return out


def summarize_daily(result: dict[str, Any]) -> str:
    """单行中文摘要（盘中监控/账户快照时定时输出，便于运维 grep）。

    示例::

        "2026-08-24 去重成交 256 笔 | 量 256 手 | 费 ¥2724.54 | 今平 128 |
         毛利 -1299.20 | 净 -4023.74 | 账户净 -4023.74 | 闭合 +0.0000"
    """
    if not result.get("exists", False):
        return "审计日志不存在，无法统计"
    n = result.get("filtered_count", 0)
    if n == 0:
        return f"{result.get('day', '')} 当日暂无成交"
    parts = [
        f"{result['day']} 去重成交 {n} 笔",
        f"量 {result['total_vol']:.0f} 手",
        f"费 ¥{result['total_fee']:.2f}",
        f"今平 {result.get('today_close', 0)}",
    ]
    gross = sum(result.get("fifo_gross", {}).values())
    net = gross - result.get("total_fee", 0.0)
    parts.append(f"毛利 {gross:+.2f}")
    parts.append(f"净 {net:+.2f}")
    acct = result.get("acct_realized")
    if acct:
        tot_a = sum(acct.values())
        parts.append(f"账户净 {tot_a:+.2f}")
        parts.append(f"闭合 {net - tot_a:+.4f}")
    return " | ".join(parts)


# 盘中告警：闭合偏差容忍阈值（元）。超过视为「日志去重净盈亏与账户快照不一致」。
ALERT_CLOSURE_TOL = 0.01


def daily_stats_alerts(result: dict[str, Any]) -> list[str]:
    """由统计结果生成告警列表（空 = 无异常）。

    当前规则：
    - 闭合偏差（日志净 = FIFO 毛利 − 手续费，对比账户快照 realized）|偏差| > ``ALERT_CLOSURE_TOL``；
    - 副本间字段不一致条数 > 0（数据质量异常，通常意味着不同会话写了不同内容的同 id 成交）。
    """
    alerts: list[str] = []
    nonidentical = result.get("nonidentical", 0)
    if nonidentical:
        alerts.append(
            f"副本间字段不一致 {nonidentical} 条：同 trade_id 的副本除 ts 外字段不同，"
            "疑似多会话写入冲突，请核查数据质量"
        )
    acct = result.get("acct_realized")
    if acct:
        net = sum(result.get("fifo_gross", {}).values()) - result.get("total_fee", 0.0)
        tot_a = sum(acct.values())
        dev = net - tot_a
        if abs(dev) > ALERT_CLOSURE_TOL:
            alerts.append(
                f"闭合偏差 {dev:+.4f}（|偏差|>{ALERT_CLOSURE_TOL}）："
                "日志去重净盈亏与账户快照不一致，请核查成交/费用口径"
            )
    return alerts


def render_daily_stats_markdown(result: dict[str, Any], acct_realized: Optional[dict] = None) -> str:
    """渲染「当日交易统计（日志去重）」内容（不含一级标题，由调用方加 ``## 八、...``）。

    返回 markdown 段落；``acct_realized`` 优先于 ``result['acct_realized']`` 做闭合校验。
    """
    if not result.get("exists", False):
        return "（当日审计日志 `trades.log` 不存在，跳过日志去重统计；明细见「三、当日交易明细」）\n"

    acct_realized = acct_realized if acct_realized is not None else result.get("acct_realized")
    L: list[str] = []
    L.append(f"- 审计日志原始成交行：**{result['raw_count']}** → 按 `trade_id` 去重后唯一成交：**{result['unique_count']}** 笔")
    if result["copy_dist"]:
        L.append(f"- 每 id 副本分布：{result['copy_dist']}（≈3× 会话重放；已去重）")
    L.append(f"- 当日（{result['day']}）过滤后成交：**{result['filtered_count']}** 笔")
    L.append(f"- 总成交量（∑|qty|）：**{result['total_vol']:.0f}** 手；总手续费：**¥{result['total_fee']:.2f}**")
    L.append(f"- 今平（is_today_close=True）：**{result['today_close']}** 笔\n")

    # 分时分布
    if result["hourly"]:
        L.append("| 小时 | 成交笔数 |")
        L.append("|---|---|")
        for h in sorted(result["hourly"]):
            L.append(f"| {h}:00 | {result['hourly'][h]} |")
        L.append("")

    # 动作分布
    L.append("| 动作 | 笔数 |")
    L.append("|---|---|")
    for k in ["开多", "开空", "平多", "平空"]:
        L.append(f"| {k} | {result['act_counter'].get(k, 0)} |")
    L.append("")

    # 品种维度 + 盈亏闭合
    L.append("| 品种 | 成交 | 开仓 | 平仓 | 今平 | FIFO毛利 | 手续费 | 净亏(毛利−费) | 账户净盈亏 | 闭合偏差 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    sym_open = Counter()
    sym_close = Counter()
    for t in result["trades"]:
        if _is_open(t):
            sym_open[t["symbol"]] += 1
        else:
            sym_close[t["symbol"]] += 1
    for sym in sorted(result["sym_counter"]):
        tc = result["sym_counter"][sym]
        sc = sum(1 for t in result["trades"] if t["symbol"] == sym and t.get("is_today_close"))
        gross = result["fifo_gross"].get(sym, 0.0)
        fee = result["fee_by_sym"].get(sym, 0.0)
        net = gross - fee
        a = acct_realized.get(sym, 0.0) if acct_realized else None
        if a is not None:
            L.append(
                f"| {sym} | {tc} | {sym_open[sym]} | {sym_close[sym]} | {sc} | "
                f"{gross:+.2f} | {fee:.2f} | {net:+.2f} | {a:+.2f} | {net - a:+.4f} |"
            )
        else:
            L.append(
                f"| {sym} | {tc} | {sym_open[sym]} | {sym_close[sym]} | {sc} | "
                f"{gross:+.2f} | {fee:.2f} | {net:+.2f} | - | - |"
            )
    tot_gross = sum(result["fifo_gross"].values())
    tot_fee = result["total_fee"]
    tot_net = tot_gross - tot_fee
    tot_a = sum(acct_realized.values()) if acct_realized else None
    if tot_a is not None:
        L.append(
            f"| **合计** | **{result['filtered_count']}** | - | - | **{result['today_close']}** | "
            f"**{tot_gross:+.2f}** | **{tot_fee:.2f}** | **{tot_net:+.2f}** | **{tot_a:+.2f}** | "
            f"**{tot_net - tot_a:+.4f}** |"
        )
        L.append("")
        L.append("> 闭合偏差≈0 表明日志去重成交集与账户快照清算逐品种一致；净亏 = FIFO 毛利 − 手续费。")
    else:
        L.append(
            f"| **合计** | **{result['filtered_count']}** | - | - | **{result['today_close']}** | "
            f"**{tot_gross:+.2f}** | **{tot_fee:.2f}** | **{tot_net:+.2f}** | - | - |"
        )
        L.append("")
        L.append("> 净亏 = FIFO 毛利 − 手续费（未提供账户快照，未做闭合校验）。")
    L.append("")
    return "\n".join(L)
