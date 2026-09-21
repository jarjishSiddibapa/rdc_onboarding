from datetime import datetime, timedelta
from urllib.parse import urlencode
import os
from flask import render_template, redirect, url_for, flash, request, abort, jsonify, current_app

# ── Pagination helpers ─────────────────────────────────────────────────────────
_PP_CHOICES = frozenset({10, 25, 50, 100})

def _per_page(default: int) -> int:
    """Read ?per_page from the request, validated against the allowed set."""
    v = request.args.get("per_page", default, type=int)
    return v if v in _PP_CHOICES else default

def _pg_base() -> str:
    """Base URL for pagination links — strips both 'page' and 'per_page'."""
    args = {k: v for k, v in request.args.items() if k not in ("page", "per_page") and v}
    return request.path + ("?" + urlencode(args) + "&" if args else "?")
from flask_login import login_required, current_user
from sqlalchemy.exc import SQLAlchemyError
from ..extensions import db, bcrypt, limiter
from ..models import (
    User, UserRole, OnboardingRequest, ApprovalAction, RequestStatus,
    PlantLocation, Designation, FormField, FormFieldOption, FieldType, OptionsSource,
    AuditLog, AuditCategory, NormRoleCategory,
    PlantDvtMapping, ClusterNameMapping, BusinessHeadRegion, InitiatorRegion,
    MatchConfidence, COMPANY_CHOICES, UserCompanyScope,
)
from ..utils import role_required, validate_password, log_audit
from . import admin_bp


def _admin_or_hr():
    allowed = {UserRole.SUPER_ADMIN, UserRole.HEAD_HR, UserRole.HR_MANAGER}
    if current_user.role not in allowed:
        abort(403)


def _active_clusters():
    """Regions (clusters) offered as checkboxes when assigning a Business Head."""
    return ClusterNameMapping.query.filter_by(is_deleted=False).order_by(ClusterNameMapping.canonical_cluster_name).all()


def _set_bh_regions(user, region_id_strs):
    """
    Replace a Business Head's region assignments with the submitted checkbox
    set. Returns {"from": [...names], "to": [...names]} for audit logging,
    or None if nothing changed. Caller commits.
    """
    ids = set()
    for s in region_id_strs:
        try:
            ids.add(int(s))
        except (ValueError, TypeError):
            pass
    existing_links = BusinessHeadRegion.query.filter_by(business_head_id=user.id).all()
    existing_ids = {link.cluster_id for link in existing_links}
    if ids == existing_ids:
        return None
    names_by_id = {c.id: c.canonical_cluster_name for c in ClusterNameMapping.query.filter(
        ClusterNameMapping.id.in_(ids | existing_ids)).all()}
    to_remove = existing_ids - ids
    to_add = ids - existing_ids
    for link in existing_links:
        if link.cluster_id in to_remove:
            db.session.delete(link)
    for cid in to_add:
        db.session.add(BusinessHeadRegion(business_head_id=user.id, cluster_id=cid))
    return {
        "from": sorted(names_by_id.get(i, str(i)) for i in existing_ids),
        "to": sorted(names_by_id.get(i, str(i)) for i in ids),
    }


def _set_initiator_regions(user, region_id_strs):
    """
    Replace an Initiator's region assignments with the submitted checkbox
    set — the mirror of _set_bh_regions() for InitiatorRegion. Returns
    {"from": [...names], "to": [...names]} for audit logging, or None if
    nothing changed. Caller commits.
    """
    ids = set()
    for s in region_id_strs:
        try:
            ids.add(int(s))
        except (ValueError, TypeError):
            pass
    existing_links = InitiatorRegion.query.filter_by(initiator_id=user.id).all()
    existing_ids = {link.cluster_id for link in existing_links}
    if ids == existing_ids:
        return None
    names_by_id = {c.id: c.canonical_cluster_name for c in ClusterNameMapping.query.filter(
        ClusterNameMapping.id.in_(ids | existing_ids)).all()}
    to_remove = existing_ids - ids
    to_add = ids - existing_ids
    for link in existing_links:
        if link.cluster_id in to_remove:
            db.session.delete(link)
    for cid in to_add:
        db.session.add(InitiatorRegion(initiator_id=user.id, cluster_id=cid))
    return {
        "from": sorted(names_by_id.get(i, str(i)) for i in existing_ids),
        "to": sorted(names_by_id.get(i, str(i)) for i in ids),
    }


# Roles that get company scoping (2026-09-21) — Head HR/Dr. Bhoon/Super
# Admin stay unscoped, confirmed with the stakeholder.
_COMPANY_SCOPED_ROLES = {UserRole.INITIATOR, UserRole.BUSINESS_HEAD, UserRole.HR_MANAGER}


def _set_company_scope(user, company_strs):
    """
    Replace a user's company-scope ticks (UserCompanyScope). Returns
    {"from": [...], "to": [...]} for audit logging, or None if nothing
    changed. Caller commits. Fail-closed downstream (see
    app/utils.py::company_scope_ids()) — an empty result here means this
    user can act on/submit nothing at all, so new_user()/edit_user() make
    at least one tick mandatory for the 3 scoped roles before validation
    even reaches this helper.
    """
    ticked = {c for c in company_strs if c in COMPANY_CHOICES}
    existing_links = UserCompanyScope.query.filter_by(user_id=user.id).all()
    existing = {link.company for link in existing_links}
    if ticked == existing:
        return None
    for link in existing_links:
        if link.company not in ticked:
            db.session.delete(link)
    for c in ticked - existing:
        db.session.add(UserCompanyScope(user_id=user.id, company=c))
    return {"from": sorted(existing), "to": sorted(ticked)}


# ── Availability check endpoints (AJAX) ───────────────────────────────────────

@admin_bp.route("/api/check-email")
@login_required
@role_required(UserRole.SUPER_ADMIN)
def check_email():
    """Return {available: bool} for a given email, optionally excluding a user id."""
    email = request.args.get("email", "").strip().lower()
    exclude_id = request.args.get("exclude_id", type=int)
    if not email:
        return jsonify({"available": False, "error": "Empty"})
    q = User.query.filter_by(email=email)
    if exclude_id:
        q = q.filter(User.id != exclude_id)
    available = q.first() is None
    return jsonify({"available": available})


@admin_bp.route("/api/check-username")
@login_required
@role_required(UserRole.SUPER_ADMIN)
def check_username():
    """Return {available: bool} for a given username, optionally excluding a user id."""
    username = request.args.get("username", "").strip().lower()
    exclude_id = request.args.get("exclude_id", type=int)
    if not username:
        return jsonify({"available": True})   # blank username is always "ok" (it's optional)
    q = User.query.filter_by(username=username)
    if exclude_id:
        q = q.filter(User.id != exclude_id)
    available = q.first() is None
    return jsonify({"available": available})


# ── Users ──────────────────────────────────────────────────────────────────────

@admin_bp.route("/users/<int:user_id>/change-password", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def admin_change_password(user_id):
    user = db.get_or_404(User, user_id)
    new_pwd = request.form.get("new_password", "").strip()
    errors = validate_password(new_pwd)
    if errors:
        for e in errors:
            flash(e, "danger")
    else:
        user.password_hash = bcrypt.generate_password_hash(new_pwd).decode("utf-8")
        log_audit("USER_MGMT", "USER_PASSWORD_CHANGED_BY_ADMIN",
                  resource_type="User", resource_id=user.id,
                  resource_label=user.name,
                  detail={"target_user": user.name, "target_email": user.email,
                          "target_role": user.role.value})
        db.session.commit()
        flash(f"Password updated for {user.name}.", "success")
    return redirect(url_for("admin.users_list"))


@admin_bp.route("/users")
@login_required
@role_required(UserRole.SUPER_ADMIN)
def users_list():
    # BH map (used for display)
    rows = db.session.execute(db.text(
        "SELECT u.id, bh.name, bh.is_active "
        "FROM users u JOIN users bh ON bh.id = u.business_head_id "
        "WHERE u.business_head_id IS NOT NULL"
    )).fetchall()
    bh_map = {r[0]: {"name": r[1], "is_active": bool(r[2])} for r in rows}

    active_users   = User.query.filter_by(is_active=True ).order_by(User.created_at.desc()).all()
    inactive_users = User.query.filter_by(is_active=False).order_by(User.created_at.desc()).all()

    return render_template(
        "admin/users.html",
        active_users=active_users,
        inactive_users=inactive_users,
        UserRole=UserRole, bh_map=bh_map,
        # keep for password-modal JS compatibility
        users=active_users + inactive_users,
    )


@admin_bp.route("/users/new", methods=["GET", "POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def new_user():
    clusters = _active_clusters()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role_val = request.form.get("role")
        username_raw = request.form.get("username", "").strip().lower()
        username = username_raw or None
        employee_code = request.form.get("employee_code", "").strip().upper() or None
        ticked_companies = {c for c in request.form.getlist("companies") if c in COMPANY_CHOICES}
        try:
            parsed_role_for_check = UserRole(role_val) if role_val else None
        except ValueError:
            parsed_role_for_check = None
        if not all([name, email, password, role_val, employee_code]):
            flash("All fields are required, including Employee Code.", "danger")
        elif User.query.filter_by(email=email).first():
            flash("Email already in use.", "danger")
        elif username and User.query.filter_by(username=username).first():
            flash("Username already taken.", "danger")
        elif parsed_role_for_check in _COMPANY_SCOPED_ROLES and not ticked_companies:
            flash("Select at least one Company Scope tick mark for this role.", "danger")
        else:
            try:
                parsed_role = UserRole(role_val)
            except ValueError:
                flash("Invalid role selected.", "danger")
                return render_template("admin/user_form.html", user=None, UserRole=UserRole,
                                       clusters=clusters, current_region_ids=[],
                                       companies=COMPANY_CHOICES, current_companies=[])
            user = User(
                name=name, email=email, username=username,
                employee_code=employee_code,
                password_hash=bcrypt.generate_password_hash(password).decode("utf-8"),
                role=parsed_role,
            )
            db.session.add(user)
            db.session.commit()

            # Company scope, then region assignment (region only relevant —
            # and only offered/saved — for RDC-ticked BH/Initiator; HR
            # Manager never gets region-narrowing regardless of company).
            scope_changes = None
            region_changes = None
            if parsed_role in _COMPANY_SCOPED_ROLES:
                scope_changes = _set_company_scope(user, ticked_companies)
                if parsed_role == UserRole.BUSINESS_HEAD:
                    region_changes = _set_bh_regions(
                        user, request.form.getlist("regions") if "RDC" in ticked_companies else [])
                elif parsed_role == UserRole.INITIATOR:
                    region_changes = _set_initiator_regions(
                        user, request.form.getlist("regions") if "RDC" in ticked_companies else [])

            log_audit("USER_MGMT", "USER_CREATED",
                      resource_type="User", resource_id=user.id,
                      resource_label=user.name,
                      detail={"name": name, "email": email, "role": role_val,
                              "username": username, "employee_code": employee_code,
                              "companies": scope_changes["to"] if scope_changes else None,
                              "regions": region_changes["to"] if region_changes else None})
            db.session.commit()
            flash(f"User {name} created successfully.", "success")
            return redirect(url_for("admin.users_list"))
    return render_template("admin/user_form.html", user=None, UserRole=UserRole,
                           clusters=clusters, current_region_ids=[],
                           companies=COMPANY_CHOICES, current_companies=[])


@admin_bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def edit_user(user_id):
    user = db.get_or_404(User, user_id)
    clusters = _active_clusters()

    def _current_region_ids(u):
        if u.role == UserRole.BUSINESS_HEAD:
            return [r.cluster_id for r in BusinessHeadRegion.query.filter_by(business_head_id=u.id)]
        if u.role == UserRole.INITIATOR:
            return [r.cluster_id for r in InitiatorRegion.query.filter_by(initiator_id=u.id)]
        return []

    def _current_company_ids(u):
        return [r.company for r in UserCompanyScope.query.filter_by(user_id=u.id)]

    current_region_ids = _current_region_ids(user)
    current_company_ids = _current_company_ids(user)
    if request.method == "POST":
        # Capture before-state for audit diff
        _old_name     = user.name
        _old_email    = user.email
        _old_username = user.username
        _old_role     = user.role.value
        _old_empcode  = user.employee_code

        new_name = request.form.get("name", user.name).strip()
        new_email = request.form.get("email", user.email).strip().lower()
        new_username_raw = request.form.get("username", "").strip().lower()
        new_username = new_username_raw or None
        new_empcode = request.form.get("employee_code", "").strip().upper() or None

        # Email uniqueness check (exclude self)
        existing_email = User.query.filter_by(email=new_email).first()
        if existing_email and existing_email.id != user.id:
            flash("Email already in use by another account.", "danger")
            return render_template("admin/user_form.html", user=user, UserRole=UserRole,
                                   clusters=clusters, current_region_ids=current_region_ids,
                                   companies=COMPANY_CHOICES, current_companies=current_company_ids)

        # Username uniqueness check (exclude self)
        if new_username:
            existing_uname = User.query.filter_by(username=new_username).first()
            if existing_uname and existing_uname.id != user.id:
                flash("Username already taken.", "danger")
                return render_template("admin/user_form.html", user=user, UserRole=UserRole,
                                   clusters=clusters, current_region_ids=current_region_ids,
                                   companies=COMPANY_CHOICES, current_companies=current_company_ids)

        role_val = request.form.get("role")
        _effective_role = user.role
        if role_val:
            try:
                _effective_role = UserRole(role_val)
            except ValueError:
                flash("Invalid role selected.", "danger")
                return render_template("admin/user_form.html", user=user, UserRole=UserRole,
                                   clusters=clusters, current_region_ids=current_region_ids,
                                   companies=COMPANY_CHOICES, current_companies=current_company_ids)

        ticked_companies = {c for c in request.form.getlist("companies") if c in COMPANY_CHOICES}
        if _effective_role in _COMPANY_SCOPED_ROLES and not ticked_companies:
            flash("Select at least one Company Scope tick mark for this role.", "danger")
            return render_template("admin/user_form.html", user=user, UserRole=UserRole,
                                   clusters=clusters, current_region_ids=current_region_ids,
                                   companies=COMPANY_CHOICES, current_companies=current_company_ids)

        user.name = new_name
        user.email = new_email
        user.username = new_username
        user.employee_code = new_empcode
        user.role = _effective_role

        # Commit name/email/role/username changes first
        db.session.commit()

        # Company scope, then region assignment — Business Heads and
        # Initiators both pick region(s) from the same checkbox set, but
        # only when "RDC" is ticked among their companies; HR Manager never
        # gets region-narrowing regardless of company. Clear any stale
        # scope/region rows if the role changed away from a scoped role, or
        # RDC was unticked.
        scope_changes = None
        region_changes = None
        if user.role in _COMPANY_SCOPED_ROLES:
            scope_changes = _set_company_scope(user, ticked_companies)
            if user.role == UserRole.BUSINESS_HEAD:
                region_changes = _set_bh_regions(
                    user, request.form.getlist("regions") if "RDC" in ticked_companies else [])
                if InitiatorRegion.query.filter_by(initiator_id=user.id).first():
                    _set_initiator_regions(user, [])
            elif user.role == UserRole.INITIATOR:
                region_changes = _set_initiator_regions(
                    user, request.form.getlist("regions") if "RDC" in ticked_companies else [])
                if BusinessHeadRegion.query.filter_by(business_head_id=user.id).first():
                    _set_bh_regions(user, [])
            else:  # HR_MANAGER: never gets regions, regardless of ticked companies
                if BusinessHeadRegion.query.filter_by(business_head_id=user.id).first():
                    _set_bh_regions(user, [])
                if InitiatorRegion.query.filter_by(initiator_id=user.id).first():
                    _set_initiator_regions(user, [])
        else:
            scope_changes = _set_company_scope(user, [])
            if BusinessHeadRegion.query.filter_by(business_head_id=user.id).first():
                _set_bh_regions(user, [])
            if InitiatorRegion.query.filter_by(initiator_id=user.id).first():
                _set_initiator_regions(user, [])

        db.session.commit()

        # Build granular before/after diff
        _changes = {}
        if _old_name != new_name:
            _changes["name"] = {"from": _old_name, "to": new_name}
        if _old_email != new_email:
            _changes["email"] = {"from": _old_email, "to": new_email}
        if (_old_username or "") != (new_username or ""):
            _changes["username"] = {"from": _old_username, "to": new_username}
        if role_val and _old_role != role_val:
            _changes["role"] = {"from": _old_role, "to": role_val}
        if (_old_empcode or "") != (new_empcode or ""):
            _changes["employee_code"] = {"from": _old_empcode, "to": new_empcode}

        # Log the edit
        if scope_changes:
            _changes["companies"] = scope_changes
        if region_changes:
            _changes["regions"] = region_changes
        log_audit("USER_MGMT", "USER_EDITED",
                  resource_type="User", resource_id=user.id,
                  resource_label=user.name,
                  detail={"changes": _changes})
        # Log role change separately for easy filtering
        if "role" in _changes:
            log_audit("USER_MGMT", "USER_ROLE_CHANGED",
                      resource_type="User", resource_id=user.id,
                      resource_label=user.name,
                      detail={"from_role": _changes["role"]["from"],
                               "to_role": _changes["role"]["to"]})
        db.session.commit()

        flash("User updated.", "success")
        return redirect(url_for("admin.users_list"))
    return render_template("admin/user_form.html", user=user, UserRole=UserRole,
                           clusters=clusters, current_region_ids=current_region_ids,
                           companies=COMPANY_CHOICES, current_companies=current_company_ids)


@admin_bp.route("/users/<int:user_id>/toggle-active", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def toggle_user_active(user_id):
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        flash("You cannot deactivate yourself.", "warning")
    else:
        user.is_active = not user.is_active
        _action_type = "USER_ENABLED" if user.is_active else "USER_DISABLED"
        log_audit("USER_MGMT", _action_type,
                  resource_type="User", resource_id=user.id,
                  resource_label=user.name,
                  detail={"target_user": user.name, "email": user.email,
                          "role": user.role.value,
                          "new_active_state": user.is_active})
        db.session.commit()
        flash(f"User {'activated' if user.is_active else 'deactivated'}.", "info")
    return redirect(url_for("admin.users_list"))


# ── Email Settings ────────────────────────────────────────────────────────────

def _smtp_for_email(email_addr):
    """Return (host, port) auto-detected from email domain."""
    domain = email_addr.split("@")[-1].lower() if "@" in email_addr else ""
    _MAP = {
        "gmail.com":       ("smtp.gmail.com",       587),
        "googlemail.com":  ("smtp.gmail.com",       587),
        "outlook.com":     ("smtp.office365.com",   587),
        "hotmail.com":     ("smtp.office365.com",   587),
        "live.com":        ("smtp.office365.com",   587),
        "msn.com":         ("smtp.office365.com",   587),
        "yahoo.com":       ("smtp.mail.yahoo.com",  587),
        "yahoo.co.in":     ("smtp.mail.yahoo.com",  587),
        "yahoo.co.uk":     ("smtp.mail.yahoo.com",  587),
        "zoho.com":        ("smtp.zoho.com",        587),
        "zohomail.com":    ("smtp.zoho.com",        587),
        "sendgrid.net":    ("smtp.sendgrid.net",    587),
    }
    # Default to Google/Workspace (covers custom domains using Google Workspace)
    return _MAP.get(domain, ("smtp.gmail.com", 587))


@admin_bp.route("/settings/email", methods=["GET", "POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def email_settings():
    from ..models import SystemConfig
    from ..utils import get_db_mail_config, _send_smtp

    def _get(key):
        row = SystemConfig.query.filter_by(key=key).first()
        return (row.value or "").strip() if row else ""

    def _set(key, value):
        row = SystemConfig.query.filter_by(key=key).first()
        if row:
            row.value = value
        else:
            db.session.add(SystemConfig(key=key, value=value))

    if request.method == "POST":
        user     = request.form.get("email_user", "").strip()
        new_pass = request.form.get("email_pass", "").strip()

        if not user:
            flash("Email address is required.", "danger")
            return redirect(url_for("admin.email_settings"))

        # Resolve password — use submitted value or fall back to stored one
        password = new_pass or _get("email_pass") or current_app.config.get("MAIL_PASSWORD") or ""
        if not password:
            flash("App password is required.", "danger")
            return redirect(url_for("admin.email_settings"))

        host, port = _smtp_for_email(user)

        # Test connection before saving
        test_cfg = {
            "server":   host,
            "port":     port,
            "username": user,
            "password": password,
            "sender":   user,
        }
        try:
            _send_smtp(test_cfg, [user],
                       "Connection Test — RDC Teamlease Portal",
                       "Connection verified. Your email settings are working correctly.")
        except Exception as exc:
            flash(f"Connection test failed — {exc}", "danger")
            return redirect(url_for("admin.email_settings"))

        # Connection OK → persist
        _set("email_host", host)
        _set("email_port", str(port))
        _set("email_user", user)
        _set("email_from", user)
        if new_pass:
            _set("email_pass", new_pass)

        log_audit("USER_MGMT", "EMAIL_SETTINGS_UPDATED",
                  detail={"host": host, "port": port, "user": user})
        db.session.commit()

        flash(f"Email settings saved. A confirmation email was sent to {user}.", "success")
        return redirect(url_for("admin.email_settings"))

    cfg_now = get_db_mail_config()
    return render_template("admin/email_settings.html",
        cfg={
            "email_user":   _get("email_user") or current_app.config.get("MAIL_USERNAME") or "",
            "has_password": bool(_get("email_pass") or current_app.config.get("MAIL_PASSWORD")),
            "is_active":    bool(cfg_now["username"]),
        },
    )


# ── All Requests ───────────────────────────────────────────────────────────────

# Statuses grouped by lifecycle stage
_ONGOING_STATUSES = {
    RequestStatus.PENDING_BH,
    RequestStatus.PENDING_DR_BHOON,
    RequestStatus.PENDING_HR_MANAGER,
    RequestStatus.PENDING_HEAD_HR,
}
_COMPLETED_STATUSES = {
    RequestStatus.ACTIVE,
    RequestStatus.REJECTED_BH,
    RequestStatus.REJECTED_DR_BHOON,
    RequestStatus.REJECTED_HRM,
    RequestStatus.REJECTED_HEAD_HR,
}


@admin_bp.route("/requests")
@login_required
def requests_list():
    _admin_or_hr()
    status_filter  = request.args.get("status")
    company_filter = request.args.get("company")
    show_deleted   = request.args.get("show_deleted") == "1"

    companies = [c[0] for c in
                 db.session.query(OnboardingRequest.company_code)
                 .filter(OnboardingRequest.is_deleted == False)
                 .distinct().all() if c[0]]

    # ── Filtered view (single-table, status explicitly chosen) ────────────────
    if status_filter:
        query = OnboardingRequest.query
        if not show_deleted:
            query = query.filter_by(is_deleted=False)
        try:
            query = query.filter_by(status=RequestStatus(status_filter))
        except ValueError:
            pass
        if company_filter:
            query = query.filter_by(company_code=company_filter)
        page = request.args.get("page", 1, type=int)
        pagination = query.order_by(OnboardingRequest.updated_at.desc()).paginate(
            page=page, per_page=_per_page(25), error_out=False
        )
        return render_template(
            "admin/requests.html",
            two_pane=False,
            requests=pagination.items,
            pagination=pagination,
            pg_base=_pg_base(),
            RequestStatus=RequestStatus,
            statuses=list(RequestStatus),
            companies=companies,
            selected_status=status_filter,
            selected_company=company_filter,
            show_deleted=show_deleted,
        )

    # ── Two-pane view (default) ───────────────────────────────────────────────
    base = OnboardingRequest.query
    if not show_deleted:
        base = base.filter_by(is_deleted=False)
    if company_filter:
        base = base.filter_by(company_code=company_filter)

    # Ongoing pane — independent pagination (?page_o=N)
    page_o = request.args.get("page_o", 1, type=int)
    ongoing_q = base.filter(
        OnboardingRequest.status.in_(_ONGOING_STATUSES)
    ).order_by(OnboardingRequest.updated_at.desc())
    ongoing_pg = ongoing_q.paginate(
        page=page_o, per_page=_per_page(25), error_out=False
    )

    # Completed pane — independent pagination (?page_c=N)
    page_c = request.args.get("page_c", 1, type=int)
    completed_q = base.filter(
        OnboardingRequest.status.in_(_COMPLETED_STATUSES)
    ).order_by(OnboardingRequest.updated_at.desc())
    completed_pg = completed_q.paginate(
        page=page_c, per_page=_per_page(25), error_out=False
    )

    # Build independent pg_base URLs for each pane
    def _pg_base_pane(strip_key):
        args = {k: v for k, v in request.args.items()
                if k not in (strip_key, "per_page") and v}
        return request.path + ("?" + urlencode(args) + "&" if args else "?")

    return render_template(
        "admin/requests.html",
        two_pane=True,
        # ongoing
        ongoing=ongoing_pg.items,
        ongoing_pg=ongoing_pg,
        pg_base_o=_pg_base_pane("page_o"),
        # completed
        completed=completed_pg.items,
        completed_pg=completed_pg,
        pg_base_c=_pg_base_pane("page_c"),
        # shared
        RequestStatus=RequestStatus,
        statuses=list(RequestStatus),
        companies=companies,
        selected_status=status_filter,
        selected_company=company_filter,
        show_deleted=show_deleted,
        # keep these so existing template references don't break
        requests=[],
        pagination=None,
        pg_base=_pg_base(),
    )


# ── Audit Log ──────────────────────────────────────────────────────────────────

@admin_bp.route("/audit-log")
@login_required
@role_required(UserRole.SUPER_ADMIN)
def audit_log():
    actor_filter    = request.args.get("actor_id",  type=int)
    category_filter = request.args.get("category",  "").strip()
    action_filter   = request.args.get("action",    "").strip()
    date_from_s     = request.args.get("date_from", "").strip()
    date_to_s       = request.args.get("date_to",   "").strip()
    page            = request.args.get("page", 1, type=int)

    query = AuditLog.query
    if actor_filter:
        query = query.filter_by(actor_id=actor_filter)
    if category_filter:
        try:
            query = query.filter_by(action_category=AuditCategory(category_filter))
        except ValueError:
            pass
    if action_filter:
        query = query.filter(AuditLog.action_type.ilike(f"%{action_filter}%"))
    if date_from_s:
        try:
            query = query.filter(
                AuditLog.created_at >= datetime.strptime(date_from_s, "%Y-%m-%d")
            )
        except ValueError:
            pass
    if date_to_s:
        try:
            query = query.filter(
                AuditLog.created_at < datetime.strptime(date_to_s, "%Y-%m-%d") + timedelta(days=1)
            )
        except ValueError:
            pass

    pagination = query.order_by(AuditLog.created_at.desc()).paginate(
        page=page, per_page=_per_page(50), error_out=False
    )
    actors     = User.query.order_by(User.name).all()
    categories = list(AuditCategory)

    return render_template(
        "admin/audit_log.html",
        pagination=pagination,
        entries=pagination.items,
        pg_base=_pg_base(),
        actors=actors,
        categories=categories,
        AuditCategory=AuditCategory,
        selected_actor=actor_filter,
        selected_category=category_filter,
        selected_action=action_filter,
        date_from=date_from_s,
        date_to=date_to_s,
    )


# ── Plant Locations ────────────────────────────────────────────────────────────

@admin_bp.route("/plants")
@login_required
@role_required(UserRole.SUPER_ADMIN)
def plants_list():
    # Full unpaginated list, same as designations/users/mappings — lets the
    # instant client-side search box (render_list_search()) find a match
    # anywhere in the list instead of only on whatever page happened to be
    # loaded. ~200 rows is trivial to render in one go for an admin table.
    # Company (added 2026-09-15) is a separate filter dimension from the
    # existing Active/Disabled tabs — one company shown at a time, default
    # RDC (the original, most heavily-used company).
    company = request.args.get("company", "RDC")
    if company not in COMPANY_CHOICES:
        company = "RDC"
    base = PlantLocation.query.filter_by(is_deleted=False, company=company).order_by(
        PlantLocation.sort_order, PlantLocation.name
    )
    active_plants   = base.filter_by(is_active=True ).all()
    inactive_plants = base.filter_by(is_active=False).all()
    return render_template("admin/plants.html",
                           active_plants=active_plants, inactive_plants=inactive_plants,
                           companies=COMPANY_CHOICES, selected_company=company)


@admin_bp.route("/plants/new", methods=["GET", "POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def new_plant():
    default_company = request.args.get("company", "RDC")
    if default_company not in COMPANY_CHOICES:
        default_company = "RDC"
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        company = request.form.get("company", "RDC")
        if company not in COMPANY_CHOICES:
            company = "RDC"
        if not name:
            flash("Plant name is required.", "danger")
        else:
            max_order = db.session.query(db.func.max(PlantLocation.sort_order)).scalar() or 0
            plant = PlantLocation(name=name, company=company, sort_order=max_order + 1)
            db.session.add(plant)
            db.session.flush()
            log_audit("ADMIN_PLANT", "PLANT_CREATED",
                      resource_type="PlantLocation", resource_id=plant.id,
                      resource_label=f"Plant: {name}",
                      detail={"name": name, "company": company})
            db.session.commit()
            flash(f"Plant '{name}' added.", "success")
            return redirect(url_for("admin.plants_list", company=company))
    return render_template("admin/plant_form.html", plant=None,
                           companies=COMPANY_CHOICES, default_company=default_company)


@admin_bp.route("/plants/<int:plant_id>/edit", methods=["GET", "POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def edit_plant(plant_id):
    plant = db.get_or_404(PlantLocation, plant_id)
    if request.method == "POST":
        _old_name = plant.name
        _old_company = plant.company
        new_name = request.form.get("name", "").strip()
        new_company = request.form.get("company", plant.company)
        if new_company not in COMPANY_CHOICES:
            new_company = plant.company
        if not new_name:
            flash("Plant name is required.", "danger")
            return render_template("admin/plant_form.html", plant=plant, companies=COMPANY_CHOICES)
        plant.name = new_name
        plant.company = new_company
        log_audit("ADMIN_PLANT", "PLANT_EDITED",
                  resource_type="PlantLocation", resource_id=plant.id,
                  resource_label=f"Plant: {plant.name}",
                  detail={"old_name": _old_name, "new_name": plant.name,
                          "old_company": _old_company, "new_company": new_company})
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            flash("Database error. Please try again.", "danger")
            return render_template("admin/plant_form.html", plant=plant, companies=COMPANY_CHOICES)
        flash("Plant updated.", "success")
        return redirect(url_for("admin.plants_list", company=new_company))
    return render_template("admin/plant_form.html", plant=plant, companies=COMPANY_CHOICES)


@admin_bp.route("/plants/<int:plant_id>/toggle", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def toggle_plant(plant_id):
    plant = db.get_or_404(PlantLocation, plant_id)
    plant.is_active = not plant.is_active
    _pat = "PLANT_ENABLED" if plant.is_active else "PLANT_DISABLED"
    log_audit("ADMIN_PLANT", _pat,
              resource_type="PlantLocation", resource_id=plant.id,
              resource_label=f"Plant: {plant.name}",
              detail={"name": plant.name, "new_active_state": plant.is_active})
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("Database error. Please try again.", "danger")
        return redirect(url_for("admin.plants_list", company=plant.company))
    flash(f"Plant {'activated' if plant.is_active else 'deactivated'}.", "info")
    return redirect(url_for("admin.plants_list", company=plant.company))


@admin_bp.route("/plants/<int:plant_id>/delete", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def delete_plant(plant_id):
    plant = db.get_or_404(PlantLocation, plant_id)
    _plant_name = plant.name
    _plant_company = plant.company
    plant.is_deleted = True
    plant.is_active = False
    log_audit("ADMIN_PLANT", "PLANT_DELETED",
              resource_type="PlantLocation", resource_id=plant.id,
              resource_label=f"Plant: {_plant_name}",
              detail={"name": _plant_name})
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("Database error. Please try again.", "danger")
        return redirect(url_for("admin.plants_list", company=_plant_company))
    flash("Plant removed.", "info")
    return redirect(url_for("admin.plants_list", company=_plant_company))


# ── Designations ───────────────────────────────────────────────────────────────

@admin_bp.route("/designations")
@login_required
@role_required(UserRole.SUPER_ADMIN)
def designations_list():
    base = Designation.query.filter_by(is_deleted=False).order_by(
        Designation.sort_order, Designation.name
    )
    active_desigs   = base.filter_by(is_active=True ).all()
    inactive_desigs = base.filter_by(is_active=False).all()
    return render_template("admin/designations.html",
                           active_desigs=active_desigs,
                           inactive_desigs=inactive_desigs)


@admin_bp.route("/designations/new", methods=["GET", "POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def new_designation():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        days = request.form.get("notice_period_days", "30").strip()
        truein_app_att = bool(request.form.get("truein_app_attendance"))
        norm_category_id = request.form.get("norm_category_id", type=int) or None
        if not name:
            flash("Designation name is required.", "danger")
        else:
            try:
                notice_days = int(days)
            except (ValueError, TypeError):
                notice_days = 30
            max_order = db.session.query(db.func.max(Designation.sort_order)).scalar() or 0
            desig = Designation(name=name, notice_period_days=notice_days,
                                truein_app_attendance=truein_app_att,
                                norm_category_id=norm_category_id,
                                sort_order=max_order + 1)
            db.session.add(desig)
            db.session.flush()
            log_audit("ADMIN_DESIG", "DESIGNATION_CREATED",
                      resource_type="Designation", resource_id=desig.id,
                      resource_label=f"Designation: {name}",
                      detail={"name": name, "notice_period_days": notice_days,
                              "truein_app_attendance": truein_app_att,
                              "norm_category_id": norm_category_id})
            try:
                db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()
                flash("Database error. Please try again.", "danger")
                return redirect(url_for("admin.designations_list"))
            flash(f"Designation '{name}' added.", "success")
            return redirect(url_for("admin.designations_list"))
    norm_categories = NormRoleCategory.query.filter_by(is_active=True, is_deleted=False).order_by(
        NormRoleCategory.scope, NormRoleCategory.sort_order).all()
    return render_template("admin/designation_form.html", designation=None, norm_categories=norm_categories)


@admin_bp.route("/designations/<int:desig_id>/edit", methods=["GET", "POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def edit_designation(desig_id):
    desig = db.get_or_404(Designation, desig_id)
    if request.method == "POST":
        _old_name   = desig.name
        _old_days   = desig.notice_period_days
        _old_att    = desig.truein_app_attendance
        _old_norm_cat = desig.norm_category_id
        desig.name  = request.form.get("name", desig.name).strip()
        try:
            desig.notice_period_days = int(request.form.get("notice_period_days", desig.notice_period_days) or 30)
        except (ValueError, TypeError):
            desig.notice_period_days = 30
        desig.truein_app_attendance = bool(request.form.get("truein_app_attendance"))
        desig.norm_category_id = request.form.get("norm_category_id", type=int) or None
        _changes = {}
        if _old_name != desig.name:
            _changes["name"] = {"from": _old_name, "to": desig.name}
        if _old_days != desig.notice_period_days:
            _changes["notice_period_days"] = {"from": _old_days, "to": desig.notice_period_days}
        if _old_att != desig.truein_app_attendance:
            _changes["truein_app_attendance"] = {"from": _old_att, "to": desig.truein_app_attendance}
        if _old_norm_cat != desig.norm_category_id:
            _changes["norm_category_id"] = {"from": _old_norm_cat, "to": desig.norm_category_id}
        log_audit("ADMIN_DESIG", "DESIGNATION_EDITED",
                  resource_type="Designation", resource_id=desig.id,
                  resource_label=f"Designation: {desig.name}",
                  detail={"changes": _changes})
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            flash("Database error. Please try again.", "danger")
            return redirect(url_for("admin.designations_list"))
        flash("Designation updated.", "success")
        return redirect(url_for("admin.designations_list"))
    norm_categories = NormRoleCategory.query.filter_by(is_active=True, is_deleted=False).order_by(
        NormRoleCategory.scope, NormRoleCategory.sort_order).all()
    return render_template("admin/designation_form.html", designation=desig, norm_categories=norm_categories)


@admin_bp.route("/designations/<int:desig_id>/toggle", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def toggle_designation(desig_id):
    desig = db.get_or_404(Designation, desig_id)
    desig.is_active = not desig.is_active
    _dat = "DESIGNATION_ENABLED" if desig.is_active else "DESIGNATION_DISABLED"
    log_audit("ADMIN_DESIG", _dat,
              resource_type="Designation", resource_id=desig.id,
              resource_label=f"Designation: {desig.name}",
              detail={"name": desig.name, "new_active_state": desig.is_active})
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("Database error. Please try again.", "danger")
        return redirect(url_for("admin.designations_list"))
    flash(f"Designation {'activated' if desig.is_active else 'deactivated'}.", "info")
    return redirect(url_for("admin.designations_list"))


@admin_bp.route("/designations/<int:desig_id>/delete", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def delete_designation(desig_id):
    desig = db.get_or_404(Designation, desig_id)
    _desig_name = desig.name
    desig.is_deleted = True
    desig.is_active = False
    log_audit("ADMIN_DESIG", "DESIGNATION_DELETED",
              resource_type="Designation", resource_id=desig.id,
              resource_label=f"Designation: {_desig_name}",
              detail={"name": _desig_name, "notice_period_days": desig.notice_period_days})
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("Database error. Please try again.", "danger")
        return redirect(url_for("admin.designations_list"))
    flash("Designation removed.", "info")
    return redirect(url_for("admin.designations_list"))


# AJAX: get notice period for a designation
@admin_bp.route("/api/designation-notice-period/<int:desig_id>")
@login_required
def designation_notice_period(desig_id):
    desig = db.session.get(Designation, desig_id)
    if not desig:
        return jsonify({"days": 0, "label": "—"})
    label = f"{desig.notice_period_days} day{'s' if desig.notice_period_days != 1 else ''}"
    return jsonify({"days": desig.notice_period_days, "label": label})


# ── Form Fields ────────────────────────────────────────────────────────────────

@admin_bp.route("/form-fields")
@login_required
@role_required(UserRole.SUPER_ADMIN)
def form_fields_list():
    fields = FormField.query.filter_by(is_deleted=False).order_by(FormField.step, FormField.sort_order).all()
    return render_template(
        "admin/form_fields.html", fields=fields,
        FieldType=FieldType, OptionsSource=OptionsSource,
    )


@admin_bp.route("/form-fields/new", methods=["GET", "POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def new_form_field():
    if request.method == "POST":
        key = request.form.get("field_key", "").strip().lower().replace(" ", "_")
        label = request.form.get("field_label", "").strip()
        ftype = request.form.get("field_type", "text")
        try:
            step = int(request.form.get("step", 1))
        except (ValueError, TypeError):
            step = 1
        is_required = bool(request.form.get("is_required"))
        allow_other = bool(request.form.get("allow_other"))
        options_source = request.form.get("options_source", "inline")
        placeholder = request.form.get("placeholder", "").strip()
        help_text = request.form.get("help_text", "").strip()

        input_pattern = request.form.get("input_pattern", "").strip() or None
        min_len_raw = request.form.get("min_length", "").strip()
        max_len_raw = request.form.get("max_length", "").strip()
        min_length = int(min_len_raw) if min_len_raw.isdigit() else None
        max_length = int(max_len_raw) if max_len_raw.isdigit() else None

        if not key or not label:
            flash("Key and label are required.", "danger")
        elif FormField.query.filter_by(field_key=key, is_deleted=False).first():
            flash(f"Field key '{key}' already exists.", "danger")
        else:
            max_order = db.session.query(db.func.max(FormField.sort_order)).filter(
                FormField.step == step).scalar() or 0
            field = FormField(
                field_key=key, field_label=label,
                field_type=FieldType(ftype), step=step,
                is_required=is_required, allow_other=allow_other,
                options_source=OptionsSource(options_source),
                placeholder=placeholder or None, help_text=help_text or None,
                sort_order=max_order + 1,
                input_pattern=input_pattern,
                min_length=min_length, max_length=max_length,
            )
            db.session.add(field)
            db.session.flush()

            # Inline options
            opt_labels = request.form.getlist("opt_label")
            opt_values = request.form.getlist("opt_value")
            _option_names = []
            for i, (ol, ov) in enumerate(zip(opt_labels, opt_values)):
                if ol.strip():
                    db.session.add(FormFieldOption(
                        field_id=field.id,
                        option_label=ol.strip(),
                        option_value=ov.strip() or ol.strip(),
                        sort_order=i,
                    ))
                    _option_names.append(ol.strip())
            log_audit("ADMIN_FIELD", "FORM_FIELD_CREATED",
                      resource_type="FormField", resource_id=field.id,
                      resource_label=f"Field: {label} ({key})",
                      detail={"field_key": key, "field_label": label,
                              "field_type": ftype, "step": step,
                              "is_required": is_required,
                              "options_source": options_source,
                              "inline_option_count": len(_option_names),
                              "inline_options": _option_names[:20]})
            db.session.commit()
            flash(f"Field '{label}' created.", "success")
            return redirect(url_for("admin.form_fields_list"))

    return render_template(
        "admin/field_form.html", field=None,
        FieldType=FieldType, OptionsSource=OptionsSource,
    )


@admin_bp.route("/form-fields/<int:field_id>/edit", methods=["GET", "POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def edit_form_field(field_id):
    field = db.get_or_404(FormField, field_id)
    if request.method == "POST":
        # Capture before-state for diff
        _old_label   = field.field_label
        _old_type    = field.field_type.value
        _old_step    = field.step
        _old_req     = field.is_required
        _old_pattern = field.input_pattern
        _old_min     = field.min_length
        _old_max     = field.max_length

        field.field_label = request.form.get("field_label", field.field_label).strip()
        field.field_type = FieldType(request.form.get("field_type", field.field_type.value))
        try:
            field.step = int(request.form.get("step", field.step))
        except (ValueError, TypeError):
            pass  # keep existing value
        field.is_required = bool(request.form.get("is_required"))
        field.allow_other = bool(request.form.get("allow_other"))
        field.options_source = OptionsSource(request.form.get("options_source", field.options_source.value))
        field.placeholder = request.form.get("placeholder", "").strip() or None
        field.help_text = request.form.get("help_text", "").strip() or None
        field.input_pattern = request.form.get("input_pattern", "").strip() or None
        min_len_raw = request.form.get("min_length", "").strip()
        max_len_raw = request.form.get("max_length", "").strip()
        field.min_length = int(min_len_raw) if min_len_raw.isdigit() else None
        field.max_length = int(max_len_raw) if max_len_raw.isdigit() else None

        # Rebuild inline options
        _new_option_names = []
        if field.options_source == OptionsSource.INLINE:
            FormFieldOption.query.filter_by(field_id=field.id).delete()
            opt_labels = request.form.getlist("opt_label")
            opt_values = request.form.getlist("opt_value")
            for i, (ol, ov) in enumerate(zip(opt_labels, opt_values)):
                if ol.strip():
                    db.session.add(FormFieldOption(
                        field_id=field.id,
                        option_label=ol.strip(),
                        option_value=ov.strip() or ol.strip(),
                        sort_order=i,
                    ))
                    _new_option_names.append(ol.strip())

        # Build diff
        _changes = {}
        if _old_label != field.field_label:
            _changes["field_label"] = {"from": _old_label, "to": field.field_label}
        if _old_type != field.field_type.value:
            _changes["field_type"] = {"from": _old_type, "to": field.field_type.value}
        if _old_step != field.step:
            _changes["step"] = {"from": _old_step, "to": field.step}
        if _old_req != field.is_required:
            _changes["is_required"] = {"from": _old_req, "to": field.is_required}
        if _old_pattern != field.input_pattern:
            _changes["input_pattern"] = {"from": _old_pattern, "to": field.input_pattern}
        if _old_min != field.min_length:
            _changes["min_length"] = {"from": _old_min, "to": field.min_length}
        if _old_max != field.max_length:
            _changes["max_length"] = {"from": _old_max, "to": field.max_length}

        log_audit("ADMIN_FIELD", "FORM_FIELD_EDITED",
                  resource_type="FormField", resource_id=field.id,
                  resource_label=f"Field: {field.field_label} ({field.field_key})",
                  detail={"field_key": field.field_key, "changes": _changes})
        if _new_option_names:
            log_audit("ADMIN_FIELD", "FORM_FIELD_OPTIONS_UPDATED",
                      resource_type="FormField", resource_id=field.id,
                      resource_label=f"Field: {field.field_label} ({field.field_key})",
                      detail={"field_key": field.field_key,
                              "option_count": len(_new_option_names),
                              "options": _new_option_names[:20]})
        db.session.commit()
        flash("Field updated.", "success")
        return redirect(url_for("admin.form_fields_list"))

    return render_template(
        "admin/field_form.html", field=field,
        FieldType=FieldType, OptionsSource=OptionsSource,
    )


@admin_bp.route("/form-fields/<int:field_id>/toggle", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def toggle_form_field(field_id):
    field = db.get_or_404(FormField, field_id)
    field.is_active = not field.is_active
    _fat = "FORM_FIELD_ENABLED" if field.is_active else "FORM_FIELD_DISABLED"
    log_audit("ADMIN_FIELD", _fat,
              resource_type="FormField", resource_id=field.id,
              resource_label=f"Field: {field.field_label} ({field.field_key})",
              detail={"field_key": field.field_key, "field_label": field.field_label,
                      "new_active_state": field.is_active})
    db.session.commit()
    flash(f"Field '{field.field_label}' {'enabled' if field.is_active else 'disabled'}.", "info")
    return redirect(url_for("admin.form_fields_list"))


@admin_bp.route("/form-fields/<int:field_id>/delete", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def delete_form_field(field_id):
    field = db.get_or_404(FormField, field_id)
    _fkey   = field.field_key
    _flabel = field.field_label
    field.is_deleted = True
    field.is_active = False
    log_audit("ADMIN_FIELD", "FORM_FIELD_DELETED",
              resource_type="FormField", resource_id=field.id,
              resource_label=f"Field: {_flabel} ({_fkey})",
              detail={"field_key": _fkey, "field_label": _flabel,
                      "field_type": field.field_type.value, "step": field.step})
    db.session.commit()
    flash("Field removed.", "info")
    return redirect(url_for("admin.form_fields_list"))


# ── Truein Dry-Run ─────────────────────────────────────────────────────────────

@admin_bp.route("/requests/<string:token>/truein-push", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN, UserRole.HEAD_HR)
def truein_push(token):
    """Manually push (or retry) an ACTIVE request to Truein."""
    from ..requests_bp.routes import _get_req_by_token
    from ..integrations.truein import push_employee
    from ..models import RequestStatus
    from datetime import datetime

    req = _get_req_by_token(token)
    if req.status != RequestStatus.ACTIVE:
        flash("Only ACTIVE requests can be pushed to Truein.", "warning")
        return redirect(url_for("admin.requests_list"))

    from ..integrations.truein import (
        push_employee, start_retry_thread, _write_push_log,
        _handle_dropped_fields, _notify_push_failed,
    )

    # Reset stop flag so background retries can run again
    req.truein_retry_stopped = False
    db.session.commit()

    _push_issue = False
    try:
        push_result = push_employee(req)
        if push_result["success"]:
            req.truein_pushed_at       = datetime.utcnow()
            req.truein_push_error      = None
            req.truein_last_attempt_at = datetime.utcnow()
            req.truein_retry_count     = (req.truein_retry_count or 0) + 1
            _write_push_log(db, req, push_result, triggered_by="manual")
            _handle_dropped_fields(db, req, push_result.get("dropped_fields", []), "manual")
            log_audit("REQUEST", "TRUEIN_PUSH_SUCCESS",
                      resource_type="OnboardingRequest", resource_id=req.id,
                      resource_label=f"Request #{req.id} — {req.candidate_name}",
                      detail={"empId": push_result["empId"],
                              "attempt": req.truein_retry_count,
                              "message": push_result["message"],
                              "manual_retry": True,
                              "dropped_fields": push_result.get("dropped_fields", [])})
            db.session.commit()
            _dropped = push_result.get("dropped_fields", [])
            if _dropped:
                _push_issue = True
                flash(f"Pushed to Truein (empId: {push_result['empId']}), but {len(_dropped)} field(s) "
                      f"were skipped — HR Manager notified to complete them in Truein.", "warning")
            else:
                flash(f"Pushed to Truein successfully (empId: {push_result['empId']}).", "success")
        else:
            req.truein_push_error      = push_result["message"]
            req.truein_last_attempt_at = datetime.utcnow()
            req.truein_retry_count     = (req.truein_retry_count or 0) + 1
            _retryable = push_result.get("retryable", True)
            _write_push_log(db, req, push_result, triggered_by="manual")
            _notify_push_failed(db, req, push_result["message"], triggered_by="manual", will_retry=_retryable)
            if not _retryable:
                req.truein_retry_stopped = True
            db.session.commit()
            _push_issue = True
            if _retryable:
                start_retry_thread(current_app._get_current_object(), req.id)
                flash(f"Push FAILED: {push_result['message']}. HR Manager, Head HR and Admin "
                      f"have been notified. Background retry started.", "danger")
            else:
                flash(f"Push FAILED: {push_result['message']}. This is a data collision with an "
                      f"existing Truein employee, so it will NOT be retried automatically. "
                      f"HR Manager, Head HR and Admin have been notified.", "danger")
    except Exception as exc:
        _exc_result = {
            "success": False, "message": str(exc),
            "empId": None, "http_status": None,
            "raw_response": {}, "payload_sent": {},
        }
        req.truein_push_error      = str(exc)
        req.truein_last_attempt_at = datetime.utcnow()
        req.truein_retry_count     = (req.truein_retry_count or 0) + 1
        _write_push_log(db, req, _exc_result, triggered_by="manual")
        try:
            _notify_push_failed(db, req, str(exc), triggered_by="manual")
        except Exception:
            pass
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        start_retry_thread(current_app._get_current_object(), req.id)
        _push_issue = True
        flash(f"Push error: {exc}. HR Manager, Head HR and Admin have been notified. "
              f"Background retry started.", "danger")

    # On an issue, land on the request's own detail page so the field-level
    # popup (see detail.html) can show exactly what went wrong — a plain
    # list-page redirect buries that detail behind another click.
    if _push_issue:
        return redirect(url_for("requests_bp.view_request", token=req.public_token, push_issue=1))
    return redirect(url_for("admin.requests_list"))


@admin_bp.route("/requests/<string:token>/truein-stop-retry", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN, UserRole.HEAD_HR)
def truein_stop_retry(token):
    """Stop background retry for a request (e.g. bad data that will never succeed)."""
    from ..requests_bp.routes import _get_req_by_token
    req = _get_req_by_token(token)
    req.truein_retry_stopped = True
    log_audit("REQUEST", "TRUEIN_RETRY_STOPPED",
              resource_type="OnboardingRequest", resource_id=req.id,
              resource_label=f"Request #{req.id} — {req.candidate_name}",
              detail={"retry_count": req.truein_retry_count,
                      "last_error": req.truein_push_error})
    db.session.commit()
    flash(f"Truein retries stopped for {req.candidate_name}. Fix the data then use Push to retry.", "info")
    return redirect(url_for("admin.requests_list"))


@admin_bp.route("/requests/<string:token>/truein-logs")
@login_required
@role_required(UserRole.SUPER_ADMIN, UserRole.HEAD_HR)
def truein_logs(token):
    """Show full push-attempt history for a request."""
    from ..requests_bp.routes import _get_req_by_token
    from ..models import TrueinPushLog
    req = _get_req_by_token(token)
    logs = (TrueinPushLog.query
            .filter_by(request_id=req.id)
            .order_by(TrueinPushLog.attempted_at.desc())
            .all())
    return render_template("admin/truein_logs.html", req=req, logs=logs)


@admin_bp.route("/requests/<string:token>/truein-dryrun", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN, UserRole.HEAD_HR)
def truein_dryrun(token):
    """
    Fetches a real Truein access token, builds the employee payload from the
    request's live data, writes both to a timestamped JSON file, then renders
    a result page.  The addEmployeeDtls endpoint is NEVER called.
    """
    from ..requests_bp.routes import _get_req_by_token
    from ..integrations.truein import dry_run_to_file

    req = _get_req_by_token(token)
    output_dir = os.path.abspath(
        os.path.join(current_app.root_path, "..", "truein_dryruns")
    )
    try:
        filepath, result = dry_run_to_file(req, output_dir)
    except Exception as exc:
        flash(f"Truein dry-run failed: {exc}", "danger")
        return redirect(url_for("admin.requests_list"))

    return render_template(
        "admin/truein_dryrun.html",
        result=result,
        filepath=filepath,
        request_obj=req,
    )


@admin_bp.route("/form-fields/reorder", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def reorder_form_fields():
    """AJAX: receives {ids: [id1, id2, ...]} and updates sort_order."""
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("ids", [])
    ids = []
    for fid in raw_ids:
        try:
            ids.append(int(fid))
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "Invalid field id."}), 400
    for i, fid in enumerate(ids):
        FormField.query.filter_by(id=fid).update({"sort_order": i})
    log_audit("ADMIN_FIELD", "FORM_FIELDS_REORDERED",
              detail={"field_count": len(ids), "new_order": ids})
    db.session.commit()
    return jsonify({"ok": True})


# ── Plant <-> Daily Volume Tracker mappings ─────────────────────────────────────
# Reconciles our plant names (form's Truein-sourced list / PlantLocation
# fallback) against the Daily Volume Tracker's plant_code, so the RDC
# staffing gate can look up a plant's last-month production. Rows are
# auto-populated by app.services.matching.auto_match_plants(); anything left
# 'unmatched' or 'auto_fuzzy' needs an admin's eyes.

_MATCH_CONFIDENCE_SORT = {
    MatchConfidence.UNMATCHED: 0, MatchConfidence.AUTO_FUZZY: 1,
    MatchConfidence.AUTO_EXACT: 2, MatchConfidence.MANUAL: 3,
}


@admin_bp.route("/plant-mappings")
@login_required
@role_required(UserRole.SUPER_ADMIN)
def plant_mappings_list():
    rows = PlantDvtMapping.query.filter_by(is_deleted=False).all()
    rows.sort(key=lambda r: (_MATCH_CONFIDENCE_SORT.get(r.match_confidence, 0), r.plant_location_name))
    return render_template("admin/plant_mappings.html", rows=rows)


@admin_bp.route("/plant-mappings/auto-match", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def run_plant_auto_match():
    from ..services import matching
    try:
        result = matching.auto_match_plants()
    except Exception as exc:
        flash(f"Auto-match failed: {exc}", "danger")
        return redirect(url_for("admin.plant_mappings_list"))
    log_audit("ADMIN_PLANT_MAPPING", "PLANT_MAPPING_AUTO_MATCH_RUN", detail=result)
    db.session.commit()
    flash(f"Auto-match done: {result['matched_exact']} exact match(es), "
          f"{result['unmatched']} unmatched (of {result['total']}).", "info")
    return redirect(url_for("admin.plant_mappings_list"))


@admin_bp.route("/plant-mappings/refresh-snapshot", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def refresh_staffing_snapshot():
    """
    Manual one-off trigger for the RDC headcount snapshot (normally runs on
    its own every 2 hours). Synchronous — blocks until done, which is fast
    if this process's Truein cache is already warm, slow (several minutes)
    if it needs a fresh paginated pull.
    """
    from ..services import snapshot_refresh
    try:
        result = snapshot_refresh.refresh_snapshot_now()
    except Exception as exc:
        flash(f"Snapshot refresh failed: {exc}", "danger")
        return redirect(url_for("admin.plant_mappings_list"))
    log_audit("ADMIN_PLANT_MAPPING", "STAFFING_SNAPSHOT_MANUAL_REFRESH", detail=result)
    db.session.commit()
    if result.get("skipped"):
        flash(f"Snapshot refresh skipped: {result['reason']}", "warning")
    else:
        flash(f"Snapshot refreshed: {result.get('snapshots_written', 0)} rows written, "
              f"{result.get('employee_rows_written', 0)} employees, "
              f"{len(result.get('warnings', []))} warning(s).", "info")
    return redirect(url_for("admin.plant_mappings_list"))


@admin_bp.route("/plant-mappings/<int:mapping_id>/edit", methods=["GET", "POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def edit_plant_mapping(mapping_id):
    from ..integrations import dvt, truein
    row = db.get_or_404(PlantDvtMapping, mapping_id)
    clusters = ClusterNameMapping.query.filter_by(is_deleted=False).order_by(ClusterNameMapping.canonical_cluster_name).all()
    try:
        dvt_plants = sorted(dvt.fetch_all_plants(), key=lambda p: p.get("plant_code") or "")
        dvt_error = None
    except Exception as exc:
        dvt_plants = []
        dvt_error = str(exc)
    # Truein sub_site list is cache-only — never blocks this request on a
    # live multi-minute paginated fetch. Cold cache just means manual entry.
    truein_sub_sites = truein.get_cached_sub_sites_if_warm()

    if request.method == "POST":
        _old = {"dvt_plant_code": row.dvt_plant_code, "cluster_id": row.cluster_id}
        selected_code = request.form.get("dvt_plant_code", "").strip() or None
        if selected_code:
            match = next((p for p in dvt_plants if p.get("plant_code") == selected_code), None)
            if match is None:
                flash("Could not confirm that plant against the Daily Volume Tracker. Please try again.", "danger")
                return render_template("admin/plant_mapping_form.html", row=row, clusters=clusters,
                                        dvt_plants=dvt_plants, dvt_error=dvt_error, truein_sub_sites=truein_sub_sites)
            row.dvt_plant_code = match.get("plant_code")
            row.dvt_daily_tracker_name = match.get("daily_tracker_name")
            row.dvt_erp_name = match.get("erp_name")
        else:
            row.dvt_plant_code = None
            row.dvt_daily_tracker_name = None
            row.dvt_erp_name = None
        row.truein_sub_site = request.form.get("truein_sub_site", "").strip() or None
        row.cluster_id = request.form.get("cluster_id", type=int) or None
        row.match_confidence = MatchConfidence.MANUAL
        row.updated_by_id = current_user.id
        log_audit("ADMIN_PLANT_MAPPING", "PLANT_MAPPING_EDITED",
                  resource_type="PlantDvtMapping", resource_id=row.id,
                  resource_label=f"Plant mapping: {row.plant_location_name}",
                  detail={"from": _old, "to": {"dvt_plant_code": row.dvt_plant_code, "cluster_id": row.cluster_id}})
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            flash("Database error. Please try again.", "danger")
            return redirect(url_for("admin.plant_mappings_list"))
        flash("Plant mapping updated.", "success")
        return redirect(url_for("admin.plant_mappings_list"))
    return render_template("admin/plant_mapping_form.html", row=row, clusters=clusters,
                            dvt_plants=dvt_plants, dvt_error=dvt_error, truein_sub_sites=truein_sub_sites)


# ── Cluster name mappings ────────────────────────────────────────────────────────
# Reconciles DVT's plant `region`, ZingHR's City, and Truein's category into
# one canonical cluster name, used for the cluster-level staffing norms
# (Accounts, Credit Control, CDS, ...) and for the headcount snapshot's
# cluster-scope grouping.

@admin_bp.route("/cluster-mappings")
@login_required
@role_required(UserRole.SUPER_ADMIN)
def cluster_mappings_list():
    rows = ClusterNameMapping.query.filter_by(is_deleted=False).all()
    rows.sort(key=lambda r: (_MATCH_CONFIDENCE_SORT.get(r.match_confidence, 0), r.canonical_cluster_name))
    return render_template("admin/cluster_mappings.html", rows=rows)


@admin_bp.route("/cluster-mappings/auto-match", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def run_cluster_auto_match():
    from ..services import matching
    try:
        result = matching.auto_match_clusters()
    except Exception as exc:
        flash(f"Auto-match failed: {exc}", "danger")
        return redirect(url_for("admin.cluster_mappings_list"))
    log_audit("ADMIN_CLUSTER_MAPPING", "CLUSTER_MAPPING_AUTO_MATCH_RUN", detail=result)
    db.session.commit()
    flash(f"Auto-match done: {result['matched_exact']} exact, {result['matched_fuzzy']} fuzzy, "
          f"{result['unmatched']} unmatched (of {result['total']} DVT regions).", "info")
    return redirect(url_for("admin.cluster_mappings_list"))


@admin_bp.route("/cluster-mappings/<int:mapping_id>/edit", methods=["GET", "POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def edit_cluster_mapping(mapping_id):
    row = db.get_or_404(ClusterNameMapping, mapping_id)
    if request.method == "POST":
        _old = {"dvt_region": row.dvt_region, "zinghr_city": row.zinghr_city, "truein_category": row.truein_category}
        row.canonical_cluster_name = request.form.get("canonical_cluster_name", row.canonical_cluster_name).strip()
        row.dvt_region = request.form.get("dvt_region", "").strip() or None
        row.zinghr_city = request.form.get("zinghr_city", "").strip() or None
        row.truein_category = request.form.get("truein_category", "").strip() or None
        row.match_confidence = MatchConfidence.MANUAL
        log_audit("ADMIN_CLUSTER_MAPPING", "CLUSTER_MAPPING_EDITED",
                  resource_type="ClusterNameMapping", resource_id=row.id,
                  resource_label=f"Cluster mapping: {row.canonical_cluster_name}",
                  detail={"from": _old, "to": {"dvt_region": row.dvt_region, "zinghr_city": row.zinghr_city,
                                                "truein_category": row.truein_category}})
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            flash("Database error. Please try again.", "danger")
            return redirect(url_for("admin.cluster_mappings_list"))
        flash("Cluster mapping updated.", "success")
        return redirect(url_for("admin.cluster_mappings_list"))
    return render_template("admin/cluster_mapping_form.html", row=row)


@admin_bp.route("/cluster-mappings/new", methods=["GET", "POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def new_cluster_mapping():
    """Manual add — for a cluster that auto-match never discovered (e.g. no DVT plant in it yet)."""
    if request.method == "POST":
        name = request.form.get("canonical_cluster_name", "").strip()
        if not name:
            flash("Cluster name is required.", "danger")
            return render_template("admin/cluster_mapping_form.html", row=None)
        row = ClusterNameMapping(
            canonical_cluster_name=name,
            dvt_region=request.form.get("dvt_region", "").strip() or None,
            zinghr_city=request.form.get("zinghr_city", "").strip() or None,
            truein_category=request.form.get("truein_category", "").strip() or None,
            match_confidence=MatchConfidence.MANUAL,
        )
        db.session.add(row)
        db.session.flush()
        log_audit("ADMIN_CLUSTER_MAPPING", "CLUSTER_MAPPING_CREATED",
                  resource_type="ClusterNameMapping", resource_id=row.id,
                  resource_label=f"Cluster mapping: {name}", detail={"name": name})
        db.session.commit()
        flash(f"Cluster '{name}' added.", "success")
        return redirect(url_for("admin.cluster_mappings_list"))
    return render_template("admin/cluster_mapping_form.html", row=None)

