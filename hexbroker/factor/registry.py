"""因子注册表（§3 / P1-6）。

``FactorRegistry`` 支持 yaml/py 配置注册 DSL 因子，并提供 ``compute`` 在单标的上求值。
新增因子与既有 25 个硬编码特征并存（``keep_features`` 白名单对两者统一裁剪）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .dsl import FactorExpr


class FactorRegistry:
    """DSL 因子注册表：name -> FactorExpr。"""

    def __init__(self) -> None:
        self._factors: dict[str, FactorExpr] = {}

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register(self, fe: FactorExpr) -> None:
        if not isinstance(fe, FactorExpr):
            raise TypeError("register 仅接受 FactorExpr")
        fe.validate()  # 注册期即校验无未来函数
        if fe.name in self._factors:
            raise ValueError(f"因子名重复注册：{fe.name}")
        self._factors[fe.name] = fe

    def register_expr(self, name: str, expr: str, category: str = "custom") -> None:
        """便捷：直接以 (name, expr) 注册。"""
        self.register(FactorExpr(name=name, expr=expr, category=category))

    @classmethod
    def from_yaml(cls, path: str) -> "FactorRegistry":
        """从 yaml 加载因子列表（键 ``factors`` 为 FactorExpr 列表）。"""
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        reg = cls()
        for item in data.get("factors") or []:
            item = dict(item)
            name = item.pop("name")
            expr = item.pop("expr")
            reg.register(FactorExpr(name=name, expr=expr, category=item.get("category", "custom")))
        return reg

    # ------------------------------------------------------------------
    # 求值
    # ------------------------------------------------------------------
    def names(self) -> list[str]:
        return list(self._factors.keys())

    def column_for(self, name: str) -> str:
        """该因子在特征帧中的列名。"""
        return f"f_{name}"

    def compute(self, df: Any, name: str) -> Any:
        """对单标的 datetime-indexed DataFrame 求 ``f_{name}`` 列。"""
        if name not in self._factors:
            raise KeyError(f"未注册因子：{name}（已注册：{self.names()}）")
        return self._factors[name].compute(df)

    def __contains__(self, name: str) -> bool:
        return name in self._factors

    def __len__(self) -> int:
        return len(self._factors)
