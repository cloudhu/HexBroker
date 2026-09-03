"""P0-B 融合护栏——QA 变异测试暴露的三个缺口（2026-09-03）。

本文件不是"再写一遍已有测试"，而是**变异测试存活项的定向处置**。
三个存活变异（详见 ``deliverables/cu_ni_rollback_qa_review_20260903.md`` §A）：

- **M7（🟡，测试缺口 → 本文件补锁）**：删掉 ``print(f"[GUARD] ...")``，
  原 23 个用例**全绿**。→ 后门留痕只被 ``p6_4_applied.json`` 锁住，
  stdout 零覆盖。而 stdout 是值班同学**唯一实时可见**的通道。

- **M8（🟡，测试缺口 → 本文件补锁）**：把 ``old_prot.isna().all().all()``
  放宽成 ``.any().any()``，原 23 个用例**全绿**。→ 护栏"回退判据"的**边界**
  没有锁。而"回退"= 退回 ``keep="last"`` = **回到 cu0/ni0 事故的原始行为**。
  任何让护栏更容易回退的改动都能绕过 CI。

- **M5（🟡，源码缺口 → 本文件只标注，不修源码）**：把
  ``valid = oi_overwrite.notna()`` 改成 ``valid = ... & (oi_overwrite > 0)``，
  23 个用例**全绿**。→ 说明没有任何用例覆盖"新帧 ``open_interest`` = 0.0 时，
  护栏把主湖既有日期的 OI **静默清零**"这条真实数据损毁路径。
  触发条件（真实可达）：持久化负载缺 ``open_interest`` 列 / 该列全 NaN /
  网关返回 0 —— ``normalize_new_df.num()`` 三种情况一律填 ``0.0``，
  ``new_oi.isna().all()`` 判不出来，护栏照常生效并写下 0.0。
  OI 是护栏**唯一**允许写的字段，而它恰恰是唯一没有取值校验的字段。
  判据需工程师定夺（OI=0 是否物理合法），故此处用 ``xfail(strict=False)``
  把缺口**钉在代码里**：修好后本用例转 XPASS，是明确的"该删标记了"信号，
  在此之前不阻塞 CI。

另补两条契约锁：

- **E 项**：``raw_close`` 不在 ``MERGE_PROTECTED_COLUMNS``，现在只是被
  "整行保留旧值"**顺带**保护，无契约。本文件把它钉死，防止将来把护栏改成
  "列级白名单覆盖"时把名义价漏出去（名义价错 → 风险敞口算错 1.46 倍，
  见 ag0 k=0.6839 取证）。
- **红线**：本文件所有用例执行期间，仓库**真实主湖**分片必须零写入。

全部用例用 ``tmp_path`` 自建数据，**不依赖本地真实产物 / 不联网**。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts import p6_4_fill_gaps as fg
from scripts.p6_4_fill_gaps import (
    SCHEMA_COLUMNS,
    coerce_schema,
    merge_year_frames,
    stage_parse,
)
from scripts.rollback_adj_cells import FROZEN_COLUMNS, ROLLBACK_COLUMNS

INCIDENT_OLD_CLOSE = 158682.55
INCIDENT_NEW_CLOSE = 159021.988958


def _frame(rows: list[dict]) -> pd.DataFrame:
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
# M5：护栏唯一可写字段 open_interest 缺取值校验（源码缺口，只标注不修）
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(
    strict=False,
    reason=(
        "M5 缺口（变异存活）：新帧 open_interest=0.0 时护栏会把主湖既有 OI "
        "静默清零。normalize_new_df.num() 在『缺列 / 全 NaN / 网关返回 0』"
        "三种情况下一律填 0.0，而 new_oi.isna().all() 判不出来。"
        "是否修、怎么修需工程师定夺（OI=0 是否物理合法），QA 不擅自改源码。"
        "建议最小修法：OI 覆盖前校验新值 > 0，不合法则保留旧值并在 note 记一笔"
        "（**不要**回退到 legacy —— 那会连同价格列一起被覆盖，等于回到事故行为）。"
        "修好后本用例转 XPASS，请删除本标记。"
    ),
)
def test_guard_does_not_zero_existing_open_interest_when_new_oi_is_zero():
    """M5 缺口探针：新帧 OI=0.0 时，主湖既有 OI **不应**被清零。"""
    old = _frame([_row("2026-08-21", close=INCIDENT_OLD_CLOSE, oi=162899.0),
                  _row("2026-08-24", close=159258.12, oi=150334.0)])
    new = _frame([_row("2026-08-21", close=INCIDENT_NEW_CLOSE, oi=0.0),
                  _row("2026-08-24", close=159553.292123, oi=0.0)])

    merged, note = merge_year_frames(old, new)
    m = _idx(merged)

    # 价格列照常受保护（这部分当前实现是对的）
    assert float(m.loc["2026-08-21", "close"]) == INCIDENT_OLD_CLOSE
    # ⛔ 核心：OI 不得被 0.0 覆盖（护栏唯一的写通道，也是唯一没校验的通道）
    assert float(m.loc["2026-08-21", "open_interest"]) == 162899.0
    assert float(m.loc["2026-08-24", "open_interest"]) == 150334.0
    assert "护栏生效" in note


# --------------------------------------------------------------------------- #
# M8：回退判据边界（回退 = 回到事故行为，必须钉死）
# --------------------------------------------------------------------------- #
def test_guard_still_applies_when_only_part_of_protected_cells_are_nan():
    """M8 补锁：旧帧保护列**只有部分格** NaN 时，护栏必须**照常生效**。

    判据原文 ``old_prot.isna().all().all()``（所有格全 NaN 才回退）。
    变异 M8 放宽成 ``.any().any()`` 时本用例变红 —— 而"回退"意味着价格列
    被新帧整行覆盖，**正是 cu0/ni0 事故的原始行为**。
    """
    old = _frame([_row("2026-08-21", close=INCIDENT_OLD_CLOSE, oi=162899.0)])
    # 只把 high 打成 NaN（coerce_schema 之后设置，模拟未经 coerce 的入参）
    old.loc[0, "high"] = float("nan")
    new = _frame([_row("2026-08-21", close=INCIDENT_NEW_CLOSE, oi=150334.0)])

    merged, note = merge_year_frames(old, new)
    m = _idx(merged)

    # 护栏必须生效（不是回退）
    assert "护栏生效" in note, f"护栏被回退了（= 回到事故行为）：{note}"
    # 价格列守住
    assert float(m.loc["2026-08-21", "close"]) == INCIDENT_OLD_CLOSE
    # 授权字段照常更新（证明不是走了 legacy 回退路径：legacy 下 OI 也会是新值，
    # 但 close 会变成新值，上一行已排除）
    assert float(m.loc["2026-08-21", "open_interest"]) == 150334.0


def test_guard_fallback_predicate_is_all_nan_not_any_nan():
    """M8 补锁（源码级红线）：回退判据不得被放宽。

    与既有 ``test_rollback_and_guard_scripts_have_no_delete_calls`` 同类型——
    用源码文本做红线扫描，防止有人把 ``all().all()`` 悄悄放宽成 ``any().any()``，
    那会让护栏在真实脏数据下频繁退化到 keep=last 事故行为。
    """
    src = Path(fg.__file__).read_text(encoding="utf-8")

    assert "old_prot.isna()" in src, "回退判据 old_prot.isna() 消失，请复核护栏"
    assert "old_prot.isna().any().any()" not in src.replace(" ", ""), (
        "回退判据被放宽为 any().any()（任一格 NaN 即回退）—— 这会让护栏在真实"
        "脏数据下频繁退化到 keep=last 事故行为（= cu0/ni0 事故原样复现）"
    )


# --------------------------------------------------------------------------- #
# M7：后门留痕（stdout + 落盘）双通道锁
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


def test_backdoor_leaves_trace_on_stdout(sandbox, capsys):
    """M7 补锁：``--allow-price-overwrite`` 必须在 stdout 留下 ``[GUARD]`` 归因。

    变异 M7（把 ``print(f"[GUARD] ...")`` 换成 ``pass``）在本用例存在时变红。
    """
    lake_path = sandbox / "cu0" / "1d" / "2026.parquet"
    _frame([_row("2026-08-21", close=INCIDENT_OLD_CLOSE, oi=162899.0)]
           ).to_parquet(lake_path, index=False)
    persisted = _write_persisted(
        sandbox.parent,
        [["20260821", "CU", 157442.0, 159243.0, 157250.0, INCIDENT_NEW_CLOSE,
          500.0, 150334.0]],
    )

    rc = stage_parse(str(persisted), "CU", "20260821_20260821",
                     force=True, dry_run=False, skip_nominal=True, scale=1.0,
                     allow_price_overwrite=True)
    assert rc == 0
    out = capsys.readouterr().out

    assert "[GUARD]" in out, "后门放行未在 stdout 留痕，值班同学无法实时发现"
    assert "护栏已显式关闭" in out
    assert "--allow-price-overwrite" in out


def test_backdoor_leaves_trace_in_applied_json_for_every_record(sandbox):
    """C 项加固：``merge_guard`` 必须**每条**记录都落盘，且写明放行归因。

    既有 ``test_stage_parse_e2e_guard_protects_existing_ohlc`` 只断言
    ``any("护栏生效" in ...)`` —— 那是**单点**锁（变异 M6 仅被这 1 个用例杀死）。
    这里收紧为"每条记录都有 merge_guard 键"，并覆盖后门场景。
    """
    lake_path = sandbox / "cu0" / "1d" / "2026.parquet"
    _frame([_row("2026-08-21", close=INCIDENT_OLD_CLOSE, oi=162899.0)]
           ).to_parquet(lake_path, index=False)
    persisted = _write_persisted(
        sandbox.parent,
        [["20260821", "CU", 157442.0, 159243.0, 157250.0, INCIDENT_NEW_CLOSE,
          500.0, 150334.0]],
    )

    assert stage_parse(str(persisted), "CU", "20260821_20260821",
                       force=True, dry_run=False, skip_nominal=True, scale=1.0,
                       allow_price_overwrite=True) == 0

    applied = json.loads(
        (sandbox.parent / "artifacts" / "p6_4_applied.json").read_text(encoding="utf-8")
    )
    assert applied, "applied 记录为空，护栏结论无处可查"
    for rec in applied:
        assert "merge_guard" in rec, f"记录缺少 merge_guard 键: {sorted(rec)}"
        assert "护栏已显式关闭" in rec["merge_guard"]
        assert "--allow-price-overwrite" in rec["merge_guard"]


def test_guard_note_persisted_on_happy_path_for_every_record(sandbox):
    """C 项加固（正常分支）：护栏生效结论同样必须逐条落盘。"""
    lake_path = sandbox / "cu0" / "1d" / "2026.parquet"
    _frame([_row("2026-08-21", close=INCIDENT_OLD_CLOSE, oi=162899.0),
            _row("2026-08-24", close=159258.12, oi=150334.0)]
           ).to_parquet(lake_path, index=False)
    persisted = _write_persisted(
        sandbox.parent,
        [
            ["20260821", "CU", 157442.0, 159243.0, 157250.0, INCIDENT_NEW_CLOSE, 500.0, 150334.0],
            ["20260824", "CU", 159627.0, 159804.0, 158771.0, 159553.292123, 500.0, 138087.0],
            ["20260902", "CU", 160900.0, 161300.0, 160800.0, 161000.0, 500.0, 224406.0],
        ],
    )

    assert stage_parse(str(persisted), "CU", "20260821_20260902",
                       force=True, dry_run=False, skip_nominal=True,
                       scale=1.0) == 0

    applied = json.loads(
        (sandbox.parent / "artifacts" / "p6_4_applied.json").read_text(encoding="utf-8")
    )
    assert applied
    for rec in applied:
        assert "merge_guard" in rec
        assert "护栏生效" in rec["merge_guard"]


# --------------------------------------------------------------------------- #
# E 项：raw_close（名义价）保护契约锁
# --------------------------------------------------------------------------- #
def test_guard_keeps_existing_raw_close_even_though_not_in_protected_list():
    """E 项：``raw_close`` 不在 ``MERGE_PROTECTED_COLUMNS``，但**必须**被守住。

    现状：护栏靠"整行保留旧值"顺带保住名义价 —— 属**隐式**保护，无契约。
    本用例把它钉死：将来若有人把护栏改成"列级白名单覆盖"，名义价被新帧
    （raw_close = close 的复制品）覆盖会让 k 塌成 1.0，风险敞口算错 1.46 倍。
    """
    old = _frame([_row("2026-08-21", close=INCIDENT_OLD_CLOSE, oi=162899.0,
                       raw=107520.0)])
    new = _frame([_row("2026-08-21", close=INCIDENT_NEW_CLOSE, oi=150334.0,
                       raw=INCIDENT_NEW_CLOSE)])  # 新帧 raw_close = 复权价复制品

    merged, _ = merge_year_frames(old, new)
    m = _idx(merged)

    assert float(m.loc["2026-08-21", "raw_close"]) == 107520.0
    k = float(m.loc["2026-08-21", "adj_close"]) / float(m.loc["2026-08-21", "raw_close"])
    assert abs(k - INCIDENT_OLD_CLOSE / 107520.0) < 1e-12


# --------------------------------------------------------------------------- #
# 回滚脚本白名单契约（与护栏保护列同源）
# --------------------------------------------------------------------------- #
def test_rollback_whitelist_matches_guard_semantics():
    """D 项白名单范围：回滚白名单 ≡ 护栏保护列，且与冻结列零交集。"""
    assert set(ROLLBACK_COLUMNS) == {"open", "high", "low", "close", "adj_close"}, (
        f"回滚白名单变成 {ROLLBACK_COLUMNS}：只允许价格/复权列")
    assert set(FROZEN_COLUMNS).isdisjoint(ROLLBACK_COLUMNS)
    # 名义价必须同时被两边冻结/保护：回滚不得改、融合不得覆盖
    assert "raw_close" in FROZEN_COLUMNS
    for col in ("volume", "amount", "open_interest"):
        assert col in FROZEN_COLUMNS


# --------------------------------------------------------------------------- #
# 红线：本文件执行期间真实主湖零写入
# --------------------------------------------------------------------------- #
def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_guard_does_not_write_main_lake(sandbox):
    """红线：跑完整条 stage_parse 后，仓库真实主湖分片 sha256 必须不变。"""
    real_lake = (Path(fg.PROJECT_ROOT) / "data" / "raw" / "processed"
                 / "cu0" / "1d" / "2026.parquet")
    if not real_lake.exists():
        pytest.skip("真实主湖分片不存在（环境无数据），跳过")
    before = _sha(real_lake)

    lake_path = sandbox / "cu0" / "1d" / "2026.parquet"
    _frame([_row("2026-08-21", close=INCIDENT_OLD_CLOSE, oi=162899.0)]
           ).to_parquet(lake_path, index=False)
    persisted = _write_persisted(
        sandbox.parent,
        [["20260821", "CU", 157442.0, 159243.0, 157250.0, INCIDENT_NEW_CLOSE,
          500.0, 150334.0]],
    )
    assert stage_parse(str(persisted), "CU", "20260821_20260821",
                       force=True, dry_run=False, skip_nominal=True,
                       scale=1.0) == 0

    assert _sha(real_lake) == before, "真实主湖被写入！"
