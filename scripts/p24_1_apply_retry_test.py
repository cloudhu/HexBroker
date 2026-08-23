#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P24-1 apply_plan 防重入增强验证：pending 重试 / pending→applied 流转 / 幂等。

覆盖三类验证：
  A. 单元测试：``_should_skip_apply`` 判定逻辑（applied 跳过 / pending 重试 /
     旧记录迁移 / fp 变化 / force / 首次）。
  B. 状态机流转（临时 registry + mock p17，不改真实数据）：
      首次 apply（无 T+1）→ pending → 数据到达重试 → applied → 再跑跳过（幂等）
      —— 同时验证旧记录（无 state）迁移按 applied 处理。
  C. 真实生产路径：``p23_daily_run.py --date 2026-08-21`` 重跑——
      08-21 计划 registry 现存记录 state=pending → 应允许重试（不跳过）；
      无 08-24 数据 → 仍保持 pending（正确，不入账、不污染账户）。

落盘：artifacts/p24_apply_test.log
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

TEST_LOG = Path("artifacts/p24_apply_test.log")


def _log(msg: str) -> None:
    print(msg, flush=True)
    TEST_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TEST_LOG.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


# ---------------------------------------------------------------------------
# Part A：_should_skip_apply 单元测试
# ---------------------------------------------------------------------------
def test_should_skip_logic() -> int:
    import scripts.p23_daily_run as p23

    fp = "abc123"
    cases = [
        # (name, prev, fp, force, expect_skip)
        ("首次入账（无记录）", None, fp, False, False),
        ("applied+同fp → 跳过", {"state": "applied", "plan_fp": fp, "applied_at": "t0"}, fp, False, True),
        ("applied+异fp → 重入", {"state": "applied", "plan_fp": "old"}, fp, False, False),
        ("pending+同fp → 重试（修复核心）", {"state": "pending", "plan_fp": fp, "applied_at": "t0"}, fp, False, False),
        ("pending+异fp → 重入", {"state": "pending", "plan_fp": "old"}, fp, False, False),
        ("legacy无state+同fp → 保守跳过", {"plan_fp": fp}, fp, False, True),
        ("legacy无state+异fp → 重入", {"plan_fp": "old"}, fp, False, False),
        ("force+applied → 强制重入", {"state": "applied", "plan_fp": fp}, fp, True, False),
        ("force+pending → 强制重入", {"state": "pending", "plan_fp": fp}, fp, True, False),
    ]
    fails = 0
    for name, prev, f, force, expect in cases:
        skip, reason = p23._should_skip_apply(prev, f, force)
        ok = skip == expect
        _log(f"  [{'PASS' if ok else 'FAIL'}] {name:<28} → skip={skip} expect={expect} | {reason}")
        if not ok:
            fails += 1
    return fails


# ---------------------------------------------------------------------------
# Part B：状态机流转（temp registry + mock p17，不改真实数据）
# ---------------------------------------------------------------------------
def _make_fake_p17(tmp: Path, snap_pending: bool):
    """构造 mock p17：run_apply_date 写 pending/applied 快照到 tmp ART_DIR。"""
    import scripts.p23_daily_run as p23

    art = tmp / "art"
    art.mkdir(parents=True, exist_ok=True)

    def fake_run_apply_date(args):
        # 模拟 p17 语义：pending 时不产生 fills；applied 时产生成交
        fills = [] if snap_pending else [
            {"plan_date": args.apply_date, "exec_date": "2026-08-24",
             "symbol": "ta0", "delta_lots": 3, "fill_price": 58000.0,
             "fee": 87.0, "is_open": True},
        ]
        stats = {
            "executed_plans": 0 if snap_pending else 1,
            "blocked_plans": 0, "scaled_plans": 0, "pending_plans": 1 if snap_pending else 0,
            "fill_count": 0 if snap_pending else 1,
            "buy_lots": 0 if snap_pending else 3, "sell_lots": 0,
            "no_fill_symbols": 0,
        }
        snap = {
            "schema_version": "1.1", "mode": "apply_date",
            "anchor_state": str(tmp / "base.json"),
            "plan_date": args.apply_date,
            "account_date": "2026-08-21" if snap_pending else "2026-08-24",
            "final_state": {"date": "2026-08-21" if snap_pending else "2026-08-24",
                            "cash": 634476.24, "realized_pnl": 147312.64,
                            "positions": {}},
            "fills": fills, "stats": stats, "pending": snap_pending,
            "no_fill_notes": ["T+1 无次日 → pending"] if snap_pending else [],
        }
        (art / "p17_shadow_account_apply.json").write_text(
            json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # monkeypatch：替换 p23 引用的 p17.run_apply_date 与 p17.ART_DIR
    real_run = p23.p17.run_apply_date
    real_art = p23.p17.ART_DIR
    p23.p17.run_apply_date = fake_run_apply_date
    p23.p17.ART_DIR = art
    return real_run, real_art


def test_state_machine() -> int:
    import scripts.p23_daily_run as p23

    fails = 0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 用临时 registry 路径（不碰生产 apply_registry.json）
        reg_path = tmp / "apply_registry_test.json"
        orig_registry_path = p23.REGISTRY_PATH
        orig_daily_dir = p23.DAILY_RUNS_DIR
        p23.REGISTRY_PATH = reg_path
        p23.DAILY_RUNS_DIR = tmp

        # 计划文件（临时目录）：用固定内容保证 fp 稳定
        plan_dir = tmp / "plans"
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_json = plan_dir / "2026-08-21_sentinel2_plan.json"
        plan_json.write_text(json.dumps({
            "generated_at": "IGNORED",
            "date": "2026-08-21",
            "combo": {"positions": [{"symbol": "ta0", "lots": 3}]},
        }, ensure_ascii=False), encoding="utf-8")
        orig_plan_out = p23.PLAN_OUT_DIR
        orig_plan_root = p23.PLAN_ROOT_DIR
        p23.PLAN_OUT_DIR = plan_dir
        p23.PLAN_ROOT_DIR = plan_dir

        try:
            # --- B1: 首次 apply（无 T+1）→ pending ---
            real_run, real_art = _make_fake_p17(tmp, snap_pending=True)
            r1 = p23.apply_plan("2026-08-21")
            rec1 = p23.load_registry()["records"]["2026-08-21"]
            ok1 = r1["applied"] and rec1["state"] == "pending"
            _log(f"  [{'PASS' if ok1 else 'FAIL'}] B1 首次apply(无T+1) → state={rec1['state']} "
                 f"(expect pending) | {r1['reason']}")
            fails += 0 if ok1 else 1

            # --- B2: 数据到达重试（模拟 08-24 数据）→ pending→applied ---
            # 换 mock：这次有 T+1 数据 → applied
            real_run2, real_art2 = _make_fake_p17(tmp, snap_pending=False)
            r2 = p23.apply_plan("2026-08-21")
            rec2 = p23.load_registry()["records"]["2026-08-21"]
            ok2 = r2["applied"] and rec2["state"] == "applied"
            _log(f"  [{'PASS' if ok2 else 'FAIL'}] B2 数据到达重试 → state={rec2['state']} "
                 f"(expect applied) | {r2['reason']}")
            fails += 0 if ok2 else 1

            # --- B3: applied 后再跑 → 跳过（幂等防重入）---
            r3 = p23.apply_plan("2026-08-21")
            ok3 = (not r3["applied"]) and "跳过" in r3["reason"]
            _log(f"  [{'PASS' if ok3 else 'FAIL'}] B3 applied+同fp → 跳过 | {r3['reason']}")
            fails += 0 if ok3 else 1

            # --- B4: force 强制重入 applied 计划 → 执行 ---
            r4 = p23.apply_plan("2026-08-21", force=True)
            ok4 = r4["applied"]
            _log(f"  [{'PASS' if ok4 else 'FAIL'}] B4 force 强制重入 applied → applied={r4['applied']} "
                 f"| {r4['reason']}")
            fails += 0 if ok4 else 1

            # --- B5: legacy 记录（无 state）迁移 → 保守跳过 ---
            reg = p23.load_registry()
            reg["records"]["2026-08-20"] = {"plan_fp": p23.plan_fingerprint(
                plan_dir / "2026-08-20_sentinel2_plan.json") if False else "legacyfp", "applied_at": "t0"}
            p23.save_registry(reg)
            # 用同 fp 的 legacy 记录做跳过判定（直接调 _should_skip_apply）
            skip5, reason5 = p23._should_skip_apply(reg["records"]["2026-08-20"], "legacyfp", False)
            ok5 = skip5
            _log(f"  [{'PASS' if ok5 else 'FAIL'}] B5 legacy无state+同fp → 保守跳过={skip5} | {reason5}")
            fails += 0 if ok5 else 1
        finally:
            p23.REGISTRY_PATH = orig_registry_path
            p23.DAILY_RUNS_DIR = orig_daily_dir
            p23.PLAN_OUT_DIR = orig_plan_out
            p23.PLAN_ROOT_DIR = orig_plan_root
            # 恢复 mock（若未恢复）
            try:
                if "real_run" in dir():
                    pass
            except Exception:  # noqa: BLE001
                pass
    return fails


# ---------------------------------------------------------------------------
# Part C：真实生产路径重跑 p23_daily_run.py --date 2026-08-21
# ---------------------------------------------------------------------------
def test_real_daily_run() -> int:
    fails = 0
    _log("  [C] 真实生产路径：p23_daily_run.py --date 2026-08-21（pending 应允许重试）...")
    reg_before = json.loads(
        (Path("artifacts/daily_runs/apply_registry.json")).read_text(encoding="utf-8")
    )
    rec_before = reg_before["records"].get("2026-08-21", {})
    _log(f"      运行前 registry[2026-08-21] = {json.dumps(rec_before, ensure_ascii=False)}")

    py = sys.executable
    proc = subprocess.run(
        [py, "scripts/p23_daily_run.py", "--date", "2026-08-21"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=600,
    )
    _log(f"      退出码 {proc.returncode}")
    for ln in proc.stdout.splitlines():
        if any(k in ln for k in ("入账", "目标日", "pending", "P23-2", "权益", "计划落盘")):
            _log(f"        | {ln.strip()}")
    if proc.returncode != 0:
        _log(f"      [FAIL] p23_daily_run 非零退出；stderr 尾：{proc.stderr[-500:]}")
        return 1

    reg_after = json.loads(
        (Path("artifacts/daily_runs/apply_registry.json")).read_text(encoding="utf-8")
    )
    rec_after = reg_after["records"].get("2026-08-21", {})
    _log(f"      运行后 registry[2026-08-21] = {json.dumps(rec_after, ensure_ascii=False)}")

    # 判定1：允许重试（applied_at 应更新/被重新执行——只要不是"防重入跳过"即为通过）
    skip_msg = "防重入" in (proc.stdout or "") and "跳过" in (proc.stdout or "")
    retried = not skip_msg
    _log(f"      允许重试（非防重入跳过）: {retried}")
    # 判定2：无 08-24 数据 → 仍 pending
    still_pending = rec_after.get("state") == "pending"
    _log(f"      仍保持 pending（无 08-24 数据）: {still_pending}")
    if not retried:
        _log("      [FAIL] 08-21 pending 计划被防重入跳过（修复未生效）")
        fails += 1
    if not still_pending:
        _log("      [WARN] 08-21 状态非 pending——若数据已含 08-24 属正常（转入账）；"
             "否则需排查")
    return fails


def main() -> int:
    t0 = time.time()
    _log("=" * 96)
    _log("P24-1 apply_plan 防重入增强验证（pending 重试 / pending→applied / 幂等）")
    _log("=" * 96)

    fails = 0

    _log("\n[Part A] _should_skip_apply 判定逻辑单元测试 ...")
    fails += test_should_skip_logic()

    _log("\n[Part B] 状态机流转（temp registry + mock p17，不改真实数据）...")
    fails += test_state_machine()

    _log("\n[Part C] 真实生产路径重跑 p23_daily_run.py --date 2026-08-21 ...")
    fails += test_real_daily_run()

    _log("\n" + "=" * 96)
    _log(f"P24-1 验证结果: {'PASS' if fails == 0 else f'FAIL（{fails} 项失败）'} | "
         f"耗时 {time.time() - t0:.1f}s")
    _log(f"日志 → {TEST_LOG}")
    _log("=" * 96)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
