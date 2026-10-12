"""
Admin screens for the RDC staffing-norm tables (2026-10-12): the volume / plant-count brackets and the
per-role limits in each bracket (NormTier + NormRequirement), the role categories themselves
(NormRoleCategory), and the Department -> category map used to bucket existing employees
(DepartmentCategoryMap).

These used to be editable only through one-off scripts (add_lt1500_tier.py and friends). The rule the
scripts had to remember by hand - two brackets of the same sheet/scope must never overlap, because
staffing_norms._find_tier() returns the first match (CLAUDE.md gotcha #17) - is now enforced here on every save.

Changes to limits take effect for the hiring gate at once (it reads NormRequirement live); the numbers
shown on the Staffing Status dashboard come from the stored snapshot and refresh at the next sync.
"""
import re

from flask import render_template, redirect, url_for, flash, request, abort
from flask_login import login_required
from sqlalchemy.exc import SQLAlchemyError

from ..extensions import db
from ..models import (
    UserRole, NormRoleCategory, NormTier, NormRequirement, NormScope, NormSheet, NormRequirementType,
    DepartmentCategoryMap,
)
from ..utils import role_required, log_audit
from . import admin_bp

_AUDIT = "ADMIN_STAFFING_NORM"
_INF = float("inf")


# ── small helpers ─────────────────────────────────────────────────────────────

def _num(raw):
    """'' -> None, '12.5' -> 12.5, junk -> raises ValueError."""
    raw = (raw or "").strip()
    return None if raw == "" else float(raw)


def _fmt_num(v):
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else str(v)


def describe_requirement(r) -> str:
    """One short phrase for a requirement row, e.g. 'Fixed 1' or '1 per 900 m³'."""
    t = r.requirement_type
    if t == NormRequirementType.FIXED:
        return f"Fixed {r.fixed_count if r.fixed_count is not None else '?'}"
    if t == NormRequirementType.RATE_PER_VOLUME:
        return f"{_fmt_num(r.rate_per_unit or 1)} per {_fmt_num(r.unit_volume)} m³"
    if t == NormRequirementType.PER_BUSINESS_HEAD:
        return "Per business head"
    return "None allowed"


def _range_text(t) -> str:
    unit = "plants" if t.scope == NormScope.CLUSTER else "m³"
    lo, hi = t.min_value, t.max_value
    if lo is None and hi is None:
        return "any"
    if lo is None:
        return f"below {_fmt_num(hi)} {unit}"
    if hi is None:
        return f"{_fmt_num(lo)} {unit} and above"
    return f"{_fmt_num(lo)} up to {_fmt_num(hi)} {unit}"


def _overlapping_tier(sheet, scope, lo, hi, exclude_id=None):
    """The first OTHER active tier of this sheet/scope whose [min, max) overlaps [lo, hi), or None."""
    a_lo = -_INF if lo is None else lo
    a_hi = _INF if hi is None else hi
    for t in NormTier.query.filter_by(sheet=sheet, scope=scope, is_active=True).all():
        if t.id == exclude_id:
            continue
        t_lo = -_INF if t.min_value is None else t.min_value
        t_hi = _INF if t.max_value is None else t.max_value
        if a_lo < t_hi and t_lo < a_hi:
            return t
    return None


def _tier_key(label, sheet, scope):
    base = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").upper()[:40] or "TIER"
    key, n = base, 2
    while NormTier.query.filter_by(sheet=sheet, scope=scope, tier_key=key).first():
        key = f"{base}_{n}"
        n += 1
    return key


def _categories_for(sheet, scope):
    return (NormRoleCategory.query.filter_by(sheet=sheet, scope=scope, is_deleted=False)
            .order_by(NormRoleCategory.sort_order, NormRoleCategory.name).all())


def _enum(cls, raw):
    try:
        return cls(raw)
    except ValueError:
        return None


# ── overview ──────────────────────────────────────────────────────────────────

@admin_bp.route("/staffing-norms")
@login_required
@role_required(UserRole.SUPER_ADMIN)
def staffing_norms():
    tiers = NormTier.query.order_by(NormTier.sheet, NormTier.scope, NormTier.min_value.asc()).all()
    reqs = NormRequirement.query.all()
    by_cell = {}
    for r in reqs:
        by_cell.setdefault((r.norm_role_category_id, r.tier_id), []).append(describe_requirement(r))
    groups = []
    seen = []
    for t in tiers:
        k = (t.sheet, t.scope)
        if k not in seen:
            seen.append(k)
    for sheet, scope in seen:
        groups.append({
            "sheet": sheet, "scope": scope,
            "title": f"{'Plants' if scope == NormScope.PLANT else 'Clusters'} — {'Sheet 1' if sheet == NormSheet.SHEET1 else 'Sheet 2'}",
            "tiers": [t for t in tiers if (t.sheet, t.scope) == (sheet, scope)],
            "categories": _categories_for(sheet, scope),
        })
    return render_template("admin/staffing_norms.html", groups=groups, cells=by_cell,
                           range_text=_range_text,
                           categories=NormRoleCategory.query.filter_by(is_deleted=False)
                           .order_by(NormRoleCategory.scope, NormRoleCategory.sort_order, NormRoleCategory.name).all())


# ── tiers + their limits ──────────────────────────────────────────────────────

def _tier_page(tier, sheet, scope, errors=None):
    cats = _categories_for(sheet, scope)
    existing = {}
    if tier is not None:
        for r in NormRequirement.query.filter_by(tier_id=tier.id).order_by(NormRequirement.id).all():
            existing.setdefault(r.norm_role_category_id, []).append(r)
    return render_template("admin/norm_tier_form.html", tier=tier, sheet=sheet, scope=scope, cats=cats,
                           existing=existing, errors=errors or [], types=NormRequirementType,
                           fmt=_fmt_num)


def _parse_requirement(prefix, form):
    """-> (dict of column values, None) or (None, error text). Blank type (new-row slot) -> ({}, None)."""
    rtype = _enum(NormRequirementType, form.get(f"{prefix}_type", ""))
    if rtype is None:
        return {}, None
    vals = {"requirement_type": rtype, "fixed_count": None, "rate_per_unit": None, "unit_volume": None,
            "notes": (form.get(f"{prefix}_notes", "") or "").strip()[:300] or None}
    try:
        if rtype == NormRequirementType.FIXED:
            n = _num(form.get(f"{prefix}_fixed"))
            if n is None or n < 0 or not float(n).is_integer():
                return None, "a 'Fixed' limit needs a whole number (0 or more)"
            vals["fixed_count"] = int(n)
        elif rtype == NormRequirementType.RATE_PER_VOLUME:
            rate, unit = _num(form.get(f"{prefix}_rate")), _num(form.get(f"{prefix}_unit"))
            if rate is None or rate <= 0 or unit is None or unit <= 0:
                return None, "a 'per volume' limit needs both numbers, greater than 0"
            vals["rate_per_unit"], vals["unit_volume"] = rate, unit
    except ValueError:
        return None, "numbers only, please"
    return vals, None


@admin_bp.route("/staffing-norms/tier/new", methods=["GET", "POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def new_norm_tier():
    sheet = _enum(NormSheet, request.values.get("sheet", "SHEET1")) or NormSheet.SHEET1
    scope = _enum(NormScope, request.values.get("scope", "PLANT")) or NormScope.PLANT
    if request.method == "POST":
        return _save_tier(None, sheet, scope)
    return _tier_page(None, sheet, scope)


@admin_bp.route("/staffing-norms/tier/<int:tier_id>", methods=["GET", "POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def edit_norm_tier(tier_id):
    tier = db.session.get(NormTier, tier_id) or abort(404)
    if request.method == "POST":
        return _save_tier(tier, tier.sheet, tier.scope)
    return _tier_page(tier, tier.sheet, tier.scope)


def _save_tier(tier, sheet, scope):
    f = request.form
    errors = []
    label = (f.get("tier_label", "") or "").strip()[:100]
    if not label:
        errors.append("Give the bracket a name, e.g. “3000-5000 m³”.")
    try:
        lo, hi = _num(f.get("min_value")), _num(f.get("max_value"))
    except ValueError:
        lo = hi = None
        errors.append("From / up-to must be numbers (or left empty).")
    if lo is not None and hi is not None and lo >= hi:
        errors.append("“From” must be smaller than “up to”.")
    active = f.get("is_active") == "1"
    if not errors and active:
        clash = _overlapping_tier(sheet, scope, lo, hi, exclude_id=tier.id if tier else None)
        if clash:
            errors.append(f"This range overlaps the bracket “{clash.tier_label}” ({_range_text(clash)}). "
                          "Brackets must not overlap — change one of them first.")

    changes = []   # (requirement or None, values, delete?)
    cats = _categories_for(sheet, scope)
    if tier is not None:
        for r in NormRequirement.query.filter_by(tier_id=tier.id).all():
            if f.get(f"r{r.id}_del") == "1":
                changes.append((r, None, True))
                continue
            vals, err = _parse_requirement(f"r{r.id}", f)
            if err:
                errors.append(f"{r.norm_role_category.name}: {err}.")
            elif vals:
                changes.append((r, vals, False))
    for c in cats:
        vals, err = _parse_requirement(f"n{c.id}", f)
        if err:
            errors.append(f"{c.name}: {err}.")
        elif vals:
            changes.append((c, vals, False))   # a NormRoleCategory in slot 0 marks "new row for this category"

    if errors:
        for e in errors:
            flash(e, "danger")
        return _tier_page(tier, sheet, scope, errors)

    creating = tier is None
    before = None
    if creating:
        tier = NormTier(sheet=sheet, scope=scope, tier_key=_tier_key(label, sheet, scope))
        db.session.add(tier)
    else:
        before = {"label": tier.tier_label, "min": tier.min_value, "max": tier.max_value, "active": tier.is_active}
    tier.tier_label, tier.min_value, tier.max_value, tier.is_active = label, lo, hi, active
    if creating:
        db.session.flush()
        tier.sort_order = tier.id

    log = []
    for target, vals, delete in changes:
        if isinstance(target, NormRoleCategory):
            row = NormRequirement(tier_id=tier.id, norm_role_category_id=target.id, **vals)
            db.session.add(row)
            log.append(f"{target.name}: added {describe_requirement(row)}")
        elif delete:
            log.append(f"{target.norm_role_category.name}: removed {describe_requirement(target)}")
            db.session.delete(target)
        else:
            old = describe_requirement(target)
            for k, v in vals.items():
                setattr(target, k, v)
            if old != describe_requirement(target):
                log.append(f"{target.norm_role_category.name}: {old} → {describe_requirement(target)}")
    log_audit(_AUDIT, "NORM_TIER_CREATED" if creating else "NORM_TIER_UPDATED", resource_type="NormTier",
              resource_id=tier.id, resource_label=f"Staffing norm bracket: {label}",
              detail={"before": before, "after": {"label": label, "min": lo, "max": hi, "active": active},
                      "limits": log})
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("Database error. Nothing was saved - please try again.", "danger")
        return _tier_page(None if creating else db.session.get(NormTier, tier.id), sheet, scope)
    flash(f"Bracket “{label}” {'added' if creating else 'saved'}.", "success")
    return redirect(url_for("admin.staffing_norms"))


# ── role categories ───────────────────────────────────────────────────────────

@admin_bp.route("/staffing-norms/category/new", methods=["GET", "POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def new_norm_category():
    return _category_form(None)


@admin_bp.route("/staffing-norms/category/<int:cat_id>", methods=["GET", "POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def edit_norm_category(cat_id):
    cat = db.session.get(NormRoleCategory, cat_id)
    if cat is None or cat.is_deleted:
        abort(404)
    return _category_form(cat)


def _category_form(cat):
    if request.method == "POST":
        f = request.form
        name = (f.get("name", "") or "").strip()[:150]
        scope = cat.scope if cat else (_enum(NormScope, f.get("scope", "")) or NormScope.PLANT)
        sheet = cat.sheet if cat else (_enum(NormSheet, f.get("sheet", "")) or NormSheet.SHEET1)
        problem = None
        if not name:
            problem = "Give the category a name."
        else:
            clash = NormRoleCategory.query.filter(
                db.func.lower(NormRoleCategory.name) == name.lower(), NormRoleCategory.scope == scope,
                NormRoleCategory.sheet == sheet, NormRoleCategory.is_deleted == False)   # noqa: E712
            if cat:
                clash = clash.filter(NormRoleCategory.id != cat.id)
            if clash.first():
                problem = f"A category named “{name}” already exists here."
        if problem:
            flash(problem, "danger")
            return render_template("admin/norm_category_form.html", cat=cat)
        gated = f.get("is_capacity_gated") == "1"
        active = f.get("is_active") == "1"
        if cat is None:
            cat = NormRoleCategory(name=name, scope=scope, sheet=sheet)
            db.session.add(cat)
            before = None
        else:
            before = {"name": cat.name, "gated": cat.is_capacity_gated, "active": cat.is_active}
        cat.name, cat.is_capacity_gated, cat.is_active = name, gated, active
        order = f.get("sort_order", type=int)
        if order is not None:
            cat.sort_order = order
        db.session.flush()
        log_audit(_AUDIT, "NORM_CATEGORY_SAVED", resource_type="NormRoleCategory", resource_id=cat.id,
                  resource_label=f"Staffing norm category: {name}",
                  detail={"before": before, "after": {"name": name, "gated": gated, "active": active}})
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            flash("Database error. Nothing was saved - please try again.", "danger")
            return render_template("admin/norm_category_form.html", cat=cat if cat.id else None)
        flash(f"Category “{name}” saved.", "success")
        return redirect(url_for("admin.staffing_norms"))
    return render_template("admin/norm_category_form.html", cat=cat)


# ── Department -> category map ────────────────────────────────────────────────

def _seed_default_department_map() -> int:
    """First visit: copy the built-in starting set into the table so every row becomes editable."""
    from ..services import headcount
    cats = {c.name: c for c in NormRoleCategory.query.filter_by(scope=NormScope.PLANT, is_deleted=False).all()}
    added = 0
    for norm, cat_name in headcount._DEPARTMENT_TO_CATEGORY.items():
        cat = cats.get(cat_name)
        if cat is None:
            continue
        db.session.add(DepartmentCategoryMap(
            department=headcount._DEFAULT_DEPARTMENT_LABELS.get(norm, norm), department_norm=norm,
            norm_role_category_id=cat.id))
        added += 1
    if added:
        log_audit(_AUDIT, "DEPARTMENT_MAP_SEEDED", resource_label="Department mapping starting set",
                  detail={"rows": added})
        db.session.commit()
    return added


def _unmapped_departments(mapped_norms, limit=40):
    """Departments on people in the latest sync that no row above covers - what an admin would want to map."""
    from ..models import EmployeeLocationSnapshot
    from ..services import headcount
    latest = db.session.query(db.func.max(EmployeeLocationSnapshot.computed_at)).scalar()
    if latest is None:
        return []
    rows = (db.session.query(EmployeeLocationSnapshot.department, db.func.count())
            .filter(EmployeeLocationSnapshot.computed_at == latest, EmployeeLocationSnapshot.company.is_(None),
                    EmployeeLocationSnapshot.norm_role_category_id.is_(None),
                    EmployeeLocationSnapshot.department.isnot(None), EmployeeLocationSnapshot.department != "")
            .group_by(EmployeeLocationSnapshot.department).all())
    merged = {}
    for dept, n in rows:
        norm = headcount._normalize_department(dept)
        if norm in mapped_norms or norm == headcount._TRAINEE_CIVIL_DEPARTMENT:
            continue
        label, total = merged.get(norm, (dept, 0))
        merged[norm] = (label, total + n)
    return sorted(((lbl, n) for lbl, n in merged.values()), key=lambda x: -x[1])[:limit]


@admin_bp.route("/department-mapping")
@login_required
@role_required(UserRole.SUPER_ADMIN)
def department_mapping():
    if DepartmentCategoryMap.query.first() is None:
        _seed_default_department_map()
    rows = (DepartmentCategoryMap.query.join(NormRoleCategory)
            .order_by(NormRoleCategory.sort_order, NormRoleCategory.name, DepartmentCategoryMap.department).all())
    cats = (NormRoleCategory.query.filter_by(scope=NormScope.PLANT, is_deleted=False, is_active=True)
            .order_by(NormRoleCategory.sort_order, NormRoleCategory.name).all())
    return render_template("admin/department_mapping.html", rows=rows, cats=cats,
                           unmapped=_unmapped_departments({r.department_norm for r in rows}),
                           prefill=request.args.get("dept", "").strip()[:200])


@admin_bp.route("/department-mapping/add", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def add_department_mapping():
    from ..services import headcount
    dept = (request.form.get("department", "") or "").strip()[:200]
    cat = db.session.get(NormRoleCategory, request.form.get("category_id", type=int) or 0)
    norm = headcount._normalize_department(dept)
    if not norm:
        flash("Type the department exactly as it appears in ZingHR / Truein.", "danger")
    elif norm == headcount._TRAINEE_CIVIL_DEPARTMENT:
        flash("“Trainee Civil” has its own built-in rule (counts as Technical after 90 days) and can't be remapped here.",
              "danger")
    elif cat is None or cat.is_deleted or cat.scope != NormScope.PLANT:
        flash("Pick a category.", "danger")
    elif DepartmentCategoryMap.query.filter_by(department_norm=norm).first():
        flash(f"“{dept}” is already in the list.", "danger")
    else:
        row = DepartmentCategoryMap(department=dept, department_norm=norm, norm_role_category_id=cat.id)
        db.session.add(row)
        db.session.flush()
        log_audit(_AUDIT, "DEPARTMENT_MAP_ADDED", resource_type="DepartmentCategoryMap", resource_id=row.id,
                  resource_label=f"Department “{dept}” → {cat.name}")
        db.session.commit()
        flash(f"“{dept}” now counts as {cat.name} from the next sync.", "success")
    return redirect(url_for("admin.department_mapping"))


@admin_bp.route("/department-mapping/<int:row_id>/update", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def update_department_mapping(row_id):
    row = db.session.get(DepartmentCategoryMap, row_id) or abort(404)
    cat = db.session.get(NormRoleCategory, request.form.get("category_id", type=int) or 0)
    if cat is None or cat.is_deleted or cat.scope != NormScope.PLANT:
        flash("Pick a category.", "danger")
        return redirect(url_for("admin.department_mapping"))
    before = {"category": row.norm_role_category.name, "active": row.is_active}
    row.norm_role_category_id = cat.id
    row.is_active = request.form.get("is_active") == "1"
    log_audit(_AUDIT, "DEPARTMENT_MAP_UPDATED", resource_type="DepartmentCategoryMap", resource_id=row.id,
              resource_label=f"Department “{row.department}”",
              detail={"before": before, "after": {"category": cat.name, "active": row.is_active}})
    db.session.commit()
    flash(f"Saved “{row.department}”. Applies from the next sync.", "success")
    return redirect(url_for("admin.department_mapping"))


@admin_bp.route("/department-mapping/<int:row_id>/delete", methods=["POST"])
@login_required
@role_required(UserRole.SUPER_ADMIN)
def delete_department_mapping(row_id):
    row = db.session.get(DepartmentCategoryMap, row_id) or abort(404)
    label = row.department
    log_audit(_AUDIT, "DEPARTMENT_MAP_REMOVED", resource_type="DepartmentCategoryMap", resource_id=row.id,
              resource_label=f"Department “{label}” → {row.norm_role_category.name}")
    db.session.delete(row)
    db.session.commit()
    flash(f"Removed “{label}”. People in that department stop being counted from the next sync.", "success")
    return redirect(url_for("admin.department_mapping"))
