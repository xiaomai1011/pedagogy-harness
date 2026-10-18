"""对接证明：岗位 3 把 `runner.py` 换成真实现后，**其余模块零改动即可接上**（契约 §1）。

做法：在测试里把主干调用的 `judge()` 换成一个「真形状」的判题结果
（带 `total_cases` / `passed_cases` / `exit_code` / `stdout` / `failed_cases`），
断言响应结构、日志列、错误分类、提示状态机都照常工作。

**本文件没有修改 `app/judge/runner.py`** —— 只是借用它的数据结构。
"""

from app import llm as llm_mod
from app.harness import pipeline as pipeline_mod
from app.harness.config_switch import PRESETS
from app.judge.runner import JudgeResult


def _fake_judge(status="fail", error_type="wrong_output", stderr="", passed=0, total=3,
                failed=None, exec_ms=42, stdout="2\n", exit_code=1, holder=None):
    """模拟岗位 3 的真实现：返回字段齐全的 JudgeResult。

    `holder` 非空时把原始结果存进去，供"原始结果未被过滤逻辑原地修改"的断言使用。
    """

    def _judge(question, code, language="python"):
        result = JudgeResult(
            status=status,
            total_cases=total,
            passed_cases=passed,
            failed_cases=failed if failed is not None else (
                [{"case_index": 1, "input": "1 2", "expected": "3", "actual": "2", "hidden": False}]
                if status != "pass" else []),
            error_type=error_type,
            stderr=stderr,
            stdout=stdout,
            exec_ms=exec_ms,
            exit_code=exit_code,
        )
        if holder is not None:
            holder["result"] = result
        return result

    return _judge


def test_real_shape_result_flows_through_pipeline(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "judge", _fake_judge())
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "先看看输入怎么读的。")
    out = pipeline_mod.handle_submission(
        participant_id="T_SWAP_1", question_id="P001", code="print(2)",
        config=PRESETS["exp"], request_hint=True)

    for key in ("status", "failed_cases", "stderr", "hint_level", "unlocked_level",
                "message", "source", "components", "log"):
        assert key in out, f"响应缺少前端依赖的字段：{key}"
    assert out["status"] == "fail"
    assert out["failed_cases"][0]["case_index"] == 1
    assert out["log"]["status"] == "fail"
    assert out["log"]["error_type"] == "wrong_output"
    assert out["log"]["error_class"] == "逻辑"        # 分类器照常接上
    assert out["log"]["elapsed_ms"] == 42            # 真耗时取自判题结果


def test_real_syntax_error_is_classified(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "judge", _fake_judge(
        error_type="syntax", stderr="SyntaxError: invalid syntax"))
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "提示正文")
    out = pipeline_mod.handle_submission(participant_id="T_SWAP_2", question_id="P001",
                                         code="a =", config=PRESETS["exp"])
    assert out["log"]["error_class"] == "语法"


def test_real_timeout_is_classified(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "judge", _fake_judge(
        status="timeout", error_type="timeout", stderr="", exit_code=None))
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "提示正文")
    out = pipeline_mod.handle_submission(participant_id="T_SWAP_3", question_id="P001",
                                         code="while True: pass", config=PRESETS["exp"])
    assert out["log"]["status"] == "timeout"
    assert out["log"]["error_class"] == "环境"
    assert out["log"]["pass_status"] == 0


def test_real_pass_path(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "judge", _fake_judge(
        status="pass", error_type="none", stderr="", passed=3, exit_code=0))
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "你正确判断了边界条件。")
    out = pipeline_mod.handle_submission(
        participant_id="T_SWAP_4", question_id="P001",
        code="a, b = map(int, input().split())\nprint(a + b)", config=PRESETS["exp"])
    assert out["status"] == "pass"
    assert out["log"]["pass_status"] == 1
    assert out["log"]["error_class"] == "未分类"      # 通过态不分类
    assert "边界条件" in out["message"]


def test_hidden_case_is_passed_through_without_echo(monkeypatch):
    """隐藏用例不回显输入/期望/实际输出：**由主干过滤**（判题侧原始结果不被修改）。

    旧版本断言的是"主干原样透传、由判题侧遵守"；F 定案后主干自己过滤，
    因此这里按四条约束拆开验证（保留旧断言里有价值的部分）：

    ① 判题器原始结果**不被原地修改**（岗位③的结果是原始事实，验收工具还要用）；
    ② 学生收到的是**过滤后**结果（隐藏用例三项清空）；
    ③ 可见用例**允许公开的信息仍保留**；
    ④ 判题状态／通过数量等**事实不被过滤逻辑改变**。
    """
    holder = {}
    hidden = [{"case_index": 2, "input": "", "expected": "", "actual": "0", "hidden": True}]
    monkeypatch.setattr(pipeline_mod, "judge", _fake_judge(failed=hidden, holder=holder))
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "提示正文")
    out = pipeline_mod.handle_submission(participant_id="T_SWAP_5", question_id="P001",
                                         code="print(0)", config=PRESETS["exp"])
    case = out["failed_cases"][0]
    # ② 学生看到的是过滤后的结果
    assert case["hidden"] is True
    assert case["input"] == "" and case["expected"] == ""
    assert case["actual"] == "", "隐藏用例的实际输出同样不得回显"
    # ① 原始结果没被改：过滤只作用在副本上
    raw = holder["result"].failed_cases[0]
    assert raw["actual"] == "0"
    # ③ 可见用例的公开信息保留（同一响应里的可见条目照旧）
    visible = {"case_index": 1, "input": "1 2", "expected": "3", "actual": "2",
               "hidden": False}
    monkeypatch.setattr(pipeline_mod, "judge",
                        _fake_judge(failed=[visible, hidden[0]], holder={}))
    out2 = pipeline_mod.handle_submission(participant_id="T_SWAP_6", question_id="P001",
                                          code="print(0)", config=PRESETS["exp"])
    vis = {c["case_index"]: c for c in out2["failed_cases"]}[1]
    assert (vis["input"], vis["expected"], vis["actual"]) == ("1 2", "3", "2")
    # ④ 判题事实不被过滤改变
    assert out2["status"] == "fail"
    assert out2["log"]["status"] == "fail" and out2["log"]["pass_status"] == 0
    assert len(out2["failed_cases"]) == 2, "隐藏用例条目本身要保留（学生需知道有隐藏用例未过）"
