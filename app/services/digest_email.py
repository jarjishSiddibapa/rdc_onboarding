"""
Background daily digest email — for users who opted into
User.daily_digest_enabled (Profile -> Email Notifications). This is an
add-on, not a replacement for real-time mail (see notify_users() in
app/utils.py): a user keeps getting real-time email per their email_mode,
plus one rollup email per day if they've also checked this box.

Reuses the existing Notification table as its data source — every real-time
event already writes one in-app Notification row per recipient
(create_in_app_notification(), app/utils.py), so this job just reads back
whatever accumulated since the last digest for each opted-in user rather
than maintaining a separate queue.

Mirrors the daemon-thread pattern already used for snapshot refresh
(app/services/snapshot_refresh.py) and Truein retries
(app/integrations/truein.py) rather than adding a scheduler dependency.
Started once from create_app().
"""
import threading
import time
from datetime import datetime, timedelta

_CHECK_INTERVAL_S = 60 * 60       # check hourly...
_DIGEST_INTERVAL = timedelta(hours=24)  # ...but only actually send once per user per 24h

_started = False
_lock = threading.Lock()


def send_due_digests(app) -> dict:
    """
    One pass: for every active user with daily_digest_enabled=True whose
    last digest was sent more than _DIGEST_INTERVAL ago (or never), gather
    their Notification rows created since then and email one combined
    summary. Returns a small result dict for logging/manual testing.
    """
    from ..extensions import db
    from ..models import User, Notification
    from ..utils import send_email

    sent = skipped_empty = 0
    now = datetime.utcnow()
    cutoff = now - _DIGEST_INTERVAL

    users = User.query.filter_by(is_active=True, daily_digest_enabled=True).all()
    for user in users:
        if user.last_digest_sent_at and user.last_digest_sent_at > cutoff:
            continue  # not due yet

        since = user.last_digest_sent_at or (now - _DIGEST_INTERVAL)
        notifs = (Notification.query
                  .filter(Notification.recipient_id == user.id, Notification.sent_at > since)
                  .order_by(Notification.sent_at.asc()).all())

        if notifs:
            lines = [f"Daily summary — {len(notifs)} update(s) since your last digest:\n"]
            for n in notifs:
                snippet = (n.body or "").strip().splitlines()[0] if n.body else ""
                lines.append(f"• {n.subject}\n  {snippet}\n")
            lines.append("\n— RDC Teamlease HR Onboarding Portal")
            body = "\n".join(lines)
            send_email(f"Daily summary — {len(notifs)} update(s)", [user.email], body)
            sent += 1
        else:
            skipped_empty += 1

        user.last_digest_sent_at = now

    db.session.commit()
    return {"sent": sent, "skipped_empty": skipped_empty, "checked": len(users)}


def _digest_loop(app):
    with app.app_context():
        while True:
            try:
                result = send_due_digests(app)
                app.logger.info(f"[DigestEmail] pass complete: {result}")
            except Exception as exc:
                app.logger.error(f"[DigestEmail] pass failed: {exc}")
            time.sleep(_CHECK_INTERVAL_S)


def start_digest_thread(app) -> bool:
    """Spawn the background digest thread once per process. Safe to call multiple times."""
    global _started
    with _lock:
        if _started:
            return False
        _started = True
    t = threading.Thread(target=_digest_loop, args=(app,), daemon=True)
    t.start()
    return True
