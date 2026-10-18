"""Q1／C／I 的**离线失败路径**测试（先红后绿）。

依据：`docs/contracts.md` **§6 事件表 `event_log`**（2026-10-18 第三次修订）。

本文件**全部离线**：判题器用假实现替换、模型用固定文本替换，**不联网**；
数据库用 conftest 的临时库（`PH_DB`），**不碰真实 `pedagogy.sqlite`**。

覆盖：

| 测试 | 覆盖 |
|---|---|
| `test_stage2_reentry_does_not_double_count_memory` | **第 2 步重复进入不增加记忆**（§6.3 硬规则 2 的幂等闸门） |
| `test_stage2_db_failure_rolls_back_half_write` | **写了但未提交即失败 → 全部回滚**，且判题事实仍在（§6.3 硬规则 3） |
| `test_hint_judge_failure_marks_aborted_and_unavailable` | judge 抛异常 → 执行 `aborted` ＋ 提示 `unavailable`（§6.5） |
| `test_fallback_hint_is_not_recorded_as_failed` | 防泄两次违规走兜底 → **有产出**，不记 `failed`（§6.3 硬规则 4） |
| `test_expected_model_failure_degrades_to_fallback` | **可预期**模型故障 → 受控兜底；执行仍 `finished`（§6.3 硬规则 4） |
| `test_unexpected_failure_must_not_be_swallowed` | **未预期**异常必须暴露，且**不覆盖**已完成的执行终态（§6.5 末条） |
| `test_phase_gating_three_entries[pretest/posttest]` | **前后测三入口**：仍判题、仍记提交、教学 `phase_disabled`、**不调模型**、关联一致（§6.4） |

> 说明：`80 passed` 只说明**既有测试覆盖的行为未发现回归**，不代表所有行为都没有问题。
"""

import json
import sqlite3

import pytest

from app import db
from app import llm as llm_mod
from app.harness import error_memory as em
from app.harness import pipeline as pipeline_mod
from app.harness.config_switch import PRESETS
from app.judge.runner import JudgeResult


class APITimeoutError(Exception):
    """模拟**可预期**的模型超时（按类名识别，不依赖 openai）。"""


# ---------------------------------------------------------------- 夹具

def _judge_fail(q, code, language="python"):
    """固定失败、可分类（NameError → 语法）。"""
    return JudgeResult(status="fail", total_cases=1, passed_cases=0,
                       failed_cases=[{"case_index": 0, "input": "1 2", "expected": "3",
                                      "actual": "-", "hidden": False}],
                       error_type="runtime", stderr="NameError: name 'b' is not defined",
                       stdout="", exec_ms=5, exit_code=1)


def _judge_boom(*a, **k):
    raise RuntimeError("judge exploded")


def _stub_llm(text="先看看输入怎么读。", *, exc=None):
    def _fn(*a, **k):
        if exc is not None:
            raise exc
        return text
    return _fn


def _events(pid, event_type=None):
    rows = db.get_events(participant_id=pid)
    return [r for r in rows if r["event_type"] == event_type] if event_type else rows


def _one(pid, event_type):
    rows = _events(pid, event_type)
    assert len(rows) == 1, f"{event_type} 事件应有且仅有一条，实际 {len(rows)} 条"
    return rows[0]


def _memory_total(pid):
    return sum(int(r["count"]) for r in db.get_error_memory(pid))


def _last_submission(pid):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM submission_log WHERE participant_id=? ORDER BY id DESC LIMIT 1",
            (pid,)).fetchone()
    return dict(row) if row else None


def _question():
    return pipeline_mod.load_question("P001")


# ---------------------------------------------------------------- I：第 2 步幂等

def test_stage2_reentry_does_not_double_count_memory(monkeypatch):
    """**重复进入第 2 步**（不是重复收尾）也不得再次增加错误记忆。"""
    monkeypatch.setattr(pipeline_mod, "judge", _judge_fail)
    monkeypatch.setattr(llm_mod, "llm", _stub_llm())
    pid = "T_EV_IDEM_1"

    pipeline_mod.handle_submission(participant_id=pid, question_id="P001",
                                   code="print(1)", config=PRESETS["exp"])
    after_first = _memory_total(pid)
    assert after_first >= 1, "首次提交本应记一次错误（分类=语法）"

    rid = _one(pid, "teaching")["request_id"]
    for _ in range(3):
        assert pipeline_mod.stage_classify_and_remember(
            request_id=rid, question=_question(), code="print(1)",
            judge_result=_judge_fail(None, ""), ts="2026-10-18T10:00:00+08:00") is None, \
            "第 2 步已经做过，重复进入应当**整体跳过**"

    assert _memory_total(pid) == after_first, "重复进入第 2 步不得再增记忆计数"


# ---------------------------------------------------------------- I：事务原子性

def test_stage2_db_failure_rolls_back_half_write(monkeypatch):
    """**已写入但事务尚未提交**时失败 → 分类、记忆、进度标记**全部回滚**；

    而**第一步已提交的判题事实仍在**（契约 §6.3 硬规则 1／3）。
    """
    monkeypatch.setattr(pipeline_mod, "judge", _judge_fail)
    monkeypatch.setattr(llm_mod, "llm", _stub_llm())
    pid = "T_EV_ATOMIC_1"

    real_upsert = db.upsert_error_memory_conn

    def _write_then_boom(conn, *a, **k):
        real_upsert(conn, *a, **k)          # 真的先把记忆写进去（同一事务、未提交）
        raise RuntimeError("commit 前失败")

    monkeypatch.setattr(db, "upsert_error_memory_conn", _write_then_boom)

    with pytest.raises(RuntimeError):
        pipeline_mod.handle_submission(participant_id=pid, question_id="P001",
                                       code="print(1)", config=PRESETS["exp"])

    # 第一组事务已提交 → 判题事实必须在
    sub = _last_submission(pid)
    assert sub is not None, "判题事实应先落库，且不得被第 2 步失败丢掉"
    assert _one(pid, "execution")["status"] == "finished"
    # 第二组事务整体回滚 → 三样都不能留半套
    assert sub["error_class"] == "未分类", "分类字段不得被半途回填"
    assert sub["error_kp"] == "", "知识点不得被半途回填"
    assert db.get_error_memory(pid) == [], "记忆计数不得落下一半"
    teaching = _one(pid, "teaching")
    assert teaching["progress"] == "", "进度标记不得被置上（否则下次会误判为已完成）"
    assert teaching["status"] in ("pending", "failed"), "教学态须反映未完成，不得记 done"


# ---------------------------------------------------------------- Q1：judge 异常

def test_hint_judge_failure_marks_aborted_and_unavailable(monkeypatch):
    """纯提示入口 judge 抛异常 → 执行记 `aborted`，已建的提示事件收尾 `unavailable`。"""
    monkeypatch.setattr(pipeline_mod, "judge", _judge_boom)
    pid = "T_EV_JBOOM_1"

    with pytest.raises(RuntimeError):
        pipeline_mod.handle_hint_request(participant_id=pid, question_id="P001",
                                         code="print(1)", config=PRESETS["exp"])

    assert _one(pid, "execution")["status"] == "aborted"
    assert _one(pid, "hint")["status"] == "unavailable"
    assert _events(pid, "teaching") == [], "纯提示不产生 teaching 事件（§6.5）"


# ---------------------------------------------------------------- I：兜底不算失败

def test_fallback_hint_is_not_recorded_as_failed(monkeypatch):
    """模型两次都违规 → 走 `leak_guard` 无模板兜底；**有产出**，故不得记 `failed`。"""
    monkeypatch.setattr(pipeline_mod, "judge", _judge_fail)
    monkeypatch.setattr(llm_mod, "llm", _stub_llm("```python\nprint(1)\n```"))
    pid = "T_EV_FALLBACK_1"

    pipeline_mod.handle_submission(participant_id=pid, question_id="P001",
                                   code="print(1)", config=PRESETS["exp"], request_hint=True)

    hint = _one(pid, "hint")
    assert hint["status"] == "returned_fallback", "兜底属'有产出（降级）'，不是生成失败"
    assert hint["hint_source"] == "fallback"
    assert _one(pid, "teaching")["status"] != "failed", "有可用兜底提示时不得整体记 failed"


def test_expected_model_failure_degrades_to_fallback(monkeypatch):
    """**可预期**的模型故障（超时）→ **受控降级**：返回可用兜底，执行事件仍 `finished`。

    不得为了让测试通过而把本应受控降级的故障改成报错。
    """
    monkeypatch.setattr(pipeline_mod, "judge", _judge_fail)
    monkeypatch.setattr(llm_mod, "llm", _stub_llm(exc=APITimeoutError("timeout")))
    pid = "T_EV_TIMEOUT_1"

    out = pipeline_mod.handle_submission(participant_id=pid, question_id="P001",
                                         code="print(1)", config=PRESETS["exp"],
                                         request_hint=True)

    assert out["message"], "受控降级仍须给学生可用反馈"
    assert _one(pid, "execution")["status"] == "finished", "判题已完成，不受模型故障影响"
    hint = _one(pid, "hint")
    assert hint["status"] == "returned_fallback"
    assert hint["hint_source"] == "fallback"
    assert _one(pid, "teaching")["status"] != "failed", "有兜底产出时不得记 failed"


def test_unexpected_failure_must_not_be_swallowed(monkeypatch):
    """**未预期**异常必须暴露；且**不得覆盖**已完成的执行终态（§6.5 末条）。"""
    monkeypatch.setattr(pipeline_mod, "judge", _judge_fail)
    monkeypatch.setattr(llm_mod, "llm", _stub_llm(exc=RuntimeError("model down")))
    pid = "T_EV_ORDER_1"

    with pytest.raises(RuntimeError):
        pipeline_mod.handle_submission(participant_id=pid, question_id="P001", code="print(1)",
                                       config=PRESETS["exp"], request_hint=True)

    assert _one(pid, "execution")["status"] == "finished", "判题已返回，不得被模型异常改写"
    assert _one(pid, "teaching")["status"] in ("partial", "failed"), "教学态须反映未完成"
    assert _one(pid, "hint")["status"] == "unavailable"


# ---------------------------------------------------------------- B：前后测三入口

@pytest.mark.parametrize("phase", ["pretest", "posttest"])
def test_phase_gating_three_entries(monkeypatch, phase):
    """前后测：**禁用教学帮助 ≠ 拒绝正式提交**；三入口各记什么，且**完全不调模型**。"""
    judge_calls = []
    llm_calls = []

    def _counting_judge(q, code, language="python"):
        judge_calls.append(1)
        return _judge_fail(q, code)

    monkeypatch.setattr(pipeline_mod, "judge", _counting_judge)
    monkeypatch.setattr(llm_mod, "llm", lambda *a, **k: (llm_calls.append(1), "文本")[1])

    question = dict(pipeline_mod.load_question("P001"))
    question["phase"] = phase
    monkeypatch.setattr(pipeline_mod, "load_question", lambda qid: dict(question))

    # ① 普通提交：仍判题、仍记提交；教学记 phase_disabled
    pid1 = f"T_EV_{phase.upper()}_1"
    out1 = pipeline_mod.handle_submission(participant_id=pid1, question_id="P001",
                                          code="print(1)", config=PRESETS["exp"])
    assert _one(pid1, "execution")["status"] == "finished", "前后测仍须正常判题"
    assert _last_submission(pid1) is not None, "前后测仍须记录正式提交"
    assert _one(pid1, "teaching")["status"] == "phase_disabled"
    assert _events(pid1, "hint") == [], "未请求提示则不得凭空创建提示事件"
    # 返回内容：不得含教学提示与教学诊断
    assert out1["message"] == pipeline_mod.PHASE_DISABLED_MESSAGE
    assert out1["hint_level"] == 0 and out1["memory_hint"] == ""
    assert out1["error_class"] == "" and out1["knowledge_point"] == ""

    # ② 提交带提示：仍判题、仍记提交；提示事件记 phase_disabled；关联要一致
    pid2 = f"T_EV_{phase.upper()}_2"
    pipeline_mod.handle_submission(participant_id=pid2, question_id="P001", code="print(1)",
                                   config=PRESETS["exp"], request_hint=True)
    assert _one(pid2, "execution")["status"] == "finished"
    sub2 = _last_submission(pid2)
    assert sub2 is not None
    exec2, hint2, teach2 = (_one(pid2, "execution"), _one(pid2, "hint"), _one(pid2, "teaching"))
    assert exec2["status"] == "finished"
    assert hint2["status"] == "phase_disabled"
    assert teach2["status"] == "phase_disabled"
    assert exec2["request_id"] == hint2["request_id"] == teach2["request_id"], \
        "同一请求的三类事件必须共用同一个 request_id"
    assert exec2["submission_id"] == sub2["id"], "执行事件须回填本请求的 submission_id"
    assert hint2["submission_id"] == sub2["id"], "**提示事件也要回填** submission_id（§6.5）"
    for ev in (hint2, teach2):
        assert ev["ts_end"], "`phase_disabled` 一建就是终态，必须同时填好结束时间"

    # ③ 纯提示：直接禁用、**提前返回、不调 judge**
    pid3 = f"T_EV_{phase.upper()}_3"
    out3 = pipeline_mod.handle_hint_request(participant_id=pid3, question_id="P001",
                                            code="print(1)", config=PRESETS["exp"])
    assert _one(pid3, "hint")["status"] == "phase_disabled"
    assert _one(pid3, "hint")["ts_end"], "纯提示的禁用事件也须填结束时间"
    assert _events(pid3, "execution") == [], "前后测纯提示不得产生执行事件（未调 judge）"
    assert out3["message"] == pipeline_mod.PHASE_DISABLED_HINT_MESSAGE, \
        "纯提示入口没有正式提交，不得沿用“提交已正常记录”的说法"
    assert out3["hint_level"] == 0

    # 计数与记忆：两次正式提交各判一次题，纯提示**零次**；三个参与者都不得更新记忆
    assert len(judge_calls) == 2, f"应只判题 2 次，实际 {len(judge_calls)} 次"
    assert llm_calls == [], "前后测**完全不得调用模型**"
    for pid in (pid1, pid2, pid3):
        assert db.get_error_memory(pid) == [], f"{pid} 前后测不得更新错误记忆"
    assert _last_submission(pid1)["error_class"] == "语法", \
        "前后测只用**规则**做内部记录（不调模型），规则命中时仍留确定分类"


# ---------------------------------------------------------------- 保持既有消融语义

def test_ctrl_group_spends_no_extra_model_call_for_classification(monkeypatch):
    """对照组（`ctrl`）不做任何教学处理 → **不因分类多花一次模型调用**，`error_class` 仍为「未分类」。"""
    calls = []
    monkeypatch.setattr(pipeline_mod, "judge", _judge_fail)
    monkeypatch.setattr(llm_mod, "llm", lambda *a, **k: (calls.append(1), "裸模型回答")[1])
    pid = "T_EV_CTRL_1"

    out = pipeline_mod.handle_submission(participant_id=pid, question_id="P001",
                                         code="print(1)", config=PRESETS["ctrl"])

    assert out["source"] == "raw_llm"
    assert len(calls) == 1, f"ctrl 只应有裸模型兜底那一次调用，实际 {len(calls)} 次"
    assert _last_submission(pid)["error_class"] == "未分类", "对照组不得被回填教学分类"
    assert db.get_error_memory(pid) == [], "对照组不得更新错误记忆"


# ---------------------------------------------------------------- 收尾必须是一个短事务

def test_finalize_failure_rolls_back_half_write(monkeypatch):
    """**收尾中途写入失败** → 提示事件、教学终态、提交行字段**整体回滚**（不留半套）。

    故障点：第 4 步回填提交行时"先真写、再抛"（第 1 次写是第 2 步，第 2 次才是收尾）。
    """
    monkeypatch.setattr(pipeline_mod, "judge", _judge_fail)
    monkeypatch.setattr(llm_mod, "llm", _stub_llm())
    pid = "T_EV_FIN_ATOMIC_1"

    real_update = db.update_submission_conn
    seen = {"n": 0}

    def _write_then_boom(conn, *a, **k):
        seen["n"] += 1
        real_update(conn, *a, **k)          # 真的先写进同一个事务
        if seen["n"] >= 2:                  # 第 2 次＝收尾那次
            raise RuntimeError("收尾写入失败")
    monkeypatch.setattr(db, "update_submission_conn", _write_then_boom)

    with pytest.raises(RuntimeError):
        pipeline_mod.handle_submission(participant_id=pid, question_id="P001",
                                       code="print(1)", config=PRESETS["exp"],
                                       request_hint=True)

    # 收尾事务整体回滚：**不得**留下"提示已记成功、教学已记 done，但提交行没回填"的半套
    assert _one(pid, "hint")["status"] == "unavailable", "回滚后应走失败收尾，不得停在已成功态"
    assert _one(pid, "teaching")["status"] == "partial", \
        "教学态须按已完成进度记录（第 2 步已提交），不得记 done"
    assert _last_submission(pid)["error_class"] == "语法", "第 2 步已提交的分类必须保留"
    assert _last_submission(pid)["hint_level"] == 0, "收尾的提示字段不得只写进去一半"


# ---------------------------------------------------------------- 分类阶段的异常收尾

def _judge_unclassifiable(q, code, language="python"):
    """规则**必然不命中**（无异常名、error_type 非 timeout/syntax/wrong_output）→ 分类会走模型。"""
    return JudgeResult(status="fail", total_cases=1, passed_cases=0,
                       failed_cases=[{"case_index": 0, "input": "1 2", "expected": "3",
                                      "actual": "-", "hidden": False}],
                       error_type="runtime", stderr="", stdout="", exec_ms=3, exit_code=1)


def test_classify_model_failure_is_finalized(monkeypatch):
    """规则未命中 → 分类要调模型；模型抛异常时**必须走失败收尾**，不得把提示事件留在 `requested`。"""
    monkeypatch.setattr(pipeline_mod, "judge", _judge_unclassifiable)
    monkeypatch.setattr(llm_mod, "llm", _stub_llm(exc=RuntimeError("classify down")))
    pid = "T_EV_CLS_FAIL_1"

    with pytest.raises(RuntimeError):
        pipeline_mod.handle_submission(participant_id=pid, question_id="P001",
                                       code="print(1)", config=PRESETS["exp"],
                                       request_hint=True)

    assert _one(pid, "hint")["status"] == "unavailable", "提示事件必须收尾，不能停在 requested"
    assert _one(pid, "teaching")["status"] == "failed", "第 2 步未完成 → failed（不是 partial）"
    assert _one(pid, "execution")["status"] == "finished", "判题已完成，不得被改写"


def test_db_error_is_not_recorded_as_model_error(monkeypatch):
    """**数据库错误**不得统一记成"模型错误"：状态按已完成进度区分，错误码分开。"""
    monkeypatch.setattr(pipeline_mod, "judge", _judge_fail)
    monkeypatch.setattr(llm_mod, "llm", _stub_llm())
    pid = "T_EV_DBERR_1"

    real = db.update_event_conn
    seen = {"n": 0}

    def _boom(conn, *a, **k):
        seen["n"] += 1
        # **只失败一次**。调用序：①TX1 写执行终态 ②TX1 回填提示关联 ③第 2 步置 progress
        # ④收尾写提示事件 ← **这里失败** ⑤收尾写教学终态（不会走到）
        # 之后的调用（失败收尾自己也要写库）必须放行，否则测不到"按进度区分状态"。
        if seen["n"] == 4:
            raise sqlite3.OperationalError("disk I/O error")
        return real(conn, *a, **k)

    monkeypatch.setattr(db, "update_event_conn", _boom)

    with pytest.raises(sqlite3.OperationalError):
        pipeline_mod.handle_submission(participant_id=pid, question_id="P001",
                                       code="print(1)", config=PRESETS["exp"],
                                       request_hint=True)

    teaching = _one(pid, "teaching")
    assert teaching["reason_code"] == "db_error", f"应记 db_error，实际 {teaching['reason_code']}"
    # 第 2 步已提交 → 按进度记为 partial，而不是一刀切 failed
    assert teaching["status"] == "partial"


# ---------------------------------------------------------------- 前后测：任何位置都不得泄露

def test_phase_disabled_response_hides_diagnostics_everywhere(monkeypatch):
    """规则分类**确实命中**（内部记录保留），但**响应里任何位置**都不得出现该诊断。"""
    monkeypatch.setattr(pipeline_mod, "judge", _judge_fail)
    monkeypatch.setattr(llm_mod, "llm", _stub_llm())
    question = dict(pipeline_mod.load_question("P001"))
    question["phase"] = "pretest"
    monkeypatch.setattr(pipeline_mod, "load_question", lambda qid: dict(question))

    pid = "T_EV_LEAK_PRE_1"
    out = pipeline_mod.handle_submission(participant_id=pid, question_id="P001",
                                         code="print(1)", config=PRESETS["exp"])

    # 内部记录：仍然留下确定性分类（用于测量）
    assert _last_submission(pid)["error_class"] == "语法"
    assert _last_submission(pid)["error_kp"] == "变量与赋值"
    # 学生可见的**整个结构**（含嵌套 log）都不得出现教学诊断
    blob = json.dumps(out, ensure_ascii=False)
    assert "语法" not in blob, "响应里任何位置都不得出现教学分类"
    assert "变量与赋值" not in blob, "响应里（尤其 `log.error_kp`）不得出现知识点"
    assert out["log"] is not None and "error_kp" not in out["log"]


def test_exception_text_is_not_persisted(monkeypatch):
    """**截断不等于脱敏**：异常消息里的敏感标记**不得**落进事件表。"""
    monkeypatch.setattr(pipeline_mod, "judge", _judge_fail)
    monkeypatch.setattr(llm_mod, "llm", _stub_llm(exc=RuntimeError("SECRET_MARKER_42")))
    pid = "T_EV_REDACT_1"

    with pytest.raises(RuntimeError):
        pipeline_mod.handle_submission(participant_id=pid, question_id="P001",
                                       code="print(1)", config=PRESETS["exp"],
                                       request_hint=True)

    rows = db.get_events(participant_id=pid)
    blob = json.dumps(rows, ensure_ascii=False)
    assert "SECRET_MARKER_42" not in blob, "异常消息不得落库（截断不等于脱敏）"
    # 仍要留下**受控**信息：异常类型名（便于定位），而不是原文
    assert any("RuntimeError" in (r.get("reason") or "") for r in rows), \
        "应保留受控的异常类型名"


# ---------------------------------------------------------------- 受控错误码不得混用

def test_internal_error_is_not_recorded_as_model_error(monkeypatch):
    """普通**程序错误**（非数据库、非已识别模型故障）→ 记 `internal_error`，不得混成 `model_error`。"""
    monkeypatch.setattr(pipeline_mod, "judge", _judge_fail)
    monkeypatch.setattr(llm_mod, "llm", _stub_llm())
    monkeypatch.setattr(em, "preventive_hint",
                        lambda pid: (_ for _ in ()).throw(ValueError("组件缺陷")))
    pid = "T_EV_INTERNAL_1"

    with pytest.raises(ValueError):
        pipeline_mod.handle_submission(participant_id=pid, question_id="P001",
                                       code="print(1)", config=PRESETS["exp"],
                                       request_hint=True)

    teaching = _one(pid, "teaching")
    assert teaching["reason_code"] == "internal_error", \
        f"应记 internal_error，实际 {teaching['reason_code']}"
    assert teaching["status"] == "partial", "第 2 步已提交 → 按进度记 partial"
