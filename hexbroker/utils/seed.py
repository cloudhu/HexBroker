"""全局随机种子（§8.2）。

设置 ``random / numpy / torch / cudnn`` 为确定性，保证可复现。
torch 为可选依赖：未安装时静默跳过。
"""

from __future__ import annotations

import os
import random

import numpy as np

from .logging import get_logger

_log = get_logger("ROOT")


def set_global_seed(seed: int = 42) -> None:
    """设置全局随机种子，确保实验可复现。"""
    random.seed(seed)
    np.random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    try:
        import torch  # type: ignore

        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        torch.use_deterministic_algorithms(True, warn_only=True)
        torch.backends.cudnn.deterministic = True  # type: ignore[attr-defined]
        torch.backends.cudnn.benchmark = False  # type: ignore[attr-defined]
    except ImportError:
        # CPU 沙箱未安装 torch，纯 numpy 路径，无需处理
        pass
    _log.info(f"全局随机种子已设置: seed={seed}")
