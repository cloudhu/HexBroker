"""P0-2 lookahead 静态分析测试（PRD A2.1/A2.2/A2.4/A2.5）。

- 注入缺陷样本检出率 100%（6 类模式全检出）；
- 现有 feature 代码 0 个未豁免误报（默认豁免）；
- 豁免机制可用；CLI 退出码正确。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from _helpers import make_leaky_features

ROOT = Path(__file__).resolve().parents[1]


def _write_samples(tmp_path, samples):
    paths = []
    for i, code in enumerate(samples):
        p = tmp_path / f"leaky_{i}.py"
        p.write_text(code, encoding="utf-8")
        paths.append(str(p))
    return paths


def test_injected_samples_all_detected(tmp_path):
    from hexbroker.tools.lookahead_analysis import analyze

    paths = _write_samples(tmp_path, make_leaky_features())
    report = analyze(paths)  # 无豁免：全部检出
    assert not report.ok
    assert report.violations >= len(make_leaky_features())
    patterns = {e.pattern for e in report.suspicious}
    expected = {"shift_negative", "rolling_center", "ewm", "asof_without_reindex", "iloc_forward", "np_roll"}
    assert expected <= patterns, f"检出率非 100%：缺 {expected - patterns}"


def test_existing_feature_code_zero_unexempted():
    """对现有 feature 代码运行：0 个未豁免误报（PRD A2.2）。"""
    from hexbroker.tools.exemptions import ExemptionRegistry
    from hexbroker.tools.lookahead_analysis import analyze

    feature_dir = ROOT / "hexbroker" / "feature"
    report = analyze([str(feature_dir)], ExemptionRegistry.default_exemptions())
    assert report.ok, (
        "现有 feature 代码存在未豁免误报: "
        f"{[(e.pattern, e.file, e.line) for e in report.suspicious]}"
    )


def test_exemption_removes_violation(tmp_path):
    from hexbroker.tools.exemptions import ExemptionRegistry
    from hexbroker.tools.lookahead_analysis import analyze

    paths = _write_samples(tmp_path, make_leaky_features()[:1])
    reg = ExemptionRegistry()
    key = "shift_negative@" + str(paths[0]).replace("\\", "/")
    reg.register(key, "测试豁免")
    report = analyze(paths, reg)
    assert report.ok
    assert key in report.exempted


def test_default_exemptions_cover_known_safe_patterns():
    """默认豁免应覆盖三类已知安全模式（设计 §1.3）。"""
    from hexbroker.tools.exemptions import ExemptionRegistry

    reg = ExemptionRegistry.default_exemptions()
    assert reg.is_exempt("ewm@hexbroker/feature/technical.py")
    assert reg.is_exempt("asof_without_reindex@hexbroker/feature/global_ref.py")
    assert reg.is_exempt("rolling_center@hexbroker/feature/normalize.py")


def test_cli_lookahead_exit_code(tmp_path):
    paths = _write_samples(tmp_path, make_leaky_features()[:1])
    r = subprocess.run(
        [sys.executable, "-m", "hexbroker.tools.lookahead_analysis", paths[0]],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert r.returncode != 0, "缺陷样本应非零退出（CI 拦截）"

    clean = tmp_path / "clean.py"
    clean.write_text(
        'def safe(df):\n    df["f_x"] = df["close"].shift(1) / df["close"]\n    return df\n',
        encoding="utf-8",
    )
    r2 = subprocess.run(
        [sys.executable, "-m", "hexbroker.tools.lookahead_analysis", str(clean)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert r2.returncode == 0, f"干净样本应退出码 0：{r2.stdout}\n{r2.stderr}"


def test_tool_is_read_only(tmp_path):
    """只读分析：不修改任何输入文件（PRD A2.5）。"""
    from hexbroker.tools.lookahead_analysis import analyze

    paths = _write_samples(tmp_path, make_leaky_features())
    before = {p: Path(p).read_text(encoding="utf-8") for p in paths}
    analyze(paths)
    after = {p: Path(p).read_text(encoding="utf-8") for p in paths}
    assert before == after
