"""
One-off: adds the stakeholder-provided real plant lists for ROBO and
Ultrafine (2026-09-21) as PlantLocation rows. Until this ran, zero
PlantLocation rows existed for either company (confirmed in CLAUDE.md's
Multi-Company Support section) — the onboarding form's Company Code
dropdown and Staffing Status page had nothing to show for these two
companies even once an initiator/BH was company-scoped to them.

These same names are what gets pushed to Truein as sub_site/sitePoint on
hiring — build_payload() in app/integrations/truein.py already falls back
to the raw PlantLocation name when no PlantDvtMapping row exists (the
2026-09-15 "Bonus fix" — Ultrafine/ROBO never have DVT mappings, that
table is RDC/DVT-specific), so no further code change is needed for the
Truein side; adding the rows here is the whole fix.

Idempotent — skips any (name, company) pair that already exists.

Run once: venv\\Scripts\\python add_robo_ultrafine_plants.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from app.extensions import db
from app.models import PlantLocation

ROBO_PLANTS = [
    "Robo-Keesara_4 Plant",
    "Robo-Lakdaram_Plant",
    "Robo-Girmapur Plant",
    "Robo-AP_RO",
    "Robo-RDU_Deshmukh",
    "Robo-Girmapur Plant 2",
    "ROBO - Mumbai",
    "Robo-RO_Bangalore",
    "ROBO - Kerala",
]

ULTRAFINE_PLANTS = [
    "ULT - Surat",
    "ULT - Galsi",
    "ULT - Nagpur",
    "Panagarh-MS",
    "ULT-Nellore",
    "ULT-Wada",
    "Panagarh-Crusher unit",
    "ULT-Koradhi",
]

app = create_app()
with app.app_context():
    added = []
    for company, names in (("ROBO", ROBO_PLANTS), ("Ultrafine", ULTRAFINE_PLANTS)):
        existing = {
            p.name.lower() for p in
            PlantLocation.query.filter_by(company=company, is_deleted=False).all()
        }
        for i, name in enumerate(names):
            if name.lower() in existing:
                continue
            db.session.add(PlantLocation(name=name, company=company, is_active=True, sort_order=i))
            added.append(f"{company}: {name}")

    if added:
        db.session.commit()
        print(f"Added {len(added)} plant(s):")
        for a in added:
            print(f"  {a}")
    else:
        print("Every plant already exists — nothing to do.")

    print("\nDone.")
