"""预测信号落盘（OOS 隔离层，§3.2 / §8.4）。

``SignalStore`` 是预测层与决策层之间唯一的桥梁：**只落盘样本外（OOS）信号**，
并按 ``(model_id, train_end)`` 分片。RL 环境只能读取 SignalStore，
永远拿不到 ForecastModel 实例——从物理上杜绝训练信息泄漏到决策层。

信号文件布局::

    {root}/{model_id}/{train_end}.parquet
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from ..utils.io import read_parquet, write_parquet
from .base import ForecastSignal


def _ts_file(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y%m%dT%H%M%S")


class SignalStore:
    """OOS 预测信号的列式落盘与检索。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    # ----------------------------- 写入 -----------------------------
    def put(self, signals: Iterable[ForecastSignal]) -> int:
        """写入信号，按 ``(model_id, train_end)`` 分片。返回写入条数。"""
        sigs = list(signals)
        if not sigs:
            return 0
        df = pd.DataFrame([s.to_record() for s in sigs])
        for (mid, te), grp in df.groupby(["model_id", "train_end"]):
            path = self.root / mid / f"{_ts_file(te)}.parquet"
            write_parquet(grp.reset_index(drop=True), path)
        return len(sigs)

    # ----------------------------- 读取 -----------------------------
    def _candidate_files(self, model_id: Optional[str]) -> list[Path]:
        if model_id:
            base = self.root / model_id
            return sorted(base.glob("*.parquet")) if base.exists() else []
        return sorted(self.root.glob("*/*.parquet"))

    def get_frame(
        self,
        model_id: Optional[str] = None,
        symbols: Optional[list[str]] = None,
        start: Optional[object] = None,
        end: Optional[object] = None,
    ) -> pd.DataFrame:
        """读取信号为 MultiIndex(symbol, datetime) 的 DataFrame。"""
        files = self._candidate_files(model_id)
        if not files:
            return pd.DataFrame(
                columns=["p_up", "exp_ret", "vol_hat", "conf", "is_effective"],
                index=pd.MultiIndex.from_arrays([[], []], names=["symbol", "datetime"]),
            )
        frames = [read_parquet(f) for f in files]
        df = pd.concat(frames, ignore_index=True)
        df["ts"] = pd.to_datetime(df["ts"])
        if symbols is not None:
            df = df[df["symbol"].isin(symbols)]
        if start is not None:
            df = df[df["ts"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["ts"] <= pd.Timestamp(end)]
        df = df.set_index(["symbol", "ts"]).sort_index()
        df.index = df.index.set_names(["symbol", "datetime"])
        return df

    def models(self) -> list[str]:
        """返回已落盘的所有 model_id。"""
        return sorted({p.parent.name for p in self.root.glob("*/*.parquet")})

    def get_at(self, symbol: str, ts: object, model_id: str) -> Optional[ForecastSignal]:
        """取某标的某 bar 的某模型信号（用于决策层逐 bar 查询）。"""
        df = self.get_frame(model_id=model_id, symbols=[symbol], start=ts, end=ts)
        if len(df) == 0:
            return None
        rec = df.iloc[0].to_dict()
        rec["ts"] = df.index.get_level_values("datetime")[0]
        return ForecastSignal.from_record(rec)

    def clear(self) -> None:
        for f in self.root.glob("*/*.parquet"):
            f.unlink()
