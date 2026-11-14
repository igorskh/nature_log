"""Species detection in photos (BioCLIP 2) and recordings (BirdNET v3). Independent of Qt."""

from naturelog.species.bioclip import BioClipDetector, DetectorUnavailable, detector_available
from naturelog.species.birdnet import BirdNetDetector
from naturelog.species.detector import SpeciesDetector, can_detect
from naturelog.species.models import (
    DETECTION_THRESHOLD,
    SpeciesPrediction,
    SpeciesSort,
    SpeciesSummary,
    detected,
    summarize,
    top_detection,
)

__all__ = [
    "DETECTION_THRESHOLD",
    "BioClipDetector",
    "BirdNetDetector",
    "DetectorUnavailable",
    "SpeciesDetector",
    "SpeciesPrediction",
    "SpeciesSort",
    "SpeciesSummary",
    "can_detect",
    "detected",
    "detector_available",
    "summarize",
    "top_detection",
]
