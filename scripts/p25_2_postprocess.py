#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P25-2 后处理编排：v13 合并 → 字节对比 → 覆盖率/引擎A/组合 → S4 监控。

在 scripts/p25_cache_rebuild.py（8 组 checkpoint）完成后运行：
  python scripts/p25_2_postprocess.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def run(step: str, cmd: list[str]) -> None:
    print(f"\n{'=' * 72}\n[STEP] {step}\n{'=' * 72}")
    t0 = time.time()
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit(f"[FAIL] {step} 退出码 {r.returncode}")
    print(f"[OK] {step} 耗时 {time.time() - t0:.1f}s")


def main() -> None:
    # 1. 合并 8 组 checkpoint → v13
    run("合并 checkpoint → v13", [PY, "-u", "scripts/p25_cache_rebuild.py", "--skip-rebuild"])
    # 2. 字节级对比 v13 vs v8
    run("字节级对比 v13 vs v8", [PY, "-u", "scripts/p25_2_byte_compare.py"])
    # 3. 覆盖率 + 引擎 A/组合三方评估
    run("覆盖率 + 引擎 A/组合三方评估", [PY, "-u", "scripts/p25_3_eval.py"])
    # 4. S4 影子监控（v13 vs v8）
    run("S4 影子监控 v13 vs v8", [PY, "-u", "scripts/p25_4_s4_v13.py"])
    print("\n" + "=" * 72)
    print("[DONE] P25-2 后处理完成")


if __name__ == "__main__":
    main()
