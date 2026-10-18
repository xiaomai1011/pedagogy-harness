"""提示质量构造样本（验收配合 ① · 方案 B）—— 供教师**早期审核**提示的分级与正确性。

**两条批次**（同一个文档里都保留，不悄悄删旧证据）：

- **批次 A（已作废，原文逐字保留）**：夹具代码是 `a = int(input())`。它在 `P001`
  （题面输入是**同一行空格分隔的两个整数**，如 `1 2`）上，第一行 `int("1 2")` 会先抛
  `ValueError: invalid literal for int() with base 10: '1 2'`，**根本到不了**第二行，
  故注入的 `NameError: name 'b' is not defined` 与代码行为**不一致** → 该批次作废。
  但它的 **L4 输出**本身是**发现的一处错误提示**（见报告），有留档价值。
- **批次 B（本轮生成）**：夹具改为 `parts = input().split()` 版 —— 第一行合法读取、
  第二行 `print(a + b)` 才触发 `NameError`，与注入结果一致。四级共用这一份夹具。

**如实声明（不得省略）**：

1. 判题结果是**人工构造并注入的**，用于独立验证教学反馈链路；**不是项目判题器的实际执行结果**。
2. 每个样本的**历史状态（连败次数、错误记忆）也由夹具构造**，**不是**学生的真实学习记录。
3. 提示正文由**真实模型生成**（走真实防泄校验与重试）。`P001` 是**短题**，本批只作**早期审核**，
   **不代表**整体教学质量；教师评价栏**留空**。

用法::

    python tools/hint_quality_samples.py --out docs/验收_提示质量构造样本.md

**只用临时库**（`tempfile.mkdtemp()`），不碰真实 `pedagogy.sqlite`；报告落盘前脱敏。

注：产品当前把提示的真实生成路径（`llm`/`llm_retry`/`fallback`）**丢弃**了
（`hint_ladder.render()` 里 `_source` 被忽略），故本脚本按
「模型调用次数 + 是否等于 `leak_guard` 兜底模板」**推断**来源，并在报告里标明这是**推断**。
"""

import argparse
import os
import re
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

QID = "P001"

# 批次 B（本轮）夹具：第一行合法，第二行才触发 NameError
WRONG_CODE = "parts = input().split()\na = int(parts[0])\nprint(a + b)\n"

INJECT = {
    "status": "fail", "error_type": "runtime",
    "stderr": "NameError: name 'b' is not defined",
    "why": "第一行按空格拆出两个数字串（合法）；`a` 取第一个；`b` 从未赋值 → "
           "**人工构造的 runtime/NameError**，对应教学五类「语法」、知识点「变量与赋值」"
           "（按 `app/harness/error_classifier.py` 规则表）",
}

# ---- 批次 A：已作废，但**原文逐字保留**（不得改写）----
LEGACY_CODE = "a = int(input())\nprint(a + b)\n"
LEGACY_BATCH = [
    {"level": 1, "calls": 1, "wall": 4590, "check": "当前规则无命中", "origin": "llm（首次即通过防泄校验）",
     "hint": "这道题处理输入时可使用input()获取输入行，借助split()拆分得到两个数字字符串，"
             "再用int()分别转换为整数并赋值给对应变量，后续再进行加法计算。"},
    {"level": 2, "calls": 1, "wall": 3002, "check": "当前规则无命中", "origin": "llm（首次即通过防泄校验）",
     "hint": "首先读取一整行包含两个用空格分隔的整数的输入；接着把读取到的输入按空格拆分为两个部分，"
             "分别将这两部分转换为整数后赋值给两个不同的变量；最后将这两个变量相加的结果输出。"},
    {"level": 3, "calls": 1, "wall": 2484, "check": "当前规则无命中", "origin": "llm（首次即通过防泄校验）",
     "hint": "1. 读取一整行输入内容，将输入按空格分割为两个独立的字符串\n"
             "2. 将第一个字符串转换为整数，赋值给变量a\n"
             "3. 将第二个字符串转换为整数，赋值给变量b\n"
             "4. 计算a与b的和，将结果输出"},
    {"level": 4, "calls": 1, "wall": 3372, "check": "当前规则无命中", "origin": "llm（首次即通过防泄校验）",
     "hint": "你当前代码中变量b未被赋值，仅通过一次input读取了a，缺少读取第二个输入并赋值给b的步骤，可补充：\n"
             "b = int(input())"},
]

_CALLS: list[tuple[float, bool]] = []


def _redact(text: str, secrets: list[str]) -> str:
    out = text or ""
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "<redacted>")
    return re.sub(r"sk-[A-Za-z0-9_\-]{6,}", "<redacted>", out)


def _quote(text: str) -> list[str]:
    return [f"> {line}" for line in (text or "（空）").split("\n")]


def main() -> int:
    ap = argparse.ArgumentParser(description="提示质量构造样本（验收配合 ① · 方案 B）")
    ap.add_argument("--out", default="", help="把 markdown 写到该路径（默认只打印）")
    args = ap.parse_args()

    tmpdir = tempfile.mkdtemp(prefix="pedagogy_hq_")
    os.environ["PH_DB"] = os.path.join(tmpdir, "hint_quality.sqlite")

    from app import db                                        # noqa: E402
    from app import llm as llm_mod                             # noqa: E402
    from app.harness import error_classifier as ec             # noqa: E402
    from app.harness import leak_guard                         # noqa: E402
    from app.harness import pipeline as pipeline_mod           # noqa: E402
    from app.harness.config_switch import PRESETS              # noqa: E402
    from app.judge.runner import JudgeResult                   # noqa: E402

    secrets = [os.environ.get("PH_LLM_API_KEY", ""), os.environ.get("PH_LLM_BASE_URL", "")]
    configured = llm_mod.is_configured()

    real_llm = llm_mod.llm

    def timed_llm(*a, **kw):
        t0 = time.perf_counter()
        try:
            out = real_llm(*a, **kw)
            _CALLS.append(((time.perf_counter() - t0) * 1000.0, False))
            return out
        except Exception:
            _CALLS.append(((time.perf_counter() - t0) * 1000.0, True))
            raise

    llm_mod.llm = timed_llm

    question = pipeline_mod.load_question(QID)

    def injected_judge(q, code, language="python"):
        """**人工构造并注入**的判题结果（不是判题器的实际执行结果）。"""
        return JudgeResult(status=INJECT["status"], total_cases=3, passed_cases=0,
                           failed_cases=[{"case_index": 1, "input": "1 2", "expected": "3",
                                          "actual": "(未执行)", "hidden": False}],
                           error_type=INJECT["error_type"], stderr=INJECT["stderr"],
                           stdout="", exec_ms=0, exit_code=1)

    pipeline_mod.judge = injected_judge
    db.init_db()

    # 真实分类器（规则表）从构造的 stderr 推出教学分类 —— 这一步是真实计算，不是构造。
    cls = ec.classify(injected_judge(question, WRONG_CODE), WRONG_CODE, question)

    samples = []
    for level in (1, 2, 3, 4):
        pid = f"HQ_FIXTURE_L{level}"
        # 夹具：造出「连败 2*level 次」的历史（**不请求提示**，故不产生模型调用）
        for _ in range(2 * level):
            pipeline_mod.handle_submission(participant_id=pid, question_id=QID,
                                           code=WRONG_CODE, config=PRESETS["exp"])
        _CALLS.clear()
        t0 = time.perf_counter()
        err, out = "", None
        try:
            out = pipeline_mod.handle_hint_request(participant_id=pid, question_id=QID,
                                                   code=WRONG_CODE, config=PRESETS["exp"])
        except Exception as exc:
            err = _redact(f"{type(exc).__name__}: {exc}", secrets)
        wall = (time.perf_counter() - t0) * 1000.0

        message = (out or {}).get("message", "") or ""
        hint = message.split("\n\n", 1)[1] if "\n\n" in message else message
        calls = len(_CALLS)
        fb = leak_guard.fallback(level, cls)     # 兜底模板（用于推断来源；kp 须与真实调用一致）
        if err:
            origin = "生成失败（抛异常）"
        elif calls >= 2 and hint.strip() == fb.strip():
            origin = "fallback（重试后仍违规 → 改用无模板兜底）"
        elif calls >= 2:
            origin = "llm_retry（首次违规，重试后通过）"
        elif calls == 1:
            origin = "llm（首次即通过防泄校验）"
        else:
            origin = "未调用模型"

        # 「当前规则无命中」**只**说明本次输出未触发 R1–R4，不代表提示正确或分级合理
        violation = leak_guard.violates(hint, level, question)
        check = ("当前规则无命中" if violation is None
                 else f"命中 {violation.rule}：{violation.detail}")

        samples.append({
            "level": level, "pid": pid, "history": 2 * level,
            "got_level": (out or {}).get("hint_level", "—"),
            "unlocked": (out or {}).get("unlocked_level", "—"),
            "calls": calls, "origin": origin, "check": check,
            "wall": wall, "hint": hint.strip(), "err": err,
        })

    md = []
    md.append("# 提示质量构造样本（验收配合 ① · 方案 B）")
    md.append("")
    md.append(f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　题目：`{QID}`　分组：`exp`")
    md.append("> **本文件含两个批次**：**批次 A 已作废**（原文逐字保留为问题记录）+ **批次 B 本轮生成**。")
    md.append("> **判题结果**：**人工构造并注入**（**不是**项目判题器的实际执行结果），仅用于独立验证教学反馈链路。")
    md.append("> **历史状态**：各样本的连败次数与错误记忆**均由夹具构造**，**不是**学生的真实学习记录。")
    md.append(f"> **模型**：{'真实调用（配置齐全）' if configured else '**未配置**，提示正文是桩文本，本批不可用于审核'}。")
    md.append("> **范围**：`P001` 是短题，本批**只作早期审核**，**不代表**整体教学质量；真判题交付后须用真实结果重跑复核。")
    md.append("> **教师评价栏留空**，由教师填写。")
    md.append("")
    md.append("## 0 修订说明：为什么有作废批次")
    md.append("")
    md.append("批次 A 的夹具代码是：")
    md.append("")
    md.append("```python")
    md.append(LEGACY_CODE.rstrip("\n"))
    md.append("```")
    md.append("")
    md.append("但 `P001` 题面要求输入**同一行空格分隔的两个整数**（如 `1 2`），于是第一行实际是 `int(\"1 2\")`，")
    md.append("**会先抛 `ValueError: invalid literal for int() with base 10: '1 2'`**，")
    md.append("**根本到不了**第二行 —— 因此批次 A 注入的 `NameError: name 'b' is not defined`")
    md.append("与代码行为**不一致**，该批次**作废**——**不作为提示质量合格证据，仅保留为问题记录**。")
    md.append("")
    md.append("- **保留原文**：批次 A 的提示正文**逐字保留**，未做任何改写。")
    md.append("- **一处发现的错误提示**：批次 A 的 L4 建议 `b = int(input())`，")
    md.append("  **不符合本题的单行输入格式**（题目不是两行各一个整数；再调一次 `input()` 在只有一行输入时还会 `EOFError`），")
    md.append("  对原代码也解决不了第一行的 `ValueError`。这是一处**真实的错误提示**。")
    md.append("- **撤回**：此前“L4 那一行与学生已有代码合起来正好是完整解”的说法**撤回**；")
    md.append("  “短题下局部片段可能直接补全答案”仍是**风险判断**，但**不能**用批次 A 这条错误修复当已证实的案例。")
    md.append("- **修正夹具**：批次 B 改用 `parts = input().split()` 版（第一行合法、第二行才触发 `NameError`），")
    md.append("  四级**共用同一份**夹具。")
    md.append("")
    md.append("## 1 批次 A（**不作合格证据** · 原文保留为问题记录）")
    md.append("")
    md.append(f"**夹具代码**：`{LEGACY_CODE.rstrip()}` （`P001` 输入 `1 2` 时先 `ValueError`）")
    md.append("")
    md.append("| 样本 | 展示等级 | 模型调用次数 | 生成来源（推断） | 防泄校验 | 耗时(ms) |")
    md.append("|---|---|---|---|---|---|")
    for s in LEGACY_BATCH:
        md.append(f"| L{s['level']} | {s['level']} | {s['calls']} | {s['origin']} | {s['check']} | {s['wall']} |")
    md.append("")
    for s in LEGACY_BATCH:
        md.append(f"**L{s['level']}（原文）**：")
        md.append("")
        md.extend(_quote(s["hint"]))
        md.append("")
    md.append("> 批次 A 的 L4 输出即为上文标注的**错误提示**（`b = int(input())`）。")
    md.append("")
    md.append("## 2 批次 B（修正夹具 · 本轮生成）")
    md.append("")
    md.append("### 2.0 固定不变的部分（四级共用）")
    md.append("")
    md.append(f"**题目**：`{QID}` {question.get('title', '')} —— {question.get('statement', '')}")
    md.append("")
    md.append("**学生代码（同一份）**：")
    md.append("")
    md.append("```python")
    md.append(WRONG_CODE.rstrip("\n"))
    md.append("```")
    md.append("")
    md.append("**注入的判题结果（同一个）**：")
    md.append("")
    md.append(f"- `status={INJECT['status']}`、`error_type={INJECT['error_type']}`、"
              f"`stderr={INJECT['stderr']}`、`passed_cases=0/3`")
    md.append(f"- 构造依据：{INJECT['why']}")
    md.append(f"- 由**真实分类器**（规则表）推出：`error_class={cls.error_class}`、"
              f"`knowledge_point={cls.knowledge_point}`、`source={cls.source}`（**真实计算**，非构造）")
    md.append("")
    md.append("> 四级只改**一个教学状态量**（夹具构造的历史连败数 2／4／6／8）→ 解锁 L1／L2／L3／L4；")
    md.append("> 但注意：它**同时**改变了解锁等级与错误记忆计数（历史记忆还会进入模型提示），")
    md.append("> 故准确说法是“**同题、同代码、同判题结果下，不同构造教学状态的示例**”，")
    md.append("> **不是**严格的单变量比较。")
    md.append("")
    md.append("### 2.1 总表")
    md.append("")
    md.append("| 样本 | 夹具历史（提交／连败） | 展示等级 | 解锁上限 | 模型调用次数 | 生成来源（**推断**） | 防泄校验 | 耗时(ms) |")
    md.append("|---|---|---|---|---|---|---|---|")
    for s in samples:
        md.append(f"| L{s['level']} | {s['history']} / {s['history']} | {s['got_level']} | {s['unlocked']} "
                  f"| {s['calls']} | {s['origin']} | {s['check']} | {s['wall']:.0f} |")
    md.append("")
    md.append("> 「生成来源」是**推断**：产品目前丢弃了 `hint_ladder` 的真实来源，")
    md.append("> 本脚本按「模型调用次数 + 是否等于 `leak_guard` 兜底模板」判定；待办 C 会把该字段真正接出来后替换。")
    md.append("")
    md.append("### 2.2 四条样本明细")
    for s in samples:
        md.append("")
        md.append(f"#### L{s['level']}　（夹具历史：连败 {s['history']} 次 → 解锁 L{s['level']}）")
        md.append("")
        md.append(f"- 展示等级：**L{s['got_level']}**　解锁上限：L{s['unlocked']}　模型调用：{s['calls']} 次　来源（推断）：{s['origin']}")
        md.append(f"- 防泄校验：{s['check']}（**只**说明未触发当前规则 R1–R4）")
        if s["err"]:
            md.append(f"- **异常**：{s['err']}")
        md.append("")
        md.append("**提示正文（模型真实产出）**：")
        md.append("")
        md.extend(_quote(s["hint"]))
        md.append("")
        md.append("**教师评价**：")
        md.append("")
        md.append("- 是否正确：")
        md.append("- 是否有帮助：")
        md.append("- 分级是否合理（与上一级相比增加了什么）：")
        md.append("")
    md.append("## 3 结论的边界（不得略去）")
    md.append("")
    md.append("- 「防泄校验：当前规则无命中」**只**说明本次输出未触发当前配置的规则 R1–R4；")
    md.append("  **不能**据此推出：提示**正确**、**分级合理**、**没有语义上的答案泄露**、**防泄机制已充分有效**。")
    md.append("  （批次 A 的 L4 就是反例：规则无命中，但提示对本题输入格式**不成立**。）")
    md.append("- 本批是“**同题、同代码、同判题结果下，不同构造教学状态的示例**”，**不是**严格单变量比较。")
    md.append("- **L1–L3 的区分度不作结论**，交教师审核。观察记录（仅供参考）：L1 已不只点名函数，")
    md.append("  还给出了“读取→拆分→转换→赋值→相加”的顺序，信息量较具体，L1–L3 的**信息增量可能偏小**；")
    md.append("  L3 用编号自然语言承担伪代码作用，**不必**仅因没有严格语法就判不合格。")
    md.append("- `P001` 是**短题**，本批只作**早期审核**，**不代表**整体教学质量；真判题交付后须用真实结果重跑复核。")
    md.append("- 若某级最终只能保守兜底，**如实保留**，不为“凑齐四级”而强行生成代码。")
    md.append("")

    text = _redact("\n".join(md), secrets)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
        print(f"\n[written] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
