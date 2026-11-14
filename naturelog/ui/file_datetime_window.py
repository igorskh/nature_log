"""Window that copies file dates from source files to matching target files.

Source and target files are added by drag and drop or with the buttons. A
target matches a source when its name fits the pattern (``{basename}_*`` by
default). The preview lists every pair; "Apply" sets the targets' modification
and creation dates to the source's. File contents are never changed.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from naturelog.media.file_datetime import (
    DEFAULT_PATTERN,
    DatetimeMapping,
    FileDatetimeError,
    apply_mapping,
    list_files,
    map_files,
)
from naturelog.ui.map_view import local_files


class _DropList(QListWidget):
    pathsDropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)

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
            self.pathsDropped.emit([Path(path) for path in paths])


class _FileColumn(QGroupBox):
    """A titled file list with add/remove buttons."""

    changed = Signal()

    def __init__(self, title: str) -> None:
        super().__init__(title)
        self._files: list[Path] = []
        self._list = _DropList()
        self._list.pathsDropped.connect(self.add_paths)

        add_files = QPushButton("Add files…")
        add_files.clicked.connect(self._choose_files)
        add_folder = QPushButton("Add folder…")
        add_folder.clicked.connect(self._choose_folder)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_selected)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)

        buttons = QHBoxLayout()
        for button in (add_files, add_folder, remove, clear):
            buttons.addWidget(button)
        buttons.addStretch(1)

        self._count = QLabel()
        layout = QVBoxLayout(self)
        layout.addLayout(buttons)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._count)
        self._update_count()

    def files(self) -> list[Path]:
        return list(self._files)

    def add_paths(self, paths: list[Path]) -> None:
        known = set(self._files)
        for path in list_files(paths):
            if path not in known:
                known.add(path)
                self._files.append(path)
                self._list.addItem(path.name)
                self._list.item(self._list.count() - 1).setToolTip(str(path))
        self._update_count()
        self.changed.emit()

    def clear(self) -> None:
        self._files.clear()
        self._list.clear()
        self._update_count()
        self.changed.emit()

    def _remove_selected(self) -> None:
        rows = sorted((self._list.row(item) for item in self._list.selectedItems()), reverse=True)
        for row in rows:
            self._list.takeItem(row)
            del self._files[row]
        self._update_count()
        self.changed.emit()

    def _choose_files(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, f"Add {self.title().lower()} files")
        if names:
            self.add_paths([Path(name) for name in names])

    def _choose_folder(self) -> None:
        name = QFileDialog.getExistingDirectory(self, f"Add {self.title().lower()} folder")
        if name:
            self.add_paths([Path(name)])

    def _update_count(self) -> None:
        self._count.setText(f"{len(self._files)} files — drop files or folders here")


def _format(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).isoformat(sep=" ", timespec="seconds")


class FileDatetimeWindow(QWidget):
    """Map created/modified dates from source files to matching target files."""

    message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Map file datetime")
        self.resize(1000, 700)
        self._mappings: list[DatetimeMapping] = []

        self._pattern = QLineEdit(DEFAULT_PATTERN)
        self._pattern.setToolTip(
            "Target file name pattern. {basename} is the source name without extension; "
            "* and ? are wildcards. An offset like +1m30s in the target name is added."
        )
        self._pattern.textChanged.connect(self._update_preview)
        pattern_row = QHBoxLayout()
        pattern_row.addWidget(QLabel("Target name pattern:"))
        pattern_row.addWidget(self._pattern, 1)

        self._source = _FileColumn("Source")
        self._target = _FileColumn("Target")
        self._source.changed.connect(self._update_preview)
        self._target.changed.connect(self._update_preview)
        columns = QHBoxLayout()
        columns.addWidget(self._source)
        columns.addWidget(self._target)

        self._preview = QTreeWidget()
        self._preview.setRootIsDecorated(False)
        self._preview.setHeaderLabels(["Target", "Source", "Offset", "Modified", "Created"])
        self._preview.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

        self._status = QLabel()
        self._apply_button = QPushButton("Apply")
        self._apply_button.clicked.connect(self._apply)
        bottom = QHBoxLayout()
        bottom.addWidget(self._status, 1)
        bottom.addWidget(self._apply_button)

        layout = QVBoxLayout(self)
        layout.addLayout(pattern_row)
        layout.addLayout(columns, 2)
        layout.addWidget(QLabel("Preview"))
        layout.addWidget(self._preview, 1)
        layout.addLayout(bottom)
        self._update_preview()

    def _update_preview(self) -> None:
        self._preview.clear()
        self._mappings = []
        try:
            self._mappings = map_files(
                self._source.files(), self._target.files(), self._pattern.text()
            )
        except (FileDatetimeError, OSError) as exc:
            self._status.setText(str(exc))
        else:
            unmatched = len(self._target.files()) - len(self._mappings)
            self._status.setText(
                f"{len(self._mappings)} target files matched, {unmatched} without a source"
            )
        for mapping in self._mappings:
            QTreeWidgetItem(
                self._preview,
                [
                    mapping.target.name,
                    mapping.source.name,
                    f"+{mapping.offset} s" if mapping.offset else "",
                    _format(mapping.modified),
                    _format(mapping.created),
                ],
            )
        self._apply_button.setEnabled(bool(self._mappings))

    def _apply(self) -> None:
        answer = QMessageBox.question(
            self,
            "Map file datetime",
            f"Set modified and created dates of {len(self._mappings)} target files?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        errors = []
        for mapping in self._mappings:
            try:
                apply_mapping(mapping)
            except (FileDatetimeError, OSError) as exc:
                errors.append(f"{mapping.target.name}: {exc}")
        done = len(self._mappings) - len(errors)
        self.message.emit(f"File dates updated for {done} files")
        if errors:
            QMessageBox.warning(self, "Map file datetime", "\n".join(errors[:20]))
        self._update_preview()
