"""
Truein problem alerts must respect company scope: an HR Manager ticked only for one company
is not told about another company's candidates. Super Admin / Head HR stay unscoped.
"""
import uuid
from unittest.mock import patch

import pytest

from app.models import UserRole, RequestStatus, OnboardingRequest, Notification
from app.integrations import truein
from .conftest import _make_user


@pytest.fixture(autouse=True)
def _app_ctx(app):
    with app.app_context():
        yield


def _world(db, company):
    ini = _make_user("Oa Ini", "oaini@t.com", UserRole.INITIATOR, db, companies=[company])
    hrm_same = _make_user("Oa HRM Same", "oahrmsame@t.com", UserRole.HR_MANAGER, db, companies=[company])
    other = "ROBO" if company == "RDC" else "RDC"
    hrm_other = _make_user("Oa HRM Other", "oahrmother@t.com", UserRole.HR_MANAGER, db, companies=[other])
    hrm_none = _make_user("Oa HRM None", "oahrmnone@t.com", UserRole.HR_MANAGER, db)
    hhr = _make_user("Oa HHR", "oahhr@t.com", UserRole.HEAD_HR, db)
    adm = _make_user("Oa Adm", "oaadm@t.com", UserRole.SUPER_ADMIN, db)
    req = OnboardingRequest(initiated_by=ini.id, status=RequestStatus.ACTIVE, public_token=uuid.uuid4().hex,
                            candidate_name="Alert Person", company_code=company, plant_location="P", designation="D")
    db.session.add(req)
    db.session.commit()
    return req, {"same": hrm_same.id, "other": hrm_other.id, "none": hrm_none.id, "hhr": hhr.id, "adm": adm.id}


@pytest.mark.parametrize("company", ["RDC", "ROBO"])
def test_push_failure_alert_goes_only_to_in_scope_hr_managers(db, company):
    req, ids = _world(db, company)
    with patch("app.utils.send_email") as send:
        truein._notify_push_failed(db, req, "boom", triggered_by="auto")
        db.session.commit()
    notified = {n.recipient_id for n in Notification.query.filter_by(request_id=req.id)}
    assert notified == {ids["same"], ids["hhr"], ids["adm"]}
    assert ids["other"] not in notified and ids["none"] not in notified
    assert {c.args[1][0] for c in send.call_args_list} == {"oahrmsame@t.com", "oahhr@t.com", "oaadm@t.com"}


def test_dropped_field_alert_is_scoped_too(db):
    req, ids = _world(db, "ROBO")
    with patch("app.utils.send_email"):
        truein._handle_dropped_fields(db, req, ["father_name"], "auto")
        db.session.commit()
    notified = {n.recipient_id for n in Notification.query.filter_by(request_id=req.id)}
    assert notified == {ids["same"], ids["hhr"]}          # this alert never went to Super Admin
