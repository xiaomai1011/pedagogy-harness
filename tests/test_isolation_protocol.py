"""G-② 第 5／6 项：**离线协议测试**。

**本文件不启动任何学生进程、不建目录、不联网、不读密钥、不安装任何东西。**
它验证的是**协议与失败处理**，不是隔离机制本身。

必须分清的两层（评阅口径 m01710）：

- **A 层：管线异常映射测试** —— `judge()` 抛 `IsolationUnavailable` 时，管线记
  `aborted` + `isolation_unavailable`，**不是**正常返回的 `finished`。
  **这一层只能证明「管线如何处理异常」，不能证明「学生代码不会运行」。**
- **B 层：执行适配器拒绝启动测试** —— 在执行适配器边界上验证
  「检查未通过 ⇒ `start()` 一次都没被调用」。
  真正的「先检查、后启动」还须在系统集成时用真实机制验证。

**不得**用 A 层的绿灯宣称隔离已验收。
"""

import pytest

from app import db
from app.harness import isolation
from app.harness import pipeline as pipeline_mod
from app.harness.config_switch import PRESETS

PID_SUBMIT = "T_ISO_SUBMIT"
PID_HINT = "T_ISO_HINT"
PID_OTHER = "T_ISO_OTHER"

#: 模拟敏感标记：用于断言诊断输出**不带**底层异常原文。
HIDDEN_MARKER = "HIDDEN_INPUT_MARKER_7"


# ---------------------------------------------------------------- 夹具

def _judge_isolation_down(*a, **k):
    """模拟岗位③ 判题器在**隔离未就绪**时拒绝执行。"""
    raise isolation.IsolationUnavailable("scope_not_ready", "VM 未就绪")


def _judge_runtime_boom(*a, **k):
    """模拟判题器自身出故障（**不是**隔离问题）。"""
    raise RuntimeError("judge exploded")


def _events(pid, event_type=None):
    rows = db.get_events(participant_id=pid)
    return [r for r in rows if r["event_type"] == event_type] if event_type else rows


def _one(pid, event_type):
    rows = _events(pid, event_type)
    assert len(rows) == 1, f"{event_type} 事件应有且仅有一条，实际 {len(rows)} 条"
    return rows[0]


class _SpyAdapter:
    """假执行适配器：记录 `start()` 被调用了几次，**不启动任何进程**。"""

    def __init__(self, ready: bool = True):
        self.ready = ready
        self.started = 0
        self.payloads = []

    def check_isolation(self) -> bool:
        return self.ready

    def start(self, payload):
        self.started += 1
        self.payloads.append(payload)
        return {"ok": True}


class _RaisingAdapter:
    """就绪检查**抛异常**（而非返回假值）的适配器。"""

    def __init__(self):
        self.started = 0

    def check_isolation(self):
        raise isolation.IsolationUnavailable("agent_unreachable", "管理通道不通")

    def start(self, payload):
        self.started += 1
        return {"ok": True}


# ================================================================
# B 层：执行适配器边界 —— 检查未通过则 start() 一次都不调用
# ================================================================

def test_adapter_start_not_called_when_check_returns_false():
    """**检查未就绪 ⇒ 拒绝执行，`start()` 一次都没被调用。**"""
    adapter = _SpyAdapter(ready=False)

    with pytest.raises(isolation.IsolationUnavailable):
        isolation.guarded_execute(adapter, {"code": "print(1)"},
                                  request_id="R_ISO_B1", phase="practice")

    assert adapter.started == 0, "隔离未就绪时**不得**启动执行"


def test_adapter_start_not_called_when_check_raises():
    """就绪检查**抛 `IsolationUnavailable`** ⇒ 同样拒绝执行，`start()` 未被调用。"""
    adapter = _RaisingAdapter()

    with pytest.raises(isolation.IsolationUnavailable):
        isolation.guarded_execute(adapter, {"code": "print(1)"}, request_id="R_ISO_B2")

    assert adapter.started == 0, "就绪检查抛异常时**不得**启动执行"


def test_adapter_start_called_exactly_once_when_ready():
    """就绪 ⇒ 正常调用一次（且**只调用一次**）。"""
    adapter = _SpyAdapter(ready=True)

    out = isolation.guarded_execute(adapter, {"code": "print(1)"}, request_id="R_ISO_B3")

    assert out == {"ok": True}
    assert adapter.started == 1, "就绪时应恰好启动一次"
    assert adapter.payloads == [{"code": "print(1)"}]


def test_guarded_execute_propagates_start_result_and_errors():
    """`start()` 自身的异常**原样传播**，不得被吞成「隔离不可用」。"""
    class _Broken:
        def check_isolation(self):
            return True

        def start(self, payload):
            raise ValueError("启动失败")

    with pytest.raises(ValueError, match="启动失败"):
        isolation.guarded_execute(_Broken(), {}, request_id="R_ISO_B4")


# ================================================================
# A 层：管线异常映射 —— aborted + isolation_unavailable（非 finished）
# ================================================================

def test_submit_entry_maps_isolation_unavailable(monkeypatch):
    """**正式提交入口**：隔离不可用 ⇒ execution 记 `aborted` + `isolation_unavailable`。"""
    monkeypatch.setattr(pipeline_mod, "judge", _judge_isolation_down)

    with pytest.raises(isolation.IsolationUnavailable):
        pipeline_mod.handle_submission(participant_id=PID_SUBMIT, question_id="P001",
                                       code="print(1)", config=PRESETS["exp"])

    exec_ev = _one(PID_SUBMIT, "execution")
    assert exec_ev["status"] == "aborted", "隔离不可用**不是**正常返回，不得记 finished"
    assert exec_ev["reason_code"] == "isolation_unavailable"
    assert exec_ev["judge_status"] is None, "aborted 时不得填 judge_status"
    assert exec_ev["ts_end"], "aborted 也必须填好结束时间"


def test_submit_entry_does_not_record_finished(monkeypatch):
    """隔离不可用时**不得**出现 `finished` 终态（那是正常返回才有的）。"""
    monkeypatch.setattr(pipeline_mod, "judge", _judge_isolation_down)

    with pytest.raises(isolation.IsolationUnavailable):
        pipeline_mod.handle_submission(participant_id="T_ISO_FIN", question_id="P001",
                                       code="print(1)", config=PRESETS["exp"])

    statuses = {r["status"] for r in _events("T_ISO_FIN", "execution")}
    assert "finished" not in statuses
    assert statuses == {"aborted"}


def test_hint_entry_maps_isolation_unavailable(monkeypatch):
    """**纯提示入口**：同样记 `aborted` + `isolation_unavailable`。"""
    monkeypatch.setattr(pipeline_mod, "judge", _judge_isolation_down)

    with pytest.raises(isolation.IsolationUnavailable):
        pipeline_mod.handle_hint_request(participant_id=PID_HINT, question_id="P001",
                                         code="print(1)", config=PRESETS["exp"])

    exec_ev = _one(PID_HINT, "execution")
    assert exec_ev["status"] == "aborted"
    assert exec_ev["reason_code"] == "isolation_unavailable"
    assert exec_ev["entry"] == "hint"


def test_existing_hint_event_is_finalized_on_isolation_failure(monkeypatch):
    """**已有提示事件的失败收尾**：hint 记 `unavailable`，不得留 `requested` 悬挂。"""
    monkeypatch.setattr(pipeline_mod, "judge", _judge_isolation_down)

    with pytest.raises(isolation.IsolationUnavailable):
        pipeline_mod.handle_submission(participant_id="T_ISO_HINTFIN", question_id="P001",
                                       code="print(1)", config=PRESETS["exp"],
                                       request_hint=True)

    hint_ev = _one("T_ISO_HINTFIN", "hint")
    assert hint_ev["status"] == "unavailable", "提示事件必须收尾，不得停在 requested"
    assert hint_ev["reason_code"] == "gen_unavailable"
    assert hint_ev["ts_end"]
    assert _events("T_ISO_HINTFIN", "teaching") == [], "隔离不可用不得产出教学事件"


def test_runtime_error_still_maps_to_judge_raised(monkeypatch):
    """**其他异常仍记 `judge_raised`** —— 不得把判题器自身故障误判成隔离问题。"""
    monkeypatch.setattr(pipeline_mod, "judge", _judge_runtime_boom)

    with pytest.raises(RuntimeError):
        pipeline_mod.handle_submission(participant_id=PID_OTHER, question_id="P001",
                                       code="print(1)", config=PRESETS["exp"])

    exec_ev = _one(PID_OTHER, "execution")
    assert exec_ev["status"] == "aborted"
    assert exec_ev["reason_code"] == "judge_raised", \
        "RuntimeError 不得被记成 isolation_unavailable"


def test_reason_code_of_helper_distinguishes_two_cases():
    """受控码分流函数本身：隔离不可用单列，其余仍是 `judge_raised`。"""
    assert isolation.reason_code_of(isolation.IsolationUnavailable("x")) == "isolation_unavailable"
    assert isolation.reason_code_of(RuntimeError("x")) == "judge_raised"
    assert isolation.reason_code_of(ValueError("x")) == "judge_raised"


def test_reason_text_does_not_echo_exception_message():
    """`reason` 只放受控说明：**不得**拼 `str(exc)`（截断不等于脱敏）。"""
    secret = "HIDDEN_INPUT_MARKER_7"
    exc = isolation.IsolationUnavailable("scope_not_ready", f"泄漏内容 {secret}")
    text = isolation.reason_text(exc)

    assert secret not in text, "reason 不得携带异常原文"
    assert "scope_not_ready" in text, "受控码应当保留，便于排查"


# ================================================================
# 独立结构化运行记录（G-① §3.5 方案 B）
# ================================================================

def test_isolation_failure_writes_correlated_run_record(monkeypatch):
    """隔离不可用**另写一条可关联请求的运行记录**，与判题结果分开。"""
    monkeypatch.setattr(pipeline_mod, "judge", _judge_isolation_down)

    with pytest.raises(isolation.IsolationUnavailable):
        pipeline_mod.handle_submission(participant_id="T_ISO_REC", question_id="P001",
                                       code="print(1)", config=PRESETS["exp"])

    request_id = _one("T_ISO_REC", "execution")["request_id"]
    rows = isolation.read_run_events(request_id=request_id)
    assert len(rows) == 1, "应恰好写一条隔离不可用运行记录"
    row = rows[0]
    assert row["event"] == "isolation_unavailable"
    assert row["detail_code"] == "scope_not_ready"
    assert row["run_id"], "必须有稳定执行标识"
    assert row["request_id"] == request_id, "必须能关联到具体请求"


def test_run_record_requires_request_id():
    """**必须能关联请求**：`request_id` 为空时不得写（与「打印一条日志」无异）。"""
    with pytest.raises(ValueError, match="request_id"):
        isolation.record_run_event(request_id="", event="cleanup_failed")


def test_run_record_rejects_unregistered_event():
    """事件名必须已登记，不得随手新增。"""
    with pytest.raises(ValueError, match="未登记的运行记录事件"):
        isolation.record_run_event(request_id="R_X", event="whatever")


def test_run_record_write_failure_is_controlled(monkeypatch):
    """**写入失败不得静默吞掉**，转成受控异常，且不携带底层异常原文。"""
    def _boom(*a, **k):
        raise RuntimeError("disk full at C:\\secret\\path")

    monkeypatch.setattr(db, "log_isolation_run", _boom)

    with pytest.raises(isolation.RunRecordWriteFailed) as ei:
        isolation.record_run_event(request_id="R_WRITE", event="cleanup_failed")

    assert "disk full" not in str(ei.value), "受控异常不得携带底层原文"


def test_run_record_write_failure_does_not_change_judge_facts(monkeypatch):
    """**运行记录写失败不得改写判题事实**：execution 仍是 aborted + 受控码。"""
    monkeypatch.setattr(pipeline_mod, "judge", _judge_isolation_down)

    def _boom(*a, **k):
        raise RuntimeError("record write failed")

    monkeypatch.setattr(db, "log_isolation_run", _boom)

    with pytest.raises(isolation.IsolationUnavailable):
        pipeline_mod.handle_submission(participant_id="T_ISO_WF", question_id="P001",
                                       code="print(1)", config=PRESETS["exp"])

    exec_ev = _one("T_ISO_WF", "execution")
    assert exec_ev["status"] == "aborted"
    assert exec_ev["reason_code"] == "isolation_unavailable", \
        "运行记录写失败**不得**把受控码改掉"


def test_run_record_write_failure_is_not_silent(monkeypatch, capsys):
    """**运行记录写失败不得被静默忽略**：须在宿主侧输出受控诊断码。

    只验「抛错 / 写库」都不够：评阅 m02249 要求的是**写入失败本身可见**，
    同时**不得**因此改写判题事实、**不得**产生新的自由文本通道。
    """
    monkeypatch.setattr(pipeline_mod, "judge", _judge_isolation_down)

    def _boom(*a, **k):
        raise RuntimeError(f"disk full at C:\\secret\\{HIDDEN_MARKER}")

    monkeypatch.setattr(db, "log_isolation_run", _boom)

    with pytest.raises(isolation.IsolationUnavailable):
        pipeline_mod.handle_submission(participant_id="T_ISO_SILENT", question_id="P001",
                                       code="print(1)", config=PRESETS["exp"])

    err = capsys.readouterr().err
    assert "record_write_failed" in err, "写入失败必须留下可见的受控诊断码"
    assert HIDDEN_MARKER not in err, "诊断输出不得携带底层异常原文"
    assert "disk full" not in err, "诊断输出不得携带底层异常原文"

    exec_ev = _one("T_ISO_SILENT", "execution")
    assert exec_ev["status"] == "aborted"
    assert exec_ev["reason_code"] == "isolation_unavailable"


def test_warn_codes_are_limited_and_text_is_fixed(capsys):
    """`warn()` 的码是**有限集合**、文案是**固定常量**：自由文本进不来。"""
    secret = "SECRET_MARKER_42"

    isolation.warn("record_write_failed")
    isolation.warn(secret)                 # 未知码 ⇒ 归一，不原样输出

    err = capsys.readouterr().err
    assert "record_write_failed" in err
    assert secret not in err, "未知诊断码不得原样输出"
    assert "unregistered_warn" in err, "未知诊断码应归一为固定码"
    assert set(isolation.WARN_CODES) == {"record_write_failed"}


def test_run_record_stores_no_hidden_content():
    """运行记录字段只有受控码与关联标识，**不含**隐藏用例输入或异常正文。"""
    rid = isolation.record_run_event(request_id="R_CLEAN", event="cleanup_failed",
                                     detail_code="rm_failed", phase="practice",
                                     case_index=2)
    row = isolation.read_run_events(run_id=rid)[0]

    assert set(row) == {"run_id", "request_id", "case_index", "phase",
                        "event", "detail_code", "ts"}, \
        "运行记录不得新增可承载原文的字段"


def test_cleanup_failure_recorded_separately_from_judge_result():
    """**清理失败与判题结果分开记录**：清理记录不得出现在 execution 事件里。"""
    rid = isolation.record_run_event(request_id="R_SEP", event="cleanup_failed",
                                     detail_code="rm_failed")
    assert isolation.read_run_events(request_id="R_SEP")[0]["run_id"] == rid

    exec_rows = [r for r in db.get_events(request_id="R_SEP") if r["event_type"] == "execution"]
    assert exec_rows == [], "清理失败不得写进判题执行事件"


def test_isolation_unavailable_records_detail_code_only():
    """隔离不可用运行记录**只存受控码**，不存异常全文。"""
    secret = "HIDDEN_INPUT_MARKER_7"
    rid = isolation.record_run_event(request_id="R_CODE", event="isolation_unavailable",
                                     detail_code="agent_unreachable")
    row = isolation.read_run_events(run_id=rid)[0]

    assert row["detail_code"] == "agent_unreachable"
    assert secret not in str(row), "运行记录不得携带任何原始内容"


# ---------------------------------------------------------------- detail_code 必须真受控

def test_detail_code_is_limited_to_registered_set():
    """**`detail_code` 是有限登记集合**：登记值原样保留，未知值一律归一。"""
    assert "scope_not_ready" in isolation.DETAIL_CODES
    assert isolation.normalize_detail_code("scope_not_ready") == "scope_not_ready"
    assert isolation.normalize_detail_code("SECRET_MARKER_42") == isolation.UNREGISTERED_DETAIL_CODE
    assert isolation.normalize_detail_code(None) == ""
    assert isolation.normalize_detail_code("   ") == ""
    assert isolation.normalize_detail_code(12345) == isolation.UNREGISTERED_DETAIL_CODE


def test_exception_detail_code_is_normalized_not_echoed():
    """**敏感标记放在 `detail_code` 本身**也不得被保存（换字段承载任意字符串照样泄露）。"""
    secret = "SECRET_MARKER_42"
    exc = isolation.IsolationUnavailable(detail_code=secret)

    assert secret not in exc.detail_code, "未知 detail_code 不得原样保留"
    assert exc.detail_code == isolation.UNREGISTERED_DETAIL_CODE
    assert secret not in isolation.reason_text(exc), "reason 同样不得出现该标记"


def test_unregistered_detail_code_not_persisted_in_run_record():
    """未知 `detail_code` **不得原样进入运行记录**（只留固定归一码）。"""
    secret = "SECRET_MARKER_42"
    rid = isolation.record_run_event(request_id="R_DC", event="isolation_unavailable",
                                     detail_code=secret)
    row = isolation.read_run_events(run_id=rid)[0]

    assert row["detail_code"] == isolation.UNREGISTERED_DETAIL_CODE
    assert secret not in str(row), "运行记录不得出现未登记的 detail_code 原文"


def test_unregistered_detail_code_not_persisted_in_event_table(monkeypatch):
    """未知 `detail_code` **不得原样进入事件表**：execution 事件的 reason 也不得含标记。"""
    secret = "SECRET_MARKER_42"

    def _judge_bad_detail(*a, **k):
        raise isolation.IsolationUnavailable(detail_code=secret)

    monkeypatch.setattr(pipeline_mod, "judge", _judge_bad_detail)

    with pytest.raises(isolation.IsolationUnavailable):
        pipeline_mod.handle_submission(participant_id="T_ISO_DC", question_id="P001",
                                       code="print(1)", config=PRESETS["exp"])

    exec_ev = _one("T_ISO_DC", "execution")
    assert secret not in str(dict(exec_ev)), "事件表不得出现未登记的 detail_code 原文"
    assert exec_ev["reason_code"] == "isolation_unavailable"

    request_id = exec_ev["request_id"]
    rows = isolation.read_run_events(request_id=request_id)
    assert len(rows) == 1
    assert secret not in str(rows[0]), "运行记录不得出现未登记的 detail_code 原文"
    assert rows[0]["detail_code"] == isolation.UNREGISTERED_DETAIL_CODE
