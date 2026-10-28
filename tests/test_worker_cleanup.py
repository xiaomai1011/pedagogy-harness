"""清理与降级闸门的单元测试（工人脚本在 vm_agent/ 下，不在包内）。"""
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# worker 是 Linux 侧脚本（用 grp/pwd/resource），在 Windows 上跑单测时给最小替身。
for _name, _attrs in (("grp", {"getgrnam": lambda n: types.SimpleNamespace(gr_gid=1001)}),
                      ("pwd", {"getpwnam": lambda n: types.SimpleNamespace(pw_uid=999)})):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.__dict__.update(_attrs)
        sys.modules[_name] = _mod
if "resource" not in sys.modules:
    _res = types.ModuleType("resource")
    _res.RLIMIT_CPU = 0
    _res.RLIMIT_AS = 1
    _res.RLIMIT_FSIZE = 2
    _res.RLIMIT_NPROC = 3
    _res.RLIMIT_CORE = 4
    _res.setrlimit = lambda *a: None
    sys.modules["resource"] = _res

_SPEC = importlib.util.spec_from_file_location(
    "ph_judge_worker", ROOT / "vm_agent" / "ph_judge_worker.py")
worker = importlib.util.module_from_spec(_SPEC)
sys.modules["ph_judge_worker"] = worker
_SPEC.loader.exec_module(worker)


def test_purge_removes_files_left_outside_job_dir(tmp_path):
    """学生代码把文件写到工作根目录（逃出 tmpdir）也要被清掉。"""
    (tmp_path / "job-abc").mkdir()
    (tmp_path / "job-abc" / "main.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "leak_marker.txt").write_text("leftover", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "deep.txt").write_text("x", encoding="utf-8")

    assert worker._purge_work_root(str(tmp_path)) == ""
    assert os.listdir(tmp_path) == []


def test_purge_reports_failure_when_something_cannot_be_removed(tmp_path, monkeypatch):
    (tmp_path / "stuck").write_text("x", encoding="utf-8")

    def boom(path):
        raise OSError("denied")

    monkeypatch.setattr(worker.os, "unlink", boom)
    assert worker._purge_work_root(str(tmp_path)).startswith("rm_failed")


def test_degraded_flag_is_set_by_run_case_cleanup_failure(tmp_path, monkeypatch):
    """清理失败后必须置降级标记，后续任务被拒绝（不能假装还能继续用）。"""
    worker.DEGRADED["reason"] = ""

    class FakeProc:
        pid = 424242
        returncode = 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(worker, "_kill_tree", lambda pid: None)
    monkeypatch.setattr(worker, "_group_alive", lambda pid, sec: False)
    monkeypatch.setattr(worker, "_purge_work_root", lambda root: "rm_failed: OSError")

    outcome = worker.run_case("print(1)", "", str(tmp_path), 1.0, 100)
    assert outcome["cleanup_status"].startswith("rm_failed")
    assert worker.DEGRADED["reason"].startswith("rm_failed")
    worker.DEGRADED["reason"] = ""


def test_clean_run_does_not_degrade(tmp_path, monkeypatch):
    worker.DEGRADED["reason"] = ""

    class FakeProc:
        pid = 424243
        returncode = 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(worker, "_kill_tree", lambda pid: None)
    monkeypatch.setattr(worker, "_group_alive", lambda pid, sec: False)

    outcome = worker.run_case("print(1)", "", str(tmp_path), 1.0, 100)
    assert outcome["cleanup_status"] == "ok"
    assert worker.DEGRADED["reason"] == ""


@pytest.mark.parametrize("reply,expected", [
    ({"cleanup_status": "ok"}, {}),
    ({}, {}),
    ({"cleanup_status": "rm_failed: OSError"}, {"error_code": "cleanup_failed"}),
])
def test_agent_cleanup_failure_mapping(reply, expected):
    import importlib.util as iu

    spec = iu.spec_from_file_location("ph_judge_agent", ROOT / "vm_agent" / "ph_judge_agent.py")
    agent = iu.module_from_spec(spec)
    spec.loader.exec_module(agent)
    got = agent.cleanup_failure(reply)
    if expected:
        assert got["error_code"] == expected["error_code"]
    else:
        assert got == {}
