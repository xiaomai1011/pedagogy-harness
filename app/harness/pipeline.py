"""Harness 主干：一次提交的完整处理流程。

数据流（交接文档第一部分）：
  提交代码 → 判题 → Harness 约束层（读开关）→ 三组件 → 返回学生 → 写日志

契约 §5 的开关在此真正生效：关闭某组件时，其对应代码路径**不执行**。
"""

from datetime import datetime

from app import db
from app import llm as llm_mod
from app.harness import error_classifier, error_memory, hint_ladder
from app.harness.config_switch import HarnessConfig
from app.judge import judge
from app.questions import load_question


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _recent_state(participant_id: str, question_id: str) -> tuple[int, int]:
    """返回 (该校验题上次的提示等级, 连续失败次数)。"""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT hint_level, pass_status FROM submission_log
               WHERE participant_id=? AND question_id=? ORDER BY id DESC LIMIT 10""",
            (participant_id, question_id),
        ).fetchall()
    if not rows:
        return 0, 0
    last_level = int(rows[0]["hint_level"])
    consecutive = 0
    for r in rows:
        if r["pass_status"] == 0:
            consecutive += 1
        else:
            break
    return last_level, consecutive


def handle_submission(*, participant_id: str, question_id: str, code: str,
                      config: HarnessConfig, request_hint: bool = False) -> dict:
    ts = _now()
    question = load_question(question_id)
    last_level, consecutive_fail = _recent_state(participant_id, question_id)

    # 1) 判题：所有组都要跑（判题不是实验变量）
    result = judge(question, code)

    # 2) 错误分类器（受开关控制）
    cls = None
    if config.error_classifier:
        cls = error_classifier.classify(result, code, question)
        error_class, error_kp = cls.error_class, cls.knowledge_point
    else:
        error_class, error_kp = "未分类", ""

    # 3) 学习者错误记忆（受开关控制）
    memory_hint = ""
    if config.error_memory:
        if result.status != "pass":
            error_memory.remember(participant_id, cls, ts)
        memory_hint = error_memory.preventive_hint(participant_id)

    # 4) 反馈与提示生成
    if config.hint_ladder:
        # 实验组：走提示阶梯（分层引导，不给答案）
        level = hint_ladder.next_level(last_level, requested=request_hint,
                                       consecutive_fail=consecutive_fail)
        message = hint_ladder.render(level, question, result, cls, memory_hint)
        source = "harness"
    else:
        # 对照组：裸大模型，直接把上下文交给模型（无教学法约束）
        level = 0
        message = llm_mod.llm(
            system="你是编程助手，请回答学生的问题。",
            user=f"题目：{question.get('statement','')}\n学生的代码：\n{code}\n"
                 f"判题结果：{result.status}；报错：{result.stderr}",
        )
        source = "raw_llm"

    # 5) 日志埋点（不得事后补录）
    payload = {
        "ts": ts,
        "participant_id": participant_id,
        "group_id": config.group_id,
        "question_id": question_id,
        "submit_seq": db.next_submit_seq(participant_id, question_id),
        "status": result.status,
        "error_type": result.error_type,
        "error_class": error_class,
        "error_kp": error_kp,
        "hint_level": level,
        "hint_requested": 1 if request_hint else 0,
        "pass_status": 1 if result.status == "pass" else 0,
        "elapsed_ms": result.exec_ms,
    }
    db.log_submission(**payload)

    return {
        "status": result.status,
        "failed_cases": result.failed_cases,
        "stderr": result.stderr,
        "error_class": error_class,
        "knowledge_point": error_kp,
        "hint_level": level,
        "memory_hint": memory_hint,
        "message": message,
        "source": source,
        "log": payload,
    }
