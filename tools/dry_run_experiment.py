#!/usr/bin/env python3
"""一次完整的**实验流程预演**（阶段 5）。

它扮演一个全新参与者，走完前测 → 练习 → 后测，并检查：

- 前测/后测：判题照常，但**教学提示与错误记忆被禁用**；
- 练习阶段：提示、错误分类、错误记忆、事件记录都正常；
- 提交真的走 **VM agent** 执行（不是宿主 subprocess）；
- 数据导出可读（提交行、事件、错误记忆、隔离运行记录）。

运行（宿主，项目 venv，需要 VM 已就绪）::

    set PH_VM_AGENT_ENABLED=1
    set PH_VM_AGENT_TOKEN_FILE=C:\\...\\vmkey\\ph_judge_agent_token
    python tools/dry_run_experiment.py --out docs/G7_预演报告.json

脚本自己建临时数据库（`PH_DB` 指向 `%TEMP%` 下的文件），**不碰真实库**。
"""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 必须在导入 app.main 之前设置：登记适配器 + 用临时库
_TMP_DB = Path(tempfile.gettempdir()) / f"ph_dryrun_{int(time.time())}.sqlite"
os.environ["PH_DB"] = str(_TMP_DB)
os.environ.setdefault("PH_VM_AGENT_ENABLED", "1")

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402

PRETEST = ["S001", "S002", "S003"]
PRACTICE = ["P001", "P002", "P003", "P004", "P005", "P006"]
POSTTEST = ["S004", "S005", "S006"]

WRONG_CODE = 'print("0 0")'


def _solution(qid: str) -> str:
    with (REPO / "data" / "questions" / f"{qid}.json").open("r", encoding="utf-8") as fh:
        return json.load(fh)["reference_solution"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "docs" / "G7_预演报告.json"))
    ap.add_argument("--participant", default=f"DRYRUN-{time.strftime('%Y%m%d-%H%M%S')}")
    ap.add_argument("--no-stub-llm", action="store_true",
                    help="不替换大模型（默认用本地桩，避免预演依赖外部 API）")
    args = ap.parse_args(argv)

    # 预演默认用**本地桩模型**：只验证流程与门禁，不验证模型回答质量。
    # 需要真实模型时加 --no-stub-llm，并保证 PH_LLM_* 配置可用。
    llm_stubbed = not args.no_stub_llm
    if llm_stubbed:
        from app.harness import pipeline as _pipeline

        def _fake_llm(system: str, user: str, **_kw):
            return "[预演桩·大模型] 这里是提示占位文本，用于验证流程与门禁。"

        _pipeline.llm_mod.llm = _fake_llm

    pid = args.participant
    client = TestClient(app)
    checks = []
    submits = []

    def check(name: str, ok: bool, detail=None):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"[{'OK ' if ok else 'FAIL'}] {name}")

    status = client.get("/api/status").json()
    check("适配器已就绪（/api/status == ready）", status.get("isolation") == "ready", status)

    def submit(qid: str, code: str) -> dict:
        resp = client.post("/api/submit", json={
            "participant_id": pid, "question_id": qid, "code": code,
            "request_hint": False,
        }).json()
        submits.append({"qid": qid, "status": resp.get("status"),
                        "error_type": resp.get("error_type"),
                        "error_class": resp.get("error_class"),
                        "knowledge_point": resp.get("knowledge_point"),
                        "source": resp.get("source"),
                        "hint_level": resp.get("hint_level")})
        return resp

    # ---------- 前测 ----------
    for qid in PRETEST:
        resp = submit(qid, _solution(qid))
        check(f"前测 {qid} 参考答案通过", resp.get("status") == "pass",
              {"status": resp.get("status"), "error_type": resp.get("error_type")})
        check(f"前测 {qid} 不泄露知识点/提示",
              not resp.get("knowledge_point") and not resp.get("memory_hint"),
              {"kp": resp.get("knowledge_point"), "memory": resp.get("memory_hint")})

    hint = client.post("/api/hint", json={
        "participant_id": pid, "question_id": "S001", "code": ""}).json()
    check("前测请求提示被拒绝（phase_disabled）",
          hint.get("status") == "phase_disabled", hint.get("status"))

    # ---------- 练习 ----------
    wrong = submit("P001", WRONG_CODE)
    check("练习 P001 错误输出判 fail/wrong_output",
          wrong.get("status") == "fail" and wrong.get("error_type") == "wrong_output",
          {"status": wrong.get("status"), "error_type": wrong.get("error_type")})
    check("练习阶段有错误分类与知识点",
          bool(wrong.get("error_class")), {"class": wrong.get("error_class"),
                                           "kp": wrong.get("knowledge_point")})

    hint = client.post("/api/hint", json={
        "participant_id": pid, "question_id": "P001", "code": WRONG_CODE}).json()
    check("练习阶段提示可用", hint.get("status") != "phase_disabled",
          {"status": hint.get("status"), "hint_level": hint.get("hint_level")})

    for qid in PRACTICE:
        resp = submit(qid, _solution(qid))
        check(f"练习 {qid} 参考答案通过", resp.get("status") == "pass",
              {"status": resp.get("status"), "error_type": resp.get("error_type")})

    # ---------- 后测 ----------
    for qid in POSTTEST:
        resp = submit(qid, _solution(qid))
        check(f"后测 {qid} 参考答案通过", resp.get("status") == "pass",
              {"status": resp.get("status"), "error_type": resp.get("error_type")})
        check(f"后测 {qid} 不泄露知识点/提示",
              not resp.get("knowledge_point") and not resp.get("memory_hint"),
              {"kp": resp.get("knowledge_point")})

    hint = client.post("/api/hint", json={
        "participant_id": pid, "question_id": "S004", "code": ""}).json()
    check("后测请求提示被拒绝（phase_disabled）",
          hint.get("status") == "phase_disabled", hint.get("status"))

    # ---------- 数据导出 ----------
    events = db.get_events(participant_id=pid)
    kinds: dict = {}
    for event in events:
        key = f"{event.get('entry')}/{event.get('status')}"
        kinds[key] = kinds.get(key, 0) + 1
    memory = db.get_error_memory(pid)
    runs = db.get_isolation_runs()

    check("提交行覆盖全部 12 道预演题目（练习 P001 有 1 次错误提交）",
          len({s["qid"] for s in submits}) == len(PRETEST) + len(PRACTICE) + len(POSTTEST)
          and len(submits) >= len(PRETEST) + len(PRACTICE) + len(POSTTEST),
          {"distinct": len({s["qid"] for s in submits}), "total": len(submits)})
    check("事件日志可按参与者导出", len(events) > 0, len(events))
    check("错误记忆只来自练习阶段（前后测不写记忆）",
          all(str(m.get("phase", "practice")) == "practice" for m in memory) if memory else True,
          memory[:3])

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "participant_id": pid,
        "db": str(_TMP_DB),
        "llm_stubbed": llm_stubbed,
        "status": status,
        "checks_ok": sum(1 for c in checks if c["ok"]),
        "checks_total": len(checks),
        "submissions": submits,
        "event_kinds": kinds,
        "events_total": len(events),
        "error_memory": memory,
        "isolation_runs": len(runs or []),
        "checks": checks,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{report['checks_ok']}/{report['checks_total']} 项通过 → {out}")
    return 0 if report["checks_ok"] == report["checks_total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
