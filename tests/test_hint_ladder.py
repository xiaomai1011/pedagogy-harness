"""提示阶梯：升级规则（B 口径）与呈现逻辑。"""

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
        last_level=0,
        consecutive_fail=0,
        classification=None,
        memory_hint="",
        persona_system="",
        hint_level=0,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_next_level_requires_request_or_two_failures():
    """B 口径：不求助、且未连败 2 次时，等级不变。"""
    assert hl.next_level(0, requested=False, consecutive_fail=0) == 0
    assert hl.next_level(0, requested=False, consecutive_fail=1) == 0
    assert hl.next_level(1, requested=False, consecutive_fail=0) == 1


def test_next_level_upgrades_on_request_or_two_failures():
    assert hl.next_level(0, requested=True, consecutive_fail=0) == 1
    assert hl.next_level(0, requested=False, consecutive_fail=2) == 1
    assert hl.next_level(3, requested=True, consecutive_fail=0) == 4


def test_next_level_caps_at_four():
    assert hl.next_level(4, requested=True, consecutive_fail=5) == 4


def test_level0_gives_no_teaching_hint():
    """初次进入题目：只给题目与判题反馈，不给教学提示。"""
    out = hl.render(_ctx(hint_level=0))
    assert "未通过测试" in out
    assert "请求提示" in out
    assert "语法" in out


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
