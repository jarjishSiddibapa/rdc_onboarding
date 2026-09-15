"""
Tests for app/integrations/truein.py::_parse_truein_response().

Regression coverage for a real production incident (2026-09-04, request
#27 "Lalu Yadav"): Truein rejected the push because the submitted email
collided with a DIFFERENT existing employee ("Match found with
Rutuja(R00284) in RDC Concrete"), but the old "already exist" heuristic
treated that as success anyway, so the failure was silently swallowed —
the employee never actually reached Truein and nobody was notified.
"""
import uuid
from unittest.mock import patch
from app.integrations.truein import _parse_truein_response, build_payload, push_employee, preflight_check
from app.models import (
    OnboardingRequest, Designation, NormRoleCategory, NormScope, NormSheet,
    PlantDvtMapping, ClusterNameMapping,
)


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.reason = "Error"
        self.text = str(body)

    def json(self):
        return self._body


class TestParseTrueinResponse:
    def test_genuine_success(self):
        resp = _FakeResponse(200, {"response": "Success!", "code": "200",
                                    "message": "Employee added", "data": [{"empId": "E1"}]})
        result = _parse_truein_response(resp, {"empId": "E1"})
        assert result["success"] is True

    def test_field_collision_with_different_employee_is_failure(self):
        """The exact real-world regression case — must NOT be treated as success."""
        resp = _FakeResponse(400, {
            "response": "Fail!", "code": "400",
            "message": "Email Id already exist. Match found with Rutuja(R00284) in RDC Concrete",
            "data": {"validation_errors": []},
        })
        result = _parse_truein_response(resp, {"empId": "NEWJOINEE1"})
        assert result["success"] is False

    def test_field_collision_with_different_employee_is_not_retryable(self):
        """
        Regression coverage for the 2026-09-10 fix (request #32 'Barkha
        Patil' — Govt ID collided with a different employee, then the
        background retry thread kept retrying an unwinnable push forever).
        A 'Match found with <other employee>' collision is a permanent data
        problem — resending the same payload will fail identically every
        time, so this must be flagged non-retryable.
        """
        resp = _FakeResponse(400, {
            "response": "Fail!", "code": "400",
            "message": "Govt ID already exist. Match found with GULSHAN KUMAR(NEWJOINEE040920260003) in RDC Concrete",
            "data": {},
        })
        result = _parse_truein_response(resp, {"empId": "NEWJOINEE2"})
        assert result["success"] is False
        assert result["retryable"] is False

    def test_generic_already_exist_without_match_found_is_still_success(self):
        """Preserve the original idempotent-retry intent: a plain 'already
        exist(ed)/requested' message with no named colliding employee is
        still treated as success (this push already went through)."""
        resp = _FakeResponse(400, {
            "response": "Fail!", "code": "400",
            "message": "Employee already requested",
            "data": {},
        })
        result = _parse_truein_response(resp, {"empId": "NEWJOINEE1"})
        assert result["success"] is True

    def test_generic_failure(self):
        resp = _FakeResponse(400, {"response": "Fail!", "code": "400",
                                    "message": "Invalid mobile number", "data": {}})
        result = _parse_truein_response(resp, {"empId": "E1"})
        assert result["success"] is False

    def test_generic_failure_is_retryable(self):
        """A transient/generic failure (no named collision) should still be
        retried by the background loop — only a genuine 'match found'
        collision stops retries."""
        resp = _FakeResponse(400, {"response": "Fail!", "code": "400",
                                    "message": "Invalid mobile number", "data": {}})
        result = _parse_truein_response(resp, {"empId": "E1"})
        assert result["retryable"] is True


class TestBuildPayloadFieldCompleteness:
    """
    Regression coverage for the 2026-09-10 fix: father_name, reporting
    manager, and UAN number were collected on the form but silently dropped
    (missing from FIELD_MAP); sub_site/category/department were never sent
    at all since the form doesn't collect them directly.
    """

    def test_previously_dropped_form_fields_now_reach_the_payload(self, db, app):
        with app.app_context():
            req = OnboardingRequest(
                initiated_by=1,
                public_token=uuid.uuid4().hex,
                candidate_name="Test Candidate",
                designation="Batching Plant Operator",
                plant_location="BG-Test Plant",
                company_code="RDC",
            )
            req.form_data = {
                "father_name": "Test Father",
                "reporting_manager_name": "Some Manager",
                "uan_number": "UAN12345",
                "plant_location": "BG-Test Plant",
            }
            db.session.add(req)
            db.session.flush()

            payload = build_payload(req)

            assert payload["father_name"] == "Test Father"
            assert payload["manager"] == "Some Manager"
            assert payload["uan_number"] == "UAN12345"

    def test_sub_site_and_category_derived_from_plant_mapping(self, db, app):
        with app.app_context():
            cluster = ClusterNameMapping(canonical_cluster_name="BG Region", truein_category="Bangalore")
            db.session.add(cluster)
            db.session.flush()
            plant_map = PlantDvtMapping(
                plant_location_name="BG-Test Plant 2",
                dvt_plant_code="BGX",
                truein_sub_site="BG-Veerasandra",
                cluster_id=cluster.id,
            )
            db.session.add(plant_map)
            db.session.flush()

            req = OnboardingRequest(
                initiated_by=1, public_token=uuid.uuid4().hex,
                candidate_name="Test Candidate 2", plant_location="BG-Test Plant 2", company_code="RDC",
            )
            req.form_data = {"plant_location": "BG-Test Plant 2"}
            db.session.add(req)
            db.session.flush()

            payload = build_payload(req)

            assert payload["sub_site"] == "BG-Veerasandra"
            assert payload["category"] == "Bangalore"

    def test_site_point_uses_reconciled_truein_value_not_raw_plant_name(self, db, app):
        """
        Regression coverage for the 2026-09-10 root-cause fix: every real
        test push before this fix had sitePoint dropped by Truein, because
        it was sent as our own raw plant_location string (e.g. "CHE-
        Trisulam") rather than the reconciled real Truein string already
        sitting in PlantDvtMapping.truein_sub_site (e.g. "CHE-Trisulam", no
        space) — the exact same value already used for the sub_site field.
        """
        with app.app_context():
            plant_map = PlantDvtMapping(
                plant_location_name="CHE- Trisulam", dvt_plant_code="C11",
                truein_sub_site="CHE-Trisulam",
            )
            db.session.add(plant_map)
            db.session.flush()

            req = OnboardingRequest(
                initiated_by=1, public_token=uuid.uuid4().hex,
                candidate_name="Test Candidate 2b", plant_location="CHE- Trisulam", company_code="RDC",
            )
            req.form_data = {"plant_location": "CHE- Trisulam"}
            db.session.add(req)
            db.session.flush()

            payload = build_payload(req)

            assert payload["sitePoint"] == "CHE-Trisulam"
            assert payload["sitePoint"] == payload["sub_site"]

    def test_sub_site_falls_back_to_plant_name_when_unreconciled(self, db, app):
        with app.app_context():
            plant_map = PlantDvtMapping(plant_location_name="BG-Test Plant 3", dvt_plant_code="BGY")
            db.session.add(plant_map)
            db.session.flush()

            req = OnboardingRequest(
                initiated_by=1, public_token=uuid.uuid4().hex,
                candidate_name="Test Candidate 3", plant_location="BG-Test Plant 3", company_code="RDC",
            )
            req.form_data = {"plant_location": "BG-Test Plant 3"}
            db.session.add(req)
            db.session.flush()

            payload = build_payload(req)

            assert payload["sub_site"] == "BG-Test Plant 3"
            assert "category" not in payload

    def test_sub_site_not_empty_when_plant_has_no_dvt_mapping_row_at_all(self, db, app):
        """
        Regression coverage for the 2026-09-15 multi-company fix. Ultrafine/
        ROBO plants live in the company-scoped PlantLocation table (added
        for multi-company support) and NEVER get a PlantDvtMapping row —
        that table is RDC/DVT-specific. Before this fix, the `if plant_map:`
        branch was the ONLY place sub_site got set, so with no
        PlantDvtMapping row at all (not even an empty one), sub_site was
        missing from the payload entirely — every non-RDC push silently
        sent no plant-level location. Confirms sitePoint and sub_site both
        still default to the raw plant name with zero PlantDvtMapping rows
        in the DB whatsoever.
        """
        with app.app_context():
            req = OnboardingRequest(
                initiated_by=1, public_token=uuid.uuid4().hex,
                candidate_name="Test Candidate Ultrafine", plant_location="UF-Test Plant",
                company_code="Ultrafine",
            )
            req.form_data = {"plant_location": "UF-Test Plant"}
            db.session.add(req)
            db.session.flush()

            payload = build_payload(req)

            assert payload["sitePoint"] == "UF-Test Plant"
            assert payload["sub_site"] == "UF-Test Plant"
            assert "category" not in payload

    def test_department_derived_from_designation_norm_category(self, db, app):
        with app.app_context():
            cat = NormRoleCategory(name="Technical", scope=NormScope.PLANT, sheet=NormSheet.SHEET1)
            db.session.add(cat)
            db.session.flush()
            desig = Designation(name="Test Technical Role", norm_category_id=cat.id)
            db.session.add(desig)
            db.session.flush()

            req = OnboardingRequest(
                initiated_by=1, public_token=uuid.uuid4().hex,
                candidate_name="Test Candidate 4", designation="Test Technical Role", company_code="RDC",
            )
            req.form_data = {}
            db.session.add(req)
            db.session.flush()

            payload = build_payload(req)

            assert payload["department"] == "Technical"


class TestMobilePreValidation:
    """
    Regression coverage for the 2026-09-10 fix: a 10-digit number starting
    with 1-5 (e.g. placeholder test data "1234567890") passed the old
    "10 digits, no leading zero" check, got sent to Truein, was rejected,
    and then got silently mislabeled as dropped-by-Truein — when it was
    actually invalid by our own rules from the start. Also: the old
    pre-check popped an invalid mobile with NO record in dropped_fields at
    all, so it never reached the notification email or the on-screen popup.
    """

    def _req(self, db, mobile):
        req = OnboardingRequest(
            initiated_by=1, public_token=uuid.uuid4().hex,
            candidate_name="Mobile Test Candidate", company_code="RDC",
        )
        req.form_data = {"mobile_number": mobile}
        db.session.add(req)
        db.session.flush()
        return req

    def test_number_starting_with_invalid_digit_is_dropped_and_recorded(self, db, app):
        with app.app_context():
            req = self._req(db, "1234567890")
            with patch("app.integrations.truein.SUBSCRIPTION_KEY", "fake-key"), \
                 patch("app.integrations.truein._do_push") as mock_push:
                mock_push.return_value = {
                    "success": True, "empId": "E1", "message": "ok",
                    "http_status": 200, "raw_response": {}, "payload_sent": {},
                }
                result = push_employee(req)

            assert result["success"] is True
            assert "mobile" in result["dropped_fields"]
            sent_payload = mock_push.call_args[0][0]
            assert "mobile" not in sent_payload

    def test_valid_indian_mobile_number_is_sent(self, db, app):
        with app.app_context():
            req = self._req(db, "9619034651")
            with patch("app.integrations.truein.SUBSCRIPTION_KEY", "fake-key"), \
                 patch("app.integrations.truein._do_push") as mock_push:
                mock_push.return_value = {
                    "success": True, "empId": "E1", "message": "ok",
                    "http_status": 200, "raw_response": {}, "payload_sent": {},
                }
                result = push_employee(req)

            assert result["dropped_fields"] == []
            sent_payload = mock_push.call_args[0][0]
            assert sent_payload["mobile"] == "9619034651"

    def test_country_code_prefixed_number_is_stripped_and_sent(self, db, app):
        with app.app_context():
            req = self._req(db, "919619034651")
            with patch("app.integrations.truein.SUBSCRIPTION_KEY", "fake-key"), \
                 patch("app.integrations.truein._do_push") as mock_push:
                mock_push.return_value = {
                    "success": True, "empId": "E1", "message": "ok",
                    "http_status": 200, "raw_response": {}, "payload_sent": {},
                }
                result = push_employee(req)

            assert result["dropped_fields"] == []
            sent_payload = mock_push.call_args[0][0]
            assert sent_payload["mobile"] == "9619034651"


class TestPreflightCheck:
    """
    Coverage for preflight_check() — the pure, no-network validation that
    powers the "warn the approver before they click Approve" popup. Must
    agree exactly with what push_employee() would do, since both now share
    _clean_mobile() as their single source of truth.
    """

    def test_no_issues_for_valid_mobile(self, db, app):
        with app.app_context():
            req = OnboardingRequest(
                initiated_by=1, public_token=uuid.uuid4().hex,
                candidate_name="Preflight OK", company_code="RDC",
            )
            req.form_data = {"mobile_number": "9619034651"}
            db.session.add(req)
            db.session.flush()

            result = preflight_check(req)

            assert result["issues"] == []

    def test_flags_invalid_mobile(self, db, app):
        with app.app_context():
            req = OnboardingRequest(
                initiated_by=1, public_token=uuid.uuid4().hex,
                candidate_name="Preflight Bad Mobile", company_code="RDC",
            )
            req.form_data = {"mobile_number": "1234567890"}
            db.session.add(req)
            db.session.flush()

            result = preflight_check(req)

            fields = [i["field"] for i in result["issues"]]
            assert "mobile" in fields

    def test_flags_missing_name(self, db, app):
        with app.app_context():
            req = OnboardingRequest(
                initiated_by=1, public_token=uuid.uuid4().hex,
                candidate_name=None, company_code="RDC",
            )
            req.form_data = {}
            db.session.add(req)
            db.session.flush()

            result = preflight_check(req)

            fields = [i["field"] for i in result["issues"]]
            assert "name" in fields

    def test_no_issue_raised_when_mobile_absent_entirely(self, db, app):
        """Mobile isn't in REQUIRED_TRUEIN_FIELDS — an onboarding request
        with no mobile at all shouldn't be flagged, only a malformed one."""
        with app.app_context():
            req = OnboardingRequest(
                initiated_by=1, public_token=uuid.uuid4().hex,
                candidate_name="No Mobile Given", company_code="RDC",
            )
            req.form_data = {}
            db.session.add(req)
            db.session.flush()

            result = preflight_check(req)

            fields = [i["field"] for i in result["issues"]]
            assert "mobile" not in fields
