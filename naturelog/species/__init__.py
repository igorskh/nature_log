"""Species detection in photos (BioCLIP 2) and recordings (BirdNET v3, Perch 2). Independent of Qt."""

from naturelog.species.audio import AudioDetector
from naturelog.species.bioclip import BioClipDetector, DetectorUnavailable, detector_available
from naturelog.species.detector import SpeciesDetector, can_detect
from naturelog.species.models import (
    DETECTION_THRESHOLD,
    AudioModel,
    DetectionConfig,
    ImageModel,
    SpeciesPrediction,
    SpeciesSort,
    SpeciesSummary,
    detected,
    summarize,
    top_detection,
)

__all__ = [
    "DETECTION_THRESHOLD",
    "AudioDetector",
    "AudioModel",
    "BioClipDetector",
    "DetectionConfig",
    "DetectorUnavailable",
    "ImageModel",
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
