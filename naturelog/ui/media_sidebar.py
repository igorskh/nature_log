"""Left sidebar listing imported media files with previews."""

from __future__ import annotations

import dataclasses
import threading
from datetime import UTC, tzinfo
from pathlib import Path

from PySide6.QtCore import (
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QPointF,
    QRect,
    QRectF,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from naturelog.gps import Track, TrackPosition, position_on_tracks
from naturelog.media import (
    MediaFile,
    MediaKind,
    TimeSource,
    expand_paths,
    make_thumbnails,
    read_media_files,
)
from naturelog.media.edit_datetime import DatetimeEdit, EditResult, edit_datetimes
from naturelog.media.exiftool import exiftool_path
from naturelog.media.formats import has_exif
from naturelog.media.geotag import GeotagResult, can_geotag, write_coordinates
from naturelog.species import SpeciesPrediction, can_detect, detected, top_detection
from naturelog.timezones import format_datetime
from naturelog.ui.edit_datetime_dialog import EditDatetimeDialog
from naturelog.ui.map_view import local_files

_ICON_SIZE = 72
_THUMB_SIZE = 2 * _ICON_SIZE  # sharp on high-DPI screens
_ROLE_MEDIA = Qt.ItemDataRole.UserRole
#: list[SpeciesPrediction] from the last detection run, or None if never run.
_ROLE_DETECTIONS = Qt.ItemDataRole.UserRole + 1
#: (stored in file, position from tracks) GPS flags of a listed file.
_ROLE_GPS = Qt.ItemDataRole.UserRole + 2
_BADGE_SIZE = 20
_BADGE_COLOR = QColor(46, 139, 87)
_GLOBE_SIZE = 18
#: Globe colours: coordinates stored in the file / interpolated from loaded tracks.
_GLOBE_STORED = QColor(47, 158, 68)
_GLOBE_TRACK = QColor(25, 113, 194)
_COPY_TIME_FORMAT = "%Y/%m/%d %H:%M"


# --- background work ----------------------------------------------------


class _Signals(QObject):
    scanned = Signal(object)  # list[MediaFile]
    thumbnail = Signal(str, object)  # path, QImage
    failed = Signal(str)
    progress = Signal(int, int)  # done, total (total 0 = unknown)
    written = Signal(object)  # list[GeotagResult]
    edited = Signal(object, object)  # list[EditResult], list[MediaFile] (re-read)
    finished = Signal()


class _ScanTask(QRunnable):
    """Expands folders and reads capture times off the UI thread."""

    def __init__(self, paths: list[str]) -> None:
        super().__init__()
        self.paths = paths
        self.signals = _Signals()

    def run(self) -> None:
        try:
            self.signals.progress.emit(0, 0)  # folder scan: size not yet known
            paths = expand_paths(self.paths)
            self.signals.scanned.emit(read_media_files(paths, self.signals.progress.emit))
        except Exception as exc:
            self.signals.failed.emit(f"Cannot import files: {exc}")
        finally:
            self.signals.finished.emit()


class _ThumbnailTask(QRunnable):
    """Generates previews in parallel, emitting each as it is ready."""

    def __init__(self, files: list[MediaFile]) -> None:
        super().__init__()
        self.files = files
        self.signals = _Signals()
        self.cancelled = threading.Event()

    def run(self) -> None:
        total = len(self.files)
        try:
            self.signals.progress.emit(0, total)
            results = make_thumbnails(self.files, _THUMB_SIZE, cancelled=self.cancelled)
            for done, (media, data) in enumerate(results, 1):
                self.signals.progress.emit(done, total)
                if data:
                    image = QImage.fromData(data)  # QImage is safe off the UI thread
                    if not image.isNull():
                        self.signals.thumbnail.emit(str(media.path), image)
        finally:
            self.signals.finished.emit()


class _GeotagTask(QRunnable):
    """Writes GPS coordinates into the EXIF of files off the UI thread."""

    def __init__(self, items: list[tuple[MediaFile, TrackPosition]]) -> None:
        super().__init__()
        self.items = items
        self.signals = _Signals()
        self.cancelled = threading.Event()

    def run(self) -> None:
        total = len(self.items)
        try:
            self.signals.progress.emit(0, total)
            results = write_coordinates(
                self.items,
                lambda done: self.signals.progress.emit(done, total),
                self.cancelled,
            )
            self.signals.written.emit(results)
        except Exception as exc:
            self.signals.failed.emit(f"Cannot write coordinates: {exc}")
        finally:
            self.signals.finished.emit()


class _EditDatetimeTask(QRunnable):
    """Sets file dates off the UI thread, then re-reads the files' capture times."""

    def __init__(self, paths: list[Path], edit: DatetimeEdit) -> None:
        super().__init__()
        self.paths = paths
        self.edit = edit
        self.signals = _Signals()

    def run(self) -> None:
        total = len(self.paths)
        try:
            self.signals.progress.emit(0, total)
            results = edit_datetimes(
                self.paths, self.edit, lambda done: self.signals.progress.emit(done, total)
            )
            self.signals.edited.emit(results, read_media_files(self.paths))
        except Exception as exc:
            self.signals.failed.emit(f"Cannot edit date and time: {exc}")
        finally:
            self.signals.finished.emit()


# --- widgets ------------------------------------------------------------


def _paint_globe(painter: QPainter, rect: QRectF, color: QColor) -> None:
    """A filled globe with meridian and parallels, outlined in white."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(Qt.GlobalColor.white, 1.5))
    painter.setBrush(color)
    painter.drawEllipse(rect)
    inner = rect.adjusted(2, 2, -2, -2)
    pen = QPen(Qt.GlobalColor.white, 1.1)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    cx, cy = inner.center().x(), inner.center().y()
    w, h = inner.width(), inner.height()
    painter.drawEllipse(QRectF(cx - w * 0.22, inner.top(), w * 0.44, h))  # meridian
    painter.drawLine(QPointF(cx, inner.top()), QPointF(cx, inner.bottom()))
    painter.drawLine(QPointF(inner.left(), cy), QPointF(inner.right(), cy))  # equator
    for dy in (-0.27, 0.27):
        y = cy + h * dy
        half = w * 0.42
        painter.drawLine(QPointF(cx - half, y), QPointF(cx + half, y))
    painter.restore()


class _DetectionBadgeDelegate(QStyledItemDelegate):
    """Draws badges on previews: species detected, and where GPS coordinates come from.

    A green globe means the file stores GPS coordinates in its EXIF; a blue
    globe means a position is interpolated from the loaded tracks.
    """

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().paint(painter, option, index)
        stored, on_track = index.data(_ROLE_GPS) or (False, False)
        detected_species = top_detection(index.data(_ROLE_DETECTIONS)) is not None
        if not (stored or on_track or detected_species):
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        icon_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemDecoration, opt, opt.widget
        )
        # Globes along the top edge, from the right: stored (green), then track (blue).
        x = icon_rect.right() - _GLOBE_SIZE + 3
        for show, color in ((stored, _GLOBE_STORED), (on_track, _GLOBE_TRACK)):
            if show:
                _paint_globe(
                    painter, QRectF(x, icon_rect.top() - 3, _GLOBE_SIZE, _GLOBE_SIZE), color
                )
                x -= _GLOBE_SIZE + 2
        if not detected_species:
            return
        badge = QRect(
            icon_rect.right() - _BADGE_SIZE + 3,
            icon_rect.bottom() - _BADGE_SIZE + 3,
            _BADGE_SIZE,
            _BADGE_SIZE,
        )
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(Qt.GlobalColor.white, 1.5))
        painter.setBrush(_BADGE_COLOR)
        painter.drawEllipse(badge)
        # Check mark
        pen = QPen(Qt.GlobalColor.white, 2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        x, y, s = badge.x(), badge.y(), _BADGE_SIZE
        painter.drawPolyline([
            QPointF(x + s * 0.28, y + s * 0.52),
            QPointF(x + s * 0.44, y + s * 0.68),
            QPointF(x + s * 0.73, y + s * 0.34),
        ])  # fmt: skip
        painter.restore()


class _MediaList(QListWidget):
    """List that accepts dropped files/folders and shows a hint when empty."""

    pathsDropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setViewMode(QListWidget.ViewMode.ListMode)
        self.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        self.setUniformItemSizes(True)
        self.setSpacing(2)
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setItemDelegate(_DetectionBadgeDelegate(self))

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
                "Drop photos, recordings or folders here",
            )


class MediaSidebar(QWidget):
    """Imported media files with capture times and previews."""

    #: Emitted after files were added; carries the new :class:`MediaFile` list.
    filesAdded = Signal(list)
    #: Emitted after files were removed from the list; carries the removed :class:`MediaFile` list.
    filesRemoved = Signal(list)
    #: Emitted when the selection changes; carries the selected :class:`MediaFile` list.
    selectionChanged = Signal(list)
    #: Emitted when a file's preview icon changes; carries the :class:`MediaFile`.
    iconChanged = Signal(object)
    #: Emitted when species detections of files changed.
    detectionsChanged = Signal()
    #: User asked to detect species in these :class:`MediaFile` photos/recordings.
    detectRequested = Signal(list)
    #: User asked to upload these selected :class:`MediaFile` objects to iNaturalist.
    uploadRequested = Signal(list)
    #: Status messages for the main window's status bar.
    message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tz: tzinfo = UTC
        self._tracks: list[Track] = []
        self._items: dict[Path, QListWidgetItem] = {}
        self._tasks: set[QRunnable] = set()
        self._pending_scans = 0
        #: Bumped by :meth:`clear`; results of tasks from older generations are ignored.
        self._generation = 0
        self._task_generation: dict[QRunnable, int] = {}
        #: Progress (done, total) of running background tasks.
        self._progress: dict[QRunnable, tuple[int, int]] = {}

        add_files = QPushButton("Add files…")
        add_files.clicked.connect(self._choose_files)
        add_folder = QPushButton("Add folder…")
        add_folder.clicked.connect(self._choose_folder)
        self._count_label = QLabel()
        self._progress_bar = QProgressBar()
        self._progress_bar.setTextVisible(True)
        self._progress_bar.hide()

        buttons = QHBoxLayout()
        buttons.addWidget(add_files)
        buttons.addWidget(add_folder)

        self._list = _MediaList()
        self._list.pathsDropped.connect(self.add_paths)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)

        # Actions live on the list: shortcuts work while it has focus, and they
        # form its right-click menu.
        self._select_all_action = self._list_action(
            "Select all", QKeySequence.StandardKey.SelectAll, self.select_all
        )
        self._clear_selection_action = self._list_action(
            "Clear selection", QKeySequence.StandardKey.Deselect, self.clear_selection
        )
        self._remove_action = self._list_action(
            "Remove from list",
            [QKeySequence(QKeySequence.StandardKey.Delete), QKeySequence(Qt.Key.Key_Backspace)],
            self.remove_selected,
        )
        separator = QAction(self._list)
        separator.setSeparator(True)
        self._list.addAction(separator)
        self._copy_coords_action = self._list_action(
            "Copy coordinates", QKeySequence(), self.copy_coordinates
        )
        self._copy_time_action = self._list_action(
            "Copy date and time", QKeySequence(), self.copy_datetimes
        )
        self._edit_time_action = self._list_action(
            "Edit date and time…", QKeySequence(), self.edit_datetimes
        )
        self._detect_action = self._list_action(
            "Detect species", QKeySequence(), self._request_detection
        )
        self._write_coords_action = self._list_action(
            "Write coordinates to files…", QKeySequence(), self.write_coordinates
        )
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)

        self._remove_button = QPushButton("Remove")
        self._remove_button.setToolTip(
            "Remove selected files from the list (files on disk are not changed)"
        )
        self._remove_button.clicked.connect(self.remove_selected)
        buttons.addWidget(self._remove_button)

        self._write_coords_button = QPushButton("Write coordinates")
        self._write_coords_button.setToolTip(
            "Write GPS coordinates from the track into the EXIF of the selected photos "
            "(a backup of each original is kept as <name>_original)"
        )
        self._write_coords_button.clicked.connect(self.write_coordinates)
        self._upload_button = QPushButton("Upload to iNaturalist")
        self._upload_button.setToolTip(
            "Open iNaturalist in a separate window to sign in and upload the selected files"
        )
        self._upload_button.clicked.connect(
            lambda: self.uploadRequested.emit(self.selected_files())
        )
        geotag_buttons = QHBoxLayout()
        geotag_buttons.addWidget(self._write_coords_button)
        geotag_buttons.addWidget(self._upload_button)
        geotag_buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(buttons)
        layout.addLayout(geotag_buttons)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._progress_bar)
        layout.addWidget(self._count_label)
        self._update_count()

        style = self.style()
        self._placeholders = {
            MediaKind.AUDIO: style.standardIcon(QStyle.StandardPixmap.SP_MediaVolume),
            MediaKind.VIDEO: style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay),
        }
        self._file_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)

    # --- public API -----------------------------------------------------

    def media_files(self) -> list[MediaFile]:
        return [self._list.item(i).data(_ROLE_MEDIA) for i in range(self._list.count())]

    def selected_files(self) -> list[MediaFile]:
        return [item.data(_ROLE_MEDIA) for item in self._list.selectedItems()]

    def icon_for(self, media: MediaFile) -> QIcon:
        """Preview icon of a listed file (placeholder until its thumbnail is ready)."""
        item = self._items.get(media.path)
        if item is not None:
            return item.icon()
        return self._placeholders.get(media.kind, self._file_icon)

    def detections(self) -> dict[Path, list[SpeciesPrediction] | None]:
        """Species predictions of every listed file (``None`` = not analysed)."""
        return {path: item.data(_ROLE_DETECTIONS) for path, item in self._items.items()}

    def detections_for(self, media: MediaFile) -> list[SpeciesPrediction] | None:
        item = self._items.get(media.path)
        return None if item is None else item.data(_ROLE_DETECTIONS)

    def set_detections(self, results: dict[Path, list[SpeciesPrediction] | None]) -> None:
        """Store species predictions of listed files (unlisted paths are ignored)."""
        changed = False
        for path, predictions in results.items():
            item = self._items.get(path)
            if item is None:
                continue
            item.setData(_ROLE_DETECTIONS, predictions)
            self._apply_text(item, item.data(_ROLE_MEDIA))
            changed = True
        if changed:
            self.detectionsChanged.emit()

    def select_all(self) -> None:
        self._list.selectAll()

    def select_paths(self, paths: list[Path]) -> None:
        """Select exactly the listed files with these paths and scroll to the first."""
        items = [self._items[p] for p in paths if p in self._items]
        self._list.blockSignals(True)
        try:
            self._list.clearSelection()
            for item in items:
                item.setSelected(True)
            if items:
                first = min(items, key=self._list.row)
                self._list.setCurrentItem(first, QItemSelectionModel.SelectionFlag.NoUpdate)
                self._list.scrollToItem(first)
        finally:
            self._list.blockSignals(False)
        self._on_selection_changed()

    def clear_selection(self) -> None:
        self._list.clearSelection()

    def remove_selected(self) -> None:
        """Remove the selected files from the list. Files on disk are not touched."""
        items = self._list.selectedItems()
        if not items:
            return
        removed: list[MediaFile] = []
        # Removing items one by one would emit a selection change per item.
        self._list.blockSignals(True)
        try:
            for item in items:
                media: MediaFile = item.data(_ROLE_MEDIA)
                self._list.takeItem(self._list.row(item))
                self._items.pop(media.path, None)
                removed.append(media)
        finally:
            self._list.blockSignals(False)
        self._on_selection_changed()
        self.detectionsChanged.emit()
        self.message.emit(
            f"Removed {len(removed)} file{'s' if len(removed) != 1 else ''} from the list"
        )
        self.filesRemoved.emit(removed)

    def set_tracks(self, tracks: list[Track]) -> None:
        """GPS tracks used to look up coordinates of the listed files."""
        self._tracks = list(tracks)
        for item in self._items.values():
            self._apply_text(item, item.data(_ROLE_MEDIA))
        self._update_count()

    def copy_coordinates(self) -> None:
        """Copy ``lat, lon`` of the selected files, one line per file.

        Files outside the track or in a gap have no position and are skipped.
        """
        files = self.selected_files()
        if not files:
            return
        if not self._tracks:
            self.message.emit("Load a GPS log to get coordinates")
            return
        lines = []
        for media in files:
            position = position_on_tracks(self._tracks, media.capture_time(self._tz))
            if position is not None:
                lines.append(f"{position.latitude:.6f}, {position.longitude:.6f}")
        missing = len(files) - len(lines)
        if not lines:
            self.message.emit("No GPS position for the selected file(s) – nothing copied")
            return
        QApplication.clipboard().setText("\n".join(lines))
        text = f"Copied coordinates of {len(lines)} file{'s' if len(lines) != 1 else ''}"
        if missing:
            text += f" ({missing} without GPS position skipped)"
        self.message.emit(text)

    def write_coordinates(self) -> None:
        """Write track coordinates into the EXIF GPS tags of the selected photos.

        Only EXIF-capable formats with a position on the track are written;
        files outside the track or in a gap are skipped (no extrapolation).
        """
        files = self.selected_files()
        if not files or self._geotag_running():
            return
        if not self._tracks:
            self.message.emit("Load a GPS log to get coordinates")
            return
        if exiftool_path() is None:
            QMessageBox.warning(
                self, "Write coordinates", "ExifTool is required to write coordinates."
            )
            return
        compatible = [m for m in files if can_geotag(m)]
        items = []
        for media in compatible:
            position = position_on_tracks(self._tracks, media.capture_time(self._tz))
            if position is not None:
                items.append((media, position))
        notes = []
        if len(files) > len(compatible):
            notes.append(f"{len(files) - len(compatible)} not a supported photo format")
        if len(compatible) > len(items):
            notes.append(f"{len(compatible) - len(items)} outside the track or in a gap")
        if not items:
            self.message.emit(
                "No selected file can be geotagged" + (f" ({', '.join(notes)})" if notes else "")
            )
            return
        text = (
            f"Write GPS coordinates into {len(items)} "
            f"file{'s' if len(items) != 1 else ''}?\n\n"
            "Existing GPS tags are replaced. ExifTool keeps each original as "
            "<name>_original next to the file."
        )
        if notes:
            text += "\n\nSkipped: " + "; ".join(notes) + "."
        answer = QMessageBox.question(
            self,
            "Write coordinates",
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        task = _GeotagTask(items)
        task.signals.written.connect(
            lambda results: self._on_written(results, len(files), dict(
                (media.path, position) for media, position in items
            ))
        )  # fmt: skip
        task.signals.failed.connect(self.message)
        task.signals.progress.connect(lambda done, total: self._on_progress(task, done, total))
        task.signals.finished.connect(lambda: self._on_geotag_finished(task))
        self._start(task)
        self._update_count()

    def copy_datetimes(self) -> None:
        """Copy capture times (selected timezone) of the selected files, one line per file."""
        files = self.selected_files()
        if not files:
            return
        lines = [
            media.capture_time(self._tz).astimezone(self._tz).strftime(_COPY_TIME_FORMAT)
            for media in files
        ]
        QApplication.clipboard().setText("\n".join(lines))
        self.message.emit(
            f"Copied date and time of {len(lines)} file{'s' if len(lines) != 1 else ''}"
        )

    def edit_datetimes(self) -> None:
        """Ask for a date and set the chosen file dates of the selected files to it."""
        files = self.selected_files()
        if not files:
            self.message.emit("Select files to edit the date and time of")
            return
        if self._edit_running():
            self.message.emit("Dates are already being edited")
            return
        dialog = EditDatetimeDialog(files, self._tz, self)
        if dialog.exec() != EditDatetimeDialog.DialogCode.Accepted:
            return
        edit = dialog.edit()
        if not edit.any:
            return
        task = _EditDatetimeTask([m.path for m in files], edit)
        task.signals.edited.connect(self._on_edited)
        task.signals.failed.connect(self.message)
        task.signals.progress.connect(lambda done, total: self._on_progress(task, done, total))
        task.signals.finished.connect(lambda: self._on_geotag_finished(task))
        self._start(task)
        self._update_count()

    def set_timezone(self, tz: tzinfo) -> None:
        self._tz = tz
        for item in self._items.values():
            self._apply_text(item, item.data(_ROLE_MEDIA))
        self._sort()

    def add_paths(self, paths: list[str]) -> None:
        if not paths:
            return
        self._pending_scans += 1
        self._update_count()
        task = _ScanTask(paths)
        task.signals.scanned.connect(lambda files: self._on_scanned(task, files))
        task.signals.failed.connect(self.message)
        task.signals.progress.connect(lambda done, total: self._on_progress(task, done, total))
        task.signals.finished.connect(lambda: self._on_scan_finished(task))
        self._start(task)

    def add_media(self, files: list[MediaFile]) -> None:
        """Add already-read media files (e.g. from a restored session) without rescanning."""
        if files:
            self._add_files(files)

    def clear(self) -> None:
        """Remove every file from the list and stop running imports and previews."""
        self.shutdown()
        self._generation += 1
        self._pending_scans = 0
        self._progress.clear()
        self._update_progress()
        removed = self.media_files()
        self._list.blockSignals(True)
        try:
            self._list.clear()
        finally:
            self._list.blockSignals(False)
        self._items.clear()
        self._on_selection_changed()
        self.detectionsChanged.emit()
        if removed:
            self.filesRemoved.emit(removed)

    def shutdown(self) -> None:
        """Stop pending previews and coordinate writes (call when the window closes)."""
        for task in self._tasks:
            if isinstance(task, (_ThumbnailTask, _GeotagTask)):
                task.cancelled.set()

    # --- selection ------------------------------------------------------

    def _list_action(self, text: str, shortcut, slot) -> QAction:
        action = QAction(text, self._list)
        if isinstance(shortcut, list):
            action.setShortcuts(shortcut)
        else:
            action.setShortcut(shortcut)
        action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        action.triggered.connect(slot)
        self._list.addAction(action)
        return action

    def _request_detection(self) -> None:
        files = [m for m in self.selected_files() if can_detect(m)]
        if files:
            self.detectRequested.emit(files)
        else:
            self.message.emit("Select photos or recordings to detect species in")

    def _on_selection_changed(self) -> None:
        self._update_count()
        self.selectionChanged.emit(self.selected_files())

    # --- dialogs --------------------------------------------------------

    def _choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add media files")
        self.add_paths(paths)

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add folder")
        if folder:
            self.add_paths([folder])

    # --- results --------------------------------------------------------

    def _start(self, task: QRunnable) -> None:
        self._tasks.add(task)  # keep the Python object (and its signals) alive
        self._task_generation[task] = self._generation
        QThreadPool.globalInstance().start(task)

    def _current(self, task: QRunnable) -> bool:
        """Whether the task was started after the last :meth:`clear`."""
        return self._task_generation.get(task) == self._generation

    def _on_scan_finished(self, task: QRunnable) -> None:
        if self._current(task):
            self._pending_scans -= 1
        self._finish(task)
        self._update_count()

    def _finish(self, task: QRunnable) -> None:
        self._tasks.discard(task)
        self._task_generation.pop(task, None)
        self._progress.pop(task, None)
        self._update_progress()

    def _edit_running(self) -> bool:
        return any(isinstance(t, _EditDatetimeTask) for t in self._tasks)

    def _on_edited(self, results: list[EditResult], files: list[MediaFile]) -> None:
        # Show the new capture times; the list may have changed meanwhile.
        updated = []
        for media in files:
            item = self._items.get(media.path)
            if item is not None:
                item.setData(_ROLE_MEDIA, media)
                self._apply_text(item, media)
                updated.append(media)
        self._sort()
        if updated:
            self.filesAdded.emit(updated)  # listeners refresh timeline and session
            self._on_selection_changed()

        failed = [r for r in results if r.error is not None]
        done = len(results) - len(failed)
        text = f"Edited date and time of {done} file{'s' if done != 1 else ''}"
        if failed:
            text += f", {len(failed)} failed"
        self.message.emit(text)
        if failed:
            lines = [f"{r.path.name}: {r.error}" for r in failed[:15]]
            if len(failed) > 15:
                lines.append(f"… and {len(failed) - 15} more")
            QMessageBox.warning(
                self,
                "Edit date and time",
                f"{len(failed)} file{'s' if len(failed) != 1 else ''} not fully updated:\n\n"
                + "\n".join(lines),
            )

    def _geotag_running(self) -> bool:
        return any(isinstance(t, _GeotagTask) for t in self._tasks)

    def _on_geotag_finished(self, task: QRunnable) -> None:
        self._finish(task)
        self._update_count()

    def _on_written(
        self, results: list[GeotagResult], selected: int, positions: dict[Path, TrackPosition]
    ) -> None:
        # Files written successfully now store these coordinates themselves.
        updated = []
        for result in results:
            item = self._items.get(result.path)
            position = positions.get(result.path)
            if result.error is None and item is not None and position is not None:
                media = dataclasses.replace(
                    item.data(_ROLE_MEDIA),
                    exif_coordinates=(position.latitude, position.longitude),
                )
                item.setData(_ROLE_MEDIA, media)
                self._apply_text(item, media)
                updated.append(media)
        if updated:
            self.filesAdded.emit(updated)  # listeners refresh markers and session
            self._on_selection_changed()

        failed = [r for r in results if r.error is not None]
        written = len(results) - len(failed)
        text = f"Wrote coordinates to {written} file{'s' if written != 1 else ''}"
        if failed:
            text += f", {len(failed)} failed"
        if selected > len(results):
            text += f" ({selected - len(results)} skipped)"
        self.message.emit(text)
        if failed:
            lines = [f"{r.path.name}: {r.error}" for r in failed[:15]]
            if len(failed) > 15:
                lines.append(f"… and {len(failed) - 15} more")
            QMessageBox.warning(
                self,
                "Write coordinates",
                f"{len(failed)} file{'s' if len(failed) != 1 else ''} not updated:\n\n"
                + "\n".join(lines),
            )

    def _on_progress(self, task: QRunnable, done: int, total: int) -> None:
        if task in self._tasks and self._current(task):  # ignore late or stale signals
            self._progress[task] = (done, total)
            self._update_progress()

    def _update_progress(self) -> None:
        """Show combined progress: metadata reading first, then previews."""
        scans = [p for t, p in self._progress.items() if isinstance(t, _ScanTask)]
        previews = [p for t, p in self._progress.items() if isinstance(t, _ThumbnailTask)]
        writes = [p for t, p in self._progress.items() if isinstance(t, _GeotagTask)]
        edits = [p for t, p in self._progress.items() if isinstance(t, _EditDatetimeTask)]
        if writes:
            entries, label = writes, "Writing coordinates"
        elif edits:
            entries, label = edits, "Editing dates"
        elif scans:
            entries, label = scans, "Reading metadata"
        else:
            entries, label = previews, "Generating previews"
        if not entries:
            self._progress_bar.hide()
            return
        if any(total == 0 for _, total in entries):  # still scanning folders
            self._progress_bar.setRange(0, 0)
            self._progress_bar.setFormat("Scanning folders…")
        else:
            self._progress_bar.setRange(0, sum(total for _, total in entries))
            self._progress_bar.setValue(sum(done for done, _ in entries))
            self._progress_bar.setFormat(f"{label} %v / %m")
        self._progress_bar.show()

    def _on_scanned(self, task: QRunnable, files: list[MediaFile]) -> None:
        if self._current(task):
            self._add_files(files)

    def _add_files(self, files: list[MediaFile]) -> None:
        new = [m for m in files if m.path not in self._items]
        for media in new:
            item = QListWidgetItem(self._placeholders.get(media.kind, self._file_icon), "")
            item.setData(_ROLE_MEDIA, media)
            self._apply_text(item, media)
            self._items[media.path] = item
            self._list.addItem(item)
        self._sort()
        self._update_count()

        skipped = len(files) - len(new)
        text = f"Added {len(new)} file{'s' if len(new) != 1 else ''}"
        if skipped:
            text += f" ({skipped} already in the list)"
        self.message.emit(text)
        if new:
            self.filesAdded.emit(new)

        images = [m for m in new if m.kind.is_image]
        if images:
            task = _ThumbnailTask(images)
            task.signals.thumbnail.connect(
                lambda path, image: self._on_thumbnail(task, path, image)
            )
            task.signals.progress.connect(
                lambda done, total: self._on_progress(task, done, total)
            )
            task.signals.finished.connect(lambda: self._finish(task))
            self._start(task)

    def _on_thumbnail(self, task: QRunnable, path: str, image: QImage) -> None:
        item = self._items.get(Path(path)) if self._current(task) else None
        if item is not None:
            item.setIcon(QIcon(QPixmap.fromImage(image)))
            self.iconChanged.emit(item.data(_ROLE_MEDIA))

    # --- presentation ---------------------------------------------------

    def _apply_text(self, item: QListWidgetItem, media: MediaFile) -> None:
        stamp = format_datetime(media.capture_time(self._tz), self._tz)
        source = "captured" if media.time_source is TimeSource.EXIF else "file created"
        lines = [media.name, stamp, source]
        tooltip = [str(media.path)]
        stored = media.exif_coordinates is not None and has_exif(media.path)
        track_position = position_on_tracks(self._tracks, media.capture_time(self._tz))
        item.setData(_ROLE_GPS, (stored, track_position is not None))
        if stored:
            lat, lon = media.exif_coordinates
            tooltip.append(f"GPS stored in file: {lat:.6f}, {lon:.6f}")
        if track_position is not None:
            how = "interpolated" if track_position.interpolated else "recorded fix"
            tooltip.append(
                f"GPS from tracks ({how}): "
                f"{track_position.latitude:.6f}, {track_position.longitude:.6f}"
            )
        predictions = item.data(_ROLE_DETECTIONS)
        hits = detected(predictions)
        if hits:
            lines.append(f"{hits[0].display_name} · {hits[0].score:.0%}")
            tooltip.append("")
            tooltip += [
                f"{p.display_name} ({p.scientific_name}) · {p.score:.0%}"
                + (f" · at {p.span_text}" if p.span_text else "")
                for p in hits
            ]
        elif predictions is not None:
            tooltip.append("No species detected")
        item.setText("\n".join(lines))
        item.setToolTip("\n".join(tooltip))

    def _sort_key(self, media: MediaFile) -> float:
        return media.capture_time(self._tz).timestamp()

    def _sort(self) -> None:
        # Re-inserting items drops the selection; restore it without emitting changes.
        selected = set(map(id, self._list.selectedItems()))
        current = self._list.currentItem()
        self._list.blockSignals(True)
        try:
            items = [self._list.takeItem(0) for _ in range(self._list.count())]
            items.sort(key=lambda it: (self._sort_key(it.data(_ROLE_MEDIA)), it.text()))
            for item in items:
                self._list.addItem(item)
                item.setSelected(id(item) in selected)
            if current is not None:
                self._list.setCurrentItem(current, QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            self._list.blockSignals(False)

    def _update_count(self) -> None:
        count = self._list.count()
        text = f"{count} file{'s' if count != 1 else ''}"
        selected = len(self._list.selectedItems())
        if selected:
            text += f" · {selected} selected"
        if self._pending_scans:
            text += " · importing…"
        self._count_label.setText(text)
        self._remove_button.setEnabled(selected > 0)
        self._remove_action.setEnabled(selected > 0)
        self._clear_selection_action.setEnabled(selected > 0)
        self._copy_coords_action.setEnabled(selected > 0 and bool(self._tracks))
        self._copy_time_action.setEnabled(selected > 0)
        self._edit_time_action.setEnabled(selected > 0 and not self._edit_running())
        self._detect_action.setEnabled(selected > 0)
        can_write = selected > 0 and bool(self._tracks) and not self._geotag_running()
        self._write_coords_action.setEnabled(can_write)
        self._write_coords_button.setEnabled(can_write)
        self._select_all_action.setEnabled(count > 0)
        self._list.viewport().update()
