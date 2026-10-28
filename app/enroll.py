"""新电脑自动登记：服务端分配固定被试编号，并用 HttpOnly Cookie 记住这台电脑。

设计边界（写清楚，避免误解）：

- 只在 `PH_AUTO_ENROLL=1` 且**没有启用访问码强制**（`PH_REQUIRE_ACCESS_CODE` 未开）时工作。
  用访问码的批次，身份由访问码决定，这里不参与。
- 编号按批次顺序分配（`A001`、`A002`…），一个浏览器（Cookie）对应一个编号；
  换电脑/换浏览器/清 Cookie 会拿到新编号，同一台电脑不同人轮流用会共用同一个编号。
  这是"按设备"的登记，不是"按人"的生物识别——实验组织上仍需一人一机或每人一台设备。
- Cookie 是随机 32 字节令牌，数据库只存它的 SHA-256，**不存明文**；HttpOnly + SameSite=Lax。
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
    user_agent     TEXT NOT NULL DEFAULT ''
);
"""


def enabled() -> bool:
    return (os.environ.get("PH_AUTO_ENROLL") or "").strip() == "1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _connect() -> sqlite3.Connection:
    conn = db.connect()
    conn.executescript(SCHEMA)
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


def resolve(request: Request) -> str | None:
    """根据 Cookie 返回已登记的被试编号；无效/批次不符返回 None。"""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT participant_id, batch FROM enrollment WHERE token_hash = ?", (_hash(token),)
        ).fetchone()
        if not row or row["batch"] != _batch():
            return None
        conn.execute("UPDATE enrollment SET last_seen_ts = ? WHERE participant_id = ?",
                     (_now(), row["participant_id"]))
        conn.commit()
        return row["participant_id"]
    finally:
        conn.close()


def enroll(request: Request, response: Response) -> dict:
    """登记这台电脑：已有有效 Cookie 就沿用，否则分配一个新编号。"""
    token = request.cookies.get(COOKIE_NAME)
    conn = _connect()
    try:
        if token:
            row = conn.execute(
                "SELECT participant_id, batch FROM enrollment WHERE token_hash = ?", (_hash(token),)
            ).fetchone()
            if row and row["batch"] == _batch():
                conn.execute("UPDATE enrollment SET last_seen_ts = ? WHERE participant_id = ?",
                             (_now(), row["participant_id"]))
                conn.commit()
                _set_cookie(response, token)
                return {"participant_id": row["participant_id"], "created": False,
                        "batch": _batch()}
        participant_id = _next_id(conn, _batch())
        token = secrets.token_hex(32)
        now = _now()
        conn.execute(
            "INSERT INTO enrollment(participant_id, token_hash, batch, created_ts, last_seen_ts, user_agent) "
            "VALUES (?,?,?,?,?,?)",
            (participant_id, _hash(token), _batch(), now, now,
             (request.headers.get("user-agent") or "")[:200]),
        )
        conn.commit()
        _set_cookie(response, token)
        return {"participant_id": participant_id, "created": True, "batch": _batch()}
    finally:
        conn.close()


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE,
                        httponly=True, samesite="lax", path="/")


def list_enrollments() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT participant_id, batch, created_ts, last_seen_ts FROM enrollment "
            "ORDER BY participant_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
