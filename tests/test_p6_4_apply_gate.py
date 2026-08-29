"""p6_4_apply_persisted_dir 的 ``--trading-day`` 拉取体检单测。

背景（2026-08-29 补齐的语义缺口）
--------------------------------
原实现只判「目录 0 个 json」，两类真实漏网：

1. **数据陈旧**：目录非空但数据是几天前的 → 照常融合并 exit 0，
   把"真实拉取失败"伪装成"刷新成功"；
2. **品种不全**：18 个只落 5 个 → 同样 exit 0，而下游按 18 品种出信号。

三者后果相同（缓存不延长 → fd>0 → 隔夜过期门禁禁开），但原因不同
（配额 / 网络 / 部分超时），因此**都必须是显式失败且判定文案准确**。

为何单测而非跑真数据
--------------------
本脚本的 happy path 会真正调用 ``p6_4_fill_gaps.py --stage parse`` 写入生产 parquet。
用真实目录验证门禁会污染生产数据，故只测纯函数判定逻辑。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
MODULE_PATH = SCRIPTS / "p6_4_apply_persisted_dir.py"


def _load_module():
    """scripts/ 不是包，按文件路径加载。"""
    spec = importlib.util.spec_from_file_location("p6_4_apply_persisted_dir", MODULE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["p6_4_apply_persisted_dir"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()

SYMS = [
    "ag0", "al0", "au0", "cf0", "cu0", "hc0", "i0", "j0", "jm0",
    "m0", "ni0", "p0", "rb0", "sc0", "sr0", "ta0", "y0", "zn0",
]


def _write(d: Path, syms: list[str], date_str: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for s in syms:
        payload = {
            "result": {
                "type": "dataframe",
                "columns": ["symbol", "date", "open", "high", "low", "close",
                            "volume", "open_interest"],
                "rows": [[f"{s.upper()}_DOMINANT", date_str, 1.0, 2.0, 0.5, 1.5,
                          100.0, 1000.0]],
            }
        }
        (d / f"{s}.json").write_text(json.dumps(payload, ensure_ascii=False),
                                     encoding="utf-8")


def _files(d: Path) -> list[Path]:
    return sorted(p for p in d.glob("*.json") if not p.name.endswith(".clean.json"))


# ---------------------------------------------------------------------------
# _norm_date
# ---------------------------------------------------------------------------
class TestNormDate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("20260828", "20260828"),       # pandadata 原生格式
            ("2026-08-28", "20260828"),     # 带横线
            ("2026/08/28", "20260828"),     # 斜杠
            ("2026-08-28 00:00:00", "20260828"),  # 带时间截前 8 位
        ],
    )
    def test_normalize(self, raw, expected):
        assert mod._norm_date(raw) == expected


# ---------------------------------------------------------------------------
# _earliest_ok
# ---------------------------------------------------------------------------
def test_earliest_ok():
    assert mod._earliest_ok("2026-08-28", 5) == "20260823"
    assert mod._earliest_ok("20260828", 0) == "20260828"
    assert mod._earliest_ok("2026-03-01", 5) == "20260224"  # 跨月


# ---------------------------------------------------------------------------
# _scan_latest_date
# ---------------------------------------------------------------------------
class TestScanLatestDate:
    def test_global_and_per_symbol(self, tmp_path):
        d = tmp_path / "mixed"
        _write(d, ["rb0"], "20260820")
        _write(d, ["cu0"], "20260828")
        latest, per = mod._scan_latest_date(_files(d))
        assert latest == "20260828"
        assert per == {"rb0": "20260820", "cu0": "20260828"}

    def test_mixed_formats(self, tmp_path):
        """pandadata(20260828) 与其他源(2026-08-28) 混排也能正确取最大。"""
        d = tmp_path / "fmt"
        _write(d, ["rb0"], "20260827")
        _write(d, ["cu0"], "2026-08-28")
        latest, _ = mod._scan_latest_date(_files(d))
        assert latest == "20260828"

    def test_missing_date_column(self, tmp_path):
        d = tmp_path / "nocol"
        d.mkdir(parents=True)
        (d / "rb0.json").write_text(
            json.dumps({"result": {"type": "dataframe", "columns": ["x"], "rows": [[1]]}}),
            encoding="utf-8",
        )
        latest, per = mod._scan_latest_date(_files(d))
        assert latest is None
        assert per == {"rb0": None}

    def test_corrupted_file_does_not_break_scan(self, tmp_path):
        d = tmp_path / "broken"
        _write(d, ["rb0"], "20260828")
        (d / "cu0.json").write_text("{ not json", encoding="utf-8")
        latest, per = mod._scan_latest_date(_files(d))
        assert latest == "20260828"
        assert per["cu0"] is None


# ---------------------------------------------------------------------------
# _check_trading_day_pull —— 三重判定
# ---------------------------------------------------------------------------
class TestTradingDayPullGate:
    def test_empty(self, tmp_path, capsys):
        d = tmp_path / "empty"
        d.mkdir()
        ok, reason = mod._check_trading_day_pull(d, _files(d), 18, "2026-08-28", 5)
        assert (ok, reason) == (False, "EMPTY")
        assert "0 个品种落盘" in capsys.readouterr().out

    def test_partial(self, tmp_path, capsys):
        """18 只落 5 个 —— 旧版漏网场景。"""
        d = tmp_path / "partial"
        _write(d, SYMS[:5], "2026-08-28")
        ok, reason = mod._check_trading_day_pull(d, _files(d), 18, "2026-08-28", 5)
        assert (ok, reason) == (False, "PARTIAL")
        out = capsys.readouterr().out
        assert "实际 5" in out
        # 判定文案必须与事实一致：数据是新鲜的，不能报"陈旧"
        assert "品种不全" in out
        assert "数据陈旧" not in out

    def test_stale(self, tmp_path, capsys):
        """目录 18 个齐全但数据停在 08-20 —— 旧版漏网的核心场景。"""
        d = tmp_path / "stale"
        _write(d, SYMS, "2026-08-20")
        ok, reason = mod._check_trading_day_pull(d, _files(d), 18, "2026-08-28", 5)
        assert (ok, reason) == (False, "STALE")
        out = capsys.readouterr().out
        assert "20260820" in out
        assert "数据陈旧" in out

    def test_fresh_passes(self, tmp_path, capsys):
        d = tmp_path / "fresh"
        _write(d, SYMS, "20260828")
        ok, reason = mod._check_trading_day_pull(d, _files(d), 18, "2026-08-28", 5)
        assert (ok, reason) == (True, "OK")

    def test_weekend_tolerance(self, tmp_path):
        """周五数据 + 周一基准，容忍窗口内放行。"""
        d = tmp_path / "wk"
        _write(d, SYMS, "2026-08-28")  # 周五
        ok, _ = mod._check_trading_day_pull(d, _files(d), 18, "2026-08-31", 5)  # 周一
        assert ok is True

    def test_empty_date(self, tmp_path, capsys):
        d = tmp_path / "nodate"
        d.mkdir(parents=True)
        for s in SYMS:
            (d / f"{s}.json").write_text(
                json.dumps({"result": {"type": "dataframe",
                                       "columns": ["x"], "rows": [[1]]}}),
                encoding="utf-8",
            )
        ok, reason = mod._check_trading_day_pull(d, _files(d), 18, "2026-08-28", 5)
        assert (ok, reason) == (False, "EMPTY_DATE")
        assert "无有效日期" in capsys.readouterr().out

    def test_custom_tolerance(self, tmp_path):
        d = tmp_path / "tol"
        _write(d, SYMS, "2026-08-20")
        # 默认 5 天 → 陈旧
        assert mod._check_trading_day_pull(d, _files(d), 18, "2026-08-28", 5)[0] is False
        # 放宽到 15 天 → 放行
        assert mod._check_trading_day_pull(d, _files(d), 18, "2026-08-28", 15)[0] is True


def test_default_stale_days_matches_data_layer():
    """脚本默认容忍天数应与数据层门禁一致，避免两处口径漂移。"""
    from hexbroker.data.freshness import DEFAULT_MAX_STALE_DAYS

    assert mod._default_stale_days() == DEFAULT_MAX_STALE_DAYS
