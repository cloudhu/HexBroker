"""静态安全审计：删除类调用红线（P2-7 审计器的基建推广版，2026-09-02）。

背景
----
沙箱 safe-delete 钩子会拦截 ``unlink`` / ``os.remove`` 等删除调用并路由至回收站，
项目已因此丢过生产文件（``.git/objects`` 事故）。凡写生产数据的代码必须走
``tmp + os.replace`` 原子替换（G5 铁律），**不得出现任何删除类调用**。

本模块把 ``tests/test_p43_cu_ni_k_step_repair.py`` 审计器中**文件无关**的红线
（删除类调用 / 非原子替换调用 / 危险导入）抽为公共审计器，供基建文件
（``hexbroker/utils/``）的回归扫描使用；p43 专属的 ``to_parquet`` 目标与
``os.replace`` 后随检查仍留在原测试文件（那是修复脚本专属语义）。

纯 AST 层扫描：注释不会被 parse 成节点，docstring 被显式跳过 ——
讲解性注释里的 ``unlink`` 字样不会产生假阳性。
"""

from __future__ import annotations

import ast

#: 属性名命中即判违规（覆盖 ``Path.unlink`` / ``os.unlink`` / ``shutil.rmtree``
#: / ``os.rmdir`` 等一切接收者）
DELETE_ATTRS = frozenset({"unlink", "rmtree", "rmdir"})

#: 点号全名命中即判违规
UNSAFE_DOTTED = frozenset({
    "os.remove",
    "shutil.remove",
    "shutil.move",    # Windows 退化为 copy2 + os.unlink，非原子且触发 safe-delete
    "shutil.rmtree",
    "os.rename",      # G5 要求原子 replace；rename 在目标已存在时必然失败
})

#: ``from os import unlink`` 之类的直接导入也算违规
UNSAFE_IMPORT_NAMES = frozenset({"unlink", "rmtree", "rmdir", "remove"})
UNSAFE_IMPORT_MODULES = frozenset({"os", "shutil", "pathlib"})


def _dotted_name(node: ast.AST) -> str:
    """把 ``a.b.c`` 还原成点号串（无法还原时返回空串）。"""
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001  （极端 node 无法 unparse，退回空串）
        return ""


def _root_name(node: ast.AST) -> str:
    """取 ``a.b.c`` 的最左侧标识符（``os.replace`` → ``os``）。"""
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _is_docstring(node: ast.AST) -> bool:
    """docstring 节点（``Expr(Constant(str))``）—— 显式跳过，防文档串误伤。"""
    return (
        isinstance(node, ast.Expr)
        and isinstance(node, ast.Constant)
        and isinstance(node.value.value, str)
    )


def audit_no_delete_calls(source: str) -> list[str]:
    """对源码做**删除类调用**静态审计，返回违规描述列表（空列表 = 通过）。

    覆盖：
      1. 属性名为 ``unlink`` / ``rmtree`` / ``rmdir`` 的调用（任意接收者）；
      2. 点号全名 ``os.remove`` / ``shutil.move`` / ``os.rename`` / ``shutil.rmtree``；
      3. ``from os import unlink`` 式危险导入；
      4. ``os.remove(...)`` 的别名形态（属性名 ``remove`` 且根名为 os/shutil）。

    注意：裸 ``remove()`` 调用（如 ``list.remove``）**不判** —— 太常见，会误伤。
    """
    tree = ast.parse(source)
    violations: list[str] = []

    for node in ast.walk(tree):
        # ---- docstring 显式跳过（AST 已天然规避注释，此为双重保险） ----
        if _is_docstring(node):
            continue

        # ---- 「from os import unlink」这类导入即违规 ----
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in UNSAFE_IMPORT_MODULES:
                for alias in node.names:
                    if alias.name in UNSAFE_IMPORT_NAMES:
                        violations.append(
                            f"L{node.lineno}: 导入删除类 API "
                            f"`from {node.module} import {alias.name}` —— 本沙箱 "
                            f"safe-delete 钩子会拦截 unlink，严禁使用")
            continue

        if not isinstance(node, ast.Call):
            continue

        func = node.func
        dotted = _dotted_name(func)

        if isinstance(func, ast.Attribute):
            if func.attr in DELETE_ATTRS:
                violations.append(
                    f"L{node.lineno}: 删除类调用 `{dotted}()` —— G5 铁律："
                    f"绝不 unlink/rmtree/rmdir（safe-delete 钩子会搬走文件），"
                    f"写盘走 tmp + os.replace 原子替换")
            elif dotted in UNSAFE_DOTTED:
                violations.append(
                    f"L{node.lineno}: 非安全调用 `{dotted}()` —— G5 要求 "
                    f"`tmp + os.replace` 原子替换")
            elif func.attr == "remove" and _root_name(func.value) in ("os", "shutil"):
                violations.append(
                    f"L{node.lineno}: 删除类调用 `{dotted}()` —— 严禁删除文件")
        elif isinstance(func, ast.Name) and func.id in UNSAFE_IMPORT_NAMES:
            if func.id != "remove":  # `remove()` 裸调用太常见（list.remove），不判
                violations.append(
                    f"L{node.lineno}: 删除类调用 `{func.id}()` —— 严禁删除文件")

    return violations
