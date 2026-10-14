"""
One-off (2026-10-14): the database used to store every timestamp in UTC; from now on it stores Indian
time (IST, UTC+5:30). This script moves every EXISTING timestamp forward by 5 h 30 min so old and new
rows agree.

  - It finds every DATETIME/TIMESTAMP column in the database itself (information_schema), so no table is
    missed, and adds 330 minutes to each non-empty value - all in ONE transaction (all or nothing).
  - It writes a marker (system_config key "time_storage" = "IST") so it can never be applied twice, and
    refuses to run if the marker is already there.
  - Unless --no-backup is given it first writes a full SQL backup to backups/ (pre-ist-migration-*.sql).
  - Uses a plain database connection (never create_app(), see CLAUDE.md gotcha #2).

STOP THE APP BEFORE RUNNING IT and start the new code only afterwards (rows written by the old code in
between would be UTC again):

    venv\\Scripts\\python migrate_to_ist.py --dry-run      # shows what it would shift
    venv\\Scripts\\python migrate_to_ist.py                # backup, then shift

Restoring an older backup (.sql taken before this date) brings UTC times back: run this script again
after restoring it (the marker is part of the restored data, so it will be absent and the script will run).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from sqlalchemy import create_engine, text   # noqa: E402

SHIFT_MINUTES = 330
MARKER_KEY = "time_storage"


def datetime_columns(conn):
    rows = conn.execute(text(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND data_type IN ('datetime', 'timestamp') "
        "ORDER BY table_name, column_name"))
    return [(r[0], r[1]) for r in rows]


def main():
    dry = "--dry-run" in sys.argv
    engine = create_engine(os.environ["DATABASE_URL"])
    if engine.dialect.name != "mysql":
        sys.exit("This script is for the MySQL database only.")

    with engine.connect() as conn:
        done = conn.execute(text("SELECT value FROM system_config WHERE `key` = :k"), {"k": MARKER_KEY}).scalar()
        cols = datetime_columns(conn)
    if done == "IST":
        sys.exit("Already migrated (system_config.time_storage = IST). Nothing to do.")

    print(f"{len(cols)} timestamp columns found:")
    with engine.connect() as conn:
        for t, c in cols:
            n = conn.execute(text(f"SELECT COUNT(`{c}`) FROM `{t}`")).scalar()
            print(f"  {t}.{c}: {n} values")
    if dry:
        print("dry run - nothing changed")
        return

    if "--no-backup" not in sys.argv:
        from app.services.db_backup import write_dump
        from app.timeutil import now_ist
        folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "pre-ist-migration-" + now_ist().strftime("%Y%m%d-%H%M%S") + ".sql")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            info = write_dump(engine, fh)
        print(f"backup written: {path} ({info['tables']} tables, {info['rows']} rows)")

    with engine.begin() as conn:
        for t, c in cols:
            res = conn.execute(text(
                f"UPDATE `{t}` SET `{c}` = `{c}` + INTERVAL {SHIFT_MINUTES} MINUTE WHERE `{c}` IS NOT NULL"))
            print(f"  shifted {t}.{c}: {res.rowcount} rows")
        conn.execute(text(
            "INSERT INTO system_config (`key`, `value`, updated_at) "
            "VALUES (:k, 'IST', UTC_TIMESTAMP() + INTERVAL 330 MINUTE)"), {"k": MARKER_KEY})
    print("done - every stored time is now Indian time. Start the app on the new code.")


if __name__ == "__main__":
    main()
