"""
Tests for the exports blueprint:
  - Preview endpoint returns correct JSON structure
  - Download endpoint requires authentication + correct role
  - Filter params are applied correctly
"""
import json
import uuid
import pytest
from app.models import UserRole, RequestStatus, OnboardingRequest
from app.extensions import db as _db
from .conftest import login, _make_user


_ALLOWED_ROLES = (UserRole.SUPER_ADMIN, UserRole.HEAD_HR, UserRole.HR_MANAGER)


def _create_active_request(db, user, candidate_name="Jane Doe", company_code="TC"):
    req = OnboardingRequest(
        initiated_by=user.id,
        status=RequestStatus.ACTIVE,
        public_token=uuid.uuid4().hex,
        candidate_name=candidate_name,
        company_code=company_code,
        plant_location="Delhi",
        designation="Engineer",
    )
    db.session.add(req)
    db.session.flush()
    return req


# ── Preview endpoint ───────────────────────────────────────────────────────────

class TestPreviewEndpoint:
    def test_unauthenticated_redirected(self, client, app):
        with app.app_context():
            resp = client.get("/exports/active-employees/preview")
        assert resp.status_code in (302, 401)

    def test_initiator_forbidden(self, client, db, app):
        user = _make_user("Init", "initexp@t.com", UserRole.INITIATOR, db)
        db.session.commit()
        with app.app_context():
            login(client, user.email)
            resp = client.get("/exports/active-employees/preview")
        assert resp.status_code in (302, 403)

    @pytest.mark.parametrize("role", _ALLOWED_ROLES)
    def test_allowed_roles_get_json(self, client, db, app, role):
        user = _make_user(f"User_{role}", f"{role.value.lower()}@t.com", role, db)
        db.session.commit()
        with app.app_context():
            login(client, user.email)
            resp = client.get("/exports/active-employees/preview")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data.get("ok") is True
        assert "columns" in data
        assert "rows" in data
        assert "total" in data

    def test_preview_returns_at_most_10_rows(self, client, db, app):
        admin = _make_user("Admin", "adminprev@t.com", UserRole.SUPER_ADMIN, db)
        initiator = _make_user("Init", "initprev@t.com", UserRole.INITIATOR, db)
        for i in range(15):
            _create_active_request(db, initiator, candidate_name=f"Candidate {i}")
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.get("/exports/active-employees/preview")
        data = json.loads(resp.data)
        assert len(data["rows"]) <= 10

    def test_company_filter_applied(self, client, db, app):
        admin     = _make_user("Admin2", "admin2prev@t.com", UserRole.SUPER_ADMIN, db)
        initiator = _make_user("Init2",  "init2prev@t.com",  UserRole.INITIATOR, db)
        _create_active_request(db, initiator, candidate_name="Alpha", company_code="ALPHA")
        _create_active_request(db, initiator, candidate_name="Beta",  company_code="BETA")
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.get("/exports/active-employees/preview?company=ALPHA")
        data = json.loads(resp.data)
        assert data["total"] >= 1
        # All preview rows must belong to ALPHA
        company_col_idx = next(
            (i for i, c in enumerate(data["columns"]) if "company" in c.lower()), None
        )
        if company_col_idx is not None:
            for row in data["rows"]:
                assert row[company_col_idx] == "ALPHA"


# ── Download endpoint ──────────────────────────────────────────────────────────

class TestDownloadEndpoint:
    def test_unauthenticated_redirected(self, client, app):
        with app.app_context():
            resp = client.get("/exports/active-employees/download")
        assert resp.status_code in (302, 401)

    def test_initiator_cannot_download(self, client, db, app):
        user = _make_user("InitDL", "initdl@t.com", UserRole.INITIATOR, db)
        db.session.commit()
        with app.app_context():
            login(client, user.email)
            resp = client.get("/exports/active-employees/download")
        assert resp.status_code in (302, 403)

    def test_super_admin_gets_xlsx(self, client, db, app):
        admin = _make_user("AdminDL", "admindl@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.get("/exports/active-employees/download")
        assert resp.status_code == 200
        assert "xlsx" in resp.content_type or \
               "spreadsheet" in resp.content_type or \
               resp.content_type in (
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   "application/octet-stream",
               )
