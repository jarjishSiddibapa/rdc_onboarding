import io
from datetime import datetime, timedelta
from flask import make_response, render_template, request, jsonify
from flask_login import login_required
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from sqlalchemy.orm import joinedload, selectinload
from ..extensions import db
from ..models import (
    OnboardingRequest, RequestStatus, UserRole, User,
    FormField, FieldType, PlantLocation, Designation, ApprovalAction,
)
from ..extensions import limiter
from ..utils import role_required, log_audit
from . import exports_bp

_ALLOWED_ROLES = (UserRole.SUPER_ADMIN, UserRole.HEAD_HR, UserRole.HR_MANAGER, UserRole.DR_BHOON)

_STATUS_LABELS = {
    "DRAFT":             "Draft",
    "PENDING_BH":        "Pending Business Head",
    "PENDING_DR_BHOON":  "Pending Dr. Bhoon",
    "PENDING_HR_MANAGER":"Pending HR Manager",
    "PENDING_HEAD_HR":   "Pending Head HR",
    "ACTIVE":            "Approved",
    "REJECTED_BH":       "Rejected by Business Head",
    "REJECTED_DR_BHOON": "Rejected by Dr. Bhoon",
    "REJECTED_HRM":      "Rejected by HR Manager",
    "REJECTED_HEAD_HR":  "Rejected by Head HR",
}


# ── Shared: collect filter params from request.args ───────────────────────────

def _collect_filter_params(args):
    return {
        "statuses_raw":   args.getlist("status"),
        "date_from_s":    args.get("date_from",    "").strip(),
        "date_to_s":      args.get("date_to",      "").strip(),
        "joining_from_s": args.get("joining_from", "").strip(),
        "joining_to_s":   args.get("joining_to",   "").strip(),
        "plant_filter":   args.get("plant",        "").strip(),
        "desig_filter":   args.get("designation",  "").strip(),
        "company_filter": args.get("company",      "").strip(),
        "initiator_id_s": args.get("initiator_id", "").strip(),
        "bh_id_s":        args.get("bh_id",        "").strip(),
        "retry_min_s":    args.get("retry_min",    "").strip(),
        "retry_max_s":    args.get("retry_max",    "").strip(),
    }


# ── Shared: apply filters and return records ──────────────────────────────────

def _apply_filters(params):
    """Build query from filter params dict; return list of matching records."""
    p = params
    # initiator/actions are lazy="select" by default (actions.actor too) —
    # _cell_val()/_actor_for_role() below touch both per record, which was
    # a real N+1 (2+ extra queries per row). Negligible at today's row
    # counts but compounds linearly as onboarding_requests grows, and it's
    # a one-line fix, so applied eagerly rather than left for later.
    q = OnboardingRequest.query.filter_by(is_deleted=False).options(
        joinedload(OnboardingRequest.initiator),
        selectinload(OnboardingRequest.actions).joinedload(ApprovalAction.actor),
    )

    # Status
    if p["statuses_raw"]:
        try:
            status_enums = [RequestStatus(s) for s in p["statuses_raw"] if s]
            if status_enums:
                q = q.filter(OnboardingRequest.status.in_(status_enums))
        except ValueError:
            pass
    else:
        q = q.filter_by(status=RequestStatus.ACTIVE)

    # Submission date range
    if p["date_from_s"]:
        try:
            q = q.filter(OnboardingRequest.created_at >=
                         datetime.strptime(p["date_from_s"], "%Y-%m-%d"))
        except ValueError:
            pass
    if p["date_to_s"]:
        try:
            q = q.filter(OnboardingRequest.created_at <
                         datetime.strptime(p["date_to_s"], "%Y-%m-%d") + timedelta(days=1))
        except ValueError:
            pass

    # Plant / designation / company
    if p["plant_filter"]:
        q = q.filter_by(plant_location=p["plant_filter"])
    if p["desig_filter"]:
        q = q.filter_by(designation=p["desig_filter"])
    if p["company_filter"]:
        q = q.filter_by(company_code=p["company_filter"])

    # Initiator / BH
    if p["initiator_id_s"]:
        try:
            q = q.filter_by(initiated_by=int(p["initiator_id_s"]))
        except ValueError:
            pass
    elif p["bh_id_s"]:
        try:
            bh_int = int(p["bh_id_s"])
            rows = db.session.execute(
                db.text("SELECT id FROM users WHERE business_head_id = :bh"),
                {"bh": bh_int}
            ).fetchall()
            initiator_ids = [r[0] for r in rows]
            if initiator_ids:
                q = q.filter(OnboardingRequest.initiated_by.in_(initiator_ids))
            else:
                q = q.filter(OnboardingRequest.id == -1)
        except ValueError:
            pass

    # Retry count range
    if p["retry_min_s"]:
        try:
            q = q.filter(OnboardingRequest.retry_count >= int(p["retry_min_s"]))
        except ValueError:
            pass
    if p["retry_max_s"]:
        try:
            q = q.filter(OnboardingRequest.retry_count <= int(p["retry_max_s"]))
        except ValueError:
            pass

    records = q.order_by(OnboardingRequest.created_at.desc()).all()

    # Python-side joining date filter (stored in JSON form_data)
    if p["joining_from_s"] or p["joining_to_s"]:
        try:
            jf = datetime.strptime(p["joining_from_s"], "%Y-%m-%d").date() if p["joining_from_s"] else None
            jt = datetime.strptime(p["joining_to_s"],   "%Y-%m-%d").date() if p["joining_to_s"]   else None
            filtered = []
            for r in records:
                jd_str = r.form_data.get("joining_date", "")
                if not jd_str:
                    continue
                try:
                    jd = datetime.strptime(jd_str[:10], "%Y-%m-%d").date()
                    if jf and jd < jf:
                        continue
                    if jt and jd > jt:
                        continue
                    filtered.append(r)
                except Exception:
                    continue
            records = filtered
        except Exception:
            pass

    return records


# ── Shared: report title ──────────────────────────────────────────────────────

def _report_title(params):
    if params["statuses_raw"]:
        status_str = " + ".join(_STATUS_LABELS.get(s, s) for s in params["statuses_raw"])
    else:
        status_str = "Approved"
    return f"{status_str} — Onboarding Report"


# ── Shared: human-readable filter summary chips ───────────────────────────────

def _filter_chips(params):
    chips = []
    p = params

    if p["statuses_raw"]:
        labels = [_STATUS_LABELS.get(s, s) for s in p["statuses_raw"]]
        chips.append({"label": "Status", "value": ", ".join(labels)})
    else:
        chips.append({"label": "Status", "value": "Approved (default)"})

    if p["date_from_s"] or p["date_to_s"]:
        val = f"{p['date_from_s'] or '…'} → {p['date_to_s'] or '…'}"
        chips.append({"label": "Submitted", "value": val})

    if p["joining_from_s"] or p["joining_to_s"]:
        val = f"{p['joining_from_s'] or '…'} → {p['joining_to_s'] or '…'}"
        chips.append({"label": "Joining date", "value": val})

    if p["plant_filter"]:
        chips.append({"label": "Plant", "value": p["plant_filter"]})

    if p["desig_filter"]:
        chips.append({"label": "Designation", "value": p["desig_filter"]})

    if p["company_filter"]:
        chips.append({"label": "Company", "value": p["company_filter"]})

    if p["initiator_id_s"]:
        u = db.session.get(User, int(p["initiator_id_s"])) if p["initiator_id_s"].isdigit() else None
        chips.append({"label": "Initiator", "value": u.name if u else p["initiator_id_s"]})

    if p["bh_id_s"]:
        u = db.session.get(User, int(p["bh_id_s"])) if p["bh_id_s"].isdigit() else None
        chips.append({"label": "Business Head", "value": u.name if u else p["bh_id_s"]})

    if p["retry_min_s"] or p["retry_max_s"]:
        val = f"{p['retry_min_s'] or '0'} – {p['retry_max_s'] or '∞'}"
        chips.append({"label": "Retries", "value": val})

    return chips


# ── Shared: get ordered form field columns (no FILE fields) ───────────────────

def _get_form_fields():
    return FormField.query.filter(
        FormField.is_active == True,
        FormField.is_deleted == False,
        FormField.field_type != FieldType.FILE,
    ).order_by(FormField.step, FormField.sort_order).all()


# ── Shared: format a single record cell value ─────────────────────────────────

def _actor_for_role(req, role_value):
    """Return 'Name (EMP_CODE)' for the first APPROVED action by a given role."""
    from ..models import ApprovalActionType
    for act in req.actions:
        if (act.actor and act.actor.role.value == role_value
                and act.action == ApprovalActionType.APPROVED):
            code = act.actor.employee_code or "—"
            return f"{act.actor.name} ({code})"
    return ""


def _plant_display_name(plant_location_name, cache):
    """Raw plant_location_name -> proper ERP-Tracker display name (see
    PlantDvtMapping.display_name), cached across one export run so a
    multi-hundred-row report doesn't re-query per row."""
    if not plant_location_name:
        return plant_location_name
    if plant_location_name in cache:
        return cache[plant_location_name]
    from ..models import PlantDvtMapping
    row = PlantDvtMapping.query.filter_by(plant_location_name=plant_location_name, is_deleted=False).first()
    result = row.display_name if row else plant_location_name
    cache[plant_location_name] = result
    return result


def _cell_val(req, key, plant_name_cache=None):
    if   key == "_id":          return req.id
    elif key == "_status":      return _STATUS_LABELS.get(req.status.value, req.status.value)
    elif key == "_initiator":   return req.initiator.name if req.initiator else ""
    elif key == "_init_code":
        return (req.initiator.employee_code or "—") if req.initiator else ""
    elif key == "_bh_approval": return _actor_for_role(req, "BUSINESS_HEAD")
    elif key == "_hrm_approval":return _actor_for_role(req, "HR_MANAGER")
    elif key == "_hhr_approval":return _actor_for_role(req, "HEAD_HR")
    elif key == "_created":
        return req.created_at.strftime("%d/%m/%Y") if req.created_at else ""
    elif key == "_updated":
        return req.updated_at.strftime("%d/%m/%Y") if req.updated_at else ""
    elif key == "_retry":       return req.retry_count
    else:
        fd  = req.form_data
        raw = fd.get(key, "")
        if raw == "Other":
            other = fd.get(f"{key}_other", "")
            return f"Other: {other}" if other else "Other"
        if key == "plant_location" and raw:
            return _plant_display_name(raw, plant_name_cache if plant_name_cache is not None else {})
        return raw


# ── Excel builder ──────────────────────────────────────────────────────────────

def _build_excel(records, report_title="Employee Onboarding Report"):
    form_fields = _get_form_fields()

    meta_cols = [
        ("Request ID",        "_id",          12),
        ("Status",            "_status",      22),
        ("Initiated By",      "_initiator",   22),
        ("Initiator Emp Code","_init_code",   18),
        ("BH Approval",       "_bh_approval", 28),
        ("HR Mgr Approval",   "_hrm_approval",28),
        ("Head HR Approval",  "_hhr_approval",28),
        ("Submitted On",      "_created",     18),
        ("Last Updated",      "_updated",     18),
        ("Retry Count",       "_retry",       12),
    ]
    form_cols = [(f.field_label, f.field_key, max(len(f.field_label) + 4, 18)) for f in form_fields]
    all_cols = meta_cols + form_cols

    wb = Workbook()
    ws = wb.active
    ws.title = "Report"

    hdr_fill  = PatternFill(start_color="1F3864", end_color="1F3864", fill_type="solid")
    hdr_font  = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    dat_font  = Font(name="Calibri", size=10)
    dat_align = Alignment(vertical="center", wrap_text=False)
    thin      = Side(style="thin", color="CCCCCC")
    bdr       = Border(left=thin, right=thin, top=thin, bottom=thin)
    alt_fill  = PatternFill(start_color="EEF2FF", end_color="EEF2FF", fill_type="solid")

    # Title row
    ws.merge_cells(f"A1:{get_column_letter(len(all_cols))}1")
    tc = ws["A1"]
    tc.value = f"{report_title}  |  Generated: {datetime.now().strftime('%d %b %Y %H:%M')}"
    tc.font  = Font(name="Calibri", size=13, bold=True, color="1F3864")
    tc.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 28

    # Header row
    for ci, (label, _, width) in enumerate(all_cols, 1):
        c = ws.cell(row=2, column=ci, value=label)
        c.font = hdr_font; c.fill = hdr_fill
        c.alignment = hdr_align; c.border = bdr
        ws.column_dimensions[get_column_letter(ci)].width = width
    ws.row_dimensions[2].height = 30
    ws.freeze_panes = "A3"

    plant_name_cache = {}
    for ri, req in enumerate(records, 3):
        for ci, (_, key, _) in enumerate(all_cols, 1):
            val = _cell_val(req, key, plant_name_cache)
            c = ws.cell(row=ri, column=ci, value=val)
            c.font = dat_font; c.alignment = dat_align; c.border = bdr
            if ri % 2 == 0:
                c.fill = alt_fill
        ws.row_dimensions[ri].height = 18

    # Footer
    fr = len(records) + 3
    ws.merge_cells(f"A{fr}:{get_column_letter(len(all_cols))}{fr}")
    fc = ws[f"A{fr}"]
    fc.value = f"Total records: {len(records)}"
    fc.font = Font(name="Calibri", size=10, bold=True, color="555555")
    fc.alignment = Alignment(horizontal="right")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ── Filter page ────────────────────────────────────────────────────────────────

@exports_bp.route("/active-employees")
@login_required
@role_required(*_ALLOWED_ROLES)
def active_employees():
    plants = PlantLocation.query.filter_by(
        is_deleted=False, is_active=True
    ).order_by(PlantLocation.name).all()

    designations = Designation.query.filter_by(
        is_deleted=False, is_active=True
    ).order_by(Designation.name).all()

    companies = [r[0] for r in db.session.execute(db.text(
        "SELECT DISTINCT company_code FROM onboarding_requests "
        "WHERE is_deleted=0 AND company_code IS NOT NULL AND company_code != '' "
        "ORDER BY company_code"
    )).fetchall()]

    initiators = User.query.filter_by(
        role=UserRole.INITIATOR, is_active=True
    ).order_by(User.name).all()

    bhs = User.query.filter_by(
        role=UserRole.BUSINESS_HEAD, is_active=True
    ).order_by(User.name).all()

    return render_template(
        "exports/filter.html",
        statuses=list(RequestStatus),
        status_labels=_STATUS_LABELS,
        plants=plants,
        designations=designations,
        companies=companies,
        initiators=initiators,
        business_heads=bhs,
    )


# ── Preview endpoint (AJAX) ────────────────────────────────────────────────────

_PREVIEW_ROWS = 10   # rows shown in the preview table

@exports_bp.route("/active-employees/preview")
@login_required
@role_required(*_ALLOWED_ROLES)
@limiter.limit("60 per minute")
def preview_excel():
    """Return JSON preview: columns, first N rows, total count, filter chips."""
    params  = _collect_filter_params(request.args)
    records = _apply_filters(params)
    title   = _report_title(params)
    chips   = _filter_chips(params)

    form_fields = _get_form_fields()
    meta_cols = [
        ("Request ID",        "_id"),
        ("Status",            "_status"),
        ("Initiated By",      "_initiator"),
        ("Initiator Emp Code","_init_code"),
        ("BH Approval",       "_bh_approval"),
        ("HR Mgr Approval",   "_hrm_approval"),
        ("Head HR Approval",  "_hhr_approval"),
        ("Submitted On",      "_created"),
        ("Last Updated",      "_updated"),
        ("Retry Count",       "_retry"),
    ]
    form_cols = [(f.field_label, f.field_key) for f in form_fields]
    all_cols  = meta_cols + form_cols

    columns   = [label for label, _ in all_cols]
    preview   = records[:_PREVIEW_ROWS]
    plant_name_cache = {}
    rows      = [
        [str(_cell_val(r, key, plant_name_cache)) for _, key in all_cols]
        for r in preview
    ]

    return jsonify({
        "ok":           True,
        "total":        len(records),
        "preview_rows": _PREVIEW_ROWS,
        "title":        title,
        "columns":      columns,
        "rows":         rows,
        "chips":        chips,
    })


# ── Download endpoint ──────────────────────────────────────────────────────────

@exports_bp.route("/active-employees/download")
@login_required
@role_required(*_ALLOWED_ROLES)
def download_excel():
    params  = _collect_filter_params(request.args)
    records = _apply_filters(params)
    title   = _report_title(params)

    buf  = _build_excel(records, title)
    data = buf.getvalue()

    slug     = "_".join(params["statuses_raw"]) if params["statuses_raw"] else "ACTIVE"
    filename = f"onboarding_{slug}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"

    _filters = {k: v for k, v in params.items() if v}
    log_audit("EXPORT", "EXPORT_DOWNLOADED",
              detail={"record_count": len(records),
                      "filename":     filename,
                      "report_title": title,
                      "filters":      _filters})
    db.session.commit()

    resp = make_response(data)
    resp.headers["Content-Type"] = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["Content-Length"]       = len(data)
    resp.headers["Cache-Control"]        = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"]               = "no-cache"
    resp.headers["Expires"]              = "0"
    return resp
