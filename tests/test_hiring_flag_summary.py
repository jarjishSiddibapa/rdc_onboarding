"""Hiring Flag shows, in plain words, the plant's production, the manpower already there and the maximum."""
import json
import uuid

import pytest

from app.models import UserRole, RequestStatus, OnboardingRequest, StaffingGateCheck, GateResult
from .conftest import login, _make_user


@pytest.fixture(autouse=True)
def _app_ctx(app):
    with app.app_context():
        yield


def _blocked_request(db, ini_id, detail, **cols):
    r = OnboardingRequest(initiated_by=ini_id, status=RequestStatus.DRAFT, public_token=uuid.uuid4().hex,
                          candidate_name="Flag Person", company_code="RDC", plant_location="Plant A",
                          designation="Officer - Technical")
    db.session.add(r)
    db.session.flush()
    db.session.add(StaffingGateCheck(request_id=r.id, result=GateResult.BLOCKED, plant_name="Plant A",
                                     detail=json.dumps(detail), **cols))
    db.session.commit()
    return r


class TestHiringFlagPage:
    def test_page_states_production_current_and_maximum(self, client, db):
        ini = _make_user("Flag Ini", "flagini@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        r = _blocked_request(db, ini.id, {"scope": "PLANT", "norm_role_category_name": "Technical",
                                          "requirement_type": "FIXED"},
                             tier_label="3000-5000 m³", volume_used=4230.4, current_headcount=11,
                             allowed_headcount=8, cluster_name="NCR-Gurgaon")
        login(client, "flagini@t.com")
        html = client.get(f"/requests/{r.public_token}/hiring-not-possible").get_data(as_text=True)
        assert "Production at this plant" in html and "4,230 m³ a month" in html
        assert "3000-5000 m³" in html and "bracket" in html
        assert "Technical manpower" in html
        assert "11 already working here, maximum allowed 8" in html


class TestFormPopupSummary:
    def test_popup_has_summary_slot_and_fills_it(self):
        src = open("app/templates/requests/form.html", encoding="utf-8").read()
        assert 'id="capacity-modal-summary"' in src
        assert "showCapacityModal(msg, d.details, plantLabel)" in src
        assert "already working here, maximum allowed" in src
