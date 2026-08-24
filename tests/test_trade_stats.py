"""trade_stats 模块单测（去重统计 + FIFO 闭合校验 + markdown 渲染）。"""

from datetime import date
from pathlib import Path

from hexbroker.paper.trade_stats import (
    ALERT_CLOSURE_TOL,
    analyze_trades_log,
    daily_stats_alerts,
    parse_audit_lines,
    render_daily_stats_markdown,
    summarize_daily,
)

# 样例审计行：T000001/T000002 各 3 副本、T000003 2 副本（模拟会话重放 3× 伪增）
_SAMPLE_LINES = [
    # T000001: ag0 开空 100 (×3)
    "2026-08-24 09:05:07 | {\"event\": \"trade\", \"trade_id\": \"T000001\", \"ts\": \"2026-08-24 09:05:07.609\", \"symbol\": \"ag0\", \"direction\": -1, \"qty\": -1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 100.0, \"fee\": 1.5, \"is_open\": true, \"is_today_close\": false}",
    "2026-08-24 09:05:20 | {\"event\": \"trade\", \"trade_id\": \"T000001\", \"ts\": \"2026-08-24 09:05:20.547\", \"symbol\": \"ag0\", \"direction\": -1, \"qty\": -1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 100.0, \"fee\": 1.5, \"is_open\": true, \"is_today_close\": false}",
    "2026-08-24 09:05:26 | {\"event\": \"trade\", \"trade_id\": \"T000001\", \"ts\": \"2026-08-24 09:05:26.625\", \"symbol\": \"ag0\", \"direction\": -1, \"qty\": -1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 100.0, \"fee\": 1.5, \"is_open\": true, \"is_today_close\": false}",
    # T000002: ag0 平空 90（今平）(×3)
    "2026-08-24 09:06:01 | {\"event\": \"trade\", \"trade_id\": \"T000002\", \"ts\": \"2026-08-24 09:06:01.111\", \"symbol\": \"ag0\", \"direction\": 1, \"qty\": 1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 90.0, \"fee\": 1.35, \"is_open\": false, \"is_today_close\": true}",
    "2026-08-24 09:06:15 | {\"event\": \"trade\", \"trade_id\": \"T000002\", \"ts\": \"2026-08-24 09:06:15.222\", \"symbol\": \"ag0\", \"direction\": 1, \"qty\": 1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 90.0, \"fee\": 1.35, \"is_open\": false, \"is_today_close\": true}",
    "2026-08-24 09:06:20 | {\"event\": \"trade\", \"trade_id\": \"T000002\", \"ts\": \"2026-08-24 09:06:20.333\", \"symbol\": \"ag0\", \"direction\": 1, \"qty\": 1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 90.0, \"fee\": 1.35, \"is_open\": false, \"is_today_close\": true}",
    # T000003: rb0 开多 200（仅 2 副本，模拟 T000145 类 minor anomaly）
    "2026-08-24 09:10:00 | {\"event\": \"trade\", \"trade_id\": \"T000003\", \"ts\": \"2026-08-24 09:10:00.100\", \"symbol\": \"rb0\", \"direction\": 1, \"qty\": 1.0, \"entry\": 0.0, \"stop\": 180.0, \"take_profit\": null, \"price\": 200.0, \"fee\": 1.0, \"is_open\": true, \"is_today_close\": false}",
    "2026-08-24 09:10:05 | {\"event\": \"trade\", \"trade_id\": \"T000003\", \"ts\": \"2026-08-24 09:10:05.100\", \"symbol\": \"rb0\", \"direction\": 1, \"qty\": 1.0, \"entry\": 0.0, \"stop\": 180.0, \"take_profit\": null, \"price\": 200.0, \"fee\": 1.0, \"is_open\": true, \"is_today_close\": false}",
]


def _write_sample(tmp_path: Path) -> Path:
    p = tmp_path / "trades.log"
    p.write_text("\n".join(_SAMPLE_LINES) + "\n", encoding="utf-8")
    return p


def test_analyze_dedup_and_closure(tmp_path) -> None:
    """去重（8 行→3 笔）+ FIFO 毛利与 account 净盈亏闭合。"""
    log = _write_sample(tmp_path)
    acct = tmp_path / "account.json"
    # 去重语义：每笔唯一成交只收一次费（副本是日志伪增，非真实计费）
    # ag0: 毛利 150 − 费 2.85 = 净 147.15；rb0: 毛利 0 − 费 1.0 = 净 −1.0
    acct.write_text('{"trade_seq": 3, "realized": {"ag0": 147.15, "rb0": -1.0}}', encoding="utf-8")

    r = analyze_trades_log(str(log), date(2026, 8, 24), account_json_path=str(acct))
    assert r["exists"] is True
    assert r["raw_count"] == 8
    assert r["unique_count"] == 3
    assert r["copy_dist"] == {3: 2, 2: 1}
    assert r["nonidentical"] == 0
    assert r["filtered_count"] == 3
    # ag0: 开空 100 → 平空 90 → 毛利 (100-90)*1*15=150；费 1.5+1.35=2.85 → 净 147.15
    assert abs(r["fifo_gross"]["ag0"] - 150.0) < 1e-9
    assert abs(r["fee_by_sym"]["ag0"] - 2.85) < 1e-9
    assert abs(r["fifo_gross"]["ag0"] - r["fee_by_sym"]["ag0"] - r["acct_realized"]["ag0"]) < 1e-9
    # rb0: 仅开仓 → 毛利 0、费 1.0 → 净 -1.0
    assert abs(r["fifo_gross"].get("rb0", 0.0) - 0.0) < 1e-9
    assert abs(r["fee_by_sym"]["rb0"] - 1.0) < 1e-9
    assert abs(0.0 - 1.0 - r["acct_realized"]["rb0"]) < 1e-9
    # 今平 = 1（T000002）
    assert r["today_close"] == 1
    assert r["act_counter"] == {"开多": 1, "开空": 1, "平空": 1}
    assert r["hourly"]["09"] == 3


def test_analyze_missing_log(tmp_path) -> None:
    r = analyze_trades_log(str(tmp_path / "nope.log"), date(2026, 8, 24))
    assert r["exists"] is False
    assert r["filtered_count"] == 0
    assert r["unique_count"] == 0


def test_analyze_until_time_filter(tmp_path) -> None:
    """until_time 截止过滤：09:10 的记录在 09:05 截止下应被排除。"""
    log = _write_sample(tmp_path)
    r = analyze_trades_log(str(log), date(2026, 8, 24), until_time="09:05:30")
    assert r["filtered_count"] == 1  # 仅 T000001（09:05:07）在窗口内


def test_render_markdown_with_account(tmp_path) -> None:
    log = _write_sample(tmp_path)
    r = analyze_trades_log(str(log), "2026-08-24")
    md = render_daily_stats_markdown(r, acct_realized={"ag0": 147.15, "rb0": -1.0})
    assert "唯一成交：**3** 笔" in md
    assert "{3: 2, 2: 1}" in md
    assert "闭合偏差" in md
    assert "+0.0000" in md  # 合计闭合


def test_render_markdown_without_account(tmp_path) -> None:
    log = _write_sample(tmp_path)
    r = analyze_trades_log(str(log), "2026-08-24")
    md = render_daily_stats_markdown(r)  # 未提供账户快照
    assert "| - | - |" in md  # 账户净盈亏/闭合偏差列为 '-'
    assert "未提供账户快照，未做闭合校验" in md


def test_render_markdown_missing_log() -> None:
    md = render_daily_stats_markdown({"exists": False})
    assert "不存在" in md


def test_summarize_daily_with_trades(tmp_path) -> None:
    """单行摘要：去重笔数/量/费/今平/毛利/净/闭合。"""
    log = _write_sample(tmp_path)
    r = analyze_trades_log(str(log), "2026-08-24", account_json_path=None)
    line = summarize_daily(r)
    assert "去重成交 3 笔" in line
    assert "量 3 手" in line
    assert "费 ¥3.85" in line
    assert "今平 1" in line
    assert "毛利 +150.00" in line
    assert "净 +146.15" in line  # 150 - 3.85
    assert "闭合" not in line  # 未提供账户快照


def test_summarize_daily_with_account(tmp_path) -> None:
    """提供账户快照时附闭合校验。"""
    log = _write_sample(tmp_path)
    r = analyze_trades_log(str(log), "2026-08-24")
    r["acct_realized"] = {"ag0": 147.15, "rb0": -1.0}
    line = summarize_daily(r)
    assert "账户净 +146.15" in line
    assert "闭合 +0.0000" in line


def test_summarize_daily_no_trades() -> None:
    line = summarize_daily({"exists": True, "day": "2026-08-24", "filtered_count": 0})
    assert "当日暂无成交" in line


def test_summarize_daily_missing_log() -> None:
    line = summarize_daily({"exists": False})
    assert "审计日志不存在" in line


def test_daily_stats_alerts_clean(tmp_path) -> None:
    """闭合偏差 0、无副本不一致 → 无告警。"""
    log = _write_sample(tmp_path)
    r = analyze_trades_log(str(log), "2026-08-24", account_json_path=None)
    r["acct_realized"] = {"ag0": 147.15, "rb0": -1.0}  # 净 146.15 = 毛利150 − 费3.85
    assert daily_stats_alerts(r) == []


def test_daily_stats_alerts_closure_mismatch(tmp_path) -> None:
    """闭合偏差超过阈值 → 告警。"""
    log = _write_sample(tmp_path)
    r = analyze_trades_log(str(log), "2026-08-24", account_json_path=None)
    r["acct_realized"] = {"ag0": 100.0, "rb0": -1.0}  # 与净 146.15 明显不符
    alerts = daily_stats_alerts(r)
    assert any("闭合偏差" in a and "不一致" in a for a in alerts)
    assert ALERT_CLOSURE_TOL > 0


def test_daily_stats_alerts_nonidentical() -> None:
    """副本间字段不一致 → 数据质量告警。"""
    r = {"exists": True, "nonidentical": 2, "acct_realized": None, "fifo_gross": {}, "total_fee": 0.0}
    alerts = daily_stats_alerts(r)
    assert any("副本间字段不一致 2 条" in a for a in alerts)


def test_parse_audit_normalizes_bool(tmp_path) -> None:
    """P2：解析层把历史日志的字符串 is_open "False"/"True" 归一化为 bool。"""
    lines = [
        "2026-08-24 09:06:09 | {\"event\": \"trade\", \"trade_id\": \"T000003\", \"ts\": \"2026-08-24 09:06:09.637\", \"symbol\": \"ag0\", \"direction\": 1, \"qty\": 1.0, \"entry\": 0.0, \"stop\": 2917.89, \"take_profit\": null, \"price\": 16896.01, \"fee\": 25.344, \"is_open\": \"False\", \"is_today_close\": \"True\"}",
        "2026-08-24 09:07:10 | {\"event\": \"trade\", \"trade_id\": \"T000005\", \"ts\": \"2026-08-24 09:07:10.637\", \"symbol\": \"ag0\", \"direction\": -1, \"qty\": -1.0, \"entry\": 16895.99, \"stop\": 17307.07, \"take_profit\": 16279.39, \"price\": 16895.99, \"fee\": 12.672, \"is_open\": \"True\", \"is_today_close\": false}",
    ]
    log = tmp_path / "trades.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    trades, _ = parse_audit_lines(str(log))
    assert len(trades) == 2
    assert trades[0]["is_open"] is False
    assert trades[0]["is_today_close"] is True
    assert trades[1]["is_open"] is True
    assert trades[1]["is_today_close"] is False
