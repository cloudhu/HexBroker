"""免费数据源供应链配置（configs/data/free.yaml）合法性校验。

独立验证：yaml.safe_load 后断言关键供应链字段符合设计契约。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# hexbroker/data/sources/test_free_config.py
#   parents[0]=sources, [1]=data, [2]=hexbroker, [3]=project root
CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "configs" / "data" / "free.yaml"
)


@pytest.fixture(scope="module")
def cfg():
    assert CONFIG_PATH.exists(), f"未找到配置文件: {CONFIG_PATH}"
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_source_priority(cfg):
    assert cfg["source_priority"] == ["pytdx", "sina", "akshare_fundamentals"]


def test_drift_close_pct_error(cfg):
    assert cfg["verification"]["tdx_mcp"]["drift"]["close_pct_error"] == 2.0


def test_drift_thresholds_present(cfg):
    drift = cfg["verification"]["tdx_mcp"]["drift"]
    assert "close_pct_warn" in drift
    assert "missing_bar_warn" in drift


def test_pytdx_servers_configured(cfg):
    servers = cfg["sources"]["pytdx"]["servers"]
    assert isinstance(servers, list) and len(servers) >= 3
    for s in servers:
        assert isinstance(s[0], str) and isinstance(s[1], int)


def test_verification_role_in_loop(cfg):
    assert cfg["verification"]["tdx_mcp"]["role"] == "in_loop_verifier"
