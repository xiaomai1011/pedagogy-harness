"""防泄答案校验的单元测试：判据必须可解释、可复现。"""

from app.harness import leak_guard

QUESTION = {"reference_solution": "def solve(n):\n    return n * 2\n"}


def test_level1_code_fence_is_violation():
    v = leak_guard.violates("你可以这样写：\n```python\nprint(1)\n```", 1, QUESTION)
    assert v is not None and v.rule == "R1"


def test_inline_code_is_allowed():
    # 行内代码（反引号）不属于"泄答案"，L1 应当放行
    assert leak_guard.violates("可以试试用 `len()` 取长度。", 1, QUESTION) is None


def test_program_marker_is_violation_at_any_level():
    v = leak_guard.violates('写完记得 if __name__ == "__main__":', 4, QUESTION)
    assert v is not None and v.rule == "R2"


def test_l4_short_snippet_is_allowed():
    assert leak_guard.violates("```python\nn = len(s)\n```", 4, QUESTION) is None


def test_l4_long_block_is_violation():
    body = "\n".join(f"x{i} = {i}" for i in range(10))
    v = leak_guard.violates(f"```python\n{body}\n```", 4, QUESTION)
    assert v is not None and v.rule == "R3"


def test_copying_reference_solution_is_violation():
    v = leak_guard.violates("def solve(n):\n    return n * 2", 4, QUESTION)
    assert v is not None and v.rule == "R4"


def test_fallback_never_contains_code_fence():
    for level in (1, 2, 3, 4):
        assert "```" not in leak_guard.fallback(level)
