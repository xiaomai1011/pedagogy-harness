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
from app.harness import redaction
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

# 教学类别 → 知识点关键词。**弱回退**：只有在 stderr 里**没有任何异常名证据**时才用
# （典型是 wrong_output、空 stderr），避免把题目主知识点硬套到具体报错上。
CLASS_KP_KEYWORDS = {
    "语法": ("语法", "变量", "赋值", "标识符", "输出", "输入", "缩进"),
    "类型": ("类型", "数据", "转换", "字符串", "整数"),
    "边界": ("边界", "索引", "下标", "取值", "范围", "条件", "越界"),
    "逻辑": ("逻辑", "循环", "遍历", "迭代", "条件", "判断", "算法", "顺序",
             "比较", "排序", "最大", "最小", "列表", "切片", "取模", "余数", "奇偶"),
    "环境": ("环境", "输入输出", "标准输入", "标准输出", "文件", "递归", "内存"),
}

# 兼容旧名字（外部/测试若直接引用 KP_KEYWORDS，拿到的是弱回退表）。
KP_KEYWORDS = CLASS_KP_KEYWORDS

# **强证据**：stderr 里出现哪个异常名，就只允许关联这几类知识点关键词。
# 顺序即优先级；一个都没匹配上时**宁可留空**，也不退回去套题目主题。
EVIDENCE_KP = {
    "indexerror": ("索引", "下标", "列表", "数组", "切片", "越界", "边界"),
    "keyerror": ("键", "字典", "映射", "边界"),
    "zerodivisionerror": ("取模", "整除", "余数", "除法", "除零"),
    "nameerror": ("变量", "赋值", "标识符", "函数", "参数", "拼写"),
    "unboundlocalerror": ("变量", "赋值", "作用域", "函数"),
    "typeerror": ("类型", "转换", "字符串", "整数", "浮点", "数据"),
    "attributeerror": ("属性", "对象", "方法", "类型"),
    "valueerror": ("取值", "转换", "范围", "输入", "类型"),
    "syntaxerror": ("语法", "括号", "语句", "缩进"),
    "indentationerror": ("缩进", "语法", "代码块"),
    "taberror": ("缩进", "语法"),
    "recursionerror": ("递归", "深度", "函数"),
    "memoryerror": ("内存", "资源"),
    "filenotfounderror": ("文件", "路径", "读写"),
    "permissionerror": ("权限", "文件", "环境"),
    "importerror": ("模块", "导入", "库"),
    "modulenotfounderror": ("模块", "导入", "库"),
    "stopiteration": ("迭代", "遍历", "循环", "生成器"),
}

# traceback 里的异常行形如 `IndexError: list index out of range`，行首即异常名。
_EXC_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\b")

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
    low = stderr.lower()
    # ValueError 不整体归"边界"：输入转换失败 / 格式字符串错误属于**类型**问题
    # （判据固定写在代码里，评测标签先行固定，不倒改标准答案）
    if "valueerror" in low and any(k in low for k in (
            "invalid literal for int", "could not convert",
            "unsupported format character", "invalid format")):
        return "类型"
    for error_class, names in RULE_TABLE:
        for name in names:
            # 大小写不敏感：`indexerror`、`Syntaxerror` 这类写法同样算证据
            if name.lower() in low:
                return error_class
    if error_type == "wrong_output":
        return "逻辑"          # 程序正常结束，但结果与期望不符
    return None


def _match_point(keywords, points: list[str]) -> str:
    """在题目知识点里按关键词挑一个（大小写不敏感）；挑不到返回空串。"""
    for keyword in keywords:
        lowered = keyword.lower()
        for point in points:
            if lowered in point.lower():
                return point
    return ""


def _evidence_names(stderr: str) -> list[str]:
    """stderr 里**真正抛出的异常行**出现的异常名（小写），按 EVIDENCE_KP 优先级排序。

    只认行首的异常名，并跳过 traceback 里 `File "...", line N` 之后那行源码——
    否则学生代码里的字符串（例如 `print("ZeroDivisionError")`）会被当成证据，
    把错误记到不相干的知识点上。
    """
    found: set[str] = set()
    skip_source_line = False
    for raw in (stderr or "").splitlines():
        line = raw.strip()
        if skip_source_line:
            skip_source_line = False
            continue
        if line.startswith('File "'):
            skip_source_line = True
            continue
        match = _EXC_LINE_RE.match(line)
        if not match:
            continue
        name = match.group(1).lower()
        if name in EVIDENCE_KP:
            found.add(name)
    return [name for name in EVIDENCE_KP if name in found]


def _guess_knowledge_point(error_class: str, question: dict, stderr: str = "") -> str:
    """给错误挑一个知识点：**强证据优先，弱回退在后，没证据就留空**。

    1. stderr 里有异常名 → 只按该异常名允许的知识点关键词匹配；
       匹配不上就返回空串（**绝不**退回"题目主题"，否则运行时输入错误会被
       记成题目主知识点，污染错误记忆）。
    2. stderr 里没有任何异常名（典型是 wrong_output）→ 才用教学类别关键词做弱回退。
    3. 都不行 → 空串（契约 §3 允许 `error_kp` 为空）。
    """
    points = [p for p in (question or {}).get("knowledge_points", []) if p]
    names = _evidence_names(stderr)
    for name in names:
        hit = _match_point(EVIDENCE_KP[name], points)
        if hit:
            return hit
    if names:
        return ""
    return _match_point(KP_KEYWORDS.get(error_class, ()), points)


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
    if not llm_mod.is_configured():
        # 没有模型就没有兜底分类：宁可空着，也不把占位文本当成分类结果
        return Classification(error_class="未分类", knowledge_point="", source="none")
    points = [p for p in (question or {}).get("knowledge_points", []) if p]
    # F：送进分类模型的报错同样是过滤后的视图（隐藏用例数据不得进入模型上下文）
    err = redaction.model_stderr(judge_result, question)[:500]
    user = (
        "请把这次学生错误归入教学五类之一。\n"
        f"可选类别：{'、'.join(ERROR_CLASSES)}\n"
        f"可选知识点：{'、'.join(points) if points else '（本题未给定，填空串）'}\n"
        f"执行层 error_type：{getattr(judge_result, 'error_type', '')}\n"
        f"报错信息：{err}\n"
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
                              knowledge_point=_guess_knowledge_point(
                                  error_class, question, getattr(judge_result, "stderr", "") or ""),
                              source="rule")
    if getattr(judge_result, "status", "") == "pass":
        # 通过：没有错误可分类。这里若调模型，既浪费一次调用又会引入噪声标签。
        return Classification(error_class="未分类", knowledge_point="", source="none")
    return _llm_classify(judge_result, code, question)


def classify_rule_only(judge_result, code: str, question: dict) -> Classification:
    """**只走规则**，绝不调用模型 —— 供**前后测**做内部记录用（口径 Q5）。

    规则未命中 → 「未分类」。这样前后测既能留下确定性分类用于测量，
    又满足"不调模型"的硬约束。
    """
    error_class = match_rule(judge_result, code)
    if error_class is None:
        return Classification(error_class="未分类", knowledge_point="", source="none")
    return Classification(error_class=error_class,
                          knowledge_point=_guess_knowledge_point(
                              error_class, question, getattr(judge_result, "stderr", "") or ""),
                          source="rule")


class ErrorClassifierComponent(TeachingComponent):
    """把 classify() 包成可插拔组件。"""

    name = "error_classifier"
    order = 10

    def run(self, ctx) -> None:
        # 第 2 步（数据记录层）已经算过分类并写进 `ctx.classification` → **不再重复算**，
        # 避免规则未命中时**再花一次模型调用**。
        if ctx.classification is not None:
            return
        ctx.classification = classify(ctx.judge_result, ctx.code, ctx.question)
