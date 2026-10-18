"""隔离验收入口 · **离线骨架**（G-② 第 6 项）。

**本脚本不启动任何学生进程、不联网、不读密钥、不安装任何东西**；只会在
**系统临时目录**下建一个本次运行独占的目录（退出时清理），**不在仓库里建任何东西**。
它验的是**协议、接线与失败处理**；真正的隔离机制（VM／agent／cgroup／netns）
只能在系统集成时验证，**本脚本不得据其绿灯宣称「安全隔离已通过」**。

与 `tools/accept_judge.py` 的分工：那个脚本验**判题契约**（12 项）；
本脚本验**隔离**，两者**独立退出码**，**不得把本脚本的检查塞进 accept_judge.py**。

## 数据库前置条件（本脚本会写**合成**运行记录）

- 本脚本在 `main()` 里创建**本次运行独占**的临时目录，把 `PH_DB` 指向其中的
  `accept.sqlite`，**然后才导入 `app`**——因为 `app/db.py` 在 **import 时**就把
  `PH_DB` 解析成模块级常量 `DB_PATH`，晚一步设置就无效。
- **导入后立即校验实际 `db.DB_PATH`**：不等于本次运行临时库 ⇒ **在写入前拒绝继续**
  （退出码 3），绝不把合成数据写进真实 `pedagogy.sqlite`。
- **不删除固定名称的既有文件**：临时目录由 `tempfile.mkdtemp()` 独占创建，
  退出时只清理**本次**创建的目录。
- **清理前必须先 `gc.collect()`**：产品侧若干调用点用的是
  `with connect() as conn:` —— 它只保证提交／回滚，**不关连接**；
  未关闭的 `sqlite3.Connection` 会一直占着 Windows 文件句柄，令 `rmtree`
  报 `PermissionError`，直到一次 gc 回收该连接对象才释放。这是**产品侧**的
  已知问题（见 `app/db.py` 里 `log_isolation_run()` 的说明），本脚本不擅自改产品代码，
  只在清理前主动回收一次；回收后仍删不掉则如实打印警告，**不静默吞掉**。

用法：
    python tools/accept_isolation.py             # 逐项检查，出退出码
    python tools/accept_isolation.py --verbose   # 额外打印细节

退出码：
    0 = 隔离门**通过**（仅当所有关键项**真正执行**且通过）
    1 = **失败**（检查执行了但不通过）
    2 = **环境未就绪**（执行器仍是桩／VM 未安装／机制未实现／关键项被跳过）
    3 = **拒绝继续**（数据库前置条件不满足：实际库不是本次运行独占的临时库）

    1、2、3 **都属非成功**。**桩、设施缺失、未实现或关键项跳过时，绝不返回「通过」。**

不覆盖（须在可销毁测试环境里人工／系统集成验证，见 G-② §5.3、§15.2）：
    - 真实 VM／agent／管理通道是否可用；
    - 文件系统边界（绝对路径读取、`..`、符号链接、重解析点）；
    - 网络边界（禁网是否可证明，而非「无默认路由」）；
    - 资源限制是否真正生效（CPU／内存／进程数／输出／磁盘）；
    - 超时是否**清掉整个进程树**（含学生代码自建的子进程）；
    - 约束是否**在学生代码开始执行前**已就绪；
    - 清理失败在真实环境中的可观测性。
"""

import argparse
import gc
import inspect
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

#: 真实数据库（仓库根）：本脚本**绝不允许**写入它。
REAL_DB = (REPO_ROOT / "pedagogy.sqlite").resolve()

RUNNER = REPO_ROOT / "app" / "judge" / "runner.py"
PIPELINE = REPO_ROOT / "app" / "harness" / "pipeline.py"
CONTRACTS = REPO_ROOT / "docs" / "contracts.md"
PROTOCOL_TESTS = REPO_ROOT / "tests" / "test_isolation_protocol.py"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_NOT_READY = 2
EXIT_REFUSED = 3

#: 检查项种类：`protocol` 必须真正执行并通过；`integration` 需真实环境，未就绪即跳过。
PROTOCOL = "protocol"
INTEGRATION = "integration"

#: 本次运行的独占临时目录与临时库（由 `_bootstrap_run_dir()` 设置，退出时清理）。
RUN_DIR: Path | None = None
RUN_DB: Path | None = None

#: 延迟导入的应用模块（必须在设置好 `PH_DB` **之后**才导入）。
isolation = None
runner_mod = None


class DbPathMismatch(RuntimeError):
    """实际使用的数据库不是**本次运行独占**的临时库 ⇒ 写入前拒绝继续。"""


def _resolve_db_path(raw) -> Path:
    path = Path(str(raw))
    return path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()


def _bootstrap_run_dir() -> Path:
    """**在 `main()` 里、导入 `app` 之前**创建本次运行独占的临时目录。

    三条硬要求（评阅 m02036）：

    - **每次运行独有**：`tempfile.mkdtemp()` 保证两次运行不共享目录、不互相干扰；
    - **不删除既有文件**：不再对固定文件名做 `unlink()`，只创建新目录；
    - **导入前配置**：`PH_DB` 必须在导入 `app.db` 之前设好，否则 `DB_PATH` 已被缓存。
      若发现 `app.db` 已被提前导入，直接抛 `DbPathMismatch`，**不写入任何数据**。
    """
    global RUN_DIR, RUN_DB
    existing = sys.modules.get("app.db")
    if existing is not None:
        raise DbPathMismatch(
            f"app.db 已被提前导入（DB_PATH={_resolve_db_path(existing.DB_PATH)}）"
            f"⇒ 此刻再设 PH_DB 已无效，拒绝继续")
    RUN_DIR = Path(tempfile.mkdtemp(prefix="pedagogy_accept_isolation_"))
    RUN_DB = RUN_DIR / "accept.sqlite"
    os.environ["PH_DB"] = str(RUN_DB)
    return RUN_DIR


def _load_app_modules():
    """导入应用模块，并**立即校验**实际 `DB_PATH` 是否就是本次运行临时库。"""
    global isolation, runner_mod
    from app import db as db_mod
    from app.harness import isolation as _isolation
    from app.judge import runner as _runner

    actual = _resolve_db_path(db_mod.DB_PATH)
    if actual == REAL_DB:
        raise DbPathMismatch(f"实际数据库指向真实库 {REAL_DB} ⇒ 拒绝继续（写入前拦截）")
    if RUN_DB is None or actual != RUN_DB.resolve():
        raise DbPathMismatch(
            f"实际数据库 {actual} ≠ 本次运行临时库 {RUN_DB} ⇒ 拒绝继续（写入前拦截）")

    isolation, runner_mod = _isolation, _runner
    return db_mod


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def runner_is_stub() -> bool:
    """岗位③ 执行器是否仍是桩（桩恒返回失败、**根本不执行学生代码**）。"""
    if runner_mod is None:
        return "[桩]" in _source(RUNNER)
    try:
        src = inspect.getsource(runner_mod.judge)
    except (OSError, TypeError):
        src = ""
    return "[桩]" in src or "[桩]" in _source(RUNNER)


# ---------------------------------------------------------------- protocol 检查

def check_run_db_isolated(verbose: bool):
    """**写入前的安全前置条件**：实际库必须是本次运行独占的临时库，且不是真实库。"""
    from app import db as db_mod
    if RUN_DIR is None or RUN_DB is None:
        return False, "本次运行临时库尚未建立"
    used = _resolve_db_path(db_mod.DB_PATH)
    if used == REAL_DB:
        return False, f"本脚本正对着真实数据库写入：{used}"
    if used != RUN_DB.resolve():
        return False, f"应强制使用 {RUN_DB}，实际 {used}"
    if RUN_DIR.resolve() not in used.parents:
        return False, f"临时库不在本次运行目录内：{used}"
    return True, f"本次运行独占临时库 {used}（真实库 {REAL_DB} 未被触碰）"


def check_protocol_module(verbose: bool):
    """协议模块存在，且关键符号齐全（异常类、受控码、边界函数、运行记录）。"""
    missing = [n for n in ("IsolationUnavailable", "guarded_execute", "reason_code_of",
                           "reason_text", "record_run_event", "read_run_events",
                           "ISOLATION_UNAVAILABLE", "CLEANUP_FAILED", "RUN_EVENTS",
                           "DETAIL_CODES", "UNREGISTERED_DETAIL_CODE", "normalize_detail_code")
               if not hasattr(isolation, n)]
    if missing:
        return False, f"协议模块缺少符号：{', '.join(missing)}"
    return True, "异常类／受控码／边界函数／运行记录接口齐全"


def check_reason_code_split(verbose: bool):
    """受控码分流：隔离不可用**单列**，其余异常仍记 `judge_raised`。"""
    got = isolation.reason_code_of(isolation.IsolationUnavailable("x"))
    other = isolation.reason_code_of(RuntimeError("x"))
    if got != "isolation_unavailable":
        return False, f"隔离不可用应记 isolation_unavailable，实际 {got!r}"
    if other != "judge_raised":
        return False, f"普通异常仍应记 judge_raised，实际 {other!r}"
    return True, "isolation_unavailable 单列｜其他异常保持 judge_raised"


def check_detail_code_limited(verbose: bool):
    """**`detail_code` 必须是有限登记集合**：未知值一律归一，不原样保存。

    「不拼 `str(exc)`」并不足够——换一个字段承载任意字符串照样会泄露（m02036）。
    """
    secret = "SECRET_MARKER_42"
    exc = isolation.IsolationUnavailable(detail_code=secret)
    if exc.detail_code != isolation.UNREGISTERED_DETAIL_CODE:
        return False, f"未知 detail_code 应归一，实际 {exc.detail_code!r}"
    if secret in exc.detail_code or secret in isolation.reason_text(exc):
        return False, "未知 detail_code 的原文被保留"

    rid = isolation.record_run_event(request_id="ACC_ISO_DC",
                                     event="isolation_unavailable", detail_code=secret)
    row = isolation.read_run_events(run_id=rid)[0]
    if row["detail_code"] != isolation.UNREGISTERED_DETAIL_CODE:
        return False, f"运行记录应存归一码，实际 {row['detail_code']!r}"
    if secret in str(row):
        return False, "运行记录出现了未登记 detail_code 的原文"
    return True, "detail_code 只接受登记集合，未知值归一为固定码"


def check_reason_text_no_echo(verbose: bool):
    """`reason` 只放受控说明，**不拼 `str(exc)`**（截断不等于脱敏）。"""
    secret = "HIDDEN_INPUT_MARKER_7"
    text = isolation.reason_text(isolation.IsolationUnavailable("scope_not_ready", secret))
    if secret in text:
        return False, "reason 携带了异常原文"
    if "scope_not_ready" not in text:
        return False, "reason 未保留受控码"
    return True, "reason 只含受控说明（受控码 + 固定文案）"


def check_guarded_execute_refuses(verbose: bool):
    """**执行适配器边界**：检查未通过 ⇒ `start()` 一次都不调用。"""
    calls = {"n": 0}

    class _Adapter:
        def check_isolation(self):
            return False

        def start(self, payload):
            calls["n"] += 1
            return None

    try:
        isolation.guarded_execute(_Adapter(), {"code": "x"}, request_id="ACC_ISO_1")
    except isolation.IsolationUnavailable:
        pass
    except Exception as exc:                       # 不得是别的异常类型
        return False, f"应抛 IsolationUnavailable，实际 {type(exc).__name__}"
    else:
        return False, "检查未通过却未拒绝执行"
    if calls["n"] != 0:
        return False, f"检查未通过却调用了 start() {calls['n']} 次"
    return True, "检查未通过 ⇒ 拒绝执行且 start() 零调用"


def check_guarded_execute_starts_once(verbose: bool):
    """就绪 ⇒ 恰好启动一次（**不得**多启动、不得空转）。"""
    calls = {"n": 0}

    class _Adapter:
        def check_isolation(self):
            return True

        def start(self, payload):
            calls["n"] += 1
            return {"ok": True}

    out = isolation.guarded_execute(_Adapter(), {"code": "x"}, request_id="ACC_ISO_2")
    if out != {"ok": True}:
        return False, f"返回值未透传：{out!r}"
    if calls["n"] != 1:
        return False, f"start() 应恰好调用 1 次，实际 {calls['n']} 次"
    return True, "就绪 ⇒ start() 恰好调用 1 次"


def check_run_record_correlation(verbose: bool):
    """运行记录**必须能关联请求**，且只存受控码（不存原文）。"""
    try:
        isolation.record_run_event(request_id="", event="cleanup_failed")
    except ValueError:
        pass
    else:
        return False, "request_id 为空时不得写运行记录"
    rid = isolation.record_run_event(request_id="ACC_ISO_REC", event="cleanup_failed",
                                     detail_code="rm_failed")
    rows = isolation.read_run_events(run_id=rid)
    if len(rows) != 1:
        return False, f"应能按 run_id 读回 1 条，实际 {len(rows)} 条"
    allowed = {"run_id", "request_id", "case_index", "phase", "event", "detail_code", "ts"}
    extra = set(rows[0]) - allowed
    if extra:
        return False, f"运行记录出现可承载原文的字段：{', '.join(sorted(extra))}"
    return True, "可按 request_id／run_id 关联，字段最小且只有受控码"


def check_run_record_write_failure_controlled(verbose: bool):
    """写入失败**不静默吞掉**，且转成的受控异常不携带底层原文。"""
    from app import db as db_mod
    real = db_mod.log_isolation_run
    try:
        db_mod.log_isolation_run = lambda **k: (_ for _ in ()).throw(
            RuntimeError("disk full at C:\\secret\\path"))
        try:
            isolation.record_run_event(request_id="ACC_ISO_W", event="cleanup_failed")
        except isolation.RunRecordWriteFailed as exc:
            if "disk full" in str(exc):
                return False, "受控异常携带了底层异常原文"
            return True, "写入失败转受控异常，不含底层原文"
        except Exception as exc:
            return False, f"应抛 RunRecordWriteFailed，实际 {type(exc).__name__}"
        return False, "写入失败被静默吞掉"
    finally:
        db_mod.log_isolation_run = real


def check_pipeline_wired(verbose: bool):
    """管线已接线：`_mark_judge_failure` 按异常类型分流，且落独立运行记录。"""
    src = _source(PIPELINE)
    if not src:
        return False, "找不到 app/harness/pipeline.py"
    problems = []
    if "isolation.reason_code_of(exc)" not in src:
        problems.append("未用 isolation.reason_code_of 分流受控码")
    if "_record_isolation_unavailable" not in src:
        problems.append("未落隔离不可用运行记录")
    if "isolation.reason_text(exc)" not in src:
        problems.append("reason 未走 isolation.reason_text")
    if "isolation.normalize_detail_code(" not in src:
        problems.append("运行记录 detail_code 未归一")
    if problems:
        return False, "；".join(problems)
    return True, "受控码分流 + 独立运行记录 + 脱敏说明 + detail_code 归一均已接线"


def check_best_effort_failure_visible(verbose: bool):
    """**尽力而为路径失败不得静默**：须留下受控诊断码，且不带底层原文。

    评阅 m02249 第 2 点：未知详情码可以归一，但**数据库写入失败不能被静默忽略**。
    这里在**管线边界**注入写入失败，断言宿主侧出现 `record_write_failed`，
    同时判题事实（`aborted` + `isolation_unavailable`）**未被改写**。
    """
    import contextlib
    import io

    from app import db as db_mod
    from app.harness import pipeline as pipeline_mod
    from app.harness.config_switch import PRESETS

    def _judge_down(*a, **k):
        raise isolation.IsolationUnavailable("scope_not_ready")

    def _boom(*a, **k):
        raise RuntimeError("disk full at C:\\secret\\path")

    real_judge, real_log = pipeline_mod.judge, db_mod.log_isolation_run
    buf = io.StringIO()
    try:
        pipeline_mod.judge, db_mod.log_isolation_run = _judge_down, _boom
        with contextlib.redirect_stderr(buf):
            try:
                pipeline_mod.handle_submission(participant_id="ACC_ISO_VIS",
                                               question_id="P001", code="print(1)",
                                               config=PRESETS["exp"])
            except isolation.IsolationUnavailable:
                pass
            except Exception as exc:
                return False, f"应抛 IsolationUnavailable，实际 {type(exc).__name__}"
    finally:
        pipeline_mod.judge, db_mod.log_isolation_run = real_judge, real_log

    err = buf.getvalue()
    if "record_write_failed" not in err:
        return False, "写入失败被静默忽略（未留下受控诊断码）"
    if "disk full" in err:
        return False, "诊断输出携带了底层异常原文"

    exec_rows = [r for r in db_mod.get_events(participant_id="ACC_ISO_VIS")
                 if r["event_type"] == "execution"]
    if len(exec_rows) != 1 or exec_rows[0]["reason_code"] != "isolation_unavailable":
        return False, "写入失败改写了判题事实"
    return True, "写入失败留下受控诊断码，且判题事实未被改写"


def check_contract_registered(verbose: bool):
    """契约 §6.2.4 已登记 `isolation_unavailable`（「扩展须登记」）。"""
    src = _source(CONTRACTS)
    if not src:
        return False, "找不到 docs/contracts.md"
    if "isolation_unavailable" not in src:
        return False, "契约受控码表未登记 isolation_unavailable"
    if "unregistered_detail" not in src:
        return False, "契约未登记 detail_code 的有限集合与归一码"
    return True, "契约已登记 isolation_unavailable 与 detail_code 归一码"


def check_protocol_tests_exist(verbose: bool):
    """离线协议测试存在，且**明确区分**两层（管线映射 / 适配器拒绝启动）。"""
    src = _source(PROTOCOL_TESTS)
    if not src:
        return False, "找不到 tests/test_isolation_protocol.py"
    problems = []
    if "guarded_execute" not in src:
        problems.append("缺少执行适配器边界测试")
    if "IsolationUnavailable" not in src:
        problems.append("缺少管线异常映射测试")
    if "started == 0" not in src and "started` 未被调用" not in src:
        problems.append("缺少「start() 未被调用」断言")
    if "UNREGISTERED_DETAIL_CODE" not in src:
        problems.append("缺少 detail_code 有限集合测试")
    if problems:
        return False, "；".join(problems)
    return True, "两层测试齐备（映射 + 拒绝启动），且含 detail_code 受控断言"


# ---------------------------------------------------------------- integration 检查（需真实环境）

def check_real_execution_environment(verbose: bool):
    """真实执行环境是否已建成（VM／agent 可达）。

    本脚本**无法**在线检查 VM —— 只认一条：执行器仍是桩即**未就绪**。
    岗位③ 交付真实现后，此检查仍**不足以**证明隔离，须由 §5.3 系统集成测试补足。
    """
    if runner_is_stub():
        return False, "执行器仍是桩（app/judge/runner.py 含 [桩]）：无真实执行环境"
    return False, "执行器已非桩，但本脚本无法在线验证 VM／agent／机制 ⇒ 仍不得判通过"


def check_mechanisms_enforced(verbose: bool):
    """约束是否在代码运行前生效（真实机制验证）。"""
    return False, "需在可销毁测试环境验证：本脚本不启动学生进程，无法判定"


# ---------------------------------------------------------------- 主流程

#: **顺序有意义**：数据库隔离检查放第一位，任何写库检查都在它之后。
CHECKS = (
    ("使用本次运行独占的临时库（不碰真实库）", PROTOCOL, check_run_db_isolated),
    ("协议模块符号齐全", PROTOCOL, check_protocol_module),
    ("受控码分流（isolation_unavailable 单列）", PROTOCOL, check_reason_code_split),
    ("detail_code 只接受登记集合（未知值归一）", PROTOCOL, check_detail_code_limited),
    ("reason 不含异常原文", PROTOCOL, check_reason_text_no_echo),
    ("检查未通过 ⇒ 拒绝执行（start 零调用）", PROTOCOL, check_guarded_execute_refuses),
    ("就绪 ⇒ start 恰好一次", PROTOCOL, check_guarded_execute_starts_once),
    ("运行记录可关联请求且字段最小", PROTOCOL, check_run_record_correlation),
    ("运行记录写失败受控（不静默、不带原文）", PROTOCOL, check_run_record_write_failure_controlled),
    ("尽力而为路径失败可见（不静默、不改判题事实）", PROTOCOL, check_best_effort_failure_visible),
    ("管线已接线（分流 + 独立记录 + 归一）", PROTOCOL, check_pipeline_wired),
    ("契约已登记 isolation_unavailable 与归一码", PROTOCOL, check_contract_registered),
    ("离线协议测试齐备（两层且断言零调用）", PROTOCOL, check_protocol_tests_exist),
    ("真实执行环境已建成", INTEGRATION, check_real_execution_environment),
    ("约束在代码运行前生效", INTEGRATION, check_mechanisms_enforced),
)

NOT_COVERED = (
    "真实 VM／agent／管理通道可用性（须在系统集成时验）",
    "文件系统边界：绝对路径读取、..、符号链接、重解析点",
    "网络边界是否**可证明**禁网（不能只用「无默认路由」）",
    "资源限制是否真正生效（CPU／内存／进程数／输出／磁盘）",
    "超时是否清掉**整棵**进程树（含学生自建子进程）",
    "约束是否在学生代码**开始执行前**已就绪（真实机制）",
    "清理失败在真实环境中的可观测性与消费方",
)


def main(argv):
    parser = argparse.ArgumentParser(description="隔离验收入口（离线骨架）")
    parser.add_argument("--verbose", action="store_true", help="打印每项细节")
    args = parser.parse_args(argv)

    print("=" * 74)
    print("隔离验收（G-② 第 6 项 · 离线骨架）")
    print("=" * 74)

    # ---- 数据库前置条件：本次运行独占临时目录，导入 app 前配置，写入前校验 ----
    try:
        run_dir = _bootstrap_run_dir()
    except DbPathMismatch as exc:
        print(f"  ⛔ **拒绝继续**：{exc}")
        return EXIT_REFUSED

    print(f"  本次运行临时目录：{run_dir}")
    print(f"  本次运行临时库：{RUN_DB}")

    try:
        try:
            _load_app_modules()
        except DbPathMismatch as exc:
            print(f"  ⛔ **拒绝继续**：{exc}")
            return EXIT_REFUSED

        # 写入前的硬闸：不通过就直接拒绝，不进入任何会写库的检查。
        ok, detail = check_run_db_isolated(args.verbose)
        print(f"  {'✅' if ok else '❌'} 使用本次运行独占的临时库（不碰真实库） —— {detail}")
        if not ok:
            print("  ⛔ **拒绝继续**：数据库前置条件不满足，未写入任何数据。")
            return EXIT_REFUSED

        if runner_is_stub():
            print("⚠ 岗位③ 执行器仍是桩（app/judge/runner.py 含 [桩] 标记）：")
            print("  真实学生代码执行路径**尚不存在**，本脚本只能验协议与失败处理。")

        failed, skipped = [], []
        for name, kind, fn in CHECKS:
            if fn is check_run_db_isolated:
                continue                       # 已在硬闸处执行过
            try:
                ok, detail = fn(args.verbose)
            except Exception as exc:           # 检查自身炸了也算不通过
                ok, detail = False, f"检查自身异常：{type(exc).__name__}"
            mark = "✅" if ok else ("⏭" if kind == INTEGRATION else "❌")
            print(f"  {mark} {name} —— {detail}")
            if not ok:
                (skipped if kind == INTEGRATION else failed).append(name)

        total = len(CHECKS) - 1
        print("-" * 74)
        print("本脚本**不覆盖**（须在可销毁测试环境验证）：")
        for item in NOT_COVERED:
            print(f"  · {item}")

        print("-" * 74)
        # `total` 已扣掉硬闸处单独跑过的那一项；它若没过早已 return，故分子分母一致。
        print(f"协议项：通过 {total - len(failed) - len(skipped)}／{total - len(skipped)}"
              f"；集成项：未执行 {len(skipped)} 项")

        if failed:
            print("  ⛔ 隔离验收未通过 —— 不得接入真实学生提交入口。")
            return EXIT_FAIL
        if skipped:
            print("  ⛔ **环境未就绪**：集成项未执行，本脚本**不得**判为通过。")
            print("     桩执行器／VM 未安装／机制未实现／关键项被跳过，均属此类。")
            return EXIT_NOT_READY

        print("  ✅ 离线协议骨架通过 —— 但**不等于隔离已验收**：")
        print("     真实机制须由系统集成测试与负责人确认后，方可开放真实执行入口。")
        return EXIT_PASS
    finally:
        # 只清理**本次**创建的目录；不碰任何既有文件。
        # 先 gc.collect()：产品侧 `with connect() as conn:` 不关连接，未回收的连接
        # 会占住 Windows 文件句柄，令 rmtree 报 PermissionError（详见模块 docstring）。
        # 清理失败**不静默吞掉**（残留临时库会干扰后续运行），但也不改变本次结论。
        if run_dir is not None:
            # 最多三次：产品侧连接对象与异常回溯之间存在引用环，需靠 gc 打破；
            # 一次 gc 通常够，但句柄释放与回收时序并不完全同步，故留重试余量。
            err = None
            for _ in range(3):
                gc.collect()
                try:
                    shutil.rmtree(run_dir)
                    err = None
                    break
                except OSError as exc:
                    err = exc
            if err is not None:
                print(f"  ⚠ 本次运行临时目录清理失败（不影响本次结论）："
                      f"{type(err).__name__} —— {run_dir}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
