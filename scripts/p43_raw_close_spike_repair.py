"""P2 处置驱动器：raw_close 单日口径残留定点修复（§4.24 缺陷闭环）。

缺陷（§4.24 定罪）：湖内 raw_close 存在单日"复权价冒充名义价"残留
（全湖 161 事件，跨品种同日聚集 —— 备源单日坏 bar 被 p37 直灌）。

修复原理（数学精确，非插值）
----------------------------
后复权口径下 ``adj_close = k * raw_close``，``k``（比例因子）仅在换月日
跳变、段内恒定。V 形毛刺日（比值单日跳离且次日精确回落）意味着该日
``raw_close`` 偏离了段内常数 ``k``，而 ``adj_close``（pandadata 真值）平滑
→ **``raw_true[d] = adj_close[d] / k``**，``k`` 由毛刺前后相邻日比值给出
（两者必须一致，否则非单日毛刺）。注意与 §4.18 证伪的"年度插值"本质
不同：此处 adj 是真值、只修 1 天 raw，无估计误差。

安全门（任一不过 → 跳过并留痕，绝不静默）
------------------------------------------
- G1 严格 V 形：``|after/before - 1| <= tol``（前后因子一致）；
- G2 adj 连续性：``|adj[d]/adj[d±1] - 1| < 15%``（排除 adj 本身是毛刺的
  情形 —— 那需要真值重建而非本工具）；
- G3 修复值合理性：``|new_raw/raw[d±1] - 1| < 20%``（名义价日间变动上限，
  超限说明信号语义不是单日残留）；
- **G4 段内 ``k≡1`` 污染门禁（P0-4 止血，整个品种拒执行）**：上述 G1~G3
  全部默认「段内 ``k`` 恒定」成立。若 ``|adj_close/raw_close - 1| < 1e-9``
  的行占比 > ``K_IDENTICAL_CAP``（50%），说明该品种 ``raw_close`` 已被
  逐字复制成 ``adj_close``（``k≡1``），前提崩塌 —— 此时 ``k`` 恒被估为
  1.0，工具会把**仅存的真实 raw_close 当成毛刺改写成 adj_close**，形成
  自我强化的污染销毁循环。故 **G4 命中即整个品种拒执行**（不是跳过单条：
  前提在该品种上整体不成立，任何单条的 ``k`` 都不可信）。

  另设**显式冻结名单** ``DEFAULT_DENY_SYMBOLS = ("ag0", "au0", "m0")``
  作为双保险（不依赖污染率自动检测命中）。冻结依据（判决性取证）：三品种
  2018 年 ``k`` 众数 = 1.0000、占比 100.0%、值域 [1.0000, 1.0000] —— 逐字
  复制指纹；2024 年污染率 82~98% 而 2025 年 0.00%，跨年归零是**处理接缝**
  而非品种特性；对照 rb0 的 ``k`` 是九年从不为 1 的平滑真曲线。详见
  ``deliverables/data_source_audit_VERDICT_20260830.md`` §三。
  人工 override：``--k-cap`` / ``--deny-symbols``（后者传空串清空名单）。

工作流：dry-run 默认（零写盘，打印修复计划）→ ``--apply``（只重写受影响
年度分区 + sidecar 留痕）→ 内置复扫（apply 后必须 0 事件才算成功）→
幂等（复跑 0 计划）。被 G4 冻结的品种**零写入**，但不影响其余品种执行。

sidecar 可回滚性
----------------
``_RAW_CLOSE_REPAIRS.json`` 的每条记录含 ``repairs``（逐行
``sym0/date/year/old_raw/new_raw/k/adj_close``）与 ``gated``，
即「改了哪几行、原值多少」全量留痕 —— 无此则修复出错一行都回滚不了
（P0-4 证据 4）。顶层仍是 list，老条目无这些键，读取方用
``.get("repairs", [])`` 兼容。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = PROJECT_ROOT / "data" / "raw"

if str(PROJECT_ROOT) not in sys.path:  # 支持直接 ``python scripts/p43_...py`` 执行
    sys.path.insert(0, str(PROJECT_ROOT))

from hexbroker.utils.io import write_json, write_parquet
from scripts.p42_dominant_reconcile import _factor_jump_dates, _load_processed

SIDECAR_NAME = "_RAW_CLOSE_REPAIRS.json"

#: G2/G3 安全门
ADJ_MOVE_CAP = 0.15
RAW_MOVE_CAP = 0.20

#: G4 段内 k≡1 污染门禁
K_IDENTICAL_CAP = 0.5        # 污染率上限（50%），超过则整个品种拒执行
K_IDENTICAL_EPS = 1e-9       # |ratio - 1| < eps 视为 k≡1（逐字复制判据）
#: 显式冻结名单（主理人裁决，双保险，不依赖污染率自动检测命中）
DEFAULT_DENY_SYMBOLS = ("ag0", "au0", "m0")


def _extract_events(ratio: pd.Series, jumps: pd.Series,
                    tol: float) -> list[dict]:
    """V 形毛刺事件抽取（dip 日 + 段内因子 k），回复跳成对消费。"""
    pos_of = {d: i for i, d in enumerate(ratio.index)}
    events: list[dict] = []
    consumed: set[int] = set()
    for d, _mag in jumps.items():
        i = pos_of.get(d)
        if i is None or i in consumed or i == 0 or i + 1 >= len(ratio):
            continue
        before = float(ratio.iloc[i - 1])
        after = float(ratio.iloc[i + 1])
        if abs(after / before - 1) <= tol:  # G1 严格 V 形
            events.append({"date": d, "k": (before * after) ** 0.5,
                           "ratio_before": before, "ratio_after": after})
            consumed.add(i)          # dip 日
            consumed.add(i + 1)      # 回复跳（同一事件）
    return events


def build_plan(root: Path, sym0: str, df: pd.DataFrame, tol: float,
               k_cap: float = K_IDENTICAL_CAP,
               deny_symbols: tuple[str, ...] = DEFAULT_DENY_SYMBOLS) -> dict:
    """单品种修复计划（含安全门过滤，gated 事件留痕；G4 命中则整品种拒执行）。"""
    m = (df.get("raw_close", pd.Series(dtype=float)) > 0) & \
        (df.get("adj_close", pd.Series(dtype=float)) > 0)
    ratio = (df.loc[m, "adj_close"] / df.loc[m, "raw_close"]).sort_index()
    ratio = ratio[~ratio.index.duplicated(keep="last")]

    # ---- G4 段内 k≡1 污染门禁（整个品种拒执行）------------------------
    if sym0 in tuple(deny_symbols):
        return {"sym0": sym0, "repairs": [], "gated": [], "blocked": True,
                "blocked_reason": (
                    f"G4 品种 {sym0} 在显式冻结名单内，k 基准已被判定不可信"
                    f"（段内 k≡1 污染，见 deliverables/"
                    f"data_source_audit_VERDICT_20260830.md §三），"
                    f"整个品种拒绝执行")}
    n_ident = int((ratio - 1).abs().lt(K_IDENTICAL_EPS).sum())
    n_total = len(ratio)
    pollution = (n_ident / n_total) if n_total else 0.0  # 空 ratio → 0，不拦截
    if pollution > k_cap:
        return {"sym0": sym0, "repairs": [], "gated": [], "blocked": True,
                "blocked_reason": (
                    f"G4 段内 k≡1 污染率 {pollution:.2%}"
                    f"（{n_ident}/{n_total} 行 |ratio-1| < {K_IDENTICAL_EPS:g}）"
                    f" > 上限 {k_cap:.2%}：段内 k 恒定前提不成立，k 估计不可信，"
                    f"整个品种拒绝执行")}

    jumps = _factor_jump_dates(df, tol)
    raw_events = _extract_events(ratio, jumps, tol)

    repairs, gated = [], []
    for ev in raw_events:
        d, k = ev["date"], ev["k"]
        i = ratio.index.get_loc(d)
        adj = df["adj_close"]
        raw = df["raw_close"]
        adj_d = float(adj.loc[d])
        prev_adj = float(adj.iloc[ratio.index.get_loc(d) - 1]) if i > 0 else None
        next_adj = float(adj.iloc[ratio.index.get_loc(d) + 1]) \
            if i + 1 < len(ratio) else None
        new_raw = adj_d / k
        # G2 adj 连续性（adj 是修复基准，必须平滑）
        g2 = (prev_adj is None or abs(adj_d / prev_adj - 1) < ADJ_MOVE_CAP) and \
             (next_adj is None or abs(adj_d / next_adj - 1) < ADJ_MOVE_CAP)
        # G3 修复值与相邻名义价的日间变动上限
        prev_raw_d = raw.iloc[ratio.index.get_loc(d) - 1] if i > 0 else None
        next_raw_d = raw.iloc[ratio.index.get_loc(d) + 1] \
            if i + 1 < len(ratio) else None
        g3 = (prev_raw_d is None or abs(new_raw / float(prev_raw_d) - 1) < RAW_MOVE_CAP) and \
             (next_raw_d is None or abs(new_raw / float(next_raw_d) - 1) < RAW_MOVE_CAP)
        rec = {
            "sym0": sym0, "date": str(d.date()),
            "old_raw": float(df["raw_close"].loc[d]),
            "new_raw": new_raw, "k": k,
            "adj_close": adj_d,
            "year": int(pd.Timestamp(d).year),
        }
        if g2 and g3:
            repairs.append(rec)
        else:
            rec["gated_out"] = not g2 and "G2_adj_jump" or "G3_raw_move"
            gated.append(rec)
    return {"sym0": sym0, "repairs": repairs, "gated": gated,
            "blocked": False, "blocked_reason": None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="raw_close 单日口径残留定点修复")
    ap.add_argument("--data-root", default=str(DEFAULT_ROOT))
    ap.add_argument("--tol", type=float, default=1e-3)
    ap.add_argument("--apply", action="store_true",
                    help="实际写盘（默认 dry-run 零写盘）")
    ap.add_argument("--json", help="计划报告落盘路径（可选）")
    ap.add_argument("--k-cap", type=float, default=K_IDENTICAL_CAP,
                    help=f"G4 污染率上限（默认 {K_IDENTICAL_CAP}）；"
                         f"超过则整个品种拒执行")
    ap.add_argument("--deny-symbols", default=",".join(DEFAULT_DENY_SYMBOLS),
                    help=f"G4 显式冻结名单，逗号分隔（默认 "
                         f"{','.join(DEFAULT_DENY_SYMBOLS)}）；"
                         f"传空字符串清空名单")
    args = ap.parse_args(argv)

    deny_symbols = tuple(s.strip().lower()
                         for s in args.deny_symbols.split(",") if s.strip())
    k_cap = args.k_cap

    root = Path(args.data_root)
    base = root / "processed"
    if not base.exists():
        print("[FAIL] 无 processed 目录")
        return 2

    plans = []
    for sym_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        if (sym_dir.name.startswith("_")):
            continue
        df = None
        for freq in ("1d",):
            df = _load_processed(root, sym_dir.name, freq)
            if df is not None:
                break
        if df is None:
            continue
        plans.append(build_plan(root, sym_dir.name, df, args.tol,
                                k_cap=k_cap, deny_symbols=deny_symbols))

    total = sum(len(p["repairs"]) for p in plans)
    gated_n = sum(len(p["gated"]) for p in plans)
    blocked = [p for p in plans if p.get("blocked")]
    if blocked:
        names = ", ".join(p["sym0"] for p in blocked)
        print(f"[BLOCKED] {len(blocked)} 个品种：{names}"
              f"（G4 冻结，零写入；人工 override 用 --k-cap / --deny-symbols）")
        for p in blocked:
            print(f"    [BLOCKED] {p['sym0']}：{p['blocked_reason']}")
    print(f"[PLAN] {len(plans)} 品种（G4 冻结 {len(blocked)}） | "
          f"修复 {total} 处 | 安全门拦截 {gated_n} 处"
          f"（dry-run 零写盘{'，加 --apply 落盘' if not args.apply else ''}）")
    by_date: dict[str, int] = {}
    for p in plans:
        for r in p["repairs"]:
            by_date[r["date"]] = by_date.get(r["date"], 0) + 1
    clusters = sorted(by_date.items(), key=lambda kv: -kv[1])[:8]
    if clusters:
        print(f"    同日聚集 Top: {clusters}")
    for p in plans:
        if p["repairs"] or p["gated"]:
            for r in p["repairs"][:4]:
                print(f"    [FIX] {r['sym0']} {r['date']}: "
                      f"raw {r['old_raw']:.2f} → {r['new_raw']:.2f} "
                      f"(k={r['k']:.6f})")
            if len(p["repairs"]) > 4:
                print(f"    … {p['sym0']} 另 {len(p['repairs']) - 4} 处")
            for r in p["gated"]:
                print(f"    [GATED] {r['sym0']} {r['date']}: {r['gated_out']} "
                      f"（人工复核，不自动修）")

    if not args.apply:
        if args.json:
            Path(args.json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json).write_text(json.dumps(plans, ensure_ascii=False,
                                                  indent=2), encoding="utf-8")
            print(f"[WRITE] {args.json}")
        print("[DRY-RUN] 结束（零写盘）")
        return 0
    if total == 0 and not blocked:
        print("[OK] 无可修复事件")
        return 0

    # ---- apply：按 (sym0, year) 分区重写 --------------------------------
    touched: dict[tuple[str, int], set] = {}
    for p in plans:
        for r in p["repairs"]:
            touched.setdefault((r["sym0"], r["year"]), set()).add(r["date"])
    n_rows = 0
    for (sym0, year), dates in sorted(touched.items()):
        path = root / "processed" / sym0 / "1d" / f"{year}.parquet"
        df = pd.read_parquet(path)
        df["datetime"] = pd.to_datetime(df["datetime"])
        repair_map = {r["date"]: r for p in plans if p["sym0"] == sym0
                      for r in p["repairs"]}
        mask = df["datetime"].dt.strftime("%Y-%m-%d").isin(dates)
        for idx in df.index[mask]:
            d = df.loc[idx, "datetime"].strftime("%Y-%m-%d")
            df.loc[idx, "raw_close"] = repair_map[d]["new_raw"]
            n_rows += 1
        # 原子写（tempfile + os.replace），避免写一半崩溃留下半截文件
        write_parquet(df, path)

    # ---- apply 后内置复扫：必须 0 事件 ----------------------------------
    residual = 0
    for p in plans:
        df2 = _load_processed(root, p["sym0"], "1d")
        r2 = build_plan(root, p["sym0"], df2, args.tol,
                        k_cap=k_cap, deny_symbols=deny_symbols)
        residual += len(r2["repairs"])
    print(f"[WRITE] 重写 {len(touched)} 个分区 / {n_rows} 行 raw_close")

    # ---- sidecar 留痕（全量逐行，可回滚）--------------------------------
    sidecar = base / SIDECAR_NAME
    log = []
    if sidecar.exists():
        try:
            log = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            log = []
    if not isinstance(log, list):
        log = []
    log.append({
        "ts": pd.Timestamp.now().isoformat(),
        "tool": "p43_raw_close_spike_repair",
        "n_partitions": len(touched), "n_rows": n_rows,
        "residual_events_after": residual,
        # 生效参数（人工 override 时留痕，便于事后重建判定条件）
        "k_cap": k_cap,
        "deny_symbols": list(deny_symbols),
        # 全量逐行记录：无此则修复出错一行都回滚不了（P0-4 证据 4）
        "repairs": [r for p in plans for r in p["repairs"]],
        "gated": [r for p in plans for r in p["gated"]],
        "blocked": [{"sym0": p["sym0"],
                     "blocked_reason": p.get("blocked_reason")}
                    for p in blocked],
    })
    write_json(log, sidecar)

    if residual > 0:
        print(f"[WARN] 修复后复扫仍有 {residual} 处事件（幂等期望 0），"
              "请人工核查")
        return 1
    print(f"[OK] 修复完成且复扫 0 事件；留痕 {sidecar.name}")
    if blocked:
        print(f"[BLOCKED] ⚠ 本次有 {len(blocked)} 个品种被 G4 冻结、零写入："
              f"{', '.join(p['sym0'] for p in blocked)}"
              f"（其余品种修复正常；退出码仍为 0）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
