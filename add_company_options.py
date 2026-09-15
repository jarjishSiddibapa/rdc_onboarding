"""
One-off fix: the live `company_code` FormField has all three FormFieldOption
rows (RDC/Ultrafine/ROBO — seed.py's FORM_FIELDS constant has always listed
all three) but Ultrafine and ROBO are stored with is_active=0, so only RDC
has ever actually appeared in the onboarding form's dropdown. Confirmed
directly against the live DB 2026-09-15: id=1 RDC is_active=1, id=2
Ultrafine is_active=0, id=3 ROBO is_active=0. This is part of the
2026-09-15 multi-company support work (see MULTI_COMPANY_MIGRATION.md).

Idempotent: activates any existing option matching models.COMPANY_CHOICES
by option_value, and inserts any that are missing entirely (covers a fresh
DB seeded before this fix too) — safe to re-run.

Run once: venv\\Scripts\\python add_company_options.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from app.extensions import db
from app.models import FormField, FormFieldOption, COMPANY_CHOICES

app = create_app()
with app.app_context():
    field = FormField.query.filter_by(field_key="company_code", is_deleted=False).first()
    if not field:
        print("No active 'company_code' FormField found — nothing to do.")
        sys.exit(0)

    existing_by_value = {o.option_value: o for o in field.options}
    max_order = max((o.sort_order for o in field.options), default=-1)

    activated, added = [], []
    for company in COMPANY_CHOICES:
        opt = existing_by_value.get(company)
        if opt is None:
            max_order += 1
            db.session.add(FormFieldOption(
                field_id=field.id,
                option_value=company,
                option_label=company,
                sort_order=max_order,
            ))
            added.append(company)
        elif not opt.is_active:
            opt.is_active = True
            activated.append(company)

    if added or activated:
        db.session.commit()
        if added:
            print(f"Added {len(added)} option(s) to 'company_code': {', '.join(added)}")
        if activated:
            print(f"Activated {len(activated)} existing option(s): {', '.join(activated)}")
    else:
        print("All company options already present and active — nothing to do.")
