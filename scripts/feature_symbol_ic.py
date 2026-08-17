"""特征×品种关联性分析：验证"特征与品种涨跌关联性决定品种效果"假设。

对 25 个特征 × 18 品种计算 RankIC（特征与未来 5 日收益的秩相关，严格 ≤t），
汇总每品种的平均特征关联度，与模型整体 exp_ret IC 对照，
回答：品种效果差异是否由"特征-品种关联性"解释。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
from scipy import stats

from hexbroker.config import load_config
from hexbroker.feature import build_features
from scripts.ablate_features import align_global_to_inner, load_global_close
from scripts.eval_signals18 import SYMBOLS18, load_sig18_sym_close
from scripts.sentinel_phase4_evo import load_local_bars

FEATURES = [
    "f_ret_1", "f_ret_acc_5", "f_ret_acc_20", "f_vol_5", "f_vol_20",
    "f_ma_spread", "f_rsi", "f_macd", "f_macd_hist", "f_boll_width",
    "f_vol_ratio", "f_intraday_range", "f_body_ratio", "f_upper_shadow",
    "f_lower_shadow", "f_bar_dir", "f_gap", "f_up_vol_share", "f_range_pos_20",
    "f_xr_spx_ratio", "f_xr_spx_mom", "f_xr_spx_vol",
    "f_xr_uup_ratio", "f_xr_uup_mom", "f_xr_uup_vol",
]
GROUPS = {
    "技术/动量": ["f_ret_1", "f_ret_acc_5", "f_ret_acc_20", "f_ma_spread", "f_macd", "f_rsi"],
    "波动/风险": ["f_vol_5", "f_vol_20", "f_boll_width", "f_vol_ratio"],
    "日内微观": ["f_intraday_range", "f_body_ratio", "f_upper_shadow", "f_lower_shadow",
                 "f_bar_dir", "f_gap", "f_up_vol_share", "f_range_pos_20"],
    "跨市场SPX": ["f_xr_spx_ratio", "f_xr_spx_mom", "f_xr_spx_vol"],
    "跨市场UUP": ["f_xr_uup_ratio", "f_xr_uup_mom", "f_xr_uup_vol"],
}


def main() -> None:
    cfg = load_config()
    cfg.data.symbols = list(SYMBOLS18)
    cfg.data.freq = "1d"
    cfg.data.start = "2018-01-01"
    cfg.data.end = "2026-08-17"
    cfg.forecast.horizon = 5
    cfg.feature.transformers = ["technical", "microstructure", "iterative", "cross", "normalize"]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": ["spx", "uup"]}
    bars = load_local_bars(SYMBOLS18)
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in ["spx", "uup"]}
    feat = build_features(bars, cfg, global_close=gc)
    df = feat.df.copy()
    df = df.reset_index()
    print(f"[OK] 特征帧 {df.shape}")

    # 未来 5 日收益
    closes = {sym: load_sig18_sym_close(sym) for sym in SYMBOLS18}
    close_df = pd.DataFrame(closes).sort_index()
    real5 = close_df.shift(-5) / close_df - 1.0
    df["real5"] = df.apply(
        lambda r: real5.loc[r["datetime"], r["symbol"]]
        if r["datetime"] in real5.index and r["symbol"] in real5.columns else np.nan,
        axis=1,
    )
    df = df.dropna(subset=["real5"])
    print(f"[OK] 有效样本 {len(df)}")

    # 特征×品种 RankIC 矩阵
    ic_rows = []
    for sym in SYMBOLS18:
        sub = df[df["symbol"] == sym]
        for f in FEATURES:
            s = sub[f].replace([np.inf, -np.inf], np.nan)
            m = sub["real5"]
            mask = s.notna() & m.notna()
            if mask.sum() < 100:
                ic = np.nan
            else:
                ic = float(stats.spearmanr(s[mask], m[mask]).statistic)
            ic_rows.append({"symbol": sym, "feature": f, "ic": ic})
    ic_df = pd.DataFrame(ic_rows)

    # 每品种平均特征关联度（abs 均值 + 原值均值）
    sym_stats = ic_df.groupby("symbol")["ic"].agg(["mean", "std"]).rename(
        columns={"mean": "ic_mean", "std": "ic_std"})
    sym_stats["ic_abs_mean"] = ic_df.groupby("symbol")["ic"].apply(lambda s: s.abs().mean())
    # 每特征平均关联度（abs）
    feat_stats = ic_df.groupby("feature")["ic"].apply(lambda s: s.abs().mean()).sort_values(ascending=False)

    print("\n=== 每品种 特征-收益平均关联度（|IC| 均值）===")
    sym_stats = sym_stats.sort_values("ic_abs_mean", ascending=False)
    print(sym_stats.round(4).to_string())

    print("\n=== 每特征 全品种平均 |IC|（特征普遍性排序）===")
    for f, v in feat_stats.items():
        g = [g for g, fs in GROUPS.items() if f in fs]
        gname = g[0] if g else "?"
        print(f"  {f:20s} |IC|={v:.4f}  [{gname}]")

    print("\n=== 特征组普遍性 ===")
    for g, fs in GROUPS.items():
        avg = feat_stats[fs].mean()
        print(f"  {g:12s} 平均|IC|={avg:.4f}")

    # 特征 IC 方向一致性：跨品种同方向比例
    print("\n=== 特征-品种方向一致性（同号比例）===")
    for f in FEATURES:
        ics = ic_df[ic_df["feature"] == f]["ic"].dropna()
        if len(ics) >= 10:
            pos = (ics > 0).mean()
            neg = (ics < 0).mean()
            dom = max(pos, neg)
            dom_sign = "+" if pos > neg else "-"
            if dom >= 0.75:
                print(f"  {f:20s} 方向一致 {dom*100:.0f}% ({dom_sign})——对所有品种同向")
            else:
                print(f"  {f:20s} 方向分歧 pos={pos*100:.0f}%/neg={neg*100:.0f}%——品种依赖")


if __name__ == "__main__":
    main()
