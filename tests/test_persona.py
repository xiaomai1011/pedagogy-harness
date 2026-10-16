"""教学人格组件：可插拔 + 注入系统提示词。"""

import types

from app.harness import registry
from app.harness.config_switch import PERSONA, PRESETS
from app.harness.persona import TutorPersonaComponent


def test_persona_is_registered():
    assert PERSONA in registry.names()


def test_persona_runs_first():
    """人格必须先于呈现层跑，否则提示阶梯拿不到系统提示词。"""
    assert registry.names()[0] == PERSONA


def test_exp_preset_enables_persona():
    assert PRESETS["exp"].is_enabled(PERSONA)


def test_persona_injects_system_prompt():
    ctx = types.SimpleNamespace()
    TutorPersonaComponent().run(ctx)
    assert "绝不给出" in ctx.persona_system
    assert "不使用表情符号" in ctx.persona_system


def test_persona_off_still_leaves_ladder_usable():
    """人格关掉时 persona_system 为空，提示阶梯应退回自带兜底提示词。"""
    ctx = types.SimpleNamespace(persona_system="")
    assert not ctx.persona_system
    from app.harness.hint_ladder import DEFAULT_PERSONA

    assert "绝不给出" in DEFAULT_PERSONA
