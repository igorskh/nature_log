"""Render crops of media files to new image or sound data, in memory. Independent of Qt.

Originals are only read. A photo crop is cut from the full-resolution, upright
image and keeps the original's EXIF (capture date, GPS) with the orientation
reset; a recording crop is cut to 16-bit WAV with FFmpeg.
"""

from __future__ import annotations

import io
import struct
import subprocess
import sys

from PIL import Image, ImageOps

from naturelog.media.crops import AudioCrop, Crop, ImageCrop
from naturelog.media.models import MediaFile, MediaKind
from naturelog.media.previews import make_thumbnail
from naturelog.media.spectrogram import ffmpeg_path

#: Longest side of the preview RAW crops are cut from (RAW files are not decoded).
_RAW_SOURCE_SIZE = 8192
_ORIENTATION_TAG = 0x0112
_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class CropExportError(Exception):
    """A crop could not be rendered. The message is suitable for the user."""


def crop_file_name(media: MediaFile, crop: Crop, number: int) -> str:
    """File name of the ``number``-th (1-based) crop of ``media``."""
    ext = ".wav" if isinstance(crop, AudioCrop) else ".jpg"
    return f"{media.path.stem}_crop{number}{ext}"


def crop_mime_type(crop: Crop) -> str:
    return "audio/x-wav" if isinstance(crop, AudioCrop) else "image/jpeg"


def render_crop(media: MediaFile, crop: Crop) -> bytes:
    """JPEG (photo crop) or WAV (recording crop) data of ``crop`` of ``media``."""
    if isinstance(crop, AudioCrop):
        return _render_audio(media, crop)
    return _render_image(media, crop)


def _render_image(media: MediaFile, crop: ImageCrop) -> bytes:
    exif: Image.Exif | None = None
    try:
        if media.kind is MediaKind.RAW:
            data = make_thumbnail(media, _RAW_SOURCE_SIZE)  # embedded preview, upright
            if data is None:
                raise CropExportError("no preview in this RAW file")
            image = Image.open(io.BytesIO(data))
        else:
            image = Image.open(media.path)
            exif = image.getexif()
            image = ImageOps.exif_transpose(image)
        w, h = image.size
        left, top = round(crop.left * w), round(crop.top * h)
        box = (
            left,
            top,
            max(left + 1, round((crop.left + crop.width) * w)),
            max(top + 1, round((crop.top + crop.height) * h)),
        )
        image = image.crop(box)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        buffer = io.BytesIO()
        if exif is not None and len(exif):
            exif[_ORIENTATION_TAG] = 1  # pixels are upright now
            image.save(buffer, "JPEG", quality=92, exif=exif.tobytes())
        else:
            image.save(buffer, "JPEG", quality=92)
        return buffer.getvalue()
    except CropExportError:
        raise
    except Exception as exc:  # Pillow raises many kinds of errors on odd files
        raise CropExportError(f"cannot crop the photo ({exc})") from exc


def _render_audio(media: MediaFile, crop: AudioCrop) -> bytes:
    exe = ffmpeg_path()
    if exe is None:
        raise CropExportError("FFmpeg is not installed")
    args = [
        exe, "-v", "error", "-nostdin",
        "-ss", f"{crop.start:.3f}", "-to", f"{crop.end:.3f}", "-i", str(media.path),
        "-map", "0:a:0", "-c:a", "pcm_s16le", "-f", "wav", "-",
    ]  # fmt: skip
    try:
        result = subprocess.run(
            args, capture_output=True, check=False, creationflags=_CREATIONFLAGS
        )
    except OSError as exc:
        raise CropExportError(f"cannot run FFmpeg ({exc.strerror or exc})") from exc
    if result.returncode != 0 or not result.stdout:
        error = result.stderr.decode(errors="replace").strip().splitlines()
        reason = error[-1] if error else "FFmpeg failed"
        raise CropExportError(f"cannot cut the recording ({reason})")
    return _fix_wav_sizes(result.stdout)


def _fix_wav_sizes(data: bytes) -> bytes:
    """Fill in the RIFF and data chunk sizes FFmpeg cannot write to a pipe."""
    out = bytearray(data)
    if out[:4] != b"RIFF" or out[8:12] != b"WAVE":
        return data
    struct.pack_into("<I", out, 4, len(out) - 8)
    pos = 12
    while pos + 8 <= len(out):
        chunk, size = out[pos : pos + 4], struct.unpack_from("<I", out, pos + 4)[0]
        if chunk == b"data":
            struct.pack_into("<I", out, pos + 4, len(out) - pos - 8)
            break
        pos += 8 + size + (size & 1)
    return bytes(out)
