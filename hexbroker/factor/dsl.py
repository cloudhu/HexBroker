"""因子表达式 DSL（§3 / P1-6）。

自研轻量表达式引擎（借鉴 qlib ``ExpressionEngine`` 思路，但**零依赖 qlib**）：
解析 ``FactorExpr.expr`` 为 AST → 对单标的 datetime-indexed DataFrame 严格因果求值
→ 输出 ``f_<name>`` 列。原语复用 ``feature/technical.py`` 语义，保证 DSL 因子可与
25 个硬编码特征逐数值对齐（<1e-9）。

防泄漏纪律：
- 仅暴露经白名单的算子；拒绝属性访问（``.``）、未知函数；
- ``Ref(x, n)`` 仅允许 ``n >= 0``（负 shift = 未来函数，注册期 ``validate()`` 即报错）；
- 所有滚动窗口均为右端对齐、[t-w+1, t] 区间，绝不引入未来信息。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# 列访问与原子原语
# ---------------------------------------------------------------------------
_COLUMNS = ("close", "open", "high", "low", "volume")


def _mp(w: float) -> int:
    """滚动窗口最小样本数：与 ``feature/technical.py`` 一致（w>=14→5，否则 2）。"""
    return 5 if int(w) >= 14 else 2


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    if name not in df.columns:
        raise KeyError(f"DSL 需要列 '{name}'，但输入 DataFrame 缺失（单标的需含 OHLCV）")
    return df[name].astype(float)


def _safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    """除零安全（与 ``feature/technical._safe_ratio`` 一致）：分母 0→NaN→（由 compute 末尾 fillna 0）。"""
    return a.divide(b.replace(0, np.nan))


def _log_ret(close: pd.Series) -> pd.Series:
    """单标的 1-bar 对数收益（严格因果：t 时刻只用 t-1 收盘）。"""
    return np.log(close / close.shift(1)).fillna(0.0)


def _rsi_series(x: pd.Series, w: int) -> pd.Series:
    """Wilder 近似 RSI，与 ``feature/technical.add_technical`` 完全一致。"""
    delta = x.diff().fillna(0.0)
    gain = delta.clip(lower=0.0).rolling(w, min_periods=5).mean()
    loss = (-delta.clip(upper=0.0)).rolling(w, min_periods=5).mean()
    rs = gain.divide(loss.replace(0, np.nan)).fillna(0.0)
    f_rsi = 100.0 - 100.0 / (1.0 + rs)
    return f_rsi.mask(loss == 0, 100.0).fillna(50.0)


def _macd_line(x: pd.Series) -> pd.Series:
    """MACD 主线（ema12 - ema26，adjust=False），与 technical 一致。"""
    return x.ewm(span=12, adjust=False).mean() - x.ewm(span=26, adjust=False).mean()

    # 原语注册表：name -> callable(*Series/number args) -> Series（返回类型宽松为 Any，
    # 因 Close/Open 等列访问占位 lambda 返回 None，且 DSL 求值允许 Series/标量混合）


_FUNCS: dict[str, Callable[..., Any]] = {
    # 0 参列访问
    "Close": lambda: None,  # 占位，实际由 _eval_call 特殊处理
    "Open": lambda: None,
    "High": lambda: None,
    "Low": lambda: None,
    "Volume": lambda: None,
    # 延迟 / 移动统计
    "Ref": lambda x, n: x.shift(int(n)),
    "MA": lambda x, w: x.rolling(int(w), min_periods=_mp(w)).mean().fillna(0.0),
    "Mean": lambda x, w: x.rolling(int(w), min_periods=_mp(w)).mean().fillna(0.0),
    "EMA": lambda x, w: x.ewm(span=int(w), adjust=False).mean(),
    "Std": lambda x, w: x.rolling(int(w), min_periods=_mp(w)).std().fillna(0.0),
    "Sum": lambda x, w: x.rolling(int(w), min_periods=_mp(w)).sum().fillna(0.0),
    # 技术指标
    "RSI": lambda x, w=14: _rsi_series(x, int(w)),
    "MACD": lambda x: _macd_line(x),
    # 数学
    "Abs": lambda x: x.abs(),
    # 与 technical.py 一致：log(价格比) 首根 NaN→0（log_ret 预填充），保证滚动聚合计数一致
    "Log": lambda x: np.log(x).fillna(0.0),
    "Div": lambda a, b: _safe_div(a, b),
    "Mul": lambda a, b: a * b,
    "Add": lambda a, b: a + b,
    "Sub": lambda a, b: a - b,
    "Min": lambda a, b: np.minimum(a, b),
    "Max": lambda a, b: np.maximum(a, b),
    "Rank": lambda x, w=60: x.rolling(int(w), min_periods=2)
    .apply(lambda s: s.rank(pct=True).iloc[-1], raw=False)
    .fillna(0.0),
}


def _is_col_call(func: str) -> bool:
    return func in ("Close", "Open", "High", "Low", "Volume")


# ---------------------------------------------------------------------------
# AST 校验
# ---------------------------------------------------------------------------
def _literal_value(node: ast.AST) -> float | None:
    """从 AST 常量/一元负号节点提取数值（支持 `5` 与 `-1` 两种写法）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _literal_value(node.operand)
        if v is None:
            return None
        return -v if isinstance(node.op, ast.USub) else v
    return None


def _validate_ast(tree: ast.Expression) -> None:
    """遍历 AST，确保仅含白名单节点/函数，且 Ref 无负 shift（未来函数）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            raise ValueError("DSL 禁止属性访问（.），仅允许列名/白名单函数")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError("DSL 仅允许直接函数调用 f(x)")
            if node.func.id not in _FUNCS:
                raise ValueError(f"DSL 未知函数：{node.func.id}")
            # Ref 负 shift = 未来函数，注册期即拒（支持 Ref(x, -1) 写法）
            if node.func.id == "Ref" and len(node.args) >= 2:
                v = _literal_value(node.args[1])
                if v is not None and v < 0:
                    raise ValueError("Ref 负 shift 属未来函数，DSL 禁止")
        if isinstance(node, ast.Lambda):
            raise ValueError("DSL 禁止 lambda")


# ---------------------------------------------------------------------------
# 求值器
# ---------------------------------------------------------------------------
def _eval_node(node: ast.AST, df: pd.DataFrame) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, df)
    if isinstance(node, ast.Constant):  # py3.8+
        if isinstance(node.value, bool):  # 布尔不当数值
            return 0.0
        if isinstance(node.value, (int, float)):
            return float(node.value)
        return 0.0
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_node(node.operand, df)
    if isinstance(node, ast.BinOp):
        a = _eval_node(node.left, df)
        b = _eval_node(node.right, df)
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Sub):
            return a - b
        if isinstance(node.op, ast.Mult):
            return a * b
        if isinstance(node.op, ast.Div):
            return _safe_div(a, b)
        if isinstance(node.op, ast.Mod):
            return a % b
        raise ValueError(f"DSL 不支持的二元运算符：{type(node.op).__name__}")
    if isinstance(node, ast.Name):
        # 允许 `close` 与 `Close` 两种写法，统一映射为小写列名
        lid = node.id.lower()
        if lid in _COLUMNS:
            return _col(df, lid)
        raise ValueError(f"DSL 未知列名：{node.id}（可用：{_COLUMNS}）")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("DSL 仅允许直接函数调用 f(x)")
        fname = node.func.id
        args = [_eval_node(a, df) for a in node.args]
        if _is_col_call(fname):
            return _col(df, fname.lower())
        return _FUNCS[fname](*args)
    raise ValueError(f"DSL 不支持的语法节点：{type(node).__name__}")


# ---------------------------------------------------------------------------
# FactorExpr
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FactorExpr:
    """单条因子表达式。

    ``expr`` 示例：``"Log(Div(Close, Ref(Close, 1)))"``、``"Mean(Volume, 20)"``。
    求值严格因果，输出列名为 ``f_{name}``。
    """

    name: str
    expr: str
    category: str = "custom"

    def parse(self) -> ast.Expression:
        """解析为 AST（含白名单/未来函数校验）。"""
        try:
            tree = ast.parse(self.expr, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"因子 '{self.name}' 表达式语法错误：{exc}") from exc
        _validate_ast(tree)
        return tree

    def validate(self) -> None:
        """注册期校验：解析 + 无未来函数。失败抛 ``ValueError``。"""
        self.parse()

    def compute(self, df: pd.DataFrame) -> pd.Series:
        """对单标的 datetime-indexed DataFrame 求值，返回 ``f_{name}`` 列（NaN→0）。"""
        tree = self.parse()
        result = _eval_node(tree, df)
        if isinstance(result, (int, float, np.floating)):
            result = pd.Series(float(result), index=df.index)
        # 标量分支处理后，DSL 求值结果必为 pd.Series（Series 或标量两种情形均已归一）
        assert isinstance(result, pd.Series)
        result = result.astype(float).fillna(0.0)
        return result.rename(f"f_{self.name}")
