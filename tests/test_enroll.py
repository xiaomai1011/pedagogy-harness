"""自动登记（新电脑分配编号）与提交行保存代码原文。"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db, enroll, main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PH_AUTO_ENROLL", "1")
    monkeypatch.delenv("PH_REQUIRE_ACCESS_CODE", raising=False)
    monkeypatch.setenv("PH_EXPERIMENT_BATCH", "ENROLL-TEST")
    monkeypatch.setenv("PH_DB", str(tmp_path / "enroll.sqlite"))
    return TestClient(main.app)


def test_enroll_disabled_returns_404(tmp_path, monkeypatch):
    monkeypatch.delenv("PH_AUTO_ENROLL", raising=False)
    monkeypatch.setenv("PH_DB", str(tmp_path / "off.sqlite"))
    client = TestClient(main.app)
    assert client.post("/api/enroll").status_code == 404


def test_enroll_assigns_stable_id_per_browser(client):
    first = client.post("/api/enroll")
    assert first.status_code == 200
    pid = first.json()["participant_id"]
    assert pid.startswith("A") and first.json()["created"] is True

    again = client.post("/api/enroll")
    assert again.json()["participant_id"] == pid          # 同一浏览器沿用同一个编号
    assert again.json()["created"] is False

    other = TestClient(main.app)                          # 另一台电脑（没有 Cookie）
    assert other.post("/api/enroll").json()["participant_id"] != pid


def test_enroll_rejected_when_access_code_required(tmp_path, monkeypatch):
    monkeypatch.setenv("PH_AUTO_ENROLL", "1")
    monkeypatch.setenv("PH_REQUIRE_ACCESS_CODE", "1")
    monkeypatch.setenv("PH_DB", str(tmp_path / "code.sqlite"))
    client = TestClient(main.app)
    resp = client.post("/api/enroll")
    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "access_code_mode"


def test_cookie_identity_wins_over_body_participant(client):
    pid = client.post("/api/enroll").json()["participant_id"]
    # 用别的编号提交：被拒（不能顶替）
    bad = client.post("/api/submit", json={"participant_id": "S999", "question_id": "P001",
                                           "code": "print(1)", "group": "exp"})
    assert bad.status_code == 403
    assert bad.json()["detail"]["status"] == "identity_mismatch"


def test_submission_row_keeps_code_text(tmp_path, monkeypatch):
    monkeypatch.setenv("PH_DB", str(tmp_path / "code_store.sqlite"))
    payload = dict(ts="2026-10-28T00:00:00+00:00", participant_id="A001", group_id="exp",
                   question_id="P001", phase="practice", submit_seq=1, status="pass",
                   error_type="none", error_class="", error_kp="", hint_level=0,
                   hint_unlocked_level=0, hint_requested=0, pass_status=1, elapsed_ms=12,
                   code="print('hi')\n")
    conn = db.connect()
    try:
        sid = db.log_submission_conn(conn, **payload)
        conn.commit()
        row = conn.execute("SELECT code FROM submission_log WHERE id=?", (sid,)).fetchone()
    finally:
        conn.close()
    assert row["code"] == "print('hi')\n"
