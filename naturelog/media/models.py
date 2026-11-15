"""Domain models for imported media files."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo
from enum import StrEnum
from pathlib import Path


class MediaKind(StrEnum):
    """Broad media category, derived from the file extension."""

    JPEG = "jpeg"
    RAW = "raw"
    IMAGE = "image"  # other still-image formats (TIFF, HEIC, PNG, …)
    AUDIO = "audio"
    VIDEO = "video"
    OTHER = "other"

    @property
    def is_image(self) -> bool:
        return self in (MediaKind.JPEG, MediaKind.RAW, MediaKind.IMAGE)


class TimeSource(StrEnum):
    """Where :attr:`MediaFile.captured_at` came from."""

    EXIF = "exif"  # capture time recorded by the camera (DateTimeOriginal)
    FILE = "file"  # file-system creation time (wall clock)


@dataclass(frozen=True, slots=True)
class MediaFile:
    """An original media file. The file is only modified on explicit user
    request (writing GPS tags, see :mod:`naturelog.media.geotag`), which keeps
    a backup of the original.

    ``captured_at`` is kept *naive*: EXIF ``DateTimeOriginal`` is the camera's
    clock reading (any recorded offset ignored) and file-system times are the
    wall clock the OS shows. Both are interpreted in the user-selected timezone
    via :meth:`capture_time`. (Sessions saved earlier may hold aware times.)
    """

    path: Path
    kind: MediaKind
    captured_at: datetime
    time_source: TimeSource
    #: EXIF orientation (1–8) of the original, if known; used for previews.
    orientation: int | None = None
    #: GPS ``(latitude, longitude)`` stored in the file's EXIF, if any (WGS84, signed).
    exif_coordinates: tuple[float, float] | None = None

    @property
    def name(self) -> str:
        return self.path.name

    def capture_time(self, tz: tzinfo) -> datetime:
        """Timezone-aware capture time; a naive time is taken to be in ``tz``."""
        if self.captured_at.tzinfo is None:
            return self.captured_at.replace(tzinfo=tz)
        return self.captured_at
