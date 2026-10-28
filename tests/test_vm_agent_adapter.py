"""宿主侧 VM 适配器 + VM agent 阶段 2 协议测试（**不启动任何学生进程**）。

覆盖：

- agent 连不上 ⇒ `IsolationUnavailable("agent_unreachable")`（fail closed）；
- agent 已连通但执行能力未启用 ⇒ `not_ready`（阶段 2 的真实状态）；
- token 错／协议不匹配 ⇒ `agent_rejected`；
- `/v1/execute` 被拒 ⇒ `agent_rejected`（不是判题失败，也不是宿主机执行）；
- 正常响应 ⇒ 映射成 `JudgeResult`，**隐藏用例的 input/expected 不外泄**。
"""

import importlib.util
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.harness import isolation
from app.harness import vm_agent_adapter as vma
from app.judge.runner import JudgeResult

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token-0123456789"

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------- 夹具

def _load_agent_module():
    spec = importlib.util.spec_from_file_location(
        "ph_judge_agent", ROOT / "vm_agent" / "ph_judge_agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _serve(httpd) -> str:
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def agent_url():
    """**真实的**阶段 2 agent（`execute=False`）。"""
    httpd = _load_agent_module().build_server("127.0.0.1", 0, TOKEN)
    url = _serve(httpd)
    try:
        yield url
    finally:
        httpd.shutdown()
        httpd.server_close()


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                     # /healthz
        payload = {"ok": True, "protocol": 1,
                   "capabilities": self.server.capabilities}
        if getattr(self.server, "readiness", ""):
            payload["readiness"] = self.server.readiness
        return self._send(200, payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path == "/v1/handshake":
            if self.headers.get("X-Agent-Token") != TOKEN:
                return self._send(401, {"ok": False, "error_code": "agent_rejected"})
            return self._send(200, {"ok": True, "protocol": self.server.protocol})
        if self.path == "/v1/execute":
            if self.headers.get("X-Agent-Token") != TOKEN:
                return self._send(401, {"ok": False, "error_code": "agent_rejected"})
            code = self.server.execute_status
            return self._send(code, self.server.execute_payload)
        self._send(404, {"ok": False})

    def log_message(self, fmt, *args):
        pass


@pytest.fixture
def stub_server():
    """可配置的假 agent：用来验证 `/v1/execute` 的映射与拒绝分支。"""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    httpd.daemon_threads = True
    httpd.capabilities = {"execute": True, "file_isolation": True,
                          "network_isolation": True, "resource_limits": True,
                          "process_tree_kill": True}
    httpd.protocol = 1
    httpd.execute_status = 200
    httpd.execute_payload = {}
    url = _serve(httpd)
    try:
        yield httpd, url
    finally:
        httpd.shutdown()
        httpd.server_close()


def _closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------- 不可达

def test_unreachable_agent_refuses_with_agent_unreachable():
    adapter = vma.VmAgentAdapter(_closed_port_url(), TOKEN, connect_timeout=0.5)
    with pytest.raises(isolation.IsolationUnavailable) as ei:
        adapter.check_isolation()
    assert ei.value.detail_code == "agent_unreachable"
    assert ei.value.stage == "precheck"


def test_unreachable_during_execute_marks_executing_stage():
    """检查通过、执行途中通道断 ⇒ 必须标成"执行中中止"，与"未执行"可区分。"""
    class _CheckOkThenUnreachable(vma.VmAgentAdapter):
        def check_isolation(self):
            return True

    adapter = _CheckOkThenUnreachable(_closed_port_url(), TOKEN, request_timeout=0.5)
    with pytest.raises(isolation.IsolationUnavailable) as ei:
        isolation.guarded_execute(adapter, {"question": {"test_cases": []}, "code": ""})
    assert ei.value.detail_code == "agent_unreachable"
    assert ei.value.stage == "executing"


# ---------------------------------------------------------------- 阶段 2 真实 agent

def test_stage2_agent_is_reachable_but_not_ready(agent_url):
    """健康检查通过，但 `execute=false` ⇒ 必须拒绝，不能误判为就绪。"""
    adapter = vma.VmAgentAdapter(agent_url, TOKEN)
    with pytest.raises(isolation.IsolationUnavailable) as ei:
        adapter.check_isolation()
    assert ei.value.detail_code == "not_ready"


def test_stage2_agent_rejects_execute_call(agent_url):
    adapter = vma.VmAgentAdapter(agent_url, TOKEN)
    with pytest.raises(isolation.IsolationUnavailable) as ei:
        adapter.start({"question": {"test_cases": []}, "code": "print(1)"})
    assert ei.value.detail_code == "agent_rejected"


def test_wrong_token_is_rejected_by_handshake(stub_server):
    """执行能力已启用但 token 不对 ⇒ 握手 401 ⇒ `agent_rejected`（不是"未就绪"）。"""
    _httpd, url = stub_server
    adapter = vma.VmAgentAdapter(url, "wrong-token-000000000")
    with pytest.raises(isolation.IsolationUnavailable) as ei:
        adapter.check_isolation()
    assert ei.value.detail_code == "agent_rejected"


# ---------------------------------------------------------------- 正常/异常映射

# ---------------------------------------------------------------- 阶段 4.3 分层就绪

def test_execution_probe_passed_is_not_ready(stub_server):
    """固定样例能跑 ≠ 隔离能力已验证：分层未到最后一层必须拒绝。"""
    httpd, url = stub_server
    httpd.capabilities = {"execute": True, "file_isolation": False,
                          "network_isolation": False, "resource_limits": False,
                          "process_tree_kill": False}
    httpd.readiness = "execution_probe_passed"
    adapter = vma.VmAgentAdapter(url, TOKEN)

    assert adapter.readiness() == "execution_probe_passed"
    with pytest.raises(isolation.IsolationUnavailable) as ei:
        adapter.check_isolation()
    assert ei.value.detail_code == "not_ready"
    assert "execution_probe_passed" in str(ei.value.detail or "")


def test_capabilities_all_true_without_readiness_field_is_ready(stub_server):
    """旧 agent 不带 readiness 字段时，退回"四项能力位全真"判定。"""
    httpd, url = stub_server
    adapter = vma.VmAgentAdapter(url, TOKEN)
    assert adapter.readiness() == ""
    assert adapter.check_isolation() is True


def test_handshake_and_execute_roundtrip(stub_server):
    httpd, url = stub_server
    httpd.execute_payload = {
        "status": "pass", "total_cases": 2, "passed_cases": 2, "failed_cases": [],
        "error_type": "none", "stdout": "3\n", "stderr": "",
        "exec_ms": 12, "exit_code": 0, "cleanup_status": "ok",
    }
    adapter = vma.VmAgentAdapter(url, TOKEN)
    assert adapter.check_isolation() is True

    result = adapter.start({"question": {"test_cases": [
        {"input": "1 2\n", "expected": "3"}]}, "code": "print(3)"})
    assert isinstance(result, JudgeResult)
    assert result.status == "pass" and result.total_cases == 2
    assert result.exec_ms == 12 and result.exit_code == 0


def test_hidden_case_never_leaks_input_or_expected(stub_server):
    httpd, url = stub_server
    httpd.execute_payload = {
        "status": "fail", "total_cases": 2, "passed_cases": 1,
        "failed_cases": [{"case_index": 2, "hidden": True,
                          "input": "7", "expected": "14",
                          "actual": "x" * 5000}],
        "error_type": "wrong_output", "stdout": "y" * 5000, "stderr": "",
        "exec_ms": 8, "exit_code": 0,
    }
    adapter = vma.VmAgentAdapter(url, TOKEN)
    result = adapter.start({"question": {"test_cases": []}, "code": ""})

    case = result.failed_cases[0]
    assert case["hidden"] is True
    assert case["input"] is None and case["expected"] is None
    assert len(case["actual"]) == 2000, "actual 必须截到 2000"
    assert len(result.stdout) == 2000


def test_execute_rejected_is_not_a_judge_failure(stub_server):
    httpd, url = stub_server
    httpd.execute_status = 503
    httpd.execute_payload = {"ok": False, "status": "error",
                             "error_code": "agent_rejected", "message": "未启用"}
    adapter = vma.VmAgentAdapter(url, TOKEN)
    with pytest.raises(isolation.IsolationUnavailable) as ei:
        adapter.start({"question": {"test_cases": []}, "code": ""})
    assert ei.value.detail_code == "agent_rejected"


def test_protocol_mismatch_rejected(stub_server):
    httpd, url = stub_server
    httpd.protocol = 2
    adapter = vma.VmAgentAdapter(url, TOKEN)
    with pytest.raises(isolation.IsolationUnavailable) as ei:
        adapter.check_isolation()
    assert ei.value.detail_code == "agent_rejected"


def test_no_registration_by_default():
    """导入本模块**不会**自动登记：生产进程必须保持 fail closed。"""
    isolation.clear_execution_adapter()
    assert isolation.registered_execution_adapter() is None
    with pytest.raises(isolation.IsolationUnavailable):
        isolation.guarded_execute(None, {"question": {}, "code": ""})


def test_startup_registration_is_opt_in(monkeypatch):
    """启动登记只认 `PH_VM_AGENT_ENABLED=1`；开关关闭时保持未登记。"""
    from app import main as main_mod

    isolation.clear_execution_adapter()
    monkeypatch.setattr(main_mod, "VM_AGENT_ENABLED", False)
    assert main_mod._register_configured_vm_agent() is False
    assert isolation.registered_execution_adapter() is None


def test_startup_registration_registers_adapter_when_enabled(monkeypatch):
    from app import main as main_mod

    monkeypatch.setattr(main_mod, "VM_AGENT_ENABLED", True)
    monkeypatch.setenv("PH_VM_AGENT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PH_VM_AGENT_TOKEN", "t" * 16)
    try:
        assert main_mod._register_configured_vm_agent() is True
        adapter = isolation.registered_execution_adapter()
        assert adapter is not None
        assert adapter.base_url == "http://127.0.0.1:9"
        # 登记 ≠ 就绪：agent 不可达时仍必须拒判
        with pytest.raises(isolation.IsolationUnavailable):
            isolation.guarded_execute(adapter, {"question": {}, "code": ""})
    finally:
        isolation.clear_execution_adapter()
