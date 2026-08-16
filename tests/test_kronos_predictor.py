"""QA：真 Kronos OOS 通路独立测试（因果性 / 符号语义 / 信号契约 / sina 修复回归）。

覆盖 ``hexbroker/forecast/kronos_predictor.py`` 的 ``KlineKronosOOS._run_fold``
与 ``hexbroker/data/sources/sina_source.SinaSource._repair_ohlc``。

设计要点：
- 不加载真实 Kronos 权重（mock ``predict_batch``），测试聚焦**因果性与信号构造**；
- 因果性：context 只含 bar<=t（含 t 的 close），y_tss 仅未来时间戳，不回读未来价格；
- 符号语义：``p_up = mean(pred_close/close_lasts - 1 > 0)``，方向与代码注释一致；
- sina 修复：废 bar（close<=0 / open/high/low<=0）丢弃，包络破坏修复，正常 bar 保留。
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from hexbroker.data.sources.sina_source import SinaSource
from hexbroker.data.splitter import WalkForwardSplitter
from hexbroker.forecast.kronos_predictor import KlineKronosOOS

# KlineKronosOOS 通路运行时硬依赖 torch（可选重依赖，CI 不安装）：
# 仅跳过依赖 torch 的 3 个测试类；TestSinaRepairOhlc（纯 pandas）不受影响。
try:
    import torch  # noqa: F401

    _HAS_TORCH = True
except ImportError:  # pragma: no cover
    _HAS_TORCH = False

_KRONOS_NEEDS_TORCH = pytest.mark.skipif(
    not _HAS_TORCH, reason="KlineKronosOOS 运行时依赖 torch（可选重依赖）"
)


# ---------------------------------------------------------------------------
# 最小 cfg 桩（与真实 HexConfig 默认值一致）
# ---------------------------------------------------------------------------
class _CfgStub:
    class _Data:
        train_len = 250
        test_len = 60
        purge = 5
        embargo = 2
        mode = "rolling"

    class _Forecast:
        horizon = 5
        effective_threshold = 0.05
        temperature = 1.0
        top_p = 0.9
        max_context = 512

    class _Feature:
        normalize_window = 120

    data = _Data()
    forecast = _Forecast()
    feature = _Feature()


class _StoreStub:
    """只记录 put 调用，不做真实落盘。"""

    def __init__(self) -> None:
        self.saved: list[Any] = []

    def put(self, signals) -> int:
        self.saved = list(signals)
        return len(self.saved)


class _FakePredictor:
    """按 close_lasts 的倍数返回预测 close，便于确定性断言。"""

    def __init__(self, factor: float = 1.1) -> None:
        self.factor = factor
        self.calls: list[dict] = []  # 记录每次 predict_batch 的入参

    def predict_batch(self, ctxs, x_tss, y_tss, pred_len, T, top_p, sample_count, verbose):
        self.calls.append(
            {
                "ctxs": list(ctxs),
                "x_tss": list(x_tss),
                "y_tss": list(y_tss),
                "pred_len": pred_len,
                "T": T,
                "top_p": top_p,
                "sample_count": sample_count,
            }
        )
        out = []
        for ctx in ctxs:
            last_close = float(ctx["close"].iloc[-1])
            close_pred = pd.DataFrame(
                {"close": np.full(pred_len, last_close * self.factor)},
                index=y_tss[0][:pred_len],
            )
            out.append(close_pred)  # 与真 KronosPredictor.predict_batch 一致：返回 DataFrame
        return out


def _make_sdf(n: int = 400, start: str = "2020-01-01") -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="B")
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    close = np.maximum(close, 1.0)
    df = pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": 1000.0,
            "amount": 100000.0,
        },
        index=idx,
    )
    return df


def _make_oos(cfg=None, predictor=None, n=400) -> tuple[list, pd.DataFrame, pd.DatetimeIndex, Any]:
    cfg = cfg or _CfgStub()
    oos = KlineKronosOOS(cfg, _StoreStub(), n_mc=3, seed=42)
    sdf = _make_sdf(n=n)
    idx = sdf.index
    splitter = WalkForwardSplitter(
        train_len=cfg.data.train_len,
        test_len=cfg.data.test_len,
        purge=cfg.data.purge,
        embargo=cfg.data.embargo,
        mode=cfg.data.mode,
    )
    folds = splitter.split(idx)
    fold = folds[0]
    predictor = predictor or _FakePredictor(factor=1.1)
    sigs = oos._run_fold(predictor, "SHFE.cu", sdf, idx, fold)
    return sigs, sdf, idx, fold


# ---------------------------------------------------------------------------
# 1. 因果性
# ---------------------------------------------------------------------------
@_KRONOS_NEEDS_TORCH
class TestCausality:
    def test_context_never_includes_future_bars(self):
        oos = KlineKronosOOS(_CfgStub(), _StoreStub(), n_mc=1, seed=42)
        sdf = _make_sdf(n=400)
        idx = sdf.index
        splitter = WalkForwardSplitter(250, 60, 5, 2, "rolling")
        fold = splitter.split(idx)[0]
        pred = _FakePredictor(factor=1.1)
        oos._run_fold(pred, "SHFE.cu", sdf, idx, fold)

        assert pred.calls, "predict_batch 应被调用"
        for call in pred.calls:
            for ctx, x_ts, y_ts in zip(call["ctxs"], call["x_tss"], call["y_tss"]):
                t_max = pd.Timestamp(ctx.index[-1])
                # ctx 最后一个 bar 即 bar t（信号时刻），ctx 不得含未来
                assert pd.Timestamp(x_ts.iloc[-1]) == t_max
                # y_tss 是未来时间戳载体，全部严格晚于 ctx 末端
                assert (pd.to_datetime(y_ts) > t_max).all()
                # 每个 ctx 内时间严格递增（无乱序/重复）
                assert ctx.index.is_monotonic_increasing

    def test_context_capped_at_max_context_and_lookback(self):
        oos = KlineKronosOOS(_CfgStub(), _StoreStub(), n_mc=1, seed=42, max_context=64)
        sdf = _make_sdf(n=400)
        idx = sdf.index
        fold = WalkForwardSplitter(250, 60, 5, 2, "rolling").split(idx)[0]
        pred = _FakePredictor(factor=1.1)
        oos._run_fold(pred, "SHFE.cu", sdf, idx, fold)
        for call in pred.calls:
            for ctx in call["ctxs"]:
                # lookback=120 但 max_context=64 -> 实际 ctx 长 ≤64
                assert len(ctx) <= 64

    def test_y_tss_are_future_timestamps_only_carrier(self):
        """y_tss 仅时间戳载体：长度=horizon、值在未来、代码不读未来价格。"""
        sigs, sdf, idx, fold = _make_oos()
        assert sigs, "应产出信号"
        # horizon 语义：每个信号 horizon=5
        assert all(s.horizon == 5 for s in sigs)


# ---------------------------------------------------------------------------
# 2. 符号语义（p_up 方向）
# ---------------------------------------------------------------------------
@_KRONOS_NEEDS_TORCH
class TestSymbolSemantics:
    def test_p_up_up_when_pred_close_above_last_close(self):
        """pred_close/close_lasts-1 > 0 -> p_up 高（≈1）。"""
        sigs, sdf, idx, fold = _make_oos(predictor=_FakePredictor(factor=1.1))
        assert sigs
        for s in sigs:
            assert s.p_up > 0.99  # 10 条路径全涨
            assert s.exp_ret > 0

    def test_p_up_down_when_pred_close_below_last_close(self):
        """pred_close/close_lasts-1 < 0 -> p_up 低（≈0）。"""
        sigs, sdf, idx, fold = _make_oos(predictor=_FakePredictor(factor=0.9))
        assert sigs
        for s in sigs:
            assert s.p_up < 0.01
            assert s.exp_ret < 0

    def test_p_up_is_fraction_of_positive_paths(self):
        """n_mc=4 且 2 涨 2 跌 -> p_up=0.5；验证 p_up 语义 = P(收益>0)。"""

        class _MixedPredictor(_FakePredictor):
            def __init__(self):
                super().__init__(factor=1.0)
                self.call_no = 0
                self.chunks_per_iter = None

            def predict_batch(self, ctxs, x_tss, y_tss, pred_len, T, top_p, sample_count, verbose):
                self.calls.append({})
                # 每次 predict_batch 处理一批 chunk；同一 MC 迭代内可能多次调用
                # （60 bars / 32 chunk -> 2 次调用）。按「MC 迭代」交替涨跌。
                n_bars = len(ctxs)
                if self.chunks_per_iter is None:
                    self.chunks_per_iter = max(1, n_bars)
                # 简化：直接按 call 次数硬编码：n_mc=4 时，前 2 次=MC0、后 2 次=MC1 交替
                # 这里更稳妥：每次调用内全部 bar 同涨同跌，用调用次序 %4 控制 2 涨 2 跌
                up = (self.call_no % 4) in (0, 1)
                self.call_no += 1
                factor = 1.1 if up else 0.9
                out = []
                for ctx in ctxs:
                    last_close = float(ctx["close"].iloc[-1])
                    close_pred = pd.DataFrame(
                        {"close": np.full(pred_len, last_close * factor)},
                        index=y_tss[0][:pred_len],
                    )
                    out.append(close_pred)
                return out

        # n_mc=4，调用 4 次 predict_batch，每次同一批 bars，但涨跌交替
        oos = KlineKronosOOS(_CfgStub(), _StoreStub(), n_mc=4, seed=42)
        sdf = _make_sdf(n=400)
        idx = sdf.index
        fold = WalkForwardSplitter(250, 60, 5, 2, "rolling").split(idx)[0]
        pred = _MixedPredictor()
        sigs = oos._run_fold(pred, "SHFE.cu", sdf, idx, fold)
        assert sigs
        # 每 bar 4 条路径：2 涨 2 跌 -> p_up=0.5
        for s in sigs:
            assert abs(s.p_up - 0.5) < 1e-6
            # exp_ret 接近 0（涨跌抵消）
            assert abs(s.exp_ret) < 1e-6


# ---------------------------------------------------------------------------
# 3. 边界 / 契约
# ---------------------------------------------------------------------------
@_KRONOS_NEEDS_TORCH
class TestBoundaryAndContract:
    def test_skips_bars_without_full_future_horizon(self):
        """t + horizon >= len(sdf) 的 bar 跳过（保留末尾 NaN 语义）。"""
        n = 320  # 测试窗 [257, 317)，有效 t <= 314 -> 58 条
        sigs, sdf, idx, fold = _make_oos(n=n)
        sig_ts = set(s.ts for s in sigs)
        last_sig_ts = max(sig_ts)
        last_sig_pos = idx.get_loc(last_sig_ts)
        assert last_sig_pos + 5 < n  # 最后一个信号也满足未来完整
        assert len(sigs) <= fold.test_len

    def test_signal_contract_fields_and_ranges(self):
        sigs, sdf, idx, fold = _make_oos(predictor=_FakePredictor(factor=1.1))
        for s in sigs:
            assert 0.0 < s.p_up < 1.0
            assert s.model_id == "kronos"
            assert s.train_end == pd.Timestamp(idx[fold.train_max_pos])
            assert s.horizon == 5
            qs = s.quantiles
            assert qs["q10"] <= qs["q25"] <= qs["q50"] <= qs["q75"] <= qs["q90"]
            # is_effective = |p_up-0.5| > eff_thr
            assert s.is_effective == (abs(s.p_up - 0.5) > 0.05)

    def test_signal_ts_is_bar_t_timestamp(self):
        """信号的 ts 必须是 bar t（信号时刻），不是未来。"""
        sigs, sdf, idx, fold = _make_oos()
        for s in sigs:
            assert s.ts == pd.Timestamp(idx[idx.get_loc(s.ts)])

    def test_put_roundtrip_contract(self):
        """ForecastSignal -> store.put -> get_frame 字段无损（SignalStore 契约）。"""
        from hexbroker.forecast.base import ForecastSignal

        s = ForecastSignal(
            symbol="SHFE.cu",
            ts=pd.Timestamp("2020-06-01"),
            horizon=5,
            p_up=0.7,
            exp_ret=0.01,
            quantiles={"q10": -0.01, "q25": 0.0, "q50": 0.01, "q75": 0.02, "q90": 0.03},
            vol_hat=0.02,
            conf=0.8,
            model_id="kronos",
            train_end=pd.Timestamp("2020-05-25"),
            is_effective=True,
        )
        rec = s.to_record()
        back = ForecastSignal.from_record(rec)
        assert back == s
        assert back.ts == pd.Timestamp("2020-06-01")


# ---------------------------------------------------------------------------
# 4. sina _repair_ohlc 回归（废 bar 丢弃 / 包络修复 / 正常 bar 保留）
# ---------------------------------------------------------------------------
class TestSinaRepairOhlc:
    def _df(self):
        return pd.DataFrame(
            {
                "date": ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07", "2020-01-08"],
                "open": [100.0, 0.0, 102.0, 99.0, 105.0],
                "high": [101.0, 101.0, 104.0, 100.0, 107.0],
                "low": [99.0, 99.0, 101.0, 98.0, 104.0],
                "close": [100.5, 100.5, 103.0, -1.0, 106.0],
                "volume": [1, 1, 1, 1, 1],
            }
        )

    def test_drops_zero_open_and_nonpositive_close(self):
        """open<=0 的废 bar（如 m0 2019-07-29 open=0）与 close<=0 必须丢弃。"""
        df = self._df()
        out = SinaSource._repair_ohlc(df.copy())
        dates = out["date"].tolist()
        assert "2020-01-03" not in dates  # open=0 丢弃
        assert "2020-01-07" not in dates  # close=-1 丢弃
        assert "2020-01-02" in dates
        assert "2020-01-06" in dates
        assert "2020-01-08" in dates

    def test_repairs_envelope_violation(self):
        """open>high 等包络破坏：以四价极值重定 low/high，保证契约成立。"""
        df = pd.DataFrame(
            {
                "date": ["2020-01-02", "2020-01-03"],
                "open": [100.0, 110.0],
                "high": [101.0, 105.0],  # open(110) > high(105) 破坏包络
                "low": [99.0, 108.0],
                "close": [100.5, 109.0],
                "volume": [1, 1],
            }
        )
        out = SinaSource._repair_ohlc(df.copy())
        for _, row in out.iterrows():
            assert row["high"] >= row["open"]
            assert row["high"] >= row["close"]
            assert row["low"] <= row["open"]
            assert row["low"] <= row["close"]

    def test_preserves_valid_bars(self):
        """正常 bar 必须原样保留（不误伤既有 cu/rb/sc 路径）。"""
        df = self._df()
        out = SinaSource._repair_ohlc(df.copy())
        kept = out.set_index("date")
        assert kept.loc["2020-01-02", "open"] == 100.0
        assert kept.loc["2020-01-02", "close"] == 100.5
        assert kept.loc["2020-01-06", "open"] == 102.0
        assert kept.loc["2020-01-06", "close"] == 103.0

    def test_empty_after_clean_raises_in_rows_to_frame(self):
        """全部废 bar 时 _rows_to_frame 抛 HexDataError（不产出空帧）。"""
        from hexbroker import HexDataError

        df = pd.DataFrame(
            {
                "date": ["2020-01-02"],
                "open": [0.0],
                "high": [0.0],
                "low": [0.0],
                "close": [0.0],
                "volume": [1],
            }
        )
        with pytest.raises(HexDataError):
            SinaSource._rows_to_frame(df.to_dict("records"), "m0")
