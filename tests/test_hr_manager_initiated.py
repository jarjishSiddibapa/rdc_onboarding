"""
Tests for HR Managers acting as initiators (2026-09-29).

An HR Manager can now submit onboarding requests the same way an Initiator
does (new_request/submit_request/resubmit_request/view_request all accept
UserRole.HR_MANAGER now). Since the HR Manager approval step exists purely
to have *an* HR Manager review the hire, and the initiator of the request
already IS one, PENDING_HR_MANAGER is skipped entirely for these requests
(OnboardingRequest.hr_manager_initiated, snapshotted at first submission —
see app/utils.py::get_new_status() and requests_bp._finalize_submission()).
Structurally this can never become a self-approval, since HR_MANAGER's
ROLE_QUEUES entry is PENDING_HR_MANAGER — a status this kind of request
never actually visits.
"""
import uuid
import pytest
from app.models import UserRole, RequestStatus, OnboardingRequest
from app.extensions import db as _db
from .conftest import login, logout, _make_user


def _create_request(db, user, status=RequestStatus.DRAFT, candidate_name="Test Candidate",
                     is_special_case=False, company_code="RDC"):
    req = OnboardingRequest(
        initiated_by=user.id,
        status=status,
        public_token=uuid.uuid4().hex,
        candidate_name=candidate_name,
        company_code=company_code,
        plant_location="Plant A",
        designation="Engineer",
        is_special_case=is_special_case,
    )
    db.session.add(req)
    db.session.flush()
    req.form_data = {"company_code": company_code, "associate_name": candidate_name,
                      "plant_location": "Plant A", "designation": "Engineer",
                      "uan_number": "UAN123456789"}
    if company_code != "RDC":   # submit now requires a non-RDC plant to be a real plant of that company
        from app.models import PlantLocation
        if not PlantLocation.query.filter_by(name="Plant A", company=company_code).first():
            db.session.add(PlantLocation(name="Plant A", company=company_code))
    db.session.flush()
    return req


class TestHrManagerCanInitiate:
    def test_hr_manager_can_reach_new_request(self, client, db, app):
        hrm = _make_user("HrmNew1", "hrmnew1@t.com", UserRole.HR_MANAGER, db, companies=["RDC"])
        with app.app_context():
            login(client, hrm.email)
            resp = client.get("/requests/new", follow_redirects=False)
        assert resp.status_code == 302
        assert "token=" in resp.headers["Location"]

    def test_plain_initiator_still_blocked_from_approver_only_routes(self, client, db, app):
        """Widening the decorator to HR_MANAGER must not also open it to
        every other approver role — BUSINESS_HEAD still gets 403."""
        bh = _make_user("BhNoInit1", "bhnoinit1@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        with app.app_context():
            login(client, bh.email)
            resp = client.get("/requests/new")
        assert resp.status_code == 403

    def test_submit_by_initiator_leaves_hr_manager_initiated_false(self, client, db, app):
        initiator = _make_user("PlainInit1", "plaininit1@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        _make_user("PlainBh1", "plainbh1@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        _make_user("PlainHrm1", "plainhrm1@t.com", UserRole.HR_MANAGER, db, companies=["RDC"])
        req = _create_request(db, initiator, RequestStatus.DRAFT)
        db.session.commit()
        token, req_id = req.public_token, req.id

        with app.app_context():
            login(client, initiator.email)
            resp = client.post(f"/requests/{token}/submit", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req_id)
            assert updated.status == RequestStatus.PENDING_BH
            assert updated.hr_manager_initiated is False

    def test_submit_by_hr_manager_sets_hr_manager_initiated_true(self, client, db, app):
        hrm = _make_user("SubHrm1", "subhrm1@t.com", UserRole.HR_MANAGER, db, companies=["RDC"])
        _make_user("SubBh1", "subbh1@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        # Deliberately no OTHER HR Manager ticked for RDC — only the
        # submitter themselves — to also prove the approver-availability
        # guard doesn't block this (see TestNoHrManagerNeeded below).
        req = _create_request(db, hrm, RequestStatus.DRAFT)
        db.session.commit()
        token, req_id = req.public_token, req.id

        with app.app_context():
            login(client, hrm.email)
            resp = client.post(f"/requests/{token}/submit", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req_id)
            assert updated.status == RequestStatus.PENDING_BH
            assert updated.hr_manager_initiated is True


class TestNoHrManagerNeeded:
    def test_hr_manager_initiated_submission_not_blocked_by_missing_hr_manager_approver(self, client, db, app):
        """_validate_approver_availability() would normally block a submit
        when zero HR Managers are ticked for the company — must NOT apply
        here, since this request never visits PENDING_HR_MANAGER at all."""
        hrm = _make_user("SoloHrm1", "solohrm1@t.com", UserRole.HR_MANAGER, db, companies=["RDC"])
        _make_user("SoloBh1", "solobh1@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        # No other HR Manager exists at all in this test's DB.
        req = _create_request(db, hrm, RequestStatus.DRAFT)
        db.session.commit()
        token, req_id = req.public_token, req.id

        with app.app_context():
            login(client, hrm.email)
            resp = client.post(f"/requests/{token}/submit", follow_redirects=True)
        assert resp.status_code == 200
        assert "no hr manager" not in resp.get_data(as_text=True).lower()
        with app.app_context():
            assert _db.session.get(OnboardingRequest, req_id).status == RequestStatus.PENDING_BH

    def test_still_blocked_when_no_business_head_ticked(self, client, db, app):
        """The Business Head guard is untouched — still required regardless
        of who initiated the request."""
        hrm = _make_user("NoBhHrm1", "nobhhrm1@t.com", UserRole.HR_MANAGER, db, companies=["RDC"])
        req = _create_request(db, hrm, RequestStatus.DRAFT)
        db.session.commit()
        token, req_id = req.public_token, req.id

        with app.app_context():
            login(client, hrm.email)
            resp = client.post(f"/requests/{token}/submit", follow_redirects=True)
        assert resp.status_code == 200
        assert "no business / functional head" in resp.get_data(as_text=True).lower()
        with app.app_context():
            assert _db.session.get(OnboardingRequest, req_id).status == RequestStatus.DRAFT


class TestHrManagerInitiatedFullChain:
    """End-to-end via the real /approve route, confirming PENDING_HR_MANAGER
    is skipped and every other stage behaves exactly as it would for a
    plain Initiator's request."""

    def test_rdc_standard_skips_hr_manager_no_dr_bhoon(self, client, db, app):
        hrm = _make_user("ChainHrm1", "chainhrm1@t.com", UserRole.HR_MANAGER, db, companies=["RDC"])
        bh = _make_user("ChainBh1", "chainbh1@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        hhr = _make_user("ChainHhr1", "chainhhr1@t.com", UserRole.HEAD_HR, db)
        bh_email, hhr_email = bh.email, hhr.email
        req = _create_request(db, hrm, RequestStatus.DRAFT)
        req.hr_manager_initiated = False  # confirm the real submit path sets it, not this fixture
        db.session.commit()
        token, req_id = req.public_token, req.id

        with app.app_context():
            login(client, hrm.email)
            resp = client.post(f"/requests/{token}/submit", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            assert _db.session.get(OnboardingRequest, req_id).hr_manager_initiated is True

        with app.app_context():
            logout(client)
            login(client, bh_email)
            resp = client.post(f"/requests/{token}/approve", data={"remark": "ok bh"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            # Skipped straight past PENDING_HR_MANAGER.
            assert _db.session.get(OnboardingRequest, req_id).status == RequestStatus.PENDING_HEAD_HR

        with app.app_context():
            logout(client)
            login(client, hhr_email)
            resp = client.post(f"/requests/{token}/approve", data={"remark": "ok hhr"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            # Standard (non-special-case) RDC never visits Dr. Bhoon either.
            assert _db.session.get(OnboardingRequest, req_id).status == RequestStatus.ACTIVE

    @pytest.mark.parametrize("company", ["Ultrafine", "ROBO"])
    def test_non_rdc_skips_hr_manager_but_still_visits_dr_bhoon(self, client, db, app, company):
        tag = company.lower()
        hrm = _make_user(f"ChainHrm_{tag}", f"chainhrm_{tag}@t.com", UserRole.HR_MANAGER, db, companies=[company])
        bh = _make_user(f"ChainBh_{tag}", f"chainbh_{tag}@t.com", UserRole.BUSINESS_HEAD, db, companies=[company])
        hhr = _make_user(f"ChainHhr_{tag}", f"chainhhr_{tag}@t.com", UserRole.HEAD_HR, db)
        drb = _make_user(f"ChainDrb_{tag}", f"chaindrb_{tag}@t.com", UserRole.DR_BHOON, db)
        bh_email, hhr_email, drb_email = bh.email, hhr.email, drb.email
        req = _create_request(db, hrm, RequestStatus.DRAFT, company_code=company)
        db.session.commit()
        token, req_id = req.public_token, req.id

        with app.app_context():
            login(client, hrm.email)
            client.post(f"/requests/{token}/submit", follow_redirects=True)

        with app.app_context():
            logout(client)
            login(client, bh_email)
            resp = client.post(f"/requests/{token}/approve", data={"remark": "ok bh"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            assert _db.session.get(OnboardingRequest, req_id).status == RequestStatus.PENDING_HEAD_HR

        with app.app_context():
            logout(client)
            login(client, hhr_email)
            resp = client.post(f"/requests/{token}/approve", data={"remark": "ok hhr"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            # Non-RDC's fixed chain still visits Dr. Bhoon regardless of the
            # HR Manager skip.
            assert _db.session.get(OnboardingRequest, req_id).status == RequestStatus.PENDING_DR_BHOON

        with app.app_context():
            logout(client)
            login(client, drb_email)
            resp = client.post(f"/requests/{token}/approve", data={"remark": "ok drb"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            assert _db.session.get(OnboardingRequest, req_id).status == RequestStatus.ACTIVE


class TestHrManagerOwnDraftsSurfaced:
    def test_own_draft_appears_on_dashboard(self, client, db, app):
        hrm = _make_user("DashHrm1", "dashhrm1@t.com", UserRole.HR_MANAGER, db, companies=["RDC"])
        req = _create_request(db, hrm, RequestStatus.DRAFT, candidate_name="Dashboard Draft Candidate")
        db.session.commit()

        with app.app_context():
            login(client, hrm.email)
            resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "Dashboard Draft Candidate" in resp.get_data(as_text=True)

    def test_no_draft_section_when_no_own_drafts(self, client, db, app):
        hrm = _make_user("DashHrm2", "dashhrm2@t.com", UserRole.HR_MANAGER, db, companies=["RDC"])
        with app.app_context():
            login(client, hrm.email)
            resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "My Draft Requests" not in resp.get_data(as_text=True)
