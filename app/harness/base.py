"""教学组件基座：统一接口 + 共享上下文。

**约定**：任何教学组件只要做到三件事，就能被主干自动调用——
  ① 继承 `TeachingComponent`；
  ② 声明 `name`（唯一名）与 `order`（执行顺序，小的先跑）；
  ③ 实现 `run(ctx)`，把结果写进 `ctx`。

主干（`pipeline.py`）只按注册表遍历，**不认识任何具体组件**。
所以"新增一个教学组件" = 新增一个文件 + 在 `builtin.py` 注册一行，
**主干代码一行都不用改**。这就是"可插拔"的技术含义。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SubmissionContext:
    """一次提交的共享上下文：判题之后、返回学生之前，在组件之间流转。

    组件的**输入**只读；组件的**产出**统一写进本对象，不用返回值。
    """

    # ---- 输入（由主干填好）----
    participant_id: str
    question_id: str
    question: dict
    code: str
    ts: str
    request_hint: bool
    judge_result: Any            # app.judge.JudgeResult
    last_level: int              # 该被试在该题上次的提示等级
    consecutive_fail: int        # 该被试在该题的连续失败次数

    # ---- 组件产出（后跑的组件可读前面组件的产出）----
    classification: Any = None   # 错误分类器的产出
    memory_hint: str = ""        # 错误记忆的产出
    persona_system: str = ""     # 教学人格提供的系统提示词（人格组件关闭时为空）
    hint_level: int = 0          # 提示阶梯给出的等级（0 = 未给提示）
    message: str = ""            # 最终呈现给学生的反馈
    source: str = "harness"      # harness = 走了教学组件；raw_llm = 裸大模型

    # ---- 执行轨迹：谁跑了、谁被开关跳过 ----
    executed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class TeachingComponent:
    """教学组件统一接口。所有教学组件都继承它。"""

    name: str = ""
    order: int = 100

    def run(self, ctx: SubmissionContext) -> None:
        """读写 ctx。**不要 return 值**——结果统一放 ctx，避免各组件签名不一致。"""
        raise NotImplementedError(f"组件 {self.name} 未实现 run()")
