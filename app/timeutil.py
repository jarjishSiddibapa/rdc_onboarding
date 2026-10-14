"""
The one place the app asks "what time is it?".

Every date/time stored in the database is Indian Standard Time (IST, UTC+5:30), as a plain
(naive) datetime - what you read in a MySQL table is the wall-clock time in India. This app is
only used in India, so there is no UTC anywhere in the stored data.

Use now_ist() instead of datetime.utcnow()/datetime.now() for anything that is stored, compared with
a stored time, or shown to a person. It does not depend on the time zone of the computer or the
Docker container the app runs on.
"""
from datetime import datetime, timedelta, timezone

IST_OFFSET = timedelta(hours=5, minutes=30)


def now_ist() -> datetime:
    """Current Indian time as a naive datetime (this is what gets stored)."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + IST_OFFSET
