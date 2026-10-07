"""
Per-company designation lists (2026-10-07): Ultrafine and ROBO have their own
designations, the RDC list is untouched, and nothing that looks a designation
up by name can cross companies.
"""
import uuid

from app.extensions import db as _db
from app.models import (
    UserRole, RequestStatus, OnboardingRequest, Designation, FormField, FieldType, OptionsSource,
)
from .conftest import login, _make_user


def _desig(db, name, company, **kw):
    d = Designation(name=name, company=company, notice_period_days=kw.pop("notice", 30), sort_order=1, **kw)
    db.session.add(d)
    db.session.flush()
    return d


class TestAdminDesignationLists:
    def test_list_is_filtered_by_company(self, client, db, app):
        admin = _make_user("DcAdm1", "dcadm1@t.com", UserRole.SUPER_ADMIN, db)
        _desig(db, "RdcOnlyRole", "RDC")
        _desig(db, "UltraOnlyRole", "Ultrafine")
        _desig(db, "RoboOnlyRole", "ROBO")
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            rdc = client.get("/admin/designations").get_data(as_text=True)
            ult = client.get("/admin/designations?company=Ultrafine").get_data(as_text=True)
            robo = client.get("/admin/designations?company=ROBO").get_data(as_text=True)
        assert "RdcOnlyRole" in rdc and "UltraOnlyRole" not in rdc and "RoboOnlyRole" not in rdc
        assert "UltraOnlyRole" in ult and "RdcOnlyRole" not in ult
        assert "RoboOnlyRole" in robo and "UltraOnlyRole" not in robo

    def test_create_designation_for_a_company(self, client, db, app):
        admin = _make_user("DcAdm2", "dcadm2@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            client.post("/admin/designations/new", data={
                "name": "Mill Operator", "company": "Ultrafine", "notice_period_days": "15",
                "norm_category_id": "1",  # must be ignored: staffing norms are RDC-only
            }, follow_redirects=True)
            d = Designation.query.filter_by(name="Mill Operator").first()
            assert d.company == "Ultrafine"
            assert d.notice_period_days == 15
            assert d.norm_category_id is None

    def test_create_defaults_to_rdc(self, client, db, app):
        admin = _make_user("DcAdm3", "dcadm3@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            client.post("/admin/designations/new", data={"name": "Plain Role", "notice_period_days": "30"},
                        follow_redirects=True)
            assert Designation.query.filter_by(name="Plain Role").first().company == "RDC"


class TestFormOffersCompanyTag:
    def test_form_options_carry_company_for_live_filtering(self, client, db, app):
        init = _make_user("DcInit1", "dcinit1@t.com", UserRole.INITIATOR, db, companies=["RDC", "ROBO"])
        _desig(db, "Crusher Operator", "ROBO")
        _desig(db, "TM Driver Test", "RDC")
        if not FormField.query.filter_by(field_key="designation").first():
            db.session.add(FormField(field_key="designation", field_label="Designation",
                                     field_type=FieldType.DROPDOWN, step=1,
                                     options_source=OptionsSource.DESIGNATION))
        db.session.commit()
        with app.app_context():
            login(client, init.email)
            r = client.get("/requests/new", follow_redirects=True)
            html = r.get_data(as_text=True)
        assert 'value="Crusher Operator"' in html and 'data-company="ROBO"' in html
        assert 'value="TM Driver Test"' in html and 'data-company="RDC"' in html


class TestNameLookupsStayCompanyScoped:
    def test_submit_rejects_designation_from_another_companys_list(self, client, db, app):
        init = _make_user("DcInit2", "dcinit2@t.com", UserRole.INITIATOR, db, companies=["ROBO"])
        _desig(db, "RdcExclusiveRole", "RDC")
        req = OnboardingRequest(
            initiated_by=init.id, status=RequestStatus.DRAFT, public_token=uuid.uuid4().hex,
            candidate_name="C", company_code="ROBO", plant_location="Plant A",
            designation="RdcExclusiveRole")
        db.session.add(req)
        db.session.flush()
        req.form_data = {"company_code": "ROBO", "associate_name": "C", "plant_location": "Plant A",
                         "designation": "RdcExclusiveRole", "uan_number": "AB1234567890"}
        db.session.commit()
        with app.app_context():
            login(client, init.email)
            resp = client.post(f"/requests/{req.public_token}/submit", follow_redirects=True)
            assert b"is not available for ROBO" in resp.data
            assert _db.session.get(OnboardingRequest, req.id).status == RequestStatus.DRAFT

    def test_same_name_in_two_companies_does_not_inflate_special_case_counts(self, db, app):
        from app.requests_bp.routes import _special_case_counts_by_category
        from app.models import NormRoleCategory
        cat = NormRoleCategory.query.first()
        if cat is None:
            return  # norm categories not seeded in this DB
        init = _make_user("DcInit3", "dcinit3@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        _desig(db, "Dup Named Role", "RDC", norm_category_id=cat.id)
        _desig(db, "Dup Named Role", "ROBO")
        db.session.add(OnboardingRequest(
            initiated_by=init.id, status=RequestStatus.ACTIVE, public_token=uuid.uuid4().hex,
            candidate_name="C", company_code="RDC", plant_location="CountPlant",
            designation="Dup Named Role", is_special_case=True))
        db.session.commit()
        assert _special_case_counts_by_category("CountPlant") == {cat.id: 1}

    def test_truein_app_attendance_lookup_uses_requests_company(self, db, app):
        from app.integrations import truein
        init = _make_user("DcInit4", "dcinit4@t.com", UserRole.INITIATOR, db, companies=["ROBO"])
        _desig(db, "Shared Name", "RDC", truein_app_attendance=False)
        _desig(db, "Shared Name", "ROBO", truein_app_attendance=True)
        req = OnboardingRequest(
            initiated_by=init.id, status=RequestStatus.ACTIVE, public_token=uuid.uuid4().hex,
            candidate_name="C", company_code="ROBO", plant_location="P", designation="Shared Name")
        db.session.add(req)
        db.session.flush()
        req.form_data = {"company_code": "ROBO", "designation": "Shared Name"}
        db.session.commit()
        payload = truein.build_payload(req)
        assert payload.get("userAppAttendance") == "1"
