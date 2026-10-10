"""
Automatic full-database backup (2026-10-10).

Writes a complete, restorable SQL dump of the whole database - every table's CREATE statement, all its rows,
and the CREATE DATABASE / USE header - in the same layout `mysqldump` produces (same version-conditional
`/*!40101 ... */` comments, `DROP TABLE IF EXISTS` + `CREATE TABLE` + `LOCK TABLES` + extended `INSERT`s), so a
backup restores with the plain MySQL client:   mysql -u <user> -p < <file>.sql

It is generated in pure Python (no `mysqldump.exe` to find or install - the path problem that breaks that approach
on Windows and inside containers). All tables are read inside ONE consistent-snapshot transaction, so the file is a
single point in time even while people keep using the app.

Everything is controlled from Admin -> Database Backup (BackupSettings): on/off, time of day (IST), how many
days of backups to keep, the folder, and an optional e-mail copy. The scheduler thread re-reads those settings every
tick, so a change applies immediately - no restart.
"""
import os
import re
import sys
import json
import shutil
import threading
import time
import smtplib
import datetime as _dt
from decimal import Decimal
from datetime import datetime, timedelta

_IST_OFFSET = timedelta(hours=5, minutes=30)
_POLL_S = 30
_ROWS_PER_PAGE = 2000            # rows fetched per query (keeps memory flat on million-row tables)
_INSERT_LINE_BYTES = 1_000_000   # target size of one multi-row INSERT statement
_MIN_FREE_BYTES = 200 * 1024 * 1024
EMAIL_MAX_BYTES = 20 * 1024 * 1024   # bigger files are saved but not e-mailed (mail servers refuse ~25 MB)
_MAX_FAILED_PER_DAY = 3
_RETRY_AFTER = timedelta(minutes=30)
_KEEP_AT_LEAST = 3               # retention never removes the newest N good backups
_LOCK_NAME = "rdc_db_backup"
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.sql$")

_started = False
_lock = threading.Lock()
_run_lock = threading.Lock()     # in-process guard (the MySQL advisory lock covers other processes)


# ── Settings helpers ───────────────────────────────────────────────────────────

def project_root():
    from flask import current_app
    return os.path.abspath(os.path.join(current_app.root_path, os.pardir))


def default_backup_dir():
    return os.path.join(project_root(), "backups")


def get_settings():
    """The single BackupSettings row, created with the defaults (on, 04:00 IST, keep 14 days) the first time."""
    from ..extensions import db
    from ..models import BackupSettings
    row = BackupSettings.query.order_by(BackupSettings.id).first()
    if row is None:
        row = BackupSettings(enabled=True, hour=4, minute=0, retention_days=14, email_enabled=False)
        db.session.add(row)
        db.session.commit()
    return row


def backup_dir(settings=None):
    s = settings or get_settings()
    return os.path.abspath((s.backup_dir or "").strip() or default_backup_dir())


def validate_backup_dir(path):
    """Return (clean_path, error). Must be an absolute folder we can create/write, and never inside the
    web-served static folder (a backup there would be downloadable by anyone who guesses the file name)."""
    from flask import current_app
    path = (path or "").strip()
    if not path:
        return "", None
    if not os.path.isabs(path):
        return None, "Enter a full folder path, e.g. D:\\RDC-Backups (or leave it blank for the default)."
    path = os.path.abspath(path)
    static_root = os.path.abspath(os.path.join(current_app.root_path, "static"))
    try:
        inside_static = os.path.commonpath([path, static_root]) == static_root
    except ValueError:      # different drive letters - cannot be inside it
        inside_static = False
    if inside_static:
        return None, "The backup folder cannot be inside the app's static folder (those files are public)."
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".write-test-{os.getpid()}")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as exc:
        return None, f"That folder can't be used: {exc.strerror or exc}"
    return path, None


def parse_recipients(raw):
    """Split a comma/semicolon/space separated list; return (valid_list, invalid_list)."""
    parts = [p.strip() for p in re.split(r"[;,\s]+", raw or "") if p.strip()]
    ok, bad, seen = [], [], set()
    for p in parts:
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", p):
            if p.lower() not in seen:
                seen.add(p.lower())
                ok.append(p)
        else:
            bad.append(p)
    return ok, bad


def next_run_ist(settings, now_utc=None):
    """The next scheduled run as an IST datetime, or None when automatic backups are off."""
    if not settings.enabled:
        return None
    now_ist = (now_utc or datetime.utcnow()) + _IST_OFFSET
    slot = now_ist.replace(hour=settings.hour, minute=settings.minute, second=0, microsecond=0)
    return slot if slot > now_ist else slot + timedelta(days=1)


# ── SQL dump writer ────────────────────────────────────────────────────────────

def _q(name):
    return "`" + str(name).replace("`", "``") + "`"


_ESC = {"\\": "\\\\", "'": "\\'", "\n": "\\n", "\r": "\\r", "\0": "\\0", "\x1a": "\\Z"}


def _str_lit(s):
    return "'" + "".join(_ESC.get(ch, ch) for ch in s) + "'"


def sql_literal(v):
    """One value as a MySQL literal."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        return "0x" + b.hex() if b else "''"
    if isinstance(v, datetime):
        return "'" + v.strftime("%Y-%m-%d %H:%M:%S" + (".%f" if v.microsecond else "")) + "'"
    if isinstance(v, _dt.date):
        return "'" + v.isoformat() + "'"
    if isinstance(v, _dt.time):
        return "'" + v.strftime("%H:%M:%S" + (".%f" if v.microsecond else "")) + "'"
    if isinstance(v, timedelta):
        total = int(v.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        return f"'{sign}{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}'"
    if isinstance(v, (dict, list)):
        return _str_lit(json.dumps(v, ensure_ascii=False, default=str))
    return _str_lit(str(v))


_PRELUDE = """/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;
"""

_POSTLUDE = """/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;
"""

COMPLETE_MARKER = "-- Dump completed on "


def _table_names(conn, is_mysql):
    import sqlalchemy as sa
    if is_mysql:
        rows = conn.exec_driver_sql("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'").fetchall()
        return sorted(r[0] for r in rows)
    return sorted(sa.inspect(conn).get_table_names())


def _create_table_sql(conn, name, is_mysql):
    if is_mysql:
        return conn.exec_driver_sql(f"SHOW CREATE TABLE {_q(name)}").fetchone()[1]
    import sqlalchemy as sa
    from sqlalchemy.schema import CreateTable
    tbl = sa.Table(name, sa.MetaData(), autoload_with=conn)
    return str(CreateTable(tbl).compile(dialect=conn.dialect)).strip()


def _iter_rows(conn, name):
    """Yield every row of a table in bounded-size pages (keyset pagination on a single integer primary key,
    else one streamed read - only small lookup tables lack such a key)."""
    import sqlalchemy as sa
    pk = sa.inspect(conn).get_pk_constraint(name).get("constrained_columns") or []
    if len(pk) == 1:
        col = pk[0]
        last = None
        while True:
            if last is None:
                res = conn.exec_driver_sql(f"SELECT * FROM {_q(name)} ORDER BY {_q(col)} LIMIT {_ROWS_PER_PAGE}")
            else:
                res = conn.exec_driver_sql(
                    f"SELECT * FROM {_q(name)} WHERE {_q(col)} > %s ORDER BY {_q(col)} LIMIT {_ROWS_PER_PAGE}"
                    if conn.dialect.name == "mysql" else
                    f"SELECT * FROM {_q(name)} WHERE {_q(col)} > ? ORDER BY {_q(col)} LIMIT {_ROWS_PER_PAGE}",
                    (last,))
            keys = list(res.keys())
            rows = res.fetchall()
            if not rows:
                return
            idx = keys.index(col)
            for r in rows:
                yield tuple(r)
            last = rows[-1][idx]
            if len(rows) < _ROWS_PER_PAGE:
                return
    else:
        res = conn.exec_driver_sql(f"SELECT * FROM {_q(name)}")
        while True:
            rows = res.fetchmany(_ROWS_PER_PAGE)
            if not rows:
                return
            for r in rows:
                yield tuple(r)


def write_dump(engine, out, now_utc=None):
    """Write a complete dump of `engine`'s database to the text file object `out`.
    Returns {"database", "tables", "rows"}. Raises on any error (the caller discards the partial file)."""
    now_utc = now_utc or datetime.utcnow()
    is_mysql = engine.dialect.name == "mysql"
    # MySQL: a private connection (own snapshot transaction). Other databases only occur in tests, where the
    # session's single shared connection must be reused - closing a second handle on it would roll the test back.
    from ..extensions import db as _db
    conn = engine.connect() if is_mysql else _db.session.connection()
    try:
        if is_mysql:
            # One point-in-time view of every table, even while the app keeps writing.
            conn.exec_driver_sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conn.exec_driver_sql("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            dbname = conn.exec_driver_sql("SELECT DATABASE()").scalar()
            version = conn.exec_driver_sql("SELECT VERSION()").scalar()
        else:
            dbname, version = "main", conn.dialect.name

        out.write(f"-- RDC Associates Hiring - full database backup\n"
                  f"-- (mysqldump-compatible layout: restore with  mysql -u <user> -p < this-file.sql)\n--\n"
                  f"-- Database: {dbname}\n-- Server version\t{version}\n"
                  f"-- Created (UTC): {now_utc.strftime('%Y-%m-%d %H:%M:%S')}\n"
                  f"-- ------------------------------------------------------\n\n")
        out.write(_PRELUDE + "\n")
        if is_mysql:
            create_db = conn.exec_driver_sql(f"SHOW CREATE DATABASE {_q(dbname)}").fetchone()[1]
            create_db = create_db.replace("CREATE DATABASE ", "CREATE DATABASE /*!32312 IF NOT EXISTS*/ ", 1)
            out.write(f"--\n-- Current Database: {_q(dbname)}\n--\n\n{create_db};\n\nUSE {_q(dbname)};\n\n")

        tables = _table_names(conn, is_mysql)
        total_rows = 0
        for name in tables:
            out.write(f"--\n-- Table structure for table {_q(name)}\n--\n\n"
                      f"DROP TABLE IF EXISTS {_q(name)};\n"
                      "/*!40101 SET @saved_cs_client     = @@character_set_client */;\n"
                      "/*!50503 SET character_set_client = utf8mb4 */;\n"
                      f"{_create_table_sql(conn, name, is_mysql)};\n"
                      "/*!40101 SET character_set_client = @saved_cs_client */;\n\n")
            out.write(f"--\n-- Dumping data for table {_q(name)}\n--\n\n")
            head = f"INSERT INTO {_q(name)} VALUES "
            buf, size, wrote_any = [], 0, False
            if is_mysql:
                out.write(f"LOCK TABLES {_q(name)} WRITE;\n/*!40000 ALTER TABLE {_q(name)} DISABLE KEYS */;\n")
            for row in _iter_rows(conn, name):
                lit = "(" + ",".join(sql_literal(v) for v in row) + ")"
                buf.append(lit)
                size += len(lit)
                total_rows += 1
                wrote_any = True
                if size >= _INSERT_LINE_BYTES:
                    out.write(head + ",".join(buf) + ";\n")
                    buf, size = [], 0
            if buf:
                out.write(head + ",".join(buf) + ";\n")
            if is_mysql:
                out.write(f"/*!40000 ALTER TABLE {_q(name)} ENABLE KEYS */;\nUNLOCK TABLES;\n")
            elif not wrote_any:
                pass
            out.write("\n")

        out.write(f"--\n-- Dumping routines for database {_q(dbname)}\n--\n")
        out.write(_POSTLUDE + "\n")
        out.write(f"{COMPLETE_MARKER}{now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC\n")
        return {"database": dbname, "tables": len(tables), "rows": total_rows}
    finally:
        if is_mysql:
            try:
                conn.rollback()   # ends the snapshot transaction (read-only, nothing to undo)
            finally:
                conn.close()


# ── Running a backup ───────────────────────────────────────────────────────────

def _ist_stamp(now_utc):
    return (now_utc + _IST_OFFSET).strftime("%Y%m%d-%H%M%S")


def _db_name(engine):
    name = engine.url.database or "database"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(name)) or "database"


def _email_backup(app, run, path, recipients):
    from ..utils import get_db_mail_config
    from email.message import EmailMessage
    cfg = get_db_mail_config()
    if not cfg["username"]:
        return "skipped", "Email is not configured (Admin -> Email Settings)."
    if run.size_bytes and run.size_bytes > EMAIL_MAX_BYTES:
        return "skipped", f"File is {run.size_bytes / 1048576:.1f} MB - too large to e-mail (limit {EMAIL_MAX_BYTES // 1048576} MB)."
    msg = EmailMessage()
    msg["From"] = cfg["sender"] or cfg["username"]
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"Database backup - RDC Associates Hiring - {run.filename}"
    msg.set_content(
        f"Automatic database backup finished.\n\nFile: {run.filename}\nSize: {run.size_bytes / 1048576:.2f} MB\n"
        f"Tables: {run.tables_count}   Rows: {run.rows_count}\n\n"
        "The file holds every table, including personal data and password hashes - keep it private.\n"
        "Restore with:  mysql -u <user> -p < " + run.filename + "\n\n- RDC Associates Hiring")
    with open(path, "rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype="sql", filename=run.filename)
    with smtplib.SMTP(cfg["server"], cfg["port"], timeout=60) as s:
        s.ehlo()
        s.starttls()
        s.ehlo()
        s.login(cfg["username"], cfg["password"])
        s.send_message(msg)
    return "sent", "Sent to " + ", ".join(recipients)


def apply_retention(settings=None):
    """Delete backup files older than the configured number of days (0 = keep everything). Only files this feature
    created and recorded, and never the newest few good backups. Returns how many files were removed."""
    from ..extensions import db
    from ..models import BackupRun
    s = settings or get_settings()
    days = int(s.retention_days or 0)
    if days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    good = (BackupRun.query.filter_by(status="success", is_deleted=False)
            .order_by(BackupRun.started_at.desc()).all())
    removed = 0
    for run in good[_KEEP_AT_LEAST:]:
        if run.started_at >= cutoff:
            continue
        if _remove_file(run):
            removed += 1
    if removed:
        db.session.commit()
    return removed


def _remove_file(run):
    """Delete a run's file (if it is still where we put it) and mark the run as removed."""
    path = run.file_path or ""
    if path and _FILENAME_RE.match(os.path.basename(path)) and os.path.basename(path) == run.filename:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            return False
    run.is_deleted = True
    return True


def run_backup(app, triggered_by="schedule", user_id=None):
    """Take one backup now. Returns the BackupRun id, or None if another backup is already running.
    Never raises - problems are recorded on the run so they show up in the admin page."""
    from ..extensions import db
    from ..models import BackupRun
    if not _run_lock.acquire(blocking=False):
        return None
    lock_conn = None
    run_id = None
    try:
        with app.app_context():
            engine = db.engine
            if engine.dialect.name == "mysql":
                lock_conn = engine.connect()
                if not lock_conn.exec_driver_sql("SELECT GET_LOCK(%s, 0)", (_LOCK_NAME,)).scalar():
                    return None
            settings = get_settings()
            directory = backup_dir(settings)
            now = datetime.utcnow()
            filename = f"{_db_name(engine)}-{_ist_stamp(now)}.sql"
            run = BackupRun(filename=filename, file_path=os.path.join(directory, filename), status="running",
                            triggered_by=triggered_by, triggered_by_user=user_id, started_at=now)
            db.session.add(run)
            db.session.commit()
            run_id = run.id
            tmp = run.file_path + ".partial"
            try:
                os.makedirs(directory, exist_ok=True)
                if shutil.disk_usage(directory).free < _MIN_FREE_BYTES:
                    raise OSError(f"Less than {_MIN_FREE_BYTES // 1048576} MB free in {directory}")
                with open(tmp, "w", encoding="utf-8", newline="\n") as out:
                    stats = write_dump(engine, out, now)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp, run.file_path)
                run.size_bytes = os.path.getsize(run.file_path)
                run.tables_count, run.rows_count = stats["tables"], stats["rows"]
                run.status = "success"
                run.message = None
            except Exception as exc:  # recorded, not raised: a failed backup must be visible, never fatal
                db.session.rollback()
                run = db.session.get(BackupRun, run_id)
                run.status = "failed"
                run.message = (f"{type(exc).__name__}: {exc}")[:2000]
                for p in (tmp, run.file_path):
                    try:
                        if p and os.path.isfile(p):
                            os.remove(p)
                    except OSError:
                        pass
                app.logger.error(f"[Backup] failed: {exc}")
            run.finished_at = datetime.utcnow()
            db.session.commit()

            if run.status == "success":
                if settings.email_enabled:
                    recipients, _bad = parse_recipients(settings.email_recipients)
                    if recipients:
                        try:
                            run.email_status, run.email_message = _email_backup(app, run, run.file_path, recipients)
                        except Exception as exc:
                            run.email_status, run.email_message = "failed", str(exc)[:500]
                    else:
                        run.email_status, run.email_message = "skipped", "No recipients set."
                    db.session.commit()
                try:
                    apply_retention(settings)
                except Exception as exc:  # pragma: no cover - never fail a good backup over cleanup
                    app.logger.error(f"[Backup] retention cleanup failed: {exc}")
            return run_id
    finally:
        if lock_conn is not None:
            try:
                lock_conn.exec_driver_sql("SELECT RELEASE_LOCK(%s)", (_LOCK_NAME,))
            finally:
                lock_conn.close()
        _run_lock.release()


def trigger_manual_backup(app, user_id=None):
    """Start a backup in the background. Returns False if one is already running."""
    if is_backup_running():
        return False
    threading.Thread(target=run_backup, args=(app, "manual", user_id), daemon=True, name="db-backup-manual").start()
    return True


def is_backup_running():
    from ..models import BackupRun
    fresh = datetime.utcnow() - timedelta(hours=3)
    return BackupRun.query.filter(BackupRun.status == "running", BackupRun.started_at >= fresh).first() is not None


def _fail_interrupted(app):
    """Runs left 'running' by a restart/crash are marked failed (nothing is still working on them)."""
    from ..extensions import db
    from ..models import BackupRun
    with app.app_context():
        if db.engine.dialect.name == "mysql":
            with db.engine.connect() as c:
                if c.exec_driver_sql("SELECT IS_USED_LOCK(%s)", (_LOCK_NAME,)).scalar() is not None:
                    return
        n = 0
        for run in BackupRun.query.filter_by(status="running").all():
            run.status, run.message, run.finished_at = "failed", "Interrupted (the server stopped during the backup).", datetime.utcnow()
            n += 1
        if n:
            db.session.commit()


# ── Scheduler ──────────────────────────────────────────────────────────────────

def due_now(settings, now_utc=None):
    """Should a scheduled backup start now? True once today's slot (IST) has passed and no scheduled run has
    started since it - which also catches up after the server was off at the slot. A failed run is retried after
    30 minutes, at most 3 times a day."""
    from ..models import BackupRun
    if not settings.enabled:
        return False
    now_utc = now_utc or datetime.utcnow()
    now_ist = now_utc + _IST_OFFSET
    slot_ist = now_ist.replace(hour=settings.hour, minute=settings.minute, second=0, microsecond=0)
    if now_ist < slot_ist:
        return False
    slot_utc = slot_ist - _IST_OFFSET
    since = (BackupRun.query.filter(BackupRun.triggered_by == "schedule", BackupRun.started_at >= slot_utc)
             .order_by(BackupRun.started_at.desc()).all())
    if any(r.status in ("success", "running") for r in since):
        return False
    if since:   # only failures so far today
        if len(since) >= _MAX_FAILED_PER_DAY:
            return False
        if now_utc - since[0].started_at < _RETRY_AFTER:
            return False
    return True


def _scheduler_loop(app):
    from ..extensions import db
    time.sleep(20)   # let the app finish starting before the first check
    try:
        _fail_interrupted(app)
    except Exception as exc:
        app.logger.error(f"[Backup] startup cleanup failed: {exc}")
    while True:
        try:
            with app.app_context():
                settings = get_settings()
                if due_now(settings):
                    run_backup(app, "schedule")
        except Exception as exc:
            app.logger.error(f"[Backup] scheduler tick failed: {exc}")
        finally:
            try:
                db.session.remove()
            except Exception:
                pass
        time.sleep(_POLL_S)


def start_backup_scheduler(app):
    """Start the scheduler thread once per process (skipped in tests). Returns True if started."""
    global _started
    # The test suite sets TESTING only AFTER create_app() has run, so that flag is not a reliable guard here;
    # tests (and any non-MySQL setup) use an in-memory SQLite DB that a second thread would corrupt.
    from ..extensions import db
    if app.config.get("TESTING") or "pytest" in sys.modules:
        return False
    with app.app_context():
        if db.engine.dialect.name != "mysql":
            return False
    with _lock:
        if _started:
            return False
        _started = True
    threading.Thread(target=_scheduler_loop, args=(app,), daemon=True, name="db-backup-scheduler").start()
    return True
