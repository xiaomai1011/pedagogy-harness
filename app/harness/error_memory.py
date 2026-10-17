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

    「未分类」不记账：否则记忆里会堆一串无意义的「未分类/」，
    既污染统计，也让预防性提示变成一句废话。
    """
    if getattr(classification, "error_class", "未分类") == "未分类":
        return
    db.bump_error_memory(
        participant_id,
        classification.error_class,
        getattr(classification, "knowledge_point", "") or "",
        ts,
    )


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

    name = "error_memory"
    order = 20

    def run(self, ctx) -> None:
        classification = _safe_classification(ctx.classification)
        # 只有**正式提交**入口才把这次判题记成一次错误（D 定案，2026-10-17）。
        # 纯提示请求（`/api/hint`）复用同一份代码与同一个判题结果，
        # 若也记账，"同类错误是否减少"的结论就会被点击次数污染（实测 1→4）。
        if ctx.is_formal_submission and ctx.judge_result.status != "pass":
            remember(ctx.participant_id, classification, ctx.ts)
        ctx.memory_hint = preventive_hint(ctx.participant_id)
