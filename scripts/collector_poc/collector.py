# -*- coding: utf-8 -*-
"""HexBroker 稳定实时数据源 PoC —— 双通道采集服务（最小闭环）。

链路：Feed（推/拉）→ Gate（落地即校验）→ Sink（append-only 落湖 + 快照）→ Heartbeat（心跳）。

设计要点（对应数据源审计 P0/P1 教训）：
- 落地即校验：非 tick 整数倍 / OHLC 逻辑破 / 超涨跌停界的 bar 拒绝落湖（防 p43 型脏值）；
- append-only：只追加不覆盖，写前自动快照（防整区覆盖截断，P1-2）；
- 沙箱落点：写 data/collector_poc/，绝不触碰主湖 data/raw/processed/（防 P1-1 root 分裂污染）；
- 心跳文件：超过 stall_seconds 无心跳即视为断流（防 112 天静默缺失重演）。

用法：
  python scripts/collector_poc/collector.py --feed synthetic --sym ag0 --steps 60
  python scripts/collector_poc/collector.py --feed synthetic --sym ag0 --steps 60 --inject-bad
  python scripts/collector_poc/collector.py --feed tqsdk --sym ag0   # 需 tqsdk + 天勤账号
"""
from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SINK_ROOT = ROOT / "data/collector_poc"
SNAP_ROOT = ROOT / "data/collector_poc/_snapshots"
HEARTBEAT = ROOT / "artifacts/collector_poc_heartbeat.json"

# 品种最小变动价位（tick size）；未知品种跳过该项校验
TICK_SIZE = {"ag0": 1.0, "au0": 0.02, "m0": 0.5, "rb0": 1.0, "cu0": 10.0, "i0": 0.5}
# 日盘时段（全品种共用）
SESSIONS = ((9, 0, 10, 15), (10, 30, 11, 30), (13, 30, 15, 0))
# 夜盘时段（分钟区间 [start, end)，跨零点段豁免 weekend 检查——交易日归属前一工作日）
NIGHT_SESSIONS = {
    "ag0": ((21 * 60, 24 * 60), (0, 2 * 60 + 30)),
    "au0": ((21 * 60, 24 * 60), (0, 2 * 60 + 30)),
    "cu0": ((21 * 60, 24 * 60), (0, 1 * 60)),
    "m0": ((21 * 60, 23 * 60),),
    "i0": ((21 * 60, 23 * 60),),
    "rb0": ((21 * 60, 23 * 60),),
}


# ---------- Gate：落地即校验 ----------
class TickGate:
    def __init__(self, sym: str) -> None:
        self.sym = sym
        self.tick = TICK_SIZE.get(sym)
        self.rejected: list[dict] = []

    def check(self, bar: dict) -> tuple[bool, str]:
        p = bar["close"]
        if not (p > 0 and math.isfinite(p)):
            return False, "price_nonpositive"
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        if not (h >= max(o, c) and l <= min(o, c) and h >= l):
            return False, "ohlc_logic_broken"
        if self.tick is not None:
            for v in (o, h, l, c):
                if abs(v / self.tick - round(v / self.tick)) > 1e-6:
                    return False, f"not_tick_multiple(tick={self.tick})"
        dt = bar["datetime"]
        minutes = dt.hour * 60 + dt.minute
        for lo, hi in NIGHT_SESSIONS.get(self.sym, ()):  # 夜盘：跨零点豁免 weekend
            if lo <= minutes < hi:
                return True, "ok"
        if dt.weekday() >= 5:
            return False, "weekend"
        if not any(s0 * 60 + m0 <= minutes < s1 * 60 + m1
                   for s0, m0, s1, m1 in SESSIONS):
            return False, "outside_session"
        return True, "ok"


# ---------- Sink：append-only + 快照 + 心跳 ----------
class AppendSink:
    def __init__(self, sym: str) -> None:
        self.sym = sym
        self.dir = SINK_ROOT / sym / "1m"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.rows_written = 0
        self.rows_rejected = 0

    def write(self, bars: list[dict]) -> int:
        if not bars:
            return 0
        df_new = pd.DataFrame(bars).drop_duplicates(subset="datetime", keep="last")
        day = bars[0]["datetime"].strftime("%Y-%m-%d")
        part = self.dir / f"{day}.parquet"
        if part.exists():  # 写前快照（P1-2 防线）
            snap = SNAP_ROOT / self.sym / f"{day}.{datetime.now().strftime('%H%M%S')}.parquet"
            snap.parent.mkdir(parents=True, exist_ok=True)
            snap.write_bytes(part.read_bytes())
            df_old = pd.read_parquet(part)
            have = set(pd.to_datetime(df_old["datetime"]))
            df_new = df_new[~pd.to_datetime(df_new["datetime"]).isin(have)]
        n = 0
        if len(df_new):
            df_all = (pd.concat([pd.read_parquet(part), df_new], ignore_index=True)
                      if part.exists() else df_new)
            df_all = df_all.sort_values("datetime").reset_index(drop=True)
            df_all.to_parquet(part, index=False)
            n = len(df_new)
        self.rows_written += n
        return n

    def heartbeat(self, feed: str, last_ts, gate_rej: int) -> None:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(json.dumps({
            "feed": feed, "sym": self.sym, "last_bar": str(last_ts),
            "rows_written": self.rows_written, "rows_rejected": self.rows_rejected,
            "gate_rejected_total": gate_rej, "ts": datetime.now().isoformat(),
        }, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- Feed 1：synthetic（今日端到端验证用） ----------
def _anchor(now: datetime) -> datetime:
    """回溯到最近一个合法交易时段内的分钟（供 synthetic 锚定，周末/夜间也能跑）。"""
    dt = now.replace(second=0, microsecond=0)
    for _ in range(7 * 24 * 60):
        if dt.weekday() < 5 and any(s0 * 60 + m0 <= dt.hour * 60 + dt.minute < s1 * 60 + m1
                                    for s0, m0, s1, m1 in SESSIONS):
            return dt
        dt -= timedelta(minutes=1)
    raise RuntimeError("未找到合法时段锚点")


def synthetic_feed(sym: str, steps: int, inject_bad: bool = False):
    import numpy as np
    rng = np.random.default_rng(7)
    tick = TICK_SIZE.get(sym, 1.0)
    q = lambda v: round(round(v / tick) * tick, 6)  # 按品种 tick 量化
    base = q(5000.0)
    end = _anchor(datetime.now())
    for i in range(steps):
        ret = rng.normal(0, 0.0008)
        c = q(base * (1 + ret))
        o = base
        h = q(max(o, c) * (1 + abs(rng.normal(0, 0.0002))))
        l = q(min(o, c) * (1 - abs(rng.normal(0, 0.0002))))
        if h < max(o, c):
            h = max(o, c)
        if l > min(o, c):
            l = min(o, c)
        bar = {"datetime": end - timedelta(minutes=steps - 1 - i),
               "open": o, "high": h, "low": l, "close": c, "volume": float(rng.integers(1e3, 5e3))}
        if inject_bad and i == steps // 2:  # 注入脏 bar：非 tick 整数倍 + 破 OHLC
            bar.update(close=bar["close"] * 1.017 + 0.0314, high=bar["low"] - 5)
        yield bar
        base = c


# ---------- Feed 2：tqsdk（真实行情，天勤免费账号） ----------
TQ_SYMBOL = {"ag0": "KQ.m@SHFE.ag", "au0": "KQ.m@SHFE.au", "m0": "KQ.m@DCE.m",
             "rb0": "KQ.m@SHFE.rb", "cu0": "KQ.m@SHFE.cu", "i0": "KQ.m@DCE.i"}


def _load_auth() -> tuple[str, str]:
    import os
    u, p = os.environ.get("TQSDK_USER"), os.environ.get("TQSDK_PASS")
    if u and p:
        return u, p
    f = Path(__file__).parent / "tqsdk_auth.local.json"
    if f.exists():
        obj = json.loads(f.read_text(encoding="utf-8"))
        return obj["user"], obj["pass"]
    raise SystemExit("缺少天勤账号：设 TQSDK_USER/TQSDK_PASS 或创建 tqsdk_auth.local.json")


def _tq_bar(r) -> dict:
    return {"datetime": pd.to_datetime(int(r["datetime"]), unit="ns", utc=True)
            .tz_convert("Asia/Shanghai").tz_localize(None),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": float(r["volume"]) if pd.notna(r["volume"]) else 0.0}


def tqsdk_feed(sym: str, steps: int, seconds: int = 20):
    from tqsdk import TqApi, TqAuth
    user, pwd = _load_auth()
    contract = TQ_SYMBOL.get(sym)
    if not contract:
        raise SystemExit(f"未配置 {sym} 的天勤合约映射（TQ_SYMBOL）")
    api = TqApi(auth=TqAuth(user, pwd))
    try:
        klines = api.get_kline_serial(contract, 60, data_length=steps)
        for _, r in klines.iterrows():  # 先吐历史快照（周末/收盘也有）
            if pd.notna(r["datetime"]) and int(r["datetime"]) > 0:
                yield _tq_bar(r)
        deadline = time.time() + seconds  # 再流式收新 bar（交易时段才有）
        while time.time() < deadline:
            # 官方契约（2026-08-30 文档核对）：deadline 模式下必须以 wait_update
            # 返回值判定是否有更新；到期返回 False 时不得再做 is_changing 判断
            if not api.wait_update(deadline=deadline):
                break
            if api.is_changing(klines.iloc[-1], "datetime"):
                r = klines.iloc[-1]
                if pd.notna(r["datetime"]) and int(r["datetime"]) > 0:
                    yield _tq_bar(r)
    finally:
        api.close()


# ---------- 主循环 ----------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed", choices=["synthetic", "tqsdk"], default="synthetic")
    ap.add_argument("--sym", default="ag0")
    ap.add_argument("--steps", type=int, default=60)
    ap.add_argument("--inject-bad", action="store_true", help="synthetic 注入脏 bar 验证门禁")
    ap.add_argument("--seconds", type=int, default=20, help="tqsdk 流式收包窗口（秒）")
    a = ap.parse_args()

    feed = synthetic_feed(a.sym, a.steps, a.inject_bad) if a.feed == "synthetic" \
        else tqsdk_feed(a.sym, a.steps, a.seconds)
    gate, sink = TickGate(a.sym), AppendSink(a.sym)
    ok_bars, last_ts = [], None
    for bar in feed:
        good, reason = gate.check(bar)
        if good:
            ok_bars.append(bar)
            last_ts = bar["datetime"]
        else:
            sink.rows_rejected += 1
            gate.rejected.append({"ts": str(bar["datetime"]), "close": bar["close"], "why": reason})
            if sink.rows_rejected <= 3:
                print(f"[GATE-REJECT] {bar['datetime']} close={bar['close']} 原因={reason}")
    n = sink.write(ok_bars)
    sink.heartbeat(a.feed, last_ts, len(gate.rejected))
    print(f"[RUN] feed={a.feed} sym={a.sym} bars_in={a.steps} gate_rejected={len(gate.rejected)} "
          f"written={n} total_rows={sink.rows_written}")
    print(f"[HB] 心跳 -> {HEARTBEAT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
