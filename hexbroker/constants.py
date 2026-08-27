"""全局常量与枚举定义（跨文件强制约定 §8.3–§8.5）。

所有枚举字符串值直接用于信号/日志/合同，便于审计与跨模块一致。
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any


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

# ---------------------------------------------------------------------------
# P0-1 撮合假设默认值（与 configs/base.yaml / hexbroker/config.py 单一事实源对齐）
# ---------------------------------------------------------------------------
#: 撮合假设开关默认值（默认 = 生产基线口径，零变化）
EXECUTION_DEFAULTS: dict[str, Any] = {
    "next_bar_execution": False,   # False：同 bar close 成交（生产基线）；True：下一 bar open 成交
    "volume_cap": None,            # None：无成交量约束；启用默认比例 0.05（5%）
    "volume_cap_mode": "partial",  # partial：按比例部分成交；reject：整单拒绝
}
#: Q4 裁决：volume_cap 启用时的默认比例（5%，日频期货流动性经验起点）
VOLUME_CAP_DEFAULT_RATIO: float = 0.05

# ---------------------------------------------------------------------------
# P0-4 block bootstrap 默认值（Q5 裁决）
# ---------------------------------------------------------------------------
#: bootstrap 参数默认值（block_len=20 / n_boot=1000 / seed=None→cfg.seed / by_symbol=False）
BOOTSTRAP_DEFAULTS: dict[str, Any] = {
    "block_len": 20,
    "n_boot": 1000,
    "seed": None,       # None → cfg.seed（组合口径与双闸门一致）
    "by_symbol": False, # False：组合口径；True：截面 CI 研究辅助
}

# ---------------------------------------------------------------------------
# P0-3 四层指纹 / sidecar 约定
# ---------------------------------------------------------------------------
#: 指纹摘要长度（sha1 前 12 位，与 model_id / config_fingerprint 一致）
FINGERPRINT_LENGTH: int = 12
#: 指纹哈希算法
FINGERPRINT_ALGO: str = "sha1"
#: DataLake sidecar manifest 文件名（data/{layer}/{symbol}/{freq}/manifest.json）
SIDECAR_MANIFEST_NAME: str = "manifest.json"
#: SignalStore 分片指纹 sidecar 后缀（{root}/{model_id}/{train_end}.manifest.json）
SIGNAL_SIDECAR_SUFFIX: str = ".manifest.json"
