"""Write GPS coordinates into the EXIF metadata of media files. Independent of Qt.

This is the one operation that modifies media files. ExifTool keeps the
unmodified file as ``<name>_original`` next to it, so the original stays preserved.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

from naturelog.gps import TrackPosition
from naturelog.media import exiftool
from naturelog.media.formats import has_exif
from naturelog.media.models import MediaFile

_WORKERS = 4


def can_geotag(media: MediaFile) -> bool:
    """Whether GPS tags can be written to this file (EXIF-capable photo format)."""
    return has_exif(media.path)


def gps_tags(position: TrackPosition) -> dict[str, str]:
    """EXIF GPS tag values for ``position`` (WGS84, GPS time in UTC)."""
    when = position.time.astimezone(UTC)
    tags = {
        "GPSLatitude": f"{abs(position.latitude):.8f}",
        "GPSLatitudeRef": "N" if position.latitude >= 0 else "S",
        "GPSLongitude": f"{abs(position.longitude):.8f}",
        "GPSLongitudeRef": "E" if position.longitude >= 0 else "W",
        "GPSDateStamp": when.strftime("%Y:%m:%d"),
        "GPSTimeStamp": when.strftime("%H:%M:%S"),
        "GPSMapDatum": "WGS-84",
    }
    if position.elevation is not None:
        tags["GPSAltitude"] = f"{abs(position.elevation):.1f}"
        tags["GPSAltitudeRef"] = "0" if position.elevation >= 0 else "1"
    return tags


@dataclass(frozen=True, slots=True)
class GeotagResult:
    path: Path
    #: ``None`` on success, otherwise why the file was not updated.
    error: str | None = None


def write_coordinates(
    items: Sequence[tuple[MediaFile, TrackPosition]],
    progress: Callable[[int], None] | None = None,
    cancelled: threading.Event | None = None,
) -> list[GeotagResult]:
    """Write each position into its file's EXIF GPS tags.

    Files are written by parallel ExifTool processes. ``progress`` gets the
    number of files processed so far. Files not yet started when ``cancelled``
    is set are reported as cancelled.
    """
    if exiftool.exiftool_path() is None:
        return [GeotagResult(m.path, "ExifTool is not installed") for m, _ in items]

    def write(media: MediaFile, position: TrackPosition) -> GeotagResult:
        if cancelled is not None and cancelled.is_set():
            return GeotagResult(media.path, "cancelled")
        try:
            exiftool.write_tags(media.path, gps_tags(position))
        except exiftool.ExifToolWriteError as exc:
            return GeotagResult(media.path, str(exc))
        except Exception as exc:  # timeout, OS error: report per file
            return GeotagResult(media.path, f"unexpected error: {exc}")
        return GeotagResult(media.path)

    results: list[GeotagResult] = []
    if not items:
        return results
    with ThreadPoolExecutor(max_workers=min(_WORKERS, len(items))) as pool:
        futures = [pool.submit(write, media, position) for media, position in items]
        for done, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            if progress is not None:
                progress(done)
    return results
