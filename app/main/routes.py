from urllib.parse import urlencode
from flask import render_template, redirect, url_for, jsonify, request, abort, flash

# ── Pagination helpers ─────────────────────────────────────────────────────────
_PP_CHOICES = frozenset({10, 25, 50, 100})

def _per_page(default: int) -> int:
    v = request.args.get("per_page", default, type=int)
    return v if v in _PP_CHOICES else default

def _pg_base() -> str:
    args = {k: v for k, v in request.args.items() if k not in ("page", "per_page") and v}
    return request.path + ("?" + urlencode(args) + "&" if args else "?")
from flask_login import login_required, current_user
from sqlalchemy import func
from ..extensions import db
from ..models import OnboardingRequest, RequestStatus, UserRole, Notification, User
from ..utils import ROLE_QUEUES, REJECTED_STATUSES, PENDING_STATUSES, log_audit
from . import main_bp


@main_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    return redirect(url_for("auth.login"))


@main_bp.route("/dashboard")
@login_required
def dashboard():
    role = current_user.role

    if role == UserRole.SUPER_ADMIN:
        return redirect(url_for("admin.requests_list"))

    status_filter = request.args.get("status", "").strip()

    if role == UserRole.INITIATOR:
        base_q = OnboardingRequest.query.filter_by(
            initiated_by=current_user.id,
            is_deleted=False,
        )

        # Use SQL aggregates instead of loading all rows into Python
        _status_counts = dict(
            db.session.query(OnboardingRequest.status, func.count())
            .filter_by(initiated_by=current_user.id, is_deleted=False)
            .group_by(OnboardingRequest.status)
            .all()
        )
        counts = {
            "total":    sum(_status_counts.values()),
            "draft":    _status_counts.get(RequestStatus.DRAFT, 0),
            "pending":  sum(_status_counts.get(s, 0) for s in PENDING_STATUSES),
            "active":   _status_counts.get(RequestStatus.ACTIVE, 0),
            "rejected": sum(_status_counts.get(s, 0) for s in REJECTED_STATUSES),
        }

        # Apply status filter + paginate
        page = request.args.get("page", 1, type=int)
        filtered_q = base_q
        if status_filter:
            try:
                filtered_q = base_q.filter_by(status=RequestStatus(status_filter))
            except ValueError:
                pass
        pagination = filtered_q.order_by(
            OnboardingRequest.updated_at.desc()
        ).paginate(page=page, per_page=_per_page(25), error_out=False)

        return render_template(
            "dashboard/initiator.html",
            requests=pagination.items,
            pagination=pagination,
            pg_base=_pg_base(),
            counts=counts,
            statuses=list(RequestStatus),
            selected_status=status_filter,
        )

    # ── Approver roles ─────────────────────────────────────────────────────────
    pending_status = ROLE_QUEUES.get(role)
    queue = []
    all_requests = []

    if pending_status:
        # Drafts are only visible to the initiator — never show them to approvers
        base_q = OnboardingRequest.query.filter(
            OnboardingRequest.is_deleted == False,
            OnboardingRequest.status != RequestStatus.DRAFT,
        )
        all_q = OnboardingRequest.query.filter(
            OnboardingRequest.is_deleted == False,
            OnboardingRequest.status != RequestStatus.DRAFT,
        )

        # Business Head: scoped by company tick marks (2026-09-21,
        # fail-closed — zero ticks means zero requests visible), and, only
        # within the RDC-ticked pool, further scoped to requests from
        # initiators who share at least one region with this BH —
        # multi-region on both sides. See utils.bh_ids_for_initiator() for
        # the same overlap from the other direction (the old direct
        # business_head_id assignment is legacy — see its comment in
        # models.py). Ultrafine/ROBO have no region concept — company scope
        # alone is the whole story for those companies' rows.
        if role == UserRole.BUSINESS_HEAD:
            from ..models import BusinessHeadRegion, InitiatorRegion, UserCompanyScope

            my_companies = {
                r.company for r in UserCompanyScope.query.filter_by(user_id=current_user.id).all()
            }
            if not my_companies:
                base_q = base_q.filter(db.false())
                all_q = all_q.filter(db.false())
            else:
                base_q = base_q.filter(OnboardingRequest.company_code.in_(my_companies))
                all_q = all_q.filter(OnboardingRequest.company_code.in_(my_companies))

                if "RDC" in my_companies:
                    my_region_ids = {
                        r.cluster_id for r in
                        BusinessHeadRegion.query.filter_by(business_head_id=current_user.id).all()
                    }
                    # Regions covered by at least one active BH (any BH, not just me) —
                    # an initiator whose region(s) nobody actively covers falls open to
                    # every BH, same as a fully-unassigned initiator.
                    covered_region_ids = {
                        r.cluster_id for r in
                        BusinessHeadRegion.query.join(User, User.id == BusinessHeadRegion.business_head_id)
                        .filter(User.is_active == True).all()
                    }

                    # Pushed to SQL as correlated EXISTS clauses instead of
                    # loading every InitiatorRegion row + every active
                    # Initiator into Python and looping (found by the
                    # 2026-09-21 performance audit — that was an
                    # O(all initiators) Python pass on every dashboard load
                    # for every Business Head). Same semantics as before,
                    # evaluated per-row by MySQL against InitiatorRegion's
                    # (initiator_id, cluster_id) unique index instead:
                    # visible if the initiator has no regions at all
                    # (fail-open), OR any of their regions overlap mine, OR
                    # none of their regions are covered by any active BH at
                    # all (fail-open).
                    def _region_exists(cluster_ids):
                        if not cluster_ids:
                            return db.false()
                        return db.exists().where(
                            InitiatorRegion.initiator_id == OnboardingRequest.initiated_by,
                            InitiatorRegion.cluster_id.in_(cluster_ids),
                        )

                    has_any_region = db.exists().where(
                        InitiatorRegion.initiator_id == OnboardingRequest.initiated_by
                    )
                    rdc_ok = db.or_(
                        ~has_any_region,
                        _region_exists(my_region_ids),
                        ~_region_exists(covered_region_ids),
                    )
                    base_q = base_q.filter(db.or_(OnboardingRequest.company_code != "RDC", rdc_ok))
                    all_q = all_q.filter(db.or_(OnboardingRequest.company_code != "RDC", rdc_ok))
                # else: RDC not ticked -> the company filter above already excludes all RDC rows

        # HR Manager: company-scoped only (2026-09-21, fail-closed) — never
        # region-narrowed, regardless of company (an RDC-ticked HR Manager
        # handles every RDC region, confirmed with the stakeholder).
        elif role == UserRole.HR_MANAGER:
            from ..models import UserCompanyScope

            my_companies = {
                r.company for r in UserCompanyScope.query.filter_by(user_id=current_user.id).all()
            }
            if not my_companies:
                base_q = base_q.filter(db.false())
                all_q = all_q.filter(db.false())
            else:
                base_q = base_q.filter(OnboardingRequest.company_code.in_(my_companies))
                all_q = all_q.filter(OnboardingRequest.company_code.in_(my_companies))

        queue = base_q.filter_by(status=pending_status).order_by(
            OnboardingRequest.updated_at.asc()).all()

        # All requests with optional status filter + paginate
        if status_filter:
            try:
                all_q = all_q.filter_by(status=RequestStatus(status_filter))
            except ValueError:
                pass
        page = request.args.get("page", 1, type=int)
        all_pagination = all_q.order_by(
            OnboardingRequest.updated_at.desc()
        ).paginate(page=page, per_page=_per_page(25), error_out=False)
        all_requests = all_pagination.items
    else:
        all_pagination = None

    counts = {
        "pending": len(queue),
        "total":   OnboardingRequest.query.filter_by(is_deleted=False).count(),
        "active":  OnboardingRequest.query.filter_by(status=RequestStatus.ACTIVE, is_deleted=False).count(),
    }

    return render_template(
        "dashboard/approver.html",
        queue=queue,
        all_requests=all_requests,
        pagination=all_pagination,
        pg_base=_pg_base(),
        counts=counts,
        role=role,
        UserRole=UserRole,
        statuses=list(RequestStatus),
        selected_status=status_filter,
    )


@main_bp.route("/notifications")
@login_required
def notifications():
    filter_type = request.args.get("filter", "all").strip()
    page        = request.args.get("page", 1, type=int)
    pp          = _per_page(20)

    base_q = Notification.query.filter_by(recipient_id=current_user.id)

    # Tab counts (always calculated against full set)
    total_count  = base_q.count()
    unread_total = Notification.query.filter_by(
        recipient_id=current_user.id, is_read=False
    ).count()
    read_total = total_count - unread_total

    # Apply filter
    if filter_type == "unread":
        q = base_q.filter_by(is_read=False)
    elif filter_type == "read":
        q = base_q.filter_by(is_read=True)
    else:
        filter_type = "all"
        q = base_q

    pagination = q.order_by(Notification.sent_at.desc()).paginate(
        page=page, per_page=pp, error_out=False
    )

    args = {k: v for k, v in request.args.items()
            if k not in ("page", "per_page") and v}
    pg_base = request.path + ("?" + urlencode(args) + "&" if args else "?")

    return render_template(
        "dashboard/notifications.html",
        notifications=pagination.items,
        pagination=pagination,
        pg_base=pg_base,
        filter_type=filter_type,
        total_count=total_count,
        unread_total=unread_total,
        read_total=read_total,
    )


@main_bp.route("/notifications/mark-all-read", methods=["POST"])
@login_required
def mark_all_read():
    count = Notification.query.filter_by(
        recipient_id=current_user.id, is_read=False
    ).count()
    if count:
        Notification.query.filter_by(
            recipient_id=current_user.id, is_read=False
        ).update({"is_read": True})
        log_audit("NOTIFICATION", "NOTIFICATIONS_VIEWED",
                  detail={"marked_read_count": count, "action": "mark_all_read"})
        db.session.commit()
        flash(f"{count} notification{'s' if count != 1 else ''} marked as read.", "success")
    return redirect(request.referrer or url_for("main.notifications"))


@main_bp.route("/notifications/bulk", methods=["POST"])
@login_required
def notifications_bulk():
    action = request.form.get("action", "")
    ids    = request.form.getlist("ids", type=int)
    if not ids or action not in ("read", "unread"):
        return redirect(url_for("main.notifications"))

    new_state = (action == "read")
    Notification.query.filter(
        Notification.id.in_(ids),
        Notification.recipient_id == current_user.id,
    ).update({"is_read": new_state}, synchronize_session=False)

    log_audit(
        "NOTIFICATION",
        "NOTIFICATION_MARKED_READ" if new_state else "NOTIFICATION_MARKED_UNREAD",
        detail={"ids": ids, "count": len(ids), "bulk": True},
    )
    db.session.commit()
    flash(
        f"{len(ids)} notification{'s' if len(ids) != 1 else ''} "
        f"marked as {'read' if new_state else 'unread'}.",
        "success",
    )
    return redirect(request.referrer or url_for("main.notifications"))


@main_bp.route("/notifications/unread-count")
@login_required
def unread_count():
    count = Notification.query.filter_by(
        recipient_id=current_user.id, is_read=False
    ).count()
    return jsonify({"count": count})


@main_bp.route("/notifications/<int:notif_id>/read", methods=["POST"])
@login_required
def mark_read(notif_id):
    notif = db.get_or_404(Notification, notif_id)
    if notif.recipient_id != current_user.id:
        abort(403)
    notif.is_read = True
    log_audit("NOTIFICATION", "NOTIFICATION_MARKED_READ",
              resource_type="Notification", resource_id=notif.id,
              detail={"notification_id": notif.id,
                      "request_id": notif.request_id,
                      "subject": notif.subject})
    db.session.commit()
    return jsonify({"ok": True, "is_read": True})


@main_bp.route("/notifications/<int:notif_id>/unread", methods=["POST"])
@login_required
def mark_unread(notif_id):
    notif = db.get_or_404(Notification, notif_id)
    if notif.recipient_id != current_user.id:
        abort(403)
    notif.is_read = False
    log_audit("NOTIFICATION", "NOTIFICATION_MARKED_UNREAD",
              resource_type="Notification", resource_id=notif.id,
              detail={"notification_id": notif.id,
                      "request_id": notif.request_id,
                      "subject": notif.subject})
    db.session.commit()
    return jsonify({"ok": True, "is_read": False})
