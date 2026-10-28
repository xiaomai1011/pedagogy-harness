"""提示阶梯修订（2026-10-28）：主动请求可升级、幂等回放、切题/回题、成功后不自动升级。

这些是**状态转换测试**——先在这里把评阅要求的情形固定下来，再让实现去满足。
"""

from app import db
from app import llm as llm_mod
from app.harness.config_switch import PRESETS
from app.harness.pipeline import handle_hint_request, handle_submission


def _stub_llm(monkeypatch):
    calls = {"n": 0}

    def fake(system, user):
        calls["n"] += 1
        return "先看看输入是什么。"

    monkeypatch.setattr(llm_mod, "llm", fake)
    return calls


def _hint(pid, qid, rid):
    return handle_hint_request(participant_id=pid, question_id=qid, code="print(1)",
                               config=PRESETS["exp"], hint_request_id=rid)


def test_first_request_gives_l1_then_each_new_request_raises_one(monkeypatch):
    _stub_llm(monkeypatch)
    pid = "T_LADDER_A"
    assert _hint(pid, "P001", "r1")["hint_level"] == 1
    assert _hint(pid, "P001", "r2")["hint_level"] == 2
    assert _hint(pid, "P001", "r3")["hint_level"] == 3
    assert _hint(pid, "P001", "r4")["hint_level"] == 4
    assert _hint(pid, "P001", "r5")["hint_level"] == 4      # 封顶


def test_same_request_id_replays_without_upgrade_or_judge(monkeypatch):
    """刷新页面/网络重试：同一个 request_id 直接回放，不升级、不再调模型。"""
    calls = _stub_llm(monkeypatch)
    pid = "T_LADDER_B"
    first = _hint(pid, "P001", "same")
    assert first["hint_level"] == 1
    before = calls["n"]

    again = _hint(pid, "P001", "same")
    assert again["replayed"] is True
    assert again["hint_level"] == 1
    assert again["message"] == first["message"]
    assert calls["n"] == before, "回放不得再调用模型"


def test_new_question_does_not_inherit_and_old_question_keeps_history(monkeypatch):
    _stub_llm(monkeypatch)
    pid = "T_LADDER_C"
    _hint(pid, "P001", "a1")
    _hint(pid, "P001", "a2")
    assert _hint(pid, "P002", "b1")["hint_level"] == 1, "新题不继承另一题等级"
    assert _hint(pid, "P001", "a3")["hint_level"] == 3, "回到旧题按该题历史继续"


def test_success_does_not_auto_upgrade(monkeypatch):
    _stub_llm(monkeypatch)
    from app.harness import pipeline as pipeline_mod
    from app.judge.runner import JudgeResult

    monkeypatch.setattr(pipeline_mod, "judge", lambda question, code, language="python": JudgeResult(
        status="pass", total_cases=1, passed_cases=1, failed_cases=[], error_type="none",
        stderr="", stdout="", exec_ms=5, exit_code=0))
    pid = "T_LADDER_D"
    _hint(pid, "P001", "s1")
    state_before = db.get_hint_state(pid, "P001")
    out = handle_submission(participant_id=pid, question_id="P001",
                            code="print(3)", config=PRESETS["exp"], request_hint=False)
    assert out["status"] == "pass"
    state_after = db.get_hint_state(pid, "P001")
    assert state_after["unlocked_level"] == state_before["unlocked_level"], "成功后不自动升级"
    assert state_after["displayed_level"] == state_before["displayed_level"], "未请求不改变展示"


def test_pure_hint_does_not_touch_submissions_or_memory(monkeypatch):
    _stub_llm(monkeypatch)
    pid = "T_LADDER_E"
    with db.connect() as conn:
        subs = conn.execute("SELECT COUNT(*) n FROM submission_log WHERE participant_id=?",
                            (pid,)).fetchone()["n"]
        mem = conn.execute("SELECT COUNT(*) n FROM error_memory WHERE participant_id=?",
                           (pid,)).fetchone()["n"]
    _hint(pid, "P001", "p1")
    _hint(pid, "P001", "p2")
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) n FROM submission_log WHERE participant_id=?",
                            (pid,)).fetchone()["n"] == subs
        assert conn.execute("SELECT COUNT(*) n FROM error_memory WHERE participant_id=?",
                            (pid,)).fetchone()["n"] == mem
