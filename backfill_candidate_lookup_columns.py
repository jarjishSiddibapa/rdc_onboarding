"""
One-off: populates the new candidate_email / candidate_govt_id columns
(2026-09-22 performance fix) on OnboardingRequest rows that existed before
this migration — those columns are now kept in sync going forward by
_sync_quick_access() on every form save, but pre-existing rows never went
through that code path with the new columns present, so they'd otherwise
sit NULL forever and silently fail to be found by the new indexed
duplicate-email/duplicate-Aadhar checks (_check_email_registered() /
_check_govt_id_registered() in app/requests_bp/routes.py).

Reads directly from each row's form_data JSON blob — the same source
_sync_quick_access() itself reads from — and writes the same
lowercased-email / digits-only-Aadhar normalization.

Idempotent — only touches rows where the mirror column doesn't already
match what form_data implies.

Run once: venv\\Scripts\\python backfill_candidate_lookup_columns.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from app.extensions import db
from app.models import OnboardingRequest

app = create_app()
with app.app_context():
    updated = 0
    for req in OnboardingRequest.query.all():
        fd = req.form_data
        want_email = (fd.get("email_id") or "").strip().lower()
        want_govt_id = re.sub(r"\D", "", fd.get("aadhar_no") or "")
        changed = False
        if (req.candidate_email or "") != want_email:
            req.candidate_email = want_email
            changed = True
        if (req.candidate_govt_id or "") != want_govt_id:
            req.candidate_govt_id = want_govt_id
            changed = True
        if changed:
            updated += 1

    if updated:
        db.session.commit()
        print(f"Backfilled {updated} request(s).")
    else:
        print("Every request already in sync — nothing to do.")

    print("Done.")
