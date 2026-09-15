"""
Follow-up to apply_notice_period_update.py — resolves the 7 designations
that had no notice period in NP.xlsx. Per the user (2026-08-27):

1. 'Trainee - Batching Plant Operator': 15 days (user-supplied)
2. 'Welder cum Operator': 15 days (user-supplied)
3. The other 5 have no active hiring intake right now, so they're
   deactivated (is_active=False, NOT is_deleted — reversible if intake
   resumes) and drop out of the initiator's onboarding-form Designation
   dropdown (new_request() filters is_active=True, is_deleted=False):
     Executive HR, Mess Caretaker, Plant Incharge, Trainee - Logistics,
     Trainee Project

   Note: 'Plant Incharge' carries norm_category_id -> 'Plant Manager'
   (RDC staffing norms). Deactivating it only removes it from the
   selectable dropdown — the NormRoleCategory/NormRequirement config it
   points to is untouched, and any already-submitted historical requests
   with this designation are unaffected (is_active only gates NEW
   selection, not existing data).
4. The file's 'Sales Exectuive' entry (from apply_notice_period_update.py's
   held-back 9th new designation) was confirmed a typo for
   'Sales Executive' — created as a new 30-day designation, sort_order=59.
"""
from dotenv import load_dotenv
load_dotenv()

import sqlalchemy as sa
import os

eng = sa.create_engine(os.environ["DATABASE_URL"])

NOTICE_UPDATES = [
    ("Trainee - Batching Plant Operator", 15),
    ("Welder cum Operator", 15),
]

DEACTIVATE = [
    "Executive HR",
    "Mess Caretaker",
    "Plant Incharge",
    "Trainee - Logistics",
    "Trainee Project",
]

with eng.begin() as conn:
    print("=== Notice period updates ===")
    for name, days in NOTICE_UPDATES:
        row = conn.execute(
            sa.text("SELECT id, notice_period_days FROM designations WHERE name=:n AND is_deleted=0"),
            {"n": name},
        ).fetchone()
        if not row:
            print(f"  SKIP (not found): {name!r}")
            continue
        conn.execute(
            sa.text("UPDATE designations SET notice_period_days=:days WHERE id=:id"),
            {"days": days, "id": row.id},
        )
        print(f"  {name!r}: notice_period_days {row.notice_period_days} -> {days}")

    print()
    print("=== Deactivated (no active intake — hidden from initiator dropdown) ===")
    for name in DEACTIVATE:
        row = conn.execute(
            sa.text("SELECT id, is_active FROM designations WHERE name=:n AND is_deleted=0"),
            {"n": name},
        ).fetchone()
        if not row:
            print(f"  SKIP (not found): {name!r}")
            continue
        conn.execute(
            sa.text("UPDATE designations SET is_active=0 WHERE id=:id"),
            {"id": row.id},
        )
        print(f"  {name!r}: is_active {bool(row.is_active)} -> False")

    print()
    print("=== 'Sales Exectuive' typo resolution ===")
    existing = conn.execute(
        sa.text("SELECT id FROM designations WHERE name='Sales Executive' AND is_deleted=0")
    ).fetchone()
    if existing:
        print("  SKIP (already exists): 'Sales Executive'")
    else:
        next_sort = conn.execute(sa.text("SELECT COALESCE(MAX(sort_order), 0) FROM designations")).scalar() + 1
        conn.execute(
            sa.text(
                "INSERT INTO designations (name, notice_period_days, truein_app_attendance, "
                "is_active, is_deleted, sort_order, created_at) "
                "VALUES ('Sales Executive', 30, 0, 1, 0, :sort_order, NOW())"
            ),
            {"sort_order": next_sort},
        )
        print(f"  created 'Sales Executive' (30 days, sort_order={next_sort})")

print()
print("Done.")
