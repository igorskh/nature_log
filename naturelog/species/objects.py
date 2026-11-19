"""Object bounding boxes in photos with LibreYOLO (via the optional ``chirppipe`` package).

Wraps chirppipe's ``LibreYoloDetection``. The photo is decoded to an oriented
preview (embedded preview for RAW files) and analysed in memory; boxes are
returned as :class:`ImageCrop` fractions of the upright image, the same frame
as user-drawn crops. The weights (``LibreYOLO9t.pt``) are looked up in the
models folder shared with the audio models and downloaded there on first use.
Independent of Qt.
"""

from __future__ import annotations

import importlib.util
import io
import threading
from dataclasses import dataclass
from typing import Any

from PIL import Image

from naturelog.media import MediaFile, make_thumbnail
from naturelog.media.crops import ImageCrop, make_image_crop
from naturelog.species.audio import _models_dir
from naturelog.species.bioclip import DetectorUnavailable

#: Weights file in the models folder (auto-downloaded by LibreYOLO if missing).
MODEL_FILE = "LibreYOLO9t.pt"
#: Longest side of the preview fed to the detector, px.
_INPUT_SIZE = 1280
#: Minimum confidence for a box to be kept.
BOX_THRESHOLD = 0.25


@dataclass(frozen=True, slots=True)
class DetectedBox:
    """One detected object: its region of the photo, class name and confidence (0–1)."""

    crop: ImageCrop
    label: str
    score: float


def boxes_available() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in ("chirps", "libreyolo"))


class ObjectDetector:
    """Loads the model on first use and keeps it loaded; one photo at a time."""

    def __init__(self) -> None:
        self._detection: Any = None
        self._lock = threading.Lock()

    def _model(self) -> Any:
        if self._detection is None:
            if not boxes_available():
                raise DetectorUnavailable(
                    "Object detection is not installed – run: uv sync --extra species"
                )
            try:
                from chirps.ml.libre_yolo_detection import LibreYoloDetection

                models = _models_dir()
                models.mkdir(parents=True, exist_ok=True)
                detection = LibreYoloDetection()
                detection.configure({"model_path": str(models / MODEL_FILE)})
            except Exception as exc:
                raise DetectorUnavailable(f"Cannot load the LibreYOLO model: {exc}") from exc
            self._detection = detection
        return self._detection

    def detect(self, media: MediaFile, threshold: float = BOX_THRESHOLD) -> list[DetectedBox]:
        """Boxes of objects in the photo, most confident first; ``[]`` if it cannot be read.

        Raises :class:`DetectorUnavailable` if the model cannot be loaded.
        """
        with self._lock:
            detection = self._model()
            data = make_thumbnail(media, _INPUT_SIZE)
            if data is None:
                return []
            image = Image.open(io.BytesIO(data)).convert("RGB")
            w, h = image.size
            result = detection.process(input=image)["detections"]
            names = detection.model.names
            boxes = result.boxes
            found: list[DetectedBox] = []
            for xyxy, conf, cls in zip(
                boxes.xyxy.tolist(), boxes.conf.tolist(), boxes.cls.tolist()
            ):
                if conf < threshold:
                    continue
                x0, y0, x1, y1 = xyxy
                crop = make_image_crop(x0 / w, y0 / h, x1 / w, y1 / h)
                if crop is not None:
                    found.append(DetectedBox(crop, str(names[int(cls)]), float(conf)))
            found.sort(key=lambda b: -b.score)
            return found
