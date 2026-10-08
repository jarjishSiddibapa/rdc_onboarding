"""
App-wide busy pill (2026-10-08): one shared indicator ("One sec... pretending this
is very complicated") instead of ad-hoc "Loading…/Sending…/Generating…" label swaps.
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
        assert "One sec... pretending this is very complicated" in js
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
