"""Dialog to set the created, modified and EXIF capture dates of media files.

The date is entered in the selected timezone and prefilled with the first
file's capture time: EXIF ``DateTimeOriginal`` for EXIF-capable formats (if
recorded), otherwise the file-system creation date.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from PySide6.QtCore import QDate, QDateTime, QTime, QTimeZone
from PySide6.QtWidgets import (
    QCheckBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from naturelog.media.edit_datetime import DatetimeEdit, can_set_created
from naturelog.media.exiftool import exiftool_path
from naturelog.media.file_datetime import creation_time
from naturelog.media.formats import has_exif
from naturelog.media.models import MediaFile, TimeSource
from naturelog.timezones import format_datetime


class EditDatetimeDialog(QDialog):
    """Choose a date and which of the file's dates to set to it."""

    def __init__(self, files: list[MediaFile], tz: tzinfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit date and time")
        self._tz = tz
        first = files[0]
        exif_files = [m for m in files if has_exif(m.path)]

        title = (
            first.name if len(files) == 1 else f"{len(files)} files (prefilled from {first.name})"
        )
        heading = QLabel(title)
        heading.setWordWrap(True)

        # The editor shows plain wall-clock values; UTC avoids local DST gaps.
        local = first.capture_time(tz).astimezone(tz)
        self._editor = QDateTimeEdit()
        self._editor.setTimeZone(QTimeZone.utc())
        self._editor.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self._editor.setCalendarPopup(True)
        self._editor.setDateTime(
            QDateTime(
                QDate(local.year, local.month, local.day),
                QTime(local.hour, local.minute, local.second),
                QTimeZone.utc(),
            )
        )
        tz_label = QLabel(f"Timezone: {format_datetime(local, tz, '').strip()}")

        current = self._current_values(first)
        self._created = QCheckBox("System file created")
        self._modified = QCheckBox("System file modified")
        self._exif = QCheckBox("EXIF DateTimeOriginal")
        for box, text in zip((self._created, self._modified, self._exif), current, strict=True):
            box.setToolTip(f"Current ({first.name}): {text}")

        if not can_set_created():
            self._created.setEnabled(False)
            self._created.setToolTip("This operating system cannot set file creation dates")
        if not exif_files:
            self._exif.setEnabled(False)
            self._exif.setToolTip("None of the files supports EXIF")
        elif exiftool_path() is None:
            self._exif.setEnabled(False)
            self._exif.setToolTip("ExifTool is not installed")
        elif len(exif_files) < len(files):
            self._exif.setText(f"EXIF DateTimeOriginal ({len(exif_files)} of {len(files)} files)")

        # Suggest the date that was prefilled.
        if has_exif(first.path) and self._exif.isEnabled():
            self._exif.setChecked(True)
        elif self._created.isEnabled():
            self._created.setChecked(True)

        form = QFormLayout()
        form.addRow("Date and time:", self._editor)
        form.addRow("", tz_label)
        form.addRow(QLabel("Set:"))
        for box, text in zip((self._created, self._modified, self._exif), current, strict=True):
            row = QVBoxLayout()
            row.setSpacing(0)
            row.addWidget(box)
            hint = QLabel(f"current: {text}")
            hint.setEnabled(False)
            hint.setIndent(24)
            row.addWidget(hint)
            form.addRow(row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._apply = buttons.addButton("Apply", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        for box in (self._created, self._modified, self._exif):
            box.toggled.connect(self._update_apply)
        self._update_apply()

        note = QLabel("Writing EXIF keeps a backup of the original as <name>_original.")
        note.setWordWrap(True)
        note.setEnabled(False)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def edit(self) -> DatetimeEdit:
        """The chosen date (aware, in the selected timezone) and dates to set."""
        value = self._editor.dateTime()
        d, t = value.date(), value.time()
        when = datetime(
            d.year(), d.month(), d.day(), t.hour(), t.minute(), t.second(), tzinfo=self._tz
        )
        return DatetimeEdit(
            when=when,
            created=self._created.isChecked(),
            modified=self._modified.isChecked(),
            exif_original=self._exif.isChecked(),
        )

    def _update_apply(self) -> None:
        self._apply.setEnabled(
            any(b.isChecked() for b in (self._created, self._modified, self._exif))
        )

    def _current_values(self, media: MediaFile) -> tuple[str, str, str]:
        def fmt(timestamp: float) -> str:
            return format_datetime(datetime.fromtimestamp(timestamp).astimezone(), self._tz)

        try:
            created = fmt(creation_time(media.path))
            modified = fmt(media.path.stat().st_mtime)
        except OSError:
            created = modified = "unavailable"
        if media.time_source is TimeSource.EXIF:
            exif = media.captured_at.strftime("%Y-%m-%d %H:%M:%S")
        else:
            exif = "not recorded" if has_exif(media.path) else "not supported"
        return created, modified, exif
