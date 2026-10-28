"""本机管理台：令牌、来源限制、发题对 /api/questions 的影响。"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import admin, main


class _Req:
    def __init__(self, host: str = "127.0.0.1") -> None:
        self.client = SimpleNamespace(host=host)


@pytest.fixture()
def admin_token(tmp_path, monkeypatch):
    token_file = tmp_path / "admin_token.txt"
    token_file.write_text("tok-123456", encoding="utf-8")
    monkeypatch.setenv("PH_ADMIN_TOKEN_FILE", str(token_file))
    return "tok-123456"


def test_admin_disabled_without_token_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PH_ADMIN_TOKEN_FILE", str(tmp_path / "missing.txt"))
    with pytest.raises(HTTPException) as excinfo:
        admin.require_admin(_Req(), "whatever")
    assert excinfo.value.status_code == 404


def test_admin_rejects_wrong_token_and_remote_host(admin_token):
    with pytest.raises(HTTPException) as excinfo:
        admin.require_admin(_Req(), "wrong")
    assert excinfo.value.status_code == 403

    with pytest.raises(HTTPException) as excinfo:
        admin.require_admin(_Req("10.30.34.99"), admin_token)
    assert excinfo.value.status_code == 403


def test_admin_summary_and_submissions_are_readable(admin_token):
    assert admin.require_admin(_Req(), admin_token) is None
    summary = admin.api_summary(_Req(), admin_token)
    assert "submissions" in summary and summary["db_path"].endswith(".sqlite")
    rows = admin.api_submissions(_Req(), "", "", 20, admin_token)
    assert isinstance(rows, list)


def test_publish_filters_participant_question_list(admin_token):
    try:
        admin.api_publish(admin.PublishRequest(question_ids=["P001"], published=True),
                          _Req(), admin_token)
        ids = [q["id"] for q in main.api_questions(x_access_code=None)]
        assert ids == ["P001"], "发题后学生端只应看到已发布题目"
    finally:
        admin.api_publish(admin.PublishRequest(question_ids=["P001"], published=False),
                          _Req(), admin_token)
    # 全部下架 ≠ 恢复默认：表里仍有记录时列表为空，这是有意为之（避免"忘发题"时全量泄露）
    assert [q["id"] for q in main.api_questions(x_access_code=None)] == []


def test_enrollment_roster_and_csv_export(admin_token):
    """现场登记：编号表要带机器地址与提交/提示计数，并能导出 CSV。"""
    from app import enroll

    conn = enroll._connect()          # 确保 enrollment 表存在
    conn.close()

    rows = admin.enrollments_with_stats()
    assert isinstance(rows, list)
    for row in rows:
        assert {"participant_id", "client_ip", "submissions", "passes", "hints"} <= set(row)

    resp = admin.api_export("enrollments", _Req(), admin_token)
    assert resp.media_type.startswith("text/csv")
    assert "enrollments.csv" in resp.headers["content-disposition"]
