"""判题运行器（契约 §1）。

`JudgeResult` 的字段与 `judge()` 的签名由岗位 1 冻结，不得更改。
实现约定见 `docs/判题模块交接说明.md` 与 `docs/contracts.md` §1：

- 只用标准库；`subprocess` + **每用例 5 秒**超时；学生代码只在子进程里跑；
- 在**系统临时目录**运行，跑完即删；
- 子进程环境变量走**白名单**（不把模型 Key 等机密交出去）；
- 超时先杀**整棵进程树**，再收割管道（否则孙进程占着管道会把主进程挂死）；
- 输出先规范化（统一换行 → 去每行行尾空白 → 去末尾空行）再精确比对；
- `hidden=true` 的用例**不回显** `input` / `expected`。

隔离（文件边界 / 禁网 / 资源限额 / 低权身份）由 `app/harness/isolation.py`
定义的适配器负责；适配器不可用时调用方应抛 `IsolationUnavailable` 拒绝执行，
不得静默退回宿主上的普通 subprocess。
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

#: 每用例超时（契约 §1 定死 5 秒）
TIMEOUT_SECONDS = 5

#: stdout / stderr / 未过用例 actual 的截断上限（契约：2000 字符）
OUTPUT_LIMIT = 2000

#: 杀掉进程树后等管道关闭的宽限秒数
_REAP_GRACE_SECONDS = 3

#: 允许继承给子进程的环境变量（白名单）；其余一律不传
_ENV_ALLOW = frozenset({
    "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR",
    "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LC_ALL", "TZ",
})


@dataclass
class JudgeResult:
    status: str                 # pass / fail / timeout / crash / error
    total_cases: int
    passed_cases: int
    failed_cases: list = field(default_factory=list)
    error_type: str = "none"    # 执行层：none / syntax / runtime / timeout / wrong_output
    stderr: str = ""
    stdout: str = ""
    exec_ms: int = 0
    exit_code: int | None = None


def _child_env() -> dict:
    """子进程环境变量白名单：不继承模型 Key、数据库路径、代理凭据等。"""
    env = {key: value for key, value in os.environ.items() if key.upper() in _ENV_ALLOW}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _normalize(text: str) -> str:
    """规范化：统一换行 → 去每行行尾空白 → 去末尾空行（契约 §1）。"""
    unified = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in unified.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _kill_tree(proc: subprocess.Popen) -> None:
    """杀掉整棵进程树（不只最外层 python 进程）。"""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def _spawn(path: str, cwd: str, env: dict) -> subprocess.Popen:
    """起一个判题子进程：独立进程组（POSIX）、无控制台窗口（Windows）、UTF-8 管道。"""
    return subprocess.Popen(
        [sys.executable, path],
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=(os.name != "nt"),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )


def _looks_like_syntax_error(stderr: str) -> bool:
    return any(marker in stderr for marker in ("SyntaxError", "IndentationError", "TabError"))


def judge(question: dict, code: str, language: str = "python") -> JudgeResult:
    """跑测试用例并返回 JudgeResult（签名由契约冻结）。"""
    if language and language != "python":
        return JudgeResult(
            status="error",
            total_cases=0,
            passed_cases=0,
            error_type="runtime",
            stderr=f"暂只支持 python，收到 language={language!r}",
        )

    cases = question.get("test_cases") or []
    total = len(cases)
    if not total:
        return JudgeResult(
            status="error",
            total_cases=0,
            passed_cases=0,
            error_type="runtime",
            stderr="题目未配置测试用例，无法判题",
        )

    tmpdir = tempfile.mkdtemp(prefix="ph-judge-")
    path = os.path.join(tmpdir, "main.py")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(code)

        env = _child_env()
        passed = 0
        failed = []
        status = "pass"
        error_type = "none"
        last_stdout = ""
        last_stderr = ""
        last_exit = None
        started = time.time()

        for index, case in enumerate(cases):
            hidden = bool(case.get("hidden", False))
            stdin_text = str(case.get("input", "")) + "\n"
            proc = _spawn(path, tmpdir, env)
            try:
                out, err = proc.communicate(input=stdin_text, timeout=TIMEOUT_SECONDS)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                try:
                    out, err = proc.communicate(timeout=_REAP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    out, err = "", ""
                last_stdout = out or ""
                last_stderr = err or ""
                last_exit = None
                status, error_type = "timeout", "timeout"
                break

            last_stdout = out or ""
            last_stderr = err or ""
            last_exit = exit_code

            if exit_code != 0:
                if _looks_like_syntax_error(last_stderr):
                    status, error_type = "fail", "syntax"
                else:
                    status, error_type = "crash", "runtime"
                break

            actual = _normalize(last_stdout)
            expected = _normalize(str(case.get("expected", "")))
            if actual == expected:
                passed += 1
                continue

            failed.append({
                "case_index": index,
                # hidden 用例不回显输入与期望输出（契约 §1、防泄题）
                "input": None if hidden else case.get("input", ""),
                "expected": None if hidden else case.get("expected", ""),
                "actual": actual[:OUTPUT_LIMIT],
                "hidden": hidden,
            })
            status, error_type = "fail", "wrong_output"

        elapsed_ms = int((time.time() - started) * 1000)
        return JudgeResult(
            status=status,
            total_cases=total,
            passed_cases=passed,
            failed_cases=failed,
            error_type=error_type,
            stderr=(last_stderr or "")[:OUTPUT_LIMIT],
            stdout=(last_stdout or "")[:OUTPUT_LIMIT],
            exec_ms=elapsed_ms,
            exit_code=last_exit,
        )
    except OSError as exc:      # 解释器找不到、临时目录不可写等 —— 判题器自身故障
        return JudgeResult(
            status="error",
            total_cases=total,
            passed_cases=0,
            error_type="runtime",
            stderr=f"判题器自身故障：{type(exc).__name__}",
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
