"""Harness 主干：一次提交（或一次"只看提示"）的完整处理流程。

数据流：判题 → **按注册表遍历教学组件** → 返回学生 →（提交时）写日志

**主干不认识任何具体组件**：它只按 `registry.ordered()` 遍历，用配置决定每个
组件"跑还是跳过"。所以新增一个教学组件，本文件一行都不用改。

**两种入口共用同一条管线**（`_run_pipeline`）：

- `handle_submission`：学生**提交代码** → 落一行 `submission_log`（推进 `submit_seq`）；
- `handle_hint_request`：学生**只想看提示、不改代码** → **不落库**，以免把
  "提交次数 / 首次通过率"这类指标虚增（见《技术方回执（第三回）》§五）。
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

# 题库尚未补 `phase` 时的兜底（契约 §2：phase 由题库自描述）
DEFAULT_PHASE = "practice"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _recent_state(participant_id: str, question_id: str) -> tuple[int, int]:
    """返回 (连续失败次数, 已**解锁**上限)。

    口径②-A 下"上次**展示**等级"不再参与计算（请求不升级），所以这里不读 `hint_level`。
    """
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT hint_unlocked_level, pass_status FROM submission_log
               WHERE participant_id=? AND question_id=? ORDER BY id DESC LIMIT 10""",
            (participant_id, question_id),
        ).fetchall()
    if not rows:
        return 0, 0
    last_unlocked = int(rows[0]["hint_unlocked_level"])
    consecutive = 0
    for r in rows:
        if r["pass_status"] == 0:
            consecutive += 1
        else:
            break
    return consecutive, last_unlocked


def _run_pipeline(*, participant_id: str, question_id: str, question: dict, code: str,
                  config: HarnessConfig, request_hint: bool, ts: str,
                  is_formal_submission: bool):
    """判题 → 按注册表遍历组件 → 兜底。返回 `(ctx, judge_result)`，**不写日志**。

    `is_formal_submission` 按**入口**区分（D 定案，2026-10-17）：

    - `True`（`/api/submit`）：本次判题**计入**一次失败 —— 连败 +1、错误记忆 +1；
    - `False`（`/api/hint`）：会对当前代码**重新判题**，但该次执行**不计入**正式提交连败、
      **也不更新**错误记忆，否则展示等级会越级、`count` 会被点击次数虚增。
      —— 本函数**不做判题缓存**，两个入口各判各的（Q1 允许提示入口判题）。

    **不得**用 `request_hint` 判断入口：`/api/submit` 也接受 `request_hint=true`。
    """
    consecutive_fail, last_unlocked = _recent_state(participant_id, question_id)

    # 1) 判题：与运行配置无关，任何情况都要跑
    result = judge(question, code)

    # 连败计数**含本次**：**仅正式提交入口**把本次判题计入（"连续失败 2 次解锁一级"
    # 要算上学生眼前这一次失败，契约 §5.2）。
    # 纯提示入口会对当前代码**重新判题**（Q1 允许），但那次执行**不**计连败
    # （否则跨门槛处会越级）。此处**没有**判题缓存，"复用判题结果"的说法不成立。
    if is_formal_submission and result.status != "pass":
        consecutive_fail += 1

    # 2) 按注册表遍历组件：开关真正决定"跑 / 跳过"
    ctx = SubmissionContext(
        participant_id=participant_id,
        question_id=question_id,
        question=question,
        code=code,
        ts=ts,
        request_hint=request_hint,
        judge_result=result,
        last_unlocked=last_unlocked,
        consecutive_fail=consecutive_fail,
        is_formal_submission=is_formal_submission,
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

    return ctx, result


def _response(ctx, result, *, log_payload: dict | None) -> dict:
    """两种入口共用的返回结构。`log_payload` 为 `None` 表示本次**没写库**。"""
    return {
        "status": result.status,
        "failed_cases": result.failed_cases,
        "stderr": result.stderr,
        "error_class": getattr(ctx.classification, "error_class", "未分类"),
        "knowledge_point": getattr(ctx.classification, "knowledge_point", ""),
        "hint_level": ctx.hint_level,
        "unlocked_level": ctx.unlocked_level,
        "hint_requested": 1 if ctx.request_hint else 0,
        "memory_hint": ctx.memory_hint,
        "message": ctx.message,
        "source": ctx.source,
        # 组件执行轨迹：便于演示与排查"哪个组件被跳过了"
        "components": {"executed": ctx.executed, "skipped": ctx.skipped},
        "log": log_payload,
    }


def handle_submission(*, participant_id: str, question_id: str, code: str,
                      config: HarnessConfig, request_hint: bool = False) -> dict:
    """学生**提交代码**：判题 + 教学反馈 + **落一行 `submission_log`**。"""
    ts = _now()
    question = load_question(question_id)
    ctx, result = _run_pipeline(
        participant_id=participant_id, question_id=question_id, question=question,
        code=code, config=config, request_hint=request_hint, ts=ts,
        is_formal_submission=True,
    )

    # 4) 日志埋点（不得事后补录）。
    #    `phase` 由题库元数据自描述、**前端不传**（防篡改）。
    payload = {
        "ts": ts,
        "participant_id": participant_id,
        "group_id": config.group_id,
        "question_id": question_id,
        "phase": question.get("phase", DEFAULT_PHASE),
        "submit_seq": db.next_submit_seq(participant_id, question_id),
        "status": result.status,
        "error_type": result.error_type,
        "error_class": getattr(ctx.classification, "error_class", "未分类"),
        "error_kp": getattr(ctx.classification, "knowledge_point", ""),
        "hint_level": ctx.hint_level,
        "hint_unlocked_level": ctx.unlocked_level,
        "hint_requested": 1 if request_hint else 0,
        "pass_status": 1 if result.status == "pass" else 0,
        "elapsed_ms": result.exec_ms,
    }
    db.log_submission(**payload)

    return _response(ctx, result, log_payload=payload)


def handle_hint_request(*, participant_id: str, question_id: str, code: str,
                        config: HarnessConfig) -> dict:
    """学生**只想看提示、代码没改**：同样过判题 + 全套教学组件 + 防泄校验。

    **但不写 `submission_log`**（返回里 `log` 恒为 `None`），
    以免把"提交次数 / 首次通过率"这类申报书指标虚增。

    同时按入口置 `is_formal_submission=False`：本次判题**不**计入连败、
    **不**写错误记忆（否则提示等级会越级、`count` 会被点击次数虚增）。
    """
    ts = _now()
    question = load_question(question_id)
    ctx, result = _run_pipeline(
        participant_id=participant_id, question_id=question_id, question=question,
        code=code, config=config, request_hint=True, ts=ts,
        is_formal_submission=False,
    )
    return _response(ctx, result, log_payload=None)
