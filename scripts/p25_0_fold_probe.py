"""P25-2 前置探针：v13 自然跨边界重建是否可行（fold 网格实际边界）。

不训练任何模型，只回答一个决定性事实问题：以当前数据（K 线到 2026-08-21，
18 品种 2093/2096/2097/2039 行）重跑 v8 同口径 walk_forward 时，WalkForwardSplitter
（train_len=250/test_len=60/purge=5/embargo=2/mode=rolling，base.yaml 默认）
是否会新增一折，使末信号日延伸到 08-21？

方法：对每个品种用真实特征索引（build_features 输出，与生产路径完全一致）跑
splitter.split()，统计折数、最后一折测试窗 [test_start, test_end) 与
test_end-1 对应日期。若任一品种出现新折（末测试窗覆盖 06-30 之后）→ v13 可行；
否则 v13 与 v8 逐字节一致（fold 截断结构性，P21 已定性）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from hexbroker.config import load_config
from hexbroker.data.splitter import WalkForwardSplitter
from hexbroker.feature import build_features
from scripts.group_modeling import LOCAL_MAP, load_fundamental_data
from scripts.group_modeling_v2 import GROUPS_V2
from scripts.refine_lightgbm_champion import DATA_START, FREQ
from scripts.sentinel_phase4_evo import load_local_bars
from scripts.ablate_features import align_global_to_inner, load_global_close

SPLITTER = dict(train_len=250, test_len=60, purge=5, embargo=2, mode="rolling")


def main() -> None:
    cfg = load_config()
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = "2026-08-21"
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30
    cfg.forecast.calibration_method = "platt"
    cfg.feature.transformers = ["technical", "microstructure", "iterative", "cross",
                                "normalize", "fundamental"]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.fundamental_params = {"window": 252, "min_periods": 60, "include_basis": True}

    all_std = [LOCAL_MAP[s] for syms in [g["syms"] for g in GROUPS_V2.values()] for s in syms]
    bars = load_local_bars(all_std)

    print("=" * 100)
    print("P25-2 fold 边界探针（不训练，仅 splitter + 真实特征索引）")
    print(f"splitter={SPLITTER} | data.end={cfg.data.end}")
    print("=" * 100)

    rows = []
    for gname, gcfg in GROUPS_V2.items():
        std_syms = [LOCAL_MAP[s] for s in gcfg["syms"]]
        gc = {c: align_global_to_inner(load_global_close(c),
                                       bars.df.index.get_level_values("datetime").unique().sort_values())
              for c in gcfg["global"]}
        cfg.data.symbols = std_syms
        cfg.feature.cross_params = {"global_codes": gcfg["global"]}
        fund = load_fundamental_data(std_syms)
        features = build_features(bars, cfg, global_close=gc, fundamental_data=fund)
        for sym in features.symbols:  # 特征索引用短名（ag0/au0/...）
            short = sym
            feat = features.df.loc[[sym]].sort_index()
            feat_dates = feat.index.get_level_values("datetime")
            n = len(feat)
            splitter = WalkForwardSplitter(**SPLITTER)
            folds = splitter.split(feat.index)
            n_folds = len(folds)
            last = folds[-1] if folds else None
            # 当前数据末日期（bars 中该品种最后交易日）
            sym_bars = bars.by_symbol(sym)
            max_date = sym_bars.index.get_level_values("datetime").max()
            if last is None:
                rows.append((gname, short, n, 0, None, None, None, max_date.date()))
                print(f"[{gname:<14}] {short:<5} n_feat={n:>5} folds=0")
                continue
            test_end_pos = last.test_end
            # test_end-1 对应特征索引日期
            last_test_date = feat_dates[min(test_end_pos - 1, n - 1)]
            next_fold_start = last.test_start + 60  # 下一折 test_s（若存在）
            need_more = (next_fold_start + 60) - n  # 还需多少行才可能建下一折
            rows.append((gname, short, n, n_folds, last.test_start, last.test_end,
                         last_test_date.date(), max_date.date()))
            flag = ""
            if last_test_date.date() < max_date.date():
                flag = "  <-- 末信号日 < 数据末日（未跨边界）"
            print(f"[{gname:<14}] {short:<5} n_feat={n:>5} folds={n_folds:>2} "
                  f"last_test=[{last.test_start},{last.test_end}) last_day={last_test_date.date()} "
                  f"data_max={max_date.date()} need_next={max(0, need_more)}{flag}")

    df = pd.DataFrame(rows, columns=["group", "symbol", "n_feat", "n_folds",
                                     "last_test_start", "last_test_end",
                                     "last_fold_day", "data_max"])
    out = ROOT / "artifacts" / "p25_0_fold_probe.csv"
    out.parent.mkdir(exist_ok=True)
    df.to_csv(out, index=False)
    print(f"\n[OK] 探针结果 → {out}")
    n_crossed = int((df["last_fold_day"] == df["data_max"]).sum())
    print(f"[判定] 末折覆盖到数据末日的品种数 = {n_crossed}/{len(df)} "
          f"→ {'v13 可行（跨边界）' if n_crossed == len(df) else 'v13 仍 fold 截断（与 v8 一致）'}")


def _norm_short(std_sym: str) -> str:
    for s, std in LOCAL_MAP.items():
        if std == std_sym:
            return s
    return std_sym


if __name__ == "__main__":
    main()
