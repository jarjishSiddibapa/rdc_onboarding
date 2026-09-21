"""
One-off migration: adds a new "< 1500 m3" PLANT/SHEET1 tier to the RDC
staffing norms, per the stakeholder's updated "Staffing Norms for Plants"
spreadsheet (2026-09-21) — a 4th, lower volume bracket was inserted before
the existing "< 3000 m3" tier. Every value in the other 3 existing tiers
(< 3000 / 3000-5000 / > 5000) is confirmed UNCHANGED in the new sheet —
this migration only adds the new bracket, it does not touch any existing
NormRequirement value.

Two things this script does, both required together:
1. Insert the new NormTier ("LT_1500", min_value=None, max_value=1500) plus
   one NormRequirement row per PLANT/SHEET1 category for it, mirroring
   exactly how "LT_3000" is already structured (including the still-present
   Sales/Customer Relations/Security rows, even though those 3 categories
   are soft-deleted post the 2026-08-20 redesign — kept for consistency with
   the other 3 tiers, which never deleted their rows either; and Technical's
   TWO rows — its own fixed formula plus the merged former "FTs/LT/TO" rate,
   same convention as every other tier since the 2026-08-20 category merge).
2. Narrow the existing "LT_3000" tier's min_value from NULL to 1500 (and
   relabel it "1500-3000 m3" to match). This is NOT cosmetic: _find_tier()
   in both app/services/staffing_norms.py and app/services/headcount.py
   scans NormTier rows with no ORDER BY and returns the FIRST range match —
   without this, "< 1500" and the old open-ended "< 3000" (min_value=NULL)
   would both match any volume under 1500, and which one "wins" would
   depend on unspecified MySQL row order. Narrowing LT_3000 to [1500, 3000)
   makes the 4 brackets a true non-overlapping partition, so tier
   resolution is correct regardless of row order.

Idempotent — safe to re-run (checks for an existing "LT_1500" tier_key and
an already-narrowed LT_3000 before doing anything).

Run once: venv\\Scripts\\python add_lt1500_tier.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from app.extensions import db
from app.models import NormTier, NormRoleCategory, NormRequirement, NormScope, NormSheet, NormRequirementType

FIXED = NormRequirementType.FIXED
NONE_REQ = NormRequirementType.NONE
MERGE_NOTE = "Merged from 'FTs/LT/TO' - combined with Technical's own formula per department-based reclassification."

# (category_name, requirement_type, fixed_count, notes)
LT_1500_REQUIREMENTS = [
    ("Plant Manager",                 FIXED,    1, None),
    ("Technical",                     FIXED,    1, None),
    ("Technical",                     FIXED,    2, MERGE_NOTE),
    ("Batchers/Production Officer",   FIXED,    1, None),
    ("Materials",                     FIXED,    1, None),
    ("Customer Relations",            NONE_REQ, None, None),
    ("Assistant",                     FIXED,    1, None),
    ("Sales",                         FIXED,    2, None),
    ("Security",                      FIXED,    2, None),
]

app = create_app()
with app.app_context():
    lt_3000 = NormTier.query.filter_by(scope=NormScope.PLANT, sheet=NormSheet.SHEET1, tier_key="LT_3000").first()
    if not lt_3000:
        raise SystemExit("'LT_3000' tier not found — expected the original seed to already be applied.")

    existing_lt_1500 = NormTier.query.filter_by(scope=NormScope.PLANT, sheet=NormSheet.SHEET1, tier_key="LT_1500").first()
    if existing_lt_1500:
        print("'LT_1500' tier already exists (id=%d) — nothing to insert." % existing_lt_1500.id)
    else:
        lt_1500 = NormTier(
            tier_key="LT_1500", tier_label="< 1500 m³", scope=NormScope.PLANT, sheet=NormSheet.SHEET1,
            min_value=None, max_value=1500, sort_order=lt_3000.sort_order - 1,
        )
        db.session.add(lt_1500)
        db.session.flush()
        print(f"Inserted 'LT_1500' tier (id={lt_1500.id}).")

        cats = {c.name: c for c in NormRoleCategory.query.filter_by(scope=NormScope.PLANT, sheet=NormSheet.SHEET1).all()}
        n = 0
        for cat_name, req_type, fixed_count, notes in LT_1500_REQUIREMENTS:
            cat = cats[cat_name]
            db.session.add(NormRequirement(
                tier_id=lt_1500.id, norm_role_category_id=cat.id,
                requirement_type=req_type, fixed_count=fixed_count, notes=notes,
            ))
            n += 1
        print(f"Inserted {n} requirement row(s) for 'LT_1500'.")

    if lt_3000.min_value is None:
        lt_3000.min_value = 1500
        lt_3000.tier_label = "1500-3000 m³"
        print(f"Narrowed 'LT_3000' (id={lt_3000.id}) to min_value=1500, relabeled to '1500-3000 m³'.")
    else:
        print(f"'LT_3000' (id={lt_3000.id}) already has min_value={lt_3000.min_value} — left as-is.")

    db.session.commit()
    print("\nDone.")
