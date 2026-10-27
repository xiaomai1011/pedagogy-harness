"""测试专用配置：单测一律用**临时库**，绝不往真实的 `pedagogy.sqlite` 写数据。

为什么需要：`app/db.py` 在 import 时就从环境变量 `PH_DB` 读路径。测试若共用一个
持久库，跨次运行留下的历史行会污染"连败次数 / 已解锁等级 / 提交序号"这类**有状态**断言
（本轮就撞上过：`hint_level` 断言被上一轮残留的行顶成了 2）。

pytest 会**先导入 conftest.py**，再导入测试模块，所以这里设的环境变量一定能生效。
"""

import os
import tempfile
from pathlib import Path

import pytest

_TEST_DB = Path(tempfile.gettempdir()) / "pedagogy_harness_test.sqlite"
_TEST_DB.unlink(missing_ok=True)          # 每次跑测试都从空库开始
os.environ["PH_DB"] = str(_TEST_DB)

from app.harness import isolation  # noqa: E402  —— 必须在 PH_DB 设好之后导入


class _PipelineTestAdapter:
    """**只跑在测试进程里的**执行适配器：把 payload 交回 `pipeline.judge`。

    为什么需要：`app/harness/pipeline.py` 现在只在执行适配器就绪时才判题
    （隔离未接入 ⇒ fail closed）。教学管线本身的单测要验证**判题之后**的流程
    （分类、提示阶梯、防泄、事件落库），所以这里提供一个诚实的"替身"：
    它**不隔离**、也不假装隔离，只把调用转给 `pipeline.judge`
    （因此 `monkeypatch.setattr(pipeline_mod, "judge", ...)` 依旧生效）。

    生产进程**不会**有这层替身：`app/main.py` 里没有任何注册调用，
    `registered_execution_adapter()` 返回 `None` ⇒ 一律拒绝执行。
    `tests/test_isolation_gate.py` 显式清空登记，专门验证这条 fail-closed 路径。
    """

    def check_isolation(self) -> bool:
        return True

    def start(self, payload):
        from app.harness import pipeline as pipeline_mod

        return pipeline_mod.judge(payload["question"], payload["code"],
                                  payload.get("language") or "python")


@pytest.fixture(autouse=True)
def _test_only_execution_adapter():
    """给所有单测装上替身适配器；退出时撤销，避免跨用例泄漏。"""
    isolation.register_execution_adapter(_PipelineTestAdapter())
    try:
        yield
    finally:
        isolation.clear_execution_adapter()
