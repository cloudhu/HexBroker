"""trade_intent 中文交易意图翻译单测（§3.1 扩展）。

覆盖：开多/平多/今平/平空、``is_open`` 字符串兼容、止损止盈缺失、计划变更、
符号中文名映射与回退、``_as_bool`` 健壮性。

注意 ``_fmt_num`` 约定：整数值浮点去尾零（``3036.0`` → ``"3036"``，``0.0`` → ``"0"``）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hexbroker.paper.trade_intent import (  # noqa: E402
    _as_bool,
    plan_change_intent,
    trade_intent,
)


def test_user_sample_close_long_today() -> None:
    """用户真实样例：今平 1 手 rb0 多单（is_open 为字符串 'False'）。"""
    ev = {
        "event": "trade",
        "trade_id": "T000048",
        "ts": "2026-08-24 09:28:39.637479",
        "symbol": "rb0",
        "direction": -1,
        "qty": -1.0,
        "entry": 0.0,
        "stop": 2917.892857142857,
        "take_profit": None,
        "price": 3036.0,
        "fee": 3.036,
        "is_open": "False",  # 历史日志偶发字符串化
        "is_today_close": True,
    }
    out = trade_intent(ev)
    assert "【今平】平多 1 手 螺纹钢(rb0)" in out
    assert "成交价 3036" in out
    assert "手续费 3.04" in out
    assert "平仓后均价 0" in out
    assert "止损 2917.89" in out
    assert "未设止盈" in out
    assert "09:28:39" in out  # 微秒已去除


def test_open_long() -> None:
    ev = {
        "symbol": "ag0",
        "direction": 1,
        "qty": 2.0,
        "entry": 8000.0,
        "stop": 7800.0,
        "take_profit": 8200.0,
        "price": 8000.0,
        "fee": 24.0,
        "is_open": True,
        "is_today_close": False,
        "ts": "2026-08-24 09:00:00",
    }
    out = trade_intent(ev)
    assert "开多 2 手 沪银(ag0)" in out
    assert "建仓均价 8000" in out
    assert "止损 7800" in out
    assert "止盈 8200" in out


def test_close_short() -> None:
    ev = {
        "symbol": "c0",
        "direction": 1,
        "qty": 3.0,
        "entry": 0.0,
        "stop": None,
        "take_profit": None,
        "price": 2300.0,
        "fee": 6.9,
        "is_open": False,
        "is_today_close": False,
        "ts": "2026-08-24 14:00:00",
    }
    out = trade_intent(ev)
    assert "平空 3 手 玉米(c0)" in out
    assert "平仓后均价 0" in out
    assert "未设止盈" in out
    assert "止损" not in out  # 无止损时不输出


def test_open_short_with_string_bool() -> None:
    ev = {
        "symbol": "rb0",
        "direction": -1,
        "qty": -1.0,
        "entry": 3000.0,
        "stop": 2950.0,
        "take_profit": None,
        "price": 3000.0,
        "fee": 3.0,
        "is_open": "True",  # 字符串真
        "is_today_close": False,
        "ts": "2026-08-24 09:05:00",
    }
    out = trade_intent(ev)
    assert "开空 1 手 螺纹钢(rb0)" in out
    assert "建仓均价 3000" in out


def test_symbol_unknown_fallback() -> None:
    ev = {
        "symbol": "zz9",
        "direction": 1,
        "qty": 1.0,
        "entry": 100.0,
        "stop": None,
        "take_profit": None,
        "price": 100.0,
        "fee": 1.0,
        "is_open": True,
        "is_today_close": False,
        "ts": "2026-08-24 10:00:00",
    }
    out = trade_intent(ev)
    assert "zz9" in out  # 未知符号回退原符号


def test_plan_change_intent() -> None:
    pc = {
        "ts": "2026-08-24 10:30:00",
        "symbol": "rb0",
        "change_type": "risk_hint",
        "detail": "螺纹钢夜盘下跌风险上升",
    }
    out = plan_change_intent(pc)
    assert "2026-08-24 10:30:00 计划变更·风险提醒｜螺纹钢(rb0)：螺纹钢夜盘下跌风险上升" == out


def test_as_bool_robust() -> None:
    assert _as_bool("False") is False
    assert _as_bool("True") is True
    assert _as_bool("true") is True
    assert _as_bool(True) is True
    assert _as_bool(False) is False
    assert _as_bool(0) is False
    assert _as_bool(1) is True
    assert _as_bool(None) is False


def test_reporter_daily_trade_intent_column() -> None:
    """trade_intent 接入 ReviewReporter 复盘「当日交易明细」中文列（用户样例：今平 rb0 多单）。"""
    from datetime import datetime

    from hexbroker.paper.reporter import ReviewReporter
    from hexbroker.paper.types import TradeEvent

    t = TradeEvent(
        trade_id="T000048",
        ts=datetime(2026, 8, 24, 9, 28, 39, 637479),
        symbol="rb0",
        direction=-1,
        qty=-1.0,
        entry=0.0,
        stop=2917.892857142857,
        take_profit=None,
        price=3036.0,
        fee=3.036,
        is_open=False,
        is_today_close=True,
    )

    class _Acct:
        equity = cash = margin_used = peak_equity = 1.0
        drawdown = 0.0
        positions = avg_entry = realized = {}

    rep = ReviewReporter(reports_dir="data/paper/_tmp_rep_test", symbols_display={"rb0": "SHFE.rb"})
    md = rep._render_md.__func__(
        rep, datetime(2026, 8, 24).date(), _Acct(), [t], {}, {}, [], []
    )
    seg = md.split("## 三、当日交易明细")[1].split("## 四")[0]
    # 表头含中文列
    assert "交易意图（中文）" in seg
    # 该行含中文意图（今平 + 螺纹钢）
    assert "【今平】" in seg
    assert "螺纹钢" in seg
    # 结构列仍保留
    assert "| 时间 | 品种 | 方向 | 手数 | 成交价 | 手续费 | 止损 | 止盈 |" in seg


def test_reporter_plan_change_intent_line() -> None:
    """plan_change_intent 接入 ReviewReporter 复盘「六、计划变更记录」中文行。"""
    from datetime import datetime

    from hexbroker.paper.reporter import ReviewReporter
    from hexbroker.paper.types import PlanChange

    pc = PlanChange(
        symbol="rb0",
        change_type="risk_hint",
        detail="螺纹钢夜盘下跌风险上升",
        ts=datetime(2026, 8, 24, 10, 30, 0),
    )

    class _Acct:
        equity = cash = margin_used = peak_equity = 1.0
        drawdown = 0.0
        positions = avg_entry = realized = {}

    rep = ReviewReporter(reports_dir="data/paper/_tmp_rep_pc_test", symbols_display={"rb0": "SHFE.rb"})
    md = rep._render_md.__func__(rep, datetime(2026, 8, 24).date(), _Acct(), [], {}, {}, [], [pc])
    seg = md.split("## 六、计划变更记录")[1].split("## 七")[0]
    # 不再输出原始 [ts] symbol type：detail 格式
    assert "[2026-08-24" not in seg
    # 中文行含标签映射（risk_hint -> 风险提醒）与中文名
    assert "计划变更·风险提醒" in seg
    assert "螺纹钢" in seg
    # 去除了旧的裸 change_type 字样（raw 格式已替换）
    assert "risk_hint" not in seg


def test_reporter_daily_stats_dedup_section(tmp_path) -> None:
    """复盘报告「八、当日交易统计（日志去重）」接入：按 trade_id 去重 + 闭合校验。"""
    from datetime import datetime

    from hexbroker.paper.reporter import ReviewReporter

    # 3× 重放样例：8 行 → 去重 3 笔（T000001/T000002 ×3、T000003 ×2）
    lines = [
        "2026-08-24 09:05:07 | {\"event\": \"trade\", \"trade_id\": \"T000001\", \"ts\": \"2026-08-24 09:05:07.609\", \"symbol\": \"ag0\", \"direction\": -1, \"qty\": -1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 100.0, \"fee\": 1.5, \"is_open\": true, \"is_today_close\": false}",
        "2026-08-24 09:05:20 | {\"event\": \"trade\", \"trade_id\": \"T000001\", \"ts\": \"2026-08-24 09:05:20.547\", \"symbol\": \"ag0\", \"direction\": -1, \"qty\": -1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 100.0, \"fee\": 1.5, \"is_open\": true, \"is_today_close\": false}",
        "2026-08-24 09:05:26 | {\"event\": \"trade\", \"trade_id\": \"T000001\", \"ts\": \"2026-08-24 09:05:26.625\", \"symbol\": \"ag0\", \"direction\": -1, \"qty\": -1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 100.0, \"fee\": 1.5, \"is_open\": true, \"is_today_close\": false}",
        "2026-08-24 09:06:01 | {\"event\": \"trade\", \"trade_id\": \"T000002\", \"ts\": \"2026-08-24 09:06:01.111\", \"symbol\": \"ag0\", \"direction\": 1, \"qty\": 1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 90.0, \"fee\": 1.35, \"is_open\": false, \"is_today_close\": true}",
        "2026-08-24 09:06:15 | {\"event\": \"trade\", \"trade_id\": \"T000002\", \"ts\": \"2026-08-24 09:06:15.222\", \"symbol\": \"ag0\", \"direction\": 1, \"qty\": 1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 90.0, \"fee\": 1.35, \"is_open\": false, \"is_today_close\": true}",
        "2026-08-24 09:06:20 | {\"event\": \"trade\", \"trade_id\": \"T000002\", \"ts\": \"2026-08-24 09:06:20.333\", \"symbol\": \"ag0\", \"direction\": 1, \"qty\": 1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 90.0, \"fee\": 1.35, \"is_open\": false, \"is_today_close\": true}",
        "2026-08-24 09:10:00 | {\"event\": \"trade\", \"trade_id\": \"T000003\", \"ts\": \"2026-08-24 09:10:00.100\", \"symbol\": \"rb0\", \"direction\": 1, \"qty\": 1.0, \"entry\": 0.0, \"stop\": 180.0, \"take_profit\": null, \"price\": 200.0, \"fee\": 1.0, \"is_open\": true, \"is_today_close\": false}",
        "2026-08-24 09:10:05 | {\"event\": \"trade\", \"trade_id\": \"T000003\", \"ts\": \"2026-08-24 09:10:05.100\", \"symbol\": \"rb0\", \"direction\": 1, \"qty\": 1.0, \"entry\": 0.0, \"stop\": 180.0, \"take_profit\": null, \"price\": 200.0, \"fee\": 1.0, \"is_open\": true, \"is_today_close\": false}",
    ]
    log = tmp_path / "trades.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    class _Acct:
        equity = cash = margin_used = peak_equity = 1.0
        drawdown = 0.0
        positions = avg_entry = {}
        realized = {"ag0": 147.15, "rb0": -1.0}

    rep = ReviewReporter(reports_dir=str(tmp_path / "reports"), symbols_display={"rb0": "SHFE.rb", "ag0": "SHFE.ag"})
    md = rep._render_md.__func__(
        rep, datetime(2026, 8, 24).date(), _Acct(), [], {}, {}, [], [],
        trades_log_path=str(log),
    )
    seg = md.split("## 八、当日交易统计（日志去重）")[1]
    # 去重口径：8 行 → 3 笔；副本分布 {3: 2, 2: 1}
    assert "唯一成交：**3** 笔" in seg
    assert "{3: 2, 2: 1}" in seg
    # 闭合校验出现且为 0（ag0: 150 毛利 − 8.55 费 = 141.45）
    assert "+0.0000" in seg
    assert "FIFO毛利" in seg


def test_reporter_evaluation_dedup_section(tmp_path) -> None:
    """generate_evaluation 接入「当日交易统计（日志去重）」段（口径同复盘八段）。"""
    from datetime import datetime

    from hexbroker.paper.reporter import ReviewReporter

    lines = [
        "2026-08-24 09:05:07 | {\"event\": \"trade\", \"trade_id\": \"T000001\", \"ts\": \"2026-08-24 09:05:07.609\", \"symbol\": \"ag0\", \"direction\": -1, \"qty\": -1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 100.0, \"fee\": 1.5, \"is_open\": true, \"is_today_close\": false}",
        "2026-08-24 09:05:20 | {\"event\": \"trade\", \"trade_id\": \"T000001\", \"ts\": \"2026-08-24 09:05:20.547\", \"symbol\": \"ag0\", \"direction\": -1, \"qty\": -1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 100.0, \"fee\": 1.5, \"is_open\": true, \"is_today_close\": false}",
        "2026-08-24 09:05:26 | {\"event\": \"trade\", \"trade_id\": \"T000001\", \"ts\": \"2026-08-24 09:05:26.625\", \"symbol\": \"ag0\", \"direction\": -1, \"qty\": -1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 100.0, \"fee\": 1.5, \"is_open\": true, \"is_today_close\": false}",
        "2026-08-24 09:06:01 | {\"event\": \"trade\", \"trade_id\": \"T000002\", \"ts\": \"2026-08-24 09:06:01.111\", \"symbol\": \"ag0\", \"direction\": 1, \"qty\": 1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 90.0, \"fee\": 1.35, \"is_open\": false, \"is_today_close\": true}",
        "2026-08-24 09:06:15 | {\"event\": \"trade\", \"trade_id\": \"T000002\", \"ts\": \"2026-08-24 09:06:15.222\", \"symbol\": \"ag0\", \"direction\": 1, \"qty\": 1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 90.0, \"fee\": 1.35, \"is_open\": false, \"is_today_close\": true}",
        "2026-08-24 09:06:20 | {\"event\": \"trade\", \"trade_id\": \"T000002\", \"ts\": \"2026-08-24 09:06:20.333\", \"symbol\": \"ag0\", \"direction\": 1, \"qty\": 1.0, \"entry\": 0.0, \"stop\": 90.0, \"take_profit\": null, \"price\": 90.0, \"fee\": 1.35, \"is_open\": false, \"is_today_close\": true}",
        "2026-08-24 09:10:00 | {\"event\": \"trade\", \"trade_id\": \"T000003\", \"ts\": \"2026-08-24 09:10:00.100\", \"symbol\": \"rb0\", \"direction\": 1, \"qty\": 1.0, \"entry\": 0.0, \"stop\": 180.0, \"take_profit\": null, \"price\": 200.0, \"fee\": 1.0, \"is_open\": true, \"is_today_close\": false}",
        "2026-08-24 09:10:05 | {\"event\": \"trade\", \"trade_id\": \"T000003\", \"ts\": \"2026-08-24 09:10:05.100\", \"symbol\": \"rb0\", \"direction\": 1, \"qty\": 1.0, \"entry\": 0.0, \"stop\": 180.0, \"take_profit\": null, \"price\": 200.0, \"fee\": 1.0, \"is_open\": true, \"is_today_close\": false}",
    ]
    log = tmp_path / "trades.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    class _Acct:
        equity = cash = margin_used = 1.0
        drawdown = 0.0
        peak_equity = 1.0
        positions = avg_entry = {}
        realized = {"ag0": 147.15, "rb0": -1.0}

    rep = ReviewReporter(reports_dir=str(tmp_path / "reports"))
    path = rep.generate_evaluation(
        datetime(2026, 8, 24).date(), _Acct(), [], 20,
        trades_log_path=str(log),
    )
    content = path.read_text(encoding="utf-8")
    seg = content.split("## 当日交易统计（日志去重）")[1].split("## 说明")[0]
    assert "唯一成交：**3** 笔" in seg
    assert "+0.0000" in seg  # 闭合校验
    # 说明段标注去重口径
    assert "按 trade_id 去重" in content

