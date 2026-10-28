"""G-② 第 5／6 项的**离线协议骨架**：隔离不可用协议、执行适配器边界、隔离／清理运行记录。

**本模块不执行任何学生代码**：不建进程、不建目录、不建 VM、不联网、不读密钥。
它只定义**接口与失败语义**，并提供可离线验证的边界函数。

三条口径（与 `docs/G1_运行目录隔离方案.md` §3.5／§3.7、
`docs/G2_安全隔离方案.md` §3.8／§5.5 一致）：

1. **隔离不可用 ⇒ 拒绝执行**。`IsolationUnavailable` 是**拒绝信号**，
   不是「判题器自己出故障」：后者由 `JudgeResult(status="error")` 表达，走**正常返回**路径
   （执行事件记 `finished` + `judge_status="error"`）；前者**抛异常**，
   由管线记 `aborted` + 受控码 `isolation_unavailable`（契约 §6.2.1／§6.2.4）。
2. **先检查、后启动**。`guarded_execute()` 是执行适配器的边界：检查未通过时
   `adapter.start()` **一次都不会被调用**（评阅明确不接受「先跑起来再加约束」）。
3. **判题事实与清理事实分开记**。清理失败**不得**改写判题结果，走**独立结构化运行记录**
   （G-① §3.5 方案 B）—— 该记录的**字段与消费方仍待负责人裁定**，
   本模块只落地骨架，**不得据此宣称「清理失败记录已验收」**。

**依赖方向**：岗位③ 的 `app/judge/runner.py` 在拒绝执行时
`from app.harness.isolation import IsolationUnavailable` 抛出即可 ——
这是岗位① 定义的协议，**不改 `judge()` 的签名**，也**不改 `error_type`／`stderr` 红线**。
"""

import sys
import uuid
from datetime import datetime

from app import db

#: 执行事件 `reason_code` 受控码：隔离不可用（契约 §6.2.4「扩展须登记」）
ISOLATION_UNAVAILABLE = "isolation_unavailable"

#: 清理失败的**运行记录事件名**（**通道未定**，见 G-① §3.5；此处只登记名字与语义）。
#:
#: ⚠️ 这**不是** `event_log.reason_code` 的取值：契约 §6.2.4 明确「`cleanup_failed` 仍未登记」，
#: 现阶段**不得**把它写进任何 `event_log.reason_code`（评阅 m02249 第 1 点核对项）。
CLEANUP_FAILED = "cleanup_failed"

#: 独立结构化运行记录的事件名（与判题结果分开记）
RUN_EVENT_ISOLATION_UNAVAILABLE = "isolation_unavailable"
RUN_EVENT_CLEANUP_FAILED = "cleanup_failed"
RUN_EVENTS = (RUN_EVENT_ISOLATION_UNAVAILABLE, RUN_EVENT_CLEANUP_FAILED)

#: `detail_code` 的**有限登记集合**（只增不改语义；新增须与契约 §6.2.4 一并登记）。
#:
#: **`detail_code` 是受控码，不是自由文本。** 只保证「不拼 `str(exc)`」是不够的：
#: 换一个字段承载任意字符串**照样会泄露**（评阅 m02036）。因此这里登记有限集合，
#: **未知值一律归一为 `UNREGISTERED_DETAIL_CODE`，绝不原样保存**。
DETAIL_CODES = frozenset({
    # 隔离未就绪（执行前检查失败）
    "not_ready",
    "hyperv_disabled",
    "vm_missing",
    "agent_unreachable",
    #: agent 可达但**明确拒绝**（token 错、协议不匹配、请求超出限制）
    "agent_rejected",
    "channel_unavailable",
    "scope_not_ready",
    "constraint_not_applied",
    # 清理失败（通道未定，只登记名字与语义；见 G-① §3.5）
    "rm_failed",
    "rm_timeout",
    "rm_denied",
    "record_write_failed",
})

#: 未知／非法 `detail_code` 的归一值：**固定码**，不携带原输入的任何片段。
UNREGISTERED_DETAIL_CODE = "unregistered_detail"

#: **宿主侧诊断码**（有限集合）：用于「尽力而为路径失败」必须**可见**、
#: 但**不得改写判题结果**的场合（G-③ 第 5 项：agent 侧写入失败 ⇒ 降级为宿主侧记录
#: 并标记 `record_write_failed`）。
WARN_CODES = frozenset({"record_write_failed"})

#: 诊断码 → **固定文案**。刻意不做参数化：一旦允许自由文本，这个字段就会变成
#: 与 `detail_code` 同样的泄露口子（评阅 m02036）。
_WARN_TEXT = {
    "record_write_failed": "运行记录写入失败（判题事实不受影响）",
}


def warn(code: str) -> None:
    """**宿主侧诊断输出**（stderr）：让「尽力而为路径」的失败**不静默**。

    - 只接受 `WARN_CODES` 内的码，文案是**固定常量**，**不接受任何自由文本**
      （异常原文、隐藏用例输入、密钥、模型原始响应都进不来）；
    - 未知码 ⇒ `unregistered_warn`，**不原样输出**；
    - **只输出，不写库、不抛错、不改写任何判题事实**：调用方（管线）已经
      catch 过底层异常，这里只负责让失败可被运维看见。

    为什么不用 `logging`／`print` 惯例：`app/` 内目前既无 `logging` 也无 `print`
    使用（全仓仅 `tools/` 与测试有输出），此处取最小实现并固定文案，
    避免为了「不静默」引入一个新的自由文本通道。
    """
    key = code if code in WARN_CODES else "unregistered_warn"
    text = _WARN_TEXT.get(key, "未登记的诊断码")
    print(f"[harness][{key}] {text}", file=sys.stderr)


def normalize_detail_code(value) -> str:
    """把任意输入归一为**登记集合内**的受控码。

    - `None` 或空白 ⇒ `""`（表示「本次没有更细的详情码」）；
    - 非字符串 ⇒ `UNREGISTERED_DETAIL_CODE`；
    - 不在 `DETAIL_CODES` 内 ⇒ `UNREGISTERED_DETAIL_CODE`。

    **归一而不是抛错**：调用方（管线落运行记录）是**尽力而为**路径，抛错会被吞掉、
    导致证据缺失；归一后至少留下「有个未登记码」这一条可关联事实。
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return UNREGISTERED_DETAIL_CODE
    code = value.strip()
    if code == "":
        return ""
    return code if code in DETAIL_CODES else UNREGISTERED_DETAIL_CODE


class IsolationUnavailable(RuntimeError):
    """**隔离设施不可用** ⇒ 拒绝本次执行。

    **不得**静默退回宿主上的普通 `subprocess`／`cwd=仓库目录`／「资源管理基础」路径
    （G-② §3.8、G-① §3.7）。

    `detail_code` 是**受控码**，只允许 `DETAIL_CODES` 内的值；**未知值会被归一为
    `UNREGISTERED_DETAIL_CODE`**，不原样保存。`detail` 是**脱敏说明**，它**不持久化**
    到任何表（仅用于人工排查），因此**不得**把隐藏用例输入、密钥或模型原始响应塞进
    `detail` 再期待它被记住（契约 §6.8）。
    """

    def __init__(self, detail_code: str = "not_ready", detail: str = "",
                 stage: str = "precheck"):
        self.detail_code = normalize_detail_code(detail_code) or "not_ready"
        self.detail = detail or ""
        #: 拒绝发生在哪一段：`precheck`（检查未通过、**学生代码一次都没启动**）
        #: 或 `executing`（已进入隔离环境后中止）。管线据此区分「未执行」与「执行中异常中止」。
        self.stage = stage if stage in ("precheck", "executing") else "precheck"
        super().__init__(f"隔离不可用：{self.detail_code}")


class RunRecordWriteFailed(RuntimeError):
    """独立运行记录**写入失败**。

    **不得静默吞掉**；但调用方也**不得**因此改写判题结果（判题事实与清理事实分开记）。
    """


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def reason_code_of(exc: BaseException) -> str:
    """`judge` 抛出异常时的**受控码**：隔离不可用**单列**，其余仍记 `judge_raised`。"""
    return ISOLATION_UNAVAILABLE if isinstance(exc, IsolationUnavailable) else "judge_raised"


def reason_text(exc: BaseException) -> str:
    """事件表 `reason` 的**受控说明**：只放固定文案 + 受控码／异常类型名。

    **截断 `str(exc)` 不等于脱敏** —— 不拼 `str(exc)`（契约 §6.8）。
    """
    if isinstance(exc, IsolationUnavailable):
        return f"隔离不可用：{exc.detail_code}"
    return f"异常类型：{type(exc).__name__}"


class ExecutionAdapter:
    """执行适配器的**最小协议**（鸭子类型即可，不必继承）。

    - `check_isolation()`：就绪返回真值；**未就绪必须抛 `IsolationUnavailable`**
      （返回假值也可以，由 `guarded_execute()` 统一转成异常）；
    - `start(payload)`：**真正启动**学生代码执行（真实实现属岗位③）。
    """

    def check_isolation(self) -> bool:  # pragma: no cover - 协议声明
        raise NotImplementedError

    def start(self, payload):  # pragma: no cover - 协议声明
        raise NotImplementedError


#: 进程内**唯一**的执行适配器登记位。生产进程默认 **None**（未接入 ⇒ 一律拒绝执行）。
_ADAPTER = None


def register_execution_adapter(adapter) -> None:
    """登记执行适配器（VM agent 接入后由启动代码调用一次）。

    **未登记 = 隔离未就绪**：管线会抛 `IsolationUnavailable("not_ready")` 并拒绝执行，
    绝不回退到宿主上的普通 `subprocess`（G-② §3.8）。
    """
    global _ADAPTER
    _ADAPTER = adapter


def registered_execution_adapter():
    """当前登记的适配器；未登记返回 `None`。"""
    return _ADAPTER


def clear_execution_adapter() -> None:
    """撤销登记（测试用；生产进程不应调用）。"""
    global _ADAPTER
    _ADAPTER = None


def guarded_execute(adapter, payload, *, request_id: str = "", phase: str = "",
                    case_index: int | None = None):
    """**执行适配器边界：先检查、后启动。**

    1. `adapter` 为 `None` ⇒ 取已登记的适配器；仍为 `None` ⇒ 立即抛
       `IsolationUnavailable("not_ready")`（**fail closed**：没接隔离就不执行）；
    2. 调 `adapter.check_isolation()`；
    3. 未就绪（抛 `IsolationUnavailable`，或返回假值）⇒ 立刻抛 `IsolationUnavailable`，
       且 `adapter.start()` **一次都不会被调用**；
    4. 就绪 ⇒ 返回 `adapter.start(payload)` 的结果；`start()` 期间抛出的
       `IsolationUnavailable` 会被标记 `stage="executing"`（已进入隔离环境后中止）。

    `request_id`／`phase`／`case_index` 只作**关联上下文**，本函数不写库
    （运行记录的写入方仍待负责人裁定）。
    """
    if adapter is None:
        adapter = registered_execution_adapter()
    if adapter is None:
        raise IsolationUnavailable("not_ready", "未登记执行适配器：隔离环境尚未接入")
    ready = adapter.check_isolation()
    if not ready:
        raise IsolationUnavailable("not_ready", "隔离设施未就绪")
    try:
        return adapter.start(payload)
    except IsolationUnavailable as exc:
        exc.stage = "executing"
        raise


def new_run_id() -> str:
    """生成**稳定执行标识**：一次执行尝试一个 `run_id`（用于关联到具体请求）。"""
    return uuid.uuid4().hex


def record_run_event(*, request_id: str, event: str, detail_code: str = "",
                     phase: str = "", case_index: int | None = None,
                     run_id: str | None = None, ts: str | None = None,
                     conn=None) -> str:
    """写一条**独立结构化运行记录**（与判题结果分开），返回 `run_id`。

    硬要求（G-① §3.5）：

    - **必须能关联请求**：`request_id` 不得为空，否则与「打印一条日志」无异；
    - 只存**受控码 + 脱敏说明**：不得写入异常全文、隐藏用例输入、密钥、模型原始响应；
      `detail_code` 经 `normalize_detail_code()` 归一，**未知值不原样入库**；
    - **写入失败抛 `RunRecordWriteFailed`**（不静默吞掉）；
    - 调用方**不得**因本函数失败而改写 `JudgeResult`。
    """
    if event not in RUN_EVENTS:
        raise ValueError(f"未登记的运行记录事件：{event}")
    if not request_id:
        raise ValueError("运行记录必须能关联请求：request_id 不得为空")
    rid = run_id or new_run_id()
    row = {"run_id": rid, "request_id": request_id, "case_index": case_index,
           "phase": phase, "event": event,
           "detail_code": normalize_detail_code(detail_code),
           "ts": ts or _now()}
    try:
        if conn is not None:
            db.log_isolation_run_conn(conn, **row)
        else:
            db.log_isolation_run(**row)
    except Exception as exc:  # 不静默：转成受控异常，且**不带**异常原文
        raise RunRecordWriteFailed(f"运行记录写入失败：{type(exc).__name__}") from exc
    return rid


def read_run_events(*, request_id: str | None = None, run_id: str | None = None,
                    event: str | None = None) -> list[dict]:
    """读运行记录（**消费方**示例：隔离验收脚本与运维排查）。"""
    return db.get_isolation_runs(request_id=request_id, run_id=run_id, event=event)
