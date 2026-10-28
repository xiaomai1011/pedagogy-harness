"""题目自洽性检查：**参考解必须能过自己的全部用例**。

在判题岗交付前，用最朴素的方式（subprocess + stdin + 规范化比对，与契约 §1 的实现约定一致）
提前发现"题自己都过不了"的问题：这类题一旦交到联调环节，会浪费判题岗与题库岗双方的时间。

覆盖范围：
- `data/questions/*.json`（岗位 4-A 将要交付的正式题库）
- `tests/fixtures/*.json`（开发期夹具，如洛谷样例）

题库为空时本测试自动跳过（不算失败）。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
QUESTION_DIRS = (REPO_ROOT / "data" / "questions", Path(__file__).resolve().parent / "fixtures")
TIMEOUT_SECONDS = 5


def _question_files() -> list[Path]:
    files: list[Path] = []
    for directory in QUESTION_DIRS:
        if directory.exists():
            files.extend(sorted(directory.glob("*.json")))
    return files


def _normalize(text: str) -> str:
    """与契约 §1 一致：统一换行 → 去行尾空白 → 去末尾空行。"""
    lines = [line.rstrip() for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _run(solution: str, stdin_text: str, workdir: Path):
    script = workdir / "solution.py"
    script.write_text(solution, encoding="utf-8")
    # 显式 UTF-8：判题器（契约 §1）用 UTF-8 跑学生代码，这里必须与之一致；
    # 否则在中文 Windows 上，含中文用例的输入会按 cp936 编码传给子进程而解码失败。
    return subprocess.run([sys.executable, str(script)], input=stdin_text + "\n",
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=TIMEOUT_SECONDS, cwd=workdir)


@pytest.mark.parametrize("path", _question_files(), ids=lambda p: p.stem)
def test_reference_solution_passes_every_case(path: Path, tmp_path: Path):
    question = json.loads(path.read_text(encoding="utf-8"))
    cases = question.get("test_cases") or []
    assert cases, f"{path.name} 没有任何 test_cases"

    for index, case in enumerate(cases, start=1):
        proc = _run(question["reference_solution"], case["input"], tmp_path)
        assert proc.returncode == 0, (
            f"{path.name} 用例{index}：参考解非零退出 —— {proc.stderr.strip()[:300]}")
        assert _normalize(proc.stdout) == _normalize(case["expected"]), (
            f"{path.name} 用例{index}：参考解输出 {proc.stdout!r} ≠ 期望 {case['expected']!r}")
