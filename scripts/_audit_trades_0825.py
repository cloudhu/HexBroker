#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HexBroker 模拟盘交易记录审计（聚焦 2026-08-25，含跨日数据质量交叉核对）。

解析 data/paper/trades.log：
  - [PAPER] 文本行（含启动/快照/重启/评估）
  - TRADE| 扁平行（legacy 格式）
  - {"event":"trade",...} JSON 行（权威成交记录，含 trade_id/entry/is_open）
输出结构化审计结论到 stdout + artifacts 下 JSON。
"""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(r"E:/Workspace/HexBroker")
LOG = ROOT / "data/paper/trades.log"
OUT = ROOT / "artifacts/audit_20260825"
OUT.mkdir(parents=True, exist_ok=True)

lines = LOG.read_text(encoding="utf-8").splitlines()

# 三类解析器
paper_msgs = []      # (date, time, raw)
trade_flat = []      # dict from TRADE| lines
trade_json = []      # dict from JSON event lines
restarts = []        # (ts, equity, positions)
snapshots = []       # (ts, equity, cash)

date_re = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) \|")
json_re = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| (\{.*\})$")

for ln in lines:
    m = date_re.match(ln)
    if not m:
        continue
    d, t = m.group(1), m.group(2)
    body = ln[m.end():].strip()
    if body.startswith("{"):
        try:
            obj = json.loads(body)
            if obj.get("event") == "trade":
                obj["_date"] = d; obj["_time"] = t
                trade_json.append(obj)
            else:
                # 其他 JSON 事件
                pass
        except Exception:
            pass
    elif body.startswith("TRADE|"):
        parts = body.split("|")
        # TRADE|ts|sym|side|qty|price|?|?|?|fee
        rec = dict(zip(
            ["tag","ts","sym","side","qty","price","c6","c7","c8","fee"],
            parts[:10]))
        rec["_date"] = d; rec["_time"] = t
        trade_flat.append(rec)
    else:
        paper_msgs.append((d,t,body))
        # 重启 / 快照 / 启动 检测
        if "账户快照已恢复" in body:
            em = re.search(r"equity=([\d.]+).*?positions=(\{.*?\})", body)
            pos = em.group(2) if em else "{}"
            restarts.append((f"{d} {t}", float(em.group(1)) if em else None, pos))
        elif "账户快照已保存" in body:
            em = re.search(r"equity=([\d.]+).*?cash=([\d.]+)", body)
            if em:
                snapshots.append((f"{d} {t}", float(em.group(1)), float(em.group(2))))

# ---------- 1) 08-25 权威成交（JSON event）----------
def is_0825(rec): return rec.get("_date") == "2026-08-25"
j25 = [r for r in trade_json if is_0825(r)]
j24 = [r for r in trade_json if r.get("_date") == "2026-08-24"]

# ---------- 2) 08-24 3x 重放检测 ----------
# 按 (ts, symbol, direction, qty, price) 去重，统计重复
def dedup_key(r):
    return (r.get("ts"), r.get("symbol"), r.get("direction"), r.get("qty"), r.get("price"), r.get("is_open"))
cnt24 = Counter(dedup_key(r) for r in j24)
dup_groups = {k:v for k,v in cnt24.items() if v > 1}
unique24 = len(cnt24)
raw24 = len(j24)

# ---------- 3) 重启 / 净值重置检测 ----------
# 同一交易日内出现多次“账户快照已恢复 equity=100000” 且中间有 MTM 亏损 -> 重置
reset_events = [r for r in restarts if r[1] is not None and abs(r[1]-100000.0) < 1e-6]

# ---------- 4) 08-25 逐笔 PnL 重建（基于 JSON event 的 entry/price/is_open）----------
# 简化：打印 08-25 每笔明细，标注 open/close
print("="*70)
print("HEXBROKER 模拟盘审计  |  目标日 2026-08-25")
print("="*70)
print(f"\n[日志总量] 行数={len(lines)}  JSON成交={len(trade_json)}  TRADE|扁平={len(trade_flat)}")
print(f"[08-25]  JSON成交笔数={len(j25)}   TRADE|笔数={len([t for t in trade_flat if t['_date']=='2026-08-25'])}")
print(f"[08-24]  JSON成交笔数={len(j24)}   去重后唯一={unique24}   重复组数={len(dup_groups)}  最大重复={max(cnt24.values()) if cnt24 else 0}")

print("\n" + "-"*70)
print("[08-25 成交明细] (trade_id | ts | sym | dir | qty | entry | price | stop | tp | is_open | fee)")
for r in j25:
    print(f"  {r.get('trade_id'):8s} {r.get('ts')} {r.get('symbol'):4s} "
          f"dir={r.get('direction'):+d} qty={r.get('qty')} entry={r.get('entry')} "
          f"px={r.get('price')} stop={r.get('stop')} tp={r.get('take_profit')} "
          f"open={r.get('is_open')} fee={r.get('fee')}")

print("\n" + "-"*70)
print("[重启 / 净值重置事件]")
for ts, eq, pos in restarts:
    flag = "  <-- 重置为初始净值" if (eq is not None and abs(eq-100000)<1e-6) else ""
    print(f"  {ts}  equity={eq}  positions={pos}{flag}")

print("\n" + "-"*70)
print("[08-25 净值快照序列(每30分钟抽样)]")
sk = [s for s in snapshots if s[0].startswith("2026-08-25")]
for i,s in enumerate(sk):
    if i % 6 == 0 or i==len(sk)-1:
        print(f"  {s[0]}  equity={s[1]:.2f}  cash={s[2]:.2f}")

# ---------- 保存 JSON ----------
report = {
    "target_date": "2026-08-25",
    "log_lines": len(lines),
    "json_trades_total": len(trade_json),
    "trade_flat_total": len(trade_flat),
    "d0825": {"json_trades": len(j25),
              "trade_flat": len([t for t in trade_flat if t['_date']=='2026-08-25'])},
    "d0824": {"json_trades_raw": raw24, "unique_after_dedup": unique24,
              "dup_groups": len(dup_groups), "max_repeat": max(cnt24.values()) if cnt24 else 0},
    "restarts": [{"ts":r[0],"equity":r[1],"positions":r[2]} for r in restarts],
    "trades_0825": j25,
}
(OUT/"audit_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\n[已保存] {OUT/'audit_report.json'}")
