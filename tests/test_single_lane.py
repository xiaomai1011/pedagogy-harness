"""单路执行闸门：并发重入必须立刻 429，不排队、不执行第二次判题。"""

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
