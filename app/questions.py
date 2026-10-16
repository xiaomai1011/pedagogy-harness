"""题库加载（契约 §2）。题库由岗位 4-A 产出，放在 data/questions/<id>.json。

当前仅实现读取；岗位 4-A 交付题库后即可直接使用。
"""

import json
from pathlib import Path

QUESTIONS_DIR = Path(__file__).resolve().parent.parent / "data" / "questions"


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
