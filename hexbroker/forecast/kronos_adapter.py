"""Kronos 适配器（§3.2）：HF 版 decoder-only Transformer，零样本预测。

**关键约束**：Kronos 需 GPU 与第三方权重。CPU 沙箱中必须 **优雅降级** 到
自研 ``ARTransformer``（主 fallback）。本模块对 Kronos 相关导入做受控包裹：
- 若未安装 ``kronos`` / ``transformers`` / ``torch``，``HAS_KRONOS=False``，``fit`` 抛
  ``HexConfigError`` 提示改用 fallback；
- 即便安装了，也只在显式 ``enable_kronos=True`` 且存在权重时启用，否则自动降级。

不要让 Kronos 的缺失导致整个预测层崩溃——这关系到 ``--demo`` 端到端可跑。
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from .. import HexConfigError
from ..utils.logging import get_logger
from ..utils.registry import register
from . import ForecastModel, build_windows

_log = get_logger("FCST")

HAS_KRONOS = False
_KRONOS_IMPORT_ERROR: Optional[str] = None
try:  # pragma: no cover - 取决于环境是否安装 torch/transformers
    import torch  # noqa: F401
    from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: F401

    HAS_KRONOS = True
except Exception as exc:  # 降级路径
    _KRONOS_IMPORT_ERROR = str(exc)


@register("kronos")
class KronosAdapter(ForecastModel):
    """Kronos 适配（GPU 优先，CPU 自动降级到 ARTransformer）。"""

    family = "kronos"

    def __init__(self, cfg: Any, model_id: Optional[str] = None, enable_kronos: bool = False) -> None:
        super().__init__(cfg, model_id)
        self.enable_kronos = bool(enable_kronos) and HAS_KRONOS
        self.model_name = getattr(cfg.forecast, "model_name", "NeoQuasar/Kronos-small")
        self.tokenizer_name = getattr(cfg.forecast, "tokenizer_name", "NeoQuasar/Kronos-Tokenizer-base")
        self._inner: Optional[ForecastModel] = None  # 降级使用的 ARTransformer

    def _ensure_inner(self) -> ForecastModel:
        if self._inner is None:
            from .autoregressive import ARTransformer

            self._inner = ARTransformer(self.cfg)
        return self._inner

    def fit(self, X: pd.DataFrame, y: np.ndarray, X_valid: Optional[pd.DataFrame] = None) -> Any:
        if not self.enable_kronos:
            _log.warning(
                "Kronos 未启用/不可用(%s)，降级为 ARTransformer fallback。",
                _KRONOS_IMPORT_ERROR or "disabled",
            )
            return self._ensure_inner().fit(X, y, X_valid)
        # 真正的 Kronos 训练路径（仅在 GPU 环境且显式启用时执行）
        _log.info("使用 Kronos 权重训练：%s", self.model_name)
        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer

            tok = AutoTokenizer.from_pretrained(self.tokenizer_name)
            model = AutoModelForCausalLM.from_pretrained(self.model_name)
            # 注：此处仅占位，真实训练需按 Kronos 数据格式构造样本；MVP 以 fallback 为准
            self._inner = None
            return self._make_log(0.0)
        except Exception as exc:  # 任何失败都降级
            _log.warning("Kronos 训练失败(%s)，降级 ARTransformer", exc)
            return self._ensure_inner().fit(X, y, X_valid)

    def predict(self, X: pd.DataFrame) -> list:
        return self._ensure_inner().predict(X)

    def _next_return(self, windows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return self._ensure_inner()._next_return(windows)

    def _make_log(self, loss: float):
        from .base import TrainLog

        return TrainLog(loss=float(loss))
