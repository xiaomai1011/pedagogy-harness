#!/usr/bin/env python3
"""ph-judge-worker —— VM 内的**受限执行进程**（阶段 4：只跑内置固定样例）。

职责边界（评阅 m00935 / m01225）：

- 以 **phcode** 身份运行：不在 sudo / docker 组，读不到 agent token 与 SSH 配置；
- 只监听 Unix socket（`/run/ph-judge/worker.sock`），**不监听任何网络端口**；
- 只接受对端 uid == `phagent` 的连接（`SO_PEERCRED`），学生代码即使以 phcode
  身份跑起来，也冒充不了 agent 的连接；
- **只执行本文件内定义的固定样例**（fixture）。阶段 4 不接收任意代码，
  收到 `code`/`question` 一律拒绝；
- 每请求独立临时目录、环境变量白名单、5 秒超时、杀整棵进程树、
  stdout/stderr 上限、结束清理目录；清理失败**如实上报**，不伪装成功。

用法：

    python3 ph_judge_worker.py --socket /run/ph-judge/worker.sock
"""

import argparse
import grp
import json
import os
import pwd
import resource
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time

WORKER_VERSION = "0.1.0"
PROTOCOL_VERSION = 1

TIMEOUT_SECONDS = 5.0
REAP_GRACE_SECONDS = 3.0
OUTPUT_LIMIT = 2000
MAX_LINE_BYTES = 64 * 1024

#: 环境变量白名单：不继承 token、代理、模型密钥等任何宿主侧变量
ENV_ALLOW = (
    "PATH", "LANG", "LC_ALL", "TZ", "HOME", "TMPDIR",
)
ENV_FIXED = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "HOME": "/var/lib/ph-code",
}

#: 固定样例。键名即协议里的 fixture 名；**不在表内的一律拒绝**。
FIXTURES = {
    "print_ok": {
        "note": "正常输出",
        "code": 'print("ok")\n',
        "expect_stdout": "ok",
        "expect_status": "pass",
    },
    "exit_zero": {
        "note": "正常退出",
        "code": 'raise SystemExit(0)\n',
        "expect_status": "pass",
    },
    "timeout_loop": {
        "note": "固定超时样例（死循环）",
        "code": 'while True:\n    pass\n',
        "expect_status": "timeout",
    },
    "output_flood": {
        "note": "固定输出过量样例",
        "code": 'import sys\nsys.stdout.write("x" * 20000)\n',
        "expect_status": "pass",
    },
    "spawn_child": {
        "note": "固定派生子进程样例",
        "code": (
            'import subprocess, sys, time\n'
            'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])\n'
            'time.sleep(0.3)\n'
            'print("parent-done")\n'
        ),
        "expect_status": "pass",
    },
    "network_probe": {
        "note": "网络访问探针（期望被拒绝）",
        "code": (
            'import socket\n'
            's = socket.socket()\n'
            's.settimeout(2)\n'
            'try:\n'
            '    s.connect(("192.168.200.1", 22))\n'
            '    print("network-open")\n'
            'except OSError as exc:\n'
            '    print("network-blocked:", type(exc).__name__)\n'
        ),
        "expect_status": "pass",
    },
    "token_probe": {
        "note": "读 agent token 探针（期望失败）",
        "code": (
            'import os\n'
            'try:\n'
            '    os.open("/etc/ph-judge-agent/token", os.O_RDONLY)\n'
            '    print("token-readable")\n'
            'except OSError as exc:\n'
            '    print("token-denied:", type(exc).__name__)\n'
            'try:\n'
            '    open("/etc/ph-judge-agent/capabilities.json", "a").write("x")\n'
            '    print("agent-config-writable")\n'
            'except OSError as exc:\n'
            '    print("agent-config-denied:", type(exc).__name__)\n'
        ),
        "expect_status": "pass",
    },
    "identity_probe": {
        "note": "执行身份与 sudo/密钥可读性探针",
        "code": (
            'import os, subprocess\n'
            'print("uid=%d gid=%d groups=%s" % (os.getuid(), os.getgid(), sorted(os.getgroups())))\n'
            'try:\n'
            '    res = subprocess.run(["sudo", "-n", "true"], capture_output=True, text=True)\n'
            '    print("sudo-rc=%d" % res.returncode)\n'
            'except OSError as exc:\n'
            '    print("sudo-error:", type(exc).__name__)\n'
            'for path in ("/etc/ph-judge-agent/token", "/home/phjudge/.ssh/id_ed25519",\n'
            '             "/etc/sudoers.d/90-ph-judge-nopasswd"):\n'
            '    try:\n'
            '        open(path, "rb").read(16)\n'
            '        print("readable:", path)\n'
            '    except OSError as exc:\n'
            '        print("denied:", path, type(exc).__name__)\n'
        ),
        "expect_status": "pass",
    },
    "network_probe_local": {
        "note": "连本机 agent 端口（验证单元级禁网）",
        "code": (
            'import socket\n'
            's = socket.socket()\n'
            's.settimeout(2)\n'
            'try:\n'
            '    s.connect(("192.168.200.10", 9443))\n'
            '    print("agent-reachable")\n'
            'except OSError as exc:\n'
            '    print("agent-blocked:", type(exc).__name__)\n'
        ),
        "expect_status": "pass",
    },
    "file_probe": {
        "note": "文件边界探针：写 agent 目录/管理账户目录，读管理密钥",
        "code": (
            'import os\n'
            'targets = ["/opt/ph-judge-agent/tamper.txt", "/etc/ph-judge-agent/tamper.txt",\n'
            '           "/home/phjudge/tamper.txt"]\n'
            'for path in targets:\n'
            '    try:\n'
            '        open(path, "w").write("x")\n'
            '        print("writable:", path)\n'
            '    except OSError as exc:\n'
            '        print("write-denied:", path, type(exc).__name__)\n'
            'for path in ("/home/phjudge/.ssh", "/home/phjudge"):\n'
            '    try:\n'
            '        os.listdir(path)\n'
            '        print("listable:", path)\n'
            '    except OSError as exc:\n'
            '        print("list-denied:", path, type(exc).__name__)\n'
            'print("cwd:", os.getcwd())\n'
        ),
        "expect_status": "pass",
    },
    "memory_probe": {
        "note": "资源上限探针：申请 400MB（RLIMIT_AS=256MB 应失败）",
        "code": (
            'try:\n'
            '    blob = bytearray(400 * 1024 * 1024)\n'
            '    print("allocated", len(blob))\n'
            'except MemoryError:\n'
            '    print("memory-denied: MemoryError")\n'
        ),
        "expect_status": "pass",
    },
}


def _child_env() -> dict:
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    env.update(ENV_FIXED)
    return env


def _limits() -> None:                      # pragma: no cover - 只能在 phcode 下跑
    """子进程资源上限（单元文件之外的第二道保险）。"""
    resource.setrlimit(resource.RLIMIT_CPU, (6, 6))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1 * 1024 * 1024, 1 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NPROC, (16, 16))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _kill_tree(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _group_alive(pgid: int, wait_seconds: float) -> bool:
    """等进程组彻底消失；返回 True 表示还有残留。"""
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        time.sleep(0.05)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def run_fixture(name: str, timeout: float = TIMEOUT_SECONDS,
                output_limit: int = OUTPUT_LIMIT) -> dict:
    spec = FIXTURES.get(name)
    if spec is None:
        return {"ok": False, "error_code": "agent_rejected",
                "message": "未知样例"}

    work_root = os.environ.get("PH_WORK_ROOT", "/run/ph-judge/work")
    os.makedirs(work_root, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="job-", dir=work_root)
    started = time.monotonic()
    exit_code = None
    timed_out = False
    leftover = False
    try:
        script = os.path.join(tmpdir, "main.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(spec["code"])
        out_path = os.path.join(tmpdir, ".stdout")
        err_path = os.path.join(tmpdir, ".stderr")
        # 用文件而不是管道：孙进程继承管道会让父进程退出后仍拿不到 EOF
        with open(out_path, "wb") as fout, open(err_path, "wb") as ferr:
            proc = subprocess.Popen(
                [sys.executable, script],
                cwd=tmpdir, env=_child_env(), stdin=subprocess.DEVNULL,
                stdout=fout, stderr=ferr,
                start_new_session=True, preexec_fn=_limits,
            )
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(proc.pid)
                try:
                    proc.wait(timeout=REAP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
            exit_code = proc.returncode
            # 主进程结束后一律清理整个进程组：派生进程不得残留
            _kill_tree(proc.pid)
            leftover = _group_alive(proc.pid, 2.0)
        with open(out_path, "rb") as fh:
            stdout = fh.read(output_limit + 1).decode("utf-8", "replace")
        with open(err_path, "rb") as fh:
            stderr = fh.read(output_limit + 1).decode("utf-8", "replace")
    except OSError as exc:
        return {"ok": False, "error_code": "execution_error",
                "message": f"启动失败：{exc.__class__.__name__}"}

    cleanup_status = "ok"
    try:
        shutil.rmtree(tmpdir)
    except OSError as exc:
        cleanup_status = f"dir_cleanup_failed: {exc.__class__.__name__}"
    if leftover:
        cleanup_status = "leftover_processes"

    elapsed_ms = int((time.monotonic() - started) * 1000)
    clipped_out = stdout[:output_limit]
    clipped_err = stderr[:output_limit]
    if timed_out:
        status = "timeout"
    elif exit_code != 0:
        status = "crash"
    else:
        status = "pass"
    return {
        "ok": True,
        "fixture": name,
        "status": status,
        "stdout": clipped_out,
        "stderr": clipped_err,
        "exit_code": exit_code,
        "elapsed_ms": elapsed_ms,
        "timed_out": timed_out,
        "cleanup_status": cleanup_status,
        "expect_status": spec.get("expect_status"),
        "expect_stdout": spec.get("expect_stdout"),
    }


def _peer_is_agent(conn: socket.socket) -> bool:
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid == pwd.getpwnam("phagent").pw_uid
    except (OSError, KeyError):
        return False


def _handle(conn: socket.socket) -> None:
    conn.settimeout(15.0)
    buf = b""
    while b"\n" not in buf and len(buf) <= MAX_LINE_BYTES:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
    line, _, _rest = buf.partition(b"\n")
    try:
        req = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        req = None

    if not isinstance(req, dict):
        reply = {"ok": False, "error_code": "agent_rejected", "message": "请求必须是 JSON 对象"}
    elif req.get("op") == "ping":
        reply = {"ok": True, "worker": "ph-judge-worker", "version": WORKER_VERSION,
                 "protocol": PROTOCOL_VERSION, "fixtures": sorted(FIXTURES)}
    elif req.get("op") == "execute_fixture":
        if "code" in req or "question" in req:
            reply = {"ok": False, "error_code": "agent_rejected",
                     "message": "阶段 4 不接受任意代码，只接受内置固定样例"}
        else:
            name = req.get("fixture")
            if isinstance(name, str) and name in FIXTURES:
                reply = run_fixture(name)
            else:
                reply = {"ok": False, "error_code": "agent_rejected",
                         "message": "未知样例"}
    else:
        reply = {"ok": False, "error_code": "agent_rejected", "message": "未知操作"}

    conn.sendall(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")


def serve(socket_path: str) -> int:
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(socket_path)
    os.chmod(socket_path, 0o660)
    try:
        # socket 属组默认继承父目录（可能不是 ph-judge），显式改成共享组，
        # 这样只有 phagent（以及同组的 worker 自己）能连
        os.chown(socket_path, os.getuid(), grp.getgrnam("ph-judge").gr_gid)
    except (KeyError, OSError):
        pass
    srv.listen(8)
    print(f"ph-judge-worker {WORKER_VERSION} 监听 {socket_path}"
          f"（uid={os.getuid()} euid={os.geteuid()}）", flush=True)
    while True:
        conn, _addr = srv.accept()
        try:
            if not _peer_is_agent(conn):
                conn.sendall(b'{"ok":false,"error_code":"agent_rejected",'
                             b'"message":"peer not allowed"}\n')
                continue
            _handle(conn)
        except Exception as exc:                      # noqa: BLE001
            sys.stderr.write(f"[worker] 处理失败：{exc.__class__.__name__}\n")
        finally:
            conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ph-judge-worker（阶段 4）")
    ap.add_argument("--socket", default=os.environ.get(
        "PH_WORKER_SOCKET", "/run/ph-judge/worker.sock"))
    args = ap.parse_args(argv)
    try:
        return serve(args.socket)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
