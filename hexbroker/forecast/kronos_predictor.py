"""真 Kronos 独立 OOS 通路（§3.2 / §8.4 扩展）。

``ForecastModel.predict(X)`` 收特征矩阵；而 ``KronosPredictor.predict(df)``
收**原始 OHLCV + 时间戳**（内部自行 z-score 归一化），因此不能走
``ForecastTrainer``。本模块提供 :class:`KlineKronosOOS`：直接消费
``BarFrame`` 的原始 K 线，按与 ``ForecastTrainer`` 相同的
``WalkForwardSplitter`` 折对齐，在测试窗逐 bar 用真实 Kronos
（NeoQuasar/Kronos-small + Kronos-Tokenizer-base）自回归采样，
由路径分布构造 :class:`ForecastSignal` 写入 ``SignalStore``。

设计要点（严格契约）：
- **严格因果**：context 只含 bar<=t 的历史（含 t 的 close），y_timestamp
  仅作未来时间戳载体（minute/hour/weekday/day/month），不读取未来价格。
- **路径分布**：``KronosPredictor.predict`` 的 ``sample_count>1`` 是内部平均，
  故以 ``sample_count=1`` 循环 ``n_mc`` 次收集独立路径（与
  ``ForecastModel.sample_paths`` 同思路）。
- **性能**：同一折内所有有效 bar 的 context 等长（max_ctx），用
  ``predict_batch`` 按 32 条一批并行（GPU 上 60x 加速），功能等价于逐 bar
  循环 ``predict``。
- **不复权**：sina 已是主力连续，直接使用 raw close。
- model_id 统一为 ``"kronos"``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

# ---- 受控导入路径：third_party/Kronos（model 包）与 third_party/pylibs ----
_THIRD = Path(__file__).resolve().parent.parent.parent / "third_party"
for _p in [str(_THIRD / "Kronos"), str(_THIRD / "pylibs")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ..data.splitter import WalkForwardSplitter
from ..forecast.trainer import TrainResult
from .base import ForecastSignal

# 批大小（predict_batch 单次并行序列数；兼顾 GPU 内存与吞吐）
_BATCH_CHUNK = 32


class KlineKronosOOS:
    """真 Kronos 的独立 walk-forward OOS 预测器（不依赖特征矩阵）。

    参数
    ----
    cfg : HexConfig
    store : SignalStore
    model_name : Kronos 主模型名，默认 "NeoQuasar/Kronos-small"。
    tokenizer_name : Kronos 分词器名，默认 "NeoQuasar/Kronos-Tokenizer-base"。
    device : 推理设备，默认 None -> cuda:0 优先、cpu 兜底。
    n_mc : 每条信号独立采样路径数，默认 10。
    max_context : Kronos 最大上下文，默认 512（Kronos-small 上限）。
    seed : 随机种子（torch.manual_seed 与 numpy 均对齐）。
    """

    def __init__(
        self,
        cfg: Any,
        store: Any,
        model_name: str = "NeoQuasar/Kronos-small",
        tokenizer_name: str = "NeoQuasar/Kronos-Tokenizer-base",
        device: Optional[str] = None,
        n_mc: int = 10,
        max_context: int = 512,
        seed: int = 42,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.model_name = model_name
        self.tokenizer_name = tokenizer_name
        # L8 修复：直接构造也必须校验模型↔分词器配对（绕过 load_config 时红线不失效）
        from ..config import validate_kronos_pairing

        validate_kronos_pairing(self.model_name, self.tokenizer_name)
        self.device = device
        self.n_mc = int(n_mc)
        self.horizon = int(getattr(cfg.forecast, "horizon", 5))
        self.lookback = int(
            getattr(cfg.feature, "normalize_window", 120) if hasattr(cfg, "feature") else 120
        )
        self.max_context = int(max_context)
        self.seed = int(seed)
        self.eff_thr = float(getattr(cfg.forecast, "effective_threshold", 0.05))
        self.temperature = float(getattr(cfg.forecast, "temperature", 1.0))
        self.top_p = float(getattr(cfg.forecast, "top_p", 0.9))
        # 统一 model_id（与 LightGBM/AR/TCN/GRU 的指纹 model_id 区分开）
        self.model_id = "kronos"
        self._predictor: Any = None

    # ------------------------------------------------------------------
    # 懒加载真 Kronos（只在首次 run 时加载，避免导入即依赖权重）
    # ------------------------------------------------------------------
    def _load_predictor(self) -> Any:
        if self._predictor is not None:
            return self._predictor
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        try:
            import torch
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Kronos OOS 通路需要 torch（可选重依赖）。请先安装：pip install torch 后重试。"
            ) from exc

        from model import Kronos, KronosPredictor, KronosTokenizer

        tokenizer = KronosTokenizer.from_pretrained(self.tokenizer_name)
        model = Kronos.from_pretrained(self.model_name)
        if self.device is None:
            self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        self._predictor = KronosPredictor(
            model, tokenizer, device=self.device, max_context=self.max_context
        )
        return self._predictor

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def run(self, barframe: Any) -> TrainResult:
        """对 barframe 每只标的跑 walk-forward OOS，产出 kronos 信号并落盘。

        返回 ``TrainResult(model_id="kronos", n_oos_signals=..., n_folds=...)``。
        """
        dc = self.cfg.data
        splitter = WalkForwardSplitter(
            train_len=int(dc.train_len),
            test_len=int(dc.test_len),
            purge=int(dc.purge),
            embargo=int(dc.embargo),
            mode=str(dc.mode),
        )
        predictor = self._load_predictor()

        all_signals: list[ForecastSignal] = []
        n_folds = 0
        for sym in barframe.symbols:
            sym_df = barframe.by_symbol(sym).sort_index()
            # 去掉 symbol 层级 -> 单 datetime 索引，便于位置切片
            sdf = sym_df.droplevel(0)
            idx = sdf.index  # DatetimeIndex
            n = len(sdf)
            if n < dc.train_len + dc.purge + dc.embargo + dc.test_len:
                continue
            folds = splitter.split(idx)
            if not folds:
                continue
            splitter.assert_no_leakage(folds)

            for fold in folds:
                fold_signals = self._run_fold(predictor, sym, sdf, idx, fold)
                all_signals.extend(fold_signals)
                n_folds += 1

        n_oos = self.store.put(all_signals) if all_signals else 0
        return TrainResult(
            model_id=self.model_id,
            n_oos_signals=n_oos,
            n_folds=n_folds,
        )

    # ------------------------------------------------------------------
    # 单折
    # ------------------------------------------------------------------
    def _run_fold(
        self, predictor: Any, sym: str, sdf: pd.DataFrame, idx: pd.DatetimeIndex, fold: Any
    ) -> list[ForecastSignal]:
        """对单个折的测试窗逐 bar 用真 Kronos 采样，构造信号列表。"""
        max_ctx = min(self.lookback, self.max_context, int(fold.train_max_pos) + 1)
        max_ctx = max(max_ctx, 1)
        train_end_ts = pd.Timestamp(idx[fold.train_max_pos])

        # 收集本折内所有有效 bar 的 context / 时间戳
        bars: list[int] = []
        ctxs: list[pd.DataFrame] = []
        x_tss: list[pd.Series] = []
        y_tss: list[pd.Series] = []
        close_lasts: list[float] = []
        for t in range(fold.test_start, fold.test_end):
            if t + self.horizon >= len(sdf):
                break  # 未来不足 horizon 根，跳过（保留末尾 NaN 语义）
            start = max(0, t + 1 - max_ctx)
            ctx = sdf.iloc[start : t + 1][["open", "high", "low", "close", "volume", "amount"]]
            x_ts = pd.Series(pd.DatetimeIndex(ctx.index), name="datetime")
            y_dts = idx[t + 1 : t + 1 + self.horizon]
            y_ts = pd.Series(pd.DatetimeIndex(y_dts), name="datetime")
            bars.append(t)
            ctxs.append(ctx)
            x_tss.append(x_ts)
            y_tss.append(y_ts)
            close_lasts.append(float(ctx["close"].iloc[-1]))
        if not bars:
            return []

        # 路径分布：循环 n_mc 次，每次 sample_count=1（内部不做平均）
        rets = np.zeros((self.n_mc, len(bars)), dtype=float)
        import torch

        for s in range(self.n_mc):
            torch.manual_seed(self.seed * 1000 + s)
            np.random.seed(self.seed * 1000 + s)
            for c0 in range(0, len(bars), _BATCH_CHUNK):
                c1 = min(c0 + _BATCH_CHUNK, len(bars))
                preds = predictor.predict_batch(
                    ctxs[c0:c1],
                    x_tss[c0:c1],
                    y_tss[c0:c1],
                    pred_len=self.horizon,
                    T=self.temperature,
                    top_p=self.top_p,
                    sample_count=1,
                    verbose=False,
                )
                for j, pred in enumerate(preds):
                    rets[s, c0 + j] = float(pred["close"].iloc[-1]) / close_lasts[c0 + j] - 1.0

        # 由路径分布构造信号
        out: list[ForecastSignal] = []
        for i, t in enumerate(bars):
            cum = rets[:, i]
            p_up = float(np.mean(cum > 0))
            exp_ret = float(np.mean(cum))
            qs = {f"q{q}": float(np.quantile(cum, q / 100)) for q in (10, 25, 50, 75, 90)}
            vol_hat = float(np.std(cum, ddof=1)) if len(cum) > 1 else 0.0
            iqr = qs["q90"] - qs["q10"]
            conf = float(np.clip(1 - iqr / (6 * (vol_hat + 1e-9)), 0.0, 1.0))
            out.append(
                ForecastSignal(
                    symbol=sym,
                    ts=pd.Timestamp(idx[t]),
                    horizon=self.horizon,
                    p_up=float(np.clip(p_up, 1e-6, 1 - 1e-6)),
                    exp_ret=exp_ret,
                    quantiles=qs,
                    vol_hat=vol_hat,
                    conf=conf,
                    model_id=self.model_id,
                    train_end=train_end_ts,
                    is_effective=abs(p_up - 0.5) > self.eff_thr,
                )
            )
        return out
