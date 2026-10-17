"""模型能力探针（**工程适配性验收，不是实验对照**）。

用途：换模型（中转站 / 供应商变更）后，先跑一遍本探针，确认新模型**能不能撑住
本项目的四个真实调用点**。测的是"这个模型能否满足组件要求"，**不产出"哪个模型更好"
的对照结论**，也不作为申报书的实验数据。

它调用的全是**项目里真实使用的函数与真实提示词构建逻辑**，不另写一套：
  - `error_classifier.classify()` —— 分类器要的是**可解析的 JSON** + 合法类别 + 合法知识点
  - `hint_ladder._llm_hint()`   —— 提示阶梯要的是**严守等级**（L1—L3 不许代码块，L4 只许片段）
  - `hint_ladder._success_message()` —— 肯定语：无代码、无表情、一两句

用法：
    python tools/model_probe.py                    # 每等级跑 3 次
    python tools/model_probe.py --runs 5           # 每等级跑 5 次
    python tools/model_probe.py --runs 5 --max-tokens 3000   # 覆盖 max_tokens

⚠️ **推理型模型**（回包里带 `reasoning_content` 的那种）会先写一大段"思考"再给正文；
若 `max_tokens` 偏小，额度会被思考吃光，**正文变成空串**——此时分类器大面积退
「未分类」，且探针会把它误判成"延迟低"。用 `--max-tokens` 放大复测即可确认。

退出码：0 = 全部达标；1 = 有未达标项（或未配置模型）。
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import llm as llm_mod  # noqa: E402
from app.harness import error_classifier as ec  # noqa: E402
from app.harness import hint_ladder as hl  # noqa: E402
from app.harness import leak_guard  # noqa: E402
from app.judge.runner import JudgeResult  # noqa: E402

# 达标线（与代码里的既有阈值对齐，不是拍出来的新指标）
THRESHOLDS = {
    "json_parse": 0.95,      # 低于此值 → 分类器大面积退「未分类」，日志分类列成片空缺
    "hint_first_pass": 0.80,  # 低于此值 → 提示大多要走重试或退兜底模板，教学信息量下降
    "p95_latency_ms": 15000,  # 学生端单次等待体验
}

_CALLS: list[dict] = []


def _instrument(max_tokens: int | None = None) -> None:
    """包一层大模型调用，用于记录**原始回包**与延迟（不改任何业务逻辑）。

    传 `max_tokens` 时注入每次调用：用于排查**推理型模型**（先输出一长段
    `reasoning_content`）被 max_tokens 截断、正文变空的情况。
    """
    original = llm_mod.llm

    def wrapper(system: str, user: str, **kwargs):
        if max_tokens is not None:
            kwargs.setdefault("max_tokens", max_tokens)
        started = time.perf_counter()
        try:
            text = original(system, user, **kwargs)
            error = None
        except Exception as exc:  # 让探针继续跑完，把失败如实记下来
            text, error = "", f"{type(exc).__name__}: {exc}"
        _CALLS.append({"ms": int((time.perf_counter() - started) * 1000),
                       "text": text, "error": error})
        if error:
            raise
        return text

    llm_mod.llm = wrapper


def _question() -> dict:
    fixture = REPO_ROOT / "tests" / "fixtures" / "DEV_P5705.json"
    if fixture.exists():
        return json.loads(fixture.read_text(encoding="utf-8"))
    return {"id": "PROBE", "title": "数字反转", "statement": "读入一行，输出它的数字反转结果。",
            "reference_solution": "print(input().strip()[::-1])\n",
            "knowledge_points": ["标准输入输出", "字符串处理"]}


def _judge(status="fail", error_type="runtime", stderr="") -> JudgeResult:
    """构造判题结果对象（**不调用判题桩**，避免被桩的假数据干扰）。"""
    return JudgeResult(status=status, total_cases=3, passed_cases=0, failed_cases=[
        {"case_index": 1, "input": "123.4", "expected": "4.321", "actual": "-", "hidden": False}],
        error_type=error_type, stderr=stderr, stdout="", exec_ms=1, exit_code=1)


def _ctx(question: dict, *, hint_level: int, code: str, classification=None) -> SimpleNamespace:
    return SimpleNamespace(
        participant_id="PROBE", question_id=question["id"], question=question, code=code,
        ts="", request_hint=True,
        judge_result=_judge(stderr="SomeWeirdError: 未知异常"),
        last_unlocked=hint_level, consecutive_fail=2,
        classification=classification, memory_hint="", persona_system="",
        hint_level=hint_level, unlocked_level=hint_level, message="", source="")


# ---------------------------------------------------------------- 1. 分类器 JSON
def probe_classifier(question: dict) -> dict:
    cases = [("n = input()\nprint(n * n)", "SomeWeirdError: 服务器返回了非预期对象"),
             ("data = input().split()\nprint(data[2])", "ParserError: 无法解析输入格式"),
             ("print(2 ** 999999)", "OverflowError: result too large")]
    rows = []
    for code, stderr in cases:
        result = _judge(stderr=stderr)
        raw_before = len(_CALLS)
        classification = ec.classify(result, code, question)   # ← 真实入口
        raw = _CALLS[raw_before]["text"] if len(_CALLS) > raw_before else ""
        parsed = bool(re.search(r"\{.*\}", raw or "", re.S))
        if parsed:
            try:
                data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
            except ValueError:
                data = None
            parsed = isinstance(data, dict)
        else:
            data = None
        rows.append({
            "stderr": stderr,
            "json_ok": parsed,
            "class_ok": isinstance(data, dict) and data.get("error_class") in ec.ERROR_CLASSES,
            "kp_ok": isinstance(data, dict) and data.get("knowledge_point", "") in question["knowledge_points"],
            "source": classification.source,
            "classified": classification.error_class,
        })
    total = len(rows)
    ok = sum(1 for r in rows if r["json_ok"])
    return {"rows": rows, "json_parse_rate": ok / total,
            "class_ok_rate": sum(1 for r in rows if r["class_ok"]) / total,
            "kp_ok_rate": sum(1 for r in rows if r["kp_ok"]) / total,
            "total": total}


# ---------------------------------------------------------------- 2/3. 提示等级
def probe_hint(question: dict, *, level: int, runs: int) -> dict:
    sources: list[str] = []
    violations: list[str] = []
    full_programs = 0
    code = "print(float(input()))"
    classification = ec.Classification(error_class="逻辑", knowledge_point="字符串处理", source="rule")
    for _ in range(runs):
        ctx = _ctx(question, hint_level=level, code=code, classification=classification)
        index = len(_CALLS)
        text, source = hl._llm_hint(ctx)          # ← 真实提示生成路径
        sources.append(source)
        first_reply = _CALLS[index]["text"] if len(_CALLS) > index else ""
        violation = leak_guard.violates(first_reply, level, question)
        if violation is not None:
            violations.append(f"{violation.rule}")
        if re.search(r"if\s+__name__|def\s+main\s*\(", text or ""):
            full_programs += 1
    return {"sources": sources, "first_pass": sources.count("llm") / runs,
            "fallback": sources.count("fallback") / runs,
            "violation_rules": violations, "full_programs": full_programs, "runs": runs}


# ---------------------------------------------------------------- 4. 肯定语风格
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_FENCE = re.compile(r"```")


def probe_praise(question: dict) -> dict:
    code = "print(input().strip()[::-1])"
    ctx = _ctx(question, hint_level=1, code=code)
    text = hl._success_message(ctx)
    return {"has_code_fence": bool(_FENCE.search(text)),
            "has_emoji": bool(_EMOJI.search(text)),
            "sentences": len(re.findall(r"[。！？]", text)),
            "sample": text}


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="模型能力探针（工程适配性验收）")
    parser.add_argument("--runs", type=int, default=3, help="每个提示等级调用次数（默认 3）")
    parser.add_argument("--max-tokens", type=int, default=None,
                        help="覆盖每次调用的 max_tokens（默认用代码里的 "
                             f"{llm_mod.DEFAULT_MAX_TOKENS}）")
    args = parser.parse_args(argv)

    if not llm_mod.is_configured():
        print("未配置模型：请在 .env 里填 PH_LLM_BASE_URL / PH_LLM_API_KEY / PH_LLM_MODEL。")
        return 1

    import os
    print("模型能力探针（工程适配性验收，非实验对照）")
    print(f"  model    = {os.environ.get('PH_LLM_MODEL')}")
    print(f"  base_url = {os.environ.get('PH_LLM_BASE_URL')}")
    print(f"  每等级调用 {args.runs} 次")
    print(f"  max_tokens = {args.max_tokens or llm_mod.DEFAULT_MAX_TOKENS}"
          f"{'（本次覆盖）' if args.max_tokens else '（代码默认）'}\n")

    question = _question()
    print(f"夹具题目：{question['id']}（{question['title']}）\n")
    _instrument(args.max_tokens)

    results: dict[str, bool] = {}
    try:
        classifier = probe_classifier(question)
        print("== 1. 结构化输出（错误分类器）==")
        for row in classifier["rows"]:
            print(f"   {row['stderr'][:34]:<36} JSON={'是' if row['json_ok'] else '否'} "
                  f"类别={'合法' if row['class_ok'] else '不合法'} "
                  f"知识点={'合法' if row['kp_ok'] else '不合法'} → 判定为「{row['classified']}」"
                  f"（source={row['source']}）")
        print(f"   JSON 可解析率 {classifier['json_parse_rate']:.0%}"
              f"（达标线 {THRESHOLDS['json_parse']:.0%}）"
              f"｜类别合法率 {classifier['class_ok_rate']:.0%}"
              f"｜知识点合法率 {classifier['kp_ok_rate']:.0%}\n")
        results["结构化输出"] = classifier["json_parse_rate"] >= THRESHOLDS["json_parse"]

        hints = {}
        for level in (1, 2, 3, 4):
            hint = probe_hint(question, level=level, runs=args.runs)
            hints[level] = hint
            label = "L4 局部片段" if level == 4 else f"L{level}（不许代码块）"
            print(f"== {level + 1}. 提示等级约束 · {label} ==")
            print(f"   首次合格 {hint['first_pass']:.0%}"
                  f"｜重试后合格 {hint['sources'].count('llm_retry') / args.runs:.0%}"
                  f"｜退兜底模板 {hint['fallback']:.0%}")
            if hint["violation_rules"]:
                print(f"   首次回包触发的规则：{', '.join(sorted(set(hint['violation_rules'])))}")
            if level == 4:
                print(f"   产出里出现完整程序（`if __name__`/`def main`）的次数：{hint['full_programs']}")
            print()
        worst = min(hint["first_pass"] for hint in hints.values())
        results["提示等级约束"] = worst >= THRESHOLDS["hint_first_pass"]
        results["不泄完整程序"] = all(hint["full_programs"] == 0 for hint in hints.values())

        praise = probe_praise(question)
        print("== 6. 肯定语风格 ==")
        print(f"   含代码块：{'是' if praise['has_code_fence'] else '否'}"
              f"｜含表情符号：{'是' if praise['has_emoji'] else '否'}"
              f"｜句数：{praise['sentences']}")
        print(f"   产出示例：{praise['sample']}")
        print("   （是否\u201c带具体点\u201d需人工看一眼，脚本不猜。）\n")
        results["肯定语无代码无表情"] = (not praise["has_code_fence"]) and (not praise["has_emoji"])

        raw_latencies = [call["ms"] for call in _CALLS if call["error"] is None]
        failures = [call["error"] for call in _CALLS if call["error"]]
        # 空正文：模型回了但没内容（多为推理型模型把 max_tokens 吃在 reasoning 上）。
        # 学生端拿到空提示 = 这次调用白花，必须计入稳定性，不能算达标。
        empty = [c for c in _CALLS
                 if c["error"] is None and not (c["text"] or "").strip()]
        p95 = _p95(raw_latencies)
        print("== 7. 延迟与稳定性 ==")
        print(f"   调用 {len(_CALLS)} 次｜失败 {len(failures)} 次｜空正文 {len(empty)} 次"
              f"｜最慢 {max(raw_latencies or [0])} ms"
              f"｜p95 {p95} ms（达标线 {THRESHOLDS['p95_latency_ms']} ms）")
        if empty:
            print("   ⚠ 空正文说明 max_tokens 不够（推理内容把额度吃光），"
                  "或模型未按格式输出——换模型前先用 --max-tokens 复测。")
        for error in failures[:3]:
            print(f"   [调用失败] {error}")
        print()
        results["延迟"] = (p95 <= THRESHOLDS["p95_latency_ms"]
                           and not failures and not empty)
    finally:
        llm_mod.llm = llm_mod.llm  # 探针为一次性进程，无需还原

    print("== 总判定 ==")
    for name, passed in results.items():
        print(f"   {'达标' if passed else '未达标'}  {name}")
    overall = all(results.values())
    print(f"\n{'全部达标，可用于本项目。' if overall else '存在未达标项：先看上面逐项数值，再决定是否换模型或改配置。'}")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
