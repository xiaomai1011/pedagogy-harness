"""错误分类器：规则优先 + 大模型兜底 + 解析失败的兜底行为。"""

from app.harness import error_classifier as ec


class _Judge:
    """判题结果的轻量替身，只带分类器用到的字段。"""

    def __init__(self, status="fail", error_type="runtime", stderr=""):
        self.status = status
        self.error_type = error_type
        self.stderr = stderr


_Q = {"id": "P001", "title": "求和", "knowledge_points": ["标准输入输出", "边界条件"]}


def test_syntax_rule():
    c = ec.classify(_Judge(error_type="syntax", stderr="SyntaxError: invalid syntax"), "x =", _Q)
    assert (c.error_class, c.source) == ("语法", "rule")


def test_indentation_counts_as_syntax():
    c = ec.classify(_Judge(error_type="syntax", stderr="IndentationError: unexpected indent"), "  print(1)", _Q)
    assert c.error_class == "语法"


def test_timeout_is_environment():
    c = ec.classify(_Judge(status="timeout", error_type="timeout", stderr=""), "while True: pass", _Q)
    assert c.error_class == "环境"


def test_name_error_counts_as_syntax():
    """NameError 执行层是 runtime，教学口径归「语法」（名字写错 / 忘了赋值）。"""
    c = ec.classify(_Judge(status="crash", error_type="runtime",
                           stderr="NameError: name 'n' is not defined"), "print(n)", _Q)
    assert (c.error_class, c.source) == ("语法", "rule")


def test_type_error_rule():
    c = ec.classify(_Judge(status="crash", error_type="runtime",
                           stderr="TypeError: unsupported operand type(s)"), "1 + 'a'", _Q)
    assert c.error_class == "类型"


def test_boundary_rules():
    index = ec.classify(_Judge(status="crash", error_type="runtime",
                               stderr="IndexError: list index out of range"), "a[9]", _Q)
    zero = ec.classify(_Judge(status="crash", error_type="runtime",
                              stderr="ZeroDivisionError: division by zero"), "1 / 0", _Q)
    assert index.error_class == "边界"
    assert zero.error_class == "边界"


def test_wrong_output_is_logic():
    c = ec.classify(_Judge(status="fail", error_type="wrong_output", stderr=""), "print(2)", _Q)
    assert (c.error_class, c.source) == ("逻辑", "rule")


def test_knowledge_point_matched_by_keyword():
    c = ec.classify(_Judge(error_type="syntax", stderr="SyntaxError"), "x =", _Q)
    assert c.knowledge_point == "标准输入输出"


def test_knowledge_point_stays_empty_when_nothing_matches():
    """题目知识点里没有对应关键词 → 留空，不硬凑一个写进日志。"""
    q = {"id": "P002", "knowledge_points": ["贪心策略"]}
    c = ec.classify(_Judge(status="crash", error_type="runtime",
                           stderr="TypeError: bad operand"), "x", q)
    assert c.error_class == "类型"
    assert c.knowledge_point == ""


def test_pass_is_not_classified_and_costs_no_model_call(monkeypatch):
    """通过态不分类，也不该花一次模型调用。"""

    def _boom(**kwargs):
        raise AssertionError("通过态不应调用大模型")

    monkeypatch.setattr(ec.llm_mod, "llm", _boom)
    c = ec.classify(_Judge(status="pass", error_type="none"), "print(1)", _Q)
    assert (c.error_class, c.source) == ("未分类", "none")


def test_llm_fallback_when_no_rule_hits(monkeypatch):
    monkeypatch.setattr(ec.llm_mod, "llm", lambda system, user:
                        '{"error_class": "边界", "knowledge_point": "边界条件"}')
    c = ec.classify(_Judge(status="crash", error_type="runtime",
                           stderr="SomeWeirdError: 未知异常"), "x", _Q)
    assert (c.error_class, c.knowledge_point, c.source) == ("边界", "边界条件", "llm")


def test_llm_reply_with_wrapper_text_is_still_parsed(monkeypatch):
    """模型爱加开场白 → 用正则抠出 JSON，仍可用。"""
    monkeypatch.setattr(ec.llm_mod, "llm", lambda system, user:
                        '好的，分类如下：\n{"error_class": "逻辑"}\n以上。')
    c = ec.classify(_Judge(status="fail", error_type="wrong_output", stderr=""), "x", _Q)
    assert c.source == "rule"          # wrong_output 已被规则命中，不会走到模型


def test_llm_wrapper_text_and_unknown_error(monkeypatch):
    monkeypatch.setattr(ec.llm_mod, "llm", lambda system, user:
                        '好的，分类如下：\n{"error_class": "逻辑"}\n以上。')
    c = ec.classify(_Judge(status="crash", error_type="runtime",
                           stderr="SomeWeirdError: 未知异常"), "x", _Q)
    assert (c.error_class, c.source) == ("逻辑", "llm")


def test_llm_invalid_class_falls_back_to_unclassified(monkeypatch):
    monkeypatch.setattr(ec.llm_mod, "llm", lambda system, user: '{"error_class": "硬件问题"}')
    c = ec.classify(_Judge(status="crash", error_type="runtime",
                           stderr="SomeWeirdError: 未知异常"), "x", _Q)
    assert (c.error_class, c.source) == ("未分类", "none")


def test_llm_invented_knowledge_point_is_dropped(monkeypatch):
    """模型自造的知识点不能被写进日志。"""
    monkeypatch.setattr(ec.llm_mod, "llm", lambda system, user:
                        '{"error_class": "类型", "knowledge_point": "量子力学"}')
    c = ec.classify(_Judge(status="crash", error_type="runtime",
                           stderr="SomeWeirdError: 未知异常"), "x", _Q)
    assert (c.error_class, c.knowledge_point) == ("类型", "")


def test_llm_garbage_reply_falls_back_to_unclassified(monkeypatch):
    monkeypatch.setattr(ec.llm_mod, "llm", lambda system, user: "抱歉，我无法完成分类。")
    c = ec.classify(_Judge(status="crash", error_type="runtime",
                           stderr="SomeWeirdError: 未知异常"), "x", _Q)
    assert (c.error_class, c.source) == ("未分类", "none")
