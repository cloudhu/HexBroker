"""模型版本注册表（P1-7，§3）。

每轮重训产出带 ``FourLayerFingerprint`` 的 artifact，支持 list/load/rollback。
默认内存实现（sidecar-free），可扩展为磁盘/对象存储回溯。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..utils.fingerprint import FourLayerFingerprint


@dataclass
class _Entry:
    """单个模型版本的注册条目。"""

    model: Any
    fingerprint: Optional[FourLayerFingerprint]


class ModelRegistry:
    """内存模型版本注册表（支持版本枚举 / 加载 / 回滚）。"""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._current: Optional[str] = None

    def register(
        self, model: Any, fp: Optional[FourLayerFingerprint], version: str
    ) -> str:
        """注册一个模型版本（幂等：同 version 覆盖最新一次生效）。"""
        self._entries[version] = _Entry(model=model, fingerprint=fp)
        if self._current is None:
            self._current = version
        return version

    def list_versions(self) -> list[str]:
        """已注册版本（排序返回）。"""
        return sorted(self._entries.keys())

    def load(self, version: str) -> Any:
        """加载指定版本模型（版本不存在抛 ``KeyError``）。"""
        if version not in self._entries:
            raise KeyError(f"未知模型版本: {version}")
        return self._entries[version].model

    def rollback(self, version: str) -> Any:
        """设 ``current = version`` 并返回加载的模型（用于回滚到历史版本）。"""
        if version not in self._entries:
            raise KeyError(f"未知模型版本: {version}")
        self._current = version
        return self.load(version)

    @property
    def current(self) -> Optional[str]:
        """当前生效版本。"""
        return self._current

    def fingerprint(self, version: str) -> Optional[FourLayerFingerprint]:
        """查询某版本关联的四层指纹（不存在返回 None）。"""
        ent = self._entries.get(version)
        return ent.fingerprint if ent else None
