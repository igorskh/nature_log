"""Species detection results and their per-species summary. Independent of Qt."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

#: Minimum probability for a prediction to count as a detection.
DETECTION_THRESHOLD = 0.2

#: Taxon ranks above species, as returned by BioCLIP (kingdom … genus).
LINEAGE_RANKS = ("kingdom", "phylum", "class", "order", "family", "genus")


def _clock(seconds: float) -> str:
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{sec:02d}" if hours else f"{minutes}:{sec:02d}"


@dataclass(frozen=True, slots=True)
class SpeciesPrediction:
    """One candidate species for a photo or recording with its model probability (0–1)."""

    scientific_name: str
    common_name: str
    score: float
    #: Higher taxa, kingdom first (see :data:`LINEAGE_RANKS`); unknown ranks are "".
    lineage: tuple[str, ...] = ()
    #: For recordings: span (seconds from start) of the segment that scored best.
    start_sec: float | None = None
    end_sec: float | None = None

    @property
    def display_name(self) -> str:
        return self.common_name or self.scientific_name

    @property
    def span_text(self) -> str:
        """``"0:12–0:15"`` for recordings, else ``""``."""
        if self.start_sec is None or self.end_sec is None:
            return ""
        return f"{_clock(self.start_sec)}–{_clock(self.end_sec)}"

    def to_json(self) -> dict[str, Any]:
        return {
            "scientific_name": self.scientific_name,
            "common_name": self.common_name,
            "score": self.score,
            "lineage": list(self.lineage),
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SpeciesPrediction:
        return cls(
            scientific_name=str(data["scientific_name"]),
            common_name=str(data.get("common_name") or ""),
            score=float(data["score"]),
            lineage=tuple(data.get("lineage") or ()),
            start_sec=data.get("start_sec"),
            end_sec=data.get("end_sec"),
        )


def detected(
    predictions: Sequence[SpeciesPrediction] | None, threshold: float = DETECTION_THRESHOLD
) -> list[SpeciesPrediction]:
    """Predictions at or above ``threshold``, most probable first."""
    if not predictions:
        return []
    return sorted((p for p in predictions if p.score >= threshold), key=lambda p: -p.score)


def top_detection(
    predictions: Sequence[SpeciesPrediction] | None, threshold: float = DETECTION_THRESHOLD
) -> SpeciesPrediction | None:
    hits = detected(predictions, threshold)
    return hits[0] if hits else None


class SpeciesSort(StrEnum):
    PROBABILITY = "probability"
    ALPHABETICAL = "alphabetical"


@dataclass(frozen=True, slots=True)
class SpeciesSummary:
    """A species detected in one or more files."""

    scientific_name: str
    common_name: str
    #: Highest probability over all files it was detected in.
    best_score: float
    paths: tuple[Path, ...]

    @property
    def display_name(self) -> str:
        return self.common_name or self.scientific_name


def summarize(
    detections: Mapping[Path, Sequence[SpeciesPrediction] | None],
    sort: SpeciesSort = SpeciesSort.PROBABILITY,
    threshold: float = DETECTION_THRESHOLD,
) -> list[SpeciesSummary]:
    """Every species detected in any file, one entry per species."""
    best: dict[str, SpeciesPrediction] = {}
    paths: dict[str, list[Path]] = {}
    for path, predictions in detections.items():
        for p in detected(predictions, threshold):
            if p.scientific_name not in best or p.score > best[p.scientific_name].score:
                best[p.scientific_name] = p
            paths.setdefault(p.scientific_name, []).append(path)
    summaries = [
        SpeciesSummary(p.scientific_name, p.common_name, p.score, tuple(paths[name]))
        for name, p in best.items()
    ]
    if sort is SpeciesSort.ALPHABETICAL:
        summaries.sort(key=lambda s: (s.display_name.casefold(), s.scientific_name))
    else:
        summaries.sort(key=lambda s: (-s.best_score, s.display_name.casefold()))
    return summaries
