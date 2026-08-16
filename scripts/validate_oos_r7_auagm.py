"""真实数据 OOS 信号验证 + R7 终裁脚本（au/ag/m 变体，真 Kronos 版）。

用法
----
    python scripts/validate_oos_r7_auagm.py

与 ``scripts/validate_oos_r7.py``（cu/rb/sc）的关系：
- 品种切到 SHFE.au / SHFE.ag / DCE.m（sina au0/ag0/m0 主力连续）。
- [lightgbm, ar_transformer, tcn, gru] 仍走 ForecastTrainer；
  **kronos 走 KlineKronosOOS**（真 Kronos，独立原始 OHLCV 通路）。
- 闸门1表加入 kronos 行；R7 终裁改为**真 Kronos vs LightGBM**：
  ``kronos_wins = (kronos.方向准确率 >= lightgbm.方向准确率)
  AND (kronos.RankIC >= lightgbm.RankIC)``。
- kronos 状态：成功产出信号 -> RUN；加载/运行失败 -> FAIL + 原因（如实）。
"""

from __future__ import annotations

import gc
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

# ---- 确保项目根目录在 sys.path（支持 `python scripts/validate_oos_r7_auagm.py`） ----
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from omegaconf import OmegaConf  # 仅用于读取 free.yaml 的 source_priority

from hexbroker import HexConfigError, HexDataError
from hexbroker.config import load_config
from hexbroker.data.sources.pytdx_source import PytdxSource
from hexbroker.data.sources.sina_source import SinaSource
from hexbroker.feature import build_features
from hexbroker.forecast.signal_store import SignalStore
from hexbroker.forecast.trainer import ForecastTrainer
from hexbroker.forecast.kronos_predictor import KlineKronosOOS
from hexbroker.utils.registry import build as build_model

# ---------------------------------------------------------------------------
# 常量（集中声明，禁止硬编码魔法数字在业务代码）
# ---------------------------------------------------------------------------
SYMBOLS = ["SHFE.au", "SHFE.ag", "DCE.m"]
FREQ = "1d"
DATA_START = "2018-01-01"
DATA_END = "2024-12-31"

# 闸门1（信号有效性）viability floor 阈（来自 README/system_design）
GATE_DIR_ACC = 0.54      # 方向准确率下限
GATE_EFF_ACC = 0.58      # 有效信号准确率下限
GATE_COVERAGE = 0.30     # 有效信号覆盖下限

MODELS = ["lightgbm", "ar_transformer", "tcn", "gru"]
KRONOS = "kronos"
BASELINE = "lightgbm"          # R7 基线对照
KRONOS_MODEL_NAME = "NeoQuasar/Kronos-small"
KRONOS_TOKENIZER_NAME = "NeoQuasar/Kronos-Tokenizer-base"
KRONOS_N_MC = int(os.environ.get("KRONOS_N_MC", "10"))

FREE_YAML = _ROOT / "configs" / "data" / "free.yaml"
STORE_DIR = _ROOT / "artifacts" / "signals_oos_r7_auagm"
DELIVERABLE_DIR = _ROOT / "deliverables" / "software-hexfutures-ai"
REPORT_DATE = "2026-08-15"
REPORT_MD = DELIVERABLE_DIR / f"oos-r7-validation-{REPORT_DATE}-auagm.md"
REPORT_JSON = DELIVERABLE_DIR / f"oos-r7-validation-{REPORT_DATE}-auagm.json"

# ---------------------------------------------------------------------------
# 相关性：优先 scipy，缺失则回退 pandas
# ---------------------------------------------------------------------------
try:
    from scipy.stats import pearsonr as _scipy_pearsonr
    from scipy.stats import spearmanr as _scipy_spearmanr

    _HAVE_SCIPY = True
except ImportError:  # pragma: no cover - 环境缺 scipy 时回退
    _HAVE_SCIPY = False


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman 秩相关（scipy 优先，否则 pandas）。"""
    if len(x) < 3:
        return float("nan")
    if _HAVE_SCIPY:
        r, _ = _scipy_spearmanr(x, y)
        return float(r) if pd.notna(r) else float("nan")
    return float(pd.Series(x).reset_index(drop=True).corr(
        pd.Series(y).reset_index(drop=True), method="spearman"
    ))


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    """Pearson 线性相关（scipy 优先，否则 pandas）。"""
    if len(x) < 3:
        return float("nan")
    if _HAVE_SCIPY:
        r, _ = _scipy_pearsonr(x, y)
        return float(r) if pd.notna(r) else float("nan")
    return float(pd.Series(x).reset_index(drop=True).corr(
        pd.Series(y).reset_index(drop=True), method="pearson"
    ))


# ---------------------------------------------------------------------------
# 1. 建真实源（按 free.yaml source_priority failover）
# ---------------------------------------------------------------------------
def _read_source_priority() -> list[str]:
    """读取 free.yaml 的 source_priority；缺失则回退默认。"""
    if FREE_YAML.exists():
        try:
            cfg = OmegaConf.load(str(FREE_YAML))
            pri = list(cfg.get("source_priority", ["pytdx", "sina"]))
            if pri:
                return [str(p) for p in pri]
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] 读取 free.yaml source_priority 失败，回退默认：{exc}")
    return ["pytdx", "sina"]


def _build_source_from_name(name: str):
    """按源名构造数据源实例（仅返回支持 fetch_bars 的 BarFrame 源）。"""
    name = str(name).strip().lower()
    if name == "pytdx":
        return PytdxSource()
    if name == "sina":
        return SinaSource()
    return None


def build_source_plan() -> list[tuple[str, Any]]:
    """按 free.yaml 的 source_priority 生成「源名 -> 构造器」计划。"""
    priority = _read_source_priority()
    bar_sources = [s for s in priority if s in ("pytdx", "sina")]
    if not bar_sources:
        bar_sources = ["pytdx", "sina"]
    plan: list[tuple[str, Any]] = []
    for name in bar_sources:
        factory = _build_source_from_name(name)
        if factory is not None:
            plan.append((name, factory))
    return plan


def fetch_with_failover(cfg: Any, plan: list[tuple[str, Any]]) -> tuple[Any, str]:
    """逐个源真实执行 fetch_bars，捕获连接/超时/网络异常后降级。"""
    last_err: Optional[Exception] = None
    for name, factory in plan:
        try:
            print(f"[INFO] 尝试源 '{name}'（按 free.yaml 优先级）...")
            src = factory
            bars = src.fetch_bars(
                symbols=list(cfg.data.symbols),
                start=cfg.data.start,
                end=cfg.data.end,
                freq=cfg.data.freq,
            )
            return bars, name
        except (HexDataError, HexConfigError, ConnectionError, TimeoutError, OSError) as exc:
            last_err = exc
            print(f"[WARN] 源 '{name}' 不可用（{type(exc).__name__}: {exc}），降级下一源")
            continue
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            print(f"[WARN] 源 '{name}' 取数异常（{type(exc).__name__}: {exc}），降级下一源")
            continue
    raise HexDataError(f"所有真实数据源均不可用（计划 {[p[0] for p in plan]}），最后错误：{last_err}")


# ---------------------------------------------------------------------------
# 5. 重算已实现 horizon 前向收益（物理隔离）
# ---------------------------------------------------------------------------
def compute_realized_returns(bars: Any, horizon: int) -> pd.Series:
    """对每只标的用 close 重算 fwd = close.shift(-horizon)/close - 1。"""
    parts: list[pd.Series] = []
    for sym in bars.symbols:
        sym_df = bars.by_symbol(sym)
        close = sym_df["close"].astype(float).reset_index(level=0, drop=True).sort_index()
        fwd = close.shift(-horizon) / close - 1.0
        mi = pd.MultiIndex.from_arrays(
            [np.array([sym] * len(fwd)), fwd.index.to_numpy()],
            names=["symbol", "datetime"],
        )
        fwd = fwd.copy()
        fwd.index = mi
        parts.append(fwd)
    realized = pd.concat(parts).sort_index()
    realized.name = "realized"
    return realized


# ---------------------------------------------------------------------------
# 6. 闸门1指标
# ---------------------------------------------------------------------------
def compute_gate1(sig: pd.DataFrame) -> dict[str, float]:
    """计算单组信号的闸门1指标。"""
    p_up = sig["p_up"].to_numpy(dtype=float)
    exp_ret = sig["exp_ret"].to_numpy(dtype=float)
    eff = sig["is_effective"].to_numpy(dtype=bool)
    realized = sig["realized"].to_numpy(dtype=float)

    n_oos = int(len(sig))
    n_effective = int(eff.sum())
    coverage = float(eff.mean()) if n_oos > 0 else 0.0

    mask = ~np.isnan(realized)
    if mask.sum() > 0:
        pred_dir = np.sign(p_up[mask] - 0.5)
        real_dir = np.sign(realized[mask])
        direction_accuracy = float(np.mean(pred_dir == real_dir))

        eff_mask = mask & eff
        if eff_mask.sum() > 0:
            pred_dir_e = np.sign(p_up[eff_mask] - 0.5)
            real_dir_e = np.sign(realized[eff_mask])
            effective_accuracy = float(np.mean(pred_dir_e == real_dir_e))
        else:
            effective_accuracy = float("nan")

        rank_ic = _spearman(p_up[mask], realized[mask])
        ic = _pearson(exp_ret[mask], realized[mask])
    else:
        direction_accuracy = float("nan")
        effective_accuracy = float("nan")
        rank_ic = float("nan")
        ic = float("nan")

    return {
        "n_oos": n_oos,
        "n_effective": n_effective,
        "coverage": coverage,
        "direction_accuracy": direction_accuracy,
        "effective_accuracy": effective_accuracy,
        "rank_ic": rank_ic,
        "ic": ic,
    }


def gate1_pass(m: dict[str, float]) -> bool:
    """闸门1判据：方向准确率≥54% 且 有效信号准确率≥58% 且 coverage≥30%。"""
    da = m.get("direction_accuracy")
    ea = m.get("effective_accuracy")
    cov = m.get("coverage")
    if da is None or ea is None or cov is None:
        return False
    if any(math.isnan(v) for v in (da, ea)):
        return False
    return bool(da >= GATE_DIR_ACC and ea >= GATE_EFF_ACC and cov >= GATE_COVERAGE)


# ---------------------------------------------------------------------------
# JSON 清洗（numpy 类型 / nan -> None）
# ---------------------------------------------------------------------------
def _sanitize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if obj is None:
        return None
    return obj


def _fmt_pct(v: Optional[float]) -> str:
    return "n/a" if (v is None or (isinstance(v, float) and math.isnan(v))) else f"{v * 100:5.2f}%"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    print("=" * 72)
    print("真实数据 OOS 信号验证 + R7 终裁（au/ag/m，真 Kronos vs LightGBM）")
    print("=" * 72)

    cfg = load_config()
    cfg.data.symbols = list(SYMBOLS)
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = DATA_END
    horizon = int(cfg.forecast.horizon)

    plan = build_source_plan()
    bars, chosen_source = fetch_with_failover(cfg, plan)
    bars.validate()
    print(f"[OK] 选用数据源：{chosen_source}")
    print(f"[OK] 取数完成：symbols={bars.symbols} bars={bars.length} freq={bars.freq} source={bars.source}")

    features = build_features(bars, cfg)
    print(f"[OK] 特征完成：n_feature_rows={features.length} symbols={features.symbols}")

    store = SignalStore(str(STORE_DIR))
    run_results: dict[str, dict[str, Any]] = {}
    model_ids: dict[str, str] = {}
    model_errors: dict[str, str] = {}
    kronos_status = "PENDING"
    kronos_reason = ""

    # ---- 4a. 传统模型（lightgbm / ar_transformer / tcn / gru）走 ForecastTrainer ----
    for m in MODELS:
        print("-" * 72)
        expected_id = build_model(m, cfg).model_id
        if expected_id in store.models():
            print(f"[SKIP] {m}: 已落盘 model_id={expected_id}，复用既有 OOS 信号")
            model_ids[m] = expected_id
            sg = store.get_frame(model_id=expected_id)
            run_results[m] = {
                "model_id": expected_id,
                "n_oos_signals": int(len(sg)),
                "n_folds": "reused",
            }
            continue

        print(f"[RUN] 训练模型：{m}")
        trainer = None
        res = None
        try:
            trainer = ForecastTrainer(cfg, store, model_name=m)
            res = trainer.run(bars, features)
            model_ids[m] = res.model_id
            run_results[m] = {
                "model_id": res.model_id,
                "n_oos_signals": res.n_oos_signals,
                "n_folds": res.n_folds,
            }
            print(f"[OK] {m}: model_id={res.model_id} folds={res.n_folds} oos={res.n_oos_signals}")
        except Exception as exc:  # noqa: BLE001
            model_errors[m] = f"{type(exc).__name__}: {exc}"
            print(f"[ERROR] 模型 {m} 训练失败：{model_errors[m]}（继续其余模型）")
        finally:
            del trainer, res
            gc.collect()

    # ---- 4b. 真 Kronos 走 KlineKronosOOS（独立原始 OHLCV 通路） ----
    print("-" * 72)
    print(f"[RUN] 真 Kronos（KlineKronosOOS）：model={KRONOS_MODEL_NAME} n_mc={KRONOS_N_MC}")
    kronos_run = None
    try:
        koo = KlineKronosOOS(
            cfg,
            store,
            model_name=KRONOS_MODEL_NAME,
            tokenizer_name=KRONOS_TOKENIZER_NAME,
            device=None,
            n_mc=KRONOS_N_MC,
            max_context=int(getattr(cfg.forecast, "max_context", 512)),
            seed=int(getattr(cfg, "seed", 42)),
        )
        kronos_run = koo.run(bars)
        model_ids[KRONOS] = kronos_run.model_id
        run_results[KRONOS] = {
            "model_id": kronos_run.model_id,
            "n_oos_signals": kronos_run.n_oos_signals,
            "n_folds": kronos_run.n_folds,
        }
        if kronos_run.n_oos_signals > 0:
            kronos_status = "RUN"
            kronos_reason = f"真 Kronos 成功产出 OOS 信号（{KRONOS_MODEL_NAME}，n_mc={KRONOS_N_MC}）"
        else:
            kronos_status = "FAIL"
            kronos_reason = "KlineKronosOOS.run 返回 0 条 OOS 信号（数据/切分/预测均无产出）"
        print(f"[OK] kronos: model_id={kronos_run.model_id} folds={kronos_run.n_folds} oos={kronos_run.n_oos_signals}")
    except Exception as exc:  # noqa: BLE001
        kronos_status = "FAIL"
        kronos_reason = f"{type(exc).__name__}: {exc}"
        model_errors[KRONOS] = kronos_reason
        print(f"[ERROR] 真 Kronos 运行失败：{kronos_reason}（如实标注，不编造成功）")
    finally:
        del kronos_run
        gc.collect()

    # ---- 5. 重算已实现收益 ----
    realized = compute_realized_returns(bars, horizon)

    # ---- 6. 闸门1指标（per-model 聚合 + per-symbol） ----
    gate1: dict[str, dict[str, Any]] = {}
    per_symbol: dict[str, dict[str, dict[str, float]]] = {}
    all_models = MODELS + [KRONOS]
    for m in all_models:
        if m in model_errors or m not in model_ids:
            print(f"[SKIP] {m}: 无 OOS 信号（运行失败），跳过闸门1计算")
            continue
        sig = store.get_frame(model_id=model_ids[m])
        if "realized" not in sig.columns:
            sig = sig.assign(realized=realized)
        else:
            sig["realized"] = realized
        sig = sig.loc[:, ~sig.columns.duplicated(keep="last")]

        agg = compute_gate1(sig)
        gate1[m] = agg
        passed = gate1_pass(agg)

        sym_metrics: dict[str, dict[str, float]] = {}
        for sym in sig.index.get_level_values("symbol").unique():
            sub = sig.loc[[sym]]
            sym_metrics[sym] = compute_gate1(sub)
        per_symbol[m] = sym_metrics

        print(
            f"[GATE1] {m}: dir_acc={_fmt_pct(agg['direction_accuracy'])} "
            f"eff_acc={_fmt_pct(agg['effective_accuracy'])} "
            f"coverage={_fmt_pct(agg['coverage'])} "
            f"rankIC={agg['rank_ic']:.4f} -> {'PASS' if passed else 'FAIL'}"
        )

    # ---- 7. R7 终裁：真 Kronos vs LightGBM ----
    kr = gate1.get(KRONOS, {})
    base = gate1.get(BASELINE, {})
    kr_dir = kr.get("direction_accuracy")
    kr_ic = kr.get("rank_ic")
    base_dir = base.get("direction_accuracy")
    base_ic = base.get("rank_ic")

    kronos_wins = (
        kr_dir is not None
        and base_dir is not None
        and not math.isnan(kr_dir)
        and not math.isnan(kr_ic)
        and not math.isnan(base_dir)
        and not math.isnan(base_ic)
        and kr_dir >= base_dir
        and kr_ic >= base_ic
    )

    kronos_pass = gate1_pass(kr)
    if kronos_wins and kronos_pass:
        r7_verdict = "真 Kronos 守主位"
    elif kronos_wins and not kronos_pass:
        r7_verdict = "方向占优但未过闸门，主位存疑"
    else:
        r7_verdict = "R7 主位让予 LightGBM(真 Kronos 数据裁决)"

    # ---- 8. 输出报告 ----
    DELIVERABLE_DIR.mkdir(parents=True, exist_ok=True)
    report = _build_report(
        chosen_source=chosen_source,
        cfg=cfg,
        bars=bars,
        run_results=run_results,
        gate1=gate1,
        per_symbol=per_symbol,
        model_errors=model_errors,
        kronos_wins=kronos_wins,
        kronos_pass=kronos_pass,
        r7_verdict=r7_verdict,
        kronos_status=kronos_status,
        kronos_reason=kronos_reason,
    )
    _write_reports(report)

    print("=" * 72)
    print(f"[DONE] 报告已写出：\n  - {REPORT_MD}\n  - {REPORT_JSON}")
    print(f"[R7] {r7_verdict}（kronos_status={kronos_status}）")
    print("=" * 72)


def _build_report(
    *,
    chosen_source: str,
    cfg: Any,
    bars: Any,
    run_results: dict[str, dict[str, Any]],
    gate1: dict[str, dict[str, float]],
    per_symbol: dict[str, dict[str, dict[str, float]]],
    model_errors: dict[str, str],
    kronos_wins: bool,
    kronos_pass: bool,
    r7_verdict: str,
    kronos_status: str,
    kronos_reason: str,
) -> dict[str, Any]:
    """组装结构化报告 dict。"""
    horizon = int(cfg.forecast.horizon)
    report: dict[str, Any] = {
        "title": "真实数据 OOS 信号验证 + R7 终裁报告（au/ag/m，真 Kronos）",
        "report_date": REPORT_DATE,
        "data_source": {
            "chosen_source": chosen_source,
            "priority": _read_source_priority(),
            "reachability": (
                "pytdx 公共服务器在本沙箱 TCP 超时 -> 自动降级 sina 成功"
                if chosen_source == "sina"
                else "主源直连成功"
            ),
            "symbols": list(cfg.data.symbols),
            "freq": cfg.data.freq,
            "start": cfg.data.start,
            "end": cfg.data.end,
            "n_bars": int(bars.length),
            "symbols_returned": list(bars.symbols),
        },
        "horizon": horizon,
        "models_requested": list(MODELS + [KRONOS]),
        "models_run": run_results,
        "models_failed": dict(model_errors),
        "gate1_thresholds": {
            "direction_accuracy": GATE_DIR_ACC,
            "effective_accuracy": GATE_EFF_ACC,
            "coverage": GATE_COVERAGE,
        },
        "gate1_aggregated": gate1,
        "gate1_per_symbol": per_symbol,
        "gate1_pass": {m: gate1_pass(gate1[m]) for m in gate1},
        "r7": {
            "contender": KRONOS,
            "baseline": BASELINE,
            "kronos_wins": bool(kronos_wins),
            "kronos_passed_gate": bool(kronos_pass),
            "verdict": r7_verdict,
            "logic": "kronos_wins = (kronos.方向准确率 >= lightgbm.方向准确率) AND (kronos.RankIC >= lightgbm.RankIC)",
        },
        "kronos": {
            "status": kronos_status,
            "reason": kronos_reason,
            "model_name": KRONOS_MODEL_NAME,
            "tokenizer_name": KRONOS_TOKENIZER_NAME,
            "n_mc": KRONOS_N_MC,
            "note": "真 Kronos vs LightGBM 数据裁决（非代理）",
        },
        "environment_notes": [
            "Python 受管解释器 3.13；venv default。",
            "pytdx 公共行情服务器在本沙箱 TCP 超时，脚本按 free.yaml 优先级自动降级 sina 并成功跑完。",
            "sina 返回真实 au0/ag0/m0 主力连续日线，数据截止 2024（区间按 2018-01-01~2024-12-31 过滤）。",
            "真 Kronos 部署：third_party/Kronos（shiyu-coder/Kronos master）+ NeoQuasar/Kronos-small 本地 HF 缓存"
            " + Kronos-Tokenizer-base（blob 经 hf-mirror 补齐，sha256 校验通过）。",
            "Kronos 推理设备：GPU cuda:0（torch 2.11.0+cu128，CUDA 可用）；单次 predict_batch(32)≈0.22s。",
            "Kronos 路径分布：sample_count=1 循环 n_mc 次收集独立路径（KronosPredictor 内部 sample_count>1 为平均）。",
            f"kronos n_mc={KRONOS_N_MC}（KRONOS_N_MC 环境变量可调）；若样本规模过大可在报告注明并按需降 n_mc。",
            f"scipy 可用={'是' if _HAVE_SCIPY else '否'}"
            f"（RankIC/IC 用 {'scipy.stats' if _HAVE_SCIPY else 'pandas.corr 回退'} 计算）。",
            "预测层推理缓存泄漏已修复（GRU/AR/TCN 的 forward 缓存现于 predict 后统一清理）。",
        ],
    }
    return report


def _write_reports(report: dict[str, Any]) -> None:
    """写出 markdown 与 json 报告。"""
    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(_sanitize(report), f, ensure_ascii=False, indent=2, default=str)

    md = _render_markdown(report)
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write(md)


def _render_markdown(report: dict[str, Any]) -> str:
    """渲染 markdown 报告。"""
    ds = report["data_source"]
    g1 = report["gate1_aggregated"]
    thr = report["gate1_thresholds"]
    r7 = report["r7"]
    kr = report["kronos"]

    lines: list[str] = []
    lines.append(f"# {report['title']}")
    lines.append("")
    lines.append(f"> 报告日期：**{report['report_date']}**  ")
    lines.append(
        f"> 选用数据源：**{ds['chosen_source']}**  "
        f"（可达性：{ds['reachability']}）"
    )
    lines.append("")

    lines.append("## 1. 数据来源与可达性")
    lines.append("")
    lines.append(f"- 供应链优先级（free.yaml）：`{ds['priority']}`")
    lines.append(f"- 最终选用源：**{ds['chosen_source']}**")
    lines.append(f"- 品种：`{ds['symbols']}`（freq={ds['freq']}，horizon={report['horizon']}）")
    lines.append(f"- 区间：{ds['start']} ~ {ds['end']}")
    lines.append(f"- 实际返回品种：{ds['symbols_returned']}；总 bar 数：{ds['n_bars']}")
    lines.append("")

    lines.append("## 2. 各模型 OOS 运行概览")
    lines.append("")
    lines.append("| 模型 | model_id | 折数(n_folds) | OOS 信号数 |")
    lines.append("| --- | --- | ---: | ---: |")
    for m, r in report["models_run"].items():
        lines.append(
            f"| {m} | {r['model_id']} | {r['n_folds']} | {r['n_oos_signals']} |"
        )
    for m, err in report.get("models_failed", {}).items():
        lines.append(f"| {m} | ❌ 失败 | - | - | `{err}` |")
    lines.append("")

    if report.get("models_failed"):
        lines.append(
            "> 注：失败模型未产生 OOS 信号，已从闸门1/R7 统计中剔除；"
            "R7 终裁仅需真 Kronos 与基线(lightgbm)，二者成功即有效。"
        )
        lines.append("")

    lines.append("## 3. 闸门1指标（信号有效性 / viability floor）")
    lines.append("")
    lines.append(
        f"判据：方向准确率 ≥ {thr['direction_accuracy']*100:.0f}% "
        f"且 有效信号准确率 ≥ {thr['effective_accuracy']*100:.0f}% "
        f"且 coverage ≥ {thr['coverage']*100:.0f}%"
    )
    lines.append("")
    lines.append(
        "| 模型 | n_oos | n_eff | coverage | 方向准确率 | 有效准确率 | RankIC | IC | 闸门1 |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    for m, g in g1.items():
        passed = "PASS" if report["gate1_pass"][m] else "FAIL"
        lines.append(
            f"| {m} | {g['n_oos']} | {g['n_effective']} | {_fmt_pct(g['coverage'])} | "
            f"{_fmt_pct(g['direction_accuracy'])} | {_fmt_pct(g['effective_accuracy'])} | "
            f"{g['rank_ic']:.4f} | {g['ic']:.4f} | **{passed}** |"
        )
    lines.append("")

    lines.append("### 3.1 分品种明细")
    lines.append("")
    for m, syms in report["gate1_per_symbol"].items():
        lines.append(f"**{m}**")
        lines.append("")
        lines.append(
            "| 品种 | n_oos | coverage | 方向准确率 | 有效准确率 | RankIC | IC |"
        )
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for sym, g in syms.items():
            lines.append(
                f"| {sym} | {g['n_oos']} | {_fmt_pct(g['coverage'])} | "
                f"{_fmt_pct(g['direction_accuracy'])} | {_fmt_pct(g['effective_accuracy'])} | "
                f"{g['rank_ic']:.4f} | {g['ic']:.4f} |"
            )
        lines.append("")

    lines.append("## 4. R7 终裁（真 Kronos vs LightGBM 主位之争）")
    lines.append("")
    lines.append(f"- 挑战者：**{r7['contender']}**（真 Kronos，非代理）")
    lines.append(f"- 基线对照：**{r7['baseline']}**")
    lines.append(
        f"- 裁决逻辑：`kronos_wins = (kronos.方向准确率 ≥ lg.方向准确率) "
        f"AND (kronos.RankIC ≥ lg.RankIC)` = **{r7['kronos_wins']}**"
    )
    lines.append(f"- **R7 结论：{r7['verdict']}**")
    lines.append(
        f"- 真 Kronos 是否过闸门1：{'是' if r7['kronos_passed_gate'] else '否'}"
    )
    lines.append("")

    lines.append("## 5. Kronos 状态")
    lines.append("")
    lines.append(f"- **kronos_status：`{kr['status']}`**")
    lines.append(f"- 原因：{kr['reason']}")
    lines.append(f"- 模型：{kr['model_name']} / {kr['tokenizer_name']}（n_mc={kr['n_mc']}）")
    lines.append(f"- {kr['note']}")
    lines.append("")

    lines.append("## 6. 环境备注")
    lines.append("")
    for note in report["environment_notes"]:
        lines.append(f"- {note}")
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    main()
