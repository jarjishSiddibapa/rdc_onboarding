"""Every date the app shows is DD/MM/YYYY."""
import re
from datetime import date, datetime

from app import _format_ist
from app.utils import fmt_date
from .conftest import login, _make_user
from app.models import UserRole


def test_fmt_date_accepts_every_stored_shape():
    assert fmt_date("2026-10-09") == "09/10/2026"
    assert fmt_date("2026-10-09T10:20:30") == "09/10/2026"
    assert fmt_date("18 Aug 2025") == "18/08/2025"
    assert fmt_date(date(2026, 1, 5)) == "05/01/2026"
    assert fmt_date(datetime(2026, 1, 5, 23, 0)) == "05/01/2026"
    assert fmt_date(None) == "" and fmt_date("") == ""
    assert fmt_date("not a date") == "not a date"          # never guessed


def test_timestamps_show_day_month_year_in_ist():
    assert _format_ist(datetime(2026, 10, 9, 20, 0)) == "10/10/2026, 01:30"       # UTC -> IST rolls the date
    assert _format_ist(datetime(2026, 10, 9, 0, 0), "%d/%m/%Y") == "09/10/2026"


def test_no_month_name_date_formats_left_in_templates_or_code():
    import pathlib
    root = pathlib.Path(__file__).parent.parent / "app"
    bad = []
    for f in list(root.rglob("*.html")) + list(root.rglob("*.py")):
        t = f.read_text(encoding="utf-8")
        if re.search(r"(strftime|ist)\([^)]*%[bB]", t):      # parsing ZingHR text with %b is fine; showing it is not
            bad.append(f.name)
    assert not bad, bad


def test_date_filters_use_the_ddmm_box_not_a_native_date_input(client, db):
    _make_user("Fmt Admin", "fmtadmin@t.com", UserRole.SUPER_ADMIN, db)
    db.session.commit()
    login(client, "fmtadmin@t.com")
    for url, n in (("/admin/audit-log?date_from=2026-06-01", 2), ("/exports/active-employees", 4)):
        page = client.get(url).get_data(as_text=True)
        assert page.count("ddmm-date") == n and 'type="date"' not in page, url
    assert 'value="2026-06-01"' in client.get("/admin/audit-log?date_from=2026-06-01").get_data(as_text=True)
