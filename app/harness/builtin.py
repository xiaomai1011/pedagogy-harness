"""内置教学组件的装配处。

**新增一个教学组件，只需要在这里多注册一行**，主干 `pipeline.py` 不用改。
（这里就是"可插拔"的插入点。）
"""

from app.harness.error_classifier import ErrorClassifierComponent
from app.harness.error_memory import ErrorMemoryComponent
from app.harness.hint_ladder import HintLadderComponent
from app.harness.registry import register


def register_builtin_components() -> None:
    """注册全部内置组件。执行顺序由各组件自己的 order 决定。"""
    for component in (
        ErrorClassifierComponent(),   # order = 10
        ErrorMemoryComponent(),       # order = 20
        HintLadderComponent(),        # order = 30
    ):
        register(component)


register_builtin_components()  # 导入本模块即完成注册
