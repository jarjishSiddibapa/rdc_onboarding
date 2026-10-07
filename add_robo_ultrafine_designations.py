"""
One-off: gives Ultrafine and ROBO their own designation lists (2026-10-07,
stakeholder's designations.xlsx).

Designations used to be one shared (RDC-shaped) list. They are now
company-specific (`designations.company`), so this script:
  1. adds the `company` column if the app hasn't done it yet (the same
     ALTER _auto_migrate() would run — existing rows default to 'RDC', i.e.
     RDC's list is untouched), then
  2. inserts each company's designations from the sheet.

Notice period / Truein app-attendance aren't in the sheet. Per the
stakeholder's choice, a new row copies both from the RDC designation with the
same name (matched ignoring case, hyphens and extra spaces, so "Officer -
Sales" inherits from "Officer Sales"); a name RDC doesn't have gets 30 days
and app attendance off. All editable afterwards in Admin -> Designations.

Idempotent — skips any (company, normalized name) that already exists.
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
    "Assistant", "Mill Operator", "Mechanic", "Fitter", "Welder", "Electrician",
    "Tipper / Bulker Driver", "Executive - Logistics", "Senior Executive - Logistics",
    "Executive - Operations", "Senior Executive - Operations", "Chemist",
    "Senior Chemist", "Lab Technician", "Shift Incharge", "Senior Mechanic",
    "Technician Apprentice", "Trainee Engineer", "VRM Operator",
    "Executive - Accounts", "Senior Executive - Accounts", "Executive - MIS",
    "Fitter / Welder cum Operator", "Officer - Sales", "Senior Officer - Sales",
    "Trainee - Sales",
]

ROBO = [
    "Assistant", "Technician", "Crusher Operator", "Quarry Supervisor", "Fitter",
    "Welder", "Electrician", "Fitter / Welder cum Operator", "Shift Incharge",
    "Dispatcher", "Officer - Sales", "Senior Officer - Sales", "Senior Mechanic",
    "Technician Apprentice", "Trainee Engineer", "Executive - Logistics",
    "Senior Executive - Logistics", "Trainee - Sales", "Executive - Collections",
    "Executive - Accounts", "Senior Executive - Accounts", "Executive - Mining",
    "Loader cum JCB Operator",
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

        rdc = {}
        for r in conn.execute(text(
                "SELECT name, notice_period_days, truein_app_attendance FROM designations "
                "WHERE company = 'RDC' AND is_deleted = 0 ORDER BY id")):
            rdc.setdefault(norm(r[0]), (r[1], r[2]))

        next_order = conn.execute(text("SELECT COALESCE(MAX(sort_order), 0) FROM designations")).scalar()
        for company, names in (("Ultrafine", ULTRAFINE), ("ROBO", ROBO)):
            existing = {norm(r[0]) for r in conn.execute(text(
                "SELECT name FROM designations WHERE company = :c AND is_deleted = 0"), {"c": company})}
            added = 0
            for name in names:
                if norm(name) in existing:
                    continue
                notice, att = rdc.get(norm(name), (30, 0))
                next_order += 1
                conn.execute(text(
                    "INSERT INTO designations (name, company, notice_period_days, truein_app_attendance, "
                    "norm_category_id, is_active, is_deleted, sort_order, created_at) "
                    "VALUES (:n, :c, :d, :a, NULL, 1, 0, :o, UTC_TIMESTAMP())"),
                    {"n": name, "c": company, "d": notice, "a": att, "o": next_order})
                existing.add(norm(name))
                added += 1
                print(f"  {company}: {name}  ({notice}d, app_att={att}, "
                      f"{'copied from RDC' if norm(name) in rdc else 'default'})")
            print(f"{company}: added {added} of {len(names)}")


if __name__ == "__main__":
    sys.exit(main())
