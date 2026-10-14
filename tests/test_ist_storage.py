"""
Everything stored in the database is Indian time (IST, UTC+5:30) - not UTC (2026-10-14).
"""
import pathlib
import re
from datetime import datetime, timedelta, timezone

from app import _format_ist
from app.timeutil import IST_OFFSET, now_ist


def test_now_ist_is_utc_plus_five_thirty():
    utc = datetime.now(timezone.utc).replace(tzinfo=None)
    diff = now_ist() - utc
    assert timedelta(hours=5, minutes=29, seconds=55) < diff < timedelta(hours=5, minutes=30, seconds=5)
    assert IST_OFFSET == timedelta(hours=5, minutes=30)


def test_ist_filter_only_formats_it_does_not_shift():
    t = datetime(2026, 10, 14, 10, 30, 15)
    assert _format_ist(t) == "14/10/2026, 10:30"
    assert _format_ist(t, "%d/%m/%Y, %H:%M:%S") == "14/10/2026, 10:30:15"
    assert _format_ist(None) == "—"


def test_rows_get_indian_time_by_default(db):
    from app.models import AuditLog, AuditCategory
    row = AuditLog(action_category=AuditCategory.AUTH, action_type="X")
    db.session.add(row)
    db.session.commit()
    assert abs((row.created_at - now_ist()).total_seconds()) < 5


def test_no_utc_clock_calls_left_in_the_app():
    root = pathlib.Path(__file__).parent.parent / "app"
    bad = []
    for f in root.rglob("*.py"):
        if f.name == "timeutil.py":
            continue
        if re.search(r"utcnow\(|datetime\.now\(", f.read_text(encoding="utf-8")):
            bad.append(f.name)
    assert not bad, f"use app.timeutil.now_ist() instead: {bad}"
