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
