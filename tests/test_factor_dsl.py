"""P1-6 因子 DSL 测试（A6.1~A6.5）。

A6.1：DSL 复刻 ≥10/25 硬编码特征 <1e-9；
A6.2：注册表新增自定义因子；
A6.3：FactorICArchive 复现滚动 RankIC；
A6.4：from_yaml 加载；
A6.5：FeaturePipeline 注入 factor_registry 后并入 f_<name> 列（默认无 registry 行为不变）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from pathlib import Path

from hexbroker.factor import FactorExpr, FactorRegistry, FactorICArchive
from hexbroker.feature.pipeline import FeaturePipeline
from hexbroker.feature.technical import add_technical


def _sample_df(n: int = 120, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.standard_normal(n))
    close = np.abs(close) + 50.0
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "open": close + rng.standard_normal(n) * 0.1,
            "high": close + np.abs(rng.standard_normal(n)) * 0.5,
            "low": close - np.abs(rng.standard_normal(n)) * 0.5,
            "close": close,
            "volume": rng.integers(100, 1000, n).astype(float),
        },
        index=idx,
    )


# DSL 复刻 technical.py 的 11 个特征（名称与 f_<name> 列一致）
_REPLICAS = [
    ("ret_1", "Log(Div(Close, Ref(Close, 1)))"),
    ("ret_acc_5", "Sum(Log(Div(Close, Ref(Close, 1))), 5)"),
    ("ret_acc_20", "Sum(Log(Div(Close, Ref(Close, 1))), 20)"),
    ("vol_5", "Std(Log(Div(Close, Ref(Close, 1))), 5)"),
    ("vol_20", "Std(Log(Div(Close, Ref(Close, 1))), 20)"),
    ("ma_spread", "Div(Sub(MA(Close, 5), MA(Close, 20)), MA(Close, 20))"),
    ("rsi", "RSI(Close, 14)"),
    ("macd", "Sub(EMA(Close, 12), EMA(Close, 26))"),
    ("macd_hist", "Sub(MACD(Close), EMA(MACD(Close), 9))"),
    ("vol_ratio", "Div(Volume, MA(Volume, 20))"),
    ("boll_width", "Div(Mul(Std(Close, 20), 2.0), MA(Close, 20))"),
]


def _registry_with_replicas() -> FactorRegistry:
    reg = FactorRegistry()
    for name, expr in _REPLICAS:
        reg.register(FactorExpr(name=name, expr=expr))
    return reg


# ---------------------------------------------------------------------------
# A6.1 DSL 复刻硬编码特征 <1e-9
# ---------------------------------------------------------------------------
def test_dsl_replicates_technical_features():
    df = _sample_df(120)
    tech = add_technical(df.copy())
    reg = _registry_with_replicas()
    for name, _ in _REPLICAS:
        dsl = reg.compute(df, name).reindex(tech.index)
        diff = (dsl - tech[f"f_{name}"]).abs().max()
        assert diff < 1e-9, f"因子 {name} 与硬编码特征偏差过大：{diff}"


def test_dsl_replicates_across_seeds():
    for seed in range(3):
        df = _sample_df(120, seed=seed)
        tech = add_technical(df.copy())
        reg = _registry_with_replicas()
        for name, _ in _REPLICAS:
            dsl = reg.compute(df, name).reindex(tech.index)
            diff = (dsl - tech[f"f_{name}"]).abs().max()
            assert diff < 1e-9, f"seed={seed} 因子 {name} 偏差 {diff}"


# ---------------------------------------------------------------------------
# A6.2 注册表新增自定义因子 + validate 拒绝未来函数
# ---------------------------------------------------------------------------
def test_register_custom_factor():
    reg = FactorRegistry()
    reg.register_expr("custom_mom_10", "Div(Close, Ref(Close, 10))", category="custom")
    assert "custom_mom_10" in reg
    assert reg.column_for("custom_mom_10") == "f_custom_mom_10"
    df = _sample_df(60)
    s = reg.compute(df, "custom_mom_10")
    assert s.name == "f_custom_mom_10"
    assert len(s) == 60


def test_validate_rejects_future_function():
    bad = FactorExpr(name="leak", expr="Ref(Close, -1)")
    with pytest.raises(ValueError):
        bad.validate()  # 负 shift = 未来函数 → 注册期即拒
    good = FactorExpr(name="ok", expr="Ref(Close, 1)")
    good.validate()  # 正 shift 允许


def test_register_duplicate_raises():
    reg = FactorRegistry()
    reg.register_expr("x", "Close")
    with pytest.raises(ValueError):
        reg.register_expr("x", "Close")


# ---------------------------------------------------------------------------
# A6.3 FactorICArchive 滚动 RankIC
# ---------------------------------------------------------------------------
def test_ic_archive_rolling_ic_and_cache():
    df = _sample_df(200)
    # 构造一个与 close 强相关的"因子"（含噪声）→ RankIC 应显著非零
    rng = np.random.default_rng(7)
    factor = df["close"].pct_change().fillna(0.0) + rng.standard_normal(200) * 0.001
    feat = factor.to_frame("f_test")
    fwd_ret = df["close"].pct_change().shift(-1).fillna(0.0)
    arch = FactorICArchive(cache_dir="artifacts/_test_factor_ic")
    ic = arch.rolling_ic("test", "X", feat, fwd_ret, window=30)
    assert isinstance(ic, pd.Series)
    assert ic.notna().sum() > 0
    # 缓存命中
    cached = arch.cached("test", "X")
    assert cached is not None
    assert np.allclose(cached.dropna().values, ic.dropna().values)


# ---------------------------------------------------------------------------
# A6.4 from_yaml 加载
# ---------------------------------------------------------------------------
def test_from_yaml_loads_factors():
    path = str(Path(__file__).resolve().parents[1] / "configs" / "factors.yaml")
    reg = FactorRegistry.from_yaml(path)
    assert "ret_1" in reg
    assert "custom_mom_10" in reg
    assert "boll_width" in reg
    # 加载后即可求值
    df = _sample_df(120)
    assert reg.compute(df, "custom_mom_10").name == "f_custom_mom_10"


# ---------------------------------------------------------------------------
# A6.5 FeaturePipeline 注入 factor_registry
# ---------------------------------------------------------------------------
class _FakeBarFrame:
    """极简 BarFrame 桩，供 pipeline.run 调用。"""

    def __init__(self, df: pd.DataFrame, symbol: str) -> None:
        self._df = df
        self._symbol = symbol
        self.freq = "1d"
        self.metadata: dict = {}

    @property
    def symbols(self) -> list[str]:
        return [self._symbol]

    def by_symbol(self, sym: str) -> pd.DataFrame:
        return self._df


def _make_barframe() -> _FakeBarFrame:
    df = _sample_df(120)
    df = df.copy()
    df.index = pd.MultiIndex.from_product([["X"], df.index])
    return _FakeBarFrame(df, "X")


def test_pipeline_injects_dsl_factors():
    from omegaconf import OmegaConf

    cfg = OmegaConf.create({})
    reg = FactorRegistry.from_yaml(
        str(Path(__file__).resolve().parents[1] / "configs" / "factors.yaml")
    )
    pipe = FeaturePipeline(cfg, factor_registry=reg)
    ff = pipe.run(_make_barframe())
    cols = set(ff.df.columns)
    assert "f_custom_mom_10" in cols
    assert "f_ret_1" in cols
    # 注入因子列数量 = 注册表因子数
    injected = {c for c in cols if c.startswith("f_") and c in {reg.column_for(n) for n in reg.names()}}
    assert len(injected) == len(reg)


def test_pipeline_without_registry_unchanged():
    """默认（factor_registry=None）pipeline 行为与现状一致：不注入 DSL 列。"""
    from omegaconf import OmegaConf

    cfg = OmegaConf.create({})
    pipe = FeaturePipeline(cfg)
    ff = pipe.run(_make_barframe())
    # 不应出现 factors.yaml 里的自定义因子列
    assert "f_custom_mom_10" not in ff.df.columns
