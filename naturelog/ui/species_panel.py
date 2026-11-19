"""Right sidebar: species detected in the listed photos and recordings, and running detection."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

from PySide6.QtCore import (
    QObject,
    QRunnable,
    QSize,
    QStandardPaths,
    Qt,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from naturelog.media import MediaFile, MediaKind
from naturelog.media.crops import AudioCrop, Crop, ImageCrop
from naturelog.species import (
    DetectionConfig,
    DetectorUnavailable,
    SpeciesDetector,
    SpeciesPrediction,
    SpeciesSort,
    can_detect,
    detector_available,
    summarize,
)
from naturelog.species.stickers import CREDIT, CREDIT_URL, StickerStore
from naturelog.ui.detection_config_dialog import DetectionConfigDialog

_ROLE_PATHS = Qt.ItemDataRole.UserRole
_ROLE_NAME = Qt.ItemDataRole.UserRole + 1
_ICON_SIZE = QSize(48, 48)


class _StickerSignals(QObject):
    ready = Signal(str, object)  # scientific name, QImage | None (no sticker)
    failed = Signal(str)
    finished = Signal()


class _StickerTask(QRunnable):
    """Loads (and downloads if needed) bird stickers off the UI thread."""

    def __init__(self, store: StickerStore, names: list[tuple[str, str]], size: int) -> None:
        super().__init__()
        self.store = store
        self.names = names  # (scientific name, common name)
        self.size = size
        self.signals = _StickerSignals()

    def run(self) -> None:
        try:
            for scientific, common in self.names:
                path = self.store.fetch(scientific, common)
                image = QImage(str(path)) if path else QImage()
                if image.isNull():
                    self.signals.ready.emit(scientific, None)
                    continue
                image = image.scaled(
                    self.size,
                    self.size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self.signals.ready.emit(scientific, image)
        except (OSError, ValueError) as exc:  # offline, or a broken download
            self.signals.failed.emit(f"Bird stickers unavailable: {exc}")
        finally:
            self.signals.finished.emit()


class _DetectSignals(QObject):
    loaded = Signal()  # model ready, classification starts
    detected = Signal(object, object, object)  # Path, Crop | None, list[SpeciesPrediction] | None
    progress = Signal(int, int)
    failed = Signal(str)
    finished = Signal()


class _DetectTask(QRunnable):
    """Classifies photos and recordings off the UI thread, emitting each result as it is ready."""

    def __init__(
        self,
        detector: SpeciesDetector,
        files: list[MediaFile],
        config: DetectionConfig,
        crops: dict[Path, list[Crop]],
    ) -> None:
        super().__init__()
        self.detector = detector
        self.files = files
        self.config = config
        self.crops = crops
        #: Results expected: one per file and one per crop of a matching kind.
        self.total = len(files) + sum(
            isinstance(c, AudioCrop if m.kind is MediaKind.AUDIO else ImageCrop)
            for m in files
            for c in crops.get(m.path, ())
        )
        self.signals = _DetectSignals()
        self.cancelled = threading.Event()

    def run(self) -> None:
        try:
            results = self.detector.detect(
                self.files,
                self.config,
                cancelled=self.cancelled,
                on_loaded=self.signals.loaded.emit,
                crops=self.crops,
            )
            for done, (media, crop, predictions) in enumerate(results, 1):
                self.signals.detected.emit(media.path, crop, predictions)
                self.signals.progress.emit(done, self.total)
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
    #: Result for one crop of a file: path, crop, predictions or None.
    cropDetected = Signal(object, object, object)
    #: User asked to run detection (main window decides which photos).
    detectRequested = Signal()
    #: User picked a species; carries the paths of the files it was detected in.
    speciesActivated = Signal(list)
    message = Signal(str)
    #: User changed the detection models.
    configChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._detector = SpeciesDetector()
        self._config = DetectionConfig()
        self._task: _DetectTask | None = None
        #: Keeps running tasks (and their signals) alive, also after a cancel.
        self._alive: set[_DetectTask] = set()
        self._detections: Mapping[Path, Sequence[SpeciesPrediction] | None] = {}

        cache = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
        self._stickers = StickerStore(Path(cache) / "bird-stickers")
        self._sticker_task: _StickerTask | None = None
        self._sticker_names: list[tuple[str, str]] = []
        #: Stickers by scientific name; ``None`` once known to have none (or failed).
        self._sticker_icons: dict[str, QIcon | None] = {}
        blank = QPixmap(_ICON_SIZE)
        blank.fill(Qt.GlobalColor.transparent)
        self._blank_icon = QIcon(blank)  # keeps names aligned while stickers load

        title = QLabel("<b>Detected species</b>")
        self._detect_button = QPushButton("Detect species")
        self._detect_button.clicked.connect(self._on_detect_clicked)
        self._config_button = QToolButton()
        self._config_button.setText("⚙")
        self._config_button.setToolTip("Choose the detection models for images and audio")
        self._config_button.clicked.connect(self._on_config_clicked)
        self._update_detect_tooltip()
        if not detector_available():
            self._detect_button.setEnabled(False)
        detect_row = QHBoxLayout()
        detect_row.addWidget(self._detect_button, 1)
        detect_row.addWidget(self._config_button)

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
        self._list.setIconSize(_ICON_SIZE)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.itemActivated.connect(self._on_item_clicked)

        self._progress_bar = QProgressBar()
        self._progress_bar.hide()
        self._count_label = QLabel()
        self._credit_label = QLabel(f'<small><a href="{CREDIT_URL}">{CREDIT}</a></small>')
        self._credit_label.setOpenExternalLinks(True)
        self._credit_label.setWordWrap(True)
        self._credit_label.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(title)
        layout.addLayout(detect_row)
        layout.addLayout(sort_row)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._progress_bar)
        layout.addWidget(self._count_label)
        layout.addWidget(self._credit_label)
        self._refresh()

    # --- public API -----------------------------------------------------

    def config(self) -> DetectionConfig:
        return self._config

    def set_config(self, config: DetectionConfig) -> None:
        """Models used by the next detection run (does not emit ``configChanged``)."""
        self._config = config
        self._update_detect_tooltip()

    def is_running(self) -> bool:
        return self._task is not None

    def set_detections(self, detections: Mapping[Path, Sequence[SpeciesPrediction] | None]) -> None:
        """Show the species detected in these files."""
        self._detections = detections
        self._refresh()

    def detect(self, files: list[MediaFile], crops: dict[Path, list[Crop]] | None = None) -> None:
        """Run species detection on these photos and recordings, and on their
        ``crops``, in the background."""
        supported = [m for m in files if can_detect(m)]
        if self._task is not None:
            self.message.emit("Species detection is already running")
            return
        if not supported:
            self.message.emit("No photos or recordings to detect species in")
            return
        task = _DetectTask(self._detector, supported, self._config, crops or {})
        task.signals.loaded.connect(lambda: self._on_loaded(task))
        task.signals.detected.connect(
            lambda path, crop, preds: self._on_detected(task, path, crop, preds)
        )
        task.signals.progress.connect(lambda done, total: self._on_progress(task, done, total))
        task.signals.failed.connect(lambda text: self._on_failed(task, text))
        task.signals.finished.connect(lambda: self._on_finished(task))
        self._task = task
        self._alive.add(task)
        self._detect_button.setEnabled(False)
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setFormat("Loading model…")
        self._progress_bar.show()
        cropped = task.total - len(supported)
        extra = f" and {cropped} crop{'s' if cropped != 1 else ''}" if cropped else ""
        self.message.emit(f"Detecting species in {len(supported)} file(s){extra}…")
        QThreadPool.globalInstance().start(task)

    def cancel(self) -> None:
        """Stop a running detection; results still in flight are dropped."""
        if self._task is not None:
            self._task.cancelled.set()
            self._reset(self._task)

    # --- detection task -------------------------------------------------

    def _on_detect_clicked(self) -> None:
        self.detectRequested.emit()

    def _on_config_clicked(self) -> None:
        dialog = DetectionConfigDialog(self._config, self)
        if dialog.exec() != DetectionConfigDialog.DialogCode.Accepted:
            return
        config = dialog.config()
        if config != self._config:
            self.set_config(config)
            self.configChanged.emit()
            self.message.emit(
                f"Species detection: images with {config.image_model.label}, "
                f"audio with {config.audio_model.label}"
            )

    def _update_detect_tooltip(self) -> None:
        if not detector_available():
            self._detect_button.setToolTip(
                "Species detection is not installed – run: uv sync --extra species"
            )
            return
        self._detect_button.setToolTip(
            f"Identify species in the selected files – photos with "
            f"{self._config.image_model.label}, recordings with "
            f"{self._config.audio_model.label} – or in all files not analysed yet "
            "if nothing is selected"
        )

    def _on_loaded(self, task: _DetectTask) -> None:
        if task is self._task:
            # Called once per model (photos, then recordings); progress keeps counting.
            if self._progress_bar.maximum() == 0:
                self._progress_bar.setRange(0, task.total)
                self._progress_bar.setValue(0)
            self._progress_bar.setFormat("Detecting species %v / %m")

    def _on_detected(self, task: _DetectTask, path: Path, crop: Crop | None, predictions) -> None:
        if task is not self._task:
            return
        if crop is None:
            self.detected.emit({path: predictions})
        else:
            self.cropDetected.emit(path, crop, predictions)

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
            item.setData(_ROLE_NAME, s.scientific_name)
            item.setIcon(self._sticker_icons.get(s.scientific_name) or self._blank_icon)
            item.setToolTip(
                f"{s.display_name} ({s.scientific_name})\n"
                f"Best probability {s.best_score:.1%}\nClick to select its files"
            )
            self._list.addItem(item)
        analysed = sum(1 for p in self._detections.values() if p is not None)
        self._count_label.setText(
            f"{len(summaries)} species · {analysed} file{'s' if analysed != 1 else ''} analysed"
        )
        self._sticker_names = [(s.scientific_name, s.common_name) for s in summaries]
        self._load_stickers()

    # --- stickers -------------------------------------------------------

    def _load_stickers(self) -> None:
        """Fetch stickers not looked up yet; one task at a time, rerun when it ends."""
        self._credit_label.setVisible(any(self._sticker_icons.values()))
        if self._sticker_task is not None:
            return
        missing = [n for n in self._sticker_names if n[0] not in self._sticker_icons]
        if not missing:
            return
        ratio = max(1.0, self.devicePixelRatioF())
        task = _StickerTask(self._stickers, missing, round(_ICON_SIZE.width() * ratio))
        task.signals.ready.connect(self._on_sticker)
        task.signals.failed.connect(lambda text: self._on_sticker_failed(task, text))
        task.signals.finished.connect(lambda: self._on_stickers_finished(task))
        self._sticker_task = task
        QThreadPool.globalInstance().start(task)

    def _on_sticker(self, scientific_name: str, image: QImage | None) -> None:
        icon = None
        if image is not None:
            pixmap = QPixmap.fromImage(image)
            pixmap.setDevicePixelRatio(max(1.0, self.devicePixelRatioF()))
            icon = QIcon(pixmap)
        self._sticker_icons[scientific_name] = icon
        if icon is None:
            return
        self._credit_label.show()
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.data(_ROLE_NAME) == scientific_name:
                item.setIcon(icon)

    def _on_sticker_failed(self, task: _StickerTask, text: str) -> None:
        # Don't retry in this session: mark what was left as having no sticker.
        for scientific, _common in task.names:
            self._sticker_icons.setdefault(scientific, None)
        self.message.emit(text)

    def _on_stickers_finished(self, task: _StickerTask) -> None:
        if task is self._sticker_task:
            self._sticker_task = None
            self._load_stickers()  # pick up species listed while the task ran

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        self.speciesActivated.emit(item.data(_ROLE_PATHS))
