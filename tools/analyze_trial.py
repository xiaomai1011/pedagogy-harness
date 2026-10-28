"""小规模试用（4–6 人）的逐人描述性分析。

输入是 `tools/export_experiment_data.py` 产出的 CSV（同一快照），不直接连数据库：

    python tools/export_experiment_data.py --db <库> --out <目录> --backup <备份>
    python tools/analyze_trial.py --export <目录> [--plan <题目安排.json>] [--roster <电脑编号.csv>]

产出（写回 `--export` 目录）：

- ``trial_participants.csv`` ：**一人一行**，前测/练习/后测的题数、完成数、通过数，
  提交次数、提示次数与等级/来源、错误类型分布、判题与模型耗时、缺失与未完成题目；
- ``trial_questions.csv`` ：**一题一行**，尝试会话数、提交数、提交通过率、首次通过/最终通过会话数；
- ``trial_prepost.csv`` ：**只含同时做了前测与后测的会话**，前测通过/尝试、后测通过/尝试、变化题数与变化率；
- ``trial_summary.md`` ：表格版摘要，含“成功率（按题）”与“前后测对比”两节，并写明口径与分母。

## 口径（不要按列名想当然）

- **得分按题目计**：`passed_any` = 至少通过过一次的题目数，`passed_first` = 首次提交即通过的题目数；
  两者都**不是**“通过的提交次数”。用哪一列作分数由预先确定的评分规则决定（`--rule`，默认 `any`）。
- `judge_ms` 是**判题执行耗时**；`model_ms` 是**模型调用耗时**（提示/分类兜底合计）；
  **都不是**学生作答时间。`gap_s` 是“提示 → 下一次提交”的间隔，含阅读、思考、改码与操作，不能当纯思考时间。
- **没有下一次提交的提示单独计数**（`pending_hints`），既不当作失败，也不静默丢弃。
- 未提交的预期题目列进 `missing_questions`；提交了但没通过的列进 `unfinished_questions`。
- 小样本只做**逐人结果 + 简单汇总**；百分比同时给分子/分母，不做显著性检验。

本脚本只读 CSV，不联网、不启动学生进程、不改数据库。
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from datetime import datetime
from pathlib import Path

PHASES = ("pretest", "practice", "posttest")
PHASE_LABEL = {"pretest": "前测", "practice": "练习", "posttest": "后测"}


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def parse_ts(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def as_int(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def load_plan(questions_dir: Path, plan_path: Path | None) -> dict[str, list[str]]:
    """题目安排：优先用 --plan，否则按题库的 phase 字段分组。"""
    if plan_path is not None:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        return {phase: list(data.get(phase, [])) for phase in PHASES}
    plan: dict[str, list[str]] = {phase: [] for phase in PHASES}
    for path in sorted(questions_dir.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        phase = item.get("phase")
        if phase in plan:
            plan[phase].append(item.get("id") or path.stem)
    return plan


def analyze_participant(pid: str, subs: list[dict], hints: list[dict], plan, rule: str) -> dict:
    mine = [s for s in subs if s.get("participant_id") == pid]
    my_hints = [h for h in hints if h.get("participant_id") == pid]

    by_q: dict[str, list[dict]] = {}
    for row in sorted(mine, key=lambda r: (as_int(r.get("submit_seq")), r.get("ts") or "")):
        by_q.setdefault(row.get("question_id", ""), []).append(row)

    out: dict = {"participant_id": pid, "submissions": len(mine),
                 "questions_attempted": len(by_q), "repeated_submissions": max(0, len(mine) - len(by_q))}
    out["questions_passed"] = len([q for q, rows in by_q.items()
                                   if any(as_int(r.get("pass_status")) == 1 for r in rows)])

    for phase in PHASES:
        expected = [q for q in plan.get(phase, [])]
        attempted = [q for q in expected if q in by_q]
        passed_any = [q for q in expected if any(as_int(r.get("pass_status")) == 1 for r in by_q.get(q, []))]
        passed_first = [q for q in expected
                        if by_q.get(q) and as_int(by_q[q][0].get("pass_status")) == 1]
        out[f"{phase}_expected"] = len(expected)
        out[f"{phase}_attempted"] = len(attempted)
        out[f"{phase}_passed_any"] = len(passed_any)
        out[f"{phase}_passed_first"] = len(passed_first)
        out[f"{phase}_missing"] = [q for q in expected if q not in by_q]
        out[f"{phase}_unfinished"] = [q for q in attempted if q not in passed_any]
        out[f"{phase}_score"] = len(passed_first if rule == "first" else passed_any)

    pre, post = out["pretest_score"], out["posttest_score"]
    out["pretest_to_posttest_delta"] = post - pre

    # 提示
    levels = [as_int(h.get("hint_level")) for h in my_hints if h.get("hint_level") not in (None, "")]
    sources: dict[str, int] = {}
    for h in my_hints:
        key = h.get("hint_source") or "unknown"
        sources[key] = sources.get(key, 0) + 1
    out["hint_requests"] = len([h for h in my_hints if h.get("entry") == "hint"])
    out["hint_events"] = len(my_hints)
    out["hint_level_max"] = max(levels) if levels else 0
    out["hint_sources"] = ";".join(f"{k}:{v}" for k, v in sorted(sources.items()))

    model_ms = [as_int(h.get("model_ms")) for h in my_hints if h.get("model_ms") not in (None, "")]
    out["model_ms_total"] = sum(model_ms)
    out["model_ms_avg"] = round(statistics.mean(model_ms), 1) if model_ms else ""

    # 错误类型（按提交次数计，不是按题）
    errors: dict[str, int] = {}
    for row in mine:
        cls = (row.get("error_type") or "").strip()
        if cls and cls != "none":
            errors[cls] = errors.get(cls, 0) + 1
    out["error_types"] = ";".join(f"{k}:{v}" for k, v in sorted(errors.items(), key=lambda kv: -kv[1]))
    out["judge_ms_total"] = sum(as_int(r.get("elapsed_ms")) for r in mine)
    out["judge_error_count"] = len([r for r in mine if (r.get("status") or "") == "error"])

    # 提示 → 下一次提交的间隔
    gaps: list[float] = []
    pending = 0
    for h in my_hints:
        qid = h.get("question_id")
        t_end = parse_ts(h.get("ts_end") or h.get("ts_start") or "")
        if t_end is None:
            continue
        later = [parse_ts(r.get("ts")) for r in by_q.get(qid, [])]
        later = [t for t in later if t is not None and t >= t_end]
        if later:
            gaps.append((min(later) - t_end).total_seconds())
        else:
            pending += 1
    out["pending_hints"] = pending
    out["gap_count"] = len(gaps)
    out["gap_median_s"] = round(statistics.median(gaps), 1) if gaps else ""
    out["gap_mean_s"] = round(statistics.mean(gaps), 1) if gaps else ""
    stamps = sorted(s for s in (r.get("ts") or "" for r in mine) if s)
    out["first_ts"] = stamps[0] if stamps else ""
    out["last_ts"] = stamps[-1] if stamps else ""
    return out


def question_stats(subs: list[dict], plan) -> list[dict]:
    """按题目统计成功率：提交通过率、首次提交通过、最终通过（按会话计）。"""
    phase_of = {q: p for p in PHASES for q in plan.get(p, [])}
    by_q: dict[str, list[dict]] = {}
    for row in subs:
        by_q.setdefault(row.get("question_id", ""), []).append(row)

    out: list[dict] = []
    for qid, rows in sorted(by_q.items()):
        ordered = sorted(rows, key=lambda r: (as_int(r.get("submit_seq")), r.get("ts") or ""))
        per_session: dict[str, list[dict]] = {}
        for row in ordered:
            per_session.setdefault(row.get("participant_id", ""), []).append(row)
        first_pass = len([1 for rs in per_session.values() if as_int(rs[0].get("pass_status")) == 1])
        eventual = len([1 for rs in per_session.values()
                        if any(as_int(x.get("pass_status")) == 1 for x in rs)])
        passed_submits = len([1 for row in rows if as_int(row.get("pass_status")) == 1])
        out.append({
            "question_id": qid,
            "phase": phase_of.get(qid, ""),
            "sessions_attempted": len(per_session),
            "submissions": len(rows),
            "passed_submissions": passed_submits,
            "first_try_pass_sessions": first_pass,
            "eventual_pass_sessions": eventual,
            "submit_pass_rate": round(passed_submits / len(rows), 3) if rows else "",
            "first_try_rate": round(first_pass / len(per_session), 3) if per_session else "",
            "eventual_rate": round(eventual / len(per_session), 3) if per_session else "",
        })
    return out


def prepost_stats(subs: list[dict], plan) -> tuple[list[dict], dict]:
    """前后测对比：只统计**同时做了**前测和后测的会话，按题计分（至少通过一次）。"""
    pre_qs, post_qs = set(plan.get("pretest", [])), set(plan.get("posttest", []))
    seen: dict[str, dict] = {}
    for row in subs:
        qid = row.get("question_id", "")
        if qid not in pre_qs and qid not in post_qs:
            continue
        bucket = "pre" if qid in pre_qs else "post"
        slot = seen.setdefault(row.get("participant_id", ""), {"pre": {}, "post": {}})[bucket]
        item = slot.setdefault(qid, {"any": False})
        if as_int(row.get("pass_status")) == 1:
            item["any"] = True

    rows: list[dict] = []
    for pid, data in sorted(seen.items()):
        pre, post = data["pre"], data["post"]
        if not pre or not post:
            continue
        pre_score = len([1 for v in pre.values() if v["any"]])
        post_score = len([1 for v in post.values() if v["any"]])
        # 分母固定为该阶段的题数：少做或不做的题按“未通过”计，避免“做得少反而比例高”
        rows.append({
            "participant_id": pid,
            "pretest_attempted": len(pre), "pretest_planned": len(pre_qs), "pretest_score": pre_score,
            "posttest_attempted": len(post), "posttest_planned": len(post_qs), "posttest_score": post_score,
            "delta_score": post_score - pre_score,
            "delta_rate": round((post_score - pre_score) / len(post_qs), 3) if post_qs else 0,
            "pretest_complete": len(pre) >= len(pre_qs),
            "posttest_complete": len(post) >= len(post_qs),
        })

    summary = {
        "sessions_with_both": len(rows),
        "sessions_pre_only": len([1 for d in seen.values() if d["pre"] and not d["post"]]),
        "sessions_post_only": len([1 for d in seen.values() if d["post"] and not d["pre"]]),
        "improved": len([1 for r in rows if r["delta_score"] > 0]),
        "unchanged": len([1 for r in rows if r["delta_score"] == 0]),
        "worse": len([1 for r in rows if r["delta_score"] < 0]),
        "pretest_fully_attempted": len([1 for r in rows if r["pretest_complete"]]),
        "posttest_fully_attempted": len([1 for r in rows if r["posttest_complete"]]),
    }
    if rows:
        summary["mean_delta_rate"] = round(statistics.mean(r["delta_rate"] for r in rows), 3)
        summary["median_delta_rate"] = round(statistics.median(r["delta_rate"] for r in rows), 3)
    return rows, summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="小规模试用逐人描述性分析（只读 CSV）")
    parser.add_argument("--export", required=True, help="export_experiment_data.py 的输出目录")
    parser.add_argument("--questions", default="data/questions", help="题库目录（用于推导各阶段题单）")
    parser.add_argument("--plan", default=None, help="可选：题目安排 JSON（pretest/practice/posttest）")
    parser.add_argument("--roster", default=None, help="可选：电脑编号 CSV（含 participant_id 列）")
    parser.add_argument("--rule", choices=("any", "first"), default="any",
                        help="计分规则：any=至少通过一次；first=首次提交即通过（默认 any）")
    parser.add_argument("--mode", choices=("flow", "free"), default="flow",
                        help="flow=前测-练习-后测流程口径；free=自由体验（不计算前后测与变化，默认 flow）")
    args = parser.parse_args(argv)

    export_dir = Path(args.export)
    subs = read_rows(export_dir / "submissions.csv")
    hints = read_rows(export_dir / "hint_events.csv")
    plan = load_plan(Path(args.questions), Path(args.plan) if args.plan else None)

    participants = sorted({row.get("participant_id", "") for row in subs if row.get("participant_id")})
    if args.roster:
        roster = read_rows(Path(args.roster))
        participants = sorted(set(participants) | {r.get("participant_id", "") for r in roster if r.get("participant_id")})

    rows = [analyze_participant(pid, subs, hints, plan, args.rule) for pid in participants]
    qstats = question_stats(subs, plan)

    if args.mode == "free":
        columns = ["participant_id", "questions_attempted", "questions_passed", "submissions",
                   "repeated_submissions", "hint_requests", "hint_level_max", "hint_sources",
                   "error_types", "model_ms_total", "judge_ms_total", "judge_error_count",
                   "pending_hints", "first_ts", "last_ts"]
    else:
        columns = ["participant_id", "submissions", "questions_attempted", "repeated_submissions"]
        for phase in PHASES:
            columns += [f"{phase}_expected", f"{phase}_attempted", f"{phase}_passed_any",
                        f"{phase}_passed_first", f"{phase}_score"]
        columns += ["pretest_to_posttest_delta", "hint_requests", "hint_events", "hint_level_max",
                    "hint_sources", "model_ms_total", "model_ms_avg", "error_types",
                    "judge_ms_total", "judge_error_count", "pending_hints", "gap_count",
                    "gap_median_s", "gap_mean_s", "pretest_missing", "pretest_unfinished",
                    "practice_missing", "practice_unfinished", "posttest_missing", "posttest_unfinished"]

    def cell(value):
        if isinstance(value, list):
            return " ".join(value)
        return value

    out_csv = export_dir / "trial_participants.csv"
    with out_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: cell(row.get(k, "")) for k in columns})

    qrows = question_stats(subs, plan)
    prows, psum = prepost_stats(subs, plan)

    q_csv = export_dir / "trial_questions.csv"
    q_fields = ["question_id", "phase", "sessions_attempted", "submissions", "passed_submissions",
                "first_try_pass_sessions", "eventual_pass_sessions",
                "submit_pass_rate", "first_try_rate", "eventual_rate"]
    with q_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=q_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(qrows)

    p_csv = export_dir / "trial_prepost.csv"
    p_fields = ["participant_id", "pretest_attempted", "pretest_planned", "pretest_score",
                "posttest_attempted", "posttest_planned", "posttest_score",
                "delta_score", "delta_rate", "pretest_complete", "posttest_complete"]
    with p_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=p_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(prows)

    def success_lines():
        out = ["", "## 成功率（按题，描述性）"]
        total = len(subs)
        passed = len([1 for r in subs if as_int(r.get("pass_status")) == 1])
        out.append(f"- 提交通过率：**{passed}/{total}**" + (f" = {passed / total * 100:.1f}%" if total else ""))
        tried = len(qrows)
        ever = len([1 for q in qrows if q["eventual_pass_sessions"] > 0])
        firstq = len([1 for q in qrows if q["first_try_pass_sessions"] > 0])
        out.append(f"- 被尝试的题目：{tried} 题；至少通过一次：**{ever}/{tried}**；首次提交即通过：**{firstq}/{tried}**")
        out += ["", "| 题目 | 阶段 | 尝试会话 | 提交 | 提交通过率 | 首次通过会话 | 最终通过会话 |",
                "|---|---|---|---|---|---|---|"]
        for q in qrows:
            rate = q["submit_pass_rate"]
            rate_text = "" if rate == "" else f"{rate * 100:.0f}%"
            out.append(f"| {q['question_id']} | {PHASE_LABEL.get(q['phase'], q['phase'] or '—')} "
                       f"| {q['sessions_attempted']} | {q['submissions']} | {rate_text} "
                       f"| {q['first_try_pass_sessions']} | {q['eventual_pass_sessions']} |")
        out += ["", "## 前后测对比（只含同时做了前测与后测的会话）",
                f"- 同时做了前后测的会话：**{psum['sessions_with_both']}**；只做前测 {psum['sessions_pre_only']}，"
                f"只做后测 {psum['sessions_post_only']}（不进入对比）。"]
        if prows:
            out.append(f"- 至少通过一次（按题计，分母固定为该阶段题数）变化：提升 {psum['improved']}、不变 {psum['unchanged']}、"
                       f"下降 {psum['worse']}；平均变化率 {psum['mean_delta_rate']:+.0%}，中位 {psum['median_delta_rate']:+.0%}。")
            out.append(f"- 前测题全部提交过：{psum['pretest_fully_attempted']}/{psum['sessions_with_both']}；"
                       f"后测题全部提交过：{psum['posttest_fully_attempted']}/{psum['sessions_with_both']}（未提交按未通过计）。")
            out += ["", "| 会话 | 前测得分 | 后测得分 | 变化(分) | 变化率 |", "|---|---|---|---|---|"]
            for r in prows:
                out.append(f"| {r['participant_id']} | {r['pretest_score']}/{r['pretest_planned']} "
                           f"| {r['posttest_score']}/{r['posttest_planned']} | {r['delta_score']:+d} "
                           f"| {r['delta_rate']:+.0%} |")
        else:
            out.append("- 目前没有哪个会话同时提交过前测与后测，暂无可比对的配对数据。")
        out += ["", "> 这不是随机分组、没有对照组，前后测之间还夹着自由练习，题目难度也不同；"
                    "上面的差值只能描述“观察到的情况”，不能当作教学效果或因果结论。"]
        return out

    if args.mode == "free":
        lines = ["# 自由体验使用记录（描述性）", ""]
        lines.append("- 按**匿名会话**汇总：**会话数不等于参与人数**（换浏览器、清 Cookie、重装会生成新会话）。")
        lines.append("- 不要求统一题量或顺序；没出现的题目只是没做，**不记为缺考或失败**。")
        lines.append("- 下方\"成功率\"与\"前后测对比\"同样是描述性指标，不做显著性检验、不当作教学效果证据。")
        lines.append("- `judge_ms` 是判题执行耗时、`model_ms` 是模型调用耗时，二者都不是作答时间。")
        lines.append("")
        lines.append("| 会话编号 | 尝试题目 | 通过题目 | 提交 | 提示次数 | 最高等级 | 判题耗时(ms) | 模型耗时(ms) | 判题故障 | 首次提交 | 最近提交 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for row in rows:
            lines.append(
                f"| {row['participant_id']} | {row['questions_attempted']} | {row['questions_passed']} "
                f"| {row['submissions']} | {row['hint_requests']} | L{row['hint_level_max']} "
                f"| {row['judge_ms_total']} | {row['model_ms_total']} | {row['judge_error_count']} "
                f"| {row['first_ts'] or '—'} | {row['last_ts'] or '—'} |"
            )
        lines.append("")
        lines.append("## 汇总")
        lines.append(f"- 会话数：**{len(rows)}**（不等于人数）。")
        lines.append(f"- 有提交的会话：{len([r for r in rows if r['submissions'] > 0])}；提交合计 {sum(r['submissions'] for r in rows)} 次。")
        lines.append(f"- 使用过提示的会话：{len([r for r in rows if r['hint_requests'] > 0])}；提示请求合计 {sum(r['hint_requests'] for r in rows)} 次。")
        lines.append(f"- 判题故障：{sum(r['judge_error_count'] for r in rows)} 次。")
        lines.extend(success_lines())
    else:
        lines = ["# 小规模试用逐人结果（描述性，不做显著性检验）", ""]
        lines.append(f"- 计分规则：**{'首次提交即通过' if args.rule == 'first' else '至少通过一次'}**，按题目计分（不是按提交次数）。")
        lines.append(f"- 题单：前测 {len(plan['pretest'])} 题 / 练习 {len(plan['practice'])} 题 / 后测 {len(plan['posttest'])} 题。")
        lines.append("- 百分比一律写成“分子/分母”；缺失题目与未完成题目在最后一节逐人列出。")
        lines.append("")
        lines.append("| 参与者 | 前测得分 | 练习通过 | 后测得分 | 前后变化 | 提交 | 提示 | 提示等级上限 | 判题耗时(ms) | 模型耗时(ms) |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for row in rows:
            lines.append(
                f"| {row['participant_id']} | {row['pretest_score']}/{row['pretest_expected']} "
                f"| {row['practice_passed_any']}/{row['practice_expected']} "
                f"| {row['posttest_score']}/{row['posttest_expected']} "
                f"| {row['pretest_to_posttest_delta']:+d} | {row['submissions']} | {row['hint_requests']} "
                f"| L{row['hint_level_max']} | {row['judge_ms_total']} | {row['model_ms_total']} |"
            )
        lines.append("")
        lines.append("## 缺失与未完成")
        for row in rows:
            missing = [f"{PHASE_LABEL[p]} {q}" for p in PHASES for q in row.get(f"{p}_missing", [])]
            unfinished = [f"{PHASE_LABEL[p]} {q}" for p in PHASES for q in row.get(f"{p}_unfinished", [])]
            lines.append(f"- **{row['participant_id']}**：未提交 {('、'.join(missing) or '无')}；提交未通过 {('、'.join(unfinished) or '无')}；"
                         f"无后续提交的提示 {row['pending_hints']} 次；判题故障 {row['judge_error_count']} 次。")
        lines.append("")
        lines.append("## 口径提醒")
        lines.append("- `judge_ms` 是判题执行耗时，`model_ms` 是模型调用耗时，二者都不是学生作答时间。")
        lines.append("- `gap_median_s`/`gap_mean_s` 是“提示 → 下一次提交”的间隔，含阅读、修改与操作。")
        lines.append("- 没有下一次提交的提示单列（`pending_hints`），不按失败处理，也不剔除。")
        lines.append("- 小样本只报告逐人结果与简单汇总；\"前后测对比\"一节只统计同时做了两侧的会话，仍属描述性。")
        lines.extend(success_lines())
    (export_dir / "trial_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"✅ 写出 {out_csv}（{len(rows)} 个会话）与 trial_summary.md")
    print(f"   模式：{'自由体验（描述性，含成功率与前后测配对对比）' if args.mode == 'free' else '流程（前测/练习/后测）'}；计分规则 {args.rule}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
