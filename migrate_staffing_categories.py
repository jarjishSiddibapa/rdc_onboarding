"""
One-off migration: reshapes the PLANT/SHEET1 NormRoleCategory set from the old
per-designation-mapped roles to the 6 department-driven manpower categories
the user specified directly (Plant Manager, Technical, Batchers/Production
Officer, Materials, Assistant, Operations).

Does NOT touch CLUSTER-scope categories/tiers/requirements or Sheet2 — both
stay exactly as seeded. Unlike seed_staffing_norms.py, this does NOT delete
and recreate every row (that would orphan historical StaffingGateCheck /
EmployeeLocationSnapshot FK references) — it renames/reassigns/soft-deletes
in place.

Steps:
1. Rename "Plant IC" -> "Plant Manager" (same row/id).
2. Reassign the 3 "FTs/LT/TO" NormRequirement rows (one per plant tier) onto
   "Technical" -- Technical now has 2 requirement rows per tier (its own
   fixed-1, plus the reassigned FT/LT/TO formula), summed at read time by
   the updated compute_allowed_headcount/_calc_allowed helpers.
3. Repoint any Designation.norm_category_id currently on "FTs/LT/TO" to
   "Technical".
4. Soft-delete the now-empty "FTs/LT/TO", "Sales", "Customer Relations",
   "Security" categories (existing soft-delete convention -- drops them from
   the admin Designation-form dropdown and the snapshot loop automatically).
5. Insert a new "Operations" category (PLANT/SHEET1, ungated, no
   requirements -- always shown as openly hireable, visibility only).

Run once: venv\\Scripts\\python migrate_staffing_categories.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from app.extensions import db
from app.models import NormRoleCategory, NormRequirement, Designation, NormScope, NormSheet

app = create_app()
with app.app_context():
    cats = {c.name: c for c in NormRoleCategory.query.filter_by(scope=NormScope.PLANT, sheet=NormSheet.SHEET1).all()}

    plant_ic = cats["Plant IC"]
    technical = cats["Technical"]
    fts_lt_to = cats["FTs/LT/TO"]
    sales = cats["Sales"]
    customer_relations = cats["Customer Relations"]
    security = cats["Security"]

    # 1. Rename Plant IC -> Plant Manager
    plant_ic.name = "Plant Manager"
    print(f"Renamed category id={plant_ic.id} 'Plant IC' -> 'Plant Manager'")

    # 2. Reassign FTs/LT/TO's NormRequirement rows onto Technical
    reqs = NormRequirement.query.filter_by(norm_role_category_id=fts_lt_to.id).all()
    for r in reqs:
        r.norm_role_category_id = technical.id
        note = "Merged from 'FTs/LT/TO' — combined with Technical's own formula per department-based reclassification."
        r.notes = f"{r.notes} {note}".strip() if r.notes else note
    print(f"Reassigned {len(reqs)} requirement row(s) from 'FTs/LT/TO' (id={fts_lt_to.id}) to 'Technical' (id={technical.id})")

    # 3. Repoint Designations pointing at FTs/LT/TO
    desigs = Designation.query.filter_by(norm_category_id=fts_lt_to.id).all()
    for d in desigs:
        d.norm_category_id = technical.id
    print(f"Repointed {len(desigs)} designation(s) from 'FTs/LT/TO' to 'Technical'")

    # 4. Soft-delete retired categories
    for cat in (fts_lt_to, sales, customer_relations, security):
        cat.is_active = False
        cat.is_deleted = True
        print(f"Soft-deleted category id={cat.id} '{cat.name}'")

    # 5. Add Operations (ungated, visibility-only, no NormRequirement rows)
    existing_ops = NormRoleCategory.query.filter_by(name="Operations", scope=NormScope.PLANT, sheet=NormSheet.SHEET1, is_deleted=False).first()
    if existing_ops:
        print("'Operations' category already exists, skipping insert")
    else:
        max_sort = db.session.query(db.func.max(NormRoleCategory.sort_order)).filter_by(scope=NormScope.PLANT, sheet=NormSheet.SHEET1).scalar() or 0
        ops = NormRoleCategory(
            name="Operations", scope=NormScope.PLANT, sheet=NormSheet.SHEET1,
            is_capacity_gated=False, sort_order=max_sort + 1,
        )
        db.session.add(ops)
        print("Inserted new 'Operations' category (ungated, visibility-only)")

    db.session.commit()
    print("\nDone.")
