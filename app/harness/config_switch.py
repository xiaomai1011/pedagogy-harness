"""组件开关配置（契约 §5）。

这是对照实验 / 消融实验的控制机制：开关必须真正改变行为，不能是装饰。
"""

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class HarnessConfig:
    group_id: str            # exp / ctrl / abl_*
    hint_ladder: bool        # 提示阶梯
    error_classifier: bool   # 错误分类器
    error_memory: bool       # 学习者错误记忆

    def to_dict(self) -> dict:
        return asdict(self)


# 预设分组（契约 §5 的表，一一对应，不要随意增删）
PRESETS = {
    "exp":       HarnessConfig("exp", True, True, True),    # 实验组：Harness 全开
    "ctrl":      HarnessConfig("ctrl", False, False, False),  # 对照组：裸大模型
    "abl_hint":  HarnessConfig("abl", True, False, False),  # 消融：只留提示阶梯
    "abl_cls":   HarnessConfig("abl", False, True, False),  # 消融：只留错误分类器
    "abl_mem":   HarnessConfig("abl", False, False, True),  # 消融：只留错误记忆
}
