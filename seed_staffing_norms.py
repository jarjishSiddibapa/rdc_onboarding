"""
Seeds the RDC staffing-norms reference data (NormRoleCategory, NormTier,
NormRequirement) from "Staffing Norms for Plants - July 2025.xlsx".
Replaces all existing rows in these three tables. Run once:
    venv\\Scripts\\python seed_staffing_norms.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from app.extensions import db
from app.models import NormRoleCategory, NormTier, NormRequirement, NormScope, NormSheet, NormRequirementType

# ── Role categories ──────────────────────────────────────────────────────────
# (name, scope, sheet)
PLANT_CATEGORIES_SHEET1 = [
    "Plant IC", "Technical", "Batchers/Production Officer", "Materials",
    "FTs/LT/TO", "Customer Relations", "Assistant", "Sales", "Security",
]
CLUSTER_CATEGORIES_SHEET1 = [
    "Accounts incl. Incharge", "Credit Control", "CDS",
    "Officer Sales Coordination", "Area Technical Manager", "EA",
]
# Sheet2 reuses these names from Sheet1's plant categories where identical;
# only these two are genuinely new/differently-named.
PLANT_CATEGORIES_SHEET2_NEW = ["Technical Incharge", "Helpers"]

# Roles NOT actually tied to concrete production — hiring for these is never
# blocked by plant volume, regardless of what PLANT_REQUIREMENTS says for
# their tier. Confirmed by the stakeholder: only roles that scale with
# output (Plant IC, Technical, Batchers/Production Officer, Materials,
# FTs/LT/TO, Assistant) should be volume-gated; Sales, Customer Relations,
# and Security should not.
UNGATED_CATEGORIES = {"Sales", "Customer Relations", "Security"}

# ── Tiers ────────────────────────────────────────────────────────────────────
# (tier_key, tier_label, scope, sheet, min_value, max_value)
TIERS = [
    ("LT_3000",     "< 3000 m³",     NormScope.PLANT,   NormSheet.SHEET1, None, 3000),
    ("3000_5000",   "3000-5000 m³",  NormScope.PLANT,   NormSheet.SHEET1, 3000, 5000),
    ("GT_5000",     "> 5000 m³",     NormScope.PLANT,   NormSheet.SHEET1, 5000, None),
    ("1_2_PLANTS",  "1-2 Plants",    NormScope.CLUSTER, NormSheet.SHEET1, 1,    3),
    ("3_4_PLANTS",  "3-4 Plants",    NormScope.CLUSTER, NormSheet.SHEET1, 3,    5),
    ("GT_4_PLANTS", "> 4 Plants",    NormScope.CLUSTER, NormSheet.SHEET1, 5,    None),
    ("10000",       "10000 m³ (Sheet2)", NormScope.PLANT, NormSheet.SHEET2, 10000, None),
]

FIXED = NormRequirementType.FIXED
RATE = NormRequirementType.RATE_PER_VOLUME
PER_BH = NormRequirementType.PER_BUSINESS_HEAD
NONE_REQ = NormRequirementType.NONE

# ── Plant table requirements: role -> [tier1, tier2, tier3] ─────────────────
# Each cell: (requirement_type, fixed_count, rate_per_unit, unit_volume)
PLANT_REQUIREMENTS = {
    "Plant IC":                    [(FIXED, 1, None, None), (FIXED, 1, None, None), (FIXED, 1, None, None)],
    "Technical":                   [(FIXED, 1, None, None), (FIXED, 1, None, None), (FIXED, 1, None, None)],
    "Batchers/Production Officer": [(FIXED, 2, None, None), (FIXED, 2, None, None), (FIXED, 3, None, None)],
    "Materials":                   [(FIXED, 1, None, None), (FIXED, 1, None, None), (FIXED, 1, None, None)],
    "FTs/LT/TO":                   [(FIXED, 3, None, None), (RATE, None, 1, 900),   (RATE, None, 1, 900)],
    "Customer Relations":          [(NONE_REQ, None, None, None), (FIXED, 1, None, None), (FIXED, 1, None, None)],
    "Assistant":                   [(FIXED, 2, None, None), (FIXED, 2, None, None), (FIXED, 3, None, None)],
    "Sales":                       [(FIXED, 2, None, None), (FIXED, 3, None, None), (FIXED, 3, None, None)],
    "Security":                    [(FIXED, 2, None, None), (FIXED, 2, None, None), (FIXED, 2, None, None)],
}

# ── Cluster table requirements: role -> [tier1, tier2, tier3] ───────────────
CLUSTER_REQUIREMENTS = {
    "Accounts incl. Incharge":     [(FIXED, 0, None, None), (FIXED, 1, None, None), (RATE, None, 1, 15000)],
    "Credit Control":              [(NONE_REQ, None, None, None), (FIXED, 1, None, None), (FIXED, 1, None, None)],
    "CDS":                         [(NONE_REQ, None, None, None), (FIXED, 2, None, None), (FIXED, 2, None, None)],
    "Officer Sales Coordination":  [(NONE_REQ, None, None, None), (FIXED, 1, None, None), (FIXED, 1, None, None)],
    "Area Technical Manager":      [(NONE_REQ, None, None, None), (NONE_REQ, None, None, None), (FIXED, 1, None, None)],
    "EA":                          [(NONE_REQ, None, None, None), (NONE_REQ, None, None, None), (PER_BH, None, None, None)],
}

# ── Sheet2 requirements: role -> (requirement_type, fixed_count) — single "10000" tier ──
SHEET2_REQUIREMENTS = [
    ("Plant IC",                    1),
    ("Technical Incharge",          1),
    ("Batchers/Production Officer", 4),
    ("Materials",                   2),
    ("FTs/LT/TO",                   10),
    ("Customer Relations",          1),
    ("Helpers",                     4),
    ("Security",                    2),
]

app = create_app()

with app.app_context():
    deleted_req = NormRequirement.query.delete()
    deleted_tier = NormTier.query.delete()
    deleted_cat = NormRoleCategory.query.delete()
    db.session.commit()
    print(f"Removed {deleted_req} requirement(s), {deleted_tier} tier(s), {deleted_cat} category(ies).")

    # ── Categories ──
    categories = {}  # name -> NormRoleCategory
    sort_order = 0
    for name in PLANT_CATEGORIES_SHEET1:
        cat = NormRoleCategory(name=name, scope=NormScope.PLANT, sheet=NormSheet.SHEET1, sort_order=sort_order,
                               is_capacity_gated=(name not in UNGATED_CATEGORIES))
        db.session.add(cat)
        categories[name] = cat
        sort_order += 1
    for name in CLUSTER_CATEGORIES_SHEET1:
        cat = NormRoleCategory(name=name, scope=NormScope.CLUSTER, sheet=NormSheet.SHEET1, sort_order=sort_order)
        db.session.add(cat)
        categories[name] = cat
        sort_order += 1
    for name in PLANT_CATEGORIES_SHEET2_NEW:
        cat = NormRoleCategory(name=name, scope=NormScope.PLANT, sheet=NormSheet.SHEET2, sort_order=sort_order)
        db.session.add(cat)
        categories[name] = cat
        sort_order += 1
    db.session.flush()
    print(f"Inserted {len(categories)} role categories.")

    # ── Tiers ──
    tiers = {}  # tier_key -> NormTier
    for i, (tier_key, tier_label, scope, sheet, min_v, max_v) in enumerate(TIERS):
        tier = NormTier(tier_key=tier_key, tier_label=tier_label, scope=scope, sheet=sheet,
                         min_value=min_v, max_value=max_v, sort_order=i)
        db.session.add(tier)
        tiers[tier_key] = tier
    db.session.flush()
    print(f"Inserted {len(tiers)} tiers.")

    # ── Plant-table requirements ──
    plant_tier_keys = ["LT_3000", "3000_5000", "GT_5000"]
    n = 0
    for role, cells in PLANT_REQUIREMENTS.items():
        for tier_key, (req_type, fixed_count, rate_per_unit, unit_volume) in zip(plant_tier_keys, cells):
            db.session.add(NormRequirement(
                tier_id=tiers[tier_key].id, norm_role_category_id=categories[role].id,
                requirement_type=req_type, fixed_count=fixed_count,
                rate_per_unit=rate_per_unit, unit_volume=unit_volume,
            ))
            n += 1

    # ── Cluster-table requirements ──
    cluster_tier_keys = ["1_2_PLANTS", "3_4_PLANTS", "GT_4_PLANTS"]
    for role, cells in CLUSTER_REQUIREMENTS.items():
        for tier_key, (req_type, fixed_count, rate_per_unit, unit_volume) in zip(cluster_tier_keys, cells):
            db.session.add(NormRequirement(
                tier_id=tiers[tier_key].id, norm_role_category_id=categories[role].id,
                requirement_type=req_type, fixed_count=fixed_count,
                rate_per_unit=rate_per_unit, unit_volume=unit_volume,
            ))
            n += 1

    # ── Sheet2 requirements ──
    for role, fixed_count in SHEET2_REQUIREMENTS:
        db.session.add(NormRequirement(
            tier_id=tiers["10000"].id, norm_role_category_id=categories[role].id,
            requirement_type=FIXED, fixed_count=fixed_count,
            notes="Sheet2 — relationship to Sheet1 tiers not yet confirmed by stakeholder",
        ))
        n += 1

    db.session.commit()
    print(f"Inserted {n} requirement rows.")
    print("\nDone.")
