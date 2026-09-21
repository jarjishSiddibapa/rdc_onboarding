"""
Integration tests for the full onboarding request workflow.

Covers: DRAFT → PENDING_BH → PENDING_HR_MANAGER → PENDING_HEAD_HR → ACTIVE
        DRAFT → PENDING_BH → PENDING_DR_BHOON → ACTIVE (Dr. Bhoon path)
        Resubmit cycle after rejection
"""
import uuid
import pytest
from app.models import (
    UserRole, RequestStatus, OnboardingRequest, ApprovalAction, ApprovalActionType,
    FormField, FieldType, PlantLocation,
)
from app.extensions import db as _db
from .conftest import login, _make_user


# ── Helpers ────────────────────────────────────────────────────────────────────

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
    # Real requests always have form_data['company_code'] in sync with the
    # denormalized company_code column (_sync_quick_access() keeps them
    # aligned during the multi-step form save) — submit_request()/
    # resubmit_request() read company_code from form_data specifically, so
    # this fixture must too, or their company-scope/approver-availability
    # guards see an empty string instead of the real value.
    req.form_data = {"company_code": company_code, "associate_name": candidate_name,
                      "plant_location": "Plant A", "designation": "Engineer"}
    db.session.flush()
    return req


def _add_action(db, req, actor, action_type, remark="Test remark for approval"):
    action = ApprovalAction(
        request_id=req.id,
        actor_id=actor.id,
        action=action_type,
        remark=remark,
    )
    db.session.add(action)
    db.session.flush()
    return action


# ── State transitions via HTTP routes ─────────────────────────────────────────

class TestNormalApprovalPath:
    """DRAFT → PENDING_BH → PENDING_HR_MANAGER → PENDING_HEAD_HR → ACTIVE."""

    def test_bh_approve_moves_to_hrm(self, client, db, app):
        initiator = _make_user("Init", "inita@t.com", UserRole.INITIATOR, db)
        bh        = _make_user("BH",   "bha@t.com",   UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        req       = _create_request(db, initiator, RequestStatus.PENDING_BH)
        db.session.commit()
        with app.app_context():
            login(client, bh.email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                               data={"remark": "Looks good"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req.id)
            assert updated.status == RequestStatus.PENDING_HR_MANAGER

    def test_hrm_approve_moves_to_head_hr(self, client, db, app):
        initiator = _make_user("Init2", "init2a@t.com", UserRole.INITIATOR, db)
        hrm       = _make_user("HRM",   "hrma@t.com",   UserRole.HR_MANAGER, db, companies=["RDC"])
        req       = _create_request(db, initiator, RequestStatus.PENDING_HR_MANAGER)
        db.session.commit()
        with app.app_context():
            login(client, hrm.email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                               data={"remark": "HR approved"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req.id)
            assert updated.status == RequestStatus.PENDING_HEAD_HR

    def test_head_hr_approve_moves_to_active(self, client, db, app):
        initiator = _make_user("Init3", "init3a@t.com", UserRole.INITIATOR, db)
        hhr       = _make_user("HHR",   "hhra@t.com",   UserRole.HEAD_HR, db)
        req       = _create_request(db, initiator, RequestStatus.PENDING_HEAD_HR)
        db.session.commit()
        with app.app_context():
            login(client, hhr.email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                               data={"remark": "Final approval"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req.id)
            assert updated.status == RequestStatus.ACTIVE


class TestFormMobileFieldValidation:
    """
    Regression coverage for the 2026-09-10 fix: the onboarding form's mobile
    field only enforced "10 digits" client-side, not "starts with 6-9" —
    the exact rule Truein actually needs (see truein._clean_mobile()). An
    initiator could see a green "Looks good" on a number like "1234567890"
    and only find out much later, at approval/push time, that it was
    invalid all along. The HTML pattern attribute must match the backend
    rule exactly so the initiator finds out immediately, while filling the
    form — not after the fact.
    """

    def test_mobile_field_pattern_matches_backend_validation_rule(self, client, db, app, initiator):
        db.session.add(FormField(
            field_key="mobile_number", field_label="Mobile Number",
            field_type=FieldType.TEL, step=1, is_required=True, is_active=True,
        ))
        db.session.commit()
        with app.app_context():
            login(client, initiator.email)
            resp = client.get("/requests/new", follow_redirects=True)
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'pattern="[6-9][0-9]{9}"' in html
        # The old, incomplete rule must not still be present anywhere.
        assert 'pattern="[0-9]{10}"' not in html


class TestGovtIdDuplicateCheck:
    """
    Regression coverage for the 2026-09-10 fix: a Govt ID (Aadhar) collision
    with an existing Truein employee (request #32 'Barkha Patil' — "Govt ID
    already exist. Match found with GULSHAN KUMAR...") was only discovered
    at final-approval push time, after the entire multi-step approval chain
    had already run. This live-checks the Aadhar field the moment the
    initiator fills it, mirroring the existing duplicate-email OTP check.
    """

    def test_no_duplicate_returns_ok(self, client, db, app, initiator):
        with app.app_context():
            login(client, initiator.email)
            resp = client.post("/requests/check-govt-id", data={"aadhar_no": "999988887777"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_duplicate_against_another_request_is_flagged(self, client, db, app, initiator):
        other_req = _create_request(db, initiator, RequestStatus.PENDING_BH, candidate_name="Other Candidate")
        other_req.form_data = {"aadhar_no": "123412341234"}
        db.session.commit()
        with app.app_context():
            login(client, initiator.email)
            resp = client.post("/requests/check-govt-id", data={"aadhar_no": "1234 1234 1234"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is False
        assert body["duplicate"] is True
        assert "already used" in body["error"]

    def test_incomplete_number_is_never_flagged(self, client, db, app, initiator):
        """Only a complete 12-digit number is checked — an in-progress typed
        number shouldn't trigger a false duplicate lookup."""
        with app.app_context():
            login(client, initiator.email)
            resp = client.post("/requests/check-govt-id", data={"aadhar_no": "12341234"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_aadhar_field_renders_with_duplicate_check_hook(self, client, db, app, initiator):
        db.session.add(FormField(
            field_key="aadhar_no", field_label="Aadhar Number",
            field_type=FieldType.TEXT, step=1, is_required=True, is_active=True,
        ))
        db.session.commit()
        with app.app_context():
            login(client, initiator.email)
            resp = client.get("/requests/new", follow_redirects=True)
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'onblur="checkGovtIdDuplicate()"' in html
        assert 'pattern="[0-9]{12}"' in html


class TestCompanyAwarePlantLocationsApi:
    """
    Coverage for the 2026-09-15 multi-company fix: /requests/api/plant-locations
    now accepts ?company= and branches between the RDC-specific DVT-matched
    list (_dvt_matched_plant_options(), unchanged) and a flat, non-DVT list
    scoped to a PlantLocation.company for Ultrafine/ROBO
    (_company_plant_options()) — plants from one company must never leak
    into another's dropdown.
    """

    def test_default_company_is_rdc(self, client, db, app, initiator):
        with app.app_context():
            login(client, initiator.email)
            resp = client.get("/requests/api/plant-locations")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_ultrafine_returns_only_ultrafine_plants_no_cluster(self, client, db, app, initiator):
        db.session.add_all([
            PlantLocation(name="UF Plant A", company="Ultrafine", is_active=True),
            PlantLocation(name="UF Plant B", company="Ultrafine", is_active=True),
            PlantLocation(name="RDC Only Plant", company="RDC", is_active=True),
        ])
        db.session.commit()
        with app.app_context():
            login(client, initiator.email)
            resp = client.get("/requests/api/plant-locations?company=Ultrafine")
        data = resp.get_json()["data"]
        values = {p["value"] for p in data}
        assert values == {"UF Plant A", "UF Plant B"}
        assert all(p["cluster"] is None for p in data)

    def test_robo_returns_only_robo_plants(self, client, db, app, initiator):
        db.session.add_all([
            PlantLocation(name="ROBO Plant One", company="ROBO", is_active=True),
            PlantLocation(name="UF Plant Not Robo", company="Ultrafine", is_active=True),
        ])
        db.session.commit()
        with app.app_context():
            login(client, initiator.email)
            resp = client.get("/requests/api/plant-locations?company=ROBO")
        values = {p["value"] for p in resp.get_json()["data"]}
        assert values == {"ROBO Plant One"}

    def test_inactive_company_plant_excluded(self, client, db, app, initiator):
        db.session.add(PlantLocation(name="Disabled UF Plant", company="Ultrafine", is_active=False))
        db.session.commit()
        with app.app_context():
            login(client, initiator.email)
            resp = client.get("/requests/api/plant-locations?company=Ultrafine")
        values = {p["value"] for p in resp.get_json()["data"]}
        assert "Disabled UF Plant" not in values

    def test_invalid_company_falls_back_to_rdc(self, client, db, app, initiator):
        with app.app_context():
            login(client, initiator.email)
            resp = client.get("/requests/api/plant-locations?company=NotAThing")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_company_code_dropdown_reacts_to_company_selection(self, client, db, app, initiator):
        """The form must fetch the company-scoped plant list on Company Code
        change, not just once on load — confirms the wiring is present."""
        with app.app_context():
            login(client, initiator.email)
            resp = client.get("/requests/new", follow_redirects=True)
        html = resp.get_data(as_text=True)
        assert "fetchPlantsForCompany" in html
        assert "'select[name=\"company_code\"]'" in html


class TestNonRdcCompanyBypassesCapacityGate:
    """
    Regression-lock for a finding made during the 2026-09-15 multi-company
    investigation: check_hiring_capacity() (the live pre-check while filling
    the form) already short-circuits to {"allowed": True} for any
    company_code other than "RDC" — confirmed by reading the code before
    building anything, not assumed. This test exists so that guard can never
    silently regress as this file gets touched for other multi-company work.
    """

    def test_ultrafine_always_allowed_regardless_of_designation_or_plant(self, client, db, app, initiator):
        with app.app_context():
            login(client, initiator.email)
            resp = client.get("/requests/api/check-hiring-capacity", query_string={
                "company_code": "Ultrafine",
                "designation": "Anything At All",
                "plant_location": "Some Nonexistent Plant",
            })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["allowed"] is True

    def test_robo_always_allowed(self, client, db, app, initiator):
        with app.app_context():
            login(client, initiator.email)
            resp = client.get("/requests/api/check-hiring-capacity", query_string={
                "company_code": "ROBO",
                "designation": "Anything",
                "plant_location": "Anywhere",
            })
        assert resp.get_json()["allowed"] is True


class TestTrueinPreflightRoute:
    """
    Coverage for the 2026-09-10 pre-flight-check feature: the final
    approver should see a warning BEFORE clicking Approve if the current
    data would produce a known Truein problem (only what's determinable
    locally — missing required fields, bad mobile format).
    """

    def test_not_applicable_when_this_approval_is_not_final(self, client, db, app):
        initiator = _make_user("InitPF1", "initpf1@t.com", UserRole.INITIATOR, db)
        bh        = _make_user("BHpf1",   "bhpf1@t.com",   UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        req       = _create_request(db, initiator, RequestStatus.PENDING_BH)
        db.session.commit()
        with app.app_context():
            login(client, bh.email)
            resp = client.get(f"/requests/{req.public_token}/truein-preflight")
        assert resp.status_code == 200
        assert resp.get_json()["applicable"] is False

    def test_flags_invalid_mobile_on_final_approval(self, client, db, app):
        initiator = _make_user("InitPF2", "initpf2@t.com", UserRole.INITIATOR, db)
        hhr       = _make_user("HHRpf2",  "hhrpf2@t.com",  UserRole.HEAD_HR, db)
        req       = _create_request(db, initiator, RequestStatus.PENDING_HEAD_HR)
        req.form_data = {"mobile_number": "1234567890"}  # 10 digits, but bad prefix
        db.session.commit()
        with app.app_context():
            login(client, hhr.email)
            resp = client.get(f"/requests/{req.public_token}/truein-preflight")
        data = resp.get_json()
        assert data["applicable"] is True
        assert any(i["field"] == "mobile" for i in data["issues"])

    def test_no_issues_for_clean_data(self, client, db, app):
        initiator = _make_user("InitPF3", "initpf3@t.com", UserRole.INITIATOR, db)
        hhr       = _make_user("HHRpf3",  "hhrpf3@t.com",  UserRole.HEAD_HR, db)
        req       = _create_request(db, initiator, RequestStatus.PENDING_HEAD_HR)
        req.form_data = {"mobile_number": "9619034651"}
        db.session.commit()
        with app.app_context():
            login(client, hhr.email)
            resp = client.get(f"/requests/{req.public_token}/truein-preflight")
        data = resp.get_json()
        assert data["applicable"] is True
        assert data["issues"] == []

    def test_forbidden_for_actor_who_cannot_act_on_request(self, client, db, app):
        initiator = _make_user("InitPF4", "initpf4@t.com", UserRole.INITIATOR, db)
        other_bh  = _make_user("BHpf4",   "bhpf4@t.com",   UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        req       = _create_request(db, initiator, RequestStatus.PENDING_HEAD_HR)
        db.session.commit()
        with app.app_context():
            login(client, other_bh.email)
            resp = client.get(f"/requests/{req.public_token}/truein-preflight")
        assert resp.status_code == 403


class TestTrueinPushFailureNotification:
    """
    Regression coverage for a real incident (2026-09-04): a Truein push
    failure was only ever shown as a flash message to whoever clicked
    Approve, with no durable trace anyone else would see — so a genuine
    rejection went unnoticed until the candidate reported it. Every full
    push failure must now generate a persistent in-app Notification for
    every active Super Admin / Head HR / HR Manager, not just a flash.

    In the test environment TRUEIN_SUBSCRIPTION_KEY is never set, so
    push_employee() always raises before contacting Truein — this
    naturally exercises the failure path without a real network call.
    """
    def test_activation_push_failure_notifies_admin_and_hr(self, client, db, app):
        from app.models import Notification
        initiator = _make_user("InitTPF", "inittpf@t.com", UserRole.INITIATOR, db)
        hhr       = _make_user("HHRTpf",  "hhrtpf@t.com",  UserRole.HEAD_HR, db)
        admin     = _make_user("AdminTpf", "admintpf@t.com", UserRole.SUPER_ADMIN, db)
        hrm       = _make_user("HRMTpf",  "hrmtpf@t.com",  UserRole.HR_MANAGER, db, companies=["RDC"])
        req       = _create_request(db, initiator, RequestStatus.PENDING_HEAD_HR,
                                     candidate_name="Push Failure Candidate")
        db.session.commit()
        req_id, admin_id, hhr_id, hrm_id = req.id, admin.id, hhr.id, hrm.id
        with app.app_context():
            login(client, hhr.email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                               data={"remark": "Final approval"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req_id)
            assert updated.status == RequestStatus.ACTIVE
            assert updated.truein_push_error  # a failure was recorded, not silently dropped
            notif_recipient_ids = {
                n.recipient_id for n in
                Notification.query.filter_by(request_id=req_id).all()
            }
            assert admin_id in notif_recipient_ids
            assert hhr_id in notif_recipient_ids
            assert hrm_id in notif_recipient_ids


class TestDrBhoonPath:
    """Dr. Bhoon approves a PENDING_DR_BHOON request (reached via the over-norm
    chain) → ACTIVE. The old BH-flags-directly-to-Dr.-Bhoon bypass route
    (flag-special) was removed — only the two documented flows remain:
    standard (BH -> HR Manager -> Head HR) and over-norm (BH -> Head HR ->
    Dr. Bhoon)."""

    def test_dr_bhoon_approve_makes_active(self, client, db, app):
        initiator = _make_user("Init5", "init5a@t.com", UserRole.INITIATOR, db)
        drb       = _make_user("DrB",   "drba@t.com",   UserRole.DR_BHOON, db)
        req       = _create_request(db, initiator, RequestStatus.PENDING_DR_BHOON)
        db.session.commit()
        with app.app_context():
            login(client, drb.email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                               data={"remark": "Approved by Dr. Bhoon"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req.id)
            assert updated.status == RequestStatus.ACTIVE


class TestOverNormApprovalPath:
    """is_special_case=True: DRAFT → PENDING_BH → PENDING_HEAD_HR → PENDING_DR_BHOON → ACTIVE (skips HR Manager)."""

    def test_bh_approve_special_case_skips_hr_manager(self, client, db, app):
        initiator = _make_user("InitON1", "initon1@t.com", UserRole.INITIATOR, db)
        bh        = _make_user("BHon1",   "bhon1@t.com",   UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        req       = _create_request(db, initiator, RequestStatus.PENDING_BH, is_special_case=True)
        db.session.commit()
        with app.app_context():
            login(client, bh.email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                               data={"remark": "Justified — production ramp-up needs extra headcount"},
                               follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req.id)
            assert updated.status == RequestStatus.PENDING_HEAD_HR

    def test_head_hr_approve_special_case_routes_to_dr_bhoon(self, client, db, app):
        initiator = _make_user("InitON2", "initon2@t.com", UserRole.INITIATOR, db)
        hhr       = _make_user("HHRon2",  "hhron2@t.com",  UserRole.HEAD_HR, db)
        req       = _create_request(db, initiator, RequestStatus.PENDING_HEAD_HR, is_special_case=True)
        db.session.commit()
        with app.app_context():
            login(client, hhr.email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                               data={"remark": "Justified — approved as an over-norm exception"},
                               follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req.id)
            assert updated.status == RequestStatus.PENDING_DR_BHOON

    def test_dr_bhoon_approve_special_case_activates(self, client, db, app):
        initiator = _make_user("InitON3", "initon3@t.com", UserRole.INITIATOR, db)
        drb       = _make_user("DrBon3",  "drbon3@t.com",  UserRole.DR_BHOON, db)
        req       = _create_request(db, initiator, RequestStatus.PENDING_DR_BHOON, is_special_case=True)
        db.session.commit()
        with app.app_context():
            login(client, drb.email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                               data={"remark": "Final over-norm approval — justified by Dr. Bhoon"},
                               follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req.id)
            assert updated.status == RequestStatus.ACTIVE

    def test_special_case_approval_requires_20_char_remark(self, client, db, app):
        initiator = _make_user("InitON4", "initon4@t.com", UserRole.INITIATOR, db)
        bh        = _make_user("BHon4",   "bhon4@t.com",   UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        req       = _create_request(db, initiator, RequestStatus.PENDING_BH, is_special_case=True)
        db.session.commit()
        with app.app_context():
            login(client, bh.email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                               data={"remark": "too short"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req.id)
            # 9-char remark is below the 20-char special-case minimum — status unchanged
            assert updated.status == RequestStatus.PENDING_BH

    def test_draft_special_case_shows_over_norm_map_before_submit(self, client, db, app):
        """A DRAFT that already has is_special_case=True (popup acknowledged,
        not yet submitted) must preview the over-norm stage labels, not the
        standard ones — regression test for a bug where the workflow map's
        path-detection only checked PENDING_BH/etc., never DRAFT, so it
        showed the wrong flow until the moment of Submit."""
        initiator = _make_user("InitON5", "initon5@t.com", UserRole.INITIATOR, db)
        req       = _create_request(db, initiator, RequestStatus.DRAFT, is_special_case=True)
        db.session.commit()
        with app.app_context():
            login(client, initiator.email)
            resp = client.get(f"/requests/{req.public_token}")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Over-Norm Approval" in body
        assert "HR Manager Review" not in body


class TestRejectionAndResubmit:
    """Rejection at BH level → initiator resubmits → back to PENDING_BH."""

    def test_bh_reject_moves_to_rejected_bh(self, client, db, app):
        initiator = _make_user("Init6", "init6a@t.com", UserRole.INITIATOR, db)
        bh        = _make_user("BH3",   "bh3a@t.com",   UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        req       = _create_request(db, initiator, RequestStatus.PENDING_BH)
        db.session.commit()
        with app.app_context():
            login(client, bh.email)
            resp = client.post(f"/requests/{req.public_token}/reject",
                               data={"remark": "Missing documents"}, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req.id)
            assert updated.status == RequestStatus.REJECTED_BH

    def test_initiator_resubmit_after_rejection(self, client, db, app):
        initiator = _make_user("Init7", "init7a@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        bh        = _make_user("BH7",   "bh7a@t.com",   UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        # _validate_approver_availability() (2026-09-21) requires at least
        # one eligible HR Manager too, since this standard-path RDC request
        # will eventually reach PENDING_HR_MANAGER.
        hrm       = _make_user("HRM7",  "hrm7a@t.com",  UserRole.HR_MANAGER, db, companies=["RDC"])
        req       = _create_request(db, initiator, RequestStatus.REJECTED_BH)
        db.session.commit()
        with app.app_context():
            login(client, initiator.email)
            resp = client.post(f"/requests/{req.public_token}/resubmit",
                               follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req.id)
            assert updated.status == RequestStatus.PENDING_BH
            assert updated.retry_count == 1

    def test_cannot_submit_non_draft(self, client, db, app):
        initiator = _make_user("Init8", "init8a@t.com", UserRole.INITIATOR, db)
        req       = _create_request(db, initiator, RequestStatus.PENDING_BH)
        db.session.commit()
        with app.app_context():
            login(client, initiator.email)
            resp = client.post(f"/requests/{req.public_token}/submit",
                               follow_redirects=True)
        # Should flash warning, not change status
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req.id)
            assert updated.status == RequestStatus.PENDING_BH

    def test_short_remark_rejected(self, client, db, app):
        initiator = _make_user("Init9", "init9a@t.com", UserRole.INITIATOR, db)
        bh        = _make_user("BH4",   "bh4a@t.com",   UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        req       = _create_request(db, initiator, RequestStatus.PENDING_BH)
        db.session.commit()
        with app.app_context():
            login(client, bh.email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                               data={"remark": "ok"}, follow_redirects=True)
        # Remark too short (< 5 chars) — status should NOT change
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req.id)
            assert updated.status == RequestStatus.PENDING_BH


class TestDeleteDraft:
    def test_initiator_can_delete_own_draft(self, client, db, app):
        initiator = _make_user("Init10", "init10a@t.com", UserRole.INITIATOR, db)
        req       = _create_request(db, initiator, RequestStatus.DRAFT)
        db.session.commit()
        req_id = req.id
        with app.app_context():
            login(client, initiator.email)
            resp = client.post(f"/requests/{req.public_token}/delete",
                               follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req_id)
            assert updated.is_deleted is True

    def test_cannot_delete_non_draft(self, client, db, app):
        initiator = _make_user("Init11", "init11a@t.com", UserRole.INITIATOR, db)
        req       = _create_request(db, initiator, RequestStatus.PENDING_BH)
        db.session.commit()
        with app.app_context():
            login(client, initiator.email)
            resp = client.post(f"/requests/{req.public_token}/delete",
                               follow_redirects=True)
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req.id)
            assert updated.is_deleted is False
