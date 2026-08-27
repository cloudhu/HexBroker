"""因子库资产化包（§3 / P1-6）。

- ``FactorExpr`` / DSL 求值器（零依赖 qlib，自研原语）。
- ``FactorRegistry``：yaml/py 注册 + 单标的求值。
- ``FactorICArchive``：滚动 OOS RankIC 档案（sidecar 缓存）。

新增 DSL 因子与既有 25 个硬编码特征并存；默认不被 ``feature/pipeline`` 自动加载，
须显式传 ``factor_registry`` 才生效（保证默认行为零变化，549 测试全绿）。
"""

from __future__ import annotations

from .dsl import FactorExpr
from .ic import FactorICArchive
from .registry import FactorRegistry

__all__ = ["FactorExpr", "FactorRegistry", "FactorICArchive"]
