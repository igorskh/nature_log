"""Bird sound detection in recordings with BirdNET v3 (via the optional ``chirppipe`` package).

Uses chirppipe's ``ONNXBioacousticsPredictor`` building blocks (audio loading,
3 s chunking, ONNX inference) but not its ``predict``, which writes a CSV next
to the recording: originals are only read, nothing is written beside them.
Each recording gets the most probable species over all chunks, with the time
span of the chunk that scored best.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from naturelog.media import MediaFile
from naturelog.species.bioclip import DetectorUnavailable, detector_available
from naturelog.species.models import LINEAGE_RANKS, SpeciesPrediction

#: Candidates kept per recording.
TOP_K = 5
# Columns of the BirdNET labels CSV joined into one label, split again below.
_SEP = "\x1f"
_LABEL_FORMAT = _SEP.join(("{sci_name}", "{com_name}", "{class}", "{order}"))


def _models_dir() -> Path:
    """chirppipe's ``models`` folder (its defaults are relative to the working directory)."""
    import chirps

    return Path(chirps.__file__).resolve().parent.parent / "models"


def _prediction(label: str, score: float, span: tuple[float, float]) -> SpeciesPrediction:
    sci_name, com_name, klass, order = (label.split(_SEP) + ["", "", "", ""])[:4]
    ranks = dict.fromkeys(LINEAGE_RANKS, "")
    ranks["class"], ranks["order"] = klass, order
    return SpeciesPrediction(
        scientific_name=sci_name,
        common_name=com_name,
        score=score,
        lineage=tuple(ranks.values()),
        start_sec=round(span[0], 2),
        end_sec=round(span[1], 2),
    )


class BirdNetDetector:
    """Wraps chirppipe's BirdNET v3 ONNX predictor; runs one recording at a time."""

    def __init__(self) -> None:
        self._predictor: Any = None
        self._lock = threading.Lock()

    def _model(self) -> Any:
        if self._predictor is None:
            if not detector_available():
                raise DetectorUnavailable(
                    "Species detection is not installed – run: uv sync --extra species"
                )
            from chirps.ml.onnx_bioacoustics import ONNXBioacousticsPredictor

            models = _models_dir()
            model, labels = models / "birdnet_v3.onnx", models / "birdnet_v3_labels.csv"
            missing = [p.name for p in (model, labels) if not p.is_file()]
            if missing:
                raise DetectorUnavailable(
                    f"BirdNET model files missing in {models}: {', '.join(missing)} "
                    "(see chirppipe models/README.md)"
                )
            predictor = ONNXBioacousticsPredictor()
            predictor.configure({
                "model": str(model),
                "labels": str(labels),
                "label_format": _LABEL_FORMAT,
            })  # fmt: skip
            try:
                predictor.load_model()  # logs instead of raising on ONNX errors
            except Exception as exc:
                raise DetectorUnavailable(f"Cannot load the BirdNET model: {exc}") from exc
            if predictor.session is None:
                raise DetectorUnavailable("Cannot load the BirdNET model (see log)")
            self._predictor = predictor
        return self._predictor

    def _classify(self, predictor: Any, path: Path) -> list[SpeciesPrediction] | None:
        from chirps.ml.utils import chunk_audio

        loaded = predictor.load_audio(str(path))  # None if it cannot be decoded
        if not loaded:
            return None
        samples, rate = loaded
        chunks, spans = chunk_audio(
            samples, predictor.chunk_length, overlap=predictor.overlap, sr=rate
        )
        if len(chunks) == 0:
            return []
        probs, _ = predictor.run_onnx_inference(chunks)
        if probs.shape[-1] != len(predictor.labels):
            raise DetectorUnavailable("BirdNET model and labels file do not match")
        best_chunk = probs.argmax(axis=0)  # per species: chunk with the highest score
        best_score = probs.max(axis=0)
        top = best_score.argsort()[::-1][:TOP_K]
        return [
            _prediction(predictor.labels[i], float(best_score[i]), spans[best_chunk[i]])
            for i in top
        ]

    def detect(
        self,
        files: Sequence[MediaFile],
        cancelled: threading.Event | None = None,
        on_loaded: Callable[[], None] | None = None,
    ) -> Iterator[tuple[MediaFile, list[SpeciesPrediction] | None]]:
        """Yield ``(media, top-k predictions)`` per recording; ``None`` if unreadable."""
        with self._lock:
            predictor = self._model()
            if on_loaded is not None:
                on_loaded()
            for media in files:
                if cancelled is not None and cancelled.is_set():
                    return
                yield media, self._classify(predictor, media.path)
