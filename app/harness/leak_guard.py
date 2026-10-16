"""防泄答案：**程序化校验**（不依赖提示词）。

为什么必须有这一层：大模型会绕过提示词约束直接给完整代码。
"不给答案"是本作品的核心主张，因此**不能只靠提示词**——
必须在模型输出之后，用可解释的规则判断它是否泄了答案。

判据（全部可解释、可调阈值）：
  R1 等级 L1—L3 出现 markdown 代码围栏（```）→ 违规（这三个等级只允许文字/行内代码）
  R2 任何等级出现完整程序标记（`if __name__` / `def main(`）→ 违规
  R3 L4 允许围栏，但代码块超过 MAX_CODE_LINES 行 → 违规（只准给"局部片段"）
  R4 输出与题目参考解的最长公共子串占比 ≥ REF_SIMILARITY → 违规（防照抄参考答案）

校验不通过时，调用方改用 `fallback()` 的无模板文本——**宁可少给信息，也绝不泄答案**。
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


@dataclass
class Violation:
    rule: str      # R1 / R2 / R3 / R4
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

    # R3：L4 只允许局部片段
    if level >= 4:
        for body in blocks:
            lines = [ln for ln in body.split("\n") if ln.strip()]
            if len(lines) > MAX_CODE_LINES:
                return Violation("R3", f"L4 代码块 {len(lines)} 行，超过 {MAX_CODE_LINES} 行上限")

    # R4：与参考解的相似度
    reference = (question.get("reference_solution") or "").strip()
    if len(reference) >= REF_MIN_LEN:
        ratio = _lcs_len(text, reference) / len(reference)
        if ratio >= REF_SIMILARITY:
            return Violation("R4", f"与参考解最长公共子串占比 {ratio:.0%}，疑似照抄")

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
