"""题库校验命令行入口（契约 §2）。

用法：
    python tools/validate_questions.py                # 校验 data/questions/ 下全部题目
    python tools/validate_questions.py 题目1.json ...  # 校验指定文件

退出码：0 = 全部合格（警告不阻塞）；1 = 存在错误。

**岗位 4-A 交题前请先本地跑一遍**，通过后再交，对接时就不会卡在格式上。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.questions import QUESTIONS_DIR, validate_question  # noqa: E402


def main(argv: list[str]) -> int:
    paths = [Path(arg) for arg in argv] if argv else (
        sorted(QUESTIONS_DIR.glob("*.json")) if QUESTIONS_DIR.exists() else [])

    if not paths:
        print(f"没有找到题目文件（目录：{QUESTIONS_DIR}）")
        return 1

    error_total = 0
    seen_ids: dict[str, str] = {}
    for path in paths:
        try:
            question = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[错误] {path.name}：JSON 无法解析 —— {exc}")
            error_total += 1
            continue

        errors, warnings = validate_question(question, filename=str(path))
        question_id = question.get("id") if isinstance(question, dict) else None
        if question_id:
            if question_id in seen_ids:
                errors.append(f"id 重复：{question_id} 已在 {seen_ids[question_id]} 用过")
            else:
                seen_ids[question_id] = path.name

        for warning in warnings:
            print(f"[警告] {path.name}：{warning}")
        for error in errors:
            print(f"[错误] {path.name}：{error}")
        error_total += len(errors)
        if not errors:
            tail = f"（{len(warnings)} 条警告）" if warnings else ""
            print(f"[通过] {path.name}（{question_id}）{tail}")

    print(f"\n共 {len(paths)} 个文件，错误 {error_total} 条。")
    return 1 if error_total else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
