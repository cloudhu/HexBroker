"""特征层（§2.2 L2）：把原始 K 线转换为因果特征矩阵。

默认管线：``technical`` → ``microstructure`` → ``normalize``（见 ``pipeline.build_features``）。
所有算子严格因果——只使用截至当前 bar 的历史，绝不窥探未来。
"""

from .pipeline import FeatureFrame, build_features, FeaturePipeline

__all__ = ["FeatureFrame", "build_features", "FeaturePipeline"]
