"""QA P8-4 零泄漏独立检查：纯循环参考实现 + 扰动测试 + union 对齐验证。

fresh-eyes（严过关）：不调用 p8_4 的 zero_leakage_check；自写：
1. 纯循环截面化参考实现（逐日手算 rank/z，仅用当日行）与 R._cross_sectionalize 对比
   （真实全 18 品种面板，逐位一致则证明实现 = 当日行内统计）。
2. 扰动测试：改未来 close → 早于 cut-h 行不变；改当天 close → 当天行变化（不影响过去）。
3. union 对齐验证：宽表 fwd 列 == 各品种自有日历 fwd（对齐只 reindex 不改值）；
   空洞日截面统计只跳过该品种（当日 skipna），不引入跨日信息。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from scripts import refine_lightgbm_champion as R

HORIZON = 5
FAIL = []


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def pure_loop_cross_sectionalize(fwd_wide: pd.DataFrame, label_mode: str) -> pd.DataFrame:
    """逐日纯循环参考实现：任意日期 t 只用第 t 行（当日品种 fwd）手算 rank/z/demean。"""
    out = fwd_wide.copy().astype(float)
    for t, row in fwd_wide.iterrows():
        vals = row.dropna()
        n_valid = len(vals)
        if label_mode == "cross_rank":
            # rank(pct=True) = 升序秩 / 有效数（na_option=keep → NaN 保留）
            if n_valid >= 2:
                ranks = vals.rank(pct=True, method="average")
                out.loc[t] = ranks.reindex(out.columns)
        elif label_mode == "cross_demean":
            if n_valid >= 2:
                out.loc[t] = (vals - vals.mean()).reindex(out.columns)
        elif label_mode == "cross_z":
            sd = vals.std(ddof=0)
            if n_valid >= 2 and pd.notna(sd) and sd > 0:
                out.loc[t] = ((vals - vals.mean()) / sd).reindex(out.columns)
        else:
            raise ValueError(label_mode)
    return out


def main() -> None:
    print("=" * 100)
    print("QA P8-4 零泄漏独立检查（纯循环参考 + 扰动 + union 对齐）")
    print("=" * 100)

    # ---- 0. 全品种面板基本事实 ----
    panel = R._build_fwd_cs_panel(None, None, HORIZON, "cross_z", "all")
    close_map = R._load_all18_close_map()
    print(f"\n[0] 面板: {panel.shape}（行=日期, 列={len(panel.columns)} 品种）| "
          f"close_map 品种数={len(close_map)}")
    check("全品种面板 18 列", panel.shape[1] == 18, f"实际 {panel.shape[1]}")
    check("面板列名全部为短名", set(panel.columns) == set(R._ALL18_DIR.values()),
          f"缺 {set(R._ALL18_DIR.values()) - set(panel.columns)}")

    # ---- 1. 纯循环参考 vs 实现（cross_z / cross_rank / cross_demean 三口径） ----
    print("\n[1] 纯循环参考实现 vs R._cross_sectionalize（真实全品种面板，逐位对比）")
    raw_wide = pd.DataFrame({
        R._ALL18_DIR[s]: R._forward_returns(c, HORIZON) for s, c in close_map.items()
    }).sort_index()
    for lm in ("cross_z", "cross_rank", "cross_demean"):
        ref = pure_loop_cross_sectionalize(raw_wide, lm)
        impl = R._cross_sectionalize(raw_wide, lm)
        diff = (ref - impl).abs().max().max()
        check(f"{lm}: 逐位一致 max|Δ|={diff:.3e}", diff < 1e-12,
              "仅用当日行 ⇒ 实现=当日行内统计" if diff < 1e-12 else f"max|Δ|={diff}")

    # ---- 2. 扰动测试：改未来不影响过去；当天扰动当天变 ----
    print("\n[2] 扰动测试（真实数据，单品种扰动避免退化）")
    sym0 = list(close_map)[0]
    short0 = R._ALL18_DIR[sym0]
    s0 = close_map[sym0]
    n = len(s0)
    cut = int(n * 0.8)

    close2 = {k: v.copy() for k, v in close_map.items()}
    close2[sym0].iloc[cut:] = close2[sym0].iloc[cut:] * 1.5  # 单品种未来扰动
    orig = R._load_all18_close_map
    R._load_all18_close_map = lambda: close2
    try:
        panel2 = R._build_fwd_cs_panel(None, None, HORIZON, "cross_z", "all")
    finally:
        R._load_all18_close_map = orig

    # 受影响窗应为 [cut-h, cut)：fwd[t]=close[t+h]/close[t]-1，t+h>=cut 且 t<cut
    before = panel.iloc[: cut - HORIZON]
    before2 = panel2.iloc[: cut - HORIZON]
    d_before = (before - before2).abs().max().max()
    check(f"早于 cut-h 行不变（无前视）max|Δ|={d_before:.3e}", d_before == 0.0)

    aff = (panel.iloc[cut - HORIZON: cut] - panel2.iloc[cut - HORIZON: cut]).abs().max().max()
    check(f"受影响窗 [cut-h, cut) 确实变化 max|Δ|={aff:.4f}", aff > 1e-6, "扰动有效（非退化）")

    # 当天扰动当天变：只改某一天的单品种 close，验证只有该天（及其 fwd 依赖窗）受影响
    t0 = s0.index[n // 2]
    pos = s0.index.get_loc(t0)
    close3 = {k: v.copy() for k, v in close_map.items()}
    close3[sym0].iloc[pos] = close3[sym0].iloc[pos] * 1.2
    R._load_all18_close_map = lambda: close3
    try:
        panel3 = R._build_fwd_cs_panel(None, None, HORIZON, "cross_z", "all")
    finally:
        R._load_all18_close_map = orig
    # fwd[t] 依赖 close[t] 与 close[t+h]：close[t0] 变 ⇒ fwd[t0-h..t0] 变；其余不变
    affected_expected = s0.index[max(0, pos - HORIZON): pos + 1]
    d3 = (panel - panel3).abs()
    changed_rows = d3.index[d3.max(axis=1) > 1e-9]
    ok_rows = set(changed_rows) <= set(affected_expected) and len(changed_rows) > 0
    check(f"单日单品种扰动 → 仅 {len(changed_rows)} 行受影响（期望 ≤ {len(affected_expected)}）",
          ok_rows, f"受影响行={list(changed_rows)[:5]}")

    # ---- 3. union 对齐验证：不引入跨日信息 ----
    print("\n[3] union 对齐验证")
    # 3a. 宽表 fwd 值 == 自有日历 fwd 值（对齐仅 reindex，不改值）
    max_rel = 0.0
    for s, c in close_map.items():
        short = R._ALL18_DIR[s]
        own_fwd = R._forward_returns(c, HORIZON)
        wide_col = raw_wide[short]
        common = own_fwd.index.intersection(wide_col.index)
        d = (own_fwd.loc[common] - wide_col.loc[common]).abs().max()
        max_rel = max(max_rel, d)
    check(f"宽表 fwd == 自有日历 fwd（max|Δ|={max_rel:.3e}）", max_rel < 1e-12,
          "union 对齐不改变 fwd 数值，空洞仅出现在单品种不交易日")

    # 3b. 空洞日截面统计只跳过该品种（当日 skipna）：找一个至少 1 品种缺数的日期
    hole_days = raw_wide.index[raw_wide.isna().any(axis=1)]
    check(f"存在 union 空洞日（不交易日）: {len(hole_days)} 天", len(hole_days) > 0,
          f"示例 {list(hole_days[:3])}")
    if len(hole_days) > 0:
        t = hole_days[0]
        row = raw_wide.loc[t]
        n_valid = int(row.notna().sum())
        # 截面化在该日应只用 n_valid 个品种的当日值（手工重算 z）
        mu = row.dropna().mean()
        sd = row.dropna().std(ddof=0)
        manual = (row.dropna() - mu) / sd
        impl_row = panel.loc[t].dropna()
        diff = (manual.sort_index() - impl_row.sort_index()).abs().max()
        check(f"空洞日 {t.date()} 截面只用 {n_valid} 个有效品种当日值（max|Δ|={diff:.3e}）",
              diff < 1e-9, "NaN 品种不参与统计，未借用其他天信息")

    # 3c. 空洞不传播：空洞日某品种 NaN 不应影响其他日期的其他品种
    #     逐列检查：除 fwd 尾部（自身日历尾部 h 行）+ 该品种空洞日外，无额外 NaN
    bad = 0
    for s, c in close_map.items():
        short = R._ALL18_DIR[s]
        own_fwd = R._forward_returns(c, HORIZON)
        col = panel[short]
        # 实现截面化会把 NaN 保留（na_option=keep）；z 只在有效品种上算
        extra_nan = col.isna() & ~own_fwd.reindex(col.index).isna()
        # cross_z 退化行（有效<2 或 sd=0）会回填原始 fwd → 不会凭空制造 NaN；上述 extra_nan 应全 False
        if extra_nan.any():
            bad += int(extra_nan.sum())
    check(f"截面化不引入额外 NaN（extra_nan 计数={bad}）", bad == 0)

    # ---- 4. 汇总 ----
    print("\n" + "=" * 100)
    if FAIL:
        print(f"零泄漏检查: {len(FAIL)} 项 FAIL → {FAIL}")
        sys.exit(1)
    print("零泄漏检查: 全部 PASS —— label_pool='all' 截面化无前视、无跨日信息、union 对齐安全")
    print("=" * 100)


if __name__ == "__main__":
    main()
