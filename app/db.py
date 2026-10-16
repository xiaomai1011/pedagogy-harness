"""SQLite 建表与日志写入（契约 §3、§4）。

岗位 1 负责建表；岗位 3 在 11/01 前按本文件字段定义把过程日志写进来，并补单元测试。
"""

import os
import sqlite3

DB_PATH = os.environ.get("PH_DB", "pedagogy.sqlite")

# 契约 §3 日志表
LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS submission_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT    NOT NULL,
    participant_id TEXT    NOT NULL,
    group_id       TEXT    NOT NULL,
    question_id    TEXT    NOT NULL,
    submit_seq     INTEGER NOT NULL,
    status         TEXT    NOT NULL,
    error_type     TEXT    NOT NULL,
    error_class    TEXT    NOT NULL,
    error_kp       TEXT    NOT NULL DEFAULT '',
    hint_level     INTEGER NOT NULL DEFAULT 0,
    hint_requested INTEGER NOT NULL DEFAULT 0,
    pass_status    INTEGER NOT NULL DEFAULT 0,
    elapsed_ms     INTEGER NOT NULL DEFAULT 0
);
"""

# 契约 §4 错误记忆表（按 participant_id 跨会话累计）
MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS error_memory (
    participant_id TEXT NOT NULL,
    error_class    TEXT NOT NULL,
    knowledge_point TEXT NOT NULL,
    count          INTEGER NOT NULL DEFAULT 0,
    last_seen_ts   TEXT NOT NULL,
    PRIMARY KEY (participant_id, error_class, knowledge_point)
);
"""


_initialized = False


def connect() -> sqlite3.Connection:
    """连接数据库，并在首次连接时自动建表。

    自动建表是为了让**任何入口**（服务、脚本、单测）都不会撞上
    "no such table" —— 岗位 3 与统计同学都会写绕过 main.py 的脚本。
    """
    global _initialized
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not _initialized:
        conn.execute(LOG_SCHEMA)
        conn.execute(MEMORY_SCHEMA)
        conn.commit()
        _initialized = True
    return conn


def init_db() -> None:
    """显式建表（与 connect() 的自动建表等价，保留供 main.py 启动时调用）。"""
    connect()


def next_submit_seq(participant_id: str, question_id: str) -> int:
    """该被试在该题的第几次提交（契约 §3 的 submit_seq）。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM submission_log WHERE participant_id=? AND question_id=?",
            (participant_id, question_id),
        ).fetchone()
    return int(row["n"]) + 1


def log_submission(*, ts: str, participant_id: str, group_id: str, question_id: str,
                   submit_seq: int, status: str, error_type: str, error_class: str,
                   error_kp: str, hint_level: int, hint_requested: int,
                   pass_status: int, elapsed_ms: int) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO submission_log
               (ts, participant_id, group_id, question_id, submit_seq, status,
                error_type, error_class, error_kp, hint_level, hint_requested,
                pass_status, elapsed_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, participant_id, group_id, question_id, submit_seq, status,
             error_type, error_class, error_kp, hint_level, hint_requested,
             pass_status, elapsed_ms),
        )


def bump_error_memory(participant_id: str, error_class: str, knowledge_point: str, ts: str) -> None:
    """命中即 count+1（契约 §4）。仅供开启 error_memory 时调用。"""
    with connect() as conn:
        conn.execute(
            """INSERT INTO error_memory (participant_id, error_class, knowledge_point, count, last_seen_ts)
               VALUES (?,?,?,1,?)
               ON CONFLICT(participant_id, error_class, knowledge_point)
               DO UPDATE SET count = count + 1, last_seen_ts = excluded.last_seen_ts""",
            (participant_id, error_class, knowledge_point, ts),
        )


def get_error_memory(participant_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM error_memory WHERE participant_id=? ORDER BY count DESC",
            (participant_id,),
        ).fetchall()
    return [dict(r) for r in rows]
