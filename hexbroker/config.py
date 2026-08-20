"""配置管理（§8.1）。

唯一入口 :func:`load_config`：yaml 组合（OmegaConf）→ Pydantic 模型校验 → 冻结。
任何魔法数字都不许写在 .py 里，一律进 yaml（见 ``configs/``）。

约定：
- ``config_fingerprint()`` 返回配置摘要 sha1 前 12 位，用于 ``run_id`` 与 ``model_id``。
- Kronos 权重/分词器配对校验 :func:`validate_kronos_pairing`，错配抛 ``HexConfigError``。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

import omegaconf
from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ConfigDict, Field

from . import HexConfigError

# ---------------------------------------------------------------------------
# Kronos 权重 ↔ 分词器 配对表（§3.2）
# ---------------------------------------------------------------------------
KRONOS_PAIRS: dict[str, str] = {
    "NeoQuasar/Kronos-mini": "NeoQuasar/Kronos-Tokenizer-2k",
    "NeoQuasar/Kronos-small": "NeoQuasar/Kronos-Tokenizer-base",
    "NeoQuasar/Kronos-base": "NeoQuasar/Kronos-Tokenizer-base",
}
KRONOS_MAX_CONTEXT: dict[str, int] = {
    "NeoQuasar/Kronos-mini": 2048,
    "NeoQuasar/Kronos-small": 512,
    "NeoQuasar/Kronos-base": 512,
}


def validate_kronos_pairing(model_name: str, tokenizer_name: str) -> None:
    """校验 Kronos 模型与分词器必须配对，否则抛 ``HexConfigError``。"""
    expected = KRONOS_PAIRS.get(model_name)
    if expected is None:
        raise HexConfigError(
            f"未知 Kronos 模型名 '{model_name}'，可选：{list(KRONOS_PAIRS)}"
        )
    if tokenizer_name != expected:
        raise HexConfigError(
            f"Kronos 配对错误：模型 '{model_name}' 必须搭配分词器 '{expected}'，"
            f"但配置给的是 '{tokenizer_name}'"
        )


# ---------------------------------------------------------------------------
# Pydantic 配置模型（冻结 + 允许额外字段）
# ---------------------------------------------------------------------------
# 配置模型：允许额外字段；可赋值（实验 override 与 --demo 需要），但每次赋值都做校验。
_MODEL_CFG = ConfigDict(extra="allow", validate_assignment=True)


class DataConfig(BaseModel):
    model_config = _MODEL_CFG
    symbols: list[str] = Field(default_factory=lambda: ["SHFE.cu"])
    freq: str = "1d"
    start: str = "2018-01-01"
    end: str = "2024-12-31"
    source: str = "csv"
    main_rule: str = "open_interest"
    adjust_method: str = "backward"
    train_len: int = 250
    test_len: int = 60
    purge: int = 5
    embargo: int = 2
    mode: str = "rolling"


class FeatureConfig(BaseModel):
    model_config = _MODEL_CFG
    transformers: list[str] = Field(
        default_factory=lambda: ["technical", "microstructure", "normalize"]
    )
    normalize_window: int = 120
    technical_params: dict[str, Any] = Field(default_factory=dict)
    iterative_params: dict[str, Any] = Field(default_factory=dict)
    cross_params: dict[str, Any] = Field(default_factory=dict)
    weekly_params: dict[str, Any] = Field(default_factory=dict)
    fundamental_params: dict[str, Any] = Field(
        default_factory=dict, description="基本面（基差）特征参数：window/min_periods/include_basis"
    )
    keep_features: list[str] | None = Field(default=None, description="特征级白名单（特征选择裁剪）；None=保留全部")


class ForecastConfig(BaseModel):
    model_config = _MODEL_CFG
    name: str = "ar_transformer"
    horizon: int = 5
    n_mc_samples: int = 30
    temperature: float = 1.0
    top_p: float = 0.9
    max_context: int = 512
    fallback: bool = True
    model_name: str = "NeoQuasar/Kronos-small"
    tokenizer_name: str = "NeoQuasar/Kronos-Tokenizer-base"
    effective_threshold: float = 0.05
    calibration_method: str = "platt"
    hidden_size: int = 32
    n_layers: int = 2
    n_heads: int = 4
    epochs: int = 20
    lr: float = 1e-3


class RiskConfig(BaseModel):
    model_config = _MODEL_CFG
    atr_mult_high: float = 2.5
    atr_mult_mid: float = 2.0
    atr_mult_low: float = 1.5
    atr_window: int = 20
    vol_high_q: float = 0.8
    vol_low_q: float = 0.2
    sell_s1_window: int = 20
    sell_s3_target: float = 0.10
    sell_s4_bars: int = 20
    sell_s5_z: float = 3.0
    vol_target: float = 0.20
    max_position_pct: float = 0.30
    kelly_cap: float = 0.25
    recovery_drawdown_r1: float = 0.05
    recovery_drawdown_r2: float = 0.10
    recovery_drawdown_r3: float = 0.15
    position_scalar_r1: float = 0.5
    position_scalar_r2: float = 0.0
    position_scalar_r3: float = 0.2
    position_scalar_r4: float = 1.0


class EngineAConfig(BaseModel):
    """引擎 A（按日截面 rank）生产配置（v1.1，P9 固化）。

    引擎 A 对信号缓存 exp_ret 做**每日截面** rank（``groupby(ts).rank(pct=True)``），
    取 top_k 分位做多（P5 修复版，替代旧版全表 rank）；稀疏日防御采用 S2：
    当日信号品种数不足 ``min_symbols`` 时整日空仓（P5 终裁选中策略）。

    信号缓存默认指向 **v8 生产缓存**（P8-4 全品种统一截面 QA VERIFIED，
    8,624 行 / 18 品种）；旧 v2/v4 缓存保留作回归基线，显式传入路径仍可覆盖。

    P9-2 新增 ``group_cap``：单组敞口上限（0,1)，None 表示不启用（默认，向后兼容）。
    """

    model_config = _MODEL_CFG
    enabled: bool = True
    signal_cache: str = "artifacts/signals_cache18_grouped_v8.parquet"  # 生产缓存（v8，P8-4 全品种截面）
    top_k: float = 0.30       # 每日截面做多分位阈值（top30%）
    min_symbols: int = 3      # S2 稀疏防御：当日品种数不足则整日空仓
    notional_frac: float = 0.20  # 名义占权益比例（与引擎 B 一致）
    group_cap: float | None = None  # P9-2 单组敞口上限（0,1)；None=不启用（向后兼容）


class EngineBConfig(BaseModel):
    """Sentinel-2 引擎 B（基差收敛）生产配置（v1.0，P5 终裁）。

    引擎 B 为 Sentinel-2 组合的基座引擎（OOS Sharpe 1.260 / MaxDD -6.7%）：
    对 basis_ratio 做品种内滚动 252 日分位，分位 >= thr 时做多。
    """

    model_config = _MODEL_CFG
    enabled: bool = True
    win: int = 252          # basis_ratio 品种内滚动分位窗口
    thr: float = 0.70       # 做多分位阈值
    notional_frac: float = 0.20  # 名义占权益比例


class ComboConfig(BaseModel):
    """Sentinel-2 双引擎组合权重（v1.1，P9 组合级验证固化）。

    P9-1 网格（A∈{0.10..0.35} × 波目标 on/off，复利口径）：
      - vol=N 网格 OOS Sharpe 最优：A10/B90（1.612 vs A15/B85 基线 1.602）
      - 全部网格 OOS Sharpe 最优：A10/B90 + vol=Y（1.662，复利 +43.8%）
      - P9 裁决：权重更新为 A10/B90（引擎 A OOS 截面 IC 为负，降低其权重更稳健）；
        波目标保持 False（P4-1 终裁；vol=Y 提升 OOS 复利但 MaxDD 更深 -8.1% vs -6.1%，
        是否切换交团队/QA 复核）。
    """

    model_config = _MODEL_CFG
    w_engine_a: float = 0.10   # P9 终裁：A10/B90（vol=N 网格最优）
    w_engine_b: float = 0.90   # B 为基（OOS Sharpe 1.622）
    vol_target: bool = False   # P4-1 终裁：不叠加组合层波目标（P9 复核保留）
    vol_target_ann: float = 0.175
    vol_ewma_halflife: int = 10


class BacktestConfig(BaseModel):
    model_config = _MODEL_CFG
    fee_rate_open: float = 0.00005
    fee_rate_close: float = 0.00005
    fee_rate_close_today: float = 0.00010
    slippage_ticks: float = 1.0
    margin_rate: float = 0.12
    multiplier: float = 10.0
    min_tick: float = 10.0
    contracts: dict[str, dict] | None = Field(
        default=None,
        description="品种级合约参数：{symbol: {multiplier, min_tick}}（au=×1000/0.02、ag=×15/0.01、m=×10/1）",
    )
    limit_trade_allowed: bool = False
    initial_capital: float = 1_000_000.0
    # 新增：Sentinel-2 双引擎生产配置（v1.0，P5/P6-5 终裁）
    engine_b: EngineBConfig = Field(default_factory=EngineBConfig)  # 引擎 B（基差收敛）
    combo: ComboConfig = Field(default_factory=ComboConfig)         # 组合权重
    engine_a: EngineAConfig = Field(default_factory=EngineAConfig)  # 引擎 A（截面 rank，v4 缓存）


class RLConfig(BaseModel):
    model_config = _MODEL_CFG
    algo: str = "PPO"
    use_sb3: bool = False  # CPU 沙箱默认 false，使用内置纯 numpy actor-critic
    action_space: str = "discrete5"
    obs_window: int = 30
    total_timesteps: int = 50_000
    w_pnl: float = 1.0
    w_cost: float = 1.0
    w_dd: float = 2.0
    w_turn: float = 0.05
    w_align: float = 0.1
    align_anneal_steps: int = 20_000
    learning_rate: float = 3e-3
    gamma: float = 0.99
    clip_range: float = 0.2
    hidden_size: int = 32
    n_hidden: int = 2


class EvolutionConfig(BaseModel):
    model_config = _MODEL_CFG
    sampler: str = "TPE"
    pruner: str = "Hyperband"
    n_trials: int = 20
    storage: str = "sqlite:///artifacts/optuna/study.db"
    examm_n_islands: int = 4
    examm_n_generations: int = 6
    examm_max_params: int = 1_000_000
    drift_psi_threshold: float = 0.2


class HexConfig(BaseModel):
    model_config = _MODEL_CFG
    seed: int = 42
    experiment: str = "demo"
    joint_training: bool = False
    data: DataConfig = Field(default_factory=DataConfig)
    feature: FeatureConfig = Field(default_factory=FeatureConfig)
    forecast: ForecastConfig = Field(default_factory=ForecastConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    rl: RLConfig = Field(default_factory=RLConfig)
    evolution: EvolutionConfig = Field(default_factory=EvolutionConfig)


# ---------------------------------------------------------------------------
# 加载逻辑
# ---------------------------------------------------------------------------
def _merge_with_defaults(cfg: DictConfig) -> DictConfig:
    """把用户 yaml 合并到代码内默认配置之上（yaml 优先）。

    注意：OmegaConf.structured 不支持 pydantic 模型，故用 ``model_dump`` 生成默认 dict。
    """
    default_dict = HexConfig().model_dump()
    default = OmegaConf.create(default_dict)
    return OmegaConf.merge(default, cfg)


def load_config(path: Optional[str | Path] = None, **overrides: Any) -> HexConfig:
    """加载配置。

    参数
    ----
    path : yaml 实验配置文件路径（相对/绝对均可）。
    overrides : 顶层键覆盖，如 ``load_config(seed=7)``。

    返回
    ----
    冻结的 :class:`HexConfig` 实例。
    """
    if path is None:
        cfg = OmegaConf.create({})
    else:
        cfg = OmegaConf.load(str(path))
    cfg = _merge_with_defaults(cfg)

    # 应用 override
    if overrides:
        override_cfg = OmegaConf.create(overrides)
        cfg = OmegaConf.merge(cfg, override_cfg)

    # Kronos 配对校验（仅当启用 kronos 时）
    forecast = cfg.get("forecast", {})
    name = forecast.get("name", "ar_transformer")
    if name in ("kronos", "kronos_small", "kronos_mini"):
        validate_kronos_pairing(
            forecast.get("model_name", "NeoQuasar/Kronos-small"),
            forecast.get("tokenizer_name", "NeoQuasar/Kronos-Tokenizer-base"),
        )

    data = OmegaConf.to_container(cfg, resolve=True, throw_on_missing=False)
    return HexConfig(**data)


def config_fingerprint(cfg: HexConfig) -> str:
    """配置指纹：sha1(配置 JSON) 前 12 位（用于 run_id / model_id）。"""
    raw = json.dumps(cfg.model_dump(), sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def default_demo_config() -> HexConfig:
    """内置默认（合成数据 + ARTransformer fallback），供 ``--demo`` 端到端使用。"""
    return load_config()
