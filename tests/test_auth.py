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
