"""题库校验：必填字段、phase 取值、warning 与 error 的分工。"""

from app.questions import load_question, validate_question


def _good() -> dict:
    return {
        "id": "P900",
        "title": "测试题",
        "statement": "读入一个整数，输出它本身。",
        "examples": [{"input": "1", "output": "1"}],
        "test_cases": [
            {"input": "1", "expected": "1", "hidden": False},
            {"input": "9", "expected": "9", "hidden": True},
        ],
        "reference_solution": "print(input())",
        "knowledge_points": ["标准输入输出"],
        "difficulty": 1,
        "phase": "practice",
    }


def test_good_question_passes_without_errors_or_warnings():
    errors, warnings = validate_question(_good(), filename="P900.json")
    assert errors == []
    assert warnings == []


def test_missing_phase_is_error():
    question = _good()
    question.pop("phase")
    errors, _ = validate_question(question, filename="P900.json")
    assert any("phase" in error for error in errors)


def test_unknown_phase_is_error():
    question = _good()
    question["phase"] = "midterm"
    errors, _ = validate_question(question, filename="P900.json")
    assert any("phase" in error for error in errors)


def test_id_filename_mismatch_is_error():
    errors, _ = validate_question(_good(), filename="P901.json")
    assert any("文件名不一致" in error for error in errors)


def test_difficulty_must_be_one_of_1_2_3():
    question = _good()
    question["difficulty"] = 5
    errors, _ = validate_question(question, filename="P900.json")
    assert any("difficulty" in error for error in errors)


def test_empty_test_cases_is_error():
    question = _good()
    question["test_cases"] = []
    errors, _ = validate_question(question, filename="P900.json")
    assert any("test_cases" in error for error in errors)


def test_non_string_case_input_is_error():
    question = _good()
    question["test_cases"] = [{"input": 1, "expected": "1", "hidden": False}]
    errors, _ = validate_question(question, filename="P900.json")
    assert any("必须是字符串" in error for error in errors)


def test_all_visible_cases_is_only_a_warning():
    """没有隐藏用例不阻塞入库，但会提示区分度下降。"""
    question = _good()
    question["test_cases"] = [{"input": "1", "expected": "1", "hidden": False}]
    errors, warnings = validate_question(question, filename="P900.json")
    assert errors == []
    assert any("hidden" in warning for warning in warnings)


def test_empty_knowledge_points_is_error():
    question = _good()
    question["knowledge_points"] = []
    errors, _ = validate_question(question, filename="P900.json")
    assert any("knowledge_points" in error for error in errors)


def test_error_sample_error_type_is_validated():
    question = _good()
    question["error_samples"] = [
        {"code": "print(1)", "error_type": "递归", "knowledge_point": "标准输入输出"}]
    errors, _ = validate_question(question, filename="P900.json")
    assert any("error_type" in error for error in errors)


def test_error_sample_unknown_knowledge_point_is_warning():
    question = _good()
    question["error_samples"] = [
        {"code": "print(1)", "error_type": "runtime", "knowledge_point": "量子力学"}]
    errors, warnings = validate_question(question, filename="P900.json")
    assert errors == []
    assert any("量子力学" in warning for warning in warnings)


def test_shipped_sample_question_stays_valid():
    """仓库里那份样例题（data/questions/P001.json）必须永远合格。"""
    errors, _ = validate_question(load_question("P001"), filename="P001.json")
    assert errors == []
