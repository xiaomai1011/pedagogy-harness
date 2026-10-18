"""H（无围栏完整代码绕过）的正反两组离线测试。

口径来源：用户 2026-10-18 指示（m00611）＋ 契约 §5.3。

- **正向（必须拦）**：L1—L4 的无围栏完整答案、正文混代码、多个片段拼成的完整答案、
  两行短答案；**短不等于局部**——「不超过 8 行」只是长度约束。
- **反向（不得误杀）**：行内标识符／表达式、L1 的方向性表述、L2 的自然语言骨架、
  L3 的自然语言伪代码与编号步骤、L4 的局部片段。

全部离线：不联网、不执行模型输出、不调用真实判题器。
"""

import types

import pytest

from app import db
from app import llm as llm_mod
from app.harness import hint_ladder as hl
from app.harness import leak_guard
from app.harness import pipeline as pipeline_mod
from app.harness.config_switch import PRESETS

# 短参考解：短于 REF_MIN_LEN(20)，避免 R4 干扰对 R5／R6 的断言
QUESTION_SHORT_REF = {
    "id": "P900",
    "title": "两数之和（短参考解）",
    "statement": "读入两个整数，输出它们的和。",
    "reference_solution": "x = 1\n",
    "knowledge_points": ["标准输入输出", "变量与赋值"],
    "phase": "practice",
    "test_cases": [{"input": "1 2", "expected": "3"}],
}

UNFENCED_ANSWER = "a, b = map(int, input().split())\nprint(a + b)"


# ---------------------------------------------------------------- 正向：必须拦


@pytest.mark.parametrize("level", [1, 2, 3, 4])
def test_unfenced_complete_answer_is_violation_at_every_level(level):
    v = leak_guard.violates(UNFENCED_ANSWER, level, QUESTION_SHORT_REF)
    assert v is not None, f"无围栏完整答案在 L{level} 不得放行"


def test_two_line_short_answer_is_not_treated_as_local_snippet():
    """两行就是完整答案时，不得因「行数短」被判成局部片段。"""
    v = leak_guard.violates(UNFENCED_ANSWER, 4, QUESTION_SHORT_REF)
    assert v is not None and v.rule == "R5"


def test_single_line_complete_answer_is_violation():
    v = leak_guard.violates("print(int(input()) + int(input()))", 4, QUESTION_SHORT_REF)
    assert v is not None and v.rule == "R5"


def test_fenced_complete_answer_inside_line_budget_is_violation():
    """围栏内、8 行以内，但整段就是完整答案 —— 仍要拦。"""
    text = "```python\na, b = map(int, input().split())\nprint(a + b)\n```"
    v = leak_guard.violates(text, 4, QUESTION_SHORT_REF)
    assert v is not None and v.rule == "R5"


def test_prose_mixed_with_code_complete_answer_is_violation():
    text = "思路是这样：\na, b = map(int, input().split())\n然后把两数相加：\nprint(a + b)"
    v = leak_guard.violates(text, 4, QUESTION_SHORT_REF)
    assert v is not None and v.rule == "R5"


def test_multiple_fragments_assembling_complete_answer_is_violation():
    """每个片段都不超行数，但拼起来是完整答案 —— 必须拦。"""
    text = "先读入：\n```python\nn = int(input())\n```\n再输出：\n```python\nprint(n * 2)\n```"
    v = leak_guard.violates(text, 4, QUESTION_SHORT_REF)
    assert v is not None and v.rule == "R5"


def test_numbered_code_lines_assembling_complete_answer_is_violation():
    text = "1. a, b = map(int, input().split())\n2. print(a + b)"
    v = leak_guard.violates(text, 2, QUESTION_SHORT_REF)
    assert v is not None, "编号列表里的完整答案不得因序号前缀而放行"


def test_function_definition_with_body_is_violation():
    text = "def solve(n):\n    return n * 2"
    v = leak_guard.violates(text, 3, QUESTION_SHORT_REF)
    assert v is not None and v.rule == "R5"


def test_unfenced_answer_with_chinese_comments_is_violation():
    text = "a, b = map(int, input().split())  # 读入两个数\nprint(a + b)  # 输出结果"
    v = leak_guard.violates(text, 4, QUESTION_SHORT_REF)
    assert v is not None and v.rule == "R5"


def test_inline_complete_answer_is_violation():
    """把完整答案塞进行内代码，同样算完整答案。"""
    text = "可以这样写：`a, b = map(int, input().split()); print(a + b)`"
    v = leak_guard.violates(text, 1, QUESTION_SHORT_REF)
    assert v is not None


def test_l4_unfenced_long_code_is_violation():
    text = "\n".join(f"x{i} = {i}" for i in range(10))
    v = leak_guard.violates(text, 4, QUESTION_SHORT_REF)
    assert v is not None and v.rule == "R3"


@pytest.mark.parametrize("level", [1, 2])
def test_l1_l2_unfenced_code_fragment_is_violation(level):
    """L1／L2 的允许内容里没有代码语句，出现两行即越界。"""
    v = leak_guard.violates("a = 1\nb = 2", level, QUESTION_SHORT_REF)
    assert v is not None and v.rule == "R6"


def test_single_line_pure_call_chain_is_violation():
    """`print(input())` 是一行完整程序：无赋值、无中文，也要按代码语句识别。"""
    v = leak_guard.violates("print(input())", 4, QUESTION_SHORT_REF)
    assert v is not None and v.rule == "R5"


def test_inline_fragments_across_two_blocks_assembling_answer_is_violation():
    """两个行内块各一句、拼起来是完整答案 —— 合并后判完整答案。"""
    text = "先 `n = int(input())`，再 `print(n)`。"
    v = leak_guard.violates(text, 3, QUESTION_SHORT_REF)
    assert v is not None and v.rule == "R5"


def test_inline_mention_of_output_function_without_argument_is_allowed():
    """`print()` 只是提到函数名，不算输出语句 —— 不得因出现 `print` 就拦。"""
    text = "注意 `print()` 和 `input()` 是两个不同的动作，先想清楚顺序。"
    assert leak_guard.violates(text, 1, QUESTION_SHORT_REF) is None


# ---------------------------------------------------------------- 反向：不得误杀


def test_l1_inline_identifier_is_allowed():
    assert leak_guard.violates("可以试试用 `len()` 取长度。", 1, QUESTION_SHORT_REF) is None


def test_l1_direction_with_inline_io_calls_is_allowed():
    text = "可以用 `input().split()` 一次读入两个数，再用 `print()` 输出结果。"
    assert leak_guard.violates(text, 1, QUESTION_SHORT_REF) is None


def test_l1_inline_expression_mentions_are_allowed():
    text = "试试 `len(s)` 取长度，或者用 `range(n)` 遍历。"
    assert leak_guard.violates(text, 1, QUESTION_SHORT_REF) is None


def test_l2_natural_language_skeleton_is_allowed():
    text = "先读入两个数，再计算它们的和，最后输出结果。"
    assert leak_guard.violates(text, 2, QUESTION_SHORT_REF) is None


def test_prose_line_starting_with_assignment_like_text_is_not_code():
    """「n = 0 时要单独判断」是解释性文字，不是代码语句。"""
    text = "n = 0 时要单独判断，n < 0 时要取绝对值。"
    assert leak_guard.violates(text, 2, QUESTION_SHORT_REF) is None


def test_l3_numbered_natural_language_steps_are_allowed():
    text = "1. 读入两个整数\n2. 把它们相加\n3. 输出结果"
    assert leak_guard.violates(text, 3, QUESTION_SHORT_REF) is None


def test_l3_chinese_pseudocode_is_allowed():
    text = "读入 a 和 b\n结果 = a 加 b\n输出结果"
    assert leak_guard.violates(text, 3, QUESTION_SHORT_REF) is None


def test_l3_python_style_pseudocode_with_chinese_steps_is_allowed():
    text = "if n 大于 0:\n    累加 n\n输出结果"
    assert leak_guard.violates(text, 3, QUESTION_SHORT_REF) is None


def test_l4_local_unfenced_fragment_is_allowed():
    text = "把这一小步单独写出来：\nn = len(s)"
    assert leak_guard.violates(text, 4, QUESTION_SHORT_REF) is None


def test_l4_single_output_fragment_is_allowed():
    text = "先确认这一步要输出什么：\nprint(a + b)"
    assert leak_guard.violates(text, 4, QUESTION_SHORT_REF) is None


def test_l4_fenced_short_snippet_is_allowed():
    assert leak_guard.violates("```python\nn = len(s)\n```", 4, QUESTION_SHORT_REF) is None


# ------------------------------------------------- 处理链：重试一次 → 兜底 + 来源


class _Judge:
    def __init__(self, status="fail", error_type="runtime", stderr="ValueError: bad input"):
        self.status = status
        self.error_type = error_type
        self.stderr = stderr
        self.exec_ms = 1
        self.failed_cases = []


def _ctx(**kw):
    base = dict(
        question=dict(QUESTION_SHORT_REF),
        code="print(0)",
        request_hint=True,
        judge_result=_Judge(),
        last_unlocked=0,
        consecutive_fail=2,
        classification=None,
        memory_hint="",
        persona_system="",
        hint_level=1,
        unlocked_level=1,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_unfenced_leak_retries_once_then_falls_back(monkeypatch):
    calls = []

    def fake(system, user):
        calls.append(user)
        return UNFENCED_ANSWER

    monkeypatch.setattr(hl.llm_mod, "llm", fake)
    out, src = hl.render(_ctx())
    assert src == "fallback", "两次都违规 → 来源记 fallback"
    assert len(calls) == 2, "必须先重试一次再兜底"
    assert "input(" not in out and "print(" not in out


def test_unfenced_leak_then_clean_records_llm_retry(monkeypatch):
    replies = iter([UNFENCED_ANSWER, "先确认输入怎么读。"])
    monkeypatch.setattr(hl.llm_mod, "llm", lambda system, user: next(replies))
    out, src = hl.render(_ctx())
    assert src == "llm_retry"
    assert out == "先确认输入怎么读。"


def test_clean_hint_records_llm(monkeypatch):
    monkeypatch.setattr(hl.llm_mod, "llm", lambda system, user: "先确认输入怎么读。")
    out, src = hl.render(_ctx())
    assert src == "llm"


def test_pipeline_records_llm_retry_source_after_unfenced_leak(monkeypatch):
    """端到端：无围栏泄露 → 重试后通过 → 事件表来源记 `llm_retry`。"""
    monkeypatch.setattr(pipeline_mod, "load_question", lambda qid: dict(QUESTION_SHORT_REF))
    replies = iter([UNFENCED_ANSWER, "先确认输入怎么读。"])
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: next(replies))
    out = pipeline_mod.handle_submission(
        participant_id="T_H_SRC",
        question_id="P900",
        code="print(0)",
        config=PRESETS["exp"],
        request_hint=True,
    )
    rows = db.get_events(participant_id="T_H_SRC", event_type="hint")
    assert len(rows) == 1
    assert rows[0]["status"] == "returned_model"
    assert rows[0]["hint_source"] == "llm_retry"
    assert out["source"] == "harness"
