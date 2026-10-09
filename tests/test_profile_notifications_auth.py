"""
Self-service profile, the notification centre, the password-reset flow (including the e-mail it
sends) and the open-redirect guard on login.
"""
import io
import os
import re
import uuid
from unittest.mock import patch

import pytest

from app.extensions import db as _db, bcrypt
from app.models import (
    UserRole, RequestStatus, OnboardingRequest, User, Notification, NotificationType, AuditLog,
)
from .conftest import login, logout, _make_user


@pytest.fixture(autouse=True)
def _app_ctx(app):
    with app.app_context():
        yield


def _text(resp):
    return resp.get_data(as_text=True)


def _u(db, name, email, role=UserRole.HEAD_HR):
    u = _make_user(name, email, role, db)
    db.session.commit()
    return u.id


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# ── profile ───────────────────────────────────────────────────────────────────

class TestProfile:
    def test_page_renders(self, client, db):
        _u(db, "Pro One", "pro1@t.com")
        login(client, "pro1@t.com")
        page = _text(client.get("/profile/"))
        assert "Profile Information" in page and "Change Password" in page and "Email Notifications" in page

    def test_requires_login(self, client):
        assert client.get("/profile/").status_code in (302, 401)

    def test_update_name_and_username(self, client, db):
        uid = _u(db, "Pro Two", "pro2@t.com")
        login(client, "pro2@t.com")
        r = client.post("/profile/", data={"action": "update_profile", "name": "Pro Two Renamed", "username": "Pro_Two"},
                        follow_redirects=True)
        assert "Profile updated." in _text(r)
        row = _db.session.get(User, uid)
        assert row.name == "Pro Two Renamed" and row.username == "pro_two"
        actions = {a.action_type for a in AuditLog.query.filter_by(actor_id=uid)}
        assert {"PROFILE_NAME_CHANGED", "PROFILE_USERNAME_CHANGED"} <= actions

    def test_update_validation(self, client, db):
        _u(db, "Pro Three", "pro3@t.com")
        other = _make_user("Taken Name", "pro3b@t.com", UserRole.HEAD_HR, db)
        other.username = "taken"
        db.session.commit()
        login(client, "pro3@t.com")
        assert "Name cannot be empty" in _text(client.post("/profile/", data={"action": "update_profile", "name": " "}, follow_redirects=True))
        assert "may only contain lowercase" in _text(client.post("/profile/", data={"action": "update_profile", "name": "X", "username": "Bad Name!"}, follow_redirects=True))
        assert "Username already taken" in _text(client.post("/profile/", data={"action": "update_profile", "name": "X", "username": "taken"}, follow_redirects=True))

    def test_username_can_be_removed(self, client, db):
        u = _make_user("Pro Four", "pro4@t.com", UserRole.HEAD_HR, db)
        u.username = "pro_four"
        db.session.commit()
        uid = u.id
        login(client, "pro4@t.com")
        client.post("/profile/", data={"action": "update_profile", "name": "Pro Four", "username": ""})
        assert _db.session.get(User, uid).username is None

    def test_profile_picture_upload_and_remove(self, client, db, app, tmp_path):
        app.config["UPLOAD_FOLDER"] = str(tmp_path)
        uid = _u(db, "Pro Five", "pro5@t.com")
        login(client, "pro5@t.com")
        r = client.post("/profile/", data={"action": "update_profile", "name": "Pro Five",
                        "profile_pic": (io.BytesIO(PNG), "me.png")}, content_type="multipart/form-data", follow_redirects=True)
        assert "Profile updated." in _text(r)
        stored = _db.session.get(User, uid).profile_pic
        assert stored and stored.startswith("profile_") and stored.endswith(".png")
        assert os.path.exists(os.path.join(str(tmp_path), stored))
        r = client.post("/profile/", data={"action": "remove_pic"}, follow_redirects=True)
        assert "Profile picture removed." in _text(r)
        assert _db.session.get(User, uid).profile_pic is None
        assert not os.path.exists(os.path.join(str(tmp_path), stored))

    def test_non_image_upload_is_refused(self, client, db, app, tmp_path):
        app.config["UPLOAD_FOLDER"] = str(tmp_path)
        uid = _u(db, "Pro Six", "pro6@t.com")
        login(client, "pro6@t.com")
        r = client.post("/profile/", data={"action": "update_profile", "name": "Pro Six",
                        "profile_pic": (io.BytesIO(b"<?php echo 1;"), "shell.php")}, content_type="multipart/form-data", follow_redirects=True)
        assert "Only PNG, JPG, GIF, or WEBP" in _text(r)
        assert _db.session.get(User, uid).profile_pic is None
        assert os.listdir(str(tmp_path)) == []
        # a png-named file whose bytes are not a png is refused too
        client.post("/profile/", data={"action": "update_profile", "name": "Pro Six",
                    "profile_pic": (io.BytesIO(b"plain text"), "fake.png")}, content_type="multipart/form-data")
        assert os.listdir(str(tmp_path)) == [] and _db.session.get(User, uid).profile_pic is None

    def test_change_password_paths(self, client, db):
        uid = _u(db, "Pro Seven", "pro7@t.com")
        login(client, "pro7@t.com")
        post = lambda **d: _text(client.post("/profile/", data={"action": "change_password", **d}, follow_redirects=True))
        assert "Current password is incorrect" in post(current_password="wrong", new_password="Newpass99", confirm_password="Newpass99")
        assert "Passwords do not match" in post(current_password="Test1234", new_password="Newpass99", confirm_password="Different1")
        assert "at least 8 characters" in post(current_password="Test1234", new_password="Ab1", confirm_password="Ab1")
        assert "Password changed successfully." in post(current_password="Test1234", new_password="Newpass99", confirm_password="Newpass99")
        assert bcrypt.check_password_hash(_db.session.get(User, uid).password_hash, "Newpass99")
        assert AuditLog.query.filter_by(action_type="PASSWORD_CHANGED_BY_SELF").count() == 1

    def test_notification_preferences(self, client, db):
        uid = _u(db, "Pro Eight", "pro8@t.com")
        login(client, "pro8@t.com")
        r = client.post("/profile/", data={"action": "update_notification_prefs", "email_mode": "HIRING_ONLY",
                        "daily_digest_enabled": "on"}, follow_redirects=True)
        assert "Notification preferences updated." in _text(r)
        row = _db.session.get(User, uid)
        assert row.email_mode == "HIRING_ONLY" and row.daily_digest_enabled is True
        client.post("/profile/", data={"action": "update_notification_prefs", "email_mode": "bogus"})
        row = _db.session.get(User, uid)
        assert row.email_mode == "ALL" and row.daily_digest_enabled is False

    def test_username_availability_api(self, client, db):
        a = _make_user("Pro Nine", "pro9@t.com", UserRole.HEAD_HR, db)
        b = _make_user("Pro Ten", "pro10@t.com", UserRole.HEAD_HR, db)
        a.username, b.username = "nine", "ten"
        db.session.commit()
        login(client, "pro9@t.com")
        assert client.get("/profile/api/check-username?username=nine").get_json() == {"available": True}   # own name is fine
        assert client.get("/profile/api/check-username?username=ten").get_json() == {"available": False}
        assert client.get("/profile/api/check-username?username=").get_json() == {"available": True}


# ── notification centre ───────────────────────────────────────────────────────

def _notifs(db, recipient_id, n_read=1, n_unread=2):
    ini = _make_user("Ini N" + uuid.uuid4().hex[:4], f"ini{uuid.uuid4().hex[:6]}@t.com", UserRole.INITIATOR, db)
    req = OnboardingRequest(initiated_by=ini.id, status=RequestStatus.PENDING_BH, public_token=uuid.uuid4().hex,
                            candidate_name="N Cand", company_code="RDC", plant_location="P", designation="D")
    db.session.add(req)
    db.session.flush()
    ids = []
    for i in range(n_read + n_unread):
        n = Notification(request_id=req.id, recipient_id=recipient_id, type=NotificationType.IN_APP,
                         subject=f"Subject {i}", body="b", is_read=i < n_read)
        db.session.add(n)
        db.session.flush()
        ids.append(n.id)
    db.session.commit()
    return ids


class TestNotificationCentre:
    def test_tabs_and_counts(self, client, db):
        uid = _u(db, "Nt One", "nt1@t.com")
        _notifs(db, uid, n_read=1, n_unread=2)
        login(client, "nt1@t.com")
        assert client.get("/notifications/unread-count").get_json() == {"count": 2}
        assert "Subject 0" in _text(client.get("/notifications?filter=read")) and "Subject 2" not in _text(client.get("/notifications?filter=read"))
        un = _text(client.get("/notifications?filter=unread"))
        assert "Subject 1" in un and "Subject 2" in un and "Subject 0" not in un
        assert "Subject 0" in _text(client.get("/notifications?filter=garbage"))      # falls back to "all"

    def test_mark_read_and_unread_json(self, client, db):
        uid = _u(db, "Nt Two", "nt2@t.com")
        nid = _notifs(db, uid, n_read=0, n_unread=1)[0]
        login(client, "nt2@t.com")
        assert client.post(f"/notifications/{nid}/read").get_json() == {"ok": True, "is_read": True}
        assert _db.session.get(Notification, nid).is_read is True
        assert client.post(f"/notifications/{nid}/unread").get_json() == {"ok": True, "is_read": False}
        assert _db.session.get(Notification, nid).is_read is False

    def test_cannot_touch_someone_elses_notification(self, client, db):
        owner = _u(db, "Nt Owner", "ntowner@t.com")
        _u(db, "Nt Other", "ntother@t.com")
        nid = _notifs(db, owner, n_read=0, n_unread=1)[0]
        login(client, "ntother@t.com")
        assert client.post(f"/notifications/{nid}/read").status_code == 403
        assert client.post(f"/notifications/{nid}/unread").status_code == 403
        assert client.post("/notifications/bulk", data={"action": "read", "ids": [str(nid)]})  # silently scoped
        assert _db.session.get(Notification, nid).is_read is False

    def test_bulk_and_mark_all(self, client, db):
        uid = _u(db, "Nt Three", "nt3@t.com")
        ids = _notifs(db, uid, n_read=0, n_unread=3)
        login(client, "nt3@t.com")
        r = client.post("/notifications/bulk", data={"action": "read", "ids": [str(ids[0]), str(ids[1])]}, follow_redirects=True)
        assert "2 notifications marked as read." in _text(r)
        assert client.get("/notifications/unread-count").get_json() == {"count": 1}
        client.post("/notifications/bulk", data={"action": "unread", "ids": [str(ids[0])]})
        assert client.get("/notifications/unread-count").get_json() == {"count": 2}
        assert client.post("/notifications/bulk", data={"action": "explode", "ids": [str(ids[0])]}).status_code == 302
        r = client.post("/notifications/mark-all-read", follow_redirects=True)
        assert "2 notifications marked as read." in _text(r)
        assert client.get("/notifications/unread-count").get_json() == {"count": 0}
        assert client.post("/notifications/mark-all-read").status_code == 302   # nothing left: still harmless

    def test_requires_login(self, client):
        assert client.get("/notifications").status_code in (302, 401)
        assert client.get("/notifications/unread-count").status_code in (302, 401)


# ── password reset and login redirect safety ──────────────────────────────────

class TestPasswordResetFlow:
    def test_forgot_password_sends_link_and_it_works_once(self, client, db, app):
        _u(db, "Rs One", "rs1@t.com")
        with patch("app.auth.routes.send_email") as send:
            r = client.post("/auth/forgot-password", data={"email": "RS1@t.com"}, follow_redirects=True)
        assert "If that email is registered" in _text(r)
        send.assert_called_once()
        kw = send.call_args.kwargs
        assert kw["recipients"] == ["rs1@t.com"] and "Password Reset — RDC Associates Onboarding" == kw["subject"]
        link = re.search(r"http://\S*/auth/reset-password/\S+", kw["body"]).group(0)
        path = link.split("localhost", 1)[1]
        assert "30 minutes" in kw["body"]
        assert "Set a new password" in _text(client.get(path)) or "New Password" in _text(client.get(path))
        weak = client.post(path, data={"password": "short", "confirm_password": "short"})
        assert "at least 8 characters" in _text(weak)
        mismatch = client.post(path, data={"password": "Goodpass1", "confirm_password": "Goodpass2"})
        assert "Passwords do not match" in _text(mismatch)
        ok = client.post(path, data={"password": "Goodpass1", "confirm_password": "Goodpass1"}, follow_redirects=True)
        assert "Password updated successfully" in _text(ok)
        logout(client)
        assert "Dashboard" in _text(login(client, "rs1@t.com", "Goodpass1"))
        logout(client)
        again = client.get(path, follow_redirects=True)
        assert "invalid, has expired, or was already used" in _text(again)

    def test_unknown_and_deactivated_accounts_get_no_email_but_same_message(self, client, db):
        uid = _u(db, "Rs Two", "rs2@t.com")
        _db.session.get(User, uid).is_active = False
        db.session.commit()
        with patch("app.auth.routes.send_email") as send:
            r1 = client.post("/auth/forgot-password", data={"email": "nobody@t.com"}, follow_redirects=True)
            r2 = client.post("/auth/forgot-password", data={"email": "rs2@t.com"}, follow_redirects=True)
        send.assert_not_called()
        assert "If that email is registered" in _text(r1) and "If that email is registered" in _text(r2)

    def test_logged_in_user_is_bounced_from_reset_pages(self, client, db):
        _u(db, "Rs Three", "rs3@t.com")
        login(client, "rs3@t.com")
        assert client.get("/auth/forgot-password").status_code == 302
        assert client.get("/auth/reset-password/whatever").status_code == 302

    def test_deactivated_account_cannot_use_a_valid_link(self, client, db, app):
        from app.auth.routes import _make_reset_token
        uid = _u(db, "Rs Four", "rs4@t.com")
        tok = _make_reset_token(_db.session.get(User, uid))
        _db.session.get(User, uid).is_active = False
        db.session.commit()
        r = client.get(f"/auth/reset-password/{tok}", follow_redirects=True)
        assert "Account not found or deactivated" in _text(r)


class TestLoginRedirectSafety:
    @pytest.mark.parametrize("nxt,expected_prefix", [
        ("/notifications", "/notifications"),
        ("https://evil.example.com/x", "/dashboard"),
        ("//evil.example.com/x", "/dashboard"),
        ("http://evil.example.com", "/dashboard"),
    ])
    def test_next_param(self, client, db, nxt, expected_prefix):
        _u(db, "Lg One", "lg1@t.com")
        r = client.post("/auth/login?next=" + nxt, data={"login_id": "lg1@t.com", "password": "Test1234"})
        loc = r.headers["Location"]
        assert loc.startswith(expected_prefix) or (expected_prefix == "/dashboard" and loc.rstrip("/") in ("", "/dashboard", "http://localhost"))
        assert "evil.example.com" not in loc

    def test_login_by_username_and_case_insensitive_email(self, client, db):
        u = _make_user("Lg Two", "lg2@t.com", UserRole.HEAD_HR, db)
        u.username = "lg_two"
        db.session.commit()
        assert "Dashboard" in _text(login(client, "lg_two"))
        logout(client)
        assert "Dashboard" in _text(login(client, "LG2@T.COM"))
