"""Species detection in recordings with BirdNET v3 or Perch 2 (via the optional ``chirppipe`` package).

Uses chirppipe's ``ONNXBioacousticsPredictor`` building blocks (audio loading,
chunking, ONNX inference) but not its ``predict``, which writes a CSV next
to the recording: originals are only read, nothing is written beside them.
Each recording gets the most probable species over all chunks, with the time
span of the chunk that scored best.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from naturelog.media import MediaFile
from naturelog.species.bioclip import DetectorUnavailable, detector_available
from naturelog.species.models import LINEAGE_RANKS, AudioModel, SpeciesPrediction

#: Candidates kept per recording.
TOP_K = 5
# Columns of a labels CSV joined into one label, split again below.
_SEP = "\x1f"


@dataclass(frozen=True, slots=True)
class _ModelSpec:
    """chirppipe predictor settings of one audio model (as in chirppipe's presets)."""

    model_file: str
    labels_file: str
    label_format: str
    chunk_length: float
    predictions_key: str = "predictions"


_SPECS: dict[AudioModel, _ModelSpec] = {
    AudioModel.BIRDNET_V3: _ModelSpec(
        "birdnet_v3.onnx",
        "birdnet_v3_labels.csv",
        _SEP.join(("{sci_name}", "{com_name}", "{class}", "{order}")),
        chunk_length=3.0,
    ),
    # Perch 2 labels carry only the scientific name.
    AudioModel.PERCH_V2: _ModelSpec(
        "perch_v2.onnx",
        "perch_2_labels.csv",
        "{inat2024_fsd50k}",
        chunk_length=5.0,
        predictions_key="label",
    ),
}


#: Environment variable pointing to a folder with the audio model files.
MODELS_DIR_ENV = "NATURELOG_MODELS_DIR"


def _models_dir() -> Path:
    """Folder with the ONNX model and labels files.

    Order: ``$NATURELOG_MODELS_DIR``; the ``models`` folder of a local chirppipe
    checkout (editable install); ``models/`` in the NatureLog project root, which
    is also where the files are expected when chirppipe is installed from git.
    """
    if env := os.environ.get(MODELS_DIR_ENV):
        return Path(env).expanduser()
    import chirps

    checkout = Path(chirps.__file__).resolve().parent.parent / "models"
    if checkout.is_dir():
        return checkout
    return Path(__file__).resolve().parents[2] / "models"


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


class AudioDetector:
    """Wraps chirppipe's ONNX bioacoustics predictor for one model; one recording at a time."""

    def __init__(self, model: AudioModel = AudioModel.BIRDNET_V3) -> None:
        self.model = model
        self._predictor: Any = None
        self._lock = threading.Lock()

    def _model(self) -> Any:
        if self._predictor is None:
            if not detector_available():
                raise DetectorUnavailable(
                    "Species detection is not installed – run: uv sync --extra species"
                )
            from chirps.ml.onnx_bioacoustics import ONNXBioacousticsPredictor

            spec, name = _SPECS[self.model], self.model.label
            models = _models_dir()
            model, labels = models / spec.model_file, models / spec.labels_file
            missing = [p.name for p in (model, labels) if not p.is_file()]
            if missing:
                raise DetectorUnavailable(
                    f"{name} model files missing in {models}: {', '.join(missing)} "
                    f"(download them as described in chirppipe's models/README.md, or set {MODELS_DIR_ENV})"
                )
            predictor = ONNXBioacousticsPredictor()
            predictor.configure({
                "model": str(model),
                "labels": str(labels),
                "label_format": spec.label_format,
                "chunk_length": spec.chunk_length,
                "predictions_key": spec.predictions_key,
            })  # fmt: skip
            try:
                predictor.load_model()  # logs instead of raising on ONNX errors
            except Exception as exc:
                raise DetectorUnavailable(f"Cannot load the {name} model: {exc}") from exc
            if predictor.session is None:
                raise DetectorUnavailable(f"Cannot load the {name} model (see log)")
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
            raise DetectorUnavailable(f"{self.model.label} model and labels file do not match")
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
