#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1-2「交易时段禁止重写生产信号缓存」端到端验证（调用生产实现，无本地逻辑副本）。

事故：2026-09-01 09:10:12（日盘）生产信号缓存被重写，而模拟盘进程 08:58 已加载旧值
（rb0 p_up 0.8667 → 0.999999）。本脚本验证护栏同时满足两件事：

  * **拦得住**：交易时段内，任何调用方都无法写生产缓存；
  * **拦不过头**：允许窗口内，真实合并照常产出正确的 tail_ext 缓存。

安全约束
--------
* 允许分支的合并把 ``V8_PATH`` / ``TAIL_EXT_PATH`` 重定向到临时目录，**绝不写生产缓存**；
* 全程比对生产缓存的 (mtime, size, sha256)，任何一步后有变化即判 FAIL（G2 口径）；
* 真实 CLI 子进程仅在**当前时刻确实处于禁写窗口**时才跑（否则会触发 8 分钟重训），
  否则显式打印 SKIP 并说明理由——该分支由 in-process 用例与 pytest 确定性覆盖。

用法
----
  python scripts/p1_2_cache_rewrite_guard_verify.py
  → 退出码 0 = 全部通过；1 = 存在 FAIL
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

import scripts.p22_tail_ext as p22  # noqa: E402
from hexbroker.diagnostics import signal_refresh as sr  # noqa: E402
from hexbroker.market.session import is_cache_rewrite_blocked  # noqa: E402
from scripts.p6_4_apply_persisted_dir import _p22_rewrite_blocked  # noqa: E402

PROD_CACHE = ROOT / "artifacts" / "signals_cache18_grouped_v8_tail_ext.parquet"
PROD_V8 = ROOT / "artifacts" / "signals_cache18_grouped_v8.parquet"

BLOCKED_TS = pd.Timestamp("2026-09-01 09:10:12")  # 事故时刻（周二日盘上午）
ALLOWED_TS = pd.Timestamp("2026-09-01 12:05:00")  # 午休补刷窗口

_PASS = 0
_FAIL = 0


def check(cond: bool, tag: str, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {tag}" + (f" — {detail}" if detail else ""))
    else:
        _FAIL += 1
        print(f"  [FAIL] {tag}" + (f" — {detail}" if detail else ""))


def fingerprint(path: Path) -> tuple:
    if not path.exists():
        return ("MISSING", 0, "")
    b = path.read_bytes()
    return (round(path.stat().st_mtime, 3), len(b), hashlib.sha256(b).hexdigest()[:16])


def main() -> int:
    print("=" * 74)
    print("P1-2 端到端验证：交易时段禁止重写生产信号缓存")
    print("=" * 74)

    now = pd.Timestamp.now()
    now_blocked = is_cache_rewrite_blocked(now)
    print(f"\n当前时刻 {now:%Y-%m-%d %H:%M:%S} → 禁写窗口内={now_blocked}")

    fp_before = fingerprint(PROD_CACHE)
    print(f"生产缓存基线 {PROD_CACHE.name}: mtime={fp_before[0]} size={fp_before[1]} sha={fp_before[2]}")

    # ---------------------------------------------------------------- A
    print("\n[A] CLI 接线：--force-in-session 逃生舱已注册")
    h = subprocess.run(
        [sys.executable, "scripts/p22_tail_ext.py", "--help"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=180,
    )
    check(h.returncode == 0, "A1", f"--help exit={h.returncode}")
    check("--force-in-session" in (h.stdout or ""), "A2", "逃生舱参数已出现在 CLI")

    # ---------------------------------------------------------------- B
    print("\n[B] 真实 CLI 子进程（仅当当前时刻处于禁写窗口时才跑，避免 8 分钟重训）")
    if now_blocked:
        r = subprocess.run(
            [sys.executable, "scripts/p22_tail_ext.py"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=300,
        )
        out = (r.stdout or "") + (r.stderr or "")
        check(r.returncode == 2, "B1", f"exit={r.returncode}（期望 2）")
        check("P1-2" in out, "B2", "输出含 P1-2 禁写横幅")
        check("未被改动" in out, "B3", "横幅明确声明未做任何改动")
    else:
        print("  [SKIP] 当前不在禁写窗口，跳过真实子进程（否则会触发全量重训）。")
        print("         该分支由 C 段 in-process 用例与 tests/test_cache_rewrite_guard.py 确定性覆盖。")

    # ---------------------------------------------------------------- C
    print("\n[C] 硬护栏：唯一写入点 merge_tail_ext 在禁写窗口内拒绝写入")
    saved_v8, saved_out = p22.V8_PATH, p22.TAIL_EXT_PATH
    tmp = Path(tempfile.mkdtemp(prefix="p1_2_"))
    try:
        p22.V8_PATH = tmp / "no_such_v8.parquet"  # 不存在 → 放行则必抛 FileNotFoundError
        try:
            p22.merge_tail_ext(now=BLOCKED_TS)
            check(False, "C1", "禁写窗口内竟然放行了")
        except SystemExit as e:
            msg = str(e)
            check("P1-2" in msg, "C1", "抛 SystemExit 且含 P1-2 横幅")
            check("未被改写" in msg, "C2", "横幅声明缓存未被改写")
        except FileNotFoundError:
            check(False, "C1", "护栏未生效（进入了 IO 阶段）")
    finally:
        p22.V8_PATH, p22.TAIL_EXT_PATH = saved_v8, saved_out

    # ---------------------------------------------------------------- D
    print("\n[D] 拦不过头：允许窗口内真实合并照常产出（输出重定向到临时目录）")
    try:
        v8_copy = tmp / "v8.parquet"
        shutil.copy(PROD_V8, v8_copy)
        out_path = tmp / "tail_ext.parquet"
        p22.V8_PATH, p22.TAIL_EXT_PATH = v8_copy, out_path
        returned = p22.merge_tail_ext(now=ALLOWED_TS)
        check(returned == out_path, "D1", f"返回路径={returned.name}")
        df = pd.read_parquet(out_path)
        check(len(df) > 0, "D2", f"合并产出 {len(df)} 行")
        check({"symbol", "ts", "p_up", "exp_ret"} <= set(df.columns), "D3", "列结构完整")
        v8_n = len(pd.read_parquet(v8_copy))
        check(len(df) >= v8_n, "D4", f"tail_ext {len(df)} >= v8 {v8_n}（v8 原样保留 + 追加尾信号）")
        check(
            pd.to_datetime(df["ts"]).max() >= pd.to_datetime(
                pd.read_parquet(v8_copy)["ts"]
            ).max(),
            "D5", "末信号日未倒退",
        )
    finally:
        p22.V8_PATH, p22.TAIL_EXT_PATH = saved_v8, saved_out

    # ---------------------------------------------------------------- E
    print("\n[E] p6_4 预检：K 线融合与信号重建解耦")
    blocked, banner = _p22_rewrite_blocked(now=BLOCKED_TS)
    check(blocked is True, "E1", "禁写窗口内判定为拦")
    check("K 线融合" in banner, "E2", "横幅说明 K 线融合不受影响（避免误报整链失败）")
    blocked2, banner2 = _p22_rewrite_blocked(now=ALLOWED_TS)
    check(blocked2 is False and banner2 == "", "E3", "允许窗口内放行且横幅为空")

    # ---------------------------------------------------------------- F
    print("\n[F] 启动期自动刷新：禁写窗口跳过，且永不自行加强制标志")
    stale = [sr.FreshnessProbe("c.parquet", "2026-08-01 00:00", fd=5, threshold=0)]
    ok, msg = sr.maybe_auto_refresh(stale, enabled=True, now=BLOCKED_TS)
    check(ok is False and "P1-2" in msg, "F1", "禁写窗口内直接跳过（不空耗 timeout）")

    seen: dict = {}

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        seen["cmd"] = list(cmd)
        return _R()

    orig_run = sr.subprocess.run
    sr.subprocess.run = fake_run
    try:
        ok2, msg2 = sr.maybe_auto_refresh(stale, enabled=True, now=ALLOWED_TS)
        joined = " ".join(seen.get("cmd", []))
        check(ok2 is True, "F2", f"允许窗口内正常拉起（{msg2}）")
        check("--force-in-session" not in joined, "F3", "自动刷新未自行加强制标志")
        check("--skip-eval" in joined, "F4", "仍为 --skip-eval 口径")
    finally:
        sr.subprocess.run = orig_run

    # ---------------------------------------------------------------- G
    print("\n[G] 生产缓存不变性（G2 口径：mtime / size / sha256 三重比对）")
    fp_after = fingerprint(PROD_CACHE)
    check(fp_before == fp_after, "G1",
          f"before={fp_before} after={fp_after}")

    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 74)
    print(f"结果：{_PASS} passed / {_FAIL} failed")
    print("=" * 74)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
