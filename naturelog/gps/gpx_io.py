"""Reading GPX documents into :class:`Track` models."""

from __future__ import annotations

from datetime import timezone
from pathlib import Path

import gpxpy
import gpxpy.gpx

from naturelog.gps.models import Track, TrackPoint, TrackSegment


class GpxError(ValueError):
    """The GPX document is invalid or has no usable timestamped fixes."""


def parse_gpx(text: str) -> gpxpy.gpx.GPX:
    try:
        return gpxpy.parse(text)
    except Exception as exc:  # gpxpy raises several unrelated exception types
        raise GpxError(f"Not a valid GPX file: {exc}") from exc


def track_from_gpx(
    gpx: gpxpy.gpx.GPX,
    *,
    name: str,
    source_path: Path | None = None,
    source_format: str = "gpx",
) -> Track:
    """Build a :class:`Track` from all track segments of a GPX document.

    Fixes without a timestamp cannot be synchronised with media and are
    skipped (counted in ``Track.skipped_points``). Timestamps without a zone
    are read as UTC, as the GPX specification requires.
    """
    segments: list[TrackSegment] = []
    skipped = 0
    for gpx_track in gpx.tracks:
        for gpx_segment in gpx_track.segments:
            by_time: dict = {}
            for p in gpx_segment.points:
                if p.time is None:
                    skipped += 1
                    continue
                time = p.time
                if time.tzinfo is None:
                    time = time.replace(tzinfo=timezone.utc)
                time = time.astimezone(timezone.utc)
                if time in by_time:
                    skipped += 1
                    continue
                by_time[time] = TrackPoint(time, p.latitude, p.longitude, p.elevation)
            if by_time:
                points = tuple(by_time[t] for t in sorted(by_time))
                segments.append(TrackSegment(points))

    if not segments:
        raise GpxError("The GPS log contains no track points with timestamps.")

    segments.sort(key=lambda s: s.start)
    track_name = next((t.name for t in gpx.tracks if t.name), None) or name
    return Track(
        name=track_name,
        segments=tuple(segments),
        source_path=source_path,
        source_format=source_format,
        skipped_points=skipped,
    )
