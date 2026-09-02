"""P1 / P1-C 回归：权益自适应可交易门槛 + 保证金口径硬顶（2026-09-02）。

**P1 权益自适应门槛**
    ag0 的「结构性不可交易」其实是**全品种通病**：按名义价口径复算（equity 94,857 /
    2.5×ATR / 1% 预算），18 个品种仅 rb0（0.84%）与 hc0（0.77%）可交易。
    引入 ``min_equity``（= 单手风险 / 本品种预算）：当 ``min_equity / equity >=
    struct_untradeable_ratio``（默认 3.0）时判定结构性不可交易，拦截日志**按日去重
    降级为 INFO**；未达倍数的边缘品种（如 m0 仅 1.18×）仍按 WARNING 告警。

**P1-C 保证金口径硬顶**
    ``max_position_pct`` 是**名义价值**口径，对带杠杆的期货根本错配（名义口径 3/18
    达标 vs 保证金口径 15/18），且它**设计上仅告警不拦截**。故另设 ``max_margin_pct``
    （保证金口径，默认 0.20）并让它**真正生效**。

⛔ **判序铁律**：``risk_budget`` 必须**先于** ``margin_cap`` 判定。否则 ag0 会被判成
保证金拦截而每轮刷告警，P1 的静默逻辑失效 —— 本文件有专门用例守这条。

⛔ **口径铁律**：以下价格一律用**名义价**（raw_close），绝不用后复权 close。
后复权价会让名义敞口/保证金/单笔风险**系统性低估 1/k 倍**（ag0 k=0.6839 → 低估 1.46×）。

零行为变化基准（改动前实测，见 planner.py::_size_qty docstring）：
    rb0 → 1 手 / hc0 → 1 手 / ag0 → 0 手（capped_by=risk_budget）
"""
from __future__ import annotations

import pytest

from hexbroker.paper.planner import PlanManager

# 名义价口径（data/raw/processed/<sym>/1d/2026.parquet，2026-09-01 收盘）
EQUITY = 94856.70
SYMBOLS = {
    #   品种   名义价        乘数    ATR%   门槛权益(万)  结构性(≥3.0×)
    "rb0": dict(price=3174.0, mult=10.0, atr_pct=0.0100),   #   7.9  否
    "hc0": dict(price=3388.0, mult=10.0, atr_pct=0.0086),   #   7.3  否
    "m0": dict(price=3377.0, mult=10.0, atr_pct=0.0133),   #  11.2  否（1.18×）
    "ag0": dict(price=16245.0, mult=15.0, atr_pct=0.0379),  # 230.9  是（24.3×）
    "cu0": dict(price=109220.0, mult=5.0, atr_pct=0.0104),  # 142.0  是
}
MULTIPLIERS = {s: v["mult"] for s, v in SYMBOLS.items()}

MARGIN_RATE = 0.12
MAX_MARGIN_PCT = 0.20
STRUCT_RATIO = 3.0


class _LogRec:
    """替换 ``hexbroker.utils.logging._loguru_logger``，按级别收集消息。

    ⛔ 必须 monkeypatch **模块属性**而非 logger 实例：``TaggedLogger`` 在方法体内
    才引用模块级 ``_loguru_logger``，故替换模块属性即可生效（``logger.remove()``
    对这条链路无效，别再试）。
    """

    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []

    @staticmethod
    def _fmt(message: str, args: tuple) -> str:
        """⛔ 必须用 loguru 的 ``{}`` 风格，不是 ``%`` 风格。

        用 ``%`` 会在 ``{:.2%}`` 处抛 ``ValueError: unsupported format character``，
        进而 fallback 成未格式化消息 —— 那时 ``symbol={}`` 里没有品种名，
        ``"ag0" in msg`` 恒为 False，测试会给出完全误导的失败。
        """
        try:
            return message.format(*args) if args else message
        except (IndexError, KeyError, ValueError):
            return message

    def info(self, message: str, *args, **kwargs) -> None:
        self.infos.append(self._fmt(message, args))

    def warning(self, message: str, *args, **kwargs) -> None:
        self.warnings.append(self._fmt(message, args))

    def debug(self, message: str, *args, **kwargs) -> None:
        pass

    def error(self, message: str, *args, **kwargs) -> None:
        pass

    def exception(self, message: str, *args, **kwargs) -> None:
        pass


@pytest.fixture
def rec(monkeypatch) -> _LogRec:
    r = _LogRec()
    monkeypatch.setattr("hexbroker.utils.logging._loguru_logger", r)
    return r


def _planner(**kw) -> PlanManager:
    base = dict(
        multipliers=MULTIPLIERS,
        plans_dir="trade_plans",
        size_by_risk=True,
        risk_per_trade=0.01,
        risk_stop_atr_mult=2.5,
        max_position_pct=0.50,
        margin_rate=MARGIN_RATE,
        max_margin_pct=MAX_MARGIN_PCT,
        struct_untradeable_ratio=STRUCT_RATIO,
    )
    base.update(kw)
    return PlanManager(**base)


def _metrics(sym: str, pm: PlanManager, intent: float = 0.30):
    v = SYMBOLS[sym]
    return pm.size_metrics(sym, intent, v["price"], EQUITY, stop=None, atr=v["price"] * v["atr_pct"])


# ----------------------------------------------------------------------
# ① 零行为变化回归（P1/P1-C 不得改变任何既有手数判定）
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "sym,expected_lots,expected_cap",
    [
        ("rb0", 1, "notional"),
        ("hc0", 1, "notional"),
        ("ag0", 0, "risk_budget"),
        ("m0", 0, "risk_budget"),
    ],
)
def test_zero_behavior_change(sym, expected_lots, expected_cap):
    """改动前后逐位一致：rb0/hc0 仍开 1 手，ag0/m0 仍 0 手。"""
    m = _metrics(sym, _planner())
    assert m["final_lots"] == expected_lots
    assert m["capped_by"] == expected_cap


def test_ag0_risk_pct_matches_production_baseline():
    """ag0 单手风险须与 planner 注释的实测 24.89% 同量级（v2 重算 24.34%）。"""
    m = _metrics("ag0", _planner())
    assert abs(m["risk_pct_1lot"] - 0.2431) < 0.01, m["risk_pct_1lot"]


# ----------------------------------------------------------------------
# ② P1 权益自适应门槛
# ----------------------------------------------------------------------
def test_min_equity_and_structural_flag():
    pm = _planner()
    ag = _metrics("ag0", pm)
    m0 = _metrics("m0", pm)
    # 门槛权益 = 单手风险 / 预算
    assert ag["min_equity"] == pytest.approx(ag["risk_per_lot"] / 0.01, rel=1e-9)
    assert ag["structurally_untradeable"] is True        # 230.9万 / 9.49万 = 24.3× ≥ 3.0
    assert m0["structurally_untradeable"] is False       # 11.2万 / 9.49万 = 1.18× < 3.0
    assert ag["min_equity"] / EQUITY >= STRUCT_RATIO
    assert m0["min_equity"] / EQUITY < STRUCT_RATIO


def test_structural_symbol_logs_info_only_once_per_day(rec):
    """结构性品种：连调 2 次只打 1 条 INFO，不再每轮 WARNING 刷屏。"""
    pm = _planner()
    for _ in range(2):
        pm._size_qty("ag0", 0.30, SYMBOLS["ag0"]["price"], EQUITY,
                     stop=None, atr=SYMBOLS["ag0"]["price"] * SYMBOLS["ag0"]["atr_pct"])
    hits = [x for x in rec.infos if "结构性不可交易" in x]
    assert len(hits) == 1, f"应只打 1 条，实得 {len(hits)}: {hits}"
    assert "ag0" in hits[0] and "门槛权益" in hits[0], hits[0]
    # 且不得作为「风险预算拦截」的 WARNING 出现
    assert not [w for w in rec.warnings if "P0-4 风险预算拦截" in w and "ag0" in w]


def test_edge_symbol_still_warns_every_round(rec):
    """边缘品种（m0 仅 1.18×，离解锁很近）：每轮都告警，值得提醒。"""
    pm = _planner()
    for _ in range(2):
        pm._size_qty("m0", 0.30, SYMBOLS["m0"]["price"], EQUITY,
                     stop=None, atr=SYMBOLS["m0"]["price"] * SYMBOLS["m0"]["atr_pct"])
    hits = [w for w in rec.warnings if "P0-4 风险预算拦截" in w and "m0" in w]
    assert len(hits) == 2, f"应每轮告警，实得 {len(hits)}"


def test_struct_ratio_zero_disables_silencing(rec):
    """struct_untradeable_ratio=0 → 关闭降级，ag0 恢复每轮 WARNING。"""
    pm = _planner(struct_untradeable_ratio=0.0)
    for _ in range(2):
        pm._size_qty("ag0", 0.30, SYMBOLS["ag0"]["price"], EQUITY,
                     stop=None, atr=SYMBOLS["ag0"]["price"] * SYMBOLS["ag0"]["atr_pct"])
    hits = [w for w in rec.warnings if "P0-4 风险预算拦截" in w and "ag0" in w]
    assert len(hits) == 2, f"关闭降级后应每轮告警，实得 {len(hits)}"
    assert not [x for x in rec.infos if "结构性不可交易" in x]


# ----------------------------------------------------------------------
# ③ P1-C 保证金口径硬顶
# ----------------------------------------------------------------------
def test_margin_cap_fields():
    pm = _planner()
    rb = _metrics("rb0", pm)
    ag = _metrics("ag0", pm)
    # 1 手保证金 / 权益
    assert rb["margin_pct"] == pytest.approx(
        3174.0 * 10.0 * MARGIN_RATE / EQUITY, rel=1e-9)
    assert rb["over_margin_cap"] is False          # 4.0% ≤ 20%
    assert ag["over_margin_cap"] is True           # 30.8% > 20%


def test_risk_budget_judged_before_margin_cap():
    """⛔ 判序铁律：保证金超标的 ag0/cu0 其 capped_by 必须是 risk_budget。

    若顺序反了，ag0 会被判成 margin_cap → 每轮刷保证金告警 → P1 静默失效。
    ⚠️ cu0 在生产意图 30% 下 raw_lots=0.052 < 0.10 会走 min_lot_threshold 提前返回
    （字段全空），故此处放大意图到 100% **只为触发完整计算链路**，非生产值。
    """
    pm = _planner()
    for sym, intent in (("ag0", 0.30), ("cu0", 1.00)):
        m = _metrics(sym, pm, intent=intent)
        assert m["over_margin_cap"] is True, f"{sym} 保证金应超标"
        assert m["capped_by"] == "risk_budget", f"{sym} 判序错误: {m['capped_by']}"


def test_margin_cap_actually_blocks_when_risk_passes():
    """保证金硬顶必须**真正拦截**：构造风险达标但保证金超标的场景。

    用极低保证金率上限（0.01）逼出 margin_cap 分支，验证 final_lots 归零。
    """
    pm = _planner(max_margin_pct=0.01)   # rb0 保证金 4.0% > 1%
    m = _metrics("rb0", pm)
    assert m["over_margin_cap"] is True
    assert m["final_lots"] == 0
    assert m["capped_by"] == "margin_cap"


def test_notional_cap_is_diagnostic_only():
    """P1-C：名义硬顶**设计上仅告警不拦截** —— ag0 名义 256.9% 仍只是诊断。

    （真正拦它的是风险预算；名义口径对期货错配，故保留为可观测指标。）
    """
    m = _metrics("ag0", _planner())
    assert m["over_notional_cap"] is True
    # 拦截归因必须是风险预算，而非名义硬顶
    assert m["capped_by"] == "risk_budget"


# ----------------------------------------------------------------------
# ④ fail-safe：保证金口径不可用 → 放弃硬顶，而非全品种停摆
#    （QA fresh-eyes 复核判定的 🔴 项，2026-09-02 修复）
# ----------------------------------------------------------------------
@pytest.mark.parametrize("bad_kw", [{"margin_rate": 0.0}, {"max_margin_pct": 0.0}])
def test_margin_cap_disabled_when_config_unusable(bad_kw):
    """🔴 保证金率/上限配置不可用时**不得**导致全品种停摆。

    修复前的实现：``margin_per_lot = price * mult * margin_rate`` 在
    ``margin_rate<=0`` 或 ``max_margin_pct<=0`` 时得到 0 → ``lots_margin=0``
    → **所有品种 final_lots=0，交易静默全停**，且只留下自相矛盾的告警
    「保证金 0.00% > 上限 20.00%」，极难定位。

    修法原则：**新增护栏不得引入新的「全停」失效模式**。口径不可用时放弃这道
    硬顶（``lots_margin=None``，不收紧 ``min()``），退回 P1-C 之前的基线 ——
    此时仍有「风险预算（planner）+ 全组合保证金（broker）」两道把关。
    """
    pm = _planner(**bad_kw)
    m = _metrics("rb0", pm)
    assert m["lots_margin"] is None          # None = 本轮不适用（区别于「算出 0 手」）
    assert m["over_margin_cap"] is False
    assert m["margin_pct"] == 0.0
    assert m["final_lots"] == 1              # 与 P1-C 之前一致 ⇒ **未停摆**
    assert m["capped_by"] == "notional"      # 由名义口径兜底

    # ⛔ 保证金硬顶缺席**没有**放 ag0 过关：风险预算仍然拦住它
    ag = _metrics("ag0", pm)
    assert ag["final_lots"] == 0
    assert ag["capped_by"] == "risk_budget"

    # 对照：正常配置下 lots_margin 是正整数。
    # rb0 单手保证金 = 3174×10×12% = 3808.8；权益 20% = 18971.34 → 可开 4 手
    assert _metrics("rb0", _planner())["lots_margin"] == 4


# ----------------------------------------------------------------------
# ⑤ P1 是**权益自适应**门槛，不是静态白名单 —— 权益上涨自动解锁
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "equity,struct,lots",
    [
        (94_856.70, True, 0),     # 当前：230.9万 / 9.49万 = 24.3× → 结构性，日志按日 INFO
        (1_000_000.0, False, 0),  # 100万：2.31× → 不再结构性，但风险预算仍只批 0 手
        (2_500_000.0, False, 1),  # 250万：0.92× → 风险预算批 1 手 → **真正解锁**
    ],
)
def test_equity_growth_unlocks_structural_symbol(equity, struct, lots):
    """门槛权益是**固定值**、结构性标记**随权益浮动** —— 加本金即可自动解锁。

    ⛔ 关键区分（易被误读）：``structurally_untradeable`` 只决定**日志级别**
    （每轮 WARNING → 按日 INFO），**不改变是否开仓**。
    100 万那一档最能说明问题：标记已转 False，但手数仍是 0（风险预算拦），
    只是日志噪音回来了 —— 证明 P1 做的是「分档降噪」而非「放行」。
    """
    pm = _planner()
    v = SYMBOLS["ag0"]
    m = pm.size_metrics(
        "ag0", 0.30, v["price"], equity, stop=None, atr=v["price"] * v["atr_pct"]
    )
    assert m["structurally_untradeable"] is struct
    assert m["final_lots"] == lots
