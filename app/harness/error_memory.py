"""学习者错误记忆（创新点 3）。

要点（契约 §4）：按 participant_id **跨对话持久化**，聚合错误类型分布，
在后续（含下次登录）给出**预防性提示**。

=== 给岗位 1 的任务（Phase 3）===
- 每次分类命中后调用 db.bump_error_memory 累计
- preventive_hint() 聚合 db.get_error_memory，产出"你此前多次在 X 上出错"的提示

当前为桩：返回空串。
"""

from app import db
from app.harness.base import TeachingComponent
from app.harness.error_classifier import Classification


def _safe_classification(classification) -> Classification:
    """分类器被开关关闭时 ctx.classification 会是 None。

    本组件不依赖分类器也能独立工作（消融组 abl_mem 就是这种情形），
    此时按"未分类"记账，避免把 None 传进 db 层。
    """
    if classification is not None:
        return classification
    return Classification(error_class="未分类", knowledge_point="", source="none")


def remember(participant_id: str, classification, ts: str) -> None:
    """把一次错误计入记忆表。仅当开关 error_memory 开启时调用。

    ===== 当前为桩实现（Phase 1）=====
    TODO(岗位 1, Phase 3)：对接 db.bump_error_memory。
    """
    return None


def preventive_hint(participant_id: str) -> str:
    """读取历史错误分布，产出预防性提示。

    ===== 当前为桩实现（Phase 1）=====
    TODO(岗位 1, Phase 3)：聚合 db.get_error_memory 生成提示。
    """
    return ""


class ErrorMemoryComponent(TeachingComponent):
    """把 remember() / preventive_hint() 包成可插拔组件。"""

    name = "error_memory"
    order = 20

    def run(self, ctx) -> None:
        classification = _safe_classification(ctx.classification)
        if ctx.judge_result.status != "pass":
            remember(ctx.participant_id, classification, ctx.ts)
        ctx.memory_hint = preventive_hint(ctx.participant_id)
