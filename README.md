# pedagogy-harness

面向编程初学者的在线练习助手：学生在浏览器里做题、提交 Python 代码，系统在**隔离的虚拟机**里判题，
再用一组**可插拔的教学法组件**（提示阶梯 / 错误分类 / 错误记忆）给出不直接给答案的引导，并把全过程
记录成可分析的事件日志。

> 当前阶段：工程实现与小规模试用。仓库里的测试、预演与验收记录用于证明**系统行为**，
> 不构成教学效果的因果证据。

## 它解决什么问题

初学者卡住时最需要的是「下一步该想什么」，而不是答案。本项目把这件事拆成可替换的组件：

- **提示阶梯（hint_ladder）**：L1 方向 → L2 思路骨架 → L3 伪代码 → L4 局部片段，永不直接给完整解。
  连续失败 2 次解锁一级；主动请求「进一步提示」每次最多升一级；两者同时满足只升一级；「重看提示」不升级。
- **错误分类（error_classifier）**：规则优先、模型兜底，把错误归到「语法 / 类型 / 边界 / 逻辑 / 环境」，
  并尽量对齐题目的知识点。证据不足时返回「未分类」，不硬猜。
- **错误记忆（error_memory）**：同一参与者同一题反复出错时，把高频错误类别带进下一次提示。
- **组件开关**：按 `group_id` 选择启用哪些组件；关掉的组件不执行、不产生模型调用。

## 判题与安全边界

学生代码**不在 Web 进程里执行**：

1. Web 端（FastAPI）只做身份、阶段、题目校验，然后通过 `IsolationUnavailable` 语义把请求交给隔离适配器；
2. 适配器把请求发给 VM 内的 agent（HTTP + 令牌，仅内网地址）；
3. agent 只做校验与转发，真正的执行由 **phcode** 低权用户在独立的临时目录里完成：
   环境变量白名单、`RLIMIT_*` 资源限制、独立进程组 + 整组清理、stdout/stderr 上限、默认无外网出口；
4. 清理失败时 worker 进入降级状态并**拒绝后续请求**，不会假装成功。

**Fail closed**：适配器未注册、健康检查失败、能力未验证时，`/api/submit`、`/api/hint` 直接返回
「判题环境未就绪」，不调用 `judge()`、不启动任何学生进程。

## 身份与实验流程

- **匿名会话**：浏览器首次访问自动分配 `A001` 形式的编号（HttpOnly Cookie 绑定，服务端为准，
  不信任请求体里的编号）。
- **阶段锁定**：前测 3 题 → 练习 8 题 → 后测 3 题，由服务端 `session_phase` 控制；越阶段请求 403；
  结束阶段后成绩锁定，不能返回修改。
- 前后测阶段取提示返回 `phase_disabled`（无 AI）；练习阶段才启用教学组件。
- 需要严格身份时可用**签名访问码**（`PH_REQUIRE_ACCESS_CODE=1`）：一人一码、绑定批次与阶段、
  有效期可设；HMAC 只能防伪造，不能防转发，因此默认只在本机或受控网络内使用。

## 快速开始

```bash
python -m venv .venv && . .venv/Scripts/activate     # Windows
pip install -r requirements.txt
cp .env.example .env                                  # 填入模型服务地址与 Key
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000/`。默认使用演示库 `data/demo.sqlite`；`use_real_db.txt` 存在时使用
`data/pedagogy.sqlite`。管理台在 `/admin`，需要 `PH_ADMIN_TOKEN_FILE` 指向的本地令牌文件。

### 主要环境变量

| 变量 | 作用 |
| --- | --- |
| `PH_LLM_BASE_URL` / `PH_LLM_API_KEY` / `PH_LLM_MODEL` | 模型服务（兼容 OpenAI 协议） |
| `PH_LLM_REASONING_EFFORT` | 可选，留空即不发送该参数 |
| `PH_DB` | SQLite 路径（默认 `data/demo.sqlite`） |
| `PH_EXPERIMENT_MODE=1` | 实验模式：关闭调试信息下发、隐藏调试入口 |
| `PH_AUTO_ENROLL=1` | 匿名会话自动编号（访问码模式下自动关闭） |
| `PH_REQUIRE_ACCESS_CODE=1` + `PH_ACCESS_SECRET_FILE` | 签名访问码强制校验 |
| `PH_EXPERIMENT_BATCH` | 批次名，写入记录并校验访问码批次 |
| `PH_VM_AGENT_ENABLED=1` / `PH_VM_AGENT_TOKEN_FILE` | 启用 VM 判题适配器 |
| `PH_ADMIN_TOKEN_FILE` | 管理台令牌文件（仅本机可访问） |

## 目录结构

```
app/                FastAPI 应用：main、pipeline、harness 组件、access、flow、admin、enroll
data/questions/     题库（含隐藏用例与参考解，公开前请评估是否泄露）
vm_agent/           VM 内的 agent、worker、systemd 单元与能力自检脚本
web/                学生端单页（含内置 Monaco）
tools/              验收与分析工具：判题契约、隔离协议、导出、试用分析、边界核验
tests/              pytest 用例
docs/               契约、设计与核验记录（见 docs/INDEX.md）
```

## 测试与验收

```bash
python -m pytest tests -q          # 本地全量
python tools/accept_judge.py       # 判题契约（12 项）
python tools/accept_isolation.py   # 隔离协议（集成项需要真实 VM）
```

最近一次本机结果：**381 passed**；判题契约 12/12；隔离协议的集成检查在无 VM 时返回
`环境未就绪（exit 2）`，不视为通过。

## 数据与隐私

- 记录内容：匿名编号、题目、提交代码、判题结果、提示等级/耗时、事件时间线。
- 不收集姓名、学号、手机号；编号与人的对应关系由组织者线下单独保存，不入库、不入导出。
- `data/*.sqlite`、导出文件、令牌与密钥均在本机，不进仓库。
- 正式使用前请自行完成知情同意、保存期限与访问控制方案。

## 许可

尚未选定开源许可证。在明确许可证之前，保留所有权利；如需引用或复用请先联系作者。

## 更多文档

- `docs/contracts.md`：判题与事件契约（字段、枚举、语义）
- `docs/INDEX.md`：文档索引与阅读顺序
- `docs/导读_给策划_2026-10-28.md`：给非工程读者的代码入口导览
