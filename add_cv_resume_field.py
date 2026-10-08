"""
One-off: adds an OPTIONAL "CV / Resume" upload as the last document field on
step 3 of the hiring form (2026-10-08, stakeholder request). Idempotent.
Uses a raw engine (no create_app() — see CLAUDE.md gotcha #2).

Run once: venv/Scripts/python add_cv_resume_field.py
"""
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


def main():
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT id, is_deleted FROM form_fields WHERE field_key = 'cv_resume'")).first()
        if row:
            print("cv_resume already exists (id=%s, is_deleted=%s) — nothing to do" % (row[0], row[1]))
            return
        order = conn.execute(text("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM form_fields")).scalar()
        conn.execute(text(
            "INSERT INTO form_fields (field_key, field_label, field_type, step, is_required, is_active, "
            "is_deleted, is_readonly, sort_order, help_text, options_source, allow_other) "
            "VALUES ('cv_resume', 'CV / Resume (Optional)', 'FILE', 3, 0, 1, 0, 0, :o, "
            "'Optional - PDF or Word document', 'INLINE', 0)"), {"o": order})
        print("added cv_resume at sort_order", order)


if __name__ == "__main__":
    sys.exit(main())
