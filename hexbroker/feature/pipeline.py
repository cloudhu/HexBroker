"""特征层流水线（§2.2 L2 / §8.3）。

把 ``BarFrame`` 转换为 ``FeatureFrame``：逐标的、严格因果地依次应用
``technical`` → ``microstructure`` → ``normalize``，输出仅含 ``f_*`` 特征列、
带 ``MultiIndex(symbol, datetime)`` 的 DataFrame。

设计要点：
- 每个 transformer 只读取该标的的历史（因果）。
- ``normalize`` 使用滚动 z-score（见 ``normalize.RollingNormalizer``），零泄漏。
- 流水线可序列化重放（相同 cfg + 相同输入 → 相同输出）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ..constants import Freq
from .cross import _is_inner_ratio, _norm_sym, _panel_close_wide, add_cross_global, add_internal_ratios
from .fundamental import add_fundamental
from .iterative import add_iterative
from .microstructure import add_microstructure
from .normalize import RollingNormalizer
from .technical import add_technical, technical_columns
from .weekly import add_weekly


@dataclass
class FeatureFrame:
    """特征矩阵（数据契约）。"""

    df: pd.DataFrame  # MultiIndex(symbol, datetime)
    freq: str = Freq.D1
    source: str = "feature_pipeline"
    metadata: dict = field(default_factory=dict)

    @property
    def symbols(self) -> list[str]:
        return list(self.df.index.get_level_values(0).unique())

    @property
    def length(self) -> int:
        return len(self.df)

    def by_symbol(self, symbol: str) -> pd.DataFrame:
        return self.df.loc[[symbol]].sort_index()

    def validate(self) -> None:
        if not isinstance(self.df.index, pd.MultiIndex):
            raise ValueError("FeatureFrame.df 必须是 MultiIndex(symbol, datetime)")
        if self.df.index.nlevels != 2:
            raise ValueError("FeatureFrame.df 必须为两级索引")
        if len(self.df) == 0:
            raise ValueError("FeatureFrame.df 不能为空")
        if not any(c.startswith("f_") for c in self.df.columns):
            raise ValueError("FeatureFrame 至少应包含一个 f_* 特征列")

    def to_frame(self) -> pd.DataFrame:
        return self.df


class FeaturePipeline:
    """可组合的特征流水线。"""

    def __init__(
        self,
        cfg: Any,
        global_close: dict[str, pd.Series] | None = None,
        fundamental_data: dict[str, pd.DataFrame | pd.Series] | None = None,
    ) -> None:
        self.cfg = cfg
        self.global_close = global_close or {}
        self.fundamental_data = fundamental_data or {}  # {品种短名: 基差 DataFrame/Series}
        self.close_panel: pd.DataFrame | None = None  # datetime × sym_short 宽表
        fc = getattr(cfg, "feature", None)
        self.transformers = list(getattr(fc, "transformers", ["technical", "microstructure", "normalize"]))
        self.normalize_window = int(getattr(fc, "normalize_window", 120))
        self.technical_params = dict(getattr(fc, "technical_params", {}) or {})
        self.iterative_params = dict(getattr(fc, "iterative_params", {}) or {})
        self.cross_params = dict(getattr(fc, "cross_params", {}) or {})
        self.weekly_params = dict(getattr(fc, "weekly_params", {}) or {})
        self.fundamental_params = dict(getattr(fc, "fundamental_params", {}) or {})
        # fail-fast：配置了外盘组但未提供外盘数据 → 显式报错（防特征静默缺失）
        # 只校验 global_codes（内盘比值 f_xr_{a}_{b} 无需外盘数据，不宜在此区分）
        need_global = bool(self.cross_params.get("global_codes"))
        if "cross" in self.transformers and need_global and not self.global_close:
            raise ValueError(
                "feature.cross_params 配置了外盘 global 特征（global_codes），"
                "但未传入 global_close 数据。请先 load_global_close + align_global_to_inner "
                "（见 hexbroker/feature/global_ref.py）。"
            )
        # fail-fast：配置了 fundamental 但未提供基本面数据 → 显式报错（防特征静默缺失）
        if "fundamental" in self.transformers and not self.fundamental_data:
            raise ValueError(
                "feature.transformers 包含 'fundamental'，但未传入 fundamental_data "
                "（dict[str, pd.DataFrame/Series]，键=品种短名如 'au'）。"
            )
        # 特征级白名单（特征选择裁剪）：None=保留全部；否则只保留列出的 f_ 特征
        keep = getattr(fc, "keep_features", None)
        self.keep_features = set(keep) if keep else None
        self._normalizer = RollingNormalizer(window=self.normalize_window, min_periods=5)

    def _per_symbol(self, raw: pd.DataFrame) -> pd.DataFrame:
        """对单标的（datetime 索引）执行流水线。"""
        # barframe.by_symbol 返回的是 MultiIndex(symbol, datetime)，先拍平为纯 DatetimeIndex
        if isinstance(raw.index, pd.MultiIndex):
            df = raw.copy()
            df.index = df.index.get_level_values(1)
        else:
            df = raw.copy()
        sym_label = str(getattr(self, "_sym_label", df.index.name or "sym")).lower()
        if "technical" in self.transformers:
            df = add_technical(df, self.technical_params)
        if "microstructure" in self.transformers:
            df = add_microstructure(df)
        if "iterative" in self.transformers:
            df = add_iterative(df, self.iterative_params)
        if "weekly" in self.transformers:
            df = add_weekly(df, self.weekly_params)
        if "cross" in self.transformers:
            # 内盘比值：仅当 include 显式含 f_xr_{a}_{b}（a/b 均为内盘品种）时生成
            inc = self.cross_params.get("include") or []
            inner_syms = getattr(self, "_inner_syms", set())
            need_inner = any(_is_inner_ratio(x, inner_syms) for x in inc) if inner_syms else False
            if self.close_panel is not None and need_inner:
                df = add_internal_ratios(df, self.close_panel, sym_label, self.cross_params)
            if self.global_close:
                df = add_cross_global(df, self.global_close, sym_label, self.cross_params)
        if "fundamental" in self.transformers:
            # 基本面（基差）：按品种短名注入，ffill 对齐 + 品种内滚动分位/z（严格因果）
            fdata = self.fundamental_data.get(sym_label)
            df = add_fundamental(df, fdata, sym_label, self.fundamental_params)
        feat_cols = technical_columns(df)
        if "normalize" in self.transformers and feat_cols:
            # 基差特征不参与滚动 z-score 标准化：rank/z 已是无量纲形态（引擎 B 定案），
            # 原始 ratio/basis 的量纲差异由树模型按特征分裂自适应（分组建模组内品种数少）。
            norm_cols = [c for c in feat_cols if not c.startswith("f_basis")]
            if norm_cols:
                sub = df[norm_cols]
                # L3 修复：每品种独立归一。原 self._normalizer 在首次 fit 后锁定 columns，
                # 后续品种新增特征列不会被归一 → 泄漏未归一特征。改为每品种新建 normalizer，
                # 各品种按自身列集合独立滚动 z-score（因果性不变，零跨品种污染）。
                normed = RollingNormalizer(
                    window=self.normalize_window, min_periods=5
                ).fit_transform(sub)
                df = df.drop(columns=norm_cols)
                df = pd.concat([df, normed], axis=1)
        # 最终只保留特征列（可选：特征级白名单裁剪）
        df = df[[c for c in df.columns if c.startswith("f_")]]
        if self.keep_features:
            df = df[[c for c in df.columns if c in self.keep_features]]
        return df

    def run(self, barframe: Any) -> FeatureFrame:
        if "cross" in self.transformers:
            self.close_panel = _panel_close_wide(barframe)
            self._inner_syms = set(self.close_panel.columns)
        pieces = []
        for sym in barframe.symbols:
            grp = barframe.by_symbol(sym)
            self._sym_label = _norm_sym(sym)  # SHFE.au/au0 -> au
            feats = self._per_symbol(grp)
            feats = feats.set_index(
                pd.MultiIndex.from_product([[sym], feats.index], names=["symbol", "datetime"])
            )
            pieces.append(feats)
        if not pieces:
            df = pd.DataFrame(
                index=pd.MultiIndex.from_arrays([[], []], names=["symbol", "datetime"])
            )
        else:
            df = pd.concat(pieces).sort_index()
        ff = FeatureFrame(df=df, freq=barframe.freq, metadata=getattr(barframe, "metadata", {}))
        ff.validate()
        return ff


def build_features(
    barframe: Any,
    cfg: Any,
    global_close: dict[str, pd.Series] | None = None,
    fundamental_data: dict[str, pd.DataFrame | pd.Series] | None = None,
) -> FeatureFrame:
    """便捷函数：构造并执行默认流水线（可选注入已对齐的外盘收盘序列/基本面数据）。"""
    return FeaturePipeline(
        cfg, global_close=global_close, fundamental_data=fundamental_data
    ).run(barframe)
