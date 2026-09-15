# Multi-Company Support — Progress Tracker

**Current state (2026-09-15): starting implementation.** Full design is in the
approved plan — see the plan file referenced by this session, or the
"Multi-Company Support" additions in `CLAUDE.md`/`AGENTS.md` once each part
lands (dated notes, same convention as every other feature in this project).
This file is a checklist only — it doesn't duplicate the design, just tracks
what's done so a stopped session can resume without re-deriving anything.

**Already true before this work started (confirmed by reading the code, not
assumed):**
- `check_rdc_staffing_gate()` is already gated `company_code == "RDC"` at
  every call site — Ultrafine/ROBO already bypass the manpower restriction
  once they're selectable at all. No gate code changes needed.
- `seed.py`'s `company_code` FormField already lists all three companies —
  only the *live* DB's `FormFieldOption` rows are missing Ultrafine/ROBO.

## Checklist

- [x] **Part A** — `PlantLocation.company` column + `/admin/plants` company tab/filter + add/edit form field (5 new tests, `TestPlantCompanyScoping`)
- [x] **Bonus fix (surfaced mid-build):** `build_payload()`'s `sub_site` field was only ever set when a `PlantDvtMapping` row existed — Ultrafine/ROBO plants never have one, so `sub_site` was silently missing from every non-RDC Truein push. Now falls back to the raw plant name, same as `sitePoint` already did. Test: `test_sub_site_not_empty_when_plant_has_no_dvt_mapping_row_at_all`.
- [x] **Part B** — `add_company_options.py` run against the live DB. Turned out all 3 `FormFieldOption` rows already existed (RDC/Ultrafine/ROBO) but Ultrafine/ROBO were `is_active=0` — script now activates existing-but-inactive rows too, not just inserts missing ones. Confirmed live: all 3 now `is_active=1`.
- [x] **Part C** — `plant_locations_api()` accepts `?company=`; `_company_plant_options()` added; `form.html` re-fetches plant list on Company Code change, collapses cluster picker to a flat list for Ultrafine/ROBO. 9 new tests. Confirmed `check_hiring_capacity()` already bypasses the gate for non-RDC (regression-locked, not newly built).
- [x] **Part D** — `EmployeeLocationSnapshot.company` column; `_compute_and_store_other_company_snapshot()` in `headcount.py`; Staffing Status page is now 3 tabs (RDC unchanged, Ultrafine/ROBO new flat plant+headcount views + plant-detail employee list). 19 new tests across `test_headcount.py` (new file) and `test_staffing_norms.py`.
  - **2 real bugs found and fixed while building this, both regression-tested:** (1) RDC's read-side functions (`get_all_latest_snapshots`-adjacent helpers) find "the latest run" via a table-wide `MAX(computed_at)` — without care, a later Ultrafine/ROBO timestamp in the same refresh cycle would silently make every RDC query return empty. Fixed by sharing one timestamp between both passes. (2) `get_employees_at_plant`/`get_employees_at_cluster`/`get_all_employees` had no company filter at all — would have let Ultrafine/ROBO employees leak into RDC-only views once the table is shared. Fixed by scoping all three to `company IS NULL` (RDC's own rows never set it).
- [ ] **Part E** — this file + `AGENTS.md` synced to `CLAUDE.md`
- [ ] **Part F** — redact 8 hardcoded fallback credentials (truein.py x3, zinghr.py x2, dvt.py x3) → `git init` + `.gitignore` + first commit (no push)
- [x] Full test suite green — 212/212 (`venv\Scripts\python -m pytest -q`)
- [x] Live-verified in browser: 3-tab Staffing Status page (tab switching, RDC unaffected, Ultrafine empty-state), admin plant CRUD per company (created + deleted a real "UF-Test Verification Plant" under Ultrafine), `/requests/api/plant-locations?company=Ultrafine` returns only that plant flat (no cluster) while `?company=RDC` is unchanged. Could NOT live-test the actual Initiator-facing form UI (company dropdown → plant list reactivity) since that route is Initiator-role-only and no Initiator credentials are available this session — verified via the underlying API + the 9 automated tests in `TestCompanyAwarePlantLocationsApi` instead.
- [ ] `graphify update .` run after the above lands
