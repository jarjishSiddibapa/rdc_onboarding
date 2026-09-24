"""
Truein HR Integration
=====================
Handles auth, payload mapping, dry-run preview, live employee push,
and infinite background retry with exponential backoff.
"""

import os
import json
import time
import threading
import requests
from datetime import datetime, timezone

# ── Truein API endpoints ────────────────────────────────────────────────────────
TRUEIN_TOKEN_URL       = "https://api.truein.com/connect/token"
TRUEIN_PUSH_URL        = "https://api.truein.com/apis/ext/attendance/v1.0/addEmployeeDtls"
TRUEIN_SITE_POINTS_URL = "https://api.truein.com/apis/ext/attendance/v1.0/getAllSitePoint"
TRUEIN_EMPLOYEES_URL   = "https://api.truein.com/apis/ext/attendance/v1.0/getEmployeeDtls"

# ── Credentials (env vars only — no hardcoded fallback, added 2026-09-15 as
# part of setting this project up as a git repo. Real values live in .env,
# which is gitignored; see .env for the values previously hardcoded here.) ──
# ACCESS_KEY / SECRET_KEY  → used for OAuth token generation (/connect/token)
# SUBSCRIPTION_KEY         → separate static key for legacy endpoints (addEmployeeDtls)
#                            Find it in: Truein admin dashboard → Settings → API Access
ACCESS_KEY       = os.environ.get("TRUEIN_ACCESS_KEY")
SECRET_KEY       = os.environ.get("TRUEIN_SECRET_KEY")
SUBSCRIPTION_KEY = os.environ.get("TRUEIN_SUBSCRIPTION_KEY")

# ── Field mapping: form_data key → Truein field name ──────────────────────────
# First match wins; denormalized columns take priority (handled in build_payload).
FIELD_MAP = {
    # Name variants
    "associate_name":        "name",
    "full_name":             "name",
    "candidate_name":        "name",
    # Employee ID variants
    "emp_id":                "empId",
    "employee_id":           "empId",
    "empId":                 "empId",
    "staff_id":              "empId",
    # Contact
    "email":                 "email",
    "email_address":         "email",
    "email_id":              "email",
    "mobile":                "mobile",
    "mobile_number":         "mobile",
    "phone":                 "mobile",
    "phone_number":          "mobile",
    "contact_number":        "mobile",
    # Personal
    "gender":                "gender",
    "date_of_birth":         "dob",
    "dob":                   "dob",
    "blood_group":           "blood_group",
    "marital_status":        "marital_status",
    "address":               "address",
    "current_address":       "address",
    "permanent_address":     "address",
    "nationality":           "nationality",
    "country":               "country",
    "state":                 "state",
    "city":                  "city",
    "qualification":         "qualification",
    # ID proof — form captures aadhar; we map to id_number (id_type set in build_payload)
    "aadhar_no":             "id_number",
    "id_number":             "id_number",
    "father_name":           "father_name",
    "uan_number":            "uan_number",
    # Bank details (step 3 of the form) — Truein has exact matching field
    # names for all three. Confirmed missing 2026-09-22: the form has
    # collected these since before this integration existed, but they were
    # never in FIELD_MAP, so they were silently dropped on every push —
    # same category of bug as the father_name/uan_number fix above.
    "bank_name":             "bank_name",
    "account_number":        "account_number",
    "ifsc_code":             "ifsc_code",
    # Employment
    "department":            "department",
    "dept":                  "department",
    "dept_code":             "dept_code",
    "joining_date":          "joining_date",
    "date_of_joining":       "joining_date",
    "doj":                   "joining_date",
    "contract_from":         "joining_date",  # form uses contract_from
    "employment_type":       "emp_type",
    "emp_type":              "emp_type",
    "employee_type":         "emp_type",
    "category":              "category",
    "grade":                 "grade",
    "division":              "division",
    "sub_division":          "sub_division",
    "sector":                "sector",
    "shift_code":            "shift_code",
    "contractor":            "contractor",
    "staff_function":        "staffFunction",
    "staffFunction":         "staffFunction",
    # Manager / hierarchy
    # NOTE: manager_emp_id is intentionally NOT mapped from reporting_manager_code.
    # reporting_manager_code in our forms is an internal HR code (e.g. "te00975")
    # that does NOT correspond to a Truein empId. Truein validates this field against
    # existing employees and rejects the push if the empId is not found.
    # Only add this back if managers are also onboarded into Truein first.
    # "reporting_manager_code": "manager_emp_id",  ← disabled
    # reporting_manager_name IS safe to send as-is — "manager" is a free-text
    # display field on Truein's side, unlike manager_emp_id which Truein
    # validates against its own employee list.
    "reporting_manager_name": "manager",
    # CORRECTED 2026-09-24 — confirmed live (request #41 "Sponge Bob"): even
    # after the 2026-09-24 fix that made _collect_form_data() actually save
    # l1_manager_emp_id (see TestManagerEmpIdPersistence), Truein's Staff
    # Directory still showed "Manager: -". Root cause: this line was mapping
    # our validated l1_manager_emp_id onto an outgoing JSON key of the same
    # name — but "l1_manager_emp_id" isn't a real Truein field at all
    # (confirmed against Truein_API_Developer_Reference.html — the only
    # manager-link field Truein's API documents is "manager_emp_id", the
    # very one disabled above). Truein silently ignores unknown JSON keys
    # rather than erroring, so the push always "succeeded" with no dropped-
    # field warning, and Manager just never got set. l1_manager_emp_id is
    # exactly the validated value manager_emp_id was disabled for lack of
    # (see the NOTE above — it can only be set by the Reporting Manager
    # typeahead in form.html actually picking a real Truein match, unlike
    # the free-typed reporting_manager_code), so it's now mapped to the
    # real field name instead. The existing "drop manager_emp_id and retry"
    # fallback (see push_employee()) already operates on this same outgoing
    # key, so a stale/no-longer-valid match is still handled gracefully.
    # l2_manager_emp_id was dead code — no form field or JS ever set it, and
    # it isn't a real Truein field either — removed rather than mapped.
    "l1_manager_emp_id":      "manager_emp_id",
    # Site / location
    "site_code":             "siteCode",
    "siteCode":              "siteCode",
    # Misc
    "title":                 "title",
    "role":                  "role",
    "regional_name":         "regional_name",
    "staff_unique_id":       "staff_unique_id",
    "payrate_name":          "payrateName",
    "payrateName":           "payrateName",
    # Captured from designation master at form-fill time (hidden field, mirrors notice_period pattern)
    "truein_app_attendance": "userAppAttendance",
}

# ── Required fields Truein enforces ───────────────────────────────────────────
REQUIRED_TRUEIN_FIELDS = ["empId", "name", "siteName"]

# ── Company gate ────────────────────────────────────────────────────────────
# Revised 2026-09-23 — corrects the 2026-09-15 "Robo/Ultrafine aren't
# tracked in Truein at all" finding, which was true about *sites* but not
# about *employees*: this account genuinely only has two site_name values
# ("RDC Concrete"/"RDC Drivers"), but real Ultrafine/ROBO employees ARE
# registered there too, filed under the "RDC Concrete" site (the only one
# available) with their real plant (e.g. "ROBO - Mumbai", "ULT-Wada") as
# the distinguishing signal — confirmed live: 28 real Ultrafine/ROBO
# employees found this way, same designations you'd expect (Plant Helper,
# Senior Mechanic, ...). So a Ultrafine/ROBO hire SHOULD be pushed the same
# way — build_payload() already hardcodes siteName to "RDC Concrete"
# regardless of company, and already falls back to the raw plant_location
# name for sitePoint/sub_site when no PlantDvtMapping exists (which is
# always the case for these two companies — see build_payload()'s comment).
# All three companies are tracked; this gate is now a no-op kept only so a
# genuinely untracked company (if one is ever added) has one place to gate.
TRUEIN_TRACKED_COMPANIES = {"RDC", "ROBO", "Ultrafine"}


def is_company_tracked_in_truein(company_code) -> bool:
    return company_code in TRUEIN_TRACKED_COMPANIES

# ── Department derivation for new hires ────────────────────────────────────────
# Our form never collects a raw "department" string from the initiator (unlike
# existing employees, who already carry one from ZingHR/Truein). For a NEW hire
# we derive it from the designation's staffing-norm category — the reverse of
# app/services/headcount.py::_DEPARTMENT_TO_CATEGORY, using the same canonical
# spelling that classifier already treats as authoritative for each category.
_CATEGORY_TO_DEPARTMENT = {
    "Plant Manager":                "PI/API/Acting PI",
    "Technical":                    "Technical",
    "Batchers/Production Officer":  "Batching",
    "Materials":                    "Materials",
    "Assistant":                    "Assistant/RMX",
    "Operations":                   "Operations",
}


def get_access_token() -> dict:
    """
    POSTs to the Truein auth endpoint and returns the token dict.
    Handles both list-wrapped and direct-dict response formats.
    Raises requests.HTTPError on non-2xx responses.
    """
    resp = requests.post(
        TRUEIN_TOKEN_URL,
        json={
            "access_key_id":     ACCESS_KEY,
            "secret_access_key": SECRET_KEY,
            "grant_type":        "client_credentials",
        },
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    # Truein wraps the token in data[0] (list) or data (dict) depending on API version
    data = body.get("data", body)
    return data[0] if isinstance(data, list) else data


def build_payload(req) -> dict:
    """
    Maps an OnboardingRequest to the Truein addEmployeeDtls body.
    Priority:
      1. Denormalized columns (always reliable)
      2. form_data walked through FIELD_MAP
      3. Auto-generated empId fallback
      4. Sensible defaults
    Never makes any HTTP call.
    """
    fd      = req.form_data or {}
    payload = {}

    # 1. Denormalized columns
    if req.candidate_name:  payload["name"]       = req.candidate_name
    if req.designation:     payload["designation"] = req.designation
    if req.company_code:    payload["company"]     = req.company_code

    # siteName is always "RDC Concrete" — the only active Truein site for this account.
    payload["siteName"] = "RDC Concrete"

    # sitePoint / sub_site / category — the plant/location the employee is
    # posted to. Confirmed 2026-09-10 (every real test push so far dropped
    # sitePoint): sending our own raw plant_location name here (e.g.
    # "CHE- Trisulam") doesn't reliably match what Truein actually has
    # registered — Truein's own string is often subtly different (e.g.
    # "CHE-Trisulam", no space), which is exactly what PlantDvtMapping.
    # truein_sub_site already holds (see matching.py's reconciliation).
    # sitePoint and sub_site are almost certainly the same underlying Truein
    # concept under two field names, so both now use the SAME reconciled,
    # known-good value whenever one exists — only falling back to our own
    # raw name (the old, drop-prone behavior) when no reconciliation has
    # happened yet for this plant.
    #
    # Ultrafine/ROBO plants (added 2026-09-15) NEVER have a PlantDvtMapping
    # row — that table is RDC/DVT-specific (see models.py's PlantLocation.company
    # note) — so they always take this fallback path. Confirmed 2026-09-15:
    # sub_site used to be left OUT of the payload entirely on this fallback
    # (only sitePoint got the raw name), so every non-RDC push silently sent
    # no plant-level location at all. Both fields now always default to the
    # raw plant name together, so neither is ever empty even with no
    # reconciliation on record.
    plant = fd.get("plant_location") or req.plant_location
    if plant:
        payload["sitePoint"] = plant
        payload["sub_site"]  = plant
        # category defaults to the plant name too — Truein shows this as
        # "Staff Category" and silently defaults it to "Other" when the
        # field is omitted entirely (confirmed live 2026-09-23: every
        # Ultrafine/ROBO push showed "Other" there, since those companies
        # never have a PlantDvtMapping/cluster to derive a real category
        # from — the block below overrides this with the reconciled
        # cluster name whenever one exists, same as sitePoint/sub_site).
        payload["category"] = plant
        try:
            from ..models import PlantDvtMapping as _PDM
            plant_map = _PDM.query.filter_by(plant_location_name=plant, is_deleted=False).first()
            if plant_map:
                site_value = plant_map.truein_sub_site or plant
                payload["sitePoint"] = site_value
                payload["sub_site"]  = site_value
                if plant_map.cluster:
                    payload["category"] = plant_map.cluster.truein_category or plant_map.cluster.canonical_cluster_name
        except Exception:
            pass

    # 2. Walk form_data through FIELD_MAP
    for form_key, truein_key in FIELD_MAP.items():
        val = fd.get(form_key)
        if val and truein_key not in payload:
            payload[truein_key] = val

    # 3. Normalize marital_status — Truein only accepts "Married" or "Unmarried".
    #    Our form uses Single/Married/Divorced/Widowed. Map or drop.
    _MARITAL_MAP = {
        "married":   "Married",
        "unmarried": "Unmarried",
        "single":    "Unmarried",   # legacy — form now shows Unmarried directly
        "other":     None,          # "Other" → omit field (Truein only accepts Married/Unmarried)
        "divorced":  None,          # not supported by Truein — omit
        "widowed":   None,
        "widow":     None,
        "widower":   None,
        "separated": None,
    }
    if "marital_status" in payload:
        _ms_truein = _MARITAL_MAP.get(payload["marital_status"].lower())
        if _ms_truein:
            payload["marital_status"] = _ms_truein
        else:
            del payload["marital_status"]   # unsupported → omit field entirely

    # 4. id_type — set alongside id_number so Truein knows what the ID is
    if "id_number" in payload and "id_type" not in payload:
        # Detect Aadhaar (12 digits) vs PAN (10 alphanumeric) vs fallback
        id_val = str(payload["id_number"]).strip()
        if id_val.isdigit() and len(id_val) == 12:
            payload["id_type"] = "Other"   # Truein accepts "Other" for Aadhaar
        else:
            payload["id_type"] = "Other"

    # 4. empId fallback — NEWJOINEE + DDMMYYYY + 4-digit daily sequence (resets from 0001 each day)
    if "empId" not in payload:
        from datetime import datetime as _dt
        created = req.created_at if req.created_at else _dt.utcnow()
        day_start = _dt(created.year, created.month, created.day, 0, 0, 0)
        day_end   = _dt(created.year, created.month, created.day, 23, 59, 59)
        try:
            from ..models import OnboardingRequest as _OR
            seq = _OR.query.filter(
                _OR.created_at >= day_start,
                _OR.created_at <= day_end,
                _OR.id <= req.id,
            ).count()
        except Exception:
            seq = req.id  # fallback outside app context
        date_str = created.strftime("%d%m%Y")
        payload["empId"] = f"NEWJOINEE{date_str}{seq:04d}"

    # 5. Fixed business rules (always override whatever form_data may have)
    payload["status"]          = "active"
    payload["role"]            = "Staff"
    payload["is_allot_leave"]  = "1"
    payload["allow_apply_leave"] = "1"
    payload["userAppAccess"]   = "1"

    # userAppAttendance — captured at form-fill time from the designation master
    # (hidden field "truein_app_attendance" is auto-populated by JS when a designation
    # is selected, exactly the same way notice_period is captured).
    # FIELD_MAP above maps "truein_app_attendance" → "userAppAttendance", so it is
    # already in the payload after step 2.  We override here only for old requests
    # that were created before this field existed (their form_data won't have it).
    desig_name = payload.get("designation") or req.designation
    desig_obj = None
    if desig_name:
        try:
            from ..models import Designation as _Desig
            desig_obj = _Desig.query.filter_by(name=desig_name, is_deleted=False).first()
        except Exception:
            pass

    if "userAppAttendance" not in payload:
        # Fallback: live lookup from designation master for legacy requests
        app_attendance = "1" if (desig_obj and desig_obj.truein_app_attendance) else "0"
        payload["userAppAttendance"] = app_attendance

    # department — see _CATEGORY_TO_DEPARTMENT above. Only ever a fallback:
    # the form has no "department" field today, so the FIELD_MAP walk never
    # populates this, but this keeps the ordering correct if one is added later.
    if "department" not in payload and desig_obj and desig_obj.norm_category_id:
        from ..extensions import db as _db
        from ..models import NormRoleCategory as _NormRoleCategory
        cat = _db.session.get(_NormRoleCategory, desig_obj.norm_category_id)
        if cat:
            dept = _CATEGORY_TO_DEPARTMENT.get(cat.name)
            if dept:
                payload["department"] = dept

    return payload


def _write_push_log(db, req, result: dict, triggered_by: str) -> None:
    """
    Append one TrueinPushLog row to the current SQLAlchemy session.
    Caller is responsible for committing.
    """
    from ..models import TrueinPushLog
    attempt_number = req.truein_retry_count or 0
    entry = TrueinPushLog(
        request_id       = req.id,
        attempt_number   = attempt_number,
        triggered_by     = triggered_by,
        emp_id           = result.get("empId"),
        payload_sent      = json.dumps(result.get("payload_sent", {}), indent=2, default=str),
        http_status       = result.get("http_status"),
        response_received = json.dumps(result.get("raw_response", {}), indent=2, default=str),
        success          = result.get("success", False),
        error_message    = None if result.get("success") else result.get("message"),
    )
    db.session.add(entry)


# Human-readable labels for fields we may drop, used in notifications + UI.
_DROPPED_FIELD_LABELS = {
    "manager_emp_id":        "Reporting Manager (manager emp id not found in Truein)",
    "sitePoint":             "Plant Location / Site Point (not configured in Truein)",
    "mobile":                "Mobile Number (invalid format — must be a 10-digit number starting with 6, 7, 8, or 9)",
    "mobile_truein_rejected": "Mobile Number (rejected by Truein — often means it's already registered to another employee in Truein)",
}


def _handle_dropped_fields(db, req, dropped: list, triggered_by: str) -> None:
    """
    When a Truein push only succeeded after dropping one or more fields:
      1. Persist the dropped-field list on the request (CSV).
      2. Write an audit-log entry.
      3. Notify every active HR Manager (in-app + email) so they can fill the
         missing data directly in Truein.
    Caller commits the session.
    """
    if not dropped:
        return

    from ..models import User, UserRole
    from ..utils import log_audit, notify_users

    req.truein_dropped_fields = ",".join(dropped)

    pretty = [_DROPPED_FIELD_LABELS.get(f, f) for f in dropped]
    pretty_list = "\n".join(f"  • {p}" for p in pretty)

    log_audit(
        "REQUEST", "TRUEIN_PUSH_PARTIAL",
        resource_type="OnboardingRequest", resource_id=req.id,
        resource_label=f"Request #{req.id} — {req.candidate_name}",
        detail={"dropped_fields": dropped, "triggered_by": triggered_by},
    )

    # Notify HR Managers (and Head HR) so someone can complete the record in Truein.
    recipients = User.query.filter(
        User.role.in_([UserRole.HR_MANAGER, UserRole.HEAD_HR]),
        User.is_active == True,  # noqa: E712
    ).all()

    if recipients:
        subject = f"Action needed: incomplete Truein push for {req.candidate_name}"
        body = (
            f"Employee {req.candidate_name} (Request #{req.id}) was pushed to Truein, "
            f"but the following field(s) could not be sent and were skipped:\n\n"
            f"{pretty_list}\n\n"
            f"The employee record exists in Truein (empId may have been generated), "
            f"but these field(s) are missing. Please log in to the Truein dashboard "
            f"and enter them manually for this employee.\n\n"
            f"— RDC Teamlease HR Onboarding Portal"
        )
        try:
            notify_users(db, req, recipients, subject, body, category="ADMIN")
        except Exception:
            pass  # never let notification failure break the push flow


def _notify_push_failed(db, req, error_message: str, triggered_by: str, will_retry: bool = True) -> None:
    """
    A Truein push outright failed (not just a partial "dropped field" case —
    Truein rejected the whole thing, or the request errored before Truein
    could even respond). Until 2026-09-04 this was only ever surfaced via a
    flash message to whoever happened to click Approve, plus a passive audit
    log entry — genuinely silent to everyone else, which is exactly how a
    real rejection (wrong candidate email colliding with an existing Truein
    employee, request #27 "Lalu Yadav") went unnoticed. Every full failure
    now gets a persistent in-app + email notification to every active
    Super Admin / Head HR / HR Manager — never rely on a flash message alone
    for this. Called once per distinct failure event (initial synchronous
    attempt, a manual "Push" retry, or the background retry loop's own
    terminal non-retryable failure — see will_retry below). NOT called on
    every ordinary background-retry attempt, which would otherwise spam a
    notification every 30s-1h forever.
    Caller commits the session.

    will_retry=False (added 2026-09-10): set when this failure is a genuine
    field collision with a DIFFERENT existing Truein employee — see the
    `retryable` flag in _parse_truein_response(). Retrying the exact same
    payload against the exact same collision will fail identically forever,
    so the background retry loop stops itself rather than looping pointlessly;
    the notification wording reflects that ("will NOT be retried automatically")
    instead of the old blanket "it will keep trying automatically" claim.
    """
    from ..models import User, UserRole
    from ..utils import log_audit, notify_users

    recipients = User.query.filter(
        User.role.in_([UserRole.SUPER_ADMIN, UserRole.HEAD_HR, UserRole.HR_MANAGER]),
        User.is_active == True,  # noqa: E712
    ).all()
    if not recipients:
        return

    subject = f"Truein push FAILED for {req.candidate_name} (Request #{req.id})"
    if will_retry:
        _retry_note = (
            f"This is being retried automatically in the background. If the "
            f"error is a data problem (e.g. an email/mobile/ID number that belongs to a "
            f"different employee already in Truein), retrying will keep failing until "
            f"the underlying data is corrected — check Admin → All Requests → "
            f"Truein Logs for the full history, and use \"Stop\" to halt pointless retries "
            f"once you've identified a data issue that needs fixing before retrying again."
        )
    else:
        _retry_note = (
            f"This will NOT be retried automatically — the error above means this exact "
            f"submitted value already belongs to a DIFFERENT employee in Truein, so resending "
            f"the same data would just fail again the same way. The underlying data must be "
            f"corrected (on this request, or on the colliding Truein record) before pushing "
            f"again manually from Admin → All Requests → Truein Logs."
        )
    body = (
        f"{req.candidate_name} (Request #{req.id}) was approved, but the push to "
        f"Truein failed — this employee is NOT yet in Truein.\n\n"
        f"Error from Truein: {error_message}\n\n"
        f"{_retry_note}\n\n"
        f"— RDC Teamlease HR Onboarding Portal"
    )
    try:
        notify_users(db, req, recipients, subject, body, category="ADMIN")
    except Exception:
        pass  # never let notification failure break the push flow

    log_audit(
        "REQUEST", "TRUEIN_PUSH_FAILURE_NOTIFIED",
        resource_type="OnboardingRequest", resource_id=req.id,
        resource_label=f"Request #{req.id} — {req.candidate_name}",
        detail={"error": error_message, "triggered_by": triggered_by,
                "recipients": [r.email for r in recipients]},
    )


def _parse_truein_response(resp, payload) -> dict:
    """Parse a raw Truein HTTP response into a standard result dict."""
    try:
        raw = resp.json()
    except Exception:
        raw = {"raw_text": resp.text[:500]}

    _msg      = (raw.get("message") or "").strip()
    _msg_lower = _msg.lower()
    _resp     = raw.get("response", "").strip().rstrip("!").lower()
    _code     = str(raw.get("code", "")).strip()
    # "Already exist(ed)/requested" means success ONLY when it's about the
    # push we just made being a duplicate of ITSELF (idempotent retry) — not
    # when a submitted field (email, mobile, ID number, ...) collides with a
    # DIFFERENT employee's existing record. Truein names the other employee
    # in that case ("... Match found with <name>(<code>) in <site>"), which
    # is the one reliable signal to tell the two apart. Confirmed against a
    # real production case where "Email Id already exist. Match found with
    # Rutuja(R00284) in RDC Concrete" was a genuine rejection for a
    # different candidate (Lalu Yadav) but got silently swallowed as
    # success by treating any "already exist" text as OK.
    _already = (
        ("already requested" in _msg_lower or "already exist" in _msg_lower)
        and "match found" not in _msg_lower
    )

    success = (
        (resp.status_code == 200 and _code == "200" and _resp == "success")
        or _already
    )

    # A genuine field collision (Truein names a DIFFERENT existing employee —
    # "Match found with <name>(<code>) in <site>") is a permanent data
    # problem: the submitted value (email, mobile, Govt ID, ...) belongs to
    # someone else in Truein, and resending the exact same payload will fail
    # identically forever. Not retryable — the background retry loop must
    # stop rather than hammer Truein every few minutes for no benefit. Any
    # other failure (network blip, transient Truein error, a field that gets
    # dropped and retried under the fallback chain) stays retryable.
    retryable = "match found" not in _msg_lower

    emp_id_returned = None
    data = raw.get("data", [])
    if isinstance(data, list) and data:
        emp_id_returned = data[0].get("empId") if isinstance(data[0], dict) else None
    elif isinstance(data, dict):
        emp_id_returned = data.get("empId")

    return {
        "success":      success,
        "retryable":    retryable,
        "empId":        emp_id_returned or payload.get("empId"),
        "message":      _msg or resp.reason or "Unknown error",
        "http_status":  resp.status_code,
        "raw_response": raw,
        "payload_sent": payload,
    }


def _do_push(payload: dict) -> dict:
    """Single HTTP POST to addEmployeeDtls. Returns parsed result dict."""
    resp = requests.post(
        TRUEIN_PUSH_URL,
        json=payload,
        headers={
            "Subscription-key": SUBSCRIPTION_KEY,
            "Content-Type":     "application/json",
        },
        timeout=30,
    )
    return _parse_truein_response(resp, payload)


def _clean_mobile(raw) -> tuple:
    """
    Single source of truth for what makes a mobile number valid for Truein:
    exactly 10 digits, starting 6-9 (real Indian mobile prefix rule), after
    stripping a +91/91 country-code prefix if present. Used by both
    push_employee() (before the real call) and preflight_check() (before
    the approver even clicks Approve), so the two can never disagree about
    what counts as valid.

    Returns (cleaned_10_digit_string, None) if valid, or (None, reason) if not.
    """
    digits = "".join(c for c in str(raw).strip() if c.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) != 10 or digits[0] not in "6789":
        return None, _DROPPED_FIELD_LABELS["mobile"]
    return digits, None


def preflight_check(req) -> dict:
    """
    Pure, local-only check of what push_employee() would send — makes no
    network call. Used to warn the final approver about a likely Truein
    push problem BEFORE they approve (instead of only finding out after,
    from a popup or a delayed HR email) — see requests_bp.truein_preflight().

    Only covers what CAN be determined without asking Truein: required
    fields being present, and mobile format. Truein's own site-point and
    manager-name registries aren't exposed by any usable API (confirmed —
    see "Endpoints defined but unused" in Truein_API_Developer_Reference.html),
    so a bad sitePoint/manager can only ever be caught by the live response
    and its own existing fallback/retry chain — that's still handled, just
    not predictable ahead of time.

    Returns {"issues": [{"field", "label"}]}.
    """
    payload = build_payload(req)
    issues = []

    for f in REQUIRED_TRUEIN_FIELDS:
        if not payload.get(f):
            issues.append({"field": f, "label": f'"{f}" is missing — Truein requires this on every push'})

    if "mobile" in payload:
        _, err = _clean_mobile(payload["mobile"])
        if err:
            issues.append({"field": "mobile", "label": err})

    return {"issues": issues}


def push_employee(req) -> dict:
    """
    Pushes employee data to Truein addEmployeeDtls (LIVE call).

    Smart retry chain — on specific Truein validation errors, the
    offending field is dropped and the push is retried automatically:
      0. Mobile pre-validation (10 digits, starts 6-9) — drop before attempt 1
         if it fails our own check, so we don't waste a round-trip on a
         number we already know Truein will reject.
      1. Full payload (all fields including manager_emp_id, sitePoint, mobile)
      2. If "provide correct manager emp id"  → retry without manager_emp_id
      3. If "provide correct Site Point"      → retry without sitePoint
      4. If "valid mobile number"             → retry without mobile (this
         number passed our pre-check, so Truein's rejection here has a
         different cause — see "mobile_truein_rejected" in dropped_fields)

    Returns a result dict:
        success        (bool)
        empId          (str)
        message        (str)
        http_status    (int)
        raw_response   (dict)
        payload_sent   (dict)
        dropped_fields (list) — fields silently removed before success

    Raises on network / auth errors (caller should catch).
    """
    if not SUBSCRIPTION_KEY:
        raise ValueError(
            "TRUEIN_SUBSCRIPTION_KEY is not set. "
            "Find it in your Truein admin dashboard → Settings → API Access."
        )

    payload = build_payload(req)

    dropped_fields = []

    # ── Mobile pre-validation (see _clean_mobile()) ────────────────────────
    # Validate before the first attempt so we don't waste a round-trip on a
    # number preflight_check() would already have flagged, and record it in
    # dropped_fields immediately — previously this pre-check popped the
    # field with NO record anywhere, so a candidate's number could vanish
    # with zero notification, audit trail, or on-screen explanation.
    if "mobile" in payload:
        cleaned, err = _clean_mobile(payload["mobile"])
        if err:
            payload.pop("mobile")  # invalid → omit rather than fail the whole push
            dropped_fields.append("mobile")
        else:
            payload["mobile"] = cleaned  # send the cleaned 10-digit form

    # ── Attempt 1: full payload ────────────────────────────────────────────
    result = _do_push(payload)
    if result["success"]:
        result["dropped_fields"] = dropped_fields
        return result

    _err = result["message"].lower()

    # ── Attempt 2: drop manager_emp_id if Truein rejects it ───────────────
    if "manager emp id" in _err and "manager_emp_id" in payload:
        dropped_fields.append("manager_emp_id")
        payload = {k: v for k, v in payload.items() if k != "manager_emp_id"}
        result = _do_push(payload)
        if result["success"]:
            result["dropped_fields"] = dropped_fields
            return result
        _err = result["message"].lower()

    # ── Attempt 3: drop sitePoint if Truein rejects it ────────────────────
    if "site point" in _err and "sitePoint" in payload:
        dropped_fields.append("sitePoint")
        payload = {k: v for k, v in payload.items() if k != "sitePoint"}
        result = _do_push(payload)
        if result["success"]:
            result["dropped_fields"] = dropped_fields
            return result
        _err = result["message"].lower()

    # ── Attempt 4: drop mobile if Truein rejects it ───────────────────────
    # This number already passed our own pre-validation (10 digits, starts
    # 6-9), so Truein's rejection here is a different, Truein-side reason
    # (commonly: already registered to another employee) — tagged separately
    # from the pre-validation drop above so the notification/popup doesn't
    # claim a format problem that isn't actually true.
    if "mobile" in _err and "mobile" in payload:
        dropped_fields.append("mobile_truein_rejected")
        payload = {k: v for k, v in payload.items() if k != "mobile"}
        result = _do_push(payload)

    result["dropped_fields"] = dropped_fields
    return result


# ── Retry engine ──────────────────────────────────────────────────────────────

# Tracks request IDs that already have a live retry thread so we never
# spawn duplicates (e.g. manual retry while background thread is running).
_active_retry_threads: set[int] = set()
_retry_lock = threading.Lock()

# Exponential backoff delays (seconds): 30s, 1m, 2m, 4m, 8m, 16m, 32m, 1h, 1h, …
_BACKOFF_BASE   = 30      # first wait in seconds
_BACKOFF_FACTOR = 2       # multiply by this each attempt
_BACKOFF_CAP    = 3600    # never wait more than 1 hour


def _backoff(attempt: int) -> int:
    """Return how many seconds to wait before attempt N (0-indexed)."""
    delay = _BACKOFF_BASE * (_BACKOFF_FACTOR ** attempt)
    return min(delay, _BACKOFF_CAP)


def _retry_loop(app, req_id: int) -> None:
    """
    Background daemon thread: push the request to Truein, retrying indefinitely
    with exponential backoff until success or an admin stops it.
    Runs entirely outside the request context — uses its own app context.
    """
    attempt = 0
    with app.app_context():
        from ..extensions import db
        from ..models import OnboardingRequest, RequestStatus

        while True:
            # ── Reload fresh from DB every loop ───────────────────────────────
            req = db.session.get(OnboardingRequest, req_id)

            # Stop conditions
            if req is None:
                break
            if req.truein_pushed_at:          # already succeeded (e.g. manual push won)
                break
            if req.truein_retry_stopped:       # admin hit "Stop retrying"
                break
            if req.status != RequestStatus.ACTIVE:
                break                          # request was somehow un-activated

            # ── Attempt push ──────────────────────────────────────────────────
            try:
                result = push_employee(req)
            except Exception as exc:
                result = {"success": False, "message": str(exc),
                          "empId": None, "http_status": None, "raw_response": {}}

            from ..utils import log_audit
            now = datetime.utcnow()

            if result["success"]:
                req.truein_pushed_at       = now
                req.truein_push_error      = None
                req.truein_last_attempt_at = now
                req.truein_retry_count     = (req.truein_retry_count or 0) + 1
                _write_push_log(db, req, result, triggered_by="auto")
                _handle_dropped_fields(db, req, result.get("dropped_fields", []), "auto")
                log_audit("REQUEST", "TRUEIN_PUSH_SUCCESS",
                          resource_type="OnboardingRequest", resource_id=req.id,
                          resource_label=f"Request #{req.id} — {req.candidate_name}",
                          detail={"empId": result["empId"],
                                  "attempt": req.truein_retry_count,
                                  "message": result["message"],
                                  "dropped_fields": result.get("dropped_fields", [])})
                db.session.commit()
                app.logger.info(
                    f"[Truein] Request #{req_id} pushed successfully "
                    f"(empId={result['empId']}, attempt={req.truein_retry_count})"
                )
                break   # ✅ done

            else:
                req.truein_push_error      = result["message"]
                req.truein_last_attempt_at = now
                req.truein_retry_count     = (req.truein_retry_count or 0) + 1
                _write_push_log(db, req, result, triggered_by="auto")

                # A genuine collision with a DIFFERENT existing Truein
                # employee (result["retryable"] is False) will fail
                # identically on every future attempt too — stop looping
                # instead of hammering Truein forever with the same payload,
                # and tell the same people who'd otherwise wait on a retry
                # that never succeeds.
                if not result.get("retryable", True):
                    req.truein_retry_stopped = True
                    log_audit("REQUEST", "TRUEIN_PUSH_STOPPED_NONRETRYABLE",
                              resource_type="OnboardingRequest", resource_id=req.id,
                              resource_label=f"Request #{req.id} — {req.candidate_name}",
                              detail={"attempt": req.truein_retry_count,
                                      "message": result["message"],
                                      "http_status": result["http_status"]})
                    db.session.commit()
                    try:
                        _notify_push_failed(db, req, result["message"], "auto", will_retry=False)
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                    app.logger.warning(
                        f"[Truein] Request #{req_id} push failed with a non-retryable "
                        f"collision (attempt {req.truein_retry_count}, "
                        f"error: {result['message']}). Stopping retries."
                    )
                    break

                log_audit("REQUEST", "TRUEIN_PUSH_FAILED",
                          resource_type="OnboardingRequest", resource_id=req.id,
                          resource_label=f"Request #{req.id} — {req.candidate_name}",
                          detail={"attempt": req.truein_retry_count,
                                  "message": result["message"],
                                  "http_status": result["http_status"]})
                db.session.commit()

                wait = _backoff(attempt)
                app.logger.warning(
                    f"[Truein] Request #{req_id} push failed "
                    f"(attempt {req.truein_retry_count}, "
                    f"error: {result['message']}). "
                    f"Retrying in {wait}s."
                )
                db.session.remove()   # release connection during sleep
                time.sleep(wait)
                attempt += 1

    # Clean up the active-thread tracker
    with _retry_lock:
        _active_retry_threads.discard(req_id)


def start_retry_thread(app, req_id: int) -> bool:
    """
    Spawn a background retry thread for req_id if one isn't already running.
    Returns True if a new thread was started, False if one was already live.
    Safe to call from any route.
    """
    with _retry_lock:
        if req_id in _active_retry_threads:
            return False
        _active_retry_threads.add(req_id)

    t = threading.Thread(target=_retry_loop, args=(app, req_id), daemon=True)
    t.start()
    return True


def resume_pending_retries(app) -> int:
    """
    Called once at app startup: find every ACTIVE request that hasn't been
    pushed yet (and hasn't been manually stopped) and start a retry thread.
    Returns the number of threads started.
    """
    with app.app_context():
        from ..extensions import db
        from ..models import OnboardingRequest, RequestStatus

        pending = OnboardingRequest.query.filter(
            OnboardingRequest.status       == RequestStatus.ACTIVE,
            OnboardingRequest.is_deleted   == False,
            OnboardingRequest.truein_pushed_at  == None,
            OnboardingRequest.truein_retry_stopped == False,
            OnboardingRequest.company_code.in_(TRUEIN_TRACKED_COMPANIES),
        ).all()

        started = 0
        for req in pending:
            if start_retry_thread(app, req.id):
                started += 1
                app.logger.info(
                    f"[Truein] Resuming retry thread for request #{req.id} "
                    f"({req.candidate_name}) after app restart."
                )
        return started


# ── Lookup helpers (managers + site points) ───────────────────────────────────
# Both are derived from the single getEmployeeDtls endpoint (no dedicated
# site-point API exists in Truein). Results are cached for 3 hours — longer
# than the 2-hourly StaffingSnapshot refresh interval (app/services/
# snapshot_refresh.py), so that background job keeps this cache warm and an
# interactive request (e.g. the site/manager picker AJAX endpoints) never
# has to pay for a full multi-minute paginated fetch itself.

_CACHE_TTL = 3 * 3600  # seconds

# Truein caps each getEmployeeDtls call at 1000 rows. The response's
# more_rows/last_uid fields drive cursor pagination — send the previous
# call's last_uid back as the (camelCase) lastUid query param to advance.
# The endpoint is rate-limited (~1 request per 10-15s); pace calls at 50s
# to stay well clear of HTTP 429s across an 8-page pull.
_PAGE_DELAY_S = 50

# Raw employee list — shared between managers and site-point derivation
_employees_cache: list | None = None
_employees_cache_at: float = 0.0


def _sub_key_headers() -> dict:
    return {"Subscription-key": SUBSCRIPTION_KEY, "Content-Type": "application/json"}


def _fetch_all_employees_raw() -> list[dict]:
    """Return the full raw employee list from Truein (all pages), cached for 3 hours."""
    global _employees_cache, _employees_cache_at
    now = time.time()
    if _employees_cache is not None and (now - _employees_cache_at) < _CACHE_TTL:
        return _employees_cache

    all_items: list[dict] = []
    last_uid = ""
    while True:
        params = {"lastUid": last_uid} if last_uid else {}
        resp = requests.get(TRUEIN_EMPLOYEES_URL, headers=_sub_key_headers(), params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        items = body.get("data", [])
        all_items.extend(items if isinstance(items, list) else [])

        if str(body.get("more_rows", "0")) != "1":
            break
        new_last_uid = body.get("last_uid")
        if not new_last_uid or new_last_uid == last_uid:
            break
        last_uid = new_last_uid
        time.sleep(_PAGE_DELAY_S)

    _employees_cache = all_items
    _employees_cache_at = now
    return _employees_cache


def get_cached_employees_if_warm() -> list[dict] | None:
    """
    Returns the employee list ONLY if the cache is already warm — never
    triggers a live fetch. The full paginated pull takes several minutes
    (8 pages, ~50s apart), so callers that can't afford to block a web
    request (e.g. an admin "Run Auto-Match" button) should use this instead
    of fetch_managers() when a stale/cold answer is acceptable. Returns None
    if nothing is cached yet.
    """
    if _employees_cache is not None and (time.time() - _employees_cache_at) < _CACHE_TTL:
        return _employees_cache
    return None


def fetch_managers() -> list[dict]:
    """
    Return only employees flagged is_manager=1 as [{empId, name}], cached
    for 3h (via _fetch_all_employees_raw). If a fresh live pull fails
    (network error, Truein rate-limit/429, timeout) and this process has
    ANY previously-fetched employee list — even one past its 3h TTL — fall
    back to it rather than raising: a slightly stale manager list is far
    more useful to the picker than a hard "API error", and manager
    identities rarely change day to day. The 2-hourly StaffingSnapshot
    background refresh (app/services/snapshot_refresh.py) also populates
    this same shared _employees_cache, so in practice a "prestored" answer
    is almost always available even right after a live-call failure. Only
    raises if there's truly no cached data at all yet (e.g. a live failure
    on the very first call after a fresh server start).
    """
    try:
        employees = _fetch_all_employees_raw()
    except Exception:
        if _employees_cache is None:
            raise
        employees = _employees_cache
    return _filter_managers(employees)


def _filter_managers(employees: list[dict]) -> list[dict]:
    return [
        {"empId": e.get("empId", "").strip(), "name": e.get("name", "").strip()}
        for e in employees
        if str(e.get("is_manager", "0")) == "1"
        and e.get("empId", "").strip()
        and e.get("name", "").strip()
    ]


def get_managers_from_cache_only() -> list[dict]:
    """
    Like fetch_managers(), but NEVER triggers a live pull — reads only
    whatever is already in _employees_cache, regardless of its 3h TTL.
    This is what the interactive manager-search endpoint
    (app/requests_bp/routes.py::truein_managers) should call: a cold cache
    means fetch_managers()/_fetch_all_employees_raw() would otherwise start
    a live paginated pull that takes several minutes (8 pages, ~50s apart —
    see _PAGE_DELAY_S), which left the picker showing "Searching Truein…"
    indefinitely and effectively blocking the form. The 2-hourly
    StaffingSnapshot background refresh keeps this same shared cache warm
    in practice, so this only ever returns [] in the narrow window right
    after a fresh server start before that first refresh completes.
    """
    return _filter_managers(_employees_cache or [])


def get_cached_sub_sites_if_warm() -> list[str] | None:
    """
    Unique 'sub_site' values from the warm employee cache — the plant-level
    location field on each Truein employee record (distinct from 'category',
    which is cluster-level — see app/services/headcount.py and
    app/services/matching.py). Returns None if the cache is cold rather than
    triggering a live multi-minute paginated fetch.
    """
    cached = get_cached_employees_if_warm()
    if cached is None:
        return None
    seen = set()
    points = []
    for e in cached:
        sub = (e.get("sub_site") or "").strip()
        if sub and sub not in seen:
            seen.add(sub)
            points.append(sub)
    return sorted(points)


def dry_run_to_file(req, output_dir: str) -> tuple:
    """
    Dry-run: builds the employee payload but does NOT call addEmployeeDtls.
    Writes results to a timestamped JSON file.
    Returns (filepath, result_dict).
    """
    payload        = build_payload(req)
    missing_fields = [f for f in REQUIRED_TRUEIN_FIELDS if not payload.get(f)]

    # Check subscription key
    sub_key_status = "SET" if SUBSCRIPTION_KEY else "NOT SET — required for live push"

    result = {
        "dry_run":        True,
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "request_id":     req.id,
        "candidate_name": req.candidate_name,
        "request_status": req.status.value,

        "auth_note": (
            "addEmployeeDtls uses a static Subscription-key header only "
            "(legacy endpoint — no Bearer token needed). "
            f"TRUEIN_SUBSCRIPTION_KEY status: {sub_key_status}"
        ),

        "employee_push": {
            "would_call":  TRUEIN_PUSH_URL,
            "method":      "POST",
            "headers": {
                "Subscription-key": SUBSCRIPTION_KEY or "<NOT SET>",
                "Content-Type":     "application/json",
            },
            "body":                    payload,
            "missing_required_fields": missing_fields,
            "would_succeed":           len(missing_fields) == 0 and bool(SUBSCRIPTION_KEY),
        },

        "note": "DRY RUN — addEmployeeDtls was NOT called. No employee data was sent to Truein.",
    }

    os.makedirs(output_dir, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"truein_dryrun_req{req.id}_{ts}.json"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)

    return filepath, result
