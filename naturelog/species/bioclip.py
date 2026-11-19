"""Species detection in photos with BioCLIP 2 (via the optional ``chirppipe`` package).

Install with ``uv sync --extra species``. Photos are decoded to an oriented
preview (embedded preview for RAW files) and classified in memory; originals
are only read and nothing is written next to them. The model is downloaded on
first use and kept loaded for later runs.
"""

from __future__ import annotations

import importlib.util
import io
import math
import threading
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from PIL import Image

from naturelog.media import MediaFile, make_thumbnail
from naturelog.media.crops import ImageCrop
from naturelog.species.models import LINEAGE_RANKS, SpeciesPrediction

#: Preview size fed to the model (BioCLIP itself works at 224 px).
_INPUT_SIZE = 512
_BATCH_SIZE = 8
#: Largest preview decoded to cut small crops from, px.
_MAX_CROP_SOURCE = 4096


class DetectorUnavailable(Exception):
    """The species detector cannot be used. The message is suitable for the user."""


def detector_available() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in ("chirps", "bioclip"))


def _prediction(row: dict[str, Any]) -> SpeciesPrediction:
    return SpeciesPrediction(
        scientific_name=str(row.get("species") or ""),
        common_name=str(row.get("common_name") or ""),
        score=float(row["score"]),
        lineage=tuple(str(row.get(rank) or "") for rank in LINEAGE_RANKS),
    )


def _load_image(media: MediaFile, crop: ImageCrop | None = None) -> Image.Image | None:
    """Oriented RGB preview of the photo, or of ``crop`` of it (decoded large enough
    that the crop still has about ``_INPUT_SIZE`` px on its longer side)."""
    size = _INPUT_SIZE
    if crop is not None:
        size = min(_MAX_CROP_SOURCE, math.ceil(_INPUT_SIZE / max(crop.width, crop.height, 1e-3)))
    data = make_thumbnail(media, size)
    if data is None:
        return None
    image = Image.open(io.BytesIO(data)).convert("RGB")
    if crop is not None:
        w, h = image.size
        box = (
            round(crop.left * w),
            round(crop.top * h),
            max(round(crop.left * w) + 1, round((crop.left + crop.width) * w)),
            max(round(crop.top * h) + 1, round((crop.top + crop.height) * h)),
        )
        image = image.crop(box)
    return image


class BioClipDetector:
    """Wraps chirppipe's ``BioClipInference``; safe to share, runs one batch at a time."""

    def __init__(self) -> None:
        self._inference: Any = None
        self._lock = threading.Lock()

    def _model(self) -> Any:
        if self._inference is None:
            if not detector_available():
                raise DetectorUnavailable(
                    "Species detection is not installed – run: uv sync --extra species"
                )
            try:
                from chirps.ml.bioclip_inference import BioClipInference

                inference = BioClipInference()
                inference.load_model()
            except Exception as exc:
                raise DetectorUnavailable(f"Cannot load the BioCLIP model: {exc}") from exc
            self._inference = inference
        return self._inference

    def detect(
        self,
        targets: Sequence[tuple[MediaFile, ImageCrop | None]],
        cancelled: threading.Event | None = None,
        on_loaded: Callable[[], None] | None = None,
    ) -> Iterator[tuple[MediaFile, ImageCrop | None, list[SpeciesPrediction] | None]]:
        """Yield ``(media, crop, top-k predictions)`` per whole photo (crop ``None``)
        or crop of a photo; predictions are ``None`` if it cannot be read.

        Raises :class:`DetectorUnavailable` before the first result if the
        model cannot be loaded.
        """
        with self._lock:
            inference = self._model()
            if on_loaded is not None:
                on_loaded()
            for start in range(0, len(targets), _BATCH_SIZE):
                if cancelled is not None and cancelled.is_set():
                    return
                batch = targets[start : start + _BATCH_SIZE]
                images = [_load_image(m, c) for m, c in batch]
                readable = [(t, img) for t, img in zip(batch, images) if img is not None]
                for (media, crop), image in zip(batch, images):
                    if image is None:
                        yield media, crop, None
                if not readable:
                    continue
                # Top-5 species per image; in-memory images are keyed by list index.
                rows = inference.predict_batch([img for _, img in readable])
                grouped: dict[int, list[SpeciesPrediction]] = {}
                for row in rows:
                    grouped.setdefault(int(row["file_name"]), []).append(_prediction(row))
                for index, ((media, crop), _) in enumerate(readable):
                    yield media, crop, grouped.get(index, [])
