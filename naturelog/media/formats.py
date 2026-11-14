"""Supported file formats, classified by extension."""

from __future__ import annotations

from pathlib import Path

from naturelog.media.models import MediaKind

JPEG_EXTENSIONS = frozenset({".jpg", ".jpeg", ".jpe", ".jfif"})

RAW_EXTENSIONS = frozenset({
    ".3fr", ".ari", ".arw", ".cr2", ".cr3", ".crw", ".dcr", ".dng", ".erf",
    ".fff", ".iiq", ".k25", ".kdc", ".mef", ".mos", ".mrw", ".nef", ".nrw",
    ".orf", ".pef", ".raf", ".raw", ".rw2", ".rwl", ".sr2", ".srf", ".srw",
    ".x3f",
})  # fmt: skip

OTHER_IMAGE_EXTENSIONS = frozenset({".tif", ".tiff", ".heic", ".heif", ".png", ".webp"})

AUDIO_EXTENSIONS = frozenset({
    ".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aif", ".aiff",
})  # fmt: skip

VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".m2ts"})

#: Formats whose capture time is read from EXIF ``DateTimeOriginal``.
EXIF_EXTENSIONS = JPEG_EXTENSIONS | RAW_EXTENSIONS | frozenset({".tif", ".tiff", ".heic", ".heif"})


def media_kind(path: Path) -> MediaKind:
    ext = path.suffix.lower()
    if ext in JPEG_EXTENSIONS:
        return MediaKind.JPEG
    if ext in RAW_EXTENSIONS:
        return MediaKind.RAW
    if ext in OTHER_IMAGE_EXTENSIONS:
        return MediaKind.IMAGE
    if ext in AUDIO_EXTENSIONS:
        return MediaKind.AUDIO
    if ext in VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    return MediaKind.OTHER


def has_exif(path: Path) -> bool:
    return path.suffix.lower() in EXIF_EXTENSIONS
