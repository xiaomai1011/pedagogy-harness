"""错误分类器 × 正式题库：知识点映射的覆盖与"不乱猜"约束。

背景（评阅意见 2026-10-28）：题库知识点用的是「循环／列表／比较／切片／取模」这类
正常课程术语，旧分类器只认「变量／赋值／输出」等泛化词，导致 `error_kp` 大量为空。
处理方式是**扩分类器**，不改题库文案；同时必须保证「题目属于循环」不等于
「所有错误都算循环错」，因此这里固定三类断言：

1. 强证据（stderr 里的异常名）优先，且只匹配该异常允许的知识点；
2. 无异常名证据时才走弱回退（典型是 wrong_output）；
3. 强证据与题目知识点对不上时，**留空**，不退回题目主题。
"""

import json
from pathlib import Path

import pytest

from app.harness import error_classifier as ec
from app.judge.runner import JudgeResult

QUESTIONS = Path(__file__).resolve().parent.parent / "data" / "questions"


def judge_result(error_type: str, stderr: str = "", status: str = "fail") -> JudgeResult:
    return JudgeResult(status=status, total_cases=1, passed_cases=0, failed_cases=[],
                       error_type=error_type, stderr=stderr, stdout="", exec_ms=1,
                       exit_code=1)


def bank() -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(QUESTIONS.glob("*.json"))}


def kp(question: dict, error_type: str, stderr: str = "") -> str:
    return ec.classify_rule_only(judge_result(error_type, stderr), "x = 1", question).knowledge_point


# 19 道题的预期映射（空串＝明确允许为空：题目没有对应知识点，不许硬凑）。
# 三个探针分别是：IndexError（边界类，列表相关）、ZeroDivisionError（取模相关）、
# wrong_output（无异常名 → 弱回退到题目主知识点）。
EXPECTED = {
    "P001": {"index": "列表", "zero": "", "wrong": "循环"},
    "P002": {"index": "切片", "zero": "", "wrong": "切片"},
    "P003": {"index": "列表", "zero": "", "wrong": "列表"},
    "P004": {"index": "", "zero": "取模", "wrong": "循环"},
    "P005": {"index": "", "zero": "", "wrong": "比较"},
    "P006": {"index": "列表", "zero": "", "wrong": "排序"},
    "P007": {"index": "", "zero": "", "wrong": "字符串遍历"},
    "P008": {"index": "", "zero": "", "wrong": "循环"},
    "P009": {"index": "", "zero": "", "wrong": "循环"},
    "P010": {"index": "", "zero": "", "wrong": ""},
    "P011": {"index": "", "zero": "取模", "wrong": "循环"},
    "P012": {"index": "边界处理", "zero": "", "wrong": ""},
    "P013": {"index": "", "zero": "", "wrong": "逻辑组合（与 / 或）"},
    "S001": {"index": "", "zero": "", "wrong": "遍历"},
    "S002": {"index": "", "zero": "", "wrong": "遍历"},
    "S003": {"index": "", "zero": "", "wrong": "三种循环关系"},
    "S004": {"index": "", "zero": "", "wrong": "遍历"},
    "S005": {"index": "", "zero": "", "wrong": "遍历"},
    "S006": {"index": "", "zero": "", "wrong": "三种循环关系"},
}

PROBES = {
    "index": ("runtime", "Traceback (most recent call last):\nIndexError: list index out of range"),
    "zero": ("runtime", "Traceback (most recent call last):\nZeroDivisionError: division by zero"),
    "wrong": ("wrong_output", ""),
}


def test_bank_has_expected_size():
    assert len(bank()) == 19, "题库应为 19 题；数量变了就要同步更新本表"


@pytest.mark.parametrize("qid", sorted(EXPECTED))
def test_expected_knowledge_point_per_question(qid: str):
    question = bank()[qid]
    for probe, (error_type, stderr) in PROBES.items():
        assert kp(question, error_type, stderr) == EXPECTED[qid][probe], (
            f"{qid} 的 {probe} 探针映射变了；若是有意调整分类器，请同步更新本表")


@pytest.mark.parametrize("qid", sorted(EXPECTED))
def test_knowledge_point_always_belongs_to_question(qid: str):
    """任何情况下都只能返回题目自己声明过的知识点，或空串。"""
    question = bank()[qid]
    probes = list(PROBES.values()) + [
        ("syntax", "SyntaxError: invalid syntax"),
        ("runtime", "NameError: name 'x' is not defined"),
        ("runtime", "TypeError: unsupported operand type(s) for +: 'int' and 'str'"),
        ("timeout", ""),
    ]
    for error_type, stderr in probes:
        got = kp(question, error_type, stderr)
        assert got == "" or got in question["knowledge_points"], f"{qid} 返回了未声明的知识点 {got!r}"


def test_evidence_beats_question_topic():
    """P001 主题是循环，但 IndexError 的强证据指向「列表」——不能被主题吃掉。"""
    assert kp(bank()["P001"], "runtime", "IndexError: list index out of range") == "列表"


def test_unrelated_error_is_not_forced_onto_topic():
    """P001（循环/列表/比较）遇到 TypeError：没有匹配的知识点就必须留空。"""
    result = ec.classify_rule_only(
        judge_result("runtime", "TypeError: unsupported operand type(s) for +: 'int' and 'str'"),
        "x = 1", bank()["P001"])
    assert result.error_class == "类型"
    assert result.knowledge_point == "", "强证据对不上时必须留空，不能回退成题目主题"


def test_exception_names_are_case_insensitive():
    question = bank()["P001"]
    assert kp(question, "runtime", "indexerror: list index out of range") == "列表"
    assert ec.match_rule(judge_result("runtime", "valueerror: invalid literal"), "") == "边界"


def test_code_text_alone_is_not_evidence():
    """stderr 里只有代码片段、没有异常名 → 不算证据；runtime 下不产知识点。"""
    question = bank()["P002"]
    stderr = "Traceback (most recent call last):\n  File \"main.py\", line 3, in <module>\n    for ch in s: pass\n"
    result = ec.classify_rule_only(judge_result("runtime", stderr), "for ch in s: pass", question)
    assert result.error_class == "未分类"
    assert result.knowledge_point == ""


def test_exception_name_inside_source_line_is_not_evidence():
    """traceback 的**源码行**里出现异常名不算证据——只认真正抛出的那一行。

    Python 的 traceback 会回显出错的那行源码，学生代码里完全可能出现
    `print("ZeroDivisionError")` 这种字符串；若整段 stderr 做子串搜索，
    就会把 ValueError 记成「取模」，污染错误记忆。
    """
    question = next(q for q in bank().values()
                    if any("取模" in p for p in q["knowledge_points"]))
    crashed = 'print("ZeroDivisionError")'
    stderr = ('Traceback (most recent call last):\n'
              '  File "main.py", line 4, in <module>\n'
              '    print("ZeroDivisionError")\n'
              'ValueError: invalid literal for int() with base 10\n')
    result = ec.classify_rule_only(judge_result("runtime", stderr), crashed, question)
    assert result.knowledge_point != "取模", "源码行里的字符串不能当作异常证据"

    real = ('Traceback (most recent call last):\n'
            '  File "main.py", line 2, in <module>\n'
            '    1 % 0\n'
            'ZeroDivisionError: integer division or modulo by zero\n')
    assert ec.classify_rule_only(judge_result("runtime", real), "1 % 0", question).knowledge_point == "取模"



def test_wrong_output_weak_fallback_stays_within_topic():
    """wrong_output 没有异常名证据 → 允许弱回退，但只能在题目知识点里挑。"""
    question = bank()["P006"]
    result = ec.classify_rule_only(judge_result("wrong_output", ""), "print(1)", question)
    assert result.error_class == "逻辑"
    assert result.knowledge_point == "排序"


def test_old_style_question_still_classified():
    """旧题（泛化知识点）行为可解释：强证据仍能命中，缺证据则留空。

    与旧实现的差别：旧版会拿「语法」类的宽关键词去套题目知识点（于是
    `标准输入输出` 因为含「输入」而被当成语法错的知识点）；新版只在异常名
    与知识点目录对得上时才落知识点，对不上就留空——这是有意的收紧。
    """
    legacy = {"knowledge_points": ["标准输入输出", "变量与赋值"]}
    assert kp(legacy, "syntax", "SyntaxError: invalid syntax") == ""
    assert kp(legacy, "runtime", "NameError: name 'a' is not defined") == "变量与赋值"
    assert kp(legacy, "wrong_output", "") == ""
