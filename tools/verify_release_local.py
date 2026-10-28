"""本机受控上线核验（真实 HTTP，不写正式库）。

前置：服务已用验收库启动，且环境为
    PH_VM_AGENT_ENABLED=1  PH_REQUIRE_ACCESS_CODE=1  PH_ALLOW_GROUP_SWITCH=0
    PH_EXPERIMENT_BATCH=<本脚本同值>  PH_ACCESS_SECRET_FILE=<受保护文件>

用法：
    set PYTHONIOENCODING=utf-8
    python tools/verify_release_local.py --base http://127.0.0.1:8010 \
        --db data/experiment_acceptance_20261028.sqlite --batch ACCEPT-20261028

只使用测试身份（T00x），不签发真实参与者凭证；访问码只在内存里用，不打印、不落盘。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

RESULTS: list[dict] = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + ((" — " + detail) if detail else ""))


def _post(base: str, path: str, body: dict, code: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    if code is not None:
        req.add_header("X-Access-Code", code)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return exc.code, payload


def _counts(db_path: str) -> dict:
    out = {}
    with sqlite3.connect(db_path) as conn:
        for table, key in (("submission_log", "submissions"), ("event_log", "events"),
                           ("error_memory", "error_memory")):
            try:
                out[key] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                out[key] = -1
        try:
            row = conn.execute("SELECT model_ms FROM event_log WHERE event_type='hint' "
                               "ORDER BY event_id DESC LIMIT 1").fetchone()
            out["last_hint_model_ms"] = row[0] if row else None
        except sqlite3.Error:
            out["last_hint_model_ms"] = None
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8010")
    ap.add_argument("--db", required=True, help="验收库绝对/相对路径（只读统计用）")
    ap.add_argument("--batch", required=True)
    args = ap.parse_args()

    from app import access

    good = access.issue_code("T001", batch=args.batch, phases=("practice",), ttl_seconds=1800)
    pretest = access.issue_code("T001", batch=args.batch, phases=("pretest",), ttl_seconds=1800)
    other_pid = access.issue_code("T002", batch=args.batch, phases=("practice",), ttl_seconds=1800)
    expired = access.issue_code("T001", batch=args.batch, phases=("practice",), ttl_seconds=-120)
    wrong_batch = access.issue_code("T001", batch=args.batch + "-OLD", phases=("practice",),
                                    ttl_seconds=1800)
    forged = good[:-2] + ("AA" if good[-2:] != "AA" else "BB")

    body = {"participant_id": "T001", "question_id": "P001", "code": "print(1)"}

    # 1) 缺失/伪造/过期/身份不符/错误批次 —— 全部在判题之前拒绝
    for name, code, expect_code in (
        ("no_code", None, "missing_code"),
        ("forged_code", forged, "bad_signature"),
        ("expired_code", expired, "expired_code"),
        ("identity_mismatch", other_pid, "identity_mismatch"),
    ):
        status, payload = _post(args.base, "/api/submit", body, code)
        detail = (payload.get("detail") or {}) if isinstance(payload, dict) else {}
        _record(f"access_{name}", status == 403 and detail.get("code") == expect_code,
                f"status={status} code={detail.get('code')}")

    status, payload = _post(args.base, "/api/submit", body, wrong_batch)
    detail = (payload.get("detail") or {}) if isinstance(payload, dict) else {}
    _record("access_wrong_batch", status == 403 and detail.get("status") == "batch_mismatch",
            f"status={status} status_field={detail.get('status')}")

    # 2) 阶段越权：前测码不能提交练习题目
    status, payload = _post(args.base, "/api/submit", body, pretest)
    detail = (payload.get("detail") or {}) if isinstance(payload, dict) else {}
    _record("phase_not_allowed", status == 403 and detail.get("status") == "phase_not_allowed",
            f"status={status} phase={detail.get('phase')}")

    # 3) 前测题目不能拿提示（且不应该调用模型/判题）
    before = _counts(args.db)
    status, payload = _post(args.base, "/api/hint",
                            {"participant_id": "T001", "question_id": "S001", "code": "x=1"},
                            pretest)
    after = _counts(args.db)
    _record("pretest_hint_disabled",
            status == 200 and payload.get("status") == "phase_disabled",
            f"status={status} field={payload.get('status')}")
    _record("pretest_hint_no_model_no_submission",
            after["submissions"] == before["submissions"]
            and after["last_hint_model_ms"] is None,
            f"events {before['events']}->{after['events']} submissions {before['submissions']}->{after['submissions']} model_ms={after['last_hint_model_ms']}")

    # 4) 练习阶段提示可用、等级 1、model_ms 有值
    status, first = _post(args.base, "/api/hint",
                          {"participant_id": "T001", "question_id": "P001",
                           "code": "n=int(input())\nprint(n)", "hint_request_id": "acc-rid-1"},
                          good)
    _record("practice_hint_level1", status == 200 and first.get("hint_level") == 1,
            f"status={status} level={first.get('hint_level')} source={first.get('source')}")
    stats = _counts(args.db)
    _record("hint_model_ms_recorded", isinstance(stats["last_hint_model_ms"], int),
            f"model_ms={stats['last_hint_model_ms']}")

    # 5) 同一个请求号重复到达：回放，不再升级、不再调模型
    events_before = stats["events"]
    status, again = _post(args.base, "/api/hint",
                          {"participant_id": "T001", "question_id": "P001",
                           "code": "n=int(input())\nprint(n)", "hint_request_id": "acc-rid-1"},
                          good)
    stats2 = _counts(args.db)
    _record("duplicate_request_replays",
            status == 200 and bool(again.get("replayed")) and again.get("hint_level") == first.get("hint_level"),
            f"status={status} replayed={again.get('replayed')} level={again.get('hint_level')}")
    _record("duplicate_request_no_extra_event", stats2["events"] == events_before,
            f"events {events_before}->{stats2['events']}")

    # 6) 换代码复用旧请求号：不能拿旧提示冒充
    status, changed = _post(args.base, "/api/hint",
                            {"participant_id": "T001", "question_id": "P001",
                             "code": "print('changed')", "hint_request_id": "acc-rid-1"},
                            good)
    _record("replay_rejected_when_code_changed",
            status == 200 and not changed.get("replayed"),
            f"status={status} replayed={changed.get('replayed')} level={changed.get('hint_level')}")

    # 7) 别的参与者拿同一请求号：不能读到别人的缓存
    status, other = _post(args.base, "/api/hint",
                          {"participant_id": "T002", "question_id": "P001",
                           "code": "n=int(input())\nprint(n)", "hint_request_id": "acc-rid-1"},
                          other_pid)
    _record("replay_isolated_across_participants",
            status == 200 and not other.get("replayed") and other.get("hint_level") == 1,
            f"status={status} replayed={other.get('replayed')} level={other.get('hint_level')}")

    # 8) 正常提交：参考答案通过，说明整条判题链路（VM agent）在核验模式下可用
    qpath = REPO / "data" / "questions" / "P001.json"
    ref = json.loads(qpath.read_text(encoding="utf-8"))["reference_solution"]
    status, passed = _post(args.base, "/api/submit",
                           {"participant_id": "T002", "question_id": "P001", "code": ref},
                           other_pid)
    _record("judge_pass_via_vm", status == 200 and passed.get("status") == "pass",
            f"status={status} judge={passed.get('status')} error_type={passed.get('error_type')}")

    # 9) 未知题目不应把单路锁卡死：异常路径后仍能正常请求
    status, _ = _post(args.base, "/api/submit",
                      {"participant_id": "T002", "question_id": "NOPE", "code": "print(1)"},
                      other_pid)
    status2, after_ok = _post(args.base, "/api/submit",
                              {"participant_id": "T002", "question_id": "P001", "code": ref},
                              other_pid)
    _record("lock_released_after_bad_request", status2 == 200 and after_ok.get("status") == "pass",
            f"first={status} second={status2}")

    # 10) 忙碌拒绝：慢提交挂住期间，第二个请求应 429 且不执行
    slow = "while True:\n    pass\n"
    t = threading.Thread(target=_post, args=(args.base, "/api/submit",
                                             {"participant_id": "T002", "question_id": "P001",
                                              "code": slow}, other_pid))
    t.start()
    time.sleep(0.6)
    busy_status, busy_payload = _post(args.base, "/api/submit",
                                      {"participant_id": "T002", "question_id": "P001",
                                       "code": ref}, other_pid)
    t.join(timeout=40)
    detail = (busy_payload.get("detail") or {}) if isinstance(busy_payload, dict) else {}
    _record("busy_rejected", busy_status == 429 and detail.get("status") == "busy",
            f"status={busy_status} field={detail.get('status')}")

    ok = sum(1 for r in RESULTS if r["ok"])
    total = len(RESULTS)
    print(f"\n{ok}/{total} checks passed")
    out = REPO / "docs" / "G14_本机核验结果.json"
    out.write_text(json.dumps({"base": args.base, "db": args.db, "batch": args.batch,
                               "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                               "checks": RESULTS, "passed": ok, "total": total},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report -> {out}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())


