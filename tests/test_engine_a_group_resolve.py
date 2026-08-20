"""P10-1：engine_a 的 group_cap / group_map 配置读取逻辑测试。

验证 ``scripts.p5_engineA_cross_section`` 的解析优先级：
  1. 显式传入优先；
  2. 未传（None）时读 ``cfg.backtest.engine_a.group_map/group_cap``；
  3. 配置也是 None → 回退 GROUPS_V2 / 不启用（向后兼容）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.p5_engineA_cross_section import (  # noqa: E402
    _default_group_map,
    _resolve_group_cap,
    _resolve_group_map,
)


def test_group_map_explicit_wins():
    explicit = {"i0": "ferrous_all", "rb0": "ferrous_all"}
    assert _resolve_group_map(explicit) == explicit


def test_group_map_config_enabled_reads_config():
    gm = {"i0": "ferrous_all", "rb0": "ferrous_all"}
    with mock.patch("scripts.p5_engineA_cross_section.load_config") as m_load:
        m_load.return_value.backtest.engine_a.group_map = gm
        assert _resolve_group_map(None) == gm


def test_group_map_config_none_falls_back_groups_v2():
    with mock.patch("scripts.p5_engineA_cross_section.load_config") as m_load:
        m_load.return_value.backtest.engine_a.group_map = None
        out = _resolve_group_map(None)
    assert out == _default_group_map()
    # GROUPS_V2 默认把黑色系拆成两组（P10-1 修复目标）
    assert out["i0"] == "ferrous_raw"
    assert out["rb0"] == "ferrous_steel"


def test_group_map_config_read_error_falls_back_groups_v2():
    with mock.patch("scripts.p5_engineA_cross_section.load_config", side_effect=RuntimeError):
        assert _resolve_group_map(None) == _default_group_map()


def test_group_cap_explicit_wins():
    assert _resolve_group_cap(0.5) == 0.5


def test_group_cap_config_enabled_reads_config():
    with mock.patch("scripts.p5_engineA_cross_section.load_config") as m_load:
        m_load.return_value.backtest.engine_a.group_cap = 0.5
        assert _resolve_group_cap(None) == 0.5


def test_group_cap_config_none_disabled():
    with mock.patch("scripts.p5_engineA_cross_section.load_config") as m_load:
        m_load.return_value.backtest.engine_a.group_cap = None
        assert _resolve_group_cap(None) is None


def test_group_cap_config_read_error_disabled():
    with mock.patch("scripts.p5_engineA_cross_section.load_config", side_effect=RuntimeError):
        assert _resolve_group_cap(None) is None
