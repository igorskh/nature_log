"""Domain models for GPS tracks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TrackPoint:
    """A recorded GPS fix. ``time`` is always timezone-aware (UTC)."""

    time: datetime
    latitude: float
    longitude: float
    elevation: float | None = None


@dataclass(frozen=True, slots=True)
class TrackSegment:
    """A continuous run of fixes, sorted by strictly increasing time."""

    points: tuple[TrackPoint, ...]

    @property
    def start(self) -> datetime:
        return self.points[0].time

    @property
    def end(self) -> datetime:
        return self.points[-1].time


@dataclass(frozen=True, slots=True)
class Track:
    """A GPS track made of one or more non-empty segments in time order."""

    name: str
    segments: tuple[TrackSegment, ...]
    source_path: Path | None = None
    source_format: str = "gpx"
    #: Fixes in the source that could not be used (no timestamp, duplicates).
    skipped_points: int = 0

    @property
    def start(self) -> datetime:
        return self.segments[0].start

    @property
    def end(self) -> datetime:
        return self.segments[-1].end

    @property
    def point_count(self) -> int:
        return sum(len(s.points) for s in self.segments)
