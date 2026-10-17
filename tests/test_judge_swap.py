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
                failed=None, exec_ms=42, stdout="2\n", exit_code=1):
    """模拟岗位 3 的真实现：返回字段齐全的 JudgeResult。"""

    def _judge(question, code, language="python"):
        return JudgeResult(
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
    """隐藏用例不回显输入/期望：由判题侧遵守，主干原样透传。"""
    hidden = [{"case_index": 2, "input": "", "expected": "", "actual": "0", "hidden": True}]
    monkeypatch.setattr(pipeline_mod, "judge", _fake_judge(failed=hidden))
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "提示正文")
    out = pipeline_mod.handle_submission(participant_id="T_SWAP_5", question_id="P001",
                                         code="print(0)", config=PRESETS["exp"])
    case = out["failed_cases"][0]
    assert case["hidden"] is True
    assert case["input"] == "" and case["expected"] == ""
