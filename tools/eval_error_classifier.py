"""错误分类评估 v2（2026-10-28）：修正样本构成口径，按五类分层报告。

样本构成（dataset_v2_2026-10-28）：
  语法 5 / 类型 5 / 边界 5 / 逻辑 5 / 环境 5 / 无错误 2 = 27 条
上一版报告把"五类各 5 条 + 2 条无错误"写成 25 条，与实际构成矛盾；本文件按实际 27 条重新计算，
并保留每类样本数、正确数、错误数、未分类数，以及无错误样本的误报数。

标签口径（固定，不因得分倒改）：
  - ValueError 不整体归入某一类：`int('abc')` 这类**输入转换失败**判"类型"；`'%d' % 'x'` 这类**格式字符串错误**也判"类型"。
  - 索引/键不存在、切片越界、除零导致的非法下标 → "边界"。
  - 能跑通但答案错、超时、死循环 → "逻辑"。
  - 内存/递归/导入/文件/网络等运行环境问题 → "环境"。
  - 标签由团队人工阅读代码与 traceback 判定，**未经教师审核**；本工具不生成"教师通过"。
"""
import argparse
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.harness.error_classifier import classify  # noqa: E402
from app.judge.runner import JudgeResult  # noqa: E402

QUESTION = {
    "id": "P001",
    "title": "两数之和",
    "knowledge_points": ["循环", "列表", "比较", "输入输出"],
    "phase": "practice",
}

DATASET_VERSION = "dataset_v2_2026-10-28"

# (human_label, status, error_type, stderr, code, note)
CASES = [
    # 语法 5
    ("语法", "fail", "syntax", "SyntaxError: '(' was never closed", "print('hi'", ""),
    ("语法", "fail", "syntax", "SyntaxError: expected ':'", "if True\n    pass", ""),
    ("语法", "fail", "syntax", "IndentationError: expected an indented block", "def f():\npass", ""),
    ("语法", "fail", "syntax", "TabError: inconsistent use of tabs and spaces", "if True:\n\tpass\n        pass", ""),
    ("语法", "fail", "syntax", "NameError: name 'total' is not defined", "print(total)", "变量未定义，按语言层面错误归语法"),
    # 类型 5
    ("类型", "crash", "runtime", "TypeError: unsupported operand type(s) for +: 'int' and 'str'", "print(1 + 'a')", ""),
    ("类型", "crash", "runtime", "ValueError: invalid literal for int() with base 10: 'abc'", "int('abc')", "输入转换失败 → 类型"),
    ("类型", "crash", "runtime", "TypeError: object of type 'int' has no len()", "len(1)", ""),
    ("类型", "crash", "runtime", "TypeError: 'NoneType' object is not subscriptable", "x = None\nprint(x[0])", ""),
    ("类型", "crash", "runtime", "ValueError: unsupported format character 'x'", "'%x' % 3.5", "格式字符串错误 → 类型"),
    # 边界 5
    ("边界", "crash", "runtime", "IndexError: list index out of range", "a = [1]\nprint(a[3])", ""),
    ("边界", "crash", "runtime", "KeyError: 'k'", "d = {}\nprint(d['k'])", ""),
    ("边界", "crash", "runtime", "IndexError: list index out of range", "a = []\nprint(a[0])", "空输入边界"),
    ("边界", "crash", "runtime", "ZeroDivisionError: division by zero", "print(1 // 0)", ""),
    ("边界", "crash", "runtime", "IndexError: string index out of range", "s = ''\nprint(s[1])", ""),
    # 逻辑 5
    ("逻辑", "fail", "wrong_output", "", "print(0)", "能跑通但答案错"),
    ("逻辑", "fail", "wrong_output", "", "a, b = map(int, input().split())\nprint(a - b)", "运算符用错"),
    ("逻辑", "timeout", "timeout", "", "while True:\n    pass", "死循环"),
    ("逻辑", "fail", "wrong_output", "", "print(input())", "只回显输入"),
    ("逻辑", "fail", "wrong_output", "", "a, b = map(int, input().split())\nprint(a + b + 1)", "边界偏移"),
    # 环境 5
    ("环境", "crash", "runtime", "MemoryError", "x = 'a' * (10 ** 12)", ""),
    ("环境", "crash", "runtime", "RecursionError: maximum recursion depth exceeded", "def f():\n    return f()\nf()", ""),
    ("环境", "crash", "runtime", "ModuleNotFoundError: No module named 'numpy'", "import numpy", ""),
    ("环境", "crash", "runtime", "FileNotFoundError: [Errno 2] No such file or directory: 'data.txt'", "open('data.txt')", ""),
    ("环境", "crash", "runtime", "ConnectionError: 网络不可达", "import socket\nsocket.create_connection(('8.8.8.8', 53), 0.1)", ""),
    # 无错误 2
    ("无错误", "pass", "none", "", "a, b = map(int, input().split())\nprint(a + b)", "正确解答"),
    ("无错误", "pass", "none", "", "print('ok')", "正确但无输入输出意义"),
]

LABELS = ("语法", "类型", "边界", "逻辑", "环境", "无错误")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "docs" / "G9b_错误分类_人工标注评估_v2.json"))
    args = ap.parse_args()

    rows = []
    for label, status, err_type, stderr, code, note in CASES:
        jr = JudgeResult(status=status, total_cases=1, passed_cases=1 if status == "pass" else 0,
                         failed_cases=[], error_type=err_type, stderr=stderr, stdout="",
                         exec_ms=1, exit_code=0 if status != "timeout" else None)
        c = classify(jr, code, QUESTION)
        rows.append({
            "human_label": label, "predicted": c.error_class, "knowledge_point": c.knowledge_point,
            "source": c.source, "status": status, "error_type": err_type,
            "stderr_tail": stderr[:200], "code": code, "note": note,
        })

    by_class = {}
    for label in LABELS:
        sel = [r for r in rows if r["human_label"] == label]
        correct = sum(1 for r in sel if r["predicted"] == label or (label == "无错误" and r["predicted"] == "未分类"))
        unclassified = sum(1 for r in sel if r["predicted"] == "未分类")
        by_class[label] = {
            "n": len(sel), "correct": correct, "wrong": len(sel) - correct - unclassified,
            "unclassified": unclassified,
            "accuracy": round(correct / len(sel), 3) if sel else None,
        }

    total = len(rows)
    correct = sum(1 for r in rows if r["predicted"] == r["human_label"])
    unclassified = sum(1 for r in rows if r["predicted"] == "未分类")
    no_error = [r for r in rows if r["human_label"] == "无错误"]
    no_error_fp = sum(1 for r in no_error if r["predicted"] not in ("未分类", "无错误"))

    summary = {
        "dataset_version": DATASET_VERSION,
        "total": total,
        "correct": correct,
        "wrong": total - correct - unclassified,
        "unclassified": unclassified,
        "accuracy": round(correct / total, 3),
        "denominator": "全部样本（含未分类）：正确数 / 总样本数",
        "by_class": by_class,
        "no_error": {
            "n": len(no_error),
            "false_positive": no_error_fp,
            "false_positive_rate": round(no_error_fp / len(no_error), 3) if no_error else None,
            "note": "无错误样本预测为空（未分类）视为正确——通过提交本就不该被分类；只有被强行归入五类才算误报。无错误样本只走分类器的判定口径；'通过提交不进入兜底/不写记忆'另有真实请求路径的测试"
                    "（tests/test_memory_switch.py::test_pass_does_not_touch_memory）。",
        },
        "label_source": "团队人工标注（阅读代码与 traceback），**未经教师审核**；标签不取自系统预测",
        "rule_vs_llm": {
            "rule": sum(1 for r in rows if r["source"] == "rule"),
            "llm": sum(1 for r in rows if r["source"] != "rule"),
        },
        "notes": [
            "上一版报告写'五类各 5 条 + 2 条无错误'却给出 25 条分母，构成描述矛盾；本版按 27 条重新计算。",
            "ValueError 不整体归类：输入转换失败与格式字符串错误都按'类型'判定，规则固定后再评估，不倒改答案。",
            "未分类计入分母；本版未分类数见 summary.unclassified。",
        ],
        "mismatches": [[r["human_label"], r["predicted"], r["code"][:40]] for r in rows
                       if r["predicted"] != r["human_label"]],
    }

    out = {"summary": summary, "cases": rows}
    pathlib.Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("written", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


