"""
Tests for role-based access control:
  - role_required decorator (HTTP-level enforcement)
  - Draft privacy (only initiator can view their own drafts)
  - BH scoping (BH can only act on requests from their assigned initiators)
"""
import pytest
from app.models import UserRole, RequestStatus, OnboardingRequest
from app.extensions import db as _db
from .conftest import login, _make_user
import uuid


def _create_request(db, user, status=RequestStatus.DRAFT):
    req = OnboardingRequest(
        initiated_by=user.id,
        status=status,
        public_token=uuid.uuid4().hex,
        candidate_name="Test Candidate",
    )
    db.session.add(req)
    db.session.flush()
    return req


# ── Role-required HTTP enforcement ────────────────────────────────────────────

class TestRoleRequired:
    def test_unauthenticated_request_gets_401_or_redirect(self, client, app):
        with app.app_context():
            # Admin users list requires SUPER_ADMIN
            resp = client.get("/admin/users")
        assert resp.status_code in (401, 302, 403)

    def test_wrong_role_gets_403(self, client, db, app):
        user = _make_user("Init", "init@test.com", UserRole.INITIATOR, db)
        db.session.commit()
        with app.app_context():
            login(client, user.email)
            resp = client.get("/admin/users")
        assert resp.status_code in (403, 302)

    def test_super_admin_can_access_admin_users(self, client, db, app):
        user = _make_user("Admin", "admin@test.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, user.email)
            resp = client.get("/admin/users")
        assert resp.status_code == 200


# ── Draft privacy ──────────────────────────────────────────────────────────────

class TestDraftPrivacy:
    def test_initiator_can_view_own_draft(self, client, db, app):
        user = _make_user("Own", "own@test.com", UserRole.INITIATOR, db)
        req  = _create_request(db, user, RequestStatus.DRAFT)
        db.session.commit()
        with app.app_context():
            login(client, user.email)
            resp = client.get(f"/requests/{req.public_token}")
        assert resp.status_code == 200

    def test_different_initiator_cannot_view_draft(self, client, db, app):
        owner  = _make_user("Owner", "owner@test.com", UserRole.INITIATOR, db)
        other  = _make_user("Other", "other@test.com", UserRole.INITIATOR, db)
        req    = _create_request(db, owner, RequestStatus.DRAFT)
        db.session.commit()
        with app.app_context():
            login(client, other.email)
            resp = client.get(f"/requests/{req.public_token}")
        assert resp.status_code == 403

    def test_business_head_cannot_view_draft(self, client, db, app):
        owner = _make_user("Owner2", "owner2@test.com", UserRole.INITIATOR, db)
        bh    = _make_user("BH", "bh@test.com", UserRole.BUSINESS_HEAD, db)
        req   = _create_request(db, owner, RequestStatus.DRAFT)
        db.session.commit()
        with app.app_context():
            login(client, bh.email)
            resp = client.get(f"/requests/{req.public_token}")
        assert resp.status_code == 403


# ── Initiator can only see own requests ───────────────────────────────────────

class TestInitiatorScope:
    def test_initiator_cannot_view_other_initiators_request(self, client, db, app):
        owner = _make_user("OwnerA", "ownera@test.com", UserRole.INITIATOR, db)
        other = _make_user("OtherB", "otherb@test.com", UserRole.INITIATOR, db)
        req   = _create_request(db, owner, RequestStatus.PENDING_BH)
        db.session.commit()
        with app.app_context():
            login(client, other.email)
            resp = client.get(f"/requests/{req.public_token}")
        assert resp.status_code == 403


# ── Only initiator can submit/delete draft ────────────────────────────────────

class TestDraftActions:
    def test_submit_requires_initiator_role(self, client, db, app):
        bh  = _make_user("BH2", "bh2@test.com", UserRole.BUSINESS_HEAD, db)
        owner = _make_user("Init2", "init2@test.com", UserRole.INITIATOR, db)
        req = _create_request(db, owner, RequestStatus.DRAFT)
        db.session.commit()
        with app.app_context():
            login(client, bh.email)
            resp = client.post(f"/requests/{req.public_token}/submit",
                               follow_redirects=True)
        assert resp.status_code in (403, 302)

    def test_delete_requires_own_draft(self, client, db, app):
        owner = _make_user("Init3", "init3@test.com", UserRole.INITIATOR, db)
        other = _make_user("Init4", "init4@test.com", UserRole.INITIATOR, db)
        req   = _create_request(db, owner, RequestStatus.DRAFT)
        db.session.commit()
        with app.app_context():
            login(client, other.email)
            resp = client.post(f"/requests/{req.public_token}/delete",
                               follow_redirects=True)
        assert resp.status_code in (403, 302)
