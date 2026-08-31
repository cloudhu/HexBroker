"""天勤 TQSDK 数据源 —— 主力连续 K 线（统一数据源模块的推送式主通道）。

2026-08-30 由骨架升级为完整实现（数据源审计 P0 事故驱动：sina 旧端点冻结于
2024-07-17 静默喂陈数据，需第二独立通道互为对账）。

设计要点（与 sina/pytdx 等源同构，复用 ``DataSource`` 基类与 ``BarFrame`` 契约）：

- 主力连续映射：``ag0 → KQ.m@SHFE.ag``（tqsdk 连续合约体系，与湖内 sym0 一一对应）；
- 凭据：构造参数 > ``TQSDK_USER``/``TQSDK_PASS`` 环境变量 >
  ``scripts/collector_poc/tqsdk_auth.local.json``（已入 git 本地排除，永不提交）；
- 禁止前复权：期货原始价，``adj_close`` 初值等同 ``close``，换月拼接复用
  ``ContractStitcher``（与 sina 源同规则）；
- tqsdk 未安装 / 凭据缺失 → ``health_check`` False / ``fetch_bars`` 抛 ``HexConfigError``，
  不静默降级；连接/拉取失败抛 ``HexNetworkError``，清晰报错。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import pandas as pd

from ... import HexConfigError, HexDataError, HexEmptyDataError, HexNetworkError
from ..base import DataSource
from ..schema import BarFrame

# sym0 -> tqsdk 主力连续合约（18 品种全集，对应 data/raw/processed/ 分区）
TQ_SYMBOLS: dict[str, str] = {
    "ag0": "KQ.m@SHFE.ag",
    "al0": "KQ.m@SHFE.al",
    "au0": "KQ.m@SHFE.au",
    "cu0": "KQ.m@SHFE.cu",
    "hc0": "KQ.m@SHFE.hc",
    "ni0": "KQ.m@SHFE.ni",
    "rb0": "KQ.m@SHFE.rb",
    "zn0": "KQ.m@SHFE.zn",
    "i0": "KQ.m@DCE.i",
    "j0": "KQ.m@DCE.j",
    "jm0": "KQ.m@DCE.jm",
    "m0": "KQ.m@DCE.m",
    "p0": "KQ.m@DCE.p",
    "y0": "KQ.m@DCE.y",
    # CZCE 合约代码为全大写（tqsdk 合约服务小写报 non-existent，2026-08-30 实测）
    "cf0": "KQ.m@CZCE.CF",
    "sr0": "KQ.m@CZCE.SR",
    "ta0": "KQ.m@CZCE.TA",
    "sc0": "KQ.m@INE.sc",
}

# freq -> tqsdk K 线周期（秒）
TQ_FREQ: dict[str, int] = {"1d": 86400, "8h": 28800, "60m": 3600, "15m": 900, "1m": 60}

_AUTH_FILE = Path(__file__).resolve().parents[3] / "scripts/collector_poc/tqsdk_auth.local.json"


def load_tqsdk_auth(username: Optional[str] = None, password: Optional[str] = None) -> tuple[str, str]:
    """加载天勤凭据：构造参数 > 环境变量 > 本地凭据文件（不入库）。"""
    user = username or os.environ.get("TQSDK_USER")
    pwd = password or os.environ.get("TQSDK_PASS")
    if user and pwd:
        return user, pwd
    if _AUTH_FILE.exists():
        obj = json.loads(_AUTH_FILE.read_text(encoding="utf-8"))
        return obj["user"], obj["pass"]
    raise HexConfigError(
        "缺少天勤凭据：构造传参、设 TQSDK_USER/TQSDK_PASS，"
        f"或创建 {_AUTH_FILE}（格式 {{\"user\": ..., \"pass\": ...}}，勿提交入库）"
    )


class TqsdkSource(DataSource):
    """TQSDK 主力连续数据源（推送式通道的历史快照用法）。"""

    name = "tqsdk"
    max_stale_days = 7  # tqsdk 数据及时性好，容忍度比 sina 略紧

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        # 官方上限 10000 根/序列（API 参考；使用指南旧版写 8000），取 8000 留安全边际
        data_length_cap: int = 8000,
    ) -> None:
        self.username = username
        self.password = password
        self.data_length_cap = data_length_cap
        self._api = None

    def _import_tqsdk(self):
        try:
            import tqsdk  # noqa: F401
        except ImportError as e:
            raise HexConfigError(
                "未安装 tqsdk，无法使用 TQSDK 数据源。请 pip install tqsdk，或使用 --source csv。"
            ) from e

    # ---- 拉取 --------------------------------------------------------
    def fetch_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
        freq: str = "1d",
    ) -> BarFrame:
        from tqsdk import TqApi, TqAuth

        self._import_tqsdk()
        dur = TQ_FREQ.get(freq)
        if dur is None:
            raise HexConfigError(f"tqsdk 源不支持 freq={freq!r}（支持 {sorted(TQ_FREQ)}）")
        unknown = [s for s in symbols if s not in TQ_SYMBOLS]
        if unknown:
            raise HexConfigError(f"tqsdk 源未配置主力连续映射的品种: {unknown}")

        user, pwd = load_tqsdk_auth(self.username, self.password)
        n_days = max(1, (pd.Timestamp(end) - pd.Timestamp(start)).days + 2)
        bars_per_day = max(1, 86400 // dur) if dur < 86400 else 1
        data_length = min(self.data_length_cap, int(n_days * bars_per_day * 1.6) + 10)

        frames: list[pd.DataFrame] = []
        t0 = time.time()
        try:
            api = TqApi(auth=TqAuth(user, pwd))
        except Exception as exc:  # 连接/认证失败 → 明确网络错误
            raise HexNetworkError(f"tqsdk 连接失败：{exc}", source="tqsdk") from exc
        try:
            for sym in symbols:
                klines = api.get_kline_serial(TQ_SYMBOLS[sym], dur, data_length=data_length)
                # 官方模式（2026-08-30 文档核对）：get_kline_serial 返回不保证历史取齐，
                # 须 is_serial_ready + wait_update 确认（小 data_length 通常立即就绪）。
                ready_deadline = time.time() + 30
                while not api.is_serial_ready(klines):
                    if not api.wait_update(deadline=ready_deadline):
                        raise HexNetworkError(
                            f"tqsdk {sym} 历史数据 30s 内未取齐（is_serial_ready=False），"
                            "拒绝部分数据", source="tqsdk")
                df = self._klines_to_frame(klines, sym)
                if df.empty:
                    raise HexEmptyDataError(
                        f"tqsdk 未取到 {sym} 的任何 bar（freq={freq}）", source="tqsdk"
                    )
                frames.append(df)
        except HexDataError:
            raise
        except Exception as exc:
            raise HexNetworkError(f"tqsdk 拉取失败：{exc}", source="tqsdk") from exc
        finally:
            try:
                api.close()
            except Exception:
                pass
        elapsed = time.time() - t0

        out = pd.concat(frames)
        bf = self._finalize(out, start, end, freq, symbols=symbols, source="tqsdk")
        bf.metadata["fetch_seconds"] = round(elapsed, 2)
        return bf

    @staticmethod
    def _klines_to_frame(klines: pd.DataFrame, sym: str) -> pd.DataFrame:
        """tqsdk klines → 契约帧（MultiIndex(symbol, datetime)，价格零换算零复权）。"""
        recs = []
        for _, r in klines.iterrows():
            ts = r.get("datetime")
            if pd.isna(ts) or int(ts) <= 0 or pd.isna(r.get("close")):
                continue
            dt = (pd.to_datetime(int(ts), unit="ns", utc=True)
                  .tz_convert("Asia/Shanghai").tz_localize(None))
            c = float(r["close"])
            recs.append({
                "open": float(r["open"]), "high": float(r["high"]),
                "low": float(r["low"]), "close": c,
                "volume": float(r["volume"]) if pd.notna(r["volume"]) else 0.0,
                "open_interest": float(r.get("open_oi", 0) or 0),
                "amount": 0.0,  # tqsdk 免费接口无成交额，按契约补 0（同 sina 规则）
                "adj_close": c, "raw_close": c,  # 禁止前复权：初值等同 close
                "datetime": dt, "symbol": sym,
            })
        if not recs:
            return pd.DataFrame()
        df = pd.DataFrame(recs)
        df = df.drop_duplicates(subset=["symbol", "datetime"], keep="last")
        df = df.sort_values(["symbol", "datetime"])
        df = df.set_index(["symbol", "datetime"])
        return df

    # ---- 健康检查 ----------------------------------------------------
    def health_check(self) -> bool:
        try:
            self._import_tqsdk()
            load_tqsdk_auth(self.username, self.password)
            return True
        except HexDataError:
            return False
