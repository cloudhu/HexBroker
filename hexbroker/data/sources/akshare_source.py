"""AkShare 免费日线备份源（可选；接口不稳，仅备份）。

依赖缺失时优雅降级。

2026-08 实测（主理人独立取证）
-----------------------------
* ``ak.futures_main_sina`` 是当前**唯一**"纯脚本 + 无 token + 无配额 + 数据新鲜"的
  免费链路：18/18 品种末日均为当日，单次调用 0.29–0.52s，深度 2046–5271 行。
  其底层正是新浪新端点 ``InnerFuturesNewService.getDailyKLine``。
* **返回列是中文**：``日期 开盘价 最高价 最低价 收盘价 成交量 持仓量 动态结算价``。
  早期实现按英文列名 ``{"date": "datetime", ...}`` 做 ``rename``，对中文列名完全无效，
  导致下游 ``KeyError: 'datetime'`` —— 该源**从未跑通过**，但被 mock 测试掩盖。
* 与 pandadata 相反，akshare 对大小写**不敏感**（``rb0`` / ``RB0`` 均可）；
  但本实现仍统一输出大写，保持与 pandadata 一致的调用约定。
* 不支持具体月份合约（如 ``RB2609``），仅支持主力连续（``RB0``）。
"""

from __future__ import annotations

import re
from typing import Optional

import pandas as pd

from ... import HexConfigError, HexDataError, HexEmptyDataError, HexNetworkError
from ..base import DataSource
from ..schema import BarFrame
from ..store import DataLake

#: akshare ``futures_main_sina`` 中文列名 -> 标准列名
AK_COLUMN_MAP: dict[str, str] = {
    "日期": "datetime",
    "开盘价": "open",
    "最高价": "high",
    "最低价": "low",
    "收盘价": "close",
    "成交量": "volume",
    "持仓量": "open_interest",
    "动态结算价": "settlement",
}

#: 具体月份合约（如 RB2609）—— futures_main_sina 不支持
_CONTRACT_RE = re.compile(r"^[A-Z]{1,2}\d{3,4}$")


class AkshareSource(DataSource):
    """AkShare 免费期货日线源（主力连续，底层走新浪新端点）。"""

    name = "akshare"

    def __init__(self, root: Optional[str] = None, save: bool = True) -> None:
        """初始化 AkShare 源。

        与其他源保持一致的构造/调用签名，便于故障切换编排器无差别调用。

        参数
        ----
        root : ``DataLake`` 根目录（落 Parquet），默认 ``data/raw``。
        save : 取数后是否落 Parquet，默认 True。
        """
        self.save = save
        self.lake = DataLake(root if root is not None else "data/raw")

    def fetch_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
        freq: str = "1d",
        save: Optional[bool] = None,
    ) -> BarFrame:
        if freq != "1d":
            raise HexConfigError(f"AkShare 仅支持 freq='1d'，收到 {freq!r}")
        try:
            import akshare as ak  # type: ignore
        except ImportError as e:
            raise HexConfigError(
                "未安装 akshare，无法使用 AkShare 数据源。请 pip install akshare，或使用 --source csv。"
            ) from e
        if not hasattr(ak, "futures_main_sina"):
            raise HexDataError(
                "当前 akshare 版本缺少 futures_main_sina 接口，请升级 akshare（实测 1.18.91 可用）。"
            )

        frames = []
        for sym in symbols:
            code = self._resolve_symbol(sym)
            try:
                df = ak.futures_main_sina(symbol=code)
            except Exception as exc:  # akshare 内部异常类型不稳定
                raise HexNetworkError(f"AkShare 抓取 {sym}({code}) 失败：{exc}") from exc
            if df is None or len(df) == 0:
                raise HexEmptyDataError(
                    f"AkShare 未取得 {sym}({code}) 数据（接口可能变更）", source="akshare"
                )

            # 中文列名 -> 标准列名；缺失列不静默丢弃，交由契约校验报错
            renamed = df.rename(columns=AK_COLUMN_MAP)
            missing = [c for c in ("datetime", "open", "high", "low", "close") if c not in renamed.columns]
            if missing:
                raise HexDataError(
                    f"AkShare {sym}({code}) 返回列名无法识别，缺少 {missing}。"
                    f"实际列：{list(df.columns)}"
                )

            out = renamed[list(set(AK_COLUMN_MAP.values()) & set(renamed.columns))].copy()
            out["symbol"] = self._symbol_key(sym)
            out["datetime"] = pd.to_datetime(out["datetime"]).dt.tz_localize(None)
            out = out.sort_values(["symbol", "datetime"]).reset_index(drop=True)
            for col in ("volume", "open_interest"):
                if col not in out.columns:
                    out[col] = 0.0
                else:
                    out[col] = out[col].astype(float)
            out["amount"] = 0.0  # akshare 不提供成交额
            out["raw_close"] = out["close"].astype(float)
            out["adj_close"] = out["close"].astype(float)  # 名义价，不复权
            out["limit_up"] = False
            out["limit_down"] = False
            out["is_rollover"] = False
            frames.append(out.set_index(["symbol", "datetime"]))

        if not frames:
            raise HexEmptyDataError(f"AkShare 未取到任何品种数据（请求 {symbols}）", source="akshare")
        merged = pd.concat(frames).sort_index()
        # 收口：裁剪 + 空结果/新鲜度门禁 + 契约校验
        bf = self._finalize(merged, start, end, freq, symbols=symbols, source="akshare")

        do_save = self.save if save is None else save
        if do_save:
            try:
                self.lake.save_processed(bf)
            except Exception as exc:  # noqa: BLE001
                raise HexDataError(f"AkShare 落 Parquet 失败：{exc}") from exc
        return bf

    # ---- 符号解析 --------------------------------------------------------
    @staticmethod
    def _resolve_symbol(symbol: str) -> str:
        """解析为 akshare ``futures_main_sina`` 所需的主力连续代码（如 ``RB0``）。

        - ``SHFE.cu`` -> ``CU0``
        - ``rb0`` / ``RB0`` -> ``RB0``
        - ``RB2609`` -> 抛错（本接口仅支持主力连续）
        """
        s = symbol.strip()
        product = s.split(".", 1)[1] if "." in s else s
        code = product.upper()
        if _CONTRACT_RE.match(code):
            raise HexConfigError(
                f"AkShare futures_main_sina 仅支持主力连续（如 RB0），不支持具体月份合约 {symbol!r}。"
                "如需单合约数据请使用其他源。"
            )
        if not code.endswith("0"):
            code += "0"
        return code

    @staticmethod
    def _symbol_key(symbol: str) -> str:
        """输出侧的 symbol 键，统一主力连续小写形式（如 ``rb0``）。"""
        s = symbol.strip()
        product = s.split(".", 1)[1] if "." in s else s
        code = product.lower()
        return code if code.endswith("0") else code + "0"

    def health_check(self) -> bool:
        try:
            import akshare  # noqa: F401

            return True
        except ImportError:
            return False
