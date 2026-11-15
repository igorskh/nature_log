"""Set the dates of a media file: file-system created/modified and EXIF capture time.

Independent of Qt. File-system dates change only metadata, never content.
Writing EXIF ``DateTimeOriginal`` rewrites the file through ExifTool, which keeps
the unmodified file as ``<name>_original`` next to it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from naturelog.media import exiftool
from naturelog.media.file_datetime import FileDatetimeError, creation_time, set_creation_time
from naturelog.media.formats import has_exif

_EXIF_FORMAT = "%Y:%m:%d %H:%M:%S"


def can_set_created() -> bool:
    """Whether the OS allows setting a file's creation date (not on Linux)."""
    return sys.platform in ("darwin", "win32")


@dataclass(frozen=True, slots=True)
class DatetimeEdit:
    """Which dates to set. ``when`` must be timezone-aware.

    EXIF ``DateTimeOriginal`` and the file-system dates are written as the
    wall-clock time of ``when`` in its own timezone (no shift), matching how
    NatureLog reads them: file dates then show that wall-clock time in the OS.
    """

    when: datetime
    created: bool = False
    modified: bool = False
    exif_original: bool = False

    def __post_init__(self) -> None:
        if self.when.tzinfo is None:
            raise ValueError("DatetimeEdit.when must be timezone-aware")

    @property
    def any(self) -> bool:
        return self.created or self.modified or self.exif_original


@dataclass(frozen=True, slots=True)
class EditResult:
    path: Path
    #: ``None`` on success, otherwise why (part of) the file was not updated.
    error: str | None = None


def edit_file_datetime(path: Path, edit: DatetimeEdit) -> None:
    """Apply ``edit`` to one file. EXIF is skipped for formats without EXIF."""
    # Naive -> interpreted as system local time, so the OS shows ``when``'s wall clock.
    timestamp = edit.when.replace(tzinfo=None).timestamp()
    # Remember the creation date: rewriting the file for EXIF may reset it.
    old_created = creation_time(path) if can_set_created() else None

    if edit.exif_original and has_exif(path):
        if exiftool.exiftool_path() is None:
            raise FileDatetimeError("ExifTool is not installed")
        exiftool.write_tags(path, {"DateTimeOriginal": edit.when.strftime(_EXIF_FORMAT)})

    if edit.modified:
        os.utime(path, (path.stat().st_atime, timestamp))
    # After the modification date: on macOS an earlier mtime pulls the creation date along.
    if edit.created and can_set_created():
        set_creation_time(path, timestamp)
    elif old_created is not None and creation_time(path) != old_created:
        set_creation_time(path, old_created)


def edit_datetimes(
    paths: Sequence[Path],
    edit: DatetimeEdit,
    progress: Callable[[int], None] | None = None,
) -> list[EditResult]:
    """Apply ``edit`` to every file, reporting errors per file."""
    results: list[EditResult] = []
    for done, path in enumerate(paths, 1):
        try:
            edit_file_datetime(path, edit)
        except (FileDatetimeError, exiftool.ExifToolWriteError, OSError) as exc:
            results.append(EditResult(path, str(exc)))
        except Exception as exc:  # timeout etc.: report per file
            results.append(EditResult(path, f"unexpected error: {exc}"))
        else:
            results.append(EditResult(path))
        if progress is not None:
            progress(done)
    return results
