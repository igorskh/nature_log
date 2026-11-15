"""Sidebar panel listing the loaded GPS tracks."""

from __future__ import annotations

from datetime import UTC, tzinfo
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QIcon,
    QKeySequence,
    QPainter,
    QPaintEvent,
    QPixmap,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from naturelog.gps import Track
from naturelog.timezones import format_datetime
from naturelog.ui.map_view import local_files

#: Track colours on the map and in the list (blue is kept for the position marker).
TRACK_COLORS = (
    "#d6336c",
    "#f08c00",
    "#7048e8",
    "#0c8599",
    "#e03131",
    "#5c940d",
    "#ae3ec9",
    "#495057",
)
_SWATCH_SIZE = 16
_ROLE_PATH = Qt.ItemDataRole.UserRole


def track_colors(count: int) -> list[str]:
    return [TRACK_COLORS[i % len(TRACK_COLORS)] for i in range(count)]


class _TrackList(QListWidget):
    """List that accepts dropped GPS logs and shows a hint when empty."""

    pathsDropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setIconSize(QSize(_SWATCH_SIZE, _SWATCH_SIZE))
        self.setSpacing(2)
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if local_files(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if local_files(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = local_files(event)
        if paths:
            event.acceptProposedAction()
            self.pathsDropped.emit(paths)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if self.count() == 0:
            painter = QPainter(self.viewport())
            painter.setPen(self.palette().placeholderText().color())
            painter.drawText(
                self.viewport().rect().adjusted(12, 12, -12, -12),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                "Drop GPS logs (GPX, NMEA) here",
            )


class TrackSidebar(QWidget):
    """Loaded GPS tracks with their colour, size and time range.

    The main window owns the tracks; this panel only shows them and reports
    what the user asks for.
    """

    #: User asked to add GPS logs through a file dialog.
    addRequested = Signal()
    #: GPS log paths dropped onto the list.
    pathsDropped = Signal(list)
    #: User asked to remove the tracks loaded from these source paths.
    removeRequested = Signal(list)
    #: Indices of the selected tracks changed.
    selectionChanged = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tz: tzinfo = UTC
        self._tracks: list[Track] = []

        add_button = QPushButton("Add GPS log…")
        add_button.clicked.connect(self.addRequested)
        self._remove_button = QPushButton("Remove")
        self._remove_button.setToolTip(
            "Remove selected tracks from the session (files on disk are not changed)"
        )
        self._remove_button.clicked.connect(self._remove_selected)
        buttons = QHBoxLayout()
        buttons.addWidget(add_button)
        buttons.addWidget(self._remove_button)
        buttons.addStretch(1)

        self._list = _TrackList()
        self._list.pathsDropped.connect(self.pathsDropped)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        remove_action = QAction("Remove from session", self._list)
        remove_action.setShortcuts(
            [QKeySequence(QKeySequence.StandardKey.Delete), QKeySequence(Qt.Key.Key_Backspace)]
        )
        remove_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        remove_action.triggered.connect(self._remove_selected)
        self._list.addAction(remove_action)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        self._remove_action = remove_action

        self._count_label = QLabel()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(buttons)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._count_label)
        self._update_count()

    def set_tracks(self, tracks: list[Track]) -> None:
        """Show these tracks (in this order, coloured like on the map)."""
        selected = {item.data(_ROLE_PATH) for item in self._list.selectedItems()}
        self._tracks = list(tracks)
        self._list.blockSignals(True)
        try:
            self._list.clear()
            for track, color in zip(self._tracks, track_colors(len(self._tracks)), strict=True):
                item = QListWidgetItem(_swatch(color), "")
                item.setData(_ROLE_PATH, track.source_path)
                self._apply_text(item, track)
                self._list.addItem(item)
                item.setSelected(track.source_path in selected)
        finally:
            self._list.blockSignals(False)
        self._on_selection_changed()

    def set_timezone(self, tz: tzinfo) -> None:
        self._tz = tz
        for row, track in enumerate(self._tracks):
            self._apply_text(self._list.item(row), track)

    def _apply_text(self, item: QListWidgetItem, track: Track) -> None:
        source = "NMEA 0183 → GPX" if track.source_format == "nmea" else "GPX"
        item.setText(
            f"{track.name}\n"
            f"{source} · {track.point_count:,} points · {len(track.segments)} "
            f"segment{'s' if len(track.segments) != 1 else ''}\n"
            f"{format_datetime(track.start, self._tz)} – {format_datetime(track.end, self._tz)}"
        )
        tooltip = str(track.source_path) if track.source_path else track.name
        if track.skipped_points:
            tooltip += f"\n{track.skipped_points:,} points without usable timestamp skipped"
        item.setToolTip(tooltip)

    def _selected_rows(self) -> list[int]:
        return sorted(self._list.row(item) for item in self._list.selectedItems())

    def _remove_selected(self) -> None:
        paths: list[Path] = [item.data(_ROLE_PATH) for item in self._list.selectedItems()]
        if paths:
            self.removeRequested.emit(paths)

    def _on_selection_changed(self) -> None:
        self._update_count()
        self.selectionChanged.emit(self._selected_rows())

    def _update_count(self) -> None:
        count = self._list.count()
        selected = len(self._list.selectedItems())
        text = f"{count} track{'s' if count != 1 else ''}"
        if selected:
            text += f" · {selected} selected"
        self._count_label.setText(text)
        self._remove_button.setEnabled(selected > 0)
        self._remove_action.setEnabled(selected > 0)


def _swatch(color: str) -> QIcon:
    pixmap = QPixmap(_SWATCH_SIZE, _SWATCH_SIZE)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawRoundedRect(2, 2, _SWATCH_SIZE - 4, _SWATCH_SIZE - 4, 3, 3)
    painter.end()
    return QIcon(pixmap)
