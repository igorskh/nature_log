"""Working session: settings, loaded GPS logs and media with their coordinates.

The session is saved as a small JSON file so the application can restore the
last state when it starts again. Independent of Qt.

Original media and GPS files are only referenced by path, never copied or
modified. Coordinates stored with each media file are a snapshot of the
position interpolated from the GPS logs when the session was saved; on restore
they are recomputed from the reloaded logs. A position the user moved a file
to on the map is kept separately and replaces the track position.

Paths are stored absolute, or relative to the session file's folder so a
session saved next to its data keeps working when the folder is moved or
copied to another computer.
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
from naturelog.species import DetectionConfig, SpeciesPrediction
from naturelog.timezones import UTC_NAME, get_timezone

SESSION_VERSION = 3
#: Older versions that can still be read (v1 had a single ``track``, v2 only
#: absolute paths).
_READABLE_VERSIONS = (1, 2, 3)


class SessionError(Exception):
    """A session file could not be read. The message is suitable for the user."""


@dataclass(slots=True)
class SessionMedia:
    """A media file in the session and the coordinates assigned to it, if any."""

    media: MediaFile
    position: TrackPosition | None = None
    #: Species candidates from the last detection run; ``None`` if never run.
    detections: list[SpeciesPrediction] | None = None
    #: ``(latitude, longitude)`` the user moved the file to; replaces the track position.
    moved_to: tuple[float, float] | None = None


@dataclass(slots=True)
class Session:
    #: IANA name of the timezone camera clocks and displayed times are read in.
    timezone: str = UTC_NAME
    #: GPS logs the media are matched against, in lookup priority order.
    track_paths: list[Path] = field(default_factory=list)
    media: list[SessionMedia] = field(default_factory=list)
    #: Models species detection runs with.
    detection: DetectionConfig = field(default_factory=DetectionConfig)


# --- paths ------------------------------------------------------------------


def _relative(path: Path, base: Path) -> str | None:
    """``path`` relative to the folder ``base`` (POSIX separators), or ``None``
    if it has none (another drive on Windows)."""
    try:
        return Path(os.path.relpath(path.absolute(), base.absolute())).as_posix()
    except ValueError:
        return None


def can_use_relative_paths(session: Session, folder: Path) -> bool:
    """Whether every file of ``session`` can be stored relative to ``folder``."""
    paths = [*session.track_paths, *(entry.media.path for entry in session.media)]
    return all(_relative(path, folder) is not None for path in paths)


def _path_to_json(path: Path, base: Path | None) -> str:
    if base is not None:
        relative = _relative(path, base)
        if relative is not None:
            return relative
    return str(path.absolute())


def _path_from_json(text: str, base: Path | None) -> Path:
    path = Path(text)
    if path.is_absolute() or base is None:
        return path
    return Path(os.path.normpath(base / path))


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


def _media_to_json(entry: SessionMedia, base: Path | None) -> dict[str, Any]:
    media = entry.media
    return {
        "path": _path_to_json(media.path, base),
        "kind": media.kind.value,
        # Naive EXIF camera-clock times stay naive (no offset in the string).
        "captured_at": media.captured_at.isoformat(),
        "time_source": media.time_source.value,
        "orientation": media.orientation,
        "exif_coordinates": None
        if media.exif_coordinates is None
        else list(media.exif_coordinates),
        "position": _position_to_json(entry.position),
        "detections": None
        if entry.detections is None
        else [p.to_json() for p in entry.detections],
        "moved_to": None if entry.moved_to is None else list(entry.moved_to),
    }


def _media_from_json(data: dict[str, Any], base: Path | None) -> SessionMedia:
    coords = data.get("exif_coordinates")
    media = MediaFile(
        path=_path_from_json(data["path"], base),
        kind=MediaKind(data["kind"]),
        captured_at=datetime.fromisoformat(data["captured_at"]),
        time_source=TimeSource(data["time_source"]),
        orientation=data.get("orientation"),
        exif_coordinates=None if coords is None else (float(coords[0]), float(coords[1])),
    )
    detections = data.get("detections")
    moved = data.get("moved_to")
    return SessionMedia(
        media,
        _position_from_json(data.get("position")),
        None if detections is None else [SpeciesPrediction.from_json(d) for d in detections],
        None if moved is None else (float(moved[0]), float(moved[1])),
    )


def session_to_json(session: Session, base: Path | None = None) -> dict[str, Any]:
    """JSON form of ``session``; with ``base``, paths are relative to that folder
    (where possible)."""
    return {
        "version": SESSION_VERSION,
        "settings": {"timezone": session.timezone, "detection": session.detection.to_json()},
        "relative_paths": base is not None,
        "tracks": [{"path": _path_to_json(path, base)} for path in session.track_paths],
        "media": [_media_to_json(entry, base) for entry in session.media],
    }


def session_from_json(data: dict[str, Any], base: Path | None = None) -> Session:
    """Session from its JSON form; relative paths are resolved against ``base``."""

    if data.get("version") not in _READABLE_VERSIONS:
        raise SessionError(f"Unsupported session version: {data.get('version')!r}")
    settings = data.get("settings", {})
    timezone = settings.get("timezone", UTC_NAME)
    if get_timezone(timezone) is None:
        timezone = UTC_NAME
    tracks = data.get("tracks")
    if tracks is None:  # version 1
        tracks = [data["track"]] if data.get("track") else []
    return Session(
        timezone=timezone,
        track_paths=[_path_from_json(track["path"], base) for track in tracks],
        media=[_media_from_json(item, base) for item in data.get("media", [])],
        detection=DetectionConfig.from_json(settings.get("detection")),
    )


# --- files ------------------------------------------------------------------


def save_session(session: Session, path: Path, relative_paths: bool = False) -> None:
    """Write the session atomically, so a crash never leaves a truncated file.

    With ``relative_paths`` file paths are stored relative to the folder of
    ``path``; a file that has no relative path (another drive) stays absolute.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    base = path.parent if relative_paths else None
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(session_to_json(session, base), indent=1), encoding="utf-8")
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
        return session_from_json(json.loads(text), path.absolute().parent)
    except (ValueError, KeyError, TypeError) as exc:
        raise SessionError(f"Session file is damaged: {exc}") from exc
