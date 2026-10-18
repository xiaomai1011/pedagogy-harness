"""F 待办：**隐藏信息过滤**（先红后绿）。

口径来源（用户 2026-10-18 指示 ＋ 2026-10-18 评阅复审收窄 ＋ 契约 §1／§6.10 Q6
＋ 工作日志 §六 Q4 定案）：

1. **学生响应**：隐藏用例的输入／期望／实际输出、原始异常正文**不得**从任何返回字段
   泄露，**包括 `judge_feedback`**；
2. **模型上下文**：分类模型、提示阶梯、裸模型兜底三条路径都要过滤 ——
   **不能只在最终 API 返回时处理**；
3. **相关日志**：不得把过滤掉的原文转存进 `reason` 等字段；
4. 过滤**只删不该给的**：判题状态、通过数量、`error_type` 枚举语义、可见用例的公开信息
   与**判题器原始结果本身**都不得被改变（旧测试"原样透传"里那条有价值的约束）。
5. **异常类别名只认有限白名单**（服务端登记的 Python 内置异常名）：未知、自定义、
   来源无法确认的名称一律退回**固定说明**，**不得因为字符串"长得像异常名"就透传**；
   提取必须锚定在"异常类型"的位置上，不得对整段文本做自由搜索。
6. **结果条目自身声明 `hidden=True` 的，始终过滤**，不得被题目"无隐藏用例"的声明覆盖；
   题目与结果元数据冲突时按**保守**处理，不直接公开。

本文件**全部离线**：判题器用假实现替换、模型用固定文本替换，**不联网**；
数据库用 conftest 的临时库（`PH_DB`），**不碰真实 `pedagogy.sqlite`**。
"""

import json

import pytest

from app import db
from app import llm as llm_mod
from app.harness import pipeline as pipeline_mod
from app.harness import redaction
from app.harness.config_switch import PRESETS
from app.judge.runner import JudgeResult

HIDDEN_IN = "HIDDEN_INPUT_MARKER_7"
HIDDEN_EXP = "HIDDEN_EXPECTED_MARKER_7"
HIDDEN_ACT = "HIDDEN_ACTUAL_MARKER_7"
VISIBLE_IN = "1 2"
VISIBLE_EXP = "3"
VISIBLE_ACT = "2"

# 原始异常正文里**直接嵌了隐藏用例的输入**（真判题器会出现这种形态：
# `ValueError: invalid literal for int() with base 10: '<隐藏输入>'`）
RAW_ERR = f"ValueError: invalid literal for int() with base 10: '{HIDDEN_IN}'"
# 规则表命中不了的异常名 → 必然走大模型分类路径（用于验证"模型上下文"也过滤了）
UNKNOWN_ERR = f"WeirdError: boom {HIDDEN_IN}"
# 异常名**本身**就带敏感标记（"长得像异常名"不得成为透传理由）
MARKER_NAME_ERR = f"{HIDDEN_IN}Error: boom"
# 多行 traceback：末行是真异常类型，敏感标记只出现在源码行与消息正文里
TRACEBACK_REAL_TYPE = (
    "Traceback (most recent call last):\n"
    '  File "sol.py", line 1, in <module>\n'
    f"    int({HIDDEN_IN})\n"
    f"ValueError: invalid literal for int() with base 10: '{HIDDEN_IN}'"
)
# 多行 traceback：末行是**伪异常名**（不在白名单）→ 只能退回固定说明
TRACEBACK_FAKE_TYPE = (
    "Traceback (most recent call last):\n"
    '  File "sol.py", line 1, in <module>\n'
    f"    raise {HIDDEN_IN}Error({HIDDEN_IN})\n"
    f"{HIDDEN_IN}Error: boom"
)


def _question(*, phase="practice", hidden_cases=True):
    """题目元数据：`hidden_cases=False` 时只声明可见用例（用于制造元数据冲突）。"""
    cases = [{"input": VISIBLE_IN, "expected": VISIBLE_EXP, "hidden": False}]
    if hidden_cases:
        cases.append({"input": HIDDEN_IN, "expected": HIDDEN_EXP, "hidden": True})
    return {
        "id": "P001", "title": "求和", "statement": "读入两个整数，输出它们的和。",
        "examples": [], "knowledge_points": ["标准输入输出"], "difficulty": 1,
        "phase": phase, "reference_solution": "print(1)", "test_cases": cases,
    }


def _judge_with_hidden(holder=None, *, status="fail", error_type="runtime", stderr=RAW_ERR):
    """真形状判题结果：一条可见用例 ＋ 一条**带敏感标记的隐藏用例**。"""

    def _judge(question, code, language="python"):
        result = JudgeResult(
            status=status, total_cases=2, passed_cases=0,
            failed_cases=[
                {"case_index": 0, "input": VISIBLE_IN, "expected": VISIBLE_EXP,
                 "actual": VISIBLE_ACT, "hidden": False},
                {"case_index": 1, "input": HIDDEN_IN, "expected": HIDDEN_EXP,
                 "actual": HIDDEN_ACT, "hidden": True},
            ],
            error_type=error_type, stderr=stderr, stdout=HIDDEN_ACT,
            exec_ms=7, exit_code=1)
        if holder is not None:
            holder["result"] = result
        return result

    return _judge


def _judge_visible_only(holder=None, *, stderr=RAW_ERR):
    """真形状判题结果：**只含可见条目**（用于"无隐藏用例时原文照常回显"的正例）。"""

    def _judge(question, code, language="python"):
        result = JudgeResult(
            status="fail", total_cases=1, passed_cases=0,
            failed_cases=[{"case_index": 0, "input": VISIBLE_IN, "expected": VISIBLE_EXP,
                           "actual": VISIBLE_ACT, "hidden": False}],
            error_type="runtime", stderr=stderr, stdout="", exec_ms=7, exit_code=1)
        if holder is not None:
            holder["result"] = result
        return result

    return _judge


def _install(monkeypatch, *, question=None, judge=None, llm=None):
    monkeypatch.setattr(pipeline_mod, "load_question", lambda qid: question or _question())
    monkeypatch.setattr(pipeline_mod, "judge", judge if judge is not None else _judge_with_hidden())
    monkeypatch.setattr(llm_mod, "llm", llm if llm is not None else (lambda system, user: "先看看输入怎么读。"))


def _submit(monkeypatch, pid, *, code="print(0)", preset="exp", request_hint=False, **kw):
    _install(monkeypatch, **kw)
    return pipeline_mod.handle_submission(
        participant_id=pid, question_id="P001", code=code,
        config=PRESETS[preset], request_hint=request_hint)


def _hint(monkeypatch, pid, *, code="print(0)", preset="exp", **kw):
    _install(monkeypatch, **kw)
    return pipeline_mod.handle_hint_request(
        participant_id=pid, question_id="P001", code=code, config=PRESETS[preset])


def _assert_no_markers(blob, where):
    for marker in (HIDDEN_IN, HIDDEN_EXP, HIDDEN_ACT):
        assert marker not in blob, f"隐藏用例数据泄露到{where}：{marker}"


# ---------------------------------------------------------------- ① 学生响应

def test_hidden_case_payload_is_not_returned_to_student(monkeypatch):
    """隐藏用例的输入／期望／实际输出**一个都不许**出现在响应里；可见用例照旧。"""
    out = _submit(monkeypatch, "T_F_RESP_1")
    _assert_no_markers(json.dumps(out, ensure_ascii=False), "响应")
    cases = {c["case_index"]: c for c in out["failed_cases"]}
    assert cases[1]["hidden"] is True
    assert cases[1]["input"] == "" and cases[1]["expected"] == "" and cases[1]["actual"] == ""
    # 可见用例的公开信息必须**保留**（过滤不得顺手把能给的也删了）
    assert cases[0]["input"] == VISIBLE_IN
    assert cases[0]["expected"] == VISIBLE_EXP
    assert cases[0]["actual"] == VISIBLE_ACT


def test_unattributable_stderr_is_replaced_by_controlled_class_name(monkeypatch):
    """顶层 `stderr` 无法判断来源 → **保守不回显原文**，只留受控提取的异常类别名。"""
    out = _submit(monkeypatch, "T_F_RESP_2")
    assert HIDDEN_IN not in out["stderr"]
    assert "ValueError" in out["stderr"], "白名单内的异常类别名（Q4）仍应保留"
    assert "invalid literal" not in out["stderr"], "冒号后的原文一律不透传"
    # `error_type` 必须保留约定枚举语义，不得被异常类别名改写
    assert out["error_type"] == "runtime"


def test_judge_feedback_does_not_echo_raw_stderr(monkeypatch):
    """`judge_feedback` 是学生可见字段 → 同样不得复述原始异常正文。"""
    out = _submit(monkeypatch, "T_F_RESP_3")
    assert HIDDEN_IN not in out["judge_feedback"]
    assert "未通过测试" in out["judge_feedback"], "判题反馈本身仍要产出"


def test_plain_submit_without_hint_also_filters(monkeypatch):
    """未请求提示的普通提交（前后测之外）同样走过滤，不存在"只在提示路径过滤"。"""
    out = _submit(monkeypatch, "T_F_RESP_4", request_hint=False)
    assert out["hint_level"] == 0
    _assert_no_markers(json.dumps(out, ensure_ascii=False), "未请求提示的普通提交响应")


def test_facts_are_not_changed_by_filtering(monkeypatch):
    """过滤**只删不该给的**：判题状态、通过数量、耗时、日志事实一概不变。"""
    out = _submit(monkeypatch, "T_F_RESP_5")
    assert out["status"] == "fail"
    assert out["log"]["status"] == "fail"
    assert out["log"]["pass_status"] == 0
    assert out["log"]["error_type"] == "runtime"
    assert out["log"]["elapsed_ms"] == 7
    assert len(out["failed_cases"]) == 2, "隐藏用例条目本身要保留（学生需知道有隐藏用例未过）"


# ------------------------------------------------- ①-b 异常类别名只认有限白名单

def test_unknown_exception_name_is_not_passed_through(monkeypatch):
    """`WeirdError` 不在白名单 → **不得**因为"长得像异常名"就输出给学生。"""
    out = _submit(monkeypatch, "T_F_NAME_1",
                  judge=_judge_with_hidden(stderr=UNKNOWN_ERR))
    assert "WeirdError" not in out["stderr"], "自定义/未知异常名不得透传"
    assert redaction.FALLBACK_NOTE in out["stderr"], "应退回服务端固定说明"
    assert HIDDEN_IN not in out["stderr"]


def test_exception_name_carrying_marker_is_not_passed_through(monkeypatch):
    """异常名**本身**就是敏感标记 → 同样只能退回固定说明。"""
    out = _submit(monkeypatch, "T_F_NAME_2",
                  judge=_judge_with_hidden(stderr=MARKER_NAME_ERR))
    _assert_no_markers(json.dumps(out, ensure_ascii=False), "异常名带标记的响应")


def test_multiline_traceback_keeps_real_type_and_drops_message(monkeypatch):
    """多行 traceback：末行是真类型 → 只留类型名；消息正文与源码行里的标记都不得透传。"""
    out = _submit(monkeypatch, "T_F_NAME_3",
                  judge=_judge_with_hidden(stderr=TRACEBACK_REAL_TYPE))
    assert "ValueError" in out["stderr"]
    assert HIDDEN_IN not in out["stderr"], "源码行/消息正文里的隐藏输入不得随类型名带出"
    assert "sol.py" not in out["stderr"] and "Traceback" not in out["stderr"]


def test_multiline_traceback_with_fake_type_falls_back(monkeypatch):
    """多行 traceback：末行是**伪异常名** → 只能退回固定说明，不得自由搜索出别的名字。"""
    out = _submit(monkeypatch, "T_F_NAME_4",
                  judge=_judge_with_hidden(stderr=TRACEBACK_FAKE_TYPE))
    _assert_no_markers(json.dumps(out, ensure_ascii=False), "伪异常名的响应")
    assert redaction.FALLBACK_NOTE in out["stderr"]


# ------------------------------------------------- ①-c 元数据冲突：结果优先

def test_hidden_entry_is_filtered_even_when_question_declares_no_hidden(monkeypatch):
    """题目声明"无隐藏用例"，但**结果条目 `hidden=True`** → 该条目仍必须过滤。"""
    out = _submit(monkeypatch, "T_F_CONFLICT_1",
                  question=_question(hidden_cases=False),
                  judge=_judge_with_hidden())
    _assert_no_markers(json.dumps(out, ensure_ascii=False), "元数据冲突的响应")
    cases = {c["case_index"]: c for c in out["failed_cases"]}
    assert cases[1]["hidden"] is True
    assert cases[1]["input"] == "" and cases[1]["expected"] == "" and cases[1]["actual"] == ""
    # 结果里有隐藏条目 → 顶层报错同样按保守口径处理（不因题目声明而放宽）
    assert HIDDEN_IN not in out["stderr"]
    assert "ValueError" in out["stderr"]


def test_allowed_names_cover_classifier_rule_table():
    """白名单必须覆盖规则表能识别的全部异常名，否则分类得出、回显却说不出。"""
    from app.harness import error_classifier as ec
    for _cls, names in ec.RULE_TABLE:
        for name in names:
            assert name in redaction.ALLOWED_EXCEPTION_NAMES, f"{name} 不在异常类别白名单里"


def test_question_declares_hidden_but_result_is_visible_only_is_conservative(monkeypatch):
    """反向冲突：题目声明含隐藏用例、结果只有可见条目 → 顶层报错仍按保守处理。"""
    out = _submit(monkeypatch, "T_F_CONFLICT_2",
                  question=_question(hidden_cases=True),
                  judge=_judge_visible_only())
    assert HIDDEN_IN not in out["stderr"]
    assert "ValueError" in out["stderr"], "保守处理不是把信息删空，类型名照旧给出"


def test_visible_only_question_and_result_keeps_raw_stderr(monkeypatch):
    """真正**两边都只有可见条目** → 没有可泄露的数据，原文照常回显。"""
    out = _submit(monkeypatch, "T_F_VIS_1",
                  question=_question(hidden_cases=False),
                  judge=_judge_visible_only())
    assert RAW_ERR in out["stderr"], "无隐藏用例时不应把能给的原文也删掉"
    assert out["failed_cases"][0]["actual"] == VISIBLE_ACT


# --------------------------------------------- ①-d 两个入口 × 三个阶段

@pytest.mark.parametrize("phase", ["practice", "pretest", "posttest"])
def test_submit_entry_filters_in_all_phases(monkeypatch, phase):
    """正式提交入口：练习／前测／后测三个阶段都不得泄露隐藏用例数据。"""
    out = _submit(monkeypatch, f"T_F_PHASE_{phase}", question=_question(phase=phase))
    _assert_no_markers(json.dumps(out, ensure_ascii=False), f"{phase} 的提交响应")
    assert out["failed_cases"][1]["input"] == "" and out["failed_cases"][1]["actual"] == ""
    assert out["failed_cases"][0]["input"] == VISIBLE_IN


def test_hint_entry_filters_in_practice_phase(monkeypatch):
    """纯提示入口（练习阶段）：同样过滤，且不写提交日志。"""
    out = _hint(monkeypatch, "T_F_HINT_PRACTICE")
    _assert_no_markers(json.dumps(out, ensure_ascii=False), "纯提示入口的响应")
    assert out["log"] is None, "纯提示入口不写 submission_log"
    assert out["failed_cases"][1]["input"] == "" and out["failed_cases"][1]["actual"] == ""


@pytest.mark.parametrize("phase", ["pretest", "posttest"])
def test_hint_entry_in_gated_phase_does_not_call_judge(monkeypatch, phase):
    """纯提示入口（前后测）：提前返回、不调判题，也不得有隐藏数据。"""
    def _boom(*a, **k):
        raise AssertionError("前后测的纯提示入口不得调用判题")

    out = _hint(monkeypatch, f"T_F_HINT_{phase}",
                question=_question(phase=phase), judge=_boom)
    assert out["source"] == "phase_disabled"
    _assert_no_markers(json.dumps(out, ensure_ascii=False), f"{phase} 的纯提示响应")


# ---------------------------------------------------------------- ② 模型上下文

def test_classifier_model_prompt_gets_filtered_view(monkeypatch):
    """分类模型的 user 提示：不得出现隐藏输入，**也不得透传未知异常名**。"""
    captured = {}

    def fake_llm(system, user):
        captured.setdefault("calls", []).append((system, user))
        return '{"error_class": "边界", "knowledge_point": ""}'

    out = _submit(monkeypatch, "T_F_MODEL_1",
                  judge=_judge_with_hidden(error_type="runtime", stderr=UNKNOWN_ERR),
                  llm=fake_llm)
    assert captured.get("calls"), "本用例应走到大模型分类路径"
    for system, user in captured["calls"]:
        assert HIDDEN_IN not in user and HIDDEN_IN not in system, "隐藏输入进了分类模型的上下文"
        assert "WeirdError" not in user, "未知异常名不得因“像异常名”而提供给模型"
        assert redaction.FALLBACK_NOTE in user, "应改为提供固定说明"
    assert out["source"] == "harness"


def test_hint_model_prompt_gets_filtered_view(monkeypatch):
    """提示阶梯模型的 user 提示里不得出现隐藏输入。"""
    captured = {}

    def fake_llm(system, user):
        captured.setdefault("calls", []).append((system, user))
        return "先确认输入怎么读。"

    out = _submit(monkeypatch, "T_F_MODEL_2",
                  judge=_judge_with_hidden(), llm=fake_llm, request_hint=True)
    assert out["source"] == "harness"
    assert captured["calls"], "请求提示应走到提示模型"
    for system, user in captured["calls"]:
        _assert_no_markers(user, "提示模型的 user 提示")
        _assert_no_markers(system, "提示模型的 system 提示")


def test_raw_llm_fallback_prompt_gets_filtered_view(monkeypatch):
    """裸大模型兜底（无任何教学组件）路径同样过滤。"""
    captured = {}

    def fake_llm(system, user):
        captured.setdefault("calls", []).append((system, user))
        return "裸模型回答"

    out = _submit(monkeypatch, "T_F_MODEL_3",
                  judge=_judge_with_hidden(), llm=fake_llm, preset="ctrl")
    assert out["source"] == "raw_llm"
    for system, user in captured["calls"]:
        _assert_no_markers(user, "裸模型兜底的 user 提示")


def test_every_model_call_receives_filtered_context(monkeypatch):
    """**收集全部** `system`／`user`（不只最后一次调用），逐一核对三个标记都不出现。"""
    calls = []

    def fake_llm(system, user):
        calls.append((system, user))
        return '{"error_class": "边界", "knowledge_point": ""}'

    out = _submit(monkeypatch, "T_F_MODEL_ALL",
                  judge=_judge_with_hidden(stderr=UNKNOWN_ERR),
                  llm=fake_llm, request_hint=True)
    assert len(calls) >= 2, "本用例应同时走到分类模型与提示模型两条路径"
    for system, user in calls:
        _assert_no_markers(system, "模型 system 提示")
        _assert_no_markers(user, "模型 user 提示")
    _assert_no_markers(json.dumps(out, ensure_ascii=False), "响应")


# ---------------------------------------------------------------- ③ 日志

def test_event_log_does_not_store_filtered_raw_text(monkeypatch):
    """过滤掉的原文不得转存进事件表 `reason` 等字段。"""
    pid = "T_F_LOG_1"
    _submit(monkeypatch, pid)
    rows = db.get_events(participant_id=pid)
    _assert_no_markers(json.dumps(rows, ensure_ascii=False, default=str), "事件表")


# ---------------------------------------------------------------- ④ 不改原始结果

def test_original_judge_result_is_not_mutated(monkeypatch):
    """判题器的原始结果**不得被原地修改**（岗位③的结果是原始事实，验收工具还要用）。"""
    holder = {}
    _submit(monkeypatch, "T_F_RAW_1", judge=_judge_with_hidden(holder))
    raw = holder["result"]
    assert raw.stderr == RAW_ERR
    assert raw.stdout == HIDDEN_ACT
    assert raw.failed_cases[1]["input"] == HIDDEN_IN
    assert raw.failed_cases[1]["expected"] == HIDDEN_EXP
    assert raw.failed_cases[1]["actual"] == HIDDEN_ACT
