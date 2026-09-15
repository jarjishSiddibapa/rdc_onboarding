"""
One-off: applies the stakeholder-verified raw-name -> correct-ERP-name
mapping from Unresolved_Plant_Names.xlsx (Source | Raw Name | Employee Count
| Correct ERP Name) to PlantDvtMapping.

For each row whose "Correct ERP Name" matches a real DVT erp_name or
daily_tracker_name: groups raw names by which DVT plant_code they resolve
to (several raw names can point at the same plant), reuses an existing
PlantDvtMapping row for that plant_code if auto_match_plants() already
created one, otherwise creates ONE new row (picking the raw name with the
highest employee count as canonical, so the most-represented spelling is
what shows in the admin UI). Every other raw name in the group becomes a
PlantNameAlias, mirroring auto_match_plants()'s own duplicate-prevention
logic (case-insensitive lookups throughout, since plant_location_name and
alias_name are both utf8mb4_unicode_ci in MySQL).

Written MANUAL, not AUTO_EXACT — this is human-verified, not
algorithmically string-matched, and MANUAL rows are never touched again by
auto_match_plants().

Run once: venv\\Scripts\\python apply_manual_plant_mapping.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from app.extensions import db
from app.models import PlantDvtMapping, PlantNameAlias, ClusterNameMapping, MatchConfidence
from app.services.matching import _normalize

import openpyxl

XLSX_PATH = r"C:\Users\Jarjish Sidhabappa\Downloads\Unresolved_Plant_Names.xlsx"
UNRESOLVED_MARKERS = {"didn't find in erp?", "can't identify if lodha captive or haven"}

app = create_app()
with app.app_context():
    from app.integrations import dvt

    wb = openpyxl.load_workbook(XLSX_PATH, data_only=True)
    ws = wb["Unresolved Plant Names"]
    excel_rows = list(ws.iter_rows(values_only=True))[1:]

    dvt_plants = dvt.fetch_all_plants()
    erp_by_norm = {_normalize(p["erp_name"]): p for p in dvt_plants if p.get("erp_name")}
    tracker_by_norm = {_normalize(p["daily_tracker_name"]): p for p in dvt_plants if p.get("daily_tracker_name")}
    cluster_by_region = {
        _normalize(c.dvt_region): c.id
        for c in ClusterNameMapping.query.filter_by(is_deleted=False).all() if c.dvt_region
    }

    # ── Group excel rows by resolved DVT plant_code ──
    groups = {}   # plant_code -> {"plant": dvt_plant, "names": [(raw_name, count), ...]}
    still_unresolved = []
    for source, raw_name, count, correct in excel_rows:
        correct = (correct or "").strip()
        if not correct or correct in UNRESOLVED_MARKERS:
            still_unresolved.append((source, raw_name, count, correct or "(blank)"))
            continue
        norm_correct = _normalize(correct)
        plant = erp_by_norm.get(norm_correct) or tracker_by_norm.get(norm_correct)
        if not plant:
            still_unresolved.append((source, raw_name, count, f"'{correct}' not found in live DVT erp_name/tracker_name"))
            continue
        groups.setdefault(plant.get("plant_code"), {"plant": plant, "names": []})["names"].append((raw_name, count or 0))

    existing_by_name_ci = {r.plant_location_name.lower(): r for r in PlantDvtMapping.query.all()}
    existing_alias_names_ci = {a.alias_name.lower() for a in PlantNameAlias.query.all()}

    applied = 0
    for code, g in groups.items():
        plant = g["plant"]
        names_here = [n for n, c in g["names"]]

        # Prefer an existing row already tracking this plant_code (created
        # by auto_match_plants() via a different name entirely, or a prior
        # manual correction) — never create a second row for the same plant.
        existing_for_code = next((r for r in existing_by_name_ci.values()
                                   if not r.is_deleted and r.dvt_plant_code == code), None)
        if existing_for_code:
            row = existing_for_code
            canonical_key = row.plant_location_name.lower()
        else:
            # Pick the raw name with the most employees as canonical — the
            # most-represented real-world spelling.
            canonical = max(g["names"], key=lambda t: t[1])[0]
            canonical_key = canonical.lower()
            row = existing_by_name_ci.get(canonical_key)
            if row is None:
                row = PlantDvtMapping(plant_location_name=canonical)
                db.session.add(row)
                db.session.flush()
            elif row.is_deleted:
                row.is_deleted = False
                row.is_active = True
            row.dvt_plant_code = plant.get("plant_code")
            row.dvt_daily_tracker_name = plant.get("daily_tracker_name")
            row.dvt_erp_name = plant.get("erp_name")
            row.cluster_id = cluster_by_region.get(_normalize(plant.get("region")))
            row.match_confidence = MatchConfidence.MANUAL
            row.match_score = 100.0
            row.matched_on = None
            existing_by_name_ci[canonical_key] = row

        for n in names_here:
            n_key = n.lower()
            if n_key == canonical_key:
                continue
            if n_key not in existing_alias_names_ci:
                db.session.add(PlantNameAlias(alias_name=n, plant_dvt_mapping_id=row.id))
                existing_alias_names_ci.add(n_key)
            stray = existing_by_name_ci.get(n_key)
            if stray is not None and stray.id != row.id and stray.match_confidence != MatchConfidence.MANUAL:
                stray.is_deleted = True
                stray.is_active = False
        applied += 1
        print(f"MAPPED: plant_code={code} -> '{row.plant_location_name}' (aliases: {[n for n in names_here if n.lower() != canonical_key]})")

    db.session.commit()
    print(f"\nApplied {applied} plant group(s) from {len(groups)} DVT plant_code(s) "
          f"({sum(len(g['names']) for g in groups.values())} raw name(s) total).")
    print(f"\nStill unresolved ({len(still_unresolved)}):")
    for r in still_unresolved:
        print(" ", r)
