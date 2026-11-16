"""Mel spectrogram display with frequency/time axes and a playback position line."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from naturelog.media.spectrogram import MelSpectrogram, hz_to_mel

#: Magma-like colour map anchors, low → high.
_ANCHORS = np.array(
    [
        (0, 0, 4), (28, 16, 68), (79, 18, 123), (129, 37, 129), (181, 54, 122),
        (229, 80, 100), (251, 135, 97), (254, 194, 135), (252, 253, 191),
    ],
    dtype=np.float64,
)
_POSITIONS = np.linspace(0.0, 1.0, len(_ANCHORS))
_LUT = (
    np.uint32(0xFF000000)
    | np.stack(
        [np.interp(np.linspace(0, 1, 256), _POSITIONS, _ANCHORS[:, c]) for c in range(3)], axis=1
    )
    .round()
    .astype(np.uint32)
    @ np.array([1 << 16, 1 << 8, 1], dtype=np.uint32)
)
#: Time tick steps, seconds.
_TIME_STEPS = (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)
#: Frequency tick candidates, Hz, labelled bottom-up while they don't overlap.
_FREQ_TICKS = (0, 250, 500, 1000, 2000, 3000, 4000, 5000, 6000, 8000, 10000, 12000, 15000, 20000)
_TICK = 4
_MIN_TIME_GAP = 70  # px between time labels
_MIN_FREQ_GAP = 18  # px between frequency labels


def spectrogram_image(spec: MelSpectrogram) -> QImage:
    """Colour image of ``spec``: high frequencies on top, one pixel per mel band and column."""
    db = spec.db
    low, high = float(db.min()), float(db.max())
    scaled = (db - low) / (high - low) if high > low else np.zeros_like(db)
    indices = np.clip((scaled * 255).astype(np.int32), 0, 255)
    pixels = np.ascontiguousarray(_LUT[indices[::-1]])
    height, width = pixels.shape
    image = QImage(pixels.data, width, height, width * 4, QImage.Format.Format_RGB32)
    return image.copy()  # own the pixels; ``pixels`` is freed after return


def _format_time(seconds: float, step: float) -> str:
    if step < 1:
        return f"{seconds:.1f} s"
    total = int(round(seconds))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{sec:02d}" if hours else f"{minutes}:{sec:02d}"


def _format_khz(hz: float) -> str:
    return f"{hz / 1000:g}"


class SpectrogramView(QWidget):
    """Paints a :class:`MelSpectrogram`; click or drag to seek."""

    #: User clicked/dragged to this position, milliseconds.
    seekRequested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._spec: MelSpectrogram | None = None
        self._image: QImage | None = None
        self._message = ""
        self._position_ms = 0
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # --- public API -----------------------------------------------------

    def set_message(self, text: str) -> None:
        """Show ``text`` instead of a spectrogram (loading, errors)."""
        self._spec = self._image = None
        self._message = text
        self.update()

    def set_spectrogram(self, spec: MelSpectrogram, image: QImage) -> None:
        self._spec, self._image, self._message = spec, image, ""
        self.update()

    def set_position(self, ms: int) -> None:
        if ms != self._position_ms:
            self._position_ms = ms
            self.update()

    # --- geometry -------------------------------------------------------

    def _plot_rect(self) -> QRect:
        fm = self.fontMetrics()
        left = max(fm.horizontalAdvance("00.0"), fm.horizontalAdvance("kHz")) + _TICK + 8
        bottom = fm.height() + _TICK + 4
        top = fm.height() + 2  # room for the "kHz" unit
        right = fm.horizontalAdvance("0:00") // 2 + 2
        return self.rect().adjusted(left, top, -right, -bottom)

    def _freq_y(self, plot: QRect, hz: float) -> float:
        assert self._spec is not None
        lo, hi = hz_to_mel(self._spec.fmin), hz_to_mel(self._spec.fmax)
        fraction = (float(hz_to_mel(hz)) - lo) / (hi - lo)
        return plot.bottom() + 1 - fraction * plot.height()

    # --- painting -------------------------------------------------------

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        palette = self.palette()
        text_pen = QPen(palette.color(palette.ColorRole.WindowText))
        plot = self._plot_rect()
        if self._spec is None or self._image is None or plot.width() < 10 or plot.height() < 10:
            painter.setPen(text_pen)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)
            return

        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(QRectF(plot), self._image)
        painter.setPen(text_pen)
        painter.drawRect(plot.adjusted(0, 0, -1, -1))
        self._draw_frequency_axis(painter, plot)
        self._draw_time_axis(painter, plot)

        duration = self._spec.duration
        if duration > 0:
            x = plot.left() + min(1.0, self._position_ms / 1000 / duration) * plot.width()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(255, 255, 255, 230), 1.5))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom() + 1))

    def _draw_frequency_axis(self, painter: QPainter, plot: QRect) -> None:
        assert self._spec is not None
        fm = painter.fontMetrics()
        fmax = self._spec.fmax
        last_y: float | None = None
        for hz in _FREQ_TICKS:
            if hz > fmax:
                break
            y = self._freq_y(plot, hz)
            if last_y is not None and last_y - y < _MIN_FREQ_GAP:
                continue
            last_y = y
            painter.drawLine(QPointF(plot.left() - _TICK, y), QPointF(plot.left(), y))
            label = _format_khz(hz)
            rect = QRectF(0, y - fm.height() / 2, plot.left() - _TICK - 3, fm.height())
            painter.drawText(rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)
        # Unit above the frequency labels.
        painter.drawText(
            QRectF(0, 0, plot.left() - _TICK - 3, fm.height()),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
            "kHz",
        )

    def _draw_time_axis(self, painter: QPainter, plot: QRect) -> None:
        assert self._spec is not None
        fm = painter.fontMetrics()
        duration = self._spec.duration
        if duration <= 0:
            return
        px_per_s = plot.width() / duration
        step = next((s for s in _TIME_STEPS if s * px_per_s >= _MIN_TIME_GAP), _TIME_STEPS[-1])
        y = plot.bottom() + 1
        count = int(duration / step + 1e-9)
        for i in range(count + 1):
            t = i * step
            x = plot.left() + t * px_per_s
            painter.drawLine(QPointF(x, y), QPointF(x, y + _TICK))
            label = _format_time(t, step)
            width = fm.horizontalAdvance(label)
            rect = QRectF(x - width / 2 - 2, y + _TICK + 1, width + 4, fm.height())
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

    # --- seeking --------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._seek(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._seek(event)

    def _seek(self, event: QMouseEvent) -> None:
        if self._spec is None or event.button() not in (
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
        ):
            return
        plot = self._plot_rect()
        fraction = (event.position().x() - plot.left()) / max(1, plot.width())
        ms = int(min(1.0, max(0.0, fraction)) * self._spec.duration * 1000)
        self.set_position(ms)
        self.seekRequested.emit(ms)
