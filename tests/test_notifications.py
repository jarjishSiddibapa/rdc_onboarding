"""
Tests for per-user email notification preferences (added 2026-09-10):
- notify_users()'s category kwarg gates the outbound email only, never the
  in-app Notification row, based on User.email_mode.
- send_email_otp() rejects with duplicate=True before sending an OTP when
  the email already appears on another live request.
"""
import uuid
from unittest.mock import patch
from app.models import UserRole, RequestStatus, OnboardingRequest, Notification
from app.utils import notify_users
from .conftest import _make_user, login


def _make_request(db, user, candidate_name="Notif Candidate"):
    req = OnboardingRequest(
        initiated_by=user.id, status=RequestStatus.PENDING_BH,
        public_token=uuid.uuid4().hex, candidate_name=candidate_name,
        company_code="TC", plant_location="Plant A", designation="Engineer",
    )
    db.session.add(req)
    db.session.flush()
    return req


class TestNotifyUsersCategoryGating:
    def test_hiring_category_always_emails_regardless_of_preference(self, db, app):
        with app.app_context():
            initiator = _make_user("NotifInit1", "notifinit1@t.com", UserRole.INITIATOR, db)
            user = _make_user("NotifUser1", "notifuser1@t.com", UserRole.HR_MANAGER, db)
            user.email_mode = "HIRING_ONLY"
            req = _make_request(db, initiator)
            db.session.commit()

            with patch("app.utils.send_email") as mock_send:
                notify_users(db, req, [user], "Subject", "Body", category="HIRING")

            mock_send.assert_called_once()
            assert Notification.query.filter_by(recipient_id=user.id).count() == 1

    def test_admin_category_suppressed_for_hiring_only_user(self, db, app):
        with app.app_context():
            initiator = _make_user("NotifInit2", "notifinit2@t.com", UserRole.INITIATOR, db)
            user = _make_user("NotifUser2", "notifuser2@t.com", UserRole.SUPER_ADMIN, db)
            user.email_mode = "HIRING_ONLY"
            req = _make_request(db, initiator)
            db.session.commit()

            with patch("app.utils.send_email") as mock_send:
                notify_users(db, req, [user], "Subject", "Body", category="ADMIN")

            # In-app notification still created — only the email is gated.
            mock_send.assert_not_called()
            assert Notification.query.filter_by(recipient_id=user.id).count() == 1

    def test_admin_category_still_emails_all_mode_user(self, db, app):
        with app.app_context():
            initiator = _make_user("NotifInit3", "notifinit3@t.com", UserRole.INITIATOR, db)
            user = _make_user("NotifUser3", "notifuser3@t.com", UserRole.SUPER_ADMIN, db)
            assert user.email_mode == "ALL"  # default
            req = _make_request(db, initiator)
            db.session.commit()

            with patch("app.utils.send_email") as mock_send:
                notify_users(db, req, [user], "Subject", "Body", category="ADMIN")

            mock_send.assert_called_once()


class TestDuplicateEmailCheck:
    def test_send_otp_rejects_email_already_used_on_another_request(self, client, db, app):
        initiator = _make_user("DupInit", "dupinit@t.com", UserRole.INITIATOR, db)
        existing = _make_request(db, initiator, candidate_name="Existing Candidate")
        existing.form_data = {"email_id": "duplicate@candidate.com"}
        db.session.commit()
        initiator_email = initiator.email

        with app.app_context():
            login(client, initiator_email)
            resp = client.post("/requests/send-email-otp", data={"email": "duplicate@candidate.com"})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False
        assert data["duplicate"] is True

    def test_send_otp_allows_fresh_email(self, client, db, app):
        initiator = _make_user("FreshInit", "freshinit@t.com", UserRole.INITIATOR, db)
        initiator_email = initiator.email
        db.session.commit()

        with app.app_context():
            login(client, initiator_email)
            resp = client.post("/requests/send-email-otp", data={"email": "brand.new@candidate.com"})

        assert resp.status_code == 200
        data = resp.get_json()
        # No SMTP configured in test env, so this fails for a DIFFERENT reason
        # (email service not configured) — the important assertion is that it
        # is NOT flagged as a duplicate.
        assert not data.get("duplicate")
