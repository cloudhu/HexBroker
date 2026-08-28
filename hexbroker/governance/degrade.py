"""运行时动态降级（P2-D 治理机制，Q5 留项落地）。

PASS 方案运行中若绩效/数据质量信号跌破红线（且样本量充足），将其**生效模式**
从 LIVE 降为 SHADOW（内存覆盖 + ledger history 留痕 + 告警）。核心纪律：

- **仅降不升**：信号恢复不自动升回；升回走既有联锁（主理人 + 校准记录）。
- **降级不热改配置**：不修改 yaml/ledger status；重启后降级清除、ledger 有痕。
- **样本护栏**：n ≥ min_n 才裁决（无证据不翻转），比较子固定 `<`（跌破红线语义）。
- **fail-safe**：信号缺失/文件损坏/评估异常一律跳过并告警，绝不阻断 tick。
- **默认关**：configs/scheme_degrade.yaml enabled=false 时 load_from_config 返回 None。

零顶层重依赖：仅标准库 + 轻量 yaml（与 scheme.py 同款）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from ..utils.logging import get_logger
from .interlock import ResolvedMode, resolve_scheme_mode
from .ledger import CalibrationLedger
from .scheme import SchemeRegistry

log = get_logger("GOV")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DegradeRule:
    """降级规则：signals[scheme_id][metric] < threshold 且 n ≥ min_n 时触发。"""

    scheme_id: str
    metric: str            # 信号字段名，如 "rolling_wr"
    threshold: float       # 红线（跌破触发），如 0.45
    min_n: int = 20        # 样本护栏：信号 n < min_n → 不裁决
    rule_id: str = ""      # 留痕标识；空则自动生成

    def resolved_id(self) -> str:
        return self.rule_id or f"{self.scheme_id}.{self.metric}<{self.threshold}"


@dataclass(frozen=True)
class DegradeEvent:
    """一次运行时降级事件（留痕用，append-only）。"""

    scheme_id: str
    rule_id: str
    metric: str
    metric_value: float
    n: int
    reason: str
    ts: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "runtime_degrade",
            "scheme_id": self.scheme_id,
            "rule_id": self.rule_id,
            "metric": self.metric,
            "metric_value": self.metric_value,
            "n": self.n,
            "reason": self.reason,
            "ts": self.ts,
        }


# ---------------------------------------------------------------------------
# 信号文件读取（P2-D3：外部写入者 → 文件观测接口）
# ---------------------------------------------------------------------------
def read_signals_file(
    path: str | Path, max_age_sec: int = 900
) -> Optional[Dict[str, Dict[str, Any]]]:
    """读取信号文件；损坏/缺失/过期 → None（调用方按"无信号"处理，不评估）。

    文件格式：{"generated_at": "ISO8601", "signals": {"1C": {"rolling_wr": 0.4, "n": 25}}}
    新鲜度优先取文件内 generated_at，缺省回退文件 mtime。
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        signals = data.get("signals")
        if not isinstance(signals, dict):
            return None
        ts_raw = data.get("generated_at")
        ts = datetime.fromisoformat(str(ts_raw)) if ts_raw else datetime.fromtimestamp(
            p.stat().st_mtime
        )
        if ts.tzinfo is not None:  # 统一为 naive 比较口径
            ts = ts.replace(tzinfo=None)
        if datetime.now() - ts > timedelta(seconds=max_age_sec):
            return None
        return signals
    except Exception:
        log.warning("治理降级信号文件解析失败（按无信号处理）：{}", path)
        return None


# ---------------------------------------------------------------------------
# 运行时降级器（P2-D1/D2）
# ---------------------------------------------------------------------------
class RuntimeDegrader:
    """运行时降级引擎：observe_and_evaluate（触发）+ effective_mode（查询，仅降不升）。"""

    def __init__(
        self,
        rules: List[DegradeRule],
        ledger: Optional[CalibrationLedger] = None,
        min_interval_sec: int = 300,
        now_fn: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._rules = list(rules)
        self._ledger = ledger
        self._min_interval_sec = max(0, int(min_interval_sec))
        self._now_fn = now_fn
        self._degraded: Dict[str, DegradeEvent] = {}
        self._last_eval: Optional[datetime] = None

    # ---- 构造 ----
    @classmethod
    def load_from_config(
        cls,
        path: str | Path,
        ledger: Optional[CalibrationLedger] = None,
    ) -> Optional["RuntimeDegrader"]:
        """从 yaml 加载；enabled=false 或文件缺失 → None（默认零行为变更）。"""
        p = Path(path)
        if not p.exists():
            return None
        try:
            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            log.warning("治理降级配置解析失败（按未启用处理）：{}", path)
            return None
        if not cfg.get("enabled", False):
            return None
        rules: List[DegradeRule] = []
        for raw in cfg.get("rules", []) or []:
            if not isinstance(raw, dict):
                continue
            try:
                rules.append(
                    DegradeRule(
                        scheme_id=str(raw["scheme_id"]),
                        metric=str(raw["metric"]),
                        threshold=float(raw["threshold"]),
                        min_n=int(raw.get("min_n", 20)),
                        rule_id=str(raw.get("rule_id", "") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError) as e:
                log.warning("治理降级规则字段非法，跳过：{}（{}）", raw, e)
        return cls(
            rules=rules,
            ledger=ledger,
            min_interval_sec=int(cfg.get("min_interval_sec", 300)),
        )

    # ---- 评估（P2-D1）----
    def should_evaluate(self, now: Optional[datetime] = None) -> bool:
        """节流：距上次评估不足 min_interval_sec → False。"""
        now = now or self._now_fn()
        if (
            self._last_eval is not None
            and (now - self._last_eval).total_seconds() < self._min_interval_sec
        ):
            return False
        return True

    def observe_and_evaluate(
        self, signals: Optional[Dict[str, Dict[str, Any]]]
    ) -> List[DegradeEvent]:
        """按规则评估信号；触发 → 记录降级 + 留痕 + 告警。异常绝不外抛。

        仅降不升：已降级方案信号恢复也不会移除 _degraded 条目。
        """
        self._last_eval = self._now_fn()
        if not signals:
            return []
        fired: List[DegradeEvent] = []
        for rule in self._rules:
            try:
                if rule.scheme_id in self._degraded:
                    continue  # 仅降不升
                sig = signals.get(rule.scheme_id)
                if not isinstance(sig, dict):
                    continue
                n = int(sig.get("n", 0) or 0)
                if n < rule.min_n:
                    continue  # 样本护栏：不足不裁决
                value = sig.get(rule.metric)
                if value is None:
                    continue
                value = float(value)
                if value < rule.threshold:
                    ev = DegradeEvent(
                        scheme_id=rule.scheme_id,
                        rule_id=rule.resolved_id(),
                        metric=rule.metric,
                        metric_value=value,
                        n=n,
                        reason=f"runtime_degraded:{rule.metric}={value:.4f}<{rule.threshold}(n={n})",
                        ts=self._now_fn().isoformat(timespec="seconds"),
                    )
                    self._degraded[rule.scheme_id] = ev
                    fired.append(ev)
                    log.warning(
                        "方案 {} 运行时降级 LIVE→SHADOW：{} （rule={}）",
                        rule.scheme_id, ev.reason, rule.resolved_id(),
                    )
                    if self._ledger is not None:
                        try:
                            self._ledger.append_history(
                                rule.scheme_id, ev.to_dict()
                            )
                        except Exception:
                            log.exception("降级留痕写入失败（不影响降级生效）")
            except Exception:
                log.exception("降级规则评估异常（跳过该规则）: {}", rule.resolved_id())
        return fired

    # ---- 查询（P2-D2：与联锁复用的生效模式）----
    def is_degraded(self, scheme_id: str) -> bool:
        return scheme_id in self._degraded

    def degraded_event(self, scheme_id: str) -> Optional[DegradeEvent]:
        return self._degraded.get(scheme_id)

    def effective_mode(
        self, requested_mode: str, scheme_id: str, registry: SchemeRegistry
    ) -> Tuple[ResolvedMode, Optional[str]]:
        """生效模式决议：已降级 → SHADOW（runtime_degraded）；否则走既有联锁。"""
        if requested_mode in ("live", "trade") and scheme_id in self._degraded:
            return ResolvedMode.SHADOW, "runtime_degraded"
        return resolve_scheme_mode(requested_mode, scheme_id, registry)
