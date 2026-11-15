"""Capture timestamps of media files.

EXIF-capable formats use ``DateTimeOriginal`` as recorded by the camera, without
any offset: it is kept naive and interpreted in the user-selected timezone.
Everything else, and EXIF files without a capture time, fall back to the
file-system creation time, likewise kept as a naive wall-clock time (as the OS
shows it) and interpreted in the user-selected timezone without shifting.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from naturelog.media import exiftool
from naturelog.media.formats import has_exif, media_kind
from naturelog.media.models import MediaFile, TimeSource

Progress = Callable[[int], None]

_EXIF_DATE = re.compile(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
# Composite GPS tags are signed (Ref already applied); the raw EXIF ones are not.
_TAGS = ("DateTimeOriginal", "Orientation", "Composite:GPSLatitude", "Composite:GPSLongitude")

# EXIF tag ids for the Pillow fallback.
_EXIF_IFD = 0x8769
_DATETIME_ORIGINAL = 36867
_ORIENTATION = 0x0112
_GPS_IFD = 0x8825
_GPS_LAT_REF, _GPS_LAT, _GPS_LON_REF, _GPS_LON = 1, 2, 3, 4


@dataclass(frozen=True, slots=True)
class ExifInfo:
    captured_at: datetime | None  # naive camera-clock time
    orientation: int | None
    #: Signed WGS84 ``(latitude, longitude)`` stored in the file, if any.
    coordinates: tuple[float, float] | None = None


def _coordinates(lat: object, lon: object) -> tuple[float, float] | None:
    """Valid signed coordinates, or ``None`` (missing, malformed or out of range)."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _dms_to_degrees(value: object, ref: object, negative: str) -> float | None:
    """EXIF degrees/minutes/seconds rationals to signed decimal degrees."""
    try:
        d, m, s = (float(v) for v in value)  # type: ignore[union-attr]
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    degrees = d + m / 60 + s / 3600
    if isinstance(ref, bytes):
        ref = ref.decode("ascii", errors="ignore")
    return -degrees if isinstance(ref, str) and ref.strip().upper() == negative else degrees


def parse_exif_datetime(value: object) -> datetime | None:
    """Parse an EXIF date such as ``2024:05:01 10:20:30`` into a naive datetime.

    Anything after the seconds (sub-seconds, offsets) is ignored. Zeroed or
    malformed dates give ``None``.
    """
    if not isinstance(value, str):
        return None
    match = _EXIF_DATE.match(value.strip())
    if not match:
        return None
    try:
        return datetime(*map(int, match.groups()))
    except ValueError:
        return None


def _orientation(value: object) -> int | None:
    return value if isinstance(value, int) and 1 <= value <= 8 else None


def _read_exif_exiftool(paths: Sequence[Path], progress: Progress | None) -> dict[Path, ExifInfo]:
    records = exiftool.read_tags(paths, _TAGS, progress)
    result: dict[Path, ExifInfo] = {}
    for path, tags in records.items():
        captured = parse_exif_datetime(tags.get("DateTimeOriginal"))
        coordinates = _coordinates(tags.get("GPSLatitude"), tags.get("GPSLongitude"))
        result[path] = ExifInfo(captured, _orientation(tags.get("Orientation")), coordinates)
    return result


def _read_exif_pillow(path: Path) -> ExifInfo:
    from PIL import Image  # imported lazily: only needed without ExifTool

    try:
        with Image.open(path) as image:
            exif = image.getexif()
    except Exception:
        return ExifInfo(None, None)
    captured = parse_exif_datetime(exif.get_ifd(_EXIF_IFD).get(_DATETIME_ORIGINAL))
    gps = exif.get_ifd(_GPS_IFD)
    coordinates = None
    if _GPS_LAT in gps and _GPS_LON in gps:
        coordinates = _coordinates(
            _dms_to_degrees(gps[_GPS_LAT], gps.get(_GPS_LAT_REF), "S"),
            _dms_to_degrees(gps[_GPS_LON], gps.get(_GPS_LON_REF), "W"),
        )
    return ExifInfo(captured, _orientation(exif.get(_ORIENTATION)), coordinates)


def read_exif(paths: Sequence[Path], progress: Progress | None = None) -> dict[Path, ExifInfo]:
    """EXIF capture time, orientation and GPS coordinates, via ExifTool or (if missing) Pillow.

    ``progress`` receives the number of files processed so far.
    """
    if not paths:
        return {}
    if exiftool.exiftool_path() is not None:
        try:
            return _read_exif_exiftool(paths, progress)
        except Exception:
            pass  # fall through to Pillow, which covers JPEG and TIFF
    result: dict[Path, ExifInfo] = {}
    for done, path in enumerate(paths, 1):
        result[path] = _read_exif_pillow(path)
        if progress is not None:
            progress(done)
    return result


def file_created_at(path: Path) -> datetime:
    """File-system creation time as a naive wall-clock time.

    Uses the birth time where the OS records it (macOS, Windows); otherwise
    the modification time, which usually survives copying from a card.

    The time is the wall clock the OS shows for the file (system local time)
    and, like EXIF times, is taken to be in the user-selected timezone.
    """
    stat = path.stat()
    timestamp = getattr(stat, "st_birthtime", None) or stat.st_mtime
    return datetime.fromtimestamp(timestamp)


def read_media_files(
    paths: Iterable[Path], progress: Callable[[int, int], None] | None = None
) -> list[MediaFile]:
    """Build :class:`MediaFile` records with capture times for ``paths``.

    ``progress(done, total)`` is called as metadata is read.
    """
    paths = list(paths)
    total = len(paths)
    exif_paths = [p for p in paths if has_exif(p)]
    others = total - len(exif_paths)  # need only a stat: counted as done up front
    if progress is not None:
        progress(others, total)
    exif = read_exif(
        exif_paths, (lambda done: progress(others + done, total)) if progress else None
    )
    files: list[MediaFile] = []
    for path in paths:
        info = exif.get(path)
        try:
            if info is not None and info.captured_at is not None:
                captured, source = info.captured_at, TimeSource.EXIF
            else:
                captured, source = file_created_at(path), TimeSource.FILE
        except OSError:
            continue  # vanished or unreadable
        files.append(
            MediaFile(
                path=path,
                kind=media_kind(path),
                captured_at=captured,
                time_source=source,
                orientation=info.orientation if info else None,
                exif_coordinates=info.coordinates if info else None,
            )
        )
    return files
