"""
One-off fix: 0 of 54 active designations had norm_category_id set (found
2026-08-27 while debugging why the RDC staffing-capacity popup never fired
for 'Assistant' at RAJ-Jaipur even though that plant/role was genuinely at
2/2 capacity). check_rdc_staffing_gate() early-returns allowed=True,
reason="not_covered" for any designation with no norm_category_id — so
with NONE mapped, the live gate was silently a no-op for every single new
hire, regardless of actual capacity.

This script links only the UNAMBIGUOUS exact/near-exact name matches
between an active designation and an active NormRoleCategory — the ones
that need no judgment call. The larger, genuinely ambiguous bucket (the
~20 field/plant driver-technician-operator roles that likely belong to
'Technical', plus various Sales/Logistics/Ops roles with no obvious
category) is intentionally left for the user to confirm before mapping.
"""
from dotenv import load_dotenv
load_dotenv()

import sqlalchemy as sa
import os

eng = sa.create_engine(os.environ["DATABASE_URL"])

MAPPINGS = [
    # (designation_name, norm_role_category_name)
    ("Assistant", "Assistant"),
    ("Batching Plant Operator", "Batchers/Production Officer"),
    ("Trainee - Batching Plant Operator", "Batchers/Production Officer"),
    ("Officer Production", "Batchers/Production Officer"),
    ("Executive Materials", "Materials"),
    ("Executive Accounts", "Accounts incl. Incharge"),
    ("Officer-Accounts", "Accounts incl. Incharge"),
    ("Trainee - Accounts", "Accounts incl. Incharge"),
    ("Executive - Credit Control", "Credit Control"),
    ("CDS", "CDS"),
]

with eng.begin() as conn:
    for desig_name, cat_name in MAPPINGS:
        desig = conn.execute(
            sa.text("SELECT id, norm_category_id FROM designations WHERE name=:n AND is_active=1 AND is_deleted=0"),
            {"n": desig_name},
        ).fetchone()
        cat = conn.execute(
            sa.text("SELECT id FROM norm_role_categories WHERE name=:n AND is_active=1 AND is_deleted=0"),
            {"n": cat_name},
        ).fetchone()
        if not desig:
            print(f"  SKIP (designation not found/active): {desig_name!r}")
            continue
        if not cat:
            print(f"  SKIP (category not found/active): {cat_name!r}")
            continue
        conn.execute(
            sa.text("UPDATE designations SET norm_category_id=:cid WHERE id=:did"),
            {"cid": cat.id, "did": desig.id},
        )
        print(f"  {desig_name!r} -> {cat_name!r}")

print()
print("Done. Remaining ~44 active designations are still unmapped — see chat for the proposed Technical-bucket batch awaiting confirmation.")
