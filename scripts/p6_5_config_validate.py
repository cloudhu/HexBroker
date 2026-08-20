"""P6-5：生产配置固化验证（引擎 A → v8 缓存 + A10/B90 + 引擎 B win252/thr0.7）。

说明（P8-4 / P9 更新）
---------------------
P8-4 将生产缓存从 v4 切换为 v8（全品种统一截面，QA VERIFIED），本脚本随之更新；
P9-1 组合网格（复利口径）裁决权重 A15/B85 → A10/B90；P9-2 新增
``EngineAConfig.group_cap``（默认 None，向后兼容）。

验证内容
--------
1. 默认 ``load_config()`` → ``cfg.backtest.engine_a.signal_cache ==
   "artifacts/signals_cache18_grouped_v8.parquet"``（v8 生产缓存）；
   engine_a 其余字段（top_k=0.30 / min_symbols=3 / notional_frac=0.20 /
   group_cap=None）与 P5/P8-4/P9 裁决一致；engine_b / combo 保持固化值。
2. 旧 yaml（无 ``engine_a`` / ``combo`` 字段）向后兼容：可加载、旧字段保留、
   新字段取默认。
3. yaml 覆盖 ``engine_a.min_symbols/top_k/notional_frac/signal_cache/group_cap``
   生效；显式 v2 路径覆盖仍可用（向后兼容）。
4. ``engine_a_targets_cs()`` 无参调用默认读 v8（mock ``pandas.read_parquet``
   捕获读取路径，轻量验证，不依赖完整回测数据）。
5. pytest 回归（tests/test_config.py 等）确认未破坏既有配置行为。

运行
----
    python scripts/p6_5_config_validate.py
退出码：全部 PASS 返回 0，否则返回 1。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from hexbroker.config import load_config  # noqa: E402

# 生产基准（口径铁律：引擎 A → v8 缓存，S2 min=3；P9 权重 A10/B90）
V8_CACHE = "artifacts/signals_cache18_grouped_v8.parquet"
V2_CACHE = "artifacts/signals_cache18_grouped_v2.parquet"
EXPECT_ENGINE_A = {
    "enabled": True,
    "signal_cache": V8_CACHE,
    "top_k": 0.30,
    "min_symbols": 3,
    "notional_frac": 0.20,
    "group_cap": None,  # P9-2 新增：默认不启用（向后兼容）
}
# P5/P6-1 固化基准（回归确认未破坏）
EXPECT_ENGINE_B = {
    "enabled": True,
    "win": 252,
    "thr": 0.70,
    "notional_frac": 0.20,
}
# P9 终裁基准（P8-4 基线 A15/B85 → 组合网格最优 A10/B90）
EXPECT_COMBO = {
    "w_engine_a": 0.10,
    "w_engine_b": 0.90,
    "vol_target": False,
    "vol_target_ann": 0.175,
    "vol_ewma_halflife": 10,
}

_FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [PASS] {name}")
    else:
        _FAILURES.append(name)
        print(f"  [FAIL] {name} {detail}")


def _write_tmp(name: str, text: str) -> Path:
    tmp = Path("artifacts") / name
    tmp.parent.mkdir(exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    return tmp


# ---------------------------------------------------------------------------
# 轻量验证 #4 的合成数据（不依赖完整回测数据文件）
# ---------------------------------------------------------------------------
def _make_prices() -> pd.DataFrame:
    """合成 prices：MultiIndex(symbol, datetime) + close，覆盖假缓存两个品种。"""
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    idx = pd.MultiIndex.from_product(
        [["SHFE.au", "SHFE.ag"], dates], names=["symbol", "datetime"]
    )
    return pd.DataFrame(
        {"close": [4500.0, 4510.0, 4520.0, 6000.0, 6010.0, 6020.0]},
        index=idx,
    )


def _fake_cache_frame() -> pd.DataFrame:
    """合成信号缓存：仅含 engine_a_targets_cs 需要的 ts/symbol/exp_ret 三列。"""
    return pd.DataFrame(
        {
            "ts": pd.to_datetime(
                ["2024-01-02", "2024-01-02", "2024-01-03", "2024-01-03"]
            ),
            "symbol": ["SHFE.au", "SHFE.ag", "SHFE.au", "SHFE.ag"],
            "exp_ret": [0.02, 0.01, -0.01, 0.03],
        }
    )


def main() -> int:
    print("=" * 72)
    print("P6-5：生产配置固化验证（引擎 A → v8 缓存 + A10/B90 + B win252/thr0.7）")
    print("=" * 72)

    # ---- 1. 默认配置：engine_a 存在 + 字段默认值 ----
    print("\n[1] 默认配置 load_config()")
    cfg = load_config()
    check("默认配置可加载", cfg is not None)
    check("backtest.engine_a 存在", hasattr(cfg.backtest, "engine_a"))
    check("backtest.engine_b 存在", hasattr(cfg.backtest, "engine_b"))
    check("backtest.combo 存在", hasattr(cfg.backtest, "combo"))
    ea = cfg.backtest.engine_a
    eb = cfg.backtest.engine_b
    cb = cfg.backtest.combo
    check("engine_a 类型", type(ea).__name__ == "EngineAConfig")
    check("engine_b 类型", type(eb).__name__ == "EngineBConfig")
    check("combo 类型", type(cb).__name__ == "ComboConfig")

    for k, v in EXPECT_ENGINE_A.items():
        check(f"engine_a.{k} == {v!r}", getattr(ea, k) == v, f"实际 {getattr(ea, k)!r}")
    # P5/P6-1 固化字段回归（确认未破坏）
    for k, v in EXPECT_ENGINE_B.items():
        check(f"engine_b.{k} == {v!r}", getattr(eb, k) == v, f"实际 {getattr(eb, k)!r}")
    for k, v in EXPECT_COMBO.items():
        check(f"combo.{k} == {v!r}", getattr(cb, k) == v, f"实际 {getattr(cb, k)!r}")

    # ---- 2. 旧 yaml（无 engine_a 字段）向后兼容 ----
    print("\n[2] 旧 yaml（无 engine_a 字段）向后兼容")
    tmp = _write_tmp(
        "p6_5_min_legacy.yaml",
        "experiment: p6_5_min_legacy\n"
        "data:\n"
        '  symbols: ["SHFE.au", "SHFE.ag", "DCE.m"]\n'
        "backtest:\n"
        "  fee_rate_open: 0.00005\n"
        "  slippage_ticks: 1.0\n"
        "  margin_rate: 0.12\n"
        "  initial_capital: 1000000.0\n",
    )
    cfg2 = load_config(str(tmp))
    check("旧 yaml 加载成功", cfg2 is not None)
    check("旧字段 experiment 保留", cfg2.experiment == "p6_5_min_legacy")
    check("旧字段 data.symbols 保留", cfg2.data.symbols == ["SHFE.au", "SHFE.ag", "DCE.m"])
    check("旧字段 backtest.fee_rate_open 保留", cfg2.backtest.fee_rate_open == 0.00005)
    check("旧字段 backtest.initial_capital 保留", cfg2.backtest.initial_capital == 1_000_000.0)
    check(
        "无 engine_a 字段 → engine_a.signal_cache 取默认 v8",
        cfg2.backtest.engine_a.signal_cache == V8_CACHE,
        f"实际 {cfg2.backtest.engine_a.signal_cache!r}",
    )
    check(
        "无 engine_a 字段 → engine_a.min_symbols 取默认 3",
        cfg2.backtest.engine_a.min_symbols == 3,
    )
    check(
        "无 engine_a 字段 → engine_a.group_cap 取默认 None（P9-2 向后兼容）",
        cfg2.backtest.engine_a.group_cap is None,
        f"实际 {cfg2.backtest.engine_a.group_cap!r}",
    )
    check(
        "无 engine_a 字段 → engine_b 取默认 win252/thr0.7",
        cfg2.backtest.engine_b.win == 252 and cfg2.backtest.engine_b.thr == 0.70,
    )
    check(
        "无 combo 字段 → combo 取默认 A10/B90（P9 组合网格裁决）",
        cfg2.backtest.combo.w_engine_a == 0.10 and cfg2.backtest.combo.w_engine_b == 0.90,
    )

    # ---- 3. yaml 覆盖 engine_a 嵌套字段生效（含显式 v2 覆盖 + group_cap） ----
    print("\n[3] yaml 覆盖 engine_a 嵌套字段（含显式 v2 路径向后兼容 + group_cap）")
    tmp2 = _write_tmp(
        "p6_5_override_test.yaml",
        "backtest:\n"
        "  engine_a:\n"
        "    top_k: 0.25\n"
        "    min_symbols: 5\n"
        "    notional_frac: 0.15\n"
        "    signal_cache: artifacts/signals_cache18_grouped_v2.parquet\n"
        "    group_cap: 0.5\n",
    )
    cfg3 = load_config(str(tmp2))
    check("engine_a.top_k 覆盖为 0.25", cfg3.backtest.engine_a.top_k == 0.25)
    check("engine_a.min_symbols 覆盖为 5", cfg3.backtest.engine_a.min_symbols == 5)
    check("engine_a.notional_frac 覆盖为 0.15", cfg3.backtest.engine_a.notional_frac == 0.15)
    check(
        "engine_a.signal_cache 覆盖为显式 v2 路径（向后兼容）",
        cfg3.backtest.engine_a.signal_cache == V2_CACHE,
        f"实际 {cfg3.backtest.engine_a.signal_cache!r}",
    )
    check(
        "engine_a.group_cap 覆盖为 0.5（P9-2 生效）",
        cfg3.backtest.engine_a.group_cap == 0.5,
        f"实际 {cfg3.backtest.engine_a.group_cap!r}",
    )
    check(
        "未覆盖字段 engine_a.enabled 保持默认 True",
        cfg3.backtest.engine_a.enabled is True,
    )
    check(
        "覆盖后 engine_b 仍取默认 win252/thr0.7",
        cfg3.backtest.engine_b.win == 252 and cfg3.backtest.engine_b.thr == 0.70,
    )
    check(
        "覆盖后 combo 仍取默认 A10/B90",
        cfg3.backtest.combo.w_engine_a == 0.10 and cfg3.backtest.combo.w_engine_b == 0.90,
    )

    # ---- 4. engine_a_targets_cs() 无参调用默认读 v8（mock 捕获读取路径） ----
    print("\n[4] engine_a_targets_cs() 无参调用默认读 v8（mock 轻量验证）")
    from scripts.p5_engineA_cross_section import engine_a_targets_cs  # noqa: E402

    captured: dict[str, str] = {}

    def fake_read_parquet(path, **kwargs):
        captured["path"] = str(path)
        return _fake_cache_frame()

    with mock.patch("pandas.read_parquet", side_effect=fake_read_parquet):
        tgt = engine_a_targets_cs(_make_prices())
    check("engine_a_targets_cs() 无参调用成功", tgt is not None and len(tgt) > 0)
    read_name = Path(captured["path"]).name
    check(
        "无参调用读取 v8 缓存文件",
        read_name == "signals_cache18_grouped_v8.parquet",
        f"实际 {read_name!r}",
    )
    v8_resolved = Path(ROOT) / V8_CACHE
    check(
        "读取路径解析到生产缓存绝对路径",
        str(Path(captured["path"])) == str(v8_resolved),
        f"实际 {captured['path']!r}",
    )

    # ---- 5. pytest 回归（test_config.py 等） ----
    print("\n[5] pytest 回归（tests/test_config.py 等）")
    targets = ["tests/test_config.py", "tests/test_kronos_pairing.py"]
    for target in targets:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", target, "-q"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        tail = (r.stdout or "").strip().splitlines()
        last = tail[-1] if tail else ""
        check(f"pytest {target} 通过", r.returncode == 0, f"退出码 {r.returncode} | {last}")

    # ---- 汇总 ----
    print("\n" + "=" * 72)
    if _FAILURES:
        print(f"[FAIL] {len(_FAILURES)} 项失败：{_FAILURES}")
        return 1
    print("[PASS] 全部检查通过：生产配置已固化（引擎A→v8缓存 + A10/B90 + B win252/thr0.7）")
    print("      向后兼容：旧 yaml 无 engine_a/combo 字段取默认；显式 cache_path/v2 路径不受影响")
    print("      P9-2：EngineAConfig.group_cap 默认 None（不启用），yaml 覆盖 0.5 生效")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
