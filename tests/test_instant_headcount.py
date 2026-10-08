"""
Headcount moves the moment a hire is approved (2026-10-08), and the full
ZingHR/Truein sync is nightly.
"""
import uuid
from datetime import datetime, timedelta

from app.extensions import db as _db
from app.models import (
    UserRole, RequestStatus, OnboardingRequest, ApprovalAction, ApprovalActionType,
    Designation, NormRoleCategory, NormScope, NormSheet, PlantDvtMapping, ClusterNameMapping,
    StaffingSnapshot, EmployeeLocationSnapshot, ExternalDesignationSource, PlantLocation,
)
from app.services import headcount, snapshot_refresh
from .conftest import login, _make_user

RUN = datetime(2026, 10, 8, 2, 0, 0)


def _world(db, company="RDC", plant="Plant One"):
    """A snapshot run + the plant/cluster/category/designation a hire needs."""
    cluster = ClusterNameMapping(canonical_cluster_name="Clu-" + uuid.uuid4().hex[:6])
    db.session.add(cluster)
    db.session.flush()
    cat = NormRoleCategory(name="Technical-" + uuid.uuid4().hex[:4], scope=NormScope.PLANT, sheet=NormSheet.SHEET1)
    db.session.add(cat)
    db.session.flush()
    if company == "RDC":
        db.session.add(PlantDvtMapping(plant_location_name=plant, cluster_id=cluster.id))
    else:
        db.session.add(PlantLocation(name=plant, company=company))
    db.session.add(Designation(name="Field Tech", company=company, norm_category_id=cat.id if company == "RDC" else None))
    # one pre-existing employee row + staffing row = "the current run"
    db.session.add(EmployeeLocationSnapshot(
        computed_at=RUN, source=ExternalDesignationSource.ZINGHR, employee_code="E1", employee_name="Old Timer",
        plant_location_key=plant, company=None if company == "RDC" else company))
    if company == "RDC":
        db.session.add(StaffingSnapshot(
            scope=NormScope.PLANT, location_key=plant, norm_role_category_id=cat.id,
            current_headcount=3, zinghr_count=2, truein_count=1, allowed_headcount=4, can_hire=True, computed_at=RUN))
    db.session.flush()
    return cluster, cat


def _req(db, user, company="RDC", plant="Plant One", name="Asha Rao", status=RequestStatus.ACTIVE):
    r = OnboardingRequest(initiated_by=user.id, status=status, public_token=uuid.uuid4().hex,
                          candidate_name=name, company_code=company, plant_location=plant, designation="Field Tech")
    db.session.add(r)
    db.session.flush()
    r.form_data = {"company_code": company, "associate_name": name, "plant_location": plant,
                   "designation": "Field Tech", "contract_from": "2026-10-09"}
    db.session.flush()
    return r


class TestRecordApprovedHire:
    def test_rdc_hire_bumps_staffing_row_and_adds_employee(self, db, app):
        init = _make_user("IhInit1", "ihinit1@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        cluster, cat = _world(db)
        req = _req(db, init)
        res = headcount.record_approved_hire(req)
        db.session.commit()
        assert res["recorded"] and res["staffing_row_updated"]
        row = StaffingSnapshot.query.filter_by(location_key="Plant One", norm_role_category_id=cat.id).one()
        assert row.current_headcount == 4 and row.truein_count == 2
        assert row.can_hire is False            # 4 of 4 allowed now
        emp = EmployeeLocationSnapshot.query.filter_by(employee_code=f"NEWHIRE-{req.id}").one()
        assert emp.employee_name == "Asha Rao" and emp.plant_location_key == "Plant One"
        assert emp.cluster_location_key == cluster.canonical_cluster_name
        assert emp.computed_at == RUN and emp.company is None
        assert emp.norm_role_category_id == cat.id and emp.date_of_joining == "2026-10-09"

    def test_visible_to_dashboard_readers(self, db, app):
        init = _make_user("IhInit2", "ihinit2@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        _world(db)
        req = _req(db, init)
        headcount.record_approved_hire(req)
        db.session.commit()
        names = [e.employee_name for e in headcount.get_employees_at_plant("Plant One")]
        assert "Asha Rao" in names and "Old Timer" in names

    def test_is_idempotent_per_run(self, db, app):
        init = _make_user("IhInit3", "ihinit3@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        _, cat = _world(db)
        req = _req(db, init)
        headcount.record_approved_hire(req)
        again = headcount.record_approved_hire(req)
        db.session.commit()
        assert again == {"recorded": False, "reason": "already recorded for this run"}
        assert StaffingSnapshot.query.filter_by(norm_role_category_id=cat.id).one().current_headcount == 4

    def test_creates_row_for_an_empty_bucket(self, db, app):
        init = _make_user("IhInit4", "ihinit4@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        _, cat = _world(db)
        StaffingSnapshot.query.filter_by(norm_role_category_id=cat.id).delete()
        # the run still has OTHER buckets (sparse table) — that's what makes it "the latest run"
        db.session.add(StaffingSnapshot(scope=NormScope.PLANT, location_key="Elsewhere",
                                        norm_role_category_id=_cat(db), current_headcount=1, computed_at=RUN))
        db.session.flush()
        req = _req(db, init)
        headcount.record_approved_hire(req)
        db.session.commit()
        row = StaffingSnapshot.query.filter_by(norm_role_category_id=cat.id).one()
        assert row.current_headcount == 1 and row.allowed_headcount is None and row.can_hire is None

    def test_no_snapshot_run_yet_is_a_clean_noop(self, db, app):
        init = _make_user("IhInit5", "ihinit5@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        req = _req(db, init)
        assert headcount.record_approved_hire(req) == {"recorded": False, "reason": "no snapshot run yet"}

    def test_other_company_hire_counts_at_its_plant(self, db, app):
        init = _make_user("IhInit6", "ihinit6@t.com", UserRole.INITIATOR, db, companies=["ROBO"])
        _world(db, company="ROBO", plant="Robo Plant")
        req = _req(db, init, company="ROBO", plant="Robo Plant")
        before = {r["plant"].name: r["headcount"] for r in headcount.get_other_company_plant_summary("ROBO")}
        headcount.record_approved_hire(req)
        db.session.commit()
        after = {r["plant"].name: r["headcount"] for r in headcount.get_other_company_plant_summary("ROBO")}
        assert after["Robo Plant"] == before["Robo Plant"] + 1
        assert EmployeeLocationSnapshot.query.filter_by(employee_code=f"NEWHIRE-{req.id}").one().company == "ROBO"


class TestNextRunReplacesInterimRows:
    def test_new_run_supersedes_interim_hire(self, db, app):
        """A later run (new computed_at) is what readers see — the interim row simply stops counting."""
        init = _make_user("IhInit7", "ihinit7@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        _world(db)
        headcount.record_approved_hire(_req(db, init))
        db.session.add(EmployeeLocationSnapshot(
            computed_at=RUN + timedelta(days=1), source=ExternalDesignationSource.TRUEIN,
            employee_code="T9", employee_name="Asha Rao", plant_location_key="Plant One"))
        db.session.commit()
        rows = headcount.get_employees_at_plant("Plant One")
        assert [e.employee_code for e in rows] == ["T9"]     # one Asha, from the real system


class TestReapplyAfterSync:
    def _approve(self, db, req, at):
        db.session.add(ApprovalAction(request_id=req.id, actor_id=req.initiated_by,
                                      action=ApprovalActionType.APPROVED, remark="ok", acted_at=at))
        db.session.flush()

    def test_hire_approved_during_sync_is_put_back(self, db, app):
        init = _make_user("IhInit8", "ihinit8@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        _world(db)
        req = _req(db, init)
        self._approve(db, req, RUN + timedelta(minutes=2))
        added = headcount.reapply_hires_since(RUN, RUN)
        assert added == 1
        assert EmployeeLocationSnapshot.query.filter_by(employee_code=f"NEWHIRE-{req.id}").count() == 1

    def test_skipped_when_the_fresh_run_already_lists_them(self, db, app):
        init = _make_user("IhInit9", "ihinit9@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        _world(db)
        db.session.add(EmployeeLocationSnapshot(
            computed_at=RUN, source=ExternalDesignationSource.TRUEIN, employee_code="T1",
            employee_name="  asha  RAO ", plant_location_key="Plant One"))
        req = _req(db, init)
        self._approve(db, req, RUN + timedelta(minutes=2))
        assert headcount.reapply_hires_since(RUN, RUN) == 0

    def test_hire_approved_before_the_sync_started_is_not_touched(self, db, app):
        init = _make_user("IhInit10", "ihinit10@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        _world(db)
        req = _req(db, init)
        self._approve(db, req, RUN - timedelta(hours=3))
        assert headcount.reapply_hires_since(RUN, RUN) == 0


class TestApprovalRouteCountsTheHire:
    def test_final_approval_adds_hire_immediately(self, client, db, app):
        init = _make_user("IhRInit", "ihrinit@t.com", UserRole.INITIATOR, db, companies=["ROBO"])
        drb = _make_user("IhRDrb", "ihrdrb@t.com", UserRole.DR_BHOON, db)
        _world(db, company="ROBO", plant="Robo Route Plant")
        req = _req(db, init, company="ROBO", plant="Robo Route Plant", status=RequestStatus.PENDING_DR_BHOON)
        db.session.commit()
        token, req_id, email = req.public_token, req.id, drb.email
        with app.app_context():
            login(client, email)
            client.post(f"/requests/{token}/approve", data={"remark": "ok"}, follow_redirects=True)
            assert _db.session.get(OnboardingRequest, req_id).status == RequestStatus.ACTIVE
            emp = EmployeeLocationSnapshot.query.filter_by(employee_code=f"NEWHIRE-{req_id}").first()
            assert emp is not None and emp.company == "ROBO" and emp.plant_location_key == "Robo Route Plant"


class TestNightlySchedule:
    def test_before_the_slot_runs_tonight(self):
        # 15:30 IST on 8 Oct -> 02:00 IST on 9 Oct == 20:30 UTC on 8 Oct
        assert snapshot_refresh._next_sync_utc(datetime(2026, 10, 8, 10, 0)) == datetime(2026, 10, 8, 20, 30)

    def test_just_before_the_slot_is_same_night(self):
        assert snapshot_refresh._next_sync_utc(datetime(2026, 10, 8, 20, 0)) == datetime(2026, 10, 8, 20, 30)

    def test_after_the_slot_waits_for_tomorrow(self):
        assert snapshot_refresh._next_sync_utc(datetime(2026, 10, 8, 21, 0)) == datetime(2026, 10, 9, 20, 30)

    def test_old_snapshot_is_overdue_fresh_one_is_not(self, db, app):
        assert snapshot_refresh._is_overdue() is True           # empty DB
        db.session.add(StaffingSnapshot(scope=NormScope.PLANT, location_key="X", norm_role_category_id=_cat(db),
                                        computed_at=datetime.utcnow()))
        db.session.commit()
        assert snapshot_refresh._is_overdue() is False


def _cat(db):
    c = NormRoleCategory(name="C" + uuid.uuid4().hex[:4], scope=NormScope.PLANT, sheet=NormSheet.SHEET1)
    db.session.add(c)
    db.session.flush()
    return c.id
