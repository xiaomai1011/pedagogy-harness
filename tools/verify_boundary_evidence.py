"""四项边界核验的第 3、4 项真实环境证据（2026-10-28）。

只做两件事：
  1）实验模式下用真实模型跑一次提示，记录来源/耗时/是否占位文本；
  2）用 5 个慢用例（每个 2.4s，总计约 12s）验证宿主等待与整任务预算对齐：
     旧实现 request_timeout=10s 会提前掐断，新实现应等待到结果。
结果写到 docs/G16_四项边界核验.json。
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for key in ("NO_PROXY", "no_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
    os.environ.pop(key, None)

tmp_db = Path(tempfile.gettempdir()) / "ph_boundary_evidence.sqlite"
if tmp_db.exists():
    tmp_db.unlink()
os.environ["PH_DB"] = str(tmp_db)
os.environ["PH_EXPERIMENT_MODE"] = "1"
os.environ.pop("PH_REQUIRE_ACCESS_CODE", None)

from app import db, llm  # noqa: E402
from app.harness import isolation  # noqa: E402
from app.main import app  # noqa: E402
from app.harness.vm_agent_adapter import VmAgentAdapter  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TOKEN_PATH = Path(r"C:\Users\45181\AppData\Local\pedagogy-harness-launcher\vmkey\ph_judge_agent_token")
token = TOKEN_PATH.read_text(encoding="utf-8").strip()
# 和正式启动一致：显式登记 VM 适配器（脚本进程没有走 main 的启动钩子）
isolation.register_execution_adapter(VmAgentAdapter(base_url="http://192.168.200.10:9443", token=token))

report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "items": {}}

# 1) 真实模型提示（走 HTTP 接口，和浏览器同一条路径）
started = time.time()
client = TestClient(app)
resp = client.post("/api/hint", json={
    "participant_id": "EVID-MODEL", "question_id": "P001", "code": "print(1)"}).json()
elapsed = int((time.time() - started) * 1000)
with db.connect() as conn:
    row = conn.execute(
        "SELECT hint_source, model_ms, status FROM event_log WHERE participant_id=? AND event_type='hint'"
        " ORDER BY event_id DESC LIMIT 1", ("EVID-MODEL",)).fetchone()
report["items"]["model_hint"] = {
    "http_path": "pipeline.handle_hint_request",
    "hint_level": resp.get("hint_level"),
    "source_recorded": row[0] if row else None,
    "model_ms_recorded": row[1] if row else None,
    "status": row[2] if row else None,
    "request_ms": elapsed,
    "text_prefix": (resp.get("message") or "")[:40],
    "looks_like_stub": "[桩" in (resp.get("message") or ""),
    "llm_configured": llm.is_configured(),
}

# 2) 整任务预算：5 个用例 × 2.4s ≈ 12s > 旧 10s 超时
adapter = isolation.registered_execution_adapter()
code = "import time\ntime.sleep(2.4)\nprint(1)\n"
payload = {
    "code": code,
    "question": {"id": "EVID-SLOW", "test_cases": [
        {"input": "", "expected": "1", "hidden": False} for _ in range(5)]},
    "timeout_seconds": 5,
}
started = time.time()
result = adapter.start(payload)
elapsed = int((time.time() - started) * 1000)
report["items"]["budget_5cases"] = {
    "cases": 5,
    "per_case_sleep_s": 2.4,
    "elapsed_ms": elapsed,
    "old_request_timeout_s": 10.0,
    "budget_timeout_s": adapter._budget_timeout(adapter._build_request(payload)),
    "status": result.status,
    "passed_cases": result.passed_cases,
    "total_cases": result.total_cases,
    "exec_ms": result.exec_ms,
}

out = ROOT / "docs" / "G16_四项边界核验.json"
out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
