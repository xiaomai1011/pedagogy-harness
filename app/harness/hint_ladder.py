"""提示阶梯状态机（创新点 1）。

四级：L1 方向 → L2 思路骨架 → L3 伪代码 → L4 代码片段
（L4 只给局部代码片段，**绝不给完整可运行答案**）

**口径②-A（2026-10-17 定案）：「解锁」与「展示」是两个独立状态，且"请求"不升级。**
- `unlocked_level`（已解锁上限）：**允许**展示到哪一级。**只有连败能解锁**——
  连续失败累计到 2 的倍数就解锁一级（2 次 → L1，4 次 → L2，6 次 → L3，8 次及以上 → L4）。
- `hint_level`（本次展示）：**这次实际给了哪一级**（0 = 没展示）。**只有学生主动请求才展示**，
  展示等级 = 已解锁等级；尚未解锁时给 L1 打底。**请求本身不升级**——
  反复点"请求提示"始终停在同一级，想拿到更具体的提示**只能靠继续失败来解锁**。

被否决的口径：①"每次提交自动升一级"；②"连败 2 次即自动展示"（学生未求助就被动拿到提示）；
③"请求即升一级"（会让提示等级由**点击次数**驱动、与"卡住程度"脱钩，且连点 4 次即可拿到 L4）。

**等级 0＝"本次没有提示"**（未请求），**不是教学等级 L0**：正文只说明"怎么拿提示"，
**不复述判题反馈**——判题反馈已由左栏「程序运行结果」与独立的 `judge_feedback` 字段承载。

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


def next_unlock(*, prev_unlocked: int, consecutive_fail: int) -> int:
    """连败解锁等级：只解锁、不展示。

    连续失败累计到 2 的倍数就解锁一级——2 次 → L1，4 次 → L2，6 次 → L3，8 次及以上 → L4。
    取"历史解锁上限"与"本次连败推出的等级"的较大者，保证**单调不回退**。
    """
    by_fail = min(consecutive_fail // 2, MAX_LEVEL)
    return max(min(prev_unlocked, MAX_LEVEL), by_fail)


def next_state(*, prev_unlocked: int, consecutive_fail: int,
               requested: bool) -> tuple[int, int]:
    """算出本次的 (展示等级, 已解锁上限)。口径②-A 的核心状态机。

    - **未请求** → 展示等级恒为 `0`（哪怕已解锁到 L4），解锁上限只按连败推进；
    - **请求** → **只展示、不再升级**：展示等级 = 已解锁等级；
      还没攒够连败（解锁为 0）时给 L1 打底，保证学生点了就有回应。

    这里**不看"上次展示等级"**——这正是"反复请求不会把等级推高"的保证。
    """
    unlocked = next_unlock(prev_unlocked=prev_unlocked, consecutive_fail=consecutive_fail)
    if not requested:
        return 0, unlocked
    unlocked = max(unlocked, 1)      # 首次求助打底 L1
    return unlocked, unlocked


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

    来源取值：`llm` 一次通过校验 / `llm_retry` 重试后通过 / `fallback` 走兜底文本。

    **模型故障分两类**（契约 §6.3 硬规则 4）：

    - **可预期**（超时／连接／限流／服务端错误）→ **受控降级**为兜底文本，来源记 `fallback`；
    - **未预期**（代码缺陷、解析错误等）→ **原样抛出**，**不得**用兜底吞掉。
    """
    system = ctx.persona_system or DEFAULT_PERSONA
    try:
        text = llm_mod.llm(system=system, user=_hint_prompt(ctx)).strip()
    except Exception as exc:
        if not llm_mod.is_expected_failure(exc):
            raise
        return leak_guard.fallback(ctx.hint_level, ctx.classification), "fallback"

    violation = leak_guard.violates(text, ctx.hint_level, ctx.question)
    if violation is None:
        return text, "llm"

    # 重试一次：把违规原因明确告诉模型
    retry_user = _hint_prompt(ctx) + (
        f"\n\n注意：你上一次的回答因「{violation.rule} {violation.detail}」不合格被拒绝。"
        "请重写并严格遵守等级限制。"
    )
    try:
        text2 = llm_mod.llm(system=system, user=retry_user).strip()
    except Exception as exc:
        if not llm_mod.is_expected_failure(exc):
            raise
        return leak_guard.fallback(ctx.hint_level, ctx.classification), "fallback"
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
    text = ""
    try:
        text = llm_mod.llm(system=system, user=prompt).strip()
    except Exception as exc:
        if not llm_mod.is_expected_failure(exc):
            raise
        return "这道题通过了测试，做得不错。"
    # 肯定语里同样不允许出现代码块（按 L1 的严格口径校验）
    if not text or leak_guard.violates(text, 1, ctx.question) is not None:
        return "这道题通过了测试，做得不错。"
    return text


def render(ctx) -> tuple[str, str | None]:
    """产出**教学反馈正文**与**真实生成来源**（契约 §6.2.2 的 `hint_source`）。

    返回 `(正文, 来源)`；来源取值 `llm`／`llm_retry`／`fallback`；**未使用模型时为 `None`**。

    **判题反馈不再拼进正文**：它写入独立的 `ctx.judge_feedback` ——
    否则"程序运行结果"与"AI 学习提示"两栏会显示同一段错误报告（重复且误导）。
    """
    if ctx.judge_result.status == "pass":
        return _success_message(ctx), None

    ctx.judge_feedback = _fail_feedback(ctx)
    if ctx.hint_level <= 0:
        # 等级 0＝**本次没有提示**（未请求）：正文只说"怎么拿提示"，
        # **不复述判题反馈**（否则右栏会把左栏的报错整段抄一遍，且会被误读成 L0 提示）。
        # 已解锁到哪一级由前端头部一行展示（`unlocked_level` 字段）。
        if ctx.unlocked_level > 0:
            return "点击「请求提示」查看已解锁等级的提示；请求提示不会提升等级。", None
        return ("需要提示时点「请求提示」；尚未解锁时也会先给出 L1 基础提示，"
                "请求提示不会提升等级。"), None

    # 等级 > 0：正文**只有提示本身**，判题反馈走独立字段
    hint, source = _llm_hint(ctx)
    return hint, source


class HintLadderComponent(TeachingComponent):
    """提示阶梯。**必须最后跑**（要用到错误分类器与错误记忆的产出）。"""

    name = "hint_ladder"
    order = 30

    def run(self, ctx) -> None:
        # 口径②-A：先按连败算"解锁到哪一级"，再看"本次展示不展示"
        # 注意顺序：next_state 返回 (展示等级, 已解锁上限)
        ctx.hint_level, ctx.unlocked_level = next_state(
            prev_unlocked=ctx.last_unlocked,
            consecutive_fail=ctx.consecutive_fail,
            requested=ctx.request_hint,
        )
        ctx.message, ctx.hint_source = render(ctx)
        ctx.source = "harness"
