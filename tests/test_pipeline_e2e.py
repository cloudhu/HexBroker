"""T05 端到端管线冒烟测试：一条命令出报告（闸门/基线/RL/诊断齐全）。"""

from __future__ import annotations

import os
import tempfile


from _helpers import fast_cfg


def test_pipeline_e2e_demo():
    cfg = fast_cfg(n_bars=260)
    cfg.data.symbols = ["SHFE.cu"]
    cfg.forecast.epochs = 2
    cfg.forecast.hidden_size = 16
    cfg.forecast.n_layers = 1
    cfg.forecast.n_heads = 2
    cfg.rl.total_timesteps = 1200
    cfg.rl.hidden_size = 16
    cfg.rl.n_hidden = 1

    from hexbroker.pipeline import run_pipeline

    report_dir = tempfile.mkdtemp(prefix="pipeline_report_")
    summary = run_pipeline(
        cfg, source="synthetic", model="ar_transformer",
        skip_evolution=True, rl_steps=1200, report_dir=report_dir,
    )
    # 报告文件落盘
    assert os.path.exists(summary["report_path"])
    assert os.path.exists(summary["json_path"])

    # 信号闸门 1 存在
    g = summary["gates"]
    assert "gate1" in g and "gate2" in g
    assert set(summary["baselines"].keys()) >= {"buy_hold", "dual_ma", "macd", "signal_threshold"}

    # RL 训练完成（未跳过）
    assert summary["rl"] is not None
    assert summary["rl"]["train_seconds"] > 0
    assert "better_than_threshold" in summary["rl"]

    # 关键指标字段齐全（报告强制并列 胜率/盈亏比/回撤 口径）
    m = summary["rl"]["metrics"]
    for k in ("sharpe", "win_rate", "max_drawdown", "calmar", "n_bars"):
        assert k in m


def test_pipeline_skip_rl_still_reports_gate2_on_threshold():
    cfg = fast_cfg(n_bars=260)
    cfg.data.symbols = ["SHFE.cu"]
    cfg.forecast.epochs = 2

    from hexbroker.pipeline import run_pipeline

    report_dir = tempfile.mkdtemp(prefix="pipeline_report_")
    summary = run_pipeline(
        cfg, source="synthetic", model="ar_transformer",
        skip_rl=True, skip_evolution=True, report_dir=report_dir,
    )
    assert summary["rl"] is None
    assert summary["gates"]["candidate"] == "signal_threshold"
    assert "pbo" in summary["gates"] and "dsr" in summary["gates"]
