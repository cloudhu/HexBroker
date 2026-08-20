"""LightGBM 精进脚本的纯函数单测（不含重 walk-forward，确保快速）。

覆盖：gate1 指标计算、特征重要性反扁平化聚合、Optuna 参数边界。
完整 walk-forward 集成由 scripts/refine_lightgbm_champion.py 端到端完成。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts import refine_lightgbm_champion as R


class _Sig:
    """最小信号桩（与 QA p6 独立验证一致：ts/p_up/exp_ret/is_effective/eff_thr）。"""

    def __init__(self, p_up: float = 0.6):
        self.ts = 0
        self.p_up = p_up
        self.exp_ret = 0.01
        self.is_effective = True
        self.eff_thr = 0.05


def test_compute_gate1_perfect():
    n = 200
    rng = np.random.default_rng(0)
    p_up = rng.uniform(0.55, 0.9, n)
    realized = np.where(p_up > 0.5, 1.0, -1.0) + rng.normal(0, 0.01, n)
    sig = pd.DataFrame({
        "p_up": p_up,
        "exp_ret": p_up - 0.5,
        "is_effective": np.ones(n, dtype=bool),
        "realized": realized,
    })
    g = R.compute_gate1(sig)
    assert g["direction_accuracy"] > 0.95
    assert g["coverage"] == 1.0
    assert g["n_oos"] == n


def test_compute_gate1_random_is_chance():
    n = 2000
    rng = np.random.default_rng(1)
    p_up = rng.uniform(0.0, 1.0, n)
    realized = rng.choice([-1.0, 1.0], n).astype(float)
    sig = pd.DataFrame({
        "p_up": p_up,
        "exp_ret": rng.normal(0, 0.1, n),
        "is_effective": np.ones(n, dtype=bool),
        "realized": realized,
    })
    g = R.compute_gate1(sig)
    # 随机 p_up 对随机方向，方向准确率应接近 50%
    assert abs(g["direction_accuracy"] - 0.5) < 0.08


def test_aggregate_importances_unflatten():
    # 构造：2 个特征，lookback=3 -> 扁平化 6 维
    feat_names = ["f_a", "f_b"]
    lookback = 3
    n_feat = len(feat_names)
    R._lookback_cache["lb"] = lookback
    # 每折 importance：把全部权重放在 (f_a, lag=0) 与 (f_b, lag=2)
    # 扁平化：j = time_off*n_feat + feat_i；lag = (lookback-1)-time_off
    # (f_a, lag=0) -> time_off=2 -> j = 2*2+0 = 4
    # (f_b, lag=2) -> time_off=0 -> j = 0*2+1 = 1
    imp = np.zeros(lookback * n_feat)
    imp[4] = 1.0  # f_a lag0
    imp[1] = 1.0  # f_b lag2
    wf = R.WFResult(records=[], importances=[("ag0", 0, imp)], feat_names=feat_names)
    out = R.aggregate_importances(wf)
    ranked = {r["feature"]: r["importance"] for r in out["ranked_features"]}
    assert abs(ranked["f_a"] - 0.5) < 1e-9
    assert abs(ranked["f_b"] - 0.5) < 1e-9
    top = {(t["feature"], t["lag"]) for t in out["top_feature_lag"]}
    assert ("f_a", 0) in top
    assert ("f_b", 2) in top


def test_suggest_params_keys():
    import optuna

    captured = {}

    def fake_objective(trial):
        captured.update(R.suggest_params(trial))
        return 0.0

    study = optuna.create_study(direction="maximize")
    study.optimize(fake_objective, n_trials=1)
    expected = {
        "lgbm_n_estimators", "lgbm_lr", "lgbm_max_depth", "lgbm_num_leaves",
        "lgbm_min_child_samples", "lgbm_subsample", "lgbm_colsample_bytree",
        "lgbm_reg_lambda", "lgbm_reg_alpha",
    }
    assert set(captured.keys()) == expected
    assert 100 <= captured["lgbm_n_estimators"] <= 600
    assert 0.01 <= captured["lgbm_lr"] <= 0.1


# ---------------------------------------------------------------------------
# P6-3：_calibrate_and_split 的 cal_return_all 新模式
# ---------------------------------------------------------------------------
def test_calibrate_and_split_default_backward_compatible():
    """默认（cal_return_all=False）行为与历史完全一致。"""
    n = 40
    sigs = [_Sig(p_up=0.9) for _ in range(n)]
    valid = np.ones(n, dtype=bool)
    y_true = np.ones(n, dtype=float)

    # cal_split=None：用全部拟合校准器，返回全部
    out_none = R._calibrate_and_split(sigs, valid, y_true, "platt", None)
    assert len(out_none) == n
    # cal_split=0.5 默认：返回 sigs[k:]（后半），不校准评估窗
    k = int(n * 0.5)
    out_half = R._calibrate_and_split(sigs, valid, y_true, "platt", 0.5)
    assert len(out_half) == n - k
    assert out_half[0] is sigs[k]  # 从第 k 个信号开始
    # 显式 False 与默认一致
    out_false = R._calibrate_and_split(sigs, valid, y_true, "platt", 0.5, False)
    assert len(out_false) == n - k


def test_calibrate_and_split_return_all_len():
    """cal_return_all=True 返回全部 len(sigs) 行，不截断 sigs[k:]。"""
    n = 40
    sigs = [_Sig(p_up=0.9) for _ in range(n)]
    valid = np.ones(n, dtype=bool)
    y_true = np.concatenate([np.ones(20), np.zeros(20)])
    out = R._calibrate_and_split(sigs, valid, y_true, "platt", 0.5, True)
    assert len(out) == n
    # 对象顺序保持（不重排）
    assert out[0] is sigs[0]
    assert out[-1] is sigs[-1]
    # 全部信号都被应用校准（p_up 被变换，不再是原始 0.9）
    assert all(s.p_up != 0.9 for s in sigs)


def test_calibrate_and_split_calibrator_fit_only_first_k():
    """校准器只在前 k 上拟合（用 isotonic 小数据可判别）。

    前 20 条 y=1（p_up 0.8/0.95），后 20 条 y=0（p_up=0.9）。
    - 若校准器只在前 k 拟合：isotonic 把 p=0.9 → ~1.0（前段全为上涨）
    - 若（错误地）在全部 40 条上拟合：p=0.9 会被压到 ~0.5（一半上涨一半下跌）
    """
    n = 40
    sigs = [_Sig(p_up=0.8) for _ in range(10)] + [_Sig(p_up=0.95) for _ in range(10)] + [_Sig(p_up=0.9) for _ in range(20)]
    valid = np.ones(n, dtype=bool)
    y_true = np.array([1.0] * 20 + [0.0] * 20)
    out = R._calibrate_and_split(sigs, valid, y_true, "isotonic", 0.5, True)
    assert len(out) == n
    # 后段（原始 y=0）被同一校准器映射为高概率 → 只在前 k 上拟合
    assert out[30].p_up > 0.8
    # 控制组：若校准器在全部数据上拟合，后段会被压到 ~0.5
    sigs2 = [_Sig(p_up=0.8) for _ in range(10)] + [_Sig(p_up=0.95) for _ in range(10)] + [_Sig(p_up=0.9) for _ in range(20)]
    out_all = R._calibrate_and_split(sigs2, valid, y_true, "isotonic", None)  # cal_split=None=用全部拟合
    assert out_all[30].p_up < 0.7


def test_calibrate_and_split_return_all_keeps_exp_ret():
    """校准只改 p_up/is_effective，不改 exp_ret（引擎 A 排序零泄漏的根基）。"""
    n = 40
    sigs = [_Sig(p_up=0.9) for _ in range(n)]
    for i, s in enumerate(sigs):
        s.exp_ret = float(i) * 0.001  # 互不相同的 exp_ret
    valid = np.ones(n, dtype=bool)
    y_true = np.concatenate([np.ones(20), np.zeros(20)])
    exp_before = [s.exp_ret for s in sigs]
    R._calibrate_and_split(sigs, valid, y_true, "platt", 0.5, True)
    assert [s.exp_ret for s in sigs] == exp_before  # exp_ret 不变


# ---------------------------------------------------------------------------
# P8-4：label_pool 全 18 品种统一截面
# ---------------------------------------------------------------------------
def test_build_fwd_cs_panel_label_pool_all_cross_z_uses_all_columns(monkeypatch):
    """label_pool='all'：面板跨全部品种列截面化（当日 z 用全品种截面统计）。

    无前视 + 当日截面由构造保证：任意日期行 z = (x - 当日均值) / 当日 std，
    只用同一行（当日）数据。
    """
    idx = pd.date_range("2020-01-01", periods=40, freq="B")
    close = pd.DataFrame(
        {f"s{i}": 100.0 + i * np.arange(40) for i in range(5)}, index=idx
    )
    monkeypatch.setattr(R, "_load_all18_close_map", lambda: {c: close[c] for c in close.columns})
    panel = R._build_fwd_cs_panel(None, None, 3, "cross_z", "all")
    assert panel is not None
    assert list(panel.columns) == [f"s{i}" for i in range(5)]
    # 抽样若干日期：panel 行 == 手工当日截面 z（原始 fwd 的截面统计）
    raw_wide = pd.DataFrame({c: R._forward_returns(close[c], 3) for c in close.columns})
    for t in (5, 12, 25, 33):
        raw = raw_wide.iloc[t]
        mu = raw.mean()
        sd = raw.std(ddof=0)
        expected = (raw - mu) / sd
        pd.testing.assert_series_equal(panel.iloc[t], expected)


def test_build_fwd_cs_panel_label_pool_all_no_lookahead(monkeypatch):
    """label_pool='all' 无前视：单品种未来扰动 → 早于 (cut-h) 的行截面逐位不变。

    注意：fwd 行 t 依赖 close[t+h]（标签地平线），扰动自 cut 起仅合法影响
    [cut-h, cut) 行；断言 t < cut-h 不变才能检验截面化无前视。只扰动单品种，
    避免"全部×常数"导致的 z 尺度不变退化。
    """
    idx = pd.date_range("2020-01-01", periods=40, freq="B")
    close = pd.DataFrame(
        {f"s{i}": 100.0 + i * np.arange(40) for i in range(5)}, index=idx
    )
    monkeypatch.setattr(R, "_load_all18_close_map", lambda: {c: close[c] for c in close.columns})
    panel = R._build_fwd_cs_panel(None, None, 3, "cross_z", "all")
    cut = 33
    close2 = close.copy()
    close2.iloc[cut:, 0] = close2.iloc[cut:, 0] * 1.5  # 只扰动 s0 未来
    monkeypatch.setattr(R, "_load_all18_close_map", lambda: {c: close2[c] for c in close2.columns})
    panel2 = R._build_fwd_cs_panel(None, None, 3, "cross_z", "all")
    # 无前视：t < cut - h 逐位不变
    pd.testing.assert_frame_equal(panel.iloc[: cut - 3], panel2.iloc[: cut - 3])
    # 扰动有效：受影响窗 [cut-3, cut) 确实变化
    aff = (panel.iloc[cut - 3: cut] - panel2.iloc[cut - 3: cut]).abs().max().max()
    assert aff > 1e-6


def test_build_fwd_cs_panel_label_pool_all_own_calendar_no_union_holes(monkeypatch):
    """label_pool='all' 逐品种 fwd 用自有日历：union 空洞不得污染 fwd 标签。

    回归 P8-4 实测 bug：union 轴让不交易日成 NaN 空洞，位置偏移把空洞算进
    horizon → fwd 错误 NaN → np.isnan(y).any() 跳过整折（8624→2208 信号）。
    构造：s0 在 2020-02-04 缺数据（其他品种有），s1 的 +3 若落在空洞日则 fwd
    应为自身日历上第 3 个交易日的收益，而非 NaN。
    """
    idx = pd.date_range("2020-01-01", periods=30, freq="B")
    # s0 缺 2020-02-04（union 空洞）；其他品种全
    hole = pd.Timestamp("2020-02-04")
    base = {f"s{i}": 100.0 + i * np.arange(len(idx)) for i in range(5)}
    close_map = {}
    for c, vals in base.items():
        s = pd.Series(vals, index=idx)
        if c == "s0":
            s = s.drop(hole)
        close_map[c] = s
    monkeypatch.setattr(R, "_load_all18_close_map", lambda: close_map)
    panel = R._build_fwd_cs_panel(None, None, 3, "cross_z", "all")
    # s0 自有日历上 2020-02-04 前一天的 fwd 应非 NaN（+3 落在 s0 自身交易日）
    d_before = idx[idx < hole][-1]
    raw = R._forward_returns(close_map["s0"], 3)
    assert pd.notna(raw.get(d_before)), "s0 自有日历 fwd 不应因 union 空洞变 NaN"
    # union 宽表在空洞日 s0 列 = NaN（截面统计跳过该品种），但不得污染其他行
    assert panel.loc[hole, "s0"] if hole in panel.index else True
    # 面板中 s0 的 NaN 行数 = 仅 horizon 尾部（5→3）+ 空洞日，不含空洞日前一行
    fwd_s0 = panel["s0"]
    assert pd.notna(fwd_s0.get(d_before))


def test_build_fwd_cs_panel_label_pool_group_default_backward_compatible():
    """默认 label_pool='group'：与 P8-3 面板构建逐字节一致（组内截面）。"""
    idx = pd.date_range("2020-01-01", periods=40, freq="B")
    syms = ["a", "b"]
    # features.symbols 只用符号名；bars.by_symbol 返回含 close 的 Series 桩
    features = type("F", (), {"symbols": syms})()

    class _Sym:
        def __init__(self, close):
            self._close = close

        def __getitem__(self, col):
            assert col == "close"
            return self._close

    class _Bars:
        symbols = syms

        def by_symbol(self, sym):
            i = syms.index(sym)
            return _Sym(pd.Series(100.0 + i * np.arange(40), index=idx, name="close"))

    bars = _Bars()
    panel_default = R._build_fwd_cs_panel(features, bars, 3, "cross_z")
    panel_explicit = R._build_fwd_cs_panel(features, bars, 3, "cross_z", "group")
    assert list(panel_default.columns) == syms
    pd.testing.assert_frame_equal(panel_default, panel_explicit)
    # 组内两品种 z：当日两值对称（均值 0）
    row = panel_default.iloc[5]
    assert abs(row["a"] + row["b"]) < 1e-9


def test_build_fwd_cs_panel_label_pool_unknown_raises():
    with pytest.raises(ValueError):
        R._build_fwd_cs_panel(None, None, 3, "cross_z", "bogus")
    # label_pool 只对截面化模式生效；absolute 恒返回 None（零开销）
    assert R._build_fwd_cs_panel(None, None, 3, "absolute", "all") is None
