"""Display timezones. Independent of Qt.

Timestamps are stored and processed in UTC; these helpers only convert them
for display in a user-selected IANA timezone.
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from functools import cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

UTC_NAME = "UTC"


@cache
def timezone_names() -> tuple[str, ...]:
    """Sorted IANA timezone names, with ``UTC`` first."""
    names = {
        name
        for name in available_timezones()
        if "/" in name and not name.startswith(("Etc/", "SystemV/", "posix/", "right/"))
    }
    return (UTC_NAME, *sorted(names))


def get_timezone(name: str) -> tzinfo | None:
    """The timezone for an IANA name, or ``None`` if it is unknown."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def format_offset(when: datetime, *, numeric_zero: bool = False) -> str:
    """UTC offset of an aware datetime as ``UTC+02:00``.

    A zero offset is shown as ``UTC`` unless ``numeric_zero`` is set.
    """
    offset = when.utcoffset()
    if offset is None:
        return UTC_NAME
    minutes = int(offset.total_seconds() // 60)
    if minutes == 0 and not numeric_zero:
        return UTC_NAME
    sign = "+" if minutes >= 0 else "-"
    hours, minutes = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def format_datetime(when: datetime, tz: tzinfo, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format an aware datetime in ``tz``, followed by its zone abbreviation and offset."""
    local = when.astimezone(tz)
    offset = format_offset(local)
    abbreviation = local.tzname() or ""
    # Some zones have no abbreviation and report the numeric offset instead.
    if not abbreviation or abbreviation[0] in "+-" or abbreviation == UTC_NAME:
        return f"{local.strftime(fmt)} {offset}"
    return f"{local.strftime(fmt)} {abbreviation} ({offset})"
