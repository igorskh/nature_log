"""Working session: settings, loaded GPS log and media with their coordinates.

The session is saved as a small JSON file so the application can restore the
last state when it starts again. Independent of Qt.

Original media and GPS files are only referenced by path, never copied or
modified. Coordinates stored with each media file are a snapshot of the
position interpolated from the GPS log when the session was saved; on restore
they are recomputed from the reloaded log.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from naturelog.gps import TrackPosition
from naturelog.media import MediaFile, MediaKind, TimeSource
from naturelog.species import SpeciesPrediction
from naturelog.timezones import UTC_NAME, get_timezone

SESSION_VERSION = 1


class SessionError(Exception):
    """A session file could not be read. The message is suitable for the user."""


@dataclass(slots=True)
class SessionMedia:
    """A media file in the session and the coordinates assigned to it, if any."""

    media: MediaFile
    position: TrackPosition | None = None
    #: Species candidates from the last detection run; ``None`` if never run.
    detections: list[SpeciesPrediction] | None = None


@dataclass(slots=True)
class Session:
    #: IANA name of the timezone camera clocks and displayed times are read in.
    timezone: str = UTC_NAME
    #: GPS log the media are matched against.
    track_path: Path | None = None
    media: list[SessionMedia] = field(default_factory=list)


# --- serialisation ----------------------------------------------------------


def _position_to_json(position: TrackPosition | None) -> dict[str, Any] | None:
    if position is None:
        return None
    return {
        "time": position.time.isoformat(),
        "latitude": position.latitude,
        "longitude": position.longitude,
        "elevation": position.elevation,
        "interpolated": position.interpolated,
    }


def _position_from_json(data: dict[str, Any] | None) -> TrackPosition | None:
    if not data:
        return None
    return TrackPosition(
        time=datetime.fromisoformat(data["time"]),
        latitude=float(data["latitude"]),
        longitude=float(data["longitude"]),
        elevation=None if data.get("elevation") is None else float(data["elevation"]),
        interpolated=bool(data.get("interpolated", True)),
    )


def _media_to_json(entry: SessionMedia) -> dict[str, Any]:
    media = entry.media
    return {
        "path": str(media.path),
        "kind": media.kind.value,
        # Naive EXIF camera-clock times stay naive (no offset in the string).
        "captured_at": media.captured_at.isoformat(),
        "time_source": media.time_source.value,
        "orientation": media.orientation,
        "position": _position_to_json(entry.position),
        "detections": None
        if entry.detections is None
        else [p.to_json() for p in entry.detections],
    }


def _media_from_json(data: dict[str, Any]) -> SessionMedia:
    media = MediaFile(
        path=Path(data["path"]),
        kind=MediaKind(data["kind"]),
        captured_at=datetime.fromisoformat(data["captured_at"]),
        time_source=TimeSource(data["time_source"]),
        orientation=data.get("orientation"),
    )
    detections = data.get("detections")
    return SessionMedia(
        media,
        _position_from_json(data.get("position")),
        None if detections is None else [SpeciesPrediction.from_json(d) for d in detections],
    )


def session_to_json(session: Session) -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "settings": {"timezone": session.timezone},
        "track": None if session.track_path is None else {"path": str(session.track_path)},
        "media": [_media_to_json(entry) for entry in session.media],
    }


def session_from_json(data: dict[str, Any]) -> Session:
    if data.get("version") != SESSION_VERSION:
        raise SessionError(f"Unsupported session version: {data.get('version')!r}")
    timezone = data.get("settings", {}).get("timezone", UTC_NAME)
    if get_timezone(timezone) is None:
        timezone = UTC_NAME
    track = data.get("track")
    return Session(
        timezone=timezone,
        track_path=Path(track["path"]) if track else None,
        media=[_media_from_json(item) for item in data.get("media", [])],
    )


# --- files ------------------------------------------------------------------


def save_session(session: Session, path: Path) -> None:
    """Write the session atomically, so a crash never leaves a truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(session_to_json(session), indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_session(path: Path) -> Session | None:
    """The saved session, or ``None`` if there is none."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SessionError(f"Cannot read session: {exc.strerror or exc}") from exc
    try:
        return session_from_json(json.loads(text))
    except (ValueError, KeyError, TypeError) as exc:
        raise SessionError(f"Session file is damaged: {exc}") from exc
