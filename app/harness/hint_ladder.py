"""提示阶梯状态机（创新点 1）。

四级：L1 方向 → L2 思路骨架 → L3 伪代码 → L4 代码片段
升级触发：学生**主动请求** 或 **连续失败 2 次**
硬约束：**L4 只给代码片段，绝不给完整可运行答案**

=== 给岗位 1 的任务（Phase 2, 10/21—10/27）===
- 实现等级推进逻辑（active / consecutive_fail / manual_upgrade）
- render() 通过 app.llm.llm() 生成该等级提示
- **必须配一层程序化校验**：若输出混入完整可运行答案，拦截并降级重写

当前为桩：固定返回 L1 提示文本。
"""

MAX_LEVEL = 4


def next_level(current: int, *, requested: bool, consecutive_fail: int) -> int:
    """计算本次应给的提示等级（0 = 不给提示）。

    ===== 当前为桩实现（Phase 1）=====
    TODO(岗位 1, Phase 2)：主动请求升一级；连败 2 次自动升一级；上限 4。
    """
    if requested:
        return min(current + 1, MAX_LEVEL)
    if consecutive_fail >= 2:
        return min(current + 1, MAX_LEVEL)
    return current


def render(level: int, question: dict, judge_result, classification, memory_hint: str) -> str:
    """生成 level 级提示文本。

    ===== 当前为桩实现（Phase 1）=====
    TODO(岗位 1, Phase 2)：按等级构造 prompt 调 llm()，并做"防泄答案"程序化校验。
    """
    labels = {0: "（无提示）", 1: "L1 方向", 2: "L2 思路骨架", 3: "L3 伪代码", 4: "L4 代码片段"}
    return f"[桩·提示阶梯] 等级={level}（{labels.get(level, '')}）"
