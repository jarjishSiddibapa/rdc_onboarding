"""2026-10-12 batch: snapshot retention, staffing-norms + department-mapping admin pages, no blank drafts,
request search, accessible button colour, sign-out / session hardening."""
from app.timeutil import now_ist
import re
import uuid
from datetime import datetime, timedelta

import pytest

from app.extensions import db as _db
from app.models import (
    OnboardingRequest, RequestStatus, UserRole, User, StaffingSnapshot, EmployeeLocationSnapshot,
    ExternalDesignationSource, NormRoleCategory, NormTier, NormRequirement, NormScope, NormSheet,
    NormRequirementType, DepartmentCategoryMap, SystemConfig, BackupRun, AuditLog,
)
from app.services import snapshot_retention, headcount, db_backup
from .conftest import login, logout, _make_user
from .test_email_actions import _world, _link, _status


@pytest.fixture(autouse=True)
def _app_ctx(app):
    with app.app_context():
        yield


def _admin(db, email="m-admin@t.com"):
    return _make_user("MAdmin", email, UserRole.SUPER_ADMIN, db)


def _cat(db, name="Technical", scope=NormScope.PLANT, sheet=NormSheet.SHEET1, gated=True):
    c = NormRoleCategory(name=name, scope=scope, sheet=sheet, is_capacity_gated=gated)
    db.session.add(c)
    db.session.flush()
    return c


def _tier(db, label, lo, hi, scope=NormScope.PLANT, active=True):
    t = NormTier(sheet=NormSheet.SHEET1, scope=scope, tier_key=label.replace(" ", "_"), tier_label=label,
                 min_value=lo, max_value=hi, is_active=active)
    db.session.add(t)
    db.session.flush()
    return t


# ── snapshot retention ─────────────────────────────────────────────────────────

def _runs(db, n, cat):
    base = datetime(2026, 9, 1, 2, 0)
    for i in range(n):
        at = base + timedelta(days=i)
        db.session.add(StaffingSnapshot(scope=NormScope.PLANT, location_key="P1", norm_role_category_id=cat.id,
                                        current_headcount=i, computed_at=at))
        db.session.add(EmployeeLocationSnapshot(source=ExternalDesignationSource.ZINGHR, employee_code=f"E{i}",
                                                computed_at=at))
    db.session.commit()
    return base


class TestSnapshotRetention:
    def test_keeps_newest_runs_and_removes_the_rest(self, db):
        cat = _cat(db)
        base = _runs(db, 20, cat)
        res = snapshot_retention.prune(keep_runs=7)
        assert res["deleted_staffing"] == 13 and res["deleted_employees"] == 13
        left = sorted(r[0] for r in _db.session.query(StaffingSnapshot.computed_at).all())
        assert len(left) == 7 and left[-1] == base + timedelta(days=19) and left[0] == base + timedelta(days=13)

    def test_never_drops_below_five_runs(self, db):
        cat = _cat(db)
        _runs(db, 9, cat)
        snapshot_retention.prune(keep_runs=1)
        assert _db.session.query(StaffingSnapshot).count() == snapshot_retention.MIN_KEEP_RUNS

    def test_nothing_to_do_when_history_is_short(self, db):
        cat = _cat(db)
        _runs(db, 6, cat)
        assert snapshot_retention.prune(keep_runs=14)["deleted_staffing"] == 0
        assert _db.session.query(StaffingSnapshot).count() == 6

    def test_setting_is_clamped_and_stored(self, db):
        assert snapshot_retention.get_keep_runs() == snapshot_retention.DEFAULT_KEEP_RUNS
        assert snapshot_retention.set_keep_runs(1) == snapshot_retention.MIN_KEEP_RUNS
        _db.session.commit()
        assert snapshot_retention.get_keep_runs() == snapshot_retention.MIN_KEEP_RUNS

    def test_automatic_cleanup_waits_when_backups_are_on_but_failing(self, db):
        cat = _cat(db)
        _runs(db, 10, cat)
        s = db_backup.get_settings()
        s.enabled = True
        _db.session.commit()
        res = snapshot_retention.prune(keep_runs=5, require_recent_backup=True)
        assert "skipped" in res and _db.session.query(StaffingSnapshot).count() == 10
        _db.session.add(BackupRun(filename="a.sql", status="success", triggered_by="schedule",
                                  started_at=now_ist() - timedelta(hours=3)))
        _db.session.commit()
        assert snapshot_retention.prune(keep_runs=5, require_recent_backup=True)["deleted_staffing"] == 5

    def test_automatic_cleanup_runs_when_backups_are_off(self, db):
        cat = _cat(db)
        _runs(db, 8, cat)
        s = db_backup.get_settings()
        s.enabled = False
        _db.session.commit()
        assert snapshot_retention.prune(keep_runs=5, require_recent_backup=True)["deleted_staffing"] == 3

    def test_admin_save_and_cleanup(self, client, db):
        admin = _admin(db)
        cat = _cat(db)
        _runs(db, 12, cat)
        login(client, admin.email)
        r = client.post("/admin/backups/history-settings", data={"keep_runs": "2"}, follow_redirects=True)
        assert b"Enter a whole number" in r.data
        client.post("/admin/backups/history-settings", data={"keep_runs": "6"})
        assert snapshot_retention.get_keep_runs() == 6
        client.post("/admin/backups/history-cleanup")
        assert _db.session.query(StaffingSnapshot).count() == 6
        assert b"Staffing history" in client.get("/admin/backups").data

    def test_only_super_admin(self, client, db, initiator):
        login(client, initiator.email)
        assert client.post("/admin/backups/history-cleanup").status_code == 403


# ── department mapping ─────────────────────────────────────────────────────────

class TestDepartmentMapLogic:
    def test_empty_table_falls_back_to_built_in_set(self, db):
        assert headcount.load_department_map()["batching"] == "Batchers/Production Officer"

    def test_table_rows_replace_the_built_in_set(self, db):
        tech = _cat(db, "Technical")
        db.session.add(DepartmentCategoryMap(department="Civil Works", department_norm="civil works",
                                             norm_role_category_id=tech.id))
        db.session.commit()
        m = headcount.load_department_map()
        assert m == {"civil works": "Technical"}

    def test_inactive_rows_are_not_used(self, db):
        tech = _cat(db, "Technical")
        db.session.add(DepartmentCategoryMap(department="Civil Works", department_norm="civil works",
                                             norm_role_category_id=tech.id, is_active=False))
        db.session.commit()
        assert headcount.load_department_map() == {}

    def test_classify_uses_the_given_map(self, db):
        ids = {"Technical": 11, "Materials": 12}
        m = {"civil works": "Technical", "stores": "Materials"}
        assert headcount._classify_by_department("  Civil  Works ", "Engineer", "", ids, m) == 11
        assert headcount._classify_by_department("Stores", "Helper", "", ids, m) == 12
        assert headcount._classify_by_department("Batching", "Operator", "", ids, m) is None

    def test_trainee_in_a_technical_department_is_not_counted_yet(self, db):
        ids = {"Technical": 11}
        m = {"civil works": "Technical"}
        assert headcount._classify_by_department("Civil Works", "Trainee Engineer", "", ids, m) is None

    def test_default_behaviour_unchanged_without_a_map(self, db):
        ids = {"Technical": 11, "Assistant": 13}
        assert headcount._classify_by_department("Plant & Technical", "Trainee Engineer", "", ids) is None
        assert headcount._classify_by_department("Assistant/RMX", "Trainee RMX Technician", "", ids) == 13


class TestDepartmentMapPage:
    def _setup(self, db):
        for n in ("Plant Manager", "Technical", "Batchers/Production Officer", "Materials", "Assistant", "Operations"):
            _cat(db, n)
        db.session.commit()

    def test_first_visit_fills_in_the_starting_set(self, client, db):
        self._setup(db)
        admin = _admin(db)
        login(client, admin.email)
        r = client.get("/admin/department-mapping")
        assert r.status_code == 200 and b"Batching" in r.data
        assert DepartmentCategoryMap.query.count() == len(headcount._DEPARTMENT_TO_CATEGORY)

    def test_add_update_remove(self, client, db):
        self._setup(db)
        admin = _admin(db, "m-admin2@t.com")
        login(client, admin.email)
        client.get("/admin/department-mapping")
        mat = NormRoleCategory.query.filter_by(name="Materials").first()
        client.post("/admin/department-mapping/add", data={"department": "  Store  Keeping ", "category_id": mat.id})
        row = DepartmentCategoryMap.query.filter_by(department_norm="store keeping").one()
        assert row.norm_role_category.name == "Materials"
        # duplicate (different capitals / spacing) refused
        r = client.post("/admin/department-mapping/add", data={"department": "STORE keeping", "category_id": mat.id},
                        follow_redirects=True)
        assert b"already in the list" in r.data
        tech = NormRoleCategory.query.filter_by(name="Technical").first()
        client.post(f"/admin/department-mapping/{row.id}/update", data={"category_id": tech.id})
        row = _db.session.get(DepartmentCategoryMap, row.id)
        assert row.norm_role_category.name == "Technical" and row.is_active is False   # unticked box = off
        client.post(f"/admin/department-mapping/{row.id}/delete")
        assert DepartmentCategoryMap.query.filter_by(department_norm="store keeping").first() is None
        assert AuditLog.query.filter(AuditLog.action_type.like("DEPARTMENT_MAP_%")).count() >= 3

    def test_trainee_civil_cannot_be_remapped(self, client, db):
        self._setup(db)
        admin = _admin(db, "m-admin3@t.com")
        login(client, admin.email)
        tech = NormRoleCategory.query.filter_by(name="Technical").first()
        r = client.post("/admin/department-mapping/add", data={"department": "Trainee Civil", "category_id": tech.id},
                        follow_redirects=True)
        assert b"built-in rule" in r.data

    def test_lists_departments_nobody_maps(self, client, db):
        self._setup(db)
        admin = _admin(db, "m-admin4@t.com")
        db.session.add(EmployeeLocationSnapshot(source=ExternalDesignationSource.ZINGHR, employee_code="U1",
                                                department="Quality Lab", computed_at=now_ist()))
        db.session.commit()
        login(client, admin.email)
        assert b"Quality Lab" in client.get("/admin/department-mapping").data

    def test_only_super_admin(self, client, db, hr_manager):
        login(client, hr_manager.email)
        assert client.get("/admin/department-mapping").status_code == 403


# ── staffing norms pages ───────────────────────────────────────────────────────

class TestStaffingNormsPages:
    def test_overview_shows_brackets_and_limits(self, client, db):
        admin = _admin(db, "n-admin@t.com")
        cat = _cat(db, "Technical")
        t = _tier(db, "3000-5000 m3", 3000, 5000)
        db.session.add_all([NormRequirement(tier_id=t.id, norm_role_category_id=cat.id,
                                            requirement_type=NormRequirementType.FIXED, fixed_count=1),
                            NormRequirement(tier_id=t.id, norm_role_category_id=cat.id,
                                            requirement_type=NormRequirementType.RATE_PER_VOLUME,
                                            rate_per_unit=1, unit_volume=900)])
        db.session.commit()
        login(client, admin.email)
        text = client.get("/admin/staffing-norms").get_data(as_text=True)
        assert "3000-5000 m3" in text and "Fixed 1 + 1 per 900" in text

    def test_add_a_bracket_with_limits(self, client, db):
        admin = _admin(db, "n-admin2@t.com")
        cat = _cat(db, "Technical")
        db.session.commit()
        login(client, admin.email)
        r = client.post("/admin/staffing-norms/tier/new", data={
            "sheet": "SHEET1", "scope": "PLANT", "tier_label": "Under 1500", "min_value": "", "max_value": "1500",
            "is_active": "1", f"n{cat.id}_type": "FIXED", f"n{cat.id}_fixed": "2"})
        assert r.status_code == 302
        t = NormTier.query.filter_by(tier_label="Under 1500").one()
        assert t.min_value is None and t.max_value == 1500
        req = NormRequirement.query.filter_by(tier_id=t.id).one()
        assert req.requirement_type == NormRequirementType.FIXED and req.fixed_count == 2

    def test_overlapping_bracket_is_refused(self, client, db):
        admin = _admin(db, "n-admin3@t.com")
        _tier(db, "1500-3000", 1500, 3000)
        db.session.commit()
        login(client, admin.email)
        r = client.post("/admin/staffing-norms/tier/new", data={
            "sheet": "SHEET1", "scope": "PLANT", "tier_label": "Bad", "min_value": "2000", "max_value": "4000",
            "is_active": "1"})
        assert r.status_code == 200 and b"overlaps" in r.data
        assert NormTier.query.filter_by(tier_label="Bad").first() is None

    def test_touching_brackets_are_fine_and_inactive_ones_ignored(self, client, db):
        admin = _admin(db, "n-admin4@t.com")
        _tier(db, "1500-3000", 1500, 3000)
        _tier(db, "old", 3000, 9000, active=False)
        db.session.commit()
        login(client, admin.email)
        r = client.post("/admin/staffing-norms/tier/new", data={
            "sheet": "SHEET1", "scope": "PLANT", "tier_label": "3000-5000", "min_value": "3000", "max_value": "5000",
            "is_active": "1"})
        assert r.status_code == 302

    def test_edit_changes_and_removes_limits_and_audits(self, client, db):
        admin = _admin(db, "n-admin5@t.com")
        cat = _cat(db, "Materials")
        t = _tier(db, "Mid", 1000, 2000)
        req = NormRequirement(tier_id=t.id, norm_role_category_id=cat.id,
                              requirement_type=NormRequirementType.FIXED, fixed_count=1)
        db.session.add(req)
        db.session.commit()
        login(client, admin.email)
        assert b"Materials" in client.get(f"/admin/staffing-norms/tier/{t.id}").data
        client.post(f"/admin/staffing-norms/tier/{t.id}", data={
            "tier_label": "Mid", "min_value": "1000", "max_value": "2000", "is_active": "1",
            f"r{req.id}_type": "RATE_PER_VOLUME", f"r{req.id}_rate": "1", f"r{req.id}_unit": "800"})
        req = _db.session.get(NormRequirement, req.id)
        assert req.requirement_type == NormRequirementType.RATE_PER_VOLUME and req.unit_volume == 800
        log = AuditLog.query.filter_by(action_type="NORM_TIER_UPDATED").one()
        assert "Fixed 1" in log.detail and "800" in log.detail
        client.post(f"/admin/staffing-norms/tier/{t.id}", data={
            "tier_label": "Mid", "min_value": "1000", "max_value": "2000", "is_active": "1", f"r{req.id}_del": "1",
            f"r{req.id}_type": "RATE_PER_VOLUME"})
        assert NormRequirement.query.filter_by(tier_id=t.id).count() == 0

    @pytest.mark.parametrize("extra,msg", [
        ({"max_value": "10", "min_value": "20"}, b"smaller"),
        ({"tier_label": ""}, b"Give the bracket a name"),
        ({"min_value": "abc"}, b"must be numbers"),
    ])
    def test_bad_bracket_input(self, client, db, extra, msg):
        admin = _admin(db, "n-admin6@t.com")
        login(client, admin.email)
        data = {"sheet": "SHEET1", "scope": "PLANT", "tier_label": "X", "is_active": "1", **extra}
        r = client.post("/admin/staffing-norms/tier/new", data=data)
        assert r.status_code == 200 and msg in r.data

    def test_bad_limit_numbers(self, client, db):
        admin = _admin(db, "n-admin7@t.com")
        cat = _cat(db, "Technical")
        db.session.commit()
        login(client, admin.email)
        r = client.post("/admin/staffing-norms/tier/new", data={
            "sheet": "SHEET1", "scope": "PLANT", "tier_label": "X", "is_active": "1",
            f"n{cat.id}_type": "RATE_PER_VOLUME", f"n{cat.id}_rate": "1", f"n{cat.id}_unit": "0"})
        assert r.status_code == 200 and b"both numbers" in r.data
        assert NormTier.query.filter_by(tier_label="X").first() is None

    def test_category_add_edit(self, client, db):
        admin = _admin(db, "n-admin8@t.com")
        login(client, admin.email)
        client.post("/admin/staffing-norms/category/new", data={"name": "Quality", "scope": "PLANT",
                                                                  "sheet": "SHEET1", "is_capacity_gated": "1",
                                                                  "is_active": "1"})
        c = NormRoleCategory.query.filter_by(name="Quality").one()
        assert c.is_capacity_gated and c.is_active
        client.post(f"/admin/staffing-norms/category/{c.id}", data={"name": "Quality Lab"})
        c = _db.session.get(NormRoleCategory, c.id)
        assert c.name == "Quality Lab" and not c.is_capacity_gated and not c.is_active
        r = client.post("/admin/staffing-norms/category/new", data={"name": "quality lab", "scope": "PLANT",
                                                                      "sheet": "SHEET1"})
        assert b"already exists" in r.data

    def test_only_super_admin(self, client, db, head_hr):
        login(client, head_hr.email)
        assert client.get("/admin/staffing-norms").status_code == 403
        assert client.post("/admin/staffing-norms/tier/new", data={}).status_code == 403


# ── blank drafts ───────────────────────────────────────────────────────────────

class TestBlankDrafts:
    def test_opening_the_form_twice_reuses_one_draft(self, client, db, initiator):
        login(client, initiator.email)
        a = client.get("/requests/new", follow_redirects=False)
        b = client.get("/requests/new", follow_redirects=False)
        assert a.status_code == 302 and a.location == b.location
        assert OnboardingRequest.query.filter_by(initiated_by=initiator.id).count() == 1

    def test_a_draft_with_data_is_left_alone_and_a_new_one_made(self, client, db, initiator):
        login(client, initiator.email)
        first = client.get("/requests/new", follow_redirects=False).location
        token = re.search(r"token=([0-9a-f]+)", first).group(1)
        req = OnboardingRequest.query.filter_by(public_token=token).one()
        req.form_data = {"associate_name": "Asha"}
        db.session.commit()
        second = client.get("/requests/new", follow_redirects=False).location
        assert second != first
        assert OnboardingRequest.query.filter_by(initiated_by=initiator.id, is_deleted=False).count() == 2

    def test_extra_blank_drafts_are_cleared_when_the_form_opens(self, client, db, initiator):
        for _ in range(3):
            db.session.add(OnboardingRequest(initiated_by=initiator.id, status=RequestStatus.DRAFT,
                                             public_token=uuid.uuid4().hex))
        db.session.commit()
        login(client, initiator.email)
        client.get("/requests/new")
        assert OnboardingRequest.query.filter_by(initiated_by=initiator.id, is_deleted=False).count() == 1

    def test_stale_blank_drafts_are_tidied_from_the_dashboard(self, client, db, initiator):
        old = OnboardingRequest(initiated_by=initiator.id, status=RequestStatus.DRAFT, public_token=uuid.uuid4().hex)
        fresh = OnboardingRequest(initiated_by=initiator.id, status=RequestStatus.DRAFT, public_token=uuid.uuid4().hex)
        db.session.add_all([old, fresh])
        db.session.commit()
        old.updated_at = now_ist() - timedelta(days=3)
        db.session.commit()
        login(client, initiator.email)
        client.get("/dashboard")
        db.session.refresh(old)
        db.session.refresh(fresh)
        assert old.is_deleted is True and fresh.is_deleted is False


# ── request search ─────────────────────────────────────────────────────────────

def _req(db, user, name, status=RequestStatus.PENDING_BH, company="RDC", desig="Field Tech", plant="Plant One"):
    r = OnboardingRequest(initiated_by=user.id, status=status, public_token=uuid.uuid4().hex, candidate_name=name,
                          company_code=company, designation=desig, plant_location=plant)
    db.session.add(r)
    db.session.commit()
    return r


class TestRequestSearch:
    def test_initiator_dashboard_search(self, client, db, initiator):
        _req(db, initiator, "Asha Rao")
        _req(db, initiator, "Bimal Das", desig="Driver")
        login(client, initiator.email)
        text = client.get("/dashboard?q=asha").get_data(as_text=True)
        assert "Asha Rao" in text and "Bimal Das" not in text
        text = client.get("/dashboard?q=driver").get_data(as_text=True)
        assert "Bimal Das" in text and "Asha Rao" not in text

    def test_search_by_request_number_and_wildcards_are_literal(self, client, db, initiator):
        a = _req(db, initiator, "Asha Rao")
        _req(db, initiator, "Bimal Das")
        login(client, initiator.email)
        assert "Bimal Das" not in client.get(f"/dashboard?q=%23{a.id}").get_data(as_text=True)
        assert "Asha Rao" not in client.get("/dashboard?q=%25").get_data(as_text=True)   # a lone "%" is not "match all"

    def test_search_keeps_other_filters_in_the_box(self, client, db, initiator):
        _req(db, initiator, "Asha Rao")
        login(client, initiator.email)
        text = client.get("/dashboard?status=PENDING_BH&q=asha").get_data(as_text=True)
        assert 'name="status" value="PENDING_BH"' in text and 'value="asha"' in text

    def test_approver_dashboard_search_and_card_count(self, client, db):
        init, bh, _, req = _world(db, "S1")
        req.candidate_name = "Zed Zimmer"
        _req(db, init, "Other Person")
        login(client, bh.email)
        text = client.get("/dashboard?q=zed").get_data(as_text=True)
        assert "Zed Zimmer" in text and "Other Person" not in text
        # the "Pending" summary card still counts the whole waiting queue
        both = client.get("/dashboard").get_data(as_text=True)
        assert 'data-count="2"' in both and 'data-count="2"' in text

    def test_admin_list_search(self, client, db, initiator):
        admin = _admin(db, "s-admin@t.com")
        _req(db, initiator, "Asha Rao", plant="Pune")
        _req(db, initiator, "Bimal Das", plant="Nagpur")
        login(client, admin.email)
        text = client.get("/admin/requests?q=nagpur").get_data(as_text=True)
        assert "Bimal Das" in text and "Asha Rao" not in text
        text = client.get("/admin/requests?status=PENDING_BH&q=asha").get_data(as_text=True)
        assert "Asha Rao" in text and "Bimal Das" not in text


# ── button colour ──────────────────────────────────────────────────────────────

def _lum(hexv):
    hexv = hexv.lstrip("#")
    c = [int(hexv[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


class TestButtonContrast:
    def test_button_fill_meets_wcag_aa_against_white(self, app):
        css = open(app.root_path + "/static/css/base.css", encoding="utf-8").read()
        for var in ("--c-btn", "--c-btn-h", "--c-btn-d"):
            hexv = re.search(re.escape(var) + r":\s*(#[0-9a-fA-F]{6})", css).group(1)
            assert 1.05 / (_lum(hexv) + 0.05) >= 4.5, (var, hexv)
        assert "linear-gradient(135deg, var(--c-btn) 0%, var(--c-btn-d) 100%)" in css

    def test_sign_in_pages_use_the_accessible_fill(self, client, app):
        for path in ("/auth/login", "/auth/forgot-password"):
            text = client.get(path).get_data(as_text=True)
            assert "background: #c44d19; color: #fff" in text


# ── sign-out and sessions ──────────────────────────────────────────────────────

def _separate_request():
    """The tests run inside one app context, so Flask's `g` (where flask-login caches the signed-in user)
    would leak from one request to the next. A real second device is a fresh request: clear it."""
    from flask import g
    g.pop("_login_user", None)


class TestSignOut:
    def test_get_does_not_sign_out(self, client, db, initiator):
        login(client, initiator.email)
        r = client.get("/auth/logout", follow_redirects=False)
        assert r.status_code == 302 and "/auth/login" not in r.location
        assert client.get("/dashboard").status_code == 200

    def test_post_signs_out_and_a_copied_cookie_is_dead(self, client, app, db, initiator):
        login(client, initiator.email)
        cookie = client.get_cookie("rdc_session").value
        assert logout(client).status_code == 200
        replay = app.test_client()
        replay.set_cookie("rdc_session", cookie)
        _separate_request()
        r = replay.get("/dashboard", follow_redirects=False)
        assert r.status_code == 302 and "/auth/login" in r.location

    def test_sidebar_uses_a_post_form(self, client, db, initiator):
        login(client, initiator.email)
        text = client.get("/dashboard").get_data(as_text=True)
        assert 'id="logout-form" method="POST"' in text

    def test_changing_password_signs_out_other_devices_not_this_one(self, client, app, db, initiator):
        login(client, initiator.email)
        other = app.test_client()
        _separate_request()
        login(other, initiator.email)
        _separate_request()
        assert other.get("/dashboard").status_code == 200
        _separate_request()
        r = client.post("/profile/", data={"action": "change_password", "current_password": "Test1234",
                                           "new_password": "NewPass#12345", "confirm_password": "NewPass#12345"},
                        follow_redirects=True)
        assert r.status_code == 200
        assert client.get("/dashboard").status_code == 200
        _separate_request()
        gone = other.get("/dashboard", follow_redirects=False)
        assert gone.status_code == 302 and "/auth/login" in gone.location

    def test_admin_password_change_signs_the_user_out(self, client, app, db, initiator):
        admin = _admin(db, "p-admin@t.com")
        login(client, admin.email)
        victim = app.test_client()
        _separate_request()
        login(victim, initiator.email)
        _separate_request()
        client.post(f"/admin/users/{initiator.id}/change-password", data={"new_password": "Another#12345"})
        _separate_request()
        assert victim.get("/dashboard", follow_redirects=False).status_code == 302

    def test_legacy_cookie_without_version_still_works_until_first_bump(self, app, db, initiator):
        from app.models import load_user
        assert load_user(str(initiator.id)).id == initiator.id
        initiator.end_all_sessions()
        db.session.commit()
        assert load_user(str(initiator.id)) is None
        assert load_user(f"{initiator.id}.1").id == initiator.id
        assert load_user("garbage") is None

    def test_email_approval_links_do_not_depend_on_any_session(self, client, app, db):
        init, bh, hrm, req = _world(db, "SO1")
        login(client, bh.email)
        logout(client)                                   # signing out bumps the version...
        url = _link(app, bh, req, "approve")             # ...and the signed email link still works
        assert client.get(url).status_code == 200
        assert client.post(url, data={"remark": "ok"}).status_code in (200, 302)
        assert _status(req) != RequestStatus.PENDING_BH

    def test_secure_cookie_flag_follows_https_setting(self, monkeypatch):
        import importlib
        import app.config as cfg
        try:
            monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
            monkeypatch.setenv("APP_BASE_URL", "http://192.168.0.26:5000")
            assert importlib.reload(cfg).Config.SESSION_COOKIE_SECURE is False
            monkeypatch.setenv("APP_BASE_URL", "https://hiring.example.com")
            assert importlib.reload(cfg).Config.SESSION_COOKIE_SECURE is True
            monkeypatch.delenv("APP_BASE_URL")
            monkeypatch.setenv("SESSION_COOKIE_SECURE", "1")
            assert importlib.reload(cfg).Config.REMEMBER_COOKIE_SECURE is True
        finally:
            monkeypatch.undo()
            importlib.reload(cfg)


# ── qualification rule on a designation ───────────────────────────────────────

class TestQualificationRule:
    def _apprentice(self, db):
        from app.models import Designation
        tech = _cat(db, "Technical", gated=False)
        batch = _cat(db, "Batchers/Production Officer", gated=False)
        d = Designation(name="Technician Apprentice", company="RDC", norm_category_id=batch.id,
                        qualification_keyword="Civil", qualification_norm_category_id=tech.id)
        db.session.add(d)
        db.session.commit()
        return d, tech, batch

    def test_civil_goes_to_technical_everyone_else_to_batching(self, db):
        from app.services.staffing_norms import resolve_norm_category_id as res
        d, tech, batch = self._apprentice(db)
        assert res(d, {"qualification": "Diploma - Civil"}) == tech.id
        assert res(d, {"qualification": "BE/B.Tech - CIVIL"}) == tech.id
        assert res(d, {"qualification": "Diploma - Mechanical"}) == batch.id
        assert res(d, {}) == batch.id
        assert res(None, {"qualification": "Civil"}) is None

    def test_no_rule_means_the_designations_own_category(self, db):
        from app.models import Designation
        from app.services.staffing_norms import resolve_norm_category_id as res
        t = _cat(db, "Technical")
        d = Designation(name="Field Technician", company="RDC", norm_category_id=t.id)
        db.session.add(d)
        db.session.commit()
        assert res(d, {"qualification": "Civil"}) == t.id

    def test_the_gate_uses_the_resolved_category(self, db):
        from app.services import staffing_norms
        self._apprentice(db)
        civil = staffing_norms.check_rdc_staffing_gate({"designation": "Technician Apprentice", "plant_location": "P",
                                                        "qualification": "Diploma - Civil"})
        other = staffing_norms.check_rdc_staffing_gate({"designation": "Technician Apprentice", "plant_location": "P",
                                                        "qualification": "ITI"})
        assert civil["details"]["norm_role_category_name"] == "Technical"
        assert other["details"]["norm_role_category_name"] == "Batchers/Production Officer"

    def test_admin_saves_the_rule_for_rdc_only(self, client, db):
        from app.models import Designation
        admin = _admin(db, "q-admin@t.com")
        tech = _cat(db, "Technical")
        batch = _cat(db, "Batchers/Production Officer")
        db.session.commit()
        login(client, admin.email)
        client.post("/admin/designations/new", data={
            "name": "Apprentice X", "company": "RDC", "notice_period_days": "15", "norm_category_id": batch.id,
            "qualification_keyword": " Civil ", "qualification_norm_category_id": tech.id})
        d = Designation.query.filter_by(name="Apprentice X").one()
        assert (d.qualification_keyword, d.qualification_norm_category_id) == ("Civil", tech.id)
        client.post(f"/admin/designations/{d.id}/edit", data={
            "name": "Apprentice X", "company": "RDC", "notice_period_days": "15", "norm_category_id": batch.id,
            "qualification_keyword": "", "qualification_norm_category_id": tech.id})
        d = _db.session.get(Designation, d.id)
        assert d.qualification_keyword is None and d.qualification_norm_category_id is None
        client.post("/admin/designations/new", data={
            "name": "Apprentice Y", "company": "ROBO", "notice_period_days": "15",
            "qualification_keyword": "Civil", "qualification_norm_category_id": tech.id})
        y = Designation.query.filter_by(name="Apprentice Y").one()
        assert y.qualification_keyword is None


# ── manpower in approval e-mails ──────────────────────────────────────────────

class TestManpowerInApprovalEmail:
    def _plant(self, db, current, fixed=2, gated=True, role="Technical"):
        from unittest.mock import patch
        from app.models import Designation, PlantDvtMapping
        cat = _cat(db, role, gated=gated)
        t = _tier(db, "3000-5000 m3", 3000, 5000)
        db.session.add(NormRequirement(tier_id=t.id, norm_role_category_id=cat.id,
                                       requirement_type=NormRequirementType.FIXED, fixed_count=fixed))
        db.session.add(Designation(name="Field Technician", company="RDC", norm_category_id=cat.id))
        db.session.add(PlantDvtMapping(plant_location_name="Plant One", dvt_plant_code="P1"))
        db.session.flush()
        db.session.add(StaffingSnapshot(scope=NormScope.PLANT, location_key="Plant One",
                                        norm_role_category_id=cat.id, current_headcount=current))
        db.session.commit()
        return patch("app.services.staffing_norms.dvt.get_average_plant_volume", return_value=4000.0)

    FD = {"designation": "Field Technician", "plant_location": "Plant One", "company_code": "RDC"}

    def test_summary_within_the_maximum(self, db):
        from app.services.staffing_norms import manpower_summary
        with self._plant(db, current=1, fixed=3):
            mp = manpower_summary(self.FD)
        assert (mp["role"], mp["location"], mp["current"], mp["allowed"], mp["after"], mp["over"]) == \
               ("Technical", "Plant One", 1, 3, 2, False)
        assert mp["volume"] == 4000.0 and mp["tier"] == "3000-5000 m3"

    def test_summary_when_hiring_goes_over_the_maximum(self, db):
        from app.services.staffing_norms import manpower_summary
        with self._plant(db, current=3, fixed=3):
            mp = manpower_summary(self.FD)
        assert mp["current"] == 3 and mp["allowed"] == 3 and mp["after"] == 4 and mp["over"] is True

    def test_nothing_for_uncovered_or_unlimited_roles(self, db):
        from app.services.staffing_norms import manpower_summary
        assert manpower_summary({"designation": "HR Executive", "plant_location": "X"}) is None
        with self._plant(db, current=1, gated=False):
            assert manpower_summary(self.FD) is None

    def test_a_note_when_figures_are_not_available(self, db):
        from app.models import Designation
        from app.services.staffing_norms import manpower_summary
        cat = _cat(db, "Technical")
        db.session.add(Designation(name="Field Technician", company="RDC", norm_category_id=cat.id))
        db.session.commit()
        mp = manpower_summary(self.FD)     # plant isn't mapped to a volume-tracker plant
        assert "note" in mp and "mapped" in mp["note"]

    def _send(self, db, app, current, fixed, company="RDC"):
        from unittest.mock import patch
        from app import utils
        init, bh, _, req = _world(db, "MP" + str(current) + company)
        req.company_code = company
        req.form_data = dict(self.FD, company_code=company)
        db.session.commit()
        sent = []
        with self._plant(db, current, fixed), \
             patch("app.utils.send_email", lambda subject, rec, body, **kw: sent.append((body, kw.get("html")))), \
             patch("app.utils.app_base_url", return_value="https://hiring.example.com"):
            utils.notify_users(_db, req, [_db.session.get(User, bh.pid)], "New hiring request", "Please review.",
                               actions=True)
        return sent

    def test_the_approval_mail_carries_role_current_and_maximum(self, db, app):
        (text, html), = self._send(db, app, current=2, fixed=4)
        assert "Manpower - Technical at Plant One" in text
        assert "Current: 2   Maximum allowed: 4   After this hire: 3 (within the maximum)" in text
        assert ("<th" in html and "Technical" in html and "Within the maximum" in html
                and html.index("Current") < html.index("Maximum") < html.index("After this hire"))
        assert "Approve" in html            # the buttons are still there, after the table
        assert html.index("After this hire") < html.index(">Approve<")

    def test_the_special_approval_mail_shows_it_is_over(self, db, app):
        (text, html), = self._send(db, app, current=4, fixed=4)
        assert "over the maximum by 1" in text and "Over the maximum by 1" in html

    def test_other_companies_get_no_manpower_block(self, db, app):
        (text, html), = self._send(db, app, current=1, fixed=4, company="ROBO")
        assert "Manpower" not in text and "Manpower" not in html
