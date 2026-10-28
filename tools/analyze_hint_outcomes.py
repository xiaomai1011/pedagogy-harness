"""提示等级 ↔ 下一次提交结果 ↔ 耗时：可计算性与样例（基于预演/任意指定库）。

三种时间必须分开（契约口径）：
  - judge_ms：submission_log.elapsed_ms —— 判题耗时
  - model_ms：event_log.model_ms —— 本请求里所有模型调用累计耗时（含分类兜底、提示、重试）；
              历史行为 NULL 表示"未记录"，不是 0。无模型调用才是 0。
  - request_ms：event_log.elapsed_ms —— 整个提示请求耗时（判题 + 模型 + 其他）
  - gap_s：提示结束→下一条提交开始的间隔，**不等于纯思考时间**

归因口径（2026-10-28 修正）：
  - 一次提交前可能有多条提示。**主分析取"该提交之前最后一条有效提示"**，不把每条提示都算成一次独立成功。
  - 还没有下一次提交的提示**单列**在 pending_hints 里，不剔除、不并入通过率。
"""
import argparse
import json
import pathlib
import sqlite3
from datetime import datetime

REPO = pathlib.Path(__file__).resolve().parent.parent

ap = argparse.ArgumentParser()
ap.add_argument("--db", default=str(REPO / "data" / "experiment_preflight.sqlite"))
ap.add_argument("--out", default=str(REPO / "docs" / "G11_提示等级_通过率_耗时样例.json"))
ap.add_argument("--label", default="预演数据（模拟，不计入真实学习效果）")
args = ap.parse_args()

conn = sqlite3.connect(args.db)
conn.row_factory = sqlite3.Row

hints = [dict(r) for r in conn.execute(
    "select participant_id, question_id, hint_level, unlocked_level, hint_source, "
    "ts_start, ts_end, elapsed_ms, model_ms, judge_status from event_log "
    "where entry='hint' and hint_level is not null and hint_level > 0 order by ts_end")]
subs = [dict(r) for r in conn.execute(
    "select participant_id, question_id, ts, status, pass_status, elapsed_ms, hint_level "
    "from submission_log order by ts")]


def parse(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


for h in hints:
    h["_end"] = parse(h["ts_end"])
for s in subs:
    s["_t"] = parse(s["ts"])

# 每条提示之后的第一条提交（用于行级历史与 pending 判定）
for h in hints:
    h["next_sub"] = None
    for s in subs:
        if s["participant_id"] != h["participant_id"] or s["question_id"] != h["question_id"]:
            continue
        if h["_end"] and s["_t"] and s["_t"] >= h["_end"]:
            h["next_sub"] = s
            break

# 主分析：每条提交归因到"它之前最后一条提示"
attributed = []
for s in subs:
    prev = None
    for h in hints:
        if h["participant_id"] != s["participant_id"] or h["question_id"] != s["question_id"]:
            continue
        if h["_end"] and s["_t"] and h["_end"] <= s["_t"]:
            if prev is None or h["_end"] >= prev["_end"]:
                prev = h
    if prev is not None:
        attributed.append((prev, s))

rows = []
for h in hints:
    nxt = h["next_sub"]
    rows.append({
        "participant": h["participant_id"],
        "question": h["question_id"],
        "hint_level": h["hint_level"],
        "hint_source": h["hint_source"],
        "model_ms": h["model_ms"],
        "request_ms": h["elapsed_ms"],
        "next_submission": None if not nxt else {
            "status": nxt["status"],
            "passed": bool(nxt["pass_status"]),
            "judge_ms": nxt["elapsed_ms"],
            "gap_seconds": round((nxt["_t"] - h["_end"]).total_seconds(), 1),
        },
    })

by_level = {}
for h, s in attributed:
    b = by_level.setdefault(h["hint_level"], {"n": 0, "passed": 0, "gaps": [], "judge_ms": [], "model_ms": []})
    b["n"] += 1
    b["passed"] += int(bool(s["pass_status"]))
    b["gaps"].append(round((s["_t"] - h["_end"]).total_seconds(), 1))
    b["judge_ms"].append(s["elapsed_ms"])
    if h["model_ms"] is not None:
        b["model_ms"].append(h["model_ms"])

pending = [h for h in hints if h["next_sub"] is None]

summary = {
    "data_label": args.label,
    "db": str(pathlib.Path(args.db)),
    "hint_events_with_level": len(rows),
    "submissions_attributed_to_a_previous_hint": len(attributed),
    "pending_hints_without_next_submission": len(pending),
    "pending_hint_levels": [h["hint_level"] for h in pending],
    "by_level": {
        str(lv): {
            "n": v["n"],
            "next_submission_pass_rate": round(v["passed"] / v["n"], 3),
            "mean_gap_seconds": round(sum(v["gaps"]) / len(v["gaps"]), 1) if v["gaps"] else None,
            "mean_judge_ms": round(sum(v["judge_ms"]) / len(v["judge_ms"]), 1) if v["judge_ms"] else None,
            "mean_model_ms": round(sum(v["model_ms"]) / len(v["model_ms"]), 1) if v["model_ms"] else None,
        } for lv, v in sorted(by_level.items())
    },
    "caveats": [
        "主分析按提交归因：一次提交只看它之前最后一条有效提示，多提示不重复计入通过率。",
        "pending_hints_without_next_submission 单列，未剔除；不得用剔除后的子集宣称高通过率。",
        "gap_seconds 是提示结束到下次提交的间隔，不等于纯思考时间（含阅读、编辑、犹豫）。",
        "model_ms 是模型调用累计耗时；历史行为 NULL（未记录），无模型调用才是 0。",
        "request_ms 为提示请求整请求耗时（判题+模型+其他），不能当模型耗时。",
        "judge_ms 取自 submission_log.elapsed_ms，是判题耗时，不可当学生作答时间。",
        "预演数据由脚本模拟，仅证明口径可计算，不进入真实学习效果分析。",
    ],
}

out = {"summary": summary, "rows": rows}
pathlib.Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
print("written", args.out)
