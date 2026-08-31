"""序 3 三品种 adj_close 口径重建（ag0/au0/m0）单测。

守护三件事
----------
1. ``RAW_SCALE_FIX`` 的历史行为**不得回归** —— P6-4 补洞路径仍依赖它保证
   拼接连续性，乱改会引入 20%~216% 伪跳变（``scale=None`` 默认路径）；
2. 序 3 口径重建必须能**显式绕过** ``RAW_SCALE_FIX``（``scale=1.0`` /
   ``--no-scale-fix``），否则正确的 close_pcr 后复权真值会被反过来污染
   成名义价，与修复目标完全相反；
3. ``--keep-raw-close`` 必须从既有分区逐行继承 raw_close（三列中唯一
   正确的列），且真值含既有分区没有的日期时**中止 + 零写盘**，
   绝不填 NaN、绝不回退到 adj 复制品。

为何全用 tmp 湖
---------------
--apply 的目标是生产湖三品种 27 个分区 —— 真数据测试等于生产污染，
铁律禁止。rebuild_partition 本体语义由 tests/test_rebuild.py 守护，
本文件只测驱动器新增的编排分支。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
P11_PATH = SCRIPTS / "p11_truth_rebuild.py"
P64_PATH = SCRIPTS / "p6_4_fill_gaps.py"

# ag0 在 RAW_SCALE_FIX 中的常数（脚本内硬编码，此处独立抄录一份以防脚本被误改）
AG0_SCALE = 1.4505499241026856


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


p6_4 = _load("p6_4_fill_gaps", P64_PATH)
mod = _load("p11_truth_rebuild", P11_PATH)


def _ag_persisted(dates: list[str], base: float = 3500.0) -> dict:
    """p6_4 persisted 格式的合成真值（AG 主力，close_pcr 口径）。"""
    rows = []
    for i, d in enumerate(dates):
        rows.append([
            d.replace("-", ""), "AG_DOMINANT.SHF", "AG", "SHF", f"AG{i + 1}.SHF",
            base + i, base + i + 5, base + i - 5, base + i + 1,
            100000.0 + i, 0.0, 200000.0 + i, base + i + 1,
        ])
    return {
        "ok": True,
        "method": "get_future_daily_post",
        "params": {"underlying_symbol": ["AG"],
                   "start_date": dates[0].replace("-", ""),
                   "end_date": dates[-1].replace("-", ""), "method": "close_pcr"},
        "result": {
            "type": "dataframe",
            "columns": ["date", "symbol", "underlying_symbol", "exchange",
                        "dominant_id", "open", "high", "low", "close",
                        "volume", "amount", "open_interest", "settle"],
            "rows": rows,
        },
    }


def _write_persisted(tmp: Path, payload: dict) -> Path:
    p = tmp / "truth.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _write_ag0_partition(root: Path, dates: list[str],
                         raw_base: float = 9000.0) -> Path:
    """写入「adj_close 被名义价污染」形态的 ag0 既有分区。

    污染形态：adj_close == raw_close == 名义价（k=1，事实 1/2 的实测形态）。
    raw_close 取 9000+ 段，与真值 close（3500+ 段）数量级分离，
    便于断言"继承的是 raw_close 而不是 adj 复制品"。
    """
    from hexbroker.utils.io import write_parquet
    n = len(dates)
    raws = [raw_base + i for i in range(n)]
    df = pd.DataFrame({
        "symbol": ["ag0"] * n,
        "datetime": pd.to_datetime(dates),
        "open": [raw_base + i for i in range(n)],
        "high": [raw_base + i + 5 for i in range(n)],
        "low": [raw_base + i - 5 for i in range(n)],
        "close": raws,                      # 污染：close 也是名义价口径
        "volume": [1.0] * n, "amount": [0.0] * n,
        "open_interest": [1.0] * n,
        "raw_close": raws,                  # ✅ 唯一正确的列
        "adj_close": raws,                  # ❌ 坏的：名义价的拷贝
        "limit_up": False, "limit_down": False, "is_rollover": False,
    })
    d = root / "processed" / "ag0" / "1d"
    d.mkdir(parents=True, exist_ok=True)
    write_parquet(df, d / "2019.parquet")
    return d / "2019.parquet"


def _write_healthy_partition(root: Path, sym: str, year: int,
                             dates: list[str]) -> Path:
    """写入一个健康品种的年度分区（只用于构造参考交易日历）。

    ``load_reference_calendar`` 只读 ``datetime`` 列，故此处保持最小列集。
    """
    from hexbroker.utils.io import write_parquet
    n = len(dates)
    df = pd.DataFrame({
        "symbol": [sym] * n,
        "datetime": pd.to_datetime(dates),
        "close": [100.0 + i for i in range(n)],
    })
    d = root / "processed" / sym / "1d"
    d.mkdir(parents=True, exist_ok=True)
    write_parquet(df, d / f"{year}.parquet")
    return d / f"{year}.parquet"


def _bdates(start: str, n: int) -> list[str]:
    """从 start 起的 n 个连续工作日（避开 2% 缩水门禁需要足够规模）。"""
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, periods=n)]


def _set_adj_close(part: Path, when: str, ratio: float) -> None:
    """把分区中某日的 adj_close 改成 raw_close * ratio（破坏 k==1 指纹）。"""
    from hexbroker.utils.io import write_parquet
    df = pd.read_parquet(part)
    m = df["datetime"] == pd.Timestamp(when)
    assert m.any(), f"分区中无 {when}"
    df.loc[m, "adj_close"] = df.loc[m, "raw_close"].astype(float) * ratio
    write_parquet(df, part)


def _make_lake(tmp: Path) -> Path:
    return tmp / "raw"


# ---------------------------------------------------------------------------
# 1&2) normalize_new_df 的 scale 语义
# ---------------------------------------------------------------------------
def test_scale_none_keeps_raw_scale_fix_for_ag0():
    """回归保护：scale=None（默认）对 ag0 仍乘 RAW_SCALE_FIX 常数。"""
    dates = ["2019-01-02", "2019-01-03", "2019-01-04"]
    df = pd.DataFrame(_ag_persisted(dates)["result"]["rows"],
                      columns=_ag_persisted(dates)["result"]["columns"])
    out = p6_4.normalize_new_df(df, "AG", "ag0",
                                date(2019, 1, 1), date(2019, 12, 31))
    assert len(out) == 3
    expected = [3501.0, 3502.0, 3503.0]
    for got, exp in zip(out["adj_close"], expected):
        assert got == exp * AG0_SCALE
    # raw_close 同步被换算（历史行为：raw_close = close * scale）
    for got, exp in zip(out["raw_close"], expected):
        assert got == exp * AG0_SCALE


def test_scale_one_bypasses_raw_scale_fix_for_ag0():
    """序 3 核心：scale=1.0 对 ag0 不乘常数，adj_close 等于输入 close 原值。"""
    dates = ["2019-01-02", "2019-01-03", "2019-01-04"]
    payload = _ag_persisted(dates)
    df = pd.DataFrame(payload["result"]["rows"],
                      columns=payload["result"]["columns"])
    out = p6_4.normalize_new_df(df, "AG", "ag0",
                                date(2019, 1, 1), date(2019, 12, 31),
                                scale=1.0)
    assert len(out) == 3
    assert list(out["adj_close"]) == [3501.0, 3502.0, 3503.0]
    assert list(out["close"]) == [3501.0, 3502.0, 3503.0]


def test_scale_one_does_not_touch_unlisted_symbol():
    """rb0 不在 RAW_SCALE_FIX 内 → scale 是否显式给都不应改变结果。"""
    dates = ["2019-01-02"]
    payload = _ag_persisted(dates)
    payload["result"]["rows"][0][1] = "RB_DOMINANT.SHF"
    payload["result"]["rows"][0][2] = "RB"
    df = pd.DataFrame(payload["result"]["rows"],
                      columns=payload["result"]["columns"])
    a = p6_4.normalize_new_df(df, "RB", "rb0", date(2019, 1, 1), date(2019, 12, 31))
    b = p6_4.normalize_new_df(df, "RB", "rb0", date(2019, 1, 1), date(2019, 12, 31),
                              scale=1.0)
    assert list(a["adj_close"]) == list(b["adj_close"]) == [3501.0]


# ---------------------------------------------------------------------------
# 3) --keep-raw-close 从既有分区继承 raw_close
# ---------------------------------------------------------------------------
def test_keep_raw_close_inherits_existing_column(tmp_path):
    """--keep-raw-close：逐值继承既有分区 raw_close，adj_close 取 close_pcr 真值。"""
    dates = ["2019-01-02", "2019-01-03", "2019-01-04"]
    persisted = _write_persisted(tmp_path, _ag_persisted(dates))
    root = _make_lake(tmp_path)
    _write_ag0_partition(root, dates, raw_base=9000.0)

    rc = mod.main(["--persisted", str(persisted), "--sym", "ag0",
                   "--year", "2019", "--data-root", str(root),
                   "--no-scale-fix", "--keep-raw-close", "--apply"])

    assert rc == 0
    from hexbroker.data.store import DataLake
    bf = DataLake(root=root).load_processed("ag0", "1d", warn=False)
    got = bf.df.reset_index()
    got = got[got["datetime"].dt.year == 2019].sort_values("datetime")
    assert len(got) == 3
    # raw_close 逐值继承（9000/9001/9002），未被真值覆盖、未被 adj 复制品替换
    assert list(got["raw_close"]) == [9000.0, 9001.0, 9002.0]
    # adj_close/close 来自 close_pcr 真值且未被 RAW_SCALE_FIX 污染
    assert list(got["adj_close"]) == [3501.0, 3502.0, 3503.0]
    assert list(got["close"]) == [3501.0, 3502.0, 3503.0]
    # k = adj/raw 已不再是 1（污染形态被根治）
    assert ((got["adj_close"] / got["raw_close"]) == 1.0).sum() == 0


def test_keep_raw_close_dry_run_writes_nothing(tmp_path):
    """--keep-raw-close + dry-run：零写盘（--apply 才落盘）。"""
    dates = ["2019-01-02", "2019-01-03"]
    persisted = _write_persisted(tmp_path, _ag_persisted(dates))
    root = _make_lake(tmp_path)
    target = _write_ag0_partition(root, dates)
    before = target.read_bytes()

    rc = mod.main(["--persisted", str(persisted), "--sym", "ag0",
                   "--year", "2019", "--data-root", str(root),
                   "--no-scale-fix", "--keep-raw-close"])

    assert rc == 0
    assert target.read_bytes() == before


# ---------------------------------------------------------------------------
# 4) --keep-raw-close 且真值含既有分区没有的日期 → 中止 + 零写盘
# ---------------------------------------------------------------------------
def test_keep_raw_close_aborts_on_unknown_truth_dates(tmp_path):
    """真值是既有分区的超集（真值多出 01-04）→ 无法继承 → 中止且零写盘。

    刻意构造成「预检能通过」的形态（真值覆盖既有分区全部日期），
    以隔离验证新增的继承分支，而非复用已有的 uncovered-dates 预检。
    """
    part_dates = ["2019-01-02", "2019-01-03"]
    truth_dates = ["2019-01-02", "2019-01-03", "2019-01-04"]  # 多出 01-04
    persisted = _write_persisted(tmp_path, _ag_persisted(truth_dates))
    root = _make_lake(tmp_path)
    target = _write_ag0_partition(root, part_dates)
    before = target.read_bytes()

    rc = mod.main(["--persisted", str(persisted), "--sym", "ag0",
                   "--year", "2019", "--data-root", str(root),
                   "--no-scale-fix", "--keep-raw-close", "--apply"])

    assert rc != 0
    # 零写盘：分区逐字节未动
    assert target.read_bytes() == before


def test_keep_raw_close_missing_partition_aborts(tmp_path):
    """--keep-raw-close 但无既有分区（无源可继承）→ 中止，不静默新建。"""
    dates = ["2019-01-02", "2019-01-03"]
    persisted = _write_persisted(tmp_path, _ag_persisted(dates))
    root = _make_lake(tmp_path)

    rc = mod.main(["--persisted", str(persisted), "--sym", "ag0",
                   "--year", "2019", "--data-root", str(root),
                   "--no-scale-fix", "--keep-raw-close", "--apply"])

    assert rc != 0
    assert not (root / "processed" / "ag0" / "1d" / "2019.parquet").exists()


# ---------------------------------------------------------------------------
# 5) --drop-uncovered 三重校验（序 3 幽灵行清理）
# ---------------------------------------------------------------------------
def test_drop_uncovered_deletes_ghost_rows(tmp_path):
    """硬校验①②全中 → 剔除幽灵行，真值落地且 raw_close 逐值继承。

    分区取 100 行、幽灵行 1 个，使缩水 1% < D2 门禁 2% 阈值——
    否则会被缩水门禁拦下（那是另一道锁，不是本用例要测的）。
    """
    part_dates = _bdates("2019-01-02", 100)
    truth_dates = part_dates[:99]          # 末日 part_dates[99] 真值没有
    persisted = _write_persisted(tmp_path, _ag_persisted(truth_dates))
    root = _make_lake(tmp_path)
    target = _write_ag0_partition(root, part_dates)   # 幽灵行 k==1（污染形态）
    # 参考日历含 99 个真实交易日，**不含**幽灵行那日
    _write_healthy_partition(root, "rb0", 2019, truth_dates)

    rc = mod.main(["--persisted", str(persisted), "--sym", "ag0",
                   "--year", "2019", "--data-root", str(root),
                   "--no-scale-fix", "--keep-raw-close",
                   "--drop-uncovered", "--apply"])

    assert rc == 0
    got = pd.read_parquet(target).sort_values("datetime").reset_index(drop=True)
    assert len(got) == 99, "幽灵行应被剔除，且不应残留"
    assert got["datetime"].iloc[-1] == pd.Timestamp(part_dates[98])
    # 真值落地：adj_close 取 close_pcr，raw_close 逐值继承既有正确列
    assert got["adj_close"].iloc[0] == 3501.0
    assert got["raw_close"].iloc[0] == 9000.0
    assert got["raw_close"].iloc[1] == 9001.0


def test_drop_uncovered_aborts_when_date_is_real_trading_day(tmp_path):
    """校验②失守：待删日期出现在 15 健康品种参考日历中 → 中止 + 零写盘。

    这是最关键的一道锁——它拦住"真实交易日被误删"。
    """
    part_dates = _bdates("2019-01-02", 100)
    truth_dates = part_dates[:99]
    persisted = _write_persisted(tmp_path, _ag_persisted(truth_dates))
    root = _make_lake(tmp_path)
    target = _write_ag0_partition(root, part_dates)
    before = target.read_bytes()
    # 参考日历**包含**幽灵行那日 → 判定为真实交易日，禁止删
    _write_healthy_partition(root, "rb0", 2019, part_dates)

    rc = mod.main(["--persisted", str(persisted), "--sym", "ag0",
                   "--year", "2019", "--data-root", str(root),
                   "--no-scale-fix", "--keep-raw-close",
                   "--drop-uncovered", "--apply"])

    assert rc != 0
    assert target.read_bytes() == before, "校验未过却写盘了"


def test_drop_uncovered_ignores_k_when_not_one(tmp_path):
    """校验③（k==1）降级为观察项：k≠1 **不再拦截**，删除照常执行。

    2026-08-30 裁决（实证见 artifacts/_gate3_out.txt）：k==1 在 2018~2024
    污染年份占比 82.4%~100.0%（2018 年三品种恰为 100%），似然比≈1.00，
    零判别力；而 2025/2026 干净年份为 0%，判别力最强却无幽灵行。判据与
    目标错位，且会在分区已重建（k 不再为 1）后**误拦正确删除**，故仅保留打印。

    本用例锁定"降级"这一语义：①② 全中、③ 未中 → rc=0 且幽灵行被删除。
    """
    part_dates = _bdates("2019-01-02", 100)
    truth_dates = part_dates[:99]
    ghost = part_dates[99]
    persisted = _write_persisted(tmp_path, _ag_persisted(truth_dates))
    root = _make_lake(tmp_path)
    target = _write_ag0_partition(root, part_dates)
    _set_adj_close(target, ghost, ratio=0.98)     # 破坏 k==1 指纹
    _write_healthy_partition(root, "rb0", 2019, truth_dates)   # 非空且不含 ghost

    rc = mod.main(["--persisted", str(persisted), "--sym", "ag0",
                   "--year", "2019", "--data-root", str(root),
                   "--no-scale-fix", "--keep-raw-close",
                   "--drop-uncovered", "--apply"])

    assert rc == 0, "③ 已降级为观察项，k≠1 不应拦截"
    got = pd.read_parquet(target).sort_values("datetime").reset_index(drop=True)
    assert len(got) == 99, "①② 全中即应删除，③ 不得否决"
    assert pd.Timestamp(ghost) not in set(got["datetime"])
    assert got["raw_close"].iloc[0] == 9000.0


def test_drop_uncovered_aborts_when_ref_calendar_empty(tmp_path):
    """② 是唯一硬锁，其参考日历为空必须硬中止。

    空并集会让"日期不在参考日历中"恒真 —— 若此时不中止，任何待删日期
    都会被放行，等于**无门禁删除**。2026-08-30 ③ 降级后，② 成为唯一
    拦截判据，这道前置检查的载荷进一步加重，故单独立例锁定。
    """
    part_dates = _bdates("2019-01-02", 100)
    truth_dates = part_dates[:99]
    persisted = _write_persisted(tmp_path, _ag_persisted(truth_dates))
    root = _make_lake(tmp_path)
    target = _write_ag0_partition(root, part_dates)
    before = target.read_bytes()
    # 刻意**不写**任何健康品种分区 → 参考日历为空

    rc = mod.main(["--persisted", str(persisted), "--sym", "ag0",
                   "--year", "2019", "--data-root", str(root),
                   "--no-scale-fix", "--keep-raw-close",
                   "--drop-uncovered", "--apply"])

    assert rc != 0
    assert target.read_bytes() == before, "参考日历为空却写盘了"


def test_drop_uncovered_without_flag_still_aborts(tmp_path):
    """回归保护：不加 --drop-uncovered 时，未覆盖日期仍一律中止（零写盘）。"""
    part_dates = _bdates("2019-01-02", 100)
    truth_dates = part_dates[:99]
    persisted = _write_persisted(tmp_path, _ag_persisted(truth_dates))
    root = _make_lake(tmp_path)
    target = _write_ag0_partition(root, part_dates)
    before = target.read_bytes()
    _write_healthy_partition(root, "rb0", 2019, truth_dates)

    rc = mod.main(["--persisted", str(persisted), "--sym", "ag0",
                   "--year", "2019", "--data-root", str(root),
                   "--no-scale-fix", "--keep-raw-close", "--apply"])

    assert rc != 0
    assert target.read_bytes() == before


# ---------------------------------------------------------------------------
# CLI 开关互斥性
# ---------------------------------------------------------------------------
def test_no_scale_fix_conflicts_with_explicit_scale(tmp_path):
    """--no-scale-fix 与 --scale 非 1.0 冲突 → rc=2 参数错误。"""
    dates = ["2019-01-02"]
    persisted = _write_persisted(tmp_path, _ag_persisted(dates))
    root = _make_lake(tmp_path)

    rc = mod.main(["--persisted", str(persisted), "--sym", "ag0",
                   "--year", "2019", "--data-root", str(root),
                   "--no-scale-fix", "--scale", "1.5"])

    assert rc == 2


def test_no_scale_fix_equivalent_to_scale_one(tmp_path):
    """--no-scale-fix 等价于 --scale 1.0（端到端结果一致）。"""
    dates = ["2019-01-02", "2019-01-03"]
    root_a = _make_lake(tmp_path / "a")
    root_b = _make_lake(tmp_path / "b")
    for root in (root_a, root_b):
        _write_ag0_partition(root, dates, raw_base=9000.0)

    pa = _write_persisted(tmp_path / "a", _ag_persisted(dates))
    pb = _write_persisted(tmp_path / "b", _ag_persisted(dates))

    assert mod.main(["--persisted", str(pa), "--sym", "ag0", "--year", "2019",
                     "--data-root", str(root_a), "--no-scale-fix",
                     "--keep-raw-close", "--apply"]) == 0
    assert mod.main(["--persisted", str(pb), "--sym", "ag0", "--year", "2019",
                     "--data-root", str(root_b), "--scale", "1.0",
                     "--keep-raw-close", "--apply"]) == 0

    a = pd.read_parquet(root_a / "processed" / "ag0" / "1d" / "2019.parquet")
    b = pd.read_parquet(root_b / "processed" / "ag0" / "1d" / "2019.parquet")
    pd.testing.assert_frame_equal(
        a.sort_values("datetime").reset_index(drop=True),
        b.sort_values("datetime").reset_index(drop=True))
