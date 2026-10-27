"""FastAPI 入口。托管静态前端 + 提供提交/请求提示接口。

启动：uvicorn app.main:app --reload
"""

import os
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
    `isolation` 只看进程内是否登记了执行适配器，**不发探测请求、不启动 VM**；
    因此 `configured` 只表示"接了适配器"，**不等于**四类隔离已验收。
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
    return {"judge": "stub" if stub else "real",
            "isolation": "configured" if adapter is not None else "not_configured",
            "note": "当前为模拟判题，结果不代表真实代码执行。" if stub else "",
            "isolation_note": "" if adapter is not None else ISOLATION_NOT_READY_MESSAGE}


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
