"""Harness 主干：一次提交（或一次"只看提示"）的完整处理流程。

**四步确定序列（契约 §6.3）—— 每一步各自"一个连接、一个短事务"**：

1. **判题 → 短事务**（仅**正式提交**）：提交事实 ＋ `teaching(pending)` ＋ **执行事件终态** ＋ 回填 `submission_id`
   （纯提示没有提交事实可原子并入 → 它的执行终态走自己的短事务）
2. **分类／记忆 → 同一个短事务**：分类字段 ＋ 记忆计数 ＋ `progress` 标记（**`BEGIN IMMEDIATE` 内复查 `progress`**）
3. **事务外**调用模型（分类兜底、提示生成／兜底）
4. **收尾 → 同一个短事务**：提示事件 ＋ 提交行提示字段 ＋ `teaching` 终态

**前后测**（`pretest`／`posttest`，契约 §6.4）：**禁用教学帮助 ≠ 拒绝正式提交** ——
仍然正常判题、仍然写提交行；教学记 `phase_disabled`；**不调模型、不更新记忆**；
且**响应里任何位置**都不得出现教学诊断（连 `log` 里的 `error_class`／`error_kp` 也要剥掉）。

主干（本文件）只按 `registry.ordered()` 遍历组件，**不认识任何具体组件**。
"""

import json
import sqlite3
import uuid
from datetime import datetime

from app import db
from app import llm as llm_mod
from app.harness import builtin  # noqa: F401  —— 导入即注册内置组件
from app.harness import error_classifier as ec
from app.harness import error_memory as em
from app.harness import isolation
from app.harness import redaction
from app.harness import registry
from app.harness.base import SubmissionContext
from app.harness.config_switch import ERROR_CLASSIFIER, PRESETS, HarnessConfig
# `judge` 不再由管线直接调用（改走 `isolation.guarded_execute` 的执行适配器边界）：
# 保留这个导入，是因为测试与运维工具用 `pipeline.judge` 作为**可替换的判题实现引用**，
# 也是「管线是否直连宿主判题」的静态检查点。**不要**在 `_pipeline()` 里直接调用它。
from app.judge import judge
from app.questions import load_question

DEFAULT_PHASE = "practice"
TEACHING_DISABLED_PHASES = ("pretest", "posttest")
# 前后测**正式提交**：提交本身照常记录，只是不给教学提示
PHASE_DISABLED_MESSAGE = "当前为前后测阶段，本阶段不提供教学提示；你的提交已正常记录。"
# 前后测**纯提示**：这个入口**没有正式提交**，不能沿用上面那句（会误导学生）
PHASE_DISABLED_HINT_MESSAGE = "当前为前后测阶段，本阶段不提供教学提示。"
# 返回给学生的 `log` 里**不得出现**的教学诊断字段（口径 Q5：不能只藏前端）
_TEACHING_DIAGNOSTIC_KEYS = ("error_class", "error_kp")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _elapsed_ms(start: str, end: str) -> int:
    try:
        return max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start))
                          .total_seconds() * 1000))
    except Exception:
        return 0


def _reason_text(exc: BaseException) -> str:
    """事件表的 `reason` 只放**受控固定说明 + 异常类型名**。

    **截断 `str(exc)` 不等于脱敏** —— 前 120 字符同样可能含隐藏输入、密钥或模型响应
    内容，所以**不拼 `str(exc)`**（契约 §6.8）。
    """
    return isolation.reason_text(exc)


def _reason_code_of(exc: BaseException) -> str:
    """**受控错误码**：数据库错误、**已识别**的模型故障、其他内部错误**三者分开**，
    **不得**把所有非数据库异常统一记成 `model_error`。
    """
    if isinstance(exc, sqlite3.Error):
        return "db_error"
    if llm_mod.is_expected_failure(exc):
        return "model_error"
    return "internal_error"


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
        return 0, db.get_hint_state(participant_id, question_id)["unlocked_level"]
    last_unlocked = int(rows[0]["hint_unlocked_level"])
    consecutive = 0
    for r in rows:
        if r["pass_status"] == 0:
            consecutive += 1
        else:
            break
    # 主动请求升级后，解锁上限可能高于任何提交行（纯提示不写 submission_log）
    last_unlocked = max(last_unlocked, int(db.get_hint_state(participant_id, question_id)["unlocked_level"]))
    return consecutive, last_unlocked


def _phase_of(question: dict) -> str:
    return (question or {}).get("phase", DEFAULT_PHASE)


def _teaching_enabled(question: dict) -> bool:
    return _phase_of(question) not in TEACHING_DISABLED_PHASES


def _config_of(group_id: str) -> HarnessConfig:
    """从提交行的 `group_id` 还原配置 —— 用于判断组件开关，保持既有消融语义。"""
    return PRESETS.get(group_id) or PRESETS["exp"]


def stage_classify_and_remember(*, request_id: str, question: dict, code: str,
                                judge_result, ts: str):
    """**第 2 步**（分类／记忆）。契约 §6.3 第 2 步。

    顺序（关键）：

    1. **事务外**完成可能**调用模型**的分类计算（模型请求不得进事务）；
    2. 打开**显式事务**（`BEGIN IMMEDIATE`，一上来就取写锁）；
    3. 事务内**重新检查** `progress`（不能靠"进入 `with` 就已有事务"的假设）；
    4. 原子提交：分类字段 ＋ 记忆计数 ＋ 进度标记。

    返回分类结果；若事务内复查发现已完成、或找不到教学事件 → 返回 `None`（幂等跳过）。
    """
    rows = db.get_events(request_id=request_id, event_type="teaching")
    if not rows:
        return None
    teaching = rows[0]
    with db.connect() as conn:
        submission = db.get_submission_conn(conn, teaching["submission_id"])
    enabled = _config_of(submission["group_id"]) if submission is not None else None

    # ---- 事务外：分类（规则未命中时可能调用模型）----
    if enabled is not None and enabled.is_enabled(ERROR_CLASSIFIER):
        classification = ec.classify(judge_result, code, question)
    else:
        classification = ec.Classification(error_class="未分类", knowledge_point="",
                                           source="none")

    # ---- 显式短事务：复查 progress → 原子写 ----
    conn = db.connect_immediate()
    try:
        conn.execute("BEGIN IMMEDIATE")
        fresh = db.get_event_conn(conn, request_id, "teaching")
        if fresh is None or fresh["progress"] == "cls_mem_done":
            conn.execute("ROLLBACK")
            return None
        db.update_submission_conn(conn, fresh["submission_id"],
                                  error_class=classification.error_class,
                                  error_kp=classification.knowledge_point or "")
        if (judge_result.status != "pass" and enabled is not None
                and enabled.is_enabled(em.NAME)):
            em.remember_conn(conn, fresh["participant_id"], classification, ts,
                             judge_error_type=judge_result.error_type)
        db.update_event_conn(conn, fresh["event_id"], progress="cls_mem_done")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return classification


def _mark_judge_failure(exec_event_id: int, hint_event_id: int | None,
                        exc: BaseException, ts: str) -> None:
    """`judge` 未正常完成：执行记 `aborted`；该请求已建的提示事件收尾为 `unavailable`。

    **隔离不可用必须与「判题器自己抛异常」分开记**（G-② §3.8）：

    - 抛 `isolation.IsolationUnavailable` ⇒ `reason_code="isolation_unavailable"`
      （**拒绝执行**，机制未就绪）；
    - 其他异常 ⇒ 仍记 `judge_raised`（既有语义，`RuntimeError` 等不得被误判）。

    两类**都不是**「判题器正常返回 `status="error"`」——后者走 `finished` 路径。
    同时落一条**独立结构化运行记录**（G-① §3.5 方案 B），**不改写判题结果**。
    """
    now = _now()
    code = isolation.reason_code_of(exc)
    # 「未执行」与「执行中异常中止」必须可区分（评阅 m00742 第 4 点）：
    # precheck = 检查未通过，学生代码一次都没启动；executing = 已进入隔离环境后中止。
    stage = getattr(exc, "stage", "precheck")
    db.update_event(exec_event_id, status="aborted", ts_end=now,
                    elapsed_ms=_elapsed_ms(ts, now),
                    reason_code=code, reason=_reason_text(exc),
                    progress="isolation_aborted" if stage == "executing" else "")
    if isinstance(exc, isolation.IsolationUnavailable):
        _record_isolation_unavailable(exec_event_id, exc, ts)
    if hint_event_id is not None:
        db.update_event(hint_event_id, status="unavailable", ts_end=now,
                        elapsed_ms=_elapsed_ms(ts, now),
                        reason_code="gen_unavailable", reason="判题未完成，未生成提示")


def _record_isolation_unavailable(exec_event_id: int, exc, ts: str) -> None:
    """写「隔离不可用」运行记录。

    **失败不得改变判题事实，但也不得静默**：`reason_code` 已记进 execution 事件，
    运行记录只是附加的可关联证据，因此这里**不重抛**（否则会把「隔离不可用」
    变成「运行记录写失败」）；失败时改为在宿主侧输出**受控诊断码**
    `record_write_failed`（`isolation.warn()`，固定文案、不接受自由文本），
    使失败**可被运维看见**而不产生新的泄露通道。
    """
    try:
        exec_event = db.get_event(exec_event_id) or {}
        isolation.record_run_event(
            request_id=exec_event.get("request_id") or "",
            phase=exec_event.get("phase") or "",
            event=isolation.RUN_EVENT_ISOLATION_UNAVAILABLE,
            detail_code=isolation.normalize_detail_code(getattr(exc, "detail_code", "not_ready")),
            ts=ts,
        )
    except Exception:
        # 不重抛、不改判题结果；但**必须可见**（G-③ 第 5 项：降级标记 record_write_failed）
        isolation.warn("record_write_failed")
        return


def _run_components(ctx: SubmissionContext, config: HarnessConfig) -> None:
    """按注册表遍历组件（**含模型调用，全程在事务外**）。"""
    for component in registry.ordered():
        if config.is_enabled(component.name):
            component.run(ctx)
            ctx.executed.append(component.name)
        else:
            ctx.skipped.append(component.name)
    if not ctx.message:
        # 没有任何教学组件产出反馈 → 裸大模型兜底：契约 §6.2.2 要求**显式**记 raw_llm
        # 送进模型的报错必须是**过滤后**的视图（F：隐藏用例数据不得进入任何模型上下文）
        ctx.message = llm_mod.llm(
            system="你是编程助手，请回答学生的问题。",
            user=f"题目：{ctx.question.get('statement', '')}\n学生的代码：\n{ctx.code}\n"
                 f"判题结果：{ctx.judge_result.status}；"
                 f"报错：{redaction.model_stderr(ctx.judge_result, ctx.question)}",
        )
        ctx.source = "raw_llm"


def _hint_event_fields(ctx: SubmissionContext, ts: str) -> dict:
    """提示事件的终态字段：`status` 回答"有没有产出"，`hint_source` 回答"怎么产出的"。"""
    if ctx.source == "raw_llm" and not ctx.hint_source:
        # 裸模型路径：显式记 `raw_llm`，等级字段为 NULL（没有阶梯等级）
        return {"status": "returned_model", "ts_end": _now(), "elapsed_ms": _elapsed_ms(ts, _now()),
                "hint_source": "raw_llm", "hint_level": None, "unlocked_level": None,
                "reason_code": "raw_llm_path", "reason": "未经提示阶梯，未过防泄校验"}
    if ctx.hint_source == "fallback":
        status = "returned_fallback"
    elif ctx.hint_source in ("llm", "llm_retry"):
        status = "returned_model"
    else:
        status = "unavailable"
    return {"status": status, "ts_end": _now(), "elapsed_ms": _elapsed_ms(ts, _now()),
            # 独立模型耗时：一次请求里**所有**模型调用（分类兜底＋提示＋重试）之和；
            # 与 `elapsed_ms`（整请求耗时，含判题）分开，便于把"判题/模型/学生作答"拆开。
            "model_ms": llm_mod.total_elapsed_ms(),
            "hint_source": ctx.hint_source, "hint_level": ctx.hint_level,
            "unlocked_level": ctx.unlocked_level,
            "reason_code": "" if status != "unavailable" else "gen_unavailable",
            "reason": "" if status != "unavailable" else "未产出可返回的提示"}


def _finalize(conn, *, hint_event_id: int | None, teaching_event_id: int | None,
              ctx: SubmissionContext, teaching_on: bool, request_hint: bool, ts: str) -> None:
    """**第 4 步**：**一个连接、一个短事务** —— 提示事件 ＋ 教学终态。

    （提交行提示字段的回填由调用方在**同一个 `conn`** 里完成 → 三者不会半套提交。）

    教学终态按"约定的教学步骤是否都有产出"判断：`fallback` 属**有产出**，
    **不因模型曾失败就整体记 `failed`**（契约 §6.3 硬规则 4）。
    """
    if hint_event_id is not None and teaching_on:
        db.update_event_conn(conn, hint_event_id, **_hint_event_fields(ctx, ts))
    if teaching_event_id is not None and teaching_on:
        produced = ((not request_hint) or ctx.source == "raw_llm" or ctx.hint_source is not None)
        db.update_event_conn(conn, teaching_event_id,
                             status="done" if produced else "partial",
                             reason_code="" if produced else "gen_unavailable",
                             reason="" if produced else "请求了提示但未产出可返回的提示",
                             ts_end=_now(), elapsed_ms=_elapsed_ms(ts, _now()))


def _finalize_failure(*, teaching_event_id: int | None, hint_event_id: int | None,
                      teaching_on: bool, request_hint: bool, ts: str,
                      exc: BaseException) -> None:
    """异常收尾：**记录后重新抛出**（数据库／程序缺陷必须暴露，不得伪装成功）。

    状态按**已完成进度**区分：第 2 步已提交（`cls_mem_done`）→ `partial`；否则 `failed`。
    **不得**一律记成"模型错误" —— 错误码由 `_reason_code_of()` 按异常类型给出。
    """
    now, code, text = _now(), _reason_code_of(exc), _reason_text(exc)
    if teaching_event_id is not None and teaching_on:
        event = db.get_event(teaching_event_id) or {}
        status = "partial" if event.get("progress") == "cls_mem_done" else "failed"
        db.update_event(teaching_event_id, status=status, ts_end=now,
                        elapsed_ms=_elapsed_ms(ts, now), reason_code=code, reason=text)
    if request_hint and teaching_on and hint_event_id is not None:
        db.update_event(hint_event_id, status="unavailable", ts_end=now,
                        elapsed_ms=_elapsed_ms(ts, now),
                        reason_code="gen_unavailable", reason=text)


def _student_log(payload: dict | None, *, phase_disabled: bool) -> dict | None:
    """返回给**学生**的 `log`。

    前后测必须剥掉**整个结构**里的教学诊断（口径 Q5：不能只藏前端，也不能只藏顶层）。
    """
    if payload is None or not phase_disabled:
        return payload
    return {k: v for k, v in payload.items() if k not in _TEACHING_DIAGNOSTIC_KEYS}


def _pipeline(*, participant_id: str, question_id: str, question: dict, code: str,
              config: HarnessConfig, request_hint: bool, entry: str,
              hint_request_id: str = ""):
    """两种入口共用的四步序列。返回 `(ctx, result, meta)`；前后测纯提示时 ctx／result 为 None。"""
    ts, request_id = _now(), uuid.uuid4().hex
    phase = _phase_of(question)
    teaching_on = _teaching_enabled(question)
    llm_mod.reset_timing()   # 请求级模型计时：后面所有 llm() 调用都累计到本次请求

    # ---- 幂等回放：同一次点击的重复到达（刷新/网络重试）----
    # 不升级、不判题、不调模型，直接回放上次结果（契约 §5 提示阶梯修订）。
    if entry == "hint" and hint_request_id:
        st = db.get_hint_state(participant_id, question_id)
        if st["last_request_id"] == hint_request_id and st["last_response"]:
            try:
                cached = json.loads(st["last_response"])
            except ValueError:
                cached = None
            if cached is not None:
                return None, None, {"replayed": True, "response": cached}

    hint_state = db.get_hint_state(participant_id, question_id) if request_hint else None

    # ---- 提示事件：**受理有效请求时先建**（契约 §6.2.2），才能留下失败路径 ----
    hint_event_id = None
    if request_hint:
        # `phase_disabled` 一建就是**终态** → 同时填好结束时间，避免"状态已结束、`ts_end` 为空"
        hint_event_id = db.create_event(
            event_type="hint", request_id=request_id, participant_id=participant_id,
            question_id=question_id, phase=phase, entry=entry, ts_start=ts,
            status="requested" if teaching_on else "phase_disabled", hint_source=None,
            ts_end=None if teaching_on else ts,
            elapsed_ms=None if teaching_on else 0,
            reason_code="" if teaching_on else "phase_disabled",
            reason="" if teaching_on else "前后测阶段不提供教学提示")

    # ---- 前后测的**纯提示**：提前返回 —— 不调 judge、不调模型（契约 §6.4 第三行）----
    if entry == "hint" and not teaching_on:
        return None, None, {"request_id": request_id, "phase": phase, "ts": ts,
                            "phase_disabled": True, "teaching_on": False, "log": None}

    # ---- 执行事件：登记（`started`）----
    exec_event_id = db.create_event(
        event_type="execution", request_id=request_id, participant_id=participant_id,
        question_id=question_id, phase=phase, entry=entry, ts_start=ts, status="started")
    try:
        # **安全门**：判题只在执行适配器（隔离环境）就绪时才发生。
        # 未登记适配器 ⇒ `guarded_execute` 直接抛 `IsolationUnavailable`，
        # `judge()` 与学生子进程**一次都不会启动**（fail closed，不回退宿主 subprocess）。
        result = isolation.guarded_execute(
            isolation.registered_execution_adapter(),
            {"question": question, "code": code, "language": "python"},
            request_id=request_id, phase=phase,
        )
    except Exception as exc:
        _mark_judge_failure(exec_event_id, hint_event_id, exc, ts)
        raise
    exec_final = {"status": "finished", "ts_end": _now(),
                  "elapsed_ms": _elapsed_ms(ts, _now()), "judge_status": result.status}

    # 连败计数**含本次**，但**仅正式提交入口**计入（D 定案；纯提示复用同一次判题不得再计）
    consecutive_fail, last_unlocked = _recent_state(participant_id, question_id)
    if hint_state is not None:
        last_unlocked = max(last_unlocked, int(hint_state["unlocked_level"]))
    if entry == "submit" and result.status != "pass":
        consecutive_fail += 1

    # ---- 步骤 1 后半：TX1（仅正式提交；执行终态**并入本事务**）----
    submission_id, teaching_event_id, classification, payload = None, None, None, None
    if entry == "submit":
        # 前后测只用**规则**做内部记录（不调模型）；练习阶段留给第 2 步回填
        classification = None if teaching_on else ec.classify_rule_only(result, code, question)
        payload = {
            "ts": ts, "participant_id": participant_id, "group_id": config.group_id,
            "question_id": question_id, "phase": phase,
            "submit_seq": db.next_submit_seq(participant_id, question_id),
            "status": result.status, "error_type": result.error_type,
            "error_class": getattr(classification, "error_class", "未分类"),
            "error_kp": getattr(classification, "knowledge_point", "") or "",
            "hint_level": 0, "hint_unlocked_level": 0,
            "hint_requested": 1 if request_hint else 0,
            "pass_status": 1 if result.status == "pass" else 0,
            "elapsed_ms": result.exec_ms,
        }
        with db.connect() as conn:
            submission_id = db.log_submission_conn(conn, **payload)
            teaching_event_id = db.create_event_conn(
                conn, event_type="teaching", request_id=request_id,
                participant_id=participant_id, question_id=question_id, phase=phase,
                entry=entry, ts_start=ts, submission_id=submission_id,
                status="pending" if teaching_on else "phase_disabled", progress="",
                ts_end=None if teaching_on else ts,
                elapsed_ms=None if teaching_on else 0,
                reason_code="" if teaching_on else "phase_disabled",
                reason="" if teaching_on else "前后测阶段禁用教学处理")
            # 执行终态**并入同一事务**（契约 §6.3 第 1 步），不再提前单独提交
            db.update_event_conn(conn, exec_event_id, submission_id=submission_id, **exec_final)
            # 关联回填：**提示事件也要回填** `submission_id`（契约 §6.5）
            if hint_event_id is not None:
                db.update_event_conn(conn, hint_event_id, submission_id=submission_id)
    else:
        # 纯提示：没有提交事实可原子并入 → 执行终态走自己的短事务
        db.update_event(exec_event_id, **exec_final)

    # ---- 步骤 2 / 3 / 4（异常时统一走失败收尾）----
    ctx = SubmissionContext(
        participant_id=participant_id, question_id=question_id, question=question,
        code=code, ts=ts, request_hint=request_hint, judge_result=result,
        last_unlocked=last_unlocked, consecutive_fail=consecutive_fail,
        is_formal_submission=(entry == "submit"), classification=classification,
        unlocked_at_last_hint=int(hint_state["unlocked_at_last_hint"]) if hint_state else 0,
    )
    meta = {"request_id": request_id, "phase": phase, "ts": ts,
            "phase_disabled": not teaching_on, "teaching_on": teaching_on,
            "submission_id": submission_id, "teaching_event_id": teaching_event_id,
            "log": None}
    try:
        if entry == "submit" and teaching_on:
            got = stage_classify_and_remember(request_id=request_id, question=question,
                                              code=code, judge_result=result, ts=ts)
            # 罕见：重复进入导致跳过 → 仍给出分类供呈现层使用
            ctx.classification = got if got is not None else ec.classify_rule_only(
                result, code, question)
            if ctx.classification is not None:
                payload = dict(payload, error_class=ctx.classification.error_class,
                               error_kp=ctx.classification.knowledge_point or "")

        if teaching_on:
            _run_components(ctx, config)
        else:
            # 前后测：**不跑任何教学组件、不调模型**；只给阶段说明（也不暴露教学诊断）
            ctx.skipped = [c.name for c in registry.ordered()]
            ctx.source = "phase_disabled"
            ctx.message = (PHASE_DISABLED_HINT_MESSAGE if entry == "hint"
                           else PHASE_DISABLED_MESSAGE)

        # ---- 步骤 4：**一个连接、一个短事务** ----
        with db.connect() as conn:
            _finalize(conn, hint_event_id=hint_event_id,
                      teaching_event_id=teaching_event_id, ctx=ctx,
                      teaching_on=teaching_on, request_hint=request_hint, ts=ts)
            if entry == "submit" and teaching_on:
                db.update_submission_conn(conn, submission_id, hint_level=ctx.hint_level,
                                          hint_unlocked_level=ctx.unlocked_level)
        if entry == "submit" and teaching_on and payload is not None:
            payload = dict(payload, hint_level=ctx.hint_level,
                           hint_unlocked_level=ctx.unlocked_level)
    except Exception as exc:
        _finalize_failure(teaching_event_id=teaching_event_id, hint_event_id=hint_event_id,
                          teaching_on=teaching_on, request_hint=request_hint, ts=ts, exc=exc)
        raise
    meta["log"] = payload
    return ctx, result, meta


def _response(ctx, result, *, log_payload: dict | None, meta: dict | None = None) -> dict:
    """两种入口共用的返回结构。`log_payload` 为 `None` 表示本次**没写库**。

    **F：学生响应里不得带隐藏用例数据** —— `failed_cases` 与 `stderr` 都走
    `redaction` 的安全视图（隐藏条目清空输入/期望/实际输出；顶层 `stderr` 无法归属来源时
    只留受控异常类别名）。判题事实（`status`／`error_type`／通过数量／耗时）一律不变。
    """
    phase_disabled = bool((meta or {}).get("phase_disabled"))
    return {
        "status": result.status if result is not None else "phase_disabled",
        "failed_cases": redaction.student_failed_cases(result, ctx.question),
        "stderr": redaction.student_stderr(result, ctx.question),
        # 执行层错误类型（`pass/fail` 之外的**结构化**字段）：前端用它渲染"错误类型：运行时错误"，
        # 不必再去抠 `judge_feedback` 的字符串
        "error_type": result.error_type if result is not None else "",
        # 前后测**不只藏顶层**：`log` 里的教学诊断同样要剥掉（口径 Q5）
        "error_class": "" if phase_disabled else getattr(ctx.classification, "error_class", "未分类"),
        "knowledge_point": "" if phase_disabled else getattr(ctx.classification, "knowledge_point", ""),
        "hint_level": ctx.hint_level,
        "unlocked_level": ctx.unlocked_level,
        "hint_requested": 1 if ctx.request_hint else 0,
        "memory_hint": "" if phase_disabled else ctx.memory_hint,
        "message": ctx.message,
        # **判题反馈独立字段**：前端"程序运行结果"栏用它，AI 提示栏只渲染 `message`（不再重复）
        "judge_feedback": "" if phase_disabled else ctx.judge_feedback,
        "source": ctx.source,
        "components": {"executed": ctx.executed, "skipped": ctx.skipped},
        "log": _student_log(log_payload, phase_disabled=phase_disabled),
    }


def _disabled_response(*, hint_requested: bool) -> dict:
    """前后测**纯提示**的提前返回：不判题、不调模型、不给教学提示。"""
    return {
        "status": "phase_disabled", "failed_cases": [], "stderr": "", "error_type": "",
        "error_class": "", "knowledge_point": "",
        "hint_level": 0, "unlocked_level": 0,
        "hint_requested": 1 if hint_requested else 0,
        "memory_hint": "", "message": PHASE_DISABLED_HINT_MESSAGE, "judge_feedback": "",
        "source": "phase_disabled",
        "components": {"executed": [], "skipped": []}, "log": None,
    }


_HINT_CACHE_KEYS = ("status", "error_type", "error_class", "knowledge_point", "hint_level",
                    "unlocked_level", "memory_hint", "message", "judge_feedback", "source",
                    "failed_cases", "stderr", "components")


def _hint_cache(resp: dict) -> dict:
    """只缓存**提示相关**字段：够回放，又不把整份响应（可能很大）存进库。"""
    return {k: resp.get(k) for k in _HINT_CACHE_KEYS}


def _remember_hint(participant_id: str, question_id: str, resp: dict, *,
                   hint_request_id: str) -> None:
    db.save_hint_state(
        participant_id, question_id,
        unlocked_level=int(resp.get("unlocked_level") or 0),
        displayed_level=int(resp.get("hint_level") or 0),
        unlocked_at_last_hint=int(resp.get("unlocked_level") or 0),
        last_request_id=hint_request_id,
        last_response=json.dumps(_hint_cache(resp), ensure_ascii=False),
        ts=_now(),
    )


def handle_submission(*, participant_id: str, question_id: str, code: str,
                      config: HarnessConfig, request_hint: bool = False) -> dict:
    """学生**提交代码**：判题 ＋ 教学反馈 ＋ 落一行 `submission_log`（四步序列，契约 §6.3）。"""
    question = load_question(question_id)
    ctx, result, meta = _pipeline(
        participant_id=participant_id, question_id=question_id, question=question,
        code=code, config=config, request_hint=request_hint, entry="submit")
    resp = _response(ctx, result, log_payload=meta.get("log"), meta=meta)
    if request_hint and meta.get("teaching_on"):
        _remember_hint(participant_id, question_id, resp, hint_request_id="")
    return resp


def handle_hint_request(*, participant_id: str, question_id: str, code: str,
                        config: HarnessConfig, hint_request_id: str = "") -> dict:
    """学生**只想看提示、代码没改**：同样走判题 ＋ 教学组件 ＋ 防泄校验。

    **不写 `submission_log`**（`log` 恒为 `None`），也**不产生 `teaching` 事件**（契约 §6.5）；
    前后测阶段**提前返回、不调 judge、不调模型**（契约 §6.4）。
    同一次点击的重复到达（`hint_request_id` 相同）**直接回放**，不升级、不判题、不调模型。
    """
    question = load_question(question_id)
    ctx, result, meta = _pipeline(
        participant_id=participant_id, question_id=question_id, question=question,
        code=code, config=config, request_hint=True, entry="hint",
        hint_request_id=hint_request_id)
    if meta.get("replayed"):
        cached = dict(meta.get("response") or {})
        cached.update({"hint_requested": 1, "replayed": True, "log": None})
        return cached
    if ctx is None:
        return _disabled_response(hint_requested=True)
    resp = _response(ctx, result, log_payload=None, meta=meta)
    if meta.get("teaching_on"):
        _remember_hint(participant_id, question_id, resp, hint_request_id=hint_request_id)
    return resp
