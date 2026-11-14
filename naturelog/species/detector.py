"""Routes media to the matching detector: photos → BioCLIP 2, recordings → BirdNET v3."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence

from naturelog.media import MediaFile, MediaKind
from naturelog.species.bioclip import BioClipDetector
from naturelog.species.birdnet import BirdNetDetector
from naturelog.species.models import SpeciesPrediction


def can_detect(media: MediaFile) -> bool:
    """Whether species detection supports this kind of file."""
    return media.kind.is_image or media.kind is MediaKind.AUDIO


class SpeciesDetector:
    """One entry point for all supported media; models load on first use and stay loaded."""

    def __init__(self) -> None:
        self.photos = BioClipDetector()
        self.recordings = BirdNetDetector()

    def detect(
        self,
        files: Sequence[MediaFile],
        cancelled: threading.Event | None = None,
        on_loaded: Callable[[], None] | None = None,
    ) -> Iterator[tuple[MediaFile, list[SpeciesPrediction] | None]]:
        """Yield ``(media, predictions)``: photos first, then recordings.

        ``on_loaded`` is called whenever a model is ready and its files start.
        Unsupported files are skipped. Raises ``DetectorUnavailable`` if a
        needed model cannot be loaded.
        """
        photos = [m for m in files if m.kind.is_image]
        recordings = [m for m in files if m.kind is MediaKind.AUDIO]
        if photos:
            yield from self.photos.detect(photos, cancelled, on_loaded)
        if recordings and not (cancelled is not None and cancelled.is_set()):
            yield from self.recordings.detect(recordings, cancelled, on_loaded)
