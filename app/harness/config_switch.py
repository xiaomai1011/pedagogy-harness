"""组件开关配置（契约 §5）。

开关按**组件名**启用/停用，而不是写死三个布尔字段——
这样将来新增组件，只要往 `enabled` 里加个名字，本文件结构不用改。
"""

from dataclasses import dataclass

# 三个内置组件名（必须与各组件类里声明的 name 完全一致）
ERROR_CLASSIFIER = "error_classifier"
ERROR_MEMORY = "error_memory"
HINT_LADDER = "hint_ladder"

ALL_COMPONENTS = (ERROR_CLASSIFIER, ERROR_MEMORY, HINT_LADDER)


@dataclass(frozen=True)
class HarnessConfig:
    group_id: str                              # exp / ctrl / abl_*
    enabled: tuple[str, ...] = ()              # 启用的组件名

    def is_enabled(self, name: str) -> bool:
        return name in self.enabled

    def to_dict(self) -> dict:
        return {"group_id": self.group_id, "enabled": list(self.enabled)}


# 预设分组（契约 §5 的表，一一对应）
PRESETS = {
    "exp":      HarnessConfig("exp", ALL_COMPONENTS),         # 全开
    "ctrl":     HarnessConfig("ctrl", ()),                    # 裸大模型：一个组件都不套
    "abl_hint": HarnessConfig("abl_hint", (HINT_LADDER,)),    # 消融：只留提示阶梯
    "abl_cls":  HarnessConfig("abl_cls", (ERROR_CLASSIFIER,)),# 消融：只留错误分类器
    "abl_mem":  HarnessConfig("abl_mem", (ERROR_MEMORY,)),    # 消融：只留错误记忆
}
