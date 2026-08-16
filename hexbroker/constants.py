"""全局常量与枚举定义（跨文件强制约定 §8.3–§8.5）。

所有枚举字符串值直接用于信号/日志/合同，便于审计与跨模块一致。
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Freq(StrEnum):
    """K 线频率。bar 时间戳以「结束时刻」标注，区间左闭右开。"""

    D1 = "1d"
    M60 = "60m"
    M30 = "30m"
    M5 = "5m"
    M1 = "1m"
    TICK = "tick"


class SignalDirection(IntEnum):
    """预测信号方向。"""

    SHORT = -1
    FLAT = 0
    LONG = 1


class ActionSpaceType(StrEnum):
    """RL 动作空间类型。"""

    DISCRETE5 = "discrete5"  # 目标仓位 {-1, -0.5, 0, 0.5, 1}
    CONTINUOUS = "continuous"  # Box(-1, 1)


class SellSignalCode(StrEnum):
    """风控卖出信号代码（S1–S5）。"""

    S1_TREND_BREAK = "S1"  # 趋势破坏
    S2_VOL_DIVERGENCE = "S2"  # 量价背离
    S3_TARGET_REACHED = "S3"  # 目标达成
    S4_TIME_STOP = "S4"  # 时间止损
    S5_VOLATILITY_SPIKE = "S5"  # 波动异常


class RecoveryStage(StrEnum):
    """回撤恢复分级（R0–R4）。"""

    R0_NORMAL = "R0"  # 正常
    R1_REDUCE = "R1"  # 降仓
    R2_HALT = "R2"  # 暂停开仓
    R3_PROBE = "R3"  # 小仓试探
    R4_RESTORE = "R4"  # 恢复满仓


class Exchange(StrEnum):
    """交易所代码（用于 symbol 的 ``EXCHANGE.product`` 格式）。"""

    SHFE = "SHFE"  # 上期所（沪铜 cu / 螺纹钢 rb）
    INE = "INE"    # 能源中心（原油 sc）
    DCE = "DCE"    # 大商所
    CZCE = "CZCE"  # 郑商所
    CFFEX = "CFFEX"  # 中金所


# 交易所层面 NaN 约定
NAN_SYMBOL = "UNKNOWN"

# 时区（tz-naive 本地时间存储，与 TQSDK 一致）
TIMEZONE = "Asia/Shanghai"

# 默认随机种子与本金等（均可在 yaml 覆盖，禁止硬编码在业务代码）
DEFAULT_SEED = 42
DEFAULT_CAPITAL = 1_000_000.0

# 涨跌停标记列名（数据契约 §8.4）
LIMIT_UP = "limit_up"
LIMIT_DOWN = "limit_down"
IS_ROLLOVER = "is_rollover"

# 标准 OHLCV 列名（小写，§8.4）
OHLCV_COLS = ["open", "high", "low", "close", "volume", "amount", "open_interest"]
