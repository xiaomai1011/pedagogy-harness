"""判题运行器（契约 §1）。

接口已由岗位 1 冻结，**真实实现（subprocess + 超时）属于岗位 3 的交付物**。
当前文件只有：① 冻结的数据结构；② 一个桩实现，供其他岗位并行开发。

=== 给岗位 3 的任务（对应分工表 3）===
- 10/20 前：实现 v0 —— 提交代码 → 跑测试用例 → 返回「通过 / 失败 + 未过的用例」
- 10/25 前：三种边界 —— 超时中断 / 程序崩溃（非零退出）/ 输出规范化比对
- 11/01 前：将过程日志按 app/db.py 的字段写入 SQLite，并补单元测试

实现要求（硬性）：
- 必须自行实现，不得引入 Judge0 / isolate 等第三方判题服务
- 必须用 subprocess + 超时控制
- 返回必须包含 error_type 与 stderr 两个字段（契约红线）
"""

from dataclasses import dataclass, field


@dataclass
class JudgeResult:
    status: str                 # pass / fail / timeout / crash / error
    total_cases: int
    passed_cases: int
    failed_cases: list = field(default_factory=list)
    error_type: str = "none"    # 执行层：none / syntax / runtime / timeout / wrong_output
    stderr: str = ""
    stdout: str = ""
    exec_ms: int = 0
    exit_code: int | None = None


def judge(question: dict, code: str, language: str = "python") -> JudgeResult:
    """跑测试用例并返回 JudgeResult。

    ===== 当前为桩实现（Phase 1）=====
    TODO(岗位 3)：替换为 subprocess + 超时的真实实现。此函数签名不得更改。
    """
    total = len(question.get("test_cases", []))
    return JudgeResult(
        status="fail",
        total_cases=total,
        passed_cases=0,
        failed_cases=[{
            "case_index": 0,
            "input": question.get("test_cases", [{}])[0].get("input", "-"),
            "expected": "-",
            "actual": "-",
            "hidden": False,
        }] if total else [],
        error_type="runtime",
        stderr="[桩] NameError: name 'x' is not defined",
        stdout="",
        exec_ms=0,
        exit_code=1,
    )
