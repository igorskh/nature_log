"""Thumbnail generation for image files. Originals are only read."""

from __future__ import annotations

import io
import os
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, ImageOps

from naturelog.media import exiftool
from naturelog.media.models import MediaFile, MediaKind

# Embedded JPEG previews in RAW files, best first (largest usable → smallest).
_RAW_PREVIEW_TAGS = ("PreviewImage", "JpgFromRaw", "ThumbnailImage")

_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def _raw_preview(path: Path, size: int) -> Image.Image | None:
    if exiftool.exiftool_path() is not None:
        for tag in _RAW_PREVIEW_TAGS:
            data = exiftool.read_binary_tag(path, tag)
            if len(data) > 1024:
                try:
                    image = Image.open(io.BytesIO(data))
                    # Decode the (often full-size) JPEG preview at reduced scale:
                    # much faster and far less memory with many files in parallel.
                    image.draft("RGB", (size, size))
                    image.load()
                    return image
                except Exception:
                    continue
    try:  # e.g. DNG/TIFF-based RAW that Pillow can decode itself
        image = Image.open(path)
        image.load()
        return image
    except Exception:
        return None


def make_thumbnail(media: MediaFile, size: int = 256) -> bytes | None:
    """A JPEG thumbnail no larger than ``size`` px, or ``None`` if not possible."""
    if not media.kind.is_image:
        return None
    try:
        if media.kind is MediaKind.RAW:
            image = _raw_preview(media.path, size)
            if image is None:
                return None
            embedded = image.getexif().get(0x0112)
            if embedded and embedded != 1:
                image = ImageOps.exif_transpose(image)
            elif media.orientation in _TRANSPOSE:
                image = image.transpose(_TRANSPOSE[media.orientation])
        else:
            image = Image.open(media.path)
            image.draft("RGB", (size, size))  # fast JPEG downscale on decode
            image = ImageOps.exif_transpose(image)
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=85)
        return buffer.getvalue()
    except Exception:
        return None


def default_workers() -> int:
    """Parallel thumbnail workers: one per CPU core, between 2 and 8."""
    return max(2, min(8, os.cpu_count() or 2))


def make_thumbnails(
    files: Sequence[MediaFile],
    size: int = 256,
    workers: int | None = None,
    cancelled: threading.Event | None = None,
) -> Iterator[tuple[MediaFile, bytes | None]]:
    """Generate thumbnails in parallel, yielding ``(media, jpeg_or_None)`` as each is ready.

    Results arrive in completion order, not input order. Setting ``cancelled``
    stops the iteration; queued files are dropped and only the few in progress
    are finished. Decoding (Pillow) and ExifTool subprocesses release the GIL,
    so threads scale across cores.
    """
    if not files:
        return
    pool = ThreadPoolExecutor(max_workers=workers or default_workers())
    try:
        futures = {pool.submit(make_thumbnail, media, size): media for media in files}
        for future in as_completed(futures):
            if cancelled is not None and cancelled.is_set():
                return
            yield futures[future], future.result()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
