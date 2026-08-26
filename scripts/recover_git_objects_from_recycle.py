"""从 Windows 回收站按原路径还原 .git/objects 下被误删的 git 对象。

背景：sandbox safe-delete 钩子把 git gc/repack 的对象删除路由到回收站，
导致 .git/objects/pack 与 loose 对象全部消失（refs/packed-refs 尚存）。

$I 文件格式（Win10, version=2）：
  0-7   版本号 (int64, =2)
  8-15  原文件大小 (int64)
  16-23 删除时间 (FILETIME)
  24-27 文件名字符数（含结尾 \0，int32）
  28-   原完整路径 UTF-16LE
"""
from __future__ import annotations

import shutil
import struct
import sys
from pathlib import Path

RECYCLE = Path(r"E:\$RECYCLE.BIN\S-1-5-21-1134724996-1222609925-1764708983-500")
TARGET_PREFIX = r"E:\Workspace\HexBroker\.git"


def parse_i_file(p: Path) -> tuple[int, str] | None:
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    if len(raw) < 28:
        return None
    version, size = struct.unpack_from("<qq", raw, 0)
    if version == 2:
        (n_chars,) = struct.unpack_from("<i", raw, 24)
        name_bytes = raw[28 : 28 + n_chars * 2]
    else:  # version 1: 固定 260 字符
        name_bytes = raw[24 : 24 + 260 * 2]
    name = name_bytes.decode("utf-16-le", errors="replace").rstrip("\x00")
    return size, name


def main() -> int:
    dry = "--apply" not in sys.argv
    items = sorted(RECYCLE.glob("$I*"))
    print(f"回收站条目总数: {len(items)}")

    hits: list[tuple[Path, Path, int]] = []
    others = 0
    for i_file in items:
        parsed = parse_i_file(i_file)
        if parsed is None:
            continue
        size, orig = parsed
        if not orig.startswith(TARGET_PREFIX):
            others += 1
            continue
        # 绝不还原锁文件/pid：还原它们会直接锁死 git（"Unable to create index.lock"）
        if orig.endswith(".lock") or orig.endswith("gc.pid"):
            others += 1
            continue
        r_file = i_file.with_name("$R" + i_file.name[2:])
        if not r_file.exists():
            print(f"  [缺数据] {orig}  (无 {r_file.name})")
            continue
        hits.append((r_file, Path(orig), size))

    print(f"命中 .git 对象: {len(hits)} / 其他条目: {others}")
    total = sum(s for _, _, s in hits)
    print(f"命中总字节: {total:,}")

    # 分类统计
    kinds: dict[str, int] = {}
    for _, orig, _ in hits:
        rel = str(orig).replace(TARGET_PREFIX + "\\", "")
        key = "objects/pack" if rel.startswith("objects\\pack") else (
            "objects/loose" if rel.startswith("objects\\") else rel.split("\\")[0]
        )
        kinds[key] = kinds.get(key, 0) + 1
    for k, v in sorted(kinds.items()):
        print(f"  {k}: {v}")

    if dry:
        print("\n[DRY-RUN] 加 --apply 执行还原")
        for _, orig, size in hits[:5]:
            print(f"  样例: {orig}  ({size:,}B)")
        return 0

    restored = skipped = failed = 0
    # 先还原目录条目（$R 为目录 → copytree），再还原文件，避免父目录缺失
    for is_dir_pass in (True, False):
        for r_file, orig, _size in hits:
            if r_file.is_dir() != is_dir_pass:
                continue
            if orig.exists() and not r_file.is_dir():
                skipped += 1
                continue
            try:
                orig.parent.mkdir(parents=True, exist_ok=True)
                if r_file.is_dir():
                    shutil.copytree(r_file, orig, dirs_exist_ok=True)
                else:
                    shutil.copy2(r_file, orig)
                restored += 1
            except OSError as exc:  # noqa: PERF203
                print(f"  [失败] {orig}: {exc}")
                failed += 1
    print(f"\n还原完成: restored={restored} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
