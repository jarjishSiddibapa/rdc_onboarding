import json as _json
import threading
from functools import wraps
from flask import abort, current_app
from flask_login import current_user
from flask_mail import Message
from .extensions import mail
from .models import UserRole, RequestStatus, ApprovalActionType, AuditCategory


# ── Password validation ────────────────────────────────────────────────────────

def validate_password(pw: str) -> list[str]:
    """Return a list of validation error messages (empty = OK)."""
    errors = []
    if len(pw) < 8:
        errors.append("Password must be at least 8 characters.")
    if not any(c.isupper() for c in pw):
        errors.append("Password must contain at least one uppercase letter.")
    if not any(c.isdigit() for c in pw):
        errors.append("Password must contain at least one number.")
    return errors


# ── Role guard ─────────────────────────────────────────────────────────────────

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator


# ── State machine ──────────────────────────────────────────────────────────────

# Maps (current_status, actor_role, action) → new_status
TRANSITIONS = {
    # Initiator submits draft
    (RequestStatus.DRAFT, UserRole.INITIATOR, "submit"): RequestStatus.PENDING_BH,

    # Business Head actions
    (RequestStatus.PENDING_BH, UserRole.BUSINESS_HEAD, ApprovalActionType.APPROVED): RequestStatus.PENDING_HR_MANAGER,
    (RequestStatus.PENDING_BH, UserRole.BUSINESS_HEAD, ApprovalActionType.REJECTED): RequestStatus.REJECTED_BH,

    # Dr. Bhoon actions
    (RequestStatus.PENDING_DR_BHOON, UserRole.DR_BHOON, ApprovalActionType.APPROVED): RequestStatus.ACTIVE,
    (RequestStatus.PENDING_DR_BHOON, UserRole.DR_BHOON, ApprovalActionType.REJECTED): RequestStatus.REJECTED_DR_BHOON,

    # HR Manager actions
    (RequestStatus.PENDING_HR_MANAGER, UserRole.HR_MANAGER, ApprovalActionType.APPROVED): RequestStatus.PENDING_HEAD_HR,
    (RequestStatus.PENDING_HR_MANAGER, UserRole.HR_MANAGER, ApprovalActionType.REJECTED): RequestStatus.REJECTED_HRM,

    # Head HR actions
    (RequestStatus.PENDING_HEAD_HR, UserRole.HEAD_HR, ApprovalActionType.APPROVED): RequestStatus.ACTIVE,
    (RequestStatus.PENDING_HEAD_HR, UserRole.HEAD_HR, ApprovalActionType.REJECTED): RequestStatus.REJECTED_HEAD_HR,

    # Initiator resubmits from any REJECTED status
    (RequestStatus.REJECTED_BH, UserRole.INITIATOR, "resubmit"): RequestStatus.PENDING_BH,
    (RequestStatus.REJECTED_DR_BHOON, UserRole.INITIATOR, "resubmit"): RequestStatus.PENDING_BH,
    (RequestStatus.REJECTED_HRM, UserRole.INITIATOR, "resubmit"): RequestStatus.PENDING_BH,
    (RequestStatus.REJECTED_HEAD_HR, UserRole.INITIATOR, "resubmit"): RequestStatus.PENDING_BH,
}

# Which status each role can act on
ROLE_QUEUES = {
    UserRole.BUSINESS_HEAD: RequestStatus.PENDING_BH,
    UserRole.DR_BHOON: RequestStatus.PENDING_DR_BHOON,
    UserRole.HR_MANAGER: RequestStatus.PENDING_HR_MANAGER,
    UserRole.HEAD_HR: RequestStatus.PENDING_HEAD_HR,
}

REJECTED_STATUSES = {
    RequestStatus.REJECTED_BH,
    RequestStatus.REJECTED_DR_BHOON,
    RequestStatus.REJECTED_HRM,
    RequestStatus.REJECTED_HEAD_HR,
}

PENDING_STATUSES = {
    RequestStatus.PENDING_BH,
    RequestStatus.PENDING_DR_BHOON,
    RequestStatus.PENDING_HR_MANAGER,
    RequestStatus.PENDING_HEAD_HR,
}


def get_new_status(req, actor_role, action):
    """Return new status or raise ValueError if transition is invalid.

    Over-norm ("special case") RDC requests skip HR Manager entirely:
    BH -> Head HR -> Dr. Bhoon -> Active. Non-RDC (Ultrafine/ROBO) requests
    (2026-09-21) have their own fixed chain that always visits every role,
    never branching by is_special_case (there's no staffing gate for them
    to trigger a special case at all): BH -> HR Manager -> Head HR ->
    Dr. Bhoon -> Active. These branch points diverge from the standard
    TRANSITIONS dict; everything else falls through to it.

    HR-Manager-initiated requests (req.hr_manager_initiated, 2026-09-29)
    skip PENDING_HR_MANAGER too, regardless of company or is_special_case —
    checked first, ahead of both branches above, since it overrides either
    of them the same way: an HR Manager hiring their own candidate has no
    separate HR Manager left to review it.
    """
    cs = req.status
    if cs == RequestStatus.PENDING_BH and actor_role == UserRole.BUSINESS_HEAD and action == ApprovalActionType.APPROVED:
        if req.hr_manager_initiated:
            return RequestStatus.PENDING_HEAD_HR      # HR Manager stage skipped — see docstring
        if req.company_code != "RDC":
            return RequestStatus.PENDING_HR_MANAGER   # fixed chain — no special-case branch for non-RDC
        return RequestStatus.PENDING_HEAD_HR if req.is_special_case else RequestStatus.PENDING_HR_MANAGER
    if cs == RequestStatus.PENDING_HEAD_HR and actor_role == UserRole.HEAD_HR and action == ApprovalActionType.APPROVED:
        if req.company_code != "RDC":
            return RequestStatus.PENDING_DR_BHOON     # fixed chain — always visits Dr. Bhoon
        return RequestStatus.PENDING_DR_BHOON if req.is_special_case else RequestStatus.ACTIVE
    key = (cs, actor_role, action)
    if key not in TRANSITIONS:
        raise ValueError(f"Invalid transition: {cs} + {actor_role} + {action}")
    return TRANSITIONS[key]


def company_scope_ids(user_id) -> set[str]:
    """Companies (RDC/Ultrafine/ROBO) a user is ticked for. Fail-closed —
    empty set if they have no UserCompanyScope rows at all (see that
    model's docstring for why this differs from the region tables' fail-open
    convention)."""
    from .models import UserCompanyScope
    return {r.company for r in UserCompanyScope.query.filter_by(user_id=user_id).all()}


def initiator_region_cluster_ids(initiator_id):
    """
    Cluster (region) ids this initiator is assigned to via InitiatorRegion.
    Returns None — not an empty set — when the initiator has zero region
    rows, meaning "unscoped, every region" (same fail-open convention as
    bh_ids_for_initiator's region half, deliberately not the company-scope
    tables' fail-closed one, since a not-yet-region-assigned initiator must
    still be able to submit *something* rather than seeing an empty plant
    dropdown).

    Added 2026-09-24 to fix a real gap: InitiatorRegion has driven which
    Business Head sees/can act on a request since 2026-09-04, but the
    initiator's own New Request plant picker never filtered by it at all —
    a Mumbai-only initiator could freely pick a plant in Assam. Used to
    narrow both the Plant Location dropdown (requests_bp.py's
    _dvt_matched_plant_options()/plant_locations_api()/new_request()) and
    the matching submit-time defense-in-depth check, RDC only — Ultrafine/
    ROBO have no region concept at all (see bh_ids_for_initiator's own
    docstring).
    """
    from .models import InitiatorRegion
    ids = {r.cluster_id for r in InitiatorRegion.query.filter_by(initiator_id=initiator_id).all()}
    return ids or None


def hr_manager_ids_for_company(company_code) -> set[int]:
    """Active HR Manager user ids ticked for company_code. Fail-closed —
    empty set if nobody is ticked."""
    from .models import User, UserCompanyScope
    ids = {r.user_id for r in UserCompanyScope.query.filter_by(company=company_code).all()}
    if not ids:
        return set()
    return {
        u.id for u in User.query.filter(
            User.id.in_(ids), User.role == UserRole.HR_MANAGER, User.is_active == True,  # noqa: E712
        ).all()
    }


def bh_ids_for_initiator(initiator, company_code):
    """
    Return the set of active Business Head user ids eligible for a request
    from this initiator for this company. Fail-closed on company scope
    (2026-09-21, deliberate stakeholder choice, unlike the region tables'
    fail-open convention below) — an empty set means nobody is eligible,
    not "unscoped, every active BH".

    For company_code == "RDC": among Business / Functional Heads ticked for RDC, the
    original 2026-09-04 region-overlap logic still applies unchanged —
    every active RDC-ticked BH who shares at least one region with the
    initiator, via InitiatorRegion <-> BusinessHeadRegion overlap. Falls
    open onto "every RDC-ticked active BH" (not "every active BH
    system-wide" like before this change) when the initiator has no
    regions assigned, or none of their regions are covered by any
    RDC-ticked BH — a request must always be reachable by someone within
    the RDC-ticked pool rather than going nowhere.

    For company_code in ("Ultrafine", "ROBO"): there is no region concept
    at all (confirmed with the stakeholder — a Robo/Ultrafine initiator can
    hire at any of that company's plants, and any Business / Functional Head ticked for
    that company can approve any request for it) — every active
    company-ticked BH is eligible, full stop, no further narrowing.
    """
    from .models import User, BusinessHeadRegion, InitiatorRegion, UserCompanyScope
    if not initiator:
        return set()

    company_bh_ids = {r.user_id for r in UserCompanyScope.query.filter_by(company=company_code).all()}
    active_company_bh_ids = set()
    if company_bh_ids:
        active_company_bh_ids = {
            u.id for u in User.query.filter(
                User.id.in_(company_bh_ids),
                User.role == UserRole.BUSINESS_HEAD,
                User.is_active == True,  # noqa: E712
            ).all()
        }
    if not active_company_bh_ids:
        return set()

    if company_code != "RDC":
        return active_company_bh_ids

    my_region_ids = {
        r.cluster_id for r in InitiatorRegion.query.filter_by(initiator_id=initiator.id).all()
    }
    if not my_region_ids:
        return active_company_bh_ids
    region_bh_ids = {
        r.business_head_id for r in
        BusinessHeadRegion.query.filter(BusinessHeadRegion.cluster_id.in_(my_region_ids)).all()
    }
    if not region_bh_ids:
        return active_company_bh_ids
    return (region_bh_ids & active_company_bh_ids) or active_company_bh_ids


def can_act_on(req, user):
    """Return True if this user can take an approval action on the request."""
    expected_status = ROLE_QUEUES.get(user.role)
    if expected_status is None or req.status != expected_status:
        return False
    if user.role == UserRole.BUSINESS_HEAD:
        return user.id in bh_ids_for_initiator(req.initiator, req.company_code)
    if user.role == UserRole.HR_MANAGER:
        return req.company_code in company_scope_ids(user.id)
    return True


# ── Email ──────────────────────────────────────────────────────────────────────

def get_db_mail_config():
    """Return effective mail config: DB values (SystemConfig) override env/app.config.
    Falls back silently if the DB is not yet reachable (e.g. during migrations)."""
    try:
        from .models import SystemConfig
        def _get(key, fallback=None):
            row = SystemConfig.query.filter_by(key=key).first()
            v = (row.value or "").strip() if row else ""
            return v or fallback
        return {
            "server":   _get("email_host",  current_app.config.get("MAIL_SERVER",  "smtp.gmail.com")),
            "port":     int(_get("email_port", current_app.config.get("MAIL_PORT", 587)) or 587),
            "username": _get("email_user",  current_app.config.get("MAIL_USERNAME")),
            "password": _get("email_pass",  current_app.config.get("MAIL_PASSWORD")),
            "sender":   _get("email_from",  current_app.config.get("MAIL_DEFAULT_SENDER")),
        }
    except Exception:
        return {
            "server":   current_app.config.get("MAIL_SERVER",  "smtp.gmail.com"),
            "port":     int(current_app.config.get("MAIL_PORT", 587) or 587),
            "username": current_app.config.get("MAIL_USERNAME"),
            "password": current_app.config.get("MAIL_PASSWORD"),
            "sender":   current_app.config.get("MAIL_DEFAULT_SENDER"),
        }


def _send_smtp(cfg, recipients, subject, body, html=None):
    """Send email synchronously via smtplib using the given config dict.
    With `html`, sends a multipart/alternative (plain text first, HTML preferred
    by clients that can show it) so the plain-text fallback is always readable."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    if html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["From"]    = cfg["sender"] or cfg["username"]
    msg["To"]      = ", ".join(recipients) if isinstance(recipients, list) else recipients
    msg["Subject"] = subject
    rcpt = recipients if isinstance(recipients, list) else [recipients]
    with smtplib.SMTP(cfg["server"], cfg["port"], timeout=30) as s:
        s.ehlo()
        s.starttls()
        s.ehlo()
        s.login(cfg["username"], cfg["password"])
        s.send_message(msg)


def _send_smtp_async(app, cfg, recipients, subject, body, html=None):
    with app.app_context():
        try:
            _send_smtp(cfg, recipients, subject, body, html)
        except Exception as e:
            app.logger.warning(f"Email send failed: {e}")


def send_email(subject, recipients, body, html=None):
    if not recipients:
        return
    app = current_app._get_current_object()
    cfg = get_db_mail_config()
    if not cfg["username"]:
        app.logger.info(f"[Email skipped — no SMTP config] To: {recipients} | Subject: {subject}")
        return
    t = threading.Thread(target=_send_smtp_async, args=(app, cfg, recipients, subject, body, html))
    t.daemon = True
    t.start()


# ── Notification helpers ───────────────────────────────────────────────────────

def create_in_app_notification(db, request_obj, recipient, subject, body):
    from .models import Notification, NotificationType
    notif = Notification(
        request_id=request_obj.id,
        recipient_id=recipient.id,
        type=NotificationType.IN_APP,
        subject=subject,
        body=body,
    )
    db.session.add(notif)


def notify_users(db, request_obj, recipients, subject, body, category="HIRING", actions=False):
    """
    category="HIRING" (default) — the 7 hiring-flow events (submit, resubmit,
    reject, each approval stage, final activation). category="ADMIN" — the 2
    operational/integration-health alerts (Truein push partial/failed). See
    CLAUDE.md's notification-preferences note. In-app notifications are never
    silenced by preference — only the outbound email is gated, and only for
    users who opted into "Hiring updates only" (User.email_mode).

    actions=True (2026-10-08) — the email is an approval request: each recipient
    who can act on the request right now gets personal Approve / Reject buttons
    (signed, expiring links — see build_email_links()). The in-app notification
    stays plain text. Every email that is about a request also gets an
    "Open request" link when the public base URL is known.
    """
    for user in recipients:
        create_in_app_notification(db, request_obj, user, subject, body)
        if user.email_mode == "HIRING_ONLY" and category == "ADMIN":
            continue
        links = build_email_links(request_obj, user, with_actions=actions)
        if links:
            send_email(subject, [user.email], render_email_text(body, links),
                       html=render_email_html(subject, body, request_obj, links))
        else:
            send_email(subject, [user.email], body)


# ── Approve / Reject straight from the email (2026-10-08) ─────────────────────
#
# Each approval email carries two buttons. The link is signed for ONE person,
# ONE request and the status it was pending at, and expires after
# EMAIL_ACTION_MAX_AGE. No login is needed — the signature is the credential.
# Opening the link only shows a confirmation page (requests_bp.email_action); the
# POST behind its Confirm button re-checks everything live (current status, user
# active, can_act_on) and then runs the normal approve/reject code as that person.

EMAIL_ACTION_SALT = "email-approval-action"
EMAIL_ACTION_MAX_AGE = 3 * 24 * 3600   # 3 days


def _email_action_serializer():
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=EMAIL_ACTION_SALT)


def make_email_action_token(user_id, request_id, status_value, round_no=0):
    """round_no = the request's retry_count: a request that is rejected and resubmitted lands on the same
    status again, which must NOT revive links from the earlier round's emails."""
    return _email_action_serializer().dumps({"u": user_id, "r": request_id, "s": status_value, "n": round_no})


def read_email_action_token(token):
    """Return {"u", "r", "s", "n"} or raise itsdangerous.SignatureExpired / BadSignature."""
    return _email_action_serializer().loads(token, max_age=EMAIL_ACTION_MAX_AGE)


def app_base_url():
    """Public address of this app for links inside emails — ONLY from settings an administrator controls:
    the address saved in Admin -> Email Settings (SystemConfig "app_base_url"), else the APP_BASE_URL env
    setting. Deliberately never derived from the incoming request: this app sits behind ProxyFix, which
    trusts X-Forwarded-Host, and even a plain Host header is client-controlled, so anyone able to submit a
    request could otherwise make the approvers' emails (which carry personal approval links) point at a
    server they control. Returns "" when unset — emails then simply go out without links/buttons."""
    base = ""
    try:
        from .models import SystemConfig
        row = SystemConfig.query.filter_by(key="app_base_url").first()
        base = (row.value or "").strip() if row else ""
    except Exception:
        base = ""
    base = (base or current_app.config.get("APP_BASE_URL") or "").strip().rstrip("/")
    return base if base.lower().startswith(("http://", "https://")) else ""


def build_email_links(req, user, with_actions=False):
    """{"open": url, "approve": url, "reject": url} for this recipient, or None when the
    base URL is unknown. approve/reject only when with_actions and the user can act now."""
    try:
        from flask import url_for
        from urllib.parse import urlsplit
        base = app_base_url()
        if not base or not getattr(req, "public_token", None):
            return None

        def _path(endpoint, **values):
            # Only the path: url_for may return an absolute URL (SERVER_NAME set), and the
            # host must always be the public base URL, never whatever the server thinks it is.
            u = urlsplit(url_for(endpoint, _external=False, **values))
            return u.path + (("?" + u.query) if u.query else "")

        links = {"open": base + _path("requests_bp.view_request", token=req.public_token)}
        if with_actions and can_act_on(req, user):
            tok = make_email_action_token(user.id, req.id, req.status.value, req.retry_count or 0)
            for act in ("approve", "reject"):
                links[act] = base + _path("requests_bp.email_action", signed=tok, action=act)
        return links
    except Exception as exc:  # a link problem must never stop the email itself
        current_app.logger.warning(f"[Email] could not build links: {exc}")
        return None


def render_email_text(body, links):
    lines = [body, ""]
    if "approve" in links:
        lines += ["Approve: " + links["approve"], "Reject:  " + links["reject"], "",
                  "No sign-in needed: each link opens a confirmation page, and nothing is recorded until "
                  "you confirm there. The links are personal to you and expire in 3 days.", ""]
    lines.append("Open request: " + links["open"])
    return "\n".join(lines)


def render_email_html(subject, body, req, links):
    from html import escape
    def _btn(label, url, bg):
        return (f'<a href="{escape(url)}" style="display:inline-block;padding:12px 26px;margin:0 8px 8px 0;'
                f'background:{bg};color:#ffffff;text-decoration:none;border-radius:8px;font-weight:700;'
                f'font-size:14px;">{label}</a>')
    rows = [("Candidate", req.candidate_name), ("Company", req.company_code),
            ("Designation", req.designation), ("Plant", req.plant_location)]
    detail = "".join(
        f'<tr><td style="padding:4px 14px 4px 0;color:#667085;font-size:13px;">{k}</td>'
        f'<td style="padding:4px 0;color:#101828;font-size:13px;font-weight:600;">{escape(str(v))}</td></tr>'
        for k, v in rows if v)
    if "approve" in links:
        actions = (_btn("Approve", links["approve"], "#067647") + _btn("Reject", links["reject"], "#B42318") +
                   '<p style="font-size:12px;color:#667085;margin:10px 0 0;">No sign-in needed. You confirm on the next page '
                   'before anything is recorded. These buttons are personal to you and expire in 3 days.</p>')
    else:
        actions = _btn("Open request", links["open"], "#0B5CAD")
    open_line = ('<p style="font-size:12.5px;margin:14px 0 0;"><a href="' + escape(links["open"]) +
                 '" style="color:#0B5CAD;">Open the full request</a></p>') if "approve" in links else ""
    return (
        '<div style="background:#F2F4F7;padding:24px 12px;font-family:Segoe UI,Arial,sans-serif;">'
        '<div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:12px;padding:28px;">'
        '<div style="font-size:12px;font-weight:700;color:#0B5CAD;letter-spacing:.06em;text-transform:uppercase;">'
        'RDC Associates Hiring</div>'
        f'<h2 style="font-size:18px;color:#101828;margin:8px 0 12px;">{escape(subject)}</h2>'
        f'<p style="font-size:14px;color:#344054;line-height:1.55;margin:0 0 16px;">{escape(body).replace(chr(10), "<br>")}</p>'
        f'<table style="border-collapse:collapse;margin:0 0 20px;">{detail}</table>'
        f'{actions}{open_line}'
        '<p style="font-size:11.5px;color:#98A2B3;margin:22px 0 0;border-top:1px solid #EAECF0;padding-top:12px;">'
        'This is an automated message from RDC Associates Hiring.</p>'
        '</div></div>')


# ── File upload ────────────────────────────────────────────────────────────────

def allowed_file(filename, allowed=None):
    if allowed is None:
        allowed = {"pdf", "doc", "docx", "jpg", "jpeg", "png"}
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed


# Magic-byte signatures for allowed file types (first N bytes)
_MAGIC = {
    b"\x25\x50\x44\x46":          "pdf",   # %PDF
    b"\xff\xd8\xff":               "jpg",   # JPEG
    b"\x89\x50\x4e\x47\x0d\x0a":  "png",   # PNG
    b"\x50\x4b\x03\x04":          "docx",  # ZIP-based (docx, xlsx …)
    b"\xd0\xcf\x11\xe0":          "doc",   # OLE2 compound (doc, xls …)
}

# Extensions we have a magic-byte signature for (keys of _MAGIC, plus xlsx
# which shares docx's ZIP signature) — used by validate_mime() below to tell
# "this extension has no known signature at all" (gif/webp — stay
# conservative) apart from "this extension has a signature and the content
# didn't match it" (a real, positively-identified mismatch).
_KNOWN_SIGNATURE_EXTS = {"pdf", "doc", "jpg", "jpeg", "png", "docx", "xlsx"}


def validate_mime(file_obj, allowed_exts=None):
    """
    Read the first 8 bytes of *file_obj*, check them against known magic bytes,
    then seek back to 0. Returns True if the file's magic matches an allowed
    extension, or if the file's own claimed extension has no known signature
    at all (conservative allow — extension check remains the primary gate for
    those, e.g. gif/webp avatar uploads). Returns False when the claimed
    extension DOES have a known signature but the content doesn't match it —
    e.g. a plain text file renamed to "resume.pdf".

    Fixed 2026-09-26 — this used to return True unconditionally whenever no
    magic matched, regardless of the claimed extension, which defeated the
    documented purpose (CLAUDE.md: "Always run validate_mime() ... in
    addition to extension checking") for every one of the 6 default allowed
    extensions, since all 6 (pdf/doc/docx/jpg/jpeg/png) DO have a signature
    here — a masquerading file renamed to a trusted-looking extension sailed
    through untouched, to later be opened by HR/approvers under that
    trusted-looking filename.
    """
    if allowed_exts is None:
        allowed_exts = {"pdf", "doc", "docx", "jpg", "jpeg", "png"}

    header = file_obj.read(8)
    file_obj.seek(0)

    for magic, detected_ext in _MAGIC.items():
        if header[:len(magic)] == magic:
            # We recognised the file type — make sure it's in the allowed set.
            # docx and xlsx both look like ZIP; we allow both.
            ok_exts = {"docx", "xlsx"} if detected_ext == "docx" else {detected_ext}
            return bool(ok_exts & allowed_exts)

    filename = getattr(file_obj, "filename", "") or ""
    claimed_ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if claimed_ext in _KNOWN_SIGNATURE_EXTS:
        return False
    return True


# ── Audit logging ──────────────────────────────────────────────────────────────

def log_audit(category, action_type, *,
              resource_type=None, resource_id=None,
              resource_label=None, detail=None, actor_id=None):
    """
    Append an AuditLog row to the current SQLAlchemy session.

    The row is NOT committed here — the caller's db.session.commit() flushes it
    atomically with the action it describes. For standalone auth events (e.g.
    failed logins) that already did their own commit, the caller must do a
    second db.session.commit() after this call.

    Args:
        category    : AuditCategory value or its string equivalent, e.g. "AUTH"
        action_type : Short machine-readable code, e.g. "LOGIN_SUCCESS"
        resource_type  : Model class name, e.g. "User", "OnboardingRequest"
        resource_id    : Integer PK of the affected object (for deep linking)
        resource_label : Human-readable label shown in the audit table
        detail         : dict/list (serialised to JSON) or plain string with
                         before/after values and any extra context
        actor_id       : Override the actor (defaults to current_user.id).
                         Pass explicitly for pre-auth events (e.g. login attempts).
    """
    from .extensions import db
    from .models import AuditLog
    from flask import request as _freq

    try:
        _actor_id = actor_id
        if _actor_id is None:
            try:
                if current_user.is_authenticated:
                    _actor_id = current_user.id
            except Exception:
                pass

        _ip = None
        try:
            # ProxyFix (applied in create_app) makes remote_addr the real client IP.
            # Fall back through common forwarding headers just in case.
            _ip = (
                _freq.environ.get("HTTP_X_REAL_IP")
                or _freq.environ.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
                or _freq.remote_addr
            )
        except Exception:
            pass

        _detail_str = None
        if detail is not None:
            _detail_str = (
                _json.dumps(detail, default=str)
                if isinstance(detail, (dict, list))
                else str(detail)
            )

        entry = AuditLog(
            actor_id=_actor_id,
            action_category=AuditCategory(category),
            action_type=action_type,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_label=resource_label,
            detail=_detail_str,
            ip_address=_ip,
        )
        db.session.add(entry)
    except Exception as exc:
        try:
            current_app.logger.warning(f"[audit] log_audit({action_type}) failed: {exc}")
        except Exception:
            pass


def fmt_date(value):
    """Any date the app displays is DD/MM/YYYY. Accepts a date/datetime, an ISO string ("2026-10-09",
    optionally with a time) or ZingHR's "18 Aug 2025"; anything else is returned unchanged (never guessed),
    None/blank gives ""."""
    from datetime import date as _d, datetime as _dt
    if value is None or value == "":
        return ""
    if isinstance(value, (_dt, _d)):
        return value.strftime("%d/%m/%Y")
    txt = str(value).strip()
    for fmt, n in (("%Y-%m-%d", 10), ("%d %b %Y", None), ("%d-%b-%Y", None)):
        try:
            return _dt.strptime(txt[:n] if n else txt, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return txt
