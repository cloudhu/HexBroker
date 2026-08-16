"""T01 配置测试：加载 / 冻结 / 指纹 / Kronos 配对校验。"""

import pytest

from hexbroker.config import (
    HexConfig,
    config_fingerprint,
    load_config,
    validate_kronos_pairing,
)
from hexbroker import HexConfigError


def test_default_config_loads():
    cfg = load_config()
    assert isinstance(cfg, HexConfig)
    assert cfg.seed == 42
    assert cfg.data.symbols  # 非空


def test_config_fingerprint_deterministic():
    a = load_config()
    b = load_config()
    assert config_fingerprint(a) == config_fingerprint(b)


def test_config_fingerprint_changes_with_seed():
    a = load_config(seed=1)
    b = load_config(seed=2)
    assert config_fingerprint(a) != config_fingerprint(b)


def test_experiment_yaml_override(tmp_path):
    yaml_text = """
experiment: e01_cu_daily
data:
  symbols: ["SHFE.rb"]
  freq: "1d"
"""
    p = tmp_path / "exp.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.experiment == "e01_cu_daily"
    assert cfg.data.symbols == ["SHFE.rb"]


def test_kronos_pairing_ok():
    # mini 配 Tokenizer-2k
    validate_kronos_pairing("NeoQuasar/Kronos-mini", "NeoQuasar/Kronos-Tokenizer-2k")
    # small 配 Tokenizer-base
    validate_kronos_pairing("NeoQuasar/Kronos-small", "NeoQuasar/Kronos-Tokenizer-base")


def test_kronos_pairing_mismatch_raises():
    with pytest.raises(HexConfigError):
        validate_kronos_pairing("NeoQuasar/Kronos-small", "NeoQuasar/Kronos-Tokenizer-2k")
