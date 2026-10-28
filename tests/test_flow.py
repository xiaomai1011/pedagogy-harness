"""匿名会话的阶段流程：前测 → 练习 → 后测，顺序锁定。"""

from __future__ import annotations

from app import flow


def test_new_session_starts_at_pretest():
    assert flow.current("FLOW-A") == "pretest"
    assert flow.allows("FLOW-A", "pretest") is True
    assert flow.allows("FLOW-A", "practice") is False


def test_advance_moves_in_order_and_stops_at_done():
    assert flow.advance("FLOW-B")["phase"] == "practice"
    assert flow.current("FLOW-B") == "practice"
    assert flow.advance("FLOW-B")["phase"] == "posttest"
    assert flow.current("FLOW-B") == "posttest"
    done = flow.advance("FLOW-B")
    assert done["phase"] == "done" and done["changed"] is True
    again = flow.advance("FLOW-B")
    assert again["phase"] == "done" and again["changed"] is False


def test_earlier_phase_is_locked_after_advance():
    flow.advance("FLOW-C")  # pretest → practice
    assert flow.allows("FLOW-C", "pretest") is False
    assert flow.allows("FLOW-C", "practice") is True
    assert flow.allows("FLOW-C", "posttest") is False


def test_advance_is_idempotent_for_same_session_without_crossing():
    # 两个会话互不影响（保持各自阶段）
    flow.advance("FLOW-D")
    flow.advance("FLOW-D")
    assert flow.current("FLOW-D") == "posttest"
    assert flow.current("FLOW-E") == "pretest"
