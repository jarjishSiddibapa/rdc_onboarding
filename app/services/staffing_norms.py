"""
RDC staffing-norms gate evaluation.

Pure logic + DB reads (NormTier/NormRequirement config, and the cached
StaffingSnapshot table via app/services/headcount.py) plus a live call to
the Daily Volume Tracker for the plant/cluster's production volume — never
calls ZingHR/Truein live (that only happens in the 2-hourly background job).

This is what app/requests_bp/routes.py::submit_request()/resubmit_request()
call when form_data['company_code'] == 'RDC', right before the
DRAFT -> PENDING_BH transition.
"""
from ..models import (
    NormTier, NormRequirement, NormScope, NormSheet, NormRequirementType,
    PlantDvtMapping, Designation, GateResult,
)
from ..integrations import dvt
from . import headcount

# Per the plan's Open Items: fail-open (allow the hire through) whenever the
# gate can't reach a confident answer — missing mapping, DVT unreachable, no
# snapshot yet, etc. — rather than fail-closed (block). This is flagged as a
# business decision the stakeholder should confirm; fail-open was the
# recommended default since blocking hires on an infrastructure hiccup is
# more disruptive than occasionally letting one through unchecked.
_FAIL_OPEN_REASONS = {
    "not_covered", "plant_not_mapped", "cluster_not_mapped", "no_volume_data",
    "no_tier_for_volume", "requirement_not_computable", "no_snapshot_yet",
    "per_business_head_not_supported", "error",
}


def _plant_volume_or_fallback(dvt_plant_code: str) -> float | None:
    """
    Live DVT volume lookup for one plant, falling back to the last
    known-good volume (from cached StaffingSnapshot data — see
    headcount.get_last_known_plant_volumes()) if the live call fails or
    raises, e.g. a transient DVT network timeout (confirmed happening
    repeatedly against the real DVT server during testing). A stale-but-real
    number is far better than silently skipping a genuine capacity block —
    only returns None if there's truly no volume available either live or
    cached, in which case the caller reports "no_volume_data" and fails
    open as before.
    """
    try:
        volume = dvt.get_plant_volume(dvt_plant_code)
        if volume is not None:
            return volume
    except Exception:
        pass
    return headcount.get_last_known_plant_volumes().get(dvt_plant_code)


def _cluster_volume_or_fallback(plant_codes: list) -> float:
    """Same fallback philosophy as _plant_volume_or_fallback(), summed across a cluster's plants."""
    try:
        return dvt.get_cluster_total_volume(plant_codes)
    except Exception:
        fallback = headcount.get_last_known_plant_volumes()
        return sum(fallback.get(c, 0.0) for c in plant_codes)


def _find_tier(scope: NormScope, sheet: NormSheet, value: float):
    tiers = NormTier.query.filter_by(scope=scope, sheet=sheet, is_active=True).all()
    for t in tiers:
        lo_ok = t.min_value is None or value >= t.min_value
        hi_ok = t.max_value is None or value < t.max_value
        if lo_ok and hi_ok:
            return t
    return None


def get_tier_for_plant(plant_name: str, sheet: NormSheet = NormSheet.SHEET1):
    """Resolve last month's DVT volume for plant_name and find the matching NormTier. None if unresolvable."""
    mapping = PlantDvtMapping.query.filter_by(plant_location_name=plant_name, is_deleted=False).first()
    if not mapping or not mapping.dvt_plant_code:
        return None
    volume = dvt.get_plant_volume(mapping.dvt_plant_code)
    if volume is None:
        return None
    return _find_tier(NormScope.PLANT, sheet, volume)


def get_tier_for_cluster(cluster_id: int, sheet: NormSheet = NormSheet.SHEET1):
    """Resolve plant count in the cluster and find the matching NormTier. None if unresolvable."""
    plant_count = PlantDvtMapping.query.filter_by(
        cluster_id=cluster_id, is_deleted=False
    ).filter(PlantDvtMapping.dvt_plant_code.isnot(None)).count()
    return _find_tier(NormScope.CLUSTER, sheet, plant_count)


def compute_allowed_headcount(requirement: NormRequirement, basis_value: float,
                               business_head_count: int | None = None) -> int | None:
    """
    FIXED              -> fixed_count
    RATE_PER_VOLUME     -> round(basis_value / unit_volume * rate_per_unit), rounded to nearest
    PER_BUSINESS_HEAD    -> business_head_count (None if not supplied -> caller must skip the gate for this role)
    NONE                -> 0
    Returns None if requirement is missing or malformed (can't compute).
    """
    if requirement is None:
        return None
    if requirement.requirement_type == NormRequirementType.FIXED:
        return requirement.fixed_count
    if requirement.requirement_type == NormRequirementType.RATE_PER_VOLUME:
        if not requirement.unit_volume:
            return None
        return round(basis_value / requirement.unit_volume * (requirement.rate_per_unit or 1))
    if requirement.requirement_type == NormRequirementType.PER_BUSINESS_HEAD:
        return business_head_count
    if requirement.requirement_type == NormRequirementType.NONE:
        return 0
    return None


def sum_allowed_headcount(requirements: list, basis_value: float,
                           business_head_count: int | None = None) -> int | None:
    """
    Sums compute_allowed_headcount() across every NormRequirement row for a
    (tier, category) pair — a category can now have more than one row (e.g.
    "Technical" absorbed the old "FTs/LT/TO" formula: fixed-1 PLUS a
    rate-per-volume component for the same tier). None if there are no rows,
    or any single one can't be computed — stays conservative rather than
    silently under-counting the cap.
    """
    if not requirements:
        return None
    total = 0
    for r in requirements:
        one = compute_allowed_headcount(r, basis_value, business_head_count)
        if one is None:
            return None
        total += one
    return total


def _result(allowed: bool, reason: str, details: dict) -> dict:
    return {"allowed": allowed, "reason": reason, "details": details}


def check_rdc_staffing_gate(form_data: dict) -> dict:
    """
    Evaluate the RDC staffing gate against a plain form_data dict (designation,
    plant_location, company_code) — takes a dict rather than an
    OnboardingRequest so this same logic can be reused for a live pre-check
    while the initiator is still filling the form, not just at Submit. Only
    meaningful when form_data['company_code'] == 'RDC' — callers are expected
    to check that themselves before calling (kept out of this function so it
    can be unit tested independent of the company check).

    Never raises. Returns {'allowed': bool, 'reason': str, 'details': {...}}.
    'details' is JSON-serializable and is what gets persisted to
    StaffingGateCheck.detail / shown on the "Hiring Not Possible" page.
    """
    try:
        fd = form_data
        designation_name = (fd.get("designation") or "").strip()
        plant_name = (fd.get("plant_location") or "").strip()

        designation = Designation.query.filter_by(name=designation_name, is_deleted=False).first()
        if not designation or not designation.norm_category_id:
            return _result(True, "not_covered", {
                "message": "Designation is not covered by RDC staffing norms.",
                "designation": designation_name,
            })

        norm_category = designation.norm_category

        if not norm_category.is_capacity_gated:
            return _result(True, "not_capacity_gated", {
                "message": "This role isn't limited by production volume.",
                "norm_role_category_id": norm_category.id, "norm_role_category_name": norm_category.name,
            })

        if norm_category.scope == NormScope.PLANT:
            mapping = PlantDvtMapping.query.filter_by(plant_location_name=plant_name, is_deleted=False).first()
            if not mapping or not mapping.dvt_plant_code:
                return _result(True, "plant_not_mapped", {
                    "message": "This plant isn't mapped to a Daily Volume Tracker plant code yet.",
                    "plant_name": plant_name,
                })

            volume = _plant_volume_or_fallback(mapping.dvt_plant_code)
            if volume is None:
                return _result(True, "no_volume_data", {
                    "message": "No production volume data available for this plant last month.",
                    "plant_name": plant_name, "dvt_plant_code": mapping.dvt_plant_code,
                })

            tier = _find_tier(NormScope.PLANT, NormSheet.SHEET1, volume)
            if not tier:
                return _result(True, "no_tier_for_volume", {
                    "message": "No staffing-norm tier configured for this volume.",
                    "plant_name": plant_name, "volume_used": volume,
                })

            requirements = NormRequirement.query.filter_by(
                tier_id=tier.id, norm_role_category_id=norm_category.id
            ).all()
            allowed = sum_allowed_headcount(requirements, volume)
            if allowed is None:
                return _result(True, "requirement_not_computable", {
                    "message": "Staffing norm for this role/tier isn't fully configured.",
                    "plant_name": plant_name, "tier_label": tier.tier_label,
                })

            snapshot = headcount.get_latest_snapshot(plant_name, NormScope.PLANT, norm_category.id)
            if snapshot is None:
                return _result(True, "no_snapshot_yet", {
                    "message": "Headcount snapshot hasn't run yet.",
                    "plant_name": plant_name,
                })

            current = snapshot.current_headcount
            would_be = current + 1
            is_allowed = would_be <= allowed
            requirement_type_val = ",".join(sorted({r.requirement_type.value for r in requirements})) if requirements else None
            return _result(is_allowed, "ok" if is_allowed else "at_or_over_norm", {
                "scope": "PLANT", "plant_name": plant_name, "cluster_name": None,
                "norm_role_category_id": norm_category.id, "norm_role_category_name": norm_category.name,
                "tier_label": tier.tier_label, "volume_used": volume,
                "requirement_type": requirement_type_val,
                "current_headcount": current, "allowed_headcount": allowed, "would_be_headcount": would_be,
                "zinghr_count": snapshot.zinghr_count, "truein_count": snapshot.truein_count,
                "unclassified_count": snapshot.unclassified_count,
                "snapshot_computed_at": snapshot.computed_at.isoformat() if snapshot.computed_at else None,
            })

        # CLUSTER scope
        plant_mapping = PlantDvtMapping.query.filter_by(plant_location_name=plant_name, is_deleted=False).first()
        if not plant_mapping or not plant_mapping.cluster_id:
            return _result(True, "cluster_not_mapped", {
                "message": "This plant isn't mapped to a cluster yet.",
                "plant_name": plant_name,
            })

        cluster = plant_mapping.cluster
        cluster_plants = PlantDvtMapping.query.filter_by(cluster_id=cluster.id, is_deleted=False).all()
        plant_codes = [p.dvt_plant_code for p in cluster_plants if p.dvt_plant_code]
        plant_count = len(plant_codes)

        tier = _find_tier(NormScope.CLUSTER, NormSheet.SHEET1, plant_count)
        if not tier:
            return _result(True, "no_tier_for_volume", {
                "message": "No staffing-norm tier configured for this cluster's plant count.",
                "cluster_name": cluster.canonical_cluster_name, "plant_count": plant_count,
            })

        requirements = NormRequirement.query.filter_by(
            tier_id=tier.id, norm_role_category_id=norm_category.id
        ).all()

        if any(r.requirement_type == NormRequirementType.PER_BUSINESS_HEAD for r in requirements):
            # Open item: no "how many Business Heads in this cluster" input exists yet — skip gate for this role.
            return _result(True, "per_business_head_not_supported", {
                "message": "This role's norm is per-Business-Head, which isn't tracked yet — gate skipped.",
                "cluster_name": cluster.canonical_cluster_name,
            })

        if any(r.requirement_type == NormRequirementType.RATE_PER_VOLUME for r in requirements):
            basis_value = _cluster_volume_or_fallback(plant_codes)
        else:
            basis_value = plant_count

        allowed = sum_allowed_headcount(requirements, basis_value)
        if allowed is None:
            return _result(True, "requirement_not_computable", {
                "message": "Staffing norm for this role/tier isn't fully configured.",
                "cluster_name": cluster.canonical_cluster_name, "tier_label": tier.tier_label,
            })

        snapshot = headcount.get_latest_snapshot(cluster.canonical_cluster_name, NormScope.CLUSTER, norm_category.id)
        if snapshot is None:
            return _result(True, "no_snapshot_yet", {
                "message": "Headcount snapshot hasn't run yet.",
                "cluster_name": cluster.canonical_cluster_name,
            })

        current = snapshot.current_headcount
        would_be = current + 1
        is_allowed = would_be <= allowed
        return _result(is_allowed, "ok" if is_allowed else "at_or_over_norm", {
            "scope": "CLUSTER", "plant_name": plant_name, "cluster_name": cluster.canonical_cluster_name,
            "norm_role_category_id": norm_category.id, "norm_role_category_name": norm_category.name,
            "tier_label": tier.tier_label, "plant_count": plant_count, "volume_used": basis_value,
            # volume_used above is really "the basis used for this role's
            # requirement" — real m^3 only when requirement_type is
            # RATE_PER_VOLUME (e.g. Accounts). For FIXED/NONE/PER_BUSINESS_HEAD
            # cluster roles it's plant_count, since a cluster's tier is
            # plant-count-based. Consumers must check this before labeling
            # volume_used as "m^3" — see hiring_not_possible.html.
            "requirement_type": ",".join(sorted({r.requirement_type.value for r in requirements})) if requirements else None,
            "current_headcount": current, "allowed_headcount": allowed, "would_be_headcount": would_be,
            "zinghr_count": snapshot.zinghr_count, "truein_count": snapshot.truein_count,
            "unclassified_count": snapshot.unclassified_count,
            "snapshot_computed_at": snapshot.computed_at.isoformat() if snapshot.computed_at else None,
        })

    except Exception as exc:
        # Fail-open on any unexpected error — see _FAIL_OPEN_REASONS note above.
        return _result(True, "error", {"message": str(exc)})
