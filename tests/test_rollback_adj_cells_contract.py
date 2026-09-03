"""``scripts/rollback_adj_cells.py``（P0-A 止血）代码路径契约测试。

D 项复核要求：不做数据再核验（cu0/ni0 20 格的数值已由
``artifacts/_tmp/rollback_adj_cells_20260903_174558.json`` 取证），只验证
**代码路径**是否具备"值班可用"的四条性质：

1. **白名单范围**：只改价格/复权列，绝不碰 ``raw_close``/``volume``/``amount``
   /``open_interest``（P2-4 方案 A 授权修正，必须保留）；
2. **G5 原子写**：只写 ``*.parquet.tmp`` 再 ``replace``，**绝不直接写目标文件**；
   零删除、无 ``shutil.move``；
3. **幂等**：连续两次 ``--apply``，第二次不产生任何数据变化，且产物逐字节相同；
4. **fail-closed**：快照缺失 / 目标日期缺失 → 显式报错且**绝不写盘**。

另含一条**红线扫描器自检**（变异思想）：把 ``os.remove("x")`` 注入真实源码
文本后，同一个 AST 扫描器必须报出命中 —— 否则
``test_rollback_and_guard_scripts_have_no_delete_calls`` 那条"永远为空"的
断言是**空断言**（vacuous），根本没牙齿。

全部用例在 ``tmp_path`` 沙箱里跑，**绝不触碰真实主湖**。
"""
from __future__ import annotations

import ast
import hashlib
import shutil
from pathlib import Path

import pandas as pd
import pytest

from scripts import rollback_adj_cells as rl

DAYS = ("2026-08-20", "2026-08-21", "2026-08-24", "2026-08-25", "2026-09-02")
# 事故真实数值：08-21 的 close 被污染成 159021.988958（旧值 158682.55）
POLLUTED = {"2026-08-21": 159021.988958, "2026-08-24": 159553.292123}
CLEAN = {"2026-08-21": 158682.55, "2026-08-24": 159258.12}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mk(polluted: bool) -> pd.DataFrame:
    """构造 2026 分片：``polluted`` 为真时 08-21/08-24 用污染值。"""
    prices = POLLUTED if polluted else CLEAN
    rows = []
    for i, day in enumerate(DAYS):
        close = prices.get(day, 159000.0 + i)
        rows.append({
            "symbol": "cu0",
            "datetime": pd.Timestamp(day),
            "open": close - 1.0,
            "high": close + 1.0,
            "low": close - 2.0,
            "close": close,
            "volume": 500.0 + i,
            "amount": 1.0e6 + i,
            "open_interest": 162899.0 + i,
            "raw_close": 107520.0 + i,          # 名义价：两边必须一致
            "adj_close": close,
            "limit_up": False,
            "limit_down": False,
            "is_rollover": False,
        })
    return pd.DataFrame(rows)


@pytest.fixture()
def sb(tmp_path, monkeypatch):
    """沙箱：lake（污染态）+ snapshot（权威态），并把落盘目标重定向。"""
    lake_root = tmp_path / "lake"
    snap_root = tmp_path / "snap"
    for sym in ("cu0", "ni0"):
        d = lake_root / sym / "1d"
        d.mkdir(parents=True, exist_ok=True)
        _mk(polluted=True).to_parquet(d / "2026.parquet", index=False)
        s = snap_root / sym / "1d"
        s.mkdir(parents=True, exist_ok=True)
        _mk(polluted=False).to_parquet(s / "2026.parquet", index=False)
    monkeypatch.setattr(rl, "PROCESSED_DIR", lake_root)
    monkeypatch.setattr(rl, "AUDIT_DIR", tmp_path / "audit")
    return lake_root, snap_root


# --------------------------------------------------------------------------- #
# 1) 白名单范围
# --------------------------------------------------------------------------- #
def test_default_plan_is_20_cells_over_five_price_columns(sb):
    """G1 预算：默认计划恰好 20 格 = 5 列 × 2 日期 × 2 品种，且全是价格/复权列。"""
    lake_root, snap_root = sb
    cells = rl.build_plan(rl.DEFAULT_PLAN, lake_root, snap_root)

    assert len(cells) == 20
    assert {c["column"] for c in cells} == set(rl.ROLLBACK_COLUMNS)
    assert {(c["sym0"], c["date"]) for c in cells} == {
        (s, d) for s in ("cu0", "ni0") for d in ("2026-08-21", "2026-08-24")
    }
    # 冻结列一格都不许出现在计划里
    assert not ({c["column"] for c in cells} & set(rl.FROZEN_COLUMNS))


def test_apply_plan_never_touches_frozen_columns_on_any_row(sb):
    """冻结列逐行逐位不变（比脚本自带的"只查目标日期"更严）。"""
    lake_root, snap_root = sb
    before = {s: pd.read_parquet(lake_root / s / "1d" / "2026.parquet")
              for s in ("cu0", "ni0")}

    cells = rl.build_plan(rl.DEFAULT_PLAN, lake_root, snap_root)
    rl.apply_plan(rl.DEFAULT_PLAN, cells, lake_root, snap_root)

    for sym, old in before.items():
        new = pd.read_parquet(lake_root / sym / "1d" / "2026.parquet")
        for col in rl.FROZEN_COLUMNS:
            assert old[col].tolist() == new[col].tolist(), (
                f"{sym}.{col} 被改动（冻结列）："
                f"before={old[col].tolist()} after={new[col].tolist()}")
            assert old[col].dtype == new[col].dtype, f"{sym}.{col} dtype 被改动"


def test_apply_plan_preserves_column_order_and_row_count(sb):
    """G3：列序与行数必须与写前完全一致。"""
    lake_root, snap_root = sb
    before = pd.read_parquet(lake_root / "cu0" / "1d" / "2026.parquet")
    cells = rl.build_plan(rl.DEFAULT_PLAN, lake_root, snap_root)
    rl.apply_plan(rl.DEFAULT_PLAN, cells, lake_root, snap_root)
    after = pd.read_parquet(lake_root / "cu0" / "1d" / "2026.parquet")

    assert list(after.columns) == list(before.columns)
    assert len(after) == len(before)


# --------------------------------------------------------------------------- #
# 2) G5 原子写
# --------------------------------------------------------------------------- #
def test_apply_plan_writes_only_tmp_then_replaces(sb, monkeypatch):
    """G5：``to_parquet`` 的目标**必须**是 .tmp，绝不直接写最终分片。"""
    lake_root, snap_root = sb
    written: list[str] = []
    original = pd.DataFrame.to_parquet

    def spy(self, path, *a, **k):
        written.append(str(path))
        return original(self, path, *a, **k)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", spy)
    cells = rl.build_plan(rl.DEFAULT_PLAN, lake_root, snap_root)
    rl.apply_plan(rl.DEFAULT_PLAN, cells, lake_root, snap_root)

    assert written, "没有任何 to_parquet 调用，测试探针失效"
    for p in written:
        assert p.endswith(".parquet.tmp"), (
            f"直接写目标文件（非原子，写一半崩溃即毁分片）: {p}")
    # 替换后不留临时文件
    assert list(lake_root.rglob("*.tmp")) == []
    # 最终分片确实存在且已被改写
    assert (lake_root / "cu0" / "1d" / "2026.parquet").exists()


def test_backup_symbol_copies_without_moving_or_deleting(sb, monkeypatch, tmp_path):
    """备份只 copy2：``shutil.move`` 被调用即视为红线，且源文件必须原样保留。"""
    def _boom(*a, **k):  # pragma: no cover - 只在违规时触发
        raise AssertionError("shutil.move 被调用：备份只允许 copy2")

    monkeypatch.setattr(shutil, "move", _boom)
    lake_root, _ = sb
    src_dir = lake_root / "cu0" / "1d"
    src_files = sorted(p.name for p in src_dir.iterdir() if p.is_file())
    src_sha = {n: _sha(src_dir / n) for n in src_files}

    copied = rl.backup_symbol("cu0", tmp_path / "pre_backup")

    assert sorted(copied) == src_files
    # 源文件一个都没少、内容一个字节都没变
    assert sorted(p.name for p in src_dir.iterdir() if p.is_file()) == src_files
    for n, h in src_sha.items():
        assert _sha(src_dir / n) == h
    # 目标端是副本，不是搬过去的
    dest = tmp_path / "pre_backup" / "cu0" / "1d"
    assert sorted(p.name for p in dest.iterdir() if p.is_file()) == src_files


def test_redline_scanner_has_teeth():
    """红线扫描器自检：注入 ``os.remove`` 后必须报命中（证明不是空断言）。"""
    src = Path(rl.__file__).read_text(encoding="utf-8")

    def scan(text: str) -> list[str]:
        banned = {"unlink", "remove", "rmtree", "move"}
        tree = ast.parse(text)
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (
                    fn.id if isinstance(fn, ast.Name) else "")
                if name in banned:
                    hits.append(f"L{node.lineno}:{name}")
        return hits

    # 原始源码：干净
    assert scan(src) == []
    # 注入 os.remove → 必须命中（否则既有红线用例是空断言）
    injected = src + '\n\ndef _evil():\n    import os\n    os.remove("x")\n'
    assert scan(injected), "扫描器对 os.remove 无反应 —— 既有红线用例是空断言"
    # 注入 shutil.move → 必须命中
    injected2 = src + '\n\ndef _evil2():\n    import shutil\n    shutil.move("a", "b")\n'
    assert scan(injected2), "扫描器对 shutil.move 无反应"


# --------------------------------------------------------------------------- #
# 3) 幂等
# --------------------------------------------------------------------------- #
def test_apply_plan_is_idempotent_and_bit_identical(sb):
    """连续两次 apply：第二次不产生数据变化，且产物逐字节相同。"""
    lake_root, snap_root = sb
    f = lake_root / "cu0" / "1d" / "2026.parquet"

    cells1 = rl.build_plan(rl.DEFAULT_PLAN, lake_root, snap_root)
    rl.apply_plan(rl.DEFAULT_PLAN, cells1, lake_root, snap_root)
    after1 = pd.read_parquet(f)
    sha1 = _sha(f)

    # 第二轮：重新求值（此时 current == target）
    cells2 = rl.build_plan(rl.DEFAULT_PLAN, lake_root, snap_root)
    rl.apply_plan(rl.DEFAULT_PLAN, cells2, lake_root, snap_root)
    after2 = pd.read_parquet(f)

    pd.testing.assert_frame_equal(after1, after2)
    assert _sha(f) == sha1, "第二次 apply 改写了分片字节（非幂等）"
    # 第二轮计划里所有 Δ 都应为 0
    assert all(abs(c["delta"]) == 0.0 for c in cells2)
    # verify 无残留
    assert rl.verify(rl.DEFAULT_PLAN, lake_root, snap_root) == []


def test_apply_plan_actually_rolls_back_the_polluted_cells(sb):
    """正向：apply 后目标格 == 快照值（回滚真的生效，不是空转）。"""
    lake_root, snap_root = sb
    cells = rl.build_plan(rl.DEFAULT_PLAN, lake_root, snap_root)
    assert any(abs(c["delta"]) > 0 for c in cells), "计划里没有任何差异，用例失效"

    rl.apply_plan(rl.DEFAULT_PLAN, cells, lake_root, snap_root)
    assert rl.verify(rl.DEFAULT_PLAN, lake_root, snap_root) == []


# --------------------------------------------------------------------------- #
# 4) fail-closed
# --------------------------------------------------------------------------- #
def test_missing_snapshot_raises_and_writes_nothing(sb):
    """快照缺失 → 显式报错，且主湖一个字节都不许变。"""
    lake_root, snap_root = sb
    f = lake_root / "cu0" / "1d" / "2026.parquet"
    before = _sha(f)

    with pytest.raises(FileNotFoundError, match="权威快照不存在"):
        rl.build_plan(rl.DEFAULT_PLAN, lake_root,
                      snap_root.parent / "no_such_snapshot")

    assert _sha(f) == before


def test_missing_target_date_raises_and_writes_nothing(sb):
    """目标日期缺失 → 显式报错，且主湖一个字节都不许变。"""
    lake_root, snap_root = sb
    f = lake_root / "cu0" / "1d" / "2026.parquet"
    before = _sha(f)
    plan = {"cu0": ("2026-08-21", "1999-01-04")}  # 1999 不在分片里

    with pytest.raises(KeyError, match="1999-01-04"):
        rl.build_plan(plan, lake_root, snap_root)

    assert _sha(f) == before


def test_dry_run_writes_nothing(sb):
    """默认 dry-run：不写盘、不留临时文件（``main()`` 端到端）。"""
    import sys

    lake_root, snap_root = sb
    f = lake_root / "cu0" / "1d" / "2026.parquet"
    before = _sha(f)

    argv = ["rollback_adj_cells.py", "--lake-dir", str(lake_root),
            "--source-dir", str(snap_root),
            "--pre-backup-dir", str(lake_root.parent / "pre")]
    old_argv = sys.argv
    sys.argv = argv
    try:
        rc = rl.main()
    finally:
        sys.argv = old_argv

    assert rc == 0
    assert _sha(f) == before, "dry-run 竟然写了盘"
    assert list(lake_root.rglob("*.tmp")) == []
    assert not (lake_root.parent / "pre").exists(), "dry-run 竟然做了写前备份"
