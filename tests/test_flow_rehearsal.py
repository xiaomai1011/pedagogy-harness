"""离线单测：预演脚本的**零模型调用**分支必须能正常生成报告。

为什么需要：正常构造流程有 4 次模型调用，**证明不了**"一次模型调用都没有"时报告仍不崩。
本测试**不联网、不碰数据库**——直接调用纯函数 `_render_report()`。
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_SCRIPT = REPO / "tools" / "flow_rehearsal.py"


def _load_module():
    """按路径加载脚本模块（`tools/` 不是包，故用 importlib）。"""
    spec = importlib.util.spec_from_file_location("flow_rehearsal", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(label: str, *, calls: int, llm_ms: float,
         log_ok: bool = True, state_ok: bool = True) -> dict:
    """构造一行 `rows` 记录（字段形状与脚本内保持一致）。"""
    return {
        "label": label, "wall": 10.0, "llm_ms": llm_ms, "calls": calls, "llm_bad": 0,
        "exp_log": 1, "got_log": 1, "log_ok": log_ok,
        "exp_status": "fail", "got_status": "fail",
        "exp_hint": 0, "got_hint": 0, "exp_unlocked": 0, "got_unlocked": 0,
        "state_ok": state_ok, "err": "",
    }


def test_report_generates_with_zero_model_calls():
    """零模型调用（只提交、不请求提示）→ 走“无样本”分支，报告仍须正常生成。"""
    mod = _load_module()
    rows = [_row("① 只提交、未请求提示", calls=0, llm_ms=0.0),
            _row("② 只提交、未请求提示", calls=0, llm_ms=0.0)]

    text, all_ok = mod._render_report(mode="constructed", judge_is_stub=True,
                                      configured=False, db_path="<tmp>/x.sqlite", rows=rows)

    assert "无样本" in text
    assert "不计算中位数" in text
    assert "无样本" in text.split("## 3")[1]      # 小结一节也如实写"无样本"
    assert all_ok is True                          # 日志与状态两项都满足 → 不是失败
    assert "最慢操作" in text


def test_report_reports_median_when_samples_exist():
    """有样本时仍给出中位数（守住上面那处修改没有把正常分支改坏）。"""
    mod = _load_module()
    rows = [_row("①", calls=1, llm_ms=1000.0), _row("②", calls=1, llm_ms=3000.0)]

    text, _ = mod._render_report(mode="constructed", judge_is_stub=True,
                                 configured=True, db_path="<tmp>/x.sqlite", rows=rows)

    assert "中位 2000 ms" in text
    assert "无样本" not in text
