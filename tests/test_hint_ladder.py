"""提示阶梯：口径②（解锁 / 展示两状态）与呈现逻辑。"""

import types

from app.harness import hint_ladder as hl


class _Judge:
    """判题结果的轻量替身，只带本模块用到的字段。"""

    def __init__(self, status="fail", error_type="语法", stderr="SyntaxError", exec_ms=1):
        self.status = status
        self.error_type = error_type
        self.stderr = stderr
        self.exec_ms = exec_ms
        self.failed_cases = []


def _ctx(**kw):
    status = kw.pop("status", "fail")
    base = dict(
        question={"id": "P001", "title": "求和", "statement": "读入 n 输出 n*2",
                  "reference_solution": ""},
        code="print(1)",
        request_hint=False,
        judge_result=_Judge(status=status),
        last_unlocked=0,
        consecutive_fail=0,
        classification=None,
        memory_hint="",
        persona_system="",
        hint_level=0,
        unlocked_level=0,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_not_requested_never_shows_even_when_unlocked():
    """口径②：学生没主动请求，就一个字提示都不展示。"""
    assert hl.next_state(prev_unlocked=0, consecutive_fail=0, requested=False) == (0, 0)
    assert hl.next_state(prev_unlocked=0, consecutive_fail=1, requested=False) == (0, 0)
    assert hl.next_state(prev_unlocked=2, consecutive_fail=4, requested=False) == (0, 2)


def test_two_consecutive_failures_only_unlock():
    """连败 2 次**只解锁**：解锁 L1，但本次不展示。"""
    assert hl.next_state(prev_unlocked=0, consecutive_fail=2, requested=False) == (0, 1)
    assert hl.next_state(prev_unlocked=1, consecutive_fail=4, requested=False) == (0, 2)


def test_request_shows_unlocked_level_without_advancing():
    """口径②-A：请求**只展示**已解锁的那一级，不再"至少推进一级"。"""
    # 还没攒够连败（解锁为 0）→ 给 L1 打底，保证学生点了就有回应
    assert hl.next_state(prev_unlocked=0, consecutive_fail=0, requested=True) == (1, 1)
    # 连败 2 次解锁 L1 → 展示 L1
    assert hl.next_state(prev_unlocked=1, consecutive_fail=2, requested=True) == (1, 1)
    # 连败 4 次解锁 L2 → 展示 L2
    assert hl.next_state(prev_unlocked=2, consecutive_fail=4, requested=True) == (2, 2)


def test_repeated_requests_never_raise_the_level():
    """回归：反复点"请求提示"必须停在已解锁的那一级。

    （改口径前这里是 1→2→3→4：点击次数本身就能把等级推满 L4。）
    """
    unlocked = 0
    shown_levels = []
    for _ in range(4):
        shown, unlocked = hl.next_state(prev_unlocked=unlocked, consecutive_fail=0, requested=True)
        shown_levels.append(shown)
    assert shown_levels == [1, 1, 1, 1]


def test_unlock_caps_at_four():
    assert hl.next_state(prev_unlocked=0, consecutive_fail=20, requested=False) == (0, 4)
    assert hl.next_state(prev_unlocked=4, consecutive_fail=8, requested=True) == (4, 4)


def test_level0_gives_no_teaching_hint():
    """初次进入题目：只给题目与判题反馈，不给教学提示。"""
    out = hl.render(_ctx(hint_level=0))
    assert "未通过测试" in out
    assert "请求提示" in out
    assert "语法" in out


def test_level0_tells_student_what_is_unlocked():
    """已解锁但没请求：只提示"已解锁 L?、可点请求"，不给提示正文。"""
    out = hl.render(_ctx(hint_level=0, unlocked_level=2))
    assert "已解锁 L2" in out


def test_hint_passes_through_when_clean(monkeypatch):
    monkeypatch.setattr(hl.llm_mod, "llm", lambda system, user: "先确认这一步的输入是什么。")
    out = hl.render(_ctx(hint_level=1))
    assert "先确认这一步的输入是什么。" in out


def test_hint_falls_back_when_model_leaks_answer(monkeypatch):
    """模型两次都给代码块 → 必须改用不含代码的兜底文本，绝不泄答案。"""
    monkeypatch.setattr(hl.llm_mod, "llm", lambda system, user: "```python\nprint(1)\n```")
    out = hl.render(_ctx(hint_level=1))
    assert "```" not in out
    assert "基本操作" in out


def test_success_message_uses_injected_persona(monkeypatch):
    captured = {}

    def fake_llm(system, user):
        captured["system"] = system
        return "你正确判断了边界条件。"

    monkeypatch.setattr(hl.llm_mod, "llm", fake_llm)
    ctx = _ctx(status="pass", hint_level=4, persona_system="PERSONA_X")
    out = hl.render(ctx)
    assert out == "你正确判断了边界条件。"
    assert captured["system"] == "PERSONA_X"


def test_success_message_falls_back_when_model_leaks(monkeypatch):
    monkeypatch.setattr(hl.llm_mod, "llm", lambda system, user: "```python\nprint(1)\n```")
    out = hl.render(_ctx(status="pass", hint_level=0))
    assert "```" not in out
