#!/usr/bin/env python3
"""19 题参考答案 + 错误样例在 **VM agent** 上的自洽验证（阶段 5）。

用途：一次性回答三件事——

1. 每道题的参考答案能否在 VM 沙箱里通过**全部可见 + 隐藏**用例；
2. 明显的错误输出 / 语法错误 / 运行时异常 / 超时能否被正确分类；
3. 隐藏用例的 `input` / `expected` 是否会泄漏到宿主侧结果里。

运行（宿主，项目 venv）::

    set PH_VM_AGENT_TOKEN_FILE=C:\\...\\vmkey\\ph_judge_agent_token
    python tools/verify_questions_vm.py --out docs/G6_19题_VM验证.json
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("PH_DB", str(REPO / "verify_tmp.sqlite"))

from app.harness.vm_agent_adapter import VmAgentAdapter  # noqa: E402

QUESTIONS = REPO / "data" / "questions"

ERROR_PROBES = [
    ("wrong_output", 'print("0 0")'),
    ("syntax", "def broken(:\n    pass\n"),
    ("runtime", "raise ValueError('boom')\n"),
    ("timeout", "while True:\n    pass\n"),
]


def _load(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _summary(result) -> dict:
    return {
        "status": result.status,
        "error_type": result.error_type,
        "exit_code": result.exit_code,
        "total": result.total_cases,
        "passed": result.passed_cases,
        "exec_ms": result.exec_ms,
        "stdout_len": len(result.stdout or ""),
        "failed": [
            {"case_index": c.get("case_index"), "hidden": c.get("hidden"),
             "input_is_none": c.get("input") is None,
             "expected_is_none": c.get("expected") is None}
            for c in (result.failed_cases or [])
        ],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "docs" / "G6_19题_VM验证.json"))
    args = ap.parse_args(argv)

    adapter = VmAgentAdapter()
    adapter.check_isolation()

    rows = []
    ok = 0
    for path in sorted(QUESTIONS.glob("*.json")):
        question = _load(path)
        qid = question["id"]
        started = time.time()
        result = adapter.start({
            "request_id": f"verify-{qid}",
            "question": question,
            "code": question.get("reference_solution", ""),
            "language": "python",
            "timeout_seconds": 5,
        })
        row = {"id": qid, "phase": question.get("phase"),
               "cases": len(question.get("test_cases") or []),
               "seconds": round(time.time() - started, 3),
               **_summary(result)}
        row["ok"] = row["status"] == "pass" and row["passed"] == row["cases"]
        ok += 1 if row["ok"] else 0
        rows.append(row)
        print(f"{qid:5s} {row['phase']:9s} {row['status']:8s} "
              f"{row['passed']}/{row['cases']} {row['exec_ms']}ms "
              f"{'OK' if row['ok'] else 'FAIL'}")

    # 错误分类探针（统一用 P001 的用例，只验分类不看通过率）
    p001 = _load(QUESTIONS / "P001.json")
    probes = []
    for name, code in ERROR_PROBES:
        result = adapter.start({
            "request_id": f"probe-{name}",
            "question": p001,
            "code": code,
            "language": "python",
            "timeout_seconds": 5,
        })
        expect = {"wrong_output": ("fail", "wrong_output"),
                  "syntax": ("fail", "syntax"),
                  "runtime": ("crash", "runtime"),
                  "timeout": ("timeout", "timeout")}[name]
        got = (result.status, result.error_type)
        probes.append({"probe": name, "expect": list(expect), "got": list(got),
                       "exit_code": result.exit_code, "exec_ms": result.exec_ms,
                       "ok": got == expect})
        print(f"probe {name:13s} expect={expect} got={got} "
              f"{'OK' if got == expect else 'FAIL'}")

    report = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "agent": adapter.describe(),
              "questions_total": len(rows), "questions_ok": ok,
              "probes_ok": sum(1 for p in probes if p["ok"]), "probes_total": len(probes),
              "rows": rows, "probes": probes}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{ok}/{len(rows)} 题通过；{report['probes_ok']}/{report['probes_total']} 探针通过 → {out}")
    return 0 if ok == len(rows) and report["probes_ok"] == len(probes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
