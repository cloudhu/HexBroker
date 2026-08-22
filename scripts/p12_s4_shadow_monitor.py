"""P12-2 S4 影子跟踪框架：rt30（候选）vs 基线（v8 生产基线）信号缓存影子监控。

背景
----
P7 终裁（sentinel2-p7-engineA-signal-fix-2026-08-19.md）：S4（更频繁重训
test_len=30，缓存 ``signals_cache18_grouped_v2_rt30.parquet``）仅影子验证候选
不投产；生产维持 v2 缓存 + A15/B85（P8-4/P9 后升级为 v8 + A10/B90）。

P25-1（2026-08-22）：影子基线从 v2 升级为 **v8（signals_cache18_grouped_v8.parquet，
生产基线，P19 固化）**。P24 教训：rt30 评估不通过（当前口径 0.544<1.064）；
p12 UPGRADE_TRIGGER 对照弱基线 v2 → 影子基线需升级 v2→v8，使对照对象与
生产口径一致（P21/P22 均以 v8 为基线重估）。

⚠️ 基线切换影响（如实说明）：
  - 覆盖率不同：v2 止 2026-06-11（976 信号日，日均 8.15 品种）；v8 止 2026-06-29
    （662 信号日，日均 13.03 品种）；rt30 止 2026-07-27（837 信号日，日均 7.25）。
  - v8 信号日更少（662 < 976）、但日均品种更多、末信号日更晚（06-29 > 06-11）。
  - 对齐日（两缓存同时有 >= min_symbols 重合品种的 OOS 日）以实际重叠为准：
    本实现实测输出 corr_n_days / n_common_dates 为准，切换前后对比见
    artifacts/p25_1_baseline_switch_impact.csv。

向后兼容：
  - CLI ``--baseline-a PATH``（默认 v8）/ ``--baseline-b PATH``（默认 rt30）可切换
    两对比缓存（如 ``--baseline-a artifacts/signals_cache18_grouped_v2.parquet``
    复现旧 v2-vs-rt30 对比）；
  - 环境变量 P12_BASELINE_A / P12_BASELINE_B 同样生效（CLI 优先）；
  - ``--baseline-csv PATH`` 指定基线快照落盘路径（默认
    artifacts/p12_s4_shadow_baseline_v8.csv，标注 v8 基线；旧 v2 基线快照
    artifacts/p12_s4_shadow_baseline.csv 保留不动）。

本脚本对候选（rt30）与基线（v8）两缓存实现影子监控（只读分析，不重训、
不改缓存/数据/hexbroker 包）：

  1) 滚动窗口指标（默认 63 交易日滚动）：
       - 滚动 OOS 截面 IC：每日截面 Spearman(exp_ret, fwd5)，仅 OOS
         （>= 2024-07-18）且当日品种数 >= min_symbols(3)，再 63 窗滚动均值
       - 滚动命中率：每日 exp_ret 截面 top30% 品种的 fwd5>0 占比，63 窗滚动均值
       - 信号相关性（漂移监测核心）：候选 vs 基线同期信号 Spearman
           主指标 = 63 窗池化 Spearman（稳健，推荐用于阈值判定）
           辅指标 = 每日截面 Spearman 的 63 窗滚动均值（n=3 时粒度粗糙）
  2) 基线快照：``--baseline`` 生成基线快照 CSV（默认 v8 基线）
  3) 复核触发：``--monitor`` 重算对比（数据刷新后运行）
       - 主触发：新数据出现（K 线刷新 / 缓存重建）后，候选滚动 OOS IC 连续
         N 窗（默认 5 窗）> 基线且 |IC| > 0.03，或滚动命中率持续优于基线
         （连续 N 窗候选 > 基线且候选 > 0.50）→ 触发升级评估
       - 漂移监测：池化信号相关性跌破阈值（默认绝对 < 0.50 告警，
         相对基线回落 > 0.15 预警）→ 两引擎分化，需人工确认
         （注：P7 登记阈值 0.8 经基线标定发现会即刻误报——当前池化相关性
         ~0.60，因 S4 本就改变重训节奏、信号真实分化；已重标定，见文档）

口径铁律
--------
  - 漂移/IC 指标只用 t 及以前信息（滚动窗口尾随，无前视）
  - fwd5 = close[t+5] / close[t] - 1（品种内交易日 shift，仅用已实现收益）
  - 完整回测口径不在此重算（影子跟踪只做信号层监测；组合层复核走既有
    p5/p7 工具链）

用法
----
  python scripts/p12_s4_shadow_monitor.py --baseline    # 生成基线快照（v8 基线）
  python scripts/p12_s4_shadow_monitor.py --monitor     # 重算对比（数据刷新后）
  python scripts/p12_s4_shadow_monitor.py               # 默认 = monitor（无基线时自动生成）
  # 向后兼容 v2 对比（旧口径）：
  python scripts/p12_s4_shadow_monitor.py --baseline-a artifacts/signals_cache18_grouped_v2.parquet \
      --baseline-csv artifacts/p12_s4_shadow_baseline.csv --baseline
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ART = ROOT / "artifacts"
KLINE_DIR = ROOT / "data" / "raw" / "processed"
# P25-1：基线从 v2 升级为 v8（生产基线）；rt30 候选不变；v2 路径保留向后兼容
V2_PATH = ART / "signals_cache18_grouped_v2.parquet"
V8_PATH = ART / "signals_cache18_grouped_v8.parquet"
RT30_PATH = ART / "signals_cache18_grouped_v2_rt30.parquet"
DEFAULT_BASELINE_PATH = V8_PATH          # 默认基线 = v8（生产基线）
DEFAULT_CANDIDATE_PATH = RT30_PATH       # 默认候选 = rt30（P7 登记 S4 候选）
DEFAULT_BASELINE_LABEL = "v8"            # 基线标签（旧 v2 对比时传 "v2"）
DEFAULT_CANDIDATE_LABEL = "rt30"         # 候选标签
BASELINE_CSV = ART / "p12_s4_shadow_baseline.csv"        # 旧基线快照（v2 基线，保留）
BASELINE_CSV_V8 = ART / "p12_s4_shadow_baseline_v8.csv"  # 新基线快照（v8 基线，默认）

# 口径常量（与 p3/p5/p7 一致）
OOS_START = pd.Timestamp("2024-07-18")   # PandaData 独立采集起点
HORIZON = 5                              # 前瞻 5 交易日
ROLL_WINDOW = 63                         # 滚动窗口（交易日）
TOP_K = 0.30                             # 每日截面 top30% 做多（与引擎 A 一致）
MIN_SYMBOLS = 3                          # P5 终裁 S2：当日品种数下限

# 触发阈值（P12 影子跟踪登记；漂移阈值经基线标定重标定，见 docstring）
IC_THRESHOLD = 0.03                      # |滚动 IC| 门槛
TRIGGER_CONSEC = 5                       # 连续窗口数
HIT_REF = 0.50                           # 命中率参考值（持续 > 0.5 且 > v2 才算优）
CORR_ALERT = 0.50                        # 池化相关性绝对告警阈值（原登记 0.8 基线即误报）
CORR_WATCH_DELTA = 0.15                  # 池化相关性相对基线回落预警


def _spearman(a: pd.Series, b: pd.Series) -> float:
    """稳健 Spearman：兼容 scipy 新旧返回值接口，样本过少返回 NaN。"""
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    try:
        res = spearmanr(a, b)
    except Exception:
        return float("nan")
    return float(getattr(res, "statistic", getattr(res, "correlation", float("nan"))))


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------
def load_close_panel(kline_dir: Path = KLINE_DIR) -> pd.DataFrame:
    """加载全部品种日线 close → DataFrame[symbol, date, close]（date 归一化）。"""
    frames: list[pd.DataFrame] = []
    for sym_dir in sorted(kline_dir.iterdir()):
        if not sym_dir.is_dir():
            continue
        sym = sym_dir.name
        d = sym_dir / "1d"
        if not d.exists():
            continue
        for f in sorted(d.glob("*.parquet")):
            try:
                df = pd.read_parquet(f)
            except Exception:
                continue
            if "datetime" not in df.columns or "close" not in df.columns:
                continue
            sub = df[["datetime", "close"]].copy()
            sub["date"] = pd.to_datetime(sub["datetime"]).dt.normalize()
            sub["symbol"] = sym
            frames.append(sub[["symbol", "date", "close"]])
    if not frames:
        raise FileNotFoundError(f"no kline parquet under {kline_dir}")
    panel = pd.concat(frames, ignore_index=True)
    panel = panel.drop_duplicates(["symbol", "date"]).sort_values(["symbol", "date"])
    return panel.reset_index(drop=True)


def load_signals(path: Path) -> pd.DataFrame:
    """加载信号缓存并归一化 ts。"""
    df = pd.read_parquet(path)
    df["ts"] = pd.to_datetime(df["ts"]).dt.normalize()
    return df


def add_fwd_returns(sig: pd.DataFrame, close_panel: pd.DataFrame, horizon: int = HORIZON) -> pd.DataFrame:
    """为信号行添加前瞻收益 fwd_{horizon}（品种内交易日 shift，无前视）。

    fwd5 = close[ts+5] / close[ts] - 1；尾部不足 5 日返回 NaN（未实现收益不算）。
    """
    close = close_panel.set_index(["symbol", "date"])["close"].sort_index()
    fwd = close.groupby(level="symbol").shift(-horizon) / close - 1.0
    fwd.name = f"fwd_{horizon}"
    fwd.index = fwd.index.set_names(["symbol", "ts"])
    return sig.join(fwd, on=["symbol", "ts"])


# ---------------------------------------------------------------------------
# 逐日指标
# ---------------------------------------------------------------------------
def daily_cs_ic(sig: pd.DataFrame, oos_start: pd.Timestamp = OOS_START,
                min_symbols: int = MIN_SYMBOLS, horizon: int = HORIZON) -> pd.DataFrame:
    """每日截面 Spearman IC：exp_ret vs fwd_{horizon}（仅 OOS、>= min_symbols 品种）。

    返回 DataFrame[ts, ic, n]（ts 升序）。
    """
    col = f"fwd_{horizon}"
    df = sig[(sig["ts"] >= oos_start) & sig[col].notna()].copy()
    rows: list[tuple[pd.Timestamp, float, int]] = []
    for ts, g in df.groupby("ts"):
        if len(g) < min_symbols:
            continue
        r = _spearman(g["exp_ret"], g[col])
        if pd.notna(r):
            rows.append((ts, r, len(g)))
    if not rows:
        return pd.DataFrame(columns=["ts", "ic", "n"])
    return pd.DataFrame(rows, columns=["ts", "ic", "n"]).sort_values("ts").reset_index(drop=True)


def daily_hit_rate(sig: pd.DataFrame, oos_start: pd.Timestamp = OOS_START,
                   min_symbols: int = MIN_SYMBOLS, horizon: int = HORIZON,
                   top_k: float = TOP_K) -> pd.DataFrame:
    """每日命中率：exp_ret 截面 top{top_k} 品种的 fwd_{horizon} > 0 占比。

    返回 DataFrame[ts, hit, n_top]（ts 升序）。
    """
    col = f"fwd_{horizon}"
    df = sig[(sig["ts"] >= oos_start) & sig[col].notna()].copy()
    df["rank_pct"] = df.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    rows: list[tuple[pd.Timestamp, float, int]] = []
    for ts, g in df.groupby("ts"):
        if len(g) < min_symbols:
            continue
        top = g[g["rank_pct"] >= 1.0 - top_k]
        if len(top) == 0:
            continue
        rows.append((ts, float((top[col] > 0).mean()), len(top)))
    if not rows:
        return pd.DataFrame(columns=["ts", "hit", "n_top"])
    return pd.DataFrame(rows, columns=["ts", "hit", "n_top"]).sort_values("ts").reset_index(drop=True)


def overlap_corr(sig_rt: pd.DataFrame, sig_v2: pd.DataFrame, min_symbols: int = MIN_SYMBOLS) -> pd.DataFrame:
    """rt30 vs v2 同期信号相关性：每日截面 Spearman（>= min_symbols 重合品种）。

    返回 DataFrame[ts, corr, n]（ts 升序）+ 内部保存合并表供池化计算。
    """
    m = sig_rt[["symbol", "ts", "exp_ret"]].merge(
        sig_v2[["symbol", "ts", "exp_ret"]],
        on=["symbol", "ts"], suffixes=("_rt", "_v2"),
    )
    rows: list[tuple[pd.Timestamp, float, int]] = []
    for ts, g in m.groupby("ts"):
        if len(g) < min_symbols:
            continue
        r = _spearman(g["exp_ret_rt"], g["exp_ret_v2"])
        if pd.notna(r):
            rows.append((ts, r, len(g)))
    c = pd.DataFrame(rows, columns=["ts", "corr", "n"]).sort_values("ts").reset_index(drop=True)
    c.attrs["overlap"] = m
    return c


def pooled_spearman(corr_df: pd.DataFrame, window: int = ROLL_WINDOW) -> tuple[float, int]:
    """63 窗池化 Spearman：取最近 window 个重合日的所有 (symbol, ts) 对一次计算。

    返回 (corr, n_pairs)。比每日截面均值的滚动均值稳健（n=3 时每日 Spearman
    只能取 {-1, -0.5, 0.5, 1}，均值被粒度噪声主导）。
    """
    m: pd.DataFrame = corr_df.attrs.get("overlap", pd.DataFrame())
    if m.empty or len(corr_df) == 0:
        return float("nan"), 0
    tail_dates = set(corr_df["ts"].tail(window))
    sub = m[m["ts"].isin(tail_dates)]
    if len(sub) < 10:
        return float("nan"), len(sub)
    return _spearman(sub["exp_ret_rt"], sub["exp_ret_v2"]), len(sub)


# ---------------------------------------------------------------------------
# 指标聚合
# ---------------------------------------------------------------------------
def rolling_mean(s: pd.Series, window: int = ROLL_WINDOW) -> pd.Series:
    """滚动均值（满窗 min_periods=window；窗口未满为 NaN，尾随无前视）。"""
    return s.rolling(window, min_periods=window).mean()


def cache_metrics(sig: pd.DataFrame, close_panel: pd.DataFrame, label: str,
                  window: int | None = None) -> dict:
    """单个缓存的影子指标汇总（含逐日序列，供 monitor 复用）。

    ``window`` 为 None 时取模块常量 ROLL_WINDOW（调用时读取，避免定义期绑定）。
    """
    window = ROLL_WINDOW if window is None else window
    sig = add_fwd_returns(sig, close_panel, HORIZON)
    ic_df = daily_cs_ic(sig)
    hit_df = daily_hit_rate(sig)
    ic_roll = rolling_mean(ic_df.set_index("ts")["ic"], window)
    hit_roll = rolling_mean(hit_df.set_index("ts")["hit"], window)
    ic_last = float(ic_roll.dropna().iloc[-1]) if ic_roll.notna().any() else float("nan")
    hit_last = float(hit_roll.dropna().iloc[-1]) if hit_roll.notna().any() else float("nan")
    ic_last_date = ic_roll.dropna().index[-1] if ic_roll.notna().any() else None
    hit_last_date = hit_roll.dropna().index[-1] if hit_roll.notna().any() else None

    dates = pd.Index(pd.to_datetime(sig["ts"].unique()))
    oos_dates = dates[dates >= OOS_START]
    daily_cnt = sig.groupby("ts")["symbol"].count()
    return {
        "label": label,
        "n_rows": len(sig),
        "n_dates": int(dates.nunique()),
        "coverage_start": dates.min().date(),
        "coverage_end": dates.max().date(),
        "signal_max_date": dates.max().date(),
        "avg_symbols_per_day": float(daily_cnt.mean()),
        "n_effective": int(sig["is_effective"].sum()),
        "effective_ratio": float(sig["is_effective"].mean()),
        "oos_n_dates": int(oos_dates.nunique()),
        "oos_ic_days": int(ic_df.shape[0]),
        "ic_last_date": ic_last_date.date() if ic_last_date is not None else None,
        "rolling_ic_63d": ic_last,
        "hit_last_date": hit_last_date.date() if hit_last_date is not None else None,
        "rolling_hit_63d": hit_last,
        "ic_series": ic_roll,
        "hit_series": hit_roll,
    }


def corr_metrics(sig_rt: pd.DataFrame, sig_v2: pd.DataFrame,
                 window: int | None = None) -> dict:
    """rt30 vs v2 信号相关性汇总（漂移监测核心）。

    ``window`` 为 None 时取模块常量 ROLL_WINDOW（调用时读取，避免定义期绑定）。
    """
    window = ROLL_WINDOW if window is None else window
    corr_df = overlap_corr(sig_rt, sig_v2, MIN_SYMBOLS)
    daily_roll = rolling_mean(corr_df.set_index("ts")["corr"], window)
    pooled, n_pairs = pooled_spearman(corr_df, window)
    return {
        "corr_n_days": int(corr_df.shape[0]),
        "corr_last_date": corr_df["ts"].max().date() if corr_df.shape[0] else None,
        "corr_daily_rolling_63d": float(daily_roll.dropna().iloc[-1]) if daily_roll.notna().any() else float("nan"),
        "corr_pooled_63d": pooled,
        "corr_pooled_n_pairs": int(n_pairs),
    }


def kline_max_date(close_panel: pd.DataFrame) -> pd.Timestamp:
    return pd.Timestamp(close_panel["date"].max())


# ---------------------------------------------------------------------------
# 基线快照
# ---------------------------------------------------------------------------
def build_baseline(close_panel: pd.DataFrame, baseline_csv: Path = BASELINE_CSV_V8,
                   baseline_path: Path = DEFAULT_BASELINE_PATH,
                   candidate_path: Path = DEFAULT_CANDIDATE_PATH,
                   baseline_label: str = DEFAULT_BASELINE_LABEL,
                   candidate_label: str = DEFAULT_CANDIDATE_LABEL,
                   window: int | None = None) -> pd.DataFrame:
    """生成基线快照 CSV（两缓存各一行 + 共享相关性/环境列）。

    P25-1：默认基线 = v8（生产基线）、候选 = rt30；``--baseline-a v2 路径``
    可复现旧 v2-vs-rt30 对比。``window`` 为 None 时取模块常量 ROLL_WINDOW
    （调用时读取，避免定义期绑定）。
    """
    window = ROLL_WINDOW if window is None else window
    sig_base = load_signals(baseline_path)
    sig_cand = load_signals(candidate_path)
    m_base = cache_metrics(sig_base, close_panel, baseline_label, window=window)
    m_cand = cache_metrics(sig_cand, close_panel, candidate_label, window=window)
    cm = corr_metrics(sig_cand, sig_base, window=window)
    label_to_path = {baseline_label: baseline_path, candidate_label: candidate_path}

    rows = []
    for m in (m_base, m_cand):
        row = {
            "baseline_date": pd.Timestamp.now().normalize().date(),
            "cache": m["label"],
            "cache_path": str(label_to_path[m["label"]]),
            "n_rows": m["n_rows"],
            "n_dates": m["n_dates"],
            "coverage_start": m["coverage_start"],
            "coverage_end": m["coverage_end"],
            "signal_max_date": m["signal_max_date"],
            "avg_symbols_per_day": round(m["avg_symbols_per_day"], 3),
            "n_effective": m["n_effective"],
            "effective_ratio": round(m["effective_ratio"], 4),
            "oos_n_dates": m["oos_n_dates"],
            "oos_ic_days": m["oos_ic_days"],
            "ic_last_date": m["ic_last_date"],
            "rolling_ic_63d": round(m["rolling_ic_63d"], 6),
            "hit_last_date": m["hit_last_date"],
            "rolling_hit_63d": round(m["rolling_hit_63d"], 6),
            "corr_n_days": cm["corr_n_days"],
            "corr_last_date": cm["corr_last_date"],
            "corr_daily_rolling_63d": round(cm["corr_daily_rolling_63d"], 6),
            "corr_pooled_63d": round(cm["corr_pooled_63d"], 6),
            "corr_pooled_n_pairs": cm["corr_pooled_n_pairs"],
            "kline_max_date": kline_max_date(close_panel).date(),
            "window": window,
            "horizon": HORIZON,
            "oos_start": OOS_START.date(),
            "min_symbols": MIN_SYMBOLS,
            "top_k": TOP_K,
        }
        rows.append(row)
    df = pd.DataFrame(rows)
    ART.mkdir(exist_ok=True)
    df.to_csv(baseline_csv, index=False)
    return df


def load_baseline(baseline_csv: Path = BASELINE_CSV_V8) -> pd.DataFrame:
    if not baseline_csv.exists():
        raise FileNotFoundError(f"基线快照不存在：{baseline_csv}（先运行 --baseline）")
    return pd.read_csv(baseline_csv)


# ---------------------------------------------------------------------------
# 复核触发
# ---------------------------------------------------------------------------
def evaluate_trigger(ic_rt: pd.Series, ic_v2: pd.Series,
                     hit_rt: pd.Series, hit_v2: pd.Series,
                     consec: int = TRIGGER_CONSEC,
                     ic_threshold: float = IC_THRESHOLD,
                     hit_ref: float = HIT_REF) -> dict:
    """主触发评估：两缓存滚动指标对齐日期的尾部连续优势计数。

    返回 {ic_consec_wins, hit_consec_wins, n_common_dates, verdict}。
    """
    common = ic_rt.dropna().index.intersection(ic_v2.dropna().index)
    if len(common) == 0:
        return {"ic_consec_wins": 0, "hit_consec_wins": 0, "n_common_dates": 0,
                "verdict": "NO_TRIGGER_NO_ALIGN"}
    ic_rt_a = ic_rt.reindex(common)
    ic_v2_a = ic_v2.reindex(common)
    hit_rt_a = hit_rt.reindex(common)
    hit_v2_a = hit_v2.reindex(common)

    ic_win = (ic_rt_a > ic_v2_a) & (ic_rt_a.abs() > ic_threshold)
    ic_consec = 0
    for v in ic_win.iloc[::-1]:
        if v:
            ic_consec += 1
        else:
            break

    hit_win = (hit_rt_a > hit_v2_a) & (hit_rt_a > hit_ref)
    hit_consec = 0
    for v in hit_win.iloc[::-1]:
        if v:
            hit_consec += 1
        else:
            break

    triggered = ic_consec >= consec or hit_consec >= consec
    return {
        "ic_consec_wins": int(ic_consec),
        "hit_consec_wins": int(hit_consec),
        "n_common_dates": int(len(common)),
        "verdict": "UPGRADE_TRIGGER" if triggered else "NO_TRIGGER",
    }


def evaluate_drift(corr_now: float, corr_baseline: float,
                   alert: float = CORR_ALERT, watch_delta: float = CORR_WATCH_DELTA) -> str:
    """漂移判定：绝对告警 + 相对基线回落预警。"""
    if pd.isna(corr_now):
        return "DRIFT_UNKNOWN"
    if corr_now < alert:
        return "DRIFT_ALERT"
    if pd.notna(corr_baseline) and corr_now < corr_baseline - watch_delta:
        return "DRIFT_WATCH"
    return "DRIFT_OK"


def run_monitor(close_panel: pd.DataFrame, baseline_csv: Path = BASELINE_CSV_V8,
                baseline_path: Path = DEFAULT_BASELINE_PATH,
                candidate_path: Path = DEFAULT_CANDIDATE_PATH,
                baseline_label: str = DEFAULT_BASELINE_LABEL,
                candidate_label: str = DEFAULT_CANDIDATE_LABEL,
                verbose: bool = True, window: int | None = None,
                consec: int | None = None, ic_threshold: float | None = None,
                corr_alert: float | None = None,
                corr_watch_delta: float | None = None) -> dict:
    """重算当前指标并与基线对比（数据刷新后调用）。

    P25-1：默认基线 = v8（生产基线）、候选 = rt30；所有可调参数为 None 时取
    模块常量（调用时读取，避免定义期绑定）——保证 CLI 覆盖（--window/--consec/
    --ic-threshold/--corr-alert/--corr-watch-delta）真实生效。
    """
    window = ROLL_WINDOW if window is None else window
    consec = TRIGGER_CONSEC if consec is None else consec
    ic_threshold = IC_THRESHOLD if ic_threshold is None else ic_threshold
    corr_alert = CORR_ALERT if corr_alert is None else corr_alert
    corr_watch_delta = CORR_WATCH_DELTA if corr_watch_delta is None else corr_watch_delta

    sig_base = load_signals(baseline_path)
    sig_cand = load_signals(candidate_path)
    m_base = cache_metrics(sig_base, close_panel, baseline_label, window=window)
    m_cand = cache_metrics(sig_cand, close_panel, candidate_label, window=window)
    cm = corr_metrics(sig_cand, sig_base, window=window)
    baseline = load_baseline(baseline_csv)

    kline_now = kline_max_date(close_panel)
    kline_base = pd.Timestamp(baseline["kline_max_date"].iloc[0])
    sig_max_base = {
        row["cache"]: pd.Timestamp(row["signal_max_date"]) for _, row in baseline.iterrows()
    }
    fresh_kline = kline_now > kline_base
    fresh_signal = any(
        pd.Timestamp(m["signal_max_date"]) > sig_max_base[m["label"]]
        for m in (m_base, m_cand)
    )
    fresh = fresh_kline or fresh_signal

    # 主触发评估：始终在当前数据上计算（回顾性），fresh-OOS 数据出现时
    # 才可作为升级证据（见 trigger['fresh_confirmed'] 标注）。
    trigger = evaluate_trigger(m_cand["ic_series"], m_base["ic_series"],
                               m_cand["hit_series"], m_base["hit_series"],
                               consec=consec, ic_threshold=ic_threshold)
    trigger["fresh_confirmed"] = bool(fresh)
    if not fresh:
        trigger["verdict"] = "UPGRADE_TRIGGER_RETRO" if trigger["verdict"] == "UPGRADE_TRIGGER" else "NO_TRIGGER"

    corr_base = float(baseline["corr_pooled_63d"].iloc[0])
    drift = evaluate_drift(cm["corr_pooled_63d"], corr_base,
                           alert=corr_alert, watch_delta=corr_watch_delta)

    result = {
        "monitor_date": pd.Timestamp.now().normalize().date(),
        "params": {"window": window, "consec": consec, "ic_threshold": ic_threshold,
                   "corr_alert": corr_alert, "corr_watch_delta": corr_watch_delta},
        "baseline_label": baseline_label,
        "candidate_label": candidate_label,
        "labels": [baseline_label, candidate_label],
        "fresh_kline": bool(fresh_kline),
        "fresh_signal": bool(fresh_signal),
        "kline_now": kline_now.date(),
        "kline_base": kline_base.date(),
        baseline_label: {k: (v.date() if isinstance(v, pd.Timestamp) else v)
                         for k, v in m_base.items() if k not in ("ic_series", "hit_series")},
        candidate_label: {k: (v.date() if isinstance(v, pd.Timestamp) else v)
                          for k, v in m_cand.items() if k not in ("ic_series", "hit_series")},
        "corr": cm,
        "trigger": trigger,
        "drift": drift,
        "corr_baseline": corr_base,
    }
    if verbose:
        print_monitor_summary(result, baseline)
    return result


def print_monitor_summary(result: dict, baseline: pd.DataFrame) -> None:
    """monitor 模式 stdout 摘要。"""
    bl = result["baseline_label"]
    cl = result["candidate_label"]
    print("=" * 92)
    print(f"P12-2 S4 影子跟踪 — 复核（{result['monitor_date']}）[{cl}(候选) vs {bl}(基线)]")
    print("=" * 92)
    print(f"K 线最新: {result['kline_now']} (基线 {result['kline_base']}) | "
          f"新 K 线: {'YES' if result['fresh_kline'] else 'NO'} | "
          f"新信号: {'YES' if result['fresh_signal'] else 'NO'}")
    for label in (bl, cl):
        m = result[label]
        print(f"  [{label:<4}] 滚动IC={m['rolling_ic_63d']:+.4f} (末值 {m['ic_last_date']}) | "
              f"滚动命中率={m['rolling_hit_63d']:.3f} (末值 {m['hit_last_date']})")
    c = result["corr"]
    print(f"  信号相关性: 池化63窗={c['corr_pooled_63d']:.4f} (n_pairs={c['corr_pooled_n_pairs']}) | "
          f"日截面滚动均值={c['corr_daily_rolling_63d']:.4f} | 末值 {c['corr_last_date']}")
    print(f"  漂移判定: {result['drift']} (基线池化={result['corr_baseline']:.4f}, "
          f"告警阈值={result['params']['corr_alert']:.2f})")
    t = result["trigger"]
    p = result["params"]
    retro_note = ""
    if t["verdict"] == "UPGRADE_TRIGGER_RETRO":
        retro_note = ("  [注意] 触发条件在**现有数据**上已满足（回顾性，位于 P7 OOS 段内）——"
                      "非 fresh-OOS 证据；需 2026-08-18 后新数据刷新确认后方可进入升级评估")
    print(f"  主触发: {t['verdict']} (IC连续胜={t['ic_consec_wins']}/{p['consec']}, "
          f"命中率连续胜={t['hit_consec_wins']}/{p['consec']}, 对齐日={t['n_common_dates']}, "
          f"fresh确认={'YES' if t['fresh_confirmed'] else 'NO'})")
    if not (result["fresh_kline"] or result["fresh_signal"]):
        print("  [NOTE] 无 fresh-OOS 数据（2026-08-18 后无新 K 线/新信号）→ fresh-OOS 复核待数据刷新；"
              "当前指标 = 现有数据最近滚动窗口表现（与基线一致属预期）")
    if retro_note:
        print(retro_note)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def print_baseline_summary(df: pd.DataFrame, close_panel: pd.DataFrame) -> None:
    """baseline 模式 stdout 摘要。"""
    baseline_label = df["cache"].iloc[0]  # 第一行 = 基线（build_baseline 顺序保证）
    print("=" * 92)
    print(f"P12-2 S4 影子跟踪 — 基线快照（{df['baseline_date'].iloc[0]}）[基线={baseline_label}]")
    print("=" * 92)
    print(f"K 线最新日期: {df['kline_max_date'].iloc[0]} | OOS 起点: {df['oos_start'].iloc[0]} | "
          f"窗口: {df['window'].iloc[0]} | 前瞻: {df['horizon'].iloc[0]} | min品种: {df['min_symbols'].iloc[0]}")
    print(f"[基线标注] 基线缓存 = {df['cache_path'].iloc[0]}（P25-1：v8 生产基线）")
    print(f"\n[缓存概览]")
    for _, r in df.iterrows():
        print(f"  {r['cache']:<6}: {r['n_rows']} 行 | {r['n_dates']} 日 | "
              f"{r['coverage_start']} ~ {r['coverage_end']} | 日均 {r['avg_symbols_per_day']:.1f} 品种 | "
              f"OOS {r['oos_n_dates']} 日 (IC日 {r['oos_ic_days']})")
    print(f"\n[滚动指标末值 (63窗滚动均值, OOS)]")
    for _, r in df.iterrows():
        print(f"  {r['cache']:<6}: 滚动截面IC={r['rolling_ic_63d']:+.4f} (末值 {r['ic_last_date']}) | "
              f"滚动命中率={r['rolling_hit_63d']:.3f} (末值 {r['hit_last_date']})")
    print(f"\n[信号相关性 (漂移监测核心)]")
    r = df.iloc[0]
    print(f"  池化63窗 Spearman = {r['corr_pooled_63d']:.4f} (n_pairs={r['corr_pooled_n_pairs']}) | "
          f"日截面滚动均值 = {r['corr_daily_rolling_63d']:.4f} | 末值 {r['corr_last_date']}")
    print(f"\n[如实说明] 当前无 fresh-OOS（2026-08-18 后）数据：基线快照 = 现有数据最近滚动窗口表现；"
          f"复核触发待数据刷新（K 线刷新 / 缓存重建）")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="P12-2 S4 影子跟踪框架（候选 vs 基线；P25-1 默认基线=v8 生产基线）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--baseline", action="store_true", help="生成基线快照")
    group.add_argument("--monitor", action="store_true", help="重算对比（数据刷新后）")
    # P25-1：基线/候选路径可显式切换（向后兼容 v2 对比）；环境变量兜底
    parser.add_argument("--baseline-a", type=str, default=None,
                        help=f"基线缓存路径（默认 {DEFAULT_BASELINE_PATH.name}；"
                             f"可用环境变量 P12_BASELINE_A，CLI 优先）")
    parser.add_argument("--baseline-b", type=str, default=None,
                        help=f"候选缓存路径（默认 {DEFAULT_CANDIDATE_PATH.name}；"
                             f"可用环境变量 P12_BASELINE_B，CLI 优先）")
    parser.add_argument("--baseline-csv", type=str, default=None,
                        help=f"基线快照 CSV 路径（默认 {BASELINE_CSV_V8.name}，v8 基线）")
    parser.add_argument("--baseline-label-a", type=str, default=None,
                        help=f"基线标签（默认 {DEFAULT_BASELINE_LABEL}；旧 v2 对比传 v2）")
    parser.add_argument("--baseline-label-b", type=str, default=None,
                        help=f"候选标签（默认 {DEFAULT_CANDIDATE_LABEL}）")
    parser.add_argument("--window", type=int, default=None, help=f"滚动窗口（默认 {ROLL_WINDOW}）")
    parser.add_argument("--consec", type=int, default=None, help=f"触发连续窗口数（默认 {TRIGGER_CONSEC}）")
    parser.add_argument("--ic-threshold", type=float, default=None, help=f"滚动 IC 门槛（默认 {IC_THRESHOLD}）")
    parser.add_argument("--corr-alert", type=float, default=None, help=f"池化相关性绝对告警阈值（默认 {CORR_ALERT}）")
    parser.add_argument("--corr-watch-delta", type=float, default=None,
                        help=f"池化相关性相对基线回落预警幅度（默认 {CORR_WATCH_DELTA}）")
    parser.add_argument("--json", action="store_true", help="stdout 末尾输出 JSON 结果块")
    args = parser.parse_args()

    # 显式解析覆盖参数（None → 模块常量/环境变量/默认值）；所有覆盖值通过
    # 函数参数显式透传，不使用 globals() 修改（默认参数在函数定义期绑定，
    # 全局修改会静默失效）。
    window = ROLL_WINDOW if args.window is None else args.window
    consec = TRIGGER_CONSEC if args.consec is None else args.consec
    ic_threshold = IC_THRESHOLD if args.ic_threshold is None else args.ic_threshold
    corr_alert = CORR_ALERT if args.corr_alert is None else args.corr_alert
    corr_watch_delta = CORR_WATCH_DELTA if args.corr_watch_delta is None else args.corr_watch_delta

    # P25-1 路径/标签解析：CLI > 环境变量 > 模块默认（v8 基线 / rt30 候选）
    baseline_path = Path(args.baseline_a) if args.baseline_a else \
        Path(os.environ.get("P12_BASELINE_A", str(DEFAULT_BASELINE_PATH)))
    candidate_path = Path(args.baseline_b) if args.baseline_b else \
        Path(os.environ.get("P12_BASELINE_B", str(DEFAULT_CANDIDATE_PATH)))
    baseline_csv = Path(args.baseline_csv) if args.baseline_csv else BASELINE_CSV_V8
    baseline_label = args.baseline_label_a if args.baseline_label_a else DEFAULT_BASELINE_LABEL
    candidate_label = args.baseline_label_b if args.baseline_label_b else DEFAULT_CANDIDATE_LABEL

    t0 = time.time()
    close_panel = load_close_panel()
    baseline_exists = baseline_csv.exists()

    if args.baseline or (not args.monitor and not baseline_exists):
        df = build_baseline(close_panel, baseline_csv=baseline_csv,
                            baseline_path=baseline_path, candidate_path=candidate_path,
                            baseline_label=baseline_label, candidate_label=candidate_label,
                            window=window)
        print_baseline_summary(df, close_panel)
        print(f"\n[OK] 基线快照（{baseline_label} 基线） → {baseline_csv} | 耗时 {time.time() - t0:.1f}s")
        if args.json:
            print("\n===JSON===")
            print(json.dumps({"mode": "baseline",
                              "baseline": df.replace({pd.NaT: None}).astype(object).to_dict("records")},
                             ensure_ascii=False, default=str))
        return

    result = run_monitor(close_panel, baseline_csv=baseline_csv,
                         baseline_path=baseline_path, candidate_path=candidate_path,
                         baseline_label=baseline_label, candidate_label=candidate_label,
                         window=window, consec=consec,
                         ic_threshold=ic_threshold, corr_alert=corr_alert,
                         corr_watch_delta=corr_watch_delta)
    print(f"\n[OK] 复核完成 | 耗时 {time.time() - t0:.1f}s")
    if args.json:
        print("\n===JSON===")
        print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
