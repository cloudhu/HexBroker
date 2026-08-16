"""T04 防泄漏静态检查：环境禁止依赖预测层（ForecastModel/SignalStore 等）。"""

from __future__ import annotations

import inspect

import pytest

from hexbroker.rl.futures_env import (
    FuturesTradingEnv,
    assert_env_has_no_model_dependency,
)


def test_env_passes_static_check():
    # 红线：环境源码与签名不得出现预测层符号
    assert_env_has_no_model_dependency(FuturesTradingEnv)


def test_env_constructor_has_no_model_param():
    sig = inspect.signature(FuturesTradingEnv.__init__)
    for pname in sig.parameters:
        assert "model" not in pname.lower(), f"构造参数含模型字样：{pname}"
    # 构造参数应为 信号帧/价格帧/配置，而非模型
    params = list(sig.parameters)
    assert "signals" in params and "prices" in params and "cfg" in params


def test_env_source_has_no_forbidden_tokens():
    src = inspect.getsource(FuturesTradingEnv)
    for tok in ("ForecastModel", "ForecastTrainer", "KronosPredictor", "from_pretrained"):
        assert tok not in src, f"环境源码出现预测层符号：{tok}"


def test_negative_check_raises_on_bad_class():
    class _BadEnv:
        def __init__(self, signals, model):  # noqa: ANN001
            self.model = model

    with pytest.raises(AssertionError):
        assert_env_has_no_model_dependency(_BadEnv)
