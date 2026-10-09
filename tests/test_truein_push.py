"""
Truein integration internals with the HTTP layer mocked: the 4-step field-dropping fallback chain
in push_employee(), the background retry loop's stop rules, and the paginated employee pull.
Nothing here ever reaches the real Truein.
"""
import uuid
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest

from app.extensions import db as _db
from app.models import UserRole, RequestStatus, OnboardingRequest, Notification, TrueinPushLog
from app.integrations import truein
from .conftest import _make_user


@pytest.fixture(autouse=True)
def _app_ctx(app):
    with app.app_context():
        yield


def _res(success, message="ok", **extra):
    r = {"success": success, "message": message, "empId": "E1" if success else None, "http_status": 200 if success else 400,
         "raw_response": {}, "payload_sent": {}}
    r.update(extra)
    return r


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(truein, "SUBSCRIPTION_KEY", "test-key")


def _payload(**over):
    p = {"empId": "E1", "mobile": "9876543210", "manager_emp_id": "M1", "sitePoint": "SP"}
    p.update(over)
    return p


class TestPushFallbackChain:
    def test_requires_a_subscription_key(self, monkeypatch):
        monkeypatch.setattr(truein, "SUBSCRIPTION_KEY", "")
        with pytest.raises(ValueError, match="TRUEIN_SUBSCRIPTION_KEY"):
            truein.push_employee(MagicMock())

    def test_clean_first_attempt(self, key):
        with patch.object(truein, "build_payload", return_value=_payload()), \
             patch.object(truein, "_do_push", return_value=_res(True)) as push:
            out = truein.push_employee(MagicMock())
        assert out["success"] and out["dropped_fields"] == [] and push.call_count == 1

    def test_country_code_is_stripped_before_sending(self, key):
        with patch.object(truein, "build_payload", return_value=_payload(mobile="+919876543210")), \
             patch.object(truein, "_do_push", return_value=_res(True)) as push:
            truein.push_employee(MagicMock())
        assert push.call_args.args[0]["mobile"] == "9876543210"

    @pytest.mark.parametrize("bad", ["1234567890", "98765", "abc", "0987654321"])
    def test_invalid_mobile_is_dropped_up_front_and_recorded(self, key, bad):
        with patch.object(truein, "build_payload", return_value=_payload(mobile=bad)), \
             patch.object(truein, "_do_push", return_value=_res(True)) as push:
            out = truein.push_employee(MagicMock())
        assert "mobile" not in push.call_args.args[0] and out["dropped_fields"] == ["mobile"]

    def test_manager_rejected_then_retried_without_it(self, key):
        seq = [_res(False, "Please provide correct Manager Emp ID"), _res(True)]
        with patch.object(truein, "build_payload", return_value=_payload()), \
             patch.object(truein, "_do_push", side_effect=seq) as push:
            out = truein.push_employee(MagicMock())
        assert out["success"] and out["dropped_fields"] == ["manager_emp_id"]
        assert "manager_emp_id" not in push.call_args_list[1].args[0] and "sitePoint" in push.call_args_list[1].args[0]

    def test_whole_chain_manager_then_site_point_then_mobile(self, key):
        seq = [_res(False, "provide correct manager emp id"), _res(False, "provide correct Site Point"),
               _res(False, "Enter valid mobile number"), _res(True)]
        with patch.object(truein, "build_payload", return_value=_payload()), \
             patch.object(truein, "_do_push", side_effect=seq) as push:
            out = truein.push_employee(MagicMock())
        assert out["success"] and push.call_count == 4
        assert out["dropped_fields"] == ["manager_emp_id", "sitePoint", "mobile_truein_rejected"]
        assert set(push.call_args_list[3].args[0]) == {"empId"}

    def test_unrelated_failure_is_returned_untouched_and_not_retried(self, key):
        with patch.object(truein, "build_payload", return_value=_payload()), \
             patch.object(truein, "_do_push", return_value=_res(False, "Server exploded")) as push:
            out = truein.push_employee(MagicMock())
        assert out["success"] is False and out["message"] == "Server exploded" and push.call_count == 1
        assert out["dropped_fields"] == []

    def test_final_failure_after_dropping_still_reports_what_was_dropped(self, key):
        seq = [_res(False, "provide correct Site Point"), _res(False, "Something else broke")]
        with patch.object(truein, "build_payload", return_value=_payload()), \
             patch.object(truein, "_do_push", side_effect=seq):
            out = truein.push_employee(MagicMock())
        assert out["success"] is False and out["dropped_fields"] == ["sitePoint"]


# ── background retry loop ─────────────────────────────────────────────────────

def _active_request(db, **over):
    ini = _make_user("Tr Ini" + uuid.uuid4().hex[:4], f"trini{uuid.uuid4().hex[:6]}@t.com", UserRole.INITIATOR, db, companies=["RDC"])
    _make_user("Tr HHR" + uuid.uuid4().hex[:4], f"trhhr{uuid.uuid4().hex[:6]}@t.com", UserRole.HEAD_HR, db)
    r = OnboardingRequest(initiated_by=ini.id, status=RequestStatus.ACTIVE, public_token=uuid.uuid4().hex,
                          candidate_name="Retry Person", company_code="RDC", plant_location="P", designation="D", **over)
    db.session.add(r)
    db.session.commit()
    return r.id


class TestRetryLoop:
    def test_retries_with_backoff_until_success(self, app, db):
        rid = _active_request(db)
        results = [_res(False, "net down"), _res(False, "net down"), _res(True, empId="E7", dropped_fields=[])]
        with patch.object(truein, "push_employee", side_effect=results) as push, \
             patch.object(truein.time, "sleep") as sleep, patch.object(truein, "_notify_push_failed"):
            truein._retry_loop(app, rid)
        assert push.call_count == 3
        assert [c.args[0] for c in sleep.call_args_list] == [30, 60]            # exponential backoff
        req = _db.session.get(OnboardingRequest, rid)
        assert req.truein_pushed_at is not None and req.truein_push_error is None and req.truein_retry_count == 3
        assert TrueinPushLog.query.filter_by(request_id=rid).count() == 3

    def test_non_retryable_collision_stops_and_notifies(self, app, db):
        rid = _active_request(db)
        with patch.object(truein, "push_employee", return_value=_res(False, "Match found with X", retryable=False)) as push, \
             patch.object(truein.time, "sleep") as sleep:
            truein._retry_loop(app, rid)
        assert push.call_count == 1 and sleep.call_count == 0
        assert _db.session.get(OnboardingRequest, rid).truein_retry_stopped is True
        assert Notification.query.filter_by(request_id=rid).count() >= 1       # Head HR / admins were told

    def test_exception_from_push_counts_as_a_failed_attempt(self, app, db):
        rid = _active_request(db)
        with patch.object(truein, "push_employee", side_effect=[RuntimeError("dns"), _res(True, empId="E2", dropped_fields=[])]), \
             patch.object(truein.time, "sleep"):
            truein._retry_loop(app, rid)
        assert _db.session.get(OnboardingRequest, rid).truein_pushed_at is not None

    @pytest.mark.parametrize("over", [
        {"truein_retry_stopped": True},
        {"truein_pushed_at": datetime(2026, 1, 1)},
    ])
    def test_never_pushes_when_stopped_or_already_pushed(self, app, db, over):
        rid = _active_request(db, **over)
        with patch.object(truein, "push_employee") as push:
            truein._retry_loop(app, rid)
        push.assert_not_called()

    def test_never_pushes_a_request_that_is_no_longer_active(self, app, db):
        rid = _active_request(db)
        _db.session.get(OnboardingRequest, rid).status = RequestStatus.REJECTED_BH
        db.session.commit()
        with patch.object(truein, "push_employee") as push:
            truein._retry_loop(app, rid)
        push.assert_not_called()

    def test_thread_tracker_prevents_duplicates(self, app, db):
        rid = _active_request(db)
        with patch.object(truein.threading, "Thread") as thread:
            assert truein.start_retry_thread(app, rid) is True
            assert truein.start_retry_thread(app, rid) is False         # already live
        assert thread.call_count == 1
        truein._active_retry_threads.discard(rid)

    def test_resume_on_startup_only_picks_up_unpushed_active_requests(self, app, db):
        a = _active_request(db)                                           # to resume
        b = _active_request(db, truein_pushed_at=datetime(2026, 1, 1))     # done
        c = _active_request(db, truein_retry_stopped=True)                 # stopped by an admin
        started = []
        with patch.object(truein, "start_retry_thread", side_effect=lambda app_, rid: started.append(rid) or True):
            n = truein.resume_pending_retries(app)
        assert started == [a] and n == 1

    def test_backoff_grows_then_caps_at_one_hour(self):
        assert [truein._backoff(i) for i in range(9)] == [30, 60, 120, 240, 480, 960, 1920, 3600, 3600]


# ── paginated employee pull ───────────────────────────────────────────────────

class TestEmployeePull:
    @pytest.fixture(autouse=True)
    def _fresh_cache(self, monkeypatch):
        monkeypatch.setattr(truein, "_employees_cache", None)
        monkeypatch.setattr(truein, "_employees_cache_at", 0.0)
        monkeypatch.setattr(truein, "SUBSCRIPTION_KEY", "test-key")
        monkeypatch.setattr(truein.time, "sleep", lambda s: None)

    def _resp(self, body):
        r = MagicMock()
        r.json.return_value = body
        r.raise_for_status.return_value = None
        return r

    def test_follows_last_uid_until_no_more_rows(self):
        pages = [self._resp({"data": [{"empId": "1"}, {"empId": "2"}], "more_rows": "1", "last_uid": "u2"}),
                 self._resp({"data": [{"empId": "3"}], "more_rows": "0"})]
        with patch.object(truein.requests, "get", side_effect=pages) as get:
            rows = truein._fetch_all_employees_raw()
        assert [e["empId"] for e in rows] == ["1", "2", "3"]
        assert get.call_args_list[0].kwargs["params"] == {} and get.call_args_list[1].kwargs["params"] == {"lastUid": "u2"}

    def test_stops_if_the_cursor_does_not_advance(self):
        page = self._resp({"data": [{"empId": "1"}], "more_rows": "1", "last_uid": "same"})
        with patch.object(truein.requests, "get", side_effect=[page, page, page]) as get:
            truein._fetch_all_employees_raw()
        assert get.call_count == 2                      # second call repeats the cursor -> loop breaks

    def test_result_is_cached(self):
        page = self._resp({"data": [{"empId": "1"}], "more_rows": "0"})
        with patch.object(truein.requests, "get", return_value=page) as get:
            truein._fetch_all_employees_raw()
            truein._fetch_all_employees_raw()
        assert get.call_count == 1
        assert truein.get_cached_employees_if_warm() == [{"empId": "1"}]

    def test_cold_cache_reports_none_and_http_errors_propagate(self):
        assert truein.get_cached_employees_if_warm() is None
        bad = MagicMock()
        bad.raise_for_status.side_effect = RuntimeError("503")
        with patch.object(truein.requests, "get", return_value=bad):
            with pytest.raises(RuntimeError):
                truein._fetch_all_employees_raw()
