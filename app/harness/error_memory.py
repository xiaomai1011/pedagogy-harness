"""学习者错误记忆（创新点 3）。

要点（契约 §4）：按 participant_id **跨对话持久化**，聚合错误类型分布，
在后续（含下次登录）给出**预防性提示**。

=== 给岗位 1 的任务（Phase 3, 10/28—11/01）===
- 每次分类命中后调用 db.bump_error_memory 累计
- preventive_hint() 聚合 db.get_error_memory，产出"你此前多次在 X 上出错"的提示

当前为桩：返回空串。
"""

from app import db


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
