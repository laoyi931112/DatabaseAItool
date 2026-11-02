from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Naive UTC timestamp, consistent with how SQLite stores datetimes."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
