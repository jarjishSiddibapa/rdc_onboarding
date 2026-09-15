"""
Database migration script — adds new columns to existing tables.
Run once: python migrate.py
Safe to re-run (uses IF NOT EXISTS / ignores duplicate-column errors).
"""
import os
import sys

# Add project root to path so we can import the app
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from app.extensions import db

app = create_app()

MIGRATIONS = [
    # Table, column name, column definition
    ("plant_locations",      "is_deleted",   "TINYINT(1) NOT NULL DEFAULT 0"),
    ("designations",         "is_deleted",   "TINYINT(1) NOT NULL DEFAULT 0"),
    ("form_fields",          "is_deleted",   "TINYINT(1) NOT NULL DEFAULT 0"),
    ("onboarding_requests",  "is_deleted",   "TINYINT(1) NOT NULL DEFAULT 0"),
    ("users",                "profile_pic",  "VARCHAR(500) NULL"),
]


def column_exists(conn, table, column):
    result = conn.execute(
        db.text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "AND table_name = :tbl AND column_name = :col"
        ),
        {"tbl": table, "col": column},
    )
    return result.scalar() > 0


def run():
    with app.app_context():
        conn = db.engine.connect()
        applied = []
        skipped = []

        for table, col, definition in MIGRATIONS:
            if column_exists(conn, table, col):
                skipped.append(f"  SKIP  {table}.{col} (already exists)")
            else:
                sql = f"ALTER TABLE `{table}` ADD COLUMN `{col}` {definition}"
                conn.execute(db.text(sql))
                conn.commit()
                applied.append(f"  OK    {table}.{col}")

        conn.close()

        print("\n=== Migration Results ===")
        for msg in applied:
            print(msg)
        for msg in skipped:
            print(msg)
        print(f"\nDone. {len(applied)} column(s) added, {len(skipped)} already existed.")


if __name__ == "__main__":
    run()
