"""组件开关：记忆开、分类关时，记忆仍要工作（记录"未分类+可信判题类别"）。"""

from app import db
from app import llm as llm_mod
from app.harness import error_memory as em
from app.harness import pipeline as pipeline_mod
from app.harness.config_switch import PRESETS
from app.harness.pipeline import handle_submission
from app.judge.runner import JudgeResult


def _fail_judge(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "judge", lambda question, code, language="python": JudgeResult(
        status="fail", total_cases=1, passed_cases=0,
        failed_cases=[{"case_index": 1, "input": "1", "expected": "2", "actual": "0", "hidden": False}],
        error_type="wrong_output", stderr="", stdout="0", exec_ms=5, exit_code=0))


def _rows(pid):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM error_memory WHERE participant_id=? ORDER BY count DESC", (pid,)).fetchall()]


def test_memory_records_unclassified_with_judge_category(monkeypatch):
    """abl_cls（分类器关、记忆开）：两次失败后应留下明确标记的"未分类"记录。"""
    _fail_judge(monkeypatch)
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "提示")
    pid = "T_MEM_SWITCH_A"
    for _ in range(2):
        handle_submission(participant_id=pid, question_id="P001", code="print(0)",
                          config=PRESETS["abl_mem"])
    rows = _rows(pid)
    assert rows, "记忆必须工作（不能因为分类器关掉就静默不记账）"
    row = rows[0]
    assert row["error_class"] == "未分类"
    assert row["knowledge_point"] == ""
    assert row["judge_error_type"] == "wrong_output"
    assert row["count"] == 2
    hint = em.preventive_hint(pid)
    assert "未分类" in hint and "wrong_output" in hint


def test_memory_off_records_nothing(monkeypatch):
    """abl_cls（只开分类器、记忆关）：不应写任何记忆行。"""
    _fail_judge(monkeypatch)
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "提示")
    pid = "T_MEM_SWITCH_B"
    for _ in range(3):
        handle_submission(participant_id=pid, question_id="P001", code="print(0)",
                          config=PRESETS["abl_cls"])
    assert _rows(pid) == []


def test_pass_does_not_touch_memory(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "judge", lambda question, code, language="python": JudgeResult(
        status="pass", total_cases=1, passed_cases=1, failed_cases=[], error_type="none",
        stderr="", stdout="", exec_ms=5, exit_code=0))
    pid = "T_MEM_SWITCH_C"
    handle_submission(participant_id=pid, question_id="P001", code="print(1)",
                      config=PRESETS["abl_mem"])
    assert _rows(pid) == []


def test_existing_history_still_readable_when_classifier_off(monkeypatch):
    """已有历史库：分类器关掉后，预防性提示仍能读出来（不依赖本次分类）。"""
    _fail_judge(monkeypatch)
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "提示")
    pid = "T_MEM_SWITCH_D"
    for _ in range(2):
        handle_submission(participant_id=pid, question_id="P001", code="print(0)",
                          config=PRESETS["exp"])
    before = em.preventive_hint(pid)
    assert before
    # 再关掉分类器跑一次：历史仍在，提示仍能产出
    handle_submission(participant_id=pid, question_id="P002", code="print(0)",
                      config=PRESETS["abl_mem"])
    after = em.preventive_hint(pid)
    assert "反复出错" in after

