"""P2-8：``hexbroker/utils/io.py::_atomic_write`` 异常路径不得删除 tmp（2026-09-02）。

**为什么**：原实现写盘抛异常时用 ``os.remove(tmp)`` 清理临时文件 —— 项目明令
禁止的删除类调用（沙箱 safe-delete 钩子会拦截 unlink/remove 并路由至回收站；
G5 铁律：绝不删除，写盘走 ``tmp + os.replace`` 原子替换）。P2-7 的 AST 审计器
原本只扫 ``scripts/p43_*`` 修复脚本，本轮把其中**文件无关的红线**抽为基建共享
审计器 ``hexbroker/utils/safety_audit.py::audit_no_delete_calls``，并推广到
``hexbroker/utils/`` 全部基建文件的回归扫描。

覆盖
----
1. :func:`test_utils_infra_files_no_delete_calls` —— 参数化扫描
   ``hexbroker/utils/*.py`` 全部文件：零删除类调用。
2. :func:`test_hexbroker_package_no_unexpected_delete_calls` —— **P2-8c 方案①**
   （主理人裁决 2026-09-03）：``hexbroker/`` 全包扩扫，豁免清单只登记
   ``rebuild.py`` / ``health_check.py`` 两处设计内 marker/PID 生命周期清理。
3. :func:`test_io_py_still_atomic` —— io.py 必须保留 ``os.replace(tmp, path)``
   原子替换主路径（证明扫描范围非空、护栏不空转）。
4. :func:`test_audit_no_delete_calls_negative_control` —— 审计器负向自测
   （项目铁律：护栏必须能抓到违规，否则是空转的）。
5. :func:`test_atomic_write_failure_keeps_tmp_and_original` —— **P2-8 核心行为**：
   写盘失败 → 异常向上传播、原档逐字节不变、tmp **保留**（而非被删）。
6. :func:`test_atomic_write_success_replaces_target` —— 正常路径回归：
   写盘成功 → 目标被原子替换、无 tmp 残留。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hexbroker.utils import io as io_mod
from hexbroker.utils.safety_audit import audit_no_delete_calls

UTILS_DIR = Path(io_mod.__file__).resolve().parent
PACKAGE_DIR = UTILS_DIR.parent

#: P2-8c 方案①豁免清单（主理人裁决 2026-09-03，取证见 deliverables
#: p2_8_io_safety_fix_20260902.md §8.1）—— 两处设计内 marker/PID 生命周期清理：
#: - data/rebuild.py:141        clear_provisional：sidecar 全部年份清空后删空文件
#: - data/rebuild.py:359        分区按真值重建后消费 _MISSING_{year}.json 标记
#: - diagnostics/health_check.py:154  僵尸 PID 锁文件清理（删不删功能等价）
#: 按**文件**粒度豁免：文件内新增删除调用不再红，属已知取舍（避免逐行豁免的
#: 脆弱性）；新增豁免必须先过主理人裁决并在本清单登记语义。
AUDIT_PACKAGE_EXEMPT = frozenset({
    "data/rebuild.py",
    "diagnostics/health_check.py",
})


# ===========================================================================
# 1. 基建文件红线扫描（参数化：hexbroker/utils/*.py 全量）
# ===========================================================================
def _utils_py_files():
    return sorted(UTILS_DIR.glob("*.py"))


@pytest.mark.parametrize("py", _utils_py_files(), ids=lambda p: p.name)
def test_utils_infra_files_no_delete_calls(py: Path):
    """``hexbroker/utils/`` 基建文件零删除类调用（P2-8 修复后的回归锁定）。

    新增 utils 文件会被自动纳入扫描 —— 这是刻意的 fail-closed 设计：
    未来谁在基建里写 ``os.remove``，回归直接红，而不是等 safe-delete 钩子
    把生产文件搬进回收站才被发现。
    """
    source = py.read_text(encoding="utf-8")
    violations = audit_no_delete_calls(source)
    assert not violations, (
        f"基建文件 {py.name} 出现删除类调用（沙箱 safe-delete 钩子会拦截删除"
        f"并路由至回收站，项目已因此丢过生产文件；写盘必须走 "
        f"tmp + os.replace 原子替换）：\n  - " + "\n  - ".join(violations)
    )


# ===========================================================================
# 1b. 包级红线扩扫（P2-8c 方案①：hexbroker/ 全包，豁免清单制）
# ===========================================================================
def test_hexbroker_package_no_unexpected_delete_calls():
    """``hexbroker/`` 全包扫描：删除类调用只允许出现在豁免清单内的文件。

    - utils/ 已由参数化用例零容忍覆盖，本用例负责其余全部包内文件；
    - ``third_party/``（vendored）不在扫描范围（repo 根目录，天然排除）；
    - 豁免清单按文件粒度，语义登记见 AUDIT_PACKAGE_EXEMPT 注释；
    - 新增文件/新增删除调用未登记 → 直接红（fail-closed）。
    """
    offenders: list[str] = []
    scanned = 0
    for py in sorted(PACKAGE_DIR.rglob("*.py")):
        rel = py.relative_to(PACKAGE_DIR).as_posix()
        scanned += 1
        violations = audit_no_delete_calls(py.read_text(encoding="utf-8"))
        if violations and rel not in AUDIT_PACKAGE_EXEMPT:
            offenders.append(f"{rel}:\n    " + "\n    ".join(violations))
    assert scanned > 100, f"包级扫描范围异常（仅扫到 {scanned} 文件），护栏可能空转"
    assert not offenders, (
        "hexbroker/ 包内出现未豁免的删除类调用（登记豁免需主理人裁决，"
        "写盘走 tmp + os.replace 原子替换）：\n  - " + "\n  - ".join(offenders)
    )


def test_io_py_still_atomic():
    """io.py 必须保留 ``os.replace(tmp, path)`` 主路径 —— 护栏不空转自证。"""
    src = (UTILS_DIR / "io.py").read_text(encoding="utf-8")
    assert "os.replace(tmp, path)" in src, (
        "io.py 的原子替换主路径丢失 —— _atomic_write 已不再是原子写，"
        "必须立即人工复核")


# ===========================================================================
# 2. 审计器负向自测
# ===========================================================================
def test_audit_no_delete_calls_negative_control():
    """审计器必须**能抓到**违规，否则参数化扫描是空转的护栏。

    5 类违规片段全部命中 + 注释/docstring 不误伤 + ``os.replace`` 正向对照通过
    （证明不是「一律报错」的假阳性机器）。
    """
    bad_snippets = {
        "os.remove": "import os\ndef f(p):\n    os.remove(p)\n",
        "Path.unlink": "from pathlib import Path\ndef f(p):\n    Path(p).unlink()\n",
        "shutil.move": "import shutil\ndef f(a, b):\n    shutil.move(a, b)\n",
        "os.rename": "import os\ndef f(a, b):\n    os.rename(a, b)\n",
        "from os import unlink": (
            "from os import unlink\ndef f(p):\n    unlink(p)\n"
        ),
    }
    for label, snippet in bad_snippets.items():
        assert audit_no_delete_calls(snippet), (
            f"负向自测失败：审计器**没有**抓到「{label}」—— "
            f"test_utils_infra_files_no_delete_calls 是空转的护栏")

    # 注释里的 unlink 字样不算违规（AST 天然规避注释）
    commented = "def f():\n    # 这里讲解为什么不能用 unlink\n    return 0\n"
    assert audit_no_delete_calls(commented) == []
    # docstring 里的 unlink 字样不算违规（显式跳过）
    docstring = 'def f():\n    """为什么不能用 unlink 的说明。"""\n    return 0\n'
    assert audit_no_delete_calls(docstring) == []
    # list.remove 裸调用不判（太常见）
    list_remove = "def f(xs):\n    xs.remove(1)\n"
    assert audit_no_delete_calls(list_remove) == []
    # 正向对照：os.replace 是合规原子替换
    good = "import os\ndef f(tmp, dst):\n    os.replace(tmp, dst)\n"
    assert audit_no_delete_calls(good) == []


# ===========================================================================
# 3. _atomic_write 行为测试
# ===========================================================================
def test_atomic_write_failure_keeps_tmp_and_original(tmp_path):
    """P2-8 核心行为：写盘异常 → 异常传播 + 原档逐字节不变 + tmp **保留**不删。

    旧实现会在 except 里 ``os.remove(tmp)`` —— 本用例若回退到旧行为，
    「tmp 保留」断言必红。
    """
    target = tmp_path / "acct.json"
    target.write_text("ORIGINAL", encoding="utf-8")

    def _boom(p: str) -> None:
        Path(p).write_text("HALF_WRITTEN", encoding="utf-8")  # 模拟写了一半
        raise RuntimeError("模拟写盘失败（磁盘满）")

    with pytest.raises(RuntimeError, match="模拟写盘失败"):
        io_mod._atomic_write(_boom, target)

    # 原档逐字节不变（os.replace 未执行）
    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    # ⛔ 关键断言：tmp 被保留（旧实现删 tmp → 此处必红）
    leftovers = sorted(tmp_path.glob("*.tmp"))
    assert len(leftovers) == 1, (
        f"写盘失败后 tmp 应保留供排查（P2-8），实际残留 {leftovers} —— "
        f"疑似回退到了 os.remove(tmp) 的旧行为")
    assert leftovers[0].read_text(encoding="utf-8") == "HALF_WRITTEN"


def test_atomic_write_success_replaces_target(tmp_path):
    """正常路径回归：写盘成功 → 目标被原子替换、无 tmp 残留。"""
    target = tmp_path / "acct.json"
    target.write_text("OLD", encoding="utf-8")

    io_mod._atomic_write(
        lambda p: Path(p).write_text("NEW", encoding="utf-8"), target)

    assert target.read_text(encoding="utf-8") == "NEW"
    assert list(tmp_path.glob("*.tmp")) == []  # tmp 已被 os.replace 消费


# ===========================================================================
# 4. QA R26 🟡 回归：模块别名不得绕过审计
# ===========================================================================
def test_audit_catches_module_alias_bypass():
    """``import os as o; o.remove(p)`` 必须被抓（QA R26 实测 bypass 形态之一）。

    QA 16 形态攻击矩阵中「模块别名」是唯一**无辜像样**的绕过形态
    （变量别名/getattr/importlib/eval 等动态形态已写明不在威胁模型内）。
    修法：Pass 1 收集 ``import <模块> as <别名>`` 别名表，Pass 2 根名解析后判定。
    """
    assert audit_no_delete_calls("import os as o\ndef f(p):\n    o.remove(p)\n"), (
        "模块别名 os→o 的 o.remove() 未被抓 —— QA R26 🟡 回归失败")
    assert audit_no_delete_calls(
        "import shutil as s\ndef f(a, b):\n    s.move(a, b)\n"), (
        "模块别名 shutil→s 的 s.move() 未被抓")
    assert audit_no_delete_calls(
        "import os as o\ndef f(a, b):\n    o.rename(a, b)\n"), (
        "模块别名 os→o 的 o.rename() 未被抓")
    # 无别名的正常 os 用法不受影响（别名表为空 → 行为与修复前一致）
    assert audit_no_delete_calls(
        "import os\ndef f(tmp, dst):\n    os.replace(tmp, dst)\n") == []
