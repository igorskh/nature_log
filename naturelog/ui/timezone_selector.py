"""Combo box for choosing the timezone dates and times are displayed in."""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo

from PySide6.QtCore import Qt, QTimeZone, Signal
from PySide6.QtWidgets import QComboBox, QCompleter

from naturelog.timezones import UTC_NAME, format_offset, get_timezone, timezone_names


def system_timezone_name() -> str:
    """IANA name of the operating system's timezone, or ``UTC`` if unknown."""
    name = bytes(QTimeZone.systemTimeZoneId().data()).decode("ascii", "replace")
    return name if get_timezone(name) is not None else UTC_NAME


class TimezoneSelector(QComboBox):
    """Editable, searchable list of IANA timezones. Starts with the system timezone."""

    #: Emitted with the newly selected ``tzinfo``.
    timezoneChanged = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMaxVisibleItems(20)
        self.setMinimumContentsLength(22)
        self.setToolTip("Timezone used to display dates and times (data is kept in UTC)")

        # Offsets are today's; zones with daylight saving time change through the year.
        now = datetime.now(timezone.utc)
        system = system_timezone_name()
        for name in timezone_names():
            offset = format_offset(now.astimezone(get_timezone(name)), numeric_zero=True)
            label = f"{name} ({offset})"
            if name == system:
                label += " (system)"
            self.addItem(label, name)

        completer = QCompleter(self.model(), self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.setCompleter(completer)

        self._name = system
        self.setCurrentIndex(self.findData(self._name))

        self.activated.connect(self._on_activated)
        self.lineEdit().editingFinished.connect(self._on_editing_finished)

    def current_timezone(self) -> tzinfo:
        return get_timezone(self._name)

    def timezone_name(self) -> str:
        return self._name

    def select_timezone(self, name: str) -> None:
        """Select a timezone by IANA name; emits :attr:`timezoneChanged` if it changes."""
        if self.findData(name) >= 0:
            self._select(name)

    def _on_activated(self, index: int) -> None:
        self._select(self.itemData(index))

    def _on_editing_finished(self) -> None:
        text = self.currentText().split(" (", 1)[0].strip()
        index = self.findData(text, flags=Qt.MatchFlag.MatchFixedString)
        if index >= 0:
            self._select(self.itemData(index))
        else:  # unknown text: restore the current choice
            self.setCurrentIndex(self.findData(self._name))

    def _select(self, name: str) -> None:
        self.setCurrentIndex(self.findData(name))
        if name == self._name:
            return
        self._name = name
        self.timezoneChanged.emit(get_timezone(name))
