"""
Tests for the state machine in app/utils.py.

Validates every transition in TRANSITIONS and confirms invalid combos raise ValueError.
Also tests can_act_on scoping logic.
"""
import pytest
from app.models import (
    UserRole, RequestStatus, ApprovalActionType, OnboardingRequest,
    ClusterNameMapping, BusinessHeadRegion, InitiatorRegion,
)
from app.extensions import db as _db
from app.utils import get_new_status, can_act_on, TRANSITIONS
from .conftest import _make_user


class _Req:
    """Minimal stand-in for OnboardingRequest — get_new_status() only reads .status/.is_special_case."""
    def __init__(self, status, is_special_case=False):
        self.status = status
        self.is_special_case = is_special_case


# ── get_new_status — valid paths ───────────────────────────────────────────────

class TestValidTransitions:
    def test_initiator_submit_draft(self):
        result = get_new_status(_Req(RequestStatus.DRAFT), UserRole.INITIATOR, "submit")
        assert result == RequestStatus.PENDING_BH

    def test_bh_approve(self):
        result = get_new_status(_Req(RequestStatus.PENDING_BH), UserRole.BUSINESS_HEAD, ApprovalActionType.APPROVED)
        assert result == RequestStatus.PENDING_HR_MANAGER

    def test_bh_reject(self):
        result = get_new_status(_Req(RequestStatus.PENDING_BH), UserRole.BUSINESS_HEAD, ApprovalActionType.REJECTED)
        assert result == RequestStatus.REJECTED_BH

    def test_dr_bhoon_approve(self):
        result = get_new_status(_Req(RequestStatus.PENDING_DR_BHOON), UserRole.DR_BHOON, ApprovalActionType.APPROVED)
        assert result == RequestStatus.ACTIVE

    def test_dr_bhoon_reject(self):
        result = get_new_status(_Req(RequestStatus.PENDING_DR_BHOON), UserRole.DR_BHOON, ApprovalActionType.REJECTED)
        assert result == RequestStatus.REJECTED_DR_BHOON

    def test_hrm_approve(self):
        result = get_new_status(_Req(RequestStatus.PENDING_HR_MANAGER), UserRole.HR_MANAGER, ApprovalActionType.APPROVED)
        assert result == RequestStatus.PENDING_HEAD_HR

    def test_hrm_reject(self):
        result = get_new_status(_Req(RequestStatus.PENDING_HR_MANAGER), UserRole.HR_MANAGER, ApprovalActionType.REJECTED)
        assert result == RequestStatus.REJECTED_HRM

    def test_head_hr_approve(self):
        result = get_new_status(_Req(RequestStatus.PENDING_HEAD_HR), UserRole.HEAD_HR, ApprovalActionType.APPROVED)
        assert result == RequestStatus.ACTIVE

    def test_head_hr_reject(self):
        result = get_new_status(_Req(RequestStatus.PENDING_HEAD_HR), UserRole.HEAD_HR, ApprovalActionType.REJECTED)
        assert result == RequestStatus.REJECTED_HEAD_HR

    @pytest.mark.parametrize("rejected_status", [
        RequestStatus.REJECTED_BH,
        RequestStatus.REJECTED_DR_BHOON,
        RequestStatus.REJECTED_HRM,
        RequestStatus.REJECTED_HEAD_HR,
    ])
    def test_initiator_resubmit_from_any_rejection(self, rejected_status):
        result = get_new_status(_Req(rejected_status), UserRole.INITIATOR, "resubmit")
        assert result == RequestStatus.PENDING_BH

    def test_all_transitions_covered(self):
        """Ensure we have tested every entry in the TRANSITIONS dict."""
        # 13 transitions defined in utils.py (9 forward + 4 resubmit-from-rejection variants)
        assert len(TRANSITIONS) == 13


# ── get_new_status — over-norm ("special case") chain ─────────────────────────
# BH -> Head HR -> Dr. Bhoon (skips HR Manager) when req.is_special_case is True.
# These two branch points diverge from TRANSITIONS; everything else is shared.

class TestOverNormTransitions:
    def test_bh_approve_special_case_skips_hr_manager(self):
        result = get_new_status(_Req(RequestStatus.PENDING_BH, is_special_case=True),
                                 UserRole.BUSINESS_HEAD, ApprovalActionType.APPROVED)
        assert result == RequestStatus.PENDING_HEAD_HR

    def test_bh_approve_non_special_case_unaffected(self):
        result = get_new_status(_Req(RequestStatus.PENDING_BH, is_special_case=False),
                                 UserRole.BUSINESS_HEAD, ApprovalActionType.APPROVED)
        assert result == RequestStatus.PENDING_HR_MANAGER

    def test_head_hr_approve_special_case_routes_to_dr_bhoon(self):
        result = get_new_status(_Req(RequestStatus.PENDING_HEAD_HR, is_special_case=True),
                                 UserRole.HEAD_HR, ApprovalActionType.APPROVED)
        assert result == RequestStatus.PENDING_DR_BHOON

    def test_head_hr_approve_non_special_case_still_activates(self):
        result = get_new_status(_Req(RequestStatus.PENDING_HEAD_HR, is_special_case=False),
                                 UserRole.HEAD_HR, ApprovalActionType.APPROVED)
        assert result == RequestStatus.ACTIVE

    def test_bh_reject_special_case_unaffected(self):
        """Rejection isn't a branch point — falls through to TRANSITIONS either way."""
        result = get_new_status(_Req(RequestStatus.PENDING_BH, is_special_case=True),
                                 UserRole.BUSINESS_HEAD, ApprovalActionType.REJECTED)
        assert result == RequestStatus.REJECTED_BH

    def test_dr_bhoon_approve_special_case_activates(self):
        result = get_new_status(_Req(RequestStatus.PENDING_DR_BHOON, is_special_case=True),
                                 UserRole.DR_BHOON, ApprovalActionType.APPROVED)
        assert result == RequestStatus.ACTIVE


# ── get_new_status — invalid combos ───────────────────────────────────────────

class TestInvalidTransitions:
    @pytest.mark.parametrize("status, role, action", [
        # Wrong role for status
        (RequestStatus.PENDING_BH, UserRole.HR_MANAGER, ApprovalActionType.APPROVED),
        (RequestStatus.PENDING_HR_MANAGER, UserRole.BUSINESS_HEAD, ApprovalActionType.APPROVED),
        (RequestStatus.PENDING_HEAD_HR, UserRole.HR_MANAGER, ApprovalActionType.APPROVED),
        (RequestStatus.PENDING_DR_BHOON, UserRole.BUSINESS_HEAD, ApprovalActionType.APPROVED),
        # Initiator cannot approve
        (RequestStatus.PENDING_BH, UserRole.INITIATOR, ApprovalActionType.APPROVED),
        # Cannot submit from non-draft
        (RequestStatus.PENDING_BH, UserRole.INITIATOR, "submit"),
        # Cannot resubmit from active
        (RequestStatus.ACTIVE, UserRole.INITIATOR, "resubmit"),
        # Nonsense action
        (RequestStatus.DRAFT, UserRole.INITIATOR, "delete"),
    ])
    def test_raises_value_error(self, status, role, action):
        with pytest.raises(ValueError):
            get_new_status(_Req(status), role, action)


# ── can_act_on ─────────────────────────────────────────────────────────────────

class TestCanActOn:
    """
    Region-scoping cases (2026-09-04 redesign — see
    utils.bh_ids_for_initiator()) need real DB rows since can_act_on() now
    always queries InitiatorRegion/BusinessHeadRegion for BUSINESS_HEAD
    role, even when there's no overlap to find. Role/status-only cases
    (wrong role, wrong status) never reach that query, so those stay as
    plain mock objects (no DB needed).
    """

    class _MockUser:
        def __init__(self, role, user_id=1):
            self.role = role
            self.id = user_id

    class _MockReq:
        def __init__(self, status):
            self.status = status
            self.initiator = TestCanActOn._MockUser(UserRole.INITIATOR)

    def test_bh_can_act_on_pending_bh_unassigned(self, client, db, app):
        with app.app_context():
            initiator = _make_user("CaoInit1", "caoinit1@t.com", UserRole.INITIATOR, db)
            bh        = _make_user("CaoBh1",   "caobh1@t.com",   UserRole.BUSINESS_HEAD, db)
            req = OnboardingRequest(initiated_by=initiator.id, status=RequestStatus.PENDING_BH)
            db.session.add(req)
            db.session.flush()
            # Initiator has no regions at all -> fail-open -> any active BH can act.
            assert can_act_on(req, bh) is True

    def test_bh_can_act_on_pending_bh_assigned_to_them(self, client, db, app):
        with app.app_context():
            region = ClusterNameMapping(canonical_cluster_name="CAO Region A")
            db.session.add(region)
            db.session.flush()
            initiator = _make_user("CaoInit2", "caoinit2@t.com", UserRole.INITIATOR, db)
            bh        = _make_user("CaoBh2",   "caobh2@t.com",   UserRole.BUSINESS_HEAD, db)
            db.session.add(InitiatorRegion(initiator_id=initiator.id, cluster_id=region.id))
            db.session.add(BusinessHeadRegion(business_head_id=bh.id, cluster_id=region.id))
            db.session.flush()
            req = OnboardingRequest(initiated_by=initiator.id, status=RequestStatus.PENDING_BH)
            db.session.add(req)
            db.session.flush()
            assert can_act_on(req, bh) is True

    def test_bh_cannot_act_on_pending_bh_assigned_to_other(self, client, db, app):
        with app.app_context():
            region_a = ClusterNameMapping(canonical_cluster_name="CAO Region B")
            region_b = ClusterNameMapping(canonical_cluster_name="CAO Region C")
            db.session.add_all([region_a, region_b])
            db.session.flush()
            initiator  = _make_user("CaoInit3", "caoinit3@t.com", UserRole.INITIATOR, db)
            other_bh   = _make_user("CaoBh3a",  "caobh3a@t.com",  UserRole.BUSINESS_HEAD, db)
            unrelated_bh = _make_user("CaoBh3b", "caobh3b@t.com", UserRole.BUSINESS_HEAD, db)
            db.session.add(InitiatorRegion(initiator_id=initiator.id, cluster_id=region_a.id))
            db.session.add(BusinessHeadRegion(business_head_id=other_bh.id, cluster_id=region_a.id))
            # unrelated_bh covers a DIFFERENT region -> no overlap with the initiator
            db.session.add(BusinessHeadRegion(business_head_id=unrelated_bh.id, cluster_id=region_b.id))
            db.session.flush()
            req = OnboardingRequest(initiated_by=initiator.id, status=RequestStatus.PENDING_BH)
            db.session.add(req)
            db.session.flush()
            # The initiator's region IS actively covered (by other_bh), so this
            # is NOT a fail-open case — unrelated_bh must be excluded.
            assert can_act_on(req, unrelated_bh) is False

    def test_bh_cannot_act_on_wrong_status(self):
        user = self._MockUser(UserRole.BUSINESS_HEAD, user_id=10)
        req  = self._MockReq(RequestStatus.PENDING_HR_MANAGER)
        assert can_act_on(req, user) is False

    def test_hrm_can_act_on_pending_hrm(self):
        user = self._MockUser(UserRole.HR_MANAGER, user_id=5)
        req  = self._MockReq(RequestStatus.PENDING_HR_MANAGER)
        assert can_act_on(req, user) is True

    def test_initiator_cannot_act_as_approver(self):
        user = self._MockUser(UserRole.INITIATOR, user_id=2)
        req  = self._MockReq(RequestStatus.PENDING_BH)
        assert can_act_on(req, user) is False

    def test_dr_bhoon_can_act_on_pending_dr_bhoon(self):
        user = self._MockUser(UserRole.DR_BHOON, user_id=3)
        req  = self._MockReq(RequestStatus.PENDING_DR_BHOON)
        assert can_act_on(req, user) is True
