"""
ZingHR Employee Master Integration
==================================
Read-only. Fetches the active employee roster from ZingHR's GEMD (Get
Employee Master Data) API. No write endpoint is used anywhere in this app.
"""

import os
import time
import requests

# ── ZingHR API endpoints ─────────────────────────────────────────────────────
ZINGHR_AUTH_URL = "https://mservices.zinghr.com/etl/api/v2/Auth/GenerateJWTToken"
ZINGHR_EMPLOYEES_URL = "https://mservices.zinghr.com/etl/api/v2/Employee/GetEmployeeDetails"

# ── Credentials (env vars only — no hardcoded fallback, added 2026-09-15 as
# part of setting this project up as a git repo. Real values live in .env,
# which is gitignored; see .env for the values previously hardcoded here.) ──
CLIENT_ID = os.environ.get("ZINGHR_CLIENT_ID")
CLIENT_SECRET = os.environ.get("ZINGHR_CLIENT_SECRET")

_PAGE_SIZE = 500

# Attribute types flattened out of each employee's `attributes` array.
_ATTRIBUTE_KEYS = ("City", "Company", "Cost Center", "Department", "Designation", "Location")

# Cached 1 hour — mirrors app/integrations/truein.py's employee-list caching pattern.
_CACHE_TTL = 3600  # seconds
_employees_cache: list | None = None
_employees_cache_at: float = 0.0


def get_access_token() -> str:
    """
    HTTP Basic Auth (client_id, client_secret) against GenerateJWTToken.
    Not cached long-term — callers request a fresh token per fetch session.
    Raises requests.HTTPError on non-2xx responses.
    """
    resp = requests.get(
        ZINGHR_AUTH_URL,
        params={"apiPermission": "GEMD"},
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["data"]


def _flatten_employee(raw: dict) -> dict:
    attrs = {a["attributeTypeCode"]: a.get("attributeTypeUnitDescription", "") for a in raw.get("attributes", [])}
    out = {
        "employeeCode": (raw.get("employeeCode") or "").strip(),
        "employeeName": (raw.get("employeeName") or "").strip(),
        "dateOfJoining": (raw.get("dateOfJoining") or "").strip(),
    }
    for key in _ATTRIBUTE_KEYS:
        out[key] = attrs.get(key, "")
    return out


def _dedupe_by_employee_code(items: list[dict]) -> list[dict]:
    """
    ZingHR's GetEmployeeDetails reliably returns a small number of
    employeeCodes twice — one copy with a full attributes[] array, one with
    attributes[] empty or missing entirely (confirmed live: 20 of 1284
    records on 2026-08-12, 100% of that plant's duplicates following this
    exact shape — a genuine ZingHR-side data issue, not a pagination
    artifact). Keep the fullest copy per employeeCode so a caller never
    sees the stripped duplicate and wrongly reads it as "no Location on
    file" for someone who actually has one.
    """
    best: dict[str, dict] = {}
    for e in items:
        code = (e.get("employeeCode") or "").strip()
        if not code:
            continue
        existing = best.get(code)
        if existing is None or len(e.get("attributes") or []) > len(existing.get("attributes") or []):
            best[code] = e
    return list(best.values())


def _fetch_all_employees_raw() -> list[dict]:
    """
    Return every active ZingHR employee (all pages), deduped by
    employeeCode (see _dedupe_by_employee_code), cached for 1 hour.
    Paginates POST GetEmployeeDetails with Pagenumber=1,2,3... until the
    response's employeesCount is 0.
    """
    global _employees_cache, _employees_cache_at
    now = time.time()
    if _employees_cache is not None and (now - _employees_cache_at) < _CACHE_TTL:
        return _employees_cache

    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    all_items: list[dict] = []
    page = 1
    while True:
        body = {"Pagesize": str(_PAGE_SIZE), "Pagenumber": str(page), "employmentstatus": "Active"}
        resp = requests.post(ZINGHR_EMPLOYEES_URL, json=body, headers=headers, timeout=30)
        resp.raise_for_status()
        payload = resp.json().get("data", {})
        employees = payload.get("employees", [])
        if not employees:
            break
        all_items.extend(employees)
        page += 1
        if page > 20:  # safety cap — well beyond any realistic page count
            break

    _employees_cache = _dedupe_by_employee_code(all_items)
    _employees_cache_at = now
    return _employees_cache


def fetch_active_employees() -> list[dict]:
    """
    Public entry point: every active ZingHR employee, flattened to
    {employeeCode, employeeName, dateOfJoining, City, Company, Cost Center,
    Department, Designation, Location}.
    """
    return [_flatten_employee(e) for e in _fetch_all_employees_raw()]
