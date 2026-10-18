"""SQLite 建表与日志写入（契约 §3、§4）。

岗位 1 负责建表；岗位 3 在 11/01 前按本文件字段定义把过程日志写进来，并补单元测试。
"""

import os
import sqlite3

DB_PATH = os.environ.get("PH_DB", "pedagogy.sqlite")

# 契约 §3 日志表
LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS submission_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT    NOT NULL,
    participant_id      TEXT    NOT NULL,
    group_id            TEXT    NOT NULL,
    question_id         TEXT    NOT NULL,
    phase               TEXT    NOT NULL DEFAULT 'practice',
    submit_seq          INTEGER NOT NULL,
    status              TEXT    NOT NULL,
    error_type          TEXT    NOT NULL,
    error_class         TEXT    NOT NULL,
    error_kp            TEXT    NOT NULL DEFAULT '',
    hint_level          INTEGER NOT NULL DEFAULT 0,
    hint_unlocked_level INTEGER NOT NULL DEFAULT 0,
    hint_requested      INTEGER NOT NULL DEFAULT 0,
    pass_status         INTEGER NOT NULL DEFAULT 0,
    elapsed_ms          INTEGER NOT NULL DEFAULT 0
);
"""

# 冻结后新增列（2026-10-17，契约 §3 修订）的兼容迁移。
# 旧库里的 submission_log 没有这两列，而 `CREATE TABLE IF NOT EXISTS` **不会补列**，
# 不迁移的话 INSERT 会直接报 "no such column"（本仓库已有旧库 pedagogy.sqlite）。
_COLUMN_MIGRATIONS = {
    "phase": "ALTER TABLE submission_log ADD COLUMN phase TEXT NOT NULL DEFAULT 'practice'",
    "hint_unlocked_level":
        "ALTER TABLE submission_log ADD COLUMN hint_unlocked_level INTEGER NOT NULL DEFAULT 0",
}

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


# 契约 §6 事件表（关联事件日志）：一张表承载 **执行／提示／教学处理** 三类事件。
# 用**显式列**（不做通用 JSON payload）；`UNIQUE(request_id, event_type)` 只防同一请求内重复落事件。
EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_log (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type     TEXT    NOT NULL,
    request_id     TEXT    NOT NULL,
    submission_id  INTEGER,
    participant_id TEXT    NOT NULL,
    question_id    TEXT    NOT NULL,
    phase          TEXT    NOT NULL DEFAULT 'practice',
    entry          TEXT    NOT NULL DEFAULT '',
    ts_start       TEXT    NOT NULL,
    ts_end         TEXT,
    elapsed_ms     INTEGER,
    status         TEXT    NOT NULL,
    progress       TEXT    NOT NULL DEFAULT '',
    hint_level     INTEGER,
    unlocked_level INTEGER,
    hint_source    TEXT,
    judge_status   TEXT,
    reason_code    TEXT    NOT NULL DEFAULT '',
    reason         TEXT    NOT NULL DEFAULT '',
    UNIQUE (request_id, event_type)
);
CREATE INDEX IF NOT EXISTS idx_event_request    ON event_log(request_id);
CREATE INDEX IF NOT EXISTS idx_event_submission ON event_log(submission_id);
CREATE INDEX IF NOT EXISTS idx_event_type_ts    ON event_log(event_type, ts_start);
"""

_EVENT_COLUMNS = (
    "event_type", "request_id", "submission_id", "participant_id", "question_id",
    "phase", "entry", "ts_start", "ts_end", "elapsed_ms", "status", "progress",
    "hint_level", "unlocked_level", "hint_source", "judge_status",
    "reason_code", "reason",
)

# G-② 第 5 项：**独立结构化运行记录**（G-① §3.5 方案 B）。
# 为什么单独一张表而不是塞进 event_log：`UNIQUE(request_id, event_type)` 意味着
# 同一请求只能有一条同类事件，而「隔离不可用」与「清理失败」都可能与判题结果**分开**
# 发生、且清理失败可能一请求多条（每用例一条）。清理失败**不得改写判题结果**，
# 所以也不该复用 execution 事件的 `reason_code` 字段。
# 字段刻意保持最小：只有**稳定执行标识**（run_id）、**请求关联**（request_id）、
# **受控码**（detail_code）与时间 —— **不存**异常全文、隐藏用例输入、密钥、模型原始响应。
# ⚠️ 字段与消费方仍待负责人裁定（G-③ 第 5 项），本表属骨架。
ISOLATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS isolation_run_log (
    run_id      TEXT    NOT NULL,
    request_id  TEXT    NOT NULL,
    case_index  INTEGER,
    phase       TEXT    NOT NULL DEFAULT '',
    event       TEXT    NOT NULL,
    detail_code TEXT    NOT NULL DEFAULT '',
    ts          TEXT    NOT NULL,
    PRIMARY KEY (run_id, event)
);
CREATE INDEX IF NOT EXISTS idx_isolation_request ON isolation_run_log(request_id);
"""

_ISOLATION_COLUMNS = ("run_id", "request_id", "case_index", "phase", "event", "detail_code", "ts")

_initialized = False


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """补齐冻结后新增的列（幂等）。"""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(submission_log)").fetchall()}
    for col, ddl in _COLUMN_MIGRATIONS.items():
        if col not in have:
            conn.execute(ddl)


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
        conn.executescript(EVENT_SCHEMA)
        conn.executescript(ISOLATION_SCHEMA)
        _ensure_columns(conn)
        conn.commit()
        _initialized = True
    return conn


def connect_immediate() -> sqlite3.Connection:
    """返回一个**显式事务**连接（`isolation_level=None`），由调用方自己
    `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`。

    为什么需要：`with connect() as conn:` 只保证"退出时提交／异常时回滚"，
    **不保证进入时立刻开始事务** —— 先 `SELECT` 再 `UPDATE` 之间仍可能被并发插入。
    需要"读-改-写"原子时用本函数 + `BEGIN IMMEDIATE`（一上来就取写锁）。
    """
    conn = connect()
    conn.isolation_level = None
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


def log_submission_conn(conn: sqlite3.Connection, **payload) -> int:
    """**用调用方的连接**写提交行 —— 便于与教学状态放进**同一个短事务**（契约 §6.3 第 1 步）。

    返回 `submission_log.id`（用于回填 `event_log.submission_id`）。
    """
    cur = conn.execute(
        """INSERT INTO submission_log
           (ts, participant_id, group_id, question_id, phase, submit_seq, status,
            error_type, error_class, error_kp, hint_level, hint_unlocked_level,
            hint_requested, pass_status, elapsed_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (payload["ts"], payload["participant_id"], payload["group_id"], payload["question_id"],
         payload["phase"], payload["submit_seq"], payload["status"], payload["error_type"],
         payload["error_class"], payload["error_kp"], payload["hint_level"],
         payload["hint_unlocked_level"], payload["hint_requested"], payload["pass_status"],
         payload["elapsed_ms"]),
    )
    return int(cur.lastrowid)


def log_submission(**payload) -> None:
    with connect() as conn:
        log_submission_conn(conn, **payload)


def get_submission_conn(conn: sqlite3.Connection, submission_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM submission_log WHERE id=?", (submission_id,)).fetchone()
    return dict(row) if row else None


def update_submission_conn(conn: sqlite3.Connection, submission_id: int, **fields) -> None:
    """回填提交行的教学字段（分类／提示等级等）。只允许白名单列。"""
    allowed = {"error_class", "error_kp", "hint_level", "hint_unlocked_level"}
    cols = [k for k in fields if k in allowed]
    if not cols or submission_id is None:
        return
    conn.execute(
        f"UPDATE submission_log SET {', '.join(f'{c}=?' for c in cols)} WHERE id=?",
        (*[fields[c] for c in cols], submission_id),
    )


# ---------------------------------------------------------------- 契约 §6 事件表

def create_event_conn(conn: sqlite3.Connection, **fields) -> int:
    """**用调用方的连接**插入一条事件，返回 `event_id`。"""
    cols = [c for c in _EVENT_COLUMNS if c in fields]
    cur = conn.execute(
        f"INSERT INTO event_log ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        tuple(fields[c] for c in cols),
    )
    return int(cur.lastrowid)


def create_event(**fields) -> int:
    with connect() as conn:
        return create_event_conn(conn, **fields)


def update_event_conn(conn: sqlite3.Connection, event_id: int, **fields) -> None:
    cols = [c for c in _EVENT_COLUMNS if c in fields]
    if not cols or event_id is None:
        return
    conn.execute(
        f"UPDATE event_log SET {', '.join(f'{c}=?' for c in cols)} WHERE event_id=?",
        (*[fields[c] for c in cols], event_id),
    )


def update_event(event_id: int, **fields) -> None:
    with connect() as conn:
        update_event_conn(conn, event_id, **fields)


def get_event_conn(conn: sqlite3.Connection, request_id: str, event_type: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM event_log WHERE request_id=? AND event_type=?",
        (request_id, event_type)).fetchone()
    return dict(row) if row else None


def get_event(event_id: int) -> dict | None:
    """按 `event_id` 读单条事件（用于读出 `progress` 等进度标记）。"""
    with connect() as conn:
        row = conn.execute("SELECT * FROM event_log WHERE event_id=?", (event_id,)).fetchone()
    return dict(row) if row else None


def get_events(*, participant_id: str | None = None, request_id: str | None = None,
               event_type: str | None = None) -> list[dict]:
    """按被试／请求／类型查事件（统计与测试用）。"""
    where, args = [], []
    for col, val in (("participant_id", participant_id),
                     ("request_id", request_id), ("event_type", event_type)):
        if val is not None:
            where.append(f"{col}=?")
            args.append(val)
    sql = "SELECT * FROM event_log" + (f" WHERE {' AND '.join(where)}" if where else "")
    sql += " ORDER BY event_id"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]



def upsert_error_memory_conn(conn: sqlite3.Connection, participant_id: str, error_class: str,
                            knowledge_point: str, ts: str) -> None:
    """**用调用方的连接**做 count+1 —— 与分类回填、进度标记放进**同一个短事务**（契约 §6.3 第 2 步）。"""
    conn.execute(
        """INSERT INTO error_memory (participant_id, error_class, knowledge_point, count, last_seen_ts)
           VALUES (?,?,?,1,?)
           ON CONFLICT(participant_id, error_class, knowledge_point)
           DO UPDATE SET count = count + 1, last_seen_ts = excluded.last_seen_ts""",
        (participant_id, error_class, knowledge_point, ts),
    )


def bump_error_memory(participant_id: str, error_class: str, knowledge_point: str, ts: str) -> None:
    """命中即 count+1（契约 §4）。仅供开启 error_memory 时调用。"""
    with connect() as conn:
        upsert_error_memory_conn(conn, participant_id, error_class, knowledge_point, ts)


def get_error_memory(participant_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM error_memory WHERE participant_id=? ORDER BY count DESC",
            (participant_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------ G-② 独立结构化运行记录（§3.5 方案 B）

def log_isolation_run_conn(conn: sqlite3.Connection, **payload) -> None:
    """**用调用方的连接**写一条运行记录（与判题事务解耦时用 `log_isolation_run`）。"""
    cols = [c for c in _ISOLATION_COLUMNS if c in payload]
    conn.execute(
        f"INSERT OR REPLACE INTO isolation_run_log ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})",
        tuple(payload[c] for c in cols),
    )


def log_isolation_run(**payload) -> None:
    """写一条独立运行记录（隔离不可用／清理失败）。

    **必须显式 `close()`**：`with connect() as conn:` 只提交／回滚，**不关连接**；
    Python 3.13 起未关闭的 `sqlite3.Connection` 不再在回收时自动关闭，
    Windows 下残留的文件句柄会让调用方随后删不掉临时库所在目录。
    """
    conn = connect()
    try:
        log_isolation_run_conn(conn, **payload)
        conn.commit()
    finally:
        conn.close()


def get_isolation_runs(*, request_id: str | None = None, run_id: str | None = None,
                       event: str | None = None) -> list[dict]:
    """按请求／执行标识／事件名读运行记录（隔离验收入口与运维排查的消费方）。"""
    where, args = [], []
    for col, val in (("request_id", request_id), ("run_id", run_id), ("event", event)):
        if val is not None:
            where.append(f"{col}=?")
            args.append(val)
    sql = "SELECT * FROM isolation_run_log" + (f" WHERE {' AND '.join(where)}" if where else "")
    sql += " ORDER BY ts, rowid"
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]
    finally:
        conn.close()
