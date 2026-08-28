"""P2-D 运行时动态降级测试：触发/样本护栏/默认关/仅降不升/留痕/fail-safe/节流/文件新鲜度。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from hexbroker.governance import (
    CalibrationLedger,
    DegradeRule,
    ResolvedMode,
    RuntimeDegrader,
    SchemeRegistry,
    read_signals_file,
)

REG = SchemeRegistry.load("configs/scheme_governance.yaml")


def _degrader(rules=None, **kw) -> RuntimeDegrader:
    return RuntimeDegrader(
        rules=rules or [DegradeRule("1C", "rolling_wr", 0.45, min_n=20, rule_id="t1c")],
        **kw,
    )


def _now() -> datetime:
    return datetime(2026, 8, 28, 14, 0, 0)


# ---- 触发 / 护栏 ----
def test_rule_fires_on_breach_with_enough_samples():
    d = _degrader()
    fired = d.observe_and_evaluate({"1C": {"rolling_wr": 0.40, "n": 25}})
    assert len(fired) == 1 and d.is_degraded("1C")
    assert fired[0].metric_value == pytest.approx(0.40)


def test_no_fire_when_samples_insufficient():
    d = _degrader()
    fired = d.observe_and_evaluate({"1C": {"rolling_wr": 0.10, "n": 19}})
    assert fired == [] and not d.is_degraded("1C")  # 样本护栏：不足不裁决


def test_no_fire_when_metric_above_threshold():
    d = _degrader()
    fired = d.observe_and_evaluate({"1C": {"rolling_wr": 0.55, "n": 100}})
    assert fired == [] and not d.is_degraded("1C")


def test_no_fire_when_scheme_or_metric_missing():
    d = _degrader()
    assert d.observe_and_evaluate({}) == []
    assert d.observe_and_evaluate({"1C": {"n": 50}}) == []  # 缺 metric
    assert not d.is_degraded("1C")


# ---- 生效模式（仅降不升，联锁复用）----
def test_effective_mode_degraded_live_forced_shadow():
    d = _degrader()
    d.observe_and_evaluate({"1C": {"rolling_wr": 0.30, "n": 40}})
    mode, reason = d.effective_mode("live", "1C", REG)
    assert mode is ResolvedMode.SHADOW and reason == "runtime_degraded"


def test_never_auto_escalate_after_recovery():
    d = _degrader()
    d.observe_and_evaluate({"1C": {"rolling_wr": 0.30, "n": 40}})
    d.observe_and_evaluate({"1C": {"rolling_wr": 0.90, "n": 99}})  # 信号恢复
    assert d.is_degraded("1C")  # 仅降不升
    mode, _ = d.effective_mode("live", "1C", REG)
    assert mode is ResolvedMode.SHADOW


def test_healthy_scheme_passthrough_to_interlock():
    d = _degrader()
    mode, reason = d.effective_mode("live", "1C", REG)   # 未降级 → 既有联锁（PASS→live）
    assert mode is ResolvedMode.LIVE and reason is None
    mode2, reason2 = d.effective_mode("live", "3B", REG)  # NOT_PASS → 既有联锁拦截
    assert mode2 is ResolvedMode.SHADOW and reason2 == "not_pass"


# ---- 默认关 / 配置 ----
def test_load_from_config_disabled_returns_none(tmp_path: Path):
    p = tmp_path / "scheme_degrade.yaml"
    p.write_text(yaml.safe_dump({"enabled": False, "rules": []}), encoding="utf-8")
    assert RuntimeDegrader.load_from_config(p) is None


def test_load_from_config_missing_file_returns_none(tmp_path: Path):
    assert RuntimeDegrader.load_from_config(tmp_path / "nope.yaml") is None


def test_load_from_config_enabled_builds_rules(tmp_path: Path):
    p = tmp_path / "scheme_degrade.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "enabled": True,
                "rules": [
                    {"scheme_id": "1C", "metric": "rolling_wr", "threshold": 0.45, "min_n": 30}
                ],
            }
        ),
        encoding="utf-8",
    )
    d = RuntimeDegrader.load_from_config(p)
    assert d is not None and d.observe_and_evaluate({"1C": {"rolling_wr": 0.44, "n": 30}}) != []


def test_real_default_config_is_disabled():
    """仓库内真实配置必须默认关（默认零行为变更红线）。"""
    cfg = yaml.safe_load(Path("configs/scheme_degrade.yaml").read_text(encoding="utf-8"))
    assert cfg["enabled"] is False and cfg["rules"] == []


# ---- 留痕 ----
def test_degrade_event_appended_to_ledger_history(tmp_path: Path):
    led = CalibrationLedger()
    led.append(
        type(led.get("x")) if False else _mk_rec("1C")
    )  # 先有 current 记录
    led_path = tmp_path / "ledger.json"
    led.save(led_path)

    led2 = CalibrationLedger.load(led_path)
    d = _degrader(ledger=led2)
    d.observe_and_evaluate({"1C": {"rolling_wr": 0.20, "n": 50}})
    led2.save(led_path)

    disk = json.loads(led_path.read_text(encoding="utf-8"))
    events = [e for e in disk["history"]["1C"] if e.get("type") == "runtime_degrade"]
    assert len(events) == 1 and events[0]["metric"] == "rolling_wr"


def _mk_rec(sid: str):
    from hexbroker.governance import CalibrationRecord, SchemeStatus

    return CalibrationRecord(sid, SchemeStatus.PASS, "PASS")


# ---- fail-safe / 节流 ----
def test_evaluate_exception_isolated():
    d = _degrader(rules=[DegradeRule("1C", "m", 0.5, min_n=1)])  # value 非法类型走异常路径
    fired = d.observe_and_evaluate({"1C": {"m": "not-a-number", "n": 5}})
    assert fired == [] and not d.is_degraded("1C")


def test_should_evaluate_throttled():
    d = _degrader(min_interval_sec=300)
    t0 = _now()
    assert d.should_evaluate(t0) is True
    d.observe_and_evaluate(None)  # 记录 _last_eval（now_fn 默认真实时钟）
    # 用真实时钟：刚刚评估过 → 立即再问应 False
    assert d.should_evaluate() is False


# ---- 信号文件 ----
def test_read_signals_file_fresh_and_stale(tmp_path: Path):
    p = tmp_path / "scheme_signals.json"
    p.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "signals": {"1C": {"rolling_wr": 0.40, "n": 25}},
            }
        ),
        encoding="utf-8",
    )
    sig = read_signals_file(p, max_age_sec=900)
    assert sig is not None and sig["1C"]["rolling_wr"] == pytest.approx(0.40)

    stale_time = datetime.now() - timedelta(seconds=3600)
    p.write_text(
        json.dumps({"generated_at": stale_time.isoformat(timespec="seconds"), "signals": {"1C": {"rolling_wr": 0.1, "n": 99}}}),
        encoding="utf-8",
    )
    assert read_signals_file(p, max_age_sec=900) is None  # 过期=无信号


def test_read_signals_file_missing_or_corrupt(tmp_path: Path):
    assert read_signals_file(tmp_path / "nope.json") is None
    p = tmp_path / "bad.json"
    p.write_text("{not-json", encoding="utf-8")
    assert read_signals_file(p) is None
