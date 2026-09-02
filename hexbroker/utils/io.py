"""IO 工具：Parquet / JSON 读写、原子写、路径语义（§8.6）。

Parquet 优先用 pyarrow；未安装 pyarrow 时自动回退为 Feather（同列存，
被测试视为等价的列式落盘格式）。所有写入走临时文件 + 原子 rename，避免半截文件。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

from .logging import get_logger

_log = get_logger("ROOT")
_HAS_PARQUET = True
try:
    import pyarrow  # noqa: F401
except ImportError:  # pragma: no cover - 取决于环境
    _HAS_PARQUET = False


def read_parquet(path: str | Path) -> pd.DataFrame:
    """读取列式数据。优先 Parquet，缺失 pyarrow 时回退 Feather（``.parquet`` 同名 feather）。"""
    path = Path(path)
    if path.exists():
        return pd.read_parquet(path)
    feather_path = path.with_suffix(".feather")
    if feather_path.exists():
        return pd.read_feather(feather_path)
    raise FileNotFoundError(str(path))


def write_parquet(df: pd.DataFrame, path: str | Path) -> None:
    """原子写列式数据。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if _HAS_PARQUET:
        _atomic_write(lambda p: df.to_parquet(p), path)
    else:  # pragma: no cover - 取决于环境
        _log.warning("pyarrow 缺失，回退 Feather 列式落盘（等价列存）")
        _atomic_write(lambda p: df.to_feather(p), path.with_suffix(".feather"))


def _atomic_write(writer: Any, path: Path) -> None:
    """临时文件写入 + 原子 rename（``tmp + os.replace``，G5）。

    ⛔ 异常路径**不删除** tmp（P2-8，2026-09-02）：项目铁律禁止任何删除类调用
    —— 沙箱 safe-delete 钩子会拦截 ``unlink`` / ``os.remove`` 并路由至回收站，
    项目已因此丢过生产文件。写盘失败时：异常向上传播（不变）、原档未被触碰
    （``os.replace`` 未执行）、**tmp 保留**供排查；孤儿 ``.tmp`` 为 mkstemp
    随机名，不会被按扩展名的数据扫描命中，下一次成功写盘会正常覆盖目标。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    try:
        writer(tmp)
        os.replace(tmp, path)
    except Exception:
        _log.error("原子写失败（原档未动，tmp 已保留）：{} -> {}", tmp, path)
        raise


def read_json(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(obj: Any, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def _w(p: str) -> None:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, default=str)

    _atomic_write(_w, Path(path))


def ensure_dir(path: str | Path) -> Path:
    """确保目录存在并返回 Path。"""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p
