"""`app/llm.py` 的调用参数：思考强度开关。

**为什么要单独测**：`reasoning_effort` 影响**延迟**与**提示等级约束的合格率**，
而本项目要求「实验组与对照组模型行为完全一致」（`app/llm.py` 模块 docstring）。
它必须是**可通过环境变量复现**的显式配置，不能依赖隐式的库默认值。

**兼容性事实（本文件断言）**：`reasoning_effort` 在 openai SDK **1.60.0** 才进入
`chat.completions.create()` 签名；1.30.0–1.55.0 既无该参数、也**没有 `**kwargs` 兜底**，
传了会 `TypeError`。而 `requirements.txt` 声明的是 `openai>=1.30`，因此
**代码默认必须是「不发送」**，由本机配置显式选择 `none`。

本测试**不联网、不调模型**，只验证参数拼装与开关语义。
"""

import inspect
import os
from types import SimpleNamespace

import pytest

from app import llm


def _fake_openai(monkeypatch, captured):
    """把 `openai.OpenAI` 换成假客户端，记录 create() 收到的参数。"""

    class _FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            msg = SimpleNamespace(content="ok")
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    class _FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=_FakeCompletions())

    import sys
    import types
    fake = types.ModuleType("openai")
    fake.OpenAI = _FakeClient
    monkeypatch.setitem(sys.modules, "openai", fake)


def _set_llm_env(monkeypatch):
    monkeypatch.setenv("PH_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("PH_LLM_API_KEY", "test-key")
    monkeypatch.setenv("PH_LLM_MODEL", "test-model")


def test_default_is_do_not_send(monkeypatch):
    """**代码默认是不发送**——这是旧 SDK 兼容性的前提，不是可随意改的偏好。"""
    monkeypatch.delenv("PH_LLM_REASONING_EFFORT", raising=False)
    assert llm.DEFAULT_REASONING_EFFORT == "", \
        "默认必须为空串（不发送）：openai>=1.30 的下限里该参数会 TypeError"
    assert llm._reasoning_effort() is None


def test_empty_env_means_do_not_send(monkeypatch):
    """显式设空串 ⇒ `None`，即**完全不发送**该参数（采用服务端默认行为）。"""
    monkeypatch.setenv("PH_LLM_REASONING_EFFORT", "")
    assert llm._reasoning_effort() is None


def test_blank_env_is_treated_as_do_not_send(monkeypatch):
    """只有空白也按「不发送」处理，避免拼出无意义的空格值。"""
    monkeypatch.setenv("PH_LLM_REASONING_EFFORT", "   ")
    assert llm._reasoning_effort() is None


def test_env_overrides_to_other_levels(monkeypatch):
    """显式取值原样透传（便于做 A/B 对比），并去除首尾空白。"""
    monkeypatch.setenv("PH_LLM_REASONING_EFFORT", "low")
    assert llm._reasoning_effort() == "low"
    monkeypatch.setenv("PH_LLM_REASONING_EFFORT", "  minimal  ")
    assert llm._reasoning_effort() == "minimal"
    monkeypatch.setenv("PH_LLM_REASONING_EFFORT", "none")
    assert llm._reasoning_effort() == "none"


def test_llm_omits_param_by_default(monkeypatch):
    """未设环境变量时**不得**把 `reasoning_effort` 传进 SDK。

    这正是「不发送参数＝服务端默认行为」的落点：客户端不替服务端假定。
    """
    monkeypatch.delenv("PH_LLM_REASONING_EFFORT", raising=False)
    _set_llm_env(monkeypatch)

    captured = {}
    _fake_openai(monkeypatch, captured)

    assert llm.llm("sys", "user") == "ok"
    assert "reasoning_effort" not in captured, "默认不得发送该参数"
    assert captured["max_tokens"] == llm.DEFAULT_MAX_TOKENS
    assert captured["temperature"] == llm.DEFAULT_TEMPERATURE


def test_llm_omits_param_when_disabled(monkeypatch):
    """显式关闭（空串）时同样不得发送。"""
    monkeypatch.setenv("PH_LLM_REASONING_EFFORT", "")
    _set_llm_env(monkeypatch)

    captured = {}
    _fake_openai(monkeypatch, captured)

    assert llm.llm("sys", "user") == "ok"
    assert "reasoning_effort" not in captured, "关闭思考时不应发送该参数"


def test_llm_sends_param_when_explicitly_enabled(monkeypatch):
    """**本机显式配置** `none` 时必须发送，确保延迟优势真的生效。"""
    monkeypatch.setenv("PH_LLM_REASONING_EFFORT", "none")
    _set_llm_env(monkeypatch)

    captured = {}
    _fake_openai(monkeypatch, captured)

    assert llm.llm("sys", "user") == "ok"
    assert captured["reasoning_effort"] == "none"


def test_sdk_interface_signature_check():
    """**接口签名检查**（不是完整请求构造验证）。

    这条不经过 `llm()` 的替身，直接问**真实安装的 SDK** 的 `create()` 是否接受该参数，
    用于避免「测试全绿但实际装的是旧 SDK」这类假阳性。

    ⚠️ **证据名称须写准**：本检查只证明**接口签名**层面能接收该参数，
    **不证明**参数确实进入了请求体，**也不证明**该版本正式支持 `none` 取值、
    或中转站会执行它。

    **完整请求构造验证**（真实客户端 + 离线模拟 HTTP 传输、检查实际发送的 JSON，
    **不调用真实模型**）留到换机部署核对时落实，本轮不追加在线探针。
    """
    openai = pytest.importorskip("openai")
    client = openai.OpenAI(api_key="x", base_url="https://example.invalid/v1")
    params = inspect.signature(client.chat.completions.create).parameters

    has_param = "reasoning_effort" in params
    has_var_kw = any(p.kind == p.VAR_KEYWORD for p in params.values())
    assert has_param or has_var_kw, (
        f"当前 openai=={openai.__version__} 的 create() 既无 reasoning_effort、"
        "也无 **kwargs 兜底 ⇒ 显式启用会 TypeError；"
        "此时必须保持默认不发送（PH_LLM_REASONING_EFFORT 留空）"
    )


def test_requirements_floor_is_safe_without_param():
    """核对依赖下限与「默认不发送」这一设计相容。

    `requirements.txt` 写 `openai>=1.30`；1.30–1.55 不支持该参数。
    因为**默认不发送**，所以这个下限仍然有效——若将来有人把默认改回 `none`，
    这条断言会失败，提醒必须同时抬高依赖下限。
    """
    assert llm.DEFAULT_REASONING_EFFORT == "", (
        "若默认改为发送 reasoning_effort，则必须把 requirements.txt 的 "
        "openai 下限抬到 >=1.60（该参数进入 create() 签名的版本）"
    )

def test_sanitize_proxy_env_drops_bracketed_entries(monkeypatch):
    """NO_PROXY 里带方括号的 IPv6 条目会让 HTTP 客户端建池时报 Invalid port: ':1]'（2026-10-28 实测）。"""
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")
    llm._sanitize_proxy_env()
    value = os.environ.get("NO_PROXY", "")
    assert "[" not in value and "]" not in value, value
    assert "localhost" in value and "127.0.0.1" in value
