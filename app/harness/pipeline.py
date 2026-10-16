"""Harness 主干：一次提交的完整处理流程。

数据流：提交代码 → 判题 → **按注册表遍历教学组件** → 返回学生 → 写日志

**主干不认识任何具体组件**：它只按 `registry.ordered()` 遍历，用配置决定每个
组件"跑还是跳过"。所以新增一个教学组件，本文件一行都不用改。
"""

from datetime import datetime

from app import db
from app import llm as llm_mod
from app.harness import builtin  # noqa: F401  —— 导入即注册内置组件
from app.harness import registry
from app.harness.base import SubmissionContext
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

    # 1) 判题：与实验分组无关，任何情况都要跑
    result = judge(question, code)

    # 2) 按注册表遍历组件：开关真正决定"跑 / 跳过"
    ctx = SubmissionContext(
        participant_id=participant_id,
        question_id=question_id,
        question=question,
        code=code,
        ts=ts,
        request_hint=request_hint,
        judge_result=result,
        last_level=last_level,
        consecutive_fail=consecutive_fail,
    )
    for component in registry.ordered():
        if config.is_enabled(component.name):
            component.run(ctx)
            ctx.executed.append(component.name)
        else:
            ctx.skipped.append(component.name)

    # 3) 兜底：没有任何教学组件产出反馈 → 裸大模型。
    #    注意：`ctrl`（组件一个都不开）走到这里就是对照组；
    #    若只开了分类器/记忆而没开提示阶梯，也会走到这里（没有呈现层）。
    if not ctx.message:
        ctx.message = llm_mod.llm(
            system="你是编程助手，请回答学生的问题。",
            user=f"题目：{question.get('statement','')}\n学生的代码：\n{code}\n"
                 f"判题结果：{result.status}；报错：{result.stderr}",
        )
        ctx.source = "raw_llm"

    # 4) 取分类结果（分类器被跳过时为「未分类」）
    error_class = getattr(ctx.classification, "error_class", "未分类")
    error_kp = getattr(ctx.classification, "knowledge_point", "")

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
        "hint_level": ctx.hint_level,
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
        "hint_level": ctx.hint_level,
        "memory_hint": ctx.memory_hint,
        "message": ctx.message,
        "source": ctx.source,
        # 组件执行轨迹：便于演示与排查"哪个组件被跳过了"
        "components": {"executed": ctx.executed, "skipped": ctx.skipped},
        "log": payload,
    }
