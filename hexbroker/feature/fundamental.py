"""基本面特征（基差，严格因果，P8-2）。

把品种基本面数据（基差/基差率）对齐到 K 线 datetime 索引，生成：

- ``f_basis_ratio``：原始基差率（ffill 对齐，只取 t 日及以前最新值）
- ``f_basis_ratio_rank``：品种内滚动 252 日分位（min_periods=60）——引擎 B 已验证形态
- ``f_basis_ratio_z``：品种内滚动 252 日 z-score（min_periods=60）
- ``f_basis``：绝对基差（可选，注意不同品种量纲不同；树模型可自适应）

因果性（QA 审查重点）
--------------------
1. **对齐无前视**：对每个 K 线日 t，取基本面日期 ``<= t`` 的最新值
   （``reindex + ffill``，当日及以前信息）。
2. **滚动统计无前视**：分位/z-score 在**原始基本面日期网格**上计算
   （与引擎 B ``scripts/p2_basis_backtest.load_basis_panel`` 完全一致：
   ``rolling(252, min_periods=60).rank(pct=True)``），只使用截至该日的过去
   252 个基本面观测；再把结果 ffill 对齐到 K 线网格。避免先 ffill 成日频后再
   滚动导致的重复值计数扭曲。
3. **缺失处理**：早于首个基本面观测日 / 无基本面数据的品种 → NaN；
   ``hexbroker/forecast/base.build_windows``（既有行为）在训练前将 NaN fill 0.0，
   等价于「中性填充 0」（LightGBM 缺失分支也可原生处理，两者均因果）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# 引擎 B 已验证参数（P2/P3 定案）：品种内滚动 252 日分位，min_periods=60
DEFAULT_WINDOW = 252
DEFAULT_MIN_PERIODS = 60


def _norm_series(s: pd.Series) -> pd.Series:
    """规范化输入序列：去重（保留最后）、升序、float。"""
    out = s.astype(float)
    out = out[~out.index.duplicated(keep="last")]
    return out.sort_index()


def _rolling_zscore(
    s: pd.Series, window: int = DEFAULT_WINDOW, min_periods: int = DEFAULT_MIN_PERIODS
) -> pd.Series:
    """严格因果滚动 z-score：只用截至 t 的过去 window 个观测。"""
    mean = s.rolling(window, min_periods=min_periods).mean()
    std = s.rolling(window, min_periods=min_periods).std(ddof=0)
    return (s - mean) / std.replace(0, np.nan)


def add_fundamental(
    df: pd.DataFrame,
    fundamental_close: pd.DataFrame | pd.Series | None,
    sym_label: str,
    params: dict | None = None,
) -> pd.DataFrame:
    """在 df 上追加基本面（基差）特征列，返回新的 DataFrame（含原始列 + 特征列）。

    参数
    ----
    df : 单标的、datetime 索引 DataFrame（对齐目标；任意列均可，不依赖 close）。
    fundamental_close : 品种基本面数据，二选一：
        - ``pd.DataFrame``：datetime 索引，含 ``basis_ratio`` 列（必需）与 ``basis`` 列（可选）
        - ``pd.Series``：datetime 索引的 ``basis_ratio`` 序列（无绝对基差）
        - ``None``：该品种无基本面数据 → 特征列全 NaN（模型侧中性处理）
    sym_label : 当前品种短名（如 'au'），仅用于诊断。
    params : 可选。
        - ``{"window": int}``：滚动窗口（默认 252，引擎 B 定案）
        - ``{"min_periods": int}``：滚动最小观测数（默认 60，引擎 B 定案）
        - ``{"include_basis": bool}``：是否生成 ``f_basis`` 绝对基差（默认 True）

    返回
    ----
    追加了 ``f_basis_ratio`` / ``f_basis_ratio_rank`` / ``f_basis_ratio_z``
    （及可选 ``f_basis``）列的 df。
    """
    params = params or {}
    window = int(params.get("window", DEFAULT_WINDOW))
    min_periods = int(params.get("min_periods", DEFAULT_MIN_PERIODS))
    include_basis = bool(params.get("include_basis", True))

    out = df.copy()
    if fundamental_close is None:
        out["f_basis_ratio"] = np.nan
        out["f_basis_ratio_rank"] = np.nan
        out["f_basis_ratio_z"] = np.nan
        if include_basis:
            out["f_basis"] = np.nan
        return out

    # ---- 输入规范化：分离 basis_ratio 与 basis ----
    if isinstance(fundamental_close, pd.Series):
        br_raw = _norm_series(fundamental_close)
        basis_raw = None
    else:
        if "basis_ratio" not in fundamental_close.columns:
            raise ValueError(
                f"fundamental_close（{sym_label}）缺少 basis_ratio 列，"
                f"实际列：{list(fundamental_close.columns)}"
            )
        br_raw = _norm_series(fundamental_close["basis_ratio"])
        basis_raw = (
            _norm_series(fundamental_close["basis"])
            if include_basis and "basis" in fundamental_close.columns
            else None
        )

    # ---- 滚动统计在原始基本面日期网格上计算（引擎 B 形态，严格因果） ----
    rank_raw = br_raw.rolling(window, min_periods=min_periods).rank(pct=True)
    z_raw = _rolling_zscore(br_raw, window, min_periods)

    # ---- ffill 对齐到 K 线索引：对每个 K 线日 t 取基本面日期 <= t 的最新值 ----
    out["f_basis_ratio"] = br_raw.reindex(out.index).ffill()
    out["f_basis_ratio_rank"] = rank_raw.reindex(out.index).ffill()
    out["f_basis_ratio_z"] = z_raw.reindex(out.index).ffill()
    if include_basis:
        if basis_raw is not None:
            out["f_basis"] = basis_raw.reindex(out.index).ffill()
        else:
            out["f_basis"] = np.nan
    return out


def fundamental_columns(df: pd.DataFrame) -> list[str]:
    """返回 df 中以 ``f_basis`` 开头的基差特征列名。"""
    return [c for c in df.columns if c.startswith("f_basis")]
