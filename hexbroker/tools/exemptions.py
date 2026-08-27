"""白名单/豁免注册表（P0-2）。

静态分析无法同时数学保证「100% 检出 + 0 误报」，故对已知安全模式提供显式豁免：
任何检出均可 ``register(key, reason)`` 豁免（QA 审查豁免理由），满足
「注入样本 100% 检出 + 现有代码 0 个未豁免误报」的工程化折衷
（freqtrade 同款务实做法，见 05-p0-arch.md §1.3 验收口径折衷）。

豁免键格式：``{pattern}@{file}``（file 为相对 cwd 的正斜杠路径）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


class ExemptionRegistry:
    """豁免注册表。"""

    def __init__(self) -> None:
        self._exemptions: dict[str, str] = {}  # key -> reason

    def register(self, key: str, reason: str) -> None:
        """注册一条豁免（key 形如 ``pattern@file``）。"""
        self._exemptions[key] = reason

    def is_exempt(self, key: str) -> bool:
        """判断某检出（``pattern@file``）是否已被豁免。

        兼容路径差异：注册键的 file 段是检出 file 段的后缀（正斜杠/相对路径
        差异）时也视为豁免。
        """
        if key in self._exemptions:
            return True
        pattern, _, file = key.partition("@")
        for reg_key in self._exemptions:
            reg_pattern, _, reg_file = reg_key.partition("@")
            if pattern == reg_pattern and file.endswith(reg_file):
                return True
        return False

    def reasons(self) -> dict[str, str]:
        """返回全部豁免及其理由（供审计）。"""
        return dict(self._exemptions)

    def load_yaml(self, path: str) -> None:
        """从 yaml 加载额外豁免：``{pattern@file: reason}``。"""
        try:
            import yaml
        except ImportError as e:  # pragma: no cover - 环境缺 pyyaml
            raise RuntimeError("加载豁免 yaml 需要 pyyaml") from e
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        for key, reason in data.items():
            self.register(str(key), str(reason))

    @classmethod
    def default_exemptions(cls) -> "ExemptionRegistry":
        """内置已知安全模式白名单（豁免理由经设计评审）。

        覆盖三类已知安全模式（05-p0-arch.md §1.3）：
        - rolling/expanding z-score（normalize.py）；
        - 外盘 ``reindex→asof`` 时差安全对齐（global_ref.py / cross.py）；
        - 标准 span/alpha EMA（technical.py / baseline.py，仅用过去值）；
        以及其余经文档化的安全用法（前向收益仅作评估、obs 窗口环形缓冲）。
        """
        reg = cls()
        defaults = {
            # rolling/expanding z-score：严格因果，仅用 [t-window+1, t] 历史
            "rolling_center@hexbroker/feature/normalize.py": "rolling/expanding z-score 严格因果（零泄漏自检辅助）",
            "iloc_forward@hexbroker/feature/normalize.py": "rolling z-score 边界外推（仅用末值，无真实未来数据）",
            # 外盘 reindex→asof：时差安全对齐（L4 修复），shift(1) 后取 <=t 最近值
            "asof_without_reindex@hexbroker/feature/global_ref.py": "外盘 shift(1) 后 asof 对齐，仅用外盘 t-1 及以前收盘（L4）",
            "asof_without_reindex@hexbroker/feature/cross.py": "跨品种锁列（外盘组 asof 对齐，时差安全）",
            # 标准 span/alpha EMA（adjust=False）：pandas ewm 仅用过去值，严格因果
            "ewm@hexbroker/feature/technical.py": "MACD/EMA span 严格因果（仅用过去值，adjust=False）",
            "ewm@hexbroker/evaluation/baseline.py": "MACD 基线 EMA 严格因果（仅用过去值，adjust=False）",
            # 前向收益仅用于信号有效性评估，不进入训练（文档化）
            "shift_negative@hexbroker/pipeline.py": "fwd 收益仅用于信号有效性评估（_forward_returns），不进入训练",
            # obs 窗口环形缓冲：非数据未来函数（滚动的是窗口内容，不是价格序列）
            "np_roll@hexbroker/rl/futures_env.py": "obs 窗口环形缓冲（np.roll 移动窗口内容，非价格未来搬移）",
            # RL 奖励用下一 bar 已实现收益：奖励在动作之后计算，非决策特征
            "shift_negative@hexbroker/rl/sentinel_env.py": "RL 奖励用下一 bar 已实现收益（动作后结算，非决策特征）",
            # next_bar_execution 显式开关（默认关闭）：预计算下一 bar open 用于保守撮合口径
            "shift_negative@hexbroker/backtest/engine.py": "next_bar_execution 显式开关（默认关闭）预计算下一 bar open 作保守成交参考价",
        }
        for k, v in defaults.items():
            reg.register(k, v)
        return reg
