"""题面接口的可见性约束：用例与参考解不下发，示例保留。"""
from app import main


def test_question_api_hides_test_cases_and_reference_solution():
    q = main.api_question("P001", x_access_code=None)

    assert q["id"] == "P001"
    assert q["examples"], "示例应保留，学生需要据此理解输入输出格式"
    assert "reference_solution" not in q
    assert "test_cases" not in q, "评测用例不应在提交前下发给页面"
