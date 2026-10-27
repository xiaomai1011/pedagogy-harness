"""**安全门（fail closed）测试**：隔离适配器未就绪时，判题一次都不许跑。

对应评阅 m00742／m00827 的六条要求：

1. 适配器未注册／检查失败 ⇒ **拒绝执行**，不回退宿主机 `judge()`；
2. 拒绝路径**不调用 `judge()`**、**不启动任何学生子进程**；
3. 返回明确的"判题环境未就绪"，**不伪装**成 `runtime error` 或普通判题失败；
4. 事件记录能区分「未执行」与「执行中异常中止」；
5. `/api/submit` 与 `/api/hint` **同一道门**；
6. 前后测阶段规则不受影响。

**这些测试证明的是"门禁接线"，不是"隔离已验收"**：真实文件/网络/资源/进程树隔离
仍须在 VM 里按 `docs/判题模块交接说明.md` §15.2 逐项验收。
"""

import pytest

from app import db
from app.harness import isolation
from app.harness import pipeline as pipeline_mod
from app.harness.config_switch import PRESETS

PID = "T_GATE_SUBMIT"
PID_HINT = "T_GATE_HINT"
PID_ABORT = "T_GATE_ABORT"
PID_PHASE = "T_GATE_PHASE"


# ---------------------------------------------------------------- 夹具

def _events(pid: str, event_type: str | None = None) -> list[dict]:
    rows = db.get_events(participant_id=pid)
    return [r for r in rows if r["event_type"] == event_type] if event_type else rows


def _submission_count(pid: str) -> int:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM submission_log WHERE participant_id=?", (pid,)
        ).fetchone()
    return int(row["n"])


def _run_events(pid: str) -> list[dict]:
    """该参与者名下所有请求的隔离运行记录（表名/字段以 `db.get_isolation_runs` 为准）。"""
    rows: list[dict] = []
    for rid in {r["request_id"] for r in db.get_events(participant_id=pid)}:
        rows.extend(db.get_isolation_runs(request_id=rid))
    return rows


class _AdapterRaisesWhileExecuting:
    """就绪检查通过、但**执行中**才失败：模拟 VM agent 中途断开。"""

    def check_isolation(self) -> bool:
        return True

    def start(self, payload):
        raise isolation.IsolationUnavailable("channel_unavailable", "通道中断")


# ---------------------------------------------------------------- ① 未注册 ⇒ 拒绝

def test_submit_without_adapter_refuses_and_never_calls_judge(monkeypatch):
    """适配器未注册 ⇒ 抛 `IsolationUnavailable`，且 `judge()` **一次都没被调用**。"""
    isolation.clear_execution_adapter()          # 模拟生产进程：没有任何适配器
    called = []

    def _boom(*a, **k):
        called.append((a, k))
        raise AssertionError("隔离未就绪时不得调用 judge()")

    monkeypatch.setattr(pipeline_mod, "judge", _boom)

    with pytest.raises(isolation.IsolationUnavailable) as ei:
        pipeline_mod.handle_submission(participant_id=PID, question_id="P001",
                                       code="print(1)", config=PRESETS["exp"])

    assert ei.value.detail_code == "not_ready"
    assert ei.value.stage == "precheck", "未注册属「检查未通过」，不是「执行中中止」"
    assert called == [], "judge() 不得被调用"

    ev = _events(PID, "execution")
    assert len(ev) == 1 and ev[0]["status"] == "aborted"
    assert ev[0]["reason_code"] == "isolation_unavailable"
    assert ev[0]["judge_status"] is None
    assert (ev[0]["progress"] or "") == "", "未执行不得打上「执行中中止」标记"
    assert _submission_count(PID) == 0, "拒绝执行不得写提交行（否则会污染实验数据）"

    runs = _run_events(PID)
    assert [r["event"] for r in runs] == ["isolation_unavailable"]
    assert runs[0]["detail_code"] == "not_ready"


def test_hint_without_adapter_refuses(monkeypatch):
    """纯提示入口走同一道门。"""
    isolation.clear_execution_adapter()
    called = []
    monkeypatch.setattr(pipeline_mod, "judge", lambda *a, **k: called.append(1))

    with pytest.raises(isolation.IsolationUnavailable):
        pipeline_mod.handle_hint_request(participant_id=PID_HINT, question_id="P001",
                                         code="print(1)", config=PRESETS["exp"])

    assert called == []
    ev = _events(PID_HINT, "execution")
    assert len(ev) == 1 and ev[0]["entry"] == "hint"
    assert ev[0]["status"] == "aborted"
    assert ev[0]["reason_code"] == "isolation_unavailable"


def test_refusal_starts_no_student_process(monkeypatch):
    """拒绝路径不得创建学生子进程（`_spawn` 是真正起进程的那一层）。"""
    isolation.clear_execution_adapter()
    from app.judge import runner

    spawned = []
    monkeypatch.setattr(runner, "_spawn", lambda *a, **k: spawned.append(a))

    with pytest.raises(isolation.IsolationUnavailable):
        pipeline_mod.handle_submission(participant_id="T_GATE_SPAWN", question_id="P001",
                                       code="import os; os.system('echo hi')",
                                       config=PRESETS["exp"])

    assert spawned == [], "拒绝路径不得启动学生进程"


# ---------------------------------------------------------------- ② 执行中中止

def test_abort_while_executing_is_marked_distinctly():
    """已进入隔离环境后中止 ⇒ 事件带 `isolation_aborted` 标记，与「未执行」可区分。"""
    isolation.register_execution_adapter(_AdapterRaisesWhileExecuting())

    with pytest.raises(isolation.IsolationUnavailable) as ei:
        pipeline_mod.handle_submission(participant_id=PID_ABORT, question_id="P001",
                                       code="print(1)", config=PRESETS["exp"])

    assert ei.value.stage == "executing"
    ev = _events(PID_ABORT, "execution")[0]
    assert ev["status"] == "aborted"
    assert ev["progress"] == "isolation_aborted"
    runs = _run_events(PID_ABORT)
    assert runs[0]["detail_code"] == "channel_unavailable"

    isolation.clear_execution_adapter()


# ---------------------------------------------------------------- ③ API 层文案

def test_api_submit_returns_judge_unavailable_not_runtime_error():
    """`/api/submit` 返回明确的"未就绪"，不是 `error_type=runtime` 的判题失败。"""
    from app import main

    isolation.clear_execution_adapter()
    out = main.api_submit(main.SubmitRequest(participant_id="T_GATE_API_SUBMIT",
                                             question_id="P001", code="print(1)"))

    assert out["status"] == "judge_unavailable"
    assert out["status"] not in ("fail", "error", "crash", "timeout")
    assert out["error_type"] == ""
    assert out["failed_cases"] == [] and out["stderr"] == ""
    assert out["source"] == "isolation_unavailable"
    assert out["log"] is None
    assert "未就绪" in out["message"] and "未执行" in out["message"]
    assert out["detail_code"] == "not_ready"


def test_api_hint_returns_judge_unavailable():
    from app import main

    isolation.clear_execution_adapter()
    out = main.api_hint(main.HintRequest(participant_id="T_GATE_API_HINT",
                                         question_id="P001", code="print(1)"))

    assert out["status"] == "judge_unavailable"
    assert out["hint_requested"] == 1
    assert "未就绪" in out["message"]


def test_api_status_reports_isolation_not_configured():
    from app import main

    isolation.clear_execution_adapter()
    st = main.api_status()
    assert st["isolation"] == "not_configured"
    assert "未就绪" in st["isolation_note"]

    isolation.register_execution_adapter(_AdapterRaisesWhileExecuting())
    st2 = main.api_status()
    assert st2["isolation"] == "configured"
    assert st2["isolation_note"] == ""
    isolation.clear_execution_adapter()


# ---------------------------------------------------------------- ④ 阶段规则不变

def test_phase_disabled_pure_hint_still_returns_early(monkeypatch):
    """前后测纯提示：**不判题、不落执行事件**，也不被安全门改写成"未就绪"。"""
    isolation.clear_execution_adapter()
    q = dict(pipeline_mod.load_question("P001"))
    q["phase"] = "pretest"
    monkeypatch.setattr(pipeline_mod, "load_question", lambda qid: dict(q))

    out = pipeline_mod.handle_hint_request(participant_id=PID_PHASE, question_id="P001",
                                           code="print(1)", config=PRESETS["exp"])

    assert out["status"] == "phase_disabled"
    assert out["message"] == pipeline_mod.PHASE_DISABLED_HINT_MESSAGE
    assert _events(PID_PHASE, "execution") == [], "阶段禁用时不得产生执行事件"
