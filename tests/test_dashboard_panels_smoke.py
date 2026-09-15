from app.models import OnboardingRequest, RequestStatus
from tests.conftest import login as _login


class TestDashboardSpecialNormalPanels:
    def test_split_panels_render(self, client, db, app, initiator, business_head):
        req_normal = OnboardingRequest(
            initiated_by=initiator.id, status=RequestStatus.PENDING_BH,
            is_special_case=False, candidate_name="Normal Candidate",
        )
        req_special = OnboardingRequest(
            initiated_by=initiator.id, status=RequestStatus.PENDING_BH,
            is_special_case=True, candidate_name="Special Candidate",
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
