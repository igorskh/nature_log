"""Load a GPS log file (GPX or NMEA 0183) as a :class:`Track`."""

from __future__ import annotations

import re
from pathlib import Path

from naturelog.gps.gpx_io import GpxError, parse_gpx, track_from_gpx
from naturelog.gps.models import Track
from naturelog.gps.nmea import NmeaError, nmea_to_gpx

GPX_SUFFIXES = (".gpx",)
NMEA_SUFFIXES = (".nmea", ".nma", ".log", ".txt")

_NMEA_SENTENCE = re.compile(r"\$[A-Z]{5},")


class GpsLogError(Exception):
    """A GPS log could not be read. The message is suitable for the user."""


def detect_format(path: Path, text: str) -> str | None:
    """Return ``"gpx"`` or ``"nmea"`` from content, falling back to the suffix."""
    head = text[:65536]
    if head.lstrip("﻿ \t\r\n").startswith("<"):
        return "gpx"
    if _NMEA_SENTENCE.search(head):
        return "nmea"
    suffix = path.suffix.lower()
    if suffix in GPX_SUFFIXES:
        return "gpx"
    if suffix in NMEA_SUFFIXES:
        return "nmea"
    return None


def load_track(path: str | Path) -> Track:
    """Read ``path`` without modifying it. NMEA logs are converted to GPX first."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise GpsLogError(f"Cannot read {path.name}: {exc.strerror or exc}") from exc

    source_format = detect_format(path, text)
    try:
        if source_format == "gpx":
            gpx = parse_gpx(text)
        elif source_format == "nmea":
            gpx = nmea_to_gpx(text, name=path.stem)
        else:
            raise GpsLogError(
                f"{path.name} is not a recognised GPS log. Supported formats: GPX, NMEA 0183."
            )
        return track_from_gpx(
            gpx, name=path.stem, source_path=path, source_format=source_format
        )
    except (GpxError, NmeaError) as exc:
        raise GpsLogError(f"{path.name}: {exc}") from exc
