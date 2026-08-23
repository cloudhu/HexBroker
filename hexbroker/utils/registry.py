"""组件注册表（§1.3 Pipeline + Registry）。

配置驱动的实例化：组件用 ``@register("name")`` 注册，配置里按名字实例化。
确保换模型/换特征只改 yaml，不改动代码。
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from .. import HexConfigError

T = TypeVar("T")

_REGISTRY: dict[str, Callable[..., Any]] = {}


def register(name: str) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """装饰器：把可调用对象注册到全局注册表 ``name`` 下。"""

    def _decorator(fn: Callable[..., T]) -> Callable[..., T]:
        if name in _REGISTRY:
            raise HexConfigError(f"注册表冲突：'{name}' 已被占用")
        _REGISTRY[name] = fn
        fn.__registry_name__ = name  # type: ignore[attr-defined]
        return fn

    return _decorator


def build(name: str, *args: Any, **kwargs: Any) -> Any:
    """按名字实例化组件。"""
    if name not in _REGISTRY:
        raise HexConfigError(
            f"未注册的组件 '{name}'。可用：{sorted(_REGISTRY)}"
        )
    return _REGISTRY[name](*args, **kwargs)


def list_registered() -> list[str]:
    """列出所有已注册组件名。"""
    return sorted(_REGISTRY)


def get_registered(name: str) -> Callable[..., Any]:
    """返回注册的可调用对象（不实例化）。"""
    if name not in _REGISTRY:
        raise HexConfigError(f"未注册的组件 '{name}'")
    return _REGISTRY[name]
