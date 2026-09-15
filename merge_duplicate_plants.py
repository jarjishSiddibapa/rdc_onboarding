"""
One-off cleanup: merges 9 confirmed duplicate PlantDvtMapping rows (same
dvt_plant_code, two different plant_location_name spellings for the same
physical plant — e.g. "JK-Jammu" / "Jammu" both = DVT code JK1) that were
silently splitting real headcount across two rows.

For each pair: soft-deletes the loser row and adds its plant_location_name as
a PlantNameAlias pointing at the winner, so future ZingHR/Truein records
carrying the old name still resolve to the surviving plant (see
PlantNameAlias's docstring in app/models.py for why this is the right fix
rather than a hard delete).

Does NOT touch the other 7 duplicate-dvt_plant_code groups (Greater Noida,
Ludhiana, Raipur, Surat, Thrissur, Trivandrum, Coimbatore) — those look like
they might be genuinely different numbered plants mis-matched to one DVT
code, not name duplicates of the same plant, and need manual review.

Run once: venv\\Scripts\\python merge_duplicate_plants.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from app.extensions import db
from app.models import PlantDvtMapping, PlantNameAlias

# (winner_id, loser_id) — see plan doc for how these were picked (higher
# match_confidence, then truein_sub_site populated, then headcount, then id).
PAIRS = [
    (76, 212),   # JK1: JK-Jammu <- Jammu
    (101, 210),  # IN2: MP-Indore <- Indore
    (103, 211),  # JA1: MP-Jabalpur <- Jabalpur
    (78, 216),   # MG1: KAR-Mangalore <- Mangalore
    (175, 234),  # TY1: TN-Trichy <- Trichy
    (1, 241),    # VI3: AP - Vijaywada 2 <- Vijaywada
    (60, 16),    # H15: HYD-Kalpataru <- BG-Kalpataru
    (5, 7),      # GW1: ASM-Guwahati <- ASM-Guwahati-3
    (90, 178),   # KL3: KOL-Howrah <- ULT - Kol Howrah
]

app = create_app()
with app.app_context():
    merged = 0
    for winner_id, loser_id in PAIRS:
        winner = PlantDvtMapping.query.get(winner_id)
        loser = PlantDvtMapping.query.get(loser_id)
        if not winner or not loser:
            print(f"SKIP {winner_id}<-{loser_id}: row missing")
            continue
        if loser.is_deleted:
            print(f"SKIP {winner.plant_location_name}<-{loser.plant_location_name}: loser already merged")
            continue
        if winner.dvt_plant_code != loser.dvt_plant_code:
            print(f"SKIP {winner.plant_location_name}<-{loser.plant_location_name}: dvt codes no longer match, aborting this pair")
            continue

        existing_alias = PlantNameAlias.query.filter_by(alias_name=loser.plant_location_name).first()
        if not existing_alias:
            db.session.add(PlantNameAlias(alias_name=loser.plant_location_name, plant_dvt_mapping_id=winner.id))
        loser.is_deleted = True
        loser.is_active = False
        print(f"MERGED: '{loser.plant_location_name}' (id {loser.id}) -> '{winner.plant_location_name}' (id {winner.id}), dvt_plant_code={winner.dvt_plant_code}")
        merged += 1

    db.session.commit()
    print(f"\nDone. {merged} plant(s) merged.")
