"""
One-off: gives Ultrafine and ROBO their own designation lists (2026-10-07,
stakeholder's designations.xlsx).

Designations used to be one shared (RDC-shaped) list. They are now
company-specific (`designations.company`), so this script:
  1. adds the `company` column if the app hasn't done it yet (the same
     ALTER _auto_migrate() would run — existing rows default to 'RDC', i.e.
     RDC's list is untouched), then
  2. inserts each company's designations from the sheet.

Notice periods come from the stakeholder's 'Notice period ROBO Ultrafine.xlsx'
(which supersedes the first designations.xlsx guess). The sheet has no Truein
app-attendance column, so that is copied from the RDC designation with the same
normalized name (case/hyphen/space-insensitive), else off. Editable afterwards in
Admin -> Designations.

Idempotent — an existing (company, normalized name) row is left alone, except its
notice period is corrected to the sheet's value if it differs.
Uses a raw engine, not create_app(), so no background refresh thread starts.

Run once, ideally with the server stopped (the OLD code has no company filter
and would show these rows to RDC initiators until it is restarted):
    venv\\Scripts\\python add_robo_ultrafine_designations.py
"""
import os
import re
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

ULTRAFINE = [
    ("Assistant", 15),
    ("Mill Operator", 15),
    ("Mechanic", 15),
    ("Fitter", 15),
    ("Welder", 15),
    ("Electrician", 15),
    ("Tipper / Bulker Driver", 15),
    ("Executive - Logistics", 30),
    ("Senior Executive - Logistics", 30),
    ("Executive - Operations", 30),
    ("Senior Executive - Operations", 30),
    ("Chemist", 15),
    ("Senior Chemist", 15),
    ("Lab Technician", 30),
    ("Shift Incharge", 30),
    ("Senior Mechanic", 30),
    ("Technician Apprentice", 15),
    ("Trainee Engineer", 30),
    ("VRM Operator", 30),
    ("Executive - Accounts", 30),
    ("Senior Executive - Accounts", 30),
    ("Executive - MIS", 30),
    ("Fitter / Welder cum Operator", 15),
    ("Officer - Sales", 30),
    ("Senior Officer - Sales", 30),
    ("Trainee - Sales", 30),
]

ROBO = [
    ("Assistant", 15),
    ("Technician", 15),
    ("Crusher Operator", 15),
    ("Quarry Supervisor", 15),
    ("Fitter", 15),
    ("Welder", 15),
    ("Electrician", 15),
    ("Fitter / Welder cum Operator", 15),
    ("Shift Incharge", 30),
    ("Dispatcher", 30),
    ("Officer - Sales", 30),
    ("Senior Officer - Sales", 30),
    ("Senior Mechanic", 30),
    ("Technician Apprentice", 15),
    ("Trainee Engineer", 30),
    ("Executive - Logistics", 30),
    ("Senior Executive - Logistics", 30),
    ("Trainee - Sales", 30),
    ("Executive - Collections", 30),
    ("Executive - Accounts", 30),
    ("Senior Executive - Accounts", 30),
    ("Executive - Mining", 30),
    ("Loader cum JCB Operator", 15),
]


def norm(name):
    return re.sub(r"[\s\-_./]+", " ", name or "").strip().lower()


def main():
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.begin() as conn:
        has_col = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() "
            "AND table_name = 'designations' AND column_name = 'company'")).scalar()
        if not has_col:
            conn.execute(text(
                "ALTER TABLE designations ADD COLUMN company VARCHAR(20) NOT NULL DEFAULT 'RDC'"))
            print("added designations.company")

        rdc_att = {}
        for r in conn.execute(text(
                "SELECT name, truein_app_attendance FROM designations "
                "WHERE company = 'RDC' AND is_deleted = 0 ORDER BY id")):
            rdc_att.setdefault(norm(r[0]), r[1])

        next_order = conn.execute(text("SELECT COALESCE(MAX(sort_order), 0) FROM designations")).scalar()
        for company, rows in (("Ultrafine", ULTRAFINE), ("ROBO", ROBO)):
            existing = {norm(r[1]): (r[0], r[2]) for r in conn.execute(text(
                "SELECT id, name, notice_period_days FROM designations WHERE company = :c AND is_deleted = 0"),
                {"c": company})}
            added = fixed = 0
            for name, notice in rows:
                key = norm(name)
                if key in existing:
                    did, cur = existing[key]
                    if cur != notice:
                        conn.execute(text("UPDATE designations SET notice_period_days = :d WHERE id = :i"),
                                     {"d": notice, "i": did})
                        fixed += 1
                    continue
                att = rdc_att.get(key, 0)
                next_order += 1
                conn.execute(text(
                    "INSERT INTO designations (name, company, notice_period_days, truein_app_attendance, "
                    "norm_category_id, is_active, is_deleted, sort_order, created_at) "
                    "VALUES (:n, :c, :d, :a, NULL, 1, 0, :o, UTC_TIMESTAMP())"),
                    {"n": name, "c": company, "d": notice, "a": att, "o": next_order})
                existing[key] = (None, notice)
                added += 1
                print(f"  {company}: {name}  ({notice}d, app_att={att})")
            print(f"{company}: added {added}, notice period corrected on {fixed}, of {len(rows)}")


if __name__ == "__main__":
    sys.exit(main())
