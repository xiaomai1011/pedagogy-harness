"""错误分类器（创新点 2）。

输入：JudgeResult 的 error_type + stderr + 学生代码
输出：教学五类（语法 / 类型 / 边界 / 逻辑 / 环境）+ 映射到的知识点

实现策略（申报书原文）：**规则优先，规则未命中时才调用大模型兜底**。

=== 给岗位 1 的任务（Phase 2）===
- 把 RULE_TABLE 填成真正可用的规则表
- classify() 里：先逐条匹配规则；全部未命中才调用 app.llm.llm() 兜底

当前为桩：一律返回「未分类」。
"""

from dataclasses import dataclass

from app.harness.base import TeachingComponent

# 执行层 error_type → 教学五类 的规则骨架（岗位 1 待填充）
RULE_TABLE = [
    # (匹配条件说明, 教学类别)   —— 示例占位，勿直接使用
    # ("stderr 含 SyntaxError", "语法"),
]


@dataclass
class Classification:
    error_class: str        # 语法 / 类型 / 边界 / 逻辑 / 环境 / 未分类
    knowledge_point: str
    source: str             # rule / llm / none


def classify(judge_result, code: str, question: dict) -> Classification:
    """把判题结果归入教学五类并映射知识点。

    ===== 当前为桩实现（Phase 1）=====
    TODO(岗位 1, Phase 2)：实现"规则优先 + LLM 兜底"。
    """
    return Classification(error_class="未分类", knowledge_point="", source="none")


class ErrorClassifierComponent(TeachingComponent):
    """把 classify() 包成可插拔组件。"""

    name = "error_classifier"
    order = 10

    def run(self, ctx) -> None:
        ctx.classification = classify(ctx.judge_result, ctx.code, ctx.question)
