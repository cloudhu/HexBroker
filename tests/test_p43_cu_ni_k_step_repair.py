"""P2-7：``scripts/p43_cu_ni_k_step_repair.py``（cu0/ni0 k 伪台阶修复器）回归锁定。

**为什么要这个文件**：该脚本**直接写生产主湖**，但此前在 ``tests/`` 与 ``hexbroker/``
下**零引用** —— 全仓 1133 个测试从未覆盖过它。「回归全绿」只证明「改数据没弄坏仓库
其他部分」，**不证明脚本本身正确**（``.workbuddy/memory/MEMORY.md`` 第三节
``⛔ 回归全绿 ≠ 修复脚本被验证过``）。本文件按 QA 建议的 P2-7 三项补上。

夹具与验证点
------------
1. :func:`test_script_safety_no_delete_calls` —— **AST 静态安全审计**（纯静态，
   不执行脚本、不碰生产数据、零 I/O）。断言：
   * 无任何删除类调用（``unlink`` / ``os.remove`` / ``shutil.rmtree`` / ``os.rmdir``
     / ``from os import unlink``）；
   * 唯一的 ``to_parquet`` 目标必须是 tmp，**不得直写原档**；
   * 落盘后必须后随 ``os.replace(tmp, src)``；
   * 附带两条 MEMORY.md 第三节红线：不得出现 ``shutil.move``（Windows 退化为
     ``copy2 + os.unlink``）、不得出现 ``os.rename``（目标已存在时必然失败）。

   ⚠️ **注释/docstring 里有大量 ``unlink`` 字样**（脚本在讲解「为什么不能用 unlink」）。
   审计走 **AST 可执行节点**，天然规避注释；再显式跳过 docstring 节点双重保险。

2. :func:`test_safety_audit_catches_violations_negative_control` —— **审计器自身的
   负向自测**（项目铁律：安全护栏必须做负向测试，否则可能是空转的）。喂进含
   ``os.unlink`` / 直写原档 / 缺 ``os.replace`` 的源码片段，断言审计器**确实报错**。

3. :func:`test_k_segments_tolerance_caliber` —— 固化 **k 段数容差口径**（本轮为此
   吵过一次）。夹具复现真实成因：``round(x, 2)`` 落盘量化在 k 上留下 ~1e-8 级残留，
   导致**同一段在严格容差下裂成多段**。真实实测值（2026-09-02 取证）：

   * cu0 修复段：0821 k=1.475842169 | 0824 k=1.475842091 | 0825~0901 k=1.475842123
     （互差 ≤3.1e-8）
   * ni0 修复段：0820 k=1.225704465 | 0821 k=1.225704489 | 0824 k=1.225704451
     | 0825~0901 k=1.225704463

   文档层面已知事实（**整年 2026 分区**，非取证窗口）：
   **1e-6 口径 cu0 12 段 / ni0 7 段**；**1e-9 口径 cu0 14 段 / ni0 10 段**。
   多出来的段全部来自落盘量化的 ~1e-8 残留，属**正常现象**，不是污染。

4. :func:`test_idempotent_refusal` —— 针对**真实生产现状**的回归锁定：cu0/ni0 主湖
   已于 2026-09-02 13:33 修复完毕，现在跑脚本（默认 dry-run）**必须拒绝**再修。

   ⚠️ **前提**：该用例依赖生产数据当前处于「已修复」状态。若哪天湖被回滚/重污，
   此用例会失败 —— 那不是用例坏了，是**湖的状态变了**。
   ⛔ **红线**：本用例**绝不**传 ``--apply``，不写任何生产文件；并在调用前后校验
   分区 parquet 与台账 sidecar 的 sha256 逐位不变，作为「零写盘」的硬证据。
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "p43_cu_ni_k_step_repair.py"
)


def _load_p43():
    """按项目惯例用 ``spec_from_file_location`` 载入 ``scripts/`` 下的一次性脚本。"""
    spec = importlib.util.spec_from_file_location(
        "p43_cu_ni_k_step_repair", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["p43_cu_ni_k_step_repair"] = mod
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# 1. AST 静态安全审计
# ===========================================================================
#: 属性名命中即判违规（覆盖 ``Path.unlink`` / ``os.unlink`` / ``shutil.rmtree``
#: / ``os.rmdir`` 等一切接收者）
_DELETE_ATTRS = frozenset({"unlink", "rmtree", "rmdir"})
#: 点号全名命中即判违规
_UNSAFE_DOTTED = frozenset({
    "os.remove",      # 直接删原档
    "shutil.remove",
    "shutil.move",    # MEMORY.md 第三节：Windows 退化为 copy2 + os.unlink
    "shutil.rmtree",
    "os.rename",      # G5 要求原子 replace；rename 在目标已存在时必然失败
})
#: ``from os import unlink`` 之类的直接导入也算违规
_UNSAFE_IMPORT_NAMES = frozenset({"unlink", "rmtree", "rmdir", "remove"})
_UNSAFE_IMPORT_MODULES = frozenset({"os", "shutil", "pathlib"})


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
    """docstring 节点（``Expr(Constant(str))``）—— 显式跳过，防注释/文档串误伤。"""
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _enclosing_func(tree: ast.AST, node: ast.AST):
    """返回 ``node`` 所在的**最内层**函数定义（模块级返回 ``None``）。"""
    best = None
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = fn.end_lineno or fn.lineno
            if fn.lineno <= node.lineno <= end:
                if best is None or fn.lineno > best.lineno:
                    best = fn
    return best


def _write_target(call: ast.Call) -> str:
    """取 ``to_parquet`` 的写入目标源码串（位置参数优先，其次 ``path=`` 关键字）。"""
    if call.args:
        return _dotted_name(call.args[0])
    for kw in call.keywords:
        if kw.arg in ("path", "buf", "destination"):
            return _dotted_name(kw.value)
    return ""


def audit_script_safety(source: str) -> list[str]:
    """对修复脚本源码做**静态安全审计**，返回违规描述列表（空列表 = 通过）。

    **纯 AST 层扫描**：注释不会被 parse 成节点，docstring 被显式跳过 —— 该脚本
    正文注释里多处出现 ``unlink`` 字样（在讲解为什么不能用它），若用正则/grep
    不做剥离会产生假阳性。

    审计项：
      1. 无任何删除类调用（``unlink`` / ``os.remove`` / ``shutil.rmtree`` /
         ``os.rmdir`` 及其 ``from ... import`` 形式）；
      2. 不得出现 ``shutil.move`` / ``os.rename``（G5 原子性红线）；
      3. 每个 ``to_parquet`` 的写入目标必须引用 ``tmp``，**不得直写原档**；
      4. 每个 ``to_parquet`` 之后，同一函数内必须存在 ``os.replace(tmp, <原档>)``。
    """
    tree = ast.parse(source)
    violations: list[str] = []
    to_parquet_calls: list[ast.Call] = []

    for node in ast.walk(tree):
        # ---- docstring 显式跳过（AST 已天然规避注释，此为双重保险） ----
        if _is_docstring(node):
            continue

        # ---- 「from os import unlink」这类导入即违规 ----
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in _UNSAFE_IMPORT_MODULES:
                for alias in node.names:
                    if alias.name in _UNSAFE_IMPORT_NAMES:
                        violations.append(
                            f"L{node.lineno}: 导入删除类 API "
                            f"`from {node.module} import {alias.name}` —— 本沙箱 "
                            f"safe-delete 钩子会拦截 unlink，严禁使用")
            continue

        if not isinstance(node, ast.Call):
            continue

        func = node.func
        dotted = _dotted_name(func)

        # ---- 1 & 2：删除类 / 非原子替换类调用 ----
        if isinstance(func, ast.Attribute):
            if func.attr in _DELETE_ATTRS:
                violations.append(
                    f"L{node.lineno}: 删除类调用 `{dotted}()` —— G5 铁律："
                    f"绝不 unlink/rmtree/rmdir 原档（safe-delete 钩子会搬走文件）")
            elif dotted in _UNSAFE_DOTTED:
                violations.append(
                    f"L{node.lineno}: 非安全写盘调用 `{dotted}()` —— G5 要求 "
                    f"`tmp + os.replace(tmp, src)` 原子替换")
            elif func.attr == "remove" and _root_name(func.value) in ("os", "shutil"):
                violations.append(
                    f"L{node.lineno}: 删除类调用 `{dotted}()` —— 严禁删除原档")
        elif isinstance(func, ast.Name) and func.id in _UNSAFE_IMPORT_NAMES:
            if func.id != "remove":  # `remove()` 裸调用太常见（list.remove），不判
                violations.append(
                    f"L{node.lineno}: 删除类调用 `{func.id}()` —— 严禁删除原档")

        # ---- 3：to_parquet 写入目标必须是 tmp ----
        if isinstance(func, ast.Attribute) and func.attr == "to_parquet":
            to_parquet_calls.append(node)
            target = _write_target(node)
            if not target:
                violations.append(
                    f"L{node.lineno}: `to_parquet()` 无法判定写入目标 "
                    f"—— 审计器失效，请人工复核")
            elif "tmp" not in target:
                violations.append(
                    f"L{node.lineno}: `to_parquet({target})` **直写非 tmp 目标** "
                    f"—— G5 要求先落 `.parquet.tmp`，不得直接覆盖原档")

    # ---- 4：每个 to_parquet 后必须紧跟 os.replace(tmp, src) ----
    if not to_parquet_calls:
        violations.append(
            "未找到任何 `to_parquet()` 调用 —— 审计器可能未匹配到写盘路径，"
            "请检查审计逻辑（护栏不能空转）")
    for call in to_parquet_calls:
        fn = _enclosing_func(tree, call)
        scope = fn if fn is not None else tree
        scope_end = (fn.end_lineno or call.lineno) if fn is not None else call.lineno
        replaced = False
        for cand in ast.walk(scope):
            if not isinstance(cand, ast.Call):
                continue
            if not isinstance(cand.func, ast.Attribute):
                continue
            if _dotted_name(cand.func) != "os.replace":
                continue
            if cand.lineno <= call.lineno or cand.lineno > scope_end:
                continue
            if len(cand.args) < 2:
                continue
            if "tmp" in _dotted_name(cand.args[0]):
                replaced = True
                break
        if not replaced:
            scope_name = f"函数 `{fn.name}`" if fn is not None else "模块级"
            violations.append(
                f"L{call.lineno}: {scope_name} 内 `to_parquet()` 之后**未后随 "
                f"`os.replace(tmp, src)`** —— 落盘不是原子的，违反 G5")

    return violations


def test_script_safety_no_delete_calls():
    """AST 静态审计生产修复脚本：无删除类调用 + tmp 落盘 + os.replace 原子替换。"""
    assert SCRIPT_PATH.exists(), f"被测脚本不存在：{SCRIPT_PATH}"
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    violations = audit_script_safety(source)
    assert not violations, (
        "p43_cu_ni_k_step_repair.py 安全审计未通过（该脚本直接写生产主湖，"
        "项目已因 safe-delete 钩子丢过生产文件，以下问题必须清零）：\n  - "
        + "\n  - ".join(violations)
    )

    # 审计器有效性自证：源码里确实存在大量 `unlink` 字样（全在注释/docstring 里），
    # 若审计器不做 AST 剥离，这里必然炸 —— 这个断言同时证明「扫描范围非空」。
    assert source.count("unlink") >= 4, (
        "源码中 `unlink` 字样少于 4 处，审计器可能没扫到讲解性注释；"
        "若脚本被改写，请重新核对 audit_script_safety 的覆盖面")


def test_safety_audit_catches_violations_negative_control():
    """⛔ 负向自测：审计器必须**能抓到**违规，否则上一条用例是空转的。

    项目铁律：安全护栏自身必须做负向测试。逐条喂进 9 类违规源码，
    断言每一类都被审计器判违规；再用标准 G5 写法做正向对照，确认不是「一律报错」。
    """
    bad_snippets: dict[str, str] = {
        "os.unlink 删原档": (
            "import os\n"
            "def g5(df, src):\n"
            "    tmp = src.with_suffix('.parquet.tmp')\n"
            "    df.to_parquet(tmp)\n"
            "    os.unlink(src)\n"
            "    os.replace(tmp, src)\n"
        ),
        "Path.unlink 删原档": (
            "from pathlib import Path\n"
            "import os\n"
            "def g5(df, src):\n"
            "    tmp = src.with_suffix('.parquet.tmp')\n"
            "    df.to_parquet(tmp)\n"
            "    Path(src).unlink()\n"
            "    os.replace(tmp, src)\n"
        ),
        "os.remove 删原档": (
            "import os\n"
            "def g5(df, src):\n"
            "    tmp = src.with_suffix('.parquet.tmp')\n"
            "    df.to_parquet(tmp)\n"
            "    os.remove(src)\n"
            "    os.replace(tmp, src)\n"
        ),
        "shutil.rmtree 删目录": (
            "import os\n"
            "import shutil\n"
            "def g5(df, src):\n"
            "    tmp = src.with_suffix('.parquet.tmp')\n"
            "    df.to_parquet(tmp)\n"
            "    shutil.rmtree(src.parent)\n"
            "    os.replace(tmp, src)\n"
        ),
        "os.rmdir 删目录": (
            "import os\n"
            "def g5(df, src):\n"
            "    tmp = src.with_suffix('.parquet.tmp')\n"
            "    df.to_parquet(tmp)\n"
            "    os.rmdir(src.parent)\n"
            "    os.replace(tmp, src)\n"
        ),
        "to_parquet 直写原档": (
            "import os\n"
            "def g5(df, src):\n"
            "    df.to_parquet(src)\n"
            "    os.replace(src, src)\n"
        ),
        "落 tmp 但缺 os.replace": (
            "def g5(df, src):\n"
            "    tmp = src.with_suffix('.parquet.tmp')\n"
            "    df.to_parquet(tmp)\n"
        ),
        "用 shutil.move 替代 os.replace": (
            "import shutil\n"
            "def g5(df, src):\n"
            "    tmp = src.with_suffix('.parquet.tmp')\n"
            "    df.to_parquet(tmp)\n"
            "    shutil.move(str(tmp), str(src))\n"
        ),
        "用 os.rename 替代 os.replace": (
            "import os\n"
            "def g5(df, src):\n"
            "    tmp = src.with_suffix('.parquet.tmp')\n"
            "    df.to_parquet(tmp)\n"
            "    os.rename(tmp, src)\n"
        ),
        "from os import unlink 导入即违规": (
            "import os\n"
            "from os import unlink\n"
            "def g5(df, src):\n"
            "    tmp = src.with_suffix('.parquet.tmp')\n"
            "    df.to_parquet(tmp)\n"
            "    os.replace(tmp, src)\n"
        ),
    }

    for label, snippet in bad_snippets.items():
        violations = audit_script_safety(snippet)
        assert violations, (
            f"负向自测失败：审计器**没有**判出违规用例「{label}」—— "
            f"说明 test_script_safety_no_delete_calls 是空转的护栏，"
            f"必须修 audit_script_safety。\n源码：\n{snippet}")

    # 正向对照：标准 G5 写法必须**通过**，确认审计器不是「一律报错」的假阳性机器
    good = (
        "import os\n"
        "def g5_atomic_write_parquet(df, src):\n"
        "    tmp = src.with_suffix('.parquet.tmp')\n"
        "    if tmp.exists():\n"
        "        os.replace(str(tmp), str(tmp.with_suffix('.parquet.stale')))\n"
        "    df.to_parquet(tmp)\n"
        "    os.replace(tmp, src)\n"
    )
    assert audit_script_safety(good) == [], (
        "正向对照失败：标准 G5 写法被误判为违规（假阳性）—— 护栏过严会让后人"
        "直接删掉这条测试，必须修 audit_script_safety。")


# ===========================================================================
# 2. k 段数容差口径
# ===========================================================================
#: 取证窗口（与脚本 G4 一致）：2026-08-14 ~ 2026-09-01，共 13 个交易日
_WINDOW_DATES = pd.bdate_range("2026-08-14", "2026-09-01")

#: cu0 修复后实测 k（2026-09-02 取证报告 §1.3）
CU_K_BEFORE = 1.472692          # 换月前段（0814~0820，5 个交易日）
CU_K_0821 = 1.475842169         # 换月日
CU_K_0824 = 1.475842091         # 与 0821 差 -7.8e-8（相对 5.3e-8）
CU_K_REST = 1.475842123         # 0825~0901（6 个交易日），与 0824 差 +3.2e-8
#: ni0 修复后实测 k
NI_K_BEFORE = 1.228266          # 换月前段（0814~0819，4 个交易日）
NI_K_0820 = 1.225704465         # 换月日
NI_K_0821 = 1.225704489         # 与 0820 差 +2.4e-8
NI_K_0824 = 1.225704451         # 与 0821 差 -3.8e-8
NI_K_REST = 1.225704463         # 0825~0901（6 个交易日），与 0824 差 +1.2e-8

#: 段内 1e-8 级「裂片」个数（= 严格口径下同一段被量化残留切成的碎片数）
CU_N_FRAGMENTS = 3
NI_N_FRAGMENTS = 4


def _build_k_series(sym0: str) -> pd.Series:
    """按取证窗口重建实测 k 序列（复现 ``round(x, 2)`` 落盘量化的 ~1e-8 残留）。"""
    dates = _WINDOW_DATES
    assert len(dates) == 13, f"取证窗口应为 13 个交易日，实际 {len(dates)}"
    if sym0 == "cu0":
        assert str(dates[5].date()) == "2026-08-21", dates[5]
        assert str(dates[6].date()) == "2026-08-24", dates[6]
        vals = ([CU_K_BEFORE] * 5              # 0814~0820
                + [CU_K_0821]                  # 0821
                + [CU_K_0824]                  # 0824
                + [CU_K_REST] * 6)             # 0825~0901
    elif sym0 == "ni0":
        assert str(dates[4].date()) == "2026-08-20", dates[4]
        assert str(dates[5].date()) == "2026-08-21", dates[5]
        assert str(dates[6].date()) == "2026-08-24", dates[6]
        vals = ([NI_K_BEFORE] * 4              # 0814~0819
                + [NI_K_0820]                  # 0820
                + [NI_K_0821]                  # 0821
                + [NI_K_0824]                  # 0824
                + [NI_K_REST] * 6)             # 0825~0901
    else:
        raise AssertionError(f"未知品种：{sym0}")
    assert len(vals) == len(dates), (len(vals), len(dates))
    return pd.Series(vals, index=pd.DatetimeIndex(dates), name="k")


def test_k_segments_tolerance_caliber():
    """固化 k 段数容差口径：1e-6 与 1e-9 的段数差 = 「1e-8 级裂片数 − 1」。

    本轮（2026-09-02）为「整年分区到底 12 段还是 14 段」吵过一次。结论：
    多出来的段全部来自 ``round(x, 2)`` 落盘量化在 k 上留下的 ~1e-8 级残留，
    **属正常现象、不是污染**。口径一旦漂移，G4「窗口内恰好 2 段」的断言
    与整年段数统计都会跟着变，故必须写死。

    文档事实（**整年 2026 分区**，非本用例的取证窗口）：
    **1e-6 口径 cu0 12 段 / ni0 7 段**；**1e-9 口径 cu0 14 段 / ni0 10 段**。
    """
    mod = _load_p43()

    for sym0, n_frag, want_before, want_after, want_switch in (
        ("cu0", CU_N_FRAGMENTS, CU_K_BEFORE, mod.EXPECTED_K_AFTER["cu0"],
         mod.EXPECTED_K_SWITCH_DATE["cu0"]),
        ("ni0", NI_N_FRAGMENTS, NI_K_BEFORE, mod.EXPECTED_K_AFTER["ni0"],
         mod.EXPECTED_K_SWITCH_DATE["ni0"]),
    ):
        k = _build_k_series(sym0)

        loose = mod._k_segments(k, tol=mod.K_SEGMENT_TOL)   # 生产口径 1e-6
        strict = mod._k_segments(k, tol=1e-9)               # 严格口径

        assert len(strict) > len(loose), (
            f"{sym0}: 严格口径段数 ({len(strict)}) 未多于生产口径 ({len(loose)}) "
            f"—— 夹具没复现出 1e-8 级裂片，请核对实测 k 值是否仍为修复后真值"
            f"（湖是否仍处于已修复态？）")

        assert len(strict) - len(loose) == n_frag - 1, (
            f"{sym0}: 段数差 {len(strict) - len(loose)} != 1e-8 级裂片数 "
            f"{n_frag} − 1 = {n_frag - 1}（got loose={len(loose)} "
            f"strict={len(strict)}）—— 容差口径已漂移，整年段数统计会跟着变")

        # 生产口径下必须恰好两段：前段 = 换月前真值，后段 = 修复后期望值，
        # 且切换日与报告一致（这正是 G4 依赖的性质）
        assert len(loose) == 2, (
            f"{sym0}: 生产口径 1e-6 下取证窗口应有 2 段，实际 {len(loose)} 段 "
            f"→ {loose}")
        assert abs(loose[0]["median"] - want_before) <= mod.FLOAT_TOL, (
            f"{sym0}: 前段 k={loose[0]['median']:.9f} != 换月前真值 {want_before}")
        assert abs(loose[1]["median"] - want_after) <= mod.FLOAT_TOL, (
            f"{sym0}: 后段 k={loose[1]['median']:.9f} != 报告期望 {want_after} "
            f"（容差 {mod.FLOAT_TOL:g}）")
        assert loose[1]["start"] == want_switch, (
            f"{sym0}: k 切换日 {loose[1]['start']} != 报告期望 {want_switch}")

        # 严格口径下窗口首尾仍被完整覆盖 —— 段数变多不代表边界漂移
        assert strict[0]["start"] == loose[0]["start"], (
            f"{sym0}: 严格口径首段起点漂移 {strict[0]['start']} != "
            f"{loose[0]['start']}")
        assert strict[-1]["end"] == loose[-1]["end"], (
            f"{sym0}: 严格口径末段终点漂移 {strict[-1]['end']} != "
            f"{loose[-1]['end']}")
        # 严格口径下每段内部仍必须恒定（残留是 1e-8 级，段内 max_dev 应远小于 1e-6）
        for seg in strict:
            assert seg["max_dev"] <= mod.FLOAT_TOL, (
                f"{sym0}: 严格口径段 {seg['start']}~{seg['end']} 段内不恒定 "
                f"（max_dev={seg['max_dev']:.3e} > {mod.FLOAT_TOL:g}）")


# ===========================================================================
# 3. 幂等拒修（真实生产现状回归锁定）
# ===========================================================================
def test_idempotent_refusal(capsys):
    """生产主湖已修复 → dry-run 必须拒修（rc=3）且**零写盘**。

    ⚠️ **用例前提**：生产主湖 ``data/raw/processed/{cu0,ni0}/1d/2026.parquet``
    当前处于「已于 2026-09-02 13:33 修复完毕」状态。若湖被回滚到污染态或重新
    污染，本用例会失败 —— 那说明**湖的状态变了**，不是用例坏了，请先确认湖的
    状态再判断。

    ⛔ **红线**：本用例**绝不**传 ``--apply``，绝不写任何生产文件。调用前后会
    校验分区 parquet 与台账 sidecar 的 sha256 逐位不变，作为「零写盘」硬证据。
    """
    mod = _load_p43()
    data_root = mod.DEFAULT_DATA_ROOT

    paths = {sym0: mod._partition_path(data_root, sym0, 2026)
             for sym0 in ("cu0", "ni0")}
    missing = [str(p) for p in paths.values() if not p.exists()]
    if missing:
        pytest.skip(f"生产分区缺失，跳过幂等拒修用例：{missing}")

    sidecar = data_root / "processed" / mod.SIDECAR_NAME
    watched = dict(paths)
    if sidecar.exists():
        watched["sidecar"] = sidecar

    before = {key: mod.sha256_of(p) for key, p in watched.items()}

    # ⛔ 绝不传 --apply：默认 dry-run，G1 应在第一行就 assert 失败 → rc=3
    rc = mod.main([])

    after = {key: mod.sha256_of(p) for key, p in watched.items()}
    out = capsys.readouterr().out

    assert rc == 3, (
        f"dry-run 返回码 got={rc} want=3：脚本应当因 G1（旧值断言失败）拒绝重复"
        f"修复。\n"
        f"  * 若 got=0：说明湖里目标行的值**又变回了**污染值（湖被回滚/重污？），"
        f"请先确认 data/raw/processed/cu0|ni0/1d/2026.parquet 是否仍处于已修复态；\n"
        f"  * 若 got=2：--symbols 参数解析异常；\n"
        f"  * 若 got=4：非断言类异常（如分区读失败）；\n"
        f"  * 若 got=5/6：写盘阶段异常 —— ⛔ 绝不应发生（dry-run 不得写盘）。\n"
        f"stdout 尾部：\n{out[-2000:]}")

    # 「拒绝」必须来自 G1 旧值断言，而不是别的意外
    assert "[ABORT]" in out and "G1 失败" in out, (
        f"rc=3 但拒绝原因不是 G1 旧值断言失败 —— 可能是别的护栏在拦，"
        f"用例语义被绕过。stdout 尾部：\n{out[-2000:]}")

    assert before == after, (
        "⛔ dry-run 竟然改动了生产文件！sha256 前后不一致："
        + "".join(
            f"\n  {key}: {p}\n    before={before[key]}\n    after ={after[key]}"
            for key, p in watched.items() if before[key] != after[key]
        )
        + "\n⛔ 立即核查 data/raw/processed/ 与 _RAW_CLOSE_REPAIRS.json，"
          "必要时用 artifacts/_p2_backup_20260902/ 回滚。")
