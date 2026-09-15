"""
Tests for admin routes:
  - User CRUD (create, edit, toggle active)
  - Plant/Designation CRUD
  - Invalid role value is rejected gracefully
"""
import pytest
from app.models import UserRole, User, PlantLocation, Designation
from app.extensions import db as _db
from .conftest import login, _make_user


# ── User Management ────────────────────────────────────────────────────────────

class TestUserCreate:
    def test_create_user_success(self, client, db, app):
        admin = _make_user("Admin", "adminca@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.post("/admin/users/new", data={
                "name":     "New Employee",
                "email":    "newemployee@t.com",
                "password": "Secure99",
                "role":     UserRole.INITIATOR.value,
                "employee_code": "EMP9001",
            }, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            user = User.query.filter_by(email="newemployee@t.com").first()
            assert user is not None
            assert user.role == UserRole.INITIATOR

    def test_duplicate_email_rejected(self, client, db, app):
        admin    = _make_user("Admin2", "admin2ca@t.com", UserRole.SUPER_ADMIN, db)
        existing = _make_user("Exist",  "exist@t.com",    UserRole.INITIATOR, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.post("/admin/users/new", data={
                "name":     "Dup User",
                "email":    existing.email,
                "password": "Secure99",
                "role":     UserRole.INITIATOR.value,
            }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"already" in resp.data.lower() or b"use" in resp.data.lower()

    def test_invalid_role_rejected(self, client, db, app):
        admin = _make_user("Admin3", "admin3ca@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.post("/admin/users/new", data={
                "name":     "Bad Role",
                "email":    "badrole@t.com",
                "password": "Secure99",
                "role":     "HACKER",
            }, follow_redirects=True)
        assert resp.status_code == 200
        # User must NOT have been created
        with app.app_context():
            user = User.query.filter_by(email="badrole@t.com").first()
            assert user is None

    def test_missing_fields_rejected(self, client, db, app):
        admin = _make_user("Admin4", "admin4ca@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.post("/admin/users/new", data={
                "name": "",
                "email": "",
                "password": "",
                "role": UserRole.INITIATOR.value,
            }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"required" in resp.data.lower()


class TestUserEdit:
    def test_edit_role_invalid_value_rejected(self, client, db, app):
        admin = _make_user("Admin5", "admin5ca@t.com", UserRole.SUPER_ADMIN, db)
        target = _make_user("Target", "target@t.com", UserRole.INITIATOR, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.post(f"/admin/users/{target.id}/edit", data={
                "name":  target.name,
                "email": target.email,
                "role":  "SUPER_VILLAIN",
            }, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            user = _db.session.get(User, target.id)
            # Role must be unchanged
            assert user.role == UserRole.INITIATOR

    def test_toggle_active_deactivates_user(self, client, db, app):
        admin  = _make_user("Admin6", "admin6ca@t.com", UserRole.SUPER_ADMIN, db)
        target = _make_user("Tog",    "tog@t.com",      UserRole.INITIATOR, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.post(f"/admin/users/{target.id}/toggle-active",
                               follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            user = _db.session.get(User, target.id)
            assert user.is_active is False

    def test_cannot_deactivate_self(self, client, db, app):
        admin = _make_user("Admin7", "admin7ca@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.post(f"/admin/users/{admin.id}/toggle-active",
                               follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            user = _db.session.get(User, admin.id)
            assert user.is_active is True


# ── Plant Locations ────────────────────────────────────────────────────────────

class TestPlantCRUD:
    def test_create_plant(self, client, db, app):
        admin = _make_user("Admin8", "admin8ca@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.post("/admin/plants/new", data={
                "name": "Mumbai Plant"
            }, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            plant = PlantLocation.query.filter_by(name="Mumbai Plant").first()
            assert plant is not None

    def test_create_plant_empty_name_rejected(self, client, db, app):
        admin = _make_user("Admin9", "admin9ca@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.post("/admin/plants/new", data={"name": ""},
                               follow_redirects=True)
        assert resp.status_code == 200
        assert b"required" in resp.data.lower()

    def test_delete_plant_soft_deletes(self, client, db, app):
        admin = _make_user("Admin10", "admin10ca@t.com", UserRole.SUPER_ADMIN, db)
        plant = PlantLocation(name="Delete Me", sort_order=999)
        db.session.add(plant)
        db.session.commit()
        plant_id = plant.id
        with app.app_context():
            login(client, admin.email)
            resp = client.post(f"/admin/plants/{plant_id}/delete",
                               follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            p = _db.session.get(PlantLocation, plant_id)
            assert p.is_deleted is True


class TestPlantCompanyScoping:
    """
    Regression coverage for the 2026-09-15 multi-company fix: PlantLocation
    now has a `company` column (default "RDC", backfilling all pre-existing
    rows correctly since they're all RDC concrete plants) so Ultrafine/ROBO
    can have their own separate plant lists, managed from the same
    /admin/plants page via a company selector.
    """

    def test_new_plant_defaults_to_rdc_when_company_omitted(self, client, db, app):
        admin = _make_user("AdminCo1", "adminco1@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            client.post("/admin/plants/new", data={"name": "No Company Given Plant"},
                        follow_redirects=True)
        with app.app_context():
            plant = PlantLocation.query.filter_by(name="No Company Given Plant").first()
            assert plant.company == "RDC"

    def test_create_plant_under_ultrafine(self, client, db, app):
        admin = _make_user("AdminCo2", "adminco2@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            client.post("/admin/plants/new", data={"name": "UF Plant One", "company": "Ultrafine"},
                        follow_redirects=True)
        with app.app_context():
            plant = PlantLocation.query.filter_by(name="UF Plant One").first()
            assert plant.company == "Ultrafine"

    def test_invalid_company_falls_back_to_rdc(self, client, db, app):
        admin = _make_user("AdminCo3", "adminco3@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            client.post("/admin/plants/new", data={"name": "Bad Company Plant", "company": "NotARealCompany"},
                        follow_redirects=True)
        with app.app_context():
            plant = PlantLocation.query.filter_by(name="Bad Company Plant").first()
            assert plant.company == "RDC"

    def test_plants_list_only_shows_selected_company(self, client, db, app):
        admin = _make_user("AdminCo4", "adminco4@t.com", UserRole.SUPER_ADMIN, db)
        db.session.add_all([
            PlantLocation(name="RDC Only Plant", company="RDC"),
            PlantLocation(name="ROBO Only Plant", company="ROBO"),
        ])
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            rdc_resp  = client.get("/admin/plants?company=RDC")
            robo_resp = client.get("/admin/plants?company=ROBO")
        rdc_html  = rdc_resp.get_data(as_text=True)
        robo_html = robo_resp.get_data(as_text=True)
        assert "RDC Only Plant" in rdc_html
        assert "ROBO Only Plant" not in rdc_html
        assert "ROBO Only Plant" in robo_html
        assert "RDC Only Plant" not in robo_html

    def test_edit_plant_can_change_company(self, client, db, app):
        admin = _make_user("AdminCo5", "adminco5@t.com", UserRole.SUPER_ADMIN, db)
        plant = PlantLocation(name="Movable Plant", company="RDC")
        db.session.add(plant)
        db.session.commit()
        plant_id = plant.id
        with app.app_context():
            login(client, admin.email)
            client.post(f"/admin/plants/{plant_id}/edit",
                        data={"name": "Movable Plant", "company": "Ultrafine"},
                        follow_redirects=True)
        with app.app_context():
            p = _db.session.get(PlantLocation, plant_id)
            assert p.company == "Ultrafine"


# ── Designations ────────────────────────────────────────────────────────────────

class TestDesignationCRUD:
    def test_create_designation(self, client, db, app):
        admin = _make_user("Admin11", "admin11ca@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.post("/admin/designations/new", data={
                "name": "Senior Engineer",
                "notice_period_days": "30",
            }, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            desig = Designation.query.filter_by(name="Senior Engineer").first()
            assert desig is not None

    def test_create_designation_empty_name_rejected(self, client, db, app):
        admin = _make_user("Admin12", "admin12ca@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.post("/admin/designations/new", data={
                "name": "",
                "notice_period_days": "30",
            }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"required" in resp.data.lower()

    def test_delete_designation_soft_deletes(self, client, db, app):
        admin = _make_user("Admin13", "admin13ca@t.com", UserRole.SUPER_ADMIN, db)
        desig = Designation(name="Delete Me Desig", notice_period_days=30, sort_order=999)
        db.session.add(desig)
        db.session.commit()
        desig_id = desig.id
        with app.app_context():
            login(client, admin.email)
            resp = client.post(f"/admin/designations/{desig_id}/delete",
                               follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            d = _db.session.get(Designation, desig_id)
            assert d.is_deleted is True


# ── Instant list search (added 2026-09-11, "these things shall have a search
#    bar right?" — Designations/Plant Locations/Form Fields had no way to
#    find one row in a long list) ──────────────────────────────────────────────

class TestListSearchUI:
    """
    Every admin config list (Designations, Plant Locations, Form Fields,
    Users, Plant/Cluster Mappings) renders the same render_list_search()
    macro (_macros.html), wired by one shared listener in base.html. These
    just confirm the search input is actually present and correctly wired
    on each page — the filtering behavior itself is plain DOM JS with no
    server round-trip, so it isn't something pytest can exercise directly;
    that was verified live in the browser (Designations, Plant Locations,
    Form Fields all confirmed instant-filtering correctly, including Plant
    Locations finding matches across its full ~200-row list after
    plants_list() was switched from paginated to a full unpaginated list —
    matching every other config page — specifically so search can find a
    match regardless of which page it used to fall on).
    """

    def test_designations_page_has_search_input(self, client, db, app):
        admin = _make_user("AdminSearch1", "adminsearch1@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.get("/admin/designations")
        html = resp.get_data(as_text=True)
        assert 'data-search-rows="#dtab-panel-active tbody tr, #dtab-panel-inactive tbody tr"' in html

    def test_plants_page_has_search_input_and_is_not_paginated(self, client, db, app):
        admin = _make_user("AdminSearch2", "adminsearch2@t.com", UserRole.SUPER_ADMIN, db)
        for i in range(3):
            db.session.add(PlantLocation(name=f"Search Test Plant {i}", sort_order=i))
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.get("/admin/plants")
        html = resp.get_data(as_text=True)
        assert 'data-search-rows="#ptab-panel-active tbody tr, #ptab-panel-inactive tbody tr"' in html
        # All 3 seeded plants must appear in one unpaginated response, or
        # search would only ever be able to find whatever page_a happened
        # to land on.
        for i in range(3):
            assert f"Search Test Plant {i}" in html

    def test_form_fields_page_has_search_input(self, client, db, app):
        admin = _make_user("AdminSearch3", "adminsearch3@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.get("/admin/form-fields")
        html = resp.get_data(as_text=True)
        assert 'data-search-rows=".sortable-body tr"' in html

    def test_users_page_has_search_input(self, client, db, app):
        admin = _make_user("AdminSearch4", "adminsearch4@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.get("/admin/users")
        html = resp.get_data(as_text=True)
        assert 'data-search-rows="#utab-panel-active tbody tr, #utab-panel-inactive tbody tr"' in html


class TestFormFieldsDragToReorder:
    """
    Regression coverage for a real bug found 2026-09-11 ("also this drag to
    reorder doesn't work"): TWO independent, stacked bugs on the Form Fields
    admin page meant drag-to-reorder never worked at all, in any environment:

    1. SortableJS was loaded from an external CDN
       (cdn.jsdelivr.net/npm/sortablejs), but the app's own CSP
       (`script-src 'self' 'unsafe-inline'` — see app/__init__.py's
       add_security_headers()) blocks every external script host. The
       script silently failed to load ("Sortable is not defined" in the
       console), confirmed live in the browser before the fix. Fixed by
       vendoring the library locally under app/static/js/Sortable.min.js
       and loading it via url_for('static', ...) — satisfies the existing
       CSP unmodified, no external network dependency, no CSP loosening.
    2. Even with the library loading, `Sortable.create(tbody, {handle:
       'tr', ...})` was backwards: SortableJS's `handle` option restricts
       drag-START to a DESCENDANT of each sortable item matching that
       selector — no <tr> ever contains a nested <tr>, so this selector
       matched nothing inside any row and dragging could never begin
       anywhere. Fixed by removing `handle` entirely, which makes the
       whole row the drag target (the originally intended behavior, per
       the removed comment "drag the whole row").

    Both were confirmed fixed by an actual mouse drag-and-drop in the
    browser (Father Name row dragged below Date of Birth, "Order changed"
    banner appeared correctly) — this test only confirms the static/config
    half that pytest can actually exercise: the vendored script tag is
    used instead of the CDN one, and `handle: 'tr'` is gone.
    """

    def test_uses_vendored_sortable_not_external_cdn(self, client, db, app):
        admin = _make_user("AdminDrag1", "admindrag1@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.get("/admin/form-fields")
        html = resp.get_data(as_text=True)
        assert "cdn.jsdelivr.net" not in html
        assert "/static/js/Sortable.min.js" in html

    def test_sortable_handle_option_removed(self, client, db, app):
        admin = _make_user("AdminDrag2", "admindrag2@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.get("/admin/form-fields")
        html = resp.get_data(as_text=True)
        assert "handle: 'tr'" not in html
        assert "handle:'tr'" not in html


class TestNoExternalCdnDependency:
    """
    Regression coverage for 2026-09-11's "no dependency on CDN, avoid
    runtime issues" follow-up: after SortableJS turned out to be silently
    broken by the app's own CSP (see TestFormFieldsDragToReorder above),
    the Inter font — the last remaining external dependency, loaded from
    fonts.googleapis.com/fonts.gstatic.com on every single page — was
    self-hosted too (app/static/fonts/*.woff2 + app/static/css/inter-font.css),
    and the CSP's now-unnecessary style-src/font-src allowances for those
    two hosts were removed. The app should load zero external hosts, full
    stop — these checks span a login page (public, no auth) and an admin
    page (behind auth, uses base.html) so both template families are
    covered.
    """

    def test_login_page_has_no_external_font_or_cdn_links(self, client):
        resp = client.get("/auth/login")
        html = resp.get_data(as_text=True)
        assert "fonts.googleapis.com" not in html
        assert "fonts.gstatic.com" not in html
        assert "cdn.jsdelivr.net" not in html
        assert "css/inter-font.css" in html

    def test_admin_page_has_no_external_font_or_cdn_links(self, client, db, app):
        admin = _make_user("AdminNoCdn1", "adminnocdn1@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.get("/admin/designations")
        html = resp.get_data(as_text=True)
        assert "fonts.googleapis.com" not in html
        assert "fonts.gstatic.com" not in html
        assert "cdn.jsdelivr.net" not in html

    def test_csp_no_longer_allows_external_font_hosts(self, client):
        resp = client.get("/auth/login")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "fonts.googleapis.com" not in csp
        assert "fonts.gstatic.com" not in csp
        assert "style-src 'self' 'unsafe-inline';" in csp
        assert "font-src 'self';" in csp

    def test_vendored_font_css_is_served_locally(self, client):
        resp = client.get("/static/css/inter-font.css")
        assert resp.status_code == 200
        css = resp.get_data(as_text=True)
        # The file's own header comment explains it replaced Google Fonts,
        # so it legitimately mentions that hostname in prose — what matters
        # is that no @font-face actually points at an external URL.
        assert "url('https://" not in css
        assert "url(https://" not in css
        assert "../fonts/inter-400.woff2" in css

    def test_vendored_sortable_js_is_served_locally(self, client):
        resp = client.get("/static/js/Sortable.min.js")
        assert resp.status_code == 200
        assert b"Sortable" in resp.data
