"""QA 独立复核：真 Kronos OOS 信号（符号反转陷阱 / 交集验证 / 因果抽样）。

仅做只读验证，不改动任何业务文件。输出：
1) 独立重算 kronos 方向准确率（对全部 3960 条信号） vs 报告 45.76%
2) p_up 取反后的方向准确率（反证：45.76 非符号 bug 所致）
3) 3~5 条信号手工符号比对（p_up 方向 vs 实际 close[ts+5]/close[ts]-1）
4) kronos vs lightgbm 共有 (symbol, ts) 交集的准确率对比
5) 全部信号 p_up / exp_ret / quantiles 契约 sanity
"""
from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "artifacts" / "signals_oos_r7_auagm"
DATA = ROOT / "data" / "raw" / "processed"
HORIZON = 5
SYMS = ["ag0", "au0", "m0"]


def load_close() -> dict[str, pd.Series]:
    """从 sina 落盘 bar 数据重建每标的 close（datetime 索引）。"""
    out = {}
    for s in SYMS:
        parts = []
        for f in sorted(glob.glob(str(DATA / s / "1d" / "*.parquet"))):
            df = pd.read_parquet(f)
            if "datetime" in df.columns:
                df = df.set_index("datetime")
            close = df["close"].astype(float)
            close = close[~close.index.duplicated(keep="last")].sort_index()
            parts.append(close)
        out[s] = pd.concat(parts)
        out[s] = out[s][~out[s].index.duplicated(keep="last")].sort_index()
    return out


def load_signals(model_id: str) -> pd.DataFrame:
    files = sorted(glob.glob(str(STORE / model_id / "*.parquet")))
    dfs = [pd.read_parquet(f) for f in files]
    df = pd.concat(dfs, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"])
    df["p_up"] = df["p_up"].astype(float)
    return df.set_index(["symbol", "ts"]).sort_index()


def dir_acc(p_up: np.ndarray, realized: np.ndarray) -> float:
    mask = ~np.isnan(realized)
    pred_dir = np.sign(p_up[mask] - 0.5)
    real_dir = np.sign(realized[mask])
    return float(np.mean(pred_dir == real_dir))


def main() -> None:
    close = load_close()
    kr = load_signals("kronos")
    lg = load_signals("c3fcfadee269")

    # --- 1. 独立重算 kronos 方向准确率 ---
    realized_all: list[float] = []
    p_up_all: list[float] = []
    for (sym, ts), row in kr.iterrows():
        c = close[sym]
        try:
            i = c.index.get_loc(ts)
        except KeyError:
            realized_all.append(np.nan)
            p_up_all.append(row["p_up"])
            continue
        if i + HORIZON < len(c):
            r = float(c.iloc[i + HORIZON] / c.iloc[i] - 1.0)
        else:
            r = np.nan
        realized_all.append(r)
        p_up_all.append(row["p_up"])

    kr_realized = np.array(realized_all)
    kr_p_up = np.array(p_up_all)
    acc = dir_acc(kr_p_up, kr_realized)
    n_valid = int((~np.isnan(kr_realized)).sum())
    print(f"[1] kronos 独立重算: n_sig={len(kr)} n_valid={n_valid} dir_acc={acc*100:.2f}%")
    print(f"    报告值: 45.76%  差异: {(acc-0.4576)*100:+.2f}pp")

    # --- 2. p_up 取反反证 ---
    inv_p_up = 1.0 - kr_p_up
    acc_inv = dir_acc(inv_p_up, kr_realized)
    print(f"[2] p_up 取反后 dir_acc={acc_inv*100:.2f}%  (若 45.76 是符号 bug，取反应≈54+)")
    # 也计算 exp_ret 方向（另一条反证路径）
    kr_exp = kr["exp_ret"].to_numpy(float)
    acc_exp = dir_acc(kr_exp, kr_realized)
    print(f"    用 exp_ret 符号 dir_acc={acc_exp*100:.2f}%  (与 p_up 应一致，互证无符号反转)")

    # --- 3. 手工抽查 5 条信号（跨品种、跨折） ---
    print("\n[3] 手工符号抽查（ts, p_up, 实际 close[ts+5]/close[ts]-1, sign 比对）")
    samples = kr.sample(5, random_state=7)
    ok = 0
    for (sym, ts), row in samples.iterrows():
        c = close[sym]
        i = c.index.get_loc(ts)
        real = float(c.iloc[i + HORIZON] / c.iloc[i] - 1.0)
        pred_dir = "UP" if row["p_up"] > 0.5 else "DOWN"
        real_dir = "UP" if real > 0 else "DOWN"
        match = pred_dir == real_dir
        ok += match
        print(
            f"  {sym} ts={ts.date()} p_up={row['p_up']:.3f} pred={pred_dir} "
            f"real={real:+.4f} ({real_dir}) match={match}"
        )
    print(f"  抽查命中 {ok}/{len(samples)}")

    # --- 4. 交集验证 kronos vs lightgbm ---
    kr_idx = set(kr.index)
    lg_idx = set(lg.index)
    common = kr_idx & lg_idx
    print(f"\n[4] 交集验证: kronos={len(kr_idx)} lightgbm={len(lg_idx)} 共有={len(common)}")
    if len(common) > 0:
        kr_c = kr.loc[sorted(common)]
        lg_c = lg.loc[sorted(common)]
        # kronos realized（用同一条实际收益）
        kr_c_real = np.array([
            float(close[s].iloc[close[s].index.get_loc(ts) + HORIZON] / close[s].iloc[close[s].index.get_loc(ts)] - 1.0)
            if close[s].index.get_loc(ts) + HORIZON < len(close[s]) else np.nan
            for (s, ts) in kr_c.index
        ])
        lg_c_real = kr_c_real.copy()
        acc_kr_i = dir_acc(kr_c["p_up"].to_numpy(float), kr_c_real)
        acc_lg_i = dir_acc(lg_c["p_up"].to_numpy(float), lg_c_real)
        print(f"  交集上 kronos dir_acc={acc_kr_i*100:.2f}%  lightgbm dir_acc={acc_lg_i*100:.2f}%  差距={(acc_lg_i-acc_kr_i)*100:.2f}pp")
    else:
        print("  交集为空——需检查 ts 对齐")

    # --- 5. 契约 sanity ---
    print("\n[5] 契约 sanity")
    print(f"  p_up 范围: [{kr_p_up.min():.6f}, {kr_p_up.max():.6f}]（应在 (0,1)）")
    print(f"  p_up 唯一值: {sorted(np.unique(kr_p_up.round(1)))} (n_mc=10 -> 0.0~1.0 步长0.1)")
    print(f"  is_effective 覆盖: {kr['is_effective'].mean()*100:.2f}%")
    print(f"  exp_ret 与 p_up 符号一致率: {np.mean(np.sign(kr_exp) == np.sign(kr_p_up - 0.5)):.2%}")
    # quantiles 单调性抽查
    mono = 0
    for q in kr["quantiles"]:
        vals = [q[f"q{qq}"] for qq in (10, 25, 50, 75, 90)]
        if all(vals[i] <= vals[i + 1] for i in range(4)):
            mono += 1
    print(f"  quantiles 单调 (q10<=q25<=q50<=q75<=q90): {mono}/{len(kr)}")


if __name__ == "__main__":
    main()
