"""真 Kronos 冒烟测试（deliverable A 步骤 4）。

用法
----
    python scripts/smoke_kronos.py

流程：
  1. 受控导入 third_party/Kronos 的 model 包 + third_party/pylibs 的 huggingface_hub。
  2. KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")（本地缓存，HF_HUB_OFFLINE=1）。
  3. Kronos.from_pretrained("NeoQuasar/Kronos-small")（本地缓存）。
  4. KronosPredictor(model, tokenizer, device="cuda:0", max_context=512)。
  5. 用 sina au0 最近 60 根日线 predict(pred_len=5, T=1.0, top_p=0.9, sample_count=2)，
     打印输出形状与样例。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_THIRD = _ROOT / "third_party"
# 受控导入：先插入 third_party/Kronos（model 包）与 third_party/pylibs（huggingface_hub 等）
for p in [str(_THIRD / "Kronos"), str(_THIRD / "pylibs"), str(_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("HF_HUB_OFFLINE", "1")


def main() -> int:
    import torch

    print(f"[SMOKE] torch={torch.__version__} cuda_available={torch.cuda.is_available()} "
          f"device_count={torch.cuda.device_count()}")

    from model import Kronos, KronosTokenizer, KronosPredictor

    # ---- 1. 加载 tokenizer（本地缓存，必须已补齐 model.safetensors） ----
    tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
    print(f"[SMOKE] tokenizer loaded: type={type(tokenizer).__name__} "
          f"d_model={tokenizer.d_model} n_heads={tokenizer.n_heads}")

    # ---- 2. 加载主模型（本地缓存） ----
    model = Kronos.from_pretrained("NeoQuasar/Kronos-small")
    print(f"[SMOKE] model loaded: type={type(model).__name__} "
          f"d_model={model.d_model} n_layers={model.n_layers} n_heads={model.n_heads}")

    # ---- 3. 构造 predictor ----
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    predictor = KronosPredictor(model, tokenizer, device=device, max_context=512)
    print(f"[SMOKE] predictor ready on device={predictor.device}")

    # ---- 4. 取 sina au0 最近 60 根日线 ----
    import requests

    url = ("http://stock2.finance.sina.com.cn/futures/api/json.php/"
           "IndexService.getInnerFuturesDailyKLine?symbol=au0")
    resp = requests.get(url, timeout=10.0)
    resp.raise_for_status()
    data = list(reversed(resp.json()))
    rows = []
    for r in data:
        if isinstance(r, dict):
            rows.append(r)
        else:
            rows.append(dict(zip(["date", "open", "high", "low", "close", "volume"], r)))
    import pandas as pd

    df = pd.DataFrame.from_records(rows)
    if "vol" in df.columns and "volume" not in df.columns:
        df = df.rename(columns={"vol": "volume"})
    df["datetime"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df = df.sort_values("datetime").reset_index(drop=True)
    df["amount"] = 0.0
    df = df[["datetime", "open", "high", "low", "close", "volume", "amount"]]
    ctx = df.tail(60).reset_index(drop=True)
    print(f"[SMOKE] au0 context bars={len(ctx)} range={ctx['datetime'].iloc[0]}~{ctx['datetime'].iloc[-1]}")

    # ---- 5. 预测 ----
    pred_len = 5
    # calc_time_stamps 使用 .dt 访问器，须传 pd.Series（DatetimeIndex 无 .dt）
    x_ts = pd.Series(pd.DatetimeIndex(ctx["datetime"]), name="datetime")
    # 未来 5 个交易日（仅作时间戳载体，不读未来价格）
    last_dt = ctx["datetime"].iloc[-1]
    y_ts = pd.Series(
        pd.bdate_range(start=last_dt + pd.Timedelta(days=1), periods=pred_len),
        name="datetime",
    )
    pred = predictor.predict(
        df=ctx[["open", "high", "low", "close", "volume", "amount"]],
        x_timestamp=x_ts,
        y_timestamp=y_ts,
        pred_len=pred_len,
        T=1.0,
        top_p=0.9,
        sample_count=2,
        verbose=False,
    )
    print(f"[SMOKE] predict output shape={pred.shape} index={list(pred.index)}")
    print(pred.round(4).to_string())
    print("[SMOKE] OK 真 Kronos 冒烟测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
