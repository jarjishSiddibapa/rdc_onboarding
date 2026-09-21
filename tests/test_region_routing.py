"""
Tests for the region-based Initiator <-> Business Head routing redesign
(2026-09-04). Regression coverage for a real production case: an Initiator
in Chennai submitted requests, but the assigned Business Head for Chennai
never saw them in "All Requests" — because the initiator had a legacy
direct business_head_id pointing at a DIFFERENT Business Head, which took
priority over region overlap. That legacy mechanism has been removed
entirely; routing is now purely region-overlap based, with both Initiators
and Business Heads supporting multiple regions.
"""
import uuid
from app.models import (
    UserRole, RequestStatus, OnboardingRequest,
    ClusterNameMapping, BusinessHeadRegion, InitiatorRegion,
)
from app.extensions import db as _db
from .conftest import login, _make_user


def _region(db, name):
    r = ClusterNameMapping(canonical_cluster_name=name)
    db.session.add(r)
    db.session.flush()
    return r


def _create_request(db, user, status=RequestStatus.PENDING_BH, candidate_name="Test Candidate"):
    req = OnboardingRequest(
        initiated_by=user.id, status=status,
        public_token=uuid.uuid4().hex, candidate_name=candidate_name,
        company_code="RDC", plant_location="Plant A", designation="Engineer",
    )
    db.session.add(req)
    db.session.flush()
    return req


class TestRegionOverlapDashboardVisibility:
    def test_bh_sees_request_from_initiator_sharing_a_region(self, client, db, app):
        """The exact real-world scenario: Chennai initiator, Chennai BH."""
        chennai = _region(db, "CHENNAI")
        initiator = _make_user("ChennaiInit", "chennaiinit@t.com", UserRole.INITIATOR, db)
        bh        = _make_user("ChennaiBH",   "chennaibh@t.com",   UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        db.session.add(InitiatorRegion(initiator_id=initiator.id, cluster_id=chennai.id))
        db.session.add(BusinessHeadRegion(business_head_id=bh.id, cluster_id=chennai.id))
        db.session.commit()
        req = _create_request(db, initiator, candidate_name="Chennai Candidate")
        db.session.commit()
        with app.app_context():
            login(client, bh.email)
            resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "Chennai Candidate" in resp.data.decode()

    def test_bh_does_not_see_request_from_initiator_in_a_different_actively_covered_region(self, client, db, app):
        chennai = _region(db, "RR Chennai")
        mumbai  = _region(db, "RR Mumbai")
        initiator = _make_user("MumbaiInit", "mumbaiinit@t.com", UserRole.INITIATOR, db)
        chennai_bh = _make_user("RRChennaiBH", "rrchennaibh@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        mumbai_bh  = _make_user("RRMumbaiBH",  "rrmumbaibh@t.com",  UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        db.session.add(InitiatorRegion(initiator_id=initiator.id, cluster_id=mumbai.id))
        db.session.add(BusinessHeadRegion(business_head_id=chennai_bh.id, cluster_id=chennai.id))
        db.session.add(BusinessHeadRegion(business_head_id=mumbai_bh.id, cluster_id=mumbai.id))
        db.session.commit()
        req = _create_request(db, initiator, candidate_name="Mumbai Only Candidate")
        db.session.commit()
        with app.app_context():
            login(client, chennai_bh.email)
            resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "Mumbai Only Candidate" not in resp.data.decode()

    def test_multiple_bhs_sharing_a_region_all_see_the_request(self, client, db, app):
        goa = _region(db, "RR Goa")
        initiator = _make_user("GoaInit", "goainit@t.com", UserRole.INITIATOR, db)
        bh1 = _make_user("GoaBH1", "goabh1@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        bh2 = _make_user("GoaBH2", "goabh2@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        db.session.add(InitiatorRegion(initiator_id=initiator.id, cluster_id=goa.id))
        db.session.add(BusinessHeadRegion(business_head_id=bh1.id, cluster_id=goa.id))
        db.session.add(BusinessHeadRegion(business_head_id=bh2.id, cluster_id=goa.id))
        db.session.commit()
        req = _create_request(db, initiator, candidate_name="Goa Shared Candidate")
        db.session.commit()
        bh1_email, bh2_email = bh1.email, bh2.email
        for email in (bh1_email, bh2_email):
            with app.app_context():
                login(client, email)
                resp = client.get("/dashboard")
            assert "Goa Shared Candidate" in resp.data.decode()

    def test_initiator_with_multiple_regions_visible_to_bh_covering_any_one_of_them(self, client, db, app):
        pune = _region(db, "RR Pune")
        nagpur = _region(db, "RR Nagpur")
        initiator = _make_user("MultiRegionInit", "multiregioninit@t.com", UserRole.INITIATOR, db)
        nagpur_bh = _make_user("NagpurBH", "nagpurbh@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        # Initiator covers BOTH Pune and Nagpur; this BH only covers Nagpur.
        db.session.add(InitiatorRegion(initiator_id=initiator.id, cluster_id=pune.id))
        db.session.add(InitiatorRegion(initiator_id=initiator.id, cluster_id=nagpur.id))
        db.session.add(BusinessHeadRegion(business_head_id=nagpur_bh.id, cluster_id=nagpur.id))
        db.session.commit()
        req = _create_request(db, initiator, candidate_name="Multi Region Candidate")
        db.session.commit()
        with app.app_context():
            login(client, nagpur_bh.email)
            resp = client.get("/dashboard")
        assert "Multi Region Candidate" in resp.data.decode()

    def test_initiator_with_no_region_is_visible_to_every_bh(self, client, db, app):
        initiator = _make_user("NoRegionInit", "noregioninit@t.com", UserRole.INITIATOR, db)
        some_bh   = _make_user("SomeBH", "somebh@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        db.session.commit()
        req = _create_request(db, initiator, candidate_name="Unassigned Candidate")
        db.session.commit()
        with app.app_context():
            login(client, some_bh.email)
            resp = client.get("/dashboard")
        assert "Unassigned Candidate" in resp.data.decode()


class TestRegionOverlapApproval:
    def test_bh_sharing_region_can_approve(self, client, db, app):
        blr = _region(db, "RR Bangalore")
        initiator = _make_user("BlrInit", "blrinit@t.com", UserRole.INITIATOR, db)
        bh        = _make_user("BlrBH",   "blrbh@t.com",   UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        db.session.add(InitiatorRegion(initiator_id=initiator.id, cluster_id=blr.id))
        db.session.add(BusinessHeadRegion(business_head_id=bh.id, cluster_id=blr.id))
        db.session.commit()
        req = _create_request(db, initiator)
        db.session.commit()
        with app.app_context():
            login(client, bh.email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                               data={"remark": "Approved by matching-region BH"},
                               follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            updated = _db.session.get(OnboardingRequest, req.id)
            assert updated.status == RequestStatus.PENDING_HR_MANAGER

    def test_bh_not_sharing_a_covered_region_gets_403(self, client, db, app):
        blr = _region(db, "RR2 Bangalore")
        hyd = _region(db, "RR2 Hyderabad")
        initiator = _make_user("BlrInit2", "blrinit2@t.com", UserRole.INITIATOR, db)
        blr_bh = _make_user("BlrBH2", "blrbh2@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        hyd_bh = _make_user("HydBH2", "hydbh2@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
        db.session.add(InitiatorRegion(initiator_id=initiator.id, cluster_id=blr.id))
        db.session.add(BusinessHeadRegion(business_head_id=blr_bh.id, cluster_id=blr.id))
        db.session.add(BusinessHeadRegion(business_head_id=hyd_bh.id, cluster_id=hyd.id))
        db.session.commit()
        req = _create_request(db, initiator)
        db.session.commit()
        with app.app_context():
            login(client, hyd_bh.email)
            resp = client.post(f"/requests/{req.public_token}/approve",
                               data={"remark": "Should not be allowed"},
                               follow_redirects=True)
        assert resp.status_code == 403
