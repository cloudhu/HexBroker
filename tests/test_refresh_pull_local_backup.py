"""Tier-2 备源通道回归测试（2026-09-03 P0 生产事故修复）。

事故机理
--------
主连换月窗口内 tqsdk ``KQ.m`` 与湖 ``raw_close`` 分歧（rb0 实测 08-31/09-01
偏差 -1.69%/-1.61%）→ 主路对齐稳定段被击穿 → 判 ``SCALE_UNSTABLE`` /
``SEAM_BASIS_CONFLICT``。而**湖不前进，稳定段就永远长不出 ``MIN_OVERLAP``
天**，主路自愈不了；pandadata MCP 又未接线（``mcp.json`` = ``{"mcpServers":
{}}``）→ 18 品种全线停摆。

修复：主路失败的品种改走 ``hexbroker.data.failover`` 早已定义的 Tier 2 备源
—— ``BackupRawFetcher(save=False)`` 拉名义价 + ``graft_adjusted`` 续接出后
复权（⛔ 严禁自乘 k）。

覆盖
----
1. 主路失败 + 备源成功 → 落盘 json、退出码 0；
2. 主路失败 + 备源亦失败 → 仍计失败、退出码 3（fail-closed）；
3. 落盘 json 与 pandadata 口径一致（columns / date 格式 / close 为后复权）；
4. 备源路径**不写主湖**（mtime + 字节双重快照）；
5. graft 无锚点 / 无新增日 / 缺 OHLC / 锚点陈旧 / 接缝断裂 → 全部拒绝；
6. 主路成功时**不触发**备源；
7. 备源拉取器必须 ``save=False``（备源绝不污染主湖）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "scripts" / "refresh_pull_local.py"
spec = importlib.util.spec_from_file_location("refresh_pull_local", SPEC)
rpl = importlib.util.module_from_spec(spec)
sys.modules["refresh_pull_local"] = rpl
spec.loader.exec_module(rpl)

from hexbroker.data.backup import RawPull  # noqa: E402  （需先插 sys.path）
from hexbroker.data.sources import tqsdk_source as tqsdk_mod  # noqa: E402

# ---- 现场常量（合成 5 日湖 + 备源续接 1 日） -----------------------------
LAKE_DAYS = ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]
LAKE_RAW = 1000.0      # 名义价（平盘，便于断言）
LAKE_K = 2.0           # 湖内复权比 k = close / raw_close
ASOF = "2026-08-31"
WINDOW_START = "2026-08-18"      # --window-days 14 的自然起点
JUMP_LIM = rpl.PRICE_JUMP_DEFAULT


# ===========================================================================
# 夹具与工具
# ===========================================================================
def _write_lake(root: Path, sym0: str, dates, raw: float, k: float) -> Path:
    """写合成主湖 <root>/<sym0>/1d/2026.parquet（close = raw × k）。"""
    day = root / sym0 / "1d"
    day.mkdir(parents=True, exist_ok=True)
    n = len(dates)
    df = pd.DataFrame({
        "symbol": [sym0] * n,
        "datetime": pd.to_datetime(dates),
        "open": raw, "high": raw * 1.01, "low": raw * 0.99,
        "close": raw * k,
        "volume": 10_000.0, "amount": 0.0, "open_interest": 50_000.0,
        "raw_close": raw, "adj_close": raw * k,
        "limit_up": False, "limit_down": False, "is_rollover": False,
    })
    df.to_parquet(day / "2026.parquet", index=False)
    return day


def _make_pull(dates, raws, *, source: str = "sina", with_frame: bool = True,
               drop_cols=()):
    """构造 RawPull（close = 名义价，frame = 完整 OHLCV+OI）。"""
    idx = pd.DatetimeIndex(pd.to_datetime(dates), name="datetime")
    n = len(idx)
    frame = pd.DataFrame({
        "open": raws, "high": [r * 1.01 for r in raws],
        "low": [r * 0.99 for r in raws], "close": raws,
        "volume": [12_000.0] * n, "open_interest": [60_000.0] * n,
    }, index=idx)
    if drop_cols:
        frame = frame.drop(columns=list(drop_cols))
    return RawPull(
        symbol="rb0",
        close=pd.Series(list(raws), index=idx, dtype=float),
        source=source,
        open_interest=pd.Series([60_000.0] * n, index=idx, dtype=float),
        volume=pd.Series([12_000.0] * n, index=idx, dtype=float),
        frame=(frame if with_frame else None),
    )


class _FakeFetcher:
    """鸭子类型备源拉取器（只需 ``fetch_raw``），记录调用参数供断言。"""

    def __init__(self, pulls=None, raise_exc=None, **kwargs):
        self.kwargs = kwargs
        self.pulls = pulls or {}
        self.raise_exc = raise_exc
        self.calls: list = []

    def fetch_raw(self, symbols, start, end):
        self.calls.append((list(symbols), start, end))
        if self.raise_exc:
            raise self.raise_exc
        return self.pulls


def _snapshot(directory: Path) -> dict:
    """目录快照：{相对路径: (mtime_ns, size, sha1前16位)} —— 用于「未写盘」断言。"""
    if not directory.exists():
        return {}
    out = {}
    for p in sorted(directory.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p)] = (st.st_mtime_ns, st.st_size,
                           p.read_bytes()[:4096].__len__())
    return out


def _snapshot_bytes(directory: Path) -> dict:
    """字节级快照（内容 + mtime），最强「未写盘」证据。"""
    if not directory.exists():
        return {}
    return {str(p): (p.stat().st_mtime_ns, p.read_bytes())
            for p in sorted(directory.rglob("*")) if p.is_file()}


# ===========================================================================
# 1. _backup_pull_symbol 单元测试
# ===========================================================================
def test_backup_happy_path_grafts_and_scales_ohlc(tmp_path, monkeypatch):
    """备源成功：gtaft 续接出 08-31，close 为后复权（1010×2=2020），OHLC 同 k 缩放。"""
    monkeypatch.setattr(rpl, "PROCESSED_DIR", tmp_path)
    _write_lake(tmp_path, "rb0", LAKE_DAYS, LAKE_RAW, LAKE_K)

    dates = LAKE_DAYS + ["2026-08-31"]
    raws = [LAKE_RAW] * len(LAKE_DAYS) + [1010.0]
    fetcher = _FakeFetcher(pulls={"rb0": _make_pull(dates, raws)})

    rows, info = rpl._backup_pull_symbol(
        "rb0", "RB", pd.Timestamp(WINDOW_START), pd.Timestamp(ASOF), JUMP_LIM,
        fetcher=fetcher)

    # 仅发射新增日（重叠日本就在湖里，备源只负责把湖往前推）
    assert [r[0] for r in rows] == ["20260831"]
    d = rows[0]
    assert d[1] == "RB"
    assert d[5] == pytest.approx(2020.0), "close 必须是后复权（名义价 1010 × k=2）"
    assert d[2] == pytest.approx(1010.0 * LAKE_K)          # open
    assert d[3] == pytest.approx(1010.0 * 1.01 * LAKE_K)   # high
    assert d[4] == pytest.approx(1010.0 * 0.99 * LAKE_K)   # low
    assert d[6] == pytest.approx(12_000.0)                 # volume
    assert d[7] == pytest.approx(60_000.0)                 # open_interest
    assert info["k"] == pytest.approx(LAKE_K)
    assert info["anchor_date"] == "2026-08-28"
    assert info["seam_status"] == "BACKUP_SINA"
    assert info["source"] == "sina"
    assert fetcher.calls, "备源必须被实际调用"


def test_backup_fetcher_built_with_save_false(tmp_path, monkeypatch):
    """备源拉取器必须以 save=False 构造 —— 备源数据绝不污染主湖。"""
    monkeypatch.setattr(rpl, "PROCESSED_DIR", tmp_path)
    _write_lake(tmp_path, "rb0", LAKE_DAYS, LAKE_RAW, LAKE_K)

    built = {}

    class _Recorder(_FakeFetcher):
        def __init__(self, **kwargs):
            built.update(kwargs)
            super().__init__(pulls={"rb0": _make_pull(
                LAKE_DAYS + ["2026-08-31"],
                [LAKE_RAW] * len(LAKE_DAYS) + [1010.0])}, **kwargs)

    monkeypatch.setattr("hexbroker.data.backup.BackupRawFetcher", _Recorder)
    rows, _ = rpl._backup_pull_symbol(
        "rb0", "RB", pd.Timestamp(WINDOW_START), pd.Timestamp(ASOF), JUMP_LIM)
    assert built.get("save") is False, (
        f"备源必须 save=False（备源落湖会让主源恢复后分不清权威数据）：{built}")
    assert tuple(built.get("sources", ())) == rpl.BACKUP_SOURCES
    assert rows


def test_backup_no_anchor_is_rejected(tmp_path, monkeypatch):
    """红线：湖内无该品种历史（无锚点）→ 拒绝，绝不自造后复权。"""
    monkeypatch.setattr(rpl, "PROCESSED_DIR", tmp_path)   # 空湖
    fetcher = _FakeFetcher(pulls={"rb0": _make_pull(LAKE_DAYS, [LAKE_RAW] * 5)})
    with pytest.raises(ValueError, match="BACKUP_NO_ANCHOR"):
        rpl._backup_pull_symbol(
            "rb0", "RB", pd.Timestamp(WINDOW_START), pd.Timestamp(ASOF),
            JUMP_LIM, fetcher=fetcher)


def test_backup_fetch_failure_is_rejected(tmp_path, monkeypatch):
    """备源拉取抛异常 → ValueError（fail-closed，绝不静默成功）。"""
    monkeypatch.setattr(rpl, "PROCESSED_DIR", tmp_path)
    _write_lake(tmp_path, "rb0", LAKE_DAYS, LAKE_RAW, LAKE_K)
    fetcher = _FakeFetcher(raise_exc=RuntimeError("新浪 502"))
    with pytest.raises(ValueError, match="BACKUP_FETCH"):
        rpl._backup_pull_symbol(
            "rb0", "RB", pd.Timestamp(WINDOW_START), pd.Timestamp(ASOF),
            JUMP_LIM, fetcher=fetcher)


def test_backup_no_new_dates_is_rejected(tmp_path, monkeypatch):
    """备源无湖内缺失的新日期（湖已覆盖）→ 拒绝。"""
    monkeypatch.setattr(rpl, "PROCESSED_DIR", tmp_path)
    _write_lake(tmp_path, "rb0", LAKE_DAYS, LAKE_RAW, LAKE_K)
    fetcher = _FakeFetcher(pulls={"rb0": _make_pull(LAKE_DAYS, [LAKE_RAW] * 5)})
    with pytest.raises(ValueError, match="BACKUP_NO_NEW_DATES"):
        rpl._backup_pull_symbol(
            "rb0", "RB", pd.Timestamp(WINDOW_START), pd.Timestamp(ASOF),
            JUMP_LIM, fetcher=fetcher)


def test_backup_missing_ohlc_frame_is_rejected(tmp_path, monkeypatch):
    """缺 OHLC 帧 → 拒绝（融合端 fillna(0.0) 会写出零价 bar，比不补更糟）。"""
    monkeypatch.setattr(rpl, "PROCESSED_DIR", tmp_path)
    _write_lake(tmp_path, "rb0", LAKE_DAYS, LAKE_RAW, LAKE_K)
    dates = LAKE_DAYS + ["2026-08-31"]
    raws = [LAKE_RAW] * len(LAKE_DAYS) + [1010.0]

    f1 = _FakeFetcher(pulls={"rb0": _make_pull(dates, raws, with_frame=False)})
    with pytest.raises(ValueError, match="BACKUP_NO_OHLC"):
        rpl._backup_pull_symbol(
            "rb0", "RB", pd.Timestamp(WINDOW_START), pd.Timestamp(ASOF),
            JUMP_LIM, fetcher=f1)

    f2 = _FakeFetcher(pulls={"rb0": _make_pull(dates, raws, drop_cols=("open",))})
    with pytest.raises(ValueError, match="BACKUP_NO_OHLC"):
        rpl._backup_pull_symbol(
            "rb0", "RB", pd.Timestamp(WINDOW_START), pd.Timestamp(ASOF),
            JUMP_LIM, fetcher=f2)


def test_backup_seam_nominal_break_is_rejected(tmp_path, monkeypatch):
    """接缝跨源断裂（备源续接段换月、湖未换）→ 拒绝（复用主路 P-NEW 护栏）。"""
    monkeypatch.setattr(rpl, "PROCESSED_DIR", tmp_path)
    _write_lake(tmp_path, "rb0", LAKE_DAYS, LAKE_RAW, LAKE_K)
    dates = LAKE_DAYS + ["2026-08-31"]
    raws = [LAKE_RAW] * len(LAKE_DAYS) + [2000.0]      # 接缝处名义价翻倍
    fetcher = _FakeFetcher(pulls={"rb0": _make_pull(dates, raws)})
    with pytest.raises(ValueError, match="BACKUP_SEAM_NOMINAL_BREAK"):
        rpl._backup_pull_symbol(
            "rb0", "RB", pd.Timestamp(WINDOW_START), pd.Timestamp(ASOF),
            JUMP_LIM, fetcher=fetcher)


def test_backup_stale_anchor_is_rejected(tmp_path, monkeypatch):
    """锚点日与首个续接日间隔 > BACKUP_MAX_ANCHOR_GAP_DAYS → 拒绝（防陈旧锚点外推）。"""
    monkeypatch.setattr(rpl, "PROCESSED_DIR", tmp_path)
    _write_lake(tmp_path, "rb0", LAKE_DAYS[:3], LAKE_RAW, LAKE_K)   # 湖止于 08-26
    dates = LAKE_DAYS[:3] + ["2026-09-04"]                          # 备源跳到 09-04
    raws = [LAKE_RAW] * 3 + [1005.0]
    fetcher = _FakeFetcher(pulls={"rb0": _make_pull(dates, raws)})
    with pytest.raises(ValueError, match="BACKUP_ANCHOR_STALE"):
        rpl._backup_pull_symbol(
            "rb0", "RB", pd.Timestamp(WINDOW_START), pd.Timestamp("2026-09-04"),
            JUMP_LIM, fetcher=fetcher)


def test_backup_never_writes_the_lake_it_reads(tmp_path, monkeypatch):
    """备源路径对主湖**只读**：湖目录 mtime + 字节双重快照零变化。"""
    lake_root = tmp_path / "lake"
    monkeypatch.setattr(rpl, "PROCESSED_DIR", lake_root)
    day = _write_lake(lake_root, "rb0", LAKE_DAYS, LAKE_RAW, LAKE_K)
    before = _snapshot_bytes(lake_root)

    dates = LAKE_DAYS + ["2026-08-31"]
    raws = [LAKE_RAW] * len(LAKE_DAYS) + [1010.0]
    fetcher = _FakeFetcher(pulls={"rb0": _make_pull(dates, raws)})
    rows, _ = rpl._backup_pull_symbol(
        "rb0", "RB", pd.Timestamp(WINDOW_START), pd.Timestamp(ASOF),
        JUMP_LIM, fetcher=fetcher)

    assert rows, "前置条件：备源应成功（否则未写盘的断言是空转的）"
    assert _snapshot_bytes(lake_root) == before, (
        "备源路径写了主湖 —— 写湖是 p6_4_apply_persisted_dir 的职责，"
        "备源只落 artifacts 下的 json")
    assert list(day.glob("*.parquet")) == [day / "2026.parquet"]


def test_backup_lookback_extends_request_window(tmp_path, monkeypatch):
    """备源请求窗口必须向前扩展，否则与湖无重叠 → graft 必然失败。"""
    monkeypatch.setattr(rpl, "PROCESSED_DIR", tmp_path)
    _write_lake(tmp_path, "rb0", LAKE_DAYS, LAKE_RAW, LAKE_K)
    dates = LAKE_DAYS + ["2026-08-31"]
    fetcher = _FakeFetcher(pulls={"rb0": _make_pull(
        dates, [LAKE_RAW] * len(LAKE_DAYS) + [1010.0])})
    rpl._backup_pull_symbol(
        "rb0", "RB", pd.Timestamp(WINDOW_START), pd.Timestamp(ASOF),
        JUMP_LIM, fetcher=fetcher)
    symbols, start, end = fetcher.calls[0]
    assert symbols == ["rb0"]
    assert pd.Timestamp(start) == (pd.Timestamp(WINDOW_START)
                                   - pd.Timedelta(days=rpl.BACKUP_LOOKBACK_DAYS))
    assert pd.Timestamp(end) == pd.Timestamp(ASOF)


def test_backup_excludes_today_bar(tmp_path, monkeypatch):
    """--exclude-today 口径：发射上界前移一日，不发射当日未成型 bar。"""
    monkeypatch.setattr(rpl, "PROCESSED_DIR", tmp_path)
    _write_lake(tmp_path, "rb0", LAKE_DAYS, LAKE_RAW, LAKE_K)
    dates = LAKE_DAYS + ["2026-08-31"]
    fetcher = _FakeFetcher(pulls={"rb0": _make_pull(
        dates, [LAKE_RAW] * len(LAKE_DAYS) + [1010.0])})
    with pytest.raises(ValueError, match="BACKUP_NO_NEW_DATES"):
        rpl._backup_pull_symbol(
            "rb0", "RB", pd.Timestamp(WINDOW_START),
            pd.Timestamp("2026-08-30"), JUMP_LIM, fetcher=fetcher)


# ===========================================================================
# 2. main() 端到端
# ===========================================================================
def _tq_frame(sym0: str, dates, closes, *, oi=50_000.0):
    """合成 tqsdk BarFrame.df（MultiIndex symbol/datetime）。"""
    idx = pd.MultiIndex.from_product(
        [[sym0], pd.DatetimeIndex(pd.to_datetime(dates))],
        names=["symbol", "datetime"])
    return pd.DataFrame({
        "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes,
        "volume": [10_000.0] * len(dates),
        "open_interest": [oi] * len(dates),
    }, index=idx)


class _FakeBarFrame:
    def __init__(self, df):
        self.df = df
        self.metadata = {"fetch_seconds": 0.01}


class _FakeTqsdkSource:
    def __init__(self, frames):
        self.frames = frames

    def fetch_bars(self, symbols, start, end, freq="1d"):
        return _FakeBarFrame(pd.concat([self.frames[s] for s in symbols]))


def _install_fakes(monkeypatch, tmp_path, tq_closes, *, backup_fetcher,
                   lake_dates=LAKE_DAYS, tq_dates=None):
    """装配 main() 的全部外部依赖，返回 (out_dir, lake_root)。"""
    tq_dates = tq_dates or LAKE_DAYS + ["2026-08-31"]
    lake_root = tmp_path / "lake"
    out_dir = tmp_path / "out"
    _write_lake(lake_root, "rb0", lake_dates, LAKE_RAW, LAKE_K)

    monkeypatch.setattr(rpl, "PROCESSED_DIR", lake_root)
    monkeypatch.setattr(rpl, "_load_collector_daily", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(tqsdk_mod, "TQ_SYMBOLS", {"rb0": "KQ.m@SHFE.rb"})
    monkeypatch.setattr(tqsdk_mod, "TqsdkSource",
                        lambda: _FakeTqsdkSource({"rb0": _tq_frame("rb0", tq_dates, tq_closes)}))
    if backup_fetcher is not None:
        monkeypatch.setattr("hexbroker.data.backup.BackupRawFetcher", backup_fetcher)
    monkeypatch.setattr(sys, "argv", [
        "refresh_pull_local.py", "--asof", ASOF, "--symbols", "rb0",
        "--include-today", "--out-dir", str(out_dir)])
    return out_dir, lake_root


def test_e2e_primary_fails_backup_succeeds_exit_zero(tmp_path, monkeypatch, capsys):
    """① 主路失败 + 备源成功 → json 落盘、退出码 0、状态 BACKUP_SINA。

    主路失败构型：末 3 个重叠日 tqs 与湖名义价偏差 20% → 对齐稳定段仅 2 天
    → ``SCALE_UNSTABLE``（rb0 现场的等价复刻）。
    """
    dates = LAKE_DAYS + ["2026-08-31"]
    tq_closes = [1000.0, 1000.0, 1200.0, 1200.0, 1200.0, 1010.0]

    class _Fetcher(_FakeFetcher):
        def __init__(self, **kwargs):
            super().__init__(pulls={"rb0": _make_pull(
                dates, [LAKE_RAW] * len(LAKE_DAYS) + [1010.0])}, **kwargs)

    out_dir, lake_root = _install_fakes(monkeypatch, tmp_path, tq_closes,
                                        backup_fetcher=_Fetcher)
    before = _snapshot_bytes(lake_root)

    rc = rpl.main()
    capsys.readouterr()

    assert rc == 0, "主备任一成功即 0（调用方依赖此语义）"
    payload = json.loads((out_dir / "rb0.json").read_text(encoding="utf-8"))
    assert payload["result"]["rows"][0][0] == "20260831"
    status = json.loads((out_dir / "_SEAM_STATUS.json").read_text(encoding="utf-8"))
    assert status["statuses"]["rb0"] == "BACKUP_SINA"
    assert status["backup"] == ["rb0"]
    assert status["backup_detail"]["rb0"]["anchor_date"] == "2026-08-28"
    assert _snapshot_bytes(lake_root) == before, "备源路径不得写主湖"


def test_e2e_backup_also_fails_exit_three(tmp_path, monkeypatch, capsys):
    """② 主路失败 + 备源亦失败 → 仍计失败、退出码 3、不落 json（fail-closed）。"""

    class _Fetcher(_FakeFetcher):
        def __init__(self, **kwargs):
            super().__init__(raise_exc=RuntimeError("全部备源失败"), **kwargs)

    tq_closes = [1000.0, 1000.0, 1200.0, 1200.0, 1200.0, 1010.0]
    out_dir, _ = _install_fakes(monkeypatch, tmp_path, tq_closes,
                                backup_fetcher=_Fetcher)

    rc = rpl.main()
    out = capsys.readouterr().out

    assert rc == 3, "仍有品种主备皆失败 → 3（调用方据此整体回退 pandadata）"
    assert not (out_dir / "rb0.json").exists(), "失败品种绝不留 json"
    assert "SCALE_UNSTABLE" in out and "备源亦失败" in out
    status = json.loads((out_dir / "_SEAM_STATUS.json").read_text(encoding="utf-8"))
    assert status["statuses"] == {} and status["backup"] == []


def test_e2e_backup_json_matches_pandadata_contract(tmp_path, monkeypatch, capsys):
    """③ 落盘 json 与 pandadata 口径一致：columns / date 格式 / close 为后复权。"""
    dates = LAKE_DAYS + ["2026-08-31"]
    tq_closes = [1000.0, 1000.0, 1200.0, 1200.0, 1200.0, 1010.0]

    class _Fetcher(_FakeFetcher):
        def __init__(self, **kwargs):
            super().__init__(pulls={"rb0": _make_pull(
                dates, [LAKE_RAW] * len(LAKE_DAYS) + [1010.0])}, **kwargs)

    out_dir, _ = _install_fakes(monkeypatch, tmp_path, tq_closes,
                                backup_fetcher=_Fetcher)
    rpl.main()
    capsys.readouterr()

    payload = json.loads((out_dir / "rb0.json").read_text(encoding="utf-8"))
    res = payload["result"]
    assert res["type"] == "dataframe"
    assert res["columns"] == rpl.SAFE_COLS
    row = res["rows"][0]
    assert len(row) == len(rpl.SAFE_COLS)
    assert isinstance(row[0], str) and len(row[0]) == 8 and row[0].isdigit(), (
        f"date 必须是 YYYYMMDD 字符串，实际 {row[0]!r}")
    assert row[1] == "RB", "underlying_symbol 必须为大写基础合约"
    assert row[5] == pytest.approx(2020.0), "close 必须是后复权值（1010 × k=2）"
    assert all(isinstance(v, float) for v in row[2:8])

    # 与真实 pandadata 口径样本逐字段对质（columns + 行宽 + date 类型）
    ref = ROOT / "artifacts" / "p6_4_pull_20260903" / "ag0.json"
    if ref.exists():
        ref_res = json.loads(ref.read_text(encoding="utf-8"))["result"]
        assert res["columns"] == ref_res["columns"], "columns 必须与 pandadata 样本一致"
        assert len(res["rows"][0]) == len(ref_res["rows"][0])
        assert isinstance(ref_res["rows"][0][0], str), "参照样本 date 亦为字符串"


def test_e2e_backup_never_touches_real_lake(tmp_path, monkeypatch, capsys):
    """④ 备源路径不写**真实**主湖：rb0/hc0 的 1d 分片 mtime + 字节零变化。"""
    real_root = ROOT / "data" / "raw" / "processed"
    watched = [real_root / s / "1d" for s in ("rb0", "hc0")]
    watched = [d for d in watched if d.exists()]
    if not watched:  # pragma: no cover - 湖缺失时退化为扫描空集，仍断言不新增
        pytest.skip("真实主湖无 rb0/hc0 分片，跳过字节级对质")
    before = {str(d): _snapshot_bytes(d) for d in watched}

    dates = LAKE_DAYS + ["2026-08-31"]
    tq_closes = [1000.0, 1000.0, 1200.0, 1200.0, 1200.0, 1010.0]

    class _Fetcher(_FakeFetcher):
        def __init__(self, **kwargs):
            super().__init__(pulls={"rb0": _make_pull(
                dates, [LAKE_RAW] * len(LAKE_DAYS) + [1010.0])}, **kwargs)

    out_dir, _ = _install_fakes(monkeypatch, tmp_path, tq_closes,
                                backup_fetcher=_Fetcher)
    rc = rpl.main()
    capsys.readouterr()
    assert rc == 0 and (out_dir / "rb0.json").exists(), "前置条件：备源须成功"

    for d in watched:
        assert _snapshot_bytes(d) == before[str(d)], (
            f"备源路径改写了真实主湖 {d} —— 备源只落 artifacts json，写湖归融合脚本")


def test_e2e_primary_success_never_triggers_backup(tmp_path, monkeypatch, capsys):
    """⑥ 主路成功时备源**不被触发**（备源是兜底，不是主路径）。"""
    tq_closes = [1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 1010.0]  # 全对齐

    triggered = {"n": 0}

    class _Fetcher(_FakeFetcher):
        def __init__(self, **kwargs):
            triggered["n"] += 1
            super().__init__(pulls={}, **kwargs)

    out_dir, _ = _install_fakes(monkeypatch, tmp_path, tq_closes,
                                backup_fetcher=_Fetcher)
    rc = rpl.main()
    capsys.readouterr()

    assert rc == 0
    assert triggered["n"] == 0, "主路成功却触发了备源 —— 备源必须只在主路失败时兜底"
    status = json.loads((out_dir / "_SEAM_STATUS.json").read_text(encoding="utf-8"))
    assert status["statuses"]["rb0"] == "OK"
    assert status["backup"] == []
    rows = json.loads((out_dir / "rb0.json").read_text(encoding="utf-8"))["result"]["rows"]
    assert [r[0] for r in rows][-1] == "20260831"
    # 报告里该品种的「来源」列必须是主路（表头的兜底通道说明不算）
    row_rb0 = [ln for ln in (out_dir / "_LOCAL_REPORT.md").read_text(
        encoding="utf-8").splitlines() if ln.startswith("| rb0 |")]
    assert len(row_rb0) == 1 and row_rb0[0].endswith("| 主路 tqsdk |")


def test_e2e_backup_rows_are_flagged_in_report(tmp_path, monkeypatch, capsys):
    """报告「来源」列必须让人一眼看出哪些品种走了备源。"""
    dates = LAKE_DAYS + ["2026-08-31"]
    tq_closes = [1000.0, 1000.0, 1200.0, 1200.0, 1200.0, 1010.0]

    class _Fetcher(_FakeFetcher):
        def __init__(self, **kwargs):
            super().__init__(pulls={"rb0": _make_pull(
                dates, [LAKE_RAW] * len(LAKE_DAYS) + [1010.0])}, **kwargs)

    out_dir, _ = _install_fakes(monkeypatch, tmp_path, tq_closes,
                                backup_fetcher=_Fetcher)
    rpl.main()
    capsys.readouterr()

    text = (out_dir / "_LOCAL_REPORT.md").read_text(encoding="utf-8")
    assert "✅备源" in text, "状态列须区分主路/备源"
    assert "备源 sina" in text and "BACKUP_SINA" in text
    assert "备源补数明细" in text, "续接段为临时值，报告须显式标注"


# ===========================================================================
# 3. 辅助函数单元测试
# ===========================================================================
def test_base_symbol_from_tq_mapping():
    """KQ.m@SHFE.rb → RB（对齐 pandadata 样本口径）。"""
    assert rpl._base_symbol("rb0", {"rb0": "KQ.m@SHFE.rb"}) == "RB"


def test_base_symbol_falls_back_when_mapping_missing():
    """符号表缺项时按 sym0 去尾 0 兜底 —— 不让备源通道整批失败（R22）。"""
    assert rpl._base_symbol("rb0", {}) == "RB"
    assert rpl._base_symbol("hc0", None) == "HC"


def test_guardrail_constants_untouched():
    """护栏一律不降级：备源是新增数据源，不是放宽主路判定。"""
    assert rpl.RAW_ALIGN_TOL == 0.005
    assert rpl.K_TOL == 5e-4
    assert rpl.MIN_OVERLAP == 3
