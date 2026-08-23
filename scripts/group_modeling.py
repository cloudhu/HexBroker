"""分组建模：按品种组单独训练 LightGBM + 特征-品种白名单。

分组（基于归因：全球定价品种挂宏观特征，国内定价品种少挂）：
  precious  贵金属 au/ag       global=['spx','uup','t10y']  全宏观（实际利率/美元/风险偏好）
  ferrous   黑色   rb/i/hc/j/jm global=['spx','t10y']       部分宏观（黑色受国内需求+宏观）
  industrial有色   cu/al/zn/ni  global=['spx','uup']        全球定价
  agri      农产品 m/y/p/sr/cf  global=['spx']              少宏观（避免 ta/cf 被污染教训）
  chem_energy化工 ta/sc         global=['spx']              少宏观

每组建模 → 拼接 exp_ret → 全局截面排序 top30% → 完整回测对比基线。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from hexbroker.config import load_config
from hexbroker.feature import build_features
from hexbroker.feature.cross import _norm_sym
from scripts.ablate_features import align_global_to_inner, load_best_params, load_global_close
from scripts.refine_lightgbm_champion import DATA_START, FREQ, walk_forward_lightgbm
from scripts.sentinel_phase4_evo import load_local_bars

# 分组定义（本地短名）
GROUPS = {
    "precious": {"syms": ["au0", "ag0"], "global": ["spx", "uup", "t10y", "vix"]},  # +vix 避险因子 (2026-08-18)
    "ferrous": {"syms": ["rb0", "i0", "hc0", "j0", "jm0"], "global": ["spx", "t10y"]},
    "industrial": {"syms": ["cu0", "al0", "zn0", "ni0"], "global": ["spx", "uup"]},
    "agri": {"syms": ["m0", "y0", "p0", "sr0", "cf0"], "global": ["spx"]},
    "chem_energy": {"syms": ["ta0", "sc0"], "global": ["spx"]},
}
# 短名 → 标准名映射（load_local_bars 需要）
LOCAL_MAP = {
    "au0": "SHFE.au", "ag0": "SHFE.ag", "m0": "DCE.m", "cu0": "SHFE.cu",
    "rb0": "SHFE.rb", "i0": "DCE.i", "al0": "SHFE.al", "zn0": "SHFE.zn",
    "ni0": "SHFE.ni", "hc0": "SHFE.hc", "y0": "DCE.y", "p0": "DCE.p",
    "j0": "DCE.j", "jm0": "DCE.jm", "sr0": "CZCE.sr", "cf0": "CZCE.cf",
    "ta0": "CZCE.ta", "sc0": "INE.sc",
}

FUND_DIR = Path("data/raw/fundamental")


def load_fundamental_data(std_syms: list[str]) -> dict[str, pd.DataFrame]:
    """按品种加载基差面板 → {品种短名: DataFrame(datetime 索引, basis_ratio, basis)}。

    数据源：``data/raw/fundamental/basis_{SYM}.parquet``（P0 落地 QA 验证，18 品种）。
    缺失品种 → 跳过（该品种在特征层得到全 NaN 基差特征，模型中性处理，不中断）。
    """
    fund: dict[str, pd.DataFrame] = {}
    for s in std_syms:
        short = _norm_sym(s)
        fp = FUND_DIR / f"basis_{short.upper()}.parquet"
        if not fp.exists():
            print(f"[WARN] 基差数据缺失: {fp}（{short} 将无基本面特征）")
            continue
        df = pd.read_parquet(fp)
        df["datetime"] = pd.to_datetime(df["date"])
        df = df.set_index("datetime")[["basis_ratio", "basis"]].sort_index()
        df = df[~df.index.duplicated(keep="last")]
        fund[short] = df
    return fund


def build_group_signals(group_syms: list[str], global_codes: list[str],
                        n_jobs: int, cal_return_all: bool = False,
                        collect_models: bool = False, label_mode: str = "absolute",
                        label_pool: str = "group"):
    """单组训练：bars/features 只含组内品种，global_codes 白名单化。

    cal_return_all：True 时以「校准后返回全部信号」模式重建（用测试窗前 50% 拟合
      校准器、对全部信号应用校准并返回全部，覆盖率翻倍）；默认 False 保持 v2 口径
      （cal_split=0.5 只返回测试窗后半）。
    collect_models：True 时额外返回 ``WFResult``（含每折 feature_importances_，
      供特征重要性验证）；False 时返回与旧版一致的 ``pd.DataFrame`` 信号表。
    label_mode（P8-3）：训练标签口径，透传 ``walk_forward_lightgbm``——
      "absolute"（默认）/ "cross_rank" / "cross_z" / "cross_demean"。
      截面化按组内品种当日分组、无前视；单品种组自动退化绝对标签（见
      ``refine_lightgbm_champion._cross_sectionalize``）。
    label_pool（P8-4）：截面化范围，透传 ``walk_forward_lightgbm``——
      "group"（默认，组内截面，P8-3 行为）/ "all"（全 18 品种统一截面，
      单品种组 m0 也参与全品种当日截面，根治组规模偏置）。
    """
    cfg = load_config()
    std_syms = [LOCAL_MAP[s] for s in group_syms]
    cfg.data.symbols = std_syms
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = "2026-08-17"
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30
    cfg.forecast.calibration_method = "platt"
    # P8-2：信号生产显式启用 fundamental（基差）特征；其余保持 v2/v4 口径
    cfg.feature.transformers = ["technical", "microstructure", "iterative", "cross",
                                "normalize", "fundamental"]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": global_codes}
    cfg.feature.fundamental_params = {"window": 252, "min_periods": 60, "include_basis": True}

    bars = load_local_bars(std_syms)
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in global_codes}
    fund = load_fundamental_data(std_syms)
    features = build_features(bars, cfg, global_close=gc, fundamental_data=fund)
    wf = walk_forward_lightgbm(
        cfg, bars, features, params=load_best_params(),
        collect_models=collect_models, splitter_overrides=None, n_jobs_folds=n_jobs, cal_split=0.5,
        cal_return_all=cal_return_all, label_mode=label_mode, label_pool=label_pool,
    )
    sig = pd.DataFrame(wf.records).set_index(["symbol", "ts"]).sort_index()
    sig = sig.loc[:, ~sig.columns.duplicated(keep="last")].reset_index()
    sig["ts"] = pd.to_datetime(sig["ts"])
    print(f"[OK] {group_syms}: {len(sig)} 条信号 (global={global_codes}, "
          f"fundamental={sorted(fund.keys())})")
    if collect_models:
        return sig, wf
    return sig


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-jobs", type=int, default=6)
    ap.add_argument("--only", type=str, default="", help="只跑指定组（逗号分隔），空=全部")
    ap.add_argument("--cal-return-all", action="store_true",
                    help="校准后返回全部信号（用前 50% 拟合校准器、评估全部，覆盖率翻倍）")
    args = ap.parse_args()

    print("=" * 72)
    print("分组建模（组内训练 + 特征白名单）")
    print(f"cal_return_all={args.cal_return_all}")
    print("=" * 72)
    only = set(args.only.split(",")) if args.only else None

    frames = []
    for gname, gcfg in GROUPS.items():
        if only is not None and gname not in only:
            continue
        sig = build_group_signals(gcfg["syms"], gcfg["global"], args.n_jobs,
                                  cal_return_all=args.cal_return_all)
        frames.append(sig)
    if not frames:
        print("[FAIL] 无信号产出")
        return
    all_sig = pd.concat(frames, ignore_index=True)
    Path("artifacts").mkdir(exist_ok=True)
    all_sig.to_parquet("artifacts/signals_cache18_grouped.parquet", index=False)
    print(f"[OK] 分组信号合并 {len(all_sig)} 条 → artifacts/signals_cache18_grouped.parquet")
    print(f"  品种覆盖: {sorted(all_sig['symbol'].unique())}")


if __name__ == "__main__":
    main()
