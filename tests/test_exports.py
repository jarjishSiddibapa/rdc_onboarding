"""
Tests for the exports blueprint:
  - Preview endpoint returns correct JSON structure
  - Download endpoint requires authentication + correct role
  - Filter params are applied correctly
"""
import json
import uuid
import pytest
from app.models import UserRole, RequestStatus, OnboardingRequest, ClusterNameMapping, InitiatorRegion, BusinessHeadRegion
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


# ── Filter by Business Head ─────────────────────────────────────────────────────
# Regression coverage for a real bug found 2026-09-24: this filter used to
# resolve via the legacy, unwritten User.business_head_id column (dead since
# the 2026-09-04 region-routing redesign), so picking ANY Business Head in
# the dropdown always returned zero rows. Fixed to re-derive eligibility via
# the canonical bh_ids_for_initiator() routing function instead.

class TestBusinessHeadFilter:
    def test_unscoped_initiator_request_matches_any_rdc_bh(self, client, db, app):
        admin = _make_user("AdminBH1", "adminbh1@t.com", UserRole.SUPER_ADMIN, db)
        initiator = _make_user("InitBH1", "initbh1@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        bh = _make_user("BhBH1", "bhbh1@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        _create_active_request(db, initiator, candidate_name="Unscoped Candidate", company_code="RDC")
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp = client.get(f"/exports/active-employees/preview?bh_id={bh.id}")
        data = json.loads(resp.data)
        # Fail-open: an initiator with no InitiatorRegion rows is reachable
        # by every RDC-ticked active BH, so this must be >=1, not 0 (the
        # dead-column bug always returned 0 here regardless of routing).
        assert data["total"] >= 1

    def test_region_scoped_bh_only_sees_own_region_requests(self, client, db, app):
        admin = _make_user("AdminBH2", "adminbh2@t.com", UserRole.SUPER_ADMIN, db)
        initiator = _make_user("InitBH2", "initbh2@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        bh_a = _make_user("BhA", "bha@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        bh_b = _make_user("BhB", "bhb@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        region = ClusterNameMapping(canonical_cluster_name="TestRegionBH")
        db.session.add(region)
        db.session.flush()
        db.session.add(InitiatorRegion(initiator_id=initiator.id, cluster_id=region.id))
        db.session.add(BusinessHeadRegion(business_head_id=bh_a.id, cluster_id=region.id))
        _create_active_request(db, initiator, candidate_name="Region Scoped Candidate", company_code="RDC")
        db.session.commit()
        with app.app_context():
            login(client, admin.email)
            resp_a = client.get(f"/exports/active-employees/preview?bh_id={bh_a.id}")
            resp_b = client.get(f"/exports/active-employees/preview?bh_id={bh_b.id}")
        data_a = json.loads(resp_a.data)
        data_b = json.loads(resp_b.data)
        assert data_a["total"] == 1
        assert data_b["total"] == 0


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
