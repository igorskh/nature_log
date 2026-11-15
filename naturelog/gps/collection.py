"""Position lookup across several GPS tracks (e.g. one per recording device)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from naturelog.gps.interpolation import TrackPosition, position_at
from naturelog.gps.models import Track


def position_on_tracks(tracks: Sequence[Track], when: datetime) -> TrackPosition | None:
    """Position at ``when`` from the first track in ``tracks`` that covers it.

    Tracks are tried in the given order, so where tracks overlap in time the
    earlier one wins. A recorded fix on any track is preferred over an
    interpolated position. Times outside every track, or in gaps, give
    ``None`` – coordinates are never extrapolated.
    """
    fallback: TrackPosition | None = None
    for track in tracks:
        position = position_at(track, when)
        if position is None:
            continue
        if not position.interpolated:
            return position
        if fallback is None:
            fallback = position
    return fallback


def tracks_span(tracks: Sequence[Track]) -> tuple[datetime, datetime] | None:
    """Earliest start and latest end over all tracks, or ``None`` if there are none."""
    if not tracks:
        return None
    return min(t.start for t in tracks), max(t.end for t in tracks)
