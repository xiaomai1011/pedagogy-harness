"""后端管线预演（验收配合 ②）。

**只验证三件事**：主干管线能否跑通 / 各步耗时（含模型等待） / 提交日志与操作是否一一对应。

**它不代表"完整课堂预演"**——脚本直接调用 `handle_submission()` 与 `handle_hint_request()`，
**不经浏览器与 HTTP 接口，也没有学生读题、编码、改错的过程**。因此它**不能**用来证明
"页面交互正常""课堂时间足够""多人并发稳定""执行事件与提示事件已全部对账"。

两种模式（**必须显式区分**，不得把构造当真实）：

- `--mode constructed`（默认）：第 ⑧ 步改用**人工构造并注入**的"通过"判题结果，
  用于稳定检查管线规则（连败解锁、纯提示不记账、日志与操作对应）。
- `--mode real`：**全程不注入**任何判题结果，且**必须**提供 `--wrong-code` 与 `--answer-code`
  （经判题器确认过的错误代码 / 正确答案）。判题器仍为桩时**无法**进行真实验证，
  脚本会直接报错退出，不会退化成构造模式。

用法::

    python tools/flow_rehearsal.py --out docs/验收_流程预演.md
    python tools/flow_rehearsal.py --mode real --wrong-code "..." --answer-code "..." --out ...

其它约定：

- **每次运行新建独立临时库**（`tempfile.mkdtemp()`，在 `main()` 内创建），不碰真实 `pedagogy.sqlite`，
  也不会互相删除彼此的文件。
- 报告落盘前对异常文本**脱敏**（去掉 `.env` 里的密钥 / base_url 与形如 `sk-…` 的串）。
- 脚本**不补录**任何日志：提交日志全部由主干正常写入，本脚本只读不改。
"""

import argparse
import os
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

PID = "REHEARSAL_01"
QID = "P001"

# 本次预演记录到的每一次模型调用：(耗时 ms, 是否抛异常)
_CALLS: list[tuple[float, bool]] = []


def _redact(text: str, secrets: list[str]) -> str:
    """对异常文本脱敏：抹掉真实密钥 / base_url 与形如 sk-… 的串。"""
    out = text or ""
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "<redacted>")
    return re.sub(r"sk-[A-Za-z0-9_\-]{6,}", "<redacted>", out)


def _build_ops(wrong_code: str, answer_code: str) -> list[dict]:
    """8 步操作与**各自期望**（日志增量 / status / hint_level / unlocked_level）。

    期望值与模式无关：构造模式的"通过"来自注入，真实模式的"通过"来自真判题器——
    两种模式下这一列的期望都是 `pass`，真实模式跑不过就直接报 ❌（不会自动降级）。
    """
    def op(label, kind, code, log, status, hint, unlocked):
        return {"label": label, "kind": kind, "code": code, "log": log,
                "status": status, "hint": hint, "unlocked": unlocked}

    return [
        op("① 首次提交（失败）", "submit", wrong_code, 1, "fail", 0, 0),
        op("② 纯提示请求（尚未解锁）", "hint", wrong_code, 0, "fail", 1, 1),
        op("③ 第 2 次提交（失败）", "submit", wrong_code, 1, "fail", 0, 1),
        op("④ 纯提示请求（已解锁 L1）", "hint", wrong_code, 0, "fail", 1, 1),
        op("⑤ 第 3 次提交（失败）", "submit", wrong_code, 1, "fail", 0, 1),
        op("⑥ 第 4 次提交（失败）", "submit", wrong_code, 1, "fail", 0, 2),
        op("⑦ 纯提示请求（已解锁 L2）", "hint", wrong_code, 0, "fail", 2, 2),
        op("⑧ 提交（应通过）", "submit", answer_code, 1, "pass", 0, 2),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="后端管线预演（验收配合 ②）")
    ap.add_argument("--mode", choices=("constructed", "real"), default="constructed")
    ap.add_argument("--out", default="", help="把 markdown 报告写到该路径（默认只打印）")
    ap.add_argument("--wrong-code", default="", help="real 模式必填：经判题器确认的错误代码")
    ap.add_argument("--answer-code", default="", help="real 模式必填：经判题器确认的正确答案")
    args = ap.parse_args()

    if args.mode == "real" and not (args.wrong_code and args.answer_code):
        print("ERR: --mode real 需要 --wrong-code 与 --answer-code（经判题器确认过的错误代码 / 正确答案）。\n"
              "     判题器仍为桩实现时无法提供这两样，故本轮**不能**做真实验证——\n"
              "     请先等岗位③交付真实判题器，或改用默认的 --mode constructed（仅检查管线规则）。")
        return 2

    # ① 独立临时库：在 main() 里创建，且必须在 import app.* 之前设好 PH_DB
    tmpdir = tempfile.mkdtemp(prefix="pedagogy_rehearsal_")
    os.environ["PH_DB"] = os.path.join(tmpdir, "rehearsal.sqlite")

    from app import db                                       # noqa: E402
    from app import llm as llm_mod                            # noqa: E402
    from app.harness import pipeline as pipeline_mod          # noqa: E402
    from app.harness.config_switch import PRESETS             # noqa: E402

    secrets = [os.environ.get("PH_LLM_API_KEY", ""), os.environ.get("PH_LLM_BASE_URL", "")]

    # ② 计时包装：主干调用的是 llm_mod.llm，故替换模块属性。失败不吞，按既定异常路径走。
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
    configured = llm_mod.is_configured()

    db.init_db()
    question = pipeline_mod.load_question(QID)

    # ③ 探测判题器是否仍为桩：只用于报告如实说明，不改变任何行为
    probe = pipeline_mod.judge(question, "print(1)")
    judge_is_stub = "桩" in (getattr(probe, "stderr", "") or "")

    # real 模式：探到**明确桩特征**就立即拒绝运行 —— 否则会出现"声称用真实判题器、
    # 实际跑的是桩"的自相矛盾报告。**不做自动降级**（不悄悄退化成 constructed）。
    if args.mode == "real" and judge_is_stub:
        print("ERR: --mode real 探测到当前仓库判题器为**桩**（stderr 含“桩”特征）→ **拒绝运行**。\n"
              "     本次无法进行真实验证；请等岗位③交付真实判题器后再用 real 模式，\n"
              "     或改用 --mode constructed（仅检查管线规则，**不等同**真实验收通过）。")
        return 2

    def _log_rows() -> int:
        with db.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) AS n FROM submission_log WHERE participant_id=?", (PID,)
            ).fetchone()["n"])

    def _constructed_pass_judge(q, code, language="python"):
        """构造模式专用：人工构造并注入的"通过"结果（**不是**判题器的实际执行结果）。"""
        from app.judge.runner import JudgeResult
        return JudgeResult(status="pass", total_cases=3, passed_cases=3, failed_cases=[],
                           error_type="none", stderr="", stdout="3\n", exec_ms=11, exit_code=0)

    wrong_code = args.wrong_code if args.mode == "real" else "print(1)"
    answer_code = args.answer_code if args.mode == "real" else "print(1)"
    ops = _build_ops(wrong_code, answer_code)

    rows = []
    for idx, op in enumerate(ops):
        if args.mode == "constructed" and idx == len(ops) - 1:
            pipeline_mod.judge = _constructed_pass_judge      # 仅构造模式、仅最后一步
        _CALLS.clear()
        before = _log_rows()
        t0 = time.perf_counter()
        err, out = "", None
        try:
            if op["kind"] == "submit":
                out = pipeline_mod.handle_submission(
                    participant_id=PID, question_id=QID, code=op["code"], config=PRESETS["exp"])
            else:
                out = pipeline_mod.handle_hint_request(
                    participant_id=PID, question_id=QID, code=op["code"], config=PRESETS["exp"])
        except Exception as exc:                              # 故障也如实记录（不吞）
            err = _redact(f"{type(exc).__name__}: {exc}", secrets)
        wall = (time.perf_counter() - t0) * 1000.0
        delta = _log_rows() - before

        got_status = (out or {}).get("status", "—")
        got_hint = (out or {}).get("hint_level", "—")
        got_unlocked = (out or {}).get("unlocked_level", "—")
        log_ok = (delta == op["log"])
        state_ok = (err == "" and got_status == op["status"]
                    and got_hint == op["hint"] and got_unlocked == op["unlocked"])
        rows.append({
            "label": op["label"],
            "wall": wall,
            "llm_ms": sum(ms for ms, _ in _CALLS),
            "calls": len(_CALLS),
            "llm_bad": sum(1 for _, bad in _CALLS if bad),
            "exp_log": op["log"], "got_log": delta, "log_ok": log_ok,
            "exp_status": op["status"], "got_status": got_status,
            "exp_hint": op["hint"], "got_hint": got_hint,
            "exp_unlocked": op["unlocked"], "got_unlocked": got_unlocked,
            "state_ok": state_ok,
            "err": err,
        })

    all_ok = all(r["log_ok"] and r["state_ok"] for r in rows)
    waits = [r["llm_ms"] for r in rows if r["calls"]]
    worst = max(rows, key=lambda r: r["wall"])
    if waits:
        median_wait = statistics.median(waits)
        wait_line = (f"本次少量**串行**调用的模型累计等待：中位 {median_wait:.0f} ms、"
                     f"最快 {min(waits):.0f} ms、最慢 {max(waits):.0f} ms（样本 {len(waits)} 次；"
                     f"**样本量小，不报 p95**）。**课堂可用性仍待真实流程与并发验证。**")
    else:
        wait_line = "模型累计等待：**无样本**，不计算中位数／最小值／最大值（本次没有任何模型调用进入计时）。"

    mode_desc = ("constructed —— **仅第 ⑧ 步**使用**人工构造并注入**的判题结果"
                 "（**不是**判题器的实际执行结果）；**其余 7 步仍走当前仓库判题器**，"
                 "故本模式**不是**“完全脱离判题器的固定夹具测试”"
                 if args.mode == "constructed"
                 else "real —— 全程**调用当前仓库判题器、未注入**任何判题结果"
                      "（**不**凭模式名宣称已通过真实判题验收）")
    if judge_is_stub:
        judge_desc = "**探测到桩特征**（stderr 含“桩”）：本次判题**不是**真实执行"
    elif args.mode == "real":
        judge_desc = "调用**当前仓库判题器**、未注入（**不**等同于已通过真实判题验收，仍需 `tools/accept_judge.py`）"
    else:
        judge_desc = "未探测到桩特征：第 ⑧ 步仍为**注入的构造结果**，常规 7 步走当前仓库判题器"
    model_desc = ("配置齐全（`PH_LLM_*` 三项均有）→ 本次“模型等待”为**真实调用**实测"
                  if configured else
                  "**不齐全**：`app/llm.py` 会返回桩文本，故本次“模型等待”**不是**真实模型调用")

    md = []
    md.append("# 后端管线预演记录（桩判题／构造通过结果）" if args.mode == "constructed"
              else "# 后端管线预演记录（真实判题器）")
    md.append("")
    md.append(f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　被试：`{PID}`　题目：`{QID}`　分组：`exp`")
    md.append(f"> **模式**：{mode_desc}")
    md.append(f"> **判题器**：{judge_desc}")
    md.append(f"> **模型配置**：{model_desc}")
    md.append(f"> **数据库**：本次新建的独立临时库 `{os.environ['PH_DB']}`，**未触碰真实 `pedagogy.sqlite`**。")
    md.append("> **本记录的范围**：只验证 **主干管线跑通 / 各步耗时 / 提交日志与操作一一对应**；")
    md.append("> **不经浏览器与 HTTP，无学生读题与编码过程 → 不代表完整课堂预演**，也不能用于证明并发稳定性。")
    md.append("")
    md.append("## 1 逐步耗时（串行）")
    md.append("")
    md.append("| 操作 | 总耗时(ms) | 模型累计等待(ms) | 模型调用次数 | 模型失败次数 |")
    md.append("|---|---|---|---|---|")
    for r in rows:
        md.append(f"| {r['label']} | {r['wall']:.0f} | {r['llm_ms']:.0f} | {r['calls']} | {r['llm_bad']} |")
    md.append("")
    md.append("> 「模型累计等待」= 该步操作内**所有**模型调用耗时之和；若发生重试，它**大于**单次请求耗时。")
    md.append("")
    md.append("## 2 逐项验收（日志与操作是否一一对应 + 返回状态与等级是否符合预期）")
    md.append("")
    md.append("| 操作 | 日志增量 期望/实际 | 日志符合 | status 期望/实际 | hint_level 期望/实际 | unlocked 期望/实际 | 状态·等级符合 | 异常 |")
    md.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        md.append(
            f"| {r['label']} | {r['exp_log']:+d} / {r['got_log']:+d} | {'✅' if r['log_ok'] else '❌'} "
            f"| {r['exp_status']} / {r['got_status']} | {r['exp_hint']} / {r['got_hint']} "
            f"| {r['exp_unlocked']} / {r['got_unlocked']} | {'✅' if r['state_ok'] else '❌'} "
            f"| {r['err'] or '—'} |")
    md.append("")
    if all_ok:
        md.append("**总验收结果**：✅ 全部通过（日志与状态两项**同时**满足）。")
    else:
        bad_log = [r["label"] for r in rows if not r["log_ok"]]
        bad_state = [r["label"] for r in rows if not r["state_ok"]]
        md.append("**总验收结果**：❌ 未通过。")
        if bad_log:
            md.append(f"- 日志计数不符：{'、'.join(bad_log)}")
        if bad_state:
            md.append(f"- 状态／等级不符或发生异常：{'、'.join(bad_state)}")
    md.append("")
    md.append("## 3 耗时与故障小结")
    md.append("")
    md.append(f"- 最慢操作：**{worst['label']}** → 总 {worst['wall']:.0f} ms（其中模型累计等待 {worst['llm_ms']:.0f} ms）")
    md.append(f"- {wait_line}")
    md.append(f"- 故障：模型调用失败 {sum(r['llm_bad'] for r in rows)} 次；主干抛异常 {sum(1 for r in rows if r['err'])} 次")
    md.append("")
    md.append("## 4 与后续验收的关系")
    md.append("")
    md.append("- 本记录**不**用于判断提示质量；提示质量由验收配合 ① 的独立材料（**人工构造并注入**的判题结果）承担。")
    if args.mode == "constructed":
        md.append("- 判题真实现交付后，须改用 `--mode real` **重跑**（届时第 ⑧ 步不再注入），本记录的“通过”才算真实。")
    else:
        md.append("- 本次为 real 模式：判题结果全部来自真实判题器，未注入。")
    md.append("")

    text = _redact("\n".join(md), secrets)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
        print(f"\n[written] {args.out}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
