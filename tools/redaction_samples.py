"""F 过滤前后的对照示例：用模拟敏感标记替代真实隐藏用例数据，不读真实题库、不联网。

用途：给评阅／交接看"过滤前长什么样、过滤后长什么样"，以及三条模型上下文（分类模型／
提示模型／裸模型兜底）实际送进去的报错文本。

跑法（仓库根目录，Windows 下须带 PYTHONIOENCODING 以免中文输出报编码错）：
    $env:PYTHONIOENCODING="utf-8"; python tools/redaction_samples.py

只读演示：数据库写临时目录，不改真实 pedagogy.sqlite，不产生对外接口。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["PH_DB"] = str(Path(tempfile.gettempdir()) / "f_demo_redaction.sqlite")
Path(os.environ["PH_DB"]).unlink(missing_ok=True)

import app.llm as llm_mod  # noqa: E402
from app.harness import error_classifier as ec  # noqa: E402
from app.harness import redaction  # noqa: E402
from app.harness.pipeline import handle_submission  # noqa: E402
from app.harness.pipeline import handle_hint_request  # noqa: E402
from app.harness.config_switch import PRESETS  # noqa: E402
from app.judge.runner import JudgeResult  # noqa: E402

HIDDEN_IN = "HIDDEN_INPUT_MARKER_7"
HIDDEN_EXP = "HIDDEN_EXPECTED_MARKER_7"
HIDDEN_ACT = "HIDDEN_ACTUAL_MARKER_7"
RAW_ERR = f"ValueError: invalid literal for int() with base 10: '{HIDDEN_IN}'"

QUESTION = {
    "id": "P_DEMO",
    "title": "演示题",
    "statement": "读入两个整数输出和。",
    "knowledge_points": ["标准输入输出"],
    "difficulty": 1,
    "phase": "practice",
    "test_cases": [
        {"input": "1 2", "expected": "3", "hidden": False},
        {"input": HIDDEN_IN, "expected": HIDDEN_EXP, "hidden": True},
    ],
}

RAW = JudgeResult(
    status="fail",
    total_cases=2,
    passed_cases=0,
    failed_cases=[
        {"case_index": 0, "input": "1 2", "expected": "3", "actual": "2", "hidden": False},
        {"case_index": 1, "input": HIDDEN_IN, "expected": HIDDEN_EXP,
         "actual": HIDDEN_ACT, "hidden": True},
    ],
    error_type="runtime",
    stderr=RAW_ERR,
    stdout=HIDDEN_ACT,
    exec_ms=7,
    exit_code=1,
)

import app.harness.pipeline as pipeline_mod  # noqa: E402

pipeline_mod.load_question = lambda qid: dict(QUESTION)
pipeline_mod.judge = lambda question, code, language="python": RAW
llm_mod.llm = lambda system, user: "先确认输入是怎么分隔的。"

out = handle_submission(participant_id="DEMO", question_id="P_DEMO", code="print(0)",
                        config=PRESETS["exp"])

print("=" * 72)
print("① 判题器原始结果（岗位③交付物，F 前后都不改）")
print("=" * 72)
print(json.dumps(RAW.failed_cases, ensure_ascii=False, indent=2))
print("stderr :", RAW.stderr)
print("stdout :", RAW.stdout)
print("status :", RAW.status, "| error_type:", RAW.error_type, "| exec_ms:", RAW.exec_ms)

print()
print("=" * 72)
print("② 学生响应（过滤后）")
print("=" * 72)
for key in ("status", "error_type", "stderr", "failed_cases", "judge_feedback", "message"):
    print(f"{key} =", json.dumps(out[key], ensure_ascii=False))

print()
print("=" * 72)
print("③ 泄露自检：三个模拟敏感标记在响应里还找得到吗？")
print("=" * 72)
blob = json.dumps(out, ensure_ascii=False)
for name, marker in (("隐藏输入", HIDDEN_IN), ("隐藏期望", HIDDEN_EXP), ("隐藏实际", HIDDEN_ACT)):
    leaked = marker in blob
    print(f"{name} {marker}: {'[泄露] 仍在响应里' if leaked else '[安全] 已不在响应里'}")

print()
print("=" * 72)
print("④ 过滤口径单元示例（redaction 层直调）")
print("=" * 72)
print("student_stderr  :", json.dumps(redaction.student_stderr(RAW, QUESTION), ensure_ascii=False))
print("model_stderr    :", json.dumps(redaction.model_stderr(RAW, QUESTION), ensure_ascii=False))
print("controlled_name :", json.dumps(redaction.controlled_exception_name(RAW_ERR), ensure_ascii=False))
VISIBLE_ONLY = JudgeResult(status="fail", total_cases=1, passed_cases=0,
                           failed_cases=[{"case_index": 0, "input": "1 2", "expected": "3",
                                          "actual": "2", "hidden": False}],
                           error_type="runtime", stderr=RAW_ERR, stdout="", exec_ms=7, exit_code=1)
print("两边都只有可见条目:", json.dumps(redaction.student_stderr(VISIBLE_ONLY, {"test_cases": [{"hidden": False}]}),
                                    ensure_ascii=False))
print("题目称无隐藏用例，但结果有:", json.dumps(
    redaction.student_stderr(RAW, {"test_cases": [{"hidden": False}]}), ensure_ascii=False))
print("白名单外的异常名  :", json.dumps(
    redaction.student_stderr(JudgeResult(status="fail", total_cases=1, passed_cases=0,
                                         failed_cases=RAW.failed_cases, error_type="runtime",
                                         stderr=f"{HIDDEN_IN}Error: boom", stdout="",
                                         exec_ms=7, exit_code=1), QUESTION), ensure_ascii=False))
print("classify() 命中 :", ec.classify(RAW, "print(0)", QUESTION))

print()
print("=" * 72)
print("⑤ 模型上下文：送进模型的报错文本（分类模型 / 提示模型 / 裸模型兜底）")
print("=" * 72)
CAPTURED = []


def _capture(system, user, **kw):
    CAPTURED.append(user)
    return "先确认输入是怎么分隔的。"


llm_mod.llm = _capture
# 分类模型路径：异常名不在规则表里，必然走大模型分类
UNKNOWN_RAW = JudgeResult(status="fail", total_cases=2, passed_cases=0,
                          failed_cases=RAW.failed_cases, error_type="runtime",
                          stderr=f"WeirdError: boom {HIDDEN_IN}", stdout="", exec_ms=7, exit_code=1)
pipeline_mod.judge = lambda question, code, language="python": UNKNOWN_RAW
CAPTURED.clear()
handle_submission(participant_id="DEMO_CLS", question_id="P_DEMO", code="print(0)",
                  config=PRESETS["exp"])
print("[分类模型] 送进去的报错行：")
for line in CAPTURED[0].splitlines():
    if "报错" in line:
        print("   ", line)

# 提示模型路径 + 裸模型兜底路径
pipeline_mod.judge = lambda question, code, language="python": RAW
CAPTURED.clear()
handle_hint_request(participant_id="DEMO_HINT", question_id="P_DEMO", code="print(0)",
                    config=PRESETS["exp"])
print("[提示模型] 送进去的报错行：")
for line in CAPTURED[0].splitlines():
    if "报错" in line:
        print("   ", line)

CAPTURED.clear()
handle_submission(participant_id="DEMO_RAW", question_id="P_DEMO", code="print(0)",
                  config=PRESETS["ctrl"])
print("[裸模型兜底] 送进去的报错行：")
for line in CAPTURED[0].splitlines():
    if "报错" in line:
        print("   ", line)

print()
print("三个模拟敏感标记在模型上下文里：")
for name, marker in (("隐藏输入", HIDDEN_IN), ("隐藏期望", HIDDEN_EXP), ("隐藏实际", HIDDEN_ACT)):
    leaked = any(marker in prompt for prompt in CAPTURED)
    print(f"  {name} {marker}: {'[泄露] 仍进了模型上下文' if leaked else '[安全] 未进模型上下文'}")
