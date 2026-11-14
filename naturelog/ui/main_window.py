"""Main application window."""

from __future__ import annotations

from datetime import datetime, tzinfo
from pathlib import Path

from PySide6.QtCore import (
    QObject,
    QRunnable,
    QStandardPaths,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from naturelog.gps import GpsLogError, Track, load_track, position_at
from naturelog.inaturalist import ObservationHint, observation_hint
from naturelog.media import MediaFile
from naturelog.session import (
    Session,
    SessionError,
    SessionMedia,
    load_session,
    save_session,
)
from naturelog.timezones import format_datetime
from naturelog.ui.file_datetime_window import FileDatetimeWindow
from naturelog.ui.inaturalist_window import InaturalistWindow
from naturelog.ui.map_view import MapView, local_files
from naturelog.ui.media_sidebar import MediaSidebar
from naturelog.ui.species_panel import SpeciesPanel
from naturelog.ui.timeline import TimelineMarker, TimelineWidget
from naturelog.ui.timezone_selector import TimezoneSelector, system_timezone_name

_FILE_FILTER = (
    "GPS logs (*.gpx *.nmea *.nma *.log *.txt);;"
    "GPX (*.gpx);;NMEA 0183 (*.nmea *.nma *.log *.txt);;All files (*)"
)
_NO_TRACK_TEXT = "No GPS log loaded"


def session_file() -> Path:
    """Where the last session is kept (per-user application data folder)."""
    folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    return Path(folder) / "session.json"


class _LoadSignals(QObject):
    loaded = Signal(object)  # Track
    failed = Signal(str)


class _LoadTask(QRunnable):
    """Parses a GPS log off the UI thread."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path
        self.signals = _LoadSignals()

    def run(self) -> None:
        try:
            track = load_track(self.path)
        except GpsLogError as exc:
            self.signals.failed.emit(str(exc))
        except Exception as exc:  # report instead of dying silently in a worker
            self.signals.failed.emit(f"{Path(self.path).name}: unexpected error: {exc}")
        else:
            self.signals.loaded.emit(track)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("NatureLog")
        self.resize(1400, 820)
        self.setAcceptDrops(True)

        self._track: Track | None = None
        self._task: _LoadTask | None = None
        self._inaturalist: InaturalistWindow | None = None

        self._session_path = session_file()
        # Save shortly after each change, so a crash loses at most the last edit.
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(1000)
        self._save_timer.timeout.connect(self._save_session)

        self._new_session_button = QPushButton("New session")
        self._new_session_button.setToolTip(
            "Clear the GPS log, media list and settings (files on disk are not changed)"
        )
        self._new_session_button.clicked.connect(self._confirm_new_session)
        self._open_button = QPushButton("Open GPS log…")
        self._open_button.clicked.connect(self._choose_file)
        self._file_datetime: FileDatetimeWindow | None = None
        self._file_datetime_button = QPushButton("Map file datetime")
        self._file_datetime_button.setToolTip(
            "Copy created/modified dates from source files to matching derived files"
        )
        self._file_datetime_button.clicked.connect(self._show_file_datetime)
        self._track_label = QLabel(_NO_TRACK_TEXT)
        # Wrap long track names instead of widening the window.
        self._track_label.setWordWrap(True)

        self._tz_selector = TimezoneSelector()
        self._tz_selector.timezoneChanged.connect(self._on_timezone_changed)
        self._tz = self._tz_selector.current_timezone()

        top = QHBoxLayout()
        top.addWidget(self._new_session_button)
        top.addWidget(self._open_button)
        top.addWidget(self._file_datetime_button)
        top.addWidget(self._track_label, 1)
        top.addWidget(QLabel("Timezone:"))
        top.addWidget(self._tz_selector)

        self._map = MapView()
        self._map.filesDropped.connect(self._load_files)

        self._position_label = QLabel(" ")
        self._timeline = TimelineWidget()
        self._timeline.set_timezone(self._tz)
        self._timeline.timeChanged.connect(self._on_time_changed)

        layout = QVBoxLayout()
        layout.addLayout(top)
        layout.addWidget(self._map, 1)
        layout.addWidget(self._position_label)
        layout.addWidget(self._timeline)
        content = QWidget()
        content.setLayout(layout)

        self._media = MediaSidebar()
        self._media.set_timezone(self._tz)
        self._media.message.connect(self.statusBar().showMessage)
        self._media.selectionChanged.connect(self._show_media_on_track)
        self._media.selectionChanged.connect(self._update_timeline_markers)
        self._media.filesAdded.connect(self._update_timeline_markers)
        self._media.filesRemoved.connect(self._update_timeline_markers)
        self._media.filesAdded.connect(self._refresh_map_markers)
        self._media.filesRemoved.connect(self._refresh_map_markers)
        self._media.filesAdded.connect(self._schedule_save)
        self._media.filesRemoved.connect(self._schedule_save)
        # Previews arrive one by one during large imports: rebuild the timeline
        # markers at most every 200 ms instead of once per thumbnail.
        self._markers_timer = QTimer(self)
        self._markers_timer.setSingleShot(True)
        self._markers_timer.setInterval(200)
        self._markers_timer.timeout.connect(self._update_timeline_markers)
        self._media.iconChanged.connect(self._schedule_marker_update)
        self._timeline.markersClicked.connect(self._media.select_paths)
        self._media.uploadRequested.connect(self._show_inaturalist)

        self._species = SpeciesPanel()
        self._species.message.connect(self.statusBar().showMessage)
        self._species.detectRequested.connect(self._detect_species)
        self._species.detected.connect(self._media.set_detections)
        self._species.speciesActivated.connect(self._media.select_paths)
        self._media.detectRequested.connect(self._species.detect)
        self._media.detectionsChanged.connect(self._schedule_save)
        # Results arrive file by file: refresh the species list at most every 300 ms.
        self._species_timer = QTimer(self)
        self._species_timer.setSingleShot(True)
        self._species_timer.setInterval(300)
        self._species_timer.timeout.connect(
            lambda: self._species.set_detections(self._media.detections())
        )
        self._media.detectionsChanged.connect(self._species_timer.start)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._media)
        splitter.addWidget(content)
        splitter.addWidget(self._species)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([340, 820, 240])
        self.setCentralWidget(splitter)

        self._restore_session()

    # --- loading --------------------------------------------------------

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open GPS log", "", _FILE_FILTER)
        if path:
            self._load_files([path])

    def _load_files(self, paths: list[str]) -> None:
        if self._task is not None:
            return
        path = paths[0]
        message = f"Loading {Path(path).name}…"
        if len(paths) > 1:
            message += f" (only the first of {len(paths)} dropped files is loaded)"
        self.statusBar().showMessage(message)
        self._open_button.setEnabled(False)
        self._new_session_button.setEnabled(False)
        self._task = _LoadTask(path)
        self._task.signals.loaded.connect(self._on_loaded)
        self._task.signals.failed.connect(self._on_failed)
        QThreadPool.globalInstance().start(self._task)

    def _finish_loading(self) -> None:
        self._task = None
        self._open_button.setEnabled(True)
        self._new_session_button.setEnabled(True)
        self._schedule_save()

    def _on_failed(self, message: str) -> None:
        self._finish_loading()
        self.statusBar().clearMessage()
        QMessageBox.warning(self, "Cannot load GPS log", message)

    def _on_loaded(self, track: Track) -> None:
        self._finish_loading()
        self._track = track
        self._update_track_label()
        message = f"Loaded {track.source_path.name}"
        if track.skipped_points:
            message += f" – {track.skipped_points:,} points without usable timestamp skipped"
        self.statusBar().showMessage(message)
        self._map.set_track(track)
        self._media.set_track(track)
        self._timeline.set_track(track)
        self._update_timeline_markers()
        self._show_media_on_track(self._media.selected_files())
        self._timeline.setFocus()

    def _update_track_label(self) -> None:
        track = self._track
        if track is None:
            return
        source = "NMEA 0183 → GPX" if track.source_format == "nmea" else "GPX"
        self._track_label.setText(
            f"<b>{track.name}</b><br>"
            f"{source} · {track.point_count:,} points<br>"
            f"{format_datetime(track.start, self._tz)} – {format_datetime(track.end, self._tz)}"
        )

    # --- timezone -------------------------------------------------------

    def _on_timezone_changed(self, tz: tzinfo) -> None:
        self._tz = tz
        self._schedule_save()
        self._update_track_label()
        self._media.set_timezone(tz)
        self._show_media_on_track(self._media.selected_files())
        self._timeline.set_timezone(tz)
        self._update_timeline_markers()
        current = self._timeline.current_time()
        if current is not None:
            self._on_time_changed(current)

    # --- timeline -------------------------------------------------------

    def _on_time_changed(self, when: datetime) -> None:
        if self._track is None:
            return
        stamp = format_datetime(when, self._tz)
        position = position_at(self._track, when)
        if position is None:
            self._map.clear_position()
            self._position_label.setText(f"{stamp} · no GPS position (gap in track)")
            return
        self._map.set_position(position.latitude, position.longitude)
        parts = [stamp, f"{position.latitude:.6f}°, {position.longitude:.6f}°"]
        if position.elevation is not None:
            parts.append(f"{position.elevation:.0f} m")
        parts.append("interpolated" if position.interpolated else "recorded fix")
        self._position_label.setText(" · ".join(parts))

    # --- species --------------------------------------------------------

    def _detect_species(self) -> None:
        """Detect species in the selected photos/recordings, or in all not yet analysed."""
        selected = self._media.selected_files()
        if selected:
            files = selected
        else:
            files = [m for m in self._media.media_files() if self._media.detections_for(m) is None]
        self._species.detect(files)

    # --- media on track -------------------------------------------------

    def _refresh_map_markers(self, *_args) -> None:
        self._show_media_on_track(self._media.selected_files(), report=False)

    def _show_media_on_track(self, files: list[MediaFile], report: bool = True) -> None:
        """Place every listed file on the track at its interpolated capture position.

        ``files`` are the selected ones: they are drawn green, above the others.
        EXIF capture times are read in the selected timezone.
        Files outside the track or in a gap get no marker (no extrapolation).
        """
        selected = {media.path for media in files}
        markers = []
        outside = 0
        for media in self._media.media_files() if self._track is not None else []:
            when = media.capture_time(self._tz)
            position = position_at(self._track, when)
            if position is None:
                outside += media.path in selected
                continue
            markers.append({
                "lat": position.latitude,
                "lon": position.longitude,
                "kind": "image" if media.kind.is_image else "file",
                "label": f"{media.name}\n{format_datetime(when, self._tz)}",
                "selected": media.path in selected,
            })  # fmt: skip
        self._map.set_media_markers(markers)
        if not report:
            return
        if self._track is None and files:
            self.statusBar().showMessage("Load a GPS log to place media on the track")
        elif outside:
            self.statusBar().showMessage(
                f"{outside} of {len(files)} selected file(s) outside the track time range – not placed"
            )

    def _schedule_marker_update(self, *_args) -> None:
        if not self._markers_timer.isActive():
            self._markers_timer.start()

    def _update_timeline_markers(self, *_args) -> None:
        """Show every listed file on the timeline at its capture time (selected tz)."""
        selected = {media.path for media in self._media.selected_files()}
        self._timeline.set_markers([
            TimelineMarker(
                when=(when := media.capture_time(self._tz)),
                icon=self._media.icon_for(media),
                label=f"{media.name} · {format_datetime(when, self._tz)}",
                selected=media.path in selected,
                key=media.path,
            )
            for media in self._media.media_files()
        ])  # fmt: skip

    def _show_inaturalist(self, files: list[MediaFile]) -> None:
        """Open (or raise) the iNaturalist upload window; it follows the media selection."""
        if self._inaturalist is None:
            self._inaturalist = InaturalistWindow(self, self._observation_hint)
            self._inaturalist.message.connect(self.statusBar().showMessage)
            self._media.selectionChanged.connect(self._inaturalist.set_files)
        self._inaturalist.set_files(files)
        self._inaturalist.show()
        self._inaturalist.raise_()
        self._inaturalist.activateWindow()

    def _observation_hint(self, media: MediaFile) -> ObservationHint | None:
        """Date, track position and top species to fill in a recording's iNaturalist card."""
        return observation_hint(
            media, self._tz, self._track, self._media.detections_for(media)
        )

    def _show_file_datetime(self) -> None:
        """Open (or raise) the window that maps file dates from source to target files."""
        if self._file_datetime is None:
            self._file_datetime = FileDatetimeWindow(self)
            self._file_datetime.message.connect(self.statusBar().showMessage)
        self._file_datetime.show()
        self._file_datetime.raise_()
        self._file_datetime.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._inaturalist is not None:
            self._inaturalist.close()
        if self._file_datetime is not None:
            self._file_datetime.close()
        self._media.shutdown()
        self._species.cancel()
        self._save_timer.stop()
        self._save_session()
        super().closeEvent(event)

    # --- session --------------------------------------------------------

    def _current_session(self) -> Session:
        """Settings, GPS log and listed media with the coordinates assigned to them."""
        track_path = self._track.source_path if self._track is not None else None
        if self._task is not None:  # a log being loaded replaces the current one
            track_path = Path(self._task.path)
        media = [
            SessionMedia(
                m,
                position_at(self._track, m.capture_time(self._tz))
                if self._track is not None
                else None,
                self._media.detections_for(m),
            )
            for m in self._media.media_files()
        ]
        return Session(timezone=self._tz_selector.timezone_name(), track_path=track_path, media=media)

    def _schedule_save(self, *_args) -> None:
        self._save_timer.start()

    def _save_session(self) -> None:
        try:
            save_session(self._current_session(), self._session_path)
        except OSError as exc:
            self.statusBar().showMessage(f"Cannot save session: {exc.strerror or exc}")

    def _restore_session(self) -> None:
        """Reopen the GPS log and media of the last session."""
        try:
            session = load_session(self._session_path)
        except SessionError as exc:
            self.statusBar().showMessage(f"{exc} – starting a new session")
            return
        if session is None:
            return
        self._tz_selector.select_timezone(session.timezone)
        files = [entry.media for entry in session.media if entry.media.path.is_file()]
        missing = len(session.media) - len(files)
        self._media.add_media(files)
        self._media.set_detections({
            entry.media.path: entry.detections
            for entry in session.media
            if entry.detections is not None
        })  # fmt: skip

        notes = []
        if missing:
            notes.append(f"{missing} media file{'s' if missing != 1 else ''} not found")
        track_path = session.track_path
        if track_path is not None and not track_path.is_file():
            notes.append(f"GPS log {track_path.name} not found")
            track_path = None
        if track_path is not None:
            self._load_files([str(track_path)])
        message = "Restored last session"
        if notes:
            message += " – " + ", ".join(notes)
        self.statusBar().showMessage(message)

    def _confirm_new_session(self) -> None:
        if self._track is None and not self._media.media_files():
            self._new_session()
            return
        answer = QMessageBox.question(
            self,
            "New session",
            "Start a new session? The GPS log, the media list and the coordinates "
            "assigned to them are cleared. Files on disk are not changed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._new_session()

    def _new_session(self) -> None:
        """Clear everything and return to default settings."""
        if self._task is not None:  # button is disabled while a log loads
            return
        self._track = None
        self._species.cancel()
        self._media.clear()
        self._media.set_track(None)
        self._map.clear()
        self._timeline.set_track(None)
        self._timeline.set_markers([])
        self._track_label.setText(_NO_TRACK_TEXT)
        self._position_label.setText(" ")
        self._tz_selector.select_timezone(system_timezone_name())
        self._save_timer.stop()
        self._save_session()
        self.statusBar().showMessage("New session started")

    # --- drag and drop --------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if local_files(event):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = local_files(event)
        if paths:
            event.acceptProposedAction()
            self._load_files(paths)
