"""The ONLY now() call site in the codebase.

Everything else either imports ``now_utc`` or receives time as an argument.
Datetimes are always timezone-aware (UTC).
"""

from datetime import datetime, timezone


def now_utc() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (offset-aware)."""
    return now_utc().isoformat()
