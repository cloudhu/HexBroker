"""成交/计划变更 → 中文交易意图翻译（运维可读化，§3.1 扩展）。

把 ``TradeEvent`` / ``PlanChange`` 的结构化字典翻译为运维友好的中文单行，用途：
① ``TradeLogger`` 实时额外输出（见 ``hexbroker/paper/logger.py``）；
② ``scripts/trade_log_chinese.py`` 离线批量转化审计日志。

设计要点：
- 纯函数、无 I/O、无日志依赖，可单测；
- 对 ``is_open`` 同时兼容 ``bool`` / ``"True"`` / ``"False"`` 字符串（历史日志兼容：
  ``TradeEvent.to_dict()`` 经 ``log_structured`` 的 ``default=str`` 序列化后偶发字符串化）；
- 符号中文名走 ``SYMBOL_CN`` 易扩展映射，缺失时回退原符号；
- 时间去微秒，便于阅读。
"""

from __future__ import annotations

from typing import Any

# 内部短名 → 中文展示名（按需扩展；缺失回退原符号）
SYMBOL_CN: dict[str, str] = {
    "ag0": "沪银",
    "rb0": "螺纹钢",
    "c0": "玉米",
    "au0": "沪金",
    "cu0": "沪铜",
    "sc0": "原油",
    "al0": "沪铝",
    "zn0": "沪锌",
    "ni0": "沪镍",
    "pb0": "沪铅",
    "sn0": "沪锡",
    "m0": "豆粕",
    "y0": "豆油",
    "p0": "棕榈油",
    "i0": "铁矿石",
    "j0": "焦炭",
    "jm0": "焦煤",
    "hc0": "热卷",
    "bu0": "沥青",
    "ru0": "橡胶",
    "pp0": "聚丙烯",
    "l0": "塑料",
    "v0": "PVC",
    "TA0": "PTA",
    "MA0": "甲醇",
    "SR0": "白糖",
    "CF0": "棉花",
    "RM0": "菜粕",
    "OI0": "菜油",
    "FG0": "玻璃",
    "ZC0": "动力煤",
    "eg0": "乙二醇",
    "sa0": "纯碱",
}

#: 计划变更类型 → 中文标签
_PLAN_LABEL: dict[str, str] = {
    "signal_update": "信号更新",
    "risk_hint": "风险提醒",
    "note": "备注",
}


def _as_bool(v: Any) -> bool:
    """兼容 ``bool`` / ``"True"|"False"`` 字符串 / 数值 的布尔解析。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y")
    if isinstance(v, (int, float)):
        return v != 0
    return bool(v)


def _fmt_num(v: Any) -> str:
    """数值友好显示：``None`` → '—'，否则最多 2 位小数（去尾零）。"""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == int(f):
        return str(int(f))
    return f"{f:.2f}"


def _fmt_time(v: Any) -> str:
    """时间戳友好显示：容忍 ISO('T') 与 ' '(``datetime.__str__``) 两种格式，去微秒。"""
    if v is None:
        return "?"
    s = str(v).strip().replace("T", " ")
    if "." in s:  # 去掉微秒，便于阅读
        s = s.split(".")[0]
    return s


def trade_intent(ev: dict[str, Any]) -> str:
    """将一笔 ``TradeEvent`` 字典翻译为中文交易意图单行。

    判定矩阵（以 ``is_open`` + ``direction`` 符号为准，而非 ``direction`` 字面）：
    - 开仓(``is_open=True``)：``direction>0`` → 开多 / ``direction<0`` → 开空
    - 平仓(``is_open=False``)：``direction>0``(买回) → 平空 / ``direction<0``(卖出) → 平多
    - 今平(``is_today_close=True``)：前缀【今平】

    ``entry`` 语义：开仓为建仓均价；平仓为平仓后剩余持仓均价（全平为 0.0）。
    """
    when = _fmt_time(ev.get("ts"))
    sym = str(ev.get("symbol", "?"))
    sym_cn = SYMBOL_CN.get(sym, sym)
    qty = abs(float(ev.get("qty", 0) or 0))
    direction = int(ev.get("direction", 0) or 0)
    is_open = _as_bool(ev.get("is_open"))
    is_today = _as_bool(ev.get("is_today_close"))

    if is_open:
        head = "开多" if direction > 0 else ("开空" if direction < 0 else "调仓")
    else:
        head = "平空" if direction > 0 else ("平多" if direction < 0 else "平仓")
    tag = "【今平】" if (is_today and not is_open) else ""

    head_part = f"{tag}{head} {qty:g} 手 {sym_cn}({sym})"
    ctx = "建仓均价" if is_open else "平仓后均价"
    detail = "，".join(
        [
            f"成交价 {_fmt_num(ev.get('price'))}",
            f"手续费 {_fmt_num(ev.get('fee'))}",
            f"{ctx} {_fmt_num(ev.get('entry'))}",
            (f"止损 {_fmt_num(ev.get('stop'))}" if ev.get("stop") is not None else ""),
            (f"止盈 {_fmt_num(ev.get('take_profit'))}" if ev.get("take_profit") is not None else "未设止盈"),
        ]
    )
    return f"{when} {head_part}：{detail}"


def plan_change_intent(pc: dict[str, Any]) -> str:
    """将一条 ``PlanChange`` 字典翻译为中文计划变更意图单行。"""
    when = _fmt_time(pc.get("ts"))
    sym = str(pc.get("symbol", "?"))
    sym_cn = SYMBOL_CN.get(sym, sym)
    ctype = pc.get("change_type", "")
    label = _PLAN_LABEL.get(ctype, ctype)
    detail = pc.get("detail", "")
    return f"{when} 计划变更·{label}｜{sym_cn}({sym})：{detail}"
