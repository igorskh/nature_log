"""Crops of media files: regions of a photo or time ranges of a recording.

Crops are only definitions stored in the session; the original file is never
changed. A file can have any number of crops. Independent of Qt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ImageCrop:
    """Rectangle of a photo as fractions (0–1) of the upright (EXIF-oriented) image."""

    left: float
    top: float
    width: float
    height: float

    def label(self) -> str:
        return f"{self.width:.0%} × {self.height:.0%}"

    def to_json(self) -> dict[str, Any]:
        return {"left": self.left, "top": self.top, "width": self.width, "height": self.height}


@dataclass(frozen=True, slots=True)
class AudioCrop:
    """Time range of a recording, seconds from its start."""

    start: float
    end: float

    def label(self) -> str:
        return f"{_seconds(self.start)} – {_seconds(self.end)}"

    def to_json(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end}


Crop = ImageCrop | AudioCrop


def _seconds(value: float) -> str:
    minutes, sec = divmod(value, 60)
    return f"{int(minutes)}:{sec:04.1f}"


def make_image_crop(x0: float, y0: float, x1: float, y1: float) -> ImageCrop | None:
    """Crop spanning two corner points (fractions, any order), clamped to the image;
    ``None`` if it is degenerate."""
    left, right = sorted((min(1.0, max(0.0, x0)), min(1.0, max(0.0, x1))))
    top, bottom = sorted((min(1.0, max(0.0, y0)), min(1.0, max(0.0, y1))))
    if right - left < 0.005 or bottom - top < 0.005:
        return None
    return ImageCrop(left, top, right - left, bottom - top)


def make_audio_crop(t0: float, t1: float, duration: float) -> AudioCrop | None:
    """Crop between two times (any order), clamped to ``duration``; ``None`` if empty."""
    start, end = sorted((min(duration, max(0.0, t0)), min(duration, max(0.0, t1))))
    if end - start < 0.05:
        return None
    return AudioCrop(start, end)


def crop_to_json(crop: Crop) -> dict[str, Any]:
    return crop.to_json()


def crop_from_json(data: dict[str, Any]) -> Crop:
    if "start" in data:
        return AudioCrop(float(data["start"]), float(data["end"]))
    return ImageCrop(
        float(data["left"]), float(data["top"]), float(data["width"]), float(data["height"])
    )
