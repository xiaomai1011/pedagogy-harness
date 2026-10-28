# G-④ VM 判题 agent 最小协议（v1 · 2026-10-28）

> 状态：**协议草案 + 阶段 2 骨架（健康检查／握手）**。执行能力**尚未启用**。
> 本协议只固定「宿主 ↔ VM」的线上格式，**不代表隔离已验收**。
> 依据：评阅 m00935「先定最小 VM agent 接口，不要先上 Docker」。

## 1. 传输与会话

| 项 | v1 取值 | 说明 |
|---|---|---|
| 传输 | HTTP/1.1，明文 | 只在内部交换机 `PH-Judge-Internal` 上，**不出网、不做 TLS 终结** |
| 绑定 | `192.168.200.10:9443` | agent 只监听该地址；宿主侧源地址 `192.168.200.1` |
| 鉴权 | 请求头 `X-Agent-Token: <token>` | token 由宿主受控文件生成，**不进仓库、不进日志、不进判题结果** |
| 连接超时 | 2 s | 建连与健康检查 |
| 请求超时 | 10 s | 单次执行；**外层另有 5 s 判题超时**，两者不互相替代 |
| 请求体上限 | 1 MiB | 超出直接 `413`，不解析 |
| 协议版本 | `1` | 握手不一致 ⇒ 拒绝，不降级 |

## 2. 端点

### `GET /healthz`（无需 token）

```json
{"ok": true, "agent": "ph-judge-agent", "version": "0.1.0", "protocol": 1,
 "capabilities": {"execute": false, "file_isolation": false,
                  "network_isolation": false, "resource_limits": false,
                  "process_tree_kill": false}}
```

### `POST /v1/handshake`

请求：`{"protocol": 1}`；响应：`{"ok": true, "protocol": 1, "agent_version": "0.1.0", "capabilities": {...}}`。
`401`（token 错）／`409`（协议版本不匹配）都表示**拒绝**，宿主必须 fail closed。

### `POST /v1/execute`

阶段 5 起支持三类请求（按优先级）：

1. `{"registered_request": "<id>"}` —— 只跑**预登记**的固定请求（P001 验证用）；
2. 真实提交：`{"code": "...", "test_cases": [...], "timeout": 5, "output_limit": 2000}`
   （也接受 `question.test_cases`）；
3. `{"fixture": "<name>"}` —— 内置样例（隔离能力自检用）。

请求（字段即宿主适配器的输入契约）：

```json
{"request_id": "R-...", "language": "python", "code": "...",
 "timeout": 5, "output_limit": 2000,
 "test_cases": [{"case_index": 1, "input": "1 2\n", "expected": "3", "hidden": false}]}
```

**限制（agent 侧校验，超限直接 `agent_rejected`）**：`code` ≤ 200 KB；
`test_cases` 1–50 条，`input`/`expected` 各 ≤ 100 KB；`timeout` 夹在 1–5 秒；
`output_limit` 夹在 100–4000。agent 只做校验与转发（以 `phagent` 身份），
真正的进程由 **`phcode`** 身份的 worker 在沙箱里起，agent 不记录请求体。

响应：

```json
{"status": "pass|fail|timeout|crash|error",
 "total_cases": 2, "passed_cases": 2,
 "failed_cases": [{"case_index": 2, "hidden": true, "input": null,
                   "expected": null, "actual": "…（≤2000 字符）"}],
 "error_type": "none|syntax|runtime|timeout|wrong_output",
 "stdout": "…（≤2000）", "stderr": "…（≤2000）",
 "exec_ms": 12, "exit_code": 0,
 "cleanup_status": "ok|failed|skipped",
 "error_code": ""}
```

## 3. agent 失败分类（**线上错误码**）

| error_code | 含义 | 宿主侧动作 |
|---|---|---|
| `agent_unavailable` | 连不上、连接中断、握手无响应 | 拒绝执行（`IsolationUnavailable("agent_unreachable")`） |
| `agent_rejected` | token 错、协议不匹配、请求超出限制、执行能力未启用 | 拒绝执行（`IsolationUnavailable("agent_rejected")`） |
| `execution_timeout` | 学生程序超时 | 正常判题结果 `status="timeout"` |
| `execution_error` | 学生程序自身错误 | 正常判题结果 `status="crash"`／`"error"` |
| `cleanup_failed` | 执行后清理失败 | 判题结果**不改写**；写独立运行记录 `cleanup_failed` |

**铁律**：`agent_unavailable` / `agent_rejected` ⇒ 宿主返回 `judge_unavailable`，
**绝不回退到 `runner.py` 在 Windows 宿主上执行**。

## 4. 阶段计划（对应评阅 m00935 的实施顺序）

| 阶段 | 内容 | 状态 |
|---|---|---|
| 2 | agent 进程 + `/healthz` + `/v1/handshake`，宿主适配器 `check_isolation()` | **本轮实现** |
| 3 | 宿主注册 `VmAgentAdapter`（`PH_VM_AGENT_URL` / `PH_VM_AGENT_TOKEN`） | 待接 |
| 4 | 受控固定样例跑通请求—响应闭环 | 待做 |
| 5 | 正确／错输出／语法／运行错误／超时／残留清理 | 待做 |
| 6 | 文件／网络／资源／派生进程限制 | 待做 |
| 7 | 演示站恢复提交执行 | 待负责人确认 |
