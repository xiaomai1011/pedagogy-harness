# 接口契约（Phase 0 · 冻结版）

> **状态**：✅ 已冻结（2026-10-16）
> **负责人**：岗位 1（负责人 / 技术）
> **变更规则**：本文件是判题岗（岗位 3）与题库岗（岗位 4）的唯一依赖。
> 冻结后任何字段改动，**必须**先与岗位 1 确认，否则联调会崩。
> **红线**：判题 I/O 必须含 `error_type` 与 `stderr` 两个字段——它们是错误分类器的输入。
>
> **冻结后修订记录**
> - **2026-10-17（经岗位 1 确认）**：`§2` 题库元数据增 `phase`；`§3` 增 `phase`、`hint_unlocked_level` 两列，
>   改写 `group_id` 与 `hint_requested` 列说明；`§5` 预设表加注，`§5.2` 升级规则按**②口径**（解锁／展示两状态）改写。
> - **红线未动**：`§1` 判题模块 I/O 的 `error_type` / `stderr` **一个字没改**。
> - **作废**：`hint_level_upgraded { reason }` 提案**作废**——代码中**不存在该事件流**（埋点＝每次提交落一行 `submission_log`）。
> - 依据：《决策存档_2026-10-17》决策 8／9／10／11、《技术方回执（第三回）》C1—C8。
>
> **2026-10-17（第二次修订，经岗位 1 确认）**
> - `§5.2` 增**第 5 条**：登记"提示入口对当前代码**独立判题、不做缓存**"与
>   "**仅**正式提交入口计连败 / 写错误记忆"的记账口径（对应待办 D 的修复，提交 `8c641bc`）。
> - **本次未改动任何字段与表结构**：`§1` 判题 I/O 的 `error_type` / `stderr` **红线未动**，
>   `§3` 日志 16 列 schema 与 `§5.1` 组件基座均**未动**。
>
> **2026-10-18（第三次修订，经岗位 1 确认）**
> - **新增 `§6` 事件表 `event_log`**：一张关联表承载 **执行／提示／教学处理** 三类事件，含状态机（§6.2）、
>   **四步确定事务顺序**（§6.3）、**前后测三入口行为**（§6.4）、**纯提示分支与判题异常收尾**（§6.5）、
>   计数口径（§6.7：四类数字分开表述）、旧库兼容与唯一性（§6.8）、保留与删除（§6.9）、Q6 登记（§6.10）。
> - **未动项**：`§1` `error_type`／`stderr` 红线、`§3` 16 列语义、`§4` 错误记忆表结构、`§5.1` 组件基座、
>   **岗位③判题器接口** —— 本表**只新增**，上述各项**一个字未改**。
> - 依据：待办 **Q1／C／I**；审议过程留档见 `docs/事件表契约草案.md`（已注明被本 §6 取代）。

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
  "phase": "practice",
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
| `phase` | ✅ | **阶段标识**：`pretest` / `practice` / `posttest`。**由题库自描述**；`submission_log` 的 `phase` 由 pipeline 从这里取，**前端不传**（防篡改）。前后测必须为**平行等值题**（同知识点、同难度、**不同题干**），前后测**不得使用同一套题** |
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
| `group_id` | TEXT | **运行配置预设名**（组件开关组合，供联调与现场演示）；**正式实验统一 `exp`**。**不是"实验分组"** |
| `question_id` | TEXT | 题目 id |
| `phase` | TEXT | 阶段：`pretest` / `practice` / `posttest`；**由 pipeline 从题库元数据取，前端不传**（防篡改） |
| `submit_seq` | INTEGER | 该被试在该题的**第几次**提交（从 1 起） |
| `status` | TEXT | 判题状态（同 `§1`） |
| `error_type` | TEXT | 执行层类别（同 `§1`） |
| `error_class` | TEXT | 教学五类（语法/类型/边界/逻辑/环境）；分类器关闭时为 `未分类` |
| `error_kp` | TEXT | 映射到的知识点；无则空串 |
| `hint_level` | INTEGER | 本次**展示**给学生的提示等级 `0..4`（`0` = 未展示）；未主动请求时恒为 `0` |
| `hint_unlocked_level` | INTEGER | 已**解锁**的提示等级上限 `0..4`。**解锁 ≠ 展示**（连败只解锁，见 `§5.2`） |
| `hint_requested` | INTEGER | `1` = **本次提交学生主动请求**提示；`0` = 未请求。**与等级无关**，每次提交按"本次动作"判定，不重算历史 |
| `pass_status` | INTEGER | `1` 通过 / `0` 未通过 |
| `elapsed_ms` | INTEGER | 判题耗时 |

> `submit_seq` 是"提交次数"的落库形式；`pass_status` 与 `elapsed_ms` 用于
> 申报书指标"提示等级与通过率、耗时的关系可量化"。
>
> **本表共 16 列**（原 14 列 + `phase` + `hint_unlocked_level`）。口径②的**两个独立状态**：
> `hint_unlocked_level` = **允许展示到哪一级**；`hint_level` = **这次实际展示了哪一级**。

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

> **注（2026-10-17）**：**本次实验不使用组间分组**；正式实验**统一使用 `exp`**（全组件开启）。
> 前测／后测由题库元数据的 **`phase`** 区分（见 `§2`／`§3`）。上表五个预设**保留**为系统底层能力，
> 用途仅限**联调与现场演示**（演示"组件可插拔"）。`group_id` 因此不是"实验分组"，见 `§3`。
>
> ⚠️ **前端不得暴露分组选择**：学生界面固定发 `group="exp"`；建议后端在正式收数期**忽略/拒绝非 `exp`**，
> 否则改包请求会污染实验数据。

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

### 5.2 提示阶梯的等级、解锁与展示规则

| 等级 | 给什么 |
|---|---|
| 0 | 只给题目与判题反馈，**不给教学提示**（初次进入题目的状态） |
| L1 | 只给方向：可能用到的函数 / 关键词 |
| L2 | 整体思路骨架：先做什么、再做什么（自然语言） |
| L3 | 步骤级伪代码 |
| L4 | 只给局部代码片段；**绝不给完整可运行答案** |

**口径②-A（2026-10-17 定案）：「解锁」与「展示」是**两个独立状态**，且"请求"不升级。**

| 状态 | 落在哪列 | 含义 |
|---|---|---|
| 已解锁等级 | `hint_unlocked_level` | **允许**展示到哪一级（`0..4`） |
| 本次展示等级 | `hint_level` | 这次**实际**给了哪一级（`0` = 没展示） |

规则：

1. **连败只解锁、不展示**。连败解锁等级 `u_fail = min(连续失败次数 // 2, 4)`
   —— "连续失败次数"**含本次提交在内**（本次判题未通过就 +1）；即连续失败 2 次 → 解锁 L1，
   4 次 → L2，6 次 → L3，8 次及以上 → L4。
   实现上解锁上限**单调不回退**：取「历史解锁上限」与本次连败推出的等级的**较大者**。
   **这条路径下本次 `hint_level` 仍为 `0`**（学生没请求，就不给提示）。
2. **主动请求才展示，且请求本身不升级**。本次提交学生主动请求（`hint_requested=1`）时：
   `hint_level = unlocked = max(u_fail, 1)`
   —— 展示等级**就是已解锁等级**；连败还不足 2 次（`u_fail = 0`）时给 **L1 打底**，保证点了就有回应。
   **反复请求不会把等级推高**：想拿到更具体的提示，**只能靠继续失败来解锁**。
   **未请求 → `hint_level = 0`，无论已解锁到几级。**
3. **无论走哪条路径，提示文本一律过 `§5.3` 程序化校验**，不得泄答案。
4. **被否决的口径**：①"每次提交自动升一级"；②"连败 2 次即自动展示"（连败自动展示会让学生**未求助就被动拿到提示**）；
   ③"请求即升一级"（2026-10-17 否决：那样提示等级由**点击次数**驱动、与"卡住程度"脱钩，学生连点 4 次即可拿到 L4）。
   **定案为口径②-A**：解锁只由连败推动，请求只负责"展示"。
5. **提示入口的判题与记账口径（2026-10-17 登记，对应待办 D，提交 `8c641bc`）**：
   `/api/hint` 与 `/api/submit` **共用同一条管线**，两个入口都会**对当前代码独立判题**
   （**不做判题缓存**，也不读上一次的判题结果）。两者的区别只在**记账**：

   | 入口 | 是否重新判题 | 是否计入连败 | 是否写错误记忆 | 是否写 `submission_log` |
   |---|---|---|---|---|
   | `/api/submit` | 是 | **计**（`consecutive_fail += 1`） | **写** | 写 |
   | `/api/hint` | 是 | **不计** | **不写** | **不写** |

   —— 若 `/api/hint` 也计连败，连败跨门槛处（3→4、5→6、7→8）会**提前提高展示等级**；
   若也写记忆，`count` 会被**点击次数**虚增（实测 1→4）。
   —— "是否计入"由**入口**决定，**不得**用"学生是否请求了提示"判断
   （`request_hint` / 日志列 `hint_requested`）——因为 `/api/submit` 也允许带 `request_hint=true`。
   —— 判题 I/O（`§1`）与日志 16 列 schema（`§3`）**本次均未改动**。

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

---

## 6. 事件表 `event_log`（关联事件日志，2026-10-18 新增）

> **为什么加**：`§3` 的 `submission_log` 只记**正式提交**；"判题执行留痕（Q1）""提示事件（C）"
> "教学处理状态（I）"三件事原先**无处可记**（`/api/hint` 不写任何日志、模型异常时对不上账）。
> **一张关联表承载三类事件**，用 `event_type` 区分；**不做**通用事件平台。
> **本表是"默认值不等于成功"这套设计的解药**：读日志者先看状态，再看默认值。

### 6.1 表结构

```sql
CREATE TABLE IF NOT EXISTS event_log (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type     TEXT    NOT NULL,            -- execution / hint / teaching
    request_id     TEXT    NOT NULL,            -- 同一请求共用；服务端 uuid4 生成
    submission_id  INTEGER,                     -- = submission_log.id；**可后回填**；纯提示为 NULL
    participant_id TEXT    NOT NULL,
    question_id    TEXT    NOT NULL,
    phase          TEXT    NOT NULL DEFAULT 'practice',
    entry          TEXT    NOT NULL DEFAULT '', -- 来源入口：submit / hint
    ts_start       TEXT    NOT NULL,            -- 事件**原始开始时间**（受理／登记时刻）
    ts_end         TEXT,                        -- 未结束为 NULL
    elapsed_ms     INTEGER,                     -- 未结束为 NULL
    status         TEXT    NOT NULL,            -- **各类型各自定义**，见 §6.2
    progress       TEXT    NOT NULL DEFAULT '', -- teaching 专用进度标记：'' / cls_mem_done
    hint_level     INTEGER,                     -- 本次**生成/准备返回**的等级（**不是**已展示/已阅读）
    unlocked_level INTEGER,                     -- **状态计算完成后**的解锁上限（含首次请求的 L1 保底）
    hint_source    TEXT,                        -- llm / llm_retry / fallback / raw_llm；**无产出为 NULL**
    judge_status   TEXT,                        -- execution 专用：pass/fail/timeout/crash/error
    reason_code    TEXT    NOT NULL DEFAULT '', -- **受控错误码**（见 §6.2.4）
    reason         TEXT    NOT NULL DEFAULT '', -- **脱敏**说明；严禁异常全文/隐藏输入/密钥/模型原始响应
    UNIQUE (request_id, event_type)             -- 同一请求内**每类事件只落一条**
);
CREATE INDEX IF NOT EXISTS idx_event_request    ON event_log(request_id);
CREATE INDEX IF NOT EXISTS idx_event_submission ON event_log(submission_id);
CREATE INDEX IF NOT EXISTS idx_event_type_ts    ON event_log(event_type, ts_start);
```

用**显式列**而非 JSON `payload`：本表要支撑申报书统计（三个计数、等级分布、生成路径分布），
显式列可直接 `GROUP BY`。各类型不适用的列留 **`NULL`**（`NULL`＝不适用，`''`＝确认即空）。
`UNIQUE(request_id, event_type)` **只**保证同一请求内不重复落事件；**客户端重发幂等不在本轮范围**。

### 6.2 三类事件的状态（**不得混用**）

#### 6.2.1 `execution`（写入方：**管线调用 `judge` 的边界**）

| 字段 | 取值 |
|---|---|
| `status` | `started`（**已登记执行尝试，尚无终态**）／`finished`（`judge` 已返回结果）／`aborted`（**程序自己捕获到异常并成功记录**） |
| `judge_status` | `finished` 时填入 `pass`／`fail`／`timeout`／`crash`／`error` |
| `entry` | `submit`／`hint` |

写入时机：**仅在"实际准备调用 `judge`"时**写 `started`；`judge` 返回后**更新同一行**为 `finished`
（补 `ts_end`／`elapsed_ms`／`judge_status`）；管线捕获异常时更新为 `aborted`。
**参数校验失败、前后测禁用**等**提前拒绝**的请求**不写**执行事件。

> **语义限定**：遗留停在 `started` 的行＝**"结果未知／未记录到完成"**（**进程退出**、**终态回写失败**等都可能造成），
> **不能**一律判成中途被杀，**也**不能假定进程被杀后还能自己补写 `aborted`；`judge` **正常返回**的 `timeout`／`crash`／`error`
> 属**判题结果**，**不等于**管线 `aborted`。**不改岗位③判题器接口**。

#### 6.2.2 `hint`（写入方：提示层／管线）

| 字段 | 取值 |
|---|---|
| `status` | `requested`（**处理中**）／`returned_model`（**正常返回模型提示**）／`returned_fallback`（**降级返回兜底提示**）／`unavailable`（**未能返回提示**）／`phase_disabled` |
| `hint_source` | `llm`／`llm_retry`／`fallback`／`raw_llm`；**无产出时为 `NULL`** |

**写入时机：受理有效提示请求时先创建 `requested`，处理完成后更新同一行** ——
这样才留得下"已请求但判题抛异常／模型期间进程退出／还没走到写日志处"这些路径。
覆盖**两个入口**：`/api/hint` 与 `/api/submit(request_hint=True)`；后者**仍只有一条提交、一次判题、一条提示事件**。

> **两个正交维度**：`status` 回答**"有没有产出"**，`hint_source` 回答**"怎么产出的"**。
> 因此 `fallback` 是 `status='returned_fallback'`（**有产出，但降级**），**不是**"生成失败"。
> **裸模型路径**（提示阶梯未启用，如 `ctrl` 内部演示）：`hint_source='raw_llm'`、
> `hint_level`／`unlocked_level` 为 **NULL**、`reason_code='raw_llm_path'`；**不得**记成 `llm`
> （避免暗示它通过了 `§5.3` 校验）。**没有学生提示请求时不凭空创建提示事件。**
> 等级字段口径＝**生成／准备返回**：后端成功返回 ≠ 浏览器已渲染 ≠ 学生已阅读；**本轮不新增前端确认机制**。

#### 6.2.3 `teaching`（写入方：管线）

| `status` | 取值 |
|---|---|
| `pending` | 判题事实已落库，教学未跑完 |
| `done` | **本请求按阶段与配置应执行的教学步骤均已完成** |
| `partial` | 部分步骤完成（须写明哪几步） |
| `failed` | 教学未成功（`reason_code` 记具体原因） |
| `phase_disabled` | 前后测**按阶段策略禁用**教学 |

**`phase_disabled` 口径**：不调模型、不更新错误记忆、不向学生返回教学提示；
它是**正常阶段策略，不是失败**，也**不得**记成 `done`。
若该阶段内部仍做**确定性分类**用于测量，须单独说明其意义（只进内部统计，**不构成教学诊断**）。

#### 6.2.4 `reason_code` 受控码表（扩展须登记）

| 事件 | 码 |
|---|---|
| `execution` | `judge_raised`／`db_error`／`process_exit` |
| `hint` | `phase_disabled`／`gen_unavailable`／`model_error`／`raw_llm_path` |
| `teaching` | `phase_disabled`／`model_error`／`db_error`／`component_skipped` |
| 通用 | `''`（正常，无异常） |

### 6.3 事务顺序（**四步确定序列**，不得只留"原则"）

| 步 | 时机 | **在一个短事务内**提交 |
|---|---|---|
| **1** | 判题返回后 | 写 `submission_log` 行 ＋ 创建 `teaching(pending)` ＋ `execution` 回写终态 ＋ **回填 `submission_id`** |
| **2** | 分类／记忆阶段 | 回填提交行分类字段（`error_class`／`error_kp`）＋ 符合条件的**错误记忆计数** ＋ 置 `teaching.progress='cls_mem_done'` |
| **3** | —— | **无事务：调用模型**（提示生成／兜底） |
| **4** | 收尾 | 更新 `hint` 事件 ＋ 回填提交行提示字段（`hint_level`／`hint_unlocked_level`）＋ `teaching` 终态 |

**四条硬规则**：

1. **模型失败不撤销已提交的分类与记忆事实**（第 2 步已提交的**不回滚**）；
2. **同一请求重复进入第 2 步不得再次增加错误记忆** —— **检查 `progress`、回填分类字段、增加符合条件的记忆计数、设置进度标记，四者必须在同一个短事务内完成**；
   **不得**在事务外先检查、再无条件写入（"先查后写"的间隙仍会重复计数）；若 `progress` 已是 `cls_mem_done` 则整体跳过；
3. **数据库失败不得伪装成教学成功** —— `reason_code='db_error'`，异常必须暴露；
4. **有可用兜底提示时，不因"模型曾失败"就把整个教学过程记为 `failed`** ——
   终态按**约定的教学步骤是否都有产出**判断（`fallback` 属**有产出**），**生成来源另记**（`hint_source`）。
   若某步确实无产出或未完成 → `partial`／`failed`，并在 `reason` 写清**哪个组件、哪一步**。

### 6.4 前后测（`phase ≠ practice`）三入口行为（**不得混为一谈**）

| 请求 | 应有行为 |
|---|---|
| 普通 `POST /api/submit` | **正常判题**；写提交行 ＋ `execution` 事件；`teaching` 记 `phase_disabled`；**不**调模型、**不**更新错误记忆、**不**返回教学提示 |
| `POST /api/submit`（`request_hint=true`） | **仍正常判题、仍写提交行**；`hint` 事件记 `phase_disabled`；**不**调模型、**不**更新记忆 |
| `POST /api/hint` | `hint` 事件记 `phase_disabled`；**提前返回**：**不**调 `judge`、**不**调模型（故**不**写 `execution` 事件） |

> **要点**：**禁用 AI 教学帮助 ≠ 拒绝学生的正式提交**。前后测必须照常判题、照常记录正式提交，
> 否则会误伤测量数据；同时不得留下可绕过限制的入口。

### 6.5 纯提示路径的分支（无 `submission_log` 行）

- 纯提示**判题返回后只更新其 `execution` 事件**，**不创建**正式提交记录；
- 本轮 **`teaching` 事件限定为"正式提交的教学处理"**；纯提示用 `execution ＋ hint` 两条表达；
- `judge` 抛**可捕获**异常 → `execution` 记 `aborted`，且该请求**已创建的 `hint` 事件收尾为 `unavailable`**；
- **正式提交带提示时**（`/api/submit` 且 `request_hint=true`）：该请求的 `submission_id` **必须同时回填到 `execution` 事件与该请求已创建的 `hint` 事件**，不得只回填执行事件；
- **无法完成收尾**的情形（**进程退出**、**终态回写失败**等）都可能留下 `started`／`requested` 等未完成状态；**仅凭该状态不能判定具体原因**；
- **`aborted` 只描述 `judge` 调用未正常完成**：后续模型／教学环节的异常**不得**覆盖**已完成**的 `execution` 终态。

### 6.6 与 `§3` `submission_log` 的关系（不改旧列）

- 旧 16 列**语义不动**，只做关联：`event_log.submission_id = submission_log.id`；
- **提交事实落库前**，该请求 `execution.submission_id` **暂空**，落库时按同一 `request_id` **回填**；
- 默认值读法（**这是本表存在的意义**）：

| 关联 `teaching.status` | 提交行教学字段（`error_class`／`error_kp`／`hint_level`…）的含义 |
|---|---|
| `pending` | **教学处理尚未结束**；**已完成到哪一步要结合 `progress` 判断** —— **不可**把该请求的所有教学字段一律当作"未处理"，更**不可**当结论 |
| `done` | **本请求按阶段与配置应执行的教学步骤已完成**；各字段须**结合组件启用情况、是否请求提示、字段自身含义**解释 —— **不得**把所有默认值统一说成"真的没命中"（例：**分类组件未启用 ≠ 分类后没命中**；**未请求提示 ≠ 请求后没产出**） |
| `partial`／`failed` | 教学**未成功**，但**判题事实有效**，可用于判题侧统计 |
| `phase_disabled` | 该阶段**本就不做教学**，空白是设计使然 |
| **无关联事件行** | **旧版本未采集**（历史提交）——**不得**当 `done`，**不得**反推"没有请求提示" |

- **纯提示请求**无提交行 → 其 `execution`／`hint` 的 `submission_id` 为 **NULL**，靠 `request_id` 串起。

### 6.7 计数口径（Q1：四类数字**分开表述**）

| 指标 | 定义 |
|---|---|
| 提交次数 | `count(*) from submission_log` |
| 提示请求次数 | `count(*) from event_log where event_type='hint'`（含 `phase_disabled` 与未完成的 `requested`） |
| **执行尝试登记数** | `count(*) where event_type='execution'` |
| **判题完成数** | `… and status='finished'` |
| **已记录中止数** | `… and status='aborted'` |
| **尚无终态数** | `… and status='started'` |

> `started` 只代表"**已登记执行尝试**"，`judge` 尚未真正返回；请求仍在处理中时它属**正常"处理中"**，
> 最终分析须**按采集截止点**说明，**不得**一律当作异常遗留。
> 上述数字**不得合并**成一个"尝试次数"；报告里出现任一数字都要写明取自哪一行。

### 6.8 旧库兼容、唯一性、脱敏与删除

- **建表**：沿用 `app/db.py` 的"首次连接自动建表"（`CREATE TABLE IF NOT EXISTS` ＋ 幂等补列），
  旧库直接补出新表，**无需**独立迁移脚本；重复启动不重复建、不改已有行；
- **历史数据**：旧提交**无事件行＝旧版本未采集**（见 §6.6 末行），不得当 `done`、不得反推；
- **唯一性**：`UNIQUE(request_id, event_type)` 只防**同一请求内**重复；**客户端重发幂等未实现**，
  **不得**声称已具备；
- **`reason` 脱敏**：只存**受控码 ＋ 脱敏说明**；**严禁**异常全文、隐藏用例输入、密钥、模型原始响应；
- **删除参与者数据**：须**一致处理**提交表、事件表、错误记忆表及任何归档副本（同一 `participant_id`），
  并留删除记录，不得只删其中一部分。

### 6.9 保留与访问（**必须在真实学生数据采集前落实**）

- **原始关联数据**（可定位到个人的三张表）：须明确**保存期限**、**访问权限**、**归档方式**与**到期删除方式**；
- **去标识化统计结果**可另定保留期限；
- 具体年限与审批口径**按知情同意与学校要求确定后回填**（本契约不擅自填数字），
  但**必须在真实学生数据采集开始前落实**。

### 6.10 Q6 受控增量登记清单

| 登记项 | 内容 |
|---|---|
| 表结构与事件语义 | §6.1 ＋ §6.2 |
| 与提交表的关联方式 | §6.6（`submission_id` 关联 ＋ 回填时机） |
| 哪些入口写入 | `§6.4`（含 `phase_disabled`）＋ §6.5（纯提示分支） |
| 重试／失败／禁用如何记录 | §6.3 四条硬规则 ＋ §6.2.4 码表 |
| 是否影响原响应 | **不影响**：`/api/submit` 与 `/api/hint` 现有响应字段不变；`event_id`／`request_id` **不回传前端** |
| 保留与删除规则 | §6.9 |
| 幂等迁移与旧库兼容 | §6.8 |
| 未动项 | `§1` `error_type`／`stderr` 红线、`§3` 16 列语义、`§4` 错误记忆表结构、岗位③判题器接口 |

### 6.11 明确**不做**

通用事件平台／消息队列；自动续跑任务；前端确认机制；客户端重发幂等；
在事件表里存学生代码全文、模型原始回包或隐藏用例输入。
