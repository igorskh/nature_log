"""NMEA 0183 parsing and conversion to GPX.

Position fixes are taken from RMC and GGA sentences (any talker: GP, GN, GL,
GA, ...). RMC and ZDA supply the calendar date; GGA supplies elevation.
Sentences with a bad checksum, an invalid fix status or malformed fields are
ignored. A log without any date information is rejected rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import reduce

import gpxpy.gpx


class NmeaError(ValueError):
    """The NMEA log has no usable fixes."""


@dataclass(slots=True)
class _Fix:
    latitude: float
    longitude: float
    elevation: float | None = None


_HALF_DAY = timedelta(hours=12)


def _fields(line: str) -> list[str] | None:
    """Return the comma-separated fields of a sentence, or None if invalid."""
    start = line.find("$")
    if start < 0:
        return None
    body, star, checksum = line[start + 1 :].strip().partition("*")
    if star:
        try:
            expected = int(checksum[:2], 16)
        except ValueError:
            return None
        if reduce(lambda acc, ch: acc ^ ord(ch), body, 0) != expected:
            return None
    fields = body.split(",")
    return fields if len(fields[0]) == 5 else None


def _time(value: str) -> time:
    seconds = float(value[4:])
    whole = int(seconds)
    return time(
        int(value[0:2]),
        int(value[2:4]),
        whole,
        round((seconds - whole) * 1_000_000) % 1_000_000,
    )


def _coordinate(value: str, hemisphere: str) -> float:
    raw = float(value)
    degrees = int(raw // 100)
    result = degrees + (raw - degrees * 100) / 60
    if hemisphere in ("S", "W"):
        return -result
    if hemisphere in ("N", "E"):
        return result
    raise ValueError(f"bad hemisphere {hemisphere!r}")


def _rmc_date(value: str) -> date:
    year = int(value[4:6])
    return date(1900 + year if year >= 80 else 2000 + year, int(value[2:4]), int(value[0:2]))


def parse_nmea(text: str) -> list[tuple[datetime, float, float, float | None]]:
    """Parse NMEA text into time-sorted ``(utc_time, lat, lon, elevation)`` fixes."""
    fixes: dict[datetime, _Fix] = {}
    # GGA fixes seen before the first sentence carrying a date.
    pending: list[tuple[time, _Fix]] = []
    current_date: date | None = None
    last_tod: time | None = None

    def add(when: datetime, fix: _Fix) -> None:
        existing = fixes.get(when)
        if existing is None:
            fixes[when] = fix
        elif existing.elevation is None:
            existing.elevation = fix.elevation

    for line in text.splitlines():
        fields = _fields(line)
        if fields is None:
            continue
        kind = fields[0][2:]
        try:
            if kind == "RMC":
                tod, new_date = _time(fields[1]), _rmc_date(fields[9])
                fix = (
                    _Fix(_coordinate(fields[3], fields[4]), _coordinate(fields[5], fields[6]))
                    if fields[2] == "A"
                    else None
                )
            elif kind == "GGA":
                tod, new_date = _time(fields[1]), None
                if int(fields[6] or 0) == 0:
                    fix = None
                else:
                    fix = _Fix(
                        _coordinate(fields[2], fields[3]),
                        _coordinate(fields[4], fields[5]),
                        float(fields[9]) if fields[9] else None,
                    )
            elif kind == "ZDA":
                tod = _time(fields[1])
                new_date = date(int(fields[4]), int(fields[3]), int(fields[2]))
                fix = None
            else:
                continue
        except (ValueError, IndexError):
            continue

        if new_date is not None:
            if current_date is None:
                # Date the fixes that arrived before any date was known.
                for p_tod, p_fix in pending:
                    p_date = new_date if p_tod <= tod else new_date - timedelta(days=1)
                    add(datetime.combine(p_date, p_tod, timezone.utc), p_fix)
                pending.clear()
            current_date = new_date
        elif current_date is not None and last_tod is not None:
            # Dateless sentence after midnight rollover.
            today = date.min
            if datetime.combine(today, last_tod) - datetime.combine(today, tod) > _HALF_DAY:
                current_date += timedelta(days=1)
        last_tod = tod

        if fix is None:
            continue
        if current_date is None:
            pending.append((tod, fix))
        else:
            add(datetime.combine(current_date, tod, timezone.utc), fix)

    if not fixes:
        if pending:
            raise NmeaError(
                "The NMEA log has positions but no date (no RMC or ZDA sentences), "
                "so the fixes cannot be placed in time."
            )
        raise NmeaError("The NMEA log contains no valid position fixes.")

    return [(t, f.latitude, f.longitude, f.elevation) for t, f in sorted(fixes.items())]


def nmea_to_gpx(text: str, *, name: str | None = None) -> gpxpy.gpx.GPX:
    """Convert NMEA 0183 text to a GPX document with a single track segment."""
    gpx = gpxpy.gpx.GPX()
    gpx.creator = "NatureLog"
    track = gpxpy.gpx.GPXTrack(name=name)
    segment = gpxpy.gpx.GPXTrackSegment()
    track.segments.append(segment)
    gpx.tracks.append(track)
    for when, lat, lon, elevation in parse_nmea(text):
        segment.points.append(
            gpxpy.gpx.GPXTrackPoint(lat, lon, elevation=elevation, time=when)
        )
    return gpx
