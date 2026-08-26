#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P步-A/B 固化应用器：把一批 persisted JSON（来自 PandaData close_pcr MCP 拉取）
确定性地融合进 18 品种日线并重建信号缓存尾折。

设计
----
- 输入目录 `artifacts/p6_4_pull_{YYYYMMDD}/` 下每个 `<sym0>.json`：
    {"result": {"type":"dataframe","columns":[...],"rows":[[...]]}}
  （直接由 pandadata MCP `get_future_daily_post(method=close_pcr)` 的返回值落盘，无需转换）
- 本脚本对每个文件：子集到安全列 → 从数据日期推导 seg(YYYYMMDD_YYYYMMDD)
  → 调用 `p6_4_fill_gaps.py --stage parse` → 写回分片（p6_4 内部已做备份/原子写/重叠校验）
- 全部品种 parse 完成后，调用 `p22_tail_ext.py` 重建尾折信号缓存（延伸至最新交易日）

为何存在
------
原刷新自动化 Step 2 走 `fetch_data.py --source akshare`（仅 SHFE.cu 且 raw sina 口径错）
或 `p22_tail_ext.py --skip-eval`（只重建、不拉新数据），缓存永远停在最后手动日期 → fd 恒>0。
本脚本 + 自动化中的 pandadata MCP 拉取，构成真正能让 fd=0 的 P步-A/B 管线。

用法
----
  python scripts/p6_4_apply_persisted_dir.py --dir artifacts/p6_4_pull_20260825
  python scripts/p6_4_apply_persisted_dir.py --dir artifacts/p6_4_pull_20260825 --skip-p22
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
P6_4 = PROJECT_ROOT / "scripts" / "p6_4_fill_gaps.py"
P22 = PROJECT_ROOT / "scripts" / "p22_tail_ext.py"

# p6_4_append_20260824 已验证可用的列子集（pandadata 返回的超集含这些，安全降维）
SAFE_COLS = [
    "date", "underlying_symbol", "open", "high", "low", "close",
    "volume", "open_interest",
]


def _resolve_result(raw: dict) -> dict:
    """兼容 {result:{...}} / 裸 {type,columns,rows} / {rows,columns}。"""
    if isinstance(raw, dict):
        if "result" in raw and isinstance(raw["result"], dict):
            return raw["result"]
        if "rows" in raw or "columns" in raw:
            return raw
    raise ValueError(f"无法识别的 persisted 结构，键: {list(raw.keys()) if isinstance(raw, dict) else type(raw)}")


def _load_df(payload: dict) -> "pd.DataFrame":
    import pandas as pd
    if payload.get("type") == "dataframe" or ("columns" in payload and "rows" in payload):
        cols = payload.get("columns") or []
        rows = payload.get("rows") or []
        return pd.DataFrame(rows, columns=cols)
    if "data" in payload:
        return pd.DataFrame(payload["data"])
    raise ValueError(f"result 结构无法识别: {list(payload.keys())}")


def _seg_from_dates(df) -> str:
    dates = df["date"].astype(str).tolist()
    return f"{min(dates)}_{max(dates)}"


def main() -> int:
    ap = argparse.ArgumentParser(description="P步-A/B 固化应用器")
    ap.add_argument("--dir", required=True, help="含 <sym0>.json 的 persisted 目录")
    ap.add_argument("--sym0-map", default=None,
                    help="可选 sym0 文件名前缀→实际 sym0 映射（默认用文件名 stem）")
    ap.add_argument("--skip-p22", action="store_true", help="跳过 p22_tail_ext 重建（仅做 K 线融合）")
    ap.add_argument("--python",
                    default=r"C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe",
                    help="python 解释器（默认 managed venv；缺失时回退 sys.executable）")
    args = ap.parse_args()

    d = Path(args.dir)
    if not d.is_dir():
        print(f"[FAIL] 目录不存在: {d}")
        return 2

    json_files = sorted(p for p in d.glob("*.json") if not p.name.endswith(".clean.json"))
    if not json_files:
        print(f"[WARN] 目录无 *.json（排除 .clean.json）: {d}（无新数据可融合，属预期）")
        return 0

    py = args.python
    if not Path(py).exists():
        py = sys.executable
        print(f"[INFO] 默认 python 不存在，回退 sys.executable: {py}")
    ok, fail = [], []
    for fp in json_files:
        sym0 = fp.stem  # 文件名即 sym0（ag0/al0/...）
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
            res = _resolve_result(raw)
            df = _load_df(res)
            # 安全降维：仅保留 p6_4 验证过的列（不存在则跳过该列）
            keep = [c for c in SAFE_COLS if c in df.columns]
            df = df[keep]
            if "date" not in df.columns or df.empty:
                print(f"[SKIP] {sym0}: 无有效日期列或空数据")
                continue
            seg = _seg_from_dates(df)
            # 写清理后的 persisted 文件到独立子目录（避免污染输入目录被重复 glob）
            clean_dir = d / "_clean"
            clean_dir.mkdir(exist_ok=True)
            clean = clean_dir / f"{sym0}.json"
            clean.write_text(
                json.dumps({"result": {"type": "dataframe",
                                        "columns": list(df.columns),
                                        "rows": df.astype(object).where(df.notna(), None).values.tolist()}},
                           ensure_ascii=False),
                encoding="utf-8",
            )
            cmd = [py, str(P6_4), "--stage", "parse", str(clean), "--sym", sym0,
                   "--seg", seg, "--force"]
            print(f"\n[PARSE] {sym0} seg={seg} rows={len(df)}")
            r = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True)
            if r.returncode != 0:
                print(r.stdout[-1500:])
                print(r.stderr[-1500:])
                raise RuntimeError(f"p6_4 parse 失败 rc={r.returncode}")
            ok.append(sym0)
        except Exception as exc:  # noqa: BLE001
            fail.append((sym0, str(exc)))
            print(f"[FAIL] {sym0}: {exc}")

    print(f"\n[KLINE] 融合完成：成功 {len(ok)} / 失败 {len(fail)}")
    for s, e in fail:
        print(f"   - {s}: {e}")

    if args.skip_p22:
        print("[SKIP] 已跳过 p22_tail_ext 重建（--skip-p22）")
        return 1 if fail else 0

    if not ok:
        print("[SKIP] 无成功融合的品种，跳过 p22 重建")
        return 1 if fail else 0

    print("\n[P22] 重建信号缓存尾折（p22_tail_ext --force，全量重训约 8 分钟）...")
    r = subprocess.run([py, str(P22), "--force"], cwd=str(PROJECT_ROOT), capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        print("[FAIL] p22_tail_ext 重建失败")
        return 1
    print(r.stdout[-2000:])
    print("[OK] P步-A/B 完成：K线融合 + 信号缓存重建")
    return 0


if __name__ == "__main__":
    sys.exit(main())
