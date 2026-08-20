"""P6 任务一：Sentinel-2 v1.0 生产配置固化验证（P9 更新：combo → A10/B90）。

验证内容
--------
1. ``load_config()`` 默认配置可加载，``backtest.engine_b`` / ``backtest.combo`` 存在。
2. 嵌套字段默认值与终裁一致：
   - engine_b: enabled=True, win=252, thr=0.70, notional_frac=0.20
   - combo: w_engine_a=0.10, w_engine_b=0.90（P9 组合网格裁决，原 A15/B85）,
     vol_target=False, vol_target_ann=0.175, vol_ewma_halflife=10
3. 最小 yaml（含旧字段）加载后旧字段不变、新字段取默认值（向后兼容）。
4. yaml 可覆盖嵌套新字段（如 w_engine_a=0.0 纯 B 运行）。

运行
----
    python scripts/p6_config_validate.py
退出码：全部 PASS 返回 0，否则返回 1。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hexbroker.config import load_config  # noqa: E402

# 终裁基准（口径铁律：引擎 B 为基；P9 组合网格权重 A10/B90）
EXPECT_ENGINE_B = {
    "enabled": True,
    "win": 252,
    "thr": 0.70,
    "notional_frac": 0.20,
}
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


def main() -> int:
    print("=" * 72)
    print("P6 任务一：Sentinel-2 v1.0 生产配置固化验证")
    print("=" * 72)

    # ---- 1. 默认配置加载 + 新字段存在 ----
    print("\n[1] 默认配置 load_config()")
    cfg = load_config()
    check("默认配置可加载", isinstance(cfg, object) and cfg is not None)
    check("backtest.engine_b 存在", hasattr(cfg.backtest, "engine_b"))
    check("backtest.combo 存在", hasattr(cfg.backtest, "combo"))
    eb = cfg.backtest.engine_b
    cb = cfg.backtest.combo
    check("engine_b 类型", type(eb).__name__ == "EngineBConfig")
    check("combo 类型", type(cb).__name__ == "ComboConfig")

    # ---- 2. 嵌套字段默认值 ----
    print("\n[2] 嵌套字段默认值（P5 终裁）")
    for k, v in EXPECT_ENGINE_B.items():
        check(f"engine_b.{k} == {v!r}", getattr(eb, k) == v, f"实际 {getattr(eb, k)!r}")
    for k, v in EXPECT_COMBO.items():
        check(f"combo.{k} == {v!r}", getattr(cb, k) == v, f"实际 {getattr(cb, k)!r}")

    # ---- 3. 最小 yaml（含旧字段）向后兼容 ----
    print("\n[3] 最小 yaml（含旧字段）向后兼容")
    tmp = Path("artifacts") / "p6_min_test.yaml"
    tmp.parent.mkdir(exist_ok=True)
    tmp.write_text(
        "experiment: p6_min_legacy\n"
        "data:\n"
        "  symbols: [\"SHFE.au\", \"SHFE.ag\", \"DCE.m\"]\n"
        "  freq: \"1d\"\n"
        "  start: \"2018-01-01\"\n"
        "  end: \"2026-08-17\"\n"
        "forecast:\n"
        "  horizon: 5\n"
        "  calibration_method: \"platt\"\n"
        "backtest:\n"
        "  fee_rate_open: 0.00005\n"
        "  slippage_ticks: 1.0\n"
        "  margin_rate: 0.12\n"
        "  initial_capital: 1000000.0\n",
        encoding="utf-8",
    )
    cfg2 = load_config(str(tmp))
    check("yaml 加载成功", cfg2 is not None)
    check("旧字段 experiment 保留", cfg2.experiment == "p6_min_legacy")
    check("旧字段 data.symbols 保留", cfg2.data.symbols == ["SHFE.au", "SHFE.ag", "DCE.m"])
    check("旧字段 backtest.fee_rate_open 保留", cfg2.backtest.fee_rate_open == 0.00005)
    check("旧字段 backtest.slippage_ticks 保留", cfg2.backtest.slippage_ticks == 1.0)
    check("旧字段 backtest.margin_rate 保留", cfg2.backtest.margin_rate == 0.12)
    check("新字段 engine_b 取默认", cfg2.backtest.engine_b.win == 252 and cfg2.backtest.engine_b.thr == 0.70)
    check("新字段 combo 取默认", cfg2.backtest.combo.w_engine_a == 0.10 and cfg2.backtest.combo.w_engine_b == 0.90)

    # ---- 4. yaml 覆盖嵌套新字段（纯 B 运行场景） ----
    print("\n[4] yaml 覆盖嵌套新字段（纯 B 运行）")
    tmp2 = Path("artifacts") / "p6_pureB_test.yaml"
    tmp2.write_text(
        "backtest:\n"
        "  combo:\n"
        "    w_engine_a: 0.0\n"
        "    w_engine_b: 1.0\n"
        "  engine_b:\n"
        "    win: 126\n"
        "    thr: 0.75\n",
        encoding="utf-8",
    )
    cfg3 = load_config(str(tmp2))
    check("combo.w_engine_a 覆盖为 0.0", cfg3.backtest.combo.w_engine_a == 0.0)
    check("combo.w_engine_b 覆盖为 1.0", cfg3.backtest.combo.w_engine_b == 1.0)
    check("engine_b.win 覆盖为 126", cfg3.backtest.engine_b.win == 126)
    check("engine_b.thr 覆盖为 0.75", cfg3.backtest.engine_b.thr == 0.75)
    check("未覆盖字段 engine_b.notional_frac 保持默认", cfg3.backtest.engine_b.notional_frac == 0.20)

    # ---- 汇总 ----
    print("\n" + "=" * 72)
    if _FAILURES:
        print(f"[FAIL] {len(_FAILURES)} 项失败：{_FAILURES}")
        return 1
    print("[PASS] 全部检查通过：Sentinel-2 v1.0 生产配置固化完成且向后兼容")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
