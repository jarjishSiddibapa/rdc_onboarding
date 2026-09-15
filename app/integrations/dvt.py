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


def fetch_monthly_volumes(month: str) -> dict:
    """
    GET /api/v1/volumes/monthly?month=YYYY-MM
    Returns the raw response dict: {"period","month","metric","count","plants":[...]}.
    """
    resp = requests.get(
        f"{BASE_URL}/api/v1/volumes/monthly",
        params={"month": month},
        headers=_auth_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


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
