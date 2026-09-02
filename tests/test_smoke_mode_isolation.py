"""⛔ P1-10（2026-09-01）：冒烟模式必须隔离**每一个**输出路径。

**事故**：``--smoke`` 原本用**手写清单**重定向输出路径，漏了 ``cooldown_file``，
于是冒烟跑完把生产 ``data/paper/cooldown.json`` 覆盖成了
``sig_source="smoke"``、``day=2026-08-24`` 的记录（md5 逐位比对确认）。

**修法**：改为**扫描式** —— 凡值落在输出目录根名下的键一律重定向
（``scripts/paper_trading_main.py::_apply_smoke_redirection``）。
新增落盘配置项自动被覆盖，无需再改清单。

⚠️ **我自己修第一版时又踩了一次**：用 ``"deliverables/"`` 这种**带斜杠**的前缀匹配，
漏掉了配置里写成 ``deliverables``（无斜杠）的 ``reports_dir``，
结果冒烟把 ``deliverables/复盘_2026-08-24.md`` 与
``trade_plans/2026-08-24_plan.json`` 写进了仓库——比手写清单那版还糟。
故本文件的核心用例是**自指式契约**：重定向后配置里不得残留任何仓库内输出路径。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_entry_module():
    """按文件路径加载入口模块（它不在包内，无法 import）。"""
    spec = importlib.util.spec_from_file_location(
        "ptm_smoke_isolation", ROOT / "scripts" / "paper_trading_main.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ptm_smoke_isolation"] = mod
    spec.loader.exec_module(mod)
    return mod


def _real_config(ptm):
    return ptm._load_paper_config(str(ROOT / "configs" / "paper.yaml"))


def test_smoke_redirection_leaves_no_output_path_pointing_into_repo(tmp_path):
    """**核心契约**：重定向后，配置里不得残留任何指向仓库输出目录的路径。

    这是唯一能防住「清单漏项」的写法 —— 它不列举具体键名，而是断言「没有残留」。
    将来新增任何落盘配置项，若扫描器没覆盖到，本用例立刻失败。
    """
    ptm = _load_entry_module()
    cfg = _real_config(ptm)

    before = {k: cfg.get(k) for k in cfg.keys() if ptm._is_smoke_output_path(cfg.get(k))}
    assert before, "扫描器没找到任何输出路径 —— 扫描逻辑失效，本用例将永远通过而失去意义"

    changed = ptm._apply_smoke_redirection(cfg, tmp_path)

    left = {k: cfg.get(k) for k in cfg.keys() if ptm._is_smoke_output_path(cfg.get(k))}
    assert not left, f"⛔ 冒烟未隔离这些输出路径：{left}"
    assert set(changed) == set(before)


def test_smoke_redirection_covers_cooldown_file(tmp_path):
    """P1-10 回归：``cooldown_file`` 是被手写清单漏掉的那一个。"""
    ptm = _load_entry_module()
    cfg = _real_config(ptm)
    changed = ptm._apply_smoke_redirection(cfg, tmp_path)
    assert "cooldown_file" in changed
    assert str(cfg.get("cooldown_file")).startswith(str(tmp_path))


def test_smoke_redirection_covers_reports_and_plans_dirs(tmp_path):
    """回归我第一版修法的坑：``deliverables`` / ``trade_plans`` 是**无斜杠**的目录根。"""
    ptm = _load_entry_module()
    cfg = _real_config(ptm)
    changed = ptm._apply_smoke_redirection(cfg, tmp_path)
    assert "reports_dir" in changed, "deliverables 必须被隔离（带斜杠前缀会漏掉它）"
    assert "plans_dir" in changed, "trade_plans 必须被隔离"
    assert str(cfg.get("reports_dir")).startswith(str(tmp_path))
    assert str(cfg.get("plans_dir")).startswith(str(tmp_path))


def test_smoke_isolation_does_not_touch_readonly_inputs(tmp_path):
    """只读输入（``configs/``）绝不能被重定向 —— 否则启动会找不到风控配置。"""
    ptm = _load_entry_module()
    cfg = _real_config(ptm)
    risk_cfg_before = cfg.get("risk_config")
    signal_cache_before = cfg.get("signal_cache")
    ptm._apply_smoke_redirection(cfg, tmp_path)
    assert cfg.get("risk_config") == risk_cfg_before
    assert cfg.get("signal_cache") == signal_cache_before


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("data/paper/account.json", True),
        ("data/paper", True),
        ("deliverables", True),            # 无斜杠的目录根 —— 第一版漏的就是它
        ("trade_plans", True),
        ("logs/app.log", True),
        ("configs/risk/v4_atr.yaml", False),   # 只读输入
        ("C:/abs/path", False),                # 盘符绝对路径
        ("/abs/path", False),                  # POSIX 绝对路径
        ("./relative", False),
        ("", False),
        (None, False),
        (0.5, False),                          # 非字符串
    ],
)
def test_is_smoke_output_path_classification(value, expected):
    ptm = _load_entry_module()
    assert ptm._is_smoke_output_path(value) is expected
