"""
Retention for the append-only staffing history (added 2026-10-12).

Every sync writes a complete new copy of `staffing_snapshots` and `employee_location_snapshots` and nothing
ever deleted the old ones (~1.5M rows after a few weeks, 95% of every database backup). Only the newest run
is shown or used by the hiring gate (plus the last few runs for the "last known volume" fallback), so older
runs are dead weight. This keeps the newest N runs (admin-editable, default 14, never fewer than 5) and
removes the rest, in small batches so MySQL is never held by one huge delete.

A "run" is one distinct `computed_at` - a sync stamps both tables with the same value, and hires added
between syncs reuse the current run's value, so they age out together with it.
"""
from datetime import datetime, timedelta
from app.timeutil import now_ist

from ..extensions import db
from ..models import SystemConfig, StaffingSnapshot, EmployeeLocationSnapshot

KEY = "snapshot_keep_runs"
DEFAULT_KEEP_RUNS = 14
MIN_KEEP_RUNS = 5          # get_last_known_plant_volumes() looks at the last 5 runs
MAX_KEEP_RUNS = 365
_BATCH = 20000
_BACKUP_MAX_AGE = timedelta(hours=48)


def get_keep_runs() -> int:
    row = SystemConfig.query.filter_by(key=KEY).first()
    try:
        n = int(row.value) if row and row.value not in (None, "") else DEFAULT_KEEP_RUNS
    except (TypeError, ValueError):
        n = DEFAULT_KEEP_RUNS
    return max(MIN_KEEP_RUNS, min(MAX_KEEP_RUNS, n))


def set_keep_runs(n: int) -> int:
    """Store the setting (clamped). Caller commits. Returns the stored value."""
    n = max(MIN_KEEP_RUNS, min(MAX_KEEP_RUNS, int(n)))
    row = SystemConfig.query.filter_by(key=KEY).first()
    if row is None:
        row = SystemConfig(key=KEY)
        db.session.add(row)
    row.value = str(n)
    return n


def stats() -> dict:
    """Current size of the history, for the admin page."""
    runs = db.session.query(db.func.count(db.distinct(StaffingSnapshot.computed_at))).scalar() or 0
    oldest = db.session.query(db.func.min(StaffingSnapshot.computed_at)).scalar()
    return {
        "runs": runs,
        "staffing_rows": db.session.query(db.func.count(StaffingSnapshot.id)).scalar() or 0,
        "employee_rows": db.session.query(db.func.count(EmployeeLocationSnapshot.id)).scalar() or 0,
        "oldest": oldest,
        "keep_runs": get_keep_runs(),
    }


def _cutoff(keep: int):
    """computed_at of the keep-th newest run; everything strictly older can go. None = fewer runs than that."""
    rows = (db.session.query(StaffingSnapshot.computed_at).distinct()
            .order_by(StaffingSnapshot.computed_at.desc()).limit(keep).all())
    return rows[-1][0] if len(rows) >= keep else None


def _delete_older(model, cutoff) -> int:
    total = 0
    while True:
        ids = [i for (i,) in db.session.query(model.id).filter(model.computed_at < cutoff).limit(_BATCH).all()]
        if not ids:
            return total
        db.session.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
        total += len(ids)


def prune(keep_runs: int | None = None, require_recent_backup: bool = False) -> dict:
    """Delete everything older than the newest `keep_runs` runs. Never touches the newest run.

    require_recent_backup=True (the automatic nightly call) skips the cleanup if the database backup is
    switched on but has not produced a good file in the last 48h - if backups are broken, don't also be
    deleting history. Returns {"deleted_staffing", "deleted_employees", ...} or {"skipped": reason}."""
    keep = max(MIN_KEEP_RUNS, keep_runs if keep_runs is not None else get_keep_runs())
    if require_recent_backup:
        from . import db_backup
        from ..models import BackupRun
        if db_backup.get_settings().enabled:
            last_ok = (BackupRun.query.filter_by(status="success")
                       .order_by(BackupRun.started_at.desc()).first())
            if last_ok is None or now_ist() - last_ok.started_at > _BACKUP_MAX_AGE:
                return {"skipped": "database backup is on but has no good backup in the last 48 hours"}
    cutoff = _cutoff(keep)
    if cutoff is None:
        return {"deleted_staffing": 0, "deleted_employees": 0, "kept_runs": keep, "cutoff": None}
    return {
        "deleted_staffing": _delete_older(StaffingSnapshot, cutoff),
        "deleted_employees": _delete_older(EmployeeLocationSnapshot, cutoff),
        "kept_runs": keep,
        "cutoff": cutoff.isoformat(),
    }
