"""Tier-2 备源通道 **fail-closed 反例契约**（QA 严过关 fresh-eyes，2026-09-03）。

与 ``test_refresh_pull_local_backup.py`` 的关系
----------------------------------------------
后者是**实现者**提交的正向用例；本文件是**独立复核者**提交的反例用例，只做一件事：
证明备源通道的每一条失败分支都**真的拒绝**，而不是静默通过。

取证方法：每个用例都先在**修复前**的代码上跑通（ACCEPTED / 写出脏值），修复后改为
REJECTED。取证脚本见 ``artifacts/_tmp/qa_yan/probe_yan.py`` 与
``probe_yan_rollover.py``。

本文件锁定 4 类缺口
--------------------
1. 🔴 **锚点退档 → 幽灵台阶**：``graft`` 的 ``t0 = common.max()`` 只是「两源重叠的
   最新日」，不是「湖的最末日」。备源在湖末日有缺口时，t0 退档；若换月恰好发生在
   湖末日，k0 取到**旧段**复权比 → 新 bar 整段偏移（rb0 量级实测 +1.72%），
   且 n_align / anchor_gap_days / 接缝名义价三道护栏**全部接不住**。
2. 🔴 **NaN OHLC → 零价 bar**：``BACKUP_NO_OHLC`` 只查列是否存在，查不出值为 NaN。
   融合端 ``coerce_schema`` 的 ``fillna(0.0)`` 会把 NaN 落成 0.0 —— 即实现者自己在
   ``RawPull.frame`` 注释里定为红线的「open/high/low=0 零价 bar」，换个入口照样进。
3. 🔴 **0 值 OHLC → 零价 bar**：sina/akshare 缺字段的另一种常见编码是 ``0.0``，
   同样绕过「列存在」检查直落主湖。
4. 🟡 **包络破坏**：同 k_d 缩放（k_d>0）本身保序，但保不住备源自带的脏数据。
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

from hexbroker.data.backup import RawPull  # noqa: E402
from hexbroker.data.sources import tqsdk_source as tqsdk_mod  # noqa: E402

WINDOW_START = "2026-08-18"
END = "2026-09-02"
JUMP_LIM = rpl.PRICE_JUMP_DEFAULT

K_OLD = 1.201347      # 换月前复权比
K_NEW = 1.181055      # 换月后复权比（rb0 2026-08-31 实测）


# ===========================================================================
# 夹具
# ===========================================================================
def _write_lake(root: Path, sym0: str, rows) -> None:
    """rows = [(date, raw_close, k)]，close = raw_close × k。"""
    day = root / sym0 / "1d"
    day.mkdir(parents=True, exist_ok=True)
    n = len(rows)
    dates = [r[0] for r in rows]
    raws = [float(r[1]) for r in rows]
    closes = [float(r[1]) * r[2] for r in rows]
    pd.DataFrame({
        "symbol": [sym0] * n,
        "datetime": pd.to_datetime(dates),
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": 10_000.0, "amount": 0.0, "open_interest": 50_000.0,
        "raw_close": raws, "adj_close": closes,
        "limit_up": False, "limit_down": False, "is_rollover": False,
    }).to_parquet(day / "2026.parquet", index=False)


def _make_pull(dates, raws, *, frame_override=None, source="sina"):
    idx = pd.DatetimeIndex(pd.to_datetime(dates), name="datetime")
    n = len(idx)
    frame = pd.DataFrame({
        "open": [float(r) for r in raws],
        "high": [float(r) * 1.01 for r in raws],
        "low": [float(r) * 0.99 for r in raws],
        "close": [float(r) for r in raws],
        "volume": [12_000.0] * n,
        "open_interest": [60_000.0] * n,
    }, index=idx)
    if frame_override is not None:
        frame = frame_override(frame)
    return RawPull(
        symbol="rb0",
        close=pd.Series([float(r) for r in raws], index=idx, dtype=float),
        source=source,
        open_interest=pd.Series([60_000.0] * n, index=idx, dtype=float),
        volume=pd.Series([12_000.0] * n, index=idx, dtype=float),
        frame=frame,
    )


class _Fetcher:
    def __init__(self, pull):
        self.pull = pull

    def fetch_raw(self, symbols, start, end):
        return {"rb0": self.pull}


def _pull(sym0="rb0", base="RB", pull=None, end_eff=END, monkeypatch=None, lake=None):
    """装配并调用 ``_backup_pull_symbol``。"""
    if monkeypatch is not None and lake is not None:
        monkeypatch.setattr(rpl, "PROCESSED_DIR", lake)
    return rpl._backup_pull_symbol(
        sym0, base, pd.Timestamp(WINDOW_START), pd.Timestamp(end_eff),
        JUMP_LIM, fetcher=_Fetcher(pull))


# ===========================================================================
# 1. 🔴 锚点退档 → 幽灵台阶（本轮最严重缺口）
# ===========================================================================
def test_qa_anchor_must_be_lake_latest_not_merely_overlapping(tmp_path, monkeypatch):
    """备源在**湖末日**有缺口 → 锚点退档 → 必须拒绝（否则取到旧段 k）。

    现场复刻：湖 08-24~08-31 为旧段 k=1.201347，09-01 换月切新段 k=1.181055；
    备源覆盖 08-24~08-31 与 09-02，**独缺 09-01**。

    修复前：三道护栏全绿（n_align=6 ≥ MIN_OVERLAP=3；anchor_gap_days=2 ≤ 7；
    接缝名义价偏差 0.53% 未超限幅；graft 零告警，因为重叠区全在旧段、比值恒定），
    于是用 k=1.201347 外推 09-02 → 相对正确值 **+1.72%** 的幽灵台阶，
    且报告里 ``warnings`` 为空 —— 完全静默。
    """
    monkeypatch.setattr(rpl, "PROCESSED_DIR", tmp_path)
    _write_lake(tmp_path, "rb0", [
        ("2026-08-24", 3050.0, K_OLD), ("2026-08-25", 3060.0, K_OLD),
        ("2026-08-26", 3070.0, K_OLD), ("2026-08-27", 3080.0, K_OLD),
        ("2026-08-28", 3112.0, K_OLD), ("2026-08-31", 3197.0, K_OLD),
        ("2026-09-01", 3174.0, K_NEW),          # ← 换月发生在湖末日
    ])
    bk_dates = ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27",
                "2026-08-28", "2026-08-31", "2026-09-02"]   # 缺 09-01
    bk_raws = [3050.0, 3060.0, 3070.0, 3080.0, 3112.0, 3197.0, 3180.0]

    with pytest.raises(ValueError, match="BACKUP_ANCHOR_NOT_LATEST") as ei:
        _pull(pull=_make_pull(bk_dates, bk_raws))
    assert "2026-09-01" in str(ei.value), "错误须点名湖末日，便于归因"
    assert "2026-08-31" in str(ei.value), "错误须点名退档后的锚点日"


def test_qa_anchor_equals_lake_last_on_happy_path(tmp_path, monkeypatch):
    """反向锁定：备源覆盖湖末日时，新护栏**不得**误杀（防过度收紧）。"""
    monkeypatch.setattr(rpl, "PROCESSED_DIR", tmp_path)
    _write_lake(tmp_path, "rb0", [
        ("2026-08-26", 3070.0, K_OLD), ("2026-08-27", 3080.0, K_OLD),
        ("2026-08-28", 3112.0, K_OLD), ("2026-08-31", 3197.0, K_NEW),
        ("2026-09-01", 3174.0, K_NEW),
    ])
    bk_dates = ["2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31",
                "2026-09-01", "2026-09-02"]
    bk_raws = [3070.0, 3080.0, 3112.0, 3197.0, 3174.0, 3180.0]

    rows, info = _pull(pull=_make_pull(bk_dates, bk_raws))
    assert [r[0] for r in rows] == ["20260902"], "应只发射新增日"
    assert info["anchor_date"] == "2026-09-01", "锚点必须是湖末日"
    assert info["k"] == pytest.approx(K_NEW), "k 必须取自换月后的新段"
    # 后复权 close = 名义价 3180 × 新段 k
    assert rows[0][5] == pytest.approx(3180.0 * K_NEW)
    # 反证：不是旧段 k（两者相差 1.72%，断言不会被浮点容差吃掉）
    assert rows[0][5] != pytest.approx(3180.0 * K_OLD)


# ===========================================================================
# 2. 🔴 NaN / 0 值 OHLC → 零价 bar
# ===========================================================================
def _flat_lake(tmp_path, monkeypatch, last="2026-08-28"):
    """5 天平盘湖（k=2.0），返回备源日期/名义价。"""
    days = ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]
    monkeypatch.setattr(rpl, "PROCESSED_DIR", tmp_path)
    _write_lake(tmp_path, "rb0", [(d, 1000.0, 2.0) for d in days])
    return days


@pytest.mark.parametrize("col", ["open", "high", "low"])
def test_qa_nan_ohlc_is_rejected(tmp_path, monkeypatch, col):
    """备源以 NaN 编码缺失 OHLC → 拒绝。

    修复前：ACCEPTED 且写出 ``open/high/low=nan``；``json.dumps`` 默认
    ``allow_nan=True`` → 产出非标准字面量 ``NaN``，严格 JSON 解析器直接拒收；
    Python 解析器接受后，融合端 ``coerce_schema`` 的 ``fillna(0.0)`` 把它填成
    **0.0 零价 bar** —— 正是实现者声明要封堵的红线。
    """
    days = _flat_lake(tmp_path, monkeypatch)
    dates = days + ["2026-08-31"]
    raws = [1000.0] * 5 + [1010.0]

    def inject(frame):
        frame.loc[pd.Timestamp("2026-08-31"), col] = float("nan")
        return frame

    with pytest.raises(ValueError, match="BACKUP_BAD_OHLC") as ei:
        _pull(pull=_make_pull(dates, raws, frame_override=inject),
              end_eff="2026-08-31")
    assert col in str(ei.value)


@pytest.mark.parametrize("col", ["open", "high", "low"])
def test_qa_zero_ohlc_is_rejected(tmp_path, monkeypatch, col):
    """备源以 0.0 编码缺失 OHLC → 拒绝（另一种常见编码，同样绕过缺列检查）。"""
    days = _flat_lake(tmp_path, monkeypatch)
    dates = days + ["2026-08-31"]
    raws = [1000.0] * 5 + [1010.0]

    def inject(frame):
        frame.loc[pd.Timestamp("2026-08-31"), col] = 0.0
        return frame

    with pytest.raises(ValueError, match="BACKUP_BAD_OHLC"):
        _pull(pull=_make_pull(dates, raws, frame_override=inject),
              end_eff="2026-08-31")


def test_qa_negative_ohlc_is_rejected(tmp_path, monkeypatch):
    """负价格 → 拒绝（脏数据，绝不落湖）。"""
    days = _flat_lake(tmp_path, monkeypatch)
    dates = days + ["2026-08-31"]
    raws = [1000.0] * 5 + [1010.0]

    def inject(frame):
        frame.loc[pd.Timestamp("2026-08-31"), "low"] = -5.0
        return frame

    with pytest.raises(ValueError, match="BACKUP_BAD_OHLC"):
        _pull(pull=_make_pull(dates, raws, frame_override=inject),
              end_eff="2026-08-31")


@pytest.mark.parametrize("col", ["volume", "open_interest"])
def test_qa_nan_volume_and_oi_are_rejected(tmp_path, monkeypatch, col):
    """volume / open_interest 为 NaN → 拒绝（NaN 会污染主湖下游口径）。"""
    days = _flat_lake(tmp_path, monkeypatch)
    dates = days + ["2026-08-31"]
    raws = [1000.0] * 5 + [1010.0]

    def inject(frame):
        frame.loc[pd.Timestamp("2026-08-31"), col] = float("nan")
        return frame

    with pytest.raises(ValueError, match="BACKUP_BAD_OHLC") as ei:
        _pull(pull=_make_pull(dates, raws, frame_override=inject),
              end_eff="2026-08-31")
    assert col in str(ei.value)


# ===========================================================================
# 3. 🟡 包络：脏数据破坏 / 正常数据保持
# ===========================================================================
def test_qa_envelope_break_from_dirty_backup_is_rejected(tmp_path, monkeypatch):
    """备源自带 low > high 的脏 bar → 拒绝（同 k 缩放保序，但保不住脏数据）。"""
    days = _flat_lake(tmp_path, monkeypatch)
    dates = days + ["2026-08-31"]
    raws = [1000.0] * 5 + [1010.0]

    def inject(frame):
        frame.loc[pd.Timestamp("2026-08-31"), "low"] = 5000.0   # low > high
        return frame

    with pytest.raises(ValueError, match="BACKUP_OHLC_INCOHERENT"):
        _pull(pull=_make_pull(dates, raws, frame_override=inject),
              end_eff="2026-08-31")


def test_qa_envelope_is_preserved_under_positive_k(tmp_path, monkeypatch):
    """正向契约：k_d > 0 时缩放必须保持 low ≤ open/close ≤ high。

    同时验证 k_d 由 graft 结果反解（close 逐位等于 graft 输出），而非手工乘 k。
    """
    days = _flat_lake(tmp_path, monkeypatch)
    dates = days + ["2026-08-31"]
    raws = [1000.0] * 5 + [1010.0]

    rows, info = _pull(pull=_make_pull(dates, raws), end_eff="2026-08-31")
    d = rows[0]
    o, h, l, c = d[2], d[3], d[4], d[5]
    assert l <= min(o, c) <= max(o, c) <= h, (
        f"包络被破坏：low={l} open={o} close={c} high={h}")
    # k 反解自证：close/名义价 == graft 的 anchor_ratio
    assert c / 1010.0 == pytest.approx(info["k"])
    assert info["k"] == pytest.approx(2.0)


# ===========================================================================
# 4. 其余 fail-closed 分支补覆盖
# ===========================================================================
def test_qa_align_insufficient_is_rejected(tmp_path, monkeypatch):
    """重叠样本 < MIN_OVERLAP → 拒绝。

    ⚠️ 这是实现者**唯一未写用例**的失败分支（``BACKUP_ALIGN_INSUFFICIENT``），
    本用例补上。
    """
    _flat_lake(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="BACKUP_ALIGN_INSUFFICIENT") as ei:
        _pull(pull=_make_pull(["2026-08-27", "2026-08-28", "2026-08-31"],
                              [1000.0, 1000.0, 1010.0]),
              end_eff="2026-08-31")
    assert str(rpl.MIN_OVERLAP) in str(ei.value)


def test_qa_future_dates_are_never_emitted(tmp_path, monkeypatch):
    """备源含超出发射上界的未来日期 → 只发射窗口内新增日，绝不发射未来 bar。"""
    days = _flat_lake(tmp_path, monkeypatch)
    rows, _ = _pull(
        pull=_make_pull(days + ["2026-08-31", "2026-09-05", "2026-09-30"],
                        [1000.0] * 5 + [1010.0, 1015.0, 1020.0]),
        end_eff="2026-08-31")
    assert [r[0] for r in rows] == ["20260831"], (
        f"发射了未来/越界日期：{[r[0] for r in rows]}")


def test_qa_frame_missing_row_is_rejected(tmp_path, monkeypatch):
    """close 序列有某日、OHLC 帧缺该日行 → 拒绝（不降级成只写 close）。"""
    days = _flat_lake(tmp_path, monkeypatch)

    def drop(frame):
        return frame.drop(index=pd.Timestamp("2026-08-31"))

    with pytest.raises(ValueError, match="BACKUP_NO_OHLC"):
        _pull(pull=_make_pull(days + ["2026-08-31"], [1000.0] * 5 + [1010.0],
                              frame_override=drop),
              end_eff="2026-08-31")


# ===========================================================================
# 5. 纵深防御：允许 NaN 的 json 绝不出门
# ===========================================================================
def test_qa_nan_payload_never_reaches_disk(tmp_path, monkeypatch, capsys):
    """纵深防御：即便校验被绕过，含 NaN 的 payload 也**写不出** json。

    直接把 ``_backup_pull_symbol`` 打桩成返回 NaN 行，验证 ``main()`` 走
    ``allow_nan=False`` 的落盘路径会响亮失败（计该品种失败 → 退出码 3），
    而不是把含非标准 ``NaN`` 字面量的 json 写进落盘目录。
    """
    days = ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]
    lake_root = tmp_path / "lake"
    out_dir = tmp_path / "out"
    _write_lake(lake_root, "rb0", [(d, 1000.0, 2.0) for d in days])

    idx = pd.MultiIndex.from_product(
        [["rb0"], pd.DatetimeIndex(pd.to_datetime(days + ["2026-08-31"]))],
        names=["symbol", "datetime"])
    tq_closes = [1000.0, 1000.0, 1200.0, 1200.0, 1200.0, 1010.0]  # 逼主路失败
    tq_df = pd.DataFrame({
        "open": tq_closes,
        "high": [c * 1.01 for c in tq_closes],
        "low": [c * 0.99 for c in tq_closes],
        "close": tq_closes,
        "volume": [10_000.0] * len(tq_closes),
        "open_interest": [50_000.0] * len(tq_closes),
    }, index=idx)

    class _BarFrame:
        def __init__(self, df):
            self.df = df
            self.metadata = {"fetch_seconds": 0.01}

    class _FakeSource:
        def fetch_bars(self, symbols, start, end, freq="1d"):
            return _BarFrame(tq_df)

    monkeypatch.setattr(rpl, "PROCESSED_DIR", lake_root)
    monkeypatch.setattr(rpl, "_load_collector_daily", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(tqsdk_mod, "TQ_SYMBOLS", {"rb0": "KQ.m@SHFE.rb"})
    monkeypatch.setattr(tqsdk_mod, "TqsdkSource", _FakeSource)
    # 打桩：绕过全部取值校验，直接返回 NaN close
    monkeypatch.setattr(rpl, "_backup_pull_symbol", lambda *a, **k: (
        [["20260831", "RB", float("nan"), 1.0, 1.0, float("nan"), 1.0, 1.0]],
        {"source": "sina", "k": 2.0, "anchor_date": "2026-08-28",
         "anchor_gap_days": 3, "new_dates": ["20260831"], "n_align": 5,
         "warnings": [], "seam_status": "BACKUP_SINA"}))
    monkeypatch.setattr(sys, "argv", [
        "refresh_pull_local.py", "--asof", "2026-08-31", "--symbols", "rb0",
        "--include-today", "--out-dir", str(out_dir)])

    rc = rpl.main()
    capsys.readouterr()

    assert rc == 3, "含 NaN 的 payload 必须让该品种失败（退出码 3），绝不静默成功"
    if (out_dir / "rb0.json").exists():
        raw = (out_dir / "rb0.json").read_text(encoding="utf-8")
        assert "NaN" not in raw, f"落盘 json 含非标准 NaN 字面量：{raw[:200]}"


# ===========================================================================
# 6. 护栏零降级契约（复核者独立断言，不信任实现者的同类用例）
# ===========================================================================
def test_qa_guardrail_constants_are_frozen():
    """护栏值一字未动 —— 备源是新增数据源，不是放宽主路判定。"""
    assert rpl.RAW_ALIGN_TOL == 0.005
    assert rpl.K_TOL == 5e-4
    assert rpl.MIN_OVERLAP == 3
