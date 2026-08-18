"""P0 基本面数据落地脚手架：PandaData DeepView 拉取（基差/期限结构/仓单/席位持仓）。

【阻塞状态 2026-08-18 傍晚】
- get_future_basis / get_future_warehouse_receipt / get_future_term_structure 等 DeepView 方法
  经 MCP 网关调用报 `unknown params: underlying_symbol`（行情类 get_future_daily_post 同参数名可用）；
- 无过滤参数时偶发 `reauth_required`（token 抖动）——疑似 DeepView 服务授权范围或网关 schema 未对齐。
- 前置验证（现有 open_interest 代理）：OI 变化因子 IC ≈ 0（+0.002/+0.004/量比 -0.004）——粗糙代理否决，
  必须真席位/基差数据。DeepView 恢复即执行本脚本。

【授权恢复后执行步骤】
1. 在连接器管理页确认 PandaData 授权包含 DeepView 数据范围；
2. 直接调用验证：mcp__pandadata__call_pandadata(method="get_future_basis",
   params={"underlying_symbol": ["AU","AG"], "start_date": "20260701", "end_date": "20260715"})
3. 通过后按下方流程拉全量。

【拉取方案】（复用 parse_pandadata.py 持久化解析）
- 指标 3 个：basis（基差率）/ warehouse_receipt（仓单）/ term_structure（期限结构，需合约代码，最后做）
- 品种 18 个：一次调用传列表（underlying_symbol 支持 List）
- 分段 2 段：2018-2022 / 2022-2026（网关 1000 行限制）
- 落盘：data/raw/fundamental/{metric}_{sym}.parquet（date + 指标列）
- 验证：覆盖/连续性/与内盘交易日对齐
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

SYMBOLS = ["AU", "AG", "M", "CU", "RB", "I", "AL", "ZN", "NI", "HC",
           "Y", "P", "J", "JM", "SR", "CF", "TA", "SC"]

METHODS = {
    "basis": "get_future_basis",              # 基差率（现货-期货）/现货
    "warehouse": "get_future_warehouse_receipt",  # 仓单（库存代理）
}

SEGMENTS = [("20180101", "20220101"), ("20220101", "20260817")]

OUT_DIR = Path("data/raw/fundamental")


def build_pull_plan() -> list[dict]:
    """生成拉取计划（人工执行 MCP 调用后解析落盘）。"""
    plan = []
    for metric, method in METHODS.items():
        for start, end in SEGMENTS:
            plan.append({
                "method": method,
                "metric": metric,
                "params": {"underlying_symbol": SYMBOLS, "start_date": start, "end_date": end},
                "expected": f"{metric} × {len(SYMBOLS)}品种 × {start}~{end}",
            })
    return plan


def parse_and_save(raw_json: dict, metric: str, sym: str, seg: str) -> None:
    """解析单次响应并落盘（示例结构，按实际响应调整）。"""
    # 响应结构示例（get_future_basis）：
    # {"data": [{"underlying_symbol": "AU", "date": "20250102",
    #            "basis": 9.0, "basis_ratio": 0.228, "spot_price": 3940.0}, ...]}
    rows = raw_json.get("data", [])
    df = pd.DataFrame(rows)
    if df.empty:
        print(f"[WARN] {metric} {sym} {seg} 空")
        return
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{metric}_{sym}_{seg}.parquet"
    df.to_parquet(out, index=False)
    print(f"[OK] {out}: {len(df)} 行 {df['date'].min().date()}~{df['date'].max().date()}")


def merge_segments(metric: str, sym: str) -> None:
    """合并两段并去重落盘。"""
    files = sorted(OUT_DIR.glob(f"{metric}_{sym}_*.parquet"))
    if not files:
        return
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df = df.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    out = OUT_DIR / f"{metric}_{sym}.parquet"
    df.to_parquet(out, index=False)
    print(f"[MERGED] {out}: {len(df)} 行 {df['date'].min().date()}~{df['date'].max().date()}")


def main() -> None:
    ap = argparse.ArgumentParser(description="DeepView 数据拉取脚手架（授权恢复后执行）")
    ap.add_argument("--plan", action="store_true", help="打印拉取计划")
    ap.add_argument("--merge", action="store_true", help="合并已落盘分段")
    args = ap.parse_args()
    if args.plan:
        for p in build_pull_plan():
            print(f"[PLAN] {p['method']}: {p['expected']}")
        print("\n执行方式：逐条调用 mcp__pandadata__call_pandadata，持久化输出用 scripts/parse_pandadata.py 解析")
    if args.merge:
        for metric in METHODS:
            for sym in SYMBOLS:
                merge_segments(metric, sym)
        print("[OK] 合并完成")


if __name__ == "__main__":
    main()
