"""序 3 备份：把三品种 1d 分区完整复制到 artifacts 快照目录并校验。

只读源数据湖，只写 artifacts/。零风险。
用法::

    python scripts/_p3_backup_caliber.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "data" / "raw" / "processed"
SYMBOLS = ("ag0", "au0", "m0")
FREQ = "1d"


def _stamp() -> str:
    out = subprocess.run(["date", "+%Y%m%dT%H%M%S"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def main() -> int:
    stamp = _stamp()
    dst_root = PROJECT_ROOT / "artifacts" / f"p3_caliber_backup_{stamp}"
    dst_root.mkdir(parents=True, exist_ok=True)
    print(f"[BACKUP] 目标目录 {dst_root}")

    rows: list[tuple[str, str, int, int, str]] = []
    total = 0
    for sym in SYMBOLS:
        src_dir = SRC_ROOT / sym / FREQ
        files = sorted(src_dir.glob("*.parquet"))
        for src in files:
            dst_dir = dst_root / sym / FREQ
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / src.name
            shutil.copy2(src, dst)
            n_src = len(pd.read_parquet(src))
            n_dst = len(pd.read_parquet(dst))
            ok = "OK" if n_src == n_dst else "MISMATCH"
            rows.append((sym, src.name, n_src, n_dst, ok))
            total += 1

    print(f"[BACKUP] 复制文件数 {total}（期望 27）")
    print(f"{'sym':<5} {'file':<12} {'src_rows':>9} {'dst_rows':>9}  {'check':<9}")
    for sym, name, a, b, ok in rows:
        print(f"{sym:<5} {name:<12} {a:>9} {b:>9}  {ok:<9}")

    bad = [r for r in rows if r[4] != "OK"]
    if total != 27 or bad:
        print(f"[FAIL] 备份不完整：文件数={total} 不一致项={len(bad)}")
        return 1
    print(f"[OK] 备份完成：{total} 文件全部行数一致 → {dst_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
