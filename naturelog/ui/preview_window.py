"""Window previewing one media file: the image or an audio player, its metadata and species."""

from __future__ import annotations

from datetime import tzinfo

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QImage, QKeySequence, QPixmap, QResizeEvent, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from naturelog.media import MediaFile, MediaKind, TimeSource, make_thumbnail
from naturelog.media.exif_details import MetadataEntry, read_metadata
from naturelog.media.spectrogram import SpectrogramError, compute_mel_spectrogram
from naturelog.species import SpeciesPrediction, detected
from naturelog.timezones import format_datetime
from naturelog.ui.spectrogram_view import SpectrogramView, spectrogram_image

#: Longest side of the decoded preview image, px.
_PREVIEW_SIZE = 2400
#: Species listed in the species panel.
_TOP_SPECIES = 10
_ROLE_START = Qt.ItemDataRole.UserRole
#: Playback line refresh while playing, ms.
_PLAYHEAD_INTERVAL = 33


def _clock(ms: int) -> str:
    seconds = max(0, ms) // 1000
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{sec:02d}" if hours else f"{minutes}:{sec:02d}"


# --- background loading -------------------------------------------------


class _Signals(QObject):
    image = Signal(int, object)  # generation, QImage | None
    metadata = Signal(int, object)  # generation, list[MetadataEntry]


class _LoadTask(QRunnable):
    """Decodes the preview image and reads EXIF metadata off the UI thread."""

    def __init__(self, media: MediaFile, generation: int) -> None:
        super().__init__()
        self.media = media
        self.generation = generation
        self.signals = _Signals()

    def run(self) -> None:
        data = make_thumbnail(self.media, _PREVIEW_SIZE)
        image = QImage.fromData(data) if data else None
        self.signals.image.emit(self.generation, image if image and not image.isNull() else None)
        self.signals.metadata.emit(self.generation, read_metadata(self.media.path))


class _SpectrogramSignals(QObject):
    done = Signal(int, object, object)  # generation, MelSpectrogram | None, QImage | str


class _SpectrogramTask(QRunnable):
    """Decodes a recording and computes its mel spectrogram off the UI thread."""

    def __init__(self, media: MediaFile, generation: int) -> None:
        super().__init__()
        self.media = media
        self.generation = generation
        self.signals = _SpectrogramSignals()

    def run(self) -> None:
        try:
            spec = compute_mel_spectrogram(self.media.path)
        except (SpectrogramError, OSError) as exc:
            self.signals.done.emit(self.generation, None, f"No spectrogram: {exc}")
            return
        self.signals.done.emit(self.generation, spec, spectrogram_image(spec))


# --- widgets --------------------------------------------------------------


class _ImageView(QLabel):
    """Shows an image scaled to fit, keeping its aspect ratio."""

    def __init__(self) -> None:
        super().__init__()
        self._pixmap: QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

    def set_image(self, image: QImage | None, placeholder: str = "") -> None:
        self._pixmap = QPixmap.fromImage(image) if image is not None else None
        if self._pixmap is None:
            self.setPixmap(QPixmap())
            self.setText(placeholder)
        else:
            self.setText("")
            self._rescale()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap is None:
            return
        ratio = self.devicePixelRatioF()
        scaled = self._pixmap.scaled(
            self.size() * ratio,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(ratio)
        self.setPixmap(scaled)


class _AudioPlayer(QWidget):
    """Mel spectrogram, play/pause button, seek slider and position of an audio file."""

    def __init__(self) -> None:
        super().__init__()
        self._player = QMediaPlayer(self)
        self._output = QAudioOutput(self)
        self._player.setAudioOutput(self._output)
        self._seeking = False
        self._generation = 0
        self._tasks: set[_SpectrogramTask] = set()
        self._spectrogram = SpectrogramView()
        self._spectrogram.seekRequested.connect(self._player.setPosition)
        self._playhead = QTimer(self)
        self._playhead.setInterval(_PLAYHEAD_INTERVAL)
        self._playhead.timeout.connect(
            lambda: self._spectrogram.set_position(self._player.position())
        )

        style = self.style()
        self._play_icon = style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        self._pause_icon = style.standardIcon(QStyle.StandardPixmap.SP_MediaPause)
        self._play = QPushButton()
        self._play.setIcon(self._play_icon)
        self._play.setIconSize(QSize(28, 28))
        self._play.clicked.connect(self.toggle)
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self._slider.sliderReleased.connect(self._on_slider_released)
        self._slider.sliderMoved.connect(self._spectrogram.set_position)
        self._time = QLabel("0:00 / 0:00")
        self._error = QLabel()
        self._error.setWordWrap(True)
        self._error.hide()

        controls = QHBoxLayout()
        controls.addWidget(self._play)
        controls.addWidget(self._slider, 1)
        controls.addWidget(self._time)
        layout = QVBoxLayout(self)
        layout.addWidget(self._spectrogram, 1)
        layout.addLayout(controls)
        layout.addWidget(self._error)

        self._player.durationChanged.connect(self._on_duration)
        self._player.positionChanged.connect(self._on_position)
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.errorOccurred.connect(self._on_error)

    def set_source(self, media: MediaFile) -> None:
        self._player.stop()
        self._error.hide()
        self._slider.setRange(0, 0)
        self._player.setSource(QUrl.fromLocalFile(str(media.path)))
        self._generation += 1
        self._spectrogram.set_position(0)
        self._spectrogram.set_message("Computing spectrogram…")
        task = _SpectrogramTask(media, self._generation)
        task.signals.done.connect(self._on_spectrogram)
        task.signals.done.connect(lambda *_: self._tasks.discard(task))
        self._tasks.add(task)  # keep the Python object (and its signals) alive
        QThreadPool.globalInstance().start(task)

    def stop(self) -> None:
        self._player.stop()

    def toggle(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def play_from(self, seconds: float) -> None:
        self._player.setPosition(int(seconds * 1000))
        self._player.play()

    def _on_slider_released(self) -> None:
        self._seeking = False
        self._player.setPosition(self._slider.value())

    def _on_duration(self, duration: int) -> None:
        self._slider.setRange(0, duration)
        self._on_position(self._player.position())

    def _on_spectrogram(self, generation: int, spec: object, result: object) -> None:
        if generation != self._generation:
            return
        if spec is None:
            self._spectrogram.set_message(str(result))
        else:
            self._spectrogram.set_spectrogram(spec, result)  # type: ignore[arg-type]

    def _on_position(self, position: int) -> None:
        if not self._seeking:
            self._slider.setValue(position)
            self._spectrogram.set_position(position)
        self._time.setText(f"{_clock(position)} / {_clock(self._player.duration())}")

    def _on_state(self, state: QMediaPlayer.PlaybackState) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._play.setIcon(self._pause_icon if playing else self._play_icon)
        if playing:
            self._playhead.start()
        else:
            self._playhead.stop()
            self._spectrogram.set_position(self._player.position())

    def _on_error(self, _error: QMediaPlayer.Error, text: str) -> None:
        self._error.setText(f"Cannot play this file: {text}")
        self._error.show()


# --- window ---------------------------------------------------------------


class PreviewWindow(QWidget):
    """One media file: image or audio player, with date/EXIF metadata and detected species."""

    #: User asked for the previous (-1) or next (+1) file in the list.
    navigateRequested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Preview")
        self.resize(1200, 780)

        self._media: MediaFile | None = None
        self._tz: tzinfo | None = None
        self._generation = 0
        self._tasks: set[_LoadTask] = set()
        #: Placeholder row while EXIF metadata is being read.
        self._metadata_loading: QTreeWidgetItem | None = None

        self._image = _ImageView()
        self._audio = _AudioPlayer()
        self._unsupported = QLabel("No preview for this file type")
        self._unsupported.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._metadata = QTreeWidget()
        self._metadata.setHeaderLabels(["Tag", "Value"])
        self._metadata.setRootIsDecorated(True)
        self._metadata.setAlternatingRowColors(True)
        self._metadata.setUniformRowHeights(True)
        self._metadata.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._metadata.setWordWrap(False)

        self._species = QListWidget()
        self._species.setWordWrap(True)
        self._species.itemActivated.connect(self._on_species_activated)

        side = QSplitter(Qt.Orientation.Vertical)
        side.addWidget(self._panel("Metadata", self._metadata))
        side.addWidget(self._panel("Detected species", self._species))
        side.setSizes([480, 240])

        style = self.style()
        self._previous = QPushButton("Previous")
        self._previous.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_ArrowBack))
        self._previous.setToolTip("Previous file in the list (Left arrow)")
        self._previous.clicked.connect(lambda: self.navigateRequested.emit(-1))
        self._next = QPushButton("Next")
        self._next.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_ArrowForward))
        self._next.setLayoutDirection(Qt.LayoutDirection.RightToLeft)  # icon after text
        self._next.setToolTip("Next file in the list (Right arrow)")
        self._next.clicked.connect(lambda: self.navigateRequested.emit(1))
        self._position = QLabel()
        self._position.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for button, key in ((self._previous, Qt.Key.Key_Left), (self._next, Qt.Key.Key_Right)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(button.click)  # click() does nothing when disabled
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self).activated.connect(self.close)
        navigation = QHBoxLayout()
        navigation.addWidget(self._previous)
        navigation.addWidget(self._position, 1)
        navigation.addWidget(self._next)

        viewer = QWidget()
        viewer_layout = QVBoxLayout(viewer)
        viewer_layout.setContentsMargins(0, 0, 0, 0)
        viewer_layout.addLayout(navigation)
        for widget in (self._image, self._audio, self._unsupported):
            viewer_layout.addWidget(widget, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(viewer)
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([820, 380])

        layout = QVBoxLayout(self)
        layout.addWidget(splitter)

    @staticmethod
    def _panel(title: str, content: QWidget) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(f"<b>{title}</b>")
        layout.addWidget(label)
        layout.addWidget(content, 1)
        return panel

    # --- public API -----------------------------------------------------

    def media(self) -> MediaFile | None:
        return self._media

    def show_media(
        self, media: MediaFile, tz: tzinfo, predictions: list[SpeciesPrediction] | None
    ) -> None:
        """Preview ``media``; capture times are shown in ``tz``."""
        self._media = media
        self._tz = tz
        self._generation += 1
        self.setWindowTitle(f"Preview – {media.name}")

        self._audio.stop()
        is_audio = media.kind is MediaKind.AUDIO
        self._image.setVisible(media.kind.is_image)
        self._audio.setVisible(is_audio)
        self._unsupported.setVisible(not (is_audio or media.kind.is_image))

        self._metadata.clear()
        self._metadata_loading = None
        self._add_datetime(media, tz)
        self.set_predictions(predictions)

        if is_audio:
            self._audio.set_source(media)
        elif media.kind.is_image:
            self._image.set_image(None, "Loading…")
            self._metadata_loading = QTreeWidgetItem(["EXIF", "Loading…"])
            self._metadata.addTopLevelItem(self._metadata_loading)
            task = _LoadTask(media, self._generation)
            task.signals.image.connect(self._on_image)
            task.signals.metadata.connect(self._on_metadata)
            task.signals.metadata.connect(lambda *_: self._tasks.discard(task))
            self._tasks.add(task)  # keep the Python object (and its signals) alive
            QThreadPool.globalInstance().start(task)

    def set_position(self, row: int | None, count: int) -> None:
        """Where the previewed file is in the list; enables Previous/Next accordingly."""
        if row is None:
            self._position.setText("Not in the list")
        else:
            self._position.setText(f"{row + 1} / {count}")
        self._previous.setEnabled(row is not None and row > 0)
        self._next.setEnabled(row is not None and row < count - 1)

    def set_timezone(self, tz: tzinfo) -> None:
        """Re-show the date and time in another timezone."""
        if self._media is None:
            return
        self._tz = tz
        if self._metadata.topLevelItem(0) is not None:
            self._metadata.takeTopLevelItem(0)  # the "Date and time" group
        self._add_datetime(self._media, tz)

    def set_predictions(self, predictions: list[SpeciesPrediction] | None) -> None:
        """Show the top detected species of the previewed file."""
        self._species.clear()
        hits = detected(predictions)[:_TOP_SPECIES]
        if not hits:
            text = "Not analysed yet" if predictions is None else "No species detected"
            item = QListWidgetItem(text)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._species.addItem(item)
            return
        for p in hits:
            lines = [f"{p.display_name} · {p.score:.0%}"]
            if p.common_name and p.scientific_name != p.common_name:
                lines.append(p.scientific_name)
            if p.span_text:
                lines.append(f"at {p.span_text}")
            item = QListWidgetItem("\n".join(lines))
            if p.start_sec is not None:
                item.setData(_ROLE_START, p.start_sec)
                item.setToolTip("Double-click to play from here")
            self._species.addItem(item)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._audio.stop()
        super().closeEvent(event)

    # --- internals ------------------------------------------------------

    def _add_datetime(self, media: MediaFile, tz: tzinfo) -> None:
        group = QTreeWidgetItem(["Date and time"])
        when = media.capture_time(tz)
        source = (
            "camera (EXIF DateTimeOriginal)"
            if media.time_source is TimeSource.EXIF
            else "file created"
        )
        for tag, value in (
            ("Date", when.astimezone(tz).strftime("%Y-%m-%d")),
            ("Time", format_datetime(when, tz)),
            ("Source", source),
            ("File", str(media.path)),
        ):
            child = QTreeWidgetItem([tag, value])
            child.setToolTip(1, value)
            group.addChild(child)
        self._metadata.insertTopLevelItem(0, group)
        group.setExpanded(True)

    def _on_image(self, generation: int, image: QImage | None) -> None:
        if generation == self._generation:
            self._image.set_image(image, "Cannot display this image")

    def _on_metadata(self, generation: int, entries: list[MetadataEntry]) -> None:
        if generation != self._generation:
            return
        if self._metadata_loading is not None:
            index = self._metadata.indexOfTopLevelItem(self._metadata_loading)
            if index >= 0:
                self._metadata.takeTopLevelItem(index)
            self._metadata_loading = None
        if not entries:
            self._metadata.addTopLevelItem(QTreeWidgetItem(["EXIF", "No metadata found"]))
            return
        groups: dict[str, QTreeWidgetItem] = {}
        for entry in entries:
            group = groups.get(entry.group)
            if group is None:
                group = groups[entry.group] = QTreeWidgetItem([entry.group])
                self._metadata.addTopLevelItem(group)
            child = QTreeWidgetItem([entry.tag, entry.value])
            child.setToolTip(1, entry.value)
            group.addChild(child)
        for group in groups.values():
            group.setExpanded(True)

    def _on_species_activated(self, item: QListWidgetItem) -> None:
        start = item.data(_ROLE_START)
        if start is not None and self._audio.isVisible():
            self._audio.play_from(float(start))
