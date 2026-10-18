"""隐藏信息过滤（F 待办）：把"判题器原始结果"转成"学生可见／模型可见"的安全视图。

**为什么需要独立一层**：`JudgeResult` 是**原始事实**（岗位③的交付物，验收工具 `tools/` 也直接读它），
但它按设计可能携带**隐藏用例**的输入／期望／实际输出与原始异常正文。
一旦这些原文进入三条通路就会泄露：

1. **学生响应**（`pipeline._response` 的 `failed_cases`／`stderr`，以及 `hint_ladder._fail_feedback`
   产出的 `judge_feedback`）；
2. **模型上下文**（错误分类器 `_llm_classify`、提示阶梯 `_hint_prompt`、裸模型兜底
   `pipeline._run_components`）；
3. **事件表**（`reason` 等字段）。

因此本模块只做一件事：**给定 `(判题结果, 题目)`，产出过滤后的副本**，
**绝不原地修改判题结果**，也**不改变任何判题事实**（`status`／通过数量／`error_type` 枚举语义／
`exec_ms` 一概不动）。

**过滤口径（契约 §6.10 Q6、工作日志 §六 Q4 定案；2026-10-18 评阅复审后收窄）**：

- `failed_cases` 里**条目自身** `hidden` 为真的，`input`／`expected`／`actual` 一律置空，
  只留 `case_index` 与 `hidden`（学生仍要知道"有隐藏用例未过"，但不知道内容）。
  **该判断只看结果条目本身，不看题目声明** —— 题目说"没有隐藏用例"**不能**覆盖结果里的
  `hidden=True`；两边元数据冲突时按**保守**处理。
- 顶层 `stderr` **没有"来自哪个用例"的来源字段** → **无法可靠判断它是否可公开**：
  - 题目**声明含隐藏用例**，**或**结果条目里**存在隐藏用例** → 按"不可归属"**保守处理**，
    只回显**受控提取的异常类别名**，冒号后的原文一律不透传；连类别名都取不到时用固定说明兜底；
  - 两边**都只有可见条目** → 没有可泄露的数据，**原样放行**（不把能给的也删掉）。
- **异常类别名只认有限白名单**（服务端登记的 Python 内置异常名，见 `ALLOWED_EXCEPTION_NAMES`）：
  提取**锚定在异常类型所在行的位置**上（末行行首的标识符），**不对整段文本做自由搜索**；
  未知、自定义、来源无法确认的名称一律退回 `FALLBACK_NOTE` ——
  **不得因为字符串"长得像异常名"（如隐藏输入恰好叫 `SECRET_42Error`）就透传**。
- `error_type` **保留约定枚举语义**，异常类别名另以独立文本表达，**不得为放名字而改写 `error_type`**。

**已知缺口（记录，不擅自改判题接口）**：契约 §1 的 `JudgeResult` 顶层 `stderr`／`stdout`／
`failed_cases[].actual` **没有"来自哪个用例"的来源字段**，因此无法把顶层 `stderr` 精确归属到
可见／隐藏用例。本模块因此按上面的保守口径处理。
若后续要让可见用例的原始报错也能回显，需要岗位③在判题 I/O 上补充来源信息 ——
那属于契约修订，**不在 F 范围内擅自实施**。
"""

import re

# 隐藏用例条目允许回显的字段（白名单：未知字段一律不转发，避免未来新增字段带出隐藏数据）
_SAFE_CASE_FIELDS = ("case_index", "input", "expected", "actual", "hidden")

# **异常类别名白名单**：只认服务端登记过的 Python 内置异常名。
# 不在表内的一律退回固定说明 —— 形状匹配（`*Error`）不是白名单，会放行自定义名称。
ALLOWED_EXCEPTION_NAMES = frozenset((
    # 与 error_classifier.RULE_TABLE 对齐（规则表能识别的名字必须能回显）
    "SyntaxError", "IndentationError", "TabError", "NameError", "UnboundLocalError",
    "RecursionError", "MemoryError", "ImportError", "ModuleNotFoundError",
    "FileNotFoundError", "PermissionError", "TypeError", "AttributeError",
    "IndexError", "KeyError", "ZeroDivisionError", "ValueError", "StopIteration",
    # 常见内置异常／基类／控制流
    "BaseException", "Exception", "ArithmeticError", "AssertionError", "BufferError",
    "EOFError", "FloatingPointError", "IOError", "OSError", "EnvironmentError",
    "LookupError", "NotImplementedError", "OverflowError", "ReferenceError",
    "RuntimeError", "SystemError", "SystemExit", "KeyboardInterrupt", "GeneratorExit",
    "ConnectionError", "BrokenPipeError", "ConnectionAbortedError",
    "ConnectionRefusedError", "ConnectionResetError", "TimeoutError",
    "FileExistsError", "IsADirectoryError", "NotADirectoryError", "InterruptedError",
    "ProcessLookupError", "ChildProcessError", "BlockingIOError",
    "UnicodeError", "UnicodeDecodeError", "UnicodeEncodeError", "UnicodeTranslateError",
    "Warning", "DeprecationWarning", "RuntimeWarning", "UserWarning", "FutureWarning",
))

# **锚定提取**：只看"异常类型所在行"的行首标识符（后面紧跟冒号或行尾），
# 不做全文自由搜索 —— 否则 traceback 的源码行／消息正文里的假名字会被当成类型名。
_EXC_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::|$)")

# 受控前缀与兜底说明（学生可见文本里不得出现原始异常正文）
_CONTROLLED_PREFIX = "异常类型："
#: 异常类别名无法从白名单内确认时，服务端给出的固定说明（公开常量，供测试与工具引用）
FALLBACK_NOTE = "程序运行出错，具体细节不在本阶段回显。"


def question_has_hidden_cases(question) -> bool:
    """题目是否声明了隐藏用例（只看 `test_cases[].hidden`；`examples` 是公开的）。"""
    if not isinstance(question, dict):
        return False
    for case in question.get("test_cases") or []:
        if isinstance(case, dict) and case.get("hidden"):
            return True
    return False


def result_has_hidden_cases(result) -> bool:
    """判题结果里是否**实际存在** `hidden=True` 的条目（与题目声明相互独立）。"""
    if result is None:
        return False
    for case in getattr(result, "failed_cases", None) or []:
        if isinstance(case, dict) and case.get("hidden"):
            return True
    return False


def controlled_exception_name(text: str) -> str:
    """从原始异常正文里**只提取白名单内的异常类别名**；取不到（或不在白名单）返回空串。

    提取锚定在**末个非空行**的行首标识符上，不搜索整段文本。
    """
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    match = _EXC_LINE_RE.match(lines[-1])
    if not match:
        return ""
    name = match.group(1)
    return name if name in ALLOWED_EXCEPTION_NAMES else ""


def student_stderr(result, question) -> str:
    """学生可见的错误输出：两边都只有可见条目→原文；否则只留白名单内的异常类别名。"""
    raw = getattr(result, "stderr", "") or ""
    if not raw:
        return ""
    if not (question_has_hidden_cases(question) or result_has_hidden_cases(result)):
        return raw
    name = controlled_exception_name(raw)
    return f"{_CONTROLLED_PREFIX}{name}" if name else FALLBACK_NOTE


def model_stderr(result, question) -> str:
    """送进模型的错误输出视图 —— 与学生可见视图**同一口径**（不因"给模型"就放宽）。"""
    return student_stderr(result, question)


def student_failed_cases(result, question) -> list:
    """学生可见的失败用例列表：**条目自身声明 `hidden=True` 的一律过滤**，其余原样。

    `question` 参数保留是为了与其它视图保持同一签名（本函数不再依赖题目声明：
    题目"无隐藏用例"不能覆盖结果条目里的 `hidden=True`）。
    """
    if result is None:
        return []
    cases = getattr(result, "failed_cases", None) or []
    out = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        if not case.get("hidden"):
            out.append(dict(case))
            continue
        safe = {key: case.get(key) for key in _SAFE_CASE_FIELDS}
        safe["input"] = ""
        safe["expected"] = ""
        safe["actual"] = ""
        safe["hidden"] = True
        out.append(safe)
    return out
