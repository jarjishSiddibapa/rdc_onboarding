"""
Background once-a-night refresh of the RDC staffing headcount snapshot.

(Was every 30 minutes until 2026-10-08. Between nightly runs, a hire whose
approval just completed is added straight into the current snapshot by
headcount.record_approved_hire(); the nightly run re-reads ZingHR/Truein and
replaces those interim rows. "Sync Now" on the dashboard still triggers a
full run on demand.)

Mirrors the daemon-thread pattern already used for Truein push retries
(app/integrations/truein.py: start_retry_thread()/resume_pending_retries())
rather than adding a scheduler dependency (no APScheduler/Celery in this
project). Started once from create_app(); the gate and the staffing-status
dashboard only ever read the resulting StaffingSnapshot rows (see
app/services/headcount.py) — neither ever calls ZingHR/Truein/DVT live.
"""
import os
import threading
import time
from datetime import datetime, timedelta

# Local clock hour (IST) of the nightly run. Override with SNAPSHOT_SYNC_HOUR_IST.
_SYNC_HOUR_IST = int(os.environ.get("SNAPSHOT_SYNC_HOUR_IST", "2"))
_IST_OFFSET = timedelta(hours=5, minutes=30)
_POLL_S = 60                   # how often the loop re-checks the clock
_MAX_AGE = timedelta(hours=24)  # a snapshot older than this is overdue (missed night / fresh DB)

_started = False
_lock = threading.Lock()


def _next_sync_utc(now_utc: datetime) -> datetime:
    """Next occurrence of the nightly slot (IST wall clock), as naive UTC."""
    now_ist = now_utc + _IST_OFFSET
    slot = now_ist.replace(hour=_SYNC_HOUR_IST, minute=0, second=0, microsecond=0)
    if slot <= now_ist:
        slot += timedelta(days=1)
    return slot - _IST_OFFSET


def _is_overdue() -> bool:
    """True if there's no snapshot at all, or the newest one is older than a day."""
    from ..extensions import db
    from ..models import StaffingSnapshot
    last = db.session.query(db.func.max(StaffingSnapshot.computed_at)).scalar()
    return last is None or (datetime.utcnow() - last) > _MAX_AGE


def _warm_caches(app):
    """
    Fill the in-process ZingHR/Truein employee caches WITHOUT writing a snapshot.
    Those caches are per-process (CLAUDE.md gotcha #1) and feed the form's Reporting
    Manager picker and the duplicate Aadhaar/mobile/email checks — with the snapshot
    only recomputed at night, a mid-day restart would otherwise leave them empty
    until 2am.
    """
    from ..integrations import truein, zinghr
    for name, fn in (("Truein", truein._fetch_all_employees_raw), ("ZingHR", zinghr.fetch_active_employees)):
        try:
            # Truein's pull takes ~8 min; if the copy saved on disk by the last pull is still usable,
            # a restart just reuses it instead of starting another one.
            if name == "Truein" and truein.get_cached_employees_if_warm() is not None:
                continue
            fn()
        except Exception as exc:
            app.logger.error(f"[StaffingSnapshot] {name} cache warm-up failed: {exc}")


def _refresh_loop(app):
    from . import headcount
    from ..extensions import db
    with app.app_context():
        next_run = None
        while True:
            try:
                now = datetime.utcnow()
                if next_run is None:
                    # Boot: catch up only if we actually missed a night (server was off at the
                    # slot, or brand-new DB) — a plain restart mid-day does NOT trigger a pull.
                    if _is_overdue():
                        next_run = now
                    else:
                        _warm_caches(app)
                        next_run = _next_sync_utc(datetime.utcnow())
                if now >= next_run:
                    result = headcount.compute_and_store_snapshot()
                    app.logger.info(f"[StaffingSnapshot] refreshed: {result}")
                    next_run = _next_sync_utc(datetime.utcnow())
            except Exception as exc:
                app.logger.error(f"[StaffingSnapshot] refresh failed: {exc}")
                # Don't hammer a failing source: retry in 30 minutes, not every poll tick.
                next_run = datetime.utcnow() + timedelta(minutes=30)
            finally:
                # This thread holds ONE app context for the process lifetime. Without releasing the session
                # each tick, an idle read transaction outlives MySQL's wait_timeout (8h) and every later
                # query - including the 02:00 sync - fails until restart (PendingRollbackError).
                try:
                    db.session.remove()
                except Exception:
                    pass
            time.sleep(_POLL_S)


def start_snapshot_refresh_thread(app) -> bool:
    """
    Spawn the background refresh thread once per process. Safe to call
    multiple times — only the first call actually starts a thread.
    Returns True if a new thread was started, False if one was already running.
    """
    global _started
    # Never in tests: the loop reads the DB from its own thread, which on the tests' shared
    # in-memory SQLite connection corrupts whichever test is mid-transaction.
    if app.config.get("TESTING"):
        return False
    with _lock:
        if _started:
            return False
        _started = True
    t = threading.Thread(target=_refresh_loop, args=(app,), daemon=True)
    t.start()
    return True


def refresh_snapshot_now() -> dict:
    """Synchronous one-off refresh — for an admin "Refresh Now" button or manual testing."""
    from . import headcount
    return headcount.compute_and_store_snapshot()


def is_refresh_in_progress() -> bool:
    """
    Non-blocking check: is a snapshot refresh (the automatic thread, or any
    manual trigger, in this or another process) currently running? Reads the
    MySQL advisory lock without acquiring it — instant, safe to call from a
    request handler. Always False on non-MySQL (e.g. sqlite in tests).
    """
    from ..extensions import db
    from .headcount import _SNAPSHOT_LOCK_NAME
    if db.engine.dialect.name != "mysql":
        return False
    with db.engine.connect() as conn:
        holder = conn.execute(db.text("SELECT IS_USED_LOCK(:name)"), {"name": _SNAPSHOT_LOCK_NAME}).scalar()
    return holder is not None


def trigger_manual_refresh(app) -> dict:
    """
    Non-blocking "Sync Now" trigger for the dashboard widget. If a refresh is
    already running anywhere (the automatic thread or another manual
    trigger), does nothing and reports that — never doubles up. Otherwise
    starts one in a background thread (a full run can take several minutes
    on a cold Truein cache — see CLAUDE.md gotcha #1) and returns
    immediately; the frontend polls refresh_status() for completion.
    """
    if is_refresh_in_progress():
        return {"status": "already_in_progress"}

    def _run():
        from . import headcount
        with app.app_context():
            try:
                result = headcount.compute_and_store_snapshot()
                app.logger.info(f"[StaffingSnapshot] manual refresh: {result}")
            except Exception as exc:
                app.logger.error(f"[StaffingSnapshot] manual refresh failed: {exc}")

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}


def refresh_status() -> dict:
    """Current sync state for the dashboard widget: in-progress flag + last-synced timestamp."""
    from ..extensions import db
    from ..models import StaffingSnapshot
    last_synced_at = db.session.query(db.func.max(StaffingSnapshot.computed_at)).scalar()
    return {
        "in_progress": is_refresh_in_progress(),
        "last_synced_at": last_synced_at.isoformat() if last_synced_at else None,
    }
