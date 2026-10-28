"""用真实大模型跑一次练习阶段提示（P001），核对内容/等级/防泄/延迟。

用途：正式实验前的收口核验。**不使用桩模型**，判题走 VM 上的 ph-judge-agent
（需要 `PH_VM_AGENT_TOKEN_FILE` 指向宿主保存的 agent token）。

用法（在仓库根目录）：

    python tools/real_model_hint_check.py [--question P001] [--keep-db]

输出：一段 JSON（stdout），包含状态、错误分类、知识点与若干防泄检查结果。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_TOKEN = pathlib.Path(
    os.path.expandvars(r"%LOCALAPPDATA%")) / "pedagogy-harness-launcher" / "vmkey" / "ph_judge_agent_token"

# 一段"能跑但算错"的学生代码：只看第一行输入，忽略后续行，典型初学错误。
BUGGY_CODE = """\
nums = list(map(int, input().split()))
print(nums[0])
"""

# 参考答案里不该出现在提示中的片段（防泄检查用）
LEAK_PATTERNS = (r"reference_solution", r"def solve", r"p001_reference")

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}


def _extract_level(text: str) -> str:
    """从提示文本里粗取"第几级/等级 N"，取不到返回空串（只作观察，不作断言）。"""
    match = re.search(r"(?:等级|级别|第)\s*([一二三四五1-5])\s*(?:级|层)?", text)
    if not match:
        return ""
    raw = match.group(1)
    return raw if raw.isdigit() else str(_CN_NUM.get(raw, ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", default="P001")
    parser.add_argument("--participant", default="REAL-CHECK-01")
    parser.add_argument("--keep-db", action="store_true")
    args = parser.parse_args()

    sys.path.insert(0, str(REPO))
    os.chdir(REPO)

    db_path = pathlib.Path(tempfile.gettempdir()) / f"ph_real_hint_{args.question}.sqlite"
    if db_path.exists():
        db_path.unlink()
    os.environ["PH_DB"] = str(db_path)
    os.environ["PH_VM_AGENT_ENABLED"] = "1"
    os.environ.setdefault("PH_VM_AGENT_TOKEN_FILE", str(DEFAULT_TOKEN))
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    from app import db
    from app.harness import isolation
    from app.harness.config_switch import PRESETS
    from app.harness.pipeline import handle_hint_request
    from app.harness.vm_agent_adapter import VmAgentAdapter
    from app.questions import load_question

    db.init_db()

    adapter = VmAgentAdapter()
    report: dict = {"question": args.question, "db": str(db_path)}
    report["readiness_before"] = adapter.readiness()

    started = time.time()
    report["check_isolation"] = adapter.check_isolation()
    isolation.register_execution_adapter(adapter)
    report["readiness_check_ms"] = round((time.time() - started) * 1000)

    question = load_question(args.question)
    started = time.time()
    response = handle_hint_request(
        participant_id=args.participant,
        question_id=args.question,
        code=BUGGY_CODE,
        config=PRESETS["exp"],
    )
    report["hint_ms"] = round((time.time() - started) * 1000)

    hint = response.get("message") or response.get("hint") or ""
    report.update({
        "status": response.get("status"),
        "error_type": response.get("error_type"),
        "knowledge_point": response.get("knowledge_point"),
        "source": response.get("source"),
        "hint_chars": len(hint),
        "hint_level": response.get("hint_level") or _extract_level(hint),
        "hint_head": hint[:160],
        "hint_tail": hint[-120:] if len(hint) > 160 else "",
        "model_used": "真实模型" if ".env" not in hint else "unknown",
    })

    # 防泄检查：提示里不得出现参考答案、隐藏用例输入/输出、token、内部路径
    haystack = json.dumps(response, ensure_ascii=False)
    leaks = [pat for pat in LEAK_PATTERNS if re.search(pat, haystack, re.IGNORECASE)]
    solution = question.get("reference_solution") or ""
    solution_lines = [line.strip() for line in solution.splitlines() if len(line.strip()) >= 12]
    leaks += [f"参考解片段: {line}" for line in solution_lines if line in haystack]
    hidden = [case for case in question.get("test_cases", []) if case.get("hidden")]
    for case in hidden:
        for field in ("input", "expected"):
            value = (case.get(field) or "").strip()
            if len(value) >= 3 and value in haystack:
                leaks.append(f"隐藏用例 {field}: {value[:40]}")
    # 结构化检查：failed_cases 里 hidden=True 的条目不得带 input/expected/actual 原文
    for item in response.get("failed_cases") or []:
        if item.get("hidden"):
            for field in ("input", "expected", "actual"):
                if (item.get(field) or "").strip():
                    leaks.append(f"隐藏失败用例泄露 {field}")
    token_file = pathlib.Path(os.environ["PH_VM_AGENT_TOKEN_FILE"])
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if token and token in haystack:
            leaks.append("agent token")
    report["leak_findings"] = leaks
    report["leak_ok"] = not leaks
    report["stub_marker"] = "[桩" in haystack

    if not args.keep_db:
        try:
            db_path.unlink(missing_ok=True)
        except PermissionError:
            report["db_cleanup"] = "skipped (sqlite connection still open)"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if (report["status"] and report["leak_ok"] and not report["stub_marker"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
