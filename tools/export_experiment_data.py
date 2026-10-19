"""管理员离线导出与一致性备份（m02653「实验访问与数据交付」）。

**用途**：把主机上 SQLite 里的实验记录，转成**可核对、可脱敏**的 CSV，
并生成**一致性备份**，供设备所有者留存与统计。**不开发后台、不开放给学生**。

用法（必须显式给库路径，**不接受**默认值，避免误碰真实库）::

    python tools/export_experiment_data.py --db pedagogy.sqlite --out exports/2026-10-20 \\
        --backup exports/2026-10-20.sqlite
    python tools/export_experiment_data.py --db ... --out ... --exclude-participant DEMO

产出三份 CSV（`utf-8-sig`，Excel 直接打开不乱码）：

- ``participants_phases.csv`` —— **参与者×阶段汇总**；
- ``submissions.csv`` —— **提交明细**：``submission_log`` 每一列；
- ``hint_events.csv`` —— **提示事件明细**：``event_log`` 中 ``event_type='hint'`` 的行。

## 同一快照（本轮修正）

**全部 CSV、全部对账数字、以及 ``--backup`` 交付的备份，都取自同一份快照。**
做法：先用 SQLite **在线备份 API** 把源库备份到**本次运行独占的临时目录**里，
之后所有查询只读这份临时快照；``--backup`` 交付的就是**这一份快照文件**本身；
导出结束后临时快照即被清理。

为什么必须这样：先前版本是「先分别查询三份内容、再查行数、关连接、另开连接备份」——
期间若有学生继续提交，**几份结果可能对应不同时刻**，行数对账也就失去意义。
在线备份 API 只保证**备份本身**一致，**不会**自动让先前的查询与它一致；
**再对源库备份一次同样不行**——那是第二次读源库，又换了一个时刻。

## 路径保护（本轮修正）

**源库与任何输出目标不得重合，``--force`` 也不能绕过这条。**
校验的是**解析后的绝对路径**：``--backup`` 不得等于源库、CSV 目标不得等于源库、
``--backup`` 不得与任一 CSV 目标重合。仅「源连接只读」并不能阻止后续的文件写入
或另一个可写连接破坏同一个文件。``--force`` 只允许覆盖**旧导出物**。

## 文本单元格防公式（本轮修正）

参与者编号等字段是**自由文本**。以 ``=``、``+``、``-``、``@``、制表符、回车开头的文本，
在电子表格软件里可能被当成**公式**执行。CSV 的引号转义**解决不了**这个问题
（它只处理分隔符与引号），所以本脚本对**文本单元格**加前导单引号转义，
并在输出里打印转换规则。数值单元格不处理。

**注意**：导出**保留参与者编号与时间戳**，属**可关联的数据**，
**不是自动匿名化**。原始数据库备份包含的信息更多，**应限制访问、不得作为公开附件**。

## 必须如实知道的字段口径（不要按字段名想当然）

- ``elapsed_ms`` 是**判题执行耗时**，**不是**学生完成题目的用时（学生用时未记录）；
- ``hint_requested``／提示事件条数是**请求次数**，**不是**学生阅读次数；
- 汇总里的 ``passed`` 是**通过的提交次数**，**不等于通过题目数、更不等于测验得分**——
  同一道题重复提交通过会把它抬高；``first_submit_passed`` 是**符合首提交通过条件的题目计数**，
  **不是**「是否通过」的布尔值。要按题目去重评分须另立规则，**不得拿提交次数当成绩**；
- ``submission_log`` 只记**正式提交**；**全部判题次数要看 ``event_log`` 的 ``execution`` 事件**
  （可统计**执行尝试登记数／判题完成数／中止数／无终态数**）。历史未采集事件的记录**不能补推**；
  本脚本的三份 CSV **暂未包含执行事件明细**，但这**不等于**数据库里没有这些信息；
- **代码全文与提示正文都不落库** ⇒ 无法分析代码修改过程或提示内容质量；
- ``error_class``／``error_kp`` 由教学组件回填，**对照组恒为「未分类」**，
  不能当作「该被试没犯这类错」；
- 前后测阶段教学组件按契约记 ``phase_disabled``；**``phase_disabled`` 不等于这些内部记录不存在**。

## 安全边界（本脚本自己遵守）

- 源库只以 ``mode=ro`` 打开，**从不写源库**；导出前后源库表集合与行数不变；
- **只认 ``--db``**，不读 ``PH_DB``，也不 import ``app.db``（避免被 ``DB_PATH`` 默认值带到真实库）；
- 输出目标与源库**不得重合**（见上），已存在即拒绝覆盖，除非显式 ``--force``；
- ``--backup`` 交付的是**同一份快照文件**（不是再读一次源库、也不是运行中复制源库文件）
  —— 后者在并发写入时可能拿到不自洽的副本；
- **全部对账通过后才打印成功**；对账失败按错误退出，**不把不完整产物当成完整导出**；
- 只写受控字段，**不导出**密钥、``.env``、隔离运行记录里的自由文本。

**本脚本不启动学生进程、不联网、不安装任何东西**，只在系统临时目录里建一个本次运行独占的目录。
"""

from __future__ import annotations

import argparse
import csv
import gc
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2

SUMMARY_CSV = "participants_phases.csv"
SUBMISSIONS_CSV = "submissions.csv"
HINTS_CSV = "hint_events.csv"
OUTPUT_FILES = (SUMMARY_CSV, SUBMISSIONS_CSV, HINTS_CSV)

SUBMISSION_COLUMNS = (
    "id", "ts", "participant_id", "group_id", "question_id", "phase", "submit_seq",
    "status", "error_type", "error_class", "error_kp", "hint_level",
    "hint_unlocked_level", "hint_requested", "pass_status", "elapsed_ms",
)

# 提示事件明细刻意**不含** event_log.reason（自由描述）；只留受控码。
HINT_COLUMNS = (
    "event_id", "request_id", "submission_id", "participant_id", "question_id", "phase",
    "entry", "ts_start", "ts_end", "elapsed_ms", "status", "hint_level",
    "unlocked_level", "hint_source", "reason_code",
)

SUMMARY_COLUMNS = (
    "participant_id", "phase", "submissions", "passed", "first_submit_passed",
    "hint_requested_submissions", "avg_exec_ms", "min_exec_ms", "max_exec_ms",
    "total_exec_ms", "first_submit_ts", "last_submit_ts",
)

REQUIRED_TABLES = ("submission_log", "event_log")

# 电子表格软件会把这些前导字符当成公式开头；引号转义挡不住，必须改写单元格文本。
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
FORMULA_ESCAPE = "'"


class Refused(RuntimeError):
    """需要用户显式处理才能继续（路径重合、不覆盖、库不存在等）。"""


# ---------------------------------------------------------------- 库访问（只读）

def open_readonly(db_path: Path) -> sqlite3.Connection:
    """以**只读**方式打开库。

    用 URI 的 ``mode=ro``：即使脚本后续手滑写了 SQL，SQLite 也会直接拒绝，
    比"约定只读"更可靠。
    """
    check_source_db(db_path)
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def check_tables(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    missing = [t for t in REQUIRED_TABLES if t not in have]
    if missing:
        raise Refused(
            f"库中缺少所需表 {missing}；现有表：{sorted(have)}。"
            "请确认这是本项目的实验数据库，而不是别的库。")


def table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


# ---------------------------------------------------------------- 路径保护

def check_paths(db_path: Path, out_dir: Path, backup_path: Path | None) -> None:
    """源库与输出目标不得重合；**不受 ``--force`` 影响**（force 只覆盖旧导出物）。

    比对的是 **resolve() 之后的绝对路径**，因此 ``..``／符号链接／相对写法都算得进去。
    """
    src = db_path.resolve()
    csv_targets = {name: (out_dir / name).resolve() for name in OUTPUT_FILES}
    targets: list[tuple[str, Path]] = [(f"CSV {n}", p) for n, p in csv_targets.items()]
    if backup_path is not None:
        targets.append(("备份", backup_path.resolve()))

    for label, target in targets:
        if target == src:
            raise Refused(
                f"{label} 目标与源数据库是同一个文件（{src}）。"
                "导出不得写到源库上；--force 也不允许绕过这条保护。请改输出路径。")

    if backup_path is not None:
        b = backup_path.resolve()
        for name, target in csv_targets.items():
            if b == target:
                raise Refused(f"备份目标与 {name} 目标重合（{b}）。请改用不同的路径。")


def check_existing(out_dir: Path, backup_path: Path | None, force: bool) -> None:
    if force:
        return
    existing = [f for f in OUTPUT_FILES if (out_dir / f).exists()]
    if existing:
        raise Refused(
            f"输出文件已存在：{existing}。"
            "请换一个 --out 目录，或确认后用 --force 覆盖。")
    if backup_path is not None and backup_path.exists():
        raise Refused(f"备份文件已存在：{backup_path}。确认后用 --force 覆盖。")


# ---------------------------------------------------------------- 同一快照

def snapshot_to(src_db: Path, dest: Path) -> None:
    """用 SQLite **在线备份 API** 生成一致性快照。

    为什么不用 ``shutil.copy``：服务在写时复制文件，可能拿到
    「主库已更新、WAL 未合并」的副本 —— 行数对不上还看不出来。
    ``Connection.backup`` 由 SQLite 自己保证快照一致，且**不打断**写入方。
    """
    check_source_db(src_db)
    src = sqlite3.connect(f"file:{src_db.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()


def check_source_db(db_path: Path) -> None:
    """源库必须**已存在且是普通文件**。

    在**建快照之前**就查，否则 SQLite 会直接抛 ``OperationalError: unable to open
    database file``，用户看到的是底层报错而不是「路径写错了」。
    """
    if not db_path.exists():
        raise Refused(f"数据库不存在：{db_path}（本脚本不会创建库文件，请核对路径）")
    if not db_path.is_file():
        raise Refused(f"不是普通文件：{db_path}")


# ---------------------------------------------------------------- 三份 CSV

def _participants_filter(exclude: list[str]) -> tuple[str, list]:
    """被试过滤子句。`--exclude-participant` 用于**分离演示数据与实验数据**。"""
    if not exclude:
        return "", []
    placeholders = ",".join("?" * len(exclude))
    return f" AND participant_id NOT IN ({placeholders})", list(exclude)


def build_submissions(conn, exclude: list[str]) -> tuple[list[str], list[list]]:
    where, args = _participants_filter(exclude)
    rows = conn.execute(
        f"SELECT {', '.join(SUBMISSION_COLUMNS)} FROM submission_log"
        f" WHERE 1=1{where} ORDER BY ts, id", tuple(args)).fetchall()
    return list(SUBMISSION_COLUMNS), [[r[c] for c in SUBMISSION_COLUMNS] for r in rows]


def build_hints(conn, exclude: list[str]) -> tuple[list[str], list[list]]:
    where, args = _participants_filter(exclude)
    rows = conn.execute(
        f"SELECT {', '.join(HINT_COLUMNS)} FROM event_log"
        f" WHERE event_type='hint'{where} ORDER BY ts_start, event_id",
        tuple(args)).fetchall()
    return list(HINT_COLUMNS), [[r[c] for c in HINT_COLUMNS] for r in rows]


def build_summary(conn, exclude: list[str]) -> tuple[list[str], list[list]]:
    """参与者×阶段汇总。

    **口径**：``passed`` 是**通过的提交次数**（同题重复提交通过会抬高），
    ``first_submit_passed`` 是**符合首提交通过条件的题目计数**，**不是布尔值**。
    两者都**不得**直接当作前后测得分。
    """
    where, args = _participants_filter(exclude)
    rows = conn.execute(
        f"""
        SELECT
            participant_id,
            phase,
            COUNT(*)                                            AS submissions,
            SUM(CASE WHEN status='pass' THEN 1 ELSE 0 END)      AS passed,
            SUM(CASE WHEN submit_seq=1 AND pass_status=1 THEN 1 ELSE 0 END)
                                                                AS first_submit_passed,
            SUM(hint_requested)                                 AS hint_requested_submissions,
            AVG(elapsed_ms)                                     AS avg_exec_ms,
            MIN(elapsed_ms)                                     AS min_exec_ms,
            MAX(elapsed_ms)                                     AS max_exec_ms,
            SUM(elapsed_ms)                                     AS total_exec_ms,
            MIN(ts)                                             AS first_submit_ts,
            MAX(ts)                                             AS last_submit_ts
        FROM submission_log
        WHERE 1=1{where}
        GROUP BY participant_id, phase
        ORDER BY participant_id, phase
        """, tuple(args)).fetchall()

    out = []
    for r in rows:
        avg = r["avg_exec_ms"]
        out.append([
            r["participant_id"], r["phase"], r["submissions"], r["passed"],
            r["first_submit_passed"], r["hint_requested_submissions"],
            "" if avg is None else round(float(avg), 1),
            r["min_exec_ms"], r["max_exec_ms"], r["total_exec_ms"],
            r["first_submit_ts"], r["last_submit_ts"],
        ])
    return list(SUMMARY_COLUMNS), out


# ---------------------------------------------------------------- CSV 写出

def safe_cell(value):
    """文本单元格防公式：以 ``= + - @ \\t \\r`` 开头的文本加前导单引号。

    只处理 **str**（数值原样通过，否则负数会被误加前缀）。
    引号转义只处理分隔符与引号，**挡不住公式解释**，所以必须改写单元格文本本身。
    """
    if isinstance(value, str) and value[:1] in FORMULA_PREFIXES:
        return FORMULA_ESCAPE + value
    return value


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    # utf-8-sig：Excel 直接双击打开不乱码。
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([safe_cell(h) for h in header])
        writer.writerows([[safe_cell(c) for c in row] for row in rows])


def read_csv_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------- main

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="管理员离线导出实验数据（三份 CSV）与 SQLite 一致性备份。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db", required=True,
                   help="源数据库路径（必填；本脚本只读打开，绝不创建库文件）")
    p.add_argument("--out", required=True, help="输出目录（三份 CSV 写到这里）")
    p.add_argument("--backup", default=None, help="可选：一致性备份文件路径（.sqlite）")
    p.add_argument("--exclude-participant", action="append", default=[],
                   metavar="ID", help="排除某被试（可重复）；用于分离演示数据与实验数据")
    p.add_argument("--force", action="store_true",
                   help="允许覆盖已存在的输出文件（默认拒绝）；不能绕过源库重合保护")
    return p.parse_args(argv)


def _cleanup_run_dir(run_dir: Path) -> bool:
    """清理本次独占临时目录。

    产品侧多处 ``with connect() as conn:`` 只提交／回滚、**不关连接**，
    未回收的 ``sqlite3.Connection`` 会在 Windows 上占住文件句柄导致删除失败，
    所以先 ``gc.collect()`` 再删、失败则重试。**这是临时补救，不是连接生命周期的最终修复。**
    """
    for _ in range(3):
        gc.collect()
        try:
            shutil.rmtree(run_dir)
            return True
        except OSError:
            continue
    return False


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = Path(args.db).expanduser()
    out_dir = Path(args.out).expanduser()
    backup_path = Path(args.backup).expanduser() if args.backup else None
    excl = args.exclude_participant

    run_dir: Path | None = None
    try:
        # ① 先做「拒绝继续」的判断，再动任何文件 —— 写前拦截。
        check_paths(db_path, out_dir, backup_path)
        check_existing(out_dir, backup_path, args.force)

        # ② 生成本次独占的临时快照；此后**全部**读取只认这一份。
        run_dir = Path(tempfile.mkdtemp(prefix="pedagogy_export_"))
        snapshot = run_dir / "snapshot.sqlite"
        snapshot_to(db_path, snapshot)

        conn = open_readonly(snapshot)
        try:
            check_tables(conn)
            files = {
                SUMMARY_CSV: build_summary(conn, excl),
                SUBMISSIONS_CSV: build_submissions(conn, excl),
                HINTS_CSV: build_hints(conn, excl),
            }
            snap_counts = {
                "submissions": table_count(conn, "submission_log"),
                "hints": int(conn.execute(
                    "SELECT COUNT(*) AS n FROM event_log WHERE event_type='hint'"
                ).fetchone()["n"]),
                "events": table_count(conn, "event_log"),
            }
        finally:
            conn.close()

        # ③ 落盘（先写 CSV，再交付同一份快照的备份）
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, (header, rows) in files.items():
            write_csv(out_dir / name, header, rows)

        if backup_path is not None:
            # **必须交付上面那一份快照**，不能再对源库做一次备份 ——
            # 再读一次源库会拿到「另一个时刻」的数据，CSV 与备份就不再同源
            # （并发写入下两者的行数会不一致，本轮回归测试正是抓这个）。
            # 快照文件此刻已完全写完且只被只读打开过，复制它就是复制该快照。
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(snapshot, backup_path)

        # ④ 全部对账**先做完**，任何一项不过 ⇒ 按错误退出，不打印成功。
        written_sub = len(read_csv_rows(out_dir / SUBMISSIONS_CSV))
        written_hint = len(read_csv_rows(out_dir / HINTS_CSV))
        written_sum = len(read_csv_rows(out_dir / SUMMARY_CSV))
        problems = []
        if not excl:
            if written_sub != snap_counts["submissions"]:
                problems.append(
                    f"{SUBMISSIONS_CSV} {written_sub} 行 ≠ 库内 {snap_counts['submissions']} 行")
            if written_hint != snap_counts["hints"]:
                problems.append(
                    f"{HINTS_CSV} {written_hint} 行 ≠ 库内 {snap_counts['hints']} 行")
        if backup_path is not None:
            bconn = open_readonly(backup_path)
            try:
                b_sub = table_count(bconn, "submission_log")
                b_hint = int(bconn.execute(
                    "SELECT COUNT(*) AS n FROM event_log WHERE event_type='hint'"
                ).fetchone()["n"])
            finally:
                bconn.close()
            if b_sub != snap_counts["submissions"] or b_hint != snap_counts["hints"]:
                problems.append(f"备份与快照不一致：备份 {b_sub}/{b_hint} 行，"
                                f"快照 {snap_counts['submissions']}/{snap_counts['hints']} 行")
            if not excl and b_sub != written_sub:
                problems.append(f"备份提交行数 {b_sub} ≠ 导出 CSV {written_sub} 行"
                                "（同一快照必须一致）")
        if problems:
            raise Refused("对账未通过：" + "；".join(problems))

    except Refused as exc:
        print(f"⛔ 拒绝继续：{exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (sqlite3.Error, OSError) as exc:
        print(f"❌ 导出失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        # 快照文件只在本次运行内使用；**清理必须放在最外层**，
        # 否则会在交付备份之前就把快照删掉（本轮测试抓到的真实缺陷）。
        # 清理失败不改变本次结论，但**不静默**。
        if run_dir is not None and not _cleanup_run_dir(run_dir):
            print(f"⚠ 本次运行临时快照目录清理失败：{run_dir}"
                  "（不影响本次导出结论，可稍后手动删除）", file=sys.stderr)

    # ⑤ 对账表（已全部通过后才打印）与成功结论
    print("📋 对账（全部取自同一份快照）")
    print(f"   {SUMMARY_CSV:<24} {written_sum:>6} 行（参与者×阶段）")
    print(f"   {SUBMISSIONS_CSV:<24} {written_sub:>6} 行"
          f"  ← 快照 submission_log {snap_counts['submissions']} 行"
          f"{f'，排除 {excl} 后 {written_sub} 行' if excl else ''}")
    print(f"   {HINTS_CSV:<24} {written_hint:>6} 行"
          f"  ← 快照 event_type='hint' {snap_counts['hints']} 行"
          f"（快照 event_log 共 {snap_counts['events']} 行，含 execution）")
    if backup_path is not None:
        print(f"   备份：{backup_path}（与上述 CSV **同一份快照**，SQLite 在线备份）")
    print("✅ 导出完成（全部对账通过）：" + str(out_dir))

    print("\n⚠ 文本单元格防公式转换规则：以 = + - @ 制表符 回车 开头的文本，"
          "已加前导单引号（引号转义挡不住公式解释）。")
    print("⚠ 导出保留参与者编号与时间戳 ⇒ 属**可关联数据**，**不是自动匿名化**；"
          "原始备份信息更多，应限制访问、不得作为公开附件。")
    print("口径提醒：elapsed_ms 是**判题执行耗时**、不是学生答题用时；"
          "hint_requested 是**请求次数**、不是阅读次数；\n"
          "          汇总的 passed 是**通过提交次数**（同题重复通过会抬高），"
          "first_submit_passed 是**首提交通过的题目计数**、不是布尔值 —— "
          "两者都不得直接当成绩；\n"
          "          submission_log 只是正式提交；**全部判题次数要看 event_log 的 execution 事件**"
          "（登记／完成／中止／无终态可统计），本三份 CSV 暂未含执行事件明细；\n"
          "          代码全文与提示正文均未落库，无法做过程性分析。")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
