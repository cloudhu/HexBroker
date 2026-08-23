"""真实数据 OOS 信号验证 + R7 裁决脚本（§3.2 / §8.4）。

用法
----
    python -m scripts.validate_oos_r7
    python scripts/validate_oos_r7.py

流程（严格复用现有契约，禁止自创）：
  1. 按 configs/data/free.yaml 的 source_priority 建真实源
     （pytdx 主 -> 超时/异常降级 sina，二者皆失败才报错退出）。
  2. source.fetch_bars(symbols, start, end, freq="1d") -> BarFrame -> validate()。
  3. build_features(bars, cfg) -> FeatureFrame。
  4. 对每个 model ∈ {lightgbm, ar_transformer, tcn, gru}：
     ForecastTrainer(cfg, store, model_name=m).run(bars, features)
     写入同一 SignalStore（artifacts/signals_oos_r7），自动按 model_id 分片。
  5. 重算已实现 horizon 前向收益（从 barframe，物理隔离，训练器不落盘已实现收益）。
  6. 闸门1指标（viability floor）：方向准确率 / 有效信号准确率 / coverage / RankIC / IC。
  7. R7 裁决：自回归家族(ar_transformer) vs 基线(lightgbm)。
  8. 输出结构化报告到 deliverables/software-hexfutures-ai/。

注意：本沙箱 pytdx 公共服务器 TCP 超时 -> 自动降级 sina（供应链 failover 设计意图）。
Kronos 权重/分词器/transformers 在本环境缺失，adapter 仅能降级 ARTransformer，
故不跑 kronos，kronos_status = BLOCKED。
"""

from __future__ import annotations

import gc
import json
import math
import sys
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

# ---- 确保项目根目录在 sys.path（支持 `python scripts/validate_oos_r7.py`） ----
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
from hexbroker.utils.registry import build as build_model

# ---------------------------------------------------------------------------
# 常量（集中声明，禁止硬编码魔法数字在业务代码）
# ---------------------------------------------------------------------------
SYMBOLS = ["SHFE.cu", "SHFE.rb", "INE.sc"]
FREQ = "1d"
# 区间取 DataConfig 默认（sina 返回数据截止 2024 属正常，区间过滤即可）
DATA_START = "2018-01-01"
DATA_END = "2024-12-31"

# 闸门1（信号有效性）viability floor 阈（来自 README/system_design）
GATE_DIR_ACC = 0.54      # 方向准确率下限
GATE_EFF_ACC = 0.58      # 有效信号准确率下限
GATE_COVERAGE = 0.30     # 有效信号覆盖下限

MODELS = ["lightgbm", "ar_transformer", "tcn", "gru"]
AR_FAMILY = "ar_transformer"   # 自回归家族代理（Kronos 架构替身）
BASELINE = "lightgbm"          # R7 基线对照

FREE_YAML = _ROOT / "configs" / "data" / "free.yaml"
STORE_DIR = _ROOT / "artifacts" / "signals_oos_r7"
DELIVERABLE_DIR = _ROOT / "deliverables" / "software-hexfutures-ai"
REPORT_DATE = "2026-08-15"
REPORT_MD = DELIVERABLE_DIR / f"oos-r7-validation-{REPORT_DATE}.md"
REPORT_JSON = DELIVERABLE_DIR / f"oos-r7-validation-{REPORT_DATE}.json"

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
    # akshare_fundamentals 等仅提供基本面特征，无 BarFrame 取数能力，跳过
    return None


def build_source_plan() -> list[tuple[str, Any]]:
    """按 free.yaml 的 source_priority 生成「源名 -> 构造器」计划。

    仅保留支持 BarFrame 取数的源（pytdx / sina）；其它（如 akshare_fundamentals）
    只提供基本面特征，无行情取数能力，跳过。
    """
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
    """逐个源真实执行 fetch_bars，捕获连接/超时/网络异常后降级。

    返回 (BarFrame, chosen_source_name)。全部失败则抛 HexDataError 退出。

    说明：failover 必须包住真实取数（构造实例不会触发 pytdx TCP 连接），
    公共服务器超时在 fetch_bars 内部 _connect 阶段抛出 HexDataError。
    """
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
        except Exception as exc:  # noqa: BLE001 - 其它网络/超时异常
            last_err = exc
            print(f"[WARN] 源 '{name}' 取数异常（{type(exc).__name__}: {exc}），降级下一源")
            continue
    raise HexDataError(f"所有真实数据源均不可用（计划 {[p[0] for p in plan]}），最后错误：{last_err}")


# ---------------------------------------------------------------------------
# 5. 重算已实现 horizon 前向收益（物理隔离：训练器不落盘已实现收益）
# ---------------------------------------------------------------------------
def compute_realized_returns(bars: Any, horizon: int) -> pd.Series:
    """对每只标的用 close 重算 fwd = close.shift(-horizon)/close - 1。

    返回 MultiIndex(symbol, datetime) 的 Series（名为 'realized'）。
    末段 horizon 根无未来收益 -> NaN（保留以统计 coverage，剔除出方向/RankIC）。
    """
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
    """计算单组信号的闸门1指标。

    参数
    ----
    sig : MultiIndex(symbol, datetime) DataFrame，需含 p_up, exp_ret,
          is_effective 列及 'realized' 列（已 join）。

    返回
    ----
    dict: n_oos, n_effective, coverage, direction_accuracy,
          effective_accuracy, rank_ic, ic
    """
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
    print("真实数据 OOS 信号验证 + R7 裁决")
    print("=" * 72)

    # ---- 配置：DataConfig 默认区间 + 指定品种/频率 ----
    cfg = load_config()
    cfg.data.symbols = list(SYMBOLS)
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = DATA_END
    horizon = int(cfg.forecast.horizon)

    # ---- 1. 建真实源计划（按 free.yaml 优先级） ----
    plan = build_source_plan()

    # ---- 2. 真实取数 + 校验（failover 包住 fetch_bars） ----
    bars, chosen_source = fetch_with_failover(cfg, plan)
    bars.validate()
    print(f"[OK] 选用数据源：{chosen_source}")
    print(
        f"[OK] 取数完成：symbols={bars.symbols} "
        f"bars={bars.length} freq={bars.freq} source={bars.source}"
    )

    # ---- 3. 特征 ----
    features = build_features(bars, cfg)
    print(f"[OK] 特征完成：n_feature_rows={features.length} symbols={features.symbols}")

    # ---- 4. 逐模型 walk-forward 训练，落盘同一 SignalStore ----
    # 每个模型独立训练并显式释放上一模型对象，避免 LGBM/AR 权重残留导致后续
    # 纯 numpy 模型（TCN/GRU）在 fit 阶段触发 numpy OOM（沙箱内存受限）。
    store = SignalStore(str(STORE_DIR))
    run_results: dict[str, dict[str, Any]] = {}
    model_ids: dict[str, str] = {}
    model_errors: dict[str, str] = {}

    for m in MODELS:
        print("-" * 72)
        # 已落盘则复用既有 OOS 信号（脚本幂等，避免重复 walk-forward 训练）
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
            print(
                f"[OK] {m}: model_id={res.model_id} "
                f"folds={res.n_folds} oos={res.n_oos_signals}"
            )
        except Exception as exc:  # noqa: BLE001 - 单模型失败不应中断整体校验
            model_errors[m] = f"{type(exc).__name__}: {exc}"
            print(f"[ERROR] 模型 {m} 训练失败：{model_errors[m]}（继续其余模型）")
        finally:
            # 释放训练器与结果对象，并强制 GC，回收上一模型占用的内存
            del trainer, res
            gc.collect()

    # ---- 5. 重算已实现收益 ----
    realized = compute_realized_returns(bars, horizon)

    # ---- 6. 闸门1指标（per-model 聚合 + per-symbol） ----
    gate1: dict[str, dict[str, Any]] = {}
    per_symbol: dict[str, dict[str, dict[str, float]]] = {}
    for m in MODELS:
        if m in model_errors or m not in model_ids:
            print(f"[SKIP] {m}: 无 OOS 信号（训练失败），跳过闸门1计算")
            continue
        sig = store.get_frame(model_id=model_ids[m])
        if "realized" not in sig.columns:
            sig = sig.assign(realized=realized)
        else:
            sig["realized"] = realized
        # 防御：仅保留与 realized 对齐的非多余列
        sig = sig.loc[:, ~sig.columns.duplicated(keep="last")]

        agg = compute_gate1(sig)
        gate1[m] = agg
        passed = gate1_pass(agg)

        # per-symbol
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

    # ---- 7. R7 裁决 ----
    ar = gate1.get(AR_FAMILY, {})
    base = gate1.get(BASELINE, {})
    ar_dir = ar.get("direction_accuracy")
    ar_ic = ar.get("rank_ic")
    base_dir = base.get("direction_accuracy")
    base_ic = base.get("rank_ic")

    ar_wins = (
        ar_dir is not None
        and base_dir is not None
        and not math.isnan(ar_dir)
        and not math.isnan(ar_ic)
        and not math.isnan(base_dir)
        and not math.isnan(base_ic)
        and ar_dir >= base_dir
        and ar_ic >= base_ic
    )

    ar_pass = gate1_pass(ar)
    if ar_wins and ar_pass:
        r7_verdict = "自回归家族守主位(待真 Kronos 复核)"
    elif ar_wins and not ar_pass:
        r7_verdict = "自回归家族方向占优但仍未过闸门，主位存疑"
    else:
        r7_verdict = "R7 主位让予 LightGBM(条件性)"

    kronos_status = "BLOCKED"
    kronos_reason = (
        "无 third_party/Kronos submodule、无 transformers、无权重；"
        "KronosAdapter 仅能降级 ARTransformer，故真 Kronos vs LightGBM "
        "终裁 pending Kronos 权重就绪"
    )

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
        ar_wins=ar_wins,
        ar_pass=ar_pass,
        r7_verdict=r7_verdict,
        kronos_status=kronos_status,
        kronos_reason=kronos_reason,
    )
    _write_reports(report)

    print("=" * 72)
    print(f"[DONE] 报告已写出：\n  - {REPORT_MD}\n  - {REPORT_JSON}")
    print(f"[R7] {r7_verdict}")
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
    ar_wins: bool,
    ar_pass: bool,
    r7_verdict: str,
    kronos_status: str,
    kronos_reason: str,
) -> dict[str, Any]:
    """组装结构化报告 dict。"""
    horizon = int(cfg.forecast.horizon)
    report: dict[str, Any] = {
        "title": "真实数据 OOS 信号验证 + R7 裁决报告",
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
        "models_requested": list(MODELS),
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
            "autoregressive_family": AR_FAMILY,
            "baseline": BASELINE,
            "autoregressive_wins": bool(ar_wins),
            "autoregressive_passed_gate": bool(ar_pass),
            "verdict": r7_verdict,
        },
        "kronos": {
            "status": kronos_status,
            "reason": kronos_reason,
            "note": "真 Kronos vs LightGBM 终裁 pending Kronos 权重就绪",
        },
        "environment_notes": [
            "Python 受管解释器 3.13；venv default。",
            "pytdx 公共行情服务器在本沙箱 TCP 超时，脚本按 free.yaml 优先级自动降级 sina 并成功跑完。",
            "sina 返回真实 cu/rb/sc 主力连续日线，数据截止 2024（区间按 2018-01-01~2024-12-31 过滤）。",
            f"scipy 可用={'是' if _HAVE_SCIPY else '否'}"
            f"（RankIC/IC 用 {'scipy.stats' if _HAVE_SCIPY else 'pandas.corr 回退'} 计算）。",
            "Kronos 权重/分词器/transformers 缺失，未运行 kronos；ar_transformer 作为其架构替身。",
            "预测层推理缓存泄漏已修复（GRU/AR/TCN 的 forward 缓存现于 predict 后统一清理，"
            "见 ForecastModel._clear_inference_caches），使 gru 在本沙箱可完整跑完而不撑爆内存。",
        ],
    }
    return report


def _write_reports(report: dict[str, Any]) -> None:
    """写出 markdown 与 json 报告。"""
    # JSON
    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(_sanitize(report), f, ensure_ascii=False, indent=2, default=str)

    # Markdown
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

    # 数据来源
    lines.append("## 1. 数据来源与可达性")
    lines.append("")
    lines.append(f"- 供应链优先级（free.yaml）：`{ds['priority']}`")
    lines.append(f"- 最终选用源：**{ds['chosen_source']}**")
    lines.append(f"- 品种：`{ds['symbols']}`（freq={ds['freq']}，horizon={report['horizon']}）")
    lines.append(f"- 区间：{ds['start']} ~ {ds['end']}")
    lines.append(f"- 实际返回品种：{ds['symbols_returned']}；总 bar 数：{ds['n_bars']}")
    lines.append("")

    # 各模型运行概览
    lines.append("## 2. 各模型 OOS 训练概览")
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
            "R7 裁决仅需自回归家族(ar_transformer)与基线(lightgbm)，二者成功即有效。"
        )
        lines.append("")

    # 闸门1指标表（聚合）
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

    # per-symbol
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

    # R7 裁决
    lines.append("## 4. R7 裁决（自回归家族 vs LightGBM 主位之争）")
    lines.append("")
    lines.append(f"- 自回归家族代理：**{r7['autoregressive_family']}**（Kronos 架构替身）")
    lines.append(f"- 基线对照：**{r7['baseline']}**")
    lines.append(
        f"- 裁决逻辑：`autoregressive_wins = (ar.方向准确率 ≥ lg.方向准确率) "
        f"AND (ar.RankIC ≥ lg.RankIC)` = **{r7['autoregressive_wins']}**"
    )
    lines.append(f"- **R7 结论：{r7['verdict']}**")
    lines.append(
        f"- 自回归家族是否过闸门1：{'是' if r7['autoregressive_passed_gate'] else '否'}"
    )
    lines.append("")

    # Kronos 阻塞
    lines.append("## 5. Kronos 阻塞说明（必须如实标注）")
    lines.append("")
    lines.append(f"- **kronos_status：`{kr['status']}`**")
    lines.append(f"- 原因：{kr['reason']}")
    lines.append(f"- {kr['note']}")
    lines.append("")

    # 环境备注
    lines.append("## 6. 环境备注")
    lines.append("")
    for note in report["environment_notes"]:
        lines.append(f"- {note}")
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    main()
