"""Right sidebar: species detected in the listed photos and recordings, and running detection."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from naturelog.media import MediaFile
from naturelog.species import (
    DetectorUnavailable,
    SpeciesDetector,
    SpeciesPrediction,
    SpeciesSort,
    can_detect,
    detector_available,
    summarize,
)

_ROLE_PATHS = Qt.ItemDataRole.UserRole


class _DetectSignals(QObject):
    loaded = Signal()  # model ready, classification starts
    detected = Signal(object, object)  # Path, list[SpeciesPrediction] | None
    progress = Signal(int, int)
    failed = Signal(str)
    finished = Signal()


class _DetectTask(QRunnable):
    """Classifies photos and recordings off the UI thread, emitting each result as it is ready."""

    def __init__(self, detector: SpeciesDetector, files: list[MediaFile]) -> None:
        super().__init__()
        self.detector = detector
        self.files = files
        self.signals = _DetectSignals()
        self.cancelled = threading.Event()

    def run(self) -> None:
        total = len(self.files)
        try:
            results = self.detector.detect(
                self.files, cancelled=self.cancelled, on_loaded=self.signals.loaded.emit
            )
            for done, (media, predictions) in enumerate(results, 1):
                self.signals.detected.emit(media.path, predictions)
                self.signals.progress.emit(done, total)
        except DetectorUnavailable as exc:
            self.signals.failed.emit(str(exc))
        except Exception as exc:  # report instead of dying silently in a worker
            self.signals.failed.emit(f"Species detection failed: {exc}")
        finally:
            self.signals.finished.emit()


class SpeciesPanel(QWidget):
    """Species detected in all listed photos and recordings, sortable by probability or name."""

    #: Results of a detection run: ``{path: predictions or None}`` (one per file).
    detected = Signal(object)  # dict[Path, ...]: Signal(dict) would need str keys
    #: User asked to run detection (main window decides which photos).
    detectRequested = Signal()
    #: User picked a species; carries the paths of the files it was detected in.
    speciesActivated = Signal(list)
    message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._detector = SpeciesDetector()
        self._task: _DetectTask | None = None
        #: Keeps running tasks (and their signals) alive, also after a cancel.
        self._alive: set[_DetectTask] = set()
        self._detections: Mapping[Path, Sequence[SpeciesPrediction] | None] = {}

        title = QLabel("<b>Detected species</b>")
        self._detect_button = QPushButton("Detect species")
        self._detect_button.setToolTip(
            "Identify species in the selected files – photos with BioCLIP 2, "
            "recordings with BirdNET v3 – or in all files not analysed yet "
            "if nothing is selected"
        )
        self._detect_button.clicked.connect(self._on_detect_clicked)
        if not detector_available():
            self._detect_button.setEnabled(False)
            self._detect_button.setToolTip(
                "Species detection is not installed – run: uv sync --extra species"
            )

        self._sort = QComboBox()
        self._sort.addItem("Probability", SpeciesSort.PROBABILITY)
        self._sort.addItem("Alphabetical", SpeciesSort.ALPHABETICAL)
        self._sort.currentIndexChanged.connect(self._refresh)
        sort_row = QHBoxLayout()
        sort_row.addWidget(QLabel("Sort by:"))
        sort_row.addWidget(self._sort, 1)

        self._list = QListWidget()
        self._list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._list.setSpacing(4)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.itemActivated.connect(self._on_item_clicked)

        self._progress_bar = QProgressBar()
        self._progress_bar.hide()
        self._count_label = QLabel()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(title)
        layout.addWidget(self._detect_button)
        layout.addLayout(sort_row)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._progress_bar)
        layout.addWidget(self._count_label)
        self._refresh()

    # --- public API -----------------------------------------------------

    def is_running(self) -> bool:
        return self._task is not None

    def set_detections(self, detections: Mapping[Path, Sequence[SpeciesPrediction] | None]) -> None:
        """Show the species detected in these files."""
        self._detections = detections
        self._refresh()

    def detect(self, files: list[MediaFile]) -> None:
        """Run species detection on these photos and recordings in the background."""
        supported = [m for m in files if can_detect(m)]
        if self._task is not None:
            self.message.emit("Species detection is already running")
            return
        if not supported:
            self.message.emit("No photos or recordings to detect species in")
            return
        task = _DetectTask(self._detector, supported)
        task.signals.loaded.connect(lambda: self._on_loaded(task))
        task.signals.detected.connect(lambda path, preds: self._on_detected(task, path, preds))
        task.signals.progress.connect(lambda done, total: self._on_progress(task, done, total))
        task.signals.failed.connect(lambda text: self._on_failed(task, text))
        task.signals.finished.connect(lambda: self._on_finished(task))
        self._task = task
        self._alive.add(task)
        self._detect_button.setEnabled(False)
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setFormat("Loading model…")
        self._progress_bar.show()
        self.message.emit(f"Detecting species in {len(supported)} file(s)…")
        QThreadPool.globalInstance().start(task)

    def cancel(self) -> None:
        """Stop a running detection; results still in flight are dropped."""
        if self._task is not None:
            self._task.cancelled.set()
            self._reset(self._task)

    # --- detection task -------------------------------------------------

    def _on_detect_clicked(self) -> None:
        self.detectRequested.emit()

    def _on_loaded(self, task: _DetectTask) -> None:
        if task is self._task:
            # Called once per model (photos, then recordings); progress keeps counting.
            if self._progress_bar.maximum() == 0:
                self._progress_bar.setRange(0, len(task.files))
                self._progress_bar.setValue(0)
            self._progress_bar.setFormat("Detecting species %v / %m")

    def _on_detected(self, task: _DetectTask, path: Path, predictions) -> None:
        if task is self._task:
            self.detected.emit({path: predictions})

    def _on_progress(self, task: _DetectTask, done: int, total: int) -> None:
        if task is self._task:
            self._progress_bar.setValue(done)

    def _on_failed(self, task: _DetectTask, text: str) -> None:
        if task is self._task:
            self.message.emit(text)

    def _on_finished(self, task: _DetectTask) -> None:
        self._alive.discard(task)
        if task is self._task:
            if not task.cancelled.is_set() and self._progress_bar.maximum():
                self.message.emit(f"Species detection finished ({len(task.files)} file(s))")
            self._reset(task)

    def _reset(self, task: _DetectTask) -> None:
        if task is self._task:
            self._task = None
            self._progress_bar.hide()
            self._detect_button.setEnabled(detector_available())

    # --- list -----------------------------------------------------------

    def _refresh(self, *_args) -> None:
        sort = self._sort.currentData() or SpeciesSort.PROBABILITY
        summaries = summarize(self._detections, sort)
        self._list.clear()
        for s in summaries:
            files = len(s.paths)
            item = QListWidgetItem(
                f"{s.display_name}\n"
                f"{s.scientific_name} · {s.best_score:.0%} · {files} file{'s' if files != 1 else ''}"
            )
            item.setData(_ROLE_PATHS, list(s.paths))
            item.setToolTip(
                f"{s.display_name} ({s.scientific_name})\n"
                f"Best probability {s.best_score:.1%}\nClick to select its files"
            )
            self._list.addItem(item)
        analysed = sum(1 for p in self._detections.values() if p is not None)
        self._count_label.setText(
            f"{len(summaries)} species · {analysed} file{'s' if analysed != 1 else ''} analysed"
        )

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        self.speciesActivated.emit(item.data(_ROLE_PATHS))
