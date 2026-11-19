"""iNaturalist upload rules. Independent of Qt.

NatureLog does not talk to the iNaturalist API: files are handed to the
iNaturalist web uploader, where the user reviews and submits them.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from naturelog.gps import Track, position_on_tracks
from naturelog.media.models import MediaFile, MediaKind
from naturelog.species import SpeciesPrediction, top_detection

SITE_URL = "https://www.inaturalist.org/"
UPLOAD_URL = "https://www.inaturalist.org/observations/upload"
UPLOAD_PATH = "/observations/upload"

#: Hosts the upload window may navigate to (iNaturalist and its sign-in providers).
ALLOWED_HOSTS = (
    "inaturalist.org",
    "accounts.google.com",
    "appleid.apple.com",
    "facebook.com",
    "orcid.org",
)

#: iNaturalist rejects larger photos and sounds.
MAX_FILE_SIZE = 20 * 1024 * 1024

_PHOTO_EXTENSIONS = frozenset({".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".heic", ".heif", ".webp"})
_SOUND_EXTENSIONS = frozenset({".wav", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".flac", ".amr"})


@dataclass(frozen=True, slots=True)
class UploadFile:
    path: Path
    mime_type: str


@dataclass(frozen=True, slots=True)
class UploadSelection:
    files: list[UploadFile]
    #: Files that cannot be uploaded, with the reason.
    skipped: list[tuple[Path, str]]


def is_allowed_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_HOSTS)


def upload_selection(media: Iterable[MediaFile]) -> UploadSelection:
    """Split ``media`` into files the iNaturalist uploader accepts and skipped ones."""
    files: list[UploadFile] = []
    skipped: list[tuple[Path, str]] = []
    for item in media:
        path = item.path
        ext = path.suffix.lower()
        if ext not in _PHOTO_EXTENSIONS and ext not in _SOUND_EXTENSIONS:
            skipped.append((path, "unsupported format"))
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            skipped.append((path, f"not readable ({exc.strerror or exc})"))
            continue
        if size > MAX_FILE_SIZE:
            skipped.append((path, "larger than 20 MB"))
            continue
        mime = mimetypes.guess_type(path.name)[0]
        if mime is None:
            mime = "image/heic" if ext in {".heic", ".heif"} else "application/octet-stream"
        files.append(UploadFile(path, mime))
    return UploadSelection(files, skipped)


@dataclass(frozen=True, slots=True)
class ObservationHint:
    """Values NatureLog suggests for the uploader card of a recording.

    iNaturalist reads date and location from photo EXIF itself, but not from
    sounds, so these are filled in for recordings.
    """

    #: Wall-clock capture time in the session timezone (iNaturalist takes the
    #: date without offset, like the EXIF time of photos).
    observed_at: datetime
    latitude: float | None = None
    longitude: float | None = None
    #: Most probable detected species, looked up by name on iNaturalist.
    scientific_name: str | None = None

    def to_json(self) -> dict[str, Any]:
        when = self.observed_at
        return {
            "date": f"{when.year:04d}/{when.month:02d}/{when.day:02d}",
            "hour": when.hour,
            "minute": when.minute,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "scientificName": self.scientific_name,
        }


def observation_hint(
    media: MediaFile,
    tz: tzinfo,
    tracks: Sequence[Track],
    predictions: Sequence[SpeciesPrediction] | None,
    moved_to: tuple[float, float] | None = None,
) -> ObservationHint | None:
    """Suggested card values for a recording; ``None`` for other files.

    The location is ``moved_to`` (set by the user on the map) if given, else
    the GPS tracks at the capture time (no position outside the track, never
    extrapolated).
    """
    if media.kind is not MediaKind.AUDIO:
        return None
    when = media.capture_time(tz)
    if moved_to is None:
        position = position_on_tracks(tracks, when)
        moved_to = (position.latitude, position.longitude) if position else None
    species = top_detection(predictions)
    return ObservationHint(
        observed_at=when.astimezone(tz).replace(tzinfo=None),
        latitude=moved_to[0] if moved_to else None,
        longitude=moved_to[1] if moved_to else None,
        scientific_name=species.scientific_name if species else None,
    )
