#!/usr/bin/env python3
"""ph-judge-agent —— VM 内最小判题 agent（**阶段 2：只有健康检查与握手**）。

设计约束（评阅 m00935）：

- 只用标准库，不装依赖：VM 目前离线，`pip` 也没有；
- 只监听内部交换机地址，默认 `192.168.200.10:9443`；
- 所有 `/v1/*` 请求都要求 `X-Agent-Token`，**token 只从文件读，不进日志**；
- **执行能力未实现**：`/v1/execute` 明确返回 `503 + agent_rejected`，
  绝不"先跑起来再加限制"。宿主因此会 fail closed（`judge_unavailable`）。

用法：

    python3 ph_judge_agent.py --bind 192.168.200.10 --port 9443 \\
        --token-file /etc/ph-judge-agent/token

后续阶段（执行、cgroup、netns、低权用户）都在本文件内增量实现，
**未通过 `tools/accept_isolation.py` 前不得把 `execute` 置为 true**。
"""

import argparse
import hmac
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AGENT_VERSION = "0.1.0"
PROTOCOL_VERSION = 1
MAX_BODY_BYTES = 1 * 1024 * 1024          # 1 MiB，超出直接 413

#: 能力声明。**只有真正实现并通过验收的项才能置 true**；
#: 阶段 2 全部为 false —— 宿主侧 `check_isolation()` 会因此拒绝执行。
CAPABILITIES = {
    "execute": False,
    "file_isolation": False,
    "network_isolation": False,
    "resource_limits": False,
    "process_tree_kill": False,
}


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
            return self._send(200, {
                "ok": True, "agent": "ph-judge-agent",
                "version": AGENT_VERSION, "protocol": PROTOCOL_VERSION,
                "capabilities": CAPABILITIES,
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
                                    "capabilities": CAPABILITIES})

        if self.path == "/v1/execute":
            if not self._authorized():
                return self._send(401, {"ok": False, "error_code": "agent_rejected",
                                        "message": "token 校验失败"})
            # 阶段 2：执行能力未实现 —— 明确拒绝，不返回任何"假结果"
            return self._send(503, {"ok": False, "status": "error",
                                    "error_code": "agent_rejected",
                                    "message": "执行能力尚未启用（阶段 2 只提供健康检查与握手）"})

        self._send(404, {"ok": False, "error_code": "agent_rejected",
                         "message": "未知路径"})


def build_server(bind: str, port: int, token: str) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((bind, port), AgentHandler)
    httpd.daemon_threads = True
    httpd.token = token
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
    print(f"ph-judge-agent {AGENT_VERSION} 监听 {args.bind}:{args.port}"
          f"（execute={'on' if CAPABILITIES['execute'] else 'off'}）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
