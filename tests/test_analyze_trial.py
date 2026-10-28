"""分析工具的统计口径：按题成功率与前后测配对对比。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.analyze_trial import prepost_stats, question_stats  # noqa: E402

PLAN = {
    "pretest": ["S001", "S002"],
    "practice": ["P001"],
    "posttest": ["S004", "S005"],
}


def _sub(pid: str, qid: str, seq: int, passed: int) -> dict:
    return {
        "participant_id": pid,
        "question_id": qid,
        "submit_seq": str(seq),
        "pass_status": str(passed),
        "ts": f"2026-10-29T10:00:{seq:02d}",
    }


def test_question_stats_counts_sessions_and_passes():
    subs = [
        _sub("A001", "P001", 1, 0),
        _sub("A001", "P001", 2, 1),
        _sub("A002", "P001", 1, 1),
        _sub("A003", "P001", 1, 0),
    ]
    stats = {row["question_id"]: row for row in question_stats(subs, PLAN)}
    p001 = stats["P001"]
    assert p001["phase"] == "practice"
    assert p001["sessions_attempted"] == 3
    assert p001["submissions"] == 4
    assert p001["passed_submissions"] == 2
    assert p001["first_try_pass_sessions"] == 1  # 只有 A002 首提交即通过
    assert p001["eventual_pass_sessions"] == 2  # A001 后来通过、A002 通过
    assert p001["submit_pass_rate"] == 0.5
    assert p001["first_try_rate"] == round(1 / 3, 3)
    assert p001["eventual_rate"] == round(2 / 3, 3)


def test_prepost_only_pairs_sessions_with_both_sides():
    subs = [
        _sub("A001", "S001", 1, 1),  # 前测通过
        _sub("A001", "S004", 1, 1),  # 后测通过 → 不变
        _sub("A002", "S001", 1, 0),  # 前测未通过
        _sub("A002", "S004", 1, 1),  # 后测通过 → 提升
        _sub("A003", "S001", 1, 0),  # 只做前测
        _sub("A004", "S005", 1, 1),  # 只做后测
    ]
    rows, summary = prepost_stats(subs, PLAN)
    by_id = {row["participant_id"]: row for row in rows}

    assert set(by_id) == {"A001", "A002"}
    assert by_id["A001"]["delta_passed"] == 0
    assert by_id["A002"]["delta_passed"] == 1
    assert by_id["A002"]["delta_rate"] == 1.0
    assert summary["sessions_with_both"] == 2
    assert summary["sessions_pre_only"] == 1
    assert summary["sessions_post_only"] == 1
    assert summary["improved"] == 1
    assert summary["unchanged"] == 1
    assert summary["worse"] == 0


def test_prepost_without_pairs_is_empty_not_error():
    rows, summary = prepost_stats([_sub("A001", "P001", 1, 1)], PLAN)
    assert rows == []
    assert summary["sessions_with_both"] == 0
