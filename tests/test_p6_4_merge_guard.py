"""P0-B 融合根因护栏回归锁（2026-09-03 cu0/ni0 复权污染事故）。

事故机理
--------
``refresh_pull_local.py`` 每次拉取都带完整 10 日窗口，融合阶段
``p6_4_fill_gaps.py`` 的 ``drop_duplicates(subset="datetime", keep="last")``
让新帧**整行**覆盖主湖既有行，把 tqsdk 换月窗口内带 seam 误差的后复权价写进
主湖（cu0 08-21 +0.2139%、08-24 +0.1853%；ni0 同构反向 −0.20%），导致同一
主力段内 ``k = adj_close / raw_close`` 出现 0.2% 级漂移。

护栏契约
--------
- 已存在日期：只许覆盖 ``open_interest``，禁止覆盖 OHLC/adj_close；
- 新日期：整行写入，全字段；
- **R22**：字段缺失 / 口径不可用 → 放弃护栏、退回既有 ``keep="last"`` 行为，
  **并在 note 里写明归因，绝不抛异常中断管线**（本文件含全部负例）。

全部用例用 ``tmp_path`` 自建数据，**不依赖本地真实产物 / 不联网**。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from scripts import p6_4_fill_gaps as fg
from scripts.p6_4_fill_gaps import (
    MERGE_GUARD_REQUIRED_COLUMNS,
    MERGE_OI_ONLY_COLUMNS,
    MERGE_PROTECTED_COLUMNS,
    SCHEMA_COLUMNS,
    coerce_schema,
    merge_year_frames,
    stage_parse,
)

# 事故真实数值（cu0 2026-08-21）：护栏必须挡住这一格
INCIDENT_OLD_CLOSE = 158682.55
INCIDENT_NEW_CLOSE = 159021.988958


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _frame(rows: list[dict]) -> pd.DataFrame:
    """按 SCHEMA_COLUMNS 构造分片帧（未强制 dtype，贴近调用方入参）。

    空帧必须显式带列名：``coerce_schema`` 对"完全没有列"的 DataFrame 无法
    补全（``stage_parse`` 里也是先建 ``columns=SCHEMA_COLUMNS`` 再 coerce）。
    """
    if not rows:
        return coerce_schema(pd.DataFrame(columns=SCHEMA_COLUMNS))
    return coerce_schema(pd.DataFrame(rows))


def _row(day: str, *, close: float = 100.0, oi: float = 1000.0,
         raw: float = 50.0, adj: float | None = None, sym: str = "cu0") -> dict:
    return {
        "symbol": sym,
        "datetime": pd.Timestamp(day),
        "open": close - 1.0,
        "high": close + 1.0,
        "low": close - 2.0,
        "close": close,
        "volume": 500.0,
        "amount": 1.0e6,
        "open_interest": oi,
        "raw_close": raw,
        "adj_close": close if adj is None else adj,
        "limit_up": False,
        "limit_down": False,
        "is_rollover": False,
    }


def _idx(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["datetime"] = pd.to_datetime(out["datetime"]).dt.normalize()
    return out.set_index("datetime")


# --------------------------------------------------------------------------- #
# 常量契约
# --------------------------------------------------------------------------- #
def test_guard_column_contract():
    """护栏字段契约：持仓量可覆盖，价格/复权列受保护，raw_close 不在其中。"""
    assert MERGE_OI_ONLY_COLUMNS == ("open_interest",)
    assert set(MERGE_PROTECTED_COLUMNS) == {
        "open", "high", "low", "close", "adj_close",
    }
    assert set(MERGE_GUARD_REQUIRED_COLUMNS) == (
        set(MERGE_PROTECTED_COLUMNS) | set(MERGE_OI_ONLY_COLUMNS) | {"datetime"}
    )
    # raw_close 是名义价，既不允许被新帧覆盖，也不是护栏的"可覆盖"字段
    assert "raw_close" in SCHEMA_COLUMNS


# --------------------------------------------------------------------------- #
# 正例：护栏生效
# --------------------------------------------------------------------------- #
def test_guard_keeps_existing_ohlc_and_overwrites_open_interest():
    """已存在日期：OHLC/adj_close 原样保留，open_interest 被新值覆盖。

    同时锚定 P2-4 方案 A 授权的持仓量修正能力**不能**被护栏误伤。
    """
    old = _frame([_row("2026-08-21", close=158682.55, oi=162899.0),
                  _row("2026-08-24", close=159258.12, oi=150334.0)])
    new = _frame([_row("2026-08-21", close=159021.988958, oi=150334.0),
                  _row("2026-08-24", close=159553.292123, oi=138087.0)])

    merged, note = merge_year_frames(old, new)

    m = _idx(merged)
    # 价格列：护栏挡住，保持旧值
    assert float(m.loc["2026-08-21", "close"]) == 158682.55
    assert float(m.loc["2026-08-21", "adj_close"]) == 158682.55
    assert float(m.loc["2026-08-24", "close"]) == 159258.12
    assert float(m.loc["2026-08-24", "open"]) == 159258.12 - 1.0
    # 持仓量：允许覆盖（新值）
    assert float(m.loc["2026-08-21", "open_interest"]) == 150334.0
    assert float(m.loc["2026-08-24", "open_interest"]) == 138087.0
    # 名义价不属于受保护列也不属于可覆盖列：保持旧值
    assert float(m.loc["2026-08-21", "raw_close"]) == 50.0
    assert "护栏生效" in note


def test_guard_writes_new_dates_with_all_fields():
    """新日期：整行采用新帧，全部字段写入（含 OHLC/adj_close）。"""
    old = _frame([_row("2026-08-21", close=158682.55, oi=162899.0)])
    new = _frame([_row("2026-08-21", close=159021.988958, oi=150334.0),
                  _row("2026-09-02", close=161000.0, oi=224406.0),
                  _row("2026-09-03", close=161500.0, oi=225000.0)])

    merged, note = merge_year_frames(old, new)
    m = _idx(merged)

    assert len(merged) == 3
    assert float(m.loc["2026-09-02", "close"]) == 161000.0
    assert float(m.loc["2026-09-02", "adj_close"]) == 161000.0
    assert float(m.loc["2026-09-02", "open_interest"]) == 224406.0
    assert float(m.loc["2026-09-03", "close"]) == 161500.0
    assert "新增 2 个日期" in note


def test_guard_preserves_schema_order_dtype_and_sorting():
    """输出必须是 coerce_schema 后的列序、float dtype，且按日期升序。"""
    old = _frame([_row("2026-08-24", close=159258.12)])
    new = _frame([_row("2026-08-21", close=159021.988958),
                  _row("2026-08-24", close=159553.292123)])

    merged, _ = merge_year_frames(old, new)

    assert list(merged.columns) == SCHEMA_COLUMNS
    for col in ("open", "high", "low", "close", "adj_close", "open_interest"):
        assert merged[col].dtype.kind == "f", f"{col} dtype={merged[col].dtype}"
    days = list(pd.to_datetime(merged["datetime"]).dt.strftime("%Y-%m-%d"))
    assert days == sorted(days)
    assert days == ["2026-08-21", "2026-08-24"]


def test_guard_blocks_the_actual_cu0_incident_cell():
    """事故回归锁：cu0 2026-08-21 close 不得被新帧的 159021.99 覆盖。"""
    old = _frame([_row("2026-08-21", close=INCIDENT_OLD_CLOSE, raw=107520.0)])
    new = _frame([_row("2026-08-21", close=INCIDENT_NEW_CLOSE, raw=107520.0)])

    merged, _ = merge_year_frames(old, new)
    m = _idx(merged)

    assert float(m.loc["2026-08-21", "close"]) == INCIDENT_OLD_CLOSE
    # 段内 k 必须自洽：adj_close / raw_close 保持旧段的 1.4758… 而非 1.4790…
    k = float(m.loc["2026-08-21", "adj_close"]) / float(m.loc["2026-08-21", "raw_close"])
    assert abs(k - INCIDENT_OLD_CLOSE / 107520.0) < 1e-12


# --------------------------------------------------------------------------- #
# 负例：R22 回退（不得抛异常，必须退回既有 keep="last" 行为）
# --------------------------------------------------------------------------- #
def _assert_legacy(merged: pd.DataFrame, note: str, expect_rows: int) -> None:
    """回退判据：结果等于既有 keep=last 行为，且 note 必须写明归因。"""
    # 空帧走"无需生效"分支，其余走"未生效→退回"分支；两者都不得静默
    assert ("护栏未生效" in note) or ("护栏无需生效" in note), note
    if "护栏未生效" in note:
        assert "退回既有 keep=last 行为" in note
    assert len(merged) == expect_rows


def test_guard_empty_old_falls_back_without_exception():
    old = _frame([])
    new = _frame([_row("2026-08-21")])
    merged, note = merge_year_frames(old, new)
    _assert_legacy(merged, note, 1)
    assert "无需生效" in note


def test_guard_empty_new_falls_back_without_exception():
    old = _frame([_row("2026-08-21")])
    new = _frame([])
    merged, note = merge_year_frames(old, new)
    _assert_legacy(merged, note, 1)
    assert "无需生效" in note


def test_guard_missing_required_column_falls_back():
    """缺 open_interest → 口径不可用 → 回退，不抛异常。"""
    old = _frame([_row("2026-08-21", close=158682.55)])
    new = _frame([_row("2026-08-21", close=159021.988958)])
    new = new.drop(columns=["open_interest"])

    merged, note = merge_year_frames(old, new)

    _assert_legacy(merged, note, 1)
    assert "open_interest" in note
    # 既有行为：新帧整行覆盖
    assert float(_idx(merged).loc["2026-08-21", "close"]) == 159021.988958


def test_guard_missing_protected_column_falls_back():
    """缺 close → 口径不可用 → 回退。"""
    old = _frame([_row("2026-08-21", close=158682.55)])
    new = _frame([_row("2026-08-21", close=159021.988958)]).drop(columns=["close"])

    merged, note = merge_year_frames(old, new)
    _assert_legacy(merged, note, 1)
    assert "close" in note


def test_guard_duplicate_datetime_in_old_falls_back():
    """旧帧 datetime 重复 → 无法唯一定位被保护行 → 回退。"""
    old = _frame([_row("2026-08-21", close=158682.55),
                  _row("2026-08-21", close=158000.0)])
    new = _frame([_row("2026-08-21", close=159021.988958)])

    merged, note = merge_year_frames(old, new)
    _assert_legacy(merged, note, 1)
    assert "旧帧 datetime 有重复" in note


def test_guard_duplicate_datetime_in_new_falls_back():
    """新帧 datetime 重复 → 无法唯一定位来源行 → 回退。"""
    old = _frame([_row("2026-08-21", close=158682.55)])
    new = _frame([_row("2026-08-21", close=159021.988958),
                  _row("2026-08-21", close=160000.0)])

    merged, note = merge_year_frames(old, new)
    _assert_legacy(merged, note, 1)
    assert "新帧 datetime 有重复" in note


def test_guard_unparsable_open_interest_falls_back():
    """新帧 open_interest 全不可解析 → 回退（原始帧，未经 coerce_schema）。"""
    old = _frame([_row("2026-08-21", close=158682.55)])
    new = _frame([_row("2026-08-21", close=159021.988958)])
    new["open_interest"] = float("nan")

    merged, note = merge_year_frames(old, new)
    _assert_legacy(merged, note, 1)
    assert "open_interest 全不可解析" in note


def test_guard_unparsable_protected_columns_falls_back():
    """旧帧保护列全不可解析 → 回退（原始帧，未经 coerce_schema）。"""
    old = _frame([_row("2026-08-21", close=158682.55)])
    for col in MERGE_PROTECTED_COLUMNS:
        old[col] = float("nan")
    new = _frame([_row("2026-08-21", close=159021.988958)])

    merged, note = merge_year_frames(old, new)
    _assert_legacy(merged, note, 1)
    assert "保护列" in note


def test_guard_nat_datetime_in_old_falls_back():
    """旧帧 datetime 含 NaT → 无法定位被保护行 → 回退（不抛异常）。

    上游 ``normalize_new_df`` 用 ``errors="coerce"``，坏日期会变成 NaT 而非
    异常，所以 NaT 才是"日期口径不可用"的真实形态。
    """
    old = _frame([_row("2026-08-21", close=158682.55)])
    old.loc[0, "datetime"] = pd.NaT
    new = _frame([_row("2026-08-21", close=159021.988958)])

    merged, note = merge_year_frames(old, new)
    _assert_legacy(merged, note, 2)
    assert "旧帧 datetime 含 NaT" in note


def test_guard_nat_datetime_in_new_falls_back():
    """新帧 datetime 含 NaT → 无法定位来源行 → 回退。"""
    old = _frame([_row("2026-08-21", close=158682.55)])
    new = _frame([_row("2026-08-21", close=159021.988958)])
    new.loc[0, "datetime"] = pd.NaT

    merged, note = merge_year_frames(old, new)
    _assert_legacy(merged, note, 2)
    assert "新帧 datetime 含 NaT" in note


@pytest.mark.parametrize("bad_new", [
    pytest.param({"open_interest": float("nan")}, id="nan-oi"),
    pytest.param({"close": float("nan")}, id="nan-close"),
])
def test_guard_negative_cases_never_raise(bad_new: dict):
    """负例总闸：任何脏输入都不允许抛异常（R22：不新增停摆失效模式）。"""
    old = _frame([_row("2026-08-21", close=158682.55)])
    new = _frame([_row("2026-08-21", close=159021.988958)])
    for col, val in bad_new.items():
        new[col] = val

    merged, note = merge_year_frames(old, new)  # 不得抛异常
    assert isinstance(merged, pd.DataFrame)
    assert isinstance(note, str) and note


# --------------------------------------------------------------------------- #
# 显式放行（取证后的修复通道）
# --------------------------------------------------------------------------- #
def test_allow_price_overwrite_restores_legacy_behavior():
    """``--allow-price-overwrite`` 才允许覆盖价格列，且 note 必须大声留痕。"""
    old = _frame([_row("2026-08-21", close=158682.55)])
    new = _frame([_row("2026-08-21", close=159021.988958)])

    merged, note = merge_year_frames(old, new, allow_price_overwrite=True)

    assert float(_idx(merged).loc["2026-08-21", "close"]) == 159021.988958
    assert "护栏已显式关闭" in note
    assert "keep=last" in note


def test_guard_is_on_by_default():
    """默认必须开启护栏：不传 allow_price_overwrite 时价格列不得被覆盖。"""
    old = _frame([_row("2026-08-21", close=158682.55)])
    new = _frame([_row("2026-08-21", close=159021.988958)])
    merged, _ = merge_year_frames(old, new)
    assert float(_idx(merged).loc["2026-08-21", "close"]) == 158682.55


# --------------------------------------------------------------------------- #
# stage_parse 端到端（沙箱目录，绝不碰真实主湖）
# --------------------------------------------------------------------------- #
def _write_persisted(tmp_path: Path, rows: list[list]) -> Path:
    p = tmp_path / "cu0.json"
    p.write_text(
        json.dumps({
            "result": {
                "type": "dataframe",
                "columns": ["date", "underlying_symbol", "open", "high", "low",
                            "close", "volume", "open_interest"],
                "rows": rows,
            }
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return p


@pytest.fixture()
def sandbox(monkeypatch, tmp_path):
    """把 p6_4_fill_gaps 的所有落盘目标重定向到 tmp_path。"""
    proc = tmp_path / "processed"
    art = tmp_path / "artifacts"
    (proc / "cu0" / "1d").mkdir(parents=True, exist_ok=True)
    art.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fg, "PROCESSED_DIR", proc)
    monkeypatch.setattr(fg, "ARTIFACTS_DIR", art)
    monkeypatch.setattr(fg, "PLAN_PATH", art / "p6_4_pull_plan.json")
    monkeypatch.setattr(fg, "APPLIED_PATH", art / "p6_4_applied.json")
    monkeypatch.setattr(fg, "BACKUP_DIR", art / "backup_p64")
    return proc


def test_stage_parse_e2e_guard_protects_existing_ohlc(sandbox):
    """端到端：融合写盘后，既有日期 OHLC 不变、OI 更新、新日期整行写入。"""
    lake_path = sandbox / "cu0" / "1d" / "2026.parquet"
    _frame([_row("2026-08-21", close=158682.55, oi=162899.0, raw=107520.0),
            _row("2026-08-24", close=159258.12, oi=150334.0, raw=107910.0)]
           ).to_parquet(lake_path, index=False)

    persisted = _write_persisted(
        sandbox.parent,
        [
            ["20260821", "CU", 157442.0, 159243.0, 157250.0, 159021.988958, 500.0, 150334.0],
            ["20260824", "CU", 159627.0, 159804.0, 158771.0, 159553.292123, 500.0, 138087.0],
            ["20260902", "CU", 160900.0, 161300.0, 160800.0, 161000.0, 500.0, 224406.0],
        ],
    )

    rc = stage_parse(str(persisted), "CU", "20260821_20260902",
                     force=True, dry_run=False, skip_nominal=True, scale=1.0)
    assert rc == 0

    out = _idx(pd.read_parquet(lake_path))
    # 既有日期：价格守住、OI 更新
    assert float(out.loc["2026-08-21", "close"]) == 158682.55
    assert float(out.loc["2026-08-21", "adj_close"]) == 158682.55
    assert float(out.loc["2026-08-21", "open_interest"]) == 150334.0
    assert float(out.loc["2026-08-24", "close"]) == 159258.12
    assert float(out.loc["2026-08-24", "open_interest"]) == 138087.0
    # 新日期：整行写入
    assert float(out.loc["2026-09-02", "close"]) == 161000.0
    assert float(out.loc["2026-09-02", "open_interest"]) == 224406.0
    # applied 记录里必须留下护栏结论，供事后审计
    applied = json.loads((sandbox.parent / "artifacts" / "p6_4_applied.json").read_text(
        encoding="utf-8"))
    assert any("护栏生效" in str(r.get("merge_guard", "")) for r in applied)


def test_stage_parse_e2e_allow_price_overwrite_writes_prices(sandbox):
    """端到端：显式放行后价格列可被覆盖（取证后修复通道，日常严禁）。"""
    lake_path = sandbox / "cu0" / "1d" / "2026.parquet"
    _frame([_row("2026-08-21", close=158682.55, oi=162899.0, raw=107520.0)]
           ).to_parquet(lake_path, index=False)

    persisted = _write_persisted(
        sandbox.parent,
        [["20260821", "CU", 157442.0, 159243.0, 157250.0, 159021.988958, 500.0, 150334.0]],
    )

    rc = stage_parse(str(persisted), "CU", "20260821_20260821",
                     force=True, dry_run=False, skip_nominal=True, scale=1.0,
                     allow_price_overwrite=True)
    assert rc == 0

    out = _idx(pd.read_parquet(lake_path))
    assert float(out.loc["2026-08-21", "close"]) == 159021.988958


def test_stage_parse_writes_atomically_and_leaves_no_tmp(sandbox):
    """G5：写盘用 tmp + replace，目录下不得残留 .parquet.tmp。"""
    lake_path = sandbox / "cu0" / "1d" / "2026.parquet"
    _frame([_row("2026-08-21", close=158682.55)]
           ).to_parquet(lake_path, index=False)

    persisted = _write_persisted(
        sandbox.parent,
        [["20260902", "CU", 160900.0, 161300.0, 160800.0, 161000.0, 500.0, 224406.0]],
    )
    assert stage_parse(str(persisted), "CU", "20260902_20260902",
                       force=True, dry_run=False, skip_nominal=True,
                       scale=1.0) == 0

    leftovers = list((sandbox / "cu0" / "1d").glob("*.tmp"))
    assert leftovers == [], f"残留临时文件: {leftovers}"
    # 写前备份仍在（护栏不削弱既有备份能力）
    assert list((sandbox.parent / "artifacts" / "backup_p64").glob("*.parquet"))


# --------------------------------------------------------------------------- #
# 红线：本次改动不得引入删除调用
# --------------------------------------------------------------------------- #
def test_rollback_and_guard_scripts_have_no_delete_calls():
    """AST 扫描：护栏与回滚脚本不得出现 unlink/remove/rmtree/move。"""
    import ast

    banned = {"unlink", "remove", "rmtree", "move"}
    for rel in ("scripts/p6_4_fill_gaps.py", "scripts/rollback_adj_cells.py"):
        path = Path(fg.PROJECT_ROOT) / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (
                    fn.id if isinstance(fn, ast.Name) else "")
                if name in banned:
                    hits.append(f"L{node.lineno}:{name}")
        assert hits == [], f"{rel} 出现删除/移动调用: {hits}"
