"""错误分类器（创新点 2）。

输入：JudgeResult 的 error_type + stderr + 学生代码
输出：教学五类（语法 / 类型 / 边界 / 逻辑 / 环境）+ 映射到的知识点

实现策略（申报书原文）：**规则优先，规则未命中时才调用大模型兜底**。

- 规则命中 → `source="rule"`（**可解释**：规则表明摆着，答辩能说清"凭哪条判的"）
- 规则全未命中且非「通过」态 → 交给大模型，`source="llm"`
- 大模型回包不可解析、或类别不在五类内 → 退回「未分类」，`source="none"`（**不猜**）
- 「通过」态不分类，也**不花一次模型调用**

注意层级：执行层的 `error_type`（契约 §1，5 个值）≠ 教学五类。例如 `NameError`
在执行层是 `runtime`，教学口径归「语法」（名字写错 / 忘了赋值）。
"""

import json
import re
from dataclasses import dataclass

from app import llm as llm_mod
from app.harness.base import TeachingComponent

# 教学五类（契约 §3 的 error_class 取值）。"未分类"是兜底态，不是类别。
ERROR_CLASSES = ("语法", "类型", "边界", "逻辑", "环境")

# ---- 规则表：**顺序敏感**，先命中先算 ----
# 判据是 stderr 里的异常名（比 error_type 更细，能区分 Type/Index/Key 等）。
RULE_TABLE = (
    # 语法：语法/缩进错；NameError 类（标识符未定义、名字写错）也归此列
    ("语法", ("SyntaxError", "IndentationError", "TabError",
              "NameError", "UnboundLocalError")),
    # 环境：资源与运行环境相关（递归过深、内存、模块导入、文件/权限）
    ("环境", ("RecursionError", "MemoryError", "ImportError",
              "ModuleNotFoundError", "FileNotFoundError", "PermissionError")),
    # 类型：类型不匹配、对象没有该属性
    ("类型", ("TypeError", "AttributeError")),
    # 边界：越界、缺键、除零、取值非法
    ("边界", ("IndexError", "KeyError", "ZeroDivisionError",
              "ValueError", "StopIteration")),
)

# 教学类别 → 知识点关键词。在 question["knowledge_points"] 里按关键词挑一个。
KP_KEYWORDS = {
    "语法": ("语法", "变量", "赋值", "标识符", "输出", "输入"),
    "类型": ("类型", "数据", "转换", "字符串"),
    "边界": ("边界", "索引", "取值", "范围", "条件"),
    "逻辑": ("逻辑", "循环", "条件", "判断", "算法", "顺序"),
    "环境": ("环境", "输入输出", "标准输入", "文件", "递归"),
}

_LLM_SYSTEM = (
    "你是编程教学错误分类器，只做分类，不写代码、不给任何解题提示。"
    "只输出一个 JSON 对象，不要输出任何其他文字。"
)


@dataclass
class Classification:
    error_class: str        # 语法 / 类型 / 边界 / 逻辑 / 环境 / 未分类
    knowledge_point: str
    source: str             # rule / llm / none


def match_rule(judge_result, code: str) -> str | None:
    """规则匹配：命中返回教学类别，未命中返回 None。

    只看 `error_type` 与 `stderr`，不看学生代码——保证「同样的报错必然同样的分类」，
    这样日志里的 `error_class` 是可复现的，便于答辩时说明判据。
    """
    status = getattr(judge_result, "status", "")
    error_type = getattr(judge_result, "error_type", "")
    stderr = getattr(judge_result, "stderr", "") or ""

    if status == "pass":
        return None
    if error_type == "timeout":
        return "环境"          # 超时（死循环、递归过深）
    if error_type == "syntax":
        return "语法"          # 判题器已判为语法错，无需再看 stderr
    for error_class, names in RULE_TABLE:
        for name in names:
            if name in stderr:
                return error_class
    if error_type == "wrong_output":
        return "逻辑"          # 程序正常结束，但结果与期望不符
    return None


def _guess_knowledge_point(error_class: str, question: dict) -> str:
    """在题目的知识点列表里按关键词挑一个；挑不到就返回空串（不硬凑）。

    挑不到时留空是契约 §3 允许的（`error_kp` 无则空串），
    避免把随便一个知识点写进日志、污染后续统计。
    """
    points = [p for p in (question or {}).get("knowledge_points", []) if p]
    for keyword in KP_KEYWORDS.get(error_class, ()):
        for point in points:
            if keyword in point:
                return point
    return ""


def _parse_llm_reply(raw: str, points: list[str]) -> Classification:
    """解析大模型回包。任何不合法 → 「未分类」。

    宁可返回「未分类」也不猜：错误分类要进日志做统计，错了比空着更糟。
    """
    matched = re.search(r"\{.*\}", raw or "", re.S)
    if not matched:
        return Classification(error_class="未分类", knowledge_point="", source="none")
    try:
        data = json.loads(matched.group(0))
    except (ValueError, TypeError):
        return Classification(error_class="未分类", knowledge_point="", source="none")
    if not isinstance(data, dict):
        return Classification(error_class="未分类", knowledge_point="", source="none")

    error_class = str(data.get("error_class", "")).strip()
    if error_class not in ERROR_CLASSES:
        return Classification(error_class="未分类", knowledge_point="", source="none")
    knowledge_point = str(data.get("knowledge_point", "")).strip()
    if knowledge_point not in points:
        knowledge_point = ""   # 只认题目给过的知识点，防止模型自造
    return Classification(error_class=error_class,
                          knowledge_point=knowledge_point, source="llm")


def _llm_classify(judge_result, code: str, question: dict) -> Classification:
    points = [p for p in (question or {}).get("knowledge_points", []) if p]
    user = (
        "请把这次学生错误归入教学五类之一。\n"
        f"可选类别：{'、'.join(ERROR_CLASSES)}\n"
        f"可选知识点：{'、'.join(points) if points else '（本题未给定，填空串）'}\n"
        f"执行层 error_type：{getattr(judge_result, 'error_type', '')}\n"
        f"报错信息：{(getattr(judge_result, 'stderr', '') or '')[:500]}\n"
        f"学生代码：\n{code}\n\n"
        '只输出 JSON，形如 {"error_class": "边界", "knowledge_point": "数组下标"}；'
        "知识点只能从上面列表里挑，挑不到就填空串。"
    )
    return _parse_llm_reply(llm_mod.llm(system=_LLM_SYSTEM, user=user), points)


def classify(judge_result, code: str, question: dict) -> Classification:
    """规则优先；规则未命中且非「通过」才交给大模型兜底。"""
    error_class = match_rule(judge_result, code)
    if error_class is not None:
        return Classification(error_class=error_class,
                              knowledge_point=_guess_knowledge_point(error_class, question),
                              source="rule")
    if getattr(judge_result, "status", "") == "pass":
        # 通过：没有错误可分类。这里若调模型，既浪费一次调用又会引入噪声标签。
        return Classification(error_class="未分类", knowledge_point="", source="none")
    return _llm_classify(judge_result, code, question)


class ErrorClassifierComponent(TeachingComponent):
    """把 classify() 包成可插拔组件。"""

    name = "error_classifier"
    order = 10

    def run(self, ctx) -> None:
        ctx.classification = classify(ctx.judge_result, ctx.code, ctx.question)
