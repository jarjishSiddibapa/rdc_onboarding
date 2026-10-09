"""
The whole hiring journey over HTTP, in one place: a ROBO Reporting Manager fills the 3-step form
(OTP-verified e-mail, PAN, joining date, Back button, document uploads), submits, and every
approver acts using ONLY the Approve button inside the e-mail they were sent (no login) until the
hire is ACTIVE; plus the rejection / resubmission loop. Truein, SMTP and the clock are mocked.
"""
import datetime
import io
import re
from unittest.mock import patch

import pytest

from app.extensions import db as _db
from app.models import (
    UserRole, RequestStatus, OnboardingRequest, FormField, FormFieldOption, FieldType, OptionsSource,
    PlantLocation, Designation, ApprovalAction, Notification,
)
from .conftest import login, _make_user

BASE = "https://onboarding.example.com"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _app_ctx(app):
    with app.app_context():
        yield


def _fields(db):
    spec = [
        ("company_code", "Company Code", FieldType.DROPDOWN, 1, True, OptionsSource.INLINE),
        ("associate_name", "Associate Name", FieldType.TEXT, 1, True, OptionsSource.INLINE),
        ("email_id", "Email ID", FieldType.EMAIL, 1, True, OptionsSource.INLINE),
        ("mobile_number", "Mobile Number", FieldType.TEL, 1, True, OptionsSource.INLINE),
        ("aadhar_no", "Aadhar Number", FieldType.TEXT, 1, True, OptionsSource.INLINE),
        ("pan_number", "PAN Number", FieldType.TEXT, 1, True, OptionsSource.INLINE),
        ("designation", "Designation", FieldType.DROPDOWN, 1, True, OptionsSource.DESIGNATION),
        ("uan_number", "UAN Number", FieldType.TEXT, 1, False, OptionsSource.INLINE),
        ("plant_location", "Plant Location", FieldType.DROPDOWN, 2, True, OptionsSource.PLANT_LOCATION),
        ("contract_from", "Contract From", FieldType.DATE, 2, True, OptionsSource.INLINE),
        ("reporting_manager_name", "Reporting Manager Name", FieldType.TEXT, 2, True, OptionsSource.INLINE),
        ("pan_card", "PAN Card", FieldType.FILE, 3, True, OptionsSource.INLINE),
        ("aadhar_card", "Aadhar Card", FieldType.FILE, 3, True, OptionsSource.INLINE),
        ("cv_resume", "CV / Resume", FieldType.FILE, 3, False, OptionsSource.INLINE),
    ]
    for i, (key, label, ftype, step, req, src) in enumerate(spec):
        f = FormField(field_key=key, field_label=label, field_type=ftype, step=step, is_required=req, options_source=src, sort_order=i)
        db.session.add(f)
        db.session.flush()
        if key == "company_code":
            for c in ("RDC", "Ultrafine", "ROBO"):
                db.session.add(FormFieldOption(field_id=f.id, option_label=c, option_value=c, sort_order=0))


@pytest.fixture
def world(db, app, tmp_path):
    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    app.config["APP_BASE_URL"] = BASE
    _fields(db)
    db.session.add(PlantLocation(name="ROBO-Plant", company="ROBO"))
    db.session.add(Designation(name="Operator", company="ROBO", notice_period_days=15))
    users = {
        "ini": _make_user("Flow Ini", "flowini@t.com", UserRole.INITIATOR, db, companies=["ROBO"]),
        "bh": _make_user("Flow BH", "flowbh@t.com", UserRole.BUSINESS_HEAD, db, companies=["ROBO"]),
        "hrm": _make_user("Flow HRM", "flowhrm@t.com", UserRole.HR_MANAGER, db, companies=["ROBO"]),
        "hhr": _make_user("Flow HHR", "flowhhr@t.com", UserRole.HEAD_HR, db),
        "sa": _make_user("Flow SA", "flowsa@t.com", UserRole.DR_BHOON, db),
    }
    db.session.commit()
    yield {k: u.email for k, u in users.items()}
    app.config["APP_BASE_URL"] = ""


class Mailbox:
    def __init__(self):
        self.sent = []          # (to, subject, text, html)
        self.otp_mail = []

    def send_email(self, subject, recipients, body, html=None):
        self.sent.append((recipients[0], subject, body, html))

    def smtp(self, cfg, recipients, subject, body, html=None):
        self.otp_mail.append(body)

    def link(self, to, action, subject_prefix=None):
        mails = [m for m in self.sent if m[0] == to and m[3] and (subject_prefix is None or m[1].startswith(subject_prefix))]
        assert mails, f"no button email for {to}"
        return re.search(r'href="%s(/requests/email-action/[^"]+/%s)"' % (re.escape(BASE), action), mails[-1][3]).group(1)


@pytest.fixture
def mailbox(app):
    mb = Mailbox()
    cfg = {"server": "x", "port": 1, "username": "u", "password": "p", "sender": "u"}
    with patch("app.utils.send_email", mb.send_email), patch("app.utils._send_smtp", mb.smtp), \
         patch("app.requests_bp.routes.get_db_mail_config", return_value=cfg):
        yield mb


STEP1 = {"action": "next", "company_code": "ROBO", "associate_name": "Rahul Verma", "email_id": "rahul@candidate.test",
         "mobile_number": "9876543210", "aadhar_no": "123412341234", "pan_number": "ABCDE1234F", "designation": "Operator",
         "uan_number": "100123456789"}


def _step2(**over):
    d = {"action": "next", "plant_location": "ROBO-Plant", "reporting_manager_name": "Mgr",
         "contract_from": (datetime.date.today() + datetime.timedelta(days=5)).isoformat()}
    d.update(over)
    return d


def _verify_email(client, mailbox, token, email="rahul@candidate.test"):
    r = client.post("/requests/send-email-otp", data={"email": email, "token": token}).get_json()
    assert r["ok"], r
    otp = re.search(r"OTP for email verification is: (\d{6})", mailbox.otp_mail[-1]).group(1)
    wrong = "000000" if otp != "000000" else "111111"
    assert client.post("/requests/verify-email-otp", data={"email": email, "otp": wrong, "token": token}).get_json()["ok"] is False
    assert client.post("/requests/verify-email-otp", data={"email": email, "otp": otp, "token": token}).get_json()["ok"] is True


def _forget_stale_user():
    """Tests run inside one shared app context, so flask-login's per-request cache (g._login_user) would leak
    from one test-client's request into the next. A real server gives every request a fresh g."""
    from flask import g
    g.pop("_login_user", None)


def _draft(client):
    loc = client.get("/requests/new").headers["Location"]
    return re.search(r"token=([0-9a-f]{32})", loc).group(1)


def _status(token):
    return OnboardingRequest.query.filter_by(public_token=token).one().status


def _create_and_submit(client, mailbox, files=True):
    token = _draft(client)
    _verify_email(client, mailbox, token)
    assert "step=2" in client.post(f"/requests/new?step=1&token={token}", data=STEP1).headers["Location"]
    assert "step=3" in client.post(f"/requests/new?step=2&token={token}", data=_step2()).headers["Location"]
    data = {"action": "save"}
    if files:
        data.update({"pan_card": (io.BytesIO(PNG), "pan.png"), "aadhar_card": (io.BytesIO(PNG), "aadhar.png"),
                     "cv_resume": (io.BytesIO(b"nope"), "cv.exe")})
    client.post(f"/requests/new?step=3&token={token}", data=data, content_type="multipart/form-data")
    return token


class TestFormSteps:
    def test_step_one_is_blocked_until_the_email_is_verified(self, client, db, world, mailbox):
        login(client, world["ini"])
        token = _draft(client)
        r = client.post(f"/requests/new?step=1&token={token}", data=STEP1)
        assert "step=1" in r.headers["Location"]
        assert "verify the candidate" in client.get(f"/requests/new?step=1&token={token}").get_data(as_text=True)

    def test_otp_wrong_then_right(self, client, db, world, mailbox):
        login(client, world["ini"])
        token = _draft(client)
        _verify_email(client, mailbox, token)
        assert "step=2" in client.post(f"/requests/new?step=1&token={token}", data=STEP1).headers["Location"]

    def test_pan_format_is_enforced(self, client, db, world, mailbox):
        login(client, world["ini"])
        token = _draft(client)
        _verify_email(client, mailbox, token)
        r = client.post(f"/requests/new?step=1&token={token}", data=dict(STEP1, pan_number="BADPAN"))
        assert "step=1" in r.headers["Location"]
        assert "Invalid PAN" in client.get(f"/requests/new?step=1&token={token}").get_data(as_text=True)

    def test_back_goes_back_and_keeps_what_was_typed(self, client, db, world, mailbox):
        login(client, world["ini"])
        token = _draft(client)
        _verify_email(client, mailbox, token)
        client.post(f"/requests/new?step=1&token={token}", data=STEP1)
        r = client.post(f"/requests/new?step=2&token={token}", data=_step2(action="back"))
        assert "step=1" in r.headers["Location"]
        page = client.get(f"/requests/new?step=1&token={token}").get_data(as_text=True)
        assert "Rahul Verma" in page and "rahul@candidate.test" in page

    def test_joining_date_cannot_be_in_the_past(self, client, db, world, mailbox):
        login(client, world["ini"])
        token = _draft(client)
        _verify_email(client, mailbox, token)
        client.post(f"/requests/new?step=1&token={token}", data=STEP1)
        past = (datetime.date.today() - datetime.timedelta(days=2)).isoformat()
        r = client.post(f"/requests/new?step=2&token={token}", data=_step2(contract_from=past))
        assert "step=2" in r.headers["Location"]
        assert "cannot be before today" in client.get(f"/requests/new?step=2&token={token}").get_data(as_text=True)
        assert "step=3" in client.post(f"/requests/new?step=2&token={token}", data=_step2(contract_from=datetime.date.today().isoformat())).headers["Location"]

    def test_only_allowed_document_types_are_stored(self, client, db, world, mailbox, tmp_path):
        login(client, world["ini"])
        token = _create_and_submit(client, mailbox)
        req = OnboardingRequest.query.filter_by(public_token=token).one()
        assert sorted(d["type"] for d in req.documents) == ["aadhar_card", "pan_card"]          # cv.exe refused
        assert len(list(tmp_path.iterdir())) == 2

    def test_a_draft_belongs_to_its_initiator(self, client, db, world, mailbox):
        login(client, world["ini"])
        token = _draft(client)
        client.get("/auth/logout")
        _make_user("Other Ini", "otherini@t.com", UserRole.INITIATOR, db, companies=["ROBO"])
        db.session.commit()
        login(client, "otherini@t.com")
        assert client.get(f"/requests/new?step=1&token={token}").status_code == 403


class TestSubmitAndApproveFromEmail:
    def test_full_chain_using_only_email_buttons(self, client, db, app, world, mailbox):
        login(client, world["ini"])
        token = _create_and_submit(client, mailbox)
        assert _status(token) == RequestStatus.DRAFT
        assert client.post(f"/requests/{token}/submit").status_code == 302
        assert _status(token) == RequestStatus.PENDING_BH
        first = [m for m in mailbox.sent if m[1].startswith("New hiring request")]
        assert [m[0] for m in first] == [world["bh"]] and ">Approve</a>" in first[0][3]
        client.get("/auth/logout")

        ok_push = {"success": True, "empId": "E1", "message": "ok", "dropped_fields": [], "http_status": 200,
                   "raw_response": {}, "payload_sent": {}}
        stages = [("bh", RequestStatus.PENDING_HR_MANAGER), ("hrm", RequestStatus.PENDING_HEAD_HR),
                  ("hhr", RequestStatus.PENDING_DR_BHOON), ("sa", RequestStatus.ACTIVE)]
        with patch("app.integrations.truein.push_employee", return_value=ok_push):
            for who, expected in stages:
                browser = app.test_client()                                    # nobody is signed in
                url = mailbox.link(world[who], "approve")
                page = browser.get(url)
                assert page.status_code == 200 and "Confirm approval" in page.get_data(as_text=True)
                assert _status(token) != expected                              # opening alone changed nothing
                done = browser.post(url, data={"remark": f"{who} ok", "via": "email"})
                assert done.status_code == 200 and "Approved" in done.get_data(as_text=True)
                assert _status(token) == expected
                assert browser.get("/dashboard").status_code == 302            # still not logged in
        req = OnboardingRequest.query.filter_by(public_token=token).one()
        assert req.truein_pushed_at is not None
        assert [a.remark for a in req.actions] == ["bh ok", "hrm ok", "hhr ok", "sa ok"]
        assert any(m[1].startswith("Employee ACTIVE") and m[0] == world["ini"] for m in mailbox.sent)

    def test_reject_and_resubmit_loop(self, client, db, app, world, mailbox):
        login(client, world["ini"])
        token = _create_and_submit(client, mailbox)
        client.post(f"/requests/{token}/submit")
        url = mailbox.link(world["bh"], "reject")
        browser = app.test_client()
        assert "Confirm rejection" in browser.get(url).get_data(as_text=True)
        assert browser.post(url, data={"remark": "  "}).status_code == 400
        assert _status(token) == RequestStatus.PENDING_BH
        assert browser.post(url, data={"remark": "No", "via": "email"}).status_code == 200
        assert _status(token) == RequestStatus.REJECTED_BH
        assert any(m[1].startswith("Request rejected") and m[0] == world["ini"] and "Remark: No" in m[2] for m in mailbox.sent)
        assert browser.post(url, data={"remark": "again"}).status_code == 409           # link is spent
        _forget_stale_user()
        client.post(f"/requests/{token}/resubmit")
        assert _status(token) == RequestStatus.PENDING_BH
        assert mailbox.link(world["bh"], "approve", "Resubmitted") != url

    def test_submit_is_refused_when_required_documents_or_fields_are_missing(self, client, db, world, mailbox):
        login(client, world["ini"])
        token = _draft(client)
        r = client.post(f"/requests/{token}/submit", follow_redirects=True)
        assert "Missing required fields" in r.get_data(as_text=True)
        assert _status(token) == RequestStatus.DRAFT
