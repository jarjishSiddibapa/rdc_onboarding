"""One-off (2026-10-12, idempotent): staffing-norm category for the technician designations, as decided by the
stakeholder. RDC rows only, found by exact name; uses a raw engine (never create_app() - see CLAUDE.md gotcha #2).

  Senior Field Technician  -> Technical
  RMX Technician           -> Technical
  Trainee RMX Technician   -> Assistant
  Technician Apprentice    -> Batchers/Production Officer, EXCEPT a Civil qualification -> Technical
"""
import os

import sqlalchemy as sa
from dotenv import load_dotenv

os.chdir(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(".env")
engine = sa.create_engine(os.environ["DATABASE_URL"])

PLAIN = {"Senior Field Technician": "Technical", "RMX Technician": "Technical", "Trainee RMX Technician": "Assistant"}
APPRENTICE = ("Technician Apprentice", "Batchers/Production Officer", "Civil", "Technical")

with engine.begin() as c:
    # The qualification-rule columns normally appear on the next server start (_auto_migrate); add them now if needed.
    for col, ddl in (("qualification_keyword", "VARCHAR(50) NULL"), ("qualification_norm_category_id", "INT NULL")):
        if not c.execute(sa.text("SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() "
                                 "AND table_name='designations' AND column_name=:c"), {"c": col}).scalar():
            c.execute(sa.text(f"ALTER TABLE designations ADD COLUMN {col} {ddl}"))
            print("added column", col)
    cats = {n: i for i, n in c.execute(sa.text(
        "SELECT id, name FROM norm_role_categories WHERE is_deleted=0 AND scope='PLANT'")).fetchall()}
    for name, cat in PLAIN.items():
        r = c.execute(sa.text("UPDATE designations SET norm_category_id=:cat WHERE name=:n AND company='RDC' "
                              "AND is_deleted=0 AND (norm_category_id IS NULL OR norm_category_id<>:cat)"),
                      {"cat": cats[cat], "n": name})
        print(f"{name:26} -> {cat:30} rows changed: {r.rowcount}")
    name, default, kw, special = APPRENTICE
    r = c.execute(sa.text("UPDATE designations SET norm_category_id=:d, qualification_keyword=:kw, "
                          "qualification_norm_category_id=:s WHERE name=:n AND company='RDC' AND is_deleted=0 AND "
                          "(norm_category_id IS NULL OR norm_category_id<>:d OR qualification_keyword IS NULL "
                          "OR qualification_keyword<>:kw OR qualification_norm_category_id IS NULL "
                          "OR qualification_norm_category_id<>:s)"),
                  {"d": cats[default], "kw": kw, "s": cats[special], "n": name})
    print(f"{name:26} -> {default} (qualification '{kw}' -> {special}) rows changed: {r.rowcount}")
    for row in c.execute(sa.text("SELECT id,name,company,norm_category_id,qualification_keyword,qualification_norm_category_id "
                                 "FROM designations WHERE company='RDC' AND name IN ('Senior Field Technician','RMX Technician',"
                                 "'Trainee RMX Technician','Technician Apprentice') ORDER BY name")):
        print(tuple(row))
