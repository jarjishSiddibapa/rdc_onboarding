"""Daily digest e-mail: who gets one, when, and what is in it."""
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.extensions import db as _db
from app.models import UserRole, RequestStatus, OnboardingRequest, Notification, NotificationType, User
from app.services import digest_email
from .conftest import _make_user


@pytest.fixture(autouse=True)
def _app_ctx(app):
    with app.app_context():
        yield


def _user(db, tag, digest=True, active=True, last=None):
    u = _make_user("Dg " + tag, f"dg{tag}@t.com", UserRole.HEAD_HR, db)
    u.daily_digest_enabled, u.is_active, u.last_digest_sent_at = digest, active, last
    db.session.commit()
    return u.id


def _notify(db, uid, subject, body="First line\nSecond line", when=None):
    ini = User.query.filter_by(role=UserRole.INITIATOR).first() or _make_user("Dg Ini", "dgini@t.com", UserRole.INITIATOR, db)
    req = OnboardingRequest.query.filter_by(candidate_name="Digest Cand").first()
    if not req:
        req = OnboardingRequest(initiated_by=ini.id, status=RequestStatus.PENDING_BH, public_token=uuid.uuid4().hex,
                                candidate_name="Digest Cand", company_code="RDC", plant_location="P", designation="D")
        db.session.add(req)
        db.session.flush()
    n = Notification(request_id=req.id, recipient_id=uid, type=NotificationType.IN_APP, subject=subject, body=body)
    if when:
        n.sent_at = when
    db.session.add(n)
    db.session.commit()


def test_sends_one_summary_with_each_update(app, db):
    uid = _user(db, "1")
    _notify(db, uid, "Approved by HR Manager: A")
    _notify(db, uid, "Final approval required: B", body="Needs you\nignored second line")
    with patch("app.utils.send_email") as send:
        res = digest_email.send_due_digests(app)
    assert res == {"sent": 1, "skipped_empty": 0, "checked": 1}
    subject, rcpts, body = send.call_args.args
    assert subject == "Daily summary — 2 update(s)" and rcpts == ["dg1@t.com"]
    assert "Approved by HR Manager: A" in body and "Final approval required: B" in body
    assert "Needs you" in body and "ignored second line" not in body and "RDC Associates Onboarding" in body
    assert _db.session.get(User, uid).last_digest_sent_at is not None


def test_nothing_new_means_no_email_but_the_clock_still_moves(app, db):
    uid = _user(db, "2")
    with patch("app.utils.send_email") as send:
        res = digest_email.send_due_digests(app)
    send.assert_not_called()
    assert res["skipped_empty"] == 1 and _db.session.get(User, uid).last_digest_sent_at is not None


def test_not_due_yet_users_are_skipped(app, db):
    uid = _user(db, "3", last=datetime.utcnow() - timedelta(hours=3))
    _notify(db, uid, "Fresh")
    with patch("app.utils.send_email") as send:
        res = digest_email.send_due_digests(app)
    send.assert_not_called()
    assert res["sent"] == 0


def test_only_updates_since_the_last_digest_are_included(app, db):
    uid = _user(db, "4", last=datetime.utcnow() - timedelta(hours=30))
    _notify(db, uid, "Old news", when=datetime.utcnow() - timedelta(hours=40))
    _notify(db, uid, "New news", when=datetime.utcnow() - timedelta(hours=2))
    with patch("app.utils.send_email") as send:
        digest_email.send_due_digests(app)
    body = send.call_args.args[2]
    assert "New news" in body and "Old news" not in body


def test_opted_out_and_deactivated_users_get_nothing(app, db):
    a = _user(db, "5", digest=False)
    b = _user(db, "6", active=False)
    _notify(db, a, "x")
    _notify(db, b, "y")
    with patch("app.utils.send_email") as send:
        res = digest_email.send_due_digests(app)
    send.assert_not_called()
    assert res["checked"] == 0


def test_thread_starts_only_once(app):
    digest_email._started = False
    try:
        with patch.object(digest_email.threading, "Thread") as thread:
            assert digest_email.start_digest_thread(app) is True
            assert digest_email.start_digest_thread(app) is False
        assert thread.call_count == 1
    finally:
        digest_email._started = True      # the app's own thread (if any) is already running
