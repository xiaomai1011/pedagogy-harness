"""教师/负责人用的**本机管理台**（只读监控 + 发题 + 导出 + 发码）。

设计约束（与实验边界一致）：
- 只在**本机**可用：除令牌外再校验请求来源是 127.0.0.1/::1，避免管理面出现在校园网。
- 令牌从 `PH_ADMIN_TOKEN_FILE` 指向的文件读（0600，不进仓库、不进日志、不进导出）；
  未配置令牌时整组接口 404，等于关闭。
- 只做三件事：看数据、决定当前可见题目、导出与发码。**不提供改分数、删记录、伪造通过的接口。**
- 学生可见题目 = 已发布 ∩ 访问码允许的阶段；发布表为空时退化为"按阶段全可见"（本地联调）。
"""

from __future__ import annotations

import csv
import hmac
import io
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import access, db
from app.questions import list_questions

router = APIRouter(prefix="/api/admin", tags=["admin"])

PUBLISH_SCHEMA = """
CREATE TABLE IF NOT EXISTS question_publish (
    question_id TEXT PRIMARY KEY,
    published   INTEGER NOT NULL DEFAULT 1,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    updated_ts  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = db.connect()
    conn.execute(PUBLISH_SCHEMA)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(question_publish)").fetchall()}
    if "sort_order" not in cols:
        conn.execute("ALTER TABLE question_publish ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    return conn


def publish_order() -> dict[str, int]:
    """题目的展示顺序（`sort_order`，0 表示未指定，按 id 兜底）。"""
    conn = _connect()
    try:
        rows = conn.execute("SELECT question_id, sort_order FROM question_publish WHERE published = 1").fetchall()
        return {r[0]: int(r[1] or 0) for r in rows}
    finally:
        conn.close()


def set_order(question_ids: list[str]) -> int:
    """按给定顺序写入 1..n（已发布的题才参与排序）。"""
    conn = _connect()
    try:
        for idx, qid in enumerate(question_ids, start=1):
            conn.execute(
                "INSERT INTO question_publish(question_id, published, sort_order, updated_ts) VALUES(?,1,?,?) "
                "ON CONFLICT(question_id) DO UPDATE SET sort_order=excluded.sort_order, updated_ts=excluded.updated_ts",
                (qid, idx, _now()),
            )
        conn.commit()
        return len(question_ids)
    finally:
        conn.close()


def _token_path() -> Path | None:
    raw = (os.environ.get("PH_ADMIN_TOKEN_FILE") or "").strip()
    return Path(raw) if raw else None


def _expected_token() -> str:
    path = _token_path()
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def enabled() -> bool:
    """管理台是否启用（配置了非空的令牌文件）。页面路由与文档用它，不必碰私有函数。"""
    return bool(_expected_token())


def require_admin(request: Request, x_admin_token: str = Header(default=None)) -> None:
    expected = _expected_token()
    if not expected:
        raise HTTPException(status_code=404, detail={"status": "admin_disabled"})
    host = (request.client.host if request.client else "") or ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail={"status": "admin_local_only"})
    given = x_admin_token if isinstance(x_admin_token, str) else ""
    if not given or not hmac.compare_digest(given, expected):
        raise HTTPException(status_code=403, detail={"status": "admin_denied"})


# ---------- 发布状态 ----------

def published_ids() -> set[str] | None:
    """已发布题目集合。

    返回 ``None`` 表示"发布表从未用过"（本地联调阶段，按阶段全可见）；
    返回空集合表示"发布表已有记录，但当前没有一道题处于发布状态"——
    这时学生端必须看不到任何题，而不是悄悄回退成全量可见。
    """
    conn = _connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM question_publish").fetchone()[0]
        if not total:
            return None
        rows = conn.execute("SELECT question_id FROM question_publish WHERE published = 1").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def publish_map() -> dict[str, bool]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT question_id, published FROM question_publish").fetchall()
        return {r[0]: bool(r[1]) for r in rows}
    finally:
        conn.close()


def set_published(question_ids: list[str], published: bool) -> int:
    conn = _connect()
    try:
        for qid in question_ids:
            conn.execute(
                "INSERT INTO question_publish(question_id, published, updated_ts) VALUES(?,?,?) "
                "ON CONFLICT(question_id) DO UPDATE SET published=excluded.published, "
                "updated_ts=excluded.updated_ts",
                (qid, 1 if published else 0, _now()),
            )
        conn.commit()
        return len(question_ids)
    finally:
        conn.close()


# ---------- 查询 ----------

def _rows(sql: str, params: tuple = ()) -> list[dict]:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def summary() -> dict:
    one = _rows(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT participant_id) AS people, "
        "COALESCE(SUM(pass_status),0) AS passes, MAX(ts) AS last_ts FROM submission_log"
    )[0]
    events = _rows("SELECT COUNT(*) AS n FROM event_log")[0]["n"]
    hints = _rows("SELECT COUNT(*) AS n FROM event_log WHERE event_type='hint'")[0]["n"]
    return {
        "db_path": os.path.abspath(db.DB_PATH),
        "batch": (os.environ.get("PH_EXPERIMENT_BATCH") or "").strip(),
        "participants": one["people"],
        "submissions": one["n"],
        "passes": one["passes"],
        "events": events,
        "hint_events": hints,
        "last_submission_ts": one["last_ts"],
        "access_required": access.access_required(),
    }


def submissions(participant_id: str = "", question_id: str = "", limit: int = 200) -> list[dict]:
    where, params = [], []
    if participant_id:
        where.append("participant_id = ?")
        params.append(participant_id)
    if question_id:
        where.append("question_id = ?")
        params.append(question_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(max(1, min(int(limit), 1000)))
    return _rows(
        "SELECT id, ts, participant_id, question_id, phase, submit_seq, status, error_type, "
        "error_class, error_kp, hint_level, hint_requested, pass_status, elapsed_ms, code "
        f"FROM submission_log {clause} ORDER BY id DESC LIMIT ?",
        tuple(params),
    )


def board() -> list[dict]:
    """参与者 × 题目 的进度看板：做题数、通过数、最高提示等级、最近活动。"""
    return _rows(
        "SELECT participant_id, question_id, COUNT(*) AS attempts, "
        "SUM(pass_status) AS passes, MAX(hint_level) AS max_hint, MAX(ts) AS last_ts "
        "FROM submission_log GROUP BY participant_id, question_id "
        "ORDER BY participant_id, question_id"
    )


def enrollments_with_stats() -> list[dict]:
    """每台已登记机器：编号、来源地址、首次/最近、提交数、通过数、提示数。

    现场登记表就靠这张表核对「哪台电脑是哪个编号」，导出后可留档。
    """
    return _rows(
        "SELECT e.participant_id, e.client_ip, e.batch, e.created_ts, e.last_seen_ts, "
        "  (SELECT COUNT(*) FROM submission_log s WHERE s.participant_id = e.participant_id) AS submissions, "
        "  (SELECT COALESCE(SUM(pass_status), 0) FROM submission_log s WHERE s.participant_id = e.participant_id) AS passes, "
        "  (SELECT COUNT(*) FROM event_log v WHERE v.participant_id = e.participant_id AND v.event_type = 'hint') AS hints, "
        "  (SELECT MAX(s.ts) FROM submission_log s WHERE s.participant_id = e.participant_id) AS last_submission_ts "
        "FROM enrollment e ORDER BY e.participant_id"
    )


def events(participant_id: str = "", limit: int = 200) -> list[dict]:
    params: list = []
    clause = ""
    if participant_id:
        clause = "WHERE participant_id = ?"
        params.append(participant_id)
    params.append(max(1, min(int(limit), 1000)))
    return _rows(
        "SELECT event_id, event_type, request_id, participant_id, question_id, phase, entry, "
        "ts_start, elapsed_ms, model_ms, status, progress, hint_level, hint_source, judge_status, "
        "reason_code FROM event_log " + clause + " ORDER BY event_id DESC LIMIT ?",
        tuple(params),
    )


def questions_overview() -> list[dict]:
    published = publish_map()
    items = list_questions()
    return [
        {
            "id": q["id"], "title": q["title"], "phase": q.get("phase"),
            "difficulty": q.get("difficulty"),
            "published": published.get(q["id"], True) if published else True,
            "publish_managed": bool(published),
        }
        for q in items
    ]


SUBMISSION_CSV_FIELDS = ["id", "ts", "participant_id", "question_id", "phase", "submit_seq", "status",
                         "error_type", "error_class", "error_kp", "hint_level", "hint_requested",
                         "pass_status", "elapsed_ms", "code"]
EVENT_CSV_FIELDS = ["event_id", "event_type", "request_id", "participant_id", "question_id", "phase",
                    "entry", "ts_start", "elapsed_ms", "model_ms", "status", "progress", "hint_level",
                    "hint_source", "judge_status", "reason_code"]
BOARD_CSV_FIELDS = ["participant_id", "question_id", "attempts", "passes", "max_hint", "last_ts"]
ENROLLMENT_CSV_FIELDS = ["participant_id", "client_ip", "batch", "created_ts", "last_seen_ts",
                         "submissions", "passes", "hints", "last_submission_ts"]


def _csv_response(rows: list[dict], filename: str, fieldnames: list[str]) -> StreamingResponse:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()          # 空表也要有表头，否则导出的文件无法判断列含义
    writer.writerows(rows)
    data = buf.getvalue().encode("utf-8-sig")   # 带 BOM，Excel 直接打开不乱码
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------- 路由 ----------

@router.get("/summary")
def api_summary(request: Request, x_admin_token: str = Header(default=None)) -> dict:
    require_admin(request, x_admin_token)
    return summary()


@router.get("/board")
def api_board(request: Request, x_admin_token: str = Header(default=None)) -> list[dict]:
    require_admin(request, x_admin_token)
    return board()


@router.get("/submissions")
def api_submissions(request: Request, participant_id: str = "", question_id: str = "",
                    limit: int = 200, x_admin_token: str = Header(default=None)) -> list[dict]:
    require_admin(request, x_admin_token)
    return submissions(participant_id, question_id, limit)


@router.get("/events")
def api_events(request: Request, participant_id: str = "", limit: int = 200,
               x_admin_token: str = Header(default=None)) -> list[dict]:
    require_admin(request, x_admin_token)
    return events(participant_id, limit)


@router.get("/questions")
def api_questions(request: Request, x_admin_token: str = Header(default=None)) -> list[dict]:
    require_admin(request, x_admin_token)
    return questions_overview()


class PublishRequest(BaseModel):
    question_ids: list[str]
    published: bool = True


@router.post("/publish")
def api_publish(req: PublishRequest, request: Request,
                x_admin_token: str = Header(default=None)) -> dict:
    require_admin(request, x_admin_token)
    known = {q["id"] for q in list_questions()}
    unknown = [q for q in req.question_ids if q not in known]
    if unknown:
        raise HTTPException(status_code=400, detail={"status": "unknown_question", "ids": unknown})
    n = set_published(req.question_ids, req.published)
    return {"updated": n, "published": req.published}


class CodeRequest(BaseModel):
    participant_id: str
    phases: list[str]
    hours: int = 8


@router.post("/access-code")
def api_access_code(req: CodeRequest, request: Request,
                    x_admin_token: str = Header(default=None)) -> dict:
    require_admin(request, x_admin_token)
    batch = access.current_batch()
    phases = tuple(p for p in req.phases if p in ("pretest", "practice", "posttest"))
    if not phases:
        raise HTTPException(status_code=400, detail={"status": "bad_phases"})
    hours = max(1, min(int(req.hours), 72))
    try:
        code = access.issue_code(req.participant_id, batch=batch, phases=phases,
                                 ttl_seconds=hours * 3600)
    except access.AccessNotConfigured:
        raise HTTPException(status_code=503, detail={"status": "access_not_configured"})
    except access.AccessError:
        raise HTTPException(status_code=400, detail={"status": "bad_participant"})
    return {"participant_id": req.participant_id, "batch": batch, "phases": list(phases),
            "hours": hours, "code": code,
            "note": "无状态签名码不能即时撤销；阶段结束或应急时轮换密钥使旧码统一失效。"}


@router.get("/enrollments")
def api_enrollments(request: Request, x_admin_token: str = Header(default=None)) -> list[dict]:
    """自动登记过的电脑：编号、来源地址、提交/通过/提示计数（现场登记对账用）。"""
    require_admin(request, x_admin_token)
    return enrollments_with_stats()


@router.get("/export/{what}.csv")
def api_export(what: str, request: Request, x_admin_token: str = Header(default=None)):
    require_admin(request, x_admin_token)
    if what == "submissions":
        return _csv_response(submissions(limit=1000), "submissions.csv", SUBMISSION_CSV_FIELDS)
    if what == "events":
        return _csv_response(events(limit=1000), "events.csv", EVENT_CSV_FIELDS)
    if what == "board":
        return _csv_response(board(), "board.csv", BOARD_CSV_FIELDS)
    if what == "enrollments":
        return _csv_response(enrollments_with_stats(), "enrollments.csv", ENROLLMENT_CSV_FIELDS)
    raise HTTPException(status_code=404, detail={"status": "unknown_export"})
