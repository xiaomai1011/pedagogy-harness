# 题库元数据说明（data/questions）

本目录 19 份 JSON 由《完整实验题库（19题·内部整合版）》（`完整实验题库_前测3题_练习13题_后测3题.md`，
整合日期 2026-10-27，版本 2.0.0-draft-20261027）机械转换而来，**不改题意**。

## 组成

| 阶段 | 题号 | 数量 |
| --- | --- | --- |
| pretest | S001–S003 | 3 |
| practice | P001–P013 | 13 |
| posttest | S004–S006 | 3 |

每题字段按契约 §2：`statement` 由「题目描述／输入格式／输出格式／说明提示」合并为纯文本；
`examples` 为公开样例（`hidden=false` 的用例）；`test_cases` 为公开样例 + 隐藏用例（`hidden=true`）；
`reference_solution` 为题库给出的 Python 参考解。后端 `/api/questions/{id}` 会剥掉
`reference_solution` 与全部 `hidden=true` 用例。

## difficulty 的口径（重要）

| 题号 | difficulty | 来源 | 状态 |
| --- | --- | --- | --- |
| P001–P013 | 1 或 2 | 原题库元信息「入门／普及−」 | 来自源文件，可作为参考 |
| S001–S006 | 2 | **无源数据，占位** | `placeholder / estimated`，**未确认** |

- 前测／后测 6 道题在源文件中**没有难度字段**，当前统一填 `2` 只是为了兼容既有
  `validate_question()`（`difficulty` 必须是 1/2/3 的整数）与前端展示。
- 这个 `2` **不得**用于：难度分布统计、题目等价性论证、前后测可比性结论、任何实验结论。
- 正式实验前必须由负责人／岗位④补齐真实难度依据；补齐后在本文件登记来源与日期。
- 若将来放宽数据结构（例如允许 `difficulty: null` + `difficulty_status: "pending"`），
  应同步改校验器、接口与前端，再替换本占位。

## knowledge_points 的口径

- 练习题取自题干元信息里的「知识点」；
- 前测／后测题没有元信息，取自每题「内部资料（学生不可见）」段的「知识点」行。
- 知识点文本**保持题库原样**（循环／列表／比较／切片／取模…），不为迁就分类器而改写；
  分类器的同义词与证据表在 `app/harness/error_classifier.py` 里扩展。

## 校验记录

- `tools/validate_questions.py`：19 个文件、错误 0 条。
- 参考答案自洽：每题的参考解在自己全部用例（含隐藏）上均产出期望输出，19/19 通过。
- 分类器覆盖：`tests/test_error_classifier_coverage.py` 固定 19 题的预期映射（含"明确允许为空"）。
