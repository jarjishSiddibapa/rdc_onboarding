from app.models import OnboardingRequest, RequestStatus, UserRole
from tests.conftest import login as _login, _make_user


class TestDashboardCountsRespectScope:
    """
    Regression for a 2026-09-26 fix: the "Total"/"Active" summary cards used
    to query OnboardingRequest completely unscoped, regardless of role —
    while the queue/table beneath them was already correctly company/
    region-scoped (2026-09-21/22). A Business Head or HR Manager restricted
    to one company saw a correctly-empty queue but a Total/Active card
    showing every company's combined counts, a real cross-company data leak.
    """

    def test_robo_only_business_head_sees_only_robo_counts(self, client, db, app, initiator):
        robo_bh = _make_user("RoboOnlyBH", "robobh@t.com", UserRole.BUSINESS_HEAD, db, companies=["ROBO"])
        db.session.add_all([
            OnboardingRequest(initiated_by=initiator.id, status=RequestStatus.PENDING_BH,
                               candidate_name="RDC Candidate 1", company_code="RDC"),
            OnboardingRequest(initiated_by=initiator.id, status=RequestStatus.ACTIVE,
                               candidate_name="RDC Candidate 2", company_code="RDC"),
            OnboardingRequest(initiated_by=initiator.id, status=RequestStatus.PENDING_BH,
                               candidate_name="ROBO Candidate", company_code="ROBO"),
        ])
        db.session.commit()

        with app.app_context():
            _login(client, robo_bh.email)
            resp = client.get("/dashboard")
        assert resp.status_code == 200
        body = resp.data.decode()
        # Only the 1 ROBO request should count. Before the fix, "total"
        # would have been 3 (2 RDC + 1 ROBO) — assert that leaked value is
        # nowhere on the page, and that the correctly-scoped value (1) is.
        assert 'data-count="3"' not in body
        assert 'data-count="1"' in body
        assert "RDC Candidate" not in body
        assert "ROBO Candidate" in body


class TestDashboardSpecialNormalPanels:
    def test_split_panels_render(self, client, db, app, initiator, business_head):
        req_normal = OnboardingRequest(
            initiated_by=initiator.id, status=RequestStatus.PENDING_BH,
            is_special_case=False, candidate_name="Normal Candidate",
            company_code="RDC",
        )
        req_special = OnboardingRequest(
            initiated_by=initiator.id, status=RequestStatus.PENDING_BH,
            is_special_case=True, candidate_name="Special Candidate",
            company_code="RDC",
        )
        db.session.add_all([req_normal, req_special])
        db.session.commit()

        with app.app_context():
            _login(client, business_head.email)
            resp = client.get("/dashboard")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Special Approvals" in body
        assert "Normal Approvals" in body
        assert "Special Candidate" in body
        assert "Normal Candidate" in body
        assert "View completed onboardings" in body
