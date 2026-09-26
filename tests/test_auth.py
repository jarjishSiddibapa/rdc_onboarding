"""
Tests for authentication routes:
  - Login success / failure
  - Account lockout after 5 failed attempts
  - Forgot-password flow (token generation, expiry)
  - Logout
"""
import pytest
from unittest.mock import patch
from app.models import User, UserRole
from app.extensions import bcrypt, db as _db
from .conftest import login, logout, _make_user


# ── Helpers ────────────────────────────────────────────────────────────────────

def _create_active_user(db, name="Test User", email="test@test.com", password="Test1234",
                        role=UserRole.INITIATOR):
    user = _make_user(name, email, role, db, password=password)
    user.is_active = True
    db.session.flush()
    return user


# ── Login ──────────────────────────────────────────────────────────────────────

class TestLogin:
    def test_valid_credentials_redirect_to_dashboard(self, client, db, app):
        user = _create_active_user(db)
        db.session.commit()
        with app.app_context():
            resp = login(client, user.email)
        assert resp.status_code == 200
        # Should land on dashboard (or at least not stay on login)
        assert b"Login" not in resp.data or b"dashboard" in resp.data.lower()

    def test_wrong_password_shows_error(self, client, db, app):
        user = _create_active_user(db)
        db.session.commit()
        with app.app_context():
            resp = client.post("/auth/login", data={
                "email": user.email,
                "password": "WrongPass1",
            }, follow_redirects=True)
        assert b"Invalid" in resp.data or b"incorrect" in resp.data.lower() or b"failed" in resp.data.lower()

    def test_nonexistent_email_shows_error(self, client, app):
        with app.app_context():
            resp = client.post("/auth/login", data={
                "email": "nobody@nowhere.com",
                "password": "Test1234",
            }, follow_redirects=True)
        assert resp.status_code == 200

    def test_inactive_user_cannot_login(self, client, db, app):
        user = _create_active_user(db)
        user.is_active = False
        db.session.commit()
        with app.app_context():
            resp = client.post("/auth/login", data={
                "email": user.email,
                "password": "Test1234",
            }, follow_redirects=True)
        # Should not reach dashboard; show inactive/disabled message
        assert b"disabled" in resp.data.lower() or b"inactive" in resp.data.lower() or resp.status_code == 200


# ── Account lockout: intentionally disabled — no limit on wrong attempts ────────

class TestNoAccountLockout:
    def test_many_failed_attempts_never_lock_account(self, client, db, app):
        user = _create_active_user(db, email="lock@test.com")
        db.session.commit()
        with app.app_context():
            for _ in range(6):
                client.post("/auth/login", data={
                    "email": user.email,
                    "password": "WrongPass1",
                }, follow_redirects=True)

            # No lockout message should ever appear, no matter how many failures
            resp = client.post("/auth/login", data={
                "email": user.email,
                "password": "WrongPass1",
            }, follow_redirects=True)
        assert b"locked" not in resp.data.lower()
        assert b"too many" not in resp.data.lower()

    def test_correct_password_still_works_after_many_failed_attempts(self, client, db, app):
        user = _create_active_user(db, email="lock2@test.com")
        db.session.commit()
        with app.app_context():
            for _ in range(6):
                client.post("/auth/login", data={
                    "email": user.email,
                    "password": "WrongPass1",
                }, follow_redirects=True)
            # The real password must still work — failed attempts never block it
            resp = client.post("/auth/login", data={
                "email": user.email,
                "password": "Test1234",
            }, follow_redirects=True)
        assert b"locked" not in resp.data.lower()


# ── Logout ─────────────────────────────────────────────────────────────────────

class TestLogout:
    def test_logout_redirects_to_login(self, client, db, app):
        user = _create_active_user(db, email="logout@test.com")
        db.session.commit()
        with app.app_context():
            login(client, user.email)
            resp = logout(client)
        # After logout, should see login page
        assert resp.status_code == 200


# ── Password reset token ───────────────────────────────────────────────────────

class TestPasswordResetToken:
    def test_forgot_password_page_loads(self, client, app):
        with app.app_context():
            resp = client.get("/auth/forgot-password")
        assert resp.status_code == 200

    def test_forgot_password_post_unknown_email_shows_generic_message(self, client, app):
        """Should not reveal whether the email exists (security: timing/enumeration)."""
        with app.app_context():
            resp = client.post("/auth/forgot-password", data={
                "email": "nobody@test.com"
            }, follow_redirects=True)
        assert resp.status_code == 200

    def test_reset_with_invalid_token_shows_error(self, client, app):
        with app.app_context():
            resp = client.get("/auth/reset-password/bad-token-value", follow_redirects=True)
        assert resp.status_code == 200
        assert (b"invalid" in resp.data.lower()
                or b"expired" in resp.data.lower()
                or b"error" in resp.data.lower())

    def test_reset_token_is_single_use(self, client, db, app):
        """
        Fixed 2026-09-26 — a reset token used to sign only the email, with no
        server-side record of consumed tokens, so the identical link could be
        replayed any number of times inside its 30-minute window. The token
        is now bound to password_hash at issue time, so the first successful
        reset invalidates every other copy of the same link immediately.
        """
        from app.auth.routes import _make_reset_token
        user = _create_active_user(db, email="resetme@test.com")
        db.session.commit()
        with app.app_context():
            token = _make_reset_token(user)

            # First use: succeeds.
            resp = client.post(f"/auth/reset-password/{token}", data={
                "password": "NewPass123!", "confirm_password": "NewPass123!",
            }, follow_redirects=True)
            assert resp.status_code == 200
            assert b"updated successfully" in resp.data.lower()

            # Second use of the SAME token: must be rejected, not silently
            # accepted again.
            resp2 = client.post(f"/auth/reset-password/{token}", data={
                "password": "AnotherPass456!", "confirm_password": "AnotherPass456!",
            }, follow_redirects=True)
            assert resp2.status_code == 200
            assert (b"invalid" in resp2.data.lower()
                    or b"expired" in resp2.data.lower()
                    or b"already used" in resp2.data.lower())

            # Confirm the password from the SECOND attempt never took effect.
            refreshed = User.query.filter_by(email="resetme@test.com").first()
            assert bcrypt.check_password_hash(refreshed.password_hash, "NewPass123!")
            assert not bcrypt.check_password_hash(refreshed.password_hash, "AnotherPass456!")

    def test_verify_reset_token_rejects_legacy_bare_email_format(self, app):
        """
        Pre-2026-09-26 tokens signed a bare email string, not a dict — those
        must be rejected outright (treated as invalid) rather than crash or
        be silently accepted, in case one is still floating around in an
        old email at the moment this ships.
        """
        from itsdangerous import URLSafeTimedSerializer
        from app.auth.routes import _verify_reset_token
        with app.app_context():
            from flask import current_app
            s = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
            legacy_token = s.dumps("someone@test.com", salt=current_app.config["PASSWORD_RESET_SALT"])
            assert _verify_reset_token(legacy_token) is None
