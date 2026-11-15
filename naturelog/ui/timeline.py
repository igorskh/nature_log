"""Draggable timeline for scrubbing through a GPS track."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QHelpEvent,
    QIcon,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from naturelog.gps import Track, tracks_span

_MARGIN = 24
_ICON_TOP = 4
_ICON_SIZE = 26
_ICON_GAP = 4  # minimum horizontal space between two icons before they are grouped
_BAR_TOP = _ICON_TOP + _ICON_SIZE + 10
_BAR_HEIGHT = 14
_TOOLTIP_ROWS = 12
_TICK_STEPS = (
    1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800,
    3600, 7200, 10800, 21600, 43200, 86400,
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class TimelineMarker:
    """A media file shown on the timeline at its capture time."""

    when: datetime
    icon: QIcon
    label: str
    selected: bool = False
    #: Opaque identifier passed back in :attr:`TimelineWidget.markersClicked`.
    key: object = None


@dataclass(slots=True)
class _Cluster:
    """Markers drawn as one icon because they are too close to tell apart."""

    x: float
    markers: list[TimelineMarker] = field(default_factory=list)
    xs: list[float] = field(default_factory=list)

    def icon_rect(self) -> QRectF:
        return QRectF(self.x - _ICON_SIZE / 2, _ICON_TOP, _ICON_SIZE, _ICON_SIZE)


class TimelineWidget(QWidget):
    """Time axis spanning a track, with a handle the user drags to pick a time.

    The bar is filled where the track has fixes and left empty in the gaps
    between segments. Media markers are drawn as icons above the bar at their
    capture time; icons that would overlap are grouped into one icon with a
    count badge. Clicking an icon moves the handle to its time and
    emits :attr:`markersClicked`. Tick labels are shown in the display timezone
    (UTC unless :meth:`set_timezone` is called); emitted times are always UTC.
    """

    #: Emitted with the selected timezone-aware ``datetime``.
    timeChanged = Signal(object)
    #: Emitted with the ``key`` of every marker in the clicked icon (group).
    markersClicked = Signal(list)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tracks: list[Track] = []
        #: Earliest start and latest end over all tracks.
        self._span: tuple[datetime, datetime] | None = None
        self._current: datetime | None = None
        self._tz: tzinfo = timezone.utc
        self._markers: list[TimelineMarker] = []
        self._clusters: list[_Cluster] | None = None  # cached layout
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setEnabled(False)

    def sizeHint(self) -> QSize:
        return QSize(600, _BAR_TOP + _BAR_HEIGHT + 30)

    def set_tracks(self, tracks: Sequence[Track]) -> None:
        """Span the timeline over all tracks; gaps between and within them stay empty."""
        self._tracks = list(tracks)
        self._span = tracks_span(self._tracks)
        self._clusters = None
        if self._span is None:
            self._current = None
            self.setEnabled(False)
            self.unsetCursor()
            self.update()
            return
        self.setEnabled(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._current = None
        self.set_time(self._span[0])

    def set_timezone(self, tz: tzinfo) -> None:
        """Set the timezone tick labels are shown in."""
        self._tz = tz
        self.update()

    def set_markers(self, markers: list[TimelineMarker]) -> None:
        """Show media markers; those outside the track's time range are not drawn."""
        self._markers = sorted(markers, key=lambda m: m.when)
        self._clusters = None
        self.update()

    def current_time(self) -> datetime | None:
        return self._current

    def set_time(self, when: datetime) -> None:
        if self._span is None:
            return
        when = min(max(when, self._span[0]), self._span[1])
        if when != self._current:
            self._current = when
            self.update()
            self.timeChanged.emit(when)

    # --- geometry -------------------------------------------------------

    def _bar(self) -> QRectF:
        return QRectF(_MARGIN, _BAR_TOP, max(1, self.width() - 2 * _MARGIN), _BAR_HEIGHT)

    def _x(self, when: datetime) -> float:
        bar = self._bar()
        span = (self._span[1] - self._span[0]).total_seconds()
        if span <= 0:
            return bar.center().x()
        return bar.left() + (when - self._span[0]).total_seconds() / span * bar.width()

    def _time_at(self, x: float) -> datetime:
        bar = self._bar()
        fraction = min(max((x - bar.left()) / bar.width(), 0.0), 1.0)
        return self._span[0] + (self._span[1] - self._span[0]) * fraction

    def resizeEvent(self, event) -> None:
        self._clusters = None
        super().resizeEvent(event)

    def _layout_clusters(self) -> list[_Cluster]:
        """Group markers whose icons would overlap at the current width."""
        if self._clusters is not None:
            return self._clusters
        clusters: list[_Cluster] = []
        if self._span is not None:
            for marker in self._markers:
                if not self._span[0] <= marker.when <= self._span[1]:
                    continue  # never place media beyond the recorded track
                x = self._x(marker.when)
                clusters.append(_Cluster(x, [marker], [x]))
        # Merge neighbours until every pair of icons has room; a merged group
        # sits at the mean time of its members.
        spacing = _ICON_SIZE + _ICON_GAP
        merged = True
        while merged and len(clusters) > 1:
            merged = False
            result = [clusters[0]]
            for cluster in clusters[1:]:
                last = result[-1]
                if cluster.x - last.x < spacing:
                    last.markers += cluster.markers
                    last.xs += cluster.xs
                    last.x = sum(last.xs) / len(last.xs)
                    merged = True
                else:
                    result.append(cluster)
            clusters = result
        # Keep icons inside the widget.
        half = _ICON_SIZE / 2
        for cluster in clusters:
            cluster.x = min(max(cluster.x, half + 1), self.width() - half - 1)
        self._clusters = clusters
        return clusters

    def _cluster_at(self, pos: QPointF) -> _Cluster | None:
        for cluster in self._layout_clusters():
            if cluster.icon_rect().adjusted(-1, -1, 1, 1).contains(pos):
                return cluster
        return None

    # --- interaction ----------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._span is not None and event.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
            cluster = self._cluster_at(event.position())
            if cluster is not None:
                self.set_time(cluster.markers[0].when)
                self.markersClicked.emit([m.key for m in cluster.markers])
            else:
                self.set_time(self._time_at(event.position().x()))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._span is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.set_time(self._time_at(event.position().x()))

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            assert isinstance(event, QHelpEvent)
            cluster = self._cluster_at(QPointF(event.pos()))
            if cluster is None:
                QToolTip.hideText()
                event.ignore()
            else:
                labels = [m.label for m in cluster.markers]
                if len(labels) > _TOOLTIP_ROWS:
                    labels = labels[:_TOOLTIP_ROWS] + [f"… and {len(labels) - _TOOLTIP_ROWS} more"]
                header = f"{len(cluster.markers)} files\n" if len(cluster.markers) > 1 else ""
                QToolTip.showText(event.globalPos(), header + "\n".join(labels), self)
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._current is None:
            return super().keyPressEvent(event)
        seconds = 60 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        match event.key():
            case Qt.Key.Key_Left:
                self.set_time(self._current - timedelta(seconds=seconds))
            case Qt.Key.Key_Right:
                self.set_time(self._current + timedelta(seconds=seconds))
            case Qt.Key.Key_Home:
                self.set_time(self._span[0])
            case Qt.Key.Key_End:
                self.set_time(self._span[1])
            case _:
                super().keyPressEvent(event)

    # --- painting -------------------------------------------------------

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        text = palette.color(QPalette.ColorRole.WindowText)
        muted = QColor(text)
        muted.setAlpha(110)
        bar = self._bar()

        painter.setPen(QPen(muted, 1))
        painter.setBrush(palette.color(QPalette.ColorRole.Base))
        painter.drawRoundedRect(bar, 3, 3)

        if self._span is None:
            painter.setPen(muted)
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "Load a GPS log to see its timeline"
            )
            return

        # Recorded coverage; gaps between segments stay empty.
        accent = palette.color(QPalette.ColorRole.Highlight)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        for segment in (s for t in self._tracks for s in t.segments):
            left = self._x(segment.start)
            width = max(2.0, self._x(segment.end) - left)
            painter.drawRect(QRectF(left, bar.top() + 1, width, bar.height() - 2))

        self._paint_ticks(painter, bar, text, muted)
        self._paint_markers(painter, bar, text, accent)

        # Handle.
        x = self._x(self._current)
        painter.setPen(QPen(text, 2))
        painter.drawLine(QPointF(x, bar.top() - 6), QPointF(x, bar.bottom() + 6))
        painter.setPen(QPen(palette.color(QPalette.ColorRole.Window), 2))
        painter.setBrush(text)
        painter.drawEllipse(QPointF(x, bar.center().y()), 7, 7)

    def _paint_markers(self, painter: QPainter, bar: QRectF, text: QColor, accent: QColor) -> None:
        palette = self.palette()
        window = palette.color(QPalette.ColorRole.Window)
        for cluster in self._layout_clusters():
            selected = any(m.selected for m in cluster.markers)
            line = QColor(accent if selected else text)
            line.setAlpha(255 if selected else 140)

            # Exact capture times as ticks on the bar, joined to the icon.
            painter.setPen(QPen(line, 1))
            icon_bottom = QPointF(cluster.x, _ICON_TOP + _ICON_SIZE)
            for x in cluster.xs:
                painter.drawLine(QPointF(x, bar.top() - 3), QPointF(x, bar.top() + 4))
                painter.drawLine(icon_bottom, QPointF(x, bar.top() - 3))

            rect = cluster.icon_rect()
            path = QPainterPath()
            path.addRoundedRect(rect, 4, 4)
            painter.save()
            painter.setClipPath(path)
            painter.fillRect(rect, palette.color(QPalette.ColorRole.Base))
            pixmap = cluster.markers[0].icon.pixmap(
                QSize(_ICON_SIZE, _ICON_SIZE) * 2, self.devicePixelRatioF()
            )
            if not pixmap.isNull():
                # Fill the square, cropping the longer side (like a thumbnail grid).
                size = pixmap.deviceIndependentSize()
                scale = max(rect.width() / size.width(), rect.height() / size.height())
                w, h = size.width() * scale, size.height() * scale
                painter.drawPixmap(
                    QRectF(rect.center().x() - w / 2, rect.center().y() - h / 2, w, h),
                    pixmap,
                    QRectF(pixmap.rect()),
                )
            painter.restore()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(line, 2 if selected else 1))
            painter.drawRoundedRect(rect, 4, 4)

            if len(cluster.markers) > 1:
                self._paint_badge(painter, rect, len(cluster.markers), accent, window)

    def _paint_badge(
        self, painter: QPainter, rect: QRectF, count: int, fill: QColor, outline: QColor
    ) -> None:
        label = str(count) if count < 100 else "99+"
        font = QFont(self.font())
        font.setPointSizeF(max(6.0, font.pointSizeF() * 0.7))
        font.setBold(True)
        painter.save()
        painter.setFont(font)
        height = painter.fontMetrics().height()
        width = max(height, painter.fontMetrics().horizontalAdvance(label) + 6)
        badge = QRectF(rect.right() - width + 5, rect.top() - 3, width, height)
        painter.setPen(QPen(outline, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(badge, height / 2, height / 2)
        painter.setPen(self.palette().color(QPalette.ColorRole.HighlightedText))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, label)
        painter.restore()

    def _paint_ticks(self, painter: QPainter, bar: QRectF, text: QColor, muted: QColor) -> None:
        start, end = self._span[0], self._span[1]
        span = (end - start).total_seconds()
        if span <= 0:
            return
        multi_day = start.astimezone(self._tz).date() != end.astimezone(self._tz).date()
        label_width = 110 if multi_day else 80
        step = next(
            (s for s in _TICK_STEPS if span / s * label_width <= bar.width()),
            86400 * max(1, round(span / 86400 * label_width / bar.width())),
        )
        if multi_day:
            fmt = "%d %b %H:%M"
        elif step < 60:
            fmt = "%H:%M:%S"
        else:
            fmt = "%H:%M"

        # Align ticks to round wall-clock times in the display timezone.
        offset = int(start.astimezone(self._tz).utcoffset().total_seconds())
        first = int((start.timestamp() + offset) // step + 1) * step - offset
        font = painter.font()
        font.setPointSizeF(font.pointSizeF() * 0.85)
        painter.setFont(font)
        t = first
        while t <= end.timestamp():
            x = bar.left() + (t - start.timestamp()) / span * bar.width()
            painter.setPen(QPen(muted, 1))
            painter.drawLine(QPointF(x, bar.bottom() + 2), QPointF(x, bar.bottom() + 7))
            painter.setPen(text)
            label = datetime.fromtimestamp(t, self._tz).strftime(fmt)
            painter.drawText(
                QRectF(x - label_width / 2, bar.bottom() + 9, label_width, 16),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                label,
            )
            t += step
