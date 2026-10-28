"""实验访问码：身份由服务端凭证确定，阶段由服务端约束（2026-10-28）。"""

import pytest

from app import access


@pytest.fixture()
def secret(monkeypatch):
    monkeypatch.setenv("PH_ACCESS_SECRET", "unit-test-secret-0123456789")
    return "unit-test-secret-0123456789"


def test_issue_and_verify_roundtrip(secret):
    code = access.issue_code("S001", batch="B1", phases=("practice",), ttl_seconds=600, now=1000)
    claims = access.verify_code(code, now=1001)
    assert claims.participant_id == "S001"
    assert claims.batch == "B1"
    assert claims.allows_phase("practice")
    assert not claims.allows_phase("pretest")


def test_expired_code_rejected(secret):
    code = access.issue_code("S001", batch="B1", ttl_seconds=10, now=1000)
    with pytest.raises(access.AccessError) as exc:
        access.verify_code(code, now=1000 + 10)
    assert exc.value.code == "expired_code"


def test_tampered_code_rejected(secret):
    code = access.issue_code("S001", batch="B1", ttl_seconds=600, now=1000)
    body = code.split(".")[1]
    forged = f"v1.{body}.{'A' * 43}"
    with pytest.raises(access.AccessError) as exc:
        access.verify_code(forged, now=1001)
    assert exc.value.code in ("bad_signature", "malformed_code")


def test_missing_code_rejected(secret):
    with pytest.raises(access.AccessError) as exc:
        access.verify_code(None)
    assert exc.value.code == "missing_code"


def test_identity_mismatch_rejected(secret):
    code = access.issue_code("S001", batch="B1", ttl_seconds=600, now=1000)
    claims = access.verify_code(code, now=1001)
    with pytest.raises(access.AccessError) as exc:
        access.resolve_participant("S999", claims)
    assert exc.value.code == "identity_mismatch"
    assert access.resolve_participant("", claims) == "S001"


def test_secret_missing_is_not_configured(monkeypatch):
    monkeypatch.delenv("PH_ACCESS_SECRET", raising=False)
    monkeypatch.delenv("PH_ACCESS_SECRET_FILE", raising=False)
    with pytest.raises(access.AccessNotConfigured):
        access.issue_code("S001", batch="B1")


def test_api_submit_requires_code_when_enforced(monkeypatch, secret):
    """强制模式下：无码 403、伪造 403，且都发生在判题之前。"""
    from fastapi import HTTPException

    from app import main
    from app.harness import pipeline as pipeline_mod

    called = {"judge": 0}
    monkeypatch.setattr(pipeline_mod, "judge", lambda *a, **k: called.__setitem__("judge", called["judge"] + 1))
    monkeypatch.setenv("PH_REQUIRE_ACCESS_CODE", "1")

    req = main.SubmitRequest(participant_id="S001", question_id="P001", code="print(1)")
    with pytest.raises(HTTPException) as exc:
        main.api_submit(req, x_access_code=None)
    assert exc.value.status_code == 403
    assert called["judge"] == 0

    with pytest.raises(HTTPException) as exc:
        main.api_submit(req, x_access_code="v1.abc.def")
    assert exc.value.status_code == 403
    assert called["judge"] == 0


def test_api_submit_rejects_phase_not_allowed(monkeypatch, secret):
    """访问码只允许 practice 时，拿前测题提交要被服务端拒绝（不能靠客户端传 phase 绕过）。"""
    from fastapi import HTTPException

    from app import main

    monkeypatch.setenv("PH_REQUIRE_ACCESS_CODE", "1")
    code = access.issue_code("S001", batch="B1", phases=("practice",), ttl_seconds=600)
    req = main.SubmitRequest(participant_id="S001", question_id="S001", code="print(1)")
    with pytest.raises(HTTPException) as exc:
        main.api_submit(req, x_access_code=code)
    assert exc.value.status_code == 403
    assert exc.value.detail["status"] == "phase_not_allowed"


def test_api_submit_uses_claim_identity(monkeypatch, secret):
    """请求体里的 participant_id 与访问码不一致 → 拒绝；一致时以访问码为准。"""
    from fastapi import HTTPException

    from app import main

    seen = {}
    monkeypatch.setattr(main, "handle_submission", lambda **kw: seen.update(kw) or {"ok": True})
    monkeypatch.setenv("PH_REQUIRE_ACCESS_CODE", "1")
    code = access.issue_code("S001", batch="B1", phases=("practice",), ttl_seconds=600)

    with pytest.raises(HTTPException):
        main.api_submit(main.SubmitRequest(participant_id="S002", question_id="P001", code=""),
                        x_access_code=code)

    main.api_submit(main.SubmitRequest(participant_id="S001", question_id="P001", code=""),
                    x_access_code=code)
    assert seen["participant_id"] == "S001"

def test_question_list_filtered_by_claimed_phases(monkeypatch, secret):
    from app import main

    code = access.issue_code("S001", batch="B1", phases=("practice",), ttl_seconds=600)
    rows = main.api_questions(x_access_code=code)
    assert rows and all(r["phase"] == "practice" for r in rows)

    with pytest.raises(Exception):
        main.api_question("S001", x_access_code=code)

def test_wrong_batch_rejected(monkeypatch, secret):
    """凭证里的 batch 必须与服务端当前批次比对，不能只签着好看。"""
    from fastapi import HTTPException

    from app import main

    monkeypatch.setenv("PH_REQUIRE_ACCESS_CODE", "1")
    monkeypatch.setenv("PH_EXPERIMENT_BATCH", "B-CURRENT")
    code = access.issue_code("S001", batch="B-OLD", phases=("practice",), ttl_seconds=600)
    with pytest.raises(HTTPException) as exc:
        main.api_submit(main.SubmitRequest(participant_id="S001", question_id="P001", code=""),
                        x_access_code=code)
    assert exc.value.status_code == 403
    assert exc.value.detail["status"] == "batch_mismatch"
