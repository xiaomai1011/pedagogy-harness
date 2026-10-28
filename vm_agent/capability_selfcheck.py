#!/usr/bin/env python3
"""VM 侧隔离能力自检（阶段 5）。

**只有四项全部实测通过，才写 `capabilities.json` 把能力位翻 true。**
四项分别对应评阅 m01647 / m01798 要求的文件、网络、资源、进程树边界：

1. `file_isolation`：读不到 agent token / 管理员 SSH 私钥，改不了 agent 程序，
   列不了 `/home/phjudge`；
2. `network_isolation`：连不上宿主 22、本机 9443、公网 53，也解析不了域名；
3. `resource_limits`：申请 400 MB 触发 `MemoryError`，输出被截到 2000 字符；
4. `process_tree_kill`：派生的 `sleep 300` 在样例结束后不存活。

脚本以 **root** 在 VM 上运行（读 token、写 capabilities.json），但**所有探针
都在 phcode 的 worker 沙箱里执行**——root 只负责发起请求与裁决。

用法::

    sudo python3 vm_agent/capability_selfcheck.py            # 只测，不写
    sudo python3 vm_agent/capability_selfcheck.py --write     # 全过才写 capabilities.json
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://192.168.200.10:9443"
DEFAULT_TOKEN = "/etc/ph-judge-agent/token"
DEFAULT_CAPS = "/etc/ph-judge-agent/capabilities.json"

FILE_PROBE = r'''
import json, os
out = {}
for name, path, mode in [
    ("token", "/etc/ph-judge-agent/token", "r"),
    ("ssh_key", "/home/phjudge/.ssh/id_ed25519", "r"),
    ("agent_src", "/opt/ph-judge-agent/ph_judge_agent.py", "a"),
    ("sudoers", "/etc/sudoers.d/90-ph-judge-nopasswd", "r"),
]:
    try:
        open(path, mode).close()
        out[name] = "allowed"
    except Exception as exc:
        out[name] = type(exc).__name__
try:
    os.listdir("/home/phjudge")
    out["list_home"] = "allowed"
except Exception as exc:
    out["list_home"] = type(exc).__name__
print(json.dumps(out))
'''

NETWORK_PROBE = r'''
import json, socket
out = {}
for name, host, port in [("host_ssh", "192.168.200.1", 22),
                         ("self_agent", "192.168.200.10", 9443),
                         ("public_dns", "8.8.8.8", 53)]:
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect((host, port))
        out[name] = "open"
    except Exception as exc:
        out[name] = type(exc).__name__
    finally:
        s.close()
try:
    socket.gethostbyname("example.com")
    out["dns"] = "resolved"
except Exception as exc:
    out["dns"] = type(exc).__name__
print(json.dumps(out))
'''

RESOURCE_PROBE = r'''
import json
out = {}
try:
    blob = bytearray(400 * 1024 * 1024)
    out["memory"] = "allocated:%d" % len(blob)
except MemoryError:
    out["memory"] = "MemoryError"
except Exception as exc:
    out["memory"] = type(exc).__name__
import sys
print(json.dumps(out))
sys.stdout.write("x" * 100000)
'''

SPAWN_PROBE = r'''
import json, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
print(json.dumps({"child_pid": child.pid}))
time.sleep(0.3)
'''

PROBE_SURVIVE = r'''
import json, os, sys
pid = int(sys.argv[1]) if len(sys.argv) > 1 else int(input())
try:
    os.kill(pid, 0)
    print(json.dumps({"alive": True}))
except ProcessLookupError:
    print(json.dumps({"alive": False}))
except PermissionError:
    print(json.dumps({"alive": True, "why": "PermissionError"}))
'''


def _post(url: str, token: str, payload: dict, timeout: float = 90.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url + "/v1/execute", data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("X-Agent-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return {"ok": False, "http_status": exc.code, "raw": body}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _run(url: str, token: str, code: str, stdin: str = "", timeout: float = 5.0) -> dict:
    return _post(url, token, {
        "code": code,
        "test_cases": [{"input": stdin, "expected": stdin, "hidden": False}],
        "timeout": timeout,
    })


def _stdout_of(reply: dict) -> str:
    return (reply.get("stdout") or "").strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="VM 隔离能力自检")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--token-file", default=DEFAULT_TOKEN)
    ap.add_argument("--caps-file", default=DEFAULT_CAPS)
    ap.add_argument("--write", action="store_true", help="四项全过时写 capabilities.json")
    args = ap.parse_args(argv)

    token = open(args.token_file, "r", encoding="utf-8").read().strip()
    results = {}
    detail = {}

    # ---- 1 文件隔离 ----
    reply = _run(args.url, token, FILE_PROBE)
    out = _stdout_of(reply)
    try:
        seen = json.loads(out)
    except json.JSONDecodeError:
        seen = {}
    file_ok = (
        seen.get("token") not in (None, "allowed")
        and seen.get("ssh_key") not in (None, "allowed")
        and seen.get("agent_src") not in (None, "allowed")
        and seen.get("sudoers") not in (None, "allowed")
        and seen.get("list_home") not in (None, "allowed")
    )
    results["file_isolation"] = file_ok
    detail["file_isolation"] = seen

    # ---- 2 网络隔离 ----
    reply = _run(args.url, token, NETWORK_PROBE, timeout=5.0)
    out = _stdout_of(reply)
    try:
        seen = json.loads(out)
    except json.JSONDecodeError:
        seen = {}
    net_ok = all(seen.get(k) not in (None, "open") for k in
                 ("host_ssh", "self_agent", "public_dns", "dns"))
    results["network_isolation"] = net_ok
    detail["network_isolation"] = seen

    # ---- 3 资源上限 ----
    reply = _run(args.url, token, RESOURCE_PROBE, timeout=5.0)
    stdout_len = len(reply.get("stdout") or "")
    mem_ok = False
    try:
        mem_ok = json.loads((reply.get("stdout") or "").split("x")[0]).get("memory") == "MemoryError"
    except (json.JSONDecodeError, AttributeError):
        mem_ok = "MemoryError" in (reply.get("stdout") or "")
    limits_ok = mem_ok and stdout_len <= 2000
    results["resource_limits"] = limits_ok
    detail["resource_limits"] = {"memory": mem_ok, "stdout_len": stdout_len}

    # ---- 4 进程树清理 ----
    reply = _run(args.url, token, SPAWN_PROBE, timeout=5.0)
    child_pid = None
    try:
        child_pid = json.loads(_stdout_of(reply)).get("child_pid")
    except json.JSONDecodeError:
        pass
    if isinstance(child_pid, int):
        check = _run(args.url, token, "import os,sys\npid=%d\n"
                     "try:\n    os.kill(pid, 0)\n    print('alive')\n"
                     "except ProcessLookupError:\n    print('gone')\n"
                     "except PermissionError:\n    print('alive')\n" % child_pid)
        alive = "alive" in _stdout_of(check)
        tree_ok = not alive
    else:
        tree_ok = False
    results["process_tree_kill"] = tree_ok
    detail["process_tree_kill"] = {"child_pid": child_pid, "cleaned": tree_ok}

    ok = all(results.values())
    report = {"ok": ok, "results": results, "detail": detail,
              "agent": reply.get("agent_version")}
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.write:
        if not ok:
            print("拒绝写 capabilities.json：仍有能力项未通过", file=sys.stderr)
            return 1
        caps = {"execute": True,
                "file_isolation": True, "network_isolation": True,
                "resource_limits": True, "process_tree_kill": True,
                "verified_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                "verified_by": "vm_agent/capability_selfcheck.py"}
        with open(args.caps_file, "w", encoding="utf-8") as fh:
            json.dump(caps, fh, ensure_ascii=False, indent=2)
        print(f"[written] {args.caps_file}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
