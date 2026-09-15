"""
Background 2-hourly refresh of the RDC staffing headcount snapshot.

Mirrors the daemon-thread pattern already used for Truein push retries
(app/integrations/truein.py: start_retry_thread()/resume_pending_retries())
rather than adding a scheduler dependency (no APScheduler/Celery in this
project). Started once from create_app(); the gate and the staffing-status
dashboard only ever read the resulting StaffingSnapshot rows (see
app/services/headcount.py) — neither ever calls ZingHR/Truein/DVT live.
"""
import threading
import time

_REFRESH_INTERVAL_S = 30 * 60  # 30 minutes

_started = False
_lock = threading.Lock()


def _refresh_loop(app):
    from . import headcount
    with app.app_context():
        while True:
            try:
                result = headcount.compute_and_store_snapshot()
                app.logger.info(f"[StaffingSnapshot] refreshed: {result}")
            except Exception as exc:
                app.logger.error(f"[StaffingSnapshot] refresh failed: {exc}")
            time.sleep(_REFRESH_INTERVAL_S)


def start_snapshot_refresh_thread(app) -> bool:
    """
    Spawn the background refresh thread once per process. Safe to call
    multiple times — only the first call actually starts a thread.
    Returns True if a new thread was started, False if one was already running.
    """
    global _started
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
