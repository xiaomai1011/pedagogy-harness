"""错误记忆：累计、跨会话读取、预防性提示的门槛与隔离。

用 conftest 的临时库（PH_DB），不碰真实数据。
"""

from app import db
from app.harness import error_classifier as ec
from app.harness import error_memory as em

_TS = "2026-10-17T10:00:00+08:00"


def _cls(error_class="边界", kp="边界条件"):
    return ec.Classification(error_class=error_class, knowledge_point=kp, source="rule")


def test_single_occurrence_is_not_enough_for_a_hint():
    db.init_db()
    em.remember("T_MEM_1", _cls(), _TS)
    assert em.preventive_hint("T_MEM_1") == ""


def test_second_occurrence_produces_preventive_hint():
    db.init_db()
    em.remember("T_MEM_2", _cls(), _TS)
    em.remember("T_MEM_2", _cls(), "2026-10-17T10:01:00+08:00")
    hint = em.preventive_hint("T_MEM_2")
    assert "边界类错误" in hint
    assert "边界条件" in hint
    assert "2 次" in hint


def test_unclassified_is_not_remembered():
    """「未分类」不记账，否则记忆表会堆满无意义的行。"""
    db.init_db()
    em.remember("T_MEM_3", ec.Classification(error_class="未分类", knowledge_point="", source="none"), _TS)
    assert db.get_error_memory("T_MEM_3") == []


def test_memory_is_isolated_per_participant():
    db.init_db()
    em.remember("T_MEM_4", _cls(), _TS)
    em.remember("T_MEM_4", _cls(), _TS)
    assert em.preventive_hint("T_MEM_5") == ""


def test_hint_lists_at_most_two_most_frequent():
    db.init_db()
    for error_class in ("语法", "类型", "边界"):
        for _ in range(2 if error_class != "语法" else 3):
            em.remember("T_MEM_6", _cls(error_class=error_class), _TS)
    rows = db.get_error_memory("T_MEM_6")
    assert len(rows) == 3
    hint = em.preventive_hint("T_MEM_6")
    assert "语法类错误" in hint
    assert "环境类错误" not in hint
