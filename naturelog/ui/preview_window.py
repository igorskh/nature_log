"""Window previewing one media file: the image or an audio player, its crops,
metadata and species."""

from __future__ import annotations

from datetime import tzinfo

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPointF,
    QRect,
    QRectF,
    QRunnable,
    QSize,
    QSizeF,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QIcon,
    QImage,
    QInputDevice,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
    QShortcut,
    QWheelEvent,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListView,
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
from naturelog.media.crops import AudioCrop, Crop, ImageCrop, make_audio_crop, make_image_crop
from naturelog.media.exif_details import MetadataEntry, read_metadata
from naturelog.media.spectrogram import SpectrogramError, compute_mel_spectrogram
from naturelog.species import DetectorUnavailable, SpeciesPrediction, detected
from naturelog.species.objects import DetectedBox, ObjectDetector
from naturelog.timezones import format_datetime
from naturelog.ui.spectrogram_view import (
    CROP_FILL,
    CROP_OUTLINE,
    CROP_SELECTED_FILL,
    CROP_SELECTED_OUTLINE,
    SpectrogramView,
    spectrogram_image,
)

#: Longest side of the decoded preview image, px.
_PREVIEW_SIZE = 2400
#: Species listed in the species panel.
_TOP_SPECIES = 10
_ROLE_START = Qt.ItemDataRole.UserRole
#: Playback line refresh while playing, ms.
_PLAYHEAD_INTERVAL = 33
#: Size of crop thumbnails in the crop strip, px.
_CROP_ICON = QSize(120, 80)
#: Distance from a crop edge that still grabs the edge, px.
_HANDLE_TOLERANCE = 6.0
#: Smallest side of a crop while resizing it, fraction of the image.
_MIN_CROP = 0.005
#: Cursor shown over each crop handle.
_HANDLE_CURSORS = {
    "l": Qt.CursorShape.SizeHorCursor,
    "r": Qt.CursorShape.SizeHorCursor,
    "t": Qt.CursorShape.SizeVerCursor,
    "b": Qt.CursorShape.SizeVerCursor,
    "lt": Qt.CursorShape.SizeFDiagCursor,
    "rb": Qt.CursorShape.SizeFDiagCursor,
    "rt": Qt.CursorShape.SizeBDiagCursor,
    "lb": Qt.CursorShape.SizeBDiagCursor,
    "move": Qt.CursorShape.SizeAllCursor,
}


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


class _BoxesSignals(QObject):
    done = Signal(int, object)  # generation, list[DetectedBox] | str (error)


class _BoxesTask(QRunnable):
    """Detects object bounding boxes in a photo off the UI thread."""

    def __init__(self, detector: ObjectDetector, media: MediaFile, generation: int) -> None:
        super().__init__()
        self.detector = detector
        self.media = media
        self.generation = generation
        self.signals = _BoxesSignals()

    def run(self) -> None:
        try:
            result: object = self.detector.detect(self.media)
        except DetectorUnavailable as exc:
            result = str(exc)
        except Exception as exc:  # noqa: BLE001 – report any model failure to the user
            result = f"Box detection failed: {exc}"
        self.signals.done.emit(self.generation, result)


# --- widgets --------------------------------------------------------------


class _ImageView(QWidget):
    """Shows an image fitted to the widget, keeping its aspect ratio, with its crops overlaid.

    Zoom with the mouse wheel or a trackpad pinch (around the pointer); pan with
    two-finger scrolling, or by dragging outside crop mode; double-click fits the
    image again. In crop mode, dragging a rectangle over the image selects a new crop.
    Crops and the dragged rectangle are kept as fractions of the image, so they
    stay on the same image content at any zoom.
    """

    #: In crop mode the user dragged a rectangle: corners as fractions of the image.
    rectSelected = Signal(float, float, float, float)
    #: User pressed on crop ``index`` (to select it).
    cropClicked = Signal(int)
    #: User resized or moved crop ``index``: the new ImageCrop.
    cropEdited = Signal(int, object)

    def __init__(self) -> None:
        super().__init__()
        self._image: QImage | None = None
        self._placeholder = ""
        self._crops: list[ImageCrop] = []
        self._selected_crop: int | None = None
        self._crop_mode = False
        #: Corners of a rectangle being dragged in crop mode, fractions of the image.
        self._drag: tuple[QPointF, QPointF] | None = None
        #: Crop being resized or moved: index, handle, crop before the drag and the
        #: (unclamped) image point where the drag started.
        self._edit: tuple[int, str, ImageCrop, QPointF] | None = None
        self.setMouseTracking(True)  # hover cursors over crop edges
        #: Magnification relative to the fitted image (1 = whole image visible).
        self._zoom = 1.0
        #: Image point (fractions) shown at the centre of the widget.
        self._center = QPointF(0.5, 0.5)
        #: Last pointer position while panning by dragging.
        self._pan_from: QPointF | None = None
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._update_cursor()

    def image(self) -> QImage | None:
        return self._image

    def set_image(self, image: QImage | None, placeholder: str = "") -> None:
        self._image = image
        self._placeholder = placeholder
        self.reset_zoom()

    def set_crops(self, crops: list[ImageCrop], selected: int | None) -> None:
        self._crops, self._selected_crop = list(crops), selected
        if self._edit is not None and self._edit[0] >= len(self._crops):
            self._edit = None
        self.update()

    def set_crop_mode(self, enabled: bool) -> None:
        self._crop_mode = enabled
        self._drag = None
        self._edit = None
        self._update_cursor()
        self.update()

    def reset_zoom(self) -> None:
        """Fit the whole image into the widget."""
        self._zoom = 1.0
        self._center = QPointF(0.5, 0.5)
        self._update_cursor()
        self.update()

    # --- geometry -------------------------------------------------------

    def _fitted_size(self) -> QSizeF:
        assert self._image is not None
        return QSizeF(self._image.size()).scaled(
            QSizeF(self.size()), Qt.AspectRatioMode.KeepAspectRatio
        )

    def _image_rect(self) -> QRectF:
        """Where the whole image is drawn (may extend beyond the widget when zoomed)."""
        size = self._fitted_size() * self._zoom
        view = QRectF(self.rect()).center()
        return QRectF(
            view.x() - self._center.x() * size.width(),
            view.y() - self._center.y() * size.height(),
            size.width(),
            size.height(),
        )

    def _max_zoom(self) -> float:
        """Zoom limit: about 8 screen pixels per image pixel."""
        assert self._image is not None
        fitted = self._fitted_size()
        per_pixel = fitted.width() / max(1, self._image.width())
        return max(1.0, 8.0 / max(per_pixel, 1e-6))

    def _clamp_center(self) -> None:
        """Keep the image covering the widget: no empty margin on a zoomed-in side."""
        size = self._fitted_size() * self._zoom
        center = []
        for value, extent, view in (
            (self._center.x(), size.width(), self.width()),
            (self._center.y(), size.height(), self.height()),
        ):
            half = view / 2 / max(extent, 1e-6)
            center.append(0.5 if half >= 0.5 else min(1.0 - half, max(half, value)))
        self._center = QPointF(*center)

    def zoom_at(self, factor: float, pos: QPointF) -> None:
        """Multiply the zoom by ``factor``, keeping the image point under ``pos`` in place."""
        if self._image is None:
            return
        zoom = min(self._max_zoom(), max(1.0, self._zoom * factor))
        if zoom == self._zoom:
            return
        before = self._image_rect()
        fx = (pos.x() - before.left()) / before.width()
        fy = (pos.y() - before.top()) / before.height()
        self._zoom = zoom
        size = self._fitted_size() * zoom
        view = QRectF(self.rect()).center()
        self._center = QPointF(
            fx + (view.x() - pos.x()) / size.width(),
            fy + (view.y() - pos.y()) / size.height(),
        )
        self._clamp_center()
        self._update_cursor()
        self.update()

    def pan_by(self, dx: float, dy: float) -> None:
        """Move the image by ``dx``, ``dy`` widget pixels."""
        if self._image is None or self._zoom <= 1.0:
            return
        size = self._fitted_size() * self._zoom
        self._center -= QPointF(dx / size.width(), dy / size.height())
        self._clamp_center()
        self.update()

    def _to_widget(self, target: QRectF, x: float, y: float, w: float, h: float) -> QRectF:
        return QRectF(
            target.left() + x * target.width(),
            target.top() + y * target.height(),
            w * target.width(),
            h * target.height(),
        )

    def _fraction(self, pos: QPointF, clamp: bool = True) -> QPointF:
        """Image point (fractions, clamped to the image unless ``clamp`` is false)
        under widget position ``pos``."""
        target = self._image_rect()
        x = (pos.x() - target.left()) / max(1.0, target.width())
        y = (pos.y() - target.top()) / max(1.0, target.height())
        if not clamp:
            return QPointF(x, y)
        return QPointF(min(1.0, max(0.0, x)), min(1.0, max(0.0, y)))

    # --- crop editing ---------------------------------------------------

    def _hit(self, pos: QPointF) -> tuple[int, str] | None:
        """Crop and handle under ``pos``: a handle is a combination of ``l``, ``r``,
        ``t``, ``b`` for an edge or corner, or ``move`` for the inside of the crop.
        The selected crop wins, then the topmost (last drawn) one; outside crop mode
        a crop's inside counts only if no edge of another crop is under ``pos``."""
        if self._image is None or not self._crops:
            return None
        target = self._image_rect()
        order = list(reversed(range(len(self._crops))))
        if self._selected_crop is not None and 0 <= self._selected_crop < len(self._crops):
            order.remove(self._selected_crop)
            order.insert(0, self._selected_crop)
        inside: int | None = None
        for i in order:
            c = self._crops[i]
            rect = self._to_widget(target, c.left, c.top, c.width, c.height)
            tol = min(_HANDLE_TOLERANCE, rect.width() / 3, rect.height() / 3)
            tol = max(tol, 3.0)
            if not rect.adjusted(-tol, -tol, tol, tol).contains(pos):
                continue
            x, y = pos.x(), pos.y()
            handle = ""
            if abs(x - rect.left()) <= tol:
                handle += "l"
            elif abs(x - rect.right()) <= tol:
                handle += "r"
            if abs(y - rect.top()) <= tol:
                handle += "t"
            elif abs(y - rect.bottom()) <= tol:
                handle += "b"
            if handle:
                return i, handle
            if inside is None and rect.contains(pos):
                inside = i
        if inside is not None and not self._crop_mode:
            return inside, "move"
        return None

    def _edited_crop(self, pos: QPointF) -> ImageCrop:
        """Crop being edited, with the drag so far applied to the handle."""
        assert self._edit is not None
        _, handle, crop, start = self._edit
        point = self._fraction(pos, clamp=False)
        dx, dy = point.x() - start.x(), point.y() - start.y()
        left, top = crop.left, crop.top
        right, bottom = crop.left + crop.width, crop.top + crop.height
        if handle == "move":
            dx = min(1.0 - right, max(-left, dx))
            dy = min(1.0 - bottom, max(-top, dy))
            return ImageCrop(left + dx, top + dy, crop.width, crop.height)
        if "l" in handle:
            left = min(right - _MIN_CROP, max(0.0, left + dx))
        if "r" in handle:
            right = max(left + _MIN_CROP, min(1.0, right + dx))
        if "t" in handle:
            top = min(bottom - _MIN_CROP, max(0.0, top + dy))
        if "b" in handle:
            bottom = max(top + _MIN_CROP, min(1.0, bottom + dy))
        return ImageCrop(left, top, right - left, bottom - top)

    # --- painting -------------------------------------------------------

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        if self._image is None:
            painter.setPen(self.palette().color(self.palette().ColorRole.WindowText))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._placeholder)
            return
        target = self._image_rect()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        # Draw only the visible part of the image (cheap at high zoom).
        visible = target.intersected(QRectF(self.rect()))
        if not visible.isEmpty():
            sx = self._image.width() / target.width()
            sy = self._image.height() / target.height()
            source = QRectF(
                (visible.left() - target.left()) * sx,
                (visible.top() - target.top()) * sy,
                visible.width() * sx,
                visible.height() * sy,
            )
            painter.drawImage(visible, self._image, source)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fm = painter.fontMetrics()
        for i, crop in enumerate(self._crops):
            selected = i == self._selected_crop
            rect = self._to_widget(target, crop.left, crop.top, crop.width, crop.height)
            painter.fillRect(rect, CROP_SELECTED_FILL if selected else CROP_FILL)
            painter.setPen(QPen(CROP_SELECTED_OUTLINE if selected else CROP_OUTLINE, 2))
            painter.drawRect(rect)
            label = str(i + 1)
            # Keep the number visible while the crop's corner is scrolled out of view.
            corner = QPointF(
                min(max(rect.left(), 0.0), max(rect.left(), rect.right() - fm.height())),
                min(max(rect.top(), 0.0), max(rect.top(), rect.bottom() - fm.height())),
            )
            box = QRectF(corner, QSizeF(fm.horizontalAdvance(label) + 8, fm.height() + 2))
            painter.fillRect(box, CROP_SELECTED_OUTLINE if selected else CROP_OUTLINE)
            painter.setPen(QColor(0, 0, 0))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
        if self._drag is not None:
            a, b = self._drag
            rect = self._to_widget(
                target, min(a.x(), b.x()), min(a.y(), b.y()), abs(a.x() - b.x()), abs(a.y() - b.y())
            )
            painter.fillRect(rect, QColor(255, 255, 255, 50))
            painter.setPen(QPen(QColor(255, 255, 255, 230), 1.5, Qt.PenStyle.DashLine))
            painter.drawRect(rect)
        if self._zoom > 1.0:
            text = f"{self._zoom:.1f}×"
            box = QRectF(
                self.width() - fm.horizontalAdvance(text) - 14, 6,
                fm.horizontalAdvance(text) + 8, fm.height() + 2,
            )  # fmt: skip
            painter.fillRect(box, QColor(0, 0, 0, 140))
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._image is not None:
            self._clamp_center()

    # --- input ----------------------------------------------------------

    def _update_cursor(self, hover: QPointF | None = None) -> None:
        hit = self._hit(hover) if hover is not None and self._pan_from is None else None
        if self._edit is not None:
            shape = _HANDLE_CURSORS[self._edit[1]]
        elif hit is not None:
            shape = _HANDLE_CURSORS[hit[1]]
        elif self._crop_mode:
            shape = Qt.CursorShape.CrossCursor
        elif self._pan_from is not None:
            shape = Qt.CursorShape.ClosedHandCursor
        elif self._zoom > 1.0:
            shape = Qt.CursorShape.OpenHandCursor
        else:
            shape = Qt.CursorShape.ArrowCursor
        self.setCursor(shape)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._image is None:
            return
        trackpad = (
            event.phase() != Qt.ScrollPhase.NoScrollPhase
            or event.pointingDevice().type() == QInputDevice.DeviceType.TouchPad
        )
        zoom_modifier = event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
        )
        if trackpad and not zoom_modifier:
            # Two-finger scroll pans (pinch arrives as a native gesture).
            delta = event.pixelDelta()
            if delta.isNull():
                delta = event.angleDelta() / 8
            self.pan_by(delta.x(), delta.y())
        else:
            # Mouse wheel, or Ctrl+scroll (pinch on Windows/Linux touchpads), zooms.
            steps = event.angleDelta().y() / 120
            if not steps:
                steps = event.pixelDelta().y() / 50
            self.zoom_at(1.25**steps, event.position())
        event.accept()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.NativeGesture:
            gesture = event.gestureType()  # type: ignore[attr-defined]
            if gesture == Qt.NativeGestureType.ZoomNativeGesture:
                self.zoom_at(1.0 + event.value(), event.position())  # type: ignore[attr-defined]
                return True
            if gesture == Qt.NativeGestureType.SmartZoomNativeGesture:
                if self._zoom > 1.0:
                    self.reset_zoom()
                else:
                    self.zoom_at(2.5, event.position())  # type: ignore[attr-defined]
                return True
        return super().event(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if (
            not self._crop_mode
            and event.button() == Qt.MouseButton.LeftButton
            and self._hit(event.position()) is None
        ):
            self.reset_zoom()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._image is None or event.button() != Qt.MouseButton.LeftButton:
            return
        hit = self._hit(event.position())
        if hit is not None:
            index, handle = hit
            start = self._fraction(event.position(), clamp=False)
            self._edit = (index, handle, self._crops[index], start)
            self._update_cursor()
            self.cropClicked.emit(index)
        elif self._crop_mode:
            point = self._fraction(event.position())
            self._drag = (point, point)
            self.update()
        elif self._zoom > 1.0:
            self._pan_from = event.position()
            self._update_cursor()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._edit is not None:
            self._crops[self._edit[0]] = self._edited_crop(event.position())
            self.update()
        elif self._drag is not None:
            self._drag = (self._drag[0], self._fraction(event.position()))
            self.update()
        elif self._pan_from is not None:
            delta = event.position() - self._pan_from
            self._pan_from = event.position()
            self.pan_by(delta.x(), delta.y())
        else:
            self._update_cursor(event.position())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._edit is not None:
            index, _, original, _ = self._edit
            crop = self._edited_crop(event.position())
            self._edit = None
            self._crops[index] = crop
            self._update_cursor(event.position())
            self.update()
            if crop != original:
                self.cropEdited.emit(index, crop)
        elif self._drag is not None:
            start = self._drag[0]
            end = self._fraction(event.position())
            self._drag = None
            self.update()
            self.rectSelected.emit(start.x(), start.y(), end.x(), end.y())
        elif self._pan_from is not None:
            self._pan_from = None
            self._update_cursor()


class _AudioPlayer(QWidget):
    """Mel spectrogram, play/pause button, seek slider and position of an audio file."""

    #: The spectrogram of the current recording was computed (or failed).
    spectrogramReady = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._player = QMediaPlayer(self)
        self._output = QAudioOutput(self)
        self._player.setAudioOutput(self._output)
        self._seeking = False
        self._generation = 0
        self._tasks: set[_SpectrogramTask] = set()
        self._spectrogram_image: QImage | None = None
        self._duration = 0.0
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

    def spectrogram(self) -> SpectrogramView:
        return self._spectrogram

    def spectrogram_image(self) -> QImage | None:
        """Colour spectrogram of the whole recording, once computed."""
        return self._spectrogram_image

    def duration(self) -> float:
        """Length of the recording in seconds (0 until known)."""
        return self._duration

    def set_source(self, media: MediaFile) -> None:
        self._player.stop()
        self._spectrogram_image = None
        self._duration = 0.0
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
            self._spectrogram_image = result  # type: ignore[assignment]
            self._duration = spec.duration  # type: ignore[attr-defined]
        self.spectrogramReady.emit()

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
    """One media file: image or audio player, with its crops, date/EXIF metadata and
    detected species."""

    #: User asked for the previous (-1) or next (+1) file in the list.
    navigateRequested = Signal(int)
    #: User added or deleted crops: the file and all its crops (list[Crop]).
    cropsChanged = Signal(object, object)

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
        #: Crops of the previewed file, in the order they were made.
        self._crops: list[Crop] = []
        #: Species detected in the whole file and in its analysed crops.
        self._predictions: list[SpeciesPrediction] | None = None
        self._crop_detections: dict[Crop, list[SpeciesPrediction] | None] = {}
        #: Object detector for "Detect boxes"; its model loads on first use.
        self._box_detector = ObjectDetector()
        self._box_tasks: set[_BoxesTask] = set()
        #: Generation of the running box detection, or None.
        self._boxes_running: int | None = None
        #: Result of the last box detection, shown in the crop hint.
        self._boxes_message = ""

        self._image = _ImageView()
        self._image.rectSelected.connect(self._on_image_rect)
        self._image.cropClicked.connect(lambda row: self._crop_list.setCurrentRow(row))
        self._image.cropEdited.connect(self._on_crop_edited)
        self._audio = _AudioPlayer()
        self._audio.spectrogram().rangeSelected.connect(self._on_audio_range)
        self._audio.spectrogramReady.connect(self._refresh_crop_icons)
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
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self).activated.connect(self._on_escape)
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

        self._crop_bar = self._build_crop_bar()

        layout = QVBoxLayout(self)
        layout.addWidget(self._crop_bar)
        layout.addWidget(splitter, 1)

    def _build_crop_bar(self) -> QWidget:
        """Crop strip at the top of the window, with buttons to add and delete crops."""
        self._add_crop = QPushButton("Add crop")
        self._add_crop.setCheckable(True)
        self._add_crop.setShortcut(QKeySequence("C"))
        self._add_crop.toggled.connect(self._set_crop_mode)
        self._delete_crop = QPushButton("Delete crop")
        self._delete_crop.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        self._delete_crop.setToolTip("Delete the selected crop (Delete)")
        self._delete_crop.setEnabled(False)
        self._delete_crop.clicked.connect(self._delete_selected_crop)
        self._detect_boxes = QPushButton("Detect boxes")
        self._detect_boxes.setToolTip(
            "Find objects in the photo with LibreYOLO and add their bounding boxes as crops"
        )
        self._detect_boxes.clicked.connect(self._start_box_detection)
        self._crop_hint = QLabel()
        self._crop_hint.setStyleSheet("color: palette(placeholder-text)")

        self._crop_list = QListWidget()
        self._crop_list.setViewMode(QListView.ViewMode.IconMode)
        self._crop_list.setFlow(QListView.Flow.LeftToRight)
        self._crop_list.setWrapping(False)
        self._crop_list.setMovement(QListView.Movement.Static)
        self._crop_list.setIconSize(_CROP_ICON)
        self._crop_list.setSpacing(4)
        self._crop_list.setWordWrap(False)
        self._crop_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._crop_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._crop_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._crop_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        fm = self.fontMetrics()
        grid = QSize(
            max(_CROP_ICON.width(), fm.horizontalAdvance("Eurasian Blackbird · 00%")) + 16,
            _CROP_ICON.height() + 2 * fm.height() + 20,
        )
        self._crop_list.setGridSize(grid)
        scrollbar = self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        self._crop_list.setFixedHeight(grid.height() + scrollbar + 6)
        self._crop_list.currentRowChanged.connect(self._on_crop_selected)
        self._crop_list.itemActivated.connect(self._on_crop_activated)
        delete = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self._crop_list)
        delete.activated.connect(self._delete_selected_crop)
        backspace = QShortcut(QKeySequence(Qt.Key.Key_Backspace), self._crop_list)
        backspace.activated.connect(self._delete_selected_crop)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(QLabel("<b>Crops</b>"))
        header.addWidget(self._add_crop)
        header.addWidget(self._delete_crop)
        header.addWidget(self._detect_boxes)
        header.addWidget(self._crop_hint, 1)
        bar = QWidget()
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(header)
        layout.addWidget(self._crop_list)
        return bar

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
        self,
        media: MediaFile,
        tz: tzinfo,
        predictions: list[SpeciesPrediction] | None,
        crops: list[Crop] | None = None,
        crop_detections: dict[Crop, list[SpeciesPrediction] | None] | None = None,
    ) -> None:
        """Preview ``media`` with its ``crops`` and the species detected in the file
        and in its crops; capture times are shown in ``tz``."""
        self._media = media
        self._tz = tz
        self._generation += 1
        self.setWindowTitle(f"Preview – {media.name}")

        self._audio.stop()
        is_audio = media.kind is MediaKind.AUDIO
        self._image.setVisible(media.kind.is_image)
        self._audio.setVisible(is_audio)
        self._unsupported.setVisible(not (is_audio or media.kind.is_image))
        self._crop_bar.setVisible(is_audio or media.kind.is_image)
        self._add_crop.setChecked(False)
        self._detect_boxes.setVisible(media.kind.is_image)
        self._boxes_running = None
        self._boxes_message = ""
        self._update_detect_boxes()
        self._crops = [
            c for c in crops or [] if isinstance(c, AudioCrop if is_audio else ImageCrop)
        ]
        self._add_crop.setToolTip(
            "Drag over the spectrogram to crop a time range (C)"
            if is_audio
            else "Drag over the image to crop a region (C)"
        )

        self._metadata.clear()
        self._metadata_loading = None
        self._add_datetime(media, tz)
        self.set_predictions(predictions, crop_detections)

        if is_audio:
            self._audio.set_source(media)
        elif media.kind.is_image:
            self._image.set_image(None, "Loading…")
        self._refresh_crops()
        if media.kind.is_image:
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

    def set_predictions(
        self,
        predictions: list[SpeciesPrediction] | None,
        crop_detections: dict[Crop, list[SpeciesPrediction] | None] | None = None,
    ) -> None:
        """Show the top species detected in the previewed file and in each of its crops."""
        self._predictions = predictions
        self._crop_detections = dict(crop_detections or {})
        self._show_species()
        self._update_crop_labels()

    def _show_species(self) -> None:
        self._species.clear()
        if not self._crops:
            self._add_species(self._predictions)
            return
        self._add_species_header("Whole file")
        self._add_species(self._predictions)
        for i, crop in enumerate(self._crops):
            self._add_species_header(f"Crop {i + 1} · {crop.label()}")
            self._add_species(self._crop_detections.get(crop))

    def _add_species_header(self, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)  # not selectable, but not greyed out
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        self._species.addItem(item)

    def _add_species(self, predictions: list[SpeciesPrediction] | None) -> None:
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
            self._refresh_crop_icons()

    # --- crops ----------------------------------------------------------

    def _on_escape(self) -> None:
        if self._add_crop.isChecked():
            self._add_crop.setChecked(False)
        else:
            self.close()

    def _set_crop_mode(self, enabled: bool) -> None:
        self._image.set_crop_mode(enabled)
        self._audio.spectrogram().set_crop_mode(enabled)
        self._update_crop_hint()

    def _on_image_rect(self, x0: float, y0: float, x1: float, y1: float) -> None:
        crop = make_image_crop(x0, y0, x1, y1)
        if crop is not None:
            self._add(crop)

    def _on_audio_range(self, t0: float, t1: float) -> None:
        crop = make_audio_crop(t0, t1, self._audio.duration())
        if crop is not None:
            self._add(crop)

    def _add(self, crop: Crop) -> None:
        self._boxes_message = ""
        self._crops.append(crop)
        self._refresh_crops(select=len(self._crops) - 1)
        self._emit_crops()

    def _on_crop_edited(self, index: int, crop: ImageCrop) -> None:
        """A crop was resized or moved on the image; its species are analysed anew."""
        if not 0 <= index < len(self._crops):
            return
        self._boxes_message = ""
        self._crops[index] = crop
        self._refresh_crops(select=index)
        self._emit_crops()

    def _start_box_detection(self) -> None:
        if (
            self._media is None
            or not self._media.kind.is_image
            or self._boxes_running is not None
        ):
            return
        self._boxes_running = self._generation
        self._boxes_message = ""
        self._update_detect_boxes()
        self._update_crop_hint()
        task = _BoxesTask(self._box_detector, self._media, self._generation)
        task.signals.done.connect(self._on_boxes)
        task.signals.done.connect(lambda *_: self._box_tasks.discard(task))
        self._box_tasks.add(task)  # keep the Python object (and its signals) alive
        QThreadPool.globalInstance().start(task)

    def _on_boxes(self, generation: int, result: object) -> None:
        if generation != self._generation:
            return  # another file is shown now
        self._boxes_running = None
        self._update_detect_boxes()
        if isinstance(result, str):
            self._boxes_message = result
            self._update_crop_hint()
            return
        boxes: list[DetectedBox] = result  # type: ignore[assignment]
        existing = set(self._crops)
        new = [b for b in boxes if b.crop not in existing]
        if not new:
            self._boxes_message = "No objects detected" if not boxes else "No new boxes"
            self._update_crop_hint()
            return
        first = len(self._crops)
        self._crops += [b.crop for b in new]
        found = ", ".join(f"{b.label} {b.score:.0%}" for b in new)
        self._boxes_message = f"Added {len(new)} box{'es' if len(new) != 1 else ''}: {found}"
        self._refresh_crops(select=first)
        self._emit_crops()

    def _update_detect_boxes(self) -> None:
        running = self._boxes_running is not None
        self._detect_boxes.setEnabled(not running)
        self._detect_boxes.setText("Detecting…" if running else "Detect boxes")

    def _delete_selected_crop(self) -> None:
        row = self._crop_list.currentRow()
        if not 0 <= row < len(self._crops):
            return
        del self._crops[row]
        self._boxes_message = ""
        self._refresh_crops(select=min(row, len(self._crops) - 1) if self._crops else None)
        self._emit_crops()

    def _emit_crops(self) -> None:
        if self._media is not None:
            self.cropsChanged.emit(self._media, list(self._crops))

    def _selected_crop(self) -> int | None:
        row = self._crop_list.currentRow()
        return row if 0 <= row < len(self._crops) else None

    def _refresh_crops(self, select: int | None = None) -> None:
        """Rebuild the crop strip and the overlays from ``self._crops``."""
        self._crop_list.blockSignals(True)
        self._crop_list.clear()
        for crop in self._crops:
            item = QListWidgetItem()
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            # A static icon view places items once, by their size when added; the
            # label and thumbnail come later, so give each item its full cell now.
            item.setSizeHint(self._crop_list.gridSize())
            self._crop_list.addItem(item)
        if select is not None:
            self._crop_list.setCurrentRow(select)
        self._crop_list.blockSignals(False)
        self._update_crop_labels()
        self._show_species()
        self._refresh_crop_icons()
        self._update_overlays()
        self._update_crop_hint()
        if select is not None:
            # clear() reset the scroll position: bring the selected crop into view
            # once the view has laid out the new items (it delays that).
            QTimer.singleShot(0, self._scroll_to_selected_crop)

    def _scroll_to_selected_crop(self) -> None:
        item = self._crop_list.currentItem()
        if item is not None:
            self._crop_list.scrollToItem(item)

    def _update_crop_labels(self) -> None:
        """Number, size or time range and top detected species of each crop in the strip."""
        for i, crop in enumerate(self._crops):
            item = self._crop_list.item(i)
            if item is None:
                continue
            predictions = self._crop_detections.get(crop)
            top = detected(predictions)[:1]
            if top:
                species = f"{top[0].display_name} · {top[0].score:.0%}"
            elif crop in self._crop_detections and predictions is not None:
                species = "No species"
            else:
                species = "Not analysed"
            item.setText(f"{i + 1} · {crop.label()}\n{species}")
            tip = [species if top else f"Species: {species.lower()}"]
            if isinstance(crop, AudioCrop):
                tip.append("Double-click to play this range")
            item.setToolTip("\n".join(tip))

    def _refresh_crop_icons(self) -> None:
        """Thumbnails of the crops, cut from the preview image or the spectrogram."""
        for i, crop in enumerate(self._crops):
            item = self._crop_list.item(i)
            if item is None:
                continue
            pixmap = self._crop_pixmap(crop)
            item.setIcon(QIcon(pixmap) if pixmap is not None else QIcon())

    def _crop_pixmap(self, crop: Crop) -> QPixmap | None:
        if isinstance(crop, ImageCrop):
            source = self._image.image()
            if source is None:
                return None
            w, h = source.width(), source.height()
            rect = QRect(
                round(crop.left * w), round(crop.top * h),
                max(1, round(crop.width * w)), max(1, round(crop.height * h)),
            )  # fmt: skip
        else:
            source = self._audio.spectrogram_image()
            duration = self._audio.duration()
            if source is None or duration <= 0:
                return None
            w = source.width()
            x0 = round(crop.start / duration * w)
            rect = QRect(x0, 0, max(1, round(crop.end / duration * w) - x0), source.height())
        ratio = self.devicePixelRatioF()
        scaled = QPixmap.fromImage(source.copy(rect)).scaled(
            _CROP_ICON * ratio,
            Qt.AspectRatioMode.KeepAspectRatio
            if isinstance(crop, ImageCrop)
            else Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(ratio)
        return scaled

    def _update_overlays(self) -> None:
        selected = self._selected_crop()
        self._image.set_crops([c for c in self._crops if isinstance(c, ImageCrop)], selected)
        self._audio.spectrogram().set_crops(
            [(c.start, c.end) for c in self._crops if isinstance(c, AudioCrop)], selected
        )
        self._delete_crop.setEnabled(selected is not None)

    def _update_crop_hint(self) -> None:
        if self._add_crop.isChecked():
            is_audio = self._media is not None and self._media.kind is MediaKind.AUDIO
            target = "spectrogram" if is_audio else "image"
            text = f"Drag over the {target} to add a crop · Esc to finish"
        elif self._boxes_running is not None:
            text = "Detecting objects… (the model loads on first use)"
        elif self._boxes_message:
            text = self._boxes_message
        elif not self._crops:
            text = "No crops yet"
        else:
            text = f"{len(self._crops)} crop{'s' if len(self._crops) != 1 else ''}"
        self._crop_hint.setText(text)

    def _on_crop_selected(self, _row: int) -> None:
        self._update_overlays()

    def _on_crop_activated(self, item: QListWidgetItem) -> None:
        crop = self._crops[self._crop_list.row(item)]
        if isinstance(crop, AudioCrop) and self._audio.isVisible():
            self._audio.play_from(crop.start)

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
