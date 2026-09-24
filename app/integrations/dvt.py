"""
Daily Volume Tracker Integration
=================================
Read-only. Fetches plant production volume (used to determine RDC staffing
norm tiers) from the separate Daily Volume Tracker app's public API.
"""

import os
import time
from datetime import datetime, timedelta

import requests

# ── DVT API base ──────────────────────────────────────────────────────────────
# Credentials (env vars only — no hardcoded fallback, added 2026-09-15 as
# part of setting this project up as a git repo. Real values live in .env,
# which is gitignored; see .env for the values previously hardcoded here.)
BASE_URL = os.environ.get("DVT_BASE_URL")
USERNAME = os.environ.get("DVT_USERNAME")
PASSWORD = os.environ.get("DVT_PASSWORD")

# Token expires 24h after issue (per DVT's own docs) — cache with a safety margin.
_TOKEN_SAFETY_MARGIN_S = 300  # re-request 5 minutes before actual expiry
_token: str | None = None
_token_expires_at: float = 0.0


def get_access_token() -> str:
    """
    POST /api/v1/token with username/password -> Bearer token, cached
    in-process until ~5 minutes before its 24h expiry.
    Raises requests.HTTPError / ValueError on failure.
    """
    global _token, _token_expires_at
    now = time.time()
    if _token and now < _token_expires_at:
        return _token

    if not USERNAME or not PASSWORD:
        raise ValueError(
            "DVT_USERNAME / DVT_PASSWORD are not set. "
            "Set them as environment variables before calling the Daily Volume Tracker API."
        )

    resp = requests.post(
        f"{BASE_URL}/api/v1/token",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    _token = body["token"]
    expires_in = body.get("expires_in_seconds", 86400)
    _token_expires_at = now + expires_in - _TOKEN_SAFETY_MARGIN_S
    return _token


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {get_access_token()}"}


# DVT was the only one of the three external integrations (Truein, ZingHR,
# DVT) with no caching at all — every plant/designation change on the New
# Request form (check_hiring_capacity(), see app/requests_bp/routes.py)
# round-tripped live to DVT, as did edit_plant_mapping()'s GET and
# matching.py's fetch_all_plants(). The actual staffing-gate check at submit
# time was never affected (staffing_norms.py reads only the cached
# StaffingSnapshot, never DVT live) — this cache only speeds up/insulates
# the interactive preview and admin-facing callers. Same 1h TTL convention
# as zinghr.py's _CACHE_TTL. Keyed by month since callers pass different
# months (though in practice almost always "last month").
_CACHE_TTL_S = 3600
_volumes_cache: dict[str, dict] = {}
_volumes_cache_at: dict[str, float] = {}


def fetch_monthly_volumes(month: str) -> dict:
    """
    GET /api/v1/volumes/monthly?month=YYYY-MM
    Returns the raw response dict: {"period","month","metric","count","plants":[...]}.
    Cached in-process per month for _CACHE_TTL_S (1h) — see module docstring above.
    """
    now = time.time()
    cached_at = _volumes_cache_at.get(month, 0.0)
    if month in _volumes_cache and (now - cached_at) < _CACHE_TTL_S:
        return _volumes_cache[month]

    resp = requests.get(
        f"{BASE_URL}/api/v1/volumes/monthly",
        params={"month": month},
        headers=_auth_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    _volumes_cache[month] = data
    _volumes_cache_at[month] = now
    return data


def _previous_month_str() -> str:
    """Return the previous calendar month as YYYY-MM, based on today's date."""
    first_of_this_month = datetime.utcnow().replace(day=1)
    last_of_previous_month = first_of_this_month - timedelta(days=1)
    return last_of_previous_month.strftime("%Y-%m")


def fetch_last_months_volumes() -> dict:
    """Convenience wrapper: fetch_monthly_volumes() for the previous calendar month."""
    return fetch_monthly_volumes(_previous_month_str())


def get_plant_volume(plant_code: str, month: str | None = None) -> float | None:
    """Look up one plant's volume for the given month (defaults to last month)."""
    data = fetch_monthly_volumes(month) if month else fetch_last_months_volumes()
    for plant in data.get("plants", []):
        if plant.get("plant_code") == plant_code:
            return plant.get("volume")
    return None


def get_cluster_total_volume(plant_codes: list[str], month: str | None = None) -> float:
    """Sum volume across the given plant_codes (for cluster-level norms like Accounts)."""
    data = fetch_monthly_volumes(month) if month else fetch_last_months_volumes()
    wanted = set(plant_codes)
    return sum(p.get("volume", 0.0) for p in data.get("plants", []) if p.get("plant_code") in wanted)


def fetch_all_plants(month: str | None = None) -> list[dict]:
    """
    Return the raw plants[] list for the given month (defaults to last month) —
    used by app/services/matching.py to auto-match our plant names against
    DVT's plant_code/daily_tracker_name/erp_name/region.
    """
    data = fetch_monthly_volumes(month) if month else fetch_last_months_volumes()
    return data.get("plants", [])


# ── Trailing 3-month average (2026-09-24 stakeholder rule) ─────────────────────
# The RDC staffing-norms hiring gate used to classify a plant's tier off a
# single month's raw volume (last calendar month). Stakeholder confirmed the
# actual rule is the AVERAGE of the last 3 completed calendar months — a
# single anomalous month (maintenance shutdown, a monsoon slowdown, a
# one-off spike) must not by itself push a plant into a different tier and
# change how many people it's allowed to hire. Everything below is additive
# — get_plant_volume()/get_cluster_total_volume()/fetch_all_plants() above
# are untouched and still serve their existing single-month callers
# (matching.py's name reconciliation, the admin plant-mappings page's
# informational display) where "which month" doesn't affect a hiring
# decision. Only the two call sites that actually feed the hiring gate —
# app/services/staffing_norms.py's live check and
# app/services/headcount.py's _compute_and_store_snapshot() — were switched
# to the functions below.
_AVG_WINDOW_MONTHS = 3


def _trailing_month_strs(months: int) -> list[str]:
    """Last `months` completed calendar months as YYYY-MM, most recent first."""
    result = []
    cursor = datetime.utcnow().replace(day=1)
    for _ in range(months):
        last_of_previous_month = cursor - timedelta(days=1)
        result.append(last_of_previous_month.strftime("%Y-%m"))
        cursor = last_of_previous_month.replace(day=1)
    return result


def get_average_plant_volume(plant_code: str, months: int = _AVG_WINDOW_MONTHS) -> float | None:
    """
    Average of plant_code's volume across the trailing `months` completed
    calendar months (default 3). Averages only over the months the plant
    actually appears in with a non-null volume — a month DVT has no data
    for (e.g. a newly commissioned plant) is skipped rather than counted
    as a zero, which would otherwise wrongly drag the average down.
    Returns None only if the plant has no volume in any of the months.
    """
    total, count = 0.0, 0
    for month in _trailing_month_strs(months):
        data = fetch_monthly_volumes(month)
        for plant in data.get("plants", []):
            if plant.get("plant_code") == plant_code and plant.get("volume") is not None:
                total += plant["volume"]
                count += 1
                break
    return (total / count) if count else None


def get_average_cluster_total_volume(plant_codes: list[str], months: int = _AVG_WINDOW_MONTHS) -> float:
    """
    Average, across the trailing `months` completed calendar months
    (default 3), of the summed volume for the given plant_codes — same
    trailing-average rule as get_average_plant_volume(), applied to a
    cluster/region total (e.g. the Accounts RATE_PER_VOLUME norm).
    """
    wanted = set(plant_codes)
    monthly_totals = []
    for month in _trailing_month_strs(months):
        data = fetch_monthly_volumes(month)
        monthly_totals.append(sum(p.get("volume", 0.0) for p in data.get("plants", []) if p.get("plant_code") in wanted))
    return sum(monthly_totals) / len(monthly_totals) if monthly_totals else 0.0


def fetch_all_plants_with_avg_volume(months: int = _AVG_WINDOW_MONTHS) -> list[dict]:
    """
    Same shape as fetch_all_plants() — one dict per plant with
    plant_code/region/erp_name/daily_tracker_name/etc. — but `volume` is
    replaced by the trailing `months`-month average (see
    get_average_plant_volume()) instead of a single month's figure. Used
    by headcount.py's _compute_and_store_snapshot(), which needs both the
    identity fields (for plant/cluster resolution) and the volume (for
    tier classification) in one bulk call per background refresh.
    Identity fields (region/erp_name/daily_tracker_name) are taken from
    the most recent month a plant appears in, since those can be corrected
    over time upstream in DVT and the newest is the best guess; a plant
    missing from the latest month but present in an older one is still
    included (e.g. briefly absent from one month's export).
    """
    month_strs = _trailing_month_strs(months)
    plants_by_code: dict[str, dict] = {}
    volume_sums: dict[str, float] = {}
    volume_counts: dict[str, int] = {}
    for month in reversed(month_strs):  # oldest first, so newest overwrites identity fields last
        data = fetch_monthly_volumes(month)
        for plant in data.get("plants", []):
            code = plant.get("plant_code")
            if not code:
                continue
            plants_by_code[code] = plant
            volume = plant.get("volume")
            if volume is not None:
                volume_sums[code] = volume_sums.get(code, 0.0) + volume
                volume_counts[code] = volume_counts.get(code, 0) + 1
    merged = []
    for code, plant in plants_by_code.items():
        row = dict(plant)
        if code in volume_sums:
            row["volume"] = volume_sums[code] / volume_counts[code]
        merged.append(row)
    return merged
