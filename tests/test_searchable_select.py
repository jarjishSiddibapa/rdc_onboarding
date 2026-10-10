"""Every dropdown is a type-to-filter dropdown (js/searchable-select.js), loaded on every app page."""
import pathlib

from .conftest import login, _make_user
from app.models import UserRole

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = (ROOT / "app/static/js/searchable-select.js").read_text(encoding="utf-8")


class TestSearchableSelectAssets:
    def test_script_is_loaded_by_the_shared_layout(self):
        base = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
        assert "js/searchable-select.js" in base
        assert base.index("js/base.js") < base.index("js/searchable-select.js")

    def test_keeps_the_real_select_as_source_of_truth(self):
        # programmatic value changes, rebuilt options and change events must keep working
        for needle in ("defineProperty(select, 'value'", "MutationObserver", "new Event('change'", "data-no-search",
                       "select.multiple"):
            assert needle in JS, needle

    def test_self_hosted_no_external_host(self):
        assert "http://" not in JS and "https://" not in JS

    def test_panel_styles_exist(self):
        css = (ROOT / "app/static/css/base.css").read_text(encoding="utf-8")
        for cls in (".ss-trigger", ".ss-panel", ".ss-search", ".ss-opt.ss-active", ".ss-native"):
            assert cls in css, cls


class TestPagesWithDropdownsLoadIt:
    def test_page_with_selects_serves_the_script(self, client, db):
        _make_user("SS Admin", "ssadmin@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        login(client, "ssadmin@t.com")
        html = client.get("/exports/active-employees").get_data(as_text=True)
        assert "<select" in html and "js/searchable-select.js" in html
