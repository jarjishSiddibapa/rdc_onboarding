"""
One-off, idempotent: correct the spelling "Aadhar" -> "Aadhaar" (official spelling) in the
admin-editable form-field texts stored in the database (label / placeholder / help text).
Field KEYS (aadhar_no, aadhar_card) are internal identifiers and are deliberately left alone.

Uses a raw engine (never create_app() — see CLAUDE.md gotcha #2). Safe to re-run.
    venv\\Scripts\\python fix_aadhaar_spelling.py
"""
import os

import sqlalchemy as sa
from dotenv import load_dotenv

load_dotenv()

COLUMNS = ("field_label", "placeholder", "help_text")


def main():
    engine = sa.create_engine(os.environ["DATABASE_URL"])
    changed = 0
    with engine.begin() as conn:
        for col in COLUMNS:
            res = conn.execute(sa.text(
                f"UPDATE form_fields SET {col} = REPLACE({col}, 'Aadhar', 'Aadhaar') WHERE {col} LIKE '%Aadhar%'"))
            changed += res.rowcount or 0
    print(f"Updated {changed} text value(s). Re-running is harmless.")


if __name__ == "__main__":
    main()
