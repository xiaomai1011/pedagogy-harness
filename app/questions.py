"""题库加载与格式校验（契约 §2）。题库由岗位 4-A 产出，放在 data/questions/<id>.json。

- `load_question()` / `list_questions()`：读取。题库未就绪时回**占位题**，保证其他岗位不被阻塞。
- `validate_question()`：按契约 §2 校验单题格式；命令行入口见 `tools/validate_questions.py`。
"""

import json
from pathlib import Path

QUESTIONS_DIR = Path(__file__).resolve().parent.parent / "data" / "questions"

# 契约 §2：phase 由题库自描述（前端不传它），固定三种取值
VALID_PHASES = ("pretest", "practice", "posttest")
VALID_DIFFICULTIES = (1, 2, 3)
VALID_ERROR_TYPES = ("none", "syntax", "runtime", "timeout", "wrong_output")


def load_question(question_id: str) -> dict:
    path = QUESTIONS_DIR / f"{question_id}.json"
    if not path.exists():
        # 题库未就绪时返回一个占位题，保证纵切链路能跑（不阻塞其他岗位）
        return {
            "id": question_id,
            "title": "（占位题）",
            "statement": "题库尚未就绪。",
            "examples": [],
            "test_cases": [],
            "reference_solution": "",
            "knowledge_points": [],
            "difficulty": 1,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def list_questions() -> list[dict]:
    if not QUESTIONS_DIR.exists():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(QUESTIONS_DIR.glob("*.json"))]


def _is_filled(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def validate_question(question, filename: str | None = None) -> tuple[list[str], list[str]]:
    """按契约 §2 校验一道题，返回 `(errors, warnings)`。

    - errors：**必须修**——缺必填字段、类型不对、`phase` 非法等，会导致判题或统计出错。
    - warnings：**建议修**，不阻塞入库——如没有隐藏用例、知识点与 `error_samples` 对不上。
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(question, dict):
        return ["题目必须是一个 JSON 对象"], []

    question_id = question.get("id")
    if not _is_filled(question_id):
        errors.append("缺少 id（必填，形如 P001）")
    elif filename and Path(filename).stem != question_id:
        errors.append(f"id 与文件名不一致：id={question_id}，文件名={Path(filename).name}")

    for field in ("title", "statement", "reference_solution"):
        if not _is_filled(question.get(field)):
            errors.append(f"缺少 {field}（必填，且不能是空字符串）")

    if question.get("difficulty") not in VALID_DIFFICULTIES:
        errors.append(f"difficulty 必须是 {VALID_DIFFICULTIES} 之一，实际为 {question.get('difficulty')!r}")

    if question.get("phase") not in VALID_PHASES:
        errors.append(f"phase 必填，且必须是 {VALID_PHASES} 之一，实际为 {question.get('phase')!r}")

    points = question.get("knowledge_points")
    if not isinstance(points, list) or not points or not all(_is_filled(p) for p in points):
        errors.append("knowledge_points 必须是非空字符串数组（错误分类器靠它做知识点映射）")

    examples = question.get("examples")
    if not isinstance(examples, list) or not examples:
        errors.append("examples 至少 1 条（必填）")
    else:
        for index, example in enumerate(examples):
            if (not isinstance(example, dict)
                    or not _is_filled(example.get("input"))
                    or not _is_filled(example.get("output"))):
                errors.append(f"examples[{index}] 需要 input 与 output（explain 可选）")

    cases = question.get("test_cases")
    if not isinstance(cases, list) or not cases:
        errors.append("test_cases 至少 1 条（必填，判题要用）")
    else:
        for index, case in enumerate(cases):
            if not isinstance(case, dict):
                errors.append(f"test_cases[{index}] 必须是对象")
                continue
            if not isinstance(case.get("input"), str) or not isinstance(case.get("expected"), str):
                errors.append(f"test_cases[{index}] 的 input / expected 必须是字符串")
            if not isinstance(case.get("hidden"), bool):
                warnings.append(f"test_cases[{index}] 未明确写 hidden（true/false），将按 false（可见）处理")
        if not any(isinstance(c, dict) and c.get("hidden") is True for c in cases):
            warnings.append("没有任何 hidden=true 的用例：学生能看到全部测试点，区分度会下降")

    samples = question.get("error_samples")
    if samples is not None:
        if not isinstance(samples, list):
            errors.append("error_samples 必须是数组（也可以不写）")
        else:
            for index, sample in enumerate(samples):
                if not isinstance(sample, dict):
                    errors.append(f"error_samples[{index}] 必须是对象")
                    continue
                if not _is_filled(sample.get("code")):
                    errors.append(f"error_samples[{index}] 缺少 code")
                if sample.get("error_type") not in VALID_ERROR_TYPES:
                    errors.append(f"error_samples[{index}] 的 error_type 必须是 {VALID_ERROR_TYPES} 之一")
                knowledge_point = sample.get("knowledge_point")
                if not _is_filled(knowledge_point):
                    errors.append(f"error_samples[{index}] 缺少 knowledge_point")
                elif isinstance(points, list) and knowledge_point not in points:
                    warnings.append(
                        f"error_samples[{index}] 的 knowledge_point「{knowledge_point}」不在 knowledge_points 列表里")

    return errors, warnings
