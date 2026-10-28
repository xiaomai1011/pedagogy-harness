"""宿主侧的 **VM 判题适配器**（协议：`docs/G4_VM_Agent_最小协议.md`）。

它只做两件事：

1. `check_isolation()`：`GET /healthz` + `POST /v1/handshake`。
   连不上 ⇒ `IsolationUnavailable("agent_unreachable")`；
   token 错／协议不匹配 ⇒ `IsolationUnavailable("agent_rejected")`；
   健康但**执行能力未启用** ⇒ `IsolationUnavailable("not_ready")`。
2. `start(payload)`：`POST /v1/execute`，把 agent 的结构化响应收回成
   `JudgeResult`（字段与契约 §1 完全一致）。

**未登记 ≠ 不会用**：本模块**不会**在导入时自动登记适配器。运维要在明确
「VM agent 已部署且验收通过」之后显式调用 `register_vm_agent_adapter()`，
否则 `app.main` 继续按 fail closed 返回 `judge_unavailable`。

绝不会做的事：连不上 VM 就退回 `app/judge/runner.py` 在宿主执行。
"""

import json
import os
import urllib.error
import urllib.request

from app.harness import isolation
from app.judge.runner import OUTPUT_LIMIT, TIMEOUT_SECONDS, JudgeResult

#: 线上协议版本（与 `vm_agent/ph_judge_agent.py` 的 `PROTOCOL_VERSION` 对齐）
PROTOCOL_VERSION = 1

#: 默认地址：只走内部交换机，不出网
DEFAULT_URL = "http://192.168.200.10:9443"

#: 连接／健康检查超时（秒）
CONNECT_TIMEOUT = 2.0

#: 单次执行请求超时（秒）；外层还有判题自身的 5 秒限制，两者不互相替代
REQUEST_TIMEOUT = 10.0

_VALID_STATUS = frozenset({"pass", "fail", "timeout", "crash", "error"})
_VALID_ERROR_TYPE = frozenset({"none", "syntax", "runtime", "timeout", "wrong_output"})


class _Response:
    __slots__ = ("status", "body")

    def __init__(self, status: int, body: dict):
        self.status = status
        self.body = body


class VmAgentAdapter:
    """VM agent 的宿主侧实现（鸭子类型满足 `execution.ExecutionAdapter`）。"""

    def __init__(self, base_url: str | None = None, token: str | None = None, *,
                 protocol: int = PROTOCOL_VERSION,
                 connect_timeout: float = CONNECT_TIMEOUT,
                 request_timeout: float = REQUEST_TIMEOUT):
        self.base_url = (base_url or os.environ.get("PH_VM_AGENT_URL")
                         or DEFAULT_URL).rstrip("/")
        self.token = token if token is not None else _token_from_env()
        self.protocol = protocol
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout

    # ---------------- 对外协议 ----------------

    def describe(self) -> dict:
        """运维/状态页可见的**非敏感**信息（不含 token）。"""
        return {"kind": "vm-agent", "url": self.base_url, "protocol": self.protocol}

    def check_isolation(self) -> bool:
        """就绪返回 `True`；否则抛 `IsolationUnavailable`（**不启动任何执行**）。"""
        health = self._request("GET", "/healthz", None, self.connect_timeout)
        if health.status != 200:
            raise isolation.IsolationUnavailable(
                "agent_rejected", f"健康检查返回 {health.status}")
        caps = health.body.get("capabilities") or {}
        if caps.get("execute") is not True:
            # 阶段 2 的正常状态：agent 只握手不执行
            raise isolation.IsolationUnavailable(
                "not_ready", "VM agent 已连通，但执行能力尚未启用")

        handshake = self._request("POST", "/v1/handshake",
                                  {"protocol": self.protocol}, self.connect_timeout)
        if handshake.status != 200:
            raise isolation.IsolationUnavailable(
                "agent_rejected", f"握手被拒绝（HTTP {handshake.status}）")
        if handshake.body.get("protocol") != self.protocol:
            raise isolation.IsolationUnavailable(
                "agent_rejected", "协议版本不匹配")
        return True

    def start(self, payload) -> JudgeResult:
        """把一次判题请求交给 VM agent，并把响应收回成 `JudgeResult`。"""
        body = self._build_request(payload)
        resp = self._request("POST", "/v1/execute", body, self.request_timeout)
        if resp.status != 200:
            code = str(resp.body.get("error_code") or "")
            # 5xx 且不是明确的受限拒绝 ⇒ 通道问题；其余按"请求被拒"
            detail = "agent_rejected" if resp.status < 500 or code == "agent_rejected" \
                else "channel_unavailable"
            raise isolation.IsolationUnavailable(
                detail, f"agent 返回 HTTP {resp.status}（{code or 'no_code'}）")
        return self._to_result(resp.body)

    # ---------------- 内部实现 ----------------

    def _build_request(self, payload: dict) -> dict:
        question = payload.get("question") or {}
        cases = []
        for index, case in enumerate(question.get("test_cases") or [], start=1):
            cases.append({
                "case_index": case.get("case_index", index),
                "input": case.get("input", ""),
                "expected": case.get("expected", ""),
                "hidden": bool(case.get("hidden", False)),
            })
        return {
            "request_id": payload.get("request_id") or "",
            "language": payload.get("language") or "python",
            "code": payload.get("code") or "",
            "timeout_seconds": int(payload.get("timeout_seconds", TIMEOUT_SECONDS)),
            "output_limit": int(payload.get("output_limit", OUTPUT_LIMIT)),
            "cases": cases,
        }

    def _request(self, method: str, path: str, payload, timeout: float) -> _Response:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        if self.token:
            req.add_header("X-Agent-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _Response(resp.status, _decode(resp.read()))
        except urllib.error.HTTPError as exc:                    # 4xx/5xx：有响应
            try:
                body = _decode(exc.read())
            except Exception:
                body = {}
            return _Response(exc.code, body)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # 连不上、DNS、连接被重置、读超时：一律按不可用处理 ⇒ fail closed
            raise isolation.IsolationUnavailable(
                "agent_unreachable", f"VM agent 不可达：{type(exc).__name__}") from exc

    def _to_result(self, body: dict) -> JudgeResult:
        status = str(body.get("status") or "error")
        if status not in _VALID_STATUS:
            status = "error"
        error_type = str(body.get("error_type") or "none")
        if error_type not in _VALID_ERROR_TYPE:
            error_type = "runtime"
        failed = []
        for case in body.get("failed_cases") or []:
            if not isinstance(case, dict):
                continue
            hidden = bool(case.get("hidden", False))
            failed.append({
                "case_index": case.get("case_index"),
                "hidden": hidden,
                # 隐藏用例：输入与期望一律不回传（服务端也应已清空，这里再兜一道）
                "input": None if hidden else _clip(case.get("input")),
                "expected": None if hidden else _clip(case.get("expected")),
                "actual": _clip(case.get("actual")),
            })
        exit_code = body.get("exit_code")
        return JudgeResult(
            status=status,
            total_cases=_as_int(body.get("total_cases")),
            passed_cases=_as_int(body.get("passed_cases")),
            failed_cases=failed,
            error_type=error_type,
            stderr=_clip(body.get("stderr")),
            stdout=_clip(body.get("stdout")),
            exec_ms=_as_int(body.get("exec_ms")),
            exit_code=exit_code if isinstance(exit_code, int) else None,
        )


def _token_from_env() -> str:
    """token 优先读文件（`PH_VM_AGENT_TOKEN_FILE`），其次读环境变量。"""
    path = os.environ.get("PH_VM_AGENT_TOKEN_FILE")
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""
    return os.environ.get("PH_VM_AGENT_TOKEN", "")


def _decode(raw: bytes) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _clip(value) -> str:
    text = "" if value is None else str(value)
    return text[:OUTPUT_LIMIT]


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def register_vm_agent_adapter(base_url: str | None = None,
                              token: str | None = None) -> VmAgentAdapter:
    """**运维入口**：登记 VM 适配器。只在 agent 部署并验收后调用。

    生产进程**没有**任何地方自动调用它；不调用就保持 fail closed。
    """
    adapter = VmAgentAdapter(base_url, token)
    isolation.register_execution_adapter(adapter)
    return adapter
