"""P2-4 QA 补充：tqsdk 持仓量口径「反例契约」用例（R26k fresh-eyes 复核新增）。

与 ``test_tqsdk_source.py`` 分工：那边钉住**断根**（主口径必须是 ``close_oi``）
与基本降级链；这边补工程师未覆盖的**边界输入形态**，把当前行为固定为契约，
防止后续重构静默改变语义。全部不触网，最小 DataFrame 直调
``TqsdkSource._klines_to_frame``。

刻意**不**固化为契约的已知口径空白（已上报，待工程侧裁决）：
- ``inf`` / ``-inf``：``pd.isna`` 为 False，且 ``inf`` 为真值，会**原样透传**而不回退；
- 负数：同样透传（不做 OI 合法性校验）；
- 超大整数（如 ``10**400``）：``float()`` 抛 ``OverflowError``，而当前
  ``except`` 只捕获 ``TypeError``/``ValueError``，**异常会向上抛**。
以上三条留白，不在本文件断言，避免把缺陷写成契约。
"""
from __future__ import annotations

import pandas as pd

from hexbroker.data.sources.tqsdk_source import TqsdkSource

OI_OPEN = 27354
OI_CLOSE = 27355


def _ts(day: str) -> int:
    """'YYYY-MM-DD' → tqsdk ``datetime`` 列口径的纳秒时间戳（UTC）。"""
    return int(pd.Timestamp(f"{day} 15:00:00", tz="Asia/Shanghai").value)


def _base_row(day: str, **over) -> dict:
    """构造一根合法日 K（缺省字段可由 ``**over`` 覆盖）。"""
    row = {
        "datetime": _ts(day),
        "open": 3000.0,
        "high": 3010.0,
        "low": 2990.0,
        "close": 3005.0,
        "volume": 100.0,
        "open_oi": OI_OPEN,
        "close_oi": OI_CLOSE,
    }
    row.update(over)
    return row


def _oi(rows: list[dict], sym: str = "ag0") -> list:
    """统一出口：取 ``open_interest`` 列值列表。"""
    df = TqsdkSource._klines_to_frame(pd.DataFrame(rows), sym)
    return list(df["open_interest"])


def test_close_oi_numeric_string_is_accepted():
    """``close_oi`` 为数值字符串 → 按 ``float()`` 语义正常转换（非 0、不回退）。"""
    assert _oi([_base_row("2026-09-01", close_oi="27399")]) == [27399.0]


def test_both_oi_nan_yields_zero():
    """``close_oi`` 与 ``open_oi`` 同时为 NaN → 两级皆空，结果为 0.0 且不抛异常。"""
    rows = [_base_row("2026-09-01", close_oi=float("nan"), open_oi=float("nan"))]
    assert _oi(rows) == [0.0]


def test_whole_close_oi_column_absent_falls_back():
    """整列 ``close_oi`` 不存在（列级缺失，非值为空）→ 逐行回退 ``open_oi``。

    与「值为 0/NaN」路径不同：此处 ``r.get("close_oi", 0)`` 走的是**默认参数**
    分支，用于覆盖旧版 tqsdk 无该字段的情形。
    """
    rows = [_base_row("2026-09-01"), _base_row("2026-09-02")]
    rows = [{k: v for k, v in r.items() if k != "close_oi"} for r in rows]
    assert "close_oi" not in pd.DataFrame(rows).columns
    assert _oi(rows) == [float(OI_OPEN), float(OI_OPEN)]


def test_non_numeric_object_yields_zero_without_raising():
    """``close_oi`` 为非数值对象（dict）→ TypeError 被吞，归 0 后回退 ``open_oi``。"""
    assert _oi([_base_row("2026-09-01", close_oi={"a": 1})]) == [float(OI_OPEN)]


def test_mixed_types_degrade_row_by_row():
    """同一批数据里逐行独立降级：正常 / 0 / 字符串 / NaN 各自按本行判定。"""
    rows = [
        _base_row("2026-09-01", open_oi=100.0, close_oi=110.0),      # 正常 → 主口径
        _base_row("2026-09-02", open_oi=110.0, close_oi=0),          # 0 → 回退 110
        _base_row("2026-09-03", open_oi=120.0, close_oi="130"),      # 字符串 → 130
        _base_row("2026-09-04", open_oi=130.0, close_oi=float("nan")),  # NaN → 回退 130
    ]
    assert _oi(rows) == [110.0, 110.0, 130.0, 130.0]


def test_large_int_oi_converts_without_error():
    """常规大整数（``2**70``，远超真实持仓量）→ 正常转 float，不触发异常。"""
    big = 2 ** 70
    assert _oi([_base_row("2026-09-01", close_oi=big)]) == [float(big)]
