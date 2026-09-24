"""
Tests for the company-scoped approval routing feature (2026-09-21):

  - UserCompanyScope tick marks on Initiator/Business Head/HR Manager,
    fail-closed by design (zero ticks = zero access), unlike the existing
    fail-open region tables.
  - Ultrafine/ROBO's own fixed approval chain:
    Initiator -> Business Head -> HR Manager -> Head HR -> Dr. Bhoon -> Active
    (never branches by is_special_case — there's no staffing gate for these
    companies to set it).
  - RDC's existing standard/special-case paths stay unaffected.
  - Admin user form makes at least one company tick mandatory for the 3
    scoped roles.
"""
import uuid
from unittest.mock import patch
import pytest
from app.models import (
    UserRole, RequestStatus, OnboardingRequest, UserCompanyScope,
)
from app.extensions import db as _db
from app.utils import company_scope_ids, hr_manager_ids_for_company, bh_ids_for_initiator, can_act_on
from .conftest import login, logout, _make_user


def _create_request(db, user, status=RequestStatus.DRAFT, candidate_name="Test Candidate",
                     company_code="RDC", is_special_case=False):
    req = OnboardingRequest(
        initiated_by=user.id, status=status, public_token=uuid.uuid4().hex,
        candidate_name=candidate_name, company_code=company_code,
        plant_location="Plant A", designation="Engineer", is_special_case=is_special_case,
    )
    db.session.add(req)
    db.session.flush()
    req.form_data = {"company_code": company_code, "associate_name": candidate_name,
                      "plant_location": "Plant A", "designation": "Engineer",
                      "uan_number": "AB1234567890"}  # avoids the unrelated UAN-required submit check
    db.session.flush()
    return req


class TestCompanyScopeFailClosed:
    def test_unscoped_business_head_cannot_act(self, client, db, app):
        with app.app_context():
            initiator = _make_user("FcInit1", "fcinit1@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            bh = _make_user("FcBh1", "fcbh1@t.com", UserRole.BUSINESS_HEAD, db)  # no companies ticked
            req = _create_request(db, initiator, RequestStatus.PENDING_BH)
            db.session.commit()
            assert can_act_on(req, bh) is False

    def test_unscoped_hr_manager_cannot_act(self, client, db, app):
        with app.app_context():
            initiator = _make_user("FcInit2", "fcinit2@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            hrm = _make_user("FcHrm1", "fchrm1@t.com", UserRole.HR_MANAGER, db)  # no companies ticked
            req = _create_request(db, initiator, RequestStatus.PENDING_HR_MANAGER)
            db.session.commit()
            assert can_act_on(req, hrm) is False

    def test_unscoped_initiator_cannot_submit(self, client, db, app):
        initiator = _make_user("FcInit3", "fcinit3@t.com", UserRole.INITIATOR, db)  # no companies ticked
        req = _create_request(db, initiator, RequestStatus.DRAFT)
        db.session.commit()
        with app.app_context():
            login(client, initiator.email)
            resp = client.post(f"/requests/{req.public_token}/submit", follow_redirects=True)
        assert resp.status_code == 200
        assert b"not authorized" in resp.data.lower()
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req.id)
            assert updated.status == RequestStatus.DRAFT

    def test_company_scope_ids_empty_for_unticked_user(self, client, db, app):
        with app.app_context():
            u = _make_user("FcU1", "fcu1@t.com", UserRole.INITIATOR, db)
            assert company_scope_ids(u.id) == set()

    def test_hr_manager_ids_for_company_empty_when_nobody_ticked(self, client, db, app):
        with app.app_context():
            assert hr_manager_ids_for_company("ROBO") == set()


class TestUltrafineRoboFullChain:
    """End-to-end: Initiator -> BH -> HR Manager -> Head HR -> Dr. Bhoon -> ACTIVE,
    for both Ultrafine and ROBO, remark min-length stays 5 chars throughout
    (never the RDC-special-case 20-char rule, since is_special_case is never
    true for non-RDC)."""

    @pytest.mark.parametrize("company", ["Ultrafine", "ROBO"])
    def test_full_chain_reaches_active(self, client, db, app, company):
        # Login normalizes the submitted login_id to lowercase before the DB
        # lookup (app/auth/routes.py), but User.email is stored verbatim —
        # embed a lowercased tag in the email so "Ultrafine" doesn't produce
        # a mixed-case stored address the lowercased login can never match.
        tag = company.lower()
        initiator = _make_user(f"FullInit_{company}", f"fullinit_{tag}@t.com", UserRole.INITIATOR, db,
                                companies=[company])
        bh = _make_user(f"FullBh_{company}", f"fullbh_{tag}@t.com", UserRole.BUSINESS_HEAD, db,
                         companies=[company])
        hrm = _make_user(f"FullHrm_{company}", f"fullhrm_{tag}@t.com", UserRole.HR_MANAGER, db,
                          companies=[company])
        hhr = _make_user(f"FullHhr_{company}", f"fullhhr_{tag}@t.com", UserRole.HEAD_HR, db)
        drb = _make_user(f"FullDrb_{company}", f"fulldrb_{tag}@t.com", UserRole.DR_BHOON, db)
        # Capture emails as plain strings now — each approve step below pops
        # its own app_context, and Flask-SQLAlchemy's teardown_appcontext
        # calls db.session.remove(), detaching any ORM object reference held
        # across that boundary (DetachedInstanceError on later attribute
        # access). Plain strings have no such lifecycle.
        bh_email, hrm_email, hhr_email, drb_email = bh.email, hrm.email, hhr.email, drb.email
        req = _create_request(db, initiator, RequestStatus.PENDING_BH, company_code=company)
        db.session.commit()

        with app.app_context():
            login(client, bh_email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                                data={"remark": "ok bh"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            assert _db.session.get(OnboardingRequest, req.id).status == RequestStatus.PENDING_HR_MANAGER

        with app.app_context():
            # /auth/login redirects away without checking credentials when
            # already authenticated (app/auth/routes.py) — must log out the
            # previous approver first, or this silently keeps acting as bh.
            logout(client)
            login(client, hrm_email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                                data={"remark": "ok hr"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            assert _db.session.get(OnboardingRequest, req.id).status == RequestStatus.PENDING_HEAD_HR

        with app.app_context():
            logout(client)
            login(client, hhr_email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                                data={"remark": "ok hhr"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            # Fixed chain — always visits Dr. Bhoon next, never straight to ACTIVE.
            assert _db.session.get(OnboardingRequest, req.id).status == RequestStatus.PENDING_DR_BHOON

        with app.app_context():
            logout(client)
            login(client, drb_email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                                data={"remark": "ok drb"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            assert _db.session.get(OnboardingRequest, req.id).status == RequestStatus.ACTIVE

    def test_any_company_ticked_bh_can_approve_any_initiator_no_region_concept(self, client, db, app):
        """Confirms the stakeholder's explicit statement: a Robo-ticked BH can
        approve ANY Robo request, with no region narrowing at all (unlike RDC)."""
        initiator = _make_user("AnyBhInit", "anybhinit@t.com", UserRole.INITIATOR, db, companies=["ROBO"])
        bh = _make_user("AnyBh", "anybh@t.com", UserRole.BUSINESS_HEAD, db, companies=["ROBO"])
        req = _create_request(db, initiator, RequestStatus.PENDING_BH, company_code="ROBO")
        db.session.commit()
        with app.app_context():
            # No InitiatorRegion/BusinessHeadRegion rows exist for either user at all.
            assert can_act_on(req, bh) is True

    @pytest.mark.parametrize("company", ["Ultrafine", "ROBO"])
    def test_activation_pushes_to_truein_with_rdc_concrete_site(self, client, db, app, company):
        """
        Revised 2026-09-23: the 2026-09-22 fix wrongly assumed Truein
        doesn't track Ultrafine/ROBO at all (based on the 2026-09-15
        finding that this account only has two *site_name* values). Live
        data later showed real Ultrafine/ROBO employees ARE registered in
        Truein — filed under the "RDC Concrete" site (the only one that
        exists) with their real plant name as the distinguishing signal —
        so a hire reaching ACTIVE should push there too, the same way.
        push_employee() is mocked here so this test never makes a live
        network call, matching the "never browser-test an approval through
        to ACTIVE without warning" standing rule for this integration.
        """
        tag = company.lower()
        drb = _make_user(f"PushDrb_{tag}", f"pushdrb_{tag}@t.com", UserRole.DR_BHOON, db)
        initiator = _make_user(f"PushInit_{tag}", f"pushinit_{tag}@t.com", UserRole.INITIATOR, db,
                                companies=[company])
        req = _create_request(db, initiator, RequestStatus.PENDING_DR_BHOON, company_code=company)
        db.session.commit()
        drb_email = drb.email

        fake_result = {
            "success": True, "empId": "NEWJOINEE0101990001", "message": "Success",
            "http_status": 200, "raw_response": {}, "payload_sent": {}, "dropped_fields": [],
        }
        with patch("app.integrations.truein.push_employee", return_value=fake_result):
            with app.app_context():
                login(client, drb_email)
                resp = client.post(f"/requests/{req.public_token}/approve",
                                    data={"remark": "ok drb"}, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            from app.models import TrueinPushLog
            updated = _db.session.get(OnboardingRequest, req.id)
            assert updated.status == RequestStatus.ACTIVE
            assert updated.truein_pushed_at is not None
            assert TrueinPushLog.query.filter_by(request_id=req.id).count() == 1

    def test_build_payload_uses_rdc_concrete_site_for_ultrafine_plant(self, db, app):
        """build_payload() itself must never need a company check — siteName
        is already unconditional, sitePoint/sub_site already fall back to
        the raw plant name (no PlantDvtMapping exists for Ultrafine/ROBO)."""
        from app.integrations.truein import build_payload
        with app.app_context():
            req = OnboardingRequest(
                initiated_by=1, public_token="tok-uf-payload",
                candidate_name="UF Payload Candidate", designation="Electrician",
                plant_location="ROBO - Mumbai", company_code="ROBO",
            )
            db.session.add(req)
            db.session.flush()
            payload = build_payload(req)
            assert payload["siteName"] == "RDC Concrete"
            assert payload["sitePoint"] == "ROBO - Mumbai"
            assert payload["sub_site"] == "ROBO - Mumbai"


class TestRdcUnaffected:
    """RDC's standard and special-case paths must stay byte-for-byte
    identical to pre-feature behavior once users are backfilled to RDC."""

    def test_standard_path_bh_approve_goes_to_hr_manager(self, client, db, app):
        initiator = _make_user("RdcStdInit", "rdcstdinit@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        bh = _make_user("RdcStdBh", "rdcstdbh@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        req = _create_request(db, initiator, RequestStatus.PENDING_BH, company_code="RDC", is_special_case=False)
        db.session.commit()
        with app.app_context():
            login(client, bh.email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                                data={"remark": "std ok"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            assert _db.session.get(OnboardingRequest, req.id).status == RequestStatus.PENDING_HR_MANAGER

    def test_special_case_head_hr_approve_still_skips_to_dr_bhoon_not_active(self, client, db, app):
        initiator = _make_user("RdcOnInit", "rdconinit@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        hhr = _make_user("RdcOnHhr", "rdconhhr@t.com", UserRole.HEAD_HR, db)
        req = _create_request(db, initiator, RequestStatus.PENDING_HEAD_HR, company_code="RDC", is_special_case=True)
        db.session.commit()
        with app.app_context():
            login(client, hhr.email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                                data={"remark": "a" * 25}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            assert _db.session.get(OnboardingRequest, req.id).status == RequestStatus.PENDING_DR_BHOON

    def test_special_case_still_requires_20_char_remark(self, client, db, app):
        initiator = _make_user("RdcRemInit", "rdcreminit@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        bh = _make_user("RdcRemBh", "rdcrembh@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        req = _create_request(db, initiator, RequestStatus.PENDING_BH, company_code="RDC", is_special_case=True)
        db.session.commit()
        with app.app_context():
            login(client, bh.email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                                data={"remark": "too short"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            # Rejected by the 20-char justification rule — still PENDING_BH.
            assert _db.session.get(OnboardingRequest, req.id).status == RequestStatus.PENDING_BH


class TestNoEligibleApproverGuard:
    def test_submit_blocked_when_no_business_head_for_company(self, client, db, app):
        initiator = _make_user("NoBhInit", "nobhinit@t.com", UserRole.INITIATOR, db, companies=["ROBO"])
        req = _create_request(db, initiator, RequestStatus.DRAFT, company_code="ROBO")
        db.session.commit()
        with app.app_context():
            login(client, initiator.email)
            resp = client.post(f"/requests/{req.public_token}/submit", follow_redirects=True)
        assert resp.status_code == 200
        assert b"no business head" in resp.data.lower()
        with app.app_context():
            assert _db.session.get(OnboardingRequest, req.id).status == RequestStatus.DRAFT

    def test_submit_blocked_when_no_hr_manager_for_company(self, client, db, app):
        initiator = _make_user("NoHrmInit", "nohrminit@t.com", UserRole.INITIATOR, db, companies=["ROBO"])
        bh = _make_user("NoHrmBh", "nohrmbh@t.com", UserRole.BUSINESS_HEAD, db, companies=["ROBO"])
        req = _create_request(db, initiator, RequestStatus.DRAFT, company_code="ROBO")
        db.session.commit()
        with app.app_context():
            login(client, initiator.email)
            resp = client.post(f"/requests/{req.public_token}/submit", follow_redirects=True)
        assert resp.status_code == 200
        assert b"no hr manager" in resp.data.lower()
        with app.app_context():
            assert _db.session.get(OnboardingRequest, req.id).status == RequestStatus.DRAFT

    def test_rdc_special_case_not_blocked_by_missing_hr_manager(self, client, db, app):
        """An RDC special-case request skips PENDING_HR_MANAGER entirely, so
        the guard must not require an HR Manager to exist for it. Calls
        _validate_approver_availability() directly rather than through the
        resubmit HTTP route — going through the route would also re-run the
        live RDC staffing gate, which (correctly, but as an unrelated side
        effect) resets is_special_case back to False for an unmapped test
        plant/designation, defeating the point of this test."""
        from app.requests_bp.routes import _validate_approver_availability
        with app.app_context():
            initiator = _make_user("SkipHrmInit", "skiphrminit@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            bh = _make_user("SkipHrmBh", "skiphrmbh@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
            req = _create_request(db, initiator, RequestStatus.REJECTED_BH, company_code="RDC", is_special_case=True)
            db.session.commit()
            # No HR Manager exists at all — must still be None (not blocked).
            assert _validate_approver_availability(req) is None

    def test_rdc_standard_path_blocked_by_missing_hr_manager(self, client, db, app):
        """The same guard, but is_special_case=False — a standard RDC
        request DOES need an eligible HR Manager, so this must block."""
        from app.requests_bp.routes import _validate_approver_availability
        with app.app_context():
            initiator = _make_user("NeedHrmInit", "needhrminit@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            bh = _make_user("NeedHrmBh", "needhrmbh@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
            req = _create_request(db, initiator, RequestStatus.REJECTED_BH, company_code="RDC", is_special_case=False)
            db.session.commit()
            err = _validate_approver_availability(req)
            assert err is not None
            assert "hr manager" in err.lower()


class TestDashboardCompanyScoping:
    def test_robo_only_bh_never_sees_rdc_request(self, client, db, app):
        rdc_initiator = _make_user("DashRdcInit", "dashrdcinit@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        robo_bh = _make_user("DashRoboBh", "dashrobobh@t.com", UserRole.BUSINESS_HEAD, db, companies=["ROBO"])
        _create_request(db, rdc_initiator, RequestStatus.PENDING_BH, company_code="RDC",
                         candidate_name="RDC Only Candidate")
        db.session.commit()
        with app.app_context():
            login(client, robo_bh.email)
            resp = client.get("/dashboard")
        assert "RDC Only Candidate" not in resp.data.decode()

    def test_robo_only_bh_sees_robo_request(self, client, db, app):
        robo_initiator = _make_user("DashRoboInit", "dashroboinit@t.com", UserRole.INITIATOR, db, companies=["ROBO"])
        robo_bh = _make_user("DashRoboBh2", "dashrobobh2@t.com", UserRole.BUSINESS_HEAD, db, companies=["ROBO"])
        _create_request(db, robo_initiator, RequestStatus.PENDING_BH, company_code="ROBO",
                         candidate_name="Robo Visible Candidate")
        db.session.commit()
        with app.app_context():
            login(client, robo_bh.email)
            resp = client.get("/dashboard")
        assert "Robo Visible Candidate" in resp.data.decode()

    def test_unscoped_hr_manager_sees_nothing(self, client, db, app):
        initiator = _make_user("DashUnInit", "dashuninit@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        hrm = _make_user("DashUnHrm", "dashunhrm@t.com", UserRole.HR_MANAGER, db)  # no companies
        _create_request(db, initiator, RequestStatus.PENDING_HR_MANAGER, company_code="RDC",
                         candidate_name="Should Not Be Visible")
        db.session.commit()
        with app.app_context():
            login(client, hrm.email)
            resp = client.get("/dashboard")
        assert "Should Not Be Visible" not in resp.data.decode()


class TestAdminCompanyScopeUI:
    def test_creating_initiator_without_company_tick_is_rejected(self, client, db, app):
        admin = _make_user("AdmNoTick", "admnotick@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.post("/admin/users/new", data={
                "name": "No Tick User", "email": "notickuser@t.com",
                "password": "Secure99", "role": UserRole.INITIATOR.value,
                "employee_code": "EMP-NT1",
            }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"company scope" in resp.data.lower()
        with app.app_context():
            from app.models import User
            assert User.query.filter_by(email="notickuser@t.com").first() is None

    def test_creating_business_head_with_company_tick_persists_scope(self, client, db, app):
        admin = _make_user("AdmTick", "admtick@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.post("/admin/users/new", data={
                "name": "Ticked BH", "email": "tickedbh@t.com",
                "password": "Secure99", "role": UserRole.BUSINESS_HEAD.value,
                "employee_code": "EMP-BH1",
                "companies": ["ROBO", "Ultrafine"],
            }, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            from app.models import User
            u = User.query.filter_by(email="tickedbh@t.com").first()
            assert u is not None
            scopes = {r.company for r in UserCompanyScope.query.filter_by(user_id=u.id)}
            assert scopes == {"ROBO", "Ultrafine"}

    def test_unticking_rdc_clears_region_rows(self, client, db, app):
        from app.models import ClusterNameMapping, BusinessHeadRegion
        admin = _make_user("AdmUntick", "admuntick@t.com", UserRole.SUPER_ADMIN, db)
        bh = _make_user("UntickBh", "untickbh@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        region = ClusterNameMapping(canonical_cluster_name="Untick Region")
        db.session.add(region)
        db.session.flush()
        db.session.add(BusinessHeadRegion(business_head_id=bh.id, cluster_id=region.id))
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.post(f"/admin/users/{bh.id}/edit", data={
                "name": bh.name, "email": bh.email, "role": UserRole.BUSINESS_HEAD.value,
                "companies": ["ROBO"],  # RDC unticked
            }, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            assert BusinessHeadRegion.query.filter_by(business_head_id=bh.id).count() == 0
            scopes = {r.company for r in UserCompanyScope.query.filter_by(user_id=bh.id)}
            assert scopes == {"ROBO"}
