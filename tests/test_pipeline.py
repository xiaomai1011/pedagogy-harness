"""主干端到端：开关真的改变行为；防泄答案在校验层真的生效。"""

from app import llm as llm_mod
from app.harness.config_switch import PRESETS
from app.harness.pipeline import handle_submission


def test_exp_with_request_gives_level1(monkeypatch):
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "先确认输入是什么。")
    out = handle_submission(participant_id="T_E2E_1", question_id="P001",
                            code="print(1)", config=PRESETS["exp"], request_hint=True)
    assert out["source"] == "harness"
    assert out["hint_level"] == 1
    assert "先确认输入是什么。" in out["message"]


def test_pipeline_blocks_answer_leak_end_to_end(monkeypatch):
    """模型输出整段代码 → 主干返回给学生的文本里必须没有代码块。"""
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "```python\nprint(1)\n```")
    out = handle_submission(participant_id="T_E2E_2", question_id="P001",
                            code="print(1)", config=PRESETS["exp"], request_hint=True)
    assert "```" not in out["message"]


def test_ctrl_is_bare_llm(monkeypatch):
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "裸模型回答")
    out = handle_submission(participant_id="T_E2E_3", question_id="P001",
                            code="print(1)", config=PRESETS["ctrl"])
    assert out["source"] == "raw_llm"
    assert out["message"] == "裸模型回答"
