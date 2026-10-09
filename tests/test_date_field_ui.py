"""Date box on the hiring form: its feedback must not be the generic blur validator's (which left a stale
"required" message after a calendar pick and, by adding lines inside the box's wrapper, pushed the calendar
button out of the box)."""
import pathlib

FORM = (pathlib.Path(__file__).parent.parent / "app" / "templates" / "requests" / "form.html").read_text(encoding="utf-8")


def test_generic_validator_and_counter_skip_the_date_box():
    assert ".field-wrapper .inp:not([readonly]):not(.date-mask-visible)" in FORM


def test_date_feedback_goes_after_the_wrapper_not_inside_it():
    assert "wrap.after(p)" in FORM and "p.className = 'date-msg'" in FORM
    assert "visible.after(errEl)" not in FORM


def test_today_is_allowed_only_earlier_days_are_rejected():
    assert "if (entered < today)" in FORM          # strictly before today — today itself passes
    assert "cf_date < _date.today()" in (pathlib.Path(__file__).parent.parent / "app" / "requests_bp" / "routes.py").read_text(encoding="utf-8")
