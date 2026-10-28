"""FastAPI 入口。托管静态前端 + 提供提交/请求提示接口。

启动：uvicorn app.main:app --reload
"""

import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import db
from app.harness import builtin  # noqa: F401  —— 导入即注册内置组件
from app.harness import registry
from app.harness.config_switch import PRESETS
from app.harness.pipeline import handle_hint_request, handle_submission
from app.harness.vm_agent_adapter import register_vm_agent_adapter
from app.questions import list_questions, load_question

app = FastAPI(title="pedagogy-harness")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
VENDOR_DIR = WEB_DIR / "vendor"

db.init_db()

# 正式实验统一走 `exp`（契约 §5）。只有现场演示/联调才允许切预设：
# 设环境变量 `PH_ALLOW_GROUP_SWITCH=1` 才放行 `group` 字段，否则一律按 exp 记。
ALLOW_GROUP_SWITCH = os.environ.get("PH_ALLOW_GROUP_SWITCH") == "1"

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
    """按运维开关登记 VM 适配器；任何失败都保持未登记，**绝不降级到宿主执行**。"""
    if not VM_AGENT_ENABLED:
        return False
    try:
        adapter = register_vm_agent_adapter()
    except Exception as exc:  # noqa: BLE001 —— 登记失败只记日志，进程继续 fail closed
        _log.warning("VM agent 适配器登记失败，保持未登记：%s", exc)
        return False
    _log.info("已登记 VM agent 适配器：%s", adapter.describe()["url"])
    return True


VM_AGENT_REGISTERED = _register_configured_vm_agent()

#: `/api/status` 的就绪探测缓存：只打 `/healthz` + 握手，**不启动任何执行**。
_ISOLATION_PROBE = {"at": 0.0, "adapter_id": None, "state": "unknown"}
_ISOLATION_PROBE_TTL = 15.0

_ISOLATION_NOTES = {
    "not_configured": ISOLATION_NOT_READY_MESSAGE,
    "registered_unverified": (
        "判题环境已接入适配器，但就绪检查未通过：提交会被拒绝并记录为“未执行”，"
        "不会运行任何学生代码。"
    ),
    "ready": "",
}


def _isolation_state(adapter) -> str:
    """返回 `not_configured` / `registered_unverified` / `ready`。

    **登记 ≠ 就绪**：只有适配器自检（health + 握手 + 能力位）通过才算 `ready`；
    探测失败一律按未就绪处理。结果按适配器身份缓存 15 秒，避免每次刷新都打网络。
    """
    if adapter is None:
        return "not_configured"
    now = time.monotonic()
    cached = _ISOLATION_PROBE
    if (cached["adapter_id"] == id(adapter)
            and cached["state"] != "unknown"
            and now - cached["at"] < _ISOLATION_PROBE_TTL):
        return cached["state"]
    try:
        state = "ready" if adapter.check_isolation() else "registered_unverified"
    except Exception:  # noqa: BLE001 —— 探测失败就是未就绪，不能冒充可用
        state = "registered_unverified"
    _ISOLATION_PROBE.update(at=now, adapter_id=id(adapter), state=state)
    return state

# 前端已隐藏分组下拉，这里再兜一层——挡住手工构造的改包请求，避免污染实验数据。
if VENDOR_DIR.exists():
    app.mount("/vendor", StaticFiles(directory=str(VENDOR_DIR)), name="vendor")


def _config(group: str):
    if not ALLOW_GROUP_SWITCH:
        return PRESETS["exp"]
    return PRESETS.get(group, PRESETS["exp"])


class SubmitRequest(BaseModel):
    participant_id: str
    question_id: str
    code: str
    group: str = "exp"          # 仅供联调/演示；默认被服务端忽略，一律按 exp 记
    request_hint: bool = False  # 学生是否主动请求提示


class HintRequest(BaseModel):
    participant_id: str
    question_id: str
    code: str = ""              # 当前编辑器里的代码，供大模型看到"学生卡在哪"


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/questions")
def api_questions() -> list[dict]:
    return [{"id": q["id"], "title": q["title"], "difficulty": q.get("difficulty"),
             "phase": q.get("phase")}
            for q in list_questions()]


@app.get("/api/components")
def api_components() -> dict:
    """当前已注册的教学组件。演示"可插拔"用：注册表里有什么，就返回什么。"""
    return {"registered": registry.names()}


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
            "isolation_note": _ISOLATION_NOTES[state]}


@app.get("/api/questions/{question_id}")
def api_question(question_id: str) -> dict:
    q = load_question(question_id)
    # 不下发参考解与隐藏用例
    q.pop("reference_solution", None)
    q["test_cases"] = [c for c in q.get("test_cases", []) if not c.get("hidden")]
    return q


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


@app.post("/api/submit")
def api_submit(req: SubmitRequest) -> dict:
    from app.harness import isolation

    try:
        return handle_submission(
            participant_id=req.participant_id,
            question_id=req.question_id,
            code=req.code,
            config=_config(req.group),
            request_hint=req.request_hint,
        )
    except isolation.IsolationUnavailable as exc:
        return _isolation_refusal(exc, hint_requested=req.request_hint)


@app.post("/api/hint")
def api_hint(req: HintRequest) -> dict:
    """学生**只想看提示、代码没改**：走同一套教学组件 + 防泄校验，但**不写日志**。

    这样"提交次数 / 首次通过率"不会被"只为看提示"的动作虚增（契约 §3 的 `submit_seq` 只由真提交推进）。
    **同一道安全门**：隔离不可用 ⇒ 同样拒绝执行、返回"未就绪"，不启动学生进程。
    """
    from app.harness import isolation

    try:
        return handle_hint_request(
            participant_id=req.participant_id,
            question_id=req.question_id,
            code=req.code,
            config=PRESETS["exp"],
        )
    except isolation.IsolationUnavailable as exc:
        return _isolation_refusal(exc, hint_requested=True)
