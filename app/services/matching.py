"""
Cross-system name reconciliation.
=================================
Our own form's plant list (populated live from Truein), the Daily Volume
Tracker's plant_code/region, ZingHR's City, and Truein's category field all
name the same real-world plants/cities differently. These functions attempt
an automatic best-effort match (RapidFuzz) and upsert the mapping tables;
anything that doesn't match cleanly is left for an admin to fix by hand via
the /admin/plant-mappings and /admin/cluster-mappings screens.

Never overwrites a row an admin has already manually corrected
(match_confidence == MANUAL).
"""
import re

from rapidfuzz import fuzz

from ..extensions import db
from ..models import (
    PlantDvtMapping, PlantNameAlias, ClusterNameMapping,
    MatchConfidence, PlantLocation,
)
from ..integrations import truein, zinghr, dvt

_TRUEIN_JUNK_CATEGORIES = {"on roll", "onroll", "other", "1st", "2nd"}
_FUZZY_MATCH_THRESHOLD = 80  # RapidFuzz token_sort_ratio, 0-100


def _normalize(name: str) -> str:
    """Collapse hyphen/whitespace inconsistencies (e.g. 'ULT - Nagpur' vs 'ULT-Nagpur')."""
    return re.sub(r"[\s\-_./]+", " ", (name or "")).strip().lower()


def _our_plant_names() -> list[str]:
    """
    Union of admin-managed PlantLocation names, Truein's sub_site values,
    and ZingHR's Location values (the plant-level field on each source —
    see _truein_sub_site_candidates()/_zinghr_location_candidates(); Truein's
    'category' and ZingHR's 'City' are cluster-level and must NOT be treated
    as a plant name here, or cluster rollups like "Hyderabad"/"Mumbai" get
    created as bogus PlantDvtMapping rows). Truein data is used only if
    already cached — a cold pull takes several minutes (8 paginated calls,
    ~50s apart) and this runs inside an admin-triggered web request, so it
    must never block on a live fetch. The 2-hourly StaffingSnapshot
    background job (app/services/snapshot_refresh.py) is what keeps this
    cache warm in normal operation. ZingHR's cache is only 1-hour TTL and
    fast to (re)fetch, so blocking on it here is fine (same as
    _zinghr_cluster_values() already does for auto_match_clusters()).
    """
    names = set()
    for p in PlantLocation.query.filter_by(is_active=True, is_deleted=False).all():
        if p.name:
            names.add(p.name.strip())
    for s in _truein_sub_site_candidates():
        names.add(s)
    for s in _zinghr_location_candidates():
        names.add(s)
    return sorted(n for n in names if n)


def _truein_sub_site_candidates() -> list[str]:
    """
    Cache-only (never blocks on a live fetch): Truein's per-employee
    'sub_site' field is the plant-level location for that source (distinct
    from 'category', which is cluster-level — see _truein_cluster_values()
    and app/services/headcount.py). This is what lets Truein-sourced
    employees resolve to an individual plant instead of only their cluster.
    """
    return truein.get_cached_sub_sites_if_warm() or []


def _zinghr_location_candidates() -> set[str]:
    """ZingHR's per-employee 'Location' field is the plant-level location for that source (distinct from 'City', which is cluster-level — see _zinghr_cluster_values())."""
    values = set()
    try:
        for e in zinghr.fetch_active_employees():
            loc = (e.get("Location") or "").strip()
            if loc:
                values.add(loc)
    except Exception:
        pass
    return values


def auto_match_plants() -> dict:
    """
    Matches our known plant names (PlantLocation + Truein sub_site + ZingHR
    Location, see _our_plant_names()) against the Daily Volume Tracker's
    erp_name and daily_tracker_name fields, upserting PlantDvtMapping rows.

    Deliberately EXACT match only (normalized string equality) — fuzzy
    matching was dropped for this step on purpose (confirmed with the
    stakeholder): several real plants have similar-sounding but genuinely
    different names (e.g. the Coimbatore/Greater Noida/Ludhiana/Raipur/
    Surat/Thrissur/Trivandrum groups — see CLAUDE.md's "Known open items"),
    and a fuzzy auto-match risks silently pointing two different physical
    plants at the same DVT code, contaminating both plants' headcount. A
    name that doesn't exactly match either DVT field is left UNMATCHED for
    an admin to resolve by hand via Edit, rather than guessed.

    Names are grouped by which DVT plant_code they resolve to BEFORE
    upserting — two differently-spelled candidate names (e.g. ZingHR's
    "GUJ- Vapi" vs Truein's "GUJ-Vapi") that both exactly match the same DVT
    plant get ONE PlantDvtMapping row (whichever name already had a row, or
    is alphabetically first for a brand-new group), with every other name in
    the group added as a PlantNameAlias pointing at it — never a second row,
    which would silently split that plant's real headcount in two (this bit
    us for real on 2026-08-20 when ZingHR Location was added as a candidate
    source — see CLAUDE.md).

    `matched_on` records which DVT field the winning match came from —
    'erp_name' or 'tracker_name'. Also fuzzy-matches against Truein's
    sub_site values (if the Truein cache happens to be warm) to populate
    truein_sub_site — a different, unrelated lookup (which raw Truein string
    corresponds to this plant, for employee resolution), not touched by the
    exact-only change above. Rows already marked 'manual' are left untouched,
    and always win as the group's canonical name.
    """
    our_names = _our_plant_names()
    dvt_plants = dvt.fetch_all_plants()

    erp_by_norm = {}
    tracker_by_norm = {}
    for p in dvt_plants:
        erp = p.get("erp_name")
        if erp:
            erp_by_norm.setdefault(_normalize(erp), p)
        tracker = p.get("daily_tracker_name")
        if tracker:
            tracker_by_norm.setdefault(_normalize(tracker), p)

    sub_site_candidates = [(_normalize(s), s) for s in _truein_sub_site_candidates()]

    # region -> ClusterNameMapping.id, so a matched plant can be linked to its
    # cluster (needed for cluster-scope norms — PlantDvtMapping.cluster_id).
    cluster_by_region = {
        _normalize(c.dvt_region): c.id
        for c in ClusterNameMapping.query.filter_by(is_deleted=False).all() if c.dvt_region
    }

    # ALL rows (including soft-deleted) keyed by LOWERCASED name —
    # plant_location_name has a DB-level UNIQUE constraint under
    # utf8mb4_unicode_ci collation, which is case-insensitive (confirmed:
    # "Head Office" and "Head office" collide as the same value even though
    # they're different Python strings) and doesn't care about is_deleted
    # either (a name merged away by merge_duplicate_plants.py or a previous
    # run's stray-retirement below still occupies that name and MUST be
    # reused rather than re-inserted, or the insert throws a duplicate-key
    # IntegrityError — hit both of these for real on 2026-08-21). Keying by
    # .lower() everywhere below mirrors what MySQL actually enforces.
    # active_by_name_ci is the is_deleted=False subset, used only to decide
    # which name in a group is "already tracked" for canonical selection —
    # a soft-deleted name should never win that over a genuinely active one.
    existing_by_name_ci = {r.plant_location_name.lower(): r for r in PlantDvtMapping.query.all()}
    active_by_name_ci = {k: r for k, r in existing_by_name_ci.items() if not r.is_deleted}
    existing_alias_names_ci = {a.alias_name.lower() for a in PlantNameAlias.query.all()}

    def _resolve_sub_site(norm_name: str) -> str | None:
        best_score, best_sub = 0.0, None
        for norm_candidate, raw in sub_site_candidates:
            if norm_candidate == norm_name:
                return raw
            score = fuzz.token_sort_ratio(norm_name, norm_candidate)
            if score > best_score:
                best_score, best_sub = score, raw
        return best_sub if best_score >= _FUZZY_MATCH_THRESHOLD else None

    # ── Group candidate names by which DVT plant they exactly match ──
    groups = {}  # plant_code -> {"plant": dvt_plant_dict, "names": [name, ...]}
    unmatched_names = []
    seen_unmatched_ci = set()
    for name in our_names:
        norm_name = _normalize(name)
        plant = erp_by_norm.get(norm_name) or tracker_by_norm.get(norm_name)
        if plant:
            groups.setdefault(plant.get("plant_code"), {"plant": plant, "names": []})["names"].append(name)
        else:
            # our_names is case-sensitive (a set of exact strings), but two
            # entries that only differ by case are the same DB row — only
            # process the first one seen this run, or the second's INSERT
            # collides with the first's.
            key = name.lower()
            if key in seen_unmatched_ci:
                continue
            seen_unmatched_ci.add(key)
            unmatched_names.append(name)

    matched_exact = unmatched = 0

    for code, g in groups.items():
        plant, names_here = g["plant"], g["names"]

        # A MANUAL row among this group's names always wins as canonical —
        # never rename or overwrite an admin's hand correction. Only an
        # ACTIVE manual row counts; a soft-deleted one is a retired
        # duplicate, not a live override.
        manual_name = next((n for n in names_here if active_by_name_ci.get(n.lower()) and
                             active_by_name_ci[n.lower()].match_confidence == MatchConfidence.MANUAL), None)
        if manual_name:
            canonical = manual_name
        else:
            already_tracked = sorted(n for n in names_here if n.lower() in active_by_name_ci)
            canonical = already_tracked[0] if already_tracked else sorted(names_here)[0]
        canonical_key = canonical.lower()

        row = existing_by_name_ci.get(canonical_key)
        is_new = row is None
        if is_new:
            row = PlantDvtMapping(plant_location_name=canonical)
            db.session.add(row)
            db.session.flush()  # need row.id for PlantNameAlias rows below
        elif row.is_deleted:
            # canonical was picked with no other active-tracked name in the
            # group (fallback to alphabetical) and happens to reuse a name
            # that was previously soft-deleted — reactivate it as the live
            # mapping now that it's a genuine DVT match again.
            row.is_deleted = False
            row.is_active = True
        existing_by_name_ci[canonical_key] = row
        active_by_name_ci[canonical_key] = row

        if not manual_name:
            norm_canonical = _normalize(canonical)
            if norm_canonical in erp_by_norm:
                matched_field = "erp_name"
            elif norm_canonical in tracker_by_norm:
                matched_field = "tracker_name"
            else:
                # canonical was picked for being already-tracked, not for
                # directly matching itself — fall back to whichever DVT
                # field this group's plant record was actually found under.
                matched_field = "erp_name" if plant.get("erp_name") else "tracker_name"

            row.dvt_plant_code = plant.get("plant_code")
            row.dvt_daily_tracker_name = plant.get("daily_tracker_name")
            row.dvt_erp_name = plant.get("erp_name")
            row.cluster_id = cluster_by_region.get(_normalize(plant.get("region")))
            row.match_confidence = MatchConfidence.AUTO_EXACT
            row.match_score = 100.0
            row.matched_on = matched_field
        matched_exact += 1

        sub_site = _resolve_sub_site(_normalize(canonical))
        if sub_site:
            row.truein_sub_site = sub_site

        seen_alias_ci_this_group = set()
        for n in names_here:
            if n.lower() == canonical_key:
                continue
            n_key = n.lower()
            if n_key in seen_alias_ci_this_group:
                continue  # two group members differing only by case — one alias row covers both
            seen_alias_ci_this_group.add(n_key)
            if n_key not in existing_alias_names_ci:
                db.session.add(PlantNameAlias(alias_name=n, plant_dvt_mapping_id=row.id))
                existing_alias_names_ci.add(n_key)
            # A stray row that used to exist under this other spelling is
            # now a duplicate of `row` — retire it (a MANUAL name would have
            # won `canonical` above, so any stray here is safe to retire).
            stray = existing_by_name_ci.get(n_key)
            if stray is not None and stray.id != row.id:
                stray.is_deleted = True
                stray.is_active = False

    for name in unmatched_names:
        name_key = name.lower()
        existing = existing_by_name_ci.get(name_key)
        if existing and existing.match_confidence == MatchConfidence.MANUAL:
            continue  # never overwrite an admin's manual correction

        row = existing or PlantDvtMapping(plant_location_name=name)
        # No exact hit this run — clear any stale DVT linkage rather than
        # leaving a dvt_plant_code in place with a confidence flag that no
        # longer reflects it (headcount.py's plant-scope loop only checks
        # dvt_plant_code, not match_confidence).
        row.dvt_plant_code = None
        row.dvt_daily_tracker_name = None
        row.dvt_erp_name = None
        row.cluster_id = None
        row.match_confidence = MatchConfidence.UNMATCHED
        row.match_score = 0.0
        row.matched_on = None
        unmatched += 1
        existing_by_name_ci[name_key] = row

        sub_site = _resolve_sub_site(_normalize(name))
        if sub_site:
            row.truein_sub_site = sub_site

        if not existing:
            db.session.add(row)

    # Sweep any row still left over from before fuzzy matching was retired
    # (2026-08-20) — AUTO_FUZZY is no longer a confidence this function ever
    # produces, but a row whose plant_location_name no longer appears in
    # this run's our_names (e.g. a name that dropped out of the Truein/
    # ZingHR cache) would otherwise never get revisited above and would
    # silently keep an old fuzzy match live indefinitely.
    stale_fuzzy = PlantDvtMapping.query.filter_by(
        is_deleted=False, match_confidence=MatchConfidence.AUTO_FUZZY
    ).all()
    for row in stale_fuzzy:
        row.dvt_plant_code = None
        row.dvt_daily_tracker_name = None
        row.dvt_erp_name = None
        row.cluster_id = None
        row.match_confidence = MatchConfidence.UNMATCHED
        row.match_score = 0.0
        row.matched_on = None
        unmatched += 1

    db.session.commit()
    return {"matched_exact": matched_exact, "unmatched": unmatched, "total": len(our_names)}


def _truein_cluster_values() -> set[str]:
    """Cache-only (see _our_plant_names docstring) — never blocks on a live fetch."""
    values = set()
    cached = truein.get_cached_employees_if_warm() or []
    for e in cached:
        cat = (e.get("category") or "").strip()
        if cat and cat.lower() not in _TRUEIN_JUNK_CATEGORIES:
            values.add(cat)
    return values


def _zinghr_cluster_values() -> set[str]:
    values = set()
    try:
        for e in zinghr.fetch_active_employees():
            city = (e.get("City") or "").strip()
            if city:
                values.add(city)
    except Exception:
        pass
    return values


def auto_match_clusters() -> dict:
    """
    DVT's plant `region` is the anchor list (it's what determines "how many
    plants are in this cluster" for the cluster-level norms). For each DVT
    region, fuzzy-matches the closest ZingHR City and Truein category value
    and upserts a ClusterNameMapping row. Rows already marked 'manual' are
    left untouched.
    """
    dvt_regions = sorted({(p.get("region") or "").strip() for p in dvt.fetch_all_plants() if p.get("region")})
    zinghr_cities = _zinghr_cluster_values()
    truein_categories = _truein_cluster_values()

    matched_exact = matched_fuzzy = unmatched = 0

    for region in dvt_regions:
        existing = ClusterNameMapping.query.filter_by(canonical_cluster_name=region).first()
        if existing and existing.match_confidence == MatchConfidence.MANUAL:
            continue

        norm_region = _normalize(region)

        def best_match(candidates: set[str]):
            best_score, best_val = 0.0, None
            for c in candidates:
                if _normalize(c) == norm_region:
                    return c, 100.0
                score = fuzz.token_sort_ratio(norm_region, _normalize(c))
                if score > best_score:
                    best_score, best_val = score, c
            return best_val, best_score

        zh_val, zh_score = best_match(zinghr_cities)
        tr_val, tr_score = best_match(truein_categories)

        row = existing or ClusterNameMapping(canonical_cluster_name=region)
        row.dvt_region = region
        row.zinghr_city = zh_val if zh_score >= _FUZZY_MATCH_THRESHOLD else None
        row.truein_category = tr_val if tr_score >= _FUZZY_MATCH_THRESHOLD else None

        overall = min(
            zh_score if row.zinghr_city else 0,
            tr_score if row.truein_category else 0,
        ) if (row.zinghr_city and row.truein_category) else max(
            zh_score if row.zinghr_city else 0, tr_score if row.truein_category else 0
        )
        if overall == 100.0:
            row.match_confidence = MatchConfidence.AUTO_EXACT
            matched_exact += 1
        elif overall >= _FUZZY_MATCH_THRESHOLD:
            row.match_confidence = MatchConfidence.AUTO_FUZZY
            matched_fuzzy += 1
        else:
            row.match_confidence = MatchConfidence.UNMATCHED
            unmatched += 1
        if not existing:
            db.session.add(row)

    db.session.commit()
    return {"matched_exact": matched_exact, "matched_fuzzy": matched_fuzzy, "unmatched": unmatched,
            "total": len(dvt_regions), "zinghr_cities_seen": len(zinghr_cities),
            "truein_categories_seen": len(truein_categories)}


