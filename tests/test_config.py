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


# ---------------------------------------------------------------------------
# P10-1：EngineAConfig.group_map / group_cap 配置字段
# ---------------------------------------------------------------------------
def test_engine_a_group_map_default_none():
    """代码默认 group_map=None（GROUPS_V2 兜底，向后兼容）；group_cap=None。"""
    cfg = load_config()
    assert cfg.backtest.engine_a.group_map is None
    assert cfg.backtest.engine_a.group_cap is None


def test_engine_a_group_map_yaml_override(tmp_path):
    """yaml 可覆盖 group_map / group_cap（部署 yaml 显式启用）。"""
    yaml_text = """
backtest:
  engine_a:
    group_cap: 0.5
    group_map:
      i0: ferrous_all
      j0: ferrous_all
      jm0: ferrous_all
      rb0: ferrous_all
      hc0: ferrous_all
      cu0: industrial
"""
    p = tmp_path / "exp.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    cfg = load_config(str(p))
    ea = cfg.backtest.engine_a
    assert ea.group_cap == 0.5
    assert ea.group_map == {
        "i0": "ferrous_all",
        "j0": "ferrous_all",
        "jm0": "ferrous_all",
        "rb0": "ferrous_all",
        "hc0": "ferrous_all",
        "cu0": "industrial",
    }


def test_engine_a_group_map_base_yaml_deployment():
    """部署 yaml configs/base.yaml 显式启用 group_cap=0.5 + ferrous_all 合并映射。"""
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "configs" / "base.yaml"
    cfg = load_config(str(base))
    ea = cfg.backtest.engine_a
    assert ea.group_cap == 0.5
    gm = ea.group_map or {}
    assert {gm.get(s) for s in ("i0", "j0", "jm0", "rb0", "hc0")} == {"ferrous_all"}
    assert gm.get("cu0") == "industrial"
    assert gm.get("au0") == "precious"
