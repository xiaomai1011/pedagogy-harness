"""H 探查：误判（本该放行却被拦）与漏判（本该拦却放行）的实证清单。

离线、不联网、不读真实题库；只调用 leak_guard.violates()。
用途：给 H 的验收报告提供代表性样例，并如实登记静态规则的上限。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.harness import leak_guard as lg  # noqa: E402

Q = {"id": "P900", "title": "两数之和（探测题）",
     "statement": "读入两个整数，输出它们的和。",
     "reference_solution": "a, b = map(int, input().split())\nprint(a + b)\n",
     "knowledge_points": ["标准输入输出"]}

# (等级, 文本, 期望)  期望 = "allow" | "block"
ALLOW_CASES = [
    (1, "想一想：这道题需要用到哪些基本操作？先看看输入是怎么给的。"),
    (1, "可以先想想怎么把输入读进来，注意 `input()` 拿到的是字符串。"),
    (1, "注意 `print()` 和 `input()` 是两个不同的动作，先想清楚顺序。"),
    (2, "把过程拆成几步：先读入两个数，再把它们相加，最后输出结果。"),
    (2, "1. 先读入两个整数\n2. 再计算它们的和\n3. 最后输出"),
    (3, "可以用注释把每一步先写出来：\n# 读入两个数\n# 求和\n# 输出"),
    (3, "伪代码大致是：读取输入 → 计算 → 输出。"),
    (3, "思路：\n读入 a 和 b\n计算 a + b\n打印结果"),
    (4, "先只处理读入这一行：`n = int(input())`"),
    (4, "把你卡住的那一小步单独写出来：\n```python\nn = len(s)\n```"),
    (4, "卡在这一步的话，先确认 `x = 1` 这样的赋值有没有写对。"),
]

BLOCK_CASES = [
    (4, "a, b = map(int, input().split())\nprint(a + b)"),
    (4, "```python\nn = int(input())\nprint(n * 2)\n```"),
    (4, "n = int(input())\nprint(n * 2)\nprint(n)"),
    (3, "n = int(input())\nprint(n * 2)"),
    (2, "n = int(input())"),
    (1, "n = int(input())"),
    (2, "x = 1"),
    (4, "```python\nx = 1\ny = 2\nz = 3\nw = 4\na = 5\nb = 6\nc = 7\nd = 8\ne = 9\n```"),
    (4, "def solve(n):\n    return n * 2"),
    (4, "print(input())"),
    (1, "```python\nprint(1)\n```"),
    (3, "先 `n = int(input())`，再 `print(n)`。"),
]

KNOWN_GAP_CASES = [
    (4, "把 `int(input())` 的结果直接交给 `print`。"),
]


def _run(cases, label, expect):
    bad = 0
    print(f"\n=== {label}（期望：{expect}） ===")
    for level, text in cases:
        v = lg.violates(text, level, Q)
        got = "block" if v else "allow"
        ok = got == expect
        if not ok:
            bad += 1
        rule = v.rule if v else "-"
        detail = (v.detail if v else "")
        mark = "OK " if ok else "!! "
        print(f"{mark}L{level} [{got}/{rule}] {text!r}")
        if v:
            print(f"        detail={detail}")
    return bad


if __name__ == "__main__":
    bad = 0
    bad += _run(ALLOW_CASES, "本该放行（误判检查）", "allow")
    bad += _run(BLOCK_CASES, "本该拦截（漏判检查）", "block")
    print("\n=== 已知缺口（预期放行，如实登记） ===")
    for level, text in KNOWN_GAP_CASES:
        v = lg.violates(text, level, Q)
        print(f"    L{level} [{'block/' + v.rule if v else 'allow'}] {text!r}")
    print(f"\n误判+漏判合计 = {bad}")
    sys.exit(0)
