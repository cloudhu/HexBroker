"""信号引擎（§3.1 SignalEngine / D2 / §3.3）。

- 主源：v8 信号缓存（``artifacts/signals_cache18_grouped_v8.parquet``，含 ag0/rb0，**不含 c0**）。
- 新鲜度检测：信号日落后当前交易日超过阈值 → 标记过期（有持仓仅风控 / 无持仓禁开+告警，§8.2）。

  ⚠️ **度量口径 = 交易日历 lag**（P3-C，2026-08-31），见 :func:`_trading_lag`：
  ``lag = idx(上一交易日) - idx(信号日)``，**阈值默认 0**
  （语义：信号必须覆盖最近一个已收盘交易日）。
  ``lag = 0`` 即标准 T+1（上一交易日收盘信号 → 当日执行），放行；
  ``lag >= 1`` 即缓存停更 N 个交易日，拦截。

  口径演化（两代前车之鉴，勿回退）：

  ====  ============================  ======  ==========================================
  代    度量                          阈值    结果
  ====  ============================  ======  ==========================================
  P0-3  ``np.busday_count`` 工作日差  0       盘中 lag 恒 ≥1 → **日盘永不主源开仓**
  P3-B  自然日差                      1       **周一/假期后首日误拦 21.36%**（已证伪）
  P3-C  **交易日历 lag**（现行）      0       健康态误拦 **0.00%**，停更 1 日即拦
  ====  ============================  ======  ==========================================

  P3-B 的错误在于把「日历长度」当成「信息陈旧」：市场的日历是**交易日**而非自然日，
  周五收盘到周一开盘市场没有产生任何新信息，与周一→周二完全等价，
  自然日差却把它算成 3。全历史实测（rb0，2097 天健康态样本）见
  ``scripts/verify_freshness_caliber_options.py --full-history``。

- 技术指标兜底（c0 或信号缺失时）：双均线 + ATR 通道（§4.3 决策建议）。
"""

from __future__ import annotations

import bisect
from datetime import date, datetime, time
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd

from ..utils.logging import get_logger
from .types import SignalFrame

log = get_logger("PAPER")

# 主湖日线目录（交易日历**经验**数据源：天然含法定休市，不依赖外部 MCP）
DEFAULT_LAKE_DIR = Path(__file__).resolve().parents[2] / "data" / "raw" / "processed"

# 节假日表（交易日**判定**数据源）。⛔ 与主湖并集是两回事，见 augment_calendar。
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "paper.yaml"

# 日盘收盘时刻（中国商品期货日盘统一 15:00 收盘，本项目 18 个品种一致）。
# 用于判定「截至某时刻，最新已收盘交易日是当日还是前一交易日」，见 _trading_lag。
# 若将来接入国债/股指（15:15 收盘），须改为按品种配置，不可继续用全局常量。
DAY_SESSION_CLOSE = time(15, 0)


def _to_date(ts: Any) -> Optional[date]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, date):
        return ts
    if hasattr(ts, "date"):
        try:
            return ts.date()
        except Exception:
            return None
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts).date()
        except Exception:
            return None
    return None


def _calendar_days(a: date, b: date) -> int:
    """两个日期之间的**自然日**差（通用日期工具）。

    ⛔ **本函数不再是新鲜度门禁的度量口径**（P3-C，2026-08-31）。
    门禁口径已改为 :func:`_trading_lag`（交易日历 lag）。保留本函数仅作通用
    日期工具与历史脚本兼容 —— 用自然日差度量信号新鲜度会把周末/假期的
    **日历长度**误当成信息陈旧，系统性误伤周一与假期后首日（实测 21.36%）。
    """
    if a is None or b is None:
        return 10 ** 9
    return abs((b - a).days)


@lru_cache(maxsize=8)
def load_trading_calendar(lake_dir: str | None = None) -> tuple[date, ...]:
    """交易日历 = 主湖**全部品种**日线日期的**并集**（升序去重）。

    ⛔ 为什么用并集而不是单品种日历（实测依据，2026-08-31）：

    各品种日历高度一致（16/18 完全相同），但存在缺口 —— ``sc0`` 少 54 天
    （上海原油 2018-03 才上市）、``ni0`` 少 1 天。若按品种取日历，
    **单品种的数据缺口会让门禁变松**：该品种缺 bar 的那天不在它自己的日历里，
    于是「上一交易日」被前移，缓存停更会被误判为新鲜。

    并集日历则相反：任一天只要有一个品种有数据就算交易日，
    **不会因为某品种自身缺数据而放宽门禁**（保守方向正确）。

    取自主湖实际存在的日期 → 天然含法定休市，无需维护节假日表，不依赖外部 MCP。

    ⛔⛔ **已知边界已升级为设计约束：日期历必须与「数据是否已补」解耦**（2026-08-31）

    主湖并集只回答「**哪天有 bar**」，回答不了「**哪天是交易日**」。
    日盘收盘后、次日 08:00 补数之前，主湖没有当日 bar → 当日不在并集里，
    于是夜盘的 R 无法命中当日 → ``lag=None``（无法判定 → 保守拦截）。

    ⛔ 这里有一段**已被证伪的论证，勿回退**（我此前的错误判断）：

        「夜盘偏松可接受：20:30 刷新本身依赖当日 bar，补数失败时信号也不可能更
        fresher；且守卫传纯 date → R 恒取上一交易日，仍会报 NEED_BACKFILL。
        即守卫拦得住、引擎偏松，分层仍然闭合。」

    错在两点：
    1. **夜盘正是 P0-3 事故发生的场景**，引擎在这一层失明，守卫只在启动期跑一次，
       长驻进程 21:00 不会重跑 —— 夜里没有任何一层在管；
    2. 把「有没有更 fresher 的信号」和「门禁该不该放」混为一谈。门禁的意义是
       **没有足够新的信息就不要开仓**，不是「反正没有更好的」。

    正解是把两件事分开（见 :func:`augment_calendar`）：
    - 「今天是不是交易日」→ **独立知识**（周一至周五 + 节假日表），与补数无关；
    - 「今天的 bar 有没有进来」→ **数据问题**，由 ``lag`` 回答。
    增补后夜盘不再返回 ``None``，而是给出**可执行的确定值**（落后 1 个交易日）。

    Args:
        lake_dir: 主湖根目录；``None`` 用 :data:`DEFAULT_LAKE_DIR`。

    Returns:
        升序日期元组；主湖缺失/读取失败 → 空元组（调用方须按「不可用」处理）。
    """
    root = Path(lake_dir) if lake_dir else DEFAULT_LAKE_DIR
    days: set[date] = set()
    if not root.exists():
        # ⛔ 占位符必须是 loguru 的 {} 风格：get_logger 走 str.format，写 %s 会原样输出
        log.warning("交易日历不可用：主湖目录不存在 {}", root)
        return ()
    n_bad = 0
    for sym_dir in sorted(root.iterdir()):
        if not sym_dir.is_dir():
            continue
        for f in sorted((sym_dir / "1d").glob("*.parquet")):
            try:
                # 只读 datetime 列，避免整表 IO
                days |= set(
                    pd.to_datetime(pd.read_parquet(f, columns=["datetime"])["datetime"]).dt.date
                )
            except Exception as exc:  # noqa: BLE001 — 单文件损坏不应毁掉整份日历
                n_bad += 1
                # 单文件警告逐条打印会刷屏（测试环境主湖缺失时可达数百条），
                # 故降级为 debug，末尾按条数汇总成一条 warning。
                log.debug("交易日历跳过损坏文件 {}：{}", f, exc)
    if not days:
        log.warning("交易日历为空：主湖无可用日线数据 {}（跳过 {} 个文件）", root, n_bad)
    elif n_bad:
        log.warning("交易日历载 {} 天，跳过 {} 个损坏文件（日历仍可用）", len(days), n_bad)
    return tuple(sorted(days))


@lru_cache(maxsize=1)
def load_market_holidays() -> frozenset[date]:
    """独立交易日判定所需的节假日表（``configs/paper.yaml`` → ``paper.holidays_2026``）。

    与 :func:`load_trading_calendar` 是**两个不同的数据源**，分工不可混淆：

    ==========================  =====================  ================================
    数据源                      回答的问题              缺了会怎样
    ==========================  =====================  ================================
    主湖并集（经验）            哪天**有 bar**         夜盘 R 无当日可命中 → None
    节假日表（判定）            哪天**是交易日**        —— 不依赖补数，恒可回答
    ==========================  =====================  ================================

    ⛔ 读取失败的退化方向必须是**保守**的：退化为「仅按周一至周五判定」时，
    法定节假日会被误判成交易日 → 夜盘 R 取到休市日 → ``lag`` 被高估 → 拦截。
    门禁只会更紧，**不会漏放**。
    """
    try:
        import yaml  # 局部导入：signals 是热路径，yaml 只在首次调用时需要
    except Exception:  # noqa: BLE001
        log.warning("PyYAML 不可用 → 退化为「仅按周一至周五」判定交易日")
        return frozenset()
    try:
        raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("节假日表读取失败 → 退化为「仅按周一至周五」判定交易日：{}", exc)
        return frozenset()
    items = (raw.get("paper") or {}).get("holidays_2026") or []
    out: set[date] = set()
    for s in items:
        try:
            out.add(datetime.strptime(str(s), "%Y-%m-%d").date())
        except ValueError:
            log.warning("节假日表条目无法解析，已跳过：{}", s)
    return frozenset(out)


@lru_cache(maxsize=8)
def _trading_day_session(holidays: frozenset[date]) -> Any:
    """按节假日表构造**仅用于交易日判定**的会话对象（frozenset 可哈希 → 可缓存）。"""
    from .sessions import TradingSession  # 局部导入：避免与 sessions 形成导入环

    return TradingSession(symbol_sessions={}, holidays=set(holidays))


def is_market_trading_day(
    d: Optional[date], holidays: Optional[frozenset[date]] = None
) -> bool:
    """独立交易日判定 —— 复用 ``TradingSession`` 的实现，**不复制其逻辑**。

    ⛔ 不要在这里重写 ``weekday() < 5 and d not in holidays``：
    那份逻辑属于 ``sessions.py``，复制一份就是口径漂移的起点。
    """
    if d is None:
        return False
    hol = load_market_holidays() if holidays is None else holidays
    return bool(_trading_day_session(hol).is_trading_day(d))


def augment_calendar(
    calendar: Sequence[date],
    ref_day: Optional[date],
    signal_days: Optional[Any] = None,
    holidays: Optional[frozenset[date]] = None,
) -> tuple[date, ...]:
    """生效日历 = 主湖并集 ∪ 独立交易日判定 ∪ 缓存信号日（均截断到 ``ref_day``）。

    三层增补，各自堵一个洞：

    1. **独立交易日判定**（主要手段）：``ref_day`` 若按节假日表是交易日，
       就纳入日历。主湖日线次日才补数，夜盘时刻主湖恒无当日；但「今天是不是
       交易日」是**独立知识**，不该由「数据有没有补进来」回答。增补后夜盘不再
       返回 ``None``（无法判定），而是给出**可执行的确定值**（落后 N 个交易日）。
    2. **缓存信号日**（兜底）：信号是对已有 bar 打分的，所以「缓存里存在某日
       信号」本身就是「该日是交易日」的证据，可防节假日表不全（如只到 2026 年）。
    3. **截断到 ``ref_day``**：缓存里混入的未来日期（数据污染）不得改变 R 的判定。

    ⛔ 第 3 条不可省：否则未来日期会把 R 推到未来，门禁失真。

    ⛔⛔ **覆盖规则（最重要）**：以上三层只会**填补主湖覆盖之外（``> cal_max``）**
    的日期，**绝不改写主湖已知的历史区间**。原因见函数体注释 —— 主湖并集对
    它覆盖到的区间是权威记录，该区间内「不在主湖里」就等于「不是交易日」，
    不需要、也不允许用任何启发式（含第 2 层）去推断。
    """
    cal = tuple(calendar)
    if not cal or ref_day is None:
        return cal

    # ⛔⛔ 主铁律：**只补主湖覆盖范围之外（> cal_max）的日期，绝不改写主湖已知的历史**
    # （2026-08-31 QA 独立复核揪出；脏信号日污染的修复关键）
    #
    # 生产缓存实测含 4 个**非交易日**信号日：2020-10-02 / 2021-10-01（国庆）、
    # 2022-04-04（清明）、2024-06-10（端午）。若允许它们进入日历，日盘的
    # R = bisect_left(cal, T) - 1 会被推到这个脏日期上，把健康信号误判为 lag=1（误拦）。
    #
    # ⛔ 光靠 is_market_trading_day 过滤**修不掉**：holidays_2026 只覆盖 2026 年，
    # 对 2020-10-02（周五）会返回 True。真正可靠的分界是 cal_max ——
    # 主湖并集对它覆盖到的区间是**权威记录**（天然含法定休市），
    # 该区间内「不在主湖里」就等于「不是交易日」，不需要、也不允许推断。
    cal_max = cal[-1]
    extra: set[date] = set()
    # 层 1：独立交易日判定（当日超出主湖覆盖 → 用节假日表补）
    if ref_day > cal_max and is_market_trading_day(ref_day, holidays):
        extra.add(ref_day)
    # 层 2：缓存信号日（同样只补超出主湖覆盖的部分，且须通过独立交易日判定）
    for d in (signal_days or ()):
        if cal_max < d <= ref_day and is_market_trading_day(d, holidays):
            extra.add(d)
    if not extra or extra <= set(cal):
        return cal
    return tuple(sorted(set(cal) | extra))


def _closed_by(ref: Any, day_close: time) -> bool:
    """``ref`` 所指当日，日盘是否已收盘。

    仅当 ``ref`` 带时刻信息（datetime / Timestamp）且时刻 >= ``day_close`` 才算收盘。
    纯 ``date``（无时刻）一律视为**盘前/未收盘** —— 与启动自检语义一致：
    开盘前检查时期望缓存覆盖**上一交易日**，而非当日。
    """
    t = getattr(ref, "time", None)
    if t is None:
        return False
    try:
        return t() >= day_close
    except Exception:  # noqa: BLE001 — 异常时间对象按未收盘处理（安全方向偏保守）
        return False


def _trading_lag(
    sig_day: date | None,
    ref: Any,
    calendar: Sequence[date],
    day_close: time = DAY_SESSION_CLOSE,
) -> int | None:
    """信号新鲜度 = **交易日历 lag**（P3-C 门禁口径）。

    定义：``lag = idx(R) - idx(信号日)``，其中 ``R`` = **截至 ref 时刻，市场最新已收盘的交易日**。

    ``R`` 的确定是本口径的关键一环（P3-B 漏掉的正是这个）：

    ==============================  ============  ==================  ======
    场景                            R（已收盘）   应有信号日           lag
    ==============================  ============  ==================  ======
    日盘 T 10:30（当日未收盘）       T-1           T-1（标准 T+1）      0 ✅
    夜盘 T 21:30（当日 15:00 已收盘） T            T（20:30 刷新的）     0 ✅
    **夜盘 T 但缓存仍是 T-1**        T             T-1                 **1 ⛔**
    周一 10:30（信号=上周五）         上周五         上周五               0 ✅
    长假后首日 10:30（信号=节前）     节前           节前                 0 ✅
    ==============================  ============  ==================  ======

    ⛔ 第三行即 **P0-3 事故场景**（2026-08-24 夜盘误用 08-21 信号）。

    ⛔⛔ **夜盘必须严格命中当日，不得回退**（P3-C 补丁，2026-08-31；QA 独立复核揪出）：

    初版写法是 ``bisect_right(cal, ref_day) - 1``。**当 ref_day 不在日历里时
    （主湖尚未补数，夜盘时刻的常态），它不会报错，而是静默退到上一交易日**
    → 缓存停更整整一个交易日却算出 ``lag=0`` → **放行**。
    这等于 P0-3 事故原样复现，而夜盘**正是事故发生的场景**。

    取证（2026-08-31，主湖末位 08-28，今日 08-31 周一已收盘）：

    ==========================================  ========  ==========
    场景                                         初版     应有
    ==========================================  ========  ==========
    夜盘，缓存停更在 08-28（落后 1 个交易日）      lag=0    ⛔ 拦截
    P0-3 还原（日历 max=08-21，夜盘用 08-21）      lag=0    ⛔ 拦截
    夜盘，20:30 已刷出当日信号                     None     ✅ 放行
    ==========================================  ========  ==========

    前两行是**门禁失明**，第三行是**行为反转**（越新的信号越被拦）。
    根因同一个：把「日历里没有」当成「往前找一个」，而不是「无法判定」。
    **无法判定必须保守拦截**，绝不能退而求其次。

    - ``None`` → 无法判定（日历缺失 / 夜盘当日不在日历 / 信号日不在日历 /
      ref 早于日历起点）→ 调用方按**保守拦截**处理。
    - ``lag`` 可为**负数**：信号来自 R 之后（如守卫传纯 date → R 回退到 T-1，
      而缓存已是当日信号 → -1）。负值表示比「标准 T+1」更新，放行。
      ⛔ 保留符号，勿取绝对值 —— 否则守卫分不清「夜盘已刷新」与「刷新漏跑」。
    """
    if sig_day is None or ref is None or not calendar:
        return None
    cal: Sequence[date] = calendar
    # bisect 只能用 date 参与比较（datetime 与 date 无法比较 → TypeError），
    # 故日期部分归一化，时刻部分单独由 _closed_by 判定。
    ref_day = _to_date(ref)
    if ref_day is None:
        return None
    if _closed_by(ref, day_close):
        # 夜盘：当日日盘已收盘 → R **必须**是当日。日历里没有 = 无法判定。
        # ⛔ 绝不用 bisect_right 回退到上一交易日（见 docstring 的失明取证）。
        i_ref = bisect.bisect_left(cal, ref_day)
        if i_ref >= len(cal) or cal[i_ref] != ref_day:
            return None
    else:
        # 日盘：当日未收盘 → R = 严格早于当日的最后一个交易日
        i_ref = bisect.bisect_left(cal, ref_day) - 1
        if i_ref < 0:
            return None
    i_sig = bisect.bisect_left(cal, sig_day)
    if i_sig >= len(cal) or cal[i_sig] != sig_day:
        # 信号日不是交易日（异常）→ 无法判定，交调用方保守处理
        return None
    return i_ref - i_sig


class SignalEngine:
    """多信号源级联读取 + 新鲜度检测 + 技术指标兜底。

    - 信号源（按优先序）：主源（如 tail_ext，覆盖至最新）→ 兜底源（如 v8 生产基线）→ 技术指标。
    - 新鲜度检测：信号日距当前交易日的距离超过阈值 → 标记过期（有持仓仅风控 / 无持仓禁开+告警，§8.2）。
    - 技术指标兜底（c0 或全部缓存信号缺失时）：双均线 + ATR 通道（§4.3 决策建议）。

    Args:
        cache_path: 单信号缓存路径（向后兼容，等价 ``cache_paths=[cache_path]``）。
        cache_paths: 多源级联信号缓存路径（优先于 ``cache_path``）。
        freshness_threshold_trading_days: 信号新鲜度阈值，单位**交易日 lag**
            （见 :func:`_trading_lag`）。**默认 0 = 信号须覆盖最近一个已收盘交易日**
            （标准 T+1：上一交易日收盘信号 → 当日执行）。

            - ``lag = 0``（阈值 0）→ 放行。周一用周五信号、假期后首日用节前信号
              **都属此类**（市场在其间未产生新信息），必须放行。
            - ``lag >= 1`` → 缓存停更 N 个交易日 → ``is_effective=False``
              → 有持仓仅风控 / 无持仓禁开（§8.2），由技术兜底接手。

            ⛔ 阈值 0 **不是** P0-3 时代那个「病态的 0」：P0-3 用自然日差，
            盘中 lag 恒 ≥1 故 0 不可达；本口径下盘中 lag 可达 0，语义为「标准 T+1」。
        trading_calendar: 显式交易日历（升序）。``None`` → 从主湖
            :func:`load_trading_calendar` 懒加载（进程内缓存）。
            **单测应显式注入**以保证用例与文件系统解耦。
        fast_ma: 技术兜底快均线窗口。
        slow_ma: 技术兜底慢均线窗口。
        atr_window: 技术兜底 ATR 窗口。
        atr_mult: 技术兜底 ATR 通道倍数。
    """

    def __init__(
        self,
        cache_path: str | Path | None = None,
        cache_paths: list[str | Path] | None = None,
        freshness_threshold_trading_days: int = 0,
        trading_calendar: Sequence[date] | None = None,
        holidays: Any = None,
        fast_ma: int = 5,
        slow_ma: int = 20,
        atr_window: int = 14,
        atr_mult: float = 1.5,
    ) -> None:
        # 多源级联：cache_path 单参数向后兼容（等价 cache_paths=[cache_path]）；显式 cache_paths 优先
        if cache_paths:
            self._paths = [Path(p) for p in cache_paths]
        elif cache_path:
            self._paths = [Path(cache_path)]
        else:
            raise ValueError("SignalEngine 至少需要一个信号缓存路径")
        self._freshness_threshold = int(freshness_threshold_trading_days)
        # ⛔ 必须是 Optional：空元组是「显式注入空日历」的合法值（测试保守拦截分支用），
        #    不能用 `if not self._calendar` 判断是否已注入，否则空注入会被真实日历覆盖。
        self._calendar: Optional[tuple[date, ...]] = (
            tuple(sorted(trading_calendar)) if trading_calendar is not None else None
        )
        self._day_close = DAY_SESSION_CLOSE
        # 独立交易日判定的节假日表；None = 用 load_market_holidays()（懒加载，可注入覆盖）
        self._holidays: Optional[frozenset[date]] = (
            frozenset(holidays) if holidays is not None else None
        )
        self._signal_days_cache: Optional[frozenset[date]] = None
        self._fast_ma = int(fast_ma)
        self._slow_ma = int(slow_ma)
        self._atr_window = int(atr_window)
        self._atr_mult = float(atr_mult)
        self._caches: list[pd.DataFrame] = [self._load_cache(p) for p in self._paths]

    @property
    def freshness_threshold(self) -> int:
        """信号新鲜度阈值（**交易日 lag**；0=信号须覆盖最近一个已收盘交易日）。

        供调用方（调度器运行时告警 / 健康自检）判定信号是否陈旧，避免各处重复读配置。
        """
        return self._freshness_threshold

    @property
    def trading_calendar(self) -> tuple[date, ...]:
        """生效的交易日历（显式注入优先；否则主湖并集日历，懒加载 + 进程内缓存）。"""
        if self._calendar is None:
            self._calendar = load_trading_calendar()
        return self._calendar

    def _load_cache(self, cache_path: Path) -> pd.DataFrame:
        """读取信号缓存并规范化列/类型；缺失时抛 FileNotFoundError（启动期致命）。"""
        if not cache_path.exists():
            raise FileNotFoundError(f"信号缓存缺失：{cache_path}")
        df = pd.read_parquet(cache_path)
        df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
        df = df.sort_values(["symbol", "ts"]).reset_index(drop=True)
        return df

    # ------------------------------------------------------------------
    # 主信号（多源级联）
    # ------------------------------------------------------------------
    def latest_signal(self, symbol: str, asof: Any = None) -> Optional[SignalFrame]:
        """按优先序取 ``symbol`` 在 ``asof`` 之前（含）最新的信号。

        - 全部源无信号 → None（调用方走技术兜底 / 禁开新仓）。
        - 主源新鲜（freshness_days <= 阈值）→ 返回主源。
        - 主源过期但兜底源有**更新**信号 → 返回兜底源信号（source 标注 engine_a_fbN）。
        - 兜底源也过期/无更新 → 返回主源（维持原语义，is_effective 按新鲜度判定）。
        - ``source`` 标注信号源（engine_a=主源 / engine_a_fbN=第 N 兜底源），供审计。
        """
        asof_dt = pd.Timestamp(asof).tz_localize(None) if asof is not None else pd.Timestamp.now()
        # 收集每个源在 asof 前最新的信号
        rows: list[tuple[int, pd.Series, int]] = []
        for i, df in enumerate(self._caches):
            sub = df[df["symbol"] == symbol]
            sub = sub[sub["ts"] <= asof_dt]
            if sub.empty:
                continue
            row = sub.iloc[-1]
            fd = self.freshness_days(symbol, asof_dt, row["ts"])
            rows.append((i, row, fd))
        if not rows:
            return None

        # 主源
        i0, row0, fd0 = rows[0]
        if fd0 <= self._freshness_threshold:
            return self._build_frame(symbol, row0, i0, fd0)

        # 主源过期：找兜底源中「比主源更新」的信号（取最新者）
        best: Optional[tuple[int, pd.Series, int]] = None
        ts0 = pd.Timestamp(row0["ts"])
        for i, row, fd in rows[1:]:
            if pd.Timestamp(row["ts"]) > ts0:
                if best is None or pd.Timestamp(row["ts"]) > pd.Timestamp(best[1]["ts"]):
                    best = (i, row, fd)
        if best is not None:
            i, row, fd = best
            return self._build_frame(symbol, row, i, fd)

        # 兜底源也过期/无更新 → 返回主源（原语义）
        return self._build_frame(symbol, row0, i0, fd0)

    def _build_frame(self, symbol: str, row: pd.Series, source_idx: int, fd: int) -> SignalFrame:
        """由缓存行构造 SignalFrame（freshness 过期 → is_effective=False）。"""
        effective = bool(row["is_effective"]) and fd <= self._freshness_threshold
        return SignalFrame(
            symbol=symbol,
            ts=pd.Timestamp(row["ts"]).to_pydatetime(),
            p_up=float(row["p_up"]),
            exp_ret=float(row["exp_ret"]),
            is_effective=effective,
            source="engine_a" if source_idx == 0 else f"engine_a_fb{source_idx}",
            freshness_days=fd,
        )

    def freshness_days(self, symbol: str, asof: Any, sig_ts: Any = None) -> int:
        """信号新鲜度 = **交易日历 lag**（见 :func:`_trading_lag`）。

        - ``0``   → 信号覆盖最近一个已收盘交易日（标准 T+1）→ 新鲜
        - ``N>0`` → 落后 N 个交易日 → 过期
        - ``10**9`` → 无法判定（无信号 / 日历不可用 / 信号日不是交易日），
          按**保守拦截**处理并打 WARNING —— 门禁宁可不放，也不静默放行。
        """
        if sig_ts is None:
            sig = self.latest_signal(symbol, asof)
            if sig is None:
                return 10 ** 9
            sig_ts = sig.ts
        # ⛔ 必须传**完整 asof**（含时刻）而非仅日期：夜盘时当日日盘已收盘，
        #    ref 应取当日；只传日期会被判为未收盘，漏掉一个已收盘交易日（P0-3 事故口径）。
        cal = self._calendar_for(_to_date(asof))
        lag = _trading_lag(_to_date(sig_ts), asof, cal, self._day_close)
        if lag is None:
            # ⛔ 静默放行是门禁最危险的行为：无法判定时必须显式告警 + 保守拦截
            # ⛔ 占位符必须是 loguru 的 {} 风格：get_logger 走 str.format，写 %s 会原样输出
            log.warning(
                "新鲜度无法判定 → 保守拦截（symbol={}, sig_ts={}, asof={}, 日历天数={}）；"
                "检查主湖日线是否缺失或信号日是否为非交易日",
                symbol, sig_ts, asof, len(cal),
            )
            return 10 ** 9
        return int(lag)

    # ------------------------------------------------------------------
    # 日历增补（P3-C 补丁：消除「刷新成功反被拦」的行为反转）
    # ------------------------------------------------------------------
    def _signal_days(self) -> frozenset[date]:
        """缓存中出现过的全部信号日（跨所有源，进程内缓存）。"""
        if self._signal_days_cache is None:
            days: set[date] = set()
            for df in self._caches:
                if df.empty or "ts" not in df.columns:
                    continue
                days |= set(pd.to_datetime(df["ts"]).dt.date)
            self._signal_days_cache = frozenset(days)
        return self._signal_days_cache

    def _calendar_for(self, ref_day: Optional[date]) -> tuple[date, ...]:
        """生效日历（委托 :func:`augment_calendar`，勿在此另写一套增补逻辑）。

        ⛔ 为什么必须增补（P3-C 补丁，2026-08-31；QA 独立复核揪出）：

        主湖日线要等次日 08:00 补数才入库，于是**夜盘时刻主湖永远没有当日**。
        打上「夜盘 R 必须严格命中当日」的补丁后，若日历仍只有主湖数据，就会出现
        **行为反转** —— 20:30 老老实实刷新出当日信号的，因为当日不在日历而
        ``lag=None`` 被拦；刷新漏跑、缓存停在昨天的，反而被放行。
        """
        return augment_calendar(
            self.trading_calendar, ref_day, self._signal_days(), self._holidays
        )

    def has_symbol(self, symbol: str) -> bool:
        return any(symbol in set(df["symbol"].unique()) for df in self._caches)

    def cache_latest_ts(self, symbol: str) -> Optional[datetime]:
        """跨全部源取最新信号时间戳（取所有源中的最大值）。"""
        latest: Optional[pd.Timestamp] = None
        for df in self._caches:
            sub = df[df["symbol"] == symbol]
            if sub.empty:
                continue
            ts = pd.Timestamp(sub["ts"].max())
            if latest is None or ts > latest:
                latest = ts
        return latest.to_pydatetime() if latest is not None else None

    # ------------------------------------------------------------------
    # 技术指标兜底（双均线 + ATR 通道）
    # ------------------------------------------------------------------
    def technical_fallback(self, symbol: str, bars: pd.DataFrame) -> Optional[SignalFrame]:
        """技术指标兜底（双均线 + ATR 通道），仅作**降级方向提示**。

        返回 ``is_effective=False`` 的帧：技术指标只能给出方向（p_up），
        无法校准「预期日收益率」(exp_ret)，因此不构成模型验证过的 edge。
        配合 P0-3 隔夜过期「无持仓禁开」硬约束，兜底信号不会驱动新开仓
        （``RiskGate._intent`` 对 ``is_effective=False`` 返回 0），
        仅保留 p_up 供人工参考 / 有持仓时风控管理。数据不足返回 None。
        """
        if bars is None or bars.empty:
            return None
        close = pd.to_numeric(bars["close"], errors="coerce").dropna()
        need = max(self._slow_ma + 1, self._atr_window + 2)
        if len(close) < need:
            return None
        high = pd.to_numeric(bars["high"], errors="coerce")
        low = pd.to_numeric(bars["low"], errors="coerce")
        fast = close.rolling(self._fast_ma).mean().iloc[-1]
        slow = close.rolling(self._slow_ma).mean().iloc[-1]
        tr = pd.concat(
            [
                high - low,
                (high - close.shift(1)).abs(),
                (low - close.shift(1)).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = tr.rolling(self._atr_window).mean().iloc[-1]
        if slow <= 0 or atr <= 0 or not np.isfinite(slow) or not np.isfinite(atr):
            return None
        last = close.iloc[-1]
        upper, lower = slow + self._atr_mult * atr, slow - self._atr_mult * atr
        if last > upper:
            p_up = 0.65
        elif last < lower:
            p_up = 0.35
        elif fast > slow:
            p_up = 0.55
        elif fast < slow:
            p_up = 0.45
        else:
            return None
        # exp_ret 不提供真实期望收益估计：技术指标仅给出方向，无法校准「预期日收益率」。
        # 若把「上一日已实现涨跌幅」当 exp_ret 喂给成本门禁，会在「昨日跌+弱多」时误拦、
        # 「昨日涨+弱多」时误放，完全取决于历史噪音，与未来期望无关（审计 P1-1）。
        # 故 exp_ret 置中性 0.0，并令 is_effective=False（降级 substitute，不构成 edge）：
        # 配合 P0-3 隔夜过期「无持仓禁开」硬约束，不会驱动任何新开仓（审计 P1-2）。
        exp_ret = 0.0
        ts = bars.index[-1]
        if not isinstance(ts, datetime):
            ts = pd.Timestamp(ts).to_pydatetime()
        return SignalFrame(
            symbol=symbol,
            ts=ts,
            p_up=float(p_up),
            exp_ret=exp_ret,
            is_effective=False,
            source="technical",
            freshness_days=0,
        )

    # ------------------------------------------------------------------
    # 风控专用中性信号（有持仓但信号缺失时：仅风控管理）
    # ------------------------------------------------------------------
    @staticmethod
    def neutral_signal(symbol: str, asof: Any = None) -> SignalFrame:
        """中性信号：p_up=0.5、is_effective=False（不驱动新开仓，仅触发风控评估）。"""
        ts = pd.Timestamp(asof).to_pydatetime() if asof is not None else datetime.now()
        return SignalFrame(
            symbol=symbol,
            ts=ts,
            p_up=0.5,
            exp_ret=0.0,
            is_effective=False,
            source="risk_only",
            freshness_days=10 ** 9,
        )
