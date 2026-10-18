"""学习者错误记忆（创新点 3）。

要点（契约 §4）：按 participant_id **跨对话持久化**，聚合错误类型分布，
在后续（含下次登录）给出**预防性提示**。

- 每次分类命中后调用 `db.bump_error_memory` 累计（命中即 count+1）
- `preventive_hint()` 聚合 `db.get_error_memory`，产出"你此前多次在 X 上出错"
- 读的是 `error_memory` 表，**跨会话累计**：换一道题、隔天再登录，记忆都还在
"""

from app import db
from app.harness.base import TeachingComponent
from app.harness.error_classifier import Classification

# 只报"反复出现"的错误：出现 1 次属偶发，称不上"倾向"，
# 报了反而像在给学生贴标签。阈值与条数都写死，避免各次行为不一致。
MIN_COUNT = 2
MAX_ITEMS = 2

# 组件名（与注册表、配置开关一致；第 2 步也要按它判断"记忆是否启用"）
NAME = "error_memory"


def _safe_classification(classification) -> Classification:
    """分类器被开关关闭时 ctx.classification 会是 None。

    本组件不依赖分类器也能独立工作（消融组 abl_mem 就是这种情形），
    此时按"未分类"记账，避免把 None 传进 db 层。
    """
    if classification is not None:
        return classification
    return Classification(error_class="未分类", knowledge_point="", source="none")


def remember_conn(conn, participant_id: str, classification, ts: str) -> None:
    """**用调用方的连接**记账 —— 与分类回填、进度标记放进**同一个短事务**（契约 §6.3 第 2 步）。

    「未分类」不记账：否则记忆里会堆一串无意义的「未分类/」，既污染统计，
    也让预防性提示变成一句废话。
    """
    classification = _safe_classification(classification)
    if getattr(classification, "error_class", "未分类") == "未分类":
        return
    db.upsert_error_memory_conn(conn, participant_id, classification.error_class,
                               getattr(classification, "knowledge_point", "") or "", ts)


def remember(participant_id: str, classification, ts: str) -> None:
    """把一次错误计入记忆表（自管连接的便利包装；主流程请走 `remember_conn`）。"""
    with db.connect() as conn:
        remember_conn(conn, participant_id, classification, ts)


def preventive_hint(participant_id: str) -> str:
    """读取历史错误分布，产出预防性提示。历史不足（无 count>=2 的项）时返回空串。"""
    rows = [r for r in db.get_error_memory(participant_id) if int(r.get("count", 0)) >= MIN_COUNT]
    if not rows:
        return ""
    items = []
    for row in rows[:MAX_ITEMS]:
        knowledge_point = row.get("knowledge_point") or ""
        where = f"（知识点：{knowledge_point}）" if knowledge_point else ""
        items.append(f"{row['error_class']}类错误 {row['count']} 次{where}")
    return "提醒一下：你此前反复出错的地方有 —— " + "、".join(items) + "。动笔前先留意。"


class ErrorMemoryComponent(TeachingComponent):
    """把 remember() / preventive_hint() 包成可插拔组件。"""

    name = NAME
    order = 20

    def run(self, ctx) -> None:
        # **不在这里记账**：错误记忆的计数已上移到**第 2 步**（数据记录层，契约 §6.3），
        # 本组件只负责**读取**历史并产出预防性提示 —— 否则会与第 2 步重复计数。
        ctx.memory_hint = preventive_hint(ctx.participant_id)
