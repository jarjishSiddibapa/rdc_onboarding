"""
Tests for app/services/headcount.py's multi-company support (added
2026-09-15) — _compute_and_store_other_company_snapshot(), the lightweight,
independent headcount path for Ultrafine/ROBO that deliberately never
touches the RDC-specific reconciliation pipeline (_compute_and_store_snapshot()).
"""
from datetime import datetime as _dt
from unittest.mock import patch
from app.extensions import db as _db
from app.models import PlantLocation, EmployeeLocationSnapshot
from app.services import headcount


def _zh_employee(code, name, company, location, department="Technical"):
    return {
        "employeeCode": code, "employeeName": name, "Company": company,
        "Location": location, "Department": department, "Designation": "Officer",
        "dateOfJoining": "01 Jan 2025", "City": "",
    }


class TestComputeAndStoreOtherCompanySnapshot:
    def test_writes_ultrafine_and_robo_employees_scoped_correctly(self, db, app):
        with app.app_context():
            db.session.add_all([
                PlantLocation(name="UF Plant One", company="Ultrafine", is_active=True),
                PlantLocation(name="ROBO Plant One", company="ROBO", is_active=True),
            ])
            db.session.commit()

            zh_raw = [
                _zh_employee("UF1", "Uma Fine", "Ultrafine Mineral and Admixtures Pvt Ltd", "UF Plant One"),
                _zh_employee("RB1", "Robo Bot", "Robo Silicon Pvt. Ltd.", "ROBO Plant One"),
                _zh_employee("RD1", "RDC Person", "RDC Ready Mix Concrete", "Some RDC Plant"),
            ]
            with patch("app.services.headcount.zinghr.fetch_active_employees", return_value=zh_raw):
                result = headcount._compute_and_store_other_company_snapshot()
                db.session.commit()

            assert result["employee_rows_written"] == 2
            assert result["by_company"]["Ultrafine"] == 1
            assert result["by_company"]["ROBO"] == 1

            rows = EmployeeLocationSnapshot.query.all()
            codes = {r.employee_code: r for r in rows}
            assert "UF1" in codes and codes["UF1"].company == "Ultrafine"
            assert codes["UF1"].plant_location_key == "UF Plant One"
            assert "RB1" in codes and codes["RB1"].company == "ROBO"
            assert "RD1" not in codes  # RDC employees are this function's job, not touched here

    def test_unresolved_location_still_written_not_dropped(self, db, app):
        with app.app_context():
            zh_raw = [_zh_employee("UF2", "Unmatched Uma", "Ultrafine Mineral and Admixtures Pvt Ltd",
                                    "Some Plant Not In PlantLocation")]
            with patch("app.services.headcount.zinghr.fetch_active_employees", return_value=zh_raw):
                headcount._compute_and_store_other_company_snapshot()
                db.session.commit()

            row = EmployeeLocationSnapshot.query.filter_by(employee_code="UF2").first()
            assert row is not None
            assert row.company == "Ultrafine"
            assert row.plant_location_key is None

    def test_face_device_excluded(self, db, app):
        with app.app_context():
            zh_raw = [_zh_employee("FACEUF1", "Face UF Device", "Ultrafine Mineral and Admixtures Pvt Ltd", "X")]
            with patch("app.services.headcount.zinghr.fetch_active_employees", return_value=zh_raw):
                result = headcount._compute_and_store_other_company_snapshot()
                db.session.commit()
            assert result["employee_rows_written"] == 0

    def test_inactive_plant_never_matched(self, db, app):
        with app.app_context():
            db.session.add(PlantLocation(name="Disabled UF Plant", company="Ultrafine", is_active=False))
            db.session.commit()
            zh_raw = [_zh_employee("UF3", "Uma Three", "Ultrafine Mineral and Admixtures Pvt Ltd", "Disabled UF Plant")]
            with patch("app.services.headcount.zinghr.fetch_active_employees", return_value=zh_raw):
                headcount._compute_and_store_other_company_snapshot()
                db.session.commit()
            row = EmployeeLocationSnapshot.query.filter_by(employee_code="UF3").first()
            assert row.plant_location_key is None


def _tr_employee(emp_id, name, sub_site, category="Bangalore"):
    return {
        "empId": emp_id, "name": name, "sub_site": sub_site, "category": category,
        "status": "active", "site_name": "RDC Concrete",
        "department": "Technical", "designation": "Officer", "joining_date": "2025-01-01",
    }


class TestPlantNameCompanyOverride:
    """
    Regression coverage for the 2026-09-23 fix: Truein has no Ultrafine/ROBO
    concept at all in this account (every record's site_name reads "RDC
    Concrete" regardless of the employee's real company), and a ZingHR
    record with a blank Company attribute isn't excluded by the
    _NON_RDC_COMPANIES filter either. Real employees at real Ultrafine/ROBO
    plants were being silently counted as RDC headcount the moment their
    raw Location/sub_site string matched one of those companies' own plant
    names. _compute_and_store_snapshot() (the RDC pass) now checks every
    employee's Location/sub_site against PlantLocation rows tagged
    company IN ('ROBO','Ultrafine') BEFORE counting them toward RDC at all.
    """

    def _run_rdc_snapshot(self, zh_raw, tr_raw):
        with patch("app.services.headcount.zinghr.fetch_active_employees", return_value=zh_raw), \
             patch("app.services.headcount.truein._fetch_all_employees_raw", return_value=tr_raw), \
             patch("app.services.headcount.dvt.fetch_all_plants_with_avg_volume", return_value=[]):
            return headcount._compute_and_store_snapshot()

    def test_truein_employee_at_ultrafine_plant_reclassified_not_counted_as_rdc(self, db, app):
        with app.app_context():
            db.session.add(PlantLocation(name="ULT-Raipur", company="Ultrafine", is_active=True))
            db.session.commit()

            tr_raw = [_tr_employee("T1", "Real Ultrafine Worker", sub_site="ULT-Raipur")]
            result = self._run_rdc_snapshot(zh_raw=[], tr_raw=tr_raw)
            db.session.commit()

            assert result["other_company_employee_rows_written"] == 1
            assert result["employee_rows_written"] == 0  # never counted toward RDC at all
            row = EmployeeLocationSnapshot.query.filter_by(employee_code="T1").first()
            assert row is not None
            assert row.company == "Ultrafine"
            assert row.plant_location_key == "ULT-Raipur"
            assert row.source == headcount.ExternalDesignationSource.TRUEIN

    def test_zinghr_employee_blank_company_at_robo_plant_reclassified(self, db, app):
        with app.app_context():
            db.session.add(PlantLocation(name="Robo-AP_RO", company="ROBO", is_active=True))
            db.session.commit()

            zh_raw = [_zh_employee("Z1", "Real Robo Worker", company="", location="Robo-AP_RO")]
            result = self._run_rdc_snapshot(zh_raw=zh_raw, tr_raw=[])
            db.session.commit()

            assert result["other_company_employee_rows_written"] == 1
            assert result["employee_rows_written"] == 0
            row = EmployeeLocationSnapshot.query.filter_by(employee_code="Z1").first()
            assert row.company == "ROBO"
            assert row.plant_location_key == "Robo-AP_RO"

    def test_real_rdc_employee_unaffected(self, db, app):
        """A plant name that doesn't match any Ultrafine/ROBO plant must
        still flow through the normal RDC path untouched."""
        with app.app_context():
            db.session.add(PlantLocation(name="ULT-Raipur", company="Ultrafine", is_active=True))
            db.session.commit()

            tr_raw = [_tr_employee("T2", "Genuine RDC Worker", sub_site="Some Real RDC Plant")]
            result = self._run_rdc_snapshot(zh_raw=[], tr_raw=tr_raw)
            db.session.commit()

            assert result["other_company_employee_rows_written"] == 0
            assert result["employee_rows_written"] == 1
            row = EmployeeLocationSnapshot.query.filter_by(employee_code="T2").first()
            assert row.company is None


class TestSharedTimestampAndCompanyIsolation:
    """
    Two real bugs found and fixed while building _compute_and_store_other_company_snapshot():

    1. Every RDC read-side helper (get_employees_at_plant, get_all_employees,
       ...) finds "the current run" via plain MAX(computed_at) across the
       WHOLE EmployeeLocationSnapshot table. If the Ultrafine/ROBO pass
       wrote a LATER timestamp than the RDC pass in the same refresh cycle,
       MAX(computed_at) would silently become the other company's run and
       every RDC query would return nothing. Fixed by compute_and_store_snapshot()
       passing RDC's own resulting `computed_at` into
       _compute_and_store_other_company_snapshot(now=...) explicitly, so
       both companies share one timestamp.
    2. Those same RDC read-side helpers had no company filter at all — once
       Ultrafine/ROBO rows exist in the same table, they'd leak into RDC-only
       views (the "All Employees" directory, region drill-downs). Fixed by
       adding `.filter(EmployeeLocationSnapshot.company.is_(None))` — RDC
       rows never set `company`, only the new function does.
    """

    def test_rdc_employees_still_findable_after_other_company_snapshot_in_same_cycle(self, db, app):
        with app.app_context():
            shared_now = _dt.utcnow()
            # Simulates an RDC row written by _compute_and_store_snapshot()
            # (company left unset) at the shared timestamp.
            db.session.add(EmployeeLocationSnapshot(
                computed_at=shared_now, source=headcount.ExternalDesignationSource.ZINGHR,
                employee_code="RDC1", employee_name="RDC Employee",
                plant_location_key="Some RDC Plant",
            ))
            db.session.commit()

            zh_raw = [_zh_employee("UF9", "UF Nine", "Ultrafine Mineral and Admixtures Pvt Ltd", "X")]
            with patch("app.services.headcount.zinghr.fetch_active_employees", return_value=zh_raw):
                headcount._compute_and_store_other_company_snapshot(now=shared_now)
                db.session.commit()

            # Both companies share the identical latest computed_at...
            latest = db.session.query(db.func.max(EmployeeLocationSnapshot.computed_at)).scalar()
            assert latest == shared_now
            # ...and the RDC-only read path still finds the RDC employee.
            rdc_employees = headcount.get_employees_at_plant("Some RDC Plant")
            assert len(rdc_employees) == 1
            assert rdc_employees[0].employee_code == "RDC1"

    def test_all_employees_directory_excludes_other_companies(self, db, app):
        with app.app_context():
            shared_now = _dt.utcnow()
            db.session.add(EmployeeLocationSnapshot(
                computed_at=shared_now, source=headcount.ExternalDesignationSource.ZINGHR,
                employee_code="RDC2", employee_name="Another RDC Employee",
            ))
            db.session.commit()
            zh_raw = [_zh_employee("RB9", "Robo Nine", "Robo Silicon Pvt. Ltd.", "X")]
            with patch("app.services.headcount.zinghr.fetch_active_employees", return_value=zh_raw):
                headcount._compute_and_store_other_company_snapshot(now=shared_now)
                db.session.commit()

            rows, total = headcount.get_all_employees()
            codes = {r.employee_code for r in rows}
            assert "RDC2" in codes
            assert "RB9" not in codes


class TestOtherCompanyReadHelpers:
    """
    get_other_company_plant_summary() / get_other_company_unresolved_count() /
    get_other_company_employees_at_plant() — the read side backing the
    Ultrafine/ROBO tabs on the Staffing Status page (added 2026-09-15).
    """

    def test_plant_summary_includes_zero_headcount_plants(self, db, app):
        with app.app_context():
            db.session.add_all([
                PlantLocation(name="UF Plant Empty", company="Ultrafine", is_active=True),
                PlantLocation(name="UF Plant Staffed", company="Ultrafine", is_active=True),
            ])
            db.session.commit()
            zh_raw = [_zh_employee("UF5", "Uma Five", "Ultrafine Mineral and Admixtures Pvt Ltd", "UF Plant Staffed")]
            with patch("app.services.headcount.zinghr.fetch_active_employees", return_value=zh_raw):
                headcount._compute_and_store_other_company_snapshot()
                db.session.commit()

            summary = headcount.get_other_company_plant_summary("Ultrafine")
            by_name = {row["plant"].name: row["headcount"] for row in summary}
            assert by_name["UF Plant Empty"] == 0
            assert by_name["UF Plant Staffed"] == 1

    def test_unresolved_count_and_employees_at_plant(self, db, app):
        with app.app_context():
            db.session.add(PlantLocation(name="ROBO Real Plant", company="ROBO", is_active=True))
            db.session.commit()
            zh_raw = [
                _zh_employee("RB5", "Robo Five", "Robo Silicon Pvt. Ltd.", "ROBO Real Plant"),
                _zh_employee("RB6", "Robo Six", "Robo Silicon Pvt. Ltd.", "Nowhere Known"),
            ]
            with patch("app.services.headcount.zinghr.fetch_active_employees", return_value=zh_raw):
                headcount._compute_and_store_other_company_snapshot()
                db.session.commit()

            assert headcount.get_other_company_unresolved_count("ROBO") == 1
            at_plant = headcount.get_other_company_employees_at_plant("ROBO", "ROBO Real Plant")
            assert len(at_plant) == 1
            assert at_plant[0].employee_code == "RB5"
