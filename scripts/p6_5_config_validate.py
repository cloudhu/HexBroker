"""P6-5：生产配置固化验证（引擎 A → v8 缓存 + A30/B70 + 引擎 B win252/thr0.7 + nf_b 0.30）。

说明（P8-4 / P9 / P10-1 / P19 更新）
-------------------------------------
P8-4 将生产缓存从 v4 切换为 v8（全品种统一截面，QA VERIFIED），本脚本随之更新；
P9-1 组合网格（复利口径）裁决权重 A15/B85 → A10/B90；P9-2 新增
``EngineAConfig.group_cap``（默认 None，向后兼容）；P10-1 新增
``EngineAConfig.group_map``（默认 None，向后兼容），并在部署 yaml
``configs/base.yaml`` 显式启用 ``group_cap=0.5`` + ``group_map``
（黑色系 5 品种合并为 ferrous_all 组）——QA 阻塞项落地。
P19（broker multiplier bug 修复后，生产口径 3b 复验，QA Round2 VERIFIED）：
终裁采纳保守方案——ComboConfig A10/B90 → **A30/B70**、EngineBConfig.notional_frac
0.20 → **0.30**（win/thr 保持 252/0.70），并在 ``configs/base.yaml`` 显式声明
engine_b / combo 段（部署以 base.yaml 为准）。

验证内容
--------
1. 默认 ``load_config()`` → ``cfg.backtest.engine_a.signal_cache ==
   "artifacts/signals_cache18_grouped_v8.parquet"``（v8 生产缓存）；
   engine_a 其余字段（top_k=0.30 / min_symbols=3 / notional_frac=0.20 /
   group_cap=None / group_map=None）与 P5/P8-4/P9 裁决一致（代码默认向后兼容）；
   engine_b / combo 保持 P19 固化值（nf_b=0.30、A30/B70）。
2. 部署 yaml ``configs/base.yaml`` → engine_a.group_cap=0.5 + group_map
   （ferrous_all 合并映射）生效（P10-1 QA 阻塞项）；engine_b/combo 显式声明
   （nf_b=0.30、A30/B70，P19 固化）。
3. 旧 yaml（无 ``engine_a`` / ``combo`` 字段）向后兼容：可加载、旧字段保留、
   新字段取默认。
4. yaml 覆盖 ``engine_a.min_symbols/top_k/notional_frac/signal_cache/group_cap/
   group_map`` 生效；显式 v2 路径覆盖仍可用（向后兼容）。
5. ``engine_a_targets_cs()`` 无参调用默认读 v8（mock ``pandas.read_parquet``
   捕获读取路径，轻量验证，不依赖完整回测数据）；group_cap/group_map 未显式
   传入时读生产配置（mock ``load_config`` 验证解析优先级）。
6. pytest 回归（tests/test_config.py 等）确认未破坏既有配置行为。

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

# 生产基准（口径铁律：引擎 A → v8 缓存，S2 min=3；P19 权重 A30/B70 + nf_b 0.30）
V8_CACHE = "artifacts/signals_cache18_grouped_v8.parquet"
V2_CACHE = "artifacts/signals_cache18_grouped_v2.parquet"
BASE_YAML = "configs/base.yaml"
EXPECT_ENGINE_A = {
    "enabled": True,
    "signal_cache": V8_CACHE,
    "top_k": 0.30,
    "min_symbols": 3,
    "notional_frac": 0.20,
    "group_cap": None,  # P9-2 新增：代码默认不启用（向后兼容；部署 yaml 显式启用）
    "group_map": None,  # P10-1 新增：代码默认 None（GROUPS_V2 兜底；部署 yaml 显式启用）
}
# 部署 yaml（configs/base.yaml）显式启用值（P10-1 QA 阻塞项）
EXPECT_BASE_YAML_GROUP_CAP = 0.5
EXPECT_BASE_YAML_GROUP_MAP_FERROUS = {"i0", "j0", "jm0", "rb0", "hc0"}
# P5/P6-1 固化基准 + P19 名义上调（win/thr 不动，nf_b 0.20→0.30）
EXPECT_ENGINE_B = {
    "enabled": True,
    "win": 252,
    "thr": 0.70,
    "notional_frac": 0.30,  # P19 终裁：0.20→0.30（B 有效名义 ≥170k 存活）
}
# P19 终裁基准（A10/B90 → A30/B70，生产口径保守方案）
EXPECT_COMBO = {
    "w_engine_a": 0.30,
    "w_engine_b": 0.70,
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
    print("P6-5：生产配置固化验证（引擎 A → v8 缓存 + A30/B70 + B win252/thr0.7 + nf_b 0.30 + P10-1 group_map）")
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

    # ---- 1b. 部署 yaml（configs/base.yaml）显式启用 group_cap + group_map（P10-1） ----
    print("\n[1b] 部署 yaml configs/base.yaml → group_cap + group_map 显式启用（P10-1）")
    cfg_base = load_config(str(BASE_YAML))
    ea_base = cfg_base.backtest.engine_a
    check(
        "base.yaml engine_a.group_cap == 0.5",
        ea_base.group_cap == EXPECT_BASE_YAML_GROUP_CAP,
        f"实际 {ea_base.group_cap!r}",
    )
    gm_base = ea_base.group_map or {}
    check(
        "base.yaml engine_a.group_map 非空",
        isinstance(gm_base, dict) and len(gm_base) >= 18,
        f"实际 {len(gm_base)} 键",
    )
    check(
        "base.yaml group_map 黑色系 5 品种合并为 ferrous_all",
        {gm_base.get(s) for s in EXPECT_BASE_YAML_GROUP_MAP_FERROUS} == {"ferrous_all"},
        f"实际 { {s: gm_base.get(s) for s in sorted(EXPECT_BASE_YAML_GROUP_MAP_FERROUS)} }",
    )
    check(
        "base.yaml group_map 其余品种沿用 GROUPS_V2 组名",
        gm_base.get("cu0") == "industrial" and gm_base.get("au0") == "precious"
        and gm_base.get("m0") == "agri_protein" and gm_base.get("sc0") == "chem_energy",
        f"cu0={gm_base.get('cu0')!r} au0={gm_base.get('au0')!r}",
    )
    check(
        "base.yaml engine_b/combo 显式声明生效（nf_b 0.30、A30/B70，P19 固化）",
        cfg_base.backtest.engine_b.win == 252 and cfg_base.backtest.engine_b.thr == 0.70
        and cfg_base.backtest.engine_b.notional_frac == 0.30
        and cfg_base.backtest.combo.w_engine_a == 0.30
        and cfg_base.backtest.combo.w_engine_b == 0.70,
    )

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
        "无 engine_a 字段 → engine_a.group_map 取默认 None（P10-1 向后兼容）",
        cfg2.backtest.engine_a.group_map is None,
        f"实际 {cfg2.backtest.engine_a.group_map!r}",
    )
    check(
        "无 engine_a 字段 → engine_b 取默认 win252/thr0.7",
        cfg2.backtest.engine_b.win == 252 and cfg2.backtest.engine_b.thr == 0.70,
    )
    check(
        "无 combo 字段 → combo 取默认 A30/B70（P19 组合网格裁决）",
        cfg2.backtest.combo.w_engine_a == 0.30 and cfg2.backtest.combo.w_engine_b == 0.70,
    )

    # ---- 3. yaml 覆盖 engine_a 嵌套字段生效（含显式 v2 覆盖 + group_cap + group_map） ----
    print("\n[3] yaml 覆盖 engine_a 嵌套字段（含显式 v2 路径向后兼容 + group_cap/group_map）")
    tmp2 = _write_tmp(
        "p6_5_override_test.yaml",
        "backtest:\n"
        "  engine_a:\n"
        "    top_k: 0.25\n"
        "    min_symbols: 5\n"
        "    notional_frac: 0.15\n"
        "    signal_cache: artifacts/signals_cache18_grouped_v2.parquet\n"
        "    group_cap: 0.5\n"
        "    group_map:\n"
        "      i0: ferrous_all\n"
        "      rb0: ferrous_all\n",
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
        "engine_a.group_map 覆盖生效（P10-1）",
        cfg3.backtest.engine_a.group_map == {"i0": "ferrous_all", "rb0": "ferrous_all"},
        f"实际 {cfg3.backtest.engine_a.group_map!r}",
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
        "覆盖后 combo 仍取默认 A30/B70",
        cfg3.backtest.combo.w_engine_a == 0.30 and cfg3.backtest.combo.w_engine_b == 0.70,
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

    # ---- 4b. group_cap / group_map 未显式传入时读生产配置（P10-1 解析优先级） ----
    print("\n[4b] group_cap / group_map 配置读取逻辑（P10-1，mock load_config 轻量验证）")
    from scripts.p5_engineA_cross_section import (  # noqa: E402
        _default_group_map,
        _resolve_group_cap,
        _resolve_group_map,
    )

    # 显式传入优先
    check(
        "group_map 显式传入优先",
        _resolve_group_map({"i0": "ferrous_all"}) == {"i0": "ferrous_all"},
    )
    check("group_cap 显式传入优先", _resolve_group_cap(0.5) == 0.5)
    # 配置启用时读取
    with mock.patch("scripts.p5_engineA_cross_section.load_config") as m_load:
        m_load.return_value.backtest.engine_a.group_map = {"i0": "ferrous_all"}
        m_load.return_value.backtest.engine_a.group_cap = 0.5
        check(
            "group_map None → 读配置（部署 yaml 生效）",
            _resolve_group_map(None) == {"i0": "ferrous_all"},
        )
        check("group_cap None → 读配置（部署 yaml 生效）", _resolve_group_cap(None) == 0.5)
    # 配置 None → 回退默认
    with mock.patch("scripts.p5_engineA_cross_section.load_config") as m_load:
        m_load.return_value.backtest.engine_a.group_map = None
        m_load.return_value.backtest.engine_a.group_cap = None
        check(
            "group_map 配置 None → 回退 GROUPS_V2（向后兼容）",
            _resolve_group_map(None) == _default_group_map(),
        )
        check("group_cap 配置 None → 不启用", _resolve_group_cap(None) is None)

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
    print("[PASS] 全部检查通过：生产配置已固化（引擎A→v8缓存 + A30/B70 + B win252/thr0.7 + nf_b 0.30）")
    print("      向后兼容：旧 yaml 无 engine_a/combo 字段取默认；显式 cache_path/v2 路径不受影响")
    print("      P9-2：EngineAConfig.group_cap 默认 None（不启用），yaml 覆盖 0.5 生效")
    print("      P10-1：EngineAConfig.group_map 默认 None（GROUPS_V2 兜底），部署 yaml")
    print("             configs/base.yaml 显式启用 group_cap=0.5 + ferrous_all 合并映射（QA 阻塞项落地）")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
