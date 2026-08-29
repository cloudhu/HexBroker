"""P1-c 存量 raw_close 批量回填驱动器（``scripts/p37_raw_close_backfill.py``）单测。

与 test_p11_truth_rebuild.py 同一纪律：--apply 的目标是生产湖，真数据
测试等于生产污染，铁律禁止 —— 全部用 tmp 湖 + 合成备源 fetcher 验证
编排逻辑（dry-run 零写盘 / apply 只动 raw_close + manifest 语义 / 幂等 /
低覆盖大声降级）。enrich_raw_close 本体已由 test_p6_4_nominal_enrich.py
10 测试守护。
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
MODULE_PATH = SCRIPTS / "p37_raw_close_backfill.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("p37_raw_close_backfill", MODULE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["p37_raw_close_backfill"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


@dataclasses.dataclass
class FakePull:
    close: pd.Series
    source: str = "sina"
    warnings: list = dataclasses.field(default_factory=list)


class FakeFetcher:
    """鸭子类型 fetcher：返回合成名义价（与 adj 复制品明显不同）。"""

    def __init__(self, nominal: pd.Series, source: str = "sina"):
        self._nominal = nominal
        self._source = source
        self.calls = 0

    def fetch_raw(self, symbols, start, end):
        self.calls += 1
        mask = ((self._nominal.index >= pd.Timestamp(start))
                & (self._nominal.index <= pd.Timestamp(end)))
        return {s: FakePull(close=self._nominal[mask], source=self._source)
                for s in symbols}


def _part_df(dates: list[str]) -> pd.DataFrame:
    n = len(dates)
    return pd.DataFrame({
        "symbol": ["A"] * n,
        "datetime": pd.to_datetime(dates),
        "open": [3500.0] * n, "high": [3510.0] * n, "low": [3490.0] * n,
        "close": [3500.0] * n, "volume": [1.0] * n, "amount": [0.0] * n,
        "open_interest": [1.0] * n,
        "raw_close": [3500.0] * n, "adj_close": [3500.0] * n,
        "limit_up": False, "limit_down": False, "is_rollover": False,
    })


def _make_lake(tmp: Path) -> tuple[Path, dict[str, bytes]]:
    """tmp 湖：A（backfill-* manifest，2 个分区）+ B（v1 manifest，1 个分区）。"""
    from hexbroker.utils.io import write_parquet
    root = tmp / "raw"
    d_a = root / "processed" / "A" / "1d"
    d_b = root / "processed" / "B" / "1d"
    d_a.mkdir(parents=True)
    d_b.mkdir(parents=True)
    write_parquet(_part_df(["2019-01-02", "2019-01-03", "2019-01-04"]),
                  d_a / "2019.parquet")
    write_parquet(_part_df(["2020-01-02", "2020-01-03"]), d_a / "2020.parquet")
    write_parquet(_part_df(["2019-01-02"]), d_b / "2019.parquet")
    (d_a / "manifest.json").write_text(
        json.dumps({"data_version": "backfill-20260101T000000", "n_rows": 5},
                   ensure_ascii=False), encoding="utf-8")
    (d_b / "manifest.json").write_text(
        json.dumps({"data_version": "v1", "n_rows": 1},
                   ensure_ascii=False), encoding="utf-8")
    snapshot = {str(p): p.read_bytes()
                for p in root.rglob("*") if p.is_file()}
    return root, snapshot


def _nominal(index) -> pd.Series:
    """合成名义价序列：3400 起（与 adj 复制品 3500 明显不同）。"""
    return pd.Series([3400.0 + i for i in range(len(index))],
                     index=pd.to_datetime(index))


def test_dry_run_makes_no_writes(tmp_path):
    root, snapshot = _make_lake(tmp_path)
    fetcher = FakeFetcher(_nominal(
        ["2019-01-02", "2019-01-03", "2019-01-04", "2020-01-02", "2020-01-03"]))

    rc = mod.main(["--data-root", str(root)], fetcher=fetcher)

    assert rc == 0
    after = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == snapshot  # 零写盘


def test_apply_rewrites_raw_close_only_and_manifest_semantics(tmp_path):
    root, snapshot = _make_lake(tmp_path)
    fetcher = FakeFetcher(_nominal(
        ["2019-01-02", "2019-01-03", "2019-01-04", "2020-01-02", "2020-01-03"]))

    rc = mod.main(["--data-root", str(root), "--apply"], fetcher=fetcher)

    assert rc == 0
    from hexbroker.utils.io import read_parquet
    d_a = root / "processed" / "A" / "1d"
    # 2019 分区：raw_close 已是真名义价，其余列逐值不变
    df19 = read_parquet(d_a / "2019.parquet")
    assert df19["raw_close"].tolist() == [3400.0, 3401.0, 3402.0]
    assert df19["adj_close"].tolist() == [3500.0] * 3
    assert df19["close"].tolist() == [3500.0] * 3
    df20 = read_parquet(d_a / "2020.parquet")
    assert df20["raw_close"].tolist() == [3403.0, 3404.0]
    # manifest：A（backfill-* 维护）重算；B（v1 盘中自动化）逐字节未动
    assert json.loads((d_a / "manifest.json").read_text(encoding="utf-8")
                      )["data_version"].startswith("backfill-2")
    assert (root / "processed" / "B" / "1d" / "manifest.json").read_bytes() \
        == snapshot[str(root / "processed" / "B" / "1d" / "manifest.json")]
    # 备份目录已创建（root.parent = tmp_path，与生产 root=data/raw → data/ 对应）
    assert any(p.name.startswith("p37_backup_processed_")
               for p in root.parent.iterdir())


def test_apply_idempotent_second_run(tmp_path):
    root, _ = _make_lake(tmp_path)
    fetcher = FakeFetcher(_nominal(
        ["2019-01-02", "2019-01-03", "2019-01-04", "2020-01-02", "2020-01-03"]))
    mod.main(["--data-root", str(root), "--apply"], fetcher=fetcher)

    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    rc = mod.main(["--data-root", str(root), "--apply"], fetcher=FakeFetcher(
        _nominal(["2019-01-02", "2019-01-03", "2019-01-04",
                  "2020-01-02", "2020-01-03"])))

    assert rc == 0
    after = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    # 幂等：第二轮零写盘（备份目录除外 —— 它在 root 外，不在 rglob 结果中）
    assert after == before


class WarmFetcher(FakeFetcher):
    """支持 warm 的 fetcher：记录预热窗口（生产 _CachingFetcher.warm 契约）。"""

    def __init__(self, nominal: pd.Series):
        super().__init__(nominal)
        self.warm_calls: list[tuple[str, str, str]] = []

    def warm(self, sym, start, end):
        self.warm_calls.append((sym, start, end))


def test_warm_up_uses_real_data_range_not_future_window(tmp_path):
    """回归：预热窗口必须取品种实际分区范围且 end 钳制到今天。

    首轮生产 dry-run 162/162 BackupExhaustedError 根因：请求 2099 未来
    窗口被 assert_fresh 判"数据陈旧"（豁免条件是 end < today-5d）。
    """
    from datetime import datetime, timezone

    root, _ = _make_lake(tmp_path)
    fetcher = WarmFetcher(_nominal(
        ["2019-01-02", "2019-01-03", "2019-01-04", "2020-01-02", "2020-01-03"]))

    rc = mod.main(["--data-root", str(root), "--apply"], fetcher=fetcher)

    assert rc == 0
    today = datetime.now(timezone.utc).astimezone().date().isoformat()
    assert sorted(fetcher.warm_calls) == [
        ("A", "2019-01-01", min("2020-12-31", today)),
        ("B", "2019-01-01", min("2019-12-31", today)),
    ]
    from hexbroker.utils.io import read_parquet
    df19 = read_parquet(root / "processed" / "A" / "1d" / "2019.parquet")
    assert df19["raw_close"].tolist() == [3400.0, 3401.0, 3402.0]


def test_low_coverage_partition_skipped_loudly(tmp_path):
    """备源缺 2020 年日期 → 2020 分区大声降级跳过，2019 分区照常回填。"""
    root, snapshot = _make_lake(tmp_path)
    fetcher = FakeFetcher(_nominal(["2019-01-02", "2019-01-03", "2019-01-04"]))

    rc = mod.main(["--data-root", str(root), "--apply"], fetcher=fetcher)

    assert rc == 0
    from hexbroker.utils.io import read_parquet
    # 2019 已回填
    df19 = read_parquet(root / "processed" / "A" / "1d" / "2019.parquet")
    assert df19["raw_close"].tolist() == [3400.0, 3401.0, 3402.0]
    # 2020 覆盖率 0% < 90% → 原样保留（raw_close 仍为 adj 复制品）
    df20 = read_parquet(root / "processed" / "A" / "1d" / "2020.parquet")
    assert df20["raw_close"].tolist() == [3500.0] * 2
    # v1 manifest（B）未动
    assert (root / "processed" / "B" / "1d" / "manifest.json").read_bytes() \
        == snapshot[str(root / "processed" / "B" / "1d" / "manifest.json")]
