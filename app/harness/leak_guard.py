"""防泄答案：**程序化校验**（不依赖提示词）。

为什么必须有这一层：大模型会绕过提示词约束直接给完整代码。
"不给答案"是本作品的核心主张，因此**不能只靠提示词**——
必须在模型输出之后，用可解释的规则判断它是否泄了答案。

判据（全部可解释、可调阈值）：
  R1 等级 L1—L3 出现 markdown 代码围栏（```）→ 违规（这三个等级只允许文字/行内代码）
  R2 任何等级出现完整程序标记（`if __name__` / `def main(`）→ 违规
  R3 L4 允许围栏，但**代码行数（围栏内外合计）**超过 MAX_CODE_LINES → 违规（只准给"局部片段"）
  R4 输出与题目参考解的最长公共子串占比 ≥ REF_SIMILARITY → 违规（防照抄参考答案）
  R5 任何等级出现**完整可运行答案**（含无围栏写法、正文混代码、多片段拼接）→ 违规
  R6 L1—L3 出现无围栏代码语句（L1/L2 一行即违规；L3 两行及以上）→ 违规

校验不通过时，调用方改用 `fallback()` 的无模板文本——**宁可少给信息，也绝不泄答案**。

**H（2026-10-18）为什么加 R5／R6**：R1 只看三反引号、R3 只数围栏内行数，
于是"无围栏的完整答案"（例如两数之和只需两行）可以整体绕过。
但**行数短 ≠ 局部片段**，所以判"完整答案"不看长度，而看**结构与题面要件**
（是否同时出现输入获取与输出、是否有函数/类定义、是否由多条语句拼成）。
同时必须保留各等级允许的教学表达——判定只认**代码语句形态**：
- 行内反引号里的单个函数名/表达式（如 `len()`）不算语句，属"提到关键词"；
- 含中文的行按自然语言处理（L3 的伪代码常用中文写），不算代码语句；
- 列表/编号前缀先剥离，编号步骤本身不构成违规。

**已知边界（静态规则的固有上限，必须如实登记）**：
- 能拦"形态上像完整程序"的输出，**不能保证**拦住语义等价、改写过的完整答案；
- L1—L3 允许单行无围栏代码语句（L1/L2 已在 R6 拦掉，L3 留一行）；
- 行内块**合并**后若"输入获取 + 带实参的输出调用"同时出现才算完整答案，
  因此把输入与输出分散到两处、且输出写成 `print`（不带实参）的表述仍可能放行；
- 多行正文里若代码行被中文包住（如 `把 int(input()) 的结果直接交给 print`），按自然语言处理会放行。
"""

import re
from dataclasses import dataclass

# R3：L4 允许的代码块最大行数
MAX_CODE_LINES = 8
# R4：与参考解的最长公共子串占参考解的比值阈值
REF_SIMILARITY = 0.5
# R4：参考解太短时不做相似度判断（避免误伤）
REF_MIN_LEN = 20

# R2：完整程序标记
PROGRAM_MARKERS = ("if __name__", "def main(")

_FENCE = re.compile(r"```")

# R5／R6：行首的列表/编号前缀（"- "、"1. "、"1) "、"（1）"）
_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+\s*[.)、]\s*)")
# 行内代码（单个反引号）
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# 行末注释（# 之后；字符串内的 # 会误判，但只用于"该行是否像代码"的辅助判断）
_COMMENT_RE = re.compile(r"#.*$")
# R5：输入获取形态
_INPUT_CALL_RE = re.compile(r"\binput\s*\(|\bsys\.stdin\b|\bopen\s*\(")
# R5：输出形态
_OUTPUT_RE = re.compile(r"\bprint\s*\(|\bsys\.stdout\b|\bwrite\s*\(")
# R5：**带实参**的输出调用（`print(n)` 算，`print()` 只是提到函数名，不算）
_OUTPUT_ARG_RE = re.compile(r"\b(?:print|write)\s*\(\s*[^)\s]")
# R5：函数/类定义形态（任意名称，含无 body 的 `def main(` 由 R2 另行处理）
_DEF_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+[A-Za-z_]")
# R5：只出现在真实代码里的调用（`input()`／`print()` 被排除，以便识别"读入→输出"结构）
_CODE_CALL_RE = re.compile(
    r"\b(?:map|len|int|float|str|list|dict|set|tuple|range|sum|min|max|abs|sorted|"
    r"enumerate|zip|reversed|round|ord|chr|bin|hex|oct|pow|divmod|type|isinstance|"
    r"append|extend|insert|remove|pop|split|join|strip|format|replace|find|count|"
    r"lower|upper|sort|read|readline|readlines|close)\s*\("
)
# 代码语句特征（用于"是否像代码语句"）
_CODE_HINT_RE = re.compile(r"[=<>+\-*/%]|\b(?:if|elif|else|for|while|return|import|from)\b")

# R6：L1／L2 允许内容里没有代码语句，出现一行即越界；L3（伪代码）允许一行
UNFENCED_CODE_LINE_LIMIT = {"L1": 1, "L2": 1, "L3": 2}


@dataclass
class Violation:
    rule: str      # R1 / R2 / R3 / R4 / R5 / R6
    detail: str


def code_blocks(text: str) -> list[str]:
    """取出 markdown 围栏代码块的内容（```lang ... ```）。"""
    blocks: list[str] = []
    parts = text.split("```")
    # 奇数下标即围栏内的内容
    for i in range(1, len(parts), 2):
        body = parts[i]
        # 去掉首行语言标注（如 python）
        lines = body.split("\n")
        if lines and lines[0].strip() and " " not in lines[0].strip():
            lines = lines[1:]
        blocks.append("\n".join(lines).strip("\n"))
    return blocks


def _strip_comments(line: str) -> str:
    return _COMMENT_RE.sub("", line)


def _has_chinese(line: str) -> bool:
    """含中文的行按自然语言处理（L3 的伪代码常写成中文）。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in line)


def _strip_list_prefix(line: str) -> str:
    return _LIST_PREFIX_RE.sub("", line)


def _line_looks_like_code(line: str) -> bool:
    """判断**行首（顶层）**是否为一条代码语句；自然语言行不算。"""
    body = _strip_list_prefix(_strip_comments(line)).strip()
    if not body:
        return False
    if _has_chinese(body):
        return False
    if _DEF_RE.match(body):
        return True
    if _CODE_HINT_RE.search(body):
        return True
    # 纯调用行（如 `print(a + b)`、`print(input())`）
    return bool(
        _CODE_CALL_RE.search(body)
        or _INPUT_CALL_RE.search(body)
        or _OUTPUT_RE.search(body)
    )


def _code_like_lines(text: str) -> list[str]:
    """取**围栏外**的行首代码语句；围栏内的行由 R1／R3 负责，不在此重复。"""
    outside = text.split("```")
    lines: list[str] = []
    for i in range(0, len(outside), 2):        # 偶数下标＝围栏之外
        for raw in outside[i].split("\n"):
            if _line_looks_like_code(raw):
                lines.append(_strip_list_prefix(_strip_comments(raw)).strip())
    return lines


def _all_code_lines(text: str) -> list[str]:
    """围栏内外**合计**的代码语句行——判"完整答案"时不能只看围栏外。"""
    lines = _code_like_lines(text)
    for body in code_blocks(text):
        for raw in body.split("\n"):
            if _line_looks_like_code(raw):
                lines.append(_strip_list_prefix(_strip_comments(raw)).strip())
    return lines


def _inline_has_complete_answer(text: str) -> bool:
    """行内代码：单个调用/表达式属"提到关键词"，一行里塞多条语句才是完整答案。

    多个行内块**合起来**看也算：`先 \\`n = int(input())\\`，再 \\`print(n)\\`` 是完整答案；
    但"`print()` 和 `input()` 是两个不同的动作"只是提到函数名，不算——
    区分点是输出调用**有没有实参**。
    """
    bodies = [b.strip() for b in _INLINE_CODE_RE.findall(text)]
    for body in bodies:
        if not body or _has_chinese(body):
            continue
        if _DEF_RE.match(body):
            return True
        if ";" in body:                        # 同一行里的多条语句
            return True
    joined = "\n".join(b for b in bodies if b and not _has_chinese(b))
    if not joined:
        return False
    return bool(_INPUT_CALL_RE.search(joined) and _OUTPUT_ARG_RE.search(joined))


def _has_complete_answer(text: str) -> bool:
    """R5：是否出现"完整可运行答案"的形态（与等级无关，也不看行数）。

    **不以行数判"是否局部片段"**：判据是结构与题面要件——
    函数/类定义、同一行的多条语句、或"输入获取 + 输出"同时出现。
    这样两行（甚至一行）的完整答案会被拦，而 `n = len(s)` 这类局部片段不会被误杀。
    """
    stripped = text.strip()
    if not stripped:
        return False
    if _DEF_RE.match(stripped):                # 函数/类定义即完整程序骨架
        return True
    if _inline_has_complete_answer(text):
        return True

    joined = "\n".join(_all_code_lines(text))
    if not joined:
        return False
    # 完整程序：从输入到输出都写了（跨围栏片段拼接也算）
    return bool(_INPUT_CALL_RE.search(joined) and _OUTPUT_RE.search(joined))


def _lcs_len(a: str, b: str) -> int:
    """两个字符串的最长公共子串长度（经典 DP，行数已足够）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def violates(text: str, level: int, question: dict) -> Violation | None:
    """返回违规详情；None 表示通过校验。"""
    if not text:
        return Violation("R1", "输出为空")

    # R2：完整程序标记，任何等级都不允许
    for marker in PROGRAM_MARKERS:
        if marker in text:
            return Violation("R2", f"出现完整程序标记 {marker!r}")

    blocks = code_blocks(text)

    # R1：L1—L3 不允许代码围栏
    if level <= 3 and blocks:
        return Violation("R1", f"等级 L{level} 不允许出现代码块")

    # R4：与参考解的相似度（先于 R5，保证"照抄参考解"仍按 R4 报告）
    reference = (question.get("reference_solution") or "").strip()
    if len(reference) >= REF_MIN_LEN:
        ratio = _lcs_len(text, reference) / len(reference)
        if ratio >= REF_SIMILARITY:
            return Violation("R4", f"与参考解最长公共子串占比 {ratio:.0%}，疑似照抄")

    # R5：完整可运行答案——任何等级都不允许（无围栏写法也不能绕过）
    if _has_complete_answer(text):
        return Violation("R5", "出现完整可运行答案（含无围栏／正文混代码／多片段拼接）")

    # R6：L1—L3 的无围栏代码语句行数上限
    if level <= 3:
        limit = UNFENCED_CODE_LINE_LIMIT.get(f"L{level}", 1)
        code_like = _code_like_lines(text)
        if len(code_like) >= limit:
            return Violation("R6", f"等级 L{level} 出现 {len(code_like)} 行无围栏代码语句，上限 {limit - 1} 行")

    # R3：L4 只允许局部片段——**围栏内外合计**，避免把代码写到围栏外绕过
    if level >= 4:
        total_lines = [ln for ln in _code_like_lines(text) if ln.strip()]
        for body in blocks:
            total_lines += [ln for ln in body.split("\n") if ln.strip()]
        if len(total_lines) > MAX_CODE_LINES:
            return Violation("R3", f"L4 代码合计 {len(total_lines)} 行，超过 {MAX_CODE_LINES} 行上限")

    return None


_FALLBACK_BY_LEVEL = {
    1: "想一想：这道题需要用到哪些基本操作？（本题考察：{kp}）",
    2: "把解题过程拆成几步：先明确输入是什么、要输出什么，中间需要哪些步骤。先把这几步用文字写出来。",
    3: "可以用注释把每一步先写出来，再逐行补上实现。先不要急着写完整代码。",
    4: "把你当前卡住的那一小步单独拿出来：先确认这一步的输入是什么、期望得到什么，再只写这一小步。",
}


def fallback(level: int, classification=None) -> str:
    """校验不通过时的兜底文本：**不含任何代码**，宁可少给信息。"""
    kp = getattr(classification, "knowledge_point", "") or "本题的基础操作"
    tpl = _FALLBACK_BY_LEVEL.get(level, _FALLBACK_BY_LEVEL[1])
    return tpl.format(kp=kp)
