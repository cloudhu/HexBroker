"""新浪期货公开接口（sina）免费数据源 —— 分钟线/实时/主力连续辅助源（P0）。

无需 key，直接 ``requests`` 抓取内盘期货公开接口。仅给**主力/指数连续**（无全部合约），
适合做分钟线/实时冗余与 pytdx 主源互相印证。设计要点：

- 复用 ``DataSource`` 基类与 ``BarFrame`` 契约。
- 主力连续代码：``cu0`` / ``rb0`` / ``sc0``；日线 ``getInnerFuturesDailyKLine``、
  60min ``getInnerFuturesMiniKLine60m``。
- 超时 ≤10s；请求间小 sleep 限频保护；无法连接清晰报错（不静默）。
- 新浪不返回 ``amount``/``open_interest``，按契约补 0.0；**禁止前复权**，``adj_close``
  初值等同 ``close``，换月拼接复用 ``ContractStitcher``。
- 依赖缺失时 ``health_check`` 返回 False，``fetch_bars`` 抛明确异常。
"""

from __future__ import annotations

import time
from typing import Optional

import pandas as pd

from ... import HexConfigError, HexDataError
from ..base import DataSource
from ..schema import BarFrame
from ..store import DataLake

# 新浪内盘期货公开接口 base
SINA_BASE = "http://stock2.finance.sina.com.cn/futures/api"

# 本项目 freq -> (脚本文件, 服务名)
FREQ_TO_SERVICE: dict[str, tuple[str, str]] = {
    "1d": ("json.php", "IndexService.getInnerFuturesDailyKLine"),
    "60m": ("json_v2.php", "IndexService.getInnerFuturesMiniKLine60m"),
    # 扩展：分钟线可按需开启（新浪支持 1/5/15/30/60m）
    "30m": ("json_v2.php", "IndexService.getInnerFuturesMiniKLine30m"),
    "15m": ("json_v2.php", "IndexService.getInnerFuturesMiniKLine15m"),
    "5m": ("json_v2.php", "IndexService.getInnerFuturesMiniKLine5m"),
    "1m": ("json_v2.php", "IndexService.getInnerFuturesMiniKLine1m"),
}

# 单合约代码后缀识别（4 字母品种 + 4 位数字，如 CU2609）
CONTINUOUS_SUFFIX = "0"


class SinaSource(DataSource):
    """新浪期货免费源（主力连续日线/分钟线）。"""

    name = "sina"

    def __init__(
        self,
        timeout: float = 10.0,
        rate_limit_sleep: float = 0.3,
        root: Optional[str] = None,
        save: bool = True,
    ) -> None:
        """初始化新浪源。

        参数
        ----
        timeout : 单次请求超时（秒），默认 10s。
        rate_limit_sleep : 请求间限频保护间隔（秒），默认 0.3s。
        root : ``DataLake`` 根目录（落 Parquet），默认 ``data/raw``。
        save : 取数后是否落 Parquet，默认 True。
        """
        self.timeout = timeout
        self.rate_limit_sleep = rate_limit_sleep
        self.save = save
        self.lake = DataLake(root if root is not None else "data/raw")

    # ---- 依赖检查 --------------------------------------------------------
    @staticmethod
    def _require_requests():
        try:
            import requests  # 懒加载
        except ImportError as exc:
            raise HexConfigError(
                "未安装 requests，无法使用新浪数据源。请 pip install requests，"
                "或使用 --source csv。"
            ) from exc
        return requests

    # ---- 符号解析 --------------------------------------------------------
    @staticmethod
    def _resolve_symbol(symbol: str) -> str:
        """解析为 sina 合约代码。

        - ``SHFE.cu`` -> ``cu0``（主力连续）
        - ``cu0`` / ``rb0`` / ``sc0`` -> 原样
        - ``CU2609`` -> 原样（新浪内盘 K 线亦支持单合约，失败将明确报错）
        """
        s = symbol.strip()
        if "." in s:
            product = s.split(".", 1)[1].lower()
            return f"{product}{CONTINUOUS_SUFFIX}"
        return s.lower()

    # ---- 抓取 ------------------------------------------------------------
    def _fetch_one(self, code: str, freq: str) -> pd.DataFrame:
        """抓单合约/连续的 K 线，返回单 symbol 的 DataFrame。"""
        requests = self._require_requests()
        if freq not in FREQ_TO_SERVICE:
            raise HexConfigError(f"新浪不支持 freq={freq!r}，可选 {list(FREQ_TO_SERVICE)}")
        script, service = FREQ_TO_SERVICE[freq]
        url = f"{SINA_BASE}/{script}/{service}?symbol={code}"
        try:
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise HexDataError(f"新浪抓取 {code}({freq}) 失败：{exc}") from exc
        if not data:
            raise HexDataError(f"新浪返回 {code}({freq}) 空数据（合约可能不支持）")
        # 新浪返回最新在前，需反转回时间升序
        data = list(reversed(data))
        return self._rows_to_frame(data, code)

    @staticmethod
    def _normalize_rows(data: list) -> list[dict]:
        """归一化新浪返回为 dict 列表。

        新浪内盘期货接口返回**列表的列表**（非 dict）：
        ``[date, open, high, low, close, volume]``（60min 字段相同，date 含时间）。
        若返回 dict 列表（部分版本）则原样使用。
        """
        cols = ["date", "open", "high", "low", "close", "volume"]
        norm: list[dict] = []
        for row in data:
            if isinstance(row, dict):
                norm.append(row)
            elif isinstance(row, (list, tuple)):
                norm.append(dict(zip(cols, row)))
            else:
                raise HexDataError(f"新浪返回行格式无法解析: {type(row)}")
        return norm

    @staticmethod
    def _repair_ohlc(df: pd.DataFrame) -> pd.DataFrame:
        """免费源偶有脏数据：修复 OHLC 包络（low<=open,close<=high）并丢弃废 bar。

        - 包络破坏（open>high 等）：以四价极值重定 low/high，保证契约成立；
        - close<=0 或 open/high/low<=0 的废 bar（无成交/异常开盘）：直接丢弃
          （如 m0 2019-07-29 open=0）。
        """
        for c in ["open", "high", "low", "close"]:
            df[c] = df[c].astype(float)
        lo = df[["open", "high", "low", "close"]].min(axis=1)
        hi = df[["open", "high", "low", "close"]].max(axis=1)
        df["low"] = lo
        df["high"] = hi
        df = df[df["close"] > 0].copy()
        df = df[(df[["open", "high", "low"]] > 0).all(axis=1)].copy()
        return df

    @staticmethod
    def _rows_to_frame(data: list, symbol: str) -> pd.DataFrame:
        """将新浪返回行映射为 BarFrame 单 symbol DataFrame。"""
        rows = SinaSource._normalize_rows(data)
        df = pd.DataFrame.from_records(rows)
        # 新浪字段可能为 vol / volume，统一为 volume
        if "vol" in df.columns and "volume" not in df.columns:
            df = df.rename(columns={"vol": "volume"})
        required = ["date", "open", "high", "low", "close"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise HexDataError(f"新浪返回缺少列: {missing}")
        df = SinaSource._repair_ohlc(df)
        if df.empty:
            raise HexDataError(f"新浪返回 {symbol} 经清洗后无有效 bar")
        df["symbol"] = symbol
        df = df.rename(columns={"date": "datetime"})
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
        # 按 (symbol, datetime) 排序，兼容新浪最新在前的顺序
        df = df.sort_values(["symbol", "datetime"]).reset_index(drop=True)
        for col in ["volume", "amount", "open_interest"]:
            if col not in df.columns:
                df[col] = 0.0
            else:
                df[col] = df[col].astype(float)
        # 新浪不提供 amount/open_interest，补 0；不复权
        df["amount"] = df["amount"].astype(float)
        df["open_interest"] = df["open_interest"].astype(float)
        df["raw_close"] = df["close"].astype(float)
        df["adj_close"] = df["close"].astype(float)
        df["limit_up"] = False
        df["limit_down"] = False
        df["is_rollover"] = False
        df = df.set_index(["symbol", "datetime"]).sort_index()
        return df

    # ---- 公共取数接口 ----------------------------------------------------
    def fetch_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
        freq: str = "1d",
        save: Optional[bool] = None,
    ) -> BarFrame:
        """拉取指定品种/区间/频率的 BarFrame（新浪期货公开接口）。"""
        frames: list[pd.DataFrame] = []
        for sym in symbols:
            code = self._resolve_symbol(sym)
            df = self._fetch_one(code, freq)
            frames.append(df)
            if self.rate_limit_sleep:
                time.sleep(self.rate_limit_sleep)

        out = pd.concat(frames)
        out = self._clip_range(out, start, end)

        bf = BarFrame(df=out, freq=freq, source="sina")
        do_save = self.save if save is None else save
        if do_save:
            try:
                self.lake.save_processed(bf)
            except Exception as exc:
                raise HexDataError(f"新浪落 Parquet 失败：{exc}") from exc
        return bf.validate()

    def health_check(self) -> bool:
        try:
            import requests  # noqa: F401

            return True
        except ImportError:
            return False
