"""判题模块 · 验收脚本（岗位 3 交付后用它验收）。

它**不实现任何判题逻辑**——只调用岗位 3 交付的 `app.judge.runner.judge()`，
对着 `docs/contracts.md` §1 逐项检查返回是否符合契约。
（判题本体仍由岗位 3 负责，本脚本不侵占其职责；这只是一份“验收尺子”。）

用法：
    python tools/accept_judge.py             # 逐项检查，出退出码
    python tools/accept_judge.py --verbose   # 额外打印返回值摘要

退出码：0 = 全部合格；1 = 有不合格项。

本脚本只验判题契约，不验安全隔离（任务单 §二 G）。隔离验收通过前，
不得把 `judge()` 接到真实学生提交入口——学生代码一能执行，就能读到 `.env` 里的模型 Key。

用例均为无害代码（死循环 5 秒内会被杀；不写文件、不联网）。
"""

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.judge.runner import judge  # noqa: E402

RUNNER = REPO_ROOT / "app" / "judge" / "runner.py"

# 验收夹具题目：自包含，不依赖 data/questions（岗位 3 手边就能跑）。
# 两条用例：可见的 “5 → 10”，隐藏的 “7 → 14”。
QUESTION = {
    "id": "ACC001",
    "title": "验收样例：输出两倍",
    "statement": "读入一个整数 n，输出 2n。",
    "test_cases": [
        {"input": "5", "expected": "10", "hidden": False},
        {"input": "7", "expected": "14", "hidden": True},
    ],
    "reference_solution": "print(int(input()) * 2)",
    "knowledge_points": ["变量与赋值"],
    "difficulty": 1,
    "phase": "practice",
}

VALID_STATUS = {"pass", "fail", "timeout", "crash", "error"}
VALID_ERROR_TYPE = {"none", "syntax", "runtime", "timeout", "wrong_output"}
REQUIRED_FIELDS = ("status", "total_cases", "passed_cases", "failed_cases",
                   "error_type", "stderr", "stdout", "exec_ms", "exit_code")

# 提交用的代码片段（每个只测一件事）
PROPER = "print(int(input()) * 2)"                    # 正确解
WRONG = "print(int(input()) * 3)"                     # 输出不符
SYNTAX = "print(int(input()) * 2"                     # 缺右括号 → 语法错
CRASH = 'print(int("abc") * 2)'                       # 运行期抛异常
LOOP = "while True:\n    pass"                        # 死循环 → 超时
SPACED = 'print(int(input()) * 2, end="  \\n\\n")'    # 尾随空格 + 末尾空行
BULK = 'print("A" * 5000)'                            # 超长输出
ECHOING = 'n = input().strip()\nprint("10" if n == "5" else "0")'  # 可见过、隐藏不过


def _run(code: str):
    return judge(QUESTION, code)


def check_fields(verbose: bool):
    result = _run(PROPER)
    missing = [name for name in REQUIRED_FIELDS if not hasattr(result, name)]
    if missing:
        return False, f"缺少契约字段：{', '.join(missing)}"
    return True, f"9 个契约字段齐全｜{result!r}" if verbose else "9 个契约字段齐全"


def check_enums(verbose: bool):
    problems = []
    for label, code in (("正确解", PROPER), ("输出错", WRONG),
                        ("语法错", SYNTAX), ("运行异常", CRASH)):
        result = _run(code)
        if result.status not in VALID_STATUS:
            problems.append(f"{label}的 status={result.status!r} 不在契约取值内")
        if result.error_type not in VALID_ERROR_TYPE:
            problems.append(f"{label}的 error_type={result.error_type!r} 不在契约取值内")
    if problems:
        return False, "；".join(problems)
    return True, "5 个 status / 5 个 error_type 取值均合法"


def check_pass(verbose: bool):
    result = _run(PROPER)
    if result.status != "pass":
        return False, f"正确解被判 {result.status!r}（应 pass）"
    if result.passed_cases != result.total_cases:
        return False, f"正确解 {result.passed_cases}/{result.total_cases}（应全过）"
    if result.error_type != "none":
        return False, f"正确解的 error_type={result.error_type!r}（应 none）"
    return True, f"pass｜{result.passed_cases}/{result.total_cases}｜error_type=none"


def check_wrong_output(verbose: bool):
    result = _run(WRONG)
    if result.status != "fail":
        return False, f"输出错被判 {result.status!r}（应 fail）"
    if result.error_type != "wrong_output":
        return False, (f"error_type={result.error_type!r}（应 wrong_output；"
                       "若落回 runtime，分类器会把「逻辑错」误判成「崩溃」）")
    return True, "fail｜error_type=wrong_output"


def check_syntax(verbose: bool):
    result = _run(SYNTAX)
    if result.status != "fail":
        return False, f"语法错被判 {result.status!r}（应 fail）"
    if result.error_type != "syntax":
        return False, f"error_type={result.error_type!r}（应 syntax）"
    return True, "fail｜error_type=syntax"


def check_crash(verbose: bool):
    result = _run(CRASH)
    problems = []
    if result.status != "crash":
        problems.append(f"status={result.status!r}（应 crash）")
    if result.error_type != "runtime":
        problems.append(f"error_type={result.error_type!r}（应 runtime）")
    if "Traceback" not in (result.stderr or ""):
        problems.append("stderr 里没有 Traceback（契约红线：stderr 必填且为原文）")
    if problems:
        return False, "；".join(problems)
    return True, "crash｜error_type=runtime｜stderr 含 Traceback"


def check_timeout(verbose: bool):
    result = _run(LOOP)
    problems = []
    if result.status != "timeout":
        problems.append(f"status={result.status!r}（应 timeout）")
    if result.error_type != "timeout":
        problems.append(f"error_type={result.error_type!r}（应 timeout）")
    if result.exit_code is not None:
        problems.append(f"exit_code={result.exit_code!r}（超时应为 null）")
    if problems:
        return False, "；".join(problems)
    detail = "5 秒内被杀｜exit_code=null"
    if verbose:
        detail += f"｜exec_ms={result.exec_ms}"
    return True, detail


def check_normalize(verbose: bool):
    result = _run(SPACED)
    if result.status != "pass":
        return False, (f"尾随空格/末尾空行被判 {result.status!r}（应 pass；"
                       "规范化＝统一换行 → 去行尾空白 → 去末尾空行）")
    return True, "输出规范化后精确比对通过"


def check_stdout_truncated(verbose: bool):
    result = _run(BULK)
    length = len(result.stdout or "")
    if length > 2000:
        return False, f"stdout 长度 {length} > 2000（契约要求截断到 2000 字符）"
    return True, f"stdout 长度 {length} ≤ 2000"


def check_hidden_not_echoed(verbose: bool):
    result = _run(ECHOING)
    hidden_entries = [entry for entry in result.failed_cases if entry.get("hidden")]
    if not hidden_entries:
        return False, ("没拿到「隐藏用例失败」的结果 —— 无法验证隐藏数据是否被回显，"
                       "请确认 hidden 标记已按契约透传")
    leaked = []
    for entry in hidden_entries:
        for field in ("input", "expected"):
            value = entry.get(field)
            if value not in (None, "", "-"):
                leaked.append(f"{field}={value!r}")
    if leaked:
        return False, "隐藏用例被回显：" + "、".join(leaked)
    detail = "隐藏用例的 input/expected 未回显"
    if str(result.stdout or "").strip():
        detail += ("（注意：stdout 非空；若其中含隐藏输入，须按任务单 §二 F 的"
                   "「内部结果 / 学生可见结果」分离处理）")
    return True, detail


def check_static(verbose: bool):
    source = RUNNER.read_text(encoding="utf-8")
    imports = [line.strip() for line in source.splitlines()
               if re.match(r"^\s*(import|from)\s", line)]
    problems = []
    if not any(re.match(r"^import\s+subprocess\b", line) for line in imports):
        problems.append("没有 `import subprocess`（契约要求必须用 subprocess + 超时）")
    banned = [line for line in imports if re.search(r"judge0|isolate", line, re.I)]
    if banned:
        problems.append("引入了被禁止的第三方判题组件：" + "、".join(banned))
    if problems:
        return False, "；".join(problems)
    return True, f"仅用标准库｜{len(imports)} 条 import 无违禁项"


def check_timeout_constant(verbose: bool):
    source = RUNNER.read_text(encoding="utf-8")
    match = re.search(r"TIMEOUT_SECONDS\s*=\s*(\d+)", source)
    if not match:
        return False, "没找到 `TIMEOUT_SECONDS` 常量（契约 §1 定死为 5 秒）"
    value = int(match.group(1))
    if value != 5:
        return False, f"TIMEOUT_SECONDS = {value}（契约定死为 5 秒）"
    return True, "TIMEOUT_SECONDS = 5"


CHECKS = [
    ("① 契约字段齐全", check_fields),
    ("② status / error_type 取值合法", check_enums),
    ("③ 正确解判 pass", check_pass),
    ("④ 输出不符判 wrong_output", check_wrong_output),
    ("⑤ 语法错判 syntax", check_syntax),
    ("⑥ 运行异常判 crash", check_crash),
    ("⑦ 死循环判 timeout", check_timeout),
    ("⑧ 输出规范化", check_normalize),
    ("⑨ stdout 截断到 2000", check_stdout_truncated),
    ("⑩ 隐藏用例不回显", check_hidden_not_echoed),
    ("⑪ 只用 subprocess、无违禁组件", check_static),
    ("⑫ TIMEOUT_SECONDS = 5", check_timeout_constant),
]

NOT_COVERED = [
    "安全隔离（任务单 §二 G）：绝对路径读取 / 环境变量泄露 / 越界写入 / 资源限制",
    "超时后是否清理**整个子进程树**（不只最外层 Python 进程）",
    "隔离不可用时的行为（要求：**拒绝执行并报错**，不得静默退回普通 subprocess）",
    "是否在主进程用 eval/exec 执行学生代码（人工看源码确认）",
    "子进程工作目录是否在**系统临时目录**、是否继承模型密钥（环境变量白名单）",
    "真实题库的参考解自洽性 —— 用 `python tools/validate_questions.py` 单独验",
]


def main(argv):
    parser = argparse.ArgumentParser(description="判题模块验收（对着契约 §1 逐项检查）")
    parser.add_argument("--verbose", action="store_true", help="打印每项返回值摘要")
    args = parser.parse_args(argv)

    source = RUNNER.read_text(encoding="utf-8")
    print("判题模块 · 契约验收（`docs/contracts.md` §1）")
    print("  被测文件 = app/judge/runner.py")
    if "[桩]" in source:
        print("  ⚠ 该文件**仍含 `[桩]` 标记**：以下不合格项属预期，等岗位 3 交付真实现后再跑。")
    print()

    results = []
    for name, function in CHECKS:
        try:
            passed, detail = function(args.verbose)
        except Exception as exc:  # 判题器自身炸了也要如实报出来
            passed, detail = False, f"检查抛异常：{type(exc).__name__}: {exc}"
        results.append(passed)
        print(f"  [{'合格' if passed else '不合格'}] {name} —— {detail}")

    print("\n== 本脚本**不覆盖**的边界（需另做验收）==")
    for item in NOT_COVERED:
        print(f"  · {item}")

    failed = results.count(False)
    print(f"\n== 汇总 ==\n  合格 {results.count(True)}/{len(results)}；不合格 {failed}")
    if failed:
        print("  ⛔ 判题契约未通过 —— 不得接入真实学生提交。")
        return 1
    print("  ✅ 判题契约通过 —— 但**隔离验收（G）未通过前仍不得接真实学生提交**。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
