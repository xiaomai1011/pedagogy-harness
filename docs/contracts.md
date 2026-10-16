# 接口契约（Phase 0 · 冻结版）

> **状态**：✅ 已冻结（2026-10-16）
> **负责人**：岗位 1（负责人 / 技术）
> **变更规则**：本文件是判题岗（岗位 3）与题库岗（岗位 4）的唯一依赖。
> 冻结后任何字段改动，**必须**先与岗位 1 确认，否则联调会崩。
> **红线**：判题 I/O 必须含 `error_type` 与 `stderr` 两个字段——它们是错误分类器的输入。

---

## 1. 判题模块 I/O

**分工**：判题岗（岗位 3）实现 `judge()`；它只负责"跑代码、给执行层结论"，**不做教学分类**。

### 输入

```json
{
  "question_id": "P001",
  "language": "python",
  "code": "print(1)"
}
```

### 输出 `JudgeResult`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `status` | enum | ✅ | `pass` / `fail` / `timeout` / `crash` / `error` |
| `total_cases` | int | ✅ | 测试用例总数 |
| `passed_cases` | int | ✅ | 通过的用例数 |
| `failed_cases` | list | ✅ | 未过用例：[`{case_index, input, expected, actual, hidden}`]（hidden 用例不回显 input/expected） |
| **`error_type`** | enum | ✅ | **执行层**错误类别：`none` / `syntax` / `runtime` / `timeout` / `wrong_output` |
| **`stderr`** | str | ✅ | 子进程标准错误原文（无则空串） |
| `stdout` | str | ✅ | 子进程标准输出（截断到 2000 字符） |
| `exec_ms` | int | ✅ | 执行耗时（毫秒） |
| `exit_code` | int \| null | ⬜ | 进程退出码；超时为 `null` |

> **注意 `error_type` 的层级**：这里是**程序化判定的执行层类别**（5 个值），
> **不是**教学五类（语法/类型/边界/逻辑/环境）。教学五类由错误分类器（`§5` 组件）从
> `error_type` + `stderr` + 代码 推导得出。两层不要混。

### 桩实现约定（Phase 1）

岗位 3 未交付前，`judge()` 返回**固定**：`status=fail`、`passed_cases=0`、
`error_type="runtime"`、`stderr` 填一句假的 `NameError`。用于跑通纵切。

### 实现约定（岗位 3 照此实现，细则见 `docs/判题模块交接说明.md`）

| 项 | 定死的规则 |
|---|---|
| 超时阈值 | **每用例 5 秒**（常量 `TIMEOUT_SECONDS = 5`） |
| 输入喂法 | 用例 `input` 字符串（末尾补换行）写入子进程 **stdin** |
| 解释器 / 工作目录 | `sys.executable`；在**系统临时目录**运行（不在仓库目录） |
| 输出比对 | 先规范化（统一换行 → 去行尾空白 → 去末尾空行），再**精确比对**；不做浮点容差 |
| stdout 上限 | 截断到 **2000 字符** |

`error_type` 判定：

| 情况 | `status` | `error_type` |
|---|---|---|
| 全用例通过 | `pass` | `none` |
| `SyntaxError` / `IndentationError` | `fail` | `syntax` |
| 运行期抛异常（`stderr` 含 `Traceback`） | `crash` | `runtime` |
| 超时被杀 | `timeout` | `timeout` |
| 正常结束但输出不符 | `fail` | `wrong_output` |
| 判题器自身故障 | `error` | `runtime` |

> `crash` = 学生代码崩了；`error` = 判题器自己出问题。禁止引入 Judge0 / isolate，禁止用 `eval`/`exec` 在主进程执行学生代码。

---

## 2. 题库数据结构

**分工**：题库岗（岗位 4-A）产出；存放于 `data/questions/*.json`（一题一文件）。

```json
{
  "id": "P001",
  "title": "两数之和",
  "statement": "题干（Markdown）",
  "examples": [
    { "input": "1 2", "output": "3", "explain": "1+2=3" }
  ],
  "test_cases": [
    { "input": "1 2", "expected": "3", "hidden": false },
    { "input": "1000000 2000000", "expected": "3000000", "hidden": true }
  ],
  "reference_solution": "参考解源码（不下发给学生）",
  "knowledge_points": ["变量与赋值", "标准输入输出"],
  "difficulty": 1,
  "error_samples": [
    { "code": "print(1)", "error_type": "wrong_output", "knowledge_point": "标准输出格式" }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 唯一，`P001` 递增 |
| `examples` | ✅ | 可见示例，至少 1 条 |
| `test_cases` | ✅ | 判题用；`hidden=true` 的不回显给学生 |
| `reference_solution` | ✅ | 参考解，**不得**下发给学生或喂给大模型 |
| `knowledge_points` | ✅ | 该题考察的知识点，供错误分类器映射 |
| `difficulty` | ✅ | `1` 基础 / `2` 中等 / `3` 较难（用于难度分层与前后测配对） |
| `error_samples` | ⬜ | **（岗位 1 补充）** 用于测"错误分类准确率"，与 `§1` 的 `error_type` 同层级 |

---

## 3. 日志表 schema（SQLite，实证数据核心）

**分工**：岗位 1 建表 + 岗位 3 按定义写入；**开发初期同步实现，不得事后补录**。

表 `submission_log`（一次提交 = 一行）：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | 主键 |
| `ts` | TEXT | 时间戳（ISO8601，含时区） |
| `participant_id` | TEXT | 被试匿名编号（如 `S01`）；**非**真实姓名 |
| `group_id` | TEXT | 预设名：`exp` / `ctrl` / `abl_hint` / `abl_cls` / `abl_mem` |
| `question_id` | TEXT | 题目 id |
| `submit_seq` | INTEGER | 该被试在该题的**第几次**提交（从 1 起） |
| `status` | TEXT | 判题状态（同 `§1`） |
| `error_type` | TEXT | 执行层类别（同 `§1`） |
| `error_class` | TEXT | 教学五类（语法/类型/边界/逻辑/环境）；分类器关闭时为 `未分类` |
| `error_kp` | TEXT | 映射到的知识点；无则空串 |
| `hint_level` | INTEGER | 本次给出的提示等级 `0..4`（0 = 未给提示） |
| `hint_requested` | INTEGER | `1` = 学生主动请求，`0` = 系统自动（连败升级） |
| `pass_status` | INTEGER | `1` 通过 / `0` 未通过 |
| `elapsed_ms` | INTEGER | 判题耗时 |

> `submit_seq` 是"提交次数"的落库形式；`pass_status` 与 `elapsed_ms` 用于
> 申报书指标"提示等级与通过率、耗时的关系可量化"。

---

## 4. 错误记忆表 schema（跨对话持久化）

**分工**：岗位 1 实现。**要点：按 `participant_id` 持久化，跨会话累计**（创新点原文为"跨对话的学习者错误记忆"）。

表 `error_memory`：

| 列 | 类型 | 说明 |
|---|---|---|
| `participant_id` | TEXT | 被试编号 |
| `error_class` | TEXT | 教学五类 |
| `knowledge_point` | TEXT | 知识点 |
| `count` | INTEGER | 累计出现次数 |
| `last_seen_ts` | TEXT | 最近一次时间 |

**主键**：`(participant_id, error_class, knowledge_point)`（命中即 `count + 1` 并更新 `last_seen_ts`）。
`preventive_hint()` 读取该表，产出"你此前多次在 X 上出错"的预防性提示。

---

## 5. 组件开关配置 schema

**分工**：岗位 1 实现。开关按**组件名**启停（不是写死三个布尔字段），
这样新增组件时本结构不用改。

```json
{ "group_id": "exp", "enabled": ["error_classifier", "error_memory", "hint_ladder"] }
```

预设：

| group_id | enabled（组件名） | 含义 |
|---|---|---|
| `exp` | 四个组件全在（含教学人格） | Harness 全开 |
| `ctrl` | `[]`（一个都不开） | 裸大模型 |
| `abl_hint` | `["hint_ladder"]` | 消融：只留提示阶梯 |
| `abl_cls` | `["error_classifier"]` | 消融：只留错误分类器 |
| `abl_mem` | `["error_memory"]` | 消融：只留错误记忆 |

执行顺序**不由配置决定**，由各组件自己声明的 `order` 决定（小的先跑）。
当前：`tutor_persona`(5) → `error_classifier`(10) → `error_memory`(20) → `hint_ladder`(30)。

### 5.1 组件基座（可插拔机制）

主干 `pipeline.py` **不认识任何具体组件**，只按注册表遍历。新增组件只需三步：

1. 建一个类，继承 `app.harness.base.TeachingComponent`，声明 `name` 与 `order`，实现 `run(ctx)`；
2. 在 `app/harness/builtin.py` 里注册一行；
3. 在预设的 `enabled` 里写上它的名字。

**主干代码零改动**——这一条有测试保证：`tests/test_registry.py`
（断言主干源码里搜不到那个新组件的名字，但注册后照样被调用）。

> 开关**必须真正改变行为**，不能是装饰：关闭某组件时，其 `run()` 不得执行。
> `ctrl`（一个组件都不开）走到主干末尾的兜底分支 = 裸大模型。
> **注意**：正文反馈由 `hint_ladder` 组件负责呈现；若只开分类器/记忆而不开提示阶梯，
> 因为没有呈现层，同样会落到兜底分支（退化为裸大模型）。这一取舍待 Phase 2 再定。
> 当前 `/api/components` 接口可查看注册表里已注册的组件，用于现场演示"可插拔"。

### 5.2 提示阶梯的等级与升级规则

| 等级 | 给什么 |
|---|---|
| 0 | 只给题目与判题反馈，**不给教学提示**（初次进入题目的状态） |
| L1 | 只给方向：可能用到的函数 / 关键词 |
| L2 | 整体思路骨架：先做什么、再做什么（自然语言） |
| L3 | 步骤级伪代码 |
| L4 | 只给局部代码片段；**绝不给完整可运行答案** |

**升级规则（B 口径）**：只有**主动请求提示**、或**连续失败 2 次**时才升一级；上限 L4。
（"每次提交自动升一级"的口径已被否决。）

### 5.3 防泄答案的程序化校验（`app/harness/leak_guard.py`）

**不依赖提示词**——大模型会绕过提示词直接给完整代码，必须在输出之后用可解释规则兜底。
"不给答案"是本作品的核心主张，因此这一层是必需项，不是可选项。

| 判据 | 内容 |
|---|---|
| R1 | L1—L3 出现 markdown 代码围栏（三反引号）→ 违规（这三个等级只允许文字与行内代码） |
| R2 | 任何等级出现 `if __name__` / `def main(` → 违规（完整程序） |
| R3 | L4 代码块超过 8 行 → 违规（只准给"局部片段"） |
| R4 | 与题目参考解的最长公共子串占比 ≥ 50% → 违规（防照抄参考答案） |

处理链：校验不通过 → 带"违规原因"**重试一次** → 仍不通过则改用**无代码的兜底模板文本**。

### 5.4 教学人格（`tutor_persona`）

风格为**中性严谨型**：未通过时客观陈述；通过时允许一两句肯定，
**但必须带具体点**（如"你正确判断了边界条件"），不用无信息量的夸奖；
中文回复、不使用表情符号；**红线同 §5.3**。

人格以**系统提示词**形式注入上下文，供提示阶梯使用；
人格组件被关闭时，提示阶梯退回自带兜底提示词 `DEFAULT_PERSONA`。

> **申报书未提及人格**——此项属团队新增的实现点，**不作为新增创新点**。
