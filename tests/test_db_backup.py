"""Automatic full-database backup (2026-10-10): dump format, settings page, scheduler rules, retention."""
import os
from datetime import datetime, timedelta
from unittest import mock

import pytest

from app.extensions import db as _db
from app.models import BackupRun, UserRole
from app.services import db_backup
from .conftest import login, _make_user


@pytest.fixture()
def backup_dir(tmp_path, app):
    s = db_backup.get_settings()
    s.backup_dir = str(tmp_path)
    _db.session.commit()
    return tmp_path


def _admin(db, email="bk-admin@t.com"):
    return _make_user("BkAdmin", email, UserRole.SUPER_ADMIN, db)


class TestDumpFormat:
    def test_dump_is_complete_and_restorable_layout(self, app, db, tmp_path):
        _make_user("Dump One", "dump1@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        db.session.commit()
        out = tmp_path / "x.sql"
        with open(out, "w", encoding="utf-8") as f:
            stats = db_backup.write_dump(_db.engine, f)
        text = out.read_text(encoding="utf-8")
        assert stats["tables"] > 10 and stats["rows"] >= 1
        assert "DROP TABLE IF EXISTS `users`;" in text
        assert "CREATE TABLE" in text and "INSERT INTO `users` VALUES" in text
        assert "dump1@t.com" in text
        assert text.rstrip().splitlines()[-1].startswith(db_backup.COMPLETE_MARKER)
        assert "FOREIGN_KEY_CHECKS=0" in text

    def test_literals_are_escaped(self):
        lit = db_backup.sql_literal
        assert lit(None) == "NULL" and lit(True) == "1" and lit(5) == "5"
        assert lit("it's \\ fine\nnew") == "'it\\'s \\\\ fine\\nnew'"
        assert lit(b"\x00\xff") == "0x00ff"
        assert lit(datetime(2026, 10, 10, 4, 0, 0)) == "'2026-10-10 04:00:00'"
        assert lit({"a": "b"}) == "'{\"a\": \"b\"}'"


class TestRunBackup:
    def test_manual_run_writes_file_and_records_it(self, app, db, backup_dir):
        _make_user("Run One", "run1@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        db.session.commit()
        rid = db_backup.run_backup(app, "manual", None)
        run = _db.session.get(BackupRun, rid)
        assert run.status == "success" and run.size_bytes > 0 and run.tables_count > 10
        assert os.path.isfile(run.file_path) and os.path.dirname(run.file_path) == str(backup_dir)
        assert not [p for p in os.listdir(backup_dir) if p.endswith(".partial")]

    def test_failure_is_recorded_and_leaves_no_file(self, app, db, backup_dir):
        with mock.patch.object(db_backup, "write_dump", side_effect=RuntimeError("disk exploded")):
            rid = db_backup.run_backup(app, "manual", None)
        run = _db.session.get(BackupRun, rid)
        assert run.status == "failed" and "disk exploded" in run.message
        assert os.listdir(backup_dir) == []

    def test_second_run_while_one_is_running_is_refused(self, app, db, backup_dir):
        assert db_backup._run_lock.acquire(blocking=False)
        try:
            assert db_backup.run_backup(app, "manual", None) is None
        finally:
            db_backup._run_lock.release()


def _ist(h, m):
    """A UTC instant whose IST wall clock is today h:m."""
    return datetime(2026, 10, 10, h, m) - db_backup._IST_OFFSET


class TestSchedule:
    def test_off_means_never_due(self, app, db):
        s = db_backup.get_settings()
        s.enabled = False
        assert db_backup.due_now(s, _ist(12, 0)) is False
        assert db_backup.next_run_ist(s) is None

    def test_not_due_before_the_time_and_due_after(self, app, db):
        s = db_backup.get_settings()
        s.enabled, s.hour, s.minute = True, 4, 0
        assert db_backup.due_now(s, _ist(3, 59)) is False
        assert db_backup.due_now(s, _ist(4, 1)) is True      # also the catch-up case

    def test_not_due_again_once_todays_run_succeeded(self, app, db):
        s = db_backup.get_settings()
        s.enabled, s.hour, s.minute = True, 4, 0
        _db.session.add(BackupRun(filename="a.sql", status="success", triggered_by="schedule", started_at=_ist(4, 0)))
        _db.session.commit()
        assert db_backup.due_now(s, _ist(9, 0)) is False

    def test_failed_run_retries_after_half_an_hour_up_to_three_times(self, app, db):
        s = db_backup.get_settings()
        s.enabled, s.hour, s.minute = True, 4, 0
        _db.session.add(BackupRun(filename="a.sql", status="failed", triggered_by="schedule", started_at=_ist(4, 0)))
        _db.session.commit()
        assert db_backup.due_now(s, _ist(4, 10)) is False
        assert db_backup.due_now(s, _ist(4, 40)) is True
        for i in (1, 2):
            _db.session.add(BackupRun(filename=f"b{i}.sql", status="failed", triggered_by="schedule",
                                      started_at=_ist(4, 30 + i)))
        _db.session.commit()
        assert db_backup.due_now(s, _ist(8, 0)) is False

    def test_manual_runs_do_not_count_as_the_scheduled_one(self, app, db):
        s = db_backup.get_settings()
        s.enabled, s.hour, s.minute = True, 4, 0
        _db.session.add(BackupRun(filename="m.sql", status="success", triggered_by="manual", started_at=_ist(4, 5)))
        _db.session.commit()
        assert db_backup.due_now(s, _ist(5, 0)) is True


class TestRetention:
    def test_old_files_removed_newest_three_kept(self, app, db, backup_dir):
        s = db_backup.get_settings()
        s.retention_days = 7
        old = datetime.utcnow() - timedelta(days=30)
        paths = []
        for i in range(5):
            name = f"old{i}.sql"
            p = backup_dir / name
            p.write_text("x")
            paths.append(p)
            _db.session.add(BackupRun(filename=name, file_path=str(p), status="success", size_bytes=1,
                                      started_at=old - timedelta(days=i)))
        _db.session.commit()
        assert db_backup.apply_retention(s) == 2
        assert [p.exists() for p in paths] == [True, True, True, False, False]

    def test_zero_keeps_everything(self, app, db, backup_dir):
        s = db_backup.get_settings()
        s.retention_days = 0
        assert db_backup.apply_retention(s) == 0


class TestAdminPage:
    def test_only_super_admin_can_open(self, client, db, app):
        init = _make_user("BkInit", "bkinit@t.com", UserRole.INITIATOR, db, companies=["RDC"])
        with app.app_context():
            login(client, init.email)
            assert client.get("/admin/backups").status_code == 403

    def test_page_renders_and_settings_save(self, client, db, app, backup_dir):
        admin = _admin(db)
        with app.app_context():
            login(client, admin.email)
            assert b"Database Backup" in client.get("/admin/backups").data
            r = client.post("/admin/backups", data={
                "time": "03:30", "retention_days": "30", "backup_dir": str(backup_dir),
                "email_enabled": "1", "email_recipients": "a@x.com; b@y.com"}, follow_redirects=True)
            assert b"Backup settings saved" in r.data
            s = db_backup.get_settings()
            assert (s.enabled, s.hour, s.minute, s.retention_days) == (False, 3, 30, 30)   # box unticked = OFF
            assert s.email_enabled and s.email_recipients == "a@x.com, b@y.com"

    def test_turning_it_on_and_off(self, client, db, app, backup_dir):
        admin = _admin(db, "bk2@t.com")
        with app.app_context():
            login(client, admin.email)
            client.post("/admin/backups", data={"enabled": "1", "time": "04:00", "retention_days": "14",
                                                "backup_dir": str(backup_dir)})
            assert db_backup.get_settings().enabled is True
            client.post("/admin/backups", data={"time": "04:00", "retention_days": "14", "backup_dir": str(backup_dir)})
            assert db_backup.get_settings().enabled is False

    @pytest.mark.parametrize("data,msg", [
        ({"time": "25:99", "retention_days": "14"}, b"HH:MM"),
        ({"time": "04:00", "retention_days": "-3"}, b"0 to 3650"),
        ({"time": "04:00", "retention_days": "14", "email_enabled": "1", "email_recipients": ""}, b"at least one"),
        ({"time": "04:00", "retention_days": "14", "email_recipients": "nope"}, b"look wrong"),
        ({"time": "04:00", "retention_days": "14", "backup_dir": "relative/path"}, b"full folder path"),
    ])
    def test_bad_input_is_rejected(self, client, db, app, data, msg):
        admin = _admin(db, "bk3@t.com")
        with app.app_context():
            login(client, admin.email)
            r = client.post("/admin/backups", data=data, follow_redirects=True)
            assert msg in r.data

    def test_folder_inside_static_is_refused(self, client, db, app):
        admin = _admin(db, "bk4@t.com")
        with app.app_context():
            login(client, admin.email)
            bad = os.path.join(app.root_path, "static", "backups")
            r = client.post("/admin/backups", data={"time": "04:00", "retention_days": "14", "backup_dir": bad},
                            follow_redirects=True)
            assert b"static folder" in r.data
            assert not os.path.exists(bad)

    def test_download_and_delete(self, client, db, app, backup_dir):
        admin = _admin(db, "bk5@t.com")
        email = admin.email
        rid = db_backup.run_backup(app, "manual", None)
        with app.app_context():
            login(client, email)
            r = client.get(f"/admin/backups/{rid}/download")
            assert r.status_code == 200 and b"CREATE TABLE" in r.data
            r.close()   # Windows keeps the file locked until the download response is closed
            path = _db.session.get(BackupRun, rid).file_path
            client.post(f"/admin/backups/{rid}/delete")
            assert not os.path.exists(path)
            assert client.get(f"/admin/backups/{rid}/download", follow_redirects=False).status_code == 302

    def test_download_refuses_a_tampered_record(self, client, db, app, backup_dir):
        admin = _admin(db, "bk6@t.com")
        evil = backup_dir / "secret.txt"
        evil.write_text("nope")
        run = BackupRun(filename="../secret.txt", file_path=str(evil), status="success", size_bytes=4)
        _db.session.add(run)
        _db.session.commit()
        with app.app_context():
            login(client, admin.email)
            r = client.get(f"/admin/backups/{run.id}/download", follow_redirects=False)
            assert r.status_code == 302   # not served
