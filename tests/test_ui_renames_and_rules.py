"""
2026-10-08 stakeholder batch: role/status renames, "New Hiring Request", "Hiring Flag",
remarks with no length limit, working Back buttons, calendar pickers, per-column
dashboard filters, optional CV/Resume.
"""
import os
import uuid

from app.extensions import db as _db
from app.models import (
    UserRole, RequestStatus, OnboardingRequest, FormField, FieldType, OptionsSource,
    ROLE_LABELS, STATUS_LABELS, ApprovalAction,
)
from .conftest import login, _make_user

ROOT = os.path.dirname(os.path.dirname(__file__))


def _req(db, user, company="RDC", status=RequestStatus.PENDING_BH, designation="Engineer", name="Test Candidate"):
    r = OnboardingRequest(initiated_by=user.id, status=status, public_token=uuid.uuid4().hex,
                          candidate_name=name, company_code=company, plant_location="Plant A", designation=designation)
    db.session.add(r)
    db.session.flush()
    r.form_data = {"company_code": company, "associate_name": name, "plant_location": "Plant A",
                   "designation": designation, "uan_number": "AB1234567890"}
    db.session.flush()
    return r


class TestRoleAndStatusNames:
    def test_role_labels(self):
        assert ROLE_LABELS["INITIATOR"] == "Reporting Manager (RM)"
        assert ROLE_LABELS["BUSINESS_HEAD"] == "Business / Functional Head"
        assert ROLE_LABELS["DR_BHOON"] == "Special Approver"
        assert ROLE_LABELS["HR_MANAGER"] == "HR Manager" and ROLE_LABELS["HEAD_HR"] == "Head HR"

    def test_status_labels(self):
        assert STATUS_LABELS["PENDING_BH"] == "Pending Business / Functional Head"
        assert STATUS_LABELS["PENDING_DR_BHOON"] == "Pending Special Approval"
        assert STATUS_LABELS["REJECTED_BH"] == "Rejected by Business / Functional Head"
        assert STATUS_LABELS["REJECTED_DR_BHOON"] == "Rejected by Special Approver"

    def test_models_use_the_shared_labels(self, db):
        u = _make_user("RnU1", "rnu1@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        r = _req(db, u, status=RequestStatus.PENDING_DR_BHOON)
        assert u.role_label == "Reporting Manager (RM)"
        assert r.status_label == "Pending Special Approval"

    def test_enum_values_unchanged(self):
        # stored in the DB and used by the state machine — display rename only
        assert UserRole.INITIATOR.value == "INITIATOR" and UserRole.DR_BHOON.value == "DR_BHOON"
        assert RequestStatus.PENDING_DR_BHOON.value == "PENDING_DR_BHOON"

    def test_no_old_names_in_templates(self):
        import re
        offenders = []
        for dp, _, files in os.walk(os.path.join(ROOT, "app", "templates")):
            for f in files:
                if not f.endswith(".html"):
                    continue
                text = open(os.path.join(dp, f), encoding="utf-8").read()
                # strip Jinja/HTML comments, then look at visible wording
                text = re.sub(r"\{#.*?#\}|<!--.*?-->", "", text, flags=re.S)
                for old in ("Business Head", "Dr. Bhoon", "Hiring Not Possible", "New Onboarding Request"):
                    if old in text:
                        offenders.append((f, old))
        assert not offenders, offenders

    def test_admin_user_form_lists_new_role_names(self, client, db, app):
        admin = _make_user("RnAdm", "rnadm@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            html = client.get("/admin/users/new").get_data(as_text=True)
        assert "Reporting Manager (RM)" in html and "Business / Functional Head" in html and "Special Approver" in html


class TestAppName:
    NAME = "RDC Associates Hiring"

    def test_no_old_app_name_in_templates_or_emails(self):
        offenders = []
        for base in ("templates", "auth", "admin", "requests_bp", "services", "integrations"):
            for dp, _, files in os.walk(os.path.join(ROOT, "app", base)):
                for f in files:
                    if not f.endswith((".html", ".py")):
                        continue
                    text = open(os.path.join(dp, f), encoding="utf-8").read()
                    for old in ("Teamlease Employee", "Employee Onboarding Portal", "Teamlease HR", "TeamLease Admin",
                                "RDC Teamlease", "RDC HR Onboarding Portal"):
                        if old in text:
                            offenders.append((f, old))
        assert not offenders, offenders

    def test_login_and_dashboard_use_new_name(self, client, db, app):
        init = _make_user("NmInit", "nminit@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        db.session.commit()
        with app.app_context():
            login_page = client.get("/auth/login").get_data(as_text=True)
            assert self.NAME in login_page and "Teamlease" not in login_page
            login(client, init.email)
            dash = client.get("/dashboard").get_data(as_text=True)
            assert f"<title>My Requests — {self.NAME}</title>" in dash
            assert self.NAME in dash and "Teamlease" not in dash


class TestRemarksHaveNoLengthLimit:
    def _bh_and_req(self, db, tag):
        init = _make_user(f"Rm{tag}I", f"rm{tag}i@t.com", UserRole.INITIATOR, db, companies=["ROBO"])
        bh = _make_user(f"Rm{tag}B", f"rm{tag}b@t.com", UserRole.BUSINESS_HEAD, db, companies=["ROBO"])
        _make_user(f"Rm{tag}H", f"rm{tag}h@t.com", UserRole.HR_MANAGER, db, companies=["ROBO"])
        req = _req(db, init, company="ROBO")
        db.session.commit()
        return bh.email, req.public_token, req.id

    def test_one_word_approval_remark_is_accepted(self, client, db, app):
        email, token, rid = self._bh_and_req(db, "a")
        with app.app_context():
            login(client, email)
            client.post(f"/requests/{token}/approve", data={"remark": "OK"}, follow_redirects=True)
            assert _db.session.get(OnboardingRequest, rid).status == RequestStatus.PENDING_HR_MANAGER

    def test_empty_approval_remark_still_rejected(self, client, db, app):
        email, token, rid = self._bh_and_req(db, "b")
        with app.app_context():
            login(client, email)
            r = client.post(f"/requests/{token}/approve", data={"remark": "   "}, follow_redirects=True)
            assert b"is required" in r.data
            assert _db.session.get(OnboardingRequest, rid).status == RequestStatus.PENDING_BH

    def test_short_rejection_remark_is_accepted(self, client, db, app):
        email, token, rid = self._bh_and_req(db, "c")
        with app.app_context():
            login(client, email)
            client.post(f"/requests/{token}/reject", data={"remark": "no"}, follow_redirects=True)
            assert _db.session.get(OnboardingRequest, rid).status == RequestStatus.REJECTED_BH

    def test_over_norm_chain_no_longer_needs_20_chars(self, client, db, app):
        init = _make_user("RmOnI", "rmoni@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        bh = _make_user("RmOnB", "rmonb@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        req = _req(db, init, company="RDC")
        req.is_special_case = True
        db.session.commit()
        token, rid, email = req.public_token, req.id, bh.email
        with app.app_context():
            login(client, email)
            client.post(f"/requests/{token}/approve", data={"remark": "fine"}, follow_redirects=True)
            assert _db.session.get(OnboardingRequest, rid).status == RequestStatus.PENDING_HEAD_HR

    def test_detail_modals_have_no_minlength(self, client, db, app):
        email, token, _ = self._bh_and_req(db, "d")
        with app.app_context():
            login(client, email)
            html = client.get(f"/requests/{token}").get_data(as_text=True)
        assert 'name="remark"' in html and "minlength" not in html.split('name="remark"')[1][:120]


class TestHiringFormBits:
    def _form_world(self, db):
        db.session.add(FormField(field_key="date_of_birth", field_label="Date of Birth",
                                 field_type=FieldType.DATE, step=1, is_required=True))
        db.session.add(FormField(field_key="cv_resume", field_label="CV / Resume (Optional)",
                                 field_type=FieldType.FILE, step=1, is_required=False,
                                 options_source=OptionsSource.INLINE))
        db.session.flush()

    def test_title_and_calendar_and_optional_cv(self, client, db, app):
        init = _make_user("HfInit1", "hfinit1@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        self._form_world(db)
        db.session.commit()
        with app.app_context():
            login(client, init.email)
            html = client.get("/requests/new", follow_redirects=True).get_data(as_text=True)
        assert "New Hiring Request" in html and "New Onboarding Request" not in html
        assert 'class="date-cal-btn"' in html and 'type="date" class="date-cal-native"' in html
        # the typed DD/MM/YYYY box is still there
        assert 'placeholder="DD/MM/YYYY"' in html
        # CV is optional: its file input must not be `required`
        cv = html.split('name="cv_resume"')[1].split(">")[0]
        assert "required" not in cv

    def test_back_button_submits_without_validation(self, client, db, app):
        init = _make_user("HfInit2", "hfinit2@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        self._form_world(db)
        db.session.add(FormField(field_key="x2", field_label="Step Two", field_type=FieldType.TEXT, step=2, is_required=True))
        db.session.commit()
        with app.app_context():
            login(client, init.email)
            first = client.get("/requests/new", follow_redirects=False)
            token = first.headers["Location"].split("token=")[1].split("&")[0] if first.status_code == 302 else None
            if token is None:
                html = client.get("/requests/new").get_data(as_text=True)
                token = html.split("token=")[1].split('"')[0].split("&")[0]
            page = client.get(f"/requests/new?step=2&token={token}").get_data(as_text=True)
            assert 'value="back" class="btn btn-secondary" formnovalidate' in page
            r = client.post(f"/requests/new?step=2&token={token}", data={"action": "back"})
            assert r.status_code == 302 and "step=1" in r.headers["Location"]

    def test_submit_handler_uses_the_clicked_button(self):
        js = open(os.path.join(ROOT, "app", "static", "js", "base.js"), encoding="utf-8").read()
        assert "e.submitter" in js
        # must not disable the submitter before the browser reads its name/value
        assert "setTimeout(function() { btn.disabled = true; }, 0)" in js

    def test_cv_field_in_fresh_db_seed(self):
        seed = open(os.path.join(ROOT, "seed.py"), encoding="utf-8").read()
        assert '"cv_resume"' in seed and "FieldType.FILE, 3, False" in seed


class TestColumnHeaderFilters:
    def _world(self, db):
        init = _make_user("ColInit", "colinit@t.com", UserRole.INITIATOR, db, companies=["RDC", "ROBO"])
        _req(db, init, company="RDC", designation="Mechanic", name="Aaa One")
        _req(db, init, company="ROBO", designation="Welder", name="Bbb Two")
        _req(db, init, company="ROBO", designation="Mechanic", name="Ccc Three")
        db.session.commit()
        return init.email

    def test_initiator_dashboard_has_column_dropdowns_not_a_filter_bar(self, client, db, app):
        email = self._world(db)
        with app.app_context():
            login(client, email)
            html = client.get("/dashboard").get_data(as_text=True)
        for param in ("company", "designation", "status"):
            assert f'data-col-filter="{param}"' in html
        assert "Filter by stage" not in html and "filter-pill" not in html.split("<table")[0]
        # options come from the viewer's own data
        assert '<option value="Welder"' in html and '<option value="ROBO"' in html

    def test_company_and_designation_filters_apply_together(self, client, db, app):
        email = self._world(db)
        with app.app_context():
            login(client, email)
            only_robo = client.get("/dashboard?company=ROBO").get_data(as_text=True)
            assert "Bbb Two" in only_robo and "Ccc Three" in only_robo and "Aaa One" not in only_robo
            both = client.get("/dashboard?company=ROBO&designation=Mechanic").get_data(as_text=True)
            assert "Ccc Three" in both and "Bbb Two" not in both and "Aaa One" not in both
            assert "Clear all filters" in both

    def test_approver_dashboard_filters(self, client, db, app):
        init = _make_user("ColInit2", "colinit2@t.com", UserRole.INITIATOR, db, companies=["RDC", "ROBO"])
        hhr = _make_user("ColHhr", "colhhr@t.com", UserRole.HEAD_HR, db)
        _req(db, init, company="RDC", designation="Mechanic", name="Zed Rdc")
        _req(db, init, company="ROBO", designation="Welder", name="Yan Robo")
        db.session.commit()
        with app.app_context():
            login(client, hhr.email)
            page = client.get("/dashboard").get_data(as_text=True)
            assert 'data-col-filter="company"' in page and "Filter by" in page
            robo = client.get("/dashboard?company=ROBO").get_data(as_text=True)
            all_section = robo.split("All Requests")[-1]
            assert "Yan Robo" in all_section and "Zed Rdc" not in all_section
