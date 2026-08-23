"""配置层 Kronos 配对校验（§3.2）：模型与分词器错配必须抛 HexConfigError。"""

from __future__ import annotations

import pytest

from hexbroker import HexConfigError
from hexbroker.config import validate_kronos_pairing


def test_valid_pair_passes():
    validate_kronos_pairing("NeoQuasar/Kronos-small", "NeoQuasar/Kronos-Tokenizer-base")
    validate_kronos_pairing("NeoQuasar/Kronos-mini", "NeoQuasar/Kronos-Tokenizer-2k")


def test_mismatched_pair_raises():
    with pytest.raises(HexConfigError):
        validate_kronos_pairing("NeoQuasar/Kronos-small", "NeoQuasar/Kronos-Tokenizer-2k")


def test_unknown_model_raises():
    with pytest.raises(HexConfigError):
        validate_kronos_pairing("NeoQuasar/Unknown", "NeoQuasar/Kronos-Tokenizer-base")


def test_max_context_table():
    from hexbroker.config import KRONOS_MAX_CONTEXT

    assert KRONOS_MAX_CONTEXT["NeoQuasar/Kronos-small"] == 512


def test_kronos_adapter_construct_enforces_pairing():
    """L8 修复：直接构造 KronosAdapter 也必须校验模型↔分词器配对。"""
    from hexbroker.forecast.kronos_adapter import KronosAdapter

    class _Cfg:
        class _F:
            model_name = "NeoQuasar/Kronos-small"
            tokenizer_name = "NeoQuasar/Kronos-Tokenizer-2k"  # 错配

        forecast = _F()

    with pytest.raises(HexConfigError):
        KronosAdapter(_Cfg())


def test_kronos_predictor_construct_enforces_pairing():
    """L8 修复：直接构造 Kronos OOS 预测器也必须校验模型↔分词器配对。"""
    from hexbroker.forecast.kronos_predictor import KlineKronosOOS

    class _Cfg:
        class _F:
            horizon = 5
            effective_threshold = 0.05

        forecast = _F()

    with pytest.raises(HexConfigError):
        KlineKronosOOS(
            _Cfg(),
            store=None,
            model_name="NeoQuasar/Kronos-small",
            tokenizer_name="NeoQuasar/Kronos-Tokenizer-2k",  # 错配
        )


def test_kronos_construct_valid_pair_ok():
    """合法配对直接构造不抛（默认路径可用）。"""
    from hexbroker.forecast.kronos_adapter import KronosAdapter

    class _Cfg:
        class _F:
            model_name = "NeoQuasar/Kronos-small"
            tokenizer_name = "NeoQuasar/Kronos-Tokenizer-base"

        forecast = _F()

    # 不抛即为通过（构造不触发真实权重加载）
    KronosAdapter(_Cfg())

