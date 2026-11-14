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
    FILE = "file"  # file-system creation time


@dataclass(frozen=True, slots=True)
class MediaFile:
    """An original media file. The file itself is never modified.

    ``captured_at`` is timezone-aware for file-system times. EXIF
    ``DateTimeOriginal`` is kept *naive* (the camera's clock reading, any
    recorded offset ignored) and is interpreted in the user-selected timezone
    via :meth:`capture_time`.
    """

    path: Path
    kind: MediaKind
    captured_at: datetime
    time_source: TimeSource
    #: EXIF orientation (1–8) of the original, if known; used for previews.
    orientation: int | None = None

    @property
    def name(self) -> str:
        return self.path.name

    def capture_time(self, tz: tzinfo) -> datetime:
        """Timezone-aware capture time; a naive EXIF time is taken to be in ``tz``."""
        if self.captured_at.tzinfo is None:
            return self.captured_at.replace(tzinfo=tz)
        return self.captured_at
