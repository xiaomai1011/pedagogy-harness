"""匿名会话的阶段流程：前测 → 练习 → 后测。

时间地点自由，但**顺序由服务端锁定**：会话从 `pretest` 开始，只能提交当前阶段的题目；
学生点「结束本阶段」后进入下一阶段，之前阶段的题目不再接受提交（防止回头改前测）。

这里只存"当前阶段"，题目与题目顺序仍来自题库/发布配置，不新增题库字段。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

PHASES = ("pretest", "practice", "posttest", "done")
LABELS = {"pretest": "前测", "practice": "练习", "posttest": "后测", "done": "已完成"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_phase (
    participant_id TEXT PRIMARY KEY,
    batch TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL DEFAULT 'pretest',
    pretest_done_ts TEXT NOT NULL DEFAULT '',
    practice_done_ts TEXT NOT NULL DEFAULT '',
    updated_ts TEXT NOT NULL DEFAULT ''
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    from app import db

    conn = db.connect()
    conn.execute(SCHEMA)
    return conn


def ensure(participant_id: str, batch: str = "") -> str:
    """建行（幂等）并返回当前阶段。"""
    if not participant_id:
        return "pretest"
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO session_phase (participant_id, batch, phase, updated_ts) VALUES (?, ?, 'pretest', ?)",
            (participant_id, batch, _now()),
        )
        row = conn.execute("SELECT phase FROM session_phase WHERE participant_id = ?", (participant_id,)).fetchone()
    return (row["phase"] if row else "pretest") or "pretest"


def current(participant_id: str, batch: str = "") -> str:
    return ensure(participant_id, batch)


def advance(participant_id: str, batch: str = "") -> dict:
    """结束当前阶段，进入下一阶段；`done` 之后不再变化。"""
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO session_phase (participant_id, batch, phase, updated_ts) VALUES (?, ?, 'pretest', ?)",
            (participant_id, batch, _now()),
        )
        row = conn.execute("SELECT * FROM session_phase WHERE participant_id = ?", (participant_id,)).fetchone()
        phase = (row["phase"] if row else "pretest") or "pretest"
        if phase in ("done",):
            return {"phase": "done", "label": LABELS["done"], "changed": False}
        idx = PHASES.index(phase)
        nxt = PHASES[idx + 1]
        stamp_col = f"{phase}_done_ts"
        fields = {"updated_ts": _now()}
        if stamp_col in ("pretest_done_ts", "practice_done_ts"):
            fields[stamp_col] = _now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE session_phase SET phase = ?, {sets} WHERE participant_id = ?",
                     (nxt, *fields.values(), participant_id))
    return {"phase": nxt, "label": LABELS[nxt], "changed": True}


def allows(participant_id: str, question_phase: str, batch: str = "") -> bool:
    """当前阶段是否允许访问该阶段的题目。"""
    if not participant_id:
        return True
    return current(participant_id, batch) == question_phase
