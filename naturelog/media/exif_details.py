"""Human-readable metadata of a single file for display. Independent of Qt.

Uses ExifTool when installed (all groups, readable values); otherwise falls
back to the EXIF tags Pillow can decode. Originals are only read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from PIL import ExifTags, Image

from naturelog.media import exiftool

#: ExifTool groups describing the tool or the file system, not the capture.
_SKIPPED_GROUPS = frozenset({"ExifTool", "System"})
_MAX_VALUE_LENGTH = 200


@dataclass(frozen=True, slots=True)
class MetadataEntry:
    group: str  # e.g. "IFD0", "ExifIFD", "GPS", "Composite"
    tag: str
    value: str


def _text(value: object) -> str:
    if isinstance(value, list):
        value = ", ".join(map(str, value))
    text = str(value)
    if len(text) > _MAX_VALUE_LENGTH:
        text = text[: _MAX_VALUE_LENGTH - 1] + "…"
    return text


def _from_exiftool(path: Path) -> list[MetadataEntry]:
    output = exiftool._run(["-json", "-G1", "-s", "-q", str(path)], timeout=30)
    if not output.strip():
        return []
    record = json.loads(output.decode("utf-8", errors="replace"))[0]
    entries = []
    for key, value in record.items():
        group, _, tag = key.rpartition(":")
        if not group or group in _SKIPPED_GROUPS:
            continue
        if isinstance(value, str) and value.startswith("(Binary data"):
            continue
        entries.append(MetadataEntry(group, tag, _text(value)))
    return entries


def _from_pillow(path: Path) -> list[MetadataEntry]:
    with Image.open(path) as image:
        exif = image.getexif()
    entries = []
    groups = (
        ("IFD0", dict(exif)),
        ("ExifIFD", dict(exif.get_ifd(ExifTags.IFD.Exif))),
        ("GPS", dict(exif.get_ifd(ExifTags.IFD.GPSInfo))),
    )
    for group, tags in groups:
        names = ExifTags.GPSTAGS if group == "GPS" else ExifTags.TAGS
        for tag_id, value in tags.items():
            if isinstance(value, (bytes, dict)):
                continue
            entries.append(MetadataEntry(group, names.get(tag_id, f"0x{tag_id:04X}"), _text(value)))
    return entries


def read_metadata(path: Path) -> list[MetadataEntry]:
    """Readable metadata of ``path`` grouped by source; empty if none can be read."""
    try:
        if exiftool.exiftool_path() is not None:
            return _from_exiftool(path)
        return _from_pillow(path)
    except Exception:
        return []
