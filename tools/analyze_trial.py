"""小规模试用（4–6 人）的逐人描述性分析。

输入是 `tools/export_experiment_data.py` 产出的 CSV（同一快照），不直接连数据库：

    python tools/export_experiment_data.py --db <库> --out <目录> --backup <备份>
    python tools/analyze_trial.py --export <目录> [--plan <题目安排.json>] [--roster <电脑编号.csv>]

产出（写回 `--export` 目录）：

- ``trial_participants.csv`` ：**一人一行**，前测/练习/后测的题数、完成数、通过数，
  提交次数、提示次数与等级/来源、错误类型分布、判题与模型耗时、缺失与未完成题目；
- ``trial_summary.md`` ：同样内容的表格版摘要，并写明口径与分母。

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
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="小规模试用逐人描述性分析（只读 CSV）")
    parser.add_argument("--export", required=True, help="export_experiment_data.py 的输出目录")
    parser.add_argument("--questions", default="data/questions", help="题库目录（用于推导各阶段题单）")
    parser.add_argument("--plan", default=None, help="可选：题目安排 JSON（pretest/practice/posttest）")
    parser.add_argument("--roster", default=None, help="可选：电脑编号 CSV（含 participant_id 列）")
    parser.add_argument("--rule", choices=("any", "first"), default="any",
                        help="计分规则：any=至少通过一次；first=首次提交即通过（默认 any）")
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
    lines.append("- 小样本只报告逐人结果与简单汇总；如需比较，先确认评分规则与题目等价性，不在本工具里做检验。")
    (export_dir / "trial_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"✅ 写出 {out_csv}（{len(rows)} 人）与 trial_summary.md")
    print(f"   题单：前测 {len(plan['pretest'])} / 练习 {len(plan['practice'])} / 后测 {len(plan['posttest'])}；计分规则 {args.rule}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
