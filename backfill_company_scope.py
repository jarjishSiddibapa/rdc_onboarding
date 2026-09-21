"""
One-off backfill: the new company-scope tick-mark system (UserCompanyScope)
defaults every user to NO companies (fail-closed, per stakeholder
confirmation, 2026-09-21). Every pre-existing INITIATOR/BUSINESS_HEAD/
HR_MANAGER account was implicitly RDC-only before this feature existed, so
without this script every live user of those three roles would be locked
out of everything the moment this ships. Idempotent — skips any user who
already has at least one UserCompanyScope row (covers re-runs and any user
an admin has already configured by hand before this script runs).

Run once, BEFORE the company-scoped approval routing feature goes live:
    venv\\Scripts\\python backfill_company_scope.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from app.extensions import db
from app.models import User, UserRole, UserCompanyScope

app = create_app()
with app.app_context():
    target_roles = [UserRole.INITIATOR, UserRole.BUSINESS_HEAD, UserRole.HR_MANAGER]
    users = User.query.filter(User.role.in_(target_roles)).all()
    scoped_user_ids = {r.user_id for r in UserCompanyScope.query.all()}

    added = []
    for u in users:
        if u.id in scoped_user_ids:
            continue
        db.session.add(UserCompanyScope(user_id=u.id, company="RDC"))
        added.append(u.name)

    if added:
        db.session.commit()
        print(f"Backfilled RDC company-scope for {len(added)} user(s): {', '.join(added)}")
    else:
        print("Every INITIATOR/BUSINESS_HEAD/HR_MANAGER user already has a company-scope row — nothing to do.")

    print("\nDone.")
