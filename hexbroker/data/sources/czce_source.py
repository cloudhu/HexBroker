"""郑商所（CZCE）官方每日行情 ``.txt`` 免费数据源（P1 官方源接入）。

端点（2026-08-29 主理人实测）：
``https://www.czce.com.cn/cn/DFSStaticFiles/Future/{yyyy}/{yyyymmdd}/FutureDataDaily.txt``
→ HTTP 200 / ~37 KB / 268 行，``http://`` 会 301 到 ``https://``（直接用 https）。
注意官网 ``.htm`` 页面返回 412，只有静态 ``.txt`` 可达。独立于新浪/akshare
的同一上游（§2.3），是**真正独立的备源**。

设计要点：

- 管道分隔 14 列（表头文字定位，不依赖列序）：
  ``合约代码|昨结算|今开盘|最高价|最低价|今收盘|今结算|涨跌1|涨跌2|成交量(手)|持仓量|增减量|成交额(万元)|交割结算价``；
  数字带千分位逗号，``小计``/``合计`` 行跳过。
- **主力连续语义差异（大声声明）**：官方只有分合约行情，无主力连续。
  ``cf0`` 形态解析为品种 ``CF``，**逐日取持仓量（OI）最大的合约**近似主力 ——
  换月切换日与新浪的主力规则可能不同日，交叉校验容差应放宽预期。
  单合约（``CF701``）原样直取。
- 品种覆盖：仅郑商所（CF/SR/TA/AP/...）；其它品种请求会得到
  ``HexEmptyDataError``（备源链据此清晰归因降级）。
- 非交易日/未发布 → 404 → 跳过该日并记录告警；网络异常 → ``HexNetworkError``
  （不静默）。全部日期无数据 → ``HexEmptyDataError``。
- ``datetime`` = 交易日 00:00（与 sina 日线惯例一致）；``amount`` 万元 → 元（×1e4）；
  官方不复权，``adj_close`` 初值等同 ``close``；包络修复委托 ``repair_envelope``。
"""

from __future__ import annotations

import re
import time
from typing import Optional

import pandas as pd

from ... import HexConfigError, HexDataError, HexEmptyDataError, HexNetworkError
from ..base import DataSource
from ..schema import BarFrame, repair_envelope
from ..store import DataLake

CZCE_BASE = "https://www.czce.com.cn/cn/DFSStaticFiles/Future"

#: 表头文字 → 标准列名（未列出的列不进 BarFrame）
CZCE_COLUMN_MAP: dict[str, str] = {
    "合约代码": "_contract",
    "今开盘": "open",
    "最高价": "high",
    "最低价": "low",
    "今收盘": "close",
    "今结算": "settlement",
    "成交量(手)": "volume",
    "持仓量": "open_interest",
    "成交额(万元)": "amount_wan",
}

#: 单合约代码形态：1-2 位大写品种 + 3-4 位合约月
CONTRACT_RE = re.compile(r"^([A-Z]{1,2})\d{3,4}$")

#: 主力连续形态：品种（大小写均可）+ "0"（如 cf0 / SR0）
CONTINUOUS_RE = re.compile(r"^([A-Za-z]{1,2})0$")


def _to_float(text: str) -> float:
    """'7,438.00  ' → 7438.0；空串 → NaN。"""
    t = text.strip().replace(",", "")
    if not t:
        return float("nan")
    return float(t)


class CzceSource(DataSource):
    """郑商所官方每日行情源（仅 1d）。"""

    name = "czce"

    def __init__(
        self,
        timeout: float = 10.0,
        rate_limit_sleep: float = 0.3,
        root: Optional[str] = None,
        save: bool = True,
    ) -> None:
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
                "未安装 requests，无法使用 CZCE 官方数据源。"
            ) from exc
        return requests

    # ---- URL / 解析 -------------------------------------------------------
    @staticmethod
    def _build_url(day: pd.Timestamp) -> str:
        ds = day.strftime("%Y%m%d")
        return f"{CZCE_BASE}/{day.year}/{ds}/FutureDataDaily.txt"

    @staticmethod
    def _resolve_symbol(symbol: str) -> str:
        """``cf0`` → 品种 ``CF``；``CF701`` → 合约 ``CF701``；其余原样大写。"""
        s = symbol.strip().upper()
        m = CONTINUOUS_RE.match(symbol.strip())
        if m:
            return m.group(1).upper()
        return s

    @staticmethod
    def _parse_table(text: str, day: pd.Timestamp) -> pd.DataFrame:
        """解析单日全表 → DataFrame（列名标准化，剔除小计/合计行）。

        日期优先取标题行括号内（与请求日应一致，不一致以文件为准并信任文件）。
        """
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if len(lines) < 2:
            raise HexDataError(f"CZCE {day.date()} 文件行数不足，无法解析")
        # 标题行：确认是行情表（含"每日行情表"）
        if "每日行情表" not in lines[0]:
            raise HexDataError(
                f"CZCE {day.date()} 首行不是行情表标题：{lines[0][:60]!r}"
            )
        # 表头行：定位"合约代码"所在行
        header_idx = next(
            (i for i, ln in enumerate(lines) if ln.strip().startswith("合约代码")),
            None,
        )
        if header_idx is None:
            raise HexDataError(f"CZCE {day.date()} 未找到表头行（合约代码...）")
        headers = [h.strip() for h in lines[header_idx].split("|")]
        idx_of = {name: i for i, name in enumerate(headers) if name in CZCE_COLUMN_MAP}
        missing = [k for k in CZCE_COLUMN_MAP if k not in idx_of]
        if missing:
            raise HexDataError(f"CZCE {day.date()} 表头缺少列: {missing}")

        rows: list[dict] = []
        for ln in lines[header_idx + 1 :]:
            parts = ln.split("|")
            contract = parts[0].strip()
            if not CONTRACT_RE.match(contract):
                continue  # 小计/合计/空行等非合约行
            row: dict = {"_contract": contract}
            for name, col in CZCE_COLUMN_MAP.items():
                if col == "_contract":
                    continue
                i = idx_of[name]
                row[col] = _to_float(parts[i]) if i < len(parts) else float("nan")
            rows.append(row)
        if not rows:
            raise HexDataError(f"CZCE {day.date()} 无有效合约行")
        return pd.DataFrame.from_records(rows)

    @staticmethod
    def _select_rows(table: pd.DataFrame, resolved: str) -> pd.DataFrame:
        """从单日全表选出目标行：品种 → 当日 OI 最大合约；单合约 → 直取。"""
        if re.match(r"^[A-Z]{1,2}$", resolved):
            sub = table[
                table["_contract"].str.match(r"^" + resolved + r"\d{3,4}$")
            ]
            if sub.empty:
                raise HexEmptyDataError(
                    f"CZCE 无品种 {resolved} 的合约行", source="czce"
                )
            # OI 最大者为当日主力（并列取第一个，保证确定性）
            sub = sub.sort_values("open_interest", ascending=False,
                                  na_position="last")
            return sub.iloc[[0]]
        sub = table[table["_contract"] == resolved]
        if sub.empty:
            raise HexEmptyDataError(
                f"CZCE 无合约 {resolved}", source="czce"
            )
        return sub

    # ---- 抓取 ------------------------------------------------------------
    def _fetch_day(self, day: pd.Timestamp) -> Optional[pd.DataFrame]:
        """抓单日全表。404/未发布 → None（调用方跳过）；网络异常 → 抛。"""
        requests = self._require_requests()
        url = self._build_url(day)
        try:
            resp = requests.get(
                url, timeout=self.timeout,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
        except Exception as exc:
            raise HexNetworkError(f"CZCE 抓取 {day.date()} 失败：{exc}") from exc
        if resp.status_code == 404:
            return None  # 非交易日/未发布
        if resp.status_code != 200:
            raise HexNetworkError(
                f"CZCE {day.date()} HTTP {resp.status_code}（预期 200/404）"
            )
        resp.raise_for_status()
        try:
            resp_text = resp.content.decode("gbk")
        except UnicodeDecodeError:
            resp_text = resp.content.decode("utf-8", errors="replace")
        table = self._parse_table(resp_text, day)
        table["datetime"] = pd.Timestamp(day)
        return table

    def fetch_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
        freq: str = "1d",
        save: Optional[bool] = None,
    ) -> BarFrame:
        if freq != "1d":
            raise HexConfigError(f"CZCE 官方源仅支持 1d，收到 {freq!r}")
        frames: list[pd.DataFrame] = []
        skipped_days: list[str] = []
        for sym in symbols:
            resolved = self._resolve_symbol(sym)
            per_sym: list[pd.DataFrame] = []
            for day in pd.bdate_range(start, end):  # 跳周末；节假日 404 跳过
                table = self._fetch_day(day)
                if table is None:
                    skipped_days.append(str(day.date()))
                    continue
                sel = self._select_rows(table, resolved)
                if not sel.empty:
                    per_sym.append(sel)
                if self.rate_limit_sleep:
                    time.sleep(self.rate_limit_sleep)
            if not per_sym:
                raise HexEmptyDataError(
                    f"CZCE 未取到 {sym}（{start}~{end}）任何数据"
                    f"（跳过 {len(skipped_days)} 个无发布日）",
                    source="czce",
                )
            df = pd.concat(per_sym, ignore_index=True)
            # 主力连续 → 系统约定名（cf0）；单合约 → 官方合约码
            out_sym = sym.lower() if CONTINUOUS_RE.match(sym.strip()) \
                else str(df["_contract"].iloc[0])
            frames.append(self._build_frame(df, out_sym))

        if not frames:
            raise HexEmptyDataError(
                f"CZCE 未取到任何品种数据（请求 {symbols}）", source="czce"
            )
        out = pd.concat(frames)
        bf = self._finalize(out, start, end, freq, symbols=symbols, source="czce")
        do_save = self.save if save is None else save
        if do_save:
            try:
                self.lake.save_processed(bf)
            except Exception as exc:
                raise HexDataError(f"CZCE 落 Parquet 失败：{exc}") from exc
        return bf

    @staticmethod
    def _build_frame(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """单 symbol 行集 → BarFrame 契约 DataFrame。"""
        df = df.copy()
        df, _ = repair_envelope(df, drop_zero_ohl=True)
        if df.empty:
            raise HexDataError(f"CZCE {symbol} 经清洗后无有效 bar")
        df["symbol"] = symbol
        for col in ("volume", "amount_wan", "open_interest", "settlement"):
            if col in df.columns:
                df[col] = df[col].astype(float)
        df["amount"] = df["amount_wan"] * 1e4  # 万元 → 元
        df["raw_close"] = df["close"].astype(float)
        df["adj_close"] = df["close"].astype(float)
        df["limit_up"] = False
        df["limit_down"] = False
        df["is_rollover"] = False
        cols = [
            "symbol", "datetime", "open", "high", "low", "close",
            "volume", "amount", "open_interest", "settlement",
            "adj_close", "raw_close", "limit_up", "limit_down", "is_rollover",
        ]
        df = df[[c for c in cols if c in df.columns]]
        df = df.set_index(["symbol", "datetime"]).sort_index()
        return df

    def health_check(self) -> bool:
        try:
            import requests  # noqa: F401

            return True
        except ImportError:
            return False
