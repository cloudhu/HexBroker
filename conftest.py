"""pytest 根配置：确保项目根目录在 sys.path，便于 ``import hexbroker``。

🚨 **生产数据护栏（2026-08-29 新增）**
------------------------------------
背景（真实事故）：``data/raw/`` 等生产数据目录被 ``.gitignore`` 忽略，
``git status`` **看不见**任何改动，因此"跑完测试后检查 git 状态"这道工序
对这些目录**完全无效**。2026-08-29 实测：跑一次全量 pytest 就把 3 个生产
parquet 覆盖成了测试夹具数据（``rb0/1d/2020.parquet`` 全年只剩 2 行合成数据、
``cu0/1d/2026.parquet`` 被写入 rb0 的数值造成跨品种串扰）。

根因链：``AkshareSource(save=True)`` 为默认 → 测试未传 ``save=False`` →
``DataLake.save_processed`` 用 ``write_parquet`` **整文件覆盖**（非 merge）。

护栏策略：
1. **默认隔离**：所有测试自动把数据湖根目录重定向到临时目录（autouse）。
2. **显式 opt-in**：确需读写真实生产数据的测试，用
   ``@pytest.mark.usefixtures("real_data_lake")`` 显式声明，便于审计。
3. **兜底哨兵**：即使隔离失效，对生产目录的**写操作**直接抛错 —— 宁可测试失败，
   也不允许静默污染（这些数据无法用 git 恢复）。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 受保护的生产数据目录（相对仓库根）。写入这些路径一律拦截。
PROTECTED_DIRS = (
    ROOT / "data",
    ROOT / "artifacts",
)

#: 允许写入的例外根目录。
_ALLOWED_ROOTS = (
    pathlib.Path(tempfile.gettempdir()).resolve(),
    (ROOT / ".pytest_tmp").resolve(),
)


def _is_under(path: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(parent)
    except (ValueError, OSError):
        return False
    return True


def _is_protected(target) -> pathlib.Path | None:
    try:
        p = target if isinstance(target, pathlib.Path) else pathlib.Path(str(target))
        p = p.resolve()
    except (OSError, TypeError, ValueError):
        return None
    for allowed in _ALLOWED_ROOTS:
        if _is_under(p, allowed):
            return None
    for d in PROTECTED_DIRS:
        if _is_under(p, d.resolve()):
            return d
    return None


def _opted_out(request) -> bool:
    """测试是否显式声明要访问真实生产数据。"""
    return ("real_data_lake" in request.keywords
            or "real_data_lake" in request.fixturenames)


# ---------------------------------------------------------------------------
# 1) 默认隔离：把 DataLake 默认 root 重定向到 tmp
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_data_lake(tmp_path, monkeypatch, request):
    """把所有测试的默认数据湖根目录重定向到临时目录。

    需要访问真实生产数据的测试请用 ``real_data_lake`` fixture 显式 opt-in。
    """
    if _opted_out(request):
        return
    sandbox = tmp_path / "_datalake"
    (sandbox / "raw" / "processed").mkdir(parents=True, exist_ok=True)

    from hexbroker.data import store

    orig_init = store.DataLake.__init__

    def _patched_init(self, root=None, *args, **kwargs):
        # 注意：源层常常显式传字符串默认值 "data/raw"（而非 None），
        # 只判 None 会漏掉这类情况 —— 改为"凡解析到生产目录的一律改道"。
        if root is None or _is_protected(root) is not None:
            root = str(sandbox)
        orig_init(self, root, *args, **kwargs)

    monkeypatch.setattr(store.DataLake, "__init__", _patched_init)


@pytest.fixture
def real_data_lake():
    """显式声明：本测试需要读写真实生产数据目录。

    使用此 fixture 的测试**必须**自行保证不破坏生产数据，且集中可审计。
    """
    return None


# ---------------------------------------------------------------------------
# 2) 兜底哨兵：拦截对生产目录的一切写操作
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _guard_production_writes(monkeypatch, request):
    """拦截写入 ``data/`` 与 ``artifacts/`` 的文件操作。

    触发拦截说明测试在生产目录落盘 —— 应改写为 ``tmp_path``。
    """
    if _opted_out(request):
        return

    from hexbroker.data import store as _store

    def _make(orig_fn, name):
        def _wrapped(*args, **kwargs):
            for cand in list(args) + list(kwargs.values()):
                hit = _is_protected(cand)
                if hit is not None:
                    raise RuntimeError(
                        f"[护栏] 测试试图写入生产目录 {hit}（{name}）：{cand!r}。"
                        "生产数据被 .gitignore 忽略、无法用 git 恢复。"
                        "请改用 tmp_path，或用 "
                        '@pytest.mark.usefixtures("real_data_lake") '
                        "显式声明并自行负责。"
                    )
            return orig_fn(*args, **kwargs)

        return _wrapped

    for fname in ("write_parquet", "write_manifest"):
        orig = getattr(_store, fname, None)
        if orig is not None:
            monkeypatch.setattr(_store, fname, _make(orig, fname))
