#!/usr/bin/env python3
"""ph-judge-agent —— VM 内最小判题 agent（**阶段 4：转发内置固定样例**）。

设计约束（评阅 m00935 / m01225）：

- 只用标准库，不装依赖：VM 目前离线，`pip` 也没有；
- 只监听内部交换机地址，默认 `192.168.200.10:9443`；
- 所有 `/v1/*` 请求都要求 `X-Agent-Token`，**token 只从文件读，不进日志**；
- 以 **phagent** 身份运行：不在 sudo/docker 组，**自己不执行任何代码**——
  执行一律转给 Unix socket 后面以 **phcode** 身份运行的 worker；
- `/v1/execute` 阶段 4 **只接受内置固定样例名**，收到 `code`/`question` 直接拒绝；
  因此宿主侧仍会 fail closed，学生提交不会被执行。

用法：

    python3 ph_judge_agent.py --bind 192.168.200.10 --port 9443 \\
        --token-file /etc/ph-judge-agent/token

能力位：`execute` 由代码决定；其余四项只能由运维写
`/etc/ph-judge-agent/capabilities.json`（验收通过后再翻）。`ready_for_controlled_demo`
必须四项全 true —— **固定样例跑通只证明 agent 能执行，不证明学生代码安全**。
"""

import argparse
import hmac
import json
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AGENT_VERSION = "0.2.0"
PROTOCOL_VERSION = 1
MAX_BODY_BYTES = 1 * 1024 * 1024          # 1 MiB，超出直接 413
WORKER_SOCKET = os.environ.get("PH_WORKER_SOCKET", "/run/ph-judge/worker.sock")
CAPABILITIES_FILE = os.environ.get(
    "PH_CAPABILITIES_FILE", "/etc/ph-judge-agent/capabilities.json")

#: 能力声明。`execute` 由本进程的实际能力决定（只转内置固定样例）；
#: 其余四项**只能由运维在验收通过后写进 capabilities.json**，默认全 false。
CAPABILITIES = {
    "execute": True,          # 阶段 4：可执行内置固定样例（不接受任意代码）
    "file_isolation": False,
    "network_isolation": False,
    "resource_limits": False,
    "process_tree_kill": False,
}


def load_capabilities(path: str = CAPABILITIES_FILE) -> dict:
    caps = dict(CAPABILITIES)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
    except (OSError, json.JSONDecodeError):
        stored = {}
    if isinstance(stored, dict):
        for key in ("file_isolation", "network_isolation",
                    "resource_limits", "process_tree_kill"):
            caps[key] = stored.get(key) is True
    caps["execute"] = True
    return caps


def readiness_of(caps: dict) -> str:
    """分层就绪度：固定样例通过只到 `execution_probe_passed`。"""
    isolation_keys = ("file_isolation", "network_isolation",
                      "resource_limits", "process_tree_kill")
    if caps.get("execute") and all(caps.get(k) for k in isolation_keys):
        return "ready_for_controlled_demo"
    if caps.get("execute"):
        return "execution_probe_passed"
    return "not_ready"


def call_worker(payload: dict, timeout: float = 20.0) -> dict:
    """把请求转给 **phcode** 身份的 worker；agent 自己不执行任何代码。"""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(WORKER_SOCKET)
        sock.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    line, _, _rest = buf.partition(b"\n")
    try:
        reply = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        reply = {}
    return reply if isinstance(reply, dict) else {}


def _json_bytes(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


class AgentHandler(BaseHTTPRequestHandler):
    server_version = "ph-judge-agent/" + AGENT_VERSION
    protocol_version = "HTTP/1.1"

    # ---------------- 基础设施 ----------------

    def _send(self, code: int, payload: dict) -> None:
        body = _json_bytes(payload)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        """返回解析后的 dict；非法 JSON 或超限返回 None（由调用方回 400/413）。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Agent-Token") or ""
        expected = getattr(self.server, "token", "")
        if not supplied or not expected:
            return False
        # 定长比较，避免按字节提前返回
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def log_message(self, fmt, *args):      # 不打印请求头与请求体
        sys.stderr.write("[agent] %s %s\n" % (self.address_string(), fmt % args))
        sys.stderr.flush()

    # ---------------- 路由 ----------------

    def do_GET(self):
        if self.path == "/healthz":
            caps = self.server.capabilities
            return self._send(200, {
                "ok": True, "agent": "ph-judge-agent",
                "version": AGENT_VERSION, "protocol": PROTOCOL_VERSION,
                "capabilities": caps,
                "readiness": readiness_of(caps),
            })
        self._send(404, {"ok": False, "error_code": "agent_rejected",
                         "message": "未知路径"})

    def do_POST(self):
        payload = self._read_json()
        if payload is None:
            return self._send(400, {"ok": False, "error_code": "agent_rejected",
                                    "message": "请求体必须是 JSON 对象且不超过 1 MiB"})

        if self.path == "/v1/handshake":
            if not self._authorized():
                return self._send(401, {"ok": False, "error_code": "agent_rejected",
                                        "message": "token 校验失败"})
            if payload.get("protocol") != PROTOCOL_VERSION:
                return self._send(409, {"ok": False, "error_code": "agent_rejected",
                                        "message": "协议版本不匹配",
                                        "protocol": PROTOCOL_VERSION})
            return self._send(200, {"ok": True, "protocol": PROTOCOL_VERSION,
                                    "agent_version": AGENT_VERSION,
                                    "capabilities": self.server.capabilities,
                                    "readiness": readiness_of(self.server.capabilities)})

        if self.path == "/v1/execute":
            if not self._authorized():
                return self._send(401, {"ok": False, "error_code": "agent_rejected",
                                        "message": "token 校验失败"})
            # 阶段 4：**只接受内置固定样例**，绝不把请求体里的代码当学生代码执行
            fixture = payload.get("fixture")
            if "code" in payload or "question" in payload:
                return self._send(503, {"ok": False, "status": "error",
                                        "error_code": "agent_rejected",
                                        "message": "阶段 4 只接受内置固定样例，不接受任意代码"})
            if not isinstance(fixture, str):
                return self._send(503, {"ok": False, "status": "error",
                                        "error_code": "agent_rejected",
                                        "message": "缺少 fixture 字段"})
            try:
                reply = call_worker({"op": "execute_fixture", "fixture": fixture})
            except OSError as exc:
                return self._send(503, {"ok": False, "status": "error",
                                        "error_code": "agent_unreachable",
                                        "message": f"worker 不可达：{exc.__class__.__name__}"})
            if not reply.get("ok"):
                return self._send(503, dict(reply, status="error"))
            return self._send(200, dict(reply, ok=True, agent_version=AGENT_VERSION))

        self._send(404, {"ok": False, "error_code": "agent_rejected",
                         "message": "未知路径"})


def build_server(bind: str, port: int, token: str) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((bind, port), AgentHandler)
    httpd.daemon_threads = True
    httpd.token = token
    httpd.capabilities = load_capabilities()
    return httpd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ph-judge-agent（阶段 2）")
    ap.add_argument("--bind", default="127.0.0.1",
                   help="监听地址；VM 内应为内部交换机地址 192.168.200.10")
    ap.add_argument("--port", type=int, default=9443)
    ap.add_argument("--token-file", default="/etc/ph-judge-agent/token")
    args = ap.parse_args(argv)

    try:
        with open(args.token_file, "r", encoding="utf-8") as f:
            token = f.read().strip()
    except OSError as exc:
        print(f"读取 token 失败：{exc}", file=sys.stderr)
        return 2
    if len(token) < 16:
        print("token 太短（要求 ≥16 字符）", file=sys.stderr)
        return 2

    httpd = build_server(args.bind, args.port, token)
    caps = httpd.capabilities
    print(f"ph-judge-agent {AGENT_VERSION} 监听 {args.bind}:{args.port}"
          f"（worker={WORKER_SOCKET} readiness={readiness_of(caps)}）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
