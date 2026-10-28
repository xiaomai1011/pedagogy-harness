"""提示等级 ↔ 下一次提交结果 ↔ 耗时：可计算性与样例（基于预演/任意指定库）。

明确区分三种时间：
  - judge_ms：submission_log.elapsed_ms（判题耗时）
  - model_ms：event_log.elapsed_ms（提示请求整请求耗时，含判题）
  - gap_s：提示结束到下一条提交开始的间隔（**不等于纯思考时间**，含阅读与编辑）
"""
import argparse
import json
import pathlib
import sqlite3
from datetime import datetime

REPO = pathlib.Path(r"D:\Downloads\Trae-workspace\pedagogy-harness")

ap = argparse.ArgumentParser()
ap.add_argument("--db", default=str(REPO / "data" / "experiment_preflight.sqlite"))
ap.add_argument("--out", default=str(REPO / "docs" / "G11_提示等级_通过率_耗时样例.json"))
ap.add_argument("--label", default="预演数据（模拟，不计入真实学习效果）")
args = ap.parse_args()

conn = sqlite3.connect(args.db)
conn.row_factory = sqlite3.Row

hints = [dict(r) for r in conn.execute(
    "select participant_id, question_id, hint_level, unlocked_level, hint_source, "
    "ts_start, ts_end, elapsed_ms from event_log "
    "where entry='hint' and hint_level is not null and hint_level > 0 order by ts_end")]
subs = [dict(r) for r in conn.execute(
    "select participant_id, question_id, ts, status, pass_status, elapsed_ms, hint_level "
    "from submission_log order by ts")]


def parse(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


rows = []
for h in hints:
    end = parse(h["ts_end"])
    nxt = None
    for s in subs:
        if s["participant_id"] != h["participant_id"] or s["question_id"] != h["question_id"]:
            continue
        t = parse(s["ts"])
        if end and t and t >= end:
            nxt = (s, t)
            break
    rows.append({
        "participant": h["participant_id"],
        "question": h["question_id"],
        "hint_level": h["hint_level"],
        "hint_source": h["hint_source"],
        "model_request_ms": h["elapsed_ms"],
        "next_submission": None if not nxt else {
            "status": nxt[0]["status"],
            "passed": bool(nxt[0]["pass_status"]),
            "judge_ms": nxt[0]["elapsed_ms"],
            "gap_seconds": round((nxt[1] - end).total_seconds(), 1) if end else None,
        },
    })

by_level = {}
for r in rows:
    if not r["next_submission"]:
        continue
    b = by_level.setdefault(r["hint_level"], {"n": 0, "passed": 0, "gaps": [], "judge_ms": []})
    b["n"] += 1
    b["passed"] += int(r["next_submission"]["passed"])
    if r["next_submission"]["gap_seconds"] is not None:
        b["gaps"].append(r["next_submission"]["gap_seconds"])
    b["judge_ms"].append(r["next_submission"]["judge_ms"])

summary = {
    "data_label": args.label,
    "db": str(pathlib.Path(args.db)),
    "hint_events_with_level": len(rows),
    "hint_events_followed_by_submission": sum(1 for r in rows if r["next_submission"]),
    "by_level": {
        str(lv): {
            "n": v["n"],
            "next_submission_pass_rate": round(v["passed"] / v["n"], 3),
            "mean_gap_seconds": round(sum(v["gaps"]) / len(v["gaps"]), 1) if v["gaps"] else None,
            "mean_judge_ms": round(sum(v["judge_ms"]) / len(v["judge_ms"]), 1),
        } for lv, v in sorted(by_level.items())
    },
    "caveats": [
        "gap_seconds 是提示结束到下次提交的间隔，不等于纯思考时间（含阅读、编辑、犹豫）。",
        "model_request_ms 为该次提示请求整请求耗时（判题+模型），不是单独模型耗时。",
        "judge_ms 取自 submission_log.elapsed_ms，是判题耗时，不可当学生作答时间。",
        "预演数据由脚本模拟，仅证明口径可计算，不进入真实学习效果分析。",
    ],
}

out = {"summary": summary, "rows": rows}
pathlib.Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
print("written", args.out)
