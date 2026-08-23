"""Walk-forward 切分器（§1.1 D2 / §2.2 L1）。

强制 **purge + embargo**，杜绝训练集统计量泄漏到测试集。

核心不变量（防泄漏红线）：
    train.max_position + purge  <  test.min_position

即任一 fold 的训练窗结束位置 + purge 间隔 + embargo 间隔 严格小于测试窗起始位置。
``assert_no_leakage`` 在发现任何重叠/越界时抛 ``HexLeakageError``（必须终止运行）。
"""

from __future__ import annotations

from dataclasses import dataclass


from .. import HexLeakageError


@dataclass
class Fold:
    """单个 walk-forward 折。位置索引为半开区间 [start, end)。"""

    train_start: int
    train_end: int  # exclusive
    test_start: int
    test_end: int  # exclusive

    @property
    def train_len(self) -> int:
        return self.train_end - self.train_start

    @property
    def test_len(self) -> int:
        return self.test_end - self.test_start

    @property
    def train_max_pos(self) -> int:
        return self.train_end - 1

    @property
    def test_min_pos(self) -> int:
        return self.test_start

    def train_slice(self) -> slice:
        return slice(self.train_start, self.train_end)

    def test_slice(self) -> slice:
        return slice(self.test_start, self.test_end)


class WalkForwardSplitter:
    """walk-forward 滚动/扩展切分器。"""

    def __init__(
        self,
        train_len: int = 250,
        test_len: int = 60,
        purge: int = 5,
        embargo: int = 2,
        mode: str = "rolling",
    ) -> None:
        if train_len <= 0 or test_len <= 0:
            raise ValueError("train_len/test_len 必须为正")
        if purge < 0 or embargo < 0:
            raise ValueError("purge/embargo 必须 >= 0")
        self.train_len = train_len
        self.test_len = test_len
        self.purge = purge
        self.embargo = embargo
        self.mode = mode

    def split(self, index: object) -> list[Fold]:
        """对索引（DatetimeIndex 或任意等长序列）切分，返回 fold 列表。

        rolling 模式：每折训练窗平移 ``test_len``；
        expanding 模式：训练窗起点固定为 0，长度逐折增长。

        **幂等性（L6 修复）**：本方法不修改任何实例属性（``self.train_len``
        等），重复调用返回完全一致的结果。旧实现会在 expanding 模式下递增
        ``self.train_len``，导致同一实例多次调用结果漂移，且在跨品种复用
        （``refine_lightgbm_champion`` 等逐 symbol 调 ``split``）时污染后续
        品种的训练窗。改用局部 ``cur_train_len`` 后该问题消除。
        """
        n = len(index)
        folds: list[Fold] = []
        start = 0
        cur_train_len = self.train_len
        while True:
            train_s = 0 if self.mode == "expanding" else start
            train_e = train_s + cur_train_len
            if train_e + self.purge + self.embargo + self.test_len > n:
                break
            test_s = train_e + self.purge + self.embargo
            test_e = test_s + self.test_len
            folds.append(Fold(train_s, train_e, test_s, test_e))
            start += self.test_len
            if self.mode == "expanding":
                # expanding 时训练窗随 start 同步增长以推进 test（仅改局部变量）
                cur_train_len += self.test_len
        return folds

    def assert_no_leakage(self, folds: list[Fold]) -> None:
        """校验所有 fold 满足防泄漏不变量；否则抛 ``HexLeakageError``。

        不变量（防泄漏红线，L6 修复：纳入 ``embargo``）：

            train.max_position + purge + embargo  <=  test.min_position

        即任一 fold 的测试窗起点必须严格晚于训练窗末端 + purge + embargo。
        """
        for i, f in enumerate(folds):
            if f.test_min_pos < f.train_max_pos + self.purge + self.embargo:
                raise HexLeakageError(
                    f"Fold#{i} 检测到泄漏：train.max_pos={f.train_max_pos} + "
                    f"purge={self.purge} + embargo={self.embargo} 未小于 "
                    f"test.min_pos={f.test_min_pos}"
                )
            if f.test_min_pos < 0 or f.test_end > 1_000_000:  # 基本越界守卫
                raise HexLeakageError(f"Fold#{i} 索引越界")
            if f.train_len < 1 or f.test_len < 1:
                raise HexLeakageError(f"Fold#{i} 空窗")

    @staticmethod
    def has_leakage(fold: Fold, purge: int) -> bool:
        """静态判定单个 fold 是否泄漏（供单测构造泄漏场景）。"""
        return fold.test_min_pos < fold.train_max_pos + purge


def assert_no_leakage(folds: list[Fold], purge: int = 0, embargo: int = 0) -> None:
    """模块级便捷函数：任一 fold 泄漏即抛 ``HexLeakageError``。

    L6 修复：原先忽略调用方传入的 embargo（硬编码默认 2），现显式透传，
    使模块级校验与实例级 ``WalkForwardSplitter.assert_no_leakage`` 口径一致。
    """
    WalkForwardSplitter(purge=purge, embargo=embargo).assert_no_leakage(folds)
