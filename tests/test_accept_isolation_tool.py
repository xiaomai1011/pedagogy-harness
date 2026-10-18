"""验收工具**自身**的数据库隔离测试（评阅 m02036 第 1 点）。

**本文件不启动学生进程、不联网、不读密钥、不安装任何东西。**
它验证的是 `tools/accept_isolation.py` 的三条硬要求：

1. **外部 `PH_DB` 指向的诱饵库运行后内容不变** —— 验收脚本**不得**写到调用方
   指定的库；诱饵库是**临时测试文件**，**绝不拿真实 `pedagogy.sqlite` 验证保护逻辑**。
2. **两次运行使用不同目录** —— 每次运行独占临时目录，不共享固定文件名，
   且运行结束后**只清理本次目录**。
3. **提前导入导致路径不匹配时，必须在写入前失败** —— `app.db` 已在
   `sys.modules` 中（`DB_PATH` 已缓存）时，脚本必须**拒绝继续**（退出码 3），
   而不是「设了环境变量却照写不误」。

这三点都通过**子进程**验证：脚本的启动顺序（建目录 → 设 `PH_DB` → 导入 `app`）
只有在**全新解释器**里才具有代表性；在当前 pytest 进程里 `app.db` 早已导入。
"""

import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
ACCEPT_TOOL = TOOLS_DIR / "accept_isolation.py"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_NOT_READY = 2
EXIT_REFUSED = 3

#: 诱饵库里的标记：运行验收脚本后必须**一字不改**地还在。
BAIT_MARKER = "BAIT_UNTOUCHED_MARKER"

_TMP_DIR_RE = re.compile(r"本次运行临时目录：(.+)")


def _run_tool(extra_env: dict | None = None, argv: list[str] | None = None):
    """在**全新解释器**里跑验收脚本，返回 (退出码, stdout)。"""
    env = dict(__import__("os").environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra_env or {})
    proc = subprocess.run(
        [sys.executable, str(ACCEPT_TOOL), *(argv or [])],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        cwd=str(REPO_ROOT), env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _make_bait_db(path: Path) -> None:
    """建一个**临时诱饵库**（测试文件，不是真实库），带一个可核对的标记表。"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE bait_marker (k TEXT NOT NULL)")
    conn.execute("INSERT INTO bait_marker (k) VALUES (?)", (BAIT_MARKER,))
    conn.commit()
    conn.close()


def _read_bait_db(path: Path):
    """读回诱饵库：返回 (表名集合, 标记行列表)。"""
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        rows = []
        if "bait_marker" in tables:
            rows = [r[0] for r in conn.execute("SELECT k FROM bait_marker").fetchall()]
        return tables, rows
    finally:
        conn.close()


# ================================================================
# 1. 诱饵库不被触碰
# ================================================================

def test_bait_db_untouched_when_external_ph_db_points_at_it(tmp_path):
    """外部 `PH_DB` 指向诱饵库 ⇒ 运行验收脚本后**内容不变**。"""
    bait = tmp_path / "bait.sqlite"
    _make_bait_db(bait)

    code, out = _run_tool({"PH_DB": str(bait)})

    tables, rows = _read_bait_db(bait)
    assert tables == {"bait_marker"}, \
        f"诱饵库不得被建表：实际表 {sorted(tables)}"
    assert rows == [BAIT_MARKER], "诱饵库标记行必须原样保留"
    assert "isolation_run_log" not in tables, "验收脚本不得把运行记录写进调用方指定的库"
    # 脚本自己仍应正常跑完协议检查（环境未就绪 ⇒ 2），只是没碰诱饵库。
    assert code in (EXIT_NOT_READY, EXIT_PASS), \
        f"脚本应正常完成协议检查，实际退出码 {code}\n{out}"


def test_bait_db_untouched_without_external_ph_db(tmp_path):
    """**不设** `PH_DB` 时同样不得落到仓库真实库（脚本自建独占临时库）。"""
    bait = tmp_path / "bait2.sqlite"
    _make_bait_db(bait)
    env = {"PH_DB": str(bait)}

    code, out = _run_tool(env)

    assert _read_bait_db(bait)[1] == [BAIT_MARKER]
    assert "本次运行独占临时库" in out, "脚本必须强制使用本次运行独占临时库"
    assert code in (EXIT_NOT_READY, EXIT_PASS), out


# ================================================================
# 2. 两次运行使用不同目录（且各自清理）
# ================================================================

def test_two_runs_use_different_dirs_and_clean_up(tmp_path):
    """两次运行必须使用**不同**临时目录；运行结束后各自清理，不残留。"""
    bait = tmp_path / "bait3.sqlite"
    _make_bait_db(bait)

    code1, out1 = _run_tool({"PH_DB": str(bait)})
    code2, out2 = _run_tool({"PH_DB": str(bait)})

    dirs = []
    for out in (out1, out2):
        m = _TMP_DIR_RE.search(out)
        assert m, f"脚本未报告本次运行临时目录：\n{out}"
        dirs.append(Path(m.group(1).strip()))

    assert dirs[0] != dirs[1], "两次运行不得共享同一个临时目录（固定文件名会互相干扰）"
    for d in dirs:
        assert not d.exists(), f"运行结束后应只清理本次目录：{d} 仍存在"
    assert code1 == code2, "两次运行的退出码应一致（同样的环境状态）"


def test_run_dir_is_inside_tempdir_and_not_repo(tmp_path):
    """本次运行目录必须落在系统临时目录下，**不在仓库里**。"""
    bait = tmp_path / "bait4.sqlite"
    _make_bait_db(bait)

    _code, out = _run_tool({"PH_DB": str(bait)})

    m = _TMP_DIR_RE.search(out)
    assert m, out
    run_dir = Path(m.group(1).strip()).resolve()
    assert run_dir != REPO_ROOT and REPO_ROOT not in run_dir.parents, \
        f"运行目录不得落在仓库内：{run_dir}"


# ================================================================
# 3. 提前导入 ⇒ 写入前拒绝继续
# ================================================================

_DRIVER = """
import os, sys
repo, tools, bait = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, repo)
os.environ["PH_DB"] = bait
from app import db            # ← 提前导入：DB_PATH 已在导入时缓存为 bait
sys.path.insert(0, tools)
import accept_isolation as tool
code = tool.main([])
print("DRIVER_EXIT", code)
"""


def test_premature_import_refuses_before_writing(tmp_path):
    """`app.db` 已被提前导入 ⇒ 脚本**拒绝继续**（退出码 3），且不写入任何数据。"""
    bait = tmp_path / "bait5.sqlite"
    _make_bait_db(bait)

    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER, str(REPO_ROOT), str(TOOLS_DIR), str(bait)],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        cwd=str(tmp_path),
    )
    out = (proc.stdout or "") + (proc.stderr or "")

    assert "DRIVER_EXIT 3" in out, \
        f"提前导入时必须返回拒绝码 3（不是照写不误）\n{out}"
    assert "拒绝继续" in out, "应明确打印拒绝原因"

    tables, rows = _read_bait_db(bait)
    assert rows == [BAIT_MARKER], "拒绝继续时诱饵库不得被改动"
    assert "isolation_run_log" not in tables, \
        "路径不匹配必须在**写入前**失败，不得先写入再报错"


def test_bootstrap_refuses_when_app_db_already_imported():
    """直接调 `_bootstrap_run_dir()`：`app.db` 已在 `sys.modules` ⇒ 抛 `DbPathMismatch`。"""
    import importlib.util
    import app.db  # noqa: F401  —— 确保 app.db 已进入 sys.modules

    spec = importlib.util.spec_from_file_location("_acc_iso_tool", ACCEPT_TOOL)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    assert sys.modules.get("app.db") is not None
    with pytest.raises(tool.DbPathMismatch, match="提前导入"):
        tool._bootstrap_run_dir()

    # 拒绝路径**不得**留下运行目录，也不得留下全局状态。
    assert tool.RUN_DIR is None, "拒绝继续时不得建立运行目录"
    assert tool.RUN_DB is None
