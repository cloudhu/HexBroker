"""P2-4 多源级联测试：主源信号过期时回退到更新的兜底源信号。

回归场景（审核缺陷）：
- 主源（tail_ext）信号停留在 08-01（已过期）而兜底源（v8 基线）有 08-20 新鲜信号
  → 应返回兜底源（source=engine_a_fb1，effective=True），而非主源过期信号。
"""

from __future__ import annotations

import pandas as pd
import pytest

from hexbroker.paper.signals import SignalEngine


def _write_cache(path, rows) -> None:
    """写入信号 parquet（列与生产缓存一致：symbol/ts/p_up/exp_ret/is_effective）。"""
    df = pd.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "ts": pd.to_datetime([r[1] for r in rows]),
            "p_up": [r[2] for r in rows],
            "exp_ret": [r[3] for r in rows],
            "is_effective": [r[4] for r in rows],
        }
    )
    df.to_parquet(path, index=False)


def _engine(tmp_path, primary_rows, backup_rows, threshold: int = 5) -> SignalEngine:
    """构造双源引擎。

    注：本文件验证「多源级联」语义，故显式传入宽松阈值 5（与生产默认解耦）；
    P0-3 后生产默认阈值为 0（隔夜过期），阈值语义的用例见 ``test_signal_freshness.py``。
    """
    primary = tmp_path / "primary.parquet"
    backup = tmp_path / "backup.parquet"
    _write_cache(primary, primary_rows)
    _write_cache(backup, backup_rows)
    return SignalEngine(cache_paths=[primary, backup], freshness_threshold_days=threshold)


def test_cascade_fallback_uses_fresh_backup_when_primary_stale(tmp_path):
    """主源 08-01 过期 + 兜底 08-20 新鲜 → 返回兜底（engine_a_fb1，effective=True）。"""
    eng = _engine(
        tmp_path,
        primary_rows=[("ag0", "2026-08-01 15:00", 0.80, 1.0, True)],
        backup_rows=[("ag0", "2026-08-20 15:00", 0.62, 0.5, True)],
    )
    sig = eng.latest_signal("ag0", asof="2026-08-21 10:00")
    assert sig is not None
    assert sig.source == "engine_a_fb1"
    assert sig.ts.date().isoformat() == "2026-08-20"
    assert sig.is_effective is True
    assert sig.p_up == pytest.approx(0.62)


def test_cascade_primary_fresh_wins(tmp_path):
    """主源新鲜 → 返回主源（engine_a）。"""
    eng = _engine(
        tmp_path,
        primary_rows=[("ag0", "2026-08-20 15:00", 0.80, 1.0, True)],
        backup_rows=[("ag0", "2026-08-18 15:00", 0.55, 0.3, True)],
    )
    sig = eng.latest_signal("ag0", asof="2026-08-21 10:00")
    assert sig is not None
    assert sig.source == "engine_a"
    assert sig.ts.date().isoformat() == "2026-08-20"
    assert sig.is_effective is True


def test_cascade_backup_stale_falls_back_to_primary(tmp_path):
    """主源过期 + 兜底也过期/无更新 → 返回主源（原语义，is_effective=False）。"""
    eng = _engine(
        tmp_path,
        primary_rows=[("ag0", "2026-08-01 15:00", 0.80, 1.0, True)],
        backup_rows=[("ag0", "2026-07-29 15:00", 0.55, 0.3, True)],
    )
    sig = eng.latest_signal("ag0", asof="2026-08-21 10:00")
    assert sig is not None
    assert sig.source == "engine_a"
    assert sig.freshness_days > 5
    assert sig.is_effective is False


def test_cascade_all_empty_returns_none(tmp_path):
    """全部源无目标品种信号 → None。"""
    eng = _engine(
        tmp_path,
        primary_rows=[("rb0", "2026-08-20 15:00", 0.7, 0.5, True)],
        backup_rows=[("rb0", "2026-08-19 15:00", 0.6, 0.4, True)],
    )
    assert eng.latest_signal("ag0", asof="2026-08-21 10:00") is None
