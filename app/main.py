"""FastAPI 入口。托管静态前端 + 提供提交接口。

启动：uvicorn app.main:app --reload
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app import db
from app.harness import builtin  # noqa: F401  —— 导入即注册内置组件
from app.harness import registry
from app.harness.config_switch import PRESETS
from app.harness.pipeline import handle_submission
from app.questions import list_questions, load_question

app = FastAPI(title="pedagogy-harness")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

db.init_db()


class SubmitRequest(BaseModel):
    participant_id: str
    question_id: str
    code: str
    group: str = "exp"          # exp / ctrl / abl_hint / abl_cls / abl_mem
    request_hint: bool = False  # 学生是否主动请求提示


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/questions")
def api_questions() -> list[dict]:
    return [{"id": q["id"], "title": q["title"], "difficulty": q.get("difficulty")}
            for q in list_questions()]


@app.get("/api/components")
def api_components() -> dict:
    """当前已注册的教学组件。演示"可插拔"用：注册表里有什么，就返回什么。"""
    return {"registered": registry.names()}


@app.get("/api/questions/{question_id}")
def api_question(question_id: str) -> dict:
    q = load_question(question_id)
    # 不下发参考解与隐藏用例
    q.pop("reference_solution", None)
    q["test_cases"] = [c for c in q.get("test_cases", []) if not c.get("hidden")]
    return q


@app.post("/api/submit")
def api_submit(req: SubmitRequest) -> dict:
    config = PRESETS.get(req.group, PRESETS["exp"])
    return handle_submission(
        participant_id=req.participant_id,
        question_id=req.question_id,
        code=req.code,
        config=config,
        request_hint=req.request_hint,
    )
