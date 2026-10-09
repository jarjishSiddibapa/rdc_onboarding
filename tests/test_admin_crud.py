"""
Admin area, end to end over HTTP: users, plants, designations, form fields, email settings,
the all-requests list, the audit log, Truein admin actions and the DVT/cluster mappings.
Complements tests/test_admin.py (which covers the basic create/delete paths).
"""
import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.extensions import db as _db
from app.models import (
    UserRole, RequestStatus, OnboardingRequest, User, UserCompanyScope, BusinessHeadRegion, InitiatorRegion,
    ClusterNameMapping, PlantLocation, Designation, FormField, FormFieldOption, FieldType, OptionsSource,
    SystemConfig, AuditLog, PlantDvtMapping, MatchConfidence, TrueinPushLog,
)
from .conftest import login, logout, _make_user


@pytest.fixture(autouse=True)
def _app_ctx(app):
    with app.app_context():
        yield


def _admin(db, tag="a"):
    u = _make_user("Adm" + tag, f"adm{tag}@t.com", UserRole.SUPER_ADMIN, db)
    db.session.commit()
    return u.id, u.email


def _login_admin(client, db, tag="a"):
    uid, email = _admin(db, tag)
    login(client, email)
    return uid


def _user(db, name, email, role, companies=None):
    u = _make_user(name, email, role, db, companies=companies)
    db.session.commit()
    return u.id


def _flashes(resp):
    return resp.get_data(as_text=True)


def _audit(action):
    return AuditLog.query.filter_by(action_type=action).order_by(AuditLog.id.desc()).first()


# ── availability APIs ─────────────────────────────────────────────────────────

class TestAvailabilityApis:
    def test_check_email(self, client, db):
        aid = _login_admin(client, db, "ce")
        _user(db, "Taken", "taken@t.com", UserRole.HEAD_HR)
        assert client.get("/admin/api/check-email?email=free@t.com").get_json() == {"available": True}
        assert client.get("/admin/api/check-email?email=TAKEN@t.com").get_json() == {"available": False}
        assert client.get("/admin/api/check-email?email=").get_json()["available"] is False
        tid = User.query.filter_by(email="taken@t.com").first().id
        assert client.get(f"/admin/api/check-email?email=taken@t.com&exclude_id={tid}").get_json() == {"available": True}

    def test_check_username(self, client, db):
        _login_admin(client, db, "cu")
        u = _make_user("Unamed", "unamed@t.com", UserRole.HEAD_HR, db)
        u.username = "bob_1"
        db.session.commit()
        uid = u.id
        assert client.get("/admin/api/check-username?username=bob_1").get_json() == {"available": False}
        assert client.get(f"/admin/api/check-username?username=bob_1&exclude_id={uid}").get_json() == {"available": True}
        assert client.get("/admin/api/check-username?username=other").get_json() == {"available": True}
        assert client.get("/admin/api/check-username?username=").get_json() == {"available": True}

    def test_non_admin_forbidden(self, client, db):
        _user(db, "Plain", "plain1@t.com", UserRole.HEAD_HR)
        login(client, "plain1@t.com")
        assert client.get("/admin/api/check-email?email=x@t.com").status_code == 403


# ── users ─────────────────────────────────────────────────────────────────────

class TestUsers:
    def test_admin_changes_password(self, client, db):
        _login_admin(client, db, "pw")
        uid = _user(db, "Target", "target1@t.com", UserRole.HEAD_HR)
        weak = client.post(f"/admin/users/{uid}/change-password", data={"new_password": "short"}, follow_redirects=True)
        assert "at least 8 characters" in _flashes(weak)
        ok = client.post(f"/admin/users/{uid}/change-password", data={"new_password": "Brandnew9"}, follow_redirects=True)
        assert "Password updated for Target" in _flashes(ok)
        assert _audit("USER_PASSWORD_CHANGED_BY_ADMIN") is not None
        logout(client)
        assert b"Dashboard" in login(client, "target1@t.com", "Brandnew9").data

    def test_toggle_user_both_ways_and_audit(self, client, db):
        _login_admin(client, db, "tg")
        uid = _user(db, "Toggled", "toggled@t.com", UserRole.HEAD_HR)
        r1 = client.post(f"/admin/users/{uid}/toggle-active", follow_redirects=True)
        assert "User deactivated." in _flashes(r1) and _db.session.get(User, uid).is_active is False
        r2 = client.post(f"/admin/users/{uid}/toggle-active", follow_redirects=True)
        assert "User activated." in _flashes(r2) and _db.session.get(User, uid).is_active is True
        assert _audit("USER_ENABLED") and _audit("USER_DISABLED")

    def test_create_business_head_with_region_and_scope(self, client, db):
        _login_admin(client, db, "cb")
        c = ClusterNameMapping(canonical_cluster_name="Reg-A")
        db.session.add(c)
        db.session.commit()
        cid = c.id
        r = client.post("/admin/users/new", data={
            "name": "New BH", "email": "newbh@t.com", "password": "Passw0rdX", "role": "BUSINESS_HEAD",
            "employee_code": "e100", "companies": ["RDC", "ROBO"], "regions": [str(cid)]}, follow_redirects=True)
        assert "created successfully" in _flashes(r)
        u = User.query.filter_by(email="newbh@t.com").first()
        assert u.employee_code == "E100"
        assert {s.company for s in UserCompanyScope.query.filter_by(user_id=u.id)} == {"RDC", "ROBO"}
        assert [b.cluster_id for b in BusinessHeadRegion.query.filter_by(business_head_id=u.id)] == [cid]

    def test_regions_ignored_without_rdc(self, client, db):
        _login_admin(client, db, "nr")
        c = ClusterNameMapping(canonical_cluster_name="Reg-B")
        db.session.add(c)
        db.session.commit()
        client.post("/admin/users/new", data={
            "name": "Robo BH", "email": "robobh@t.com", "password": "Passw0rdX", "role": "BUSINESS_HEAD",
            "employee_code": "e101", "companies": ["ROBO"], "regions": [str(c.id)]})
        u = User.query.filter_by(email="robobh@t.com").first()
        assert BusinessHeadRegion.query.filter_by(business_head_id=u.id).count() == 0

    def test_create_requires_company_for_scoped_roles_and_unique_username(self, client, db):
        _login_admin(client, db, "cr")
        r = client.post("/admin/users/new", data={
            "name": "No Scope", "email": "noscope@t.com", "password": "Passw0rdX", "role": "HR_MANAGER",
            "employee_code": "e102"}, follow_redirects=True)
        assert "at least one Company Scope" in _flashes(r)
        assert User.query.filter_by(email="noscope@t.com").first() is None
        client.post("/admin/users/new", data={"name": "Has U", "email": "hasu@t.com", "password": "Passw0rdX",
                    "role": "HEAD_HR", "employee_code": "e103", "username": "Same_Name"})
        r2 = client.post("/admin/users/new", data={"name": "Dup U", "email": "dupu@t.com", "password": "Passw0rdX",
                         "role": "HEAD_HR", "employee_code": "e104", "username": "same_name"}, follow_redirects=True)
        assert "Username already taken" in _flashes(r2)

    @pytest.mark.parametrize("role", ["INITIATOR", "BUSINESS_HEAD", "HR_MANAGER", "HEAD_HR", "DR_BHOON", "SUPER_ADMIN"])
    def test_edit_form_renders_for_every_role(self, client, db, role):
        _login_admin(client, db, "er" + role[:3].lower())
        uid = _user(db, "R " + role, f"r_{role.lower()}@t.com", UserRole(role),
                    companies=["RDC"] if role in ("INITIATOR", "BUSINESS_HEAD", "HR_MANAGER") else None)
        assert client.get(f"/admin/users/{uid}/edit").status_code == 200

    def test_edit_rejects_blank_name_and_duplicate_email_and_username(self, client, db):
        _login_admin(client, db, "ev")
        a = _user(db, "A User", "auser@t.com", UserRole.HEAD_HR)
        _user(db, "B User", "buser@t.com", UserRole.HEAD_HR)
        u = _db.session.get(User, a); u.username = None
        other = User.query.filter_by(email="buser@t.com").first(); other.username = "buser"
        db.session.commit()
        r = client.post(f"/admin/users/{a}/edit", data={"name": "  ", "email": "auser@t.com", "role": "HEAD_HR"})
        assert "Name and email are required" in _flashes(r)
        assert _db.session.get(User, a).name == "A User"
        r = client.post(f"/admin/users/{a}/edit", data={"name": "A", "email": "buser@t.com", "role": "HEAD_HR"})
        assert "Email already in use" in _flashes(r)
        r = client.post(f"/admin/users/{a}/edit", data={"name": "A", "email": "auser@t.com", "role": "HEAD_HR",
                                                          "username": "buser"})
        assert "Username already taken" in _flashes(r)
        r = client.post(f"/admin/users/{a}/edit", data={"name": "A", "email": "auser@t.com", "role": "NOPE"})
        assert "Invalid role" in _flashes(r)

    def test_admin_cannot_change_own_role(self, client, db):
        aid = _login_admin(client, db, "sr")
        r = client.post(f"/admin/users/{aid}/edit", data={"name": "Me", "email": "admsr@t.com", "role": "HEAD_HR"},
                        follow_redirects=True)
        assert "cannot change your own role" in _flashes(r)
        assert _db.session.get(User, aid).role == UserRole.SUPER_ADMIN

    def test_role_change_moves_scope_and_clears_stale_regions(self, client, db):
        _login_admin(client, db, "rc")
        c = ClusterNameMapping(canonical_cluster_name="Reg-C")
        db.session.add(c)
        db.session.commit()
        bh = _user(db, "Was BH", "wasbh@t.com", UserRole.BUSINESS_HEAD, companies=["RDC"])
        db.session.add(BusinessHeadRegion(business_head_id=bh, cluster_id=c.id))
        db.session.commit()
        # BH -> HR Manager: scope kept as ticked, regions wiped (HR Manager never has regions)
        r = client.post(f"/admin/users/{bh}/edit", data={"name": "Was BH", "email": "wasbh@t.com",
                        "role": "HR_MANAGER", "companies": ["RDC", "Ultrafine"]}, follow_redirects=True)
        assert "User updated." in _flashes(r)
        assert _db.session.get(User, bh).role == UserRole.HR_MANAGER
        assert BusinessHeadRegion.query.filter_by(business_head_id=bh).count() == 0
        assert {s.company for s in UserCompanyScope.query.filter_by(user_id=bh)} == {"RDC", "Ultrafine"}
        assert _audit("USER_ROLE_CHANGED") is not None
        # HR Manager -> Head HR: unscoped roles lose every company tick
        client.post(f"/admin/users/{bh}/edit", data={"name": "Was BH", "email": "wasbh@t.com", "role": "HEAD_HR"})
        assert UserCompanyScope.query.filter_by(user_id=bh).count() == 0

    def test_unticking_rdc_clears_initiator_regions(self, client, db):
        _login_admin(client, db, "ui")
        c = ClusterNameMapping(canonical_cluster_name="Reg-D")
        db.session.add(c)
        db.session.commit()
        ini = _user(db, "Ini X", "inix@t.com", UserRole.INITIATOR, companies=["RDC", "ROBO"])
        db.session.add(InitiatorRegion(initiator_id=ini, cluster_id=c.id))
        db.session.commit()
        client.post(f"/admin/users/{ini}/edit", data={"name": "Ini X", "email": "inix@t.com", "role": "INITIATOR",
                                                       "companies": ["ROBO"], "regions": [str(c.id)]})
        assert InitiatorRegion.query.filter_by(initiator_id=ini).count() == 0

    def test_edit_requires_a_company_for_scoped_role(self, client, db):
        _login_admin(client, db, "rq")
        h = _user(db, "HRM Q", "hrmq@t.com", UserRole.HR_MANAGER, companies=["RDC"])
        r = client.post(f"/admin/users/{h}/edit", data={"name": "HRM Q", "email": "hrmq@t.com", "role": "HR_MANAGER"})
        assert "at least one Company Scope" in _flashes(r)
        assert UserCompanyScope.query.filter_by(user_id=h).count() == 1


# ── plants ────────────────────────────────────────────────────────────────────

class TestPlants:
    def test_edit_toggle_and_validation(self, client, db):
        _login_admin(client, db, "pl")
        p = PlantLocation(name="P One", company="RDC")
        db.session.add(p)
        db.session.commit()
        pid = p.id
        assert client.get(f"/admin/plants/{pid}/edit").status_code == 200
        bad = client.post(f"/admin/plants/{pid}/edit", data={"name": " ", "company": "RDC"})
        assert "Plant name is required" in _flashes(bad)
        ok = client.post(f"/admin/plants/{pid}/edit", data={"name": "P One Renamed", "company": "ROBO"}, follow_redirects=True)
        assert "Plant updated." in _flashes(ok)
        row = _db.session.get(PlantLocation, pid)
        assert row.name == "P One Renamed" and row.company == "ROBO"
        r = client.post(f"/admin/plants/{pid}/toggle", follow_redirects=True)
        assert "Plant deactivated." in _flashes(r) and _db.session.get(PlantLocation, pid).is_active is False
        r = client.post(f"/admin/plants/{pid}/toggle", follow_redirects=True)
        assert "Plant activated." in _flashes(r)
        assert _audit("PLANT_EDITED") and _audit("PLANT_DISABLED") and _audit("PLANT_ENABLED")

    def test_unknown_plant_is_404(self, client, db):
        _login_admin(client, db, "p4")
        assert client.get("/admin/plants/999999/edit").status_code == 404
        assert client.post("/admin/plants/999999/toggle").status_code == 404


# ── designations ──────────────────────────────────────────────────────────────

class TestDesignations:
    def test_edit_requires_name_and_handles_bad_notice(self, client, db):
        _login_admin(client, db, "de")
        d = Designation(name="Welder", company="RDC", notice_period_days=15)
        db.session.add(d)
        db.session.commit()
        did = d.id
        assert client.get(f"/admin/designations/{did}/edit").status_code == 200
        r = client.post(f"/admin/designations/{did}/edit", data={"name": "   ", "notice_period_days": "20", "company": "RDC"})
        assert "Designation name is required" in _flashes(r)
        assert _db.session.get(Designation, did).name == "Welder"
        client.post(f"/admin/designations/{did}/edit", data={"name": "Welder II", "notice_period_days": "abc", "company": "RDC",
                                                              "truein_app_attendance": "on"})
        row = _db.session.get(Designation, did)
        assert row.name == "Welder II" and row.notice_period_days == 30 and row.truein_app_attendance is True

    def test_moving_to_another_company_drops_norm_bucket(self, client, db):
        from app.models import NormRoleCategory, NormScope, NormSheet
        _login_admin(client, db, "dn")
        cat = NormRoleCategory(name="Cat-Edit", scope=NormScope.PLANT, sheet=NormSheet.SHEET1)
        db.session.add(cat)
        db.session.flush()
        d = Designation(name="Fitter X", company="RDC", norm_category_id=cat.id)
        db.session.add(d)
        db.session.commit()
        did = d.id
        client.post(f"/admin/designations/{did}/edit", data={"name": "Fitter X", "notice_period_days": "15", "company": "ROBO"})
        row = _db.session.get(Designation, did)
        assert row.company == "ROBO" and row.norm_category_id is None

    def test_toggle_and_notice_api(self, client, db):
        _login_admin(client, db, "dt")
        d = Designation(name="Mason", company="RDC", notice_period_days=1)
        db.session.add(d)
        db.session.commit()
        did = d.id
        assert "deactivated" in _flashes(client.post(f"/admin/designations/{did}/toggle", follow_redirects=True))
        assert "activated" in _flashes(client.post(f"/admin/designations/{did}/toggle", follow_redirects=True))
        assert client.get(f"/admin/api/designation-notice-period/{did}").get_json() == {"days": 1, "label": "1 day"}
        assert client.get("/admin/api/designation-notice-period/99999").get_json() == {"days": 0, "label": "—"}


# ── form fields ───────────────────────────────────────────────────────────────

class TestFormFields:
    def _create(self, client, key="extra_q", **over):
        data = {"field_key": key, "field_label": "Extra Question", "field_type": "dropdown", "step": "2",
                "options_source": "inline", "is_required": "on", "opt_label": ["Yes", "No", ""], "opt_value": ["y", "", ""]}
        data.update(over)
        return client.post("/admin/form-fields/new", data=data, follow_redirects=True)

    def test_create_with_inline_options(self, client, db):
        _login_admin(client, db, "fc")
        r = self._create(client, key="Extra Q")
        assert "Field &#39;Extra Question&#39; created." in _flashes(r)
        f = FormField.query.filter_by(field_key="extra_q").first()
        assert f.step == 2 and f.is_required is True
        assert [(o.option_label, o.option_value) for o in f.options] == [("Yes", "y"), ("No", "No")]

    def test_create_validation(self, client, db):
        _login_admin(client, db, "fv")
        assert "Key and label are required" in _flashes(self._create(client, key="", field_label=""))
        self._create(client, key="dup_key")
        assert "already exists" in _flashes(self._create(client, key="dup_key"))
        assert "Invalid field type" in _flashes(self._create(client, key="bad_t", field_type="hologram"))
        assert FormField.query.filter_by(field_key="bad_t").first() is None

    def test_edit_replaces_options_and_validates(self, client, db):
        _login_admin(client, db, "fe")
        self._create(client, key="edit_me")
        f = FormField.query.filter_by(field_key="edit_me").first()
        fid = f.id
        assert client.get(f"/admin/form-fields/{fid}/edit").status_code == 200
        bad = client.post(f"/admin/form-fields/{fid}/edit", data={"field_label": " ", "field_type": "dropdown", "step": "2",
                                                                   "options_source": "inline"})
        assert "Field label is required" in _flashes(bad)
        r = client.post(f"/admin/form-fields/{fid}/edit", data={
            "field_label": "Renamed", "field_type": "dropdown", "step": "3", "options_source": "inline",
            "opt_label": ["Maybe"], "opt_value": ["m"], "min_length": "2", "max_length": "9", "input_pattern": "alpha"},
            follow_redirects=True)
        assert "Field updated." in _flashes(r)
        f = _db.session.get(FormField, fid)
        assert f.field_label == "Renamed" and f.step == 3 and f.is_required is False
        assert (f.min_length, f.max_length, f.input_pattern) == (2, 9, "alpha")
        assert [o.option_label for o in f.options] == ["Maybe"]
        assert _audit("FORM_FIELD_EDITED") and _audit("FORM_FIELD_OPTIONS_UPDATED")
        bad_type = client.post(f"/admin/form-fields/{fid}/edit", data={"field_label": "X", "field_type": "nope"})
        assert "must be valid" in _flashes(bad_type)

    def test_toggle_delete_and_reorder(self, client, db):
        _login_admin(client, db, "ft")
        self._create(client, key="tg_a")
        self._create(client, key="tg_b")
        a = FormField.query.filter_by(field_key="tg_a").first().id
        b = FormField.query.filter_by(field_key="tg_b").first().id
        assert "disabled" in _flashes(client.post(f"/admin/form-fields/{a}/toggle", follow_redirects=True))
        assert "enabled" in _flashes(client.post(f"/admin/form-fields/{a}/toggle", follow_redirects=True))
        ok = client.post("/admin/form-fields/reorder", json={"ids": [b, a]})
        assert ok.get_json() == {"ok": True}
        assert _db.session.get(FormField, b).sort_order == 0 and _db.session.get(FormField, a).sort_order == 1
        bad = client.post("/admin/form-fields/reorder", json={"ids": ["x"]})
        assert bad.status_code == 400
        assert "Field removed." in _flashes(client.post(f"/admin/form-fields/{a}/delete", follow_redirects=True))
        assert _db.session.get(FormField, a).is_deleted is True
        assert "tg_a" not in client.get("/admin/form-fields").get_data(as_text=True)


# ── email settings ────────────────────────────────────────────────────────────

class TestEmailSettings:
    def test_page_and_validation(self, client, db):
        _login_admin(client, db, "es")
        assert client.get("/admin/settings/email").status_code == 200
        r = client.post("/admin/settings/email", data={"email_user": "", "email_pass": "x"}, follow_redirects=True)
        assert "Email address is required" in _flashes(r)
        r = client.post("/admin/settings/email", data={"email_user": "ops@gmail.com", "email_pass": ""}, follow_redirects=True)
        assert "App password is required" in _flashes(r)

    def test_failed_connection_saves_nothing(self, client, db):
        _login_admin(client, db, "ef")
        with patch("app.utils._send_smtp", side_effect=RuntimeError("auth rejected")):
            r = client.post("/admin/settings/email", data={"email_user": "ops@gmail.com", "email_pass": "pw"}, follow_redirects=True)
        assert "Connection test failed" in _flashes(r) and "auth rejected" in _flashes(r)
        assert SystemConfig.query.filter_by(key="email_user").first() is None

    def test_successful_save_stores_but_never_shows_password(self, client, db):
        _login_admin(client, db, "eo")
        with patch("app.utils._send_smtp") as smtp:
            r = client.post("/admin/settings/email", data={"email_user": "ops@gmail.com", "email_pass": "S3cretAppPw"}, follow_redirects=True)
        assert "Email settings saved" in _flashes(r)
        smtp.assert_called_once()
        assert SystemConfig.query.filter_by(key="email_user").first().value == "ops@gmail.com"
        assert SystemConfig.query.filter_by(key="email_host").first().value == "smtp.gmail.com"
        page = client.get("/admin/settings/email").get_data(as_text=True)
        assert "S3cretAppPw" not in page and "ops@gmail.com" in page
        # a second save without a new password keeps the stored one
        with patch("app.utils._send_smtp"):
            client.post("/admin/settings/email", data={"email_user": "ops@outlook.com", "email_pass": ""})
        assert SystemConfig.query.filter_by(key="email_pass").first().value == "S3cretAppPw"
        assert SystemConfig.query.filter_by(key="email_host").first().value == "smtp.office365.com"

    def test_smtp_domain_detection(self):
        from app.admin.routes import _smtp_for_email
        assert _smtp_for_email("a@yahoo.com") == ("smtp.mail.yahoo.com", 587)
        assert _smtp_for_email("a@zoho.com") == ("smtp.zoho.com", 587)
        assert _smtp_for_email("a@mycompany.in") == ("smtp.gmail.com", 587)


# ── all requests list ─────────────────────────────────────────────────────────

def _req(db, user_id, status, company="RDC", name="Cand", deleted=False):
    r = OnboardingRequest(initiated_by=user_id, status=status, public_token=uuid.uuid4().hex, candidate_name=name,
                          company_code=company, plant_location="P", designation="D", is_deleted=deleted)
    db.session.add(r)
    db.session.flush()
    return r


class TestAllRequests:
    def test_default_filters_and_deleted(self, client, db):
        aid = _login_admin(client, db, "ar")
        ini = _user(db, "Ini AR", "iniar@t.com", UserRole.INITIATOR, companies=["RDC"])
        _req(db, ini, RequestStatus.PENDING_BH, name="Ongoing One")
        _req(db, ini, RequestStatus.ACTIVE, name="Done One")
        _req(db, ini, RequestStatus.PENDING_BH, company="ROBO", name="Robo One")
        _req(db, ini, RequestStatus.PENDING_BH, name="Gone One", deleted=True)
        db.session.commit()
        page = client.get("/admin/requests").get_data(as_text=True)
        assert "Ongoing One" in page and "Done One" in page and "Robo One" in page and "Gone One" not in page
        assert "Gone One" in client.get("/admin/requests?show_deleted=1").get_data(as_text=True)
        only_robo = client.get("/admin/requests?company=ROBO").get_data(as_text=True)
        assert "Robo One" in only_robo and "Ongoing One" not in only_robo
        filtered = client.get("/admin/requests?status=ACTIVE").get_data(as_text=True)
        assert "Done One" in filtered and "Ongoing One" not in filtered
        assert client.get("/admin/requests?status=NOT_A_STATUS").status_code == 200

    def test_hr_manager_only_sees_ticked_companies_and_initiator_blocked(self, client, db):
        ini = _user(db, "Ini AH", "iniah@t.com", UserRole.INITIATOR, companies=["RDC"])
        _req(db, ini, RequestStatus.PENDING_BH, company="RDC", name="Visible Rdc")
        _req(db, ini, RequestStatus.PENDING_BH, company="ROBO", name="Hidden Robo")
        _user(db, "HRM AH", "hrmah@t.com", UserRole.HR_MANAGER, companies=["RDC"])
        db.session.commit()
        login(client, "hrmah@t.com")
        page = client.get("/admin/requests").get_data(as_text=True)
        assert "Visible Rdc" in page and "Hidden Robo" not in page
        assert "Hidden Robo" not in client.get("/admin/requests?status=PENDING_BH&company=ROBO").get_data(as_text=True)
        logout(client)
        login(client, "iniah@t.com")
        assert client.get("/admin/requests").status_code == 403


# ── audit log ─────────────────────────────────────────────────────────────────

class TestAuditLogFilters:
    def test_filters_and_bad_input(self, client, db):
        aid = _login_admin(client, db, "al")
        client.post("/admin/plants/new", data={"name": "Audit Plant", "company": "RDC"})
        assert client.get("/admin/audit-log").status_code == 200
        assert "PLANT_CREATED" in client.get("/admin/audit-log?category=ADMIN_PLANT").get_data(as_text=True) or \
               "Plant Added" in client.get("/admin/audit-log?category=ADMIN_PLANT").get_data(as_text=True)
        assert "Audit Plant" in client.get("/admin/audit-log?action=plant_created").get_data(as_text=True)
        assert "Audit Plant" not in client.get("/admin/audit-log?category=AUTH").get_data(as_text=True)
        assert "Audit Plant" in client.get(f"/admin/audit-log?actor_id={aid}").get_data(as_text=True)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        yesterday = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d")
        assert "Audit Plant" in client.get(f"/admin/audit-log?date_from={yesterday}&date_to={today}").get_data(as_text=True)
        assert "Audit Plant" not in client.get("/admin/audit-log?date_to=2000-01-01").get_data(as_text=True)
        # junk filters never 500
        assert client.get("/admin/audit-log?category=NOPE&date_from=garbage&date_to=garbage").status_code == 200

    def test_staffing_categories_have_friendly_labels(self, client, db):
        _login_admin(client, db, "ac")
        page = client.get("/admin/audit-log").get_data(as_text=True)
        for label in ("Staffing Norms", "Plant Mappings", "Cluster Mappings", "ZingHR Departments"):
            assert label in page
        assert ">ADMIN_STAFFING_NORM<" not in page


# ── Truein admin actions (the integration itself is mocked) ───────────────────

def _push_result(**over):
    r = {"success": True, "message": "ok", "empId": "EMP1", "http_status": 200, "raw_response": {},
         "payload_sent": {}, "dropped_fields": [], "retryable": True}
    r.update(over)
    return r


class TestTrueinAdminActions:
    def _active(self, db, company="RDC"):
        ini = _user(db, "Ini TR" + company, f"initr{company.lower()}@t.com", UserRole.INITIATOR, companies=[company])
        r = _req(db, ini, RequestStatus.ACTIVE, company=company, name="Pushy " + company)
        db.session.commit()
        return r.public_token, r.id

    def test_push_only_for_active_requests(self, client, db):
        _login_admin(client, db, "t1")
        ini = _user(db, "Ini T1", "init1@t.com", UserRole.INITIATOR, companies=["RDC"])
        r = _req(db, ini, RequestStatus.PENDING_BH)
        db.session.commit()
        resp = client.post(f"/admin/requests/{r.public_token}/truein-push", follow_redirects=True)
        assert "Only ACTIVE requests can be pushed" in _flashes(resp)

    def test_successful_push_clean_and_with_dropped_fields(self, client, db):
        _login_admin(client, db, "t2")
        token, rid = self._active(db)
        with patch("app.integrations.truein.push_employee", return_value=_push_result()):
            r = client.post(f"/admin/requests/{token}/truein-push", follow_redirects=True)
        assert "Pushed to Truein successfully (empId: EMP1)" in _flashes(r)
        assert _db.session.get(OnboardingRequest, rid).truein_pushed_at is not None
        assert TrueinPushLog.query.filter_by(request_id=rid).count() == 1
        with patch("app.integrations.truein.push_employee", return_value=_push_result(dropped_fields=["father_name"])), \
             patch("app.integrations.truein._handle_dropped_fields"):
            r = client.post(f"/admin/requests/{token}/truein-push", follow_redirects=True)
        assert "1 field(s) were skipped" in _flashes(r)

    def test_failed_push_retryable_and_collision(self, client, db):
        _login_admin(client, db, "t3")
        token, rid = self._active(db)
        with patch("app.integrations.truein.push_employee", return_value=_push_result(success=False, message="boom")), \
             patch("app.integrations.truein.start_retry_thread") as retry, patch("app.integrations.truein._notify_push_failed"):
            r = client.post(f"/admin/requests/{token}/truein-push", follow_redirects=True)
        assert "Push FAILED: boom" in _flashes(r) and "Background retry started" in _flashes(r)
        retry.assert_called_once()
        with patch("app.integrations.truein.push_employee", return_value=_push_result(success=False, message="Match found with X", retryable=False)), \
             patch("app.integrations.truein.start_retry_thread") as retry2, patch("app.integrations.truein._notify_push_failed"):
            r = client.post(f"/admin/requests/{token}/truein-push", follow_redirects=True)
        assert "will NOT be retried automatically" in _flashes(r)
        retry2.assert_not_called()
        assert _db.session.get(OnboardingRequest, rid).truein_retry_stopped is True

    def test_exception_during_push_is_reported_not_raised(self, client, db):
        _login_admin(client, db, "t4")
        token, rid = self._active(db)
        with patch("app.integrations.truein.push_employee", side_effect=RuntimeError("network down")), \
             patch("app.integrations.truein.start_retry_thread"), patch("app.integrations.truein._notify_push_failed"):
            r = client.post(f"/admin/requests/{token}/truein-push", follow_redirects=True)
        assert "Push error: network down" in _flashes(r)

    def test_stop_retry_logs_and_permissions(self, client, db):
        _login_admin(client, db, "t5")
        token, rid = self._active(db)
        r = client.post(f"/admin/requests/{token}/truein-stop-retry", follow_redirects=True)
        assert "Truein retries stopped" in _flashes(r)
        assert _db.session.get(OnboardingRequest, rid).truein_retry_stopped is True
        assert client.get(f"/admin/requests/{token}/truein-logs").status_code == 200
        logout(client)
        _user(db, "HRM T5", "hrmt5@t.com", UserRole.HR_MANAGER, companies=["RDC"])
        login(client, "hrmt5@t.com")
        assert client.post(f"/admin/requests/{token}/truein-push").status_code == 403
        assert client.get(f"/admin/requests/{token}/truein-logs").status_code == 403

    def test_dry_run_failure_is_reported(self, client, db):
        _login_admin(client, db, "t6")
        token, rid = self._active(db)
        with patch("app.integrations.truein.dry_run_to_file", side_effect=RuntimeError("no creds")):
            r = client.post(f"/admin/requests/{token}/truein-dryrun", follow_redirects=True)
        assert "Truein dry-run failed: no creds" in _flashes(r)


# ── DVT plant mappings and cluster mappings ───────────────────────────────────

class TestMappings:
    def test_plant_mapping_edit_flow(self, client, db):
        _login_admin(client, db, "pm")
        cl = ClusterNameMapping(canonical_cluster_name="Mum")
        pm = PlantDvtMapping(plant_location_name="MUM-Test", match_confidence=MatchConfidence.UNMATCHED)
        db.session.add_all([cl, pm])
        db.session.commit()
        mid, cid = pm.id, cl.id
        dvt_plants = [{"plant_code": "M1", "daily_tracker_name": "Mum One", "erp_name": "MUM-Test"}]
        with patch("app.integrations.dvt.fetch_all_plants", return_value=dvt_plants):
            assert client.get("/admin/plant-mappings").status_code == 200
            assert client.get(f"/admin/plant-mappings/{mid}/edit").status_code == 200
            bad = client.post(f"/admin/plant-mappings/{mid}/edit", data={"dvt_plant_code": "ZZ", "cluster_id": cid})
            assert "Could not confirm that plant" in _flashes(bad)
            ok = client.post(f"/admin/plant-mappings/{mid}/edit",
                             data={"dvt_plant_code": "M1", "cluster_id": cid, "truein_sub_site": "MUM-Test"}, follow_redirects=True)
        assert "Plant mapping updated." in _flashes(ok)
        row = _db.session.get(PlantDvtMapping, mid)
        assert (row.dvt_plant_code, row.cluster_id, row.match_confidence) == ("M1", cid, MatchConfidence.MANUAL)
        with patch("app.integrations.dvt.fetch_all_plants", return_value=dvt_plants):
            client.post(f"/admin/plant-mappings/{mid}/edit", data={"dvt_plant_code": ""})
        assert _db.session.get(PlantDvtMapping, mid).dvt_plant_code is None

    def test_plant_mapping_edit_survives_dvt_outage(self, client, db):
        _login_admin(client, db, "po")
        pm = PlantDvtMapping(plant_location_name="MUM-Out", match_confidence=MatchConfidence.UNMATCHED)
        db.session.add(pm)
        db.session.commit()
        with patch("app.integrations.dvt.fetch_all_plants", side_effect=RuntimeError("dvt down")):
            r = client.get(f"/admin/plant-mappings/{pm.id}/edit")
        assert r.status_code == 200 and "dvt down" in r.get_data(as_text=True)

    def test_auto_match_and_refresh_actions(self, client, db):
        _login_admin(client, db, "am")
        with patch("app.services.matching.auto_match_plants", return_value={"matched_exact": 3, "unmatched": 1, "total": 4}):
            r = client.post("/admin/plant-mappings/auto-match", follow_redirects=True)
        assert "3 exact match(es), 1 unmatched (of 4)" in _flashes(r)
        with patch("app.services.matching.auto_match_plants", side_effect=RuntimeError("dvt 500")):
            assert "Auto-match failed: dvt 500" in _flashes(client.post("/admin/plant-mappings/auto-match", follow_redirects=True))
        with patch("app.services.matching.auto_match_clusters",
                   return_value={"matched_exact": 2, "matched_fuzzy": 1, "unmatched": 0, "total": 3}):
            r = client.post("/admin/cluster-mappings/auto-match", follow_redirects=True)
        assert "2 exact, 1 fuzzy, 0 unmatched (of 3 DVT regions)" in _flashes(r)
        with patch("app.services.snapshot_refresh.refresh_snapshot_now",
                   return_value={"snapshots_written": 5, "employee_rows_written": 9, "warnings": []}):
            r = client.post("/admin/plant-mappings/refresh-snapshot", follow_redirects=True)
        assert "Snapshot refreshed: 5 rows written, 9 employees, 0 warning(s)." in _flashes(r)
        with patch("app.services.snapshot_refresh.refresh_snapshot_now", return_value={"skipped": True, "reason": "already running"}):
            r = client.post("/admin/plant-mappings/refresh-snapshot", follow_redirects=True)
        assert "Snapshot refresh skipped: already running" in _flashes(r)
        with patch("app.services.snapshot_refresh.refresh_snapshot_now", side_effect=RuntimeError("zinghr 401")):
            r = client.post("/admin/plant-mappings/refresh-snapshot", follow_redirects=True)
        assert "Snapshot refresh failed: zinghr 401" in _flashes(r)

    def test_cluster_mapping_crud(self, client, db):
        _login_admin(client, db, "cm")
        assert client.get("/admin/cluster-mappings").status_code == 200
        assert client.get("/admin/cluster-mappings/new").status_code == 200
        assert "Cluster name is required" in _flashes(client.post("/admin/cluster-mappings/new", data={"canonical_cluster_name": " "}))
        r = client.post("/admin/cluster-mappings/new", data={"canonical_cluster_name": "North", "dvt_region": "N"}, follow_redirects=True)
        assert "Cluster &#39;North&#39; added." in _flashes(r)
        cid = ClusterNameMapping.query.filter_by(canonical_cluster_name="North").first().id
        assert client.get(f"/admin/cluster-mappings/{cid}/edit").status_code == 200
        assert "Cluster name is required" in _flashes(client.post(f"/admin/cluster-mappings/{cid}/edit", data={"canonical_cluster_name": ""}))
        client.post(f"/admin/cluster-mappings/{cid}/edit", data={"canonical_cluster_name": "North Zone", "zinghr_city": "Delhi"})
        row = _db.session.get(ClusterNameMapping, cid)
        assert (row.canonical_cluster_name, row.zinghr_city, row.match_confidence) == ("North Zone", "Delhi", MatchConfidence.MANUAL)


# ── duplicate-name guards (these used to 500 or silently duplicate) ───────────

class TestDuplicateGuards:
    def test_duplicate_plant_name_in_same_company_is_refused(self, client, db):
        _login_admin(client, db, "dg1")
        client.post("/admin/plants/new", data={"name": "Dup Plant", "company": "ROBO"})
        r = client.post("/admin/plants/new", data={"name": "  dup plant ", "company": "ROBO"}, follow_redirects=True)
        assert "already exists" in _flashes(r)
        assert PlantLocation.query.filter_by(company="ROBO", is_deleted=False).count() == 1
        # same name under another company is fine
        client.post("/admin/plants/new", data={"name": "Dup Plant", "company": "RDC"})
        assert PlantLocation.query.filter_by(name="Dup Plant", is_deleted=False).count() == 2

    def test_form_field_key_of_a_removed_field_gives_a_message_not_a_500(self, client, db):
        _login_admin(client, db, "dg2")
        data = {"field_key": "old_key", "field_label": "Old", "field_type": "text", "step": "1", "options_source": "inline"}
        client.post("/admin/form-fields/new", data=data)
        fid = FormField.query.filter_by(field_key="old_key").first().id
        client.post(f"/admin/form-fields/{fid}/delete")
        r = client.post("/admin/form-fields/new", data=data, follow_redirects=True)
        assert r.status_code == 200 and "already exists" in _flashes(r)

    def test_duplicate_cluster_name_is_refused(self, client, db):
        _login_admin(client, db, "dg3")
        client.post("/admin/cluster-mappings/new", data={"canonical_cluster_name": "Zone One"})
        r = client.post("/admin/cluster-mappings/new", data={"canonical_cluster_name": "zone one"}, follow_redirects=True)
        assert r.status_code == 200 and "already exists" in _flashes(r)
        assert ClusterNameMapping.query.filter(ClusterNameMapping.canonical_cluster_name.ilike("zone one")).count() == 1
