"""QA P8-3 零泄漏独立检查：纯循环参考实现 + 扰动测试。

三类验证：
  1. 参考一致性：自写纯循环 cross_rank/cross_z/cross_demean（逐行遍历、逐元素
     计算，不用 pandas rank/std 捷径）vs ``_cross_sectionalize`` 输出 → 数值一致。
  2. 因果性（无前视）：任意日期 t 的输出只依赖当日（行 t）各品种 fwd——
     扰动"未来"日期（t'>t）不影响 t 行；扰动"当日"某品种影响当日该品种。
  3. 退化处理：单品种组/双品种组/σ=0 时输出 = 原始 fwd（逐元素比对）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from scripts.refine_lightgbm_champion import _cross_sectionalize


# ---------------------------------------------------------------------------
# 纯循环参考实现（不用 pandas rank/std 内置捷径）
# ---------------------------------------------------------------------------
def ref_rank_row(row: np.ndarray) -> np.ndarray:
    """升序分位 [0,1]（na 保留）：**pandas 口径 pct = 平均位次 / 有效数**（非 (r-1)/(n-1)），
    并列取平均位次。经实证：pandas rank(pct=True) 对 [1,2] 给 [0.5,1.0]，对 [1,2,3] 给
    [1/3,2/3,1.0]；单有效值给 1.0（但实现层退化回填原始 fwd，训练不接触该值）。"""
    out = np.full(row.shape, np.nan)
    valid_mask = ~np.isnan(row)
    idx = np.where(valid_mask)[0]
    vals = row[idx]
    n = len(vals)
    if n < 1:
        return out
    order = np.argsort(vals, kind="stable")
    sorted_vals = vals[order]
    # 平均位次（含并列）
    ranks = np.empty(n)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1-based 平均位次
        ranks[i:j + 1] = avg_rank
        i = j + 1
    pct = ranks / n  # pandas 口径
    # 写回原列序
    out[idx[order]] = pct
    return out


def ref_cross_rank(wide: pd.DataFrame) -> pd.DataFrame:
    """逐行（当日）跨品种 rank：out[t, j] = 当日第 j 品种的 pct rank。"""
    vals = wide.to_numpy(dtype=float)
    out = np.full_like(vals, np.nan)
    for t in range(vals.shape[0]):
        out[t] = ref_rank_row(vals[t])
    return pd.DataFrame(out, index=wide.index, columns=wide.columns)


def ref_cross_demean(wide: pd.DataFrame) -> pd.DataFrame:
    vals = wide.to_numpy(dtype=float)
    out = np.full_like(vals, np.nan)
    for t in range(vals.shape[0]):
        row = vals[t]
        m = np.nanmean(row)
        out[t] = row - m
    return pd.DataFrame(out, index=wide.index, columns=wide.columns)


def ref_cross_z(wide: pd.DataFrame) -> pd.DataFrame:
    vals = wide.to_numpy(dtype=float)
    out = np.full_like(vals, np.nan)
    for t in range(vals.shape[0]):
        row = vals[t]
        m = np.nanmean(row)
        sd = np.nanstd(row, ddof=0)
        if sd == 0 or np.isnan(sd):
            out[t] = row  # 退化：σ=0 → 保留原始
        else:
            out[t] = (row - m) / sd
    return pd.DataFrame(out, index=wide.index, columns=wide.columns)


def make_synthetic(n_days: int = 40, n_syms: int = 6, seed: int = 7,
                   nan_frac: float = 0.08) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n_days, freq="B")
    cols = [f"s{i}" for i in range(n_syms)]
    vals = rng.normal(0.001, 0.02, size=(n_days, n_syms))
    mask = rng.random(vals.shape) < nan_frac
    vals[mask] = np.nan
    # 保证每天至少 2 个有效值（构造用例）
    for t in range(n_days):
        if np.isnan(vals[t]).sum() > n_syms - 2:
            ok = np.where(~np.isnan(vals[t]))[0]
            fill = np.where(np.isnan(vals[t]))[0][: n_syms - 2 - len(ok)]
            vals[t, fill] = 0.0
    return pd.DataFrame(vals, index=idx, columns=cols)


def compare_modes(wide: pd.DataFrame) -> None:
    """1. 参考一致性：逐模式与 _cross_sectionalize 比对。"""
    print(f"[1] 参考一致性：n_days={len(wide)} n_syms={len(wide.columns)} "
          f"nan_frac={wide.isna().mean().mean():.2%}")
    for mode in ("cross_rank", "cross_z", "cross_demean", "absolute"):
        impl = _cross_sectionalize(wide.copy(), mode)
        if mode == "absolute":
            ref = wide.copy()
        elif mode == "cross_rank":
            ref = ref_cross_rank(wide)
        elif mode == "cross_z":
            ref = ref_cross_z(wide)
        else:
            ref = ref_cross_demean(wide)
        # _cross_sectionalize 会做退化回填（保留原始 fwd）；参考实现对齐该口径：
        # 仅当"有效<2 或 cross_z σ=0"时参考也回填原始。
        n_valid = wide.notna().sum(axis=1)
        keep_raw = n_valid < 2
        if mode == "cross_z":
            sd = wide.std(axis=1, skipna=True, ddof=0)
            keep_raw = keep_raw | sd.isna() | (sd == 0.0)
        ref = ref.where(~keep_raw, wide, axis=0)
        impl_arr = impl.to_numpy(dtype=float)
        ref_arr = ref.to_numpy(dtype=float)
        both = ~(np.isnan(impl_arr) & np.isnan(ref_arr))
        diff = np.abs(impl_arr - ref_arr)
        max_diff = float(np.nanmax(np.where(both, diff, np.nan)))
        n_mismatch = int(np.sum(both & (diff > 1e-9)))
        print(f"    {mode:<14} max|diff|={max_diff:.2e} mismatches={n_mismatch} "
              f"{'PASS' if max_diff < 1e-9 and n_mismatch == 0 else 'FAIL'}")


def perturbation_tests(wide: pd.DataFrame) -> None:
    """2. 因果性（无前视）：扰动未来不影响过去；扰动当日影响当日。"""
    print("[2] 因果性（无前视）")
    rng = np.random.default_rng(99)
    for mode in ("cross_rank", "cross_z", "cross_demean"):
        base = _cross_sectionalize(wide.copy(), mode)
        # 2a. 扰动"未来"：改 t_future 行某个值 → 检查所有 t <= t_future-2 行不变
        t_future = len(wide) - 5
        col = wide.columns[2]
        perturbed = wide.copy()
        perturbed.iloc[t_future, 2] = wide.iloc[t_future, 2] + 0.5
        p_out = _cross_sectionalize(perturbed, mode)
        past_rows = base.iloc[: t_future - 1]
        p_past = p_out.iloc[: t_future - 1]
        same = np.allclose(past_rows.to_numpy(dtype=float),
                           p_past.to_numpy(dtype=float), equal_nan=True)
        # 更严格：未来行任意日期 t' 改值，所有 < t' 的行逐元素完全一致
        all_past_same = True
        for tprime in range(5, len(wide)):
            pp = wide.copy()
            pp.iloc[tprime, 1] = wide.iloc[tprime, 1] + 0.3
            po = _cross_sectionalize(pp, mode)
            if not np.allclose(base.iloc[:tprime].to_numpy(dtype=float),
                               po.iloc[:tprime].to_numpy(dtype=float), equal_nan=True):
                all_past_same = False
                break
        # 2b. 扰动"当日"某品种 → 当日该品种（及可能其他品种）值变化。
        #     rank 对单值单调平移不变（除非越过其他值），故用"交换当日两个品种值"
        #     保证翻位：若当日存在 >=2 个不同的有限值，交换最小/最大值 → rank 必变。
        t0 = 10
        row_vals = wide.iloc[t0].to_numpy(dtype=float)
        finite = np.where(~np.isnan(row_vals))[0]
        distinct = len(set(row_vals[finite])) if len(finite) > 0 else 0
        if distinct >= 2:
            i_min = finite[np.nanargmin(row_vals)]
            i_max = finite[np.nanargmax(row_vals)]
            p2 = wide.copy()
            tmp = p2.iloc[t0, i_min]
            p2.iloc[t0, i_min] = p2.iloc[t0, i_max]
            p2.iloc[t0, i_max] = tmp
            p2_out = _cross_sectionalize(p2, mode)
            changed_same_day = not np.allclose(
                base.iloc[t0].to_numpy(dtype=float), p2_out.iloc[t0].to_numpy(dtype=float),
                equal_nan=True)
            # 当日之外的行不变
            other_rows_same = np.allclose(
                np.delete(base.to_numpy(dtype=float), t0, axis=0),
                np.delete(p2_out.to_numpy(dtype=float), t0, axis=0), equal_nan=True)
        else:
            # 退化行（无 >=2 个不同值）→ 交换不可行，跳过当日断言
            changed_same_day, other_rows_same = True, True
        print(f"    {mode:<14} 改未来->过去不变: {same and all_past_same} | "
              f"改当日->当日变: {changed_same_day} | 改当日->其他日不变: {other_rows_same} "
              f"{'PASS' if (same and all_past_same and changed_same_day and other_rows_same) else 'FAIL'}")


def degradation_tests() -> None:
    """3. 退化处理：单品种组 / 双品种 / σ=0。"""
    print("[3] 退化处理")
    # 单品种组：整列唯一
    single = pd.DataFrame({"a": [0.01, -0.02, 0.03]}, index=pd.date_range("2020-01-01", periods=3))
    for mode in ("cross_rank", "cross_z", "cross_demean"):
        out = _cross_sectionalize(single.copy(), mode)
        ok = np.allclose(out.to_numpy(dtype=float), single.to_numpy(dtype=float), equal_nan=True)
        print(f"    单品种 {mode:<14} 输出==原始fwd: {ok} {'PASS' if ok else 'FAIL'}")
    # σ=0（cross_z 退化）：两品种同值
    sd0 = pd.DataFrame({"a": [0.01, 0.01, 0.01], "b": [0.01, 0.01, 0.01]},
                       index=pd.date_range("2020-01-01", periods=3))
    out = _cross_sectionalize(sd0.copy(), "cross_z")
    ok = np.allclose(out.to_numpy(dtype=float), sd0.to_numpy(dtype=float), equal_nan=True)
    print(f"    cross_z σ=0 输出==原始fwd: {ok} {'PASS' if ok else 'FAIL'}")
    # 双品种正常：pandas 口径 rank pct = rank/n → 应为 {0.5, 1.0}
    two = pd.DataFrame({"a": [0.01, 0.02], "b": [0.03, -0.01]},
                       index=pd.date_range("2020-01-01", periods=2))
    out = _cross_sectionalize(two.copy(), "cross_rank")
    exp = pd.DataFrame({"a": [0.5, 1.0], "b": [1.0, 0.5]}, index=two.index)
    ok = np.allclose(out.to_numpy(dtype=float), exp.to_numpy(dtype=float), equal_nan=True)
    print(f"    双品种 cross_rank 期望 {{0.5,1.0}} (pandas rank/n): {ok} {'PASS' if ok else 'FAIL'}")
    # 未知模式报错
    try:
        _cross_sectionalize(two.copy(), "bogus")
        print("    未知 label_mode 抛错: FAIL")
    except ValueError:
        print("    未知 label_mode 抛错: PASS")


if __name__ == "__main__":
    print("=" * 80)
    print("QA P8-3 零泄漏独立检查（纯循环参考实现 + 扰动测试）")
    print("=" * 80)
    wide = make_synthetic()
    compare_modes(wide)
    perturbation_tests(wide)
    degradation_tests()
    print("=" * 80)
    print("DONE")
