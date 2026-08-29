"""P0-3 存量数据 manifest 回填迁移脚本（PRD A3.1）。

对 ``data/{raw,interim,processed}`` 下已存在的 parquet 分区一键回填
``manifest.json``（sidecar JSON，不改 Parquet schema）。

用法::

    python scripts/backfill_manifests.py --data-root data [--config configs/base.yaml]
"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="存量数据 manifest 回填（P0-3）")
    ap.add_argument("--data-root", default="data", help="数据湖根目录（默认 data）")
    ap.add_argument("--config", default=None, help="实验 yaml（用于抽取口径常量）")
    ap.add_argument("--skip-existing", action="store_true",
                    help="增量模式：跳过已有 manifest 的分区（不触碰生产自动化维护的 manifest）")
    args = ap.parse_args(argv)

    from hexbroker.config import load_config
    from hexbroker.data.manifest import backfill_manifests

    cfg = load_config(args.config)
    n = backfill_manifests(args.data_root, cfg, skip_existing=args.skip_existing)
    print(f"回填完成：{n} 个 manifest（root={args.data_root}，skip_existing={args.skip_existing}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
