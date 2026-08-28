"""六方案治理控制平面单元测试（P2 双治理 + 落地）。

覆盖：联锁 4 类 reason + 未知方案 fail-safe、账本 JSON 往返/追加幂等、寄存器 verify_all、
6 方案 YAML 加载、启动自检接线语义。
"""
import json
import textwrap
from pathlib import Path

import pytest

from hexbroker.governance import (
    SchemeStatus,
    Scheme,
    SchemeRegistry,
    MisOpenBlocked,
    ResolvedMode,
    assert_safe_to_activate,
    resolve_scheme_mode,
    CalibrationRecord,
    CalibrationLedger,
)

CFG = Path("configs/scheme_governance.yaml")
LEDGER = Path("data/governance/calibration_ledger.json")


# --------------------------------------------------------------------------
# 夹具：内存寄存器 / 账本
# --------------------------------------------------------------------------
def _registry():
    return SchemeRegistry(
        {
            "1C": Scheme("1C", "跨年", SchemeStatus.PASS, calibration_ref="cal_1c"),
            "3B": Scheme("3B", "放量突破", SchemeStatus.NOT_PASS, calibration_ref="cal_3b"),
            "3A": Scheme("3A", "买点时效", SchemeStatus.DEPRECATED, calibration_ref="cal_3a"),
        }
    )


# --------------------------------------------------------------------------
# 寄存器加载（6 方案 YAML）
# --------------------------------------------------------------------------
def test_registry_loads_six_schemes():
    reg = SchemeRegistry.load(CFG)
    assert set(reg.ids()) == {"1C", "2A", "3A", "3B", "1B", "2B"}
    assert reg.get("1C").status is SchemeStatus.PASS
    assert reg.get("2A").status is SchemeStatus.PASS
    assert reg.get("3A").status is SchemeStatus.DEPRECATED
    assert reg.get("3B").status is SchemeStatus.NOT_PASS
    assert reg.get("1B").status is SchemeStatus.NOT_PASS
    assert reg.get("2B").status is SchemeStatus.NOT_PASS


def test_registry_verify_all_blocks_pass_without_calibration():
    bad = SchemeRegistry({"X": Scheme("X", "x", SchemeStatus.PASS, calibration_ref=None)})
    with pytest.raises(ValueError):
        bad.verify_all()


# --------------------------------------------------------------------------
# 联锁：4 类 reason + 未知方案 fail-safe
# --------------------------------------------------------------------------
def test_interlock_pass_scheme_ok():
    reg = _registry()
    assert_safe_to_activate("1C", reg)  # 不抛


def test_interlock_not_pass_reason():
    reg = _registry()
    with pytest.raises(MisOpenBlocked) as ei:
        assert_safe_to_activate("3B", reg)
    assert ei.value.reason == "not_pass"


def test_interlock_deprecated_reason():
    reg = _registry()
    with pytest.raises(MisOpenBlocked) as ei:
        assert_safe_to_activate("3A", reg)
    assert ei.value.reason == "deprecated"


def test_interlock_unknown_scheme_fail_safe():
    reg = _registry()
    with pytest.raises(MisOpenBlocked) as ei:
        assert_safe_to_activate("ZZ", reg)
    assert ei.value.reason == "unknown_scheme"


def test_interlock_no_calibration_reason():
    reg = SchemeRegistry(
        {"1C": Scheme("1C", "跨年", SchemeStatus.PASS, calibration_ref=None)}
    )
    with pytest.raises(MisOpenBlocked) as ei:
        assert_safe_to_activate("1C", reg)
    assert ei.value.reason == "no_calibration"


def test_resolve_mode_fail_safe_returns_shadow_with_reason():
    reg = _registry()
    mode, reason = resolve_scheme_mode("live", "3B", reg)
    assert mode is ResolvedMode.SHADOW
    assert reason == "not_pass"


def test_resolve_mode_pass_returns_live():
    reg = _registry()
    mode, reason = resolve_scheme_mode("live", "1C", reg)
    assert mode is ResolvedMode.LIVE
    assert reason is None


def test_resolve_mode_shadow_request_unchanged():
    reg = _registry()
    mode, reason = resolve_scheme_mode("shadow", "1C", reg)
    assert mode is ResolvedMode.SHADOW
    assert reason is None


# --------------------------------------------------------------------------
# 账本：JSON 往返 + 追加幂等 + 原子写
# --------------------------------------------------------------------------
def test_ledger_roundtrip(tmp_path):
    p = tmp_path / "ledger.json"
    led = CalibrationLedger()
    led.append(CalibrationRecord("3B", SchemeStatus.NOT_PASS, "NOT_PASS", n=183, gate1_dir_acc=0.497))
    led.save(p)
    loaded = CalibrationLedger.load(p)
    assert loaded.get("3B").n == 183
    assert loaded.get("3B").status is SchemeStatus.NOT_PASS


def test_ledger_append_keeps_history(tmp_path):
    p = tmp_path / "ledger.json"
    led = CalibrationLedger()
    led.append(CalibrationRecord("1C", SchemeStatus.PASS, "PASS", n=240))
    led.append(CalibrationRecord("1C", SchemeStatus.PASS, "PASS", n=260))  # 新版本覆盖
    led.save(p)
    assert led.get("1C").n == 260
    assert len(led._history["1C"]) == 1  # 旧版留痕
    assert led._history["1C"][0]["n"] == 240


def test_ledger_no_tmp_leftover(tmp_path):
    p = tmp_path / "ledger.json"
    CalibrationLedger().save(p)
    assert not p.with_name(p.name + ".tmp").exists()  # 原子写 .tmp 已 replace


# --------------------------------------------------------------------------
# 账本与寄存器联动：PASS 方案须有校准记录
# --------------------------------------------------------------------------
def test_ledger_prefilled_aligns_with_registry():
    reg = SchemeRegistry.load(CFG)
    led = CalibrationLedger.load(LEDGER)
    for sid in reg.ids():
        if reg.get(sid).status is SchemeStatus.PASS:
            assert led.has(sid), f"PASS 方案 {sid} 缺校准记录"
        else:
            # 非 PASS 方案，联锁本就拦截 LIVE
            mode, _ = resolve_scheme_mode("live", sid, reg)
            assert mode is ResolvedMode.SHADOW


# --------------------------------------------------------------------------
# 启动自检接线语义（不进入 tick；仅决议 mode）
# --------------------------------------------------------------------------
def test_startup_selfcheck_semantics():
    reg = SchemeRegistry.load(CFG)
    live, shadow = [], []
    for sid in reg.ids():
        mode, reason = resolve_scheme_mode("live", sid, reg)
        (live if mode is ResolvedMode.LIVE else shadow).append(sid)
    # 仅 PASS 方案可实盘；其余强制影子
    assert set(live) == {"1C", "2A"}
    assert set(shadow) == {"3A", "3B", "1B", "2B"}
