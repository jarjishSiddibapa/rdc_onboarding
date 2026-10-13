"""
App-wide busy pill (2026-10-08): one shared indicator ("Loading...") instead of ad-hoc "Loading…/Sending…/Generating…" label swaps.
"""
import os
import re

from app.models import UserRole
from .conftest import login, _make_user

STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "static")
TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "templates")


class TestBusyAssetsServed:
    def test_message_lives_in_one_place(self):
        js = open(os.path.join(STATIC, "js", "busy.js"), encoding="utf-8").read()
        assert "var MSG = 'Loading...';" in js
        assert "pretending" not in js
        assert "window.Busy" in js

    def test_static_files_are_served(self, client):
        assert client.get("/static/js/busy.js").status_code == 200
        assert client.get("/static/css/busy.css").status_code == 200


class TestBusyIncludedOnPages:
    def test_app_pages_load_pill(self, client, db, app):
        user = _make_user("BusyU1", "busyu1@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, user.email)
            html = client.get("/admin/designations").get_data(as_text=True)
        assert "js/busy.js" in html and "css/busy.css" in html
        # must run before anything else calls fetch()
        assert html.index("js/busy.js") < html.index("js/base.js")

    def test_auth_pages_load_pill(self, client):
        for path in ("/auth/login", "/auth/forgot-password"):
            html = client.get(path).get_data(as_text=True)
            assert "js/busy.js" in html and "css/busy.css" in html, path


class TestNoGenericLoadingLabelsLeft:
    LABELS = ("'Sending…'", "'Generating…'", "'Checking...'", "'Saving…'", "'Searching Truein…'")

    def test_templates_no_longer_swap_generic_labels(self):
        offenders = []
        for root, _, files in os.walk(TEMPLATES):
            for f in files:
                if f.endswith(".html"):
                    text = open(os.path.join(root, f), encoding="utf-8").read()
                    for label in self.LABELS:
                        if label in text:
                            offenders.append((f, label))
        assert not offenders, offenders

    def test_background_pollers_opt_out(self):
        base = open(os.path.join(TEMPLATES, "base.html"), encoding="utf-8").read()
        sync = open(os.path.join(TEMPLATES, "_sync_now_widget.html"), encoding="utf-8").read()
        assert re.search(r"unread_count'\)\s*\}\}\"\s*,\s*\{busy: false\}", base)
        assert "fetch(STATUS_URL, {busy: false})" in sync


class TestAboutPage:
    def test_about_requires_login(self, client):
        assert client.get("/about").status_code in (301, 302)

    def test_about_credits(self, client, db, app):
        user = _make_user("AboutU1", "aboutu1@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, user.email)
            html = client.get("/about").get_data(as_text=True)
            dash = client.get("/admin/designations").get_data(as_text=True)
        for name in ("Jarjish Siddibapa", "Developer", "Rutuja Gaikwad", "HR Manager", "Vimal Sethi", "Head HR", "Suresh Kumar", "Dr. K. S. Bhoon"):
            assert name in html, name
        assert "for guiding us throughout the process. His mentorship, patience and trust shaped this app at every step." in html
        # the five tribute lines are written in the third person
        assert "Your " not in html and "Thank you, " not in html
        assert 'href="/about"' in dash
        for img in ("rdc-logo-onblue", "gptw-certified-2026", "gptw-mid-size-2026", "gptw-cement-2026", "gptw-women-2026"):
            assert f"img/about/{img}.png" in html
            assert client.get(f"/static/img/about/{img}.png").status_code == 200


class TestAdminContactEmail:
    def test_helpdesk_address_is_one_constant(self):
        from app.utils import ADMIN_CONTACT_EMAIL
        assert ADMIN_CONTACT_EMAIL == "ithelpdesk@rdc.in"

    def test_403_page_shows_it(self, client, db, app):
        user = _make_user("HelpU1", "helpu1@t.com", UserRole.INITIATOR, db)
        db.session.commit()
        with app.app_context():
            login(client, user.email)
            denied = client.get("/admin/users")          # Reporting Manager -> 403 page
        assert denied.status_code == 403
        assert "ithelpdesk@rdc.in" in denied.get_data(as_text=True)

    def test_no_contact_administrator_message_is_left_without_the_address(self):
        root = os.path.dirname(os.path.dirname(__file__))
        pat = re.compile(r"contact (?:your|an) administrator(?!\s*(?:\(|at))", re.I)
        offenders = []
        for base in (os.path.join(root, "app"),):
            for dp, _dn, files in os.walk(base):
                for f in files:
                    if f.endswith((".py", ".html")):
                        path = os.path.join(dp, f)
                        for n, line in enumerate(open(path, encoding="utf-8"), 1):
                            if pat.search(line):
                                offenders.append((os.path.relpath(path, root), n, line.strip()[:160]))
        assert not offenders, offenders
