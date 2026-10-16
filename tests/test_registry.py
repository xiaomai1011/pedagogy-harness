"""验证"组件可插拔"是真的：新增组件不需要改主干。

关键断言：本文件里定义的组件名，**在 pipeline.py 里搜索不到**——
但注册进注册表后，主干照样会跑它。这就是"主干不认识具体组件"的证明。
"""

from app.harness import registry
from app.harness.base import SubmissionContext, TeachingComponent
from app.harness.config_switch import HarnessConfig
from app.harness.pipeline import handle_submission


class SpyComponent(TeachingComponent):
    """一个只用于验证的外来组件（模拟"第三方接入的组件"）。"""

    name = "spy_component"
    order = 25

    def run(self, ctx: SubmissionContext) -> None:
        ctx.message = "spy 组件写入的反馈"
        ctx.source = "harness"


def test_pipeline_source_does_not_know_this_component():
    """主干代码里不应该出现这个组件的名字。"""
    from pathlib import Path

    pipeline_src = (
        Path(__file__).resolve().parent.parent / "app" / "harness" / "pipeline.py"
    ).read_text(encoding="utf-8")
    assert "spy_component" not in pipeline_src
    assert "SpyComponent" not in pipeline_src


def test_registry_order_is_by_order_field():
    assert registry.names() == [
        "tutor_persona", "error_classifier", "error_memory", "hint_ladder"
    ]


def test_new_component_works_without_touching_pipeline():
    """注册一个主干完全不认识的组件，配置启用它，主干照样会调用。"""
    registry.register(SpyComponent())
    try:
        config = HarnessConfig(group_id="test", enabled=("spy_component",))
        out = handle_submission(
            participant_id="TEST_SPY", question_id="P001",
            code="print(1)", config=config,
        )
        assert out["components"]["executed"] == ["spy_component"]
        assert out["message"] == "spy 组件写入的反馈"
        # 内置组件本次都被跳过
        assert set(out["components"]["skipped"]) == {
            "tutor_persona", "error_classifier", "error_memory", "hint_ladder"
        }
    finally:
        # 复原：清掉测试组件，重新注册内置组件，避免污染后续测试
        registry.clear()
        from app.harness.builtin import register_builtin_components

        register_builtin_components()
