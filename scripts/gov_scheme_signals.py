"""P2-D 信号管线·HexBroker 侧写入器（数据契约消费端，云侠侧零耦合）。

用途：把方案级绩效/质量信号写入 data/governance/scheme_signals.json，
供 RuntimeDegrader（governance/degrade.py）运行时观测。云侠侧未来只需按
docs/scheme-signals-contract.md 导出约定格式数据（CSV/手工），无需改产品代码。

写入格式（与 degrade.read_signals_file 对齐）：
    {"generated_at": "<ISO8601>", "signals": {"1C": {"rolling_wr": 0.40, "n": 25}}}

用法：
    python scripts/gov_scheme_signals.py --set 1C rolling_wr 0.40 --n 25
    python scripts/gov_scheme_signals.py --csv signals.csv        # 列: scheme,metric,value,n
    python scripts/gov_scheme_signals.py --show                   # 查看当前
    python scripts/gov_scheme_signals.py --clear 1C               # 移除单方案
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SIGNALS_PATH = ROOT / "data" / "governance" / "scheme_signals.json"
VALID_METRICS = {"rolling_wr", "rolling_maxdd", "oos_wr", "data_quality"}  # 契约白名单（可扩展）


def load() -> Dict[str, Dict[str, Any]]:
    if not SIGNALS_PATH.exists():
        return {}
    try:
        data = json.loads(SIGNALS_PATH.read_text(encoding="utf-8"))
        return data.get("signals") or {}
    except Exception as e:
        print(f"[warn] 现有信号文件解析失败，按空处理：{e}")
        return {}


def save_atomic(signals: Dict[str, Dict[str, Any]]) -> None:
    SIGNALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"), "signals": signals}
    tmp = SIGNALS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, SIGNALS_PATH)


def validate(scheme: str, metric: str, value: float, n: int) -> List[str]:
    errs = []
    if not scheme:
        errs.append("scheme 为空")
    if metric not in VALID_METRICS:
        errs.append(f"metric '{metric}' 不在契约白名单 {sorted(VALID_METRICS)}")
    if value != value:  # NaN
        errs.append("value 为 NaN")
    if n < 0:
        errs.append(f"n={n} 为负")
    return errs


def merge_one(signals: Dict[str, Dict[str, Any]], scheme: str, metric: str,
              value: float, n: int) -> None:
    sig = signals.setdefault(scheme, {})
    sig[metric] = float(value)
    sig["n"] = int(n)  # 样本量随最新一次写入覆盖（护栏消费端 min_n 用）


def from_csv(path: str) -> List[Tuple[str, str, float, int]]:
    rows: List[Tuple[str, str, float, int]] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            try:
                rows.append((row["scheme"].strip(), row["metric"].strip(),
                             float(row["value"]), int(row["n"])))
            except (KeyError, ValueError) as e:
                print(f"[warn] CSV 第 {i} 行字段非法，跳过：{row}（{e}）")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="P2-D 治理信号写入器（契约见 docs/scheme-signals-contract.md）")
    ap.add_argument("--set", nargs=3, metavar=("SCHEME", "METRIC", "VALUE"), action="append")
    ap.add_argument("--n", type=int, default=0, help="最近一次 --set 的样本量")
    ap.add_argument("--csv", help="批量导入：列 scheme,metric,value,n")
    ap.add_argument("--clear", metavar="SCHEME")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    if args.show:
        print(json.dumps({"generated_at": (
            json.loads(SIGNALS_PATH.read_text(encoding="utf-8")).get("generated_at")
            if SIGNALS_PATH.exists() else None), "signals": load()},
            ensure_ascii=False, indent=2))
        return 0

    signals = load()
    changed = 0
    if args.clear:
        if signals.pop(args.clear, None) is not None:
            changed += 1

    pending: List[Tuple[str, str, float, int]] = []
    for triple in args.set or []:
        scheme, metric, raw = triple
        try:
            pending.append((scheme.strip(), metric.strip(), float(raw), int(args.n)))
        except ValueError:
            print(f"[err] --set 值非法：{triple}")
            return 1
    if args.csv:
        pending.extend(from_csv(args.csv))

    for scheme, metric, value, n in pending:
        errs = validate(scheme, metric, value, n)
        if errs:
            print(f"[err] {scheme}.{metric}: " + "; ".join(errs))
            return 1
        merge_one(signals, scheme, metric, value, n)
        changed += 1

    if changed:
        save_atomic(signals)
        print(f"[ok] 已写入 {changed} 条 → {SIGNALS_PATH}")
    else:
        print("[noop] 无变更")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
