"""指纹（§8.2）：``model_id = sha1(config + 数据范围 + git_sha)[:12]``。

P0-3 新增：``FourLayerFingerprint``（数据-特征-模型-参数-配置五字段）与
``compute_four_layer`` / ``four_layer_report_block``，供 SignalStore sidecar
与报告输出使用。指纹约定与 ``model_id`` / ``config_fingerprint`` 一致：
一律 ``sha1(规范化JSON)[:12]``。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd


def get_git_sha(repo_root: Optional[str | Path] = None) -> str:
    """获取当前 git 仓库短 sha；非 git 仓库或无 git 时返回 'nogit'。"""
    root = Path(repo_root) if repo_root else Path.cwd()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()[:12]
    except Exception:
        pass
    return "nogit"


def model_id(
    config: Any,
    data_range: tuple[str, str],
    git_sha: Optional[str] = None,
) -> str:
    """计算模型/运行的指纹 ID。

    参数
    ----
    config : 可被 ``json.dumps`` 序列化的配置对象（如 HexConfig.model_dump()）。
    data_range : (start, end) 数据范围字符串。
    git_sha : 可选的 git 短 sha；缺省时自动探测。
    """
    if git_sha is None:
        git_sha = get_git_sha()
    cfg_str = json.dumps(config, sort_keys=True, default=str)
    payload = f"{cfg_str}|{data_range[0]}|{data_range[1]}|{git_sha}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# P0-3 四层指纹（数据 / 特征 / 模型 / 参数 + 配置层）
# ---------------------------------------------------------------------------
def _sha12(payload: str) -> str:
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class FourLayerFingerprint:
    """数据-特征-模型-参数-配置 五字段四层指纹（P0-3，结构相等判定）。

    字段
    ----
    data_version    : 数据层——DataLake manifest data_version + 内容指纹；
    feature_version : 特征层——sha1(feature 配置序列化) 前 12 位；
    model_version   : 模型层——model_id + train_end + 模型配置哈希；
    param_hash      : 参数层——sha1(sorted(model._params)) 前 12 位；
    config_version  : 配置层——config_fingerprint(cfg)。
    """

    data_version: str
    feature_version: str
    model_version: str
    param_hash: str
    config_version: str

    def to_dict(self) -> dict:
        return {
            "data_version": self.data_version,
            "feature_version": self.feature_version,
            "model_version": self.model_version,
            "param_hash": self.param_hash,
            "config_version": self.config_version,
        }

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, FourLayerFingerprint):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __hash__(self) -> int:
        return hash(tuple(sorted(self.to_dict().items())))


def compute_four_layer(cfg: Any, barframe: Any, model_id: str,
                       train_end: Any, params: dict) -> FourLayerFingerprint:
    """计算四层指纹（数据 / 特征 / 模型 / 参数 + 配置层）。

    同配置重跑结果完全一致（A3.3）；任一配置/数据/参数变化 → 指纹必然变化（A3.4）。
    """
    from ..data.manifest import content_fingerprint

    # 数据层：数据内容指纹（若 barframe.metadata 携带 manifest data_version 则优先）
    meta = getattr(barframe, "metadata", {}) or {}
    if isinstance(meta, dict) and meta.get("data_version"):
        data_version = f"{meta['data_version']}:{content_fingerprint(barframe.df)}"
    else:
        data_version = f"cf:{content_fingerprint(barframe.df)}"

    # 特征层：特征管道配置序列化指纹
    feat_cfg: dict = {}
    try:
        feat_cfg = cfg.feature.model_dump()
    except Exception:
        feat_cfg = {}
    feature_version = _sha12(json.dumps(feat_cfg, sort_keys=True, default=str))

    # 模型层：model_id + train_end + 模型配置哈希
    model_cfg: dict = {}
    try:
        model_cfg = cfg.forecast.model_dump()
    except Exception:
        model_cfg = {}
    model_cfg_hash = _sha12(json.dumps(model_cfg, sort_keys=True, default=str))
    te = ""
    if train_end is not None:
        try:
            te = pd.Timestamp(train_end).strftime("%Y%m%dT%H%M%S")
        except Exception:
            te = str(train_end)
    model_version = f"{model_id}:{te}:{model_cfg_hash}"

    # 参数层：sha1(sorted(model._params)) 前 12 位
    param_hash = _sha12(json.dumps(params, sort_keys=True, default=str))

    # 配置层：config_fingerprint(cfg)
    config_version = _config_version(cfg)

    return FourLayerFingerprint(
        data_version=data_version,
        feature_version=feature_version,
        model_version=model_version,
        param_hash=param_hash,
        config_version=config_version,
    )


def _config_version(cfg: Any) -> str:
    try:
        from ..config import config_fingerprint

        return config_fingerprint(cfg)
    except Exception:
        try:
            dump = cfg.model_dump() if hasattr(cfg, "model_dump") else {}
        except Exception:
            dump = {}
        return _sha12(json.dumps(dump, sort_keys=True, default=str))


def four_layer_report_block(fp: FourLayerFingerprint) -> dict:
    """报告用四层指纹块（T05 接入 pipeline）。"""
    return {
        "fingerprint": fp.to_dict(),
        "note": "四层指纹：data(内容指纹)/feature(管道配置)/model(model_id+train_end+模型配置)/param(参数哈希)/config(全局配置)",
    }
