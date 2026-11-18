"""FastAPI 入口。托管静态前端 + 提供提交/请求提示接口。

启动：uvicorn app.main:app --reload
"""

import logging
import os
import threading
import time
import weakref
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import access
from app import admin
from app import db
from app import enroll
from app import flow
from app import llm as _llm
from app.harness import builtin  # noqa: F401  —— 导入即注册内置组件
from app.harness import registry
from app.harness.config_switch import PRESETS
from app.harness.pipeline import handle_hint_request, handle_submission
from app.harness import isolation
from app.harness.vm_agent_adapter import VmAgentAdapter, register_vm_agent_adapter
from app.questions import list_questions, load_question

app = FastAPI(title="面向编程初学者的可插拔教学法练习助手的设计与实现")
app.include_router(admin.router)
APP_VERSION = "0.5.4-order"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
VENDOR_DIR = WEB_DIR / "vendor"

db.init_db()

# 正式实验统一走 `exp`（契约 §5）。只有现场演示/联调才允许切预设：
# 设环境变量 `PH_ALLOW_GROUP_SWITCH=1` 才放行 `group` 字段，否则一律按 exp 记。
ALLOW_GROUP_SWITCH = os.environ.get("PH_ALLOW_GROUP_SWITCH") == "1"

#: 单路执行闸门：实验为「单参与者、单次提交」，不允许两个请求同时判题/调模型。
#: 非阻塞获取，占不到就立刻 429（不排队、不等待），避免把等待时间算进学生作答耗时。
_SINGLE_LANE = threading.Lock()


def _enter_single_lane() -> None:
    if not _SINGLE_LANE.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail={
                "status": "busy",
                "message": "上一次提交仍在执行，请稍候再试（本实验为单路执行，不排队）。",
            },
        )

#: 拒判时给学生看的**固定文案**（不拼异常原文、不含学生代码、不伪装成 runtime error）
ISOLATION_NOT_READY_MESSAGE = (
    "判题环境未就绪：本次提交未执行判题（没有启动任何学生代码）。"
    "请联系管理员完成隔离环境接入后再提交。"
)

# 阶段 3 的**运维开关**：只有显式设置 `PH_VM_AGENT_ENABLED=1` 才在启动时登记 VM 适配器。
# 不设置时保持"未登记" ⇒ 管线 fail closed，宁可不判也不在宿主机上执行学生代码。
VM_AGENT_ENABLED = os.environ.get("PH_VM_AGENT_ENABLED") == "1"

_log = logging.getLogger(__name__)


def _register_configured_vm_agent() -> bool:
    """按运维开关登记 VM 适配器；任何失败都保持未登记，**绝不降级到宿主执行**。

    登记前先做一次**健康检查 + 握手 + 能力检查**（`check_isolation()`）：
    未就绪就干脆不登记，避免"登记了但每个请求都拒"的假接入。
    """
    if not VM_AGENT_ENABLED:
        return False
    try:
        adapter = VmAgentAdapter()
        adapter.check_isolation()
        isolation.register_execution_adapter(adapter)
    except Exception as exc:  # noqa: BLE001 —— 登记失败只记日志，进程继续 fail closed
        _log.warning("VM agent 适配器未登记（就绪检查未通过）：%s", exc)
        return False
    _log.info("已登记 VM agent 适配器：%s", adapter.describe()["url"])
    return True


VM_AGENT_REGISTERED = _register_configured_vm_agent()

#: `/api/status` 的就绪探测缓存：只打 `/healthz` + 握手，**不启动任何执行**。
#: 用弱引用记住是哪一个适配器，避免对象回收后 id 被复用导致误命中缓存。
_ISOLATION_PROBE = {"at": 0.0, "adapter": None, "state": "unknown"}
_ISOLATION_PROBE_TTL = 15.0

_ISOLATION_NOTES = {
    "not_configured": ISOLATION_NOT_READY_MESSAGE,
    "registered_unverified": (
        "判题环境已接入适配器，但就绪检查未通过：提交会被拒绝并记录为“未执行”，"
        "不会运行任何学生代码。"
    ),
    "execution_probe_passed": (
        "判题环境已连通、固定样例可执行；隔离能力尚未全部验证，"
        "学生提交仍会被拒绝（不会执行）。"
    ),
    "isolation_capabilities_verified": (
        "隔离能力自检通过，等待受控演示开关；学生提交仍会被拒绝。"
    ),
    "ready": "",
}

#: agent 自报的分层 → `/api/status` 展示值（未知值一律按未就绪处理）
_READINESS_STATES = {
    "execution_probe_passed": "execution_probe_passed",
    "isolation_capabilities_verified": "isolation_capabilities_verified",
    "ready_for_controlled_demo": "ready",
}


def _isolation_state(adapter) -> str:
    """返回 `not_configured` / `registered_unverified` / 分层值 / `ready`。

    **登记 ≠ 就绪**：只有适配器自检（health + 握手 + 能力位）通过才算 `ready`；
    探测失败一律按未就绪处理。结果按适配器身份缓存 15 秒，避免每次刷新都打网络。
    """
    if adapter is None:
        return "not_configured"
    now = time.monotonic()
    cached = _ISOLATION_PROBE
    ref = cached["adapter"]
    if (ref is not None and ref() is adapter
            and cached["state"] != "unknown"
            and now - cached["at"] < _ISOLATION_PROBE_TTL):
        return cached["state"]

    def _remember(state: str) -> str:
        try:
            handle = weakref.ref(adapter)
        except TypeError:                      # 有些测试替身不支持弱引用
            handle = None
        _ISOLATION_PROBE.update(at=now, adapter=handle, state=state)
        return state

    probe = getattr(adapter, "readiness", None)
    if callable(probe):
        try:
            layered = _READINESS_STATES.get(probe())
        except Exception:  # noqa: BLE001 —— 探测失败就是未就绪，不能冒充可用
            layered = None
        if layered:
            return _remember(layered)
    try:
        state = "ready" if adapter.check_isolation() else "registered_unverified"
    except Exception:  # noqa: BLE001 —— 探测失败就是未就绪，不能冒充可用
        state = "registered_unverified"
    return _remember(state)

# 前端已隐藏分组下拉，这里再兜一层——挡住手工构造的改包请求，避免污染实验数据。
if VENDOR_DIR.exists():
    app.mount("/vendor", StaticFiles(directory=str(VENDOR_DIR)), name="vendor")


def _config(group: str):
    if not ALLOW_GROUP_SWITCH:
        return PRESETS["exp"]
    return PRESETS.get(group, PRESETS["exp"])


class SubmitRequest(BaseModel):
    participant_id: str = ""     # 留空时由访问码/登记 Cookie 决定身份（学生不必手填）
    question_id: str
    code: str
    group: str = "exp"          # 仅供联调/演示；默认被服务端忽略，一律按 exp 记
    request_hint: bool = False  # 学生是否主动请求提示


class HintRequest(BaseModel):
    participant_id: str = ""     # 同上：留空则由服务端凭证解析
    question_id: str
    code: str = ""              # 当前编辑器里的代码，供大模型看到"学生卡在哪"
    hint_request_id: str = ""   # 同一次点击的幂等键（刷新/重试复用，服务端回放不升级）


def _debug_allowed() -> bool:
    """开发调试信息是否允许下发。

    实验/正式模式（访问码强制或 `PH_EXPERIMENT_MODE=1`）一律关闭：
    界面上隐藏「调试」按钮只是表面，**服务端不返回组件轨迹**才算真正关掉。
    """
    return not _llm.experiment_mode()


def _claims(x_access_code: str | None) -> access.Claims | None:
    """解析实验访问码。未开启强制模式且没带码 → None（本地演示兼容）。

    正式采集必须 `PH_REQUIRE_ACCESS_CODE=1`；此时缺失/过期/伪造一律在**判题之前**拒绝。
    """
    if not isinstance(x_access_code, str):
        # 直接调用函数（测试/脚本）时拿到的是 FastAPI 的 Header 默认对象，按"没带码"处理
        x_access_code = None
    if not access.access_required() and not x_access_code:
        return None
    try:
        claims = access.verify_code(x_access_code)
        access.ensure_batch(claims)
        return claims
    except access.AccessNotConfigured:
        raise HTTPException(status_code=503, detail={
            "status": "access_not_configured",
            "message": "实验访问码未配置：服务端缺少签名密钥，本次请求未执行。",
        })
    except access.AccessError as exc:
        if exc.code == "batch_mismatch":
            raise HTTPException(status_code=403, detail={
                "status": "batch_mismatch",
                "message": "访问码不属于当前实验批次，本次请求未执行。",
            })
        raise HTTPException(status_code=403, detail={
            "status": "access_denied", "code": exc.code,
            "message": "实验访问码无效或已过期，本次请求未执行。",
        })


def _participant_of(body_participant_id: str, claims: access.Claims | None,
                    request: Request | None = None) -> str:
    # 自动登记模式：以服务端下发的 Cookie 为准，请求体里的编号不能顶替别人
    if request is not None and enroll.enabled():
        enrolled = enroll.resolve(request)
        if not enrolled:
            raise HTTPException(status_code=403, detail={
                "status": "session_required", "code": "session_missing",
                "message": "页面会话尚未建立，请刷新页面后重试（本次请求未执行）。",
            })
        if (body_participant_id or "").strip() and body_participant_id.strip() != enrolled:
            raise HTTPException(status_code=403, detail={
                "status": "identity_mismatch", "code": "enrollment_mismatch",
                "message": "这台电脑登记的是另一个被试编号，本次请求未执行。",
            })
        return enrolled
    try:
        return access.resolve_participant(body_participant_id, claims)
    except access.AccessError as exc:
        raise HTTPException(status_code=403, detail={
            "status": "access_denied", "code": exc.code,
            "message": "访问码与请求中的参与者不一致，本次请求未执行。",
        })


def _require_phase_allowed(question_id: str, claims: access.Claims | None) -> None:
    """阶段必须由**服务端**根据题目与访问码判定，不能只信客户端传的 phase。"""
    if claims is None:
        return
    question = load_question(question_id)
    phase = (question or {}).get("phase", "practice")
    if not claims.allows_phase(phase):
        raise HTTPException(status_code=403, detail={
            "status": "phase_not_allowed", "phase": phase,
            "message": "当前访问码不允许进入该阶段题目，本次请求未执行。",
        })


def _require_flow_phase(question_id: str, participant_id: str) -> None:
    """匿名会话模式：只能提交**当前阶段**的题目（顺序锁定，不能靠前端隐藏）。"""
    if not enroll.enabled() or not participant_id:
        return
    question = load_question(question_id)
    phase = (question or {}).get("phase", "practice")
    now = flow.current(participant_id, enroll._batch())
    if now != phase:
        raise HTTPException(status_code=403, detail={
            "status": "phase_locked", "phase": now, "question_phase": phase,
            "message": f"当前阶段是「{flow.LABELS.get(now, now)}」，只能做本阶段题目；"
                       "上一阶段的成绩已锁定，不能返回修改。",
        })


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/me")
def api_me(request: Request = None, x_access_code: str = Header(default=None)) -> dict:
    """身份自证：带有效访问码时返回**服务端认可**的参与者编号、批次与当前阶段。

    自动登记模式下（无访问码）返回本机 Cookie 对应的编号与阶段，页面据此显示流程位置；
    未启用访问码时返回 `access_required=false`，由自动登记或手填编号处理。
    """
    if not access.access_required():
        out = {"access_required": False}
        if request is not None and enroll.enabled():
            pid = enroll.resolve(request)
            if pid:
                out.update({"participant_id": pid, "batch": enroll._batch(),
                            "phase": flow.current(pid, enroll._batch()),
                            "phase_label": flow.LABELS.get(flow.current(pid, enroll._batch()), "")})
        return out
    claims = _claims(x_access_code)          # 缺失/伪造/过期/批次不符都在这里 403
    if claims is None:
        raise HTTPException(status_code=403, detail={
            "status": "access_denied", "code": "missing_code",
            "message": "请先填写实验访问码。",
        })
    return {"access_required": True,
            "participant_id": claims.participant_id,
            "batch": claims.batch,
            "phases": list(claims.phases),
            "expires_at": claims.expires_at}


@app.get("/api/questions")
def api_questions(request: Request = None, x_access_code: str = Header(default=None)) -> list[dict]:
    """题目列表：带有效访问码时**只返回该批次允许的阶段**（服务端约束，不靠客户端传 phase）。

    自动登记模式（匿名会话）下只返回**当前阶段**的题目：前测→练习→后测的顺序由服务端锁定。
    """
    claims = _claims(x_access_code)
    items = list_questions()
    if claims is not None:
        items = [q for q in items if claims.allows_phase(q.get("phase", ""))]
    if request is not None and enroll.enabled():
        pid = enroll.resolve(request)
        if pid:
            now = flow.current(pid, enroll._batch())
            items = [q for q in items if q.get("phase") == now]
    # 负责人在管理台"发题"后，学生只看到已发布的题目；发布表从未用过时按阶段全可见（本地联调）。
    published = admin.published_ids()
    if published is not None:
        items = [q for q in items if q["id"] in published]
    # 顺序：阶段（前测→练习→后测）→ 管理台设定的 sort_order → 题号
    order = admin.publish_order()
    rank = {"pretest": 0, "practice": 1, "posttest": 2}
    items.sort(key=lambda q: (rank.get(q.get("phase"), 9), order.get(q["id"], 0) or 0, q["id"]))
    return [{"id": q["id"], "title": q["title"], "difficulty": q.get("difficulty"),
             "phase": q.get("phase")}
            for q in items]


@app.get("/api/components")
def api_components() -> dict:
    """当前已注册的教学组件。演示"可插拔"用：注册表里有什么，就返回什么。"""
    return {"registered": registry.names()}


@app.get("/admin", response_class=HTMLResponse)
def admin_page() -> str:
    """负责人本机管理台页面（数据接口还需 X-Admin-Token；未配置令牌时 404）。"""
    if not admin.enabled():
        raise HTTPException(status_code=404, detail={"status": "admin_disabled"})
    return (WEB_DIR / "admin.html").read_text(encoding="utf-8")


@app.get("/api/status")
def api_status() -> dict:
    """前端**展示**用：判题器当前是"真实现"还是"桩"，以及**执行适配器是否接入**。

    判据是**只读**的：看 `app/judge/runner.py` 的 `judge()` 源码里是否仍带桩标记——
    **不执行判题**（在 GET 上真跑学生代码既慢又危险），也**不改**岗位③的文件。
    `isolation` 只有三种取值：`not_configured`（没登记适配器）、`registered_unverified`
    （登记了但自检没过：health/握手/能力位任一不满足）、`ready`（自检通过）。
    **`ready` 仍不等于四类隔离已验收**，它只表示适配器自认为可用。
    """
    import inspect

    from app.harness import isolation

    from app.judge import runner

    try:
        src = inspect.getsource(runner.judge)
    except Exception:
        src = ""
    stub = "桩" in src
    adapter = isolation.registered_execution_adapter()
    state = _isolation_state(adapter)
    return {"judge": "stub" if stub else "real",
            "isolation": state,
            "note": "当前为模拟判题，结果不代表真实代码执行。" if stub else "",
            "isolation_note": _ISOLATION_NOTES[state],
            "model": "configured" if _llm.is_configured() else "not_configured",
            "debug": _debug_allowed(),
            "version": APP_VERSION}


@app.get("/api/questions/{question_id}")
def api_question(question_id: str, request: Request = None,
                 x_access_code: str = Header(default=None)) -> dict:
    q = load_question(question_id)
    claims = _claims(x_access_code)
    if claims is not None:
        _require_phase_allowed(question_id, claims)
    _require_published(question_id)
    if request is not None and enroll.enabled():
        pid = enroll.resolve(request)
        if pid:
            _require_flow_phase(question_id, pid)
    # 不下发参考解与评测用例：题面示例走 examples，用例一律提交后才在判题结果里体现
    q.pop("reference_solution", None)
    q.pop("test_cases", None)
    return q


def _require_published(question_id: str) -> None:
    """发布名单一旦启用（表里已有记录），所有入口必须一致生效。

    只过滤列表等于「看不见但调得动」：知道题号仍能读题、提交、请求提示。
    """
    published = admin.published_ids()
    if published is not None and question_id not in published:
        raise HTTPException(status_code=403, detail={
            "status": "question_not_published",
            "message": "该题目不在本轮发布名单中，本次请求未执行。",
        })


def _require_model_ready() -> None:
    """实验模式下缺模型 ⇒ 明确报未就绪；不得用占位文本冒充真实提示。"""
    from app import llm

    if llm.experiment_mode() and not llm.is_configured():
        raise HTTPException(status_code=503, detail={
            "status": "model_not_configured",
            "message": "教学模型未配置：本次提示未生成，也不会记为模型成功（不计入提示等级）。",
        })


def _isolation_refusal(exc, *, hint_requested: bool) -> dict:
    """**隔离不可用时的学生可见返回**：明确的"未执行"，不伪装成运行时错误或判题失败。"""
    from app.harness import isolation

    return {
        "status": "judge_unavailable",
        "failed_cases": [], "stderr": "", "error_type": "",
        "error_class": "", "knowledge_point": "",
        "hint_level": 0, "unlocked_level": 0,
        "hint_requested": 1 if hint_requested else 0,
        "memory_hint": "", "message": ISOLATION_NOT_READY_MESSAGE, "judge_feedback": "",
        "source": "isolation_unavailable",
        "components": {"executed": [], "skipped": []},
        "log": None,
        # 受控码（有限集合），用于运维定位；不含异常原文、不含学生代码
        "detail_code": getattr(exc, "detail_code", isolation.UNREGISTERED_DETAIL_CODE),
    }


@app.post("/api/enroll")
def api_enroll(request: Request, response: Response) -> dict:
    """新电脑自动登记：服务端分配编号并下发 HttpOnly Cookie。

    只在 `PH_AUTO_ENROLL=1` 且未启用访问码强制时可用——用访问码的批次，身份由码决定。
    """
    if not enroll.enabled():
        raise HTTPException(status_code=404, detail={"status": "enroll_disabled",
                                                     "message": "本服务未开启自动登记"})
    if access.access_required():
        raise HTTPException(status_code=409, detail={
            "status": "access_code_mode",
            "message": "本批次使用访问码固定身份，不需要也不允许自动分配编号。",
        })
    return enroll.enroll(request, response)


@app.post("/api/phase/advance")
def api_phase_advance(request: Request) -> dict:
    """结束当前阶段（可保留未通过与未完成题目），进入下一阶段；上一阶段随即锁定。"""
    if not enroll.enabled():
        raise HTTPException(status_code=404, detail={"status": "flow_disabled",
                                                     "message": "本服务未启用分阶段流程"})
    pid = enroll.resolve(request)
    if not pid:
        raise HTTPException(status_code=403, detail={
            "status": "session_required", "code": "session_missing",
            "message": "页面会话尚未建立，请刷新页面后重试。",
        })
    return flow.advance(pid, enroll._batch())


@app.post("/api/submit")
def api_submit(req: SubmitRequest, request: Request = None,
               x_access_code: str | None = Header(default=None, alias=access.HEADER_NAME)) -> dict:
    from app.harness import isolation

    claims = _claims(x_access_code)
    participant_id = _participant_of(req.participant_id, claims, request)
    _require_phase_allowed(req.question_id, claims)
    _require_published(req.question_id)
    _require_flow_phase(req.question_id, participant_id)
    if req.request_hint:
        _require_model_ready()
    _enter_single_lane()
    try:
        out = handle_submission(
            participant_id=participant_id,
            question_id=req.question_id,
            code=req.code,
            config=_config(req.group),
            request_hint=req.request_hint,
        )
        if not _debug_allowed():
            # 正式/实验模式下不把组件轨迹发给浏览器：隐藏按钮只是界面，数据也要在服务端去掉
            out.pop("log", None)
        return out
    except isolation.IsolationUnavailable as exc:
        return _isolation_refusal(exc, hint_requested=req.request_hint)
    finally:
        _SINGLE_LANE.release()


@app.post("/api/hint")
def api_hint(req: HintRequest, request: Request = None,
             x_access_code: str | None = Header(default=None, alias=access.HEADER_NAME)) -> dict:
    """学生**只想看提示、代码没改**：走同一套教学组件 + 防泄校验，但**不写日志**。

    这样"提交次数 / 首次通过率"不会被"只为看提示"的动作虚增（契约 §3 的 `submit_seq` 只由真提交推进）。
    **同一道安全门**：隔离不可用 ⇒ 同样拒绝执行、返回"未就绪"，不启动学生进程。
    访问码无效/身份不符/阶段不符 ⇒ 同样在判题与模型调用之前拒绝。
    """
    from app.harness import isolation

    claims = _claims(x_access_code)
    participant_id = _participant_of(req.participant_id, claims, request)
    _require_phase_allowed(req.question_id, claims)
    _require_published(req.question_id)
    _require_flow_phase(req.question_id, participant_id)
    _require_model_ready()
    _enter_single_lane()
    try:
        return handle_hint_request(
            participant_id=participant_id,
            question_id=req.question_id,
            code=req.code,
            config=PRESETS["exp"],
            hint_request_id=req.hint_request_id,
            hint_batch=(claims.batch if claims is not None else ""),
        )
    except isolation.IsolationUnavailable as exc:
        return _isolation_refusal(exc, hint_requested=True)
    finally:
        _SINGLE_LANE.release()

