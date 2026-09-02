"""P1-a 防再发：行情拉取失败不得静默降级为「属预期」。

背景（2026-08-28 停摆终裁，见 32-stall-root-cause-final.md）：
Pandadata 网关 500009「单日总流量超限」→ 18/18 拉取失败 → 目录 0 个 json，
而 ``p6_4_apply_persisted_dir.py`` 把「目录无 json」一律当「非交易日/无新数据」
安全返回 exit 0 —— **数据源中断与休市不可区分**，失败被静默吞掉，最终表现为
「系统照常运行却不交易」。

本测试锁定：调用方显式声明交易日（``--trading-day``）时，0 落盘必须判定为
**拉取失败**（醒目横幅 + exit 3）；未声明时维持旧的安全返回语义（向后兼容）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "p6_4_apply_persisted_dir.py"


def _load():
    spec = importlib.util.spec_from_file_location("p64apply", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


# ---- 核心门禁 ----
def test_trading_day_with_zero_json_is_hard_failure(mod, tmp_path: Path, monkeypatch, capsys):
    """交易日 + 0 落盘 = 拉取失败（exit 3 + 醒目横幅），不得静默返回 0。"""
    monkeypatch.setattr("sys.argv", ["p6_4", "--dir", str(tmp_path), "--trading-day"])
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 3, f"拉取失败必须 exit 3，实际 {rc}"
    assert "行情拉取失败" in out and "不会延长" in out
    assert "500009" in out          # 常见原因提示，便于运维秒判


def test_without_trading_day_keeps_legacy_soft_return(mod, tmp_path: Path, monkeypatch, capsys):
    """未声明交易日 → 维持旧语义（WARN + exit 0），保证非交易日/无新数据不受影响。"""
    monkeypatch.setattr("sys.argv", ["p6_4", "--dir", str(tmp_path)])
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "属预期" in out and "行情拉取失败" not in out


def test_missing_dir_still_exit_2(mod, tmp_path: Path, monkeypatch):
    """目录不存在 → exit 2（优先级高于交易日判定）。"""
    monkeypatch.setattr("sys.argv", ["p6_4", "--dir", str(tmp_path / "nope"), "--trading-day"])
    assert mod.main() == 2


def test_expect_only_affects_message(mod, tmp_path: Path, monkeypatch, capsys):
    """--expect 仅影响提示文案，不改变判定。"""
    monkeypatch.setattr(
        "sys.argv", ["p6_4", "--dir", str(tmp_path), "--trading-day", "--expect", "6"]
    )
    assert mod.main() == 3
    assert "期望 6 个" in capsys.readouterr().out


# ---- Part A 接线：_*.json 元数据必须被排除（2026-09-02） ----
def test_persisted_json_listing_excludes_metadata(tmp_path: Path):
    """_list_persisted_json 排除 _*.json（_SEAM_STATUS.json）与 *.clean.json，
    只留品种文件——否则 sym0="_SEAM_STATUS" 走 parse 整批报错。"""
    d = tmp_path
    names = ["ag0.json", "rb0.json", "_SEAM_STATUS.json",
             "_LOCAL_REPORT.json", "ag0.clean.json"]
    for n in names:
        (d / n).write_text("{}", encoding="utf-8")
    got = [p.name for p in mod_module_list(d)]
    assert got == ["ag0.json", "rb0.json"]


def mod_module_list(d: Path):
    """独立加载脚本模块取 _list_persisted_json（与 module fixture 解耦）。"""
    return _load()._list_persisted_json(d)


# ---- 口径锁：真实失败目录复现 ----
def test_real_0827_failure_dir_reproduces(mod, monkeypatch, capsys):
    """用 2026-08-27 真实失败目录复现（若存在）：0 json → 交易日判定下必为 exit 3。"""
    d = ROOT / "artifacts" / "p6_4_pull_20260827"
    if not d.is_dir():
        pytest.skip("2026-08-27 拉取目录不存在（artifacts 未保留）")
    monkeypatch.setattr("sys.argv", ["p6_4", "--dir", str(d), "--trading-day"])
    assert mod.main() == 3
    assert "行情拉取失败" in capsys.readouterr().out
