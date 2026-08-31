# -*- coding: utf-8 -*-
"""统一数据源模块验证：sina vs tqsdk 历史真实数据逐位比对 + 网络可靠性实测。

关注两点（主理人 2026-08-30 裁决）：
① 数据真实性 —— 两个独立通道各自拉主力连续日线，OHLC 逐位比对（期货原始价无复权，
   真实数据必须逐位一致；差异即至少一个通道在撒谎）；
② 网络可靠性 —— sina 日线 5 轮拉取 + 实时快照 20 次轮询；tqsdk 3 次完整会话
   （连接→认证→拉取→关闭），统计成功率与延迟。

运行：envs/default/Scripts/python.exe scripts/verify_dual_source.py
"""
from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hexbroker.data.sources.sina_source import SinaSource  # noqa: E402
from hexbroker.data.sources.tqsdk_source import TqsdkSource  # noqa: E402

SYMBOLS = ["ag0", "rb0", "m0", "cu0"]  # 三污染品种 + cu0 健康对照
END = "2026-08-28"                      # 上周五（最后交易日）
START = "2026-05-01"
REPORT = ROOT / "artifacts/dual_source_verification_20260830.md"

CMP_COLS = ["open", "high", "low", "close"]
TICK = {"ag0": 1.0, "rb0": 1.0, "m0": 0.5, "cu0": 10.0}

lines: list[str] = []
log = print


def section(t: str) -> None:
    lines.extend(["", f"## {t}", ""])


# ---------- ① 历史真实数据逐位比对 ----------
section("① sina vs tqsdk 主力连续日线逐位比对")
log(f"[1] 拉取 sina {SYMBOLS} {START}~{END} ...")
sina = SinaSource(root="data/raw", save=False)
t0 = time.time()
bf_sina = sina.fetch_bars(SYMBOLS, START, END, "1d")
t_sina = time.time() - t0
log(f"    sina: {bf_sina.length} 行, {t_sina:.2f}s")
log("[2] 拉取 tqsdk 同区间 ...")
tq = TqsdkSource()
t0 = time.time()
bf_tq = tq.fetch_bars(SYMBOLS, START, END, "1d")
t_tq = time.time() - t0
log(f"    tqsdk: {bf_tq.length} 行, {t_tq:.2f}s")

section("逐位比对结果")
lines += ["| 品种 | sina行数 | tqsdk行数 | 共同交易日 | OHLC不一致 | volume不一致 | 判定 |",
          "|---|---|---|---|---|---|---|"]
all_ok = True
mismatch_detail: list[str] = []
for sym in SYMBOLS:
    ds = bf_sina.by_symbol(sym).droplevel(0)
    dt = bf_tq.by_symbol(sym).droplevel(0)
    common = ds.index.intersection(dt.index)
    n_bad_ohlc, n_bad_vol = 0, 0
    for d in common:
        rs, rt = ds.loc[d], dt.loc[d]
        for c in CMP_COLS:
            if abs(float(rs[c]) - float(rt[c])) > 1e-6:
                n_bad_ohlc += 1
                if len(mismatch_detail) < 8:
                    mismatch_detail.append(
                        f"{sym} {d.date()} {c}: sina={float(rs[c])} tqsdk={float(rt[c])}")
                break
        else:
            if abs(float(rs["volume"]) - float(rt["volume"])) > 1e-6:
                n_bad_vol += 1
    ok = len(common) > 0 and n_bad_ohlc == 0
    all_ok &= ok
    lines.append(f"| {sym} | {len(ds)} | {len(dt)} | {len(common)} | {n_bad_ohlc} | "
                 f"{n_bad_vol} | {'✅ 逐位一致' if ok else '⛔ 存在差异'} |")
if mismatch_detail:
    lines += ["", "差异明细（前 8 条）：", "```", *mismatch_detail, "```"]

# ---------- ② 网络可靠性 ----------
section("② 网络可靠性实测")
lines += ["| 通道 | 测试 | 次数 | 成功 | 平均延迟 | 判定 |", "|---|---|---|---|---|---|"]

# sina 日线 5 轮
ok_n, lat = 0, []
for i in range(5):
    t0 = time.time()
    try:
        sina.fetch_bars(["ag0"], START, END, "1d")
        lat.append(time.time() - t0)
        ok_n += 1
    except Exception as e:
        log(f"    sina 轮 {i+1} 失败: {e}")
rel = "✅ 稳定" if ok_n == 5 else ("🟡 部分失败" if ok_n >= 3 else "🔴 不稳定")
lines.append(f"| sina | 日线拉取 | 5 | {ok_n} | {sum(lat)/len(lat):.2f}s（成功轮） | {rel} |")

# sina 实时快照 20 次（模拟盘同款通道）
try:
    from hexbroker.paper.quotes import RealTimeQuoteClient
    qc = RealTimeQuoteClient(symbols={"ag0": "nf_AG0"})
    ok_q, lat_q = 0, []
    for _ in range(20):
        t0 = time.time()
        try:
            qs = qc.fetch_quotes(["ag0"])
            if qs and qs["ag0"].price > 0:
                ok_q += 1
            lat_q.append(time.time() - t0)
        except Exception:
            lat_q.append(time.time() - t0)
        time.sleep(0.2)
    rel = "✅ 稳定" if ok_q == 20 else ("🟡 部分失败" if ok_q >= 16 else "🔴 不稳定")
    lines.append(f"| sina | 实时快照轮询 | 20 | {ok_q} | {sum(lat_q)/len(lat_q):.2f}s | {rel} |")
except Exception as e:
    lines.append(f"| sina | 实时快照轮询 | - | - | - | ⛔ 模块不可用: {e} |")

# tqsdk 3 次完整会话
ok_t, lat_t = 0, []
for i in range(3):
    t0 = time.time()
    try:
        TqsdkSource().fetch_bars(["ag0"], START, END, "1d")
        lat_t.append(time.time() - t0)
        ok_t += 1
    except Exception as e:
        log(f"    tqsdk 会话 {i+1} 失败: {e}")
rel = "✅ 稳定" if ok_t == 3 else ("🟡 部分失败" if ok_t >= 2 else "🔴 不稳定")
lines.append(f"| tqsdk | 完整会话(连接+认证+拉取) | 3 | {ok_t} | "
             f"{sum(lat_t)/len(lat_t):.2f}s（成功轮） | {rel} |")

# ---------- 汇总 ----------
lines += ["", "---", "",
          f"**总判定**：{'✅ 双通道数据真实性成立（逐位一致）' if all_ok else '⛔ 双通道存在数据差异，需归因'}；"
          f"网络可靠性见上表。", "",
          f"*生成：{date.today()} · sina {t_sina:.2f}s / tqsdk {t_tq:.2f}s（4 品种 1d 首拉）*"]

REPORT.write_text("\n".join(lines), encoding="utf-8")
log(f"\n[OK] 报告 -> {REPORT}")
log("\n".join(lines[:40]))
sys.exit(0 if all_ok else 1)
