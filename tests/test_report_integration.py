"""T05 报告集成测试：报告含双口径/指纹/CI 三块且旧 key 不变（PRD A1.3/A3.5/A4.3）。"""

from __future__ import annotations

import os
import tempfile

from _helpers import fast_cfg


def _mini_cfg():
    cfg = fast_cfg(n_bars=260)
    cfg.data.symbols = ["SHFE.cu"]
    cfg.forecast.epochs = 2
    cfg.forecast.hidden_size = 16
    cfg.forecast.n_layers = 1
    cfg.forecast.n_heads = 2
    cfg.backtest.bootstrap.n_boot = 50  # 测试加速
    return cfg


def test_pipeline_report_contains_p0_blocks():
    from hexbroker.pipeline import run_pipeline

    cfg = _mini_cfg()
    report_dir = tempfile.mkdtemp(prefix="pipeline_p0_")
    summary = run_pipeline(
        cfg, source="synthetic", model="ar_transformer",
        skip_rl=True, skip_evolution=True, report_dir=report_dir,
    )

    # P0 三块存在
    assert "dual_caliber" in summary
    assert "fingerprints" in summary
    assert "bootstrap" in summary

    dc = summary["dual_caliber"]
    assert "error" not in dc
    assert set(dc) == {"same_bar", "next_bar", "delta_pct", "note"}

    boot = summary["bootstrap"]
    assert "error" not in boot
    assert set(boot) == {"sharpe", "calmar", "max_drawdown", "params"}

    # 既有 key 一律不动（旧消费方兼容）
    for k in ("run_id", "config", "train", "signals", "baselines", "rl", "evolution", "gates", "elapsed_seconds"):
        assert k in summary, f"既有 key {k} 丢失"
    assert "gate1" in summary["gates"] and "gate2" in summary["gates"]
    assert set(summary["baselines"].keys()) >= {"buy_hold", "dual_ma", "macd", "signal_threshold"}

    # 报告文件落盘且含三个新章节
    assert os.path.exists(summary["report_path"])
    md = open(summary["report_path"], encoding="utf-8").read()
    assert "撮合双口径对照（P0-1）" in md
    assert "四层指纹（P0-3）" in md
    assert "Bootstrap 绩效区间（P0-4）" in md


def test_pipeline_no_dual_caliber_flag():
    from hexbroker.pipeline import run_pipeline

    cfg = _mini_cfg()
    report_dir = tempfile.mkdtemp(prefix="pipeline_p0_nodc_")
    summary = run_pipeline(
        cfg, source="synthetic", model="ar_transformer",
        skip_rl=True, skip_evolution=True, report_dir=report_dir,
        enable_dual_caliber=False,
    )
    assert summary["dual_caliber"] is None
    assert "fingerprints" in summary and "bootstrap" in summary


def test_pipeline_fingerprints_from_signal_store():
    """trainer 内部计算的四层指纹应随报告输出（A3.5）。"""
    from hexbroker.pipeline import run_pipeline

    cfg = _mini_cfg()
    report_dir = tempfile.mkdtemp(prefix="pipeline_p0_fp_")
    summary = run_pipeline(
        cfg, source="synthetic", model="ar_transformer",
        skip_rl=True, skip_evolution=True, report_dir=report_dir,
    )
    fps = summary["fingerprints"]
    assert isinstance(fps, list)
    if fps:
        rec = fps[0]
        for k in ("data_version", "feature_version", "model_version", "param_hash", "config_version"):
            assert k in rec, f"指纹缺 {k}"
