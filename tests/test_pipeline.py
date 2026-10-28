"""主干端到端：开关真的改变行为；防泄答案在校验层真的生效；口径②的解锁/展示分离。"""

from app import db
from app import llm as llm_mod
from app.harness import pipeline as pipeline_mod
from app.harness.config_switch import PRESETS
from app.harness.pipeline import handle_hint_request, handle_submission


def _log_count(participant_id: str) -> int:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM submission_log WHERE participant_id=?",
            (participant_id,),
        ).fetchone()
    return int(row["n"])


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


def test_submit_without_request_never_shows_hint(monkeypatch):
    """口径②：没请求就不展示提示（哪怕大模型能生成）。"""
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "这段提示不该出现在界面上")
    out = handle_submission(participant_id="T_E2E_4", question_id="P001",
                            code="print(1)", config=PRESETS["exp"], request_hint=False)
    assert out["hint_level"] == 0
    assert "这段提示不该出现在界面上" not in out["message"]


def test_two_consecutive_failures_unlock_but_do_not_show(monkeypatch):
    """连败 2 次只解锁：日志里 `hint_unlocked_level=1`，但 `hint_level` 仍是 0。"""
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "提示正文")
    pid = "T_E2E_5"
    handle_submission(participant_id=pid, question_id="P001", code="print(1)",
                      config=PRESETS["exp"])
    out = handle_submission(participant_id=pid, question_id="P001", code="print(1)",
                            config=PRESETS["exp"])
    assert out["hint_level"] == 0
    assert out["log"]["hint_unlocked_level"] == 1


def test_submission_log_carries_phase(monkeypatch):
    """`phase` 由题库元数据自描述；题库没写时兜底 `practice`，且前端传不进来。"""
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "提示正文")
    out = handle_submission(participant_id="T_E2E_6", question_id="P001",
                            code="print(1)", config=PRESETS["exp"])
    assert out["log"]["phase"] == "practice"


def test_hint_request_does_not_write_submission_log(monkeypatch):
    """只看提示 → **不落库**，`submit_seq` 不被虚增（契约 §3）。"""
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "先确认这一步的输入是什么。")
    pid = "T_HINT_1"
    before = _log_count(pid)
    out = handle_hint_request(participant_id=pid, question_id="P001",
                              code="print(1)", config=PRESETS["exp"])
    assert out["log"] is None
    assert out["hint_level"] == 1
    assert _log_count(pid) == before


def test_repeated_hint_requests_raise_one_level_each(monkeypatch):
    """口径（2026-10-28 修订）：每次**新的**主动请求最多升一级，且不因连败叠加跳两级。

    该被试连续 3 次失败且每次请求：连败每次都解锁不到新等级（fails=1/2/3 → 上限 0/1/1），
    请求逐级推进 → 展示 1、2、3。
    """
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "提示正文")
    pid = "T_E2E_7"
    levels = []
    for _ in range(3):
        out = handle_submission(participant_id=pid, question_id="P001", code="print(1)",
                                config=PRESETS["exp"], request_hint=True)
        levels.append(out["hint_level"])
    assert levels == [1, 2, 3]
    assert out["log"]["hint_unlocked_level"] == 3


# ---------------------------------------------------------------------------
# D · 按**入口**区分"是否记一次正式失败"（2026-10-17 定案）
#
# 病因：`_run_pipeline` 对**所有**入口都执行 `consecutive_fail += 1`，
# 而 `error_memory` 也在两个入口都 `remember()`。
# 于是纯提示请求被当成一次新失败 + 一次新错误 → 等级越级、记忆虚增。
#
# 下面四组是 D 的验收条件，**先确认红**再改 `pipeline.py`。
# 用假判题器（而非真桩）：真桩会被岗位③替换掉，这组回归要长期稳定。
# ---------------------------------------------------------------------------

def _fake_fail_judge(monkeypatch, stderr="NameError: name 'x' is not defined"):
    """把主干调用的 `judge` 换成"必定失败且可分类"的真形状结果（归类「语法」）。"""
    from app.judge.runner import JudgeResult
    monkeypatch.setattr(pipeline_mod, "judge", lambda question, code, language="python": JudgeResult(
        status="fail", total_cases=3, passed_cases=0,
        failed_cases=[{"case_index": 1, "input": "1", "expected": "2",
                       "actual": "0", "hidden": False}],
        error_type="runtime", stderr=stderr, stdout="", exec_ms=10, exit_code=1,
    ))


def _memory_total(participant_id: str) -> int:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(count), 0) AS n FROM error_memory WHERE participant_id=?",
            (participant_id,),
        ).fetchone()
    return int(row["n"])


def test_d1_hint_request_does_not_bump_error_memory(monkeypatch):
    """D-①：纯提示请求**不增加**错误记忆（同一份代码反复看提示，`count` 不变）。"""
    _fake_fail_judge(monkeypatch)
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "提示正文")
    pid = "T_D_MEM_1"
    handle_submission(participant_id=pid, question_id="P001", code="print(1)",
                      config=PRESETS["exp"])
    after_submit = _memory_total(pid)
    assert after_submit >= 1, "正式失败本应记账一次"
    for _ in range(3):
        handle_hint_request(participant_id=pid, question_id="P001",
                            code="print(1)", config=PRESETS["exp"])
    assert _memory_total(pid) == after_submit, "纯提示请求不得再记一次错误"


def test_d2_hint_request_does_not_count_extra_failure(monkeypatch):
    """D-②：纯提示**不额外计算一次失败**——展示等级必须＝已解锁上限，不得越级。"""
    _fake_fail_judge(monkeypatch)
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "提示正文")
    pid = "T_D_CNT_1"
    for _ in range(3):                       # 3 次正式失败 → 解锁上限 L1
        handle_submission(participant_id=pid, question_id="P001",
                          code="print(1)", config=PRESETS["exp"])
    out = handle_hint_request(participant_id=pid, question_id="P001",
                              code="print(1)", config=PRESETS["exp"])
    assert out["unlocked_level"] == 1
    assert out["hint_level"] == 1            # 若额外 +1 会越级到 L2


def test_d3_formal_failure_boundaries_do_not_escalate_on_hint(monkeypatch):
    """D-③：3／5／7 次正式失败后，一次纯提示**不得提前升级**（跨门槛不越级）。"""
    _fake_fail_judge(monkeypatch)
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "提示正文")
    for n, expected in ((3, 1), (5, 2), (7, 3)):
        pid = f"T_D_EDGE_{n}"
        for _ in range(n):
            handle_submission(participant_id=pid, question_id="P001",
                              code="print(1)", config=PRESETS["exp"])
        out = handle_hint_request(participant_id=pid, question_id="P001",
                                  code="print(1)", config=PRESETS["exp"])
        assert out["hint_level"] == expected, f"{n} 次失败后纯提示应展示 L{expected}"


def test_d4_submission_with_hint_counts_exactly_once(monkeypatch):
    """D-④：正式提交**即使带 `request_hint=true`**，也只记一次失败。

    该条是**修法红线**的守卫：不得图省事用 `ctx.request_hint` 区分入口
    （`/api/submit` 也接受 `request_hint=true`），否则这组会红。
    """
    _fake_fail_judge(monkeypatch)
    monkeypatch.setattr(llm_mod, "llm", lambda system, user: "提示正文")
    pid = "T_D_SUBHINT_1"
    out = handle_submission(participant_id=pid, question_id="P001", code="print(1)",
                            config=PRESETS["exp"], request_hint=True)
    assert out["hint_level"] == 1
    assert _log_count(pid) == 1
    assert _memory_total(pid) == 1
    handle_submission(participant_id=pid, question_id="P001", code="print(1)",
                      config=PRESETS["exp"], request_hint=True)
    assert _log_count(pid) == 2
    assert _memory_total(pid) == 2
