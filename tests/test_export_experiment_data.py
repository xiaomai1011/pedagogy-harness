"""管理员离线导出脚本的合成数据验证（m02653「实验访问与数据交付」）。

**本文件不启动学生进程、不联网、不读密钥。** 所有断言都跑在**合成临时库**上；
需要验证「导出脚本不会顺手写到别的库」时，用**临时诱饵库**，
**绝不拿真实 `pedagogy.sqlite` 验证保护逻辑**（评阅 m02036 第 1 点）。

验证的事：

1. 三份 CSV 的行数与库内 `COUNT(*)` 一致 —— **导出数量必须能核对**；
2. 参与者×阶段汇总的计数与**手工期望**一致；
3. 导出**只读**：源库表集合与行数在导出前后不变，且不新建任何表；
4. 导出**不碰** `PH_DB` 指定的库（诱饵库核对）；目标文件已存在时**拒绝覆盖**；
5. **全部 CSV、对账数字与备份取自同一份快照**（评阅 m02810 第 1 点）；
6. **源库与输出目标不得重合，`--force` 也不能绕过**（评阅 m02810 第 2 点）；
7. **文本单元格防公式**，且**全部对账通过后才打印成功**（评阅 m02810 第 1／3 点）。
"""

import csv
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import app.db as db_mod  # noqa: F401  —— 只为复用建表 DDL，**不 connect**

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPORT_TOOL = REPO_ROOT / "tools" / "export_experiment_data.py"

BAIT_MARKER = "BAIT_UNTOUCHED_MARKER"

SUMMARY_CSV = "participants_phases.csv"
SUBMISSIONS_CSV = "submissions.csv"
HINTS_CSV = "hint_events.csv"


def _run_tool(*argv: str, env_extra: dict | None = None):
    """在**全新解释器**里跑导出脚本，返回 (退出码, stdout+stderr)。"""
    env = dict(__import__("os").environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, str(EXPORT_TOOL), *argv],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        cwd=str(REPO_ROOT), env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ================================================================ 合成数据

def _create_tables(conn: sqlite3.Connection) -> None:
    """按产品侧 DDL 建表（**复用常量**，保证 schema 与产品一致）。"""
    conn.execute(db_mod.LOG_SCHEMA)
    conn.executescript(db_mod.EVENT_SCHEMA)
    conn.executescript(db_mod.MEMORY_SCHEMA)


def _insert_submission(conn, *, ts, pid, phase, seq, status, pass_status,
                       hint_requested, elapsed_ms, gid="exp", qid="P001",
                       error_type="runtime", error_class="未分类", error_kp=""):
    conn.execute(
        """INSERT INTO submission_log
           (ts, participant_id, group_id, question_id, phase, submit_seq, status,
            error_type, error_class, error_kp, hint_level, hint_unlocked_level,
            hint_requested, pass_status, elapsed_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ts, pid, gid, qid, phase, seq, status, error_type, error_class, error_kp,
         0, 0, hint_requested, pass_status, elapsed_ms),
    )


def _insert_event(conn, *, etype, request_id, pid, phase, entry, ts_start, status,
                  submission_id=None, hint_level=None, unlocked_level=None,
                  hint_source=None, judge_status=None, reason_code="", reason=""):
    conn.execute(
        """INSERT INTO event_log
           (event_type, request_id, submission_id, participant_id, question_id, phase,
            entry, ts_start, ts_end, elapsed_ms, status, progress, hint_level,
            unlocked_level, hint_source, judge_status, reason_code, reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (etype, request_id, submission_id, pid, "P001", phase, entry, ts_start, ts_start,
         7, status, "", hint_level, unlocked_level, hint_source, judge_status,
         reason_code, reason),
    )


def _make_synth_db(path: Path) -> None:
    """合成库：2 个被试 × 2 个阶段，6 次提交、4 条事件（其中 3 条 hint）。"""
    conn = sqlite3.connect(path)
    try:
        _create_tables(conn)
        # ---- P_A / practice：seq1 失败、seq2 通过（请求过提示），exec_ms 10/20 ----
        _insert_submission(conn, ts="2026-10-18T10:00:00+08:00", pid="P_A", phase="practice",
                           seq=1, status="fail", pass_status=0, hint_requested=0, elapsed_ms=10)
        _insert_submission(conn, ts="2026-10-18T10:01:00+08:00", pid="P_A", phase="practice",
                           seq=2, status="pass", pass_status=1, hint_requested=1, elapsed_ms=20)
        # ---- P_A / pretest：seq1 通过，exec_ms 5 ----
        _insert_submission(conn, ts="2026-10-18T09:00:00+08:00", pid="P_A", phase="pretest",
                           seq=1, status="pass", pass_status=1, hint_requested=0, elapsed_ms=5)
        # ---- P_B / practice：seq1 失败（请求提示）、seq2 失败、seq3 通过，exec_ms 30/40/50 ----
        _insert_submission(conn, ts="2026-10-18T11:00:00+08:00", pid="P_B", phase="practice",
                           seq=1, status="fail", pass_status=0, hint_requested=1, elapsed_ms=30)
        _insert_submission(conn, ts="2026-10-18T11:02:00+08:00", pid="P_B", phase="practice",
                           seq=2, status="fail", pass_status=0, hint_requested=0, elapsed_ms=40)
        _insert_submission(conn, ts="2026-10-18T11:05:00+08:00", pid="P_B", phase="practice",
                           seq=3, status="pass", pass_status=1, hint_requested=0, elapsed_ms=50)
        # ---- 事件：3 条 hint ＋ 1 条 execution（后者**不得**出现在提示事件明细里）----
        _insert_event(conn, etype="hint", request_id="R1", pid="P_A", phase="practice",
                      entry="hint", ts_start="2026-10-18T10:00:30+08:00", status="returned_model",
                      submission_id=2, hint_level=1, unlocked_level=1, hint_source="llm")
        _insert_event(conn, etype="hint", request_id="R2", pid="P_A", phase="pretest",
                      entry="hint", ts_start="2026-10-18T09:00:30+08:00", status="phase_disabled",
                      hint_level=None, unlocked_level=None, hint_source=None,
                      reason_code="phase_disabled")
        _insert_event(conn, etype="hint", request_id="R3", pid="P_B", phase="practice",
                      entry="hint", ts_start="2026-10-18T11:00:30+08:00", status="returned_fallback",
                      submission_id=4, hint_level=2, unlocked_level=2, hint_source="fallback")
        _insert_event(conn, etype="execution", request_id="R1", pid="P_A", phase="practice",
                      entry="submit", ts_start="2026-10-18T10:00:00+08:00", status="finished",
                      judge_status="pass")
        conn.commit()
    finally:
        conn.close()


def _make_bait_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE bait_marker (k TEXT NOT NULL)")
        conn.execute("INSERT INTO bait_marker (k) VALUES (?)", (BAIT_MARKER,))
        conn.commit()
    finally:
        conn.close()


def _snapshot(path: Path):
    """(表名集合, 各表行数) —— 用于核对「导出前后源库一字未改」。"""
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall())
        counts = {}
        for t in tables:
            if t == "sqlite_sequence":
                continue
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return set(tables), counts
    finally:
        conn.close()


def _read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


# ================================================================ 1. 行数一致

def test_export_row_counts_match_database(tmp_path):
    """三份 CSV 的行数必须与库内 `COUNT(*)` 一致（导出数量可核对）。"""
    synth = tmp_path / "synth.sqlite"
    _make_synth_db(synth)
    out = tmp_path / "out"

    code, log = _run_tool("--db", str(synth), "--out", str(out))
    assert code == 0, f"导出应成功，实际退出码 {code}\n{log}"

    _, counts = _snapshot(synth)
    subs = _read_csv(out / SUBMISSIONS_CSV)
    hints = _read_csv(out / HINTS_CSV)

    assert len(subs) == counts["submission_log"] == 6, "提交明细行数必须等于库内提交行数"
    assert len(hints) == 3, "提示事件明细只含 event_type='hint' 的行"
    assert counts["event_log"] == 4, "库内事件共 4 条（3 hint ＋ 1 execution）"
    # execution 行**不得**混进提示事件明细
    assert all(r["entry"] == "hint" for r in hints)
    assert {r["request_id"] for r in hints} == {"R1", "R2", "R3"}


def test_submission_csv_carries_every_log_column(tmp_path):
    """提交明细要带齐 `submission_log` 的每一列，字段名与库内一致。"""
    synth = tmp_path / "synth.sqlite"
    _make_synth_db(synth)
    out = tmp_path / "out"
    assert _run_tool("--db", str(synth), "--out", str(out))[0] == 0

    with (out / SUBMISSIONS_CSV).open(encoding="utf-8-sig", newline="") as fh:
        cols = next(csv.reader(fh))
    assert cols == ["id", "ts", "participant_id", "group_id", "question_id", "phase",
                    "submit_seq", "status", "error_type", "error_class", "error_kp",
                    "hint_level", "hint_unlocked_level", "hint_requested", "pass_status",
                    "elapsed_ms"], f"提交明细列不符：{cols}"


# ================================================================ 2. 汇总计数

def test_summary_counts_match_hand_computed_expectation(tmp_path):
    """参与者×阶段汇总的计数与**手工期望**一致（不是照抄库内总数）。"""
    synth = tmp_path / "synth.sqlite"
    _make_synth_db(synth)
    out = tmp_path / "out"
    assert _run_tool("--db", str(synth), "--out", str(out))[0] == 0

    rows = {(r["participant_id"], r["phase"]): r for r in _read_csv(out / SUMMARY_CSV)}
    assert set(rows) == {("P_A", "practice"), ("P_A", "pretest"), ("P_B", "practice")}

    a_prac = rows[("P_A", "practice")]
    assert a_prac["submissions"] == "2"
    assert a_prac["passed"] == "1"
    assert a_prac["first_submit_passed"] == "0", "首提交未通过 ⇒ 0"
    assert a_prac["hint_requested_submissions"] == "1"
    assert float(a_prac["avg_exec_ms"]) == pytest.approx(15.0)

    a_pre = rows[("P_A", "pretest")]
    assert (a_pre["submissions"], a_pre["passed"], a_pre["first_submit_passed"]) == ("1", "1", "1")
    assert a_pre["hint_requested_submissions"] == "0"
    assert float(a_pre["avg_exec_ms"]) == pytest.approx(5.0)

    b_prac = rows[("P_B", "practice")]
    assert (b_prac["submissions"], b_prac["passed"], b_prac["first_submit_passed"]) == ("3", "1", "0")
    assert b_prac["hint_requested_submissions"] == "1"
    assert float(b_prac["avg_exec_ms"]) == pytest.approx(40.0)


# ================================================================ 3. 只读

def test_export_does_not_modify_source_db(tmp_path):
    """导出**只读**：源库表集合与行数在导出前后**一字未改**，且不新建表。"""
    synth = tmp_path / "synth.sqlite"
    _make_synth_db(synth)
    before = _snapshot(synth)
    out = tmp_path / "out"

    assert _run_tool("--db", str(synth), "--out", str(out))[0] == 0

    after = _snapshot(synth)
    assert after == before, f"导出不得改动源库：\n前 {before}\n后 {after}"
    assert "isolation_run_log" not in after[0], "导出脚本不得顺手建表"


def test_export_never_touches_ph_db_bait(tmp_path):
    """导出只认 `--db`；**不碰** `PH_DB` 指定的库（用诱饵库核对，不拿真实库）。"""
    bait = tmp_path / "bait.sqlite"
    _make_bait_db(bait)
    synth = tmp_path / "synth.sqlite"
    _make_synth_db(synth)
    out = tmp_path / "out"

    code, log = _run_tool("--db", str(synth), "--out", str(out),
                          env_extra={"PH_DB": str(bait)})
    assert code == 0, log

    tables, _counts = _snapshot(bait)
    assert tables == {"bait_marker"}, f"诱饵库不得被建表：{sorted(tables)}"
    conn = sqlite3.connect(f"file:{bait.as_posix()}?mode=ro", uri=True)
    try:
        assert [r[0] for r in conn.execute("SELECT k FROM bait_marker")] == [BAIT_MARKER]
    finally:
        conn.close()


# ================================================================ 4. 覆盖保护与筛选

def test_refuses_to_overwrite_existing_output(tmp_path):
    """目标文件已存在 ⇒ **拒绝覆盖**（退出码非 0），避免误盖上一次的实验数据。"""
    synth = tmp_path / "synth.sqlite"
    _make_synth_db(synth)
    out = tmp_path / "out"
    out.mkdir()
    (out / SUBMISSIONS_CSV).write_text("上次的数据，不许被覆盖\n", encoding="utf-8")

    code, log = _run_tool("--db", str(synth), "--out", str(out))
    assert code != 0, "已存在的输出文件必须拒绝覆盖"
    assert "已存在" in log, f"应明确说明拒绝原因\n{log}"
    assert (out / SUBMISSIONS_CSV).read_text(encoding="utf-8") == "上次的数据，不许被覆盖\n"

    # 显式 --force 才允许覆盖
    code2, log2 = _run_tool("--db", str(synth), "--out", str(out), "--force")
    assert code2 == 0, log2
    assert len(_read_csv(out / SUBMISSIONS_CSV)) == 6


def test_exclude_participant_filters_all_three_files(tmp_path):
    """`--exclude-participant` 用于**分离演示数据与实验数据**，三份文件同时生效。"""
    synth = tmp_path / "synth.sqlite"
    _make_synth_db(synth)
    out = tmp_path / "out"

    code, log = _run_tool("--db", str(synth), "--out", str(out),
                          "--exclude-participant", "P_B")
    assert code == 0, log

    subs = _read_csv(out / SUBMISSIONS_CSV)
    assert len(subs) == 3 and {r["participant_id"] for r in subs} == {"P_A"}
    assert len(_read_csv(out / HINTS_CSV)) == 2
    assert {r["participant_id"] for r in _read_csv(out / SUMMARY_CSV)} == {"P_A"}


# ================================================================ 5. 一致性备份

def test_backup_is_consistent_and_complete(tmp_path):
    """`--backup` 走 SQLite 一致性备份（不是复制文件），内容与源库一致。"""
    synth = tmp_path / "synth.sqlite"
    _make_synth_db(synth)
    out = tmp_path / "out"
    backup = tmp_path / "backup.sqlite"

    code, log = _run_tool("--db", str(synth), "--out", str(out), "--backup", str(backup))
    assert code == 0, log
    assert backup.exists(), "备份文件必须生成"

    _, src_counts = _snapshot(synth)
    _, dst_counts = _snapshot(backup)
    assert dst_counts == src_counts, f"备份与源库行数必须一致：\n源 {src_counts}\n备 {dst_counts}"


# ================================================================ 6. 错误路径

def test_missing_db_reports_error_without_creating_it(tmp_path):
    """`--db` 指向不存在的库 ⇒ 明确报错、非 0 退出，**且不创建**该文件。"""
    missing = tmp_path / "nope.sqlite"
    out = tmp_path / "out"

    code, log = _run_tool("--db", str(missing), "--out", str(out))
    assert code != 0
    assert "不存在" in log or "无法" in log, f"应给出可读的错误原因\n{log}"
    assert not missing.exists(), "不得因为导出而创建库文件"


# ================================================================ 7. 同一快照（m02810 第 1 点）

def test_backup_and_csv_are_from_the_same_snapshot(tmp_path):
    """备份与三份 CSV **取自同一份快照** ⇒ 行数必须两两一致（含 `--exclude` 场景）。"""
    synth = tmp_path / "synth.sqlite"
    _make_synth_db(synth)
    out = tmp_path / "out"
    backup = tmp_path / "backup.sqlite"

    code, log = _run_tool("--db", str(synth), "--out", str(out), "--backup", str(backup))
    assert code == 0, log

    _, snap_counts = _snapshot(synth)
    _, bak_counts = _snapshot(backup)
    csv_sub = len(_read_csv(out / SUBMISSIONS_CSV))
    csv_hint = len(_read_csv(out / HINTS_CSV))

    assert bak_counts == snap_counts, f"备份必须等于源库快照：\n源 {snap_counts}\n备 {bak_counts}"
    assert csv_sub == bak_counts["submission_log"], "CSV 提交行数必须等于同一快照"
    assert csv_hint == 3, "CSV 提示行数必须等于同一快照的 hint 事件数"


def test_concurrent_writes_do_not_break_snapshot_consistency(tmp_path):
    """**有写入方并发提交**时，CSV 与备份仍必须同源一致（旧实现会在此出现数量差）。

    这是回归护栏：两者都取自同一份快照，因此断言**恒成立**；
    旧实现「先查 CSV、后另开连接备份」在并发写入下会拿到不同时刻的数据。
    """
    synth = tmp_path / "synth.sqlite"
    _make_synth_db(synth)
    out = tmp_path / "out"
    backup = tmp_path / "backup.sqlite"

    writer = subprocess.Popen(
        [sys.executable, "-c", _WRITER_SRC, str(synth)],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        code, log = _run_tool("--db", str(synth), "--out", str(out), "--backup", str(backup))
    finally:
        writer.wait(timeout=60)

    assert code == 0, log
    csv_sub = len(_read_csv(out / SUBMISSIONS_CSV))
    _, bak_counts = _snapshot(backup)
    assert csv_sub == bak_counts["submission_log"], (
        "CSV 与备份必须来自同一份快照："
        f"CSV {csv_sub} 行，备份 {bak_counts['submission_log']} 行")


_WRITER_SRC = """
import sqlite3, sys, time
conn = sqlite3.connect(sys.argv[1])
for i in range(400):
    try:
        conn.execute(
            "INSERT INTO submission_log (ts, participant_id, group_id, question_id, phase,"
            " submit_seq, status, error_type, error_class, error_kp, hint_level,"
            " hint_unlocked_level, hint_requested, pass_status, elapsed_ms)"
            " VALUES ('2026-10-18T12:00:00+08:00','P_W','exp','P001','practice',1,"
            "'fail','runtime','未分类','',0,0,0,0,1)")
        conn.commit()
    except sqlite3.Error:
        pass
    time.sleep(0.001)
conn.close()
"""


def test_reconciliation_reported_before_success_line(tmp_path):
    """**先对账、后报成功**：对账行必须出现在「✅ 导出完成」之前（m02810 第 1 点）。"""
    synth = tmp_path / "synth.sqlite"
    _make_synth_db(synth)
    out = tmp_path / "out"

    code, log = _run_tool("--db", str(synth), "--out", str(out))
    assert code == 0, log

    idx_recon = log.find("对账")
    idx_ok = log.find("✅ 导出完成")
    assert idx_recon != -1, f"必须打印对账表\n{log}"
    assert idx_ok != -1, f"必须打印成功结论\n{log}"
    assert idx_recon < idx_ok, "对账必须**先**完成，成功结论在其后"


# ================================================================ 8. 路径保护（m02810 第 2 点）

def test_refuses_when_backup_target_is_the_source_db(tmp_path):
    """备份目标**就是源库** ⇒ 拒绝继续；`--force` **也不能绕过**。"""
    synth = tmp_path / "synth.sqlite"
    _make_synth_db(synth)
    before = _snapshot(synth)
    out = tmp_path / "out"

    for extra in ([], ["--force"]):
        code, log = _run_tool("--db", str(synth), "--out", str(out),
                              "--backup", str(synth), *extra)
        assert code == 2, f"必须按「拒绝继续」退出，实际 {code}\n{log}"
        assert "同一个文件" in log, log
    assert _snapshot(synth) == before, "拒绝时源库必须一字未改"


def test_refuses_when_csv_target_is_the_source_db(tmp_path):
    """某个 CSV 目标**就是源库** ⇒ 拒绝继续；`--force` 也不能绕过。"""
    # 把源库命名成 CSV 文件名，再让 --out 指向它所在目录。
    synth = tmp_path / SUBMISSIONS_CSV
    _make_synth_db(synth)
    before = _snapshot(synth)

    for extra in ([], ["--force"]):
        code, log = _run_tool("--db", str(synth), "--out", str(tmp_path), *extra)
        assert code == 2, f"必须拒绝，实际 {code}\n{log}"
        assert "同一个文件" in log, log
    assert _snapshot(synth) == before, "拒绝时源库必须一字未改"


def test_refuses_when_backup_target_collides_with_csv_target(tmp_path):
    """备份目标与某个 CSV 目标重合 ⇒ 拒绝继续。"""
    synth = tmp_path / "synth.sqlite"
    _make_synth_db(synth)
    out = tmp_path / "out"

    code, log = _run_tool("--db", str(synth), "--out", str(out),
                          "--backup", str(out / SUBMISSIONS_CSV))
    assert code == 2, f"必须拒绝，实际 {code}\n{log}"
    assert "重合" in log, log
    assert not (out / SUBMISSIONS_CSV).exists(), "拒绝时不得留下任何输出"


# ================================================================ 9. 防公式注入（m02810 第 3 点）

def test_formula_like_text_cells_are_escaped(tmp_path):
    """以 `= + - @` 开头的**文本**必须加前导单引号；数值不受影响。"""
    synth = tmp_path / "synth.sqlite"
    _make_synth_db(synth)
    out = tmp_path / "out"

    evil = ["=cmd|'/C calc'!A0", "+1+1", "-2+3", "@SUM(A1:A9)", "\tTAB_LEAD"]
    conn = sqlite3.connect(synth)
    try:
        for i, pid in enumerate(evil):
            _insert_submission(conn, ts=f"2026-10-18T13:0{i}:00+08:00", pid=pid,
                               phase="practice", seq=1, status="fail", pass_status=0,
                               hint_requested=0, elapsed_ms=7)
        conn.commit()
    finally:
        conn.close()

    code, log = _run_tool("--db", str(synth), "--out", str(out))
    assert code == 0, log

    rows = {r["participant_id"]: r for r in _read_csv(out / SUBMISSIONS_CSV)}
    for pid in evil:
        assert "'" + pid in rows, f"公式型文本必须加前导单引号，实际键：{sorted(rows)}"
        assert rows["'" + pid]["participant_id"][1:] == pid, "只加前缀，不得改写原值"
    assert rows["'=cmd|'/C calc'!A0"]["elapsed_ms"] == "7", "数值单元格不得被改写"
    assert "防公式" in log, "转换规则必须打印出来"


def test_plain_text_cells_are_left_alone(tmp_path):
    """普通编号**不得**被加前缀（避免把正常数据改脏）。"""
    synth = tmp_path / "synth.sqlite"
    _make_synth_db(synth)
    out = tmp_path / "out"
    assert _run_tool("--db", str(synth), "--out", str(out))[0] == 0

    ids = {r["participant_id"] for r in _read_csv(out / SUBMISSIONS_CSV)}
    assert ids == {"P_A", "P_B"}, f"普通编号必须原样保留：{ids}"

