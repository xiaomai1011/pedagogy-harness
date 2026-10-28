"""单路执行闸门：并发重入必须立刻 429，不排队、不执行第二次判题。

注意：进程内锁只保护**单进程**。实验服务必须以单 worker 启动（见 G13 报告与启动器），
且不允许再起第二个实例；本文件只验证进程内行为。
"""

import pytest
from fastapi import HTTPException

from app import main


def test_second_request_is_rejected_while_first_is_running():
    main._SINGLE_LANE.acquire()
    try:
        with pytest.raises(HTTPException) as ei:
            main.api_submit(main.SubmitRequest(participant_id="LANE-1",
                                               question_id="P001", code="print(1)"))
        assert ei.value.status_code == 429
        assert ei.value.detail["status"] == "busy"

        with pytest.raises(HTTPException) as eh:
            main.api_hint(main.HintRequest(participant_id="LANE-1",
                                           question_id="P001", code="print(1)"))
        assert eh.value.status_code == 429
    finally:
        main._SINGLE_LANE.release()


def test_lock_released_after_request():
    out = main.api_submit(main.SubmitRequest(participant_id="LANE-2",
                                             question_id="P001", code="print(1)"))
    assert out["status"] in ("pass", "fail", "crash", "timeout", "error")
    assert not main._SINGLE_LANE.locked()


def test_lock_released_on_exception(monkeypatch):
    """异常路径必须释放锁，否则一次崩溃就把整站锁死。"""
    def boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "handle_submission", boom)
    with pytest.raises(RuntimeError):
        main.api_submit(main.SubmitRequest(participant_id="LANE-3",
                                           question_id="P001", code="print(1)"))
    assert not main._SINGLE_LANE.locked()


def test_busy_request_does_not_execute(monkeypatch):
    """忙碌请求在拿到锁之前就该被拒，不能触发判题/模型调用。"""
    calls = {"judge": 0, "handle": 0}

    def counted(**kwargs):
        calls["handle"] += 1
        return {"status": "pass"}

    monkeypatch.setattr(main, "handle_submission", counted)
    main._SINGLE_LANE.acquire()
    try:
        with pytest.raises(HTTPException) as ei:
            main.api_submit(main.SubmitRequest(participant_id="LANE-4",
                                               question_id="P001", code="print(1)"))
        assert ei.value.status_code == 429
    finally:
        main._SINGLE_LANE.release()
    assert calls["handle"] == 0
