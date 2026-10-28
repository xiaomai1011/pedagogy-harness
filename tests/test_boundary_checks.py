"""四项边界核验（2026-10-28）：发布名单、模型就绪、整任务预算。

这些测试只覆盖可自动化的部分；浏览器侧 429 恢复另见 `test_web_busy_recovery.py`。
"""

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import admin, db, main
from app.harness import vm_agent_adapter
from app.harness import vm_agent_adapter
from app.harness.vm_agent_adapter import VmAgentAdapter
from app.main import app


def _count(table: str, participant_id: str) -> int:
    with db.connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE participant_id = ?", (participant_id,)
        ).fetchone()
    return int(row[0])


@pytest.fixture()
def only_p001():
    def _clear():
        admin.published_ids()  # 确保发布表已建（首次访问时惰性建表）
        with db.connect() as conn:
            conn.execute("DELETE FROM question_publish")
            conn.commit()

    _clear()
    admin.set_published(["P001"], True)
    try:
        yield
    finally:
        _clear()


def test_unpublished_question_rejected_on_every_entry(only_p001):
    client = TestClient(app)
    # 列表里看不到
    listing = client.get("/api/questions")
    assert listing.status_code == 200
    assert [q["id"] for q in listing.json()] == ["P001"]
    # 但知道题号也不能读题、提交、请求提示
    assert client.get("/api/questions/P002").status_code == 403
    submit = client.post("/api/submit", json={
        "participant_id": "T-PUB", "question_id": "P002", "code": "print(1)"})
    assert submit.status_code == 403
    assert submit.json()["detail"]["status"] == "question_not_published"
    hint = client.post("/api/hint", json={
        "participant_id": "T-PUB", "question_id": "P002", "code": "print(1)"})
    assert hint.status_code == 403
    assert hint.json()["detail"]["status"] == "question_not_published"
    # 被拒绝的请求不写入学习数据
    assert _count("submission_log", "T-PUB") == 0
    assert _count("event_log", "T-PUB") == 0
    # 已发布题目仍然可用
    assert client.get("/api/questions/P001").status_code == 200


def test_hint_without_model_in_experiment_mode_is_explicit(monkeypatch):
    monkeypatch.setenv("PH_EXPERIMENT_MODE", "1")
    for key in ("PH_LLM_BASE_URL", "PH_LLM_API_KEY", "PH_LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    client = TestClient(app)
    resp = client.post("/api/hint", json={
        "participant_id": "T-MODEL", "question_id": "P001", "code": "print(1)"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["status"] == "model_not_configured"
    assert "[桩" not in json.dumps(resp.json(), ensure_ascii=False)
    assert _count("event_log", "T-MODEL") == 0


def test_dev_mode_stub_is_labelled_not_recorded_as_llm(monkeypatch):
    monkeypatch.delenv("PH_EXPERIMENT_MODE", raising=False)
    monkeypatch.delenv("PH_REQUIRE_ACCESS_CODE", raising=False)
    for key in ("PH_LLM_BASE_URL", "PH_LLM_API_KEY", "PH_LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    client = TestClient(app)
    resp = client.post("/api/hint", json={
        "participant_id": "T-STUB", "question_id": "P001", "code": "print(1)"})
    assert resp.status_code == 200
    assert resp.json()["message"].lstrip().startswith("[桩·大模型]")
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT hint_source FROM event_log WHERE participant_id = ? AND event_type = 'hint'",
            ("T-STUB",),
        ).fetchall()
    assert rows and all(r[0] == "stub" for r in rows)


def test_status_reports_model_state(monkeypatch):
    monkeypatch.setenv("PH_LLM_MODEL", "x")
    monkeypatch.setenv("PH_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("PH_LLM_API_KEY", "k")
    assert TestClient(app).get("/api/status").json()["model"] == "configured"


def test_agent_budget_covers_multi_case_runs():
    adapter = VmAgentAdapter(base_url="http://127.0.0.1:9", token="x")
    six = {"test_cases": [{"input": "1", "expected": "1"}] * 6, "timeout": 5.0}
    assert adapter._budget_timeout(six) >= 45.0 + 5.0
    single = {"test_cases": [{"input": "1", "expected": "1"}], "timeout": 5.0}
    assert adapter._budget_timeout(single) >= adapter.request_timeout


def test_agent_timeout_is_channel_not_student_error(monkeypatch):
    def boom(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(vm_agent_adapter.urllib.request, "urlopen", boom)
    adapter = VmAgentAdapter(base_url="http://127.0.0.1:9", token="x")
    with pytest.raises(Exception) as exc:
        adapter._request("POST", "/v1/execute", {}, 1.0)
    assert getattr(exc.value, "detail_code", "") == "channel_unavailable"


def test_hint_busy_path_restores_frontend_state():
    """前端 429 分支必须回到可操作状态（真 bug：keepBusy 让按钮永久禁用）。

    这是静态回归护栏：真正点一遍浏览器由负责人完成，见 G16 报告。
    """
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "keepBusy" not in html
    assert '} finally { setBusy(false, "hint"); }' in html
    assert "不自动重试" in html


def test_after_busy_release_same_request_succeeds():
    """忙碌拒绝之后，服务空闲时同一请求应能正常完成（服务端侧证据）。"""
    main._SINGLE_LANE.acquire()
    try:
        with pytest.raises(HTTPException) as ei:
            main.api_hint(main.HintRequest(participant_id="T-BUSY", question_id="P001",
                                           code="print(1)"))
        assert ei.value.status_code == 429
        assert ei.value.detail["status"] == "busy"
    finally:
        main._SINGLE_LANE.release()
    out = main.api_hint(main.HintRequest(participant_id="T-BUSY", question_id="P001",
                                         code="print(1)"))
    assert out["message"]
    assert not main._SINGLE_LANE.locked()
