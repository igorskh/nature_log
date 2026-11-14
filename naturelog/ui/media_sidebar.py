"""Left sidebar listing imported media files with previews."""

from __future__ import annotations

import threading
from datetime import UTC, tzinfo
from pathlib import Path

from PySide6.QtCore import (
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QPointF,
    QRect,
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
    QProgressBar,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from naturelog.gps import Track, position_at
from naturelog.media import (
    MediaFile,
    MediaKind,
    TimeSource,
    expand_paths,
    make_thumbnails,
    read_media_files,
)
from naturelog.species import SpeciesPrediction, can_detect, detected, top_detection
from naturelog.timezones import format_datetime
from naturelog.ui.map_view import local_files

_ICON_SIZE = 72
_THUMB_SIZE = 2 * _ICON_SIZE  # sharp on high-DPI screens
_ROLE_MEDIA = Qt.ItemDataRole.UserRole
#: list[SpeciesPrediction] from the last detection run, or None if never run.
_ROLE_DETECTIONS = Qt.ItemDataRole.UserRole + 1
_BADGE_SIZE = 20
_BADGE_COLOR = QColor(46, 139, 87)
_COPY_TIME_FORMAT = "%Y/%m/%d %H:%M"


# --- background work ----------------------------------------------------


class _Signals(QObject):
    scanned = Signal(object)  # list[MediaFile]
    thumbnail = Signal(str, object)  # path, QImage
    failed = Signal(str)
    progress = Signal(int, int)  # done, total (total 0 = unknown)
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


# --- widgets ------------------------------------------------------------


class _DetectionBadgeDelegate(QStyledItemDelegate):
    """Draws a badge on the preview of files with a detected species."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().paint(painter, option, index)
        if top_detection(index.data(_ROLE_DETECTIONS)) is None:
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        icon_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemDecoration, opt, opt.widget
        )
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
    #: Status messages for the main window's status bar.
    message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tz: tzinfo = UTC
        self._track: Track | None = None
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
        self._detect_action = self._list_action(
            "Detect species", QKeySequence(), self._request_detection
        )
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)

        self._remove_button = QPushButton("Remove")
        self._remove_button.setToolTip(
            "Remove selected files from the list (files on disk are not changed)"
        )
        self._remove_button.clicked.connect(self.remove_selected)
        buttons.addWidget(self._remove_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(buttons)
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

    def set_track(self, track: Track | None) -> None:
        """GPS track used to look up coordinates of the listed files."""
        self._track = track
        self._update_count()

    def copy_coordinates(self) -> None:
        """Copy ``lat, lon`` of the selected files, one line per file.

        Files outside the track or in a gap have no position and are skipped.
        """
        files = self.selected_files()
        if not files:
            return
        if self._track is None:
            self.message.emit("Load a GPS log to get coordinates")
            return
        lines = []
        for media in files:
            position = position_at(self._track, media.capture_time(self._tz))
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
        """Stop pending preview generation (call when the window closes)."""
        for task in self._tasks:
            if isinstance(task, _ThumbnailTask):
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

    def _on_progress(self, task: QRunnable, done: int, total: int) -> None:
        if task in self._tasks and self._current(task):  # ignore late or stale signals
            self._progress[task] = (done, total)
            self._update_progress()

    def _update_progress(self) -> None:
        """Show combined progress: metadata reading first, then previews."""
        scans = [p for t, p in self._progress.items() if isinstance(t, _ScanTask)]
        previews = [p for t, p in self._progress.items() if isinstance(t, _ThumbnailTask)]
        entries, label = (scans, "Reading metadata") if scans else (previews, "Generating previews")
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
        self._copy_coords_action.setEnabled(selected > 0 and self._track is not None)
        self._copy_time_action.setEnabled(selected > 0)
        self._detect_action.setEnabled(selected > 0)
        self._select_all_action.setEnabled(count > 0)
        self._list.viewport().update()
