"""预测信号落盘（OOS 隔离层，§3.2 / §8.4）。

``SignalStore`` 是预测层与决策层之间唯一的桥梁：**只落盘样本外（OOS）信号**，
并按 ``(model_id, train_end)`` 分片。RL 环境只能读取 SignalStore，
永远拿不到 ForecastModel 实例——从物理上杜绝训练信息泄漏到决策层。

信号文件布局::

    {root}/{model_id}/{train_end}.parquet
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

from ..utils.io import read_json, read_parquet, write_json, write_parquet
from .base import ForecastSignal


def _ts_file(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y%m%dT%H%M%S")


class SignalStore:
    """OOS 预测信号的列式落盘与检索。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    # ----------------------------- 写入 -----------------------------
    def put(self, signals: Iterable[ForecastSignal],
            fingerprint: "Any | None" = None) -> int:
        """写入信号，按 ``(model_id, train_end)`` 分片。返回实际落盘（OOS）条数。

        **OOS 隔离红线（L9 修复）**：``SignalStore`` 是 OOS 信号的唯一落盘层，
        必须拒绝样本内（in-sample）信号。任何 ``ts <= train_end`` 的信号都被
        视为泄漏，丢弃并告警，绝不落盘——即使调用方误传，物理上也无法让样本内
        信号进入决策层。

        **P0-3（向后兼容）**：``fingerprint`` 可选（``FourLayerFingerprint`` 或
        dict，默认 None）。非 None 时写分片 sidecar
        ``{root}/{model_id}/{train_end}.manifest.json``，**Parquet schema 不变**；
        同分片指纹不一致 → ``warnings.warn``（可配置升级为 raise）。
        """
        sigs = list(signals)
        if not sigs:
            return 0
        df = pd.DataFrame([s.to_record() for s in sigs])
        df["ts"] = pd.to_datetime(df["ts"])
        df["train_end"] = pd.to_datetime(df["train_end"])
        oos_mask = df["ts"] > df["train_end"]
        n_drop = int((~oos_mask).sum())
        if n_drop:
            warnings.warn(
                f"SignalStore.put 丢弃 {n_drop} 条样本内(in-sample)信号"
                f"(ts<=train_end)，仅落盘 OOS 信号——样本内信号严禁进入决策层",
                stacklevel=2,
            )
            df = df[oos_mask]
        if df.empty:
            return 0
        for (mid, te), grp in df.groupby(["model_id", "train_end"]):
            path = self.root / mid / f"{_ts_file(te)}.parquet"
            write_parquet(grp.reset_index(drop=True), path)
            if fingerprint is not None:
                self._write_fingerprint(mid, te, fingerprint)
        return len(df)

    def _write_fingerprint(self, model_id: str, train_end, fingerprint: Any) -> None:
        """写/校验分片 sidecar manifest（P0-3，不改 Parquet schema）。"""
        from ..utils.fingerprint import FourLayerFingerprint

        if isinstance(fingerprint, FourLayerFingerprint):
            fp_dict = fingerprint.to_dict()
        elif isinstance(fingerprint, dict):
            fp_dict = dict(fingerprint)
        else:
            raise TypeError(
                f"fingerprint 必须是 FourLayerFingerprint 或 dict，got {type(fingerprint).__name__}"
            )
        manifest = {
            "model_id": model_id,
            "train_end": _ts_file(train_end),
            "fingerprint": fp_dict,
        }
        path = self.root / model_id / f"{_ts_file(train_end)}.manifest.json"
        if path.exists():
            try:
                old = read_json(path)
                if old.get("fingerprint") != fp_dict:
                    warnings.warn(
                        f"SignalStore 分片 {model_id}/{_ts_file(train_end)} 指纹不一致："
                        f"旧 {old.get('fingerprint')} vs 新 {fp_dict}——缓存混用风险，请核查",
                        stacklevel=3,
                    )
            except Exception:
                pass
        write_json(manifest, path)

    def verify(self, model_id: Optional[str] = None) -> dict:
        """读取侧校验：返回 ``{checked, mismatched: [...], missing: [...]}``（PRD A3.2）。

        对每个分片 parquet 检查 sidecar manifest 是否存在且与分片身份一致。
        默认不侵入现有 ``get_frame`` 消费方。
        """
        files = self._candidate_files(model_id)
        checked = 0
        mismatched: list[str] = []
        missing: list[str] = []
        for f in files:
            checked += 1
            mid = f.parent.name
            stem = f.stem
            mpath = f.with_suffix(".manifest.json")
            rel = str(f.relative_to(self.root))
            if not mpath.exists():
                missing.append(rel)
                continue
            try:
                m = json.loads(mpath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                mismatched.append(rel)
                continue
            if m.get("model_id") != mid or m.get("train_end") != stem:
                mismatched.append(rel)
        return {"checked": checked, "mismatched": mismatched, "missing": missing}

    def fingerprints(self, model_id: Optional[str] = None) -> list[dict]:
        """列出所有分片四层指纹（含 model_id/train_end）；无 sidecar 的分片指纹为空。"""
        files = self._candidate_files(model_id)
        out: list[dict] = []
        for f in files:
            mid = f.parent.name
            stem = f.stem
            mpath = f.with_suffix(".manifest.json")
            rec: dict = {"model_id": mid, "train_end": stem}
            if mpath.exists():
                try:
                    m = json.loads(mpath.read_text(encoding="utf-8"))
                    rec.update(m.get("fingerprint") or {})
                except (json.JSONDecodeError, OSError):
                    rec["fingerprint_error"] = True
            out.append(rec)
        return out

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
