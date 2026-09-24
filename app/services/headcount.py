"""
Combined ZingHR + Truein headcount computation for the RDC staffing gate.

Write side (compute_and_store_snapshot) runs only from the 2-hourly
background refresh job (app/services/snapshot_refresh.py) — it's the only
code path that calls the live ZingHR/Truein APIs for headcount purposes.
Read side is DB-only (StaffingSnapshot table), safe to call from any web
request including the submit_request() gate.

Design notes (filling gaps the stakeholder left open — see plan's Open
Items — with the most defensible reading of the confirmed decisions,
flagged here rather than silently assumed):

- PLANT-level headcount comes from ZingHR's `Location` attribute, and from
  Truein's `sub_site` field via PlantDvtMapping.truein_sub_site (distinct
  from Truein's `category`, which is cluster-level — see below).
- CLUSTER-level headcount comes from both ZingHR's `City` and Truein's
  `category`, reconciled via ClusterNameMapping.
- Role-bucketing (which norm_role_category an existing employee occupies)
  is driven entirely by the employee's raw Department value — see
  _classify_by_department() / _DEPARTMENT_TO_CATEGORY below. Employees whose
  department doesn't match any of the 6 known manpower categories are
  counted in `unclassified_count` rather than silently dropped or guessed.
"""
import json
import re
from datetime import datetime, timedelta

from ..extensions import db
from ..models import (
    StaffingSnapshot, EmployeeLocationSnapshot,
    NormScope, NormSheet, NormRequirementType, NormRoleCategory, NormTier, NormRequirement,
    PlantDvtMapping, PlantNameAlias, ClusterNameMapping, MatchConfidence,
    ExternalDesignationSource, PlantLocation,
)
from ..integrations import zinghr, truein, dvt

_TRUEIN_JUNK_CATEGORIES = {"on roll", "onroll", "other", "1st", "2nd"}
_TRUEIN_ALLOWED_SITE_NAME = "RDC Concrete"

# Department -> manpower category name, driving BOTH the read-only staffing
# dashboard and the live submit-time hiring gate — the only classification
# signal now used for existing employees. Verified directly against the raw
# Department strings ZingHR/Truein actually return (case/whitespace
# normalized by _normalize_department() below); anything not listed here
# stays unclassified, not counted in any of the 6 buckets.
_DEPARTMENT_TO_CATEGORY = {
    "pi/api/acting pi": "Plant Manager",
    "technical": "Technical",
    "techinical": "Technical",          # Truein spelling seen in live data
    "plant & technical": "Technical",
    "batching": "Batchers/Production Officer",
    "material": "Materials",
    "materials": "Materials",
    "assistant/rmx": "Assistant",       # slash-spacing normalized below
    "operations": "Operations",
}

_TRAINEE_CIVIL_DEPARTMENT = "trainee civil"
# "more than 3 months of joining completed" — approximated as 90 days.
_TRAINEE_CIVIL_MIN_TENURE_DAYS = 90

# Department values that read as "Technical" on their face, but must NOT
# immediately count a *trainee* toward Technical — see _classify_by_department.
_TECHNICAL_DEPARTMENT_VALUES = {"technical", "techinical", "plant & technical"}


def _normalize_department(department: str) -> str:
    norm = re.sub(r"\s+", " ", (department or "").strip()).lower()
    return re.sub(r"\s*/\s*", "/", norm)


def _parse_join_date(raw: str):
    """
    ZingHR and Truein store date_of_joining as free text in two different
    formats ("18 Aug 2025" vs ISO "2025-08-16"). Returns None on anything
    unparseable/missing rather than guessing.
    """
    if not raw:
        return None
    for fmt in ("%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    return None


def _classify_by_department(department: str, designation: str, date_of_joining_raw: str, cat_id_by_name: dict):
    """
    Maps a raw Department value straight onto one of the 6 manpower
    categories (see _DEPARTMENT_TO_CATEGORY) — the primary signal used to
    bucket an *existing* employee. Returns None (unclassified) if the
    department doesn't match any known value.

    "Trainee Civil" is a special case: it only counts toward Technical once
    the employee has completed more than 3 months' tenure — a brand-new
    Trainee Civil joiner isn't yet doing technical work. An unparseable or
    missing joining date is treated as "not yet 3 months" (excluded) rather
    than guessed, matching this codebase's fail-toward-permissive default.

    "Trainee Engineer" (designation) is a superset that includes Trainee
    Civil — confirmed against real data (2026-08-21, MH- Chh. Sambhaji
    Nagar): a "Trainee Engineer" tagged under Department "Plant & Technical"
    (not "Trainee Civil") who had joined ~7 weeks earlier was being counted
    as Technical immediately, with no tenure gate at all, since "Plant &
    Technical" is one of the direct-Technical department values above. Any
    trainee (designation containing "trainee") sitting under a
    Technical-looking department OTHER than "Trainee Civil" itself does NOT
    count toward Technical — only the Trainee Civil + >3-months path above
    can promote a trainee into Technical. This does not affect trainees in
    other departments (e.g. "Trainee RMX Technician" under "Assistant/RMX"
    still counts toward Assistant immediately) — the gate is specific to the
    Technical-looking department values, matching the confirmed bug.
    """
    norm = _normalize_department(department)
    if norm == _TRAINEE_CIVIL_DEPARTMENT:
        joined = _parse_join_date(date_of_joining_raw)
        if joined and (datetime.utcnow() - joined).days > _TRAINEE_CIVIL_MIN_TENURE_DAYS:
            return cat_id_by_name.get("Technical")
        return None
    if norm in _TECHNICAL_DEPARTMENT_VALUES and "trainee" in (designation or "").lower():
        return None
    cat_name = _DEPARTMENT_TO_CATEGORY.get(norm)
    return cat_id_by_name.get(cat_name) if cat_name else None


def _is_face_device(code: str, name: str) -> bool:
    """
    Face-attendance registration devices get their own pseudo-employee
    record in both ZingHR and Truein (e.g. code 'FACEBACHUPALLY', name
    'Face HYD Bachupally') — not a real person, must never count toward
    headcount. The word "face" reliably shows up in the code even on the
    handful of records where the name alone wouldn't catch it.
    """
    return "face" in (code or "").lower() or "face" in (name or "").lower()


def _normalize_code(code: str) -> str:
    return (code or "").strip().upper()


def _normalize_name(name: str) -> str:
    return re.sub(r"[\s\-_./]+", " ", (name or "")).strip().lower()


def _find_tier(tiers: list, value: float):
    for t in tiers:
        lo_ok = t.min_value is None or value >= t.min_value
        hi_ok = t.max_value is None or value < t.max_value
        if lo_ok and hi_ok:
            return t
    return None


def _calc_allowed_one(requirement, basis_value: float):
    """
    Duplicated from staffing_norms.compute_allowed_headcount() rather than
    imported — staffing_norms.py imports this module (for get_latest_snapshot),
    so importing the other way would create a circular import. Keep in sync
    if the norm-computation rules ever change.
    """
    if requirement is None:
        return None
    if requirement.requirement_type == NormRequirementType.FIXED:
        return requirement.fixed_count
    if requirement.requirement_type == NormRequirementType.RATE_PER_VOLUME:
        if not requirement.unit_volume:
            return None
        return round(basis_value / requirement.unit_volume * (requirement.rate_per_unit or 1))
    if requirement.requirement_type == NormRequirementType.NONE:
        return 0
    return None  # PER_BUSINESS_HEAD — not supported yet, see plan's Open Items


def _calc_allowed(requirements: list, basis_value: float):
    """
    Sums the allowed headcount across every NormRequirement row for a given
    (tier, category) — a category can now have more than one row (e.g.
    "Technical" absorbed the old "FTs/LT/TO" formula, so its allowed count
    is the fixed-1 formula PLUS the rate-per-volume formula for the same
    tier). None (unknown) if there are no requirement rows, or any single
    one can't be computed — stays conservative rather than silently
    under-counting the cap.
    """
    if not requirements:
        return None
    total = 0
    for r in requirements:
        one = _calc_allowed_one(r, basis_value)
        if one is None:
            return None
        total += one
    return total


def get_last_known_plant_volumes() -> dict[str, float]:
    """
    plant_code -> most recent known-good production volume, reconstructed
    from prior StaffingSnapshot runs. Used as a fallback when a live DVT
    fetch fails or comes back missing some plants, so a transient DVT outage
    doesn't blank a plant's tier/allowed-headcount to "Unknown" — showing a
    slightly stale-but-real number reads far better than "Unknown" for
    something that was known minutes/hours ago.
    """
    rows = (
        db.session.query(StaffingSnapshot.location_key, StaffingSnapshot.production_volume)
        .filter(StaffingSnapshot.scope == NormScope.PLANT, StaffingSnapshot.production_volume.isnot(None))
        .order_by(StaffingSnapshot.location_key, StaffingSnapshot.computed_at.desc())
        .all()
    )
    volume_by_location = {}
    for location_key, volume in rows:
        volume_by_location.setdefault(location_key, volume)

    code_by_location = dict(
        db.session.query(PlantDvtMapping.plant_location_name, PlantDvtMapping.dvt_plant_code)
        .filter(PlantDvtMapping.is_deleted.is_(False), PlantDvtMapping.dvt_plant_code.isnot(None))
        .all()
    )
    return {
        code: volume_by_location[name]
        for name, code in code_by_location.items()
        if name in volume_by_location
    }


_SNAPSHOT_LOCK_NAME = "rdc_staffing_snapshot_refresh"


def compute_and_store_snapshot() -> dict:
    """
    Public entry point. Wraps _compute_and_store_snapshot() in a MySQL
    advisory lock (GET_LOCK/RELEASE_LOCK) so two overlapping runs — e.g. the
    background thread's own boot-time run racing a manually-triggered one —
    can never write duplicate rows under the same (or same-second-rounded)
    timestamp. If another run is already in progress, this one waits briefly
    then skips rather than doubling up. No-ops the locking on non-MySQL
    (e.g. sqlite in tests), since only one process ever touches that DB.

    GET_LOCK/RELEASE_LOCK are scoped to a single physical MySQL connection —
    they are NOT session-scoped. Using db.session for both calls is unsafe:
    _compute_and_store_snapshot() commits internally, which can return the
    session's connection to the pool, so the later RELEASE_LOCK call may be
    issued on a *different* pooled connection and silently no-op, orphaning
    the lock until that connection is eventually recycled. A dedicated raw
    connection, held open for the whole critical section, avoids this.
    """
    def _run():
        # RDC first (its own commit inside, including its own
        # collision-avoidance bump on `computed_at` — see that function's
        # internals), then the independent Ultrafine/ROBO pass (added
        # 2026-09-15) reusing that EXACT SAME timestamp (see
        # _compute_and_store_other_company_snapshot()'s `now` docstring for
        # why this must match, not just be "close enough") — same lock,
        # same refresh cycle, but never touches the RDC computation itself.
        result = _compute_and_store_snapshot()
        shared_now = datetime.fromisoformat(result["computed_at"])
        other = _compute_and_store_other_company_snapshot(now=shared_now)
        db.session.commit()
        result["other_company"] = other
        return result

    is_mysql = db.engine.dialect.name == "mysql"
    if not is_mysql:
        return _run()

    conn = db.engine.connect()
    try:
        got_lock = conn.execute(db.text("SELECT GET_LOCK(:name, 5)"), {"name": _SNAPSHOT_LOCK_NAME}).scalar()
        if not got_lock:
            return {"skipped": True, "reason": "another snapshot refresh is already in progress"}
        try:
            return _run()
        finally:
            conn.execute(db.text("SELECT RELEASE_LOCK(:name)"), {"name": _SNAPSHOT_LOCK_NAME})
    finally:
        conn.close()


def _compute_and_store_snapshot() -> dict:
    """
    Pulls ZingHR + Truein once, resolves every active employee to a plant
    and/or cluster location and a norm role category, and writes one
    StaffingSnapshot row per (scope, location_key, norm_role_category_id)
    that had at least one matched employee. Rows are append-only — see
    get_latest_snapshot()/get_all_latest_snapshots() for how "no row this
    run" is distinguished from "stale data from an earlier run".
    """
    warnings = []

    # ZingHR is one shared tenant across RDC, Robo Silicon, and Ultrafine —
    # GetEmployeeDetails returns all three companies with no way to filter
    # server-side. This staffing system is RDC-only, so anyone whose
    # Company attribute confirms a *different* company must be excluded, or
    # Robo/Ultrafine headcount silently contaminates RDC's numbers whenever
    # their Location happens to match an RDC plant/cluster name. Records
    # with no Company attribute at all are kept (can't confirm they're not
    # RDC, and historically some legitimate RDC records lack it).
    _NON_RDC_COMPANIES = {"robo silicon pvt. ltd.", "ultrafine mineral and admixtures pvt ltd"}
    zh_employees = [
        e for e in zinghr.fetch_active_employees()
        if not _is_face_device(e.get("employeeCode"), e.get("employeeName"))
        and (e.get("Company") or "").strip().lower() not in _NON_RDC_COMPANIES
        and (e.get("Location") or "").strip().lower() != "head office"
    ]
    try:
        tr_employees_raw = truein._fetch_all_employees_raw()
    except Exception as exc:
        warnings.append(f"Truein fetch failed: {exc}")
        tr_employees_raw = []

    # Unlike ZingHR (filtered to Active via the "employmentstatus" API param —
    # see zinghr.fetch_active_employees()), Truein's getEmployeeDtls has no
    # such request-side filter: it returns every employee it has ever
    # recorded, active or not. ~23% of raw records are 'inactive' (left
    # organisation, auto-deactivated for long absence, etc.) and must be
    # excluded here or they're wrongly counted as current headcount.
    tr_employees = [
        e for e in tr_employees_raw
        if not _is_face_device(e.get("empId"), e.get("name"))
        and (e.get("status") or "").strip().lower() == "active"
        and (e.get("site_name") or "").strip() == _TRUEIN_ALLOWED_SITE_NAME
        and (e.get("category") or "").strip().lower() not in _TRUEIN_JUNK_CATEGORIES
        and (e.get("sub_site") or "").strip().lower() != "head office"
    ]

    # "ignore common ones" — an employee identifiable in both systems (matched
    # by normalized code) is counted once, via ZingHR (it carries City/
    # Location/Department; Truein doesn't need to double-contribute).
    zh_codes = {_normalize_code(e["employeeCode"]) for e in zh_employees if e.get("employeeCode")}
    tr_employees_deduped = [e for e in tr_employees if _normalize_code(e.get("empId")) not in zh_codes]
    dedup_count = len(tr_employees) - len(tr_employees_deduped)

    # Plant-name-based company override (added 2026-09-23, confirmed with the
    # stakeholder): Truein has no Ultrafine/ROBO concept at all in this
    # account — every Truein record's site_name is "RDC Concrete"/"RDC
    # Drivers" regardless of who the employee actually works for, since
    # those two companies were never given their own Truein subscription.
    # Before this fix, a real Ultrafine/ROBO employee whose attendance is
    # tracked through Truein (or a ZingHR record with a blank Company
    # attribute — see the _NON_RDC_COMPANIES filter above, which only
    # excludes a *confirmed* non-RDC Company) was silently counted as an
    # unclassified RDC employee the moment their raw Location/sub_site
    # string happened to match one of these companies' own plant names
    # (e.g. real employees at "ULT-Wada"/"Robo-AP_RO" were showing up as
    # RDC headcount under a stale, unmatched RDC-tagged PlantLocation row
    # with the same name). The stakeholder's rule: an employee's plant name
    # is the authoritative signal of which company they belong to — if it
    # matches a known Ultrafine/ROBO plant, they ARE that company's
    # employee, full stop, regardless of what source system reported them
    # or what that system's own company/site field says. Checked BEFORE the
    # RDC plant-matching below in both the ZingHR and Truein loops; a match
    # here means the employee is written to other_company_employee_rows
    # instead of counted toward RDC at all. Deliberately NOT filtered to
    # is_active=True (fixed 2026-09-24, stakeholder request): a plant being
    # closed/deactivated in admin doesn't mean its former employees stop
    # existing in ZingHR/Truein — excluding closed plants here would have
    # let a real ROBO/Ultrafine employee at a since-closed plant fall
    # through and get miscounted as RDC (or unclassified RDC) instead of
    # correctly attributed to their own company. Only is_deleted (a genuine
    # data-entry mistake being undone) should ever remove a plant from this
    # lookup.
    other_company_plants_by_norm = {
        _normalize_name(p.name): (p.company, p.name)
        for p in PlantLocation.query.filter(
            PlantLocation.company.in_(("ROBO", "Ultrafine")),
            PlantLocation.is_deleted == False).all()
    }
    other_company_employee_rows = []  # dicts backing EmployeeLocationSnapshot(company=...), written alongside RDC's own employee_rows

    plant_mappings = PlantDvtMapping.query.filter_by(is_deleted=False).all()
    plant_name_by_norm = {_normalize_name(p.plant_location_name): p.plant_location_name for p in plant_mappings}
    plant_name_by_truein_sub_site = {
        _normalize_name(p.truein_sub_site): p.plant_location_name
        for p in plant_mappings if p.truein_sub_site
    }
    # Alternate ZingHR Location / Truein sub_site strings that were merged
    # into one canonical plant (see PlantNameAlias) — without these, a
    # ZingHR/Truein record carrying the old, now-merged-away name would go
    # unresolved instead of counting toward the plant it was consolidated
    # into. Aliases never override an exact primary-name/sub_site match.
    for alias in PlantNameAlias.query.all():
        key = _normalize_name(alias.alias_name)
        plant_name_by_norm.setdefault(key, alias.plant.plant_location_name)
        plant_name_by_truein_sub_site.setdefault(key, alias.plant.plant_location_name)

    cluster_mappings = ClusterNameMapping.query.filter_by(is_deleted=False).all()
    cluster_by_zinghr_city = {_normalize_name(c.zinghr_city): c.canonical_cluster_name
                               for c in cluster_mappings if c.zinghr_city}
    cluster_by_truein_category = {_normalize_name(c.truein_category): c.canonical_cluster_name
                                   for c in cluster_mappings if c.truein_category}

    # A category's own fixed scope (PLANT or CLUSTER) — an employee must
    # only be bumped into the bucket matching that scope, never both. All 6
    # department-derived categories are PLANT-scope, so this also keeps
    # CLUSTER-scope unclassified_count meaning what it always meant (nobody
    # in these 6 buckets was ever cluster-scoped).
    cat_scope_by_id = {c.id: c.scope for c in NormRoleCategory.query.all()}
    cat_name_by_id = {c.id: c.name for c in NormRoleCategory.query.all()}
    cat_id_by_name = {name: id_ for id_, name in cat_name_by_id.items()}

    buckets = {}          # (scope, location_key, norm_role_category_id) -> {'zinghr': n, 'truein': n}
    unclassified_by_key = {}  # (scope, location_key) -> count
    employee_rows = []     # one dict per matched employee, backs the dashboard drill-down

    def bump(scope, location_key, norm_role_category_id, source_key):
        key = (scope, location_key, norm_role_category_id)
        b = buckets.setdefault(key, {"zinghr": 0, "truein": 0})
        b[source_key] += 1

    def bump_unclassified(scope, location_key):
        k = (scope, location_key)
        unclassified_by_key[k] = unclassified_by_key.get(k, 0) + 1

    for e in zh_employees:
        # Plant-name company override — see other_company_plants_by_norm's
        # docstring above. A blank/unconfirmed ZingHR Company attribute made
        # it this far (the _NON_RDC_COMPANIES filter only excludes a
        # *confirmed* non-RDC value), so this is the only remaining signal
        # for a genuine Ultrafine/ROBO employee ZingHR didn't tag correctly.
        _other = other_company_plants_by_norm.get(_normalize_name(e.get("Location")))
        if _other:
            _other_company, _other_plant_name = _other
            other_company_employee_rows.append({
                "source": ExternalDesignationSource.ZINGHR,
                "employee_code": e.get("employeeCode"),
                "employee_name": e.get("employeeName"),
                "designation": e.get("Designation"),
                "department": (e.get("Department") or "").strip() or None,
                "date_of_joining": e.get("dateOfJoining"),
                "norm_role_category_id": None,
                "plant_location_key": _other_plant_name,
                "cluster_location_key": None,
                "company": _other_company,
            })
            continue

        department = (e.get("Department") or "").strip()
        date_of_joining = e.get("dateOfJoining")
        norm_cat_id = _classify_by_department(department, e.get("Designation"), date_of_joining, cat_id_by_name)
        cat_scope = cat_scope_by_id.get(norm_cat_id) if norm_cat_id else None

        plant_key = plant_name_by_norm.get(_normalize_name(e.get("Location")))
        if plant_key:
            if norm_cat_id and cat_scope == NormScope.PLANT:
                bump(NormScope.PLANT, plant_key, norm_cat_id, "zinghr")
            elif not norm_cat_id:
                bump_unclassified(NormScope.PLANT, plant_key)

        cluster_key = cluster_by_zinghr_city.get(_normalize_name(e.get("City")))
        if cluster_key:
            if norm_cat_id and cat_scope == NormScope.CLUSTER:
                bump(NormScope.CLUSTER, cluster_key, norm_cat_id, "zinghr")
            elif not norm_cat_id:
                bump_unclassified(NormScope.CLUSTER, cluster_key)

        # Recorded regardless of whether plant_key/cluster_key resolved — an
        # employee with neither must still be visible (e.g. via the "All
        # Employees" directory / designation drill-down) rather than
        # silently vanishing because their Location/City isn't mapped yet.
        employee_rows.append({
            "source": ExternalDesignationSource.ZINGHR,
            "employee_code": e.get("employeeCode"),
            "employee_name": e.get("employeeName"),
            "designation": e.get("Designation"),
            "department": department,
            "date_of_joining": date_of_joining,
            "norm_role_category_id": norm_cat_id,
            "plant_location_key": plant_key,
            "cluster_location_key": cluster_key,
        })

    for e in tr_employees_deduped:
        # Plant-name company override — see other_company_plants_by_norm's
        # docstring above. Every Truein record in this account carries
        # site_name "RDC Concrete"/"RDC Drivers" regardless of the
        # employee's real company (Ultrafine/ROBO were never given their
        # own Truein subscription), so sub_site is the only signal here.
        _other = other_company_plants_by_norm.get(_normalize_name(e.get("sub_site")))
        if _other:
            _other_company, _other_plant_name = _other
            other_company_employee_rows.append({
                "source": ExternalDesignationSource.TRUEIN,
                "employee_code": e.get("empId"),
                "employee_name": e.get("name"),
                "designation": e.get("designation"),
                "department": (e.get("department") or "").strip() or None,
                "date_of_joining": e.get("joining_date"),
                "norm_role_category_id": None,
                "plant_location_key": _other_plant_name,
                "cluster_location_key": None,
                "company": _other_company,
            })
            continue

        department = (e.get("department") or "").strip()
        date_of_joining = e.get("joining_date")
        norm_cat_id = _classify_by_department(department, e.get("designation"), date_of_joining, cat_id_by_name)
        cat_scope = cat_scope_by_id.get(norm_cat_id) if norm_cat_id else None

        # 'sub_site' is the plant-level field for Truein (distinct from
        # 'category', which is cluster-level) — see PlantDvtMapping.truein_sub_site.
        plant_key = plant_name_by_truein_sub_site.get(_normalize_name(e.get("sub_site")))
        cluster_key = cluster_by_truein_category.get(_normalize_name(e.get("category")))

        # Recorded regardless of resolution — see the matching comment in
        # the ZingHR loop above.
        employee_rows.append({
            "source": ExternalDesignationSource.TRUEIN,
            "employee_code": e.get("empId"),
            "employee_name": e.get("name"),
            "designation": e.get("designation"),
            "department": department or None,
            "date_of_joining": date_of_joining,
            "norm_role_category_id": norm_cat_id if cat_scope in (NormScope.PLANT, NormScope.CLUSTER) else None,
            "plant_location_key": plant_key,
            "cluster_location_key": cluster_key,
        })
        if plant_key:
            if norm_cat_id and cat_scope == NormScope.PLANT:
                bump(NormScope.PLANT, plant_key, norm_cat_id, "truein")
            elif not norm_cat_id:
                bump_unclassified(NormScope.PLANT, plant_key)
        if cluster_key:
            if norm_cat_id and cat_scope == NormScope.CLUSTER:
                bump(NormScope.CLUSTER, cluster_key, norm_cat_id, "truein")
            elif not norm_cat_id:
                bump_unclassified(NormScope.CLUSTER, cluster_key)

    # ── Resolve the actual hiring limit for every plant/cluster x role ──
    # One bulk DVT call for the whole run (not per-plant), so this never
    # turns into N live lookups. Rows are written for EVERY mapped plant x
    # every plant-scope category (and every cluster x cluster-scope
    # category) — not just where headcount already exists — so the
    # dashboard can answer "can we hire here?" even for currently-empty
    # roles, not just show historical headcount.
    try:
        # Trailing 3-month average, not a single month's figure — stakeholder
        # rule, 2026-09-24 (see dvt.fetch_all_plants_with_avg_volume() docstring).
        dvt_plants_raw = dvt.fetch_all_plants_with_avg_volume()
    except Exception as exc:
        warnings.append(f"DVT fetch failed: {exc}")
        dvt_plants_raw = []
    volume_by_plant_code = {p.get("plant_code"): p.get("volume", 0.0) for p in dvt_plants_raw if p.get("plant_code")}

    # DVT outage/partial-response fallback — carry forward each missing
    # plant's last known-good volume rather than leaving it (and every
    # role's allowed-headcount) as "Unknown" for this run. Covers both a
    # total fetch failure (dvt_plants_raw == []) and a live response that
    # simply omits some mapped plants.
    _fallback_volumes = get_last_known_plant_volumes()
    _missing_codes = set(_fallback_volumes) - set(volume_by_plant_code)
    if _missing_codes:
        if not dvt_plants_raw:
            warnings.append(f"DVT fetch failed — used last known volume for all {len(_missing_codes)} mapped plant(s)")
        else:
            warnings.append(f"DVT response missing {len(_missing_codes)} mapped plant(s) — used last known volume for those")
        for code in _missing_codes:
            volume_by_plant_code[code] = _fallback_volumes[code]
    # Region-level totals straight from DVT, for the cluster "total m^3
    # produced" display — deliberately includes EVERY plant DVT reports for
    # that region, even ones we haven't matched into a PlantDvtMapping row
    # (or ZingHR/Truein) at all. This is intentionally a different, wider
    # set than cluster_plant_codes below (which stays confirmed-match-only
    # since it drives the plant-count tier / Accounts RATE_PER_VOLUME norm —
    # an unverified name guess must not skew what we're allowed to hire).
    volume_by_region: dict[str, float] = {}
    for p in dvt_plants_raw:
        region = (p.get("region") or "").strip()
        if region:
            volume_by_region[region] = volume_by_region.get(region, 0.0) + (p.get("volume") or 0.0)

    plant_tiers = NormTier.query.filter_by(scope=NormScope.PLANT, sheet=NormSheet.SHEET1, is_active=True).all()
    cluster_tiers = NormTier.query.filter_by(scope=NormScope.CLUSTER, sheet=NormSheet.SHEET1, is_active=True).all()
    # A (tier, category) pair can have more than one requirement row now
    # (see Technical's merged "FTs/LT/TO" formula) — keep every row, summed
    # by _calc_allowed() below, rather than a single dict value that would
    # silently clobber down to just the last-seen row.
    requirement_by_tier_cat = {}
    for r in NormRequirement.query.all():
        requirement_by_tier_cat.setdefault((r.tier_id, r.norm_role_category_id), []).append(r)
    plant_categories = NormRoleCategory.query.filter_by(scope=NormScope.PLANT, sheet=NormSheet.SHEET1, is_active=True).all()
    cluster_categories = NormRoleCategory.query.filter_by(scope=NormScope.CLUSTER, sheet=NormSheet.SHEET1, is_active=True).all()

    # MySQL's DATETIME column only stores second-level precision by default,
    # so two runs within the same second (e.g. the background thread's
    # immediate first-run-at-boot firing right after a manual refresh) would
    # collide and get treated as a single "latest run" by the *_latest_*
    # read functions, doubling every row. Guarantee strictly-increasing
    # timestamps regardless of storage precision.
    now = datetime.utcnow()
    _existing_latest = db.session.query(db.func.max(StaffingSnapshot.computed_at)).scalar()
    if _existing_latest and now <= _existing_latest:
        now = _existing_latest + timedelta(seconds=1)
    warnings_json = json.dumps(warnings) if warnings else None
    written = 0

    for pm in plant_mappings:
        if not pm.dvt_plant_code:
            continue
        volume = volume_by_plant_code.get(pm.dvt_plant_code)
        tier = _find_tier(plant_tiers, volume) if volume is not None else None
        for cat in plant_categories:
            counts = buckets.get((NormScope.PLANT, pm.plant_location_name, cat.id), {"zinghr": 0, "truein": 0})
            current = counts["zinghr"] + counts["truein"]
            if not cat.is_capacity_gated:
                # Never blocked by production volume — always shown as
                # openly hireable, with no numeric cap and no volume basis.
                allowed, tier_label_val, volume_val, can_hire = None, "Not volume-gated", None, True
            else:
                requirements = requirement_by_tier_cat.get((tier.id, cat.id)) if tier else None
                allowed = _calc_allowed(requirements, volume) if (tier and volume is not None) else None
                tier_label_val = tier.tier_label if tier else None
                volume_val = volume
                can_hire = (current < allowed) if allowed is not None else None
            db.session.add(StaffingSnapshot(
                scope=NormScope.PLANT, location_key=pm.plant_location_name, norm_role_category_id=cat.id,
                current_headcount=current, zinghr_count=counts["zinghr"], truein_count=counts["truein"],
                deduped_count=dedup_count,
                unclassified_count=unclassified_by_key.get((NormScope.PLANT, pm.plant_location_name), 0),
                allowed_headcount=allowed, tier_label=tier_label_val, volume_used=volume_val,
                production_volume=volume,
                can_hire=can_hire,
                computed_at=now, source_warnings=warnings_json,
            ))
            written += 1

    for cm in cluster_mappings:
        # Only plants confidently matched to a DVT identity count toward the
        # cluster's plant-count tier and total volume — an AUTO_FUZZY/
        # UNMATCHED row is an unverified name guess (see
        # get_plants_in_cluster()/_CONFIRMED_MATCH_CONFIDENCE, which already
        # hides these from the dashboard's plant list). Without this filter
        # a duplicate/fuzzy row sharing a DVT code with an already-confirmed
        # plant double-counts that plant's volume, and extra unconfirmed
        # plants inflate the plant-count tier — both silently skew "can we
        # hire here?" even though the plant itself is invisible in the UI.
        cluster_plant_codes = [p.dvt_plant_code for p in plant_mappings
                                if p.cluster_id == cm.id and p.dvt_plant_code
                                and p.match_confidence in _CONFIRMED_MATCH_CONFIDENCE]
        plant_count = len(cluster_plant_codes)
        tier = _find_tier(cluster_tiers, plant_count)
        cluster_total_volume = sum(volume_by_plant_code.get(c, 0.0) for c in cluster_plant_codes)
        # For the "total m^3 produced" DISPLAY figure only, use every plant
        # DVT reports under this region — not just the ones we've confirmed
        # a match for — per the stakeholder's request: a plant's production
        # should count toward the region total even if it's never been
        # reconciled into ZingHR/Truein/our plant mapping. Falls back to the
        # confirmed-only total if this cluster has no dvt_region link yet
        # (e.g. never auto-matched), which is the best we can say in that case.
        cluster_display_volume = volume_by_region.get(cm.dvt_region, cluster_total_volume) if cm.dvt_region else cluster_total_volume
        for cat in cluster_categories:
            counts = buckets.get((NormScope.CLUSTER, cm.canonical_cluster_name, cat.id), {"zinghr": 0, "truein": 0})
            requirements = requirement_by_tier_cat.get((tier.id, cat.id)) if tier else None
            has_rate = any(r.requirement_type == NormRequirementType.RATE_PER_VOLUME for r in (requirements or []))
            basis = cluster_total_volume if has_rate else plant_count
            allowed = _calc_allowed(requirements, basis) if tier else None
            current = counts["zinghr"] + counts["truein"]
            db.session.add(StaffingSnapshot(
                scope=NormScope.CLUSTER, location_key=cm.canonical_cluster_name, norm_role_category_id=cat.id,
                current_headcount=current, zinghr_count=counts["zinghr"], truein_count=counts["truein"],
                deduped_count=dedup_count,
                unclassified_count=unclassified_by_key.get((NormScope.CLUSTER, cm.canonical_cluster_name), 0),
                allowed_headcount=allowed, tier_label=(tier.tier_label if tier else None), volume_used=basis,
                production_volume=cluster_display_volume,
                can_hire=(current < allowed) if allowed is not None else None,
                computed_at=now, source_warnings=warnings_json,
            ))
            written += 1

    for row in employee_rows:
        db.session.add(EmployeeLocationSnapshot(computed_at=now, **row))

    # Employees reclassified to Ultrafine/ROBO by plant name (see
    # other_company_plants_by_norm above) — written with the exact same
    # `now` as everything else in this run, alongside (not instead of) the
    # ZingHR-sourced other-company pass compute_and_store_snapshot() runs
    # right after this function returns. Never counted toward RDC above.
    for row in other_company_employee_rows:
        db.session.add(EmployeeLocationSnapshot(computed_at=now, **row))

    db.session.commit()
    return {
        "snapshots_written": written,
        "employee_rows_written": len(employee_rows),
        "other_company_employee_rows_written": len(other_company_employee_rows),
        "zinghr_employees": len(zh_employees),
        "truein_employees_considered": len(tr_employees_deduped),
        "deduped_count": dedup_count,
        "unclassified_total": sum(unclassified_by_key.values()),
        "warnings": warnings,
        "computed_at": now.isoformat(),
    }


# ZingHR's raw Company attribute value (lowercased) -> our internal company
# code — the exact inverse of the _NON_RDC_COMPANIES exclusion set above.
# Added 2026-09-15, multi-company support.
_ZINGHR_COMPANY_TO_CODE = {
    "robo silicon pvt. ltd.": "ROBO",
    "ultrafine mineral and admixtures pvt ltd": "Ultrafine",
}


def _compute_and_store_other_company_snapshot(now=None) -> dict:
    """
    Lightweight, independent headcount snapshot for Ultrafine/ROBO (added
    2026-09-15, multi-company support) — deliberately NOT woven into
    _compute_and_store_snapshot() above, to avoid any risk to the
    carefully-tuned RDC-specific reconciliation logic it already has. No
    production-volume gating exists for these companies (see
    COMPANY_CHOICES / PlantLocation.company in models.py), so this only
    needs "who works where," not "is that allowed" — no
    NormRequirement/NormTier/DVT involvement at all.

    ZingHR-only in THIS function specifically — but this is no longer the
    complete picture for Ultrafine/ROBO headcount (revised 2026-09-23).
    Truein has no Ultrafine/ROBO concept at all in this account (every
    record's site_name reads "RDC Concrete"/"RDC Drivers" regardless of the
    employee's real company), so a real Ultrafine/ROBO employee tracked via
    Truein could never be caught here. That gap is now closed on the OTHER
    side: _compute_and_store_snapshot() (the RDC pass, called right before
    this one) checks every ZingHR/Truein employee's raw Location/sub_site
    against other_company_plants_by_norm (this same PlantLocation.company
    IN ('ROBO','Ultrafine') table) BEFORE counting them toward RDC, and
    writes any match here as its own EmployeeLocationSnapshot(company=...)
    row using this run's exact `now` — see that function's docstring. This
    function still only pulls ZingHR (nothing changed in its own logic),
    but the two passes together now cover both source systems.

    Reuses zinghr.fetch_active_employees()'s own 1h cache (no extra live
    API call — see zinghr._fetch_all_employees_raw()) and this module's own
    _is_face_device()/_normalize_name() helpers for consistency with the
    RDC pipeline, but resolves each employee to a company-scoped
    PlantLocation row (never PlantDvtMapping — that table is RDC/DVT-
    specific and these companies never get rows there) by normalized
    ZingHR Location match. An employee whose Location doesn't match any
    known plant for their company is still written (plant_location_key=None)
    rather than silently dropped, same "surface it as Unresolved, don't
    hide it" convention the RDC pipeline already uses.

    `now`: every read-side helper in this module (get_all_latest_snapshots,
    get_employees_at_plant, get_all_employees, ...) finds "the current run"
    via a plain MAX(computed_at) across the WHOLE EmployeeLocationSnapshot
    table — company-agnostic. compute_and_store_snapshot() calls this right
    after _compute_and_store_snapshot() in the same refresh cycle and passes
    that call's own resulting timestamp in here explicitly — if this
    function generated its own, later timestamp instead, MAX(computed_at)
    would silently become THIS company's run and every RDC read-side query
    would find zero matching rows. Defaults to a fresh timestamp only for
    standalone/test calls that don't care about that interaction.

    Caller commits — this only adds rows to the session, inside the same
    advisory-lock critical section and 2-hourly refresh cadence as the RDC
    computation right before it.
    """
    zh_employees_raw = zinghr.fetch_active_employees()

    # Deliberately not filtered to is_active=True (fixed 2026-09-24,
    # stakeholder request): a closed/inactive plant can still have real
    # employees on it in ZingHR (closure lags separation) — excluding it
    # here silently dropped them into "Unresolved" instead of correctly
    # counting them against their actual plant. Only is_deleted removes a
    # plant from this lookup; get_other_company_plant_summary() below is
    # what decides whether a closed plant is still shown/labeled.
    plants_by_company: dict[str, dict[str, str]] = {}
    for p in (PlantLocation.query
              .filter_by(is_deleted=False)
              .filter(PlantLocation.company.in_(_ZINGHR_COMPANY_TO_CODE.values()))
              .all()):
        plants_by_company.setdefault(p.company, {})[_normalize_name(p.name)] = p.name

    now = now or datetime.utcnow()
    written_by_company = {code: 0 for code in _ZINGHR_COMPANY_TO_CODE.values()}
    for e in zh_employees_raw:
        code = _ZINGHR_COMPANY_TO_CODE.get((e.get("Company") or "").strip().lower())
        if not code:
            continue
        if _is_face_device(e.get("employeeCode"), e.get("employeeName")):
            continue
        plant_key = plants_by_company.get(code, {}).get(_normalize_name(e.get("Location")))
        db.session.add(EmployeeLocationSnapshot(
            computed_at=now,
            source=ExternalDesignationSource.ZINGHR,
            employee_code=e.get("employeeCode"),
            employee_name=e.get("employeeName"),
            designation=e.get("Designation"),
            department=(e.get("Department") or "").strip() or None,
            date_of_joining=e.get("dateOfJoining"),
            norm_role_category_id=None,
            plant_location_key=plant_key,
            cluster_location_key=None,
            company=code,
        ))
        written_by_company[code] += 1

    return {"employee_rows_written": sum(written_by_company.values()), "by_company": written_by_company}


def get_latest_snapshot(location_key: str, scope, norm_role_category_id: int):
    """
    Returns the StaffingSnapshot row for this key from the most recent
    completed run, or a zero-count (unsaved, transient) StaffingSnapshot if
    that run had nobody at this key — this table is sparse (only non-zero
    buckets get a row per run), so "no row for this run" must read as zero,
    never silently fall back to an older run's possibly-stale nonzero count.
    Returns None only if no snapshot run has ever completed (the background
    job hasn't run yet) — callers should treat that as a distinguishable
    "no data" / ERROR state, not as zero.
    """
    latest_run = db.session.query(db.func.max(StaffingSnapshot.computed_at)).scalar()
    if not latest_run:
        return None
    row = StaffingSnapshot.query.filter_by(
        location_key=location_key, scope=scope, norm_role_category_id=norm_role_category_id,
        computed_at=latest_run,
    ).first()
    if row:
        return row
    return StaffingSnapshot(
        scope=scope, location_key=location_key, norm_role_category_id=norm_role_category_id,
        current_headcount=0, zinghr_count=0, truein_count=0, deduped_count=0,
        unclassified_count=0, computed_at=latest_run,
    )


def get_snapshot_rows_for_location(location_key: str, scope, latest_run=None) -> list:
    """All role-level StaffingSnapshot rows for one plant or cluster, from the
    latest run — the "can we hire here?" table on the plant/cluster detail pages.
    `latest_run` can be passed in by a caller that already resolved it once
    (e.g. looping over many locations in one report) to avoid re-running the
    MAX(computed_at) scan per location — see staffing_status_download()."""
    if latest_run is None:
        latest_run = db.session.query(db.func.max(StaffingSnapshot.computed_at)).scalar()
    if not latest_run:
        return []
    return (StaffingSnapshot.query
            .filter_by(location_key=location_key, scope=scope, computed_at=latest_run)
            .join(StaffingSnapshot.norm_role_category)
            .order_by(NormRoleCategory.sort_order)
            .all())


def get_all_latest_snapshots(scope=None) -> list:
    """All rows from the most recently completed snapshot run (for the dashboard)."""
    latest_run = db.session.query(db.func.max(StaffingSnapshot.computed_at)).scalar()
    if not latest_run:
        return []
    q = StaffingSnapshot.query.filter_by(computed_at=latest_run)
    if scope:
        q = q.filter_by(scope=scope)
    return q.order_by(StaffingSnapshot.location_key).all()


def get_employees_at_plant(plant_name: str, latest_run=None) -> list:
    """
    Real employee list (name, designation, department, ...) for one RDC
    plant, from the latest run. `company.is_(None)` (added 2026-09-15):
    RDC rows never set `company` (only _compute_and_store_other_company_snapshot()
    does, for Ultrafine/ROBO) — without this filter, once both companies
    share this table and the same computed_at (see that function's `now`
    docstring), a plant name collision between companies would mix a
    different company's employees into this RDC-only view.

    `latest_run` can be passed in by a caller that already resolved it once
    (e.g. looping over many plants in one report) to avoid re-running the
    MAX(computed_at) scan per plant — see staffing_status_download().
    """
    if latest_run is None:
        latest_run = db.session.query(db.func.max(EmployeeLocationSnapshot.computed_at)).scalar()
    if not latest_run:
        return []
    return (EmployeeLocationSnapshot.query
            .filter_by(plant_location_key=plant_name, computed_at=latest_run)
            .filter(EmployeeLocationSnapshot.company.is_(None))
            .order_by(EmployeeLocationSnapshot.employee_name)
            .all())


def get_employees_at_cluster(cluster_name: str, unassigned_to_plant_only: bool = False, latest_run=None) -> list:
    """
    Real employee list for a cluster. With unassigned_to_plant_only=True,
    returns only employees resolved to this cluster but NOT to any specific
    plant within it (e.g. regional/HQ roles — Accounts, Credit Control) —
    used on the cluster detail page's "Cluster-level staff" section, since
    plant-level staff are already shown when you drill into their plant.

    `latest_run` — see get_employees_at_plant()'s docstring.
    """
    if latest_run is None:
        latest_run = db.session.query(db.func.max(EmployeeLocationSnapshot.computed_at)).scalar()
    if not latest_run:
        return []
    # company.is_(None): see get_employees_at_plant()'s docstring — Ultrafine/
    # ROBO rows never set cluster_location_key (they have no cluster
    # concept, always None), so they can't actually match this query's
    # cluster_name filter, but the explicit check is kept for consistency
    # and to stay correct if that ever changes.
    q = (EmployeeLocationSnapshot.query
         .filter_by(cluster_location_key=cluster_name, computed_at=latest_run)
         .filter(EmployeeLocationSnapshot.company.is_(None)))
    if unassigned_to_plant_only:
        q = q.filter(EmployeeLocationSnapshot.plant_location_key.is_(None))
    return q.order_by(EmployeeLocationSnapshot.employee_name).all()


_EMPLOYEE_DIRECTORY_LIMIT = 500


def get_all_employees(source=None, designation=None, department=None, resolved=None, search=None,
                       cluster_names=None) -> tuple[list, int]:
    """
    Full employee directory from the latest run — every ZingHR/Truein
    employee, including anyone not yet resolved to a plant or cluster (so
    an admin can see exactly who's currently invisible everywhere else,
    not just the ones with a known location). Filters combine with AND;
    `resolved` is 'yes' / 'no' / None (any). `cluster_names`, if given,
    restricts to employees whose cluster_location_key is in that set — used
    to region-scope a Business Head's view. Returns (rows, total_count) —
    rows capped at _EMPLOYEE_DIRECTORY_LIMIT so an unfiltered query of a
    few thousand employees doesn't render an enormous table. Scoped to RDC
    only (`company.is_(None)`, added 2026-09-15) — this is the "All
    Employees" directory used throughout the RDC dashboard, and without
    this filter Ultrafine/ROBO employees (which now share this table, see
    get_employees_at_plant()'s docstring) would leak into it.
    """
    latest_run = db.session.query(db.func.max(EmployeeLocationSnapshot.computed_at)).scalar()
    if not latest_run:
        return [], 0
    q = (EmployeeLocationSnapshot.query
         .filter_by(computed_at=latest_run)
         .filter(EmployeeLocationSnapshot.company.is_(None)))
    if source:
        q = q.filter(EmployeeLocationSnapshot.source == source)
    if designation:
        q = q.filter(EmployeeLocationSnapshot.designation == designation)
    if department:
        q = q.filter(EmployeeLocationSnapshot.department == department)
    if cluster_names is not None:
        q = q.filter(EmployeeLocationSnapshot.cluster_location_key.in_(cluster_names))
    if resolved == "yes":
        q = q.filter(db.or_(EmployeeLocationSnapshot.plant_location_key.isnot(None),
                             EmployeeLocationSnapshot.cluster_location_key.isnot(None)))
    elif resolved == "no":
        q = q.filter(EmployeeLocationSnapshot.plant_location_key.is_(None),
                      EmployeeLocationSnapshot.cluster_location_key.is_(None))
    if search:
        like = f"%{search}%"
        q = q.filter(db.or_(EmployeeLocationSnapshot.employee_name.ilike(like),
                             EmployeeLocationSnapshot.employee_code.ilike(like)))
    total = q.count()
    rows = q.order_by(EmployeeLocationSnapshot.employee_name).limit(_EMPLOYEE_DIRECTORY_LIMIT).all()
    return rows, total


def get_rdc_unmapped_employees(exclude_ids: set, latest_run=None) -> list:
    """
    RDC employees (`company.is_(None)`) from the latest run that a caller's
    own By Cluster/By Plant/By Employees traversal didn't already surface
    (`exclude_ids` — the `EmployeeLocationSnapshot.id`s already emitted by
    that traversal). In practice this is anyone whose location doesn't
    resolve to a plant this app currently trusts: no `plant_location_key`
    at all, or one set to a raw string with no CONFIRMED (AUTO_EXACT/MANUAL)
    `PlantDvtMapping` row — an UNMATCHED/AUTO_FUZZY guess, a plant that's
    been closed/decommissioned and dropped out of DVT's own list, or a
    non-plant string like "MUM-Area Office" that was never meant to map to
    a plant at all.

    Added 2026-09-24 (stakeholder request). `staffing_status_download()`'s
    By Plant/By Employees sheets only ever traverse
    `get_plants_in_cluster()`'s CONFIRMED plants — deliberately, so an
    unverified plant-name guess never shows fabricated volume/tier/allowed-
    headcount data (see that function's docstring, and gotcha #13 in
    CLAUDE.md). That's still correct for tier/hiring-gate data, but it also
    meant a real employee at any unconfirmed or closed plant was completely
    invisible in the download, with no way to even know they existed — a
    live check (2026-09-24) found ~26 distinct raw location strings this
    way, several hundred real employees combined. This function is pure
    headcount visibility, no tier/volume/hiring-gate implication at all, so
    it's safe to surface regardless of match confidence — the RDC
    equivalent of `get_other_company_unresolved_count()` for Ultrafine/ROBO,
    added the same day for the same reason.

    `exclude_ids` is required, not inferred from match-confidence alone,
    because the caller's own traversal is ALREADY correctly region/company
    scoped (a Business Head only sees their own clusters) and already
    covers "resolved to a cluster but not a specific plant" — recomputing
    that scoping independently here would either duplicate rows already
    shown, or leak an out-of-scope employee into a report the viewer isn't
    supposed to see the rest of. Passing the exact set of ids the caller
    already emitted keeps this function correct by construction instead of
    by parallel, driftable logic.

    `latest_run` — see get_employees_at_plant()'s docstring.
    """
    if latest_run is None:
        latest_run = db.session.query(db.func.max(EmployeeLocationSnapshot.computed_at)).scalar()
    if not latest_run:
        return []
    rows = (EmployeeLocationSnapshot.query
            .filter_by(computed_at=latest_run)
            .filter(EmployeeLocationSnapshot.company.is_(None))
            .order_by(EmployeeLocationSnapshot.employee_name)
            .all())
    return [e for e in rows if e.id not in exclude_ids]


# ── Ultrafine/ROBO — simple headcount views (added 2026-09-15) ────────────────
# No production-volume gating, no DVT, no role-category classification for
# these two companies (see COMPANY_CHOICES / PlantLocation.company) — these
# are deliberately the "who works where" half only, mirroring the RDC read
# functions above in shape but always explicitly scoped to one company
# rather than to `company.is_(None)`.

def get_other_company_plant_summary(company: str) -> list[dict]:
    """
    One row per PlantLocation for `company` — active AND closed/inactive
    (changed 2026-09-24, stakeholder request) — with its current headcount
    from the latest EmployeeLocationSnapshot run — the data source for that
    company's tab on the Staffing Status page. A plant with zero matched
    employees still appears, with headcount 0 (visible, not hidden, same as
    RDC's own convention for a plant with no snapshot data yet).

    Closed plants are deliberately still included: a plant being marked
    inactive in admin doesn't mean its former employees have all left —
    hiding the plant here used to make their headcount silently vanish from
    every report instead of still being attributable to the plant they're
    actually on. `is_active` is carried through per row so callers can
    label a closed plant instead of pretending it's indistinguishable from
    an open one.
    """
    plants = (PlantLocation.query
              .filter_by(is_deleted=False, company=company)
              .order_by(PlantLocation.is_active.desc(), PlantLocation.sort_order, PlantLocation.name)
              .all())
    latest_run = db.session.query(db.func.max(EmployeeLocationSnapshot.computed_at)).scalar()
    counts = {}
    if latest_run:
        rows = (db.session.query(EmployeeLocationSnapshot.plant_location_key, db.func.count())
                .filter(EmployeeLocationSnapshot.company == company,
                        EmployeeLocationSnapshot.computed_at == latest_run)
                .group_by(EmployeeLocationSnapshot.plant_location_key)
                .all())
        counts = {key: n for key, n in rows}
    return [{"plant": p, "headcount": counts.get(p.name, 0)} for p in plants]


def get_other_company_unresolved_count(company: str) -> int:
    """Employees of `company` whose ZingHR Location didn't match any of that company's plants."""
    latest_run = db.session.query(db.func.max(EmployeeLocationSnapshot.computed_at)).scalar()
    if not latest_run:
        return 0
    return (EmployeeLocationSnapshot.query
            .filter_by(company=company, plant_location_key=None, computed_at=latest_run)
            .count())


def get_other_company_employees_at_plant(company: str, plant_name: str) -> list:
    """Real employee list for one Ultrafine/ROBO plant, from the latest run."""
    latest_run = db.session.query(db.func.max(EmployeeLocationSnapshot.computed_at)).scalar()
    if not latest_run:
        return []
    return (EmployeeLocationSnapshot.query
            .filter_by(company=company, plant_location_key=plant_name, computed_at=latest_run)
            .order_by(EmployeeLocationSnapshot.employee_name)
            .all())


_CONFIRMED_MATCH_CONFIDENCE = (MatchConfidence.AUTO_EXACT, MatchConfidence.MANUAL)


def get_plants_in_cluster(cluster_id: int) -> list:
    """
    PlantDvtMapping rows belonging to a cluster — current admin-managed
    mapping, not snapshot-dependent. Only plants confidently matched to a
    DVT/ERP identity (AUTO_EXACT or admin-confirmed MANUAL) are returned —
    AUTO_FUZZY/UNMATCHED rows are unverified name guesses and stay hidden
    from the dashboard until confirmed, per stakeholder request, rather
    than showing a plant name that might be wrong.
    """
    return (PlantDvtMapping.query
            .filter_by(cluster_id=cluster_id, is_deleted=False)
            .filter(PlantDvtMapping.match_confidence.in_(_CONFIRMED_MATCH_CONFIDENCE))
            .order_by(PlantDvtMapping.plant_location_name)
            .all())


