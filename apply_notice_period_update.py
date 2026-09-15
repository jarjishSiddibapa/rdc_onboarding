"""
One-off migration applying the user-confirmed changes from NP.xlsx
(C:\\Users\\Jarjish Sidhabappa\\Downloads\\NP.xlsx) against the designations
table. Confirmed with the user via AskUserQuestion on 2026-08-27. Already
run against the live DB — safe to re-run (each block checks current state
before writing, so a second run is a no-op except any not-yet-applied
NEW_DESIGNATIONS entries).

1. Batching Plant Operator: 15 -> 30 days (clear, unambiguous file change)
2. Dispatcher: 15 -> 30 days (clear, unambiguous file change)
3. Cosmetic spelling/punctuation-only renames to match the file exactly —
   no notice-period change:
     'BPO Cum Mechanic'        -> 'BPO cum Mechanic'        (15 days)
     'Officer - Sales Executive' -> 'Officer - sales Executive' (30 days)
     'Senior Officer Sales'    -> 'Senior Officer - Sales'   (30 days)
     'Trainee Accounts'        -> 'Trainee - Accounts'       (30 days)
4. 'TM Supervisors' -> 'TM Supervisor' (singular), 30 -> 15 days — user
   confirmed this is the same role, renamed AND moved.
5. File's hyphenated near-duplicates ('Executive - Logistics',
   'Officer-Logistics') are the SAME role as the existing plain-spaced
   designations ('Executive Logistics', 'Officer Logistics') per the user —
   no action needed, existing rows are left as-is.
6. 8 new 30-day designations created (of the 9 flagged, 'Sales Exectuive'
   held back pending a spelling clarification with the user):
     Officer-Accounts, Officer - Technical, Executive - Technical,
     Executive Sales, Executive - Sales Coordination,
     Senior Engineer Projects, Senior Executive Operations,
     Trainee Engineer

Explicitly NOT touched (7 designations absent from the file entirely —
per the user, this needs their follow-up on missing notice periods before
any change): Executive HR, Mess Caretaker, Plant Incharge,
Trainee - Batching Plant Operator, Trainee - Logistics, Trainee Project,
Welder cum Operator.
"""
from dotenv import load_dotenv
load_dotenv()

import sqlalchemy as sa
import os

eng = sa.create_engine(os.environ["DATABASE_URL"])

RENAMES = [
    # (old_name, new_name, new_notice_period_days)
    ("BPO Cum Mechanic", "BPO cum Mechanic", 15),
    ("Officer - Sales Executive", "Officer - sales Executive", 30),
    ("Senior Officer Sales", "Senior Officer - Sales", 30),
    ("Trainee Accounts", "Trainee - Accounts", 30),
    ("TM Supervisors", "TM Supervisor", 15),
]

NOTICE_ONLY_CHANGES = [
    # (name, new_notice_period_days)
    ("Batching Plant Operator", 30),
    ("Dispatcher", 30),
]

NEW_DESIGNATIONS = [
    "Officer-Accounts",
    "Officer - Technical",
    "Executive - Technical",
    "Executive Sales",
    "Executive - Sales Coordination",
    "Senior Engineer Projects",
    "Senior Executive Operations",
    "Trainee Engineer",
]

with eng.begin() as conn:
    print("=== Renames (+ notice period) ===")
    for old, new, days in RENAMES:
        row = conn.execute(
            sa.text("SELECT id, name, notice_period_days FROM designations WHERE name=:n AND is_deleted=0"),
            {"n": old},
        ).fetchone()
        if not row:
            print(f"  SKIP (not found): {old!r}")
            continue
        conn.execute(
            sa.text("UPDATE designations SET name=:new, notice_period_days=:days WHERE id=:id"),
            {"new": new, "days": days, "id": row.id},
        )
        print(f"  {old!r} -> {new!r}, notice_period_days {row.notice_period_days} -> {days}")

    print()
    print("=== Notice-period-only changes ===")
    for name, days in NOTICE_ONLY_CHANGES:
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
    print("=== New designations (30 days) ===")
    next_sort = conn.execute(sa.text("SELECT COALESCE(MAX(sort_order), 0) FROM designations")).scalar()
    for name in NEW_DESIGNATIONS:
        existing = conn.execute(
            sa.text("SELECT id FROM designations WHERE name=:n AND is_deleted=0"), {"n": name}
        ).fetchone()
        if existing:
            print(f"  SKIP (already exists): {name!r}")
            continue
        next_sort += 1
        conn.execute(
            sa.text(
                "INSERT INTO designations (name, notice_period_days, truein_app_attendance, "
                "is_active, is_deleted, sort_order, created_at) "
                "VALUES (:name, 30, 0, 1, 0, :sort_order, NOW())"
            ),
            {"name": name, "sort_order": next_sort},
        )
        print(f"  created {name!r} (30 days, sort_order={next_sort})")

print()
print("Done.")
