"""
Replaces ALL designations with the master list below.
Run once:  venv\Scripts\python seed_designations.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from app.extensions import db
from app.models import Designation

# ── Master designation list ────────────────────────────────────────────────────
DESIGNATIONS = [
    # (name, notice_period_days)

    # 15-day notice period
    ("BPO Cum Mechanic",                        15),
    ("Batching Plant Operator",                  15),
    ("Loader / JCB / Bull Trackter Operator",    15),
    ("Field Technician",                         15),
    ("Assistant",                                15),
    ("Technician Apprentice",                    15),
    ("TM Driver",                                15),
    ("Trainee RMX Technician",                   15),
    ("Pump Operator",                            15),
    ("Senior Field Technician",                  15),
    ("Boom Pump Operator cum Driver",            15),
    ("Boom Pump Helper",                         15),
    ("RMX Technician",                           15),
    ("Tipper Driver",                            15),
    ("CRO cum Line Pump Operator",               15),
    ("Scrapper Operator",                        15),
    ("Electrician",                              15),
    ("Driver",                                   15),
    ("Data Entry Operator",                      15),
    ("Pump Operator Cum CRO",                    15),
    ("Dispatcher",                               15),

    # 30-day notice period
    ("Officer - Safety",                         30),
    ("Executive Systems",                        30),
    ("Customer Relationship Officer",            30),
    ("Executive Accounts",                       30),
    ("Lab Technician",                           30),
    ("Executive Dispatch & Logistics",           30),
    ("Executive Materials",                      30),
    ("Executive Logistics",                      30),
    ("Officer Sales",                            30),
    ("Trainee Accounts",                         30),
    ("Executive - Credit Control",               30),
    ("Trainee - Batching Plant Operator",        30),
    ("Officer Production",                       30),
    ("OSE",                                      30),
    ("TM Supervisors",                           30),
    ("Executive HR",                             30),
    ("Mess Caretaker",                           30),
    ("CDS",                                      30),
    ("Executive Operations",                     30),
    ("Trainee - Sales",                          30),
    ("Officer - Sales Executive",                30),
    ("Admin",                                    30),
    ("Trainee Project",                          30),
    ("Senior Executive Logistics",               30),
    ("Officer Logistics",                        30),
    ("Senior Officer Sales",                     30),
    ("Welder cum Operator",                      30),
    ("Trainee - Logistics",                      30),
]

app = create_app()

with app.app_context():
    # Hard-delete everything (config data, safe to replace)
    deleted = Designation.query.delete()
    db.session.commit()
    print(f"Removed {deleted} old designation(s).")

    # Insert fresh
    for i, (name, days) in enumerate(DESIGNATIONS):
        db.session.add(Designation(
            name=name,
            notice_period_days=days,
            is_active=True,
            is_deleted=False,
            sort_order=i + 1,
        ))
    db.session.commit()
    print(f"Inserted {len(DESIGNATIONS)} designations.")
    print()

    fifteen = [d for d in DESIGNATIONS if d[1] == 15]
    thirty  = [d for d in DESIGNATIONS if d[1] == 30]
    print(f"  15-day notice: {len(fifteen)}")
    print(f"  30-day notice: {len(thirty)}")
    print("\nDone.")
