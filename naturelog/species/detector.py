"""Routes media to the configured detector: photos → BioCLIP 2, recordings → BirdNET v3 or Perch 2."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

from naturelog.media import MediaFile, MediaKind
from naturelog.media.crops import AudioCrop, Crop, ImageCrop
from naturelog.species.audio import AudioDetector
from naturelog.species.bioclip import BioClipDetector
from naturelog.species.models import AudioModel, DetectionConfig, ImageModel, SpeciesPrediction


def can_detect(media: MediaFile) -> bool:
    """Whether species detection supports this kind of file."""
    return media.kind.is_image or media.kind is MediaKind.AUDIO


class SpeciesDetector:
    """One entry point for all supported media; models load on first use and stay loaded."""

    def __init__(self) -> None:
        self._photos: dict[ImageModel, BioClipDetector] = {}
        self._recordings: dict[AudioModel, AudioDetector] = {}

    def _photo_detector(self, model: ImageModel) -> BioClipDetector:
        # BioCLIP 2 is the only image model so far.
        return self._photos.setdefault(model, BioClipDetector())

    def _recording_detector(self, model: AudioModel) -> AudioDetector:
        if model not in self._recordings:
            self._recordings[model] = AudioDetector(model)
        return self._recordings[model]

    def detect(
        self,
        files: Sequence[MediaFile],
        config: DetectionConfig | None = None,
        cancelled: threading.Event | None = None,
        on_loaded: Callable[[], None] | None = None,
        crops: Mapping[Path, Sequence[Crop]] | None = None,
    ) -> Iterator[tuple[MediaFile, Crop | None, list[SpeciesPrediction] | None]]:
        """Yield ``(media, crop, predictions)``: photos first, then recordings.

        Each file is classified as a whole (crop ``None``) and, if ``crops`` lists
        crops of it, each crop separately.
        ``config`` picks the models (defaults: BioCLIP 2, BirdNET v3).
        ``on_loaded`` is called whenever a model is ready and its files start.
        Unsupported files are skipped. Raises ``DetectorUnavailable`` if a
        needed model cannot be loaded.
        """
        config = config or DetectionConfig()
        crops = crops or {}
        photos: list[tuple[MediaFile, ImageCrop | None]] = []
        recordings: list[tuple[MediaFile, AudioCrop | None]] = []
        for m in files:
            if m.kind.is_image:
                photos.append((m, None))
                photos += [(m, c) for c in crops.get(m.path, ()) if isinstance(c, ImageCrop)]
            elif m.kind is MediaKind.AUDIO:
                recordings.append((m, None))
                recordings += [(m, c) for c in crops.get(m.path, ()) if isinstance(c, AudioCrop)]
        if photos:
            yield from self._photo_detector(config.image_model).detect(photos, cancelled, on_loaded)
        if recordings and not (cancelled is not None and cancelled.is_set()):
            detector = self._recording_detector(config.audio_model)
            yield from detector.detect(recordings, cancelled, on_loaded)
