"""判题运行器（`app/judge/runner.py`）的行为测试。

覆盖契约 §1 的执行层约定：通过／输出不符／语法错／运行异常／超时、
输出规范化、stdout 截断、隐藏用例不回显，以及三条安全约定：
子进程环境变量白名单、超时杀整棵进程树、临时目录跑完即删。

**这些测试会真的起子进程跑无害代码**（`print`、`while True: pass` 之类），
不写文件、不联网。隔离验收（G 门）不因本文件通过而成立。
"""

import os
import sys
import time

import pytest

from app.judge.runner import OUTPUT_LIMIT, TIMEOUT_SECONDS, judge

QUESTION = {
    "id": "T001",
    "test_cases": [
        {"input": "5", "expected": "10", "hidden": False},
        {"input": "7", "expected": "14", "hidden": True},
    ],
}


def test_pass_all_cases():
    result = judge(QUESTION, "print(int(input()) * 2)")
    assert result.status == "pass"
    assert result.error_type == "none"
    assert (result.passed_cases, result.total_cases) == (2, 2)
    assert result.exit_code == 0
    assert result.failed_cases == []


def test_wrong_output_keeps_case_redacted_for_hidden():
    result = judge(QUESTION, "print(int(input()) * 3)")
    assert result.status == "fail"
    assert result.error_type == "wrong_output"
    assert result.passed_cases == 0
    visible, hidden = result.failed_cases
    assert visible["input"] == "5" and visible["expected"] == "10"
    # 隐藏用例：只留标记，不回显输入与期望输出
    assert hidden["hidden"] is True
    assert hidden["input"] is None
    assert hidden["expected"] is None


def test_syntax_error():
    result = judge(QUESTION, "print(int(input()) * 2")
    assert (result.status, result.error_type) == ("fail", "syntax")
    assert "SyntaxError" in result.stderr


def test_runtime_crash_keeps_traceback():
    result = judge(QUESTION, 'print(int("abc") * 2)')
    assert (result.status, result.error_type) == ("crash", "runtime")
    assert "Traceback" in result.stderr
    assert result.exit_code != 0


def test_timeout_sets_null_exit_code():
    started = time.time()
    result = judge(QUESTION, "while True:\n    pass")
    elapsed = time.time() - started
    assert (result.status, result.error_type) == ("timeout", "timeout")
    assert result.exit_code is None
    # 超时必须是硬上限：不允许被孙进程拖长
    assert elapsed < TIMEOUT_SECONDS + 3


def test_output_normalized_per_line():
    question = {"test_cases": [{"input": "", "expected": "10\n20", "hidden": False}]}
    result = judge(question, 'print("10 "); print("20"); print(); print("   ")')
    assert (result.status, result.error_type) == ("pass", "none")


def test_stdout_and_actual_truncated():
    question = {"test_cases": [{"input": "", "expected": "x", "hidden": False}]}
    result = judge(question, 'print("B" * 5000)')
    assert len(result.stdout) <= OUTPUT_LIMIT
    assert len(result.failed_cases[0]["actual"]) <= OUTPUT_LIMIT


def test_env_whitelist_hides_secrets(monkeypatch):
    monkeypatch.setenv("PH_LLM_API_KEY", "sk-test-must-not-leak")
    question = {"test_cases": [{"input": "", "expected": "none", "hidden": False}]}
    result = judge(question, 'import os\nprint(os.environ.get("PH_LLM_API_KEY", "none"))')
    assert result.status == "pass", result.stderr
    assert "sk-test-must-not-leak" not in (result.stdout + result.stderr)


def test_language_other_than_python_rejected():
    result = judge(QUESTION, "console.log(1)", language="javascript")
    assert (result.status, result.error_type) == ("error", "runtime")
    assert "javascript" in result.stderr


def test_empty_test_cases_is_judge_error():
    result = judge({"test_cases": []}, "print(1)")
    assert (result.status, result.error_type) == ("error", "runtime")


def test_timeout_kills_whole_process_tree(tmp_path):
    marker = (tmp_path / "grandchild.txt").as_posix()
    code = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', "
        f"\"import time, pathlib; time.sleep(8); pathlib.Path({marker!r}).write_text('alive')\"])\n"
        "while True:\n"
        "    pass\n"
    )
    question = {"test_cases": [{"input": "", "expected": "ok", "hidden": False}]}
    started = time.time()
    result = judge(question, code)
    assert result.status == "timeout"
    assert time.time() - started < TIMEOUT_SECONDS + 3
    time.sleep(2)
    assert not (tmp_path / "grandchild.txt").exists(), "孙进程逃过了超时清理"


def test_temp_dir_removed_after_judging(tmp_path):
    question = {"test_cases": [{"input": "", "expected": "1", "hidden": False}]}
    before = set(os.listdir(_temp_root()))
    judge(question, "print(1)")
    after = set(os.listdir(_temp_root()))
    assert not any(name.startswith("ph-judge-") for name in after - before)


def _temp_root() -> str:
    import tempfile
    return tempfile.gettempdir()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
