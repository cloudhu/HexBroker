"""指纹（§8.2）：``model_id = sha1(config + 数据范围 + git_sha)[:12]``。"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Optional


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
