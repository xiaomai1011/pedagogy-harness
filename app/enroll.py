"""匿名会话登记：服务端给每台浏览器分配一个编号，只用来关联这台浏览器的使用记录。

设计边界（写清楚，避免误解）：

- 只在 `PH_AUTO_ENROLL=1` 且**没有启用访问码强制**（`PH_REQUIRE_ACCESS_CODE` 未开）时工作。
  用访问码的批次，身份由访问码决定，这里不参与。
- 编号按批次顺序分配（`A001`、`A002`…），只认浏览器 Cookie（`ph_subject`，HttpOnly，
  随机令牌，库里只存 SHA-256）。**不按来源地址找回、不按来源地址合并**：多人共用校园网
  出口时不会串号；代价是换浏览器、清 Cookie、重装系统后会拿到新编号（这是可接受的，
  不承诺跨浏览器识别同一个人）。
- 编号是**匿名会话**，不是真人身份：会话数不等于参与人数；一台电脑多人轮流用会共用同一个编号。
- `client_ip` 只记录用于本地排障（如某个会话请求异常），**不参与身份判定**，
  也不出现在学生端。
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timezone

from fastapi import Request, Response

from app import db

COOKIE_NAME = "ph_subject"
COOKIE_MAX_AGE = 60 * 60 * 24 * 120          # 120 天，覆盖一个实验周期
ID_PREFIX = "A"

SCHEMA = """
CREATE TABLE IF NOT EXISTS enrollment (
    participant_id TEXT PRIMARY KEY,
    token_hash     TEXT NOT NULL UNIQUE,
    batch          TEXT NOT NULL DEFAULT '',
    created_ts     TEXT NOT NULL,
    last_seen_ts   TEXT NOT NULL DEFAULT '',
    user_agent     TEXT NOT NULL DEFAULT '',
    client_ip      TEXT NOT NULL DEFAULT ''
);
"""


def enabled() -> bool:
    return (os.environ.get("PH_AUTO_ENROLL") or "").strip() == "1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _client_ip(request: Request) -> str:
    host = request.client.host if request.client else ""
    return (host or "").strip()[:64]


def _connect() -> sqlite3.Connection:
    conn = db.connect()
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(enrollment)")}
    if "client_ip" not in columns:
        conn.execute("ALTER TABLE enrollment ADD COLUMN client_ip TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_enrollment_ip ON enrollment(batch, client_ip)")
    conn.commit()
    return conn


def _batch() -> str:
    return (os.environ.get("PH_EXPERIMENT_BATCH") or "").strip()


def _next_id(conn: sqlite3.Connection, batch: str) -> str:
    rows = conn.execute("SELECT participant_id FROM enrollment WHERE batch = ?", (batch,)).fetchall()
    biggest = 0
    for row in rows:
        pid = row["participant_id"]
        if pid.startswith(ID_PREFIX) and pid[len(ID_PREFIX):].isdigit():
            biggest = max(biggest, int(pid[len(ID_PREFIX):]))
    return f"{ID_PREFIX}{biggest + 1:03d}"


def _by_token(conn: sqlite3.Connection, token: str, batch: str):
    if not token:
        return None
    row = conn.execute(
        "SELECT participant_id, batch FROM enrollment WHERE token_hash = ?", (_hash(token),)
    ).fetchone()
    if row and row["batch"] == batch:
        return row
    return None


def _touch(conn: sqlite3.Connection, participant_id: str) -> None:
    conn.execute("UPDATE enrollment SET last_seen_ts = ? WHERE participant_id = ?",
                 (_now(), participant_id))
    conn.commit()


def resolve(request: Request) -> str | None:
    """返回这台浏览器的会话编号：只认 Cookie，没有就返回 None（不回退来源地址）。"""
    batch = _batch()
    conn = _connect()
    try:
        row = _by_token(conn, request.cookies.get(COOKIE_NAME, ""), batch)
        if row is None:
            return None
        _touch(conn, row["participant_id"])
        return row["participant_id"]
    finally:
        conn.close()


def enroll(request: Request, response: Response) -> dict:
    """登记这台浏览器：Cookie 有效则沿用，否则分配一个新编号（不按来源地址合并）。"""
    batch = _batch()
    ip = _client_ip(request)
    conn = _connect()
    try:
        row = _by_token(conn, request.cookies.get(COOKIE_NAME, ""), batch)
        if row is not None:
            token = secrets.token_hex(32)
            conn.execute(
                "UPDATE enrollment SET token_hash = ?, last_seen_ts = ? WHERE participant_id = ?",
                (_hash(token), _now(), row["participant_id"]),
            )
            conn.commit()
            _set_cookie(response, token)
            return {"participant_id": row["participant_id"], "created": False,
                    "batch": batch, "bound_by": "cookie"}

        participant_id = _next_id(conn, batch)
        token = secrets.token_hex(32)
        now = _now()
        conn.execute(
            "INSERT INTO enrollment(participant_id, token_hash, batch, created_ts, last_seen_ts, user_agent, client_ip) "
            "VALUES (?,?,?,?,?,?,?)",
            (participant_id, _hash(token), batch, now, now,
             (request.headers.get("user-agent") or "")[:200], ip),
        )
        conn.commit()
        _set_cookie(response, token)
        return {"participant_id": participant_id, "created": True, "batch": batch,
                "bound_by": "new"}
    finally:
        conn.close()


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE,
                        httponly=True, samesite="lax", path="/")


def list_enrollments() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT participant_id, batch, created_ts, last_seen_ts, client_ip FROM enrollment "
            "ORDER BY participant_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
