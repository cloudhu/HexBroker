"""P2-D 信号写入器测试：schema 校验 / 合并 / 原子写 / CSV 导入 / 坏输入拒绝 / 与消费端对齐。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "gov_scheme_signals", Path(__file__).resolve().parents[1] / "scripts" / "gov_scheme_signals.py"
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


@pytest.fixture()
def tmp_signals(tmp_path, monkeypatch):
    p = tmp_path / "scheme_signals.json"
    monkeypatch.setattr(mod, "SIGNALS_PATH", p)
    return p


def test_set_and_show_roundtrip(tmp_signals):
    # 直接调用内部函数（避免 argparse 读 pytest argv）
    signals = mod.load()
    mod.merge_one(signals, "1C", "rolling_wr", 0.40, 25)
    mod.save_atomic(signals)
    disk = json.loads(tmp_signals.read_text(encoding="utf-8"))
    assert disk["signals"]["1C"]["rolling_wr"] == pytest.approx(0.40)
    assert disk["signals"]["1C"]["n"] == 25
    assert "generated_at" in disk


def test_atomic_write_leaves_no_tmp(tmp_signals):
    mod.save_atomic(mod.load())
    assert tmp_signals.exists()
    assert not tmp_signals.with_suffix(".json.tmp").exists()


def test_validate_rejects_bad_metric_and_nan():
    assert mod.validate("1C", "bad_metric", 0.5, 10)  # 白名单外
    assert mod.validate("1C", "rolling_wr", float("nan"), 10)  # NaN
    assert mod.validate("", "rolling_wr", 0.5, 10)  # 空 scheme
    assert mod.validate("1C", "rolling_wr", 0.5, -1)  # 负 n
    assert mod.validate("1C", "rolling_wr", 0.5, 10) == []  # 合法


def test_merge_overwrites_and_keeps_other_metrics(tmp_signals):
    signals = mod.load()
    mod.merge_one(signals, "1C", "rolling_wr", 0.60, 30)
    mod.merge_one(signals, "1C", "rolling_wr", 0.35, 20)  # 覆盖
    mod.merge_one(signals, "2A", "data_quality", 0.9, 10)  # 别的方案
    mod.save_atomic(signals)
    disk = json.loads(tmp_signals.read_text(encoding="utf-8"))["signals"]
    assert disk["1C"]["rolling_wr"] == pytest.approx(0.35)
    assert disk["1C"]["n"] == 20
    assert disk["2A"]["data_quality"] == pytest.approx(0.9)


def test_csv_import_skips_bad_rows(tmp_path):
    csv_path = tmp_path / "s.csv"
    csv_path.write_text(
        "scheme,metric,value,n\n"
        "1C,rolling_wr,0.42,25\n"
        "2A,foo,0.9,10\n"          # 白名单外 → 跳过
        "3B,rolling_wr,abc,10\n",  # 值非法 → 跳过
        encoding="utf-8",
    )
    rows = mod.from_csv(str(csv_path))
    # from_csv 仅跳过"字段非法"行；白名单校验在 main（validate）
    assert rows == [("1C", "rolling_wr", 0.42, 25), ("2A", "foo", 0.9, 10)]


def test_corrupt_existing_file_treated_as_empty(tmp_signals):
    tmp_signals.write_text("{broken", encoding="utf-8")
    assert mod.load() == {}  # 损坏→按空处理（fail-safe，与消费端语义一致）


def test_contract_aligns_with_consumer(tmp_signals):
    """写入器产出能被 governance.read_signals_file 正确消费（端到端契约）。"""
    from hexbroker.governance import read_signals_file

    signals = mod.load()
    mod.merge_one(signals, "1C", "rolling_wr", 0.40, 25)
    mod.save_atomic(signals)
    sig = read_signals_file(tmp_signals, max_age_sec=900)
    assert sig is not None and sig["1C"]["rolling_wr"] == pytest.approx(0.40)
    assert sig["1C"]["n"] == 25
