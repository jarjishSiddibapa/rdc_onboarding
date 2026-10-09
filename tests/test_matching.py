"""
Plant / cluster auto-match (app/services/matching.py) with the three external systems mocked.
This decides which Daily Volume Tracker plant every employee and hiring check is attributed to,
so the rules (exact-only plants, never touching MANUAL rows, no duplicate rows for one plant,
no crash on soft-deleted names) are pinned down here.
"""
from unittest.mock import patch

import pytest

from app.models import (
    PlantDvtMapping, PlantNameAlias, ClusterNameMapping, MatchConfidence, PlantLocation,
)
from app.services import matching


@pytest.fixture(autouse=True)
def _app_ctx(app):
    with app.app_context():
        yield


DVT = [
    {"plant_code": "M1", "erp_name": "MUM-Deonar", "daily_tracker_name": "Mumbai Deonar", "region": "Mumbai"},
    {"plant_code": "N1", "erp_name": "MH-Nagpur 1", "daily_tracker_name": "Nagpur One", "region": "Nagpur"},
    {"plant_code": "V1", "erp_name": "GUJ-Vapi", "daily_tracker_name": "Vapi", "region": "Gujarat"},
]


def _run(db, plants=(), zinghr_locations=(), sub_sites=(), dvt=DVT):
    for name in plants:
        if not PlantLocation.query.filter_by(name=name).first():
            db.session.add(PlantLocation(name=name, company="RDC"))
    db.session.commit()
    zinghr_rows = [{"Location": loc} for loc in zinghr_locations]
    with patch("app.services.matching.dvt.fetch_all_plants", return_value=dvt), \
         patch("app.services.matching.zinghr.fetch_active_employees", return_value=zinghr_rows), \
         patch("app.services.matching.truein.get_cached_sub_sites_if_warm", return_value=list(sub_sites)):
        return matching.auto_match_plants()


def _row(name):
    return PlantDvtMapping.query.filter(PlantDvtMapping.plant_location_name.ilike(name)).first()


class TestAutoMatchPlants:
    def test_exact_erp_name_match(self, db):
        db.session.add(ClusterNameMapping(canonical_cluster_name="Mumbai Cluster", dvt_region="Mumbai"))
        db.session.commit()
        res = _run(db, plants=["MUM-Deonar"])
        row = _row("MUM-Deonar")
        assert res == {"matched_exact": 1, "unmatched": 0, "total": 1}
        assert (row.dvt_plant_code, row.match_confidence, row.matched_on, row.match_score) == \
               ("M1", MatchConfidence.AUTO_EXACT, "erp_name", 100.0)
        assert row.cluster.canonical_cluster_name == "Mumbai Cluster"

    def test_match_by_tracker_name(self, db):
        _run(db, plants=["Nagpur One"])
        row = _row("Nagpur One")
        assert row.dvt_plant_code == "N1" and row.matched_on == "tracker_name"

    def test_matching_ignores_case_hyphens_and_spacing(self, db):
        _run(db, plants=["mum - deonar"])
        assert _row("mum - deonar").dvt_plant_code == "M1"

    def test_unknown_name_is_left_unmatched_never_guessed(self, db):
        res = _run(db, plants=["MUM-Deonarr", "Totally Unknown"])      # near-miss spelling must NOT match
        assert res["matched_exact"] == 0 and res["unmatched"] == 2
        for n in ("MUM-Deonarr", "Totally Unknown"):
            r = _row(n)
            assert r.dvt_plant_code is None and r.match_confidence == MatchConfidence.UNMATCHED and r.match_score == 0.0

    def test_spelling_variants_of_one_plant_share_a_single_row_plus_alias(self, db):
        res = _run(db, plants=["GUJ-Vapi"], zinghr_locations=["GUJ- Vapi", "GUJ-Vapi"])
        rows = PlantDvtMapping.query.filter_by(dvt_plant_code="V1", is_deleted=False).all()
        assert len(rows) == 1 and res["matched_exact"] == 1
        assert {a.alias_name for a in PlantNameAlias.query.filter_by(plant_dvt_mapping_id=rows[0].id)} == {"GUJ- Vapi"} \
               or rows[0].plant_location_name == "GUJ- Vapi"

    def test_names_that_differ_only_by_case_never_create_two_rows(self, db):
        _run(db, plants=["Head Office"], zinghr_locations=["Head office"])
        assert PlantDvtMapping.query.filter(PlantDvtMapping.plant_location_name.ilike("head office")).count() == 1

    def test_manual_row_is_never_overwritten_and_wins_as_canonical(self, db):
        db.session.add(PlantDvtMapping(plant_location_name="GUJ- Vapi", dvt_plant_code="HAND", dvt_erp_name="Hand Set",
                                       match_confidence=MatchConfidence.MANUAL))
        db.session.commit()
        _run(db, plants=["GUJ-Vapi"], zinghr_locations=["GUJ- Vapi"])
        manual = _row("GUJ- Vapi")
        assert manual.dvt_plant_code == "HAND" and manual.match_confidence == MatchConfidence.MANUAL and not manual.is_deleted
        # a manual row for a name that matches nothing is skipped as well
        db.session.add(PlantDvtMapping(plant_location_name="Odd Site", dvt_plant_code="X9", match_confidence=MatchConfidence.MANUAL))
        db.session.commit()
        _run(db, plants=["Odd Site"])
        assert _row("Odd Site").dvt_plant_code == "X9"

    def test_stale_links_are_cleared_when_a_name_stops_matching(self, db):
        db.session.add(PlantDvtMapping(plant_location_name="Gone Plant", dvt_plant_code="OLD",
                                       match_confidence=MatchConfidence.AUTO_EXACT, match_score=100.0, matched_on="erp_name"))
        db.session.add(PlantLocation(name="Gone Plant", company="RDC"))
        db.session.commit()
        _run(db)
        r = _row("Gone Plant")
        assert r.dvt_plant_code is None and r.match_confidence == MatchConfidence.UNMATCHED and r.matched_on is None

    def test_leftover_fuzzy_rows_are_swept_to_unmatched(self, db):
        db.session.add(PlantDvtMapping(plant_location_name="Old Fuzzy", dvt_plant_code="F1",
                                       match_confidence=MatchConfidence.AUTO_FUZZY, match_score=88.0))
        db.session.commit()
        res = _run(db)
        r = _row("Old Fuzzy")
        assert r.match_confidence == MatchConfidence.UNMATCHED and r.dvt_plant_code is None and res["unmatched"] >= 1

    def test_soft_deleted_row_is_reactivated_not_reinserted(self, db):
        db.session.add(PlantDvtMapping(plant_location_name="MUM-Deonar", is_deleted=True, is_active=False,
                                       match_confidence=MatchConfidence.UNMATCHED))
        db.session.commit()
        _run(db, plants=["MUM-Deonar"])                                  # would raise IntegrityError if re-inserted
        r = _row("MUM-Deonar")
        assert r.is_deleted is False and r.dvt_plant_code == "M1"
        assert PlantDvtMapping.query.filter(PlantDvtMapping.plant_location_name.ilike("mum-deonar")).count() == 1

    def test_second_run_is_idempotent(self, db):
        _run(db, plants=["MUM-Deonar", "Nowhere"])
        first = PlantDvtMapping.query.count()
        res = _run(db, plants=["MUM-Deonar", "Nowhere"])
        assert PlantDvtMapping.query.count() == first and res["matched_exact"] == 1 and res["unmatched"] == 1

    def test_truein_sub_site_is_linked_exact_and_fuzzy_but_only_when_close(self, db):
        _run(db, plants=["MUM-Deonar", "Nagpur One", "Random Place"], sub_sites=["MUM Deonar", "Nagpur Onee", "ZZZ"])
        assert PlantDvtMapping.query.filter_by(dvt_plant_code="M1").one().truein_sub_site == "MUM Deonar"
        assert PlantDvtMapping.query.filter_by(dvt_plant_code="N1").one().truein_sub_site == "Nagpur Onee"
        assert _row("Random Place").truein_sub_site is None

    def test_zinghr_outage_does_not_break_matching(self, db):
        db.session.add(PlantLocation(name="MUM-Deonar", company="RDC"))
        db.session.commit()
        with patch("app.services.matching.dvt.fetch_all_plants", return_value=DVT), \
             patch("app.services.matching.zinghr.fetch_active_employees", side_effect=RuntimeError("401")), \
             patch("app.services.matching.truein.get_cached_sub_sites_if_warm", return_value=None):
            res = matching.auto_match_plants()
        assert res["matched_exact"] == 1

    def test_inactive_or_deleted_admin_plants_are_not_candidates(self, db):
        db.session.add(PlantLocation(name="MUM-Deonar", company="RDC", is_active=False))
        db.session.add(PlantLocation(name="MH-Nagpur 1", company="RDC", is_deleted=True))
        db.session.commit()
        assert _run(db)["total"] == 0


class TestAutoMatchClusters:
    def _run(self, regions, cities=(), categories=()):
        plants = [{"plant_code": f"P{i}", "region": r} for i, r in enumerate(regions)]
        with patch("app.services.matching.dvt.fetch_all_plants", return_value=plants), \
             patch("app.services.matching.zinghr.fetch_active_employees", return_value=[{"City": c} for c in cities]), \
             patch("app.services.matching.truein.get_cached_employees_if_warm", return_value=[{"category": c} for c in categories]):
            return matching.auto_match_clusters()

    def test_exact_fuzzy_and_unmatched(self, db):
        res = self._run(["Mumbai", "Greater Noida", "Atlantis"], cities=["mumbai", "Noida Greater Area"], categories=["Mumbai"])
        assert res["total"] == 3
        by = {c.canonical_cluster_name: c for c in ClusterNameMapping.query.all()}
        assert by["Mumbai"].match_confidence == MatchConfidence.AUTO_EXACT
        assert by["Mumbai"].zinghr_city == "mumbai" and by["Mumbai"].truein_category == "Mumbai"
        assert by["Greater Noida"].match_confidence == MatchConfidence.AUTO_FUZZY       # one side only fuzzy
        assert by["Atlantis"].match_confidence == MatchConfidence.UNMATCHED
        assert (res["matched_exact"], res["matched_fuzzy"], res["unmatched"]) == (1, 1, 1)

    def test_manual_cluster_is_left_alone(self, db):
        db.session.add(ClusterNameMapping(canonical_cluster_name="Mumbai", zinghr_city="Hand Picked",
                                          match_confidence=MatchConfidence.MANUAL))
        db.session.commit()
        self._run(["Mumbai"], cities=["Mumbai"], categories=["Mumbai"])
        c = ClusterNameMapping.query.filter_by(canonical_cluster_name="Mumbai").one()
        assert c.zinghr_city == "Hand Picked" and c.match_confidence == MatchConfidence.MANUAL

    def test_junk_truein_categories_are_ignored(self, db):
        res = self._run(["On Roll"], categories=["on roll", "Other", "1st", "2nd", "onroll"])
        assert res["truein_categories_seen"] == 0

    def test_rerun_updates_instead_of_duplicating(self, db):
        self._run(["Mumbai"], cities=["Mumbai"])
        self._run(["Mumbai"], cities=["Mumbai"])
        assert ClusterNameMapping.query.filter_by(canonical_cluster_name="Mumbai").count() == 1


def test_normalize_helper():
    assert matching._normalize("  ULT - Nagpur ") == "ult nagpur"
    assert matching._normalize("A_B.C/D") == "a b c d"
    assert matching._normalize(None) == ""
