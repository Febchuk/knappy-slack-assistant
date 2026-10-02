"""When the running process should include the morning cadence scan."""

from __future__ import annotations

from datetime import date, datetime


def cadence_due(now: datetime, last_digest_date: date | None) -> bool:
    """Cadence and the morning digest run once during the 8:00 hour."""
    return now.hour == 8 and last_digest_date != now.date()
