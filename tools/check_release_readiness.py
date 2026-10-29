"""公开仓库前的只读检查（不修改任何文件、不联网）。

用法：
    python tools/check_release_readiness.py

退出码：0 = 未发现阻塞项；1 = 有阻塞项，先别改成 Public。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SECRET_PATTERNS = [
    ("模型 Key（sk-…）", re.compile(r"sk-[A-Za-z0-9]{16,}")),
    ("64 位十六进制密钥", re.compile(r"\b[0-9a-f]{64}\b")),
    ("访问码（v1.eyJ…）", re.compile(r"v1\.eyJ[A-Za-z0-9_\-]{10,}")),
    ("私钥块", re.compile(r"BEGIN (OPENSSH|RSA|EC|PGP) PRIVATE KEY")),
]
INTERNAL_IP = re.compile(r"\b(?:10\.30\.34\.|192\.168\.200\.|10\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
SENSITIVE_PATHS = re.compile(r"(^|/)(\.env$|.*\.sqlite3?$|.*\.db$|.*\.log$|.*\.key$|.*token.*$|.*secret.*$|docs/exports/|data/backups/)")

results: list[tuple[str, str, str]] = []  # (level, title, detail)


def git(*args: str, timeout: int = 120) -> str:
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return proc.stdout or ""


def add(level: str, title: str, detail: str = "") -> None:
    results.append((level, title, detail))


def check_license() -> None:
    names = {p.name.upper() for p in ROOT.iterdir() if p.is_file()}
    if "LICENSE" in names or "LICENSE.MD" in names or "COPYING" in names:
        add("PASS", "许可证文件存在")
    else:
        add("FAIL", "缺少 LICENSE", "公开前必须选定许可证（MIT / Apache-2.0 / 其它）")


def check_tracked_paths() -> None:
    tracked = [line for line in git("ls-files").splitlines() if line.strip()]
    hits = [p for p in tracked if SENSITIVE_PATHS.search(p.replace("\\", "/"))]
    if hits:
        add("FAIL", f"受控文件里有 {len(hits)} 个敏感路径", "\n    ".join(hits[:12]))
    else:
        add("PASS", "没有被跟踪的 .env / 数据库 / 日志 / 密钥文件")


def check_history_secrets() -> None:
    # 只看改动内容，避免把二进制题库当作文本扫描
    diff = git("log", "--all", "-p", "--text", "--", ".", timeout=300)
    found: list[str] = []
    for name, pattern in SECRET_PATTERNS:
        match = pattern.search(diff)
        if match:
            found.append(f"{name}: {match.group(0)[:24]}…")
    if found:
        add("FAIL", "提交历史里出现疑似密钥", "；".join(found))
    else:
        add("PASS", "提交历史未发现密钥/私钥/访问码形态的字符串")


def check_question_bank() -> None:
    files = [line for line in git("ls-files", "data/questions").splitlines() if line.endswith(".json")]
    if not files:
        add("PASS", "题库没有进入版本控制")
        return
    sample = ROOT / files[0]
    text = sample.read_text(encoding="utf-8", errors="replace")
    has_solution = '"reference_solution"' in text
    has_hidden = '"hidden": true' in text or '"hidden":true' in text
    flags = []
    if has_solution:
        flags.append("含参考解")
    if has_hidden:
        flags.append("含隐藏用例")
    detail = "、".join(flags) + "；公开前需确认这些题不再用于测量"
    add("FAIL" if flags else "WARN", f"题库被跟踪：{len(files)} 个 JSON", detail)


def check_internal_ips() -> None:
    grep = git("grep", "-I", "-l", "-E", INTERNAL_IP.pattern, "--", "docs", "README.md", "app", "tools")
    files = [line for line in grep.splitlines() if line.strip()]
    if files:
        add("FAIL", f"{len(files)} 个受控文件含内网地址", "\n    ".join(files[:8]))
    else:
        add("PASS", "文档与代码里没有内网地址")


def check_exports() -> None:
    tracked = [line for line in git("ls-files").splitlines() if line.strip()]
    exports = [p for p in tracked if p.replace("\\", "/").startswith("docs/exports/")]
    if exports:
        add("WARN", f"受控的导出文件：{len(exports)} 个", "确认其中只有构造数据（DRYRUN 等），没有真实参与者数据")
    else:
        add("PASS", "没有受控的导出文件")


def main() -> int:
    if not (ROOT / ".git").exists():
        print("当前目录不是 git 仓库")
        return 1
    check_license()
    check_tracked_paths()
    check_history_secrets()
    check_question_bank()
    check_internal_ips()
    check_exports()

    fails = 0
    print("=" * 62)
    print("公开前检查（只读）")
    print("=" * 62)
    for level, title, detail in results:
        if level == "FAIL":
            fails += 1
        print(f"[{level}] {title}")
        if detail:
            for line in detail.splitlines():
                print(f"       {line}")
    print("-" * 62)
    print(f"阻塞项：{fails} 个")
    print("结论：" + ("可以进入人工复核与许可证确认" if fails == 0 else "现在不要改成 Public"))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
