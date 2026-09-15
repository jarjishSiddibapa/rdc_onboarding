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

    Over-norm ("special case") requests skip HR Manager entirely:
    BH -> Head HR -> Dr. Bhoon -> Active. These two branch points diverge
    from the standard TRANSITIONS dict; everything else falls through to it.
    """
    cs = req.status
    if cs == RequestStatus.PENDING_BH and actor_role == UserRole.BUSINESS_HEAD and action == ApprovalActionType.APPROVED:
        return RequestStatus.PENDING_HEAD_HR if req.is_special_case else RequestStatus.PENDING_HR_MANAGER
    if cs == RequestStatus.PENDING_HEAD_HR and actor_role == UserRole.HEAD_HR and action == ApprovalActionType.APPROVED:
        return RequestStatus.PENDING_DR_BHOON if req.is_special_case else RequestStatus.ACTIVE
    key = (cs, actor_role, action)
    if key not in TRANSITIONS:
        raise ValueError(f"Invalid transition: {cs} + {actor_role} + {action}")
    return TRANSITIONS[key]


def bh_ids_for_initiator(initiator):
    """
    Return the set of active Business Head user ids eligible for this
    initiator's requests, or None to mean "unscoped — every active BH".

    Purely region-based (2026-09-04 redesign — the old direct
    User.business_head_id assignment is legacy/unused, see its comment in
    models.py): every active Business Head who shares AT LEAST ONE region
    with the initiator, via InitiatorRegion <-> BusinessHeadRegion overlap.
    Both sides can have multiple regions. Falls open (returns None, meaning
    "every active BH") when the initiator has no regions assigned at all,
    or none of their regions are actively covered by any BH — a request
    must always be reachable by someone rather than going nowhere.
    """
    from .models import User, BusinessHeadRegion, InitiatorRegion
    if not initiator:
        return None
    my_region_ids = {
        r.cluster_id for r in InitiatorRegion.query.filter_by(initiator_id=initiator.id).all()
    }
    if not my_region_ids:
        return None
    region_bh_ids = {
        r.business_head_id for r in
        BusinessHeadRegion.query.filter(BusinessHeadRegion.cluster_id.in_(my_region_ids)).all()
    }
    if not region_bh_ids:
        return None
    active_ids = {
        u.id for u in User.query.filter(
            User.id.in_(region_bh_ids),
            User.role == UserRole.BUSINESS_HEAD,
            User.is_active == True,  # noqa: E712
        ).all()
    }
    return active_ids if active_ids else None


def can_act_on(req, user):
    """Return True if this user can take an approval action on the request."""
    expected_status = ROLE_QUEUES.get(user.role)
    if expected_status is None or req.status != expected_status:
        return False
    # Business Head can only act on requests scoped to them by region.
    # Unscoped (fail-open) means any BH can act.
    if user.role == UserRole.BUSINESS_HEAD:
        initiator = req.initiator  # loaded via relationship
        ids = bh_ids_for_initiator(initiator)
        if ids is not None:
            return user.id in ids
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


def _send_smtp(cfg, recipients, subject, body):
    """Send email synchronously via smtplib using the given config dict."""
    import smtplib
    from email.mime.text import MIMEText
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


def _send_smtp_async(app, cfg, recipients, subject, body):
    with app.app_context():
        try:
            _send_smtp(cfg, recipients, subject, body)
        except Exception as e:
            app.logger.warning(f"Email send failed: {e}")


def send_email(subject, recipients, body):
    if not recipients:
        return
    app = current_app._get_current_object()
    cfg = get_db_mail_config()
    if not cfg["username"]:
        app.logger.info(f"[Email skipped — no SMTP config] To: {recipients} | Subject: {subject}")
        return
    t = threading.Thread(target=_send_smtp_async, args=(app, cfg, recipients, subject, body))
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


def notify_users(db, request_obj, recipients, subject, body, category="HIRING"):
    """
    category="HIRING" (default) — the 7 hiring-flow events (submit, resubmit,
    reject, each approval stage, final activation). category="ADMIN" — the 2
    operational/integration-health alerts (Truein push partial/failed). See
    CLAUDE.md's notification-preferences note. In-app notifications are never
    silenced by preference — only the outbound email is gated, and only for
    users who opted into "Hiring updates only" (User.email_mode).
    """
    for user in recipients:
        create_in_app_notification(db, request_obj, user, subject, body)
        if user.email_mode == "HIRING_ONLY" and category == "ADMIN":
            continue
        send_email(subject, [user.email], body)


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

def validate_mime(file_obj, allowed_exts=None):
    """
    Read the first 8 bytes of *file_obj*, check them against known magic bytes,
    then seek back to 0.  Returns True if the file's magic matches an allowed
    extension (or if we have no signature for this ext — conservative allow).
    Returns False only when we *positively* identify a mismatch.
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

    # No magic matched → we can't positively identify the type.
    # Be conservative: allow it through (extension check is the primary gate).
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
