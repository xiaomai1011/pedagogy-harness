"""组件注册表：主干唯一认识的东西。

主干不 import 任何具体组件，只 import 本模块，按 `ordered()` 拿组件列表。
"""

from app.harness.base import TeachingComponent

_REGISTRY: dict[str, TeachingComponent] = {}


def register(component: TeachingComponent) -> TeachingComponent:
    """注册一个组件实例。

    同名会被覆盖（便于测试时临时替换组件，也便于将来做"热插拔"演示）。
    """
    if not getattr(component, "name", ""):
        raise ValueError("组件必须声明非空的 name")
    _REGISTRY[component.name] = component
    return component


def ordered() -> list[TeachingComponent]:
    """按 order 升序返回全部已注册组件。"""
    return sorted(_REGISTRY.values(), key=lambda c: c.order)


def names() -> list[str]:
    """已注册组件的名字列表（便于写文档 / 做演示）。"""
    return [c.name for c in ordered()]


def get(name: str) -> TeachingComponent | None:
    return _REGISTRY.get(name)


def clear() -> None:
    """清空注册表。仅测试用。"""
    _REGISTRY.clear()
