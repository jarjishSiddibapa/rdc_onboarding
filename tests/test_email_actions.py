"""
Approve / Reject straight from the approval email (2026-10-08).

The link is signed for one person + one request + one status and expires. It needs no login:
opening it shows a confirmation page (and changes nothing), and the Confirm button records the
action as the link's owner through the normal approve/reject code.
"""
import json
import uuid
from unittest.mock import patch

import pytest
from itsdangerous import BadSignature, SignatureExpired

from app.extensions import db as _db
from app.models import (
    UserRole, RequestStatus, OnboardingRequest, User, AuditLog, ApprovalAction, ApprovalActionType, UserCompanyScope,
)
from app import utils
from .conftest import login, _make_user


BASE = "https://onboarding.example.com"


@pytest.fixture(autouse=True)
def _app_ctx(app):
    """Every test runs inside one app context, like the other HTTP-route test files do."""
    with app.app_context():
        yield


def _world(db, suffix, company="RDC", status=RequestStatus.PENDING_BH):
    init = _make_user("EaInit" + suffix, f"eainit{suffix.lower()}@t.com", UserRole.INITIATOR, db, companies=[company])
    bh = _make_user("EaBH" + suffix, f"eabh{suffix.lower()}@t.com", UserRole.BUSINESS_HEAD, db, companies=[company])
    hrm = _make_user("EaHRM" + suffix, f"eahrm{suffix.lower()}@t.com", UserRole.HR_MANAGER, db, companies=[company])
    req = OnboardingRequest(initiated_by=init.id, status=status, public_token=uuid.uuid4().hex,
                            candidate_name="Asha <b>Rao</b>", company_code=company,
                            plant_location="Plant One", designation="Field Tech")
    db.session.add(req)
    db.session.flush()
    db.session.commit()
    for u in (init, bh, hrm):
        u.pid, u.pemail = u.id, u.email          # plain copies: usable after the session is gone
    req.pid, req.ptoken = req.id, req.public_token
    return init, bh, hrm, req


# ── token ──────────────────────────────────────────────────────────────────────

class TestToken:
    def test_roundtrip(self, app, db):
        with app.app_context():
            tok = utils.make_email_action_token(7, 9, "PENDING_BH")
            assert utils.read_email_action_token(tok) == {"u": 7, "r": 9, "s": "PENDING_BH", "n": 0}

    def test_tampered_token_rejected(self, app, db):
        with app.app_context():
            tok = utils.make_email_action_token(7, 9, "PENDING_BH")
            with pytest.raises(BadSignature):
                utils.read_email_action_token(tok[:-3] + ("AAA" if not tok.endswith("AAA") else "BBB"))

    def test_token_signed_with_other_salt_rejected(self, app, db):
        from itsdangerous import URLSafeTimedSerializer
        with app.app_context():
            other = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="password-reset").dumps({"u": 1, "r": 1, "s": "X"})
            with pytest.raises(BadSignature):
                utils.read_email_action_token(other)

    def test_expired_token_rejected(self, app, db, monkeypatch):
        with app.app_context():
            tok = utils.make_email_action_token(7, 9, "PENDING_BH")
            monkeypatch.setattr(utils, "EMAIL_ACTION_MAX_AGE", -1)
            with pytest.raises(SignatureExpired):
                utils.read_email_action_token(tok)


# ── building links / the email itself ─────────────────────────────────────────

class TestLinks:
    def test_eligible_approver_gets_approve_reject_and_open(self, app, db):
        _, bh, _, req = _world(db, "L1")
        with app.app_context():
            app.config["APP_BASE_URL"] = BASE
            links = utils.build_email_links(req, bh, with_actions=True)
            assert set(links) == {"open", "approve", "reject"}
            assert links["approve"].startswith(BASE + "/requests/email-action/")
            assert links["approve"].endswith("/approve") and links["reject"].endswith("/reject")
            assert links["open"] == f"{BASE}/requests/{req.public_token}"
            app.config["APP_BASE_URL"] = ""

    def test_non_approver_gets_only_open_link(self, app, db):
        init, _, _, req = _world(db, "L2")
        with app.app_context():
            app.config["APP_BASE_URL"] = BASE
            assert set(utils.build_email_links(req, init, with_actions=True)) == {"open"}
            app.config["APP_BASE_URL"] = ""

    def test_no_actions_unless_asked(self, app, db):
        _, bh, _, req = _world(db, "L3")
        with app.app_context():
            app.config["APP_BASE_URL"] = BASE
            assert set(utils.build_email_links(req, bh)) == {"open"}
            app.config["APP_BASE_URL"] = ""

    def test_wrong_stage_user_gets_no_buttons(self, app, db):
        """An HR Manager is not the PENDING_BH approver — no buttons even if asked."""
        _, _, hrm, req = _world(db, "L4")
        with app.app_context():
            app.config["APP_BASE_URL"] = BASE
            assert set(utils.build_email_links(req, hrm, with_actions=True)) == {"open"}
            app.config["APP_BASE_URL"] = ""

    def test_unknown_base_url_means_no_links_and_plain_email_still_sent(self, app, db):
        _, bh, _, req = _world(db, "L5")
        with app.app_context():
            app.config["APP_BASE_URL"] = ""
            assert utils.build_email_links(req, bh, with_actions=True) is None
            with patch("app.utils.send_email") as send:
                utils.notify_users(db, req, [bh], "Subj", "Body text", actions=True)
            send.assert_called_once()
            assert send.call_args.args == ("Subj", [bh.email], "Body text")
            assert "html" not in send.call_args.kwargs

    def test_email_carries_buttons_in_html_and_urls_in_text(self, app, db):
        _, bh, _, req = _world(db, "L6")
        with app.app_context():
            app.config["APP_BASE_URL"] = BASE
            with patch("app.utils.send_email") as send:
                utils.notify_users(db, req, [bh], "Approval needed", "Please review.", actions=True)
            app.config["APP_BASE_URL"] = ""
        subject, rcpts, text = send.call_args.args
        html = send.call_args.kwargs["html"]
        assert subject == "Approval needed" and rcpts == [bh.email]
        assert "Approve: " + BASE + "/requests/email-action/" in text
        assert "Reject:  " + BASE + "/requests/email-action/" in text
        assert "Open request: " + BASE + "/requests/" + req.public_token in text
        assert ">Approve</a>" in html and ">Reject</a>" in html
        assert "RDC Associates Onboarding" in html
        assert "expire" in html and "expire" in text

    def test_candidate_data_is_html_escaped(self, app, db):
        _, bh, _, req = _world(db, "L7")
        with app.app_context():
            app.config["APP_BASE_URL"] = BASE
            with patch("app.utils.send_email") as send:
                utils.notify_users(db, req, [bh], "S <i>", "x <script>alert(1)</script>", actions=True)
            app.config["APP_BASE_URL"] = ""
        html = send.call_args.kwargs["html"]
        assert "<script>" not in html and "<b>Rao</b>" not in html
        assert "&lt;script&gt;" in html and "&lt;b&gt;Rao&lt;/b&gt;" in html

    def test_informational_email_has_open_link_only(self, app, db):
        init, _, _, req = _world(db, "L8")
        with app.app_context():
            app.config["APP_BASE_URL"] = BASE
            with patch("app.utils.send_email") as send:
                utils.notify_users(db, req, [init], "Employee ACTIVE", "Done.")
            app.config["APP_BASE_URL"] = ""
        text = send.call_args.args[2]
        html = send.call_args.kwargs["html"]
        assert "Approve:" not in text and "Open request: " in text
        assert ">Approve</a>" not in html and ">Open request</a>" in html


# ── the entry route (no login: the signed link is the credential) ─────────────

def _link(app, user, req, action, status="PENDING_BH"):
    with app.app_context():
        tok = utils.make_email_action_token(user.pid, req.pid, status)
    return f"/requests/email-action/{tok}/{action}"


def _page(resp):
    return resp.get_data(as_text=True)


def _status(req):
    return _db.session.get(OnboardingRequest, req.pid).status


class TestConfirmPage:
    def test_anonymous_gets_a_confirmation_page_not_a_login(self, client, db, app):
        _, bh, _, req = _world(db, "C1")
        resp = client.get(_link(app, bh, req, "approve"))
        page = _page(resp)
        assert resp.status_code == 200
        assert "Approve this hiring request?" in page and "Asha &lt;b&gt;Rao&lt;/b&gt;" in page      # escaped
        assert "Confirm approval" in page and "Approved via email." in page
        assert "Functional Head" in page and "Pending Functional Head" in page
        assert "Sign in" not in page.split("<form")[0]                                              # nothing demanded first

    def test_reject_page_has_no_prefilled_reason(self, client, db, app):
        _, bh, _, req = _world(db, "C2")
        page = _page(client.get(_link(app, bh, req, "reject")))
        assert "Reject this hiring request?" in page and "Confirm rejection" in page
        assert "Approved via email." not in page and "Approve instead" in page

    def test_over_norm_request_asks_for_a_justification_not_a_prefill(self, client, db, app):
        _, bh, _, req = _world(db, "C3")
        OnboardingRequest.query.filter_by(id=req.pid).update({"is_special_case": True})
        db.session.commit()
        page = _page(client.get(_link(app, bh, req, "approve")))
        assert "over-norm hiring request" in page and "Approved via email." not in page

    def test_opening_or_scanning_the_link_changes_nothing(self, client, db, app):
        _, bh, _, req = _world(db, "C4")
        for act in ("approve", "reject"):
            for _ in range(3):
                client.get(_link(app, bh, req, act))
                client.head(_link(app, bh, req, act))
        assert _status(req) == RequestStatus.PENDING_BH
        assert ApprovalAction.query.filter_by(request_id=req.pid).count() == 0

    def test_final_approval_page_lists_truein_preflight_issues(self, client, db, app):
        init, _, _, req = _world(db, "C5", status=RequestStatus.PENDING_HEAD_HR)
        hh = _make_user("EaHHc5", "eahhc5@t.com", UserRole.HEAD_HR, db)
        db.session.commit()
        hh.pid = hh.id
        with patch("app.integrations.truein.preflight_check", return_value={"issues": [{"label": "Mobile number is not valid"}]}):
            page = _page(client.get(_link(app, hh, req, "approve", status="PENDING_HEAD_HR")))
        assert "push the employee to Truein" in page and "Mobile number is not valid" in page

    def test_unknown_action_is_404(self, client, db, app):
        _, bh, _, req = _world(db, "C6")
        assert client.get(_link(app, bh, req, "approve").rsplit("/", 1)[0] + "/delete").status_code == 404


class TestNoLoginApproval:
    def test_confirm_approves_as_the_link_owner_with_no_session(self, client, db, app):
        init, bh, hrm, req = _world(db, "N1")
        url = _link(app, bh, req, "approve")
        with patch("app.utils.send_email"):
            resp = client.post(url, data={"remark": "Fine by me", "via": "email"})
        page = _page(resp)
        assert resp.status_code == 200 and "Approved" in page and "Pending HR Manager" in page
        assert _status(req) == RequestStatus.PENDING_HR_MANAGER
        action = ApprovalAction.query.filter_by(request_id=req.pid).one()
        assert action.actor_id == bh.pid and action.remark == "Fine by me" and action.action == ApprovalActionType.APPROVED
        row = AuditLog.query.filter_by(action_type="REQUEST_APPROVED").order_by(AuditLog.id.desc()).first()
        assert row.actor_id == bh.pid and json.loads(row.detail)["via"] == "email_link"
        # no login session came out of it
        assert client.get("/dashboard").status_code == 302

    def test_confirm_rejects_and_notifies_the_initiator(self, client, db, app):
        init, bh, _, req = _world(db, "N2")
        with patch("app.utils.send_email") as send:
            resp = client.post(_link(app, bh, req, "reject"), data={"remark": "Missing papers", "via": "email"})
        assert "Rejected" in _page(resp) and _status(req) == RequestStatus.REJECTED_BH
        assert any(c.args[1] == [init.pemail] and "Missing papers" in c.args[2] for c in send.call_args_list)
        row = AuditLog.query.filter_by(action_type="REQUEST_REJECTED").order_by(AuditLog.id.desc()).first()
        assert json.loads(row.detail)["via"] == "email_link"

    def test_remark_is_still_mandatory(self, client, db, app):
        _, bh, _, req = _world(db, "N3")
        url = _link(app, bh, req, "reject")
        resp = client.post(url, data={"remark": "   "})
        assert resp.status_code == 400 and "A remark is required." in _page(resp)
        assert _status(req) == RequestStatus.PENDING_BH

    def test_a_one_word_remark_is_enough(self, client, db, app):
        _, bh, _, req = _world(db, "N4")
        with patch("app.utils.send_email"):
            client.post(_link(app, bh, req, "approve"), data={"remark": "OK"})
        assert _status(req) == RequestStatus.PENDING_HR_MANAGER

    def test_second_confirm_on_the_same_link_is_harmless(self, client, db, app):
        _, bh, _, req = _world(db, "N5")
        url = _link(app, bh, req, "approve")
        with patch("app.utils.send_email"):
            client.post(url, data={"remark": "ok"})
            again = client.post(url, data={"remark": "ok"})
        assert again.status_code == 409 and "Already handled" in _page(again)
        assert ApprovalAction.query.filter_by(request_id=req.pid).count() == 1

    def test_link_is_ignored_if_someone_else_is_logged_in_on_that_browser(self, client, db, app):
        init, bh, _, req = _world(db, "N6")
        login(client, init.pemail)                    # the initiator is signed in here...
        with patch("app.utils.send_email"):
            client.post(_link(app, bh, req, "approve"), data={"remark": "ok"})
        action = ApprovalAction.query.filter_by(request_id=req.pid).one()
        assert action.actor_id == bh.pid              # ...but the action is the link owner's, never theirs

    def test_works_with_csrf_protection_on_because_the_token_is_the_credential(self, client, db, app):
        _, bh, _, req = _world(db, "N7")
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            with patch("app.utils.send_email"):
                resp = client.post(_link(app, bh, req, "approve"), data={"remark": "ok"})
            assert resp.status_code == 200 and _status(req) == RequestStatus.PENDING_HR_MANAGER
            # while the normal in-app approve route still demands a CSRF token
            _, bh2, _, req2 = _world(db, "N7b")
            login(client, bh2.pemail)
            blocked = client.post(f"/requests/{req2.ptoken}/approve", data={"remark": "ok"})
            assert _status(req2) == RequestStatus.PENDING_BH and blocked.status_code in (302, 400)
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    def test_final_approval_activates_and_pushes_to_truein(self, client, db, app):
        init, _, _, req = _world(db, "N8", status=RequestStatus.PENDING_HEAD_HR)
        hh = _make_user("EaHHn8", "eahhn8@t.com", UserRole.HEAD_HR, db)
        db.session.commit()
        hh.pid = hh.id
        ok = {"success": True, "empId": "X9", "message": "ok", "dropped_fields": [], "http_status": 200,
              "raw_response": {}, "payload_sent": {}}
        with patch("app.utils.send_email"), patch("app.integrations.truein.push_employee", return_value=ok) as push:
            resp = client.post(_link(app, hh, req, "approve", status="PENDING_HEAD_HR"), data={"remark": "go"})
        assert _status(req) == RequestStatus.ACTIVE
        push.assert_called_once()
        assert "pushed to Truein (empId: X9)" in _page(resp)

    def test_non_final_approval_emails_next_approver_with_working_buttons(self, client, db, app):
        init, bh, hrm, req = _world(db, "N9")
        app.config["APP_BASE_URL"] = BASE
        try:
            with patch("app.utils.send_email") as send:
                client.post(_link(app, bh, req, "approve"), data={"remark": "ok"})
        finally:
            app.config["APP_BASE_URL"] = ""
        to_hrm = [c for c in send.call_args_list if c.args[1] == [hrm.pemail]]
        assert len(to_hrm) == 1 and ">Approve</a>" in to_hrm[0].kwargs["html"]
        tok = to_hrm[0].kwargs["html"].split("/requests/email-action/")[1].split("/")[0]
        assert utils.read_email_action_token(tok) == {"u": hrm.pid, "r": req.pid, "s": "PENDING_HR_MANAGER", "n": 0}
        # ...and that next link works too, still without any login
        with patch("app.utils.send_email"):
            client.post(f"/requests/email-action/{tok}/approve", data={"remark": "ok"})
        assert _status(req) == RequestStatus.PENDING_HEAD_HR


class TestDeadLinks:
    def test_bad_signature(self, client, db, app):
        resp = client.get("/requests/email-action/not-a-real-token/approve")
        assert resp.status_code == 400 and "This link is not valid" in _page(resp)
        assert client.post("/requests/email-action/not-a-real-token/approve", data={"remark": "x"}).status_code == 400

    def test_expired(self, client, db, app, monkeypatch):
        _, bh, _, req = _world(db, "D1")
        url = _link(app, bh, req, "approve")
        monkeypatch.setattr(utils, "EMAIL_ACTION_MAX_AGE", -1)
        resp = client.get(url)
        assert resp.status_code == 410 and "This link has expired" in _page(resp)
        assert client.post(url, data={"remark": "x"}).status_code == 410
        assert _status(req) == RequestStatus.PENDING_BH

    def test_request_moved_on(self, client, db, app):
        _, bh, _, req = _world(db, "D2")
        url = _link(app, bh, req, "approve")
        OnboardingRequest.query.filter_by(id=req.pid).update({"status": RequestStatus.PENDING_HR_MANAGER})
        db.session.commit()
        resp = client.get(url)
        assert resp.status_code == 409 and "Pending HR Manager" in _page(resp)
        assert client.post(url, data={"remark": "x"}).status_code == 409
        assert ApprovalAction.query.filter_by(request_id=req.pid).count() == 0

    def test_user_lost_company_access(self, client, db, app):
        _, bh, _, req = _world(db, "D3")
        url = _link(app, bh, req, "approve")
        UserCompanyScope.query.filter_by(user_id=bh.pid).delete()
        db.session.commit()
        resp = client.post(url, data={"remark": "x"})
        assert resp.status_code == 403 and "can no longer act" in _page(resp)
        assert _status(req) == RequestStatus.PENDING_BH

    def test_deactivated_user(self, client, db, app):
        _, bh, _, req = _world(db, "D4")
        url = _link(app, bh, req, "approve")
        User.query.filter_by(id=bh.pid).update({"is_active": False})
        db.session.commit()
        assert client.post(url, data={"remark": "x"}).status_code == 403
        assert _status(req) == RequestStatus.PENDING_BH

    def test_deleted_request(self, client, db, app):
        _, bh, _, req = _world(db, "D5")
        url = _link(app, bh, req, "approve")
        OnboardingRequest.query.filter_by(id=req.pid).update({"is_deleted": True})
        db.session.commit()
        resp = client.get(url)
        assert resp.status_code == 404 and "no longer exists" in _page(resp)

    def test_a_link_cannot_be_used_for_a_different_stage_or_person(self, client, db, app):
        """The HR Manager's link for stage PENDING_HR_MANAGER is dead while the request is still PENDING_BH."""
        _, bh, hrm, req = _world(db, "D6")
        early = _link(app, hrm, req, "approve", status="PENDING_HR_MANAGER")
        assert client.post(early, data={"remark": "x"}).status_code == 409
        assert _status(req) == RequestStatus.PENDING_BH

    def test_forged_user_id_is_rejected(self, client, db, app):
        """A token signed with the wrong secret/salt can not be minted by an attacker."""
        from itsdangerous import URLSafeTimedSerializer
        _, bh, _, req = _world(db, "D7")
        forged = URLSafeTimedSerializer("not-the-secret", salt=utils.EMAIL_ACTION_SALT).dumps(
            {"u": bh.pid, "r": req.pid, "s": "PENDING_BH"})
        assert client.post(f"/requests/email-action/{forged}/approve", data={"remark": "x"}).status_code == 400
        assert _status(req) == RequestStatus.PENDING_BH


# ── approval emails really carry the buttons ──────────────────────────────────

class TestApprovalEmailsCarryButtons:
    def test_page_button_approval_is_not_marked_as_email(self, client, db, app):
        _, bh, _, req = _world(db, "E2")
        login(client, bh.pemail)
        client.post(f"/requests/{req.ptoken}/approve", data={"remark": "ok"})
        row = AuditLog.query.filter_by(action_type="REQUEST_APPROVED").order_by(AuditLog.id.desc()).first()
        assert "via" not in json.loads(row.detail)

    def test_rejection_email_to_initiator_has_open_link_not_buttons(self, client, db, app):
        init, bh, _, req = _world(db, "E6")
        app.config["APP_BASE_URL"] = BASE
        try:
            login(client, bh.pemail)
            with patch("app.utils.send_email") as send:
                client.post(f"/requests/{req.ptoken}/reject", data={"remark": "Missing docs"})
        finally:
            app.config["APP_BASE_URL"] = ""
        c = [c for c in send.call_args_list if c.args[1] == [init.pemail]][0]
        assert ">Approve</a>" not in c.kwargs["html"] and ">Open request</a>" in c.kwargs["html"]
        assert "Missing docs" in c.args[2]

    def test_final_approval_emails_have_no_buttons(self, client, db, app):
        init, _, hrm, req = _world(db, "E7", status=RequestStatus.PENDING_HEAD_HR)
        _make_user("EaHH7", "eahh7@t.com", UserRole.HEAD_HR, db)
        db.session.commit()
        app.config["APP_BASE_URL"] = BASE
        try:
            login(client, "eahh7@t.com")
            with patch("app.utils.send_email") as send, \
                 patch("app.integrations.truein.push_employee", return_value={
                     "success": True, "empId": "X1", "message": "ok", "dropped_fields": [], "http_status": 200,
                     "raw_response": {}, "payload_sent": {}}):
                client.post(f"/requests/{req.ptoken}/approve", data={"remark": "ok"})
        finally:
            app.config["APP_BASE_URL"] = ""
        assert OnboardingRequest.query.get(req.pid).status == RequestStatus.ACTIVE
        assert send.call_args_list
        for c in send.call_args_list:
            assert ">Approve</a>" not in c.kwargs["html"]

    def _draft_world(self, db, suffix, status):
        init = _make_user("EaDi" + suffix, f"eadi{suffix}@t.com", UserRole.INITIATOR, db, companies=["ROBO"])
        bh = _make_user("EaDb" + suffix, f"eadb{suffix}@t.com", UserRole.BUSINESS_HEAD, db, companies=["ROBO"])
        _make_user("EaDh" + suffix, f"eadh{suffix}@t.com", UserRole.HR_MANAGER, db, companies=["ROBO"])
        req = OnboardingRequest(initiated_by=init.id, status=status, public_token=uuid.uuid4().hex,
                                candidate_name="Nidhi Gala", company_code="ROBO",
                                plant_location="Plant A", designation="Engineer")
        db.session.add(req)
        db.session.flush()
        req.form_data = {"company_code": "ROBO", "associate_name": "Nidhi Gala", "plant_location": "Plant A",
                         "designation": "Engineer", "uan_number": "AB1234567890"}
        db.session.commit()
        return init.email, bh.email, bh.id, req.public_token

    def test_real_submit_emails_functional_head_with_working_buttons(self, client, db, app):
        init_email, bh_email, bh_id, token = self._draft_world(db, "s1", RequestStatus.DRAFT)
        app.config["APP_BASE_URL"] = BASE
        try:
            login(client, init_email)
            with patch("app.utils.send_email") as send:
                client.post(f"/requests/{token}/submit", follow_redirects=True)
        finally:
            app.config["APP_BASE_URL"] = ""
        sent = {c.args[1][0]: c for c in send.call_args_list}
        assert set(sent) == {bh_email}
        html = sent[bh_email].kwargs["html"]
        assert ">Approve</a>" in html and ">Reject</a>" in html and "No sign-in needed" in html
        assert "Nidhi Gala" in html and "ROBO" in html
        tok = html.split("/requests/email-action/")[1].split("/")[0]
        assert utils.read_email_action_token(tok)["u"] == bh_id
        assert "No sign-in needed" in sent[bh_email].args[2]

    def test_real_resubmit_emails_functional_head_with_buttons(self, client, db, app):
        init_email, bh_email, bh_id, token = self._draft_world(db, "s2", RequestStatus.REJECTED_BH)
        app.config["APP_BASE_URL"] = BASE
        try:
            login(client, init_email)
            with patch("app.utils.send_email") as send:
                client.post(f"/requests/{token}/resubmit", follow_redirects=True)
        finally:
            app.config["APP_BASE_URL"] = ""
        sent = {c.args[1][0]: c for c in send.call_args_list}
        assert ">Approve</a>" in sent[bh_email].kwargs["html"]


class TestLinksNeverTrustRequestHeaders:
    """Host-header injection: links in emails come only from APP_BASE_URL, never from the request."""

    def test_no_links_when_base_url_unset_even_inside_a_request(self, client, db, app):
        init, bh, hrm, req = _world(db, "H1")
        app.config["APP_BASE_URL"] = ""
        login(client, bh.pemail)
        with patch("app.utils.send_email") as send:
            client.post(f"/requests/{req.ptoken}/approve", data={"remark": "ok"},
                        headers={"Host": "evil.example.com", "X-Forwarded-Host": "evil.example.com"})
        to_hrm = [c for c in send.call_args_list if c.args[1] == [hrm.pemail]][0]
        assert "html" not in to_hrm.kwargs                       # plain email, no buttons, no links
        assert "evil.example.com" not in to_hrm.args[2] and "email-action" not in to_hrm.args[2]

    def test_links_use_the_configured_address_regardless_of_spoofed_headers(self, client, db, app):
        init, bh, hrm, req = _world(db, "H2")
        app.config["APP_BASE_URL"] = BASE
        try:
            login(client, bh.pemail)
            with patch("app.utils.send_email") as send:
                client.post(f"/requests/{req.ptoken}/approve", data={"remark": "ok"},
                            headers={"Host": "evil.example.com", "X-Forwarded-Host": "evil.example.com"})
        finally:
            app.config["APP_BASE_URL"] = ""
        to_hrm = [c for c in send.call_args_list if c.args[1] == [hrm.pemail]][0]
        assert "evil.example.com" not in to_hrm.kwargs["html"] and "evil.example.com" not in to_hrm.args[2]
        assert BASE + "/requests/email-action/" in to_hrm.kwargs["html"]

    def test_garbage_base_url_is_ignored(self, app):
        app.config["APP_BASE_URL"] = "javascript:alert(1)"
        try:
            assert utils.app_base_url() == ""
        finally:
            app.config["APP_BASE_URL"] = ""

    def test_admin_email_page_says_whether_buttons_are_on(self, client, db, app):
        _make_user("EaAdm", "eaadm@t.com", UserRole.SUPER_ADMIN, db)
        db.session.commit()
        login(client, "eaadm@t.com")
        assert "buttons are switched off" in client.get("/admin/settings/email").get_data(as_text=True)
        app.config["APP_BASE_URL"] = BASE
        try:
            assert BASE in client.get("/admin/settings/email").get_data(as_text=True)
        finally:
            app.config["APP_BASE_URL"] = ""


def test_email_action_pages_are_never_cached(client, db, app):
    _, bh, _, req = _world(db, "H9")
    resp = client.get(_link(app, bh, req, "approve"))
    assert resp.headers["Cache-Control"] == "no-store"
    dead = client.get("/requests/email-action/garbage/approve")
    assert dead.headers["Cache-Control"] == "no-store"


def test_a_link_from_an_earlier_round_is_dead_after_reject_and_resubmit(client, db, app):
    """Reject -> resubmit puts the request back on the same status; the old email's link must stay dead."""
    init, bh, hrm, req = _world(db, "R1")
    old = _link(app, bh, req, "approve")
    with patch("app.utils.send_email"):
        client.post(_link(app, bh, req, "reject"), data={"remark": "No"})
    assert _status(req) == RequestStatus.REJECTED_BH
    OnboardingRequest.query.filter_by(id=req.pid).update({"status": RequestStatus.PENDING_BH, "retry_count": 1})
    db.session.commit()
    resp = client.post(old, data={"remark": "stale"})
    assert resp.status_code == 409 and "out of date" in _page(resp)
    assert _status(req) == RequestStatus.PENDING_BH
    with app.app_context():
        fresh = utils.make_email_action_token(bh.pid, req.pid, "PENDING_BH", 1)
    with patch("app.utils.send_email"):
        assert client.post(f"/requests/email-action/{fresh}/approve", data={"remark": "ok"}).status_code == 200
    assert _status(req) == RequestStatus.PENDING_HR_MANAGER
