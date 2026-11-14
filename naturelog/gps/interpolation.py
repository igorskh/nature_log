"""Position lookup on a track by time."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime

from pyproj import Geod

from naturelog.gps.models import Track

_GEOD = Geod(ellps="WGS84")


@dataclass(frozen=True, slots=True)
class TrackPosition:
    time: datetime
    latitude: float
    longitude: float
    elevation: float | None
    #: False when the time coincides with a recorded fix.
    interpolated: bool


def position_at(track: Track, when: datetime) -> TrackPosition | None:
    """Return the position on ``track`` at ``when`` (timezone-aware).

    Between two fixes of the same segment the position is interpolated along
    the WGS84 geodesic, linearly in time. Outside the track, or in the gap
    between two segments, there is no position and ``None`` is returned –
    coordinates are never extrapolated.
    """
    for segment in track.segments:
        if when < segment.start:
            return None
        if when > segment.end:
            continue
        points = segment.points
        i = bisect_left(points, when, key=lambda p: p.time)
        after = points[i]
        if after.time == when:
            return TrackPosition(when, after.latitude, after.longitude, after.elevation, False)
        before = points[i - 1]
        fraction = (when - before.time) / (after.time - before.time)
        azimuth, _, distance = _GEOD.inv(
            before.longitude, before.latitude, after.longitude, after.latitude
        )
        lon, lat, _ = _GEOD.fwd(before.longitude, before.latitude, azimuth, distance * fraction)
        elevation = None
        if before.elevation is not None and after.elevation is not None:
            elevation = before.elevation + (after.elevation - before.elevation) * fraction
        return TrackPosition(when, lat, lon, elevation, True)
    return None
