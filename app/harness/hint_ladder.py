"""提示阶梯状态机（创新点 1）。

四级：L1 方向 → L2 思路骨架 → L3 伪代码 → L4 代码片段
（L4 只给局部代码片段，**绝不给完整可运行答案**）

**升级规则（B 口径）**：只有学生**主动请求提示**、或**连续失败 2 次**时才升一级。
另一种"每次提交自动升一级"的口径已被否决——那会让学生在未求助时被动拿到提示。

**等级 0**：初次进入题目只给题目与判题反馈，不给任何教学提示。

提示文本由大模型生成，但生成后**必须过 `leak_guard` 程序化校验**；
校验不通过则先重试一次，仍不过则改用无模板兜底文本。
"""

from app import llm as llm_mod
from app.harness import leak_guard
from app.harness.base import TeachingComponent

MAX_LEVEL = 4

# 人格组件被开关关闭时使用的兜底提示词（保证不依赖人格组件也能跑）
DEFAULT_PERSONA = (
    "你是一位面向编程初学者的助教，风格中性、严谨。"
    "绝不给出完整可运行代码或最终答案；用中文回答，不使用表情符号。"
)

# 各等级允许给出的内容（写进提示词，也用于文档与答辩解释）
LEVEL_GUIDE = {
    1: "只给方向：这道题可能用到哪些函数或关键词，不要给出实现步骤。",
    2: "给整体思路骨架：先做什么、再做什么，用自然语言描述步骤，不要写代码。",
    3: "给步骤级伪代码：可以描述每一步该做什么，但不要写可直接运行的代码。",
    4: "只给局部代码片段：只针对学生当前卡住的那一小步，不得给出完整函数或完整程序。",
}


def next_level(current: int, *, requested: bool, consecutive_fail: int) -> int:
    """计算本次应给的提示等级。等级 0 = 不给教学提示。"""
    current = max(0, min(current, MAX_LEVEL))
    if requested or consecutive_fail >= 2:
        return min(current + 1, MAX_LEVEL)
    return current


def _fail_feedback(ctx) -> str:
    """判题反馈（客观陈述，不评价学生本人）。"""
    lines = [f"未通过测试（{ctx.judge_result.status}）；错误类型：{ctx.judge_result.error_type}"]
    stderr = (ctx.judge_result.stderr or "").strip()
    if stderr:
        lines.append("报错信息：\n" + "\n".join(stderr.split("\n")[:3]))
    return "\n".join(lines)


def _hint_prompt(ctx) -> str:
    q = ctx.question
    parts = [
        f"题目标题：{q.get('title', '')}",
        f"题面：{q.get('statement', '')}",
        f"学生当前代码：\n{ctx.code}",
        f"判题结果：未通过。错误类型：{ctx.judge_result.error_type}；"
        f"报错：{(ctx.judge_result.stderr or '').strip()[:300]}",
    ]
    cls = ctx.classification
    if cls is not None and getattr(cls, "error_class", "未分类") != "未分类":
        parts.append(f"错误分类：{cls.error_class}；可能缺失的知识点：{cls.knowledge_point}")
    if ctx.memory_hint:
        parts.append(f"该学生历史上的错误倾向：{ctx.memory_hint}")
    parts.append(f"本次只允许给出 L{ctx.hint_level} 级提示：{LEVEL_GUIDE.get(ctx.hint_level, '')}")
    parts.append("请直接给出提示正文，不要加'以下是'之类的开场话。")
    return "\n".join(parts)


def _llm_hint(ctx) -> tuple[str, str]:
    """生成提示文本。返回 (文本, 来源)。

    来源取值：`llm` 一次通过校验 / `llm_retry` 重试后通过 / `fallback` 校验失败改用兜底文本。
    """
    system = ctx.persona_system or DEFAULT_PERSONA
    text = llm_mod.llm(system=system, user=_hint_prompt(ctx)).strip()

    violation = leak_guard.violates(text, ctx.hint_level, ctx.question)
    if violation is None:
        return text, "llm"

    # 重试一次：把违规原因明确告诉模型
    retry_user = _hint_prompt(ctx) + (
        f"\n\n注意：你上一次的回答因「{violation.rule} {violation.detail}」不合格被拒绝。"
        "请重写并严格遵守等级限制。"
    )
    text2 = llm_mod.llm(system=system, user=retry_user).strip()
    if leak_guard.violates(text2, ctx.hint_level, ctx.question) is None:
        return text2, "llm_retry"

    # 兜底：宁可少给信息，也绝不泄答案
    return leak_guard.fallback(ctx.hint_level, ctx.classification), "fallback"


def _success_message(ctx) -> str:
    """通过时：一两句适度肯定，且必须带具体点（人格要求）。"""
    system = ctx.persona_system or DEFAULT_PERSONA
    prompt = (
        f"学生通过了这道题：{ctx.question.get('title', '')}。\n"
        f"他的代码：\n{ctx.code}\n\n"
        "请用一两句中文给他肯定，必须带上具体点"
        "（例如他正确判断了边界条件、用对了哪个操作），不要只说'做得好'。"
        "不要给出任何代码。"
    )
    text = llm_mod.llm(system=system, user=prompt).strip()
    # 肯定语里同样不允许出现代码块（按 L1 的严格口径校验）
    if not text or leak_guard.violates(text, 1, ctx.question) is not None:
        return "这道题通过了测试，做得不错。"
    return text


def render(ctx) -> str:
    """按等级产出最终呈现给学生的文本。"""
    if ctx.judge_result.status == "pass":
        return _success_message(ctx)

    if ctx.hint_level <= 0:
        # 等级 0：只给题目与判题反馈，不给教学提示
        return _fail_feedback(ctx) + "\n\n（需要提示时，可以点「请求提示」。）"

    hint, _source = _llm_hint(ctx)
    return _fail_feedback(ctx) + "\n\n" + hint


class HintLadderComponent(TeachingComponent):
    """提示阶梯。**必须最后跑**（要用到错误分类器与错误记忆的产出）。"""

    name = "hint_ladder"
    order = 30

    def run(self, ctx) -> None:
        ctx.hint_level = next_level(
            ctx.last_level,
            requested=ctx.request_hint,
            consecutive_fail=ctx.consecutive_fail,
        )
        ctx.message = render(ctx)
        ctx.source = "harness"
