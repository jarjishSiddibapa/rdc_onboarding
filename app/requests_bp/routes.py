import os
import re
import time
import uuid
import random
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeoutError
from datetime import datetime, date as _date
from flask import render_template, redirect, url_for, flash, request, current_app, abort, session, jsonify
from flask_login import login_required, current_user
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import SQLAlchemyError
from ..extensions import db
from ..models import (
    OnboardingRequest, RequestStatus, ApprovalAction,
    ApprovalActionType, UserRole, User, FormField, FieldType, OptionsSource,
    Designation, StaffingGateCheck, GateResult, PlantDvtMapping,
    PlantLocation, COMPANY_CHOICES,
)
from ..extensions import limiter
from ..utils import (
    role_required, get_new_status, can_act_on,
    notify_users, allowed_file, validate_mime, REJECTED_STATUSES, log_audit,
    get_db_mail_config,
)
from . import requests_bp


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_active_fields(step=None):
    """Return active FormField records, optionally filtered by step."""
    q = FormField.query.filter_by(is_active=True, is_deleted=False).order_by(FormField.step, FormField.sort_order)
    if step:
        q = q.filter_by(step=step)
    return q.all()


def _save_file(file_obj):
    ext = file_obj.filename.rsplit(".", 1)[-1].lower()
    stored = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], stored)
    file_obj.save(path)
    url = url_for("static", filename=f"uploads/{stored}")
    return file_obj.filename, stored, url


def _collect_form_data(fields, existing=None):
    """Pull non-file field values from request.form, return dict."""
    data = dict(existing or {})
    for f in fields:
        if f.field_type == FieldType.FILE:
            continue
        val = request.form.get(f.field_key, "").strip()
        data[f.field_key] = val
        # Capture "other" text if allow_other and value is "Other"
        if f.allow_other and val == "Other":
            other_val = request.form.get(f"{f.field_key}_other", "").strip()
            data[f"{f.field_key}_other"] = other_val
        # l1_manager_emp_id (confirmed bug, found 2026-09-24 while
        # investigating real requests #37/#38 showing no Manager in
        # Truein): companion hidden input to reporting_manager_code, set by
        # the Truein-manager-typeahead JS in form.html only when the
        # initiator picks a VALIDATED match. It has never been an
        # admin-configured FormField, so this loop — which only reads keys
        # from `fields` — silently dropped it on every save, even when the
        # initiator correctly picked a validated manager and the client-side
        # "must pick a match" check passed. Truein's own "Manager" column
        # reflects only this validated link, never the free-text `manager`/
        # reporting_manager_name field (see app/integrations/truein.py's
        # FIELD_MAP manager_emp_id note) — so every push before this fix
        # sent a name but never the piece Truein actually displays.
        if f.field_key == "reporting_manager_code":
            data["l1_manager_emp_id"] = request.form.get("l1_manager_emp_id", "").strip()
    return data


def _collect_documents(fields, existing_docs):
    """Handle file uploads for the step; returns updated docs list."""
    docs = list(existing_docs or [])
    for f in fields:
        if f.field_type != FieldType.FILE:
            continue
        file_obj = request.files.get(f.field_key)
        if file_obj and file_obj.filename and allowed_file(file_obj.filename) and validate_mime(file_obj):
            orig, stored, url = _save_file(file_obj)
            docs = [d for d in docs if d.get("type") != f.field_key]
            docs.append({
                "type": f.field_key,
                "label": f.field_label,
                "name": orig,
                "filename": stored,
                "url": url,
            })
    return docs


def _sync_quick_access(req):
    """Populate convenience columns from form_data for table display."""
    fd = req.form_data
    req.candidate_name = fd.get("associate_name", "") or ""
    req.company_code = fd.get("company_code", "") or ""
    req.plant_location = fd.get("plant_location", "") or ""
    req.designation = fd.get("designation", "") or ""
    req.candidate_email = (fd.get("email_id") or "").strip().lower()
    req.candidate_govt_id = re.sub(r"\D", "", fd.get("aadhar_no") or "")


def _validate_required(fields, form_data):
    """Return list of missing required field labels."""
    missing = []
    for f in fields:
        if f.is_required and f.field_type != FieldType.FILE and not f.is_readonly:
            val = form_data.get(f.field_key, "").strip()
            if not val:
                missing.append(f.field_label)
    return missing


def _finalize_submission(req):
    """Shared tail for submit_request()/submit_as_special_case(): set
    PENDING_BH, log, commit, notify the Business Head. Returns True on
    success (already flashed); on failure, flashes an error and returns False."""
    req.status = RequestStatus.PENDING_BH
    req.updated_at = datetime.utcnow()
    log_audit("REQUEST", "REQUEST_SUBMITTED",
              resource_type="OnboardingRequest", resource_id=req.id,
              resource_label=f"Request #{req.id} — {req.candidate_name}",
              detail={"candidate_name": req.candidate_name,
                      "designation": req.designation,
                      "company": req.company_code,
                      "plant": req.plant_location,
                      "is_special_case": req.is_special_case})
    try:
        db.session.commit()
        bh_users = _get_bh_recipients(db.session.get(User, req.initiated_by), req.company_code)
        notify_users(db, req, bh_users,
                     subject=f"New onboarding request: {req.candidate_name}",
                     body=f"A new onboarding request for {req.candidate_name} ({req.designation}) "
                          f"has been submitted and requires your approval."
                          + (" This request is proceeding as a special case outside staffing norms — "
                             "a justification will be required at every approval step."
                             if req.is_special_case else ""))
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("An unexpected error occurred. Please try again.", "danger")
        return False
    flash("Request submitted for Business Head approval.", "success")
    return True


def _get_bh_recipients(initiator_user, company_code):
    """Return the list of BH users to notify for a given initiator/company —
    see utils.bh_ids_for_initiator() for the company-scope/region/fail-open
    priority order. Fail-closed: an empty result means nobody is currently
    ticked for this company — see _validate_approver_availability(), which
    blocks the submission before it ever reaches this point."""
    from ..utils import bh_ids_for_initiator
    ids = bh_ids_for_initiator(initiator_user, company_code)
    return User.query.filter(User.id.in_(ids)).all() if ids else []


def _validate_approver_availability(req):
    """
    Fail-closed guard (company-scope tick marks default to NOTHING until an
    admin explicitly configures them, 2026-09-21): block a submission that
    would land in a PENDING_BH or PENDING_HR_MANAGER queue nobody can act
    on, rather than silently creating a request no approver can ever see.
    Call after req.is_special_case has been finalized by the RDC staffing
    gate check (so the HR Manager check correctly skips an RDC special-case
    request, which never visits PENDING_HR_MANAGER). Returns an error
    string to flash, or None if the request is fully reachable.
    """
    from ..utils import bh_ids_for_initiator, hr_manager_ids_for_company
    initiator = db.session.get(User, req.initiated_by)
    company = req.form_data.get("company_code", "")
    if not bh_ids_for_initiator(initiator, company):
        return (f"No Business Head is currently configured for {company}. "
                f"Contact your administrator before submitting this request.")
    visits_hr_manager = (company != "RDC") or not req.is_special_case
    if visits_hr_manager and not hr_manager_ids_for_company(company):
        return (f"No HR Manager is currently configured for {company}. "
                f"Contact your administrator before submitting this request.")
    return None


# ── RDC staffing gate helper ────────────────────────────────────────────────────

def _persist_gate_check(req, gate: dict) -> StaffingGateCheck:
    """Writes one StaffingGateCheck row from check_rdc_staffing_gate()'s result dict. Caller commits."""
    import json
    d = gate.get("details", {})
    result = GateResult.ERROR if gate.get("reason") == "error" else (
        GateResult.ALLOWED if gate.get("allowed") else GateResult.BLOCKED
    )
    check = StaffingGateCheck(
        request_id=req.id,
        plant_name=d.get("plant_name"),
        cluster_name=d.get("cluster_name"),
        norm_role_category_id=d.get("norm_role_category_id"),
        tier_label=d.get("tier_label"),
        volume_used=d.get("volume_used"),
        current_headcount=d.get("current_headcount"),
        allowed_headcount=d.get("allowed_headcount"),
        snapshot_computed_at=(
            datetime.fromisoformat(d["snapshot_computed_at"]) if d.get("snapshot_computed_at") else None
        ),
        result=result,
        detail=json.dumps({"reason": gate.get("reason"), **d}, default=str),
    )
    db.session.add(check)
    db.session.flush()
    return check


def _bh_region_ids(user) -> set[int]:
    """Cluster ids a Business Head is scoped to on the RDC staffing dashboard."""
    from ..models import BusinessHeadRegion
    return {r.cluster_id for r in BusinessHeadRegion.query.filter_by(business_head_id=user.id)}


def _staffing_company_scope(user):
    """
    Company-scope gate for every Staffing Status route (added 2026-09-23 —
    these pages predate the company-scope tick-mark feature and were never
    updated to respect it: a Business Head or HR Manager ticked only for
    ROBO could still browse full RDC/Ultrafine data here). Returns None for
    unscoped roles (HEAD_HR/DR_BHOON/SUPER_ADMIN — see any of their
    company's data), otherwise the set of companies this user is ticked
    for (possibly empty). Every staffing-status route must check this
    BEFORE returning any company's data, not just filter what a template
    happens to render — a scoped-out user must get a 403/404 on direct URL
    access too, not just a hidden tab.
    """
    from ..utils import company_scope_ids
    if user.role in (UserRole.BUSINESS_HEAD, UserRole.HR_MANAGER):
        return company_scope_ids(user.id)
    return None


def _special_case_counts_by_category(plant_name: str) -> dict:
    """Count of ACTIVE, is_special_case requests at this plant, by norm_category_id."""
    rows = (
        db.session.query(Designation.norm_category_id, db.func.count(OnboardingRequest.id))
        .join(Designation, Designation.name == OnboardingRequest.designation)
        .filter(OnboardingRequest.plant_location == plant_name,
                OnboardingRequest.status == RequestStatus.ACTIVE,
                OnboardingRequest.is_special_case == True,  # noqa: E712
                OnboardingRequest.is_deleted == False,      # noqa: E712
                Designation.norm_category_id.isnot(None))
        .group_by(Designation.norm_category_id)
        .all()
    )
    return dict(rows)


# ── Lookup helper ─────────────────────────────────────────────────────────────

def _get_req_by_token(token):
    """Return OnboardingRequest by public_token or 404 (with eager-loaded relationships)."""
    req = OnboardingRequest.query.options(
        selectinload(OnboardingRequest.actions).selectinload(ApprovalAction.actor),
        selectinload(OnboardingRequest.initiator),
    ).filter_by(public_token=token, is_deleted=False).first_or_404()
    return req


def _check_email_registered(email: str, exclude_token: str | None = None) -> str | None:
    """
    Best-effort early duplicate-email check for the onboarding form. Returns a
    human-readable reason if the email looks already registered, else None.
    Not authoritative — Truein's own response at push time (already hardened
    against its false-positive "already exist" wording) remains the final say.

    Checks, in order:
      1. Our own DB — any other non-deleted, non-rejected request already
         using this email, via the indexed candidate_email column (kept in
         sync by _sync_quick_access() on every form save) rather than
         loading and JSON-parsing every request row on every field blur.
      2. Truein's warm employee cache ONLY (get_cached_employees_if_warm never
         triggers a live pull — see CLAUDE.md gotcha #1). A cold cache simply
         skips this half rather than blocking the initiator on a multi-minute
         pull inside an interactive request.
    """
    email = (email or "").strip().lower()
    if not email:
        return None

    dup_q = OnboardingRequest.query.filter_by(is_deleted=False, candidate_email=email).filter(
        OnboardingRequest.status.notin_(REJECTED_STATUSES))
    if exclude_token:
        dup_q = dup_q.filter(OnboardingRequest.public_token != exclude_token)
    if dup_q.first():
        return "This email is already used on another onboarding request in this system."

    from ..integrations import truein
    for e in (truein.get_cached_employees_if_warm() or []):
        if (e.get("email") or "").strip().lower() == email:
            return "This email appears to already be registered in Truein as an existing employee."

    return None


def _check_govt_id_registered(aadhar_no: str, exclude_token: str | None = None) -> str | None:
    """
    Best-effort early duplicate-Govt-ID check for the onboarding form —
    the same "show the problem immediately" pattern as _check_email_registered
    above, added 2026-09-10 after a real live collision (request #32, "Barkha
    Patil") was only discovered at final-approval Truein push time: "Govt ID
    already exist. Match found with GULSHAN KUMAR(...) in RDC Concrete". By
    then the request had already gone through the entire multi-step approval
    chain — the initiator should know the moment they type a number that
    collides, not days later after everyone else has already signed off.
    Not authoritative — Truein's own response at push time remains the final
    say (and is already hardened, see _parse_truein_response()'s "match
    found" collision handling), but this catches the common case immediately.

    Checks, in order (mirrors _check_email_registered exactly):
      1. Our own DB — any other non-deleted, non-rejected request already
         using this Aadhar number, via the indexed candidate_govt_id column
         (kept in sync by _sync_quick_access() on every form save) rather
         than loading and JSON-parsing every request row on every blur.
      2. Truein's warm employee cache ONLY (never triggers a live pull).
    Compares digits-only so formatting (spaces/dashes) never causes a
    false negative — candidate_govt_id is stored digits-only for exactly
    this reason.
    """
    digits = re.sub(r"\D", "", aadhar_no or "")
    if len(digits) != 12:
        return None  # not a complete number yet — pattern validation handles format separately

    dup_q = OnboardingRequest.query.filter_by(is_deleted=False, candidate_govt_id=digits).filter(
        OnboardingRequest.status.notin_(REJECTED_STATUSES))
    if exclude_token:
        dup_q = dup_q.filter(OnboardingRequest.public_token != exclude_token)
    if dup_q.first():
        return "This Aadhar number is already used on another onboarding request in this system."

    from ..integrations import truein
    for e in (truein.get_cached_employees_if_warm() or []):
        existing_id = re.sub(r"\D", "", str(e.get("id_number") or ""))
        if existing_id and existing_id == digits:
            name = (e.get("name") or "").strip()
            code = (e.get("empId") or "").strip()
            who = f" — matches existing employee {name} ({code})" if name else ""
            return f"This Aadhar number appears to already be registered in Truein{who}."

    return None


# ── Email OTP (candidate email verification) ──────────────────────────────────

# smtplib's own `timeout=` (see _send_smtp) only bounds each individual
# socket read/write — NOT the DNS lookup that happens before the socket
# even exists (a real Python/stdlib gotcha, not specific to this app). On a
# factory LAN where outbound SMTP is flaky or a DNS resolver hangs instead
# of failing fast, that left send_email_otp() able to block the whole
# request — and the DB connection it holds for the request's lifetime —
# for far longer than 30s, with the browser's "Sending…" button never
# resolving either way (confirmed incident, 2026-09-24: reported as the
# button going "into a loop", then a stuck blank page on refresh). Running
# the send in this dedicated worker pool and bounding it with .result()
# guarantees the HTTP response — and the DB connection — is never held
# past _OTP_EMAIL_TIMEOUT, no matter what hangs on the SMTP/DNS side. A
# separate small pool (not the main Werkzeug request threads, not the DB
# connection pool) also means a handful of stuck sends can only ever queue
# future OTP sends, never stall unrelated page loads.
_OTP_EMAIL_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="otp-email")
_OTP_EMAIL_TIMEOUT = 30  # seconds — comfortably more than any legitimate SMTP send needs (those normally finish in a few seconds), but still short enough that a real hang gives feedback before it feels broken again (2026-09-24)


@requests_bp.route("/send-email-otp", methods=["POST"])
@login_required
@role_required(UserRole.INITIATOR)
@limiter.limit("10 per hour")
def send_email_otp():
    """Send a 6-digit OTP to the candidate email to prove it exists.

    Uses synchronous SMTP (via a bounded worker, see _OTP_EMAIL_EXECUTOR
    above) so failures are caught and returned immediately — async
    send_email() would silently swallow errors and show a false 'OTP sent'.
    """
    from ..utils import _send_smtp
    email = request.form.get("email", "").strip().lower()
    token = request.form.get("token", "").strip() or None
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"ok": False, "error": "Enter a valid email address first."})
    dup_reason = _check_email_registered(email, exclude_token=token)
    if dup_reason:
        return jsonify({"ok": False, "duplicate": True, "error": dup_reason})
    cfg = get_db_mail_config()
    if not cfg["username"]:
        return jsonify({"ok": False, "error": "Email service is not configured on this server. Contact the administrator."})
    otp = f"{random.randint(0, 999999):06d}"
    future = _OTP_EMAIL_EXECUTOR.submit(
        _send_smtp, cfg, [email],
        "Email Verification OTP — RDC Teamlease Onboarding",
        f"Your OTP for email verification is: {otp}\n\n"
        f"Valid for 10 minutes. Do not share it with anyone.\n\n"
        f"— RDC Teamlease HR Onboarding Portal",
    )
    try:
        future.result(timeout=_OTP_EMAIL_TIMEOUT)
    except _FutureTimeoutError:
        return jsonify({"ok": False, "error": "The email server is taking too long to respond. Please wait a moment and try again."})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not send OTP email: {exc}"})
    # Store in session only after confirmed delivery
    session["_email_otp"] = {"email": email, "code": otp, "at": time.time(), "verified": False}
    return jsonify({"ok": True, "msg": f"OTP sent to {email}. Check inbox (and spam folder)."})


@requests_bp.route("/verify-email-otp", methods=["POST"])
@login_required
@role_required(UserRole.INITIATOR)
@limiter.limit("30 per hour")
def verify_email_otp():
    """
    Check the OTP entered against what was sent, and persist the result onto
    the request itself (candidate_email_verified) — not just the Flask
    session (session['_email_otp_verified'], still set too, for any
    same-request-object-not-yet-available caller). Session-only storage
    meant any session loss (the 10-min inactivity auto-logout, resuming the
    draft from a different device, or just a long enough gap between
    sessions) forced re-verifying an email already proven once for this
    exact draft — a real reported gap (2026-09-23), since "save as draft,
    come back later" is a normal, expected flow for this multi-step form.
    """
    otp_input = request.form.get("otp", "").strip()
    email     = request.form.get("email", "").strip().lower()
    token     = request.form.get("token", "").strip() or None
    stored    = session.get("_email_otp", {})
    if not stored:
        return jsonify({"ok": False, "error": "No OTP has been sent. Click 'Send OTP' first."})
    if stored.get("email") != email:
        return jsonify({"ok": False, "error": "Email mismatch — re-send OTP for this email address."})
    if time.time() - stored.get("at", 0) > 600:
        return jsonify({"ok": False, "error": "OTP expired (10 min limit). Request a new one."})
    if stored.get("code") != otp_input:
        return jsonify({"ok": False, "error": "Incorrect OTP. Please try again."})
    session["_email_otp"]["verified"] = True
    session["_email_otp_verified"] = email
    if token:
        req = OnboardingRequest.query.filter_by(public_token=token, is_deleted=False).first()
        if req and req.initiated_by == current_user.id:
            req.candidate_email_verified = email
            db.session.commit()
    return jsonify({"ok": True, "msg": "Email verified!"})


@requests_bp.route("/check-govt-id", methods=["POST"])
@login_required
@role_required(UserRole.INITIATOR)
def check_govt_id():
    """
    Live duplicate check for the Aadhar Number field, fired on blur (see
    form.html's checkGovtIdDuplicate()) — same "show the problem immediately"
    pattern as send_email_otp()'s duplicate-email check, added 2026-09-10.
    """
    aadhar = request.form.get("aadhar_no", "").strip()
    token  = request.form.get("token", "").strip() or None
    dup_reason = _check_govt_id_registered(aadhar, exclude_token=token)
    if dup_reason:
        return jsonify({"ok": False, "duplicate": True, "error": dup_reason})
    return jsonify({"ok": True})


# ── Truein lookup proxies ─────────────────────────────────────────────────────

def _dvt_matched_plant_options() -> list[dict]:
    """
    Plant Location dropdown options for the onboarding form — restricted to
    plants we actually have a confirmed Daily Volume Tracker mapping for
    (AUTO_EXACT or admin-set MANUAL, i.e. dvt_plant_code is set), so an
    initiator can never pick a raw/unverified name the staffing gate can't
    resolve. `value` stays PlantDvtMapping.plant_location_name (the internal
    key every other lookup in the app — the gate, the snapshot, admin
    display — joins on), `label` is the proper "ERP-Tracker" display name
    (see PlantDvtMapping.display_name) used consistently everywhere in the
    app a plant name is shown to a user. `cluster` is the plant's
    region/cluster name (or "Other" if never clustered) — used to drive the
    form's Cluster -> Plant cascading picker so the initiator isn't
    scrolling one 139-long flat list.
    """
    rows = (PlantDvtMapping.query
            .filter_by(is_deleted=False)
            .filter(PlantDvtMapping.dvt_plant_code.isnot(None))
            .order_by(PlantDvtMapping.plant_location_name)
            .all())
    return [{
        "value": p.plant_location_name,
        "label": p.display_name,
        "cluster": p.cluster.canonical_cluster_name if p.cluster else "Other",
    } for p in rows]


def _company_plant_options(company: str) -> list[dict]:
    """
    Plant Location dropdown options for Ultrafine/ROBO (added 2026-09-15,
    multi-company support) — a flat list of that company's active
    PlantLocation rows, no DVT reconciliation and no region/cluster
    grouping (neither company has one). `cluster` is always None so the
    form's cascading Cluster -> Plant picker collapses to a single flat
    list for these companies, the same "Other" no-cluster fallback path it
    already has for any RDC plant with no cluster assigned.
    """
    rows = (PlantLocation.query
            .filter_by(is_deleted=False, is_active=True, company=company)
            .order_by(PlantLocation.sort_order, PlantLocation.name)
            .all())
    return [{"value": p.name, "label": p.name, "cluster": None} for p in rows]


@requests_bp.route("/api/plant-locations")
@login_required
def plant_locations_api():
    company = request.args.get("company", "RDC")
    if company not in COMPANY_CHOICES:
        company = "RDC"
    options = _dvt_matched_plant_options() if company == "RDC" else _company_plant_options(company)
    return jsonify({"ok": True, "data": options})


@requests_bp.route("/api/check-hiring-capacity")
@login_required
@role_required(UserRole.INITIATOR)
def check_hiring_capacity():
    """
    Live pre-check while the initiator is still filling the form — as soon as
    plant + designation are both picked, ask the same RDC staffing gate used
    at Submit (see check_rdc_staffing_gate) whether hiring is still possible
    there, so a full plant is flagged immediately instead of only after
    filling out the whole form. Read-only: unlike submit_request/
    resubmit_request, this never writes a StaffingGateCheck row — it's a
    preview, not a recorded decision, and the real block still happens
    server-side at Submit regardless of what this endpoint says.
    """
    company_code = request.args.get("company_code", "").strip()
    designation = request.args.get("designation", "").strip()
    plant_location = request.args.get("plant_location", "").strip()
    if company_code != "RDC" or not designation or not plant_location:
        return jsonify({"ok": True, "allowed": True})
    from ..services.staffing_norms import check_rdc_staffing_gate
    gate = check_rdc_staffing_gate({
        "company_code": company_code,
        "designation": designation,
        "plant_location": plant_location,
    })
    return jsonify({"ok": True, "allowed": gate["allowed"], "reason": gate["reason"], "details": gate.get("details", {})})


@requests_bp.route("/<string:token>/acknowledge-special-case", methods=["POST"])
@login_required
@role_required(UserRole.INITIATOR)
def acknowledge_special_case(token):
    """
    Fired by the form popup's "OK, Proceed Anyway" button as soon as the
    initiator acknowledges a staffing-gate block while still filling the
    form. Marks the draft to route through the over-norm chain (BH -> Head
    HR -> Dr. Bhoon) once submitted. The real gate re-check at Submit is
    still authoritative — this is just the initiator's acknowledgment.
    """
    req = _get_req_by_token(token)
    if req.initiated_by != current_user.id:
        abort(403)
    req.is_special_case = True
    db.session.commit()
    return "", 204


@requests_bp.route("/api/truein/managers")
@login_required
def truein_managers():
    """
    Returns only Truein employees flagged as is_manager=1, read from cache
    only — never triggers a live Truein pull. fetch_managers() (used by the
    2-hourly background refresh) would otherwise start a multi-minute
    paginated fetch on a cold cache, leaving the manager-search picker
    stuck on "Searching Truein…" indefinitely and blocking the form.
    """
    from ..integrations.truein import get_managers_from_cache_only
    return jsonify({"ok": True, "data": get_managers_from_cache_only()})


# ── New Request (multi-step) ───────────────────────────────────────────────────

@requests_bp.route("/new", methods=["GET", "POST"])
@login_required
@role_required(UserRole.INITIATOR)
def new_request():
    step = request.args.get("step", 1, type=int) or 1
    token = request.args.get("token")

    if token:
        req = _get_req_by_token(token)
        if req.initiated_by != current_user.id:
            abort(403)
        if req.status not in (RequestStatus.DRAFT,) and req.status not in REJECTED_STATUSES:
            abort(400)
    else:
        req = OnboardingRequest(initiated_by=current_user.id, status=RequestStatus.DRAFT)
        db.session.add(req)
        db.session.flush()
        log_audit("REQUEST", "REQUEST_CREATED",
                  resource_type="OnboardingRequest", resource_id=req.id,
                  resource_label=f"Request #{req.id} (new draft)",
                  detail={"initiated_by": current_user.name})
        db.session.commit()
        return redirect(url_for("requests_bp.new_request", step=1, token=req.public_token))

    step_fields = _get_active_fields(step)
    all_fields = _get_active_fields()
    # Server-rendered fallback must already match the request's own saved
    # company — this used to be unconditionally _dvt_matched_plant_options()
    # (RDC-only), so a Ultrafine/ROBO draft's step 2/3 always rendered RDC's
    # cluster-plant picker on first paint, even before the client-side JS
    # re-fetch (see form.html) had a chance to correct it. If that fetch
    # ever failed, the wrong company's plants stayed on screen indefinitely.
    _req_company = req.form_data.get("company_code") or "RDC"
    if _req_company not in COMPANY_CHOICES:
        _req_company = "RDC"
    plants = _dvt_matched_plant_options() if _req_company == "RDC" else _company_plant_options(_req_company)
    designations = Designation.query.filter_by(is_active=True, is_deleted=False).order_by(Designation.sort_order, Designation.name).all()

    if request.method == "POST":
        action = request.form.get("action")
        _docs_before = list(req.documents)
        _prev_plant = req.form_data.get("plant_location")
        _prev_designation = req.form_data.get("designation")
        form_data = _collect_form_data(step_fields, req.form_data)
        docs = _collect_documents(step_fields, req.documents)

        # Detect newly uploaded files
        _before_keys = {d.get("type") for d in _docs_before}
        _new_files = [d for d in docs if d.get("type") not in _before_keys
                      or any(b.get("type") == d.get("type") and b.get("filename") != d.get("filename")
                             for b in _docs_before)]

        req.form_data = form_data
        req.documents = docs
        _sync_quick_access(req)

        # Changing plant/designation invalidates any prior "proceed anyway"
        # acknowledgment of a staffing-gate block — force a fresh popup.
        # Exception: this very save may BE the first save of a plant that was
        # just acknowledged client-side (the popup's "OK" fires a separate
        # acknowledge-special-case call, then this save merges plant_location
        # into the draft for the first time) — the hidden
        # _special_case_ack_pending marker distinguishes that case from an
        # unacknowledged change, so it isn't immediately undone here.
        _just_acknowledged = request.form.get("_special_case_ack_pending") == "1"
        if (form_data.get("plant_location") != _prev_plant
                or form_data.get("designation") != _prev_designation) and not _just_acknowledged:
            req.is_special_case = False

        # Log draft update
        _candidate = req.candidate_name or f"Request #{req.id}"
        log_audit("REQUEST", "REQUEST_DRAFT_UPDATED",
                  resource_type="OnboardingRequest", resource_id=req.id,
                  resource_label=f"Request #{req.id} — {_candidate}",
                  detail={"step": step, "form_action": action,
                          "candidate_name": req.candidate_name})

        # Log each newly uploaded file
        for _doc in _new_files:
            log_audit("REQUEST", "REQUEST_FILE_UPLOADED",
                      resource_type="OnboardingRequest", resource_id=req.id,
                      resource_label=f"Request #{req.id} — {_candidate}",
                      detail={"field": _doc.get("type"),
                              "field_label": _doc.get("label"),
                              "original_filename": _doc.get("name"),
                              "step": step})

        db.session.commit()

        # ── Step-specific validation before advancing ─────────────────────────
        if action == "next" and step == 1:
            # 1a. Email OTP — must be verified before leaving step 1. Checks
            # the DB-persisted candidate_email_verified first (durable across
            # session loss — see verify_email_otp()) and the session as a
            # fallback for the same request within the same page load.
            email_val = form_data.get("email_id", "").strip().lower()
            if (email_val and req.candidate_email_verified != email_val
                    and session.get("_email_otp_verified", "") != email_val):
                flash("Please verify the candidate's email address with OTP before proceeding.", "danger")
                return redirect(url_for("requests_bp.new_request", step=1, token=req.public_token))
            # 1b. PAN number format — 5 letters + 4 digits + 1 letter
            pan_val = form_data.get("pan_number", "").strip().upper()
            if pan_val and not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', pan_val):
                flash("Invalid PAN number — must be 5 letters, 4 digits, 1 letter (e.g. ABCDE1234F).", "danger")
                return redirect(url_for("requests_bp.new_request", step=1, token=req.public_token))

        if action == "next" and step == 2:
            # 2a. Contract From — no backdates
            cf_val = form_data.get("contract_from", "")
            if cf_val:
                try:
                    cf_date = datetime.strptime(cf_val, "%Y-%m-%d").date()
                    if cf_date < _date.today():
                        flash("Contract From date cannot be before today. Please select today or a future date.", "danger")
                        return redirect(url_for("requests_bp.new_request", step=2, token=req.public_token))
                except ValueError:
                    pass

        if action == "next" and step < 3:
            return redirect(url_for("requests_bp.new_request", step=step + 1, token=req.public_token))
        if action == "back" and step > 1:
            return redirect(url_for("requests_bp.new_request", step=step - 1, token=req.public_token))
        if action == "save":
            flash("Draft saved.", "success")
            return redirect(url_for("requests_bp.view_request", token=req.public_token))

    from ..utils import company_scope_ids
    return render_template(
        "requests/form.html",
        req=req, step=step,
        step_fields=step_fields,
        all_fields=all_fields,
        plants=plants,
        designations=designations,
        FieldType=FieldType,
        OptionsSource=OptionsSource,
        form_data=req.form_data,
        initiator_companies=company_scope_ids(current_user.id),
    )


@requests_bp.route("/<string:token>/edit", methods=["GET", "POST"])
@login_required
@role_required(UserRole.INITIATOR)
def edit_request(token):
    req = _get_req_by_token(token)
    if req.initiated_by != current_user.id:
        abort(403)
    if req.status not in REJECTED_STATUSES and req.status != RequestStatus.DRAFT:
        flash("Only draft or rejected requests can be edited.", "warning")
        return redirect(url_for("requests_bp.view_request", token=req.public_token))
    return redirect(url_for("requests_bp.new_request", step=1, token=req.public_token))


# ── Submit ─────────────────────────────────────────────────────────────────────

@requests_bp.route("/<string:token>/submit", methods=["POST"])
@login_required
@role_required(UserRole.INITIATOR)
def submit_request(token):
    req = _get_req_by_token(token)
    if req.initiated_by != current_user.id:
        abort(403)
    if req.status != RequestStatus.DRAFT:
        flash("Only DRAFT requests can be submitted.", "warning")
        return redirect(url_for("requests_bp.view_request", token=req.public_token))

    # Validate required non-file fields
    all_fields = _get_active_fields()
    non_file_fields = [f for f in all_fields if f.field_type != FieldType.FILE and not f.is_readonly]
    missing = _validate_required(non_file_fields, req.form_data)
    if missing:
        flash(f"Missing required fields: {', '.join(missing[:5])}{'…' if len(missing) > 5 else ''}.", "danger")
        return redirect(url_for("requests_bp.new_request", step=1, token=req.public_token))

    # Defense-in-depth: the form dropdown already filters Company Code to
    # this initiator's ticked companies (see new_request()), but a crafted
    # POST could still name an unticked company — reject it explicitly.
    from ..utils import company_scope_ids
    if req.form_data.get("company_code", "") not in company_scope_ids(current_user.id):
        flash("You are not authorized to submit requests for this company. Contact your administrator.", "danger")
        return redirect(url_for("requests_bp.new_request", step=1, token=req.public_token))

    # UAN: mandatory for non-trainee designations
    designation_val = req.form_data.get("designation", "")
    if designation_val and "trainee" not in designation_val.lower():
        if not req.form_data.get("uan_number", "").strip():
            flash("UAN Number is required for this designation.", "danger")
            return redirect(url_for("requests_bp.new_request", step=1, token=req.public_token))

    # Replacement: both employee name and code required
    if req.form_data.get("hiring_type") == "Replacement":
        if not req.form_data.get("replacement_employee", "").strip():
            flash("Replacement Employee Name is required when Hiring Type is Replacement.", "danger")
            return redirect(url_for("requests_bp.new_request", step=2, token=req.public_token))
        if not req.form_data.get("replacement_employee_code", "").strip():
            flash("Replacement Employee Code is required when Hiring Type is Replacement.", "danger")
            return redirect(url_for("requests_bp.new_request", step=2, token=req.public_token))

    # RDC staffing-norms gate: advisory at Submit. Ultrafine/ROBO are
    # unaffected — same form, same flow, this block simply never triggers
    # for them. A blocked plant only hard-stops the initiator if they never
    # acknowledged the popup (is_special_case still False) — otherwise the
    # request proceeds through the over-norm approval chain instead.
    if req.form_data.get("company_code") == "RDC":
        from ..services.staffing_norms import check_rdc_staffing_gate
        gate = check_rdc_staffing_gate(req.form_data)
        _persist_gate_check(req, gate)
        if not gate["allowed"]:
            if not req.is_special_case:
                db.session.commit()
                log_audit("REQUEST", "STAFFING_GATE_BLOCKED",
                          resource_type="OnboardingRequest", resource_id=req.id,
                          resource_label=f"Request #{req.id} — {req.candidate_name}",
                          detail=gate)
                db.session.commit()
                return redirect(url_for("requests_bp.hiring_not_possible", token=req.public_token))
            log_audit("REQUEST", "STAFFING_GATE_BLOCKED_PROCEEDING_AS_SPECIAL_CASE",
                      resource_type="OnboardingRequest", resource_id=req.id,
                      resource_label=f"Request #{req.id} — {req.candidate_name}",
                      detail=gate)
        else:
            req.is_special_case = False   # capacity opened up since the popup fired
            log_audit("REQUEST", "STAFFING_GATE_PASSED",
                      resource_type="OnboardingRequest", resource_id=req.id,
                      resource_label=f"Request #{req.id} — {req.candidate_name}",
                      detail=gate)

    _approver_err = _validate_approver_availability(req)
    if _approver_err:
        db.session.commit()   # persist the gate-check row already added above, if any
        flash(_approver_err, "danger")
        return redirect(url_for("requests_bp.view_request", token=req.public_token))

    _finalize_submission(req)
    return redirect(url_for("requests_bp.view_request", token=req.public_token))


# ── Resubmit ───────────────────────────────────────────────────────────────────

@requests_bp.route("/<string:token>/resubmit", methods=["POST"])
@login_required
@role_required(UserRole.INITIATOR)
def resubmit_request(token):
    req = _get_req_by_token(token)
    if req.initiated_by != current_user.id:
        abort(403)
    if req.status not in REJECTED_STATUSES:
        flash("Only rejected requests can be resubmitted.", "warning")
        return redirect(url_for("requests_bp.view_request", token=req.public_token))

    from ..utils import company_scope_ids
    if req.form_data.get("company_code", "") not in company_scope_ids(current_user.id):
        flash("You are not authorized to submit requests for this company. Contact your administrator.", "danger")
        return redirect(url_for("requests_bp.new_request", step=1, token=req.public_token))

    if req.form_data.get("company_code") == "RDC":
        from ..services.staffing_norms import check_rdc_staffing_gate
        gate = check_rdc_staffing_gate(req.form_data)
        _persist_gate_check(req, gate)
        if not gate["allowed"]:
            if not req.is_special_case:
                db.session.commit()
                log_audit("REQUEST", "STAFFING_GATE_BLOCKED",
                          resource_type="OnboardingRequest", resource_id=req.id,
                          resource_label=f"Request #{req.id} — {req.candidate_name}",
                          detail=gate)
                db.session.commit()
                return redirect(url_for("requests_bp.hiring_not_possible", token=req.public_token))
            log_audit("REQUEST", "STAFFING_GATE_BLOCKED_PROCEEDING_AS_SPECIAL_CASE",
                      resource_type="OnboardingRequest", resource_id=req.id,
                      resource_label=f"Request #{req.id} — {req.candidate_name}",
                      detail=gate)
        else:
            req.is_special_case = False
            log_audit("REQUEST", "STAFFING_GATE_PASSED",
                      resource_type="OnboardingRequest", resource_id=req.id,
                      resource_label=f"Request #{req.id} — {req.candidate_name}",
                      detail=gate)

    _approver_err = _validate_approver_availability(req)
    if _approver_err:
        db.session.commit()   # persist the gate-check row already added above, if any
        flash(_approver_err, "danger")
        return redirect(url_for("requests_bp.view_request", token=req.public_token))

    _prev_status = req.status.value
    req.status = RequestStatus.PENDING_BH
    req.retry_count += 1
    req.updated_at = datetime.utcnow()
    log_audit("REQUEST", "REQUEST_RESUBMITTED",
              resource_type="OnboardingRequest", resource_id=req.id,
              resource_label=f"Request #{req.id} — {req.candidate_name}",
              detail={"candidate_name": req.candidate_name,
                      "retry_number": req.retry_count,
                      "previous_rejection_status": _prev_status})
    try:
        db.session.commit()
        initiator = db.session.get(User, req.initiated_by)
        bh_users = _get_bh_recipients(initiator, req.company_code)
        notify_users(db, req, bh_users,
                     subject=f"Resubmitted: {req.candidate_name}",
                     body=f"Resubmission #{req.retry_count} for {req.candidate_name}.")
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("An unexpected error occurred. Please try again.", "danger")
        return redirect(url_for("requests_bp.view_request", token=req.public_token))
    flash("Request resubmitted.", "success")
    return redirect(url_for("requests_bp.view_request", token=req.public_token))


# ── RDC staffing gate — Hiring Not Possible ─────────────────────────────────────

@requests_bp.route("/<string:token>/hiring-not-possible")
@login_required
@role_required(UserRole.INITIATOR)
def hiring_not_possible(token):
    import json
    req = _get_req_by_token(token)
    if req.initiated_by != current_user.id:
        abort(403)
    check = (StaffingGateCheck.query
             .filter_by(request_id=req.id)
             .order_by(StaffingGateCheck.checked_at.desc())
             .first())
    detail = json.loads(check.detail) if check and check.detail else {}
    return render_template("requests/hiring_not_possible.html", req=req, check=check, detail=detail)


@requests_bp.route("/<string:token>/submit-as-special-case", methods=["POST"])
@login_required
@role_required(UserRole.INITIATOR)
def submit_as_special_case(token):
    """
    Fallback path from hiring_not_possible.html for the rare case where the
    initiator reaches Submit without ever acknowledging the form popup
    (JS disabled, or a race between the live pre-check and Submit). Marks
    the request as a special case and finalizes submission the same way
    submit_request() would for an already-acknowledged one.
    """
    req = _get_req_by_token(token)
    if req.initiated_by != current_user.id:
        abort(403)
    if req.status != RequestStatus.DRAFT:
        flash("Only DRAFT requests can be submitted.", "warning")
        return redirect(url_for("requests_bp.view_request", token=req.public_token))
    req.is_special_case = True
    log_audit("REQUEST", "STAFFING_GATE_BLOCKED_PROCEEDING_AS_SPECIAL_CASE",
              resource_type="OnboardingRequest", resource_id=req.id,
              resource_label=f"Request #{req.id} — {req.candidate_name}",
              detail={"acknowledged_at": "submit-time fallback"})
    _finalize_submission(req)
    return redirect(url_for("requests_bp.view_request", token=req.public_token))


# ── RDC Staffing Status Dashboard ────────────────────────────────────────────────
# Read-only, backed entirely by StaffingSnapshot (no live ZingHR/Truein/DVT
# calls) — visible to HR Manager, Head HR, Dr. Bhoon, and Admin per the
# stakeholder's requirement that current staffing status be visible, not
# just implicitly enforced by the gate.

@requests_bp.route("/staffing-status")
@login_required
@role_required(UserRole.HR_MANAGER, UserRole.HEAD_HR, UserRole.DR_BHOON, UserRole.SUPER_ADMIN, UserRole.BUSINESS_HEAD)
def staffing_status():
    """
    Landing page for the dashboard: Regions (clusters) first, each showing
    how many plants it has and a rolled-up technical/hiring summary — click
    a region to see its plants (staffing_status_cluster), click a plant
    there to see its own role-by-role detail (staffing_status_plant).
    Plants not yet assigned to a region (Admin -> Plant Mappings) would
    otherwise be unreachable through that drill-down, so any such plant
    that actually has headcount or a known hiring limit is still surfaced
    below the region list rather than silently hidden.
    """
    from ..models import ClusterNameMapping, PlantDvtMapping, NormScope, MatchConfidence
    from ..services import headcount

    my_companies = _staffing_company_scope(current_user)

    def _hiring_rollup(rows):
        agg = {}
        for row in rows:
            bucket = agg.setdefault(row.location_key, {"can_hire_roles": 0, "at_capacity_roles": 0})
            if row.can_hire is True:
                bucket["can_hire_roles"] += 1
            elif row.can_hire is False:
                bucket["at_capacity_roles"] += 1
        return agg

    hiring_by_plant = _hiring_rollup(headcount.get_all_latest_snapshots(scope=NormScope.PLANT))
    hiring_by_cluster = _hiring_rollup(headcount.get_all_latest_snapshots(scope=NormScope.CLUSTER))

    # Only plants confidently matched to a DVT/ERP identity (AUTO_EXACT or
    # admin-confirmed MANUAL) show up anywhere in this dashboard — an
    # AUTO_FUZZY/UNMATCHED name is an unverified guess and could be wrong,
    # so it's excluded from region plant-counts and rollups too, not just
    # hidden from the plant list itself (see get_plants_in_cluster()).
    plants_by_cluster_id = {}
    confirmed_plants = (PlantDvtMapping.query.filter_by(is_deleted=False)
                         .filter(PlantDvtMapping.match_confidence.in_(
                             (MatchConfidence.AUTO_EXACT, MatchConfidence.MANUAL))).all())
    for p in confirmed_plants:
        plants_by_cluster_id.setdefault(p.cluster_id, []).append(p)

    clusters = ClusterNameMapping.query.filter_by(is_deleted=False).order_by(ClusterNameMapping.canonical_cluster_name).all()
    if my_companies is not None and "RDC" not in my_companies:
        clusters = []   # not ticked for RDC at all — the RDC tab shows nothing
    elif current_user.role == UserRole.BUSINESS_HEAD:
        allowed_ids = _bh_region_ids(current_user)
        clusters = [c for c in clusters if c.id in allowed_ids]
    # HR_MANAGER never gets region-narrowing (see CLAUDE.md) — an
    # RDC-ticked HR Manager sees every RDC region, same as before.
    regions = []
    for c in clusters:
        plants = plants_by_cluster_id.get(c.id, [])
        hiring = dict(hiring_by_cluster.get(c.canonical_cluster_name, {"can_hire_roles": 0, "at_capacity_roles": 0}))
        for p in plants:
            ph = hiring_by_plant.get(p.plant_location_name, {"can_hire_roles": 0, "at_capacity_roles": 0})
            hiring["can_hire_roles"] += ph["can_hire_roles"]
            hiring["at_capacity_roles"] += ph["at_capacity_roles"]
        regions.append({"cluster": c, "plant_count": len(plants), "hiring": hiring})

    def _plant_has_data(p):
        h = hiring_by_plant.get(p.plant_location_name)
        return bool(h and (h["can_hire_roles"] or h["at_capacity_roles"]))

    if my_companies is not None and "RDC" not in my_companies:
        unmapped_plants = []
    elif current_user.role == UserRole.BUSINESS_HEAD:
        unmapped_plants = []   # no cluster to attribute to a region — hide, don't guess
    else:
        unmapped_plants = [
            p for p in plants_by_cluster_id.get(None, [])
            if _plant_has_data(p)
        ]
        unmapped_plants.sort(key=lambda p: p.plant_location_name)

    has_snapshot = bool(hiring_by_plant or hiring_by_cluster)

    # Ultrafine/ROBO (added 2026-09-15) — flat plant list + simple
    # headcount, no region drill-down and no production-volume gating (see
    # COMPANY_CHOICES / PlantLocation.company). Neither company has a
    # region concept, so no _bh_region_ids-style narrowing applies — but
    # company-scope gating (my_companies, above) still must: a Business
    # Head/HR Manager only sees a company's tab at all if they're ticked
    # for it. Unscoped roles (my_companies is None) see every company.
    other_companies = [c for c in COMPANY_CHOICES if c != "RDC"
                        and (my_companies is None or c in my_companies)]
    other_company_summaries = {c: headcount.get_other_company_plant_summary(c) for c in other_companies}
    other_company_unresolved = {c: headcount.get_other_company_unresolved_count(c) for c in other_companies}
    visible_companies = (["RDC"] if (my_companies is None or "RDC" in my_companies) else []) + other_companies

    return render_template("requests/staffing_status.html", regions=regions,
                           hiring_by_plant=hiring_by_plant,
                           unmapped_plants=unmapped_plants, has_snapshot=has_snapshot,
                           other_company_summaries=other_company_summaries,
                           other_company_unresolved=other_company_unresolved,
                           visible_companies=visible_companies)


def _xlsx_report_style():
    """
    Shared header/data cell styling for every Staffing Status Excel export
    (RDC's staffing_status_download() and the Ultrafine/ROBO
    staffing_status_company_download() below) — factored out 2026-09-23 so
    the two reports render identically instead of drifting apart.
    """
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    hdr_fill  = PatternFill(start_color="1F3864", end_color="1F3864", fill_type="solid")
    hdr_font  = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    dat_font  = Font(name="Calibri", size=10)
    thin      = Side(style="thin", color="CCCCCC")
    bdr       = Border(left=thin, right=thin, top=thin, bottom=thin)
    alt_fill  = PatternFill(start_color="EEF2FF", end_color="EEF2FF", fill_type="solid")
    return hdr_fill, hdr_font, hdr_align, dat_font, bdr, alt_fill


def _write_xlsx_sheet(ws, columns, rows, style):
    from openpyxl.utils import get_column_letter
    hdr_fill, hdr_font, hdr_align, dat_font, bdr, alt_fill = style
    for ci, (label, width) in enumerate(columns, 1):
        c = ws.cell(row=1, column=ci, value=label)
        c.font = hdr_font; c.fill = hdr_fill; c.alignment = hdr_align; c.border = bdr
        ws.column_dimensions[get_column_letter(ci)].width = width
    ws.row_dimensions[1].height = 26
    ws.freeze_panes = "A2"
    for ri, row in enumerate(rows, 2):
        for ci, val in enumerate(row, 1):
            c = ws.cell(row=ri, column=ci, value=val)
            c.font = dat_font; c.border = bdr
            if ri % 2 == 0:
                c.fill = alt_fill


@requests_bp.route("/staffing-status/download")
@login_required
@role_required(UserRole.HR_MANAGER, UserRole.HEAD_HR, UserRole.DR_BHOON, UserRole.SUPER_ADMIN, UserRole.BUSINESS_HEAD)
def staffing_status_download():
    """
    Excel download of the RDC Staffing Status dashboard — one sheet per scope
    (By Cluster / By Plant / By Employees), each row a (location, role
    category) pair with current/allowed headcount, can-hire, and the
    location's production volume — except By Employees, which is one row per
    actual employee (Cluster, Plant, then their details) rather than an
    aggregated headcount. Same data source and role/region scoping as
    staffing_status()/staffing_status_cluster()/staffing_status_plant() above
    — just flattened into rows instead of drilled into per-page.
    """
    import io
    from datetime import datetime as _dt
    from flask import make_response
    from openpyxl import Workbook
    from ..models import ClusterNameMapping, NormScope, StaffingSnapshot, EmployeeLocationSnapshot
    from ..services import headcount

    my_companies = _staffing_company_scope(current_user)
    clusters = ClusterNameMapping.query.filter_by(is_deleted=False).order_by(ClusterNameMapping.canonical_cluster_name).all()
    if my_companies is not None and "RDC" not in my_companies:
        clusters = []   # not ticked for RDC — an HR Manager/BH scoped to ROBO/Ultrafine only gets an empty report
    elif current_user.role == UserRole.BUSINESS_HEAD:
        allowed_ids = _bh_region_ids(current_user)
        clusters = [c for c in clusters if c.id in allowed_ids]

    style = _xlsx_report_style()

    def _write_sheet(ws, columns, rows):
        _write_xlsx_sheet(ws, columns, rows, style)

    def _can_hire_label(v):
        return "Yes" if v is True else ("No" if v is False else "Unknown")

    cluster_cols = [("Region", 22), ("Volume (m³)", 14), ("Tier", 20), ("Role Category", 26),
                     ("Current Headcount", 16), ("Allowed Headcount", 16), ("Can Hire?", 12)]
    plant_cols = [("Region", 22), ("Plant", 26), ("Volume (m³)", 14), ("Tier", 20), ("Role Category", 26),
                   ("Current Headcount", 16), ("Allowed Headcount", 16), ("Can Hire?", 12)]
    employee_cols = [("Cluster", 22), ("Plant", 26), ("Employee Name", 26), ("Employee Code", 16),
                      ("Designation", 26), ("Department", 20), ("Date of Joining", 16), ("Source", 10)]

    def _employee_row(cluster_name, plant_name, e):
        return (cluster_name, plant_name, e.employee_name, e.employee_code,
                e.designation, e.department, e.date_of_joining, e.source.value)

    # Resolved ONCE for the whole report instead of once per cluster/plant
    # (28 clusters + 140 plants previously meant ~336 redundant
    # MAX(computed_at) full-table scans across two large, unpruned history
    # tables — the dominant cost of this download, confirmed by profiling).
    # Every location in this report reads from the same single latest run,
    # so resolving it once and threading it through is correct, not just
    # faster.
    latest_snapshot_run = db.session.query(db.func.max(StaffingSnapshot.computed_at)).scalar()
    latest_employee_run = db.session.query(db.func.max(EmployeeLocationSnapshot.computed_at)).scalar()

    cluster_rows, plant_rows, employee_rows = [], [], []
    shown_employee_ids = set()   # feeds Unmapped Employees below — see its comment
    for c in clusters:
        for s in headcount.get_snapshot_rows_for_location(c.canonical_cluster_name, NormScope.CLUSTER, latest_run=latest_snapshot_run):
            cluster_rows.append((c.canonical_cluster_name, s.production_volume, s.tier_label,
                                  s.norm_role_category.name, s.current_headcount, s.allowed_headcount,
                                  _can_hire_label(s.can_hire)))
        for p in headcount.get_plants_in_cluster(c.id):
            for s in headcount.get_snapshot_rows_for_location(p.plant_location_name, NormScope.PLANT, latest_run=latest_snapshot_run):
                plant_rows.append((c.canonical_cluster_name, p.display_name, s.production_volume, s.tier_label,
                                    s.norm_role_category.name, s.current_headcount, s.allowed_headcount,
                                    _can_hire_label(s.can_hire)))
            for e in headcount.get_employees_at_plant(p.plant_location_name, latest_run=latest_employee_run):
                employee_rows.append(_employee_row(c.canonical_cluster_name, p.display_name, e))
                shown_employee_ids.add(e.id)
        # Employees resolved to this cluster but not to any specific plant
        # within it (e.g. regional/HQ roles) — same "cluster-only staff"
        # concept as the cluster detail page, Plant left blank here.
        for e in headcount.get_employees_at_cluster(c.canonical_cluster_name, unassigned_to_plant_only=True, latest_run=latest_employee_run):
            employee_rows.append(_employee_row(c.canonical_cluster_name, "", e))
            shown_employee_ids.add(e.id)
    employee_rows.sort(key=lambda r: (r[0] or "", r[1] or "", r[2] or ""))

    # Unmapped Employees (added 2026-09-24, stakeholder request): every real
    # employee whose location doesn't resolve to a CONFIRMED plant is
    # invisible in By Plant/By Employees above by design (get_plants_in_cluster()
    # only traverses AUTO_EXACT/MANUAL matches, so an unverified guess never
    # shows fabricated volume/tier data) — but that also silently dropped
    # anyone at a closed/decommissioned plant or an unmapped one, with no way
    # to trace them. This sheet is pure headcount visibility (raw recorded
    # location + employee detail), independent of match confidence, so a
    # closed/unmapped plant's real headcount is still traceable somewhere in
    # this report.
    #
    # An unmapped employee has no resolvable region to scope by — a
    # Business Head restricted to their own clusters above would otherwise
    # leak every other region's unmapped employees into their download, the
    # exact leak `clusters` filtering exists to prevent. Hidden entirely for
    # BUSINESS_HEAD, same "no cluster to attribute to a region — hide,
    # don't guess" convention staffing_status()'s own unmapped_plants list
    # already uses. HR_MANAGER (never region-scoped) and HEAD_HR/DR_BHOON/
    # SUPER_ADMIN (always unscoped) see the full list.
    if current_user.role == UserRole.BUSINESS_HEAD:
        unmapped_rows = []
    else:
        unmapped_rows = [
            (e.plant_location_key or "(no location on record)", e.employee_name, e.employee_code,
             e.designation, e.department, e.date_of_joining, e.source.value)
            for e in headcount.get_rdc_unmapped_employees(shown_employee_ids, latest_run=latest_employee_run)
        ]
    unmapped_cols = [("Location (as recorded)", 30), ("Employee Name", 26), ("Employee Code", 16),
                      ("Designation", 26), ("Department", 20), ("Date of Joining", 16), ("Source", 10)]

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "By Cluster"
    _write_sheet(ws1, cluster_cols, cluster_rows)
    ws2 = wb.create_sheet("By Plant")
    _write_sheet(ws2, plant_cols, plant_rows)
    ws3 = wb.create_sheet("By Employees")
    _write_sheet(ws3, employee_cols, employee_rows)
    ws4 = wb.create_sheet("Unmapped Employees")
    _write_sheet(ws4, unmapped_cols, unmapped_rows)

    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()

    filename = f"rdc_staffing_status_{_dt.now().strftime('%Y%m%d_%H%M')}.xlsx"
    log_audit("EXPORT", "EXPORT_DOWNLOADED", resource_type="StaffingStatus",
              resource_label="RDC Staffing Status report")
    db.session.commit()

    resp = make_response(data)
    resp.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["Content-Length"] = len(data)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@requests_bp.route("/staffing-status/download/<company>")
@login_required
@role_required(UserRole.HR_MANAGER, UserRole.HEAD_HR, UserRole.DR_BHOON, UserRole.SUPER_ADMIN, UserRole.BUSINESS_HEAD)
def staffing_status_company_download(company):
    """
    Excel download for the Ultrafine/ROBO Staffing Status tabs (added
    2026-09-23) — the RDC tab already had this above; Ultrafine/ROBO didn't.
    Three sheets: By Plant (plant, headcount, and a Status column — see
    below — no volume/tier/allowed-headcount columns, since neither company
    is production-volume gated, same as their staffing_status.html tab), By
    Employees (one row per employee actually resolved to a plant), and
    Unmapped Employees (added 2026-09-24, stakeholder request — anyone whose
    ZingHR Location didn't resolve to ANY known plant for their company, so
    HR can trace and correct them — previously only a count on the
    dashboard tab, or mixed into By Employees as "(Unresolved)" rows).

    Closed/inactive plants (fixed 2026-09-24, stakeholder request) are now
    included in By Plant with their real headcount, not silently dropped —
    get_other_company_plant_summary() stopped filtering to is_active plants
    only, since a plant being closed in admin doesn't mean its employees
    have all left; they were previously falling into "Unresolved" purely
    because the plant lookup used to skip closed plants. Status distinguishes
    Active from Closed so a closed plant's headcount isn't mistaken for a
    currently-hiring-eligible one.
    """
    if company not in COMPANY_CHOICES or company == "RDC":
        abort(404)
    my_companies = _staffing_company_scope(current_user)
    if my_companies is not None and company not in my_companies:
        abort(403)
    import io
    from datetime import datetime as _dt
    from flask import make_response
    from openpyxl import Workbook
    from ..models import EmployeeLocationSnapshot
    from ..services import headcount

    style = _xlsx_report_style()
    plant_summary = headcount.get_other_company_plant_summary(company)
    plant_rows = [(p["plant"].name, p["headcount"], "Active" if p["plant"].is_active else "Closed")
                  for p in plant_summary]

    employee_cols = [("Plant", 26), ("Employee Name", 26), ("Employee Code", 16),
                      ("Designation", 26), ("Department", 20), ("Date of Joining", 16), ("Source", 10)]
    employee_rows = []
    for p in plant_summary:
        for e in headcount.get_other_company_employees_at_plant(company, p["plant"].name):
            employee_rows.append((p["plant"].name, e.employee_name, e.employee_code,
                                   e.designation, e.department, e.date_of_joining, e.source.value))

    unmapped_cols = [("Employee Name", 26), ("Employee Code", 16), ("Designation", 26),
                      ("Department", 20), ("Date of Joining", 16), ("Source", 10)]
    unmapped_rows = []
    latest_run = db.session.query(db.func.max(EmployeeLocationSnapshot.computed_at)).scalar()
    if latest_run:
        unresolved = (EmployeeLocationSnapshot.query
                      .filter_by(company=company, plant_location_key=None, computed_at=latest_run)
                      .order_by(EmployeeLocationSnapshot.employee_name).all())
        for e in unresolved:
            unmapped_rows.append((e.employee_name, e.employee_code, e.designation,
                                   e.department, e.date_of_joining, e.source.value))

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "By Plant"
    _write_xlsx_sheet(ws1, [("Plant", 30), ("Headcount", 14), ("Status", 12)], plant_rows, style)
    ws2 = wb.create_sheet("By Employees")
    _write_xlsx_sheet(ws2, employee_cols, employee_rows, style)
    ws3 = wb.create_sheet("Unmapped Employees")
    _write_xlsx_sheet(ws3, unmapped_cols, unmapped_rows, style)

    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()

    filename = f"{company.lower()}_staffing_status_{_dt.now().strftime('%Y%m%d_%H%M')}.xlsx"
    log_audit("EXPORT", "EXPORT_DOWNLOADED", resource_type="StaffingStatus",
              resource_label=f"{company} Staffing Status report")
    db.session.commit()

    resp = make_response(data)
    resp.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["Content-Length"] = len(data)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@requests_bp.route("/staffing-status/cluster/<path:cluster_name>")
@login_required
@role_required(UserRole.HR_MANAGER, UserRole.HEAD_HR, UserRole.DR_BHOON, UserRole.SUPER_ADMIN, UserRole.BUSINESS_HEAD)
def staffing_status_cluster(cluster_name):
    from ..models import ClusterNameMapping, NormScope
    from ..services import headcount
    cluster = ClusterNameMapping.query.filter_by(canonical_cluster_name=cluster_name, is_deleted=False).first_or_404()
    my_companies = _staffing_company_scope(current_user)
    if my_companies is not None and "RDC" not in my_companies:
        abort(403)
    if current_user.role == UserRole.BUSINESS_HEAD and cluster.id not in _bh_region_ids(current_user):
        abort(403)
    plants = headcount.get_plants_in_cluster(cluster.id)
    cluster_staff = headcount.get_employees_at_cluster(cluster_name, unassigned_to_plant_only=True)
    # Every employee resolved anywhere in this cluster — every plant plus
    # the cluster-only staff above — so the region view shows the full
    # roster in one place instead of requiring a click into each plant.
    all_region_employees, all_region_employees_total = headcount.get_all_employees(cluster_names={cluster_name})
    hiring_capacity = headcount.get_snapshot_rows_for_location(cluster_name, NormScope.CLUSTER)
    # Every cluster-scope row shares the same production_volume (the
    # cluster's real, summed last-month DVT m^3 across confirmed plants —
    # NOT the per-role volume_used/basis, which is plant_count for
    # FIXED/NONE/PER_BUSINESS_HEAD roles since a cluster's tier is
    # plant-count-based, not volume-based; see StaffingSnapshot docstring)
    # — surface it prominently instead of only inside a role row's "Basis"
    # text, mirroring the plant detail page's volume callout.
    volume_row = next((s for s in hiring_capacity if s.production_volume is not None), None)
    cluster_volume = volume_row.production_volume if volume_row else None
    cluster_tier = volume_row.tier_label if volume_row else None
    # per-plant "can we hire anything here?" summary for the plants table
    all_plant_rows = headcount.get_all_latest_snapshots(scope=NormScope.PLANT)
    hiring_by_plant = {}
    for row in all_plant_rows:
        agg = hiring_by_plant.setdefault(row.location_key, {"can_hire_roles": 0, "at_capacity_roles": 0})
        if row.can_hire is True:
            agg["can_hire_roles"] += 1
        elif row.can_hire is False:
            agg["at_capacity_roles"] += 1
    return render_template("requests/staffing_status_cluster.html", cluster=cluster, plants=plants,
                           cluster_staff=cluster_staff,
                           all_region_employees=all_region_employees,
                           all_region_employees_total=all_region_employees_total,
                           hiring_capacity=hiring_capacity, hiring_by_plant=hiring_by_plant,
                           cluster_volume=cluster_volume, cluster_tier=cluster_tier)


@requests_bp.route("/staffing-status/employees")
@login_required
@role_required(UserRole.HR_MANAGER, UserRole.HEAD_HR, UserRole.DR_BHOON, UserRole.SUPER_ADMIN, UserRole.BUSINESS_HEAD)
def staffing_status_employees():
    from ..models import PlantDvtMapping, ClusterNameMapping
    from ..services import headcount
    source = request.args.get("source", "").strip().upper() or None
    designation = request.args.get("designation", "").strip() or None
    department = request.args.get("department", "").strip() or None
    resolved = request.args.get("resolved", "").strip().lower() or None
    search = request.args.get("q", "").strip() or None
    cluster_filter = request.args.get("cluster", "").strip() or None
    my_companies = _staffing_company_scope(current_user)
    cluster_names = None
    if my_companies is not None and "RDC" not in my_companies:
        cluster_names = set()   # not ticked for RDC at all — this RDC-wide directory shows nobody
    elif current_user.role == UserRole.BUSINESS_HEAD:
        allowed_ids = _bh_region_ids(current_user)
        cluster_names = {c.canonical_cluster_name for c in
                          ClusterNameMapping.query.filter(ClusterNameMapping.id.in_(allowed_ids)).all()} if allowed_ids else set()
    # An explicit ?cluster= (from the "view full directory" link on a region
    # page) narrows further — intersect rather than override, so a Business
    # Head can never widen past their own region scoping via the URL.
    if cluster_filter:
        cluster_names = {cluster_filter} if cluster_names is None else (cluster_names & {cluster_filter})
    employees, total = headcount.get_all_employees(source=source, designation=designation,
                                                     department=department, resolved=resolved, search=search,
                                                     cluster_names=cluster_names)
    plant_display_names = {p.plant_location_name: p.display_name
                           for p in PlantDvtMapping.query.filter_by(is_deleted=False).all()}
    return render_template("requests/staffing_status_employees.html", employees=employees, total=total,
                           source=source, designation=designation, department=department,
                           resolved=resolved, search=search, plant_display_names=plant_display_names,
                           cluster_filter=cluster_filter)


@requests_bp.route("/staffing-status/plant/<path:plant_name>")
@login_required
@role_required(UserRole.HR_MANAGER, UserRole.HEAD_HR, UserRole.DR_BHOON, UserRole.SUPER_ADMIN, UserRole.BUSINESS_HEAD)
def staffing_status_plant(plant_name):
    from ..models import PlantDvtMapping, NormScope
    from ..services import headcount
    mapping = PlantDvtMapping.query.filter_by(plant_location_name=plant_name, is_deleted=False).first_or_404()
    my_companies = _staffing_company_scope(current_user)
    if my_companies is not None and "RDC" not in my_companies:
        abort(403)
    if current_user.role == UserRole.BUSINESS_HEAD:
        if mapping.cluster_id is None or mapping.cluster_id not in _bh_region_ids(current_user):
            abort(403)
    employees = headcount.get_employees_at_plant(plant_name)
    hiring_capacity = headcount.get_snapshot_rows_for_location(plant_name, NormScope.PLANT)
    special_counts = _special_case_counts_by_category(plant_name)
    # Every row for a given plant shares the same production volume/tier —
    # surface it once, prominently, instead of only inside each role row's
    # "Basis" text, so it's obvious at a glance why hiring is or isn't open.
    volume_row = next((s for s in hiring_capacity if s.production_volume is not None), None)
    plant_volume = volume_row.production_volume if volume_row else None
    plant_tier = volume_row.tier_label if volume_row else None
    return render_template("requests/staffing_status_plant.html", mapping=mapping, employees=employees,
                           hiring_capacity=hiring_capacity, plant_volume=plant_volume, plant_tier=plant_tier,
                           special_counts=special_counts)


@requests_bp.route("/staffing-status/company/<company>/plant/<path:plant_name>")
@login_required
@role_required(UserRole.HR_MANAGER, UserRole.HEAD_HR, UserRole.DR_BHOON, UserRole.SUPER_ADMIN, UserRole.BUSINESS_HEAD)
def staffing_status_company_plant(company, plant_name):
    """
    Ultrafine/ROBO plant detail (added 2026-09-15, multi-company support) —
    the employee list only, no production-volume/role-category columns at
    all (there's no gating for these companies — see COMPANY_CHOICES /
    PlantLocation.company). Deliberately a separate, much simpler template
    from staffing_status_plant.html rather than bolting an "if no formulas"
    branch onto that RDC-specific one.
    """
    from ..services import headcount
    if company not in COMPANY_CHOICES or company == "RDC":
        abort(404)
    my_companies = _staffing_company_scope(current_user)
    if my_companies is not None and company not in my_companies:
        abort(403)
    plant = PlantLocation.query.filter_by(
        name=plant_name, company=company, is_deleted=False).first_or_404()
    employees = headcount.get_other_company_employees_at_plant(company, plant_name)
    return render_template("requests/staffing_status_company_plant.html",
                           company=company, plant=plant, employees=employees)


# ── Manual "Sync Now" — dashboard widget ────────────────────────────────────────
# Non-blocking trigger for the RDC headcount snapshot, surfaced on the main
# dashboard for everyone who can see staffing data. If a refresh (automatic
# or another manual trigger) is already running anywhere, this is a no-op —
# the frontend just shows "syncing…" and polls sync-status until it clears.

_SYNC_ROLES = (UserRole.HR_MANAGER, UserRole.HEAD_HR, UserRole.DR_BHOON,
               UserRole.SUPER_ADMIN, UserRole.BUSINESS_HEAD)


@requests_bp.route("/staffing-status/sync-now", methods=["POST"])
@login_required
@role_required(*_SYNC_ROLES)
def staffing_sync_now():
    from flask import current_app as _app
    from ..services import snapshot_refresh
    result = snapshot_refresh.trigger_manual_refresh(_app._get_current_object())
    if result["status"] == "started":
        log_audit("REQUEST", "STAFFING_SNAPSHOT_MANUAL_REFRESH_TRIGGERED",
                  detail={"triggered_by": current_user.name})
        db.session.commit()
    return jsonify({"ok": True, **result})


@requests_bp.route("/staffing-status/sync-status")
@login_required
@role_required(*_SYNC_ROLES)
def staffing_sync_status():
    from ..services import snapshot_refresh
    return jsonify({"ok": True, **snapshot_refresh.refresh_status()})


# ── View ───────────────────────────────────────────────────────────────────────

@requests_bp.route("/<string:token>")
@login_required
def view_request(token):
    req = _get_req_by_token(token)

    # Drafts are private — only the initiator who created them may view
    if req.status == RequestStatus.DRAFT and req.initiated_by != current_user.id:
        abort(403)

    # Initiators can only see their own requests (any status)
    if current_user.role == UserRole.INITIATOR and req.initiated_by != current_user.id:
        abort(403)

    # Company-scope gating (added 2026-09-23 — same gap class as Staffing
    # Status/admin requests_list: this route predates the company-scope
    # tick-mark feature). Without this, any Business Head/HR Manager could
    # view full request detail for a company they aren't ticked for at all,
    # simply by having/guessing the public_token — can_act_on() below only
    # ever gated the Approve/Reject buttons, never the page itself.
    # HEAD_HR/DR_BHOON/SUPER_ADMIN stay unscoped by design.
    if current_user.role in (UserRole.BUSINESS_HEAD, UserRole.HR_MANAGER):
        from ..utils import company_scope_ids
        if req.company_code not in company_scope_ids(current_user.id):
            abort(403)

    can_approve = can_act_on(req, current_user)
    can_submit = (req.status == RequestStatus.DRAFT and
                  current_user.role == UserRole.INITIATOR and
                  req.initiated_by == current_user.id)
    can_resubmit = (req.status in REJECTED_STATUSES and
                    current_user.role == UserRole.INITIATOR and
                    req.initiated_by == current_user.id)

    all_fields = FormField.query.filter_by(is_active=True, is_deleted=False).order_by(
        FormField.step, FormField.sort_order).all()

    # ── Workflow mini-map (multi-round) ───────────────────────────────────────
    sv = req.status.value

    _ACTOR_STATUS_MAP = {
        UserRole.BUSINESS_HEAD: "PENDING_BH",
        UserRole.DR_BHOON:      "PENDING_DR_BHOON",
        UserRole.HR_MANAGER:    "PENDING_HR_MANAGER",
        UserRole.HEAD_HR:       "PENDING_HEAD_HR",
    }
    _STAGE_PROGRESS = {
        "PENDING_BH":        1,
        "PENDING_DR_BHOON":  2,
        "PENDING_HR_MANAGER":2,
        "PENDING_HEAD_HR":   3,
        "ACTIVE":            99,
    }
    _PROGRESS = {
        "DRAFT": 0,
        "PENDING_BH": 1,       "REJECTED_BH": 1,
        "PENDING_DR_BHOON": 2, "REJECTED_DR_BHOON": 2,
        "PENDING_HR_MANAGER": 2, "REJECTED_HRM": 2,
        "PENDING_HEAD_HR": 3,  "REJECTED_HEAD_HR": 3,
        "ACTIVE": 99,
    }
    # Over-norm chain visits Head HR *before* Dr. Bhoon — the opposite order
    # from _STAGE_PROGRESS/_PROGRESS above (built for the standard/BH-bypass
    # paths), so it needs its own progress numbering.
    _STAGE_PROGRESS_OVER_NORM = {
        "PENDING_BH":        1,
        "PENDING_HEAD_HR":   2,
        "PENDING_DR_BHOON":  3,
        "ACTIVE":            99,
    }
    _PROGRESS_OVER_NORM = {
        "DRAFT": 0,
        "PENDING_BH": 1,       "REJECTED_BH": 1,
        "PENDING_HEAD_HR": 2,  "REJECTED_HEAD_HR": 2,
        "PENDING_DR_BHOON": 3, "REJECTED_DR_BHOON": 3,
        "ACTIVE": 99,
    }
    # Ultrafine/ROBO (2026-09-21): a third, fixed chain that always visits
    # every role in order BH -> HR Manager -> Head HR -> Dr. Bhoon -> Active
    # — never branches by is_special_case (no staffing gate exists for
    # these companies to set it). Its own progress numbering since it has
    # 4 real stages, one more than either RDC path.
    _STAGE_PROGRESS_OTHER_COMPANY = {
        "PENDING_BH":         1,
        "PENDING_HR_MANAGER": 2,
        "PENDING_HEAD_HR":    3,
        "PENDING_DR_BHOON":   4,
        "ACTIVE":             99,
    }
    _PROGRESS_OTHER_COMPANY = {
        "DRAFT": 0,
        "PENDING_BH": 1,          "REJECTED_BH": 1,
        "PENDING_HR_MANAGER": 2,  "REJECTED_HRM": 2,
        "PENDING_HEAD_HR": 3,     "REJECTED_HEAD_HR": 3,
        "PENDING_DR_BHOON": 4,    "REJECTED_DR_BHOON": 4,
        "ACTIVE": 99,
    }
    _NEXT_ROLE_LABEL = {
        "PENDING_BH":        "Business Head",
        "PENDING_DR_BHOON":  "Dr. Bhoon",
        "PENDING_HR_MANAGER":"HR Manager",
        "PENDING_HEAD_HR":   "Head HR",
    }
    next_role = _NEXT_ROLE_LABEL.get(sv)

    # Split req.actions into rounds: each REJECTED action ends a round
    rounds_raw = []
    current_round_acts = []
    for act in req.actions:   # already ordered by acted_at
        current_round_acts.append(act)
        if act.action == ApprovalActionType.REJECTED:
            rounds_raw.append(current_round_acts)
            current_round_acts = []
    rounds_raw.append(current_round_acts)   # final / currently active round

    cur_progress = _PROGRESS.get(sv, 0)
    cur_progress_over_norm = _PROGRESS_OVER_NORM.get(sv, 0)
    cur_progress_other_company = _PROGRESS_OTHER_COMPANY.get(sv, 0)

    def _build_round_stages(round_acts, is_last_round):
        """Build the stages list for one round of the workflow.

        Three paths can be newly created: STANDARD (BH -> HR Manager -> Head
        HR, RDC only), OVER_NORM (BH -> Head HR -> Dr. Bhoon, skipping HR
        Manager — the RDC staffing-gate "proceed anyway" chain), and
        OTHER_COMPANY (BH -> HR Manager -> Head HR -> Dr. Bhoon, Ultrafine/
        ROBO's own fixed chain, added 2026-09-21 — checked first since
        company_code is a stable request attribute, unlike the other two
        paths which must be inferred from action history). BH_BYPASS (BH
        manually flags straight to Dr. Bhoon, skipping HR entirely) was a
        separate mechanism that has been removed (its POST route/UI no
        longer exist) — this branch is kept only to render the workflow map
        correctly for requests that already went through it before removal.
        """
        has_flagged_special = any(a.action == ApprovalActionType.FLAGGED_SPECIAL for a in round_acts)
        has_hrm_action = any(a.actor.role == UserRole.HR_MANAGER for a in round_acts)
        has_head_hr_action = any(a.actor.role == UserRole.HEAD_HR for a in round_acts)

        if req.company_code and req.company_code != "RDC":
            path = "OTHER_COMPANY"
        elif has_flagged_special or (is_last_round and sv in ("PENDING_DR_BHOON", "REJECTED_DR_BHOON")
                                    and not has_head_hr_action and not req.is_special_case):
            path = "BH_BYPASS"
        elif has_head_hr_action and not has_hrm_action:
            path = "OVER_NORM"
        elif is_last_round and req.is_special_case and sv in (
                "DRAFT", "PENDING_BH", "REJECTED_BH", "PENDING_HEAD_HR", "PENDING_DR_BHOON",
                "REJECTED_HEAD_HR", "REJECTED_DR_BHOON"):
            path = "OVER_NORM"        # still-pending round (including pre-submit draft), not
                                       # enough actions yet to infer from history
        else:
            path = "STANDARD"

        if path == "BH_BYPASS":
            _stage_defs = [
                ("PENDING_BH",        "Business Head Review"),
                ("PENDING_DR_BHOON",  "Dr. Bhoon Review"),
                ("ACTIVE",            "Approved"),
            ]
        elif path == "OVER_NORM":
            _stage_defs = [
                ("PENDING_BH",         "Business Head — Over-Norm Approval"),
                ("PENDING_HEAD_HR",    "Head HR — Over-Norm Approval"),
                ("PENDING_DR_BHOON",   "Dr. Bhoon — Over-Norm Approval"),
                ("ACTIVE",             "Approved"),
            ]
        elif path == "OTHER_COMPANY":
            _stage_defs = [
                ("PENDING_BH",         "Business Head Review"),
                ("PENDING_HR_MANAGER", "HR Manager Review"),
                ("PENDING_HEAD_HR",    "Head HR Review"),
                ("PENDING_DR_BHOON",   "Dr. Bhoon Review"),
                ("ACTIVE",             "Approved"),
            ]
        else:
            _stage_defs = [
                ("PENDING_BH",         "Business Head Review"),
                ("PENDING_HR_MANAGER", "HR Manager Review"),
                ("PENDING_HEAD_HR",    "Head HR Review"),
                ("ACTIVE",             "Approved"),
            ]
        if path == "OVER_NORM":
            _stage_progress = _STAGE_PROGRESS_OVER_NORM
            _round_cur_progress = cur_progress_over_norm
        elif path == "OTHER_COMPANY":
            _stage_progress = _STAGE_PROGRESS_OTHER_COMPANY
            _round_cur_progress = cur_progress_other_company
        else:
            _stage_progress = _STAGE_PROGRESS
            _round_cur_progress = cur_progress

        # Build action map for this round only
        round_action_at = {}
        for act in round_acts:
            s = _ACTOR_STATUS_MAP.get(act.actor.role)
            if s:
                round_action_at[s] = act

        stages = []
        for status_val, label in _stage_defs:
            act = round_action_at.get(status_val)

            if status_val == "ACTIVE":
                if is_last_round and sv == "ACTIVE":
                    state = "done"
                else:
                    state = "upcoming"
            elif act is not None:
                if act.action == ApprovalActionType.REJECTED:
                    state = "rejected"
                elif act.action == ApprovalActionType.FLAGGED_SPECIAL:
                    state = "flagged"
                else:
                    state = "done"
            elif is_last_round:
                if sv == status_val:
                    state = "current"
                else:
                    sp = _stage_progress.get(status_val, 99)
                    state = "done" if _round_cur_progress > sp else "upcoming"
            else:
                # In a completed (rejected) round, any stage with no action = never reached
                state = "upcoming"

            stages.append({
                "label":           label,
                "state":           state,
                "actor_name":      act.actor.name if act else None,
                "actor_role_label":act.actor.role_label if act else None,
                "acted_at":        act.acted_at if act else None,
                "remark":          act.remark if act else None,
                "action_type":     act.action.value if act else None,
            })
        return stages

    # Build the full rounds list passed to the template
    workflow_rounds = []
    for i, round_acts in enumerate(rounds_raw):
        is_last = (i == len(rounds_raw) - 1)
        is_rejected_round = not is_last   # completed rounds always ended in rejection

        stages = _build_round_stages(round_acts, is_last)

        # "Submitted / Resubmitted" marker at the top of each round
        if i == 0:
            submit_stage = {
                "label":           "Submitted",
                "state":           "done",
                "actor_name":      req.initiator.name,
                "actor_role_label":"Initiator",
                "acted_at":        req.created_at,
                "remark":          None,
                "action_type":     None,
            }
        else:
            # Timestamp: just before the first action in this round (if any)
            first_act = round_acts[0] if round_acts else None
            submit_stage = {
                "label":           "Resubmitted",
                "state":           "done",
                "actor_name":      req.initiator.name,
                "actor_role_label":"Initiator",
                "acted_at":        first_act.acted_at if first_act else None,
                "remark":          None,
                "action_type":     None,
            }

        workflow_rounds.append({
            "number":      i + 1,
            "is_current":  is_last,
            "is_rejected": is_rejected_round,
            "submit_stage": submit_stage,
            "stages":      stages,
        })

    return render_template(
        "requests/detail.html",
        req=req,
        all_fields=all_fields,
        can_approve=can_approve,
        can_submit=can_submit,
        can_resubmit=can_resubmit,
        UserRole=UserRole,
        RequestStatus=RequestStatus,
        REJECTED_STATUSES=REJECTED_STATUSES,
        FieldType=FieldType,
        workflow_rounds=workflow_rounds,
        next_role=next_role,
    )


# ── Truein pre-flight check ──────────────────────────────────────────────────
# Called by the frontend right when the final approver clicks "Approve", so
# a likely Truein push problem (missing required field, bad mobile format)
# can be shown BEFORE they commit to the live push — see the "show the
# problem immediately" note in CLAUDE.md. Read-only: makes no HTTP call to
# Truein, just re-runs the same local payload build/validation push_employee()
# would use.

@requests_bp.route("/<string:token>/truein-preflight")
@login_required
def truein_preflight(token):
    req = _get_req_by_token(token)
    if not can_act_on(req, current_user):
        abort(403)
    try:
        new_status = get_new_status(req, current_user.role, ApprovalActionType.APPROVED)
    except ValueError:
        return jsonify({"applicable": False})
    if new_status != RequestStatus.ACTIVE:
        return jsonify({"applicable": False})
    from ..integrations.truein import preflight_check, is_company_tracked_in_truein
    if not is_company_tracked_in_truein(req.company_code):
        return jsonify({"applicable": False})
    result = preflight_check(req)
    return jsonify({"applicable": True, "issues": result["issues"]})


# ── Approve ────────────────────────────────────────────────────────────────────

@requests_bp.route("/<string:token>/approve", methods=["POST"])
@login_required
def approve_request(token):
    req = _get_req_by_token(token)
    if not can_act_on(req, current_user):
        abort(403)
    remark = request.form.get("remark", "").strip()
    min_len = 20 if req.is_special_case else 5
    if len(remark) < min_len:
        label = "justification for hiring outside norms" if req.is_special_case else "approval comment"
        flash(f"A {label} (min {min_len} characters) is required.", "danger")
        return redirect(url_for("requests_bp.view_request", token=req.public_token))
    try:
        new_status = get_new_status(req, current_user.role, ApprovalActionType.APPROVED)
    except ValueError:
        abort(400)
    _from_status = req.status
    action = ApprovalAction(request_id=req.id, actor_id=current_user.id,
                            action=ApprovalActionType.APPROVED, remark=remark)
    db.session.add(action)
    req.status = new_status
    req.updated_at = datetime.utcnow()
    _send_approval_notifications(db, req, new_status)
    # Log approve action
    _at = "REQUEST_ACTIVATED" if new_status == RequestStatus.ACTIVE else "REQUEST_APPROVED"
    log_audit("REQUEST", _at,
              resource_type="OnboardingRequest", resource_id=req.id,
              resource_label=f"Request #{req.id} — {req.candidate_name}",
              detail={"candidate_name": req.candidate_name,
                      "from_status": _from_status.value,
                      "to_status": new_status.value,
                      "approver_role": current_user.role.value,
                      "remark": remark})
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("An unexpected error occurred. Please try again.", "danger")
        return redirect(url_for("requests_bp.view_request", token=req.public_token))

    flash(f"Approved. Status: {req.status_label}", "success")

    # ── Auto-push to Truein when request reaches ACTIVE ───────────────────────
    # Runs synchronously, right here, the moment this approval activates the
    # request — never deferred, never waiting on a human to notice and ask.
    # is_company_tracked_in_truein() covers all 3 companies (RDC/ROBO/Ultrafine
    # — corrected 2026-09-23, see its docstring in truein.py; ROBO/Ultrafine
    # employees ARE registered in this Truein account, just filed under the
    # only site it has, "RDC Concrete") — this is effectively unconditional
    # today, kept only as a single choke point for a genuinely untracked
    # company if one is ever added. On failure the request is NOT left
    # silent: a background retry thread starts immediately (unless the
    # failure is a non-retryable data collision) and HR Manager/Head HR/
    # Admin are notified by email + in-app right here, synchronously.
    _push_issue = False
    from ..integrations.truein import is_company_tracked_in_truein
    if new_status == RequestStatus.ACTIVE and is_company_tracked_in_truein(req.company_code):
        from ..integrations.truein import (
            push_employee, start_retry_thread, _write_push_log,
            _handle_dropped_fields, _notify_push_failed,
        )
        from flask import current_app as _app
        try:
            push_result = push_employee(req)
            if push_result["success"]:
                req.truein_pushed_at       = datetime.utcnow()
                req.truein_push_error      = None
                req.truein_retry_count     = 1
                req.truein_last_attempt_at = datetime.utcnow()
                _write_push_log(db, req, push_result, triggered_by="auto")
                _handle_dropped_fields(db, req, push_result.get("dropped_fields", []), "auto")
                log_audit("REQUEST", "TRUEIN_PUSH_SUCCESS",
                          resource_type="OnboardingRequest", resource_id=req.id,
                          resource_label=f"Request #{req.id} — {req.candidate_name}",
                          detail={"empId": push_result["empId"],
                                  "attempt": 1, "message": push_result["message"],
                                  "dropped_fields": push_result.get("dropped_fields", [])})
                db.session.commit()
                _dropped = push_result.get("dropped_fields", [])
                if _dropped:
                    _push_issue = True
                    flash(f"Pushed to Truein (empId: {push_result['empId']}), but {len(_dropped)} field(s) "
                          f"were skipped — HR Manager has been notified to complete them.", "warning")
                else:
                    flash(f"Employee data pushed to Truein (empId: {push_result['empId']}).", "success")
            else:
                req.truein_push_error      = push_result["message"]
                req.truein_retry_count     = 1
                req.truein_last_attempt_at = datetime.utcnow()
                _retryable = push_result.get("retryable", True)
                _write_push_log(db, req, push_result, triggered_by="auto")
                _notify_push_failed(db, req, push_result["message"], triggered_by="auto", will_retry=_retryable)
                if not _retryable:
                    req.truein_retry_stopped = True
                db.session.commit()
                _push_issue = True
                if _retryable:
                    start_retry_thread(_app._get_current_object(), req.id)
                    flash(f"Approved, but the Truein push FAILED: {push_result['message']} — "
                          f"HR Manager, Head HR and Admin have been notified. Retrying automatically.", "danger")
                else:
                    flash(f"Approved, but the Truein push FAILED: {push_result['message']} — "
                          f"this is a data collision with an existing Truein employee, so it will NOT be "
                          f"retried automatically. HR Manager, Head HR and Admin have been notified.", "danger")
        except Exception as exc:
            _exc_result = {
                "success": False, "message": str(exc),
                "empId": None, "http_status": None,
                "raw_response": {}, "payload_sent": {},
            }
            req.truein_push_error      = str(exc)
            req.truein_retry_count     = 1
            req.truein_last_attempt_at = datetime.utcnow()
            _write_push_log(db, req, _exc_result, triggered_by="auto")
            try:
                _notify_push_failed(db, req, str(exc), triggered_by="auto")
            except Exception:
                pass
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            start_retry_thread(_app._get_current_object(), req.id)
            _push_issue = True
            flash(f"Approved, but the Truein push FAILED: {exc} — "
                  f"HR Manager, Head HR and Admin have been notified. Retrying automatically.", "danger")

    return redirect(url_for("requests_bp.view_request", token=req.public_token,
                             push_issue=1 if _push_issue else None))


# ── Reject ─────────────────────────────────────────────────────────────────────

@requests_bp.route("/<string:token>/reject", methods=["POST"])
@login_required
def reject_request(token):
    req = _get_req_by_token(token)
    if not can_act_on(req, current_user):
        abort(403)
    remark = request.form.get("remark", "").strip()
    if len(remark) < 10:
        flash("Rejection remark must be at least 10 characters.", "danger")
        return redirect(url_for("requests_bp.view_request", token=req.public_token))
    try:
        new_status = get_new_status(req, current_user.role, ApprovalActionType.REJECTED)
    except ValueError:
        abort(400)
    _from_status_r = req.status
    action = ApprovalAction(request_id=req.id, actor_id=current_user.id,
                            action=ApprovalActionType.REJECTED, remark=remark)
    db.session.add(action)
    req.status = new_status
    req.updated_at = datetime.utcnow()
    initiator = db.session.get(User, req.initiated_by)
    notify_users(db, req, [initiator],
                 subject=f"Request rejected: {req.candidate_name}",
                 body=f"Rejected by {current_user.name}.\n\nRemark: {remark}")
    log_audit("REQUEST", "REQUEST_REJECTED",
              resource_type="OnboardingRequest", resource_id=req.id,
              resource_label=f"Request #{req.id} — {req.candidate_name}",
              detail={"candidate_name": req.candidate_name,
                      "from_status": _from_status_r.value,
                      "to_status": new_status.value,
                      "rejector_role": current_user.role.value,
                      "remark": remark})
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("An unexpected error occurred. Please try again.", "danger")
        return redirect(url_for("requests_bp.view_request", token=req.public_token))
    flash("Request rejected.", "info")
    return redirect(url_for("main.dashboard"))


# ── Delete draft ───────────────────────────────────────────────────────────────

@requests_bp.route("/<string:token>/delete", methods=["POST"])
@login_required
@role_required(UserRole.INITIATOR)
def delete_request(token):
    req = _get_req_by_token(token)
    if req.initiated_by != current_user.id:
        abort(403)
    if req.status != RequestStatus.DRAFT:
        flash("Only DRAFT requests can be deleted.", "warning")
        return redirect(url_for("requests_bp.view_request", token=req.public_token))
    _cname = req.candidate_name
    req.is_deleted = True
    log_audit("REQUEST", "REQUEST_DELETED",
              resource_type="OnboardingRequest", resource_id=req.id,
              resource_label=f"Request #{req.id} — {_cname}",
              detail={"candidate_name": _cname,
                      "designation": req.designation,
                      "company": req.company_code})
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("An unexpected error occurred. Please try again.", "danger")
        return redirect(url_for("requests_bp.view_request", token=req.public_token))
    flash("Draft removed.", "info")
    return redirect(url_for("main.dashboard"))


# ── Approval notification helpers ──────────────────────────────────────────────

def _send_approval_notifications(db, req, new_status):
    from ..utils import hr_manager_ids_for_company

    if new_status == RequestStatus.PENDING_HR_MANAGER:
        recipients = User.query.filter(User.id.in_(hr_manager_ids_for_company(req.company_code))).all()
        notify_users(db, req, recipients,
                     subject=f"Approved by Business Head: {req.candidate_name}",
                     body=f"Please review the request for {req.candidate_name}.")
    elif new_status == RequestStatus.PENDING_HEAD_HR:
        # Unscoped — Head HR is never company-scoped. approved_by already
        # reads correctly for non-RDC: is_special_case is always False for
        # Ultrafine/ROBO (no staffing gate to set it), so this already says
        # "HR Manager", which is accurate — they always visit that step.
        recipients = User.query.filter_by(role=UserRole.HEAD_HR, is_active=True).all()
        approved_by = "Business Head" if req.is_special_case else "HR Manager"
        notify_users(db, req, recipients,
                     subject=f"Approved by {approved_by}: {req.candidate_name}",
                     body=f"{'Over-norm special approval' if req.is_special_case else 'Final approval'} "
                          f"needed for {req.candidate_name}.")
    elif new_status == RequestStatus.PENDING_DR_BHOON:
        recipients = User.query.filter_by(role=UserRole.DR_BHOON, is_active=True).all()  # unscoped
        if req.company_code == "RDC":
            body = (f"Approved by Head HR — over-norm hiring for {req.candidate_name} "
                     f"needs your final approval.")
        else:
            body = (f"Approved by Head HR — {req.candidate_name} ({req.company_code}) "
                     f"needs your final approval.")
        notify_users(db, req, recipients,
                     subject=f"Final approval required: {req.candidate_name}",
                     body=body)
    elif new_status == RequestStatus.ACTIVE:
        initiator = db.session.get(User, req.initiated_by)
        hr_managers = User.query.filter(User.id.in_(hr_manager_ids_for_company(req.company_code))).all()
        head_hrs = User.query.filter_by(role=UserRole.HEAD_HR, is_active=True).all()  # unscoped
        notify_users(db, req, [initiator] + hr_managers + head_hrs,
                     subject=f"Employee ACTIVE: {req.candidate_name}",
                     body=f"{req.candidate_name} is now ACTIVE. Download the Excel from the dashboard.")
