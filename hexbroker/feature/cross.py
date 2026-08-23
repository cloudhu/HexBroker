"""跨品种特征（第三轮特征工程，严格因果）。

两类特征：

A. 内盘三品种相对比值（``add_internal_ratios``）
   au/ag/m 同一交易时段同时收盘，t 时点比值 = 各品种 t 收盘之比，无时差问题，
   严格因果。经典套利/相对强弱信号：
   - 金银比 f_xr_au_ag（黄金/白银），高位均值回复
   - 金豆比 f_xr_au_m、银豆比 f_xr_ag_m（贵金属 vs 农产品相对强弱）

B. 外盘参照比值（``add_cross_global``）
   标普500(SPX)、美元指数(UDI)、WTI 原油(CL) 等全球宏观因子。
   **时差安全（关键）**：外盘收盘（尤其美盘）发生在内盘收盘之后，调用方必须先做：
   ``global_shifted = global_close.shift(1)`` 再 ``asof`` 对齐到内盘交易日，
   本模块只消费已对齐且已 shift(1) 的外盘序列，保证 t 日特征仅依赖外盘 t-1 及以前信息。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Any


def add_internal_ratios(
    df: pd.DataFrame,
    close_panel: pd.DataFrame,
    sym_label: str,
    params: dict | None = None,
) -> pd.DataFrame:
    """在 df 上追加内盘两两品种的相对比值特征（严格因果，同交易时段）。

    对每一对品种 (a, b)（字典序 a<b）统一输出列 ``f_xr_{a}_{b}`` = log(close_a / close_b)，
    所有品种均用 close_panel 计算该列，故各品种行该列都有值（无 NaN 列问题）。

    参数
    ----
    df : 单标的、datetime 索引 DataFrame（含 close 列，用于对齐）。
    close_panel : datetime × symbol_short 的 close 宽表（含全部内盘品种）。
    sym_label : 当前品种短名（如 'au'），仅用于诊断。
    params : 可选，``{"include": [...]}`` 白名单（用列名 f_xr_{a}_{b}）。
    """
    params = params or {}
    include = params.get("include")
    if include is not None:
        include = set(include)

    def want(name: str) -> bool:
        return include is None or name in include

    out = df.copy()
    # 归一化 close_panel 为宽表（datetime × symbol）
    if isinstance(close_panel.index, pd.MultiIndex):
        panel = close_panel[["close"]].unstack(level=0)["close"]
    else:
        panel = close_panel
    if isinstance(panel.columns, pd.MultiIndex):
        panel.columns = panel.columns.get_level_values(0)
    panel = panel.reindex(out.index).astype(float)
    syms = [c for c in panel.columns if c != "close"]
    # 稳定优先级：au < ag < m（贵金属在前），保证列名 f_xr_{a}_{b} 字典序稳定
    _PRIORITY = {"au": 0, "ag": 1, "m": 2}
    syms.sort(key=lambda s: (_PRIORITY.get(s, 99), s))
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            a, b = syms[i], syms[j]
            fname = f"f_xr_{a}_{b}"
            if not want(fname):
                continue
            pa = panel[a].replace(0, np.nan)
            pb = panel[b].replace(0, np.nan)
            ratio = pa / pb
            out[fname] = np.log(ratio).fillna(0.0)
    return out


def add_cross_global(
    df: pd.DataFrame,
    global_close: dict[str, pd.Series],
    sym_label: str,
    params: dict | None = None,
) -> pd.DataFrame:
    """在 df 上追加跨品种特征列。

    参数
    ----
    df : 单标的、datetime 索引 DataFrame（含 close 列）。
    global_close : {外盘代码: 已 shift(1) 且 asof 对齐到 df.index 的收盘 Series}。
    sym_label : 当前内盘品种短名（如 'au'），用于特征命名。
    params : 可选。
      - ``{"include": [...]}`` 白名单（按特征名精确过滤）
      - ``{"global_codes": [code...]}`` 组模式：每个 code 生成全部 3 类特征
        （f_xr_{sym}_{code} 比值 / f_xr_{code}_mom 动量 / f_xr_{code}_vol 波动率）。
        组模式优先于 include（若提供 global_codes 则按组生成）。
    """
    params = params or {}
    include = params.get("include")
    if include is not None:
        include = set(include)

    def want(name: str) -> bool:
        return include is None or name in include

    global_codes = params.get("global_codes")
    if global_codes is not None:
        global_codes = set(global_codes)

    out = df.copy()
    close = out["close"].astype(float)
    for code, g_close in (global_close or {}).items():
        if global_codes is not None and code not in global_codes:
            continue
        g = g_close.reindex(close.index).astype(float)
        ratio = close / g.replace(0, np.nan)
        # 1. 比值对数（相对定价）：统一列名 f_xr_{code}_ratio（每品种用自己的 close 计算，
        #    concat 后各品种同列均有值，避免跨品种 NaN）
        fname = f"f_xr_{code}_ratio"
        if global_codes is not None or want(fname):
            out[fname] = np.log(ratio).fillna(0.0)
        # 2. 外盘自身 5 日动量（全局风险偏好）
        fname_mom = f"f_xr_{code}_mom"
        if global_codes is not None or want(fname_mom):
            g_log = np.log(g).diff().fillna(0.0)
            out[fname_mom] = g_log.rolling(5, min_periods=2).sum().fillna(0.0)
        # 3. 外盘 20 日已实现波动率（风险偏好代理）
        fname_vol = f"f_xr_{code}_vol"
        if global_codes is not None or want(fname_vol):
            g_log = np.log(g).diff().fillna(0.0)
            out[fname_vol] = g_log.rolling(20, min_periods=5).std().fillna(0.0)
    return out


def cross_columns(df: pd.DataFrame) -> list[str]:
    """返回 df 中以 ``f_xr_`` 开头的跨品种特征列。"""
    return [c for c in df.columns if c.startswith("f_xr_")]


def _norm_sym(s: str) -> str:
    """规范化品种短名：'SHFE.au'/'au0'/'AU0' -> 'au'（去点前缀与尾部数字）。"""
    base = s.split(".")[-1]
    return base.rstrip("0123456789").lower()


def _is_inner_ratio(feature_name: str, inner_syms: set) -> bool:
    """判断特征名是否为内盘比值 f_xr_{a}_{b}（a/b 均在 inner_syms 中）。"""
    parts = feature_name.split("_")
    return len(parts) == 4 and parts[0] == "f" and parts[1] == "xr" \
        and parts[2] in inner_syms and parts[3] in inner_syms


def _panel_close_wide(barframe: Any) -> pd.DataFrame:
    """从 BarFrame 提取 close 宽表（datetime × symbol_short）。

    L5 修复：若多个不同合约归一为同一短名（如 'au0' 与 'au2506' 均→'au'），
    原 ``pivot_table`` 默认 ``aggfunc='mean'`` 会**静默平均**两个不同品种的收盘 →
    跨品种特征污染。此处 pivot 前按短名去重，每短名仅保留一个代表合约
    （优先连续主力：全名以 '0' 结尾，如 au0/ag0；否则取全名字典序最小者），
    杜绝静默平均，同时保留短名特征命名约定（f_xr_au_ag 不变）。
    """
    df = barframe.df[["close"]].copy()
    sym_full = df.index.get_level_values("symbol")
    short = sym_full.map(_norm_sym)
    df["sym_short"] = short
    df["sym_full"] = sym_full
    # 选代表合约：优先全名以 '0' 结尾（连续主力约定），否则全名字典序最小
    rep: dict[str, str] = {}
    for s_full, s_short in zip(sym_full, short):
        if s_short not in rep:
            rep[s_short] = s_full
        else:
            cur = rep[s_short]
            if not cur.endswith("0") and s_full.endswith("0"):
                rep[s_short] = s_full
    keep = set(rep.values())
    df = df[df["sym_full"].isin(keep)]
    panel = df.reset_index().pivot_table(index="datetime", columns="sym_short", values="close")
    return panel
