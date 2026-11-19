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
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from naturelog.gps import GpsLogError, Track, load_track, position_on_tracks, tracks_span
from naturelog.inaturalist import ObservationHint, observation_hint
from naturelog.media import MediaFile
from naturelog.species import DetectionConfig
from naturelog.session import (
    Session,
    SessionError,
    SessionMedia,
    can_use_relative_paths,
    load_session,
    save_session,
)
from naturelog.timezones import format_datetime
from naturelog.ui.file_datetime_window import FileDatetimeWindow
from naturelog.ui.inaturalist_window import InaturalistWindow
from naturelog.ui.map_view import MapView, local_files
from naturelog.ui.media_sidebar import MediaSidebar
from naturelog.ui.preview_window import PreviewWindow
from naturelog.ui.species_panel import SpeciesPanel
from naturelog.ui.track_sidebar import TrackSidebar, track_colors
from naturelog.ui.timeline import TimelineMarker, TimelineWidget
from naturelog.ui.timezone_selector import TimezoneSelector, system_timezone_name

_FILE_FILTER = (
    "GPS logs (*.gpx *.nmea *.nma *.log *.txt);;"
    "GPX (*.gpx);;NMEA 0183 (*.nmea *.nma *.log *.txt);;All files (*)"
)
_NO_TRACK_TEXT = "No GPS log loaded"
_SESSION_SUFFIX = ".naturelog.json"
_SESSION_FILTER = "NatureLog sessions (*.naturelog.json *.json);;All files (*)"
_SIDEBAR_PAGES = ("Observations", "Tracks")
# Sidebar sections look like tabs (underlined, larger text), not like action buttons.
_SECTION_SWITCH_STYLE = """
#sectionSwitch { border-bottom: 1px solid palette(mid); }
#sectionSwitch QPushButton {
    background: transparent;
    border: none;
    border-bottom: 3px solid transparent;
    padding: 8px 4px 6px 4px;
    font-size: 15px;
    color: palette(window-text);
}
#sectionSwitch QPushButton:hover { background: palette(alternate-base); }
#sectionSwitch QPushButton:checked {
    font-weight: bold;
    color: palette(highlight);
    border-bottom: 3px solid palette(highlight);
}
"""


def session_file() -> Path:
    """Where the last session is kept (per-user application data folder)."""
    folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    return Path(folder) / "session.json"


class _LoadSignals(QObject):
    loaded = Signal(object, object)  # list[Track], list[str] error messages


class _LoadTask(QRunnable):
    """Parses GPS logs off the UI thread; a file that fails does not stop the others."""

    def __init__(self, paths: list[str]) -> None:
        super().__init__()
        self.paths = paths
        self.signals = _LoadSignals()

    def run(self) -> None:
        tracks: list[Track] = []
        errors: list[str] = []
        for path in self.paths:
            try:
                tracks.append(load_track(path))
            except GpsLogError as exc:
                errors.append(str(exc))
            except Exception as exc:  # report instead of dying silently in a worker
                errors.append(f"{Path(path).name}: unexpected error: {exc}")
        self.signals.loaded.emit(tracks, errors)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("NatureLog")
        self.resize(1400, 820)
        self.setAcceptDrops(True)

        #: Loaded tracks sorted by start time; on overlap the earlier one gives positions.
        self._tracks: list[Track] = []
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
            "Clear the GPS logs, media list and settings (files on disk are not changed)"
        )
        self._new_session_button.clicked.connect(self._confirm_new_session)
        self._open_session_button = QPushButton("Open session…")
        self._open_session_button.setToolTip("Replace the current session with a saved session file")
        self._open_session_button.clicked.connect(self._open_session_file)
        self._save_session_button = QPushButton("Save session as…")
        self._save_session_button.setToolTip(
            "Save GPS logs, media list, coordinates and settings to a session file"
        )
        self._save_session_button.clicked.connect(self._save_session_file)
        #: Session file last opened or saved by the user (suggested by the dialogs).
        self._session_file: Path | None = None
        #: Path style last chosen when saving a session file.
        self._relative_paths = True
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

        # Session-wide actions live in a fixed bar at the very top of the window.
        top_bar = QToolBar("Main")
        top_bar.setObjectName("mainToolBar")
        top_bar.setMovable(False)
        top_bar.setFloatable(False)
        top_bar.toggleViewAction().setVisible(False)
        top_bar.addWidget(self._new_session_button)
        top_bar.addWidget(self._open_session_button)
        top_bar.addWidget(self._save_session_button)
        top_bar.addWidget(self._file_datetime_button)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, top_bar)

        top = QHBoxLayout()
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
        self._preview: PreviewWindow | None = None
        self._media.previewRequested.connect(self._show_preview)
        self._media.detectionsChanged.connect(self._refresh_preview_species)
        self._map.moveRequested.connect(self._media.move_selected_to)
        self._media.positionsChanged.connect(self._refresh_map_markers)
        self._media.positionsChanged.connect(self._schedule_save)

        self._track_panel = TrackSidebar()
        self._track_panel.addRequested.connect(self._choose_file)
        self._track_panel.pathsDropped.connect(self._load_files)
        self._track_panel.removeRequested.connect(self._remove_tracks)
        self._track_panel.selectionChanged.connect(self._map.highlight_tracks)
        self._track_panel.set_timezone(self._tz)

        self._species = SpeciesPanel()
        self._species.message.connect(self.statusBar().showMessage)
        self._species.detectRequested.connect(self._detect_species)
        self._species.configChanged.connect(self._schedule_save)
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
        splitter.addWidget(self._build_sidebar())
        splitter.addWidget(content)
        splitter.addWidget(self._species)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([340, 820, 240])
        self.setCentralWidget(splitter)

        self._restore_session()

    # --- sidebar --------------------------------------------------------

    def _build_sidebar(self) -> QWidget:
        """Left sidebar: a switch between the observations (media) and tracks panels."""
        stack = QStackedWidget()
        stack.addWidget(self._media)
        stack.addWidget(self._track_panel)
        group = QButtonGroup(self)
        group.setExclusive(True)
        switch = QWidget()
        switch.setObjectName("sectionSwitch")
        switch.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        switch.setStyleSheet(_SECTION_SWITCH_STYLE)
        switch_layout = QHBoxLayout(switch)
        switch_layout.setContentsMargins(0, 0, 0, 0)
        switch_layout.setSpacing(0)
        for index, label in enumerate(_SIDEBAR_PAGES):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            group.addButton(button, index)
            switch_layout.addWidget(button, 1)
        group.idToggled.connect(lambda index, on: on and stack.setCurrentIndex(index))
        self._sidebar_buttons = group

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(switch)
        layout.addWidget(stack, 1)
        sidebar = QWidget()
        sidebar.setLayout(layout)
        return sidebar

    # --- loading --------------------------------------------------------

    def _choose_file(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add GPS logs", "", _FILE_FILTER)
        if paths:
            self._load_files(paths)

    def _load_files(self, paths: list[str]) -> None:
        if self._task is not None:
            self.statusBar().showMessage("GPS logs are already being loaded")
            return
        if len(paths) == 1:
            message = f"Loading {Path(paths[0]).name}…"
        else:
            message = f"Loading {len(paths)} GPS logs…"
        self.statusBar().showMessage(message)
        self._new_session_button.setEnabled(False)
        self._open_session_button.setEnabled(False)
        self._task = _LoadTask(paths)
        self._task.signals.loaded.connect(self._on_loaded)
        QThreadPool.globalInstance().start(self._task)

    def _finish_loading(self) -> None:
        self._task = None
        self._new_session_button.setEnabled(True)
        self._open_session_button.setEnabled(True)
        self._schedule_save()

    def _on_loaded(self, tracks: list[Track], errors: list[str]) -> None:
        """Add newly read tracks; a log loaded again replaces its earlier copy."""
        self._finish_loading()
        if tracks:
            reloaded = {t.source_path for t in tracks}
            kept = [t for t in self._tracks if t.source_path not in reloaded]
            self._set_tracks(kept + tracks)
            if len(tracks) == 1:
                message = f"Loaded {tracks[0].source_path.name}"
            else:
                message = f"Loaded {len(tracks)} GPS logs"
            skipped = sum(t.skipped_points for t in tracks)
            if skipped:
                message += f" – {skipped:,} points without usable timestamp skipped"
            self.statusBar().showMessage(message)
            self._timeline.setFocus()
        else:
            self.statusBar().clearMessage()
        if errors:
            QMessageBox.warning(
                self,
                "Cannot load GPS log" if len(errors) == 1 else "Cannot load some GPS logs",
                "\n\n".join(errors),
            )

    def _remove_tracks(self, paths: list[Path]) -> None:
        removed = set(paths)
        remaining = [t for t in self._tracks if t.source_path not in removed]
        count = len(self._tracks) - len(remaining)
        self._set_tracks(remaining, fit=False)
        self.statusBar().showMessage(
            f"Removed {count} track{'s' if count != 1 else ''} (files on disk are not changed)"
        )

    def _set_tracks(self, tracks: list[Track], fit: bool = True) -> None:
        """Show these tracks everywhere and recompute media positions from them."""
        self._tracks = sorted(tracks, key=lambda t: t.start)
        self._map.set_tracks(self._tracks, track_colors(len(self._tracks)), fit=fit)
        self._media.set_tracks(self._tracks)
        self._track_panel.set_tracks(self._tracks)
        self._timeline.set_tracks(self._tracks)
        if not self._tracks:
            self._position_label.setText(" ")
        self._update_track_label()
        self._update_timeline_markers()
        self._show_media_on_track(self._media.selected_files(), report=False)
        self._schedule_save()

    def _update_track_label(self) -> None:
        tracks = self._tracks
        if not tracks:
            self._track_label.setText(_NO_TRACK_TEXT)
            return
        start, end = tracks_span(tracks)
        if len(tracks) == 1:
            track = tracks[0]
            source = "NMEA 0183 → GPX" if track.source_format == "nmea" else "GPX"
            title, details = track.name, f"{source} · {track.point_count:,} points"
        else:
            points = sum(t.point_count for t in tracks)
            title, details = f"{len(tracks)} GPS logs", f"{points:,} points"
        self._track_label.setText(
            f"<b>{title}</b><br>{details}<br>"
            f"{format_datetime(start, self._tz)} – {format_datetime(end, self._tz)}"
        )

    # --- timezone -------------------------------------------------------

    def _on_timezone_changed(self, tz: tzinfo) -> None:
        self._tz = tz
        self._schedule_save()
        self._update_track_label()
        self._media.set_timezone(tz)
        self._track_panel.set_timezone(tz)
        if self._preview is not None:
            self._preview.set_timezone(tz)
            self._update_preview_position()  # the list is re-sorted by capture time
        self._show_media_on_track(self._media.selected_files())
        self._timeline.set_timezone(tz)
        self._update_timeline_markers()
        current = self._timeline.current_time()
        if current is not None:
            self._on_time_changed(current)

    # --- timeline -------------------------------------------------------

    def _on_time_changed(self, when: datetime) -> None:
        if not self._tracks:
            return
        stamp = format_datetime(when, self._tz)
        position = position_on_tracks(self._tracks, when)
        if position is None:
            self._map.clear_position()
            self._position_label.setText(f"{stamp} · no GPS position (gap between or in tracks)")
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
        """Place every listed file on the map at its position.

        That is where the user moved it, else its interpolated capture position
        on the tracks. ``files`` are the selected ones: they are drawn green,
        above the others. EXIF capture times are read in the selected timezone.
        Files outside the track or in a gap get no marker (no extrapolation).
        """
        selected = {media.path for media in files}
        markers = []
        outside = 0
        for media in self._media.media_files():
            when = media.capture_time(self._tz)
            track_position = self._media.track_position_of(media)
            moved = self._media.moved_position(media)
            if moved is None and track_position is None:
                outside += media.path in selected and bool(self._tracks)
                continue
            lat, lon = moved or (track_position.latitude, track_position.longitude)
            label = f"{media.name}\n{format_datetime(when, self._tz)}"
            if moved is not None:
                label += "\nmoved on the map"
            markers.append({
                "lat": lat,
                "lon": lon,
                "kind": "image" if media.kind.is_image else "file",
                "label": label,
                "selected": media.path in selected,
                "moved": moved is not None,
                "orig": None
                if moved is None or track_position is None
                else [track_position.latitude, track_position.longitude],
            })  # fmt: skip
        self._map.set_media_markers(markers)
        if not report:
            return
        if not self._tracks and files and not any(m["selected"] for m in markers):
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
        """iNaturalist card values: session location, plus date and top species for recordings."""
        return observation_hint(
            media,
            self._tz,
            self._tracks,
            self._media.detections_for(media),
            self._media.moved_position(media),
        )

    def _show_preview(self, media: MediaFile) -> None:
        """Open (or raise) the preview window showing ``media``."""
        if self._preview is None:
            self._preview = PreviewWindow(self)
            self._preview.navigateRequested.connect(self._navigate_preview)
            self._media.filesAdded.connect(self._update_preview_position)
            self._media.filesRemoved.connect(self._update_preview_position)
        self._preview.show_media(media, self._tz, self._media.detections_for(media))
        self._update_preview_position()
        self._preview.show()
        self._preview.raise_()
        self._preview.activateWindow()

    def _navigate_preview(self, step: int) -> None:
        """Preview the previous/next file of the list and select it there too."""
        current = self._preview.media() if self._preview is not None else None
        row = self._media.row_of(current) if current is not None else None
        target = self._media.file_at(row + step) if row is not None else None
        if target is None:
            return
        self._media.select_paths([target.path])
        self._preview.show_media(target, self._tz, self._media.detections_for(target))
        self._update_preview_position()

    def _update_preview_position(self, *_args) -> None:
        preview = self._preview
        if preview is not None and preview.media() is not None:
            preview.set_position(self._media.row_of(preview.media()), self._media.count())

    def _refresh_preview_species(self) -> None:
        preview = self._preview
        if preview is not None and preview.isVisible() and preview.media() is not None:
            preview.set_predictions(self._media.detections_for(preview.media()))

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
        """Settings, GPS logs and listed media with the coordinates assigned to them."""
        track_paths = [t.source_path for t in self._tracks if t.source_path is not None]
        if self._task is not None:  # logs being loaded are part of the session too
            track_paths += [Path(p) for p in self._task.paths if Path(p) not in track_paths]
        media = [
            SessionMedia(
                m,
                self._media.position_of(m),
                self._media.detections_for(m),
                self._media.moved_position(m),
            )
            for m in self._media.media_files()
        ]
        return Session(
            timezone=self._tz_selector.timezone_name(),
            track_paths=track_paths,
            media=media,
            detection=self._species.config(),
        )

    def _schedule_save(self, *_args) -> None:
        self._save_timer.start()

    def _save_session(self) -> None:
        try:
            save_session(self._current_session(), self._session_path)
        except OSError as exc:
            self.statusBar().showMessage(f"Cannot save session: {exc.strerror or exc}")

    def _restore_session(self) -> None:
        """Reopen the GPS logs and media of the last session."""
        try:
            session = load_session(self._session_path)
        except SessionError as exc:
            self.statusBar().showMessage(f"{exc} – starting a new session")
            return
        if session is not None:
            self._apply_session(session, "Restored last session")

    def _apply_session(self, session: Session, message: str) -> None:
        """Load settings, media and GPS logs of ``session`` into an empty window."""
        self._tz_selector.select_timezone(session.timezone)
        self._species.set_config(session.detection)
        files = [entry.media for entry in session.media if entry.media.path.is_file()]
        missing = len(session.media) - len(files)
        self._media.add_media(files)
        self._media.set_detections({
            entry.media.path: entry.detections
            for entry in session.media
            if entry.detections is not None
        })  # fmt: skip
        self._media.set_moved_positions({
            entry.media.path: entry.moved_to
            for entry in session.media
            if entry.moved_to is not None
        })  # fmt: skip

        notes = []
        if missing:
            notes.append(f"{missing} media file{'s' if missing != 1 else ''} not found")
        track_paths = [p for p in session.track_paths if p.is_file()]
        notes += [f"GPS log {p.name} not found" for p in session.track_paths if not p.is_file()]
        if track_paths:
            self._load_files([str(p) for p in track_paths])
        if notes:
            message += " – " + ", ".join(notes)
        self.statusBar().showMessage(message)

    def _confirm_new_session(self) -> None:
        if not self._tracks and not self._media.media_files():
            self._new_session()
            return
        answer = QMessageBox.question(
            self,
            "New session",
            "Start a new session? The GPS logs, the media list and the coordinates "
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
        self._clear_session()
        self._session_file = None
        self._save_timer.stop()
        self._save_session()
        self.statusBar().showMessage("New session started")

    def _clear_session(self) -> None:
        """Remove GPS logs and media, and reset the settings to their defaults."""
        self._tracks = []
        self._species.cancel()
        self._media.clear()
        self._media.set_tracks([])
        self._track_panel.set_tracks([])
        self._map.clear()
        self._timeline.set_tracks([])
        self._timeline.set_markers([])
        self._track_label.setText(_NO_TRACK_TEXT)
        self._position_label.setText(" ")
        self._tz_selector.select_timezone(system_timezone_name())
        self._species.set_config(DetectionConfig())

    def _open_session_file(self) -> None:
        """Replace the current session with one read from a session file."""
        if self._task is not None:  # button is disabled while a log loads
            return
        start = str(self._session_file.parent) if self._session_file else ""
        name, _ = QFileDialog.getOpenFileName(
            self, "Open session", start, _SESSION_FILTER
        )
        if not name:
            return
        path = Path(name)
        try:
            session = load_session(path)
        except SessionError as exc:
            QMessageBox.warning(self, "Open session", str(exc))
            return
        if session is None:
            QMessageBox.warning(self, "Open session", f"{path.name} not found")
            return
        if self._tracks or self._media.media_files():
            answer = QMessageBox.question(
                self,
                "Open session",
                f"Replace the current session with {path.name}? Save it first if you "
                "want to keep it. Files on disk are not changed.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._clear_session()
        self._session_file = path
        self._apply_session(session, f"Opened session {path.name}")
        self._schedule_save()

    def _save_session_file(self) -> None:
        """Save the current session to a file the user chooses, with the path style chosen there."""
        chosen = self._choose_session_save_path()
        if chosen is None:
            return
        path, relative = chosen
        if not path.name.endswith(_SESSION_SUFFIX) and path.suffix.lower() != ".json":
            path = path.with_name(path.name + _SESSION_SUFFIX)
        session = self._current_session()
        try:
            save_session(session, path, relative_paths=relative)
        except OSError as exc:
            QMessageBox.warning(self, "Save session", f"Cannot save session: {exc.strerror or exc}")
            return
        self._session_file = path
        self._relative_paths = relative
        message = f"Saved session {path.name} ({'relative' if relative else 'absolute'} paths)"
        if relative and not can_use_relative_paths(session, path.parent):
            message += " – files on another drive are stored with absolute paths"
        self.statusBar().showMessage(message)

    def _choose_session_save_path(self) -> tuple[Path, bool] | None:
        """Save dialog with a choice of relative or absolute file paths.

        Returns the chosen file and whether paths are stored relative to it;
        ``None`` if cancelled. Qt's own dialog is used because native dialogs
        cannot hold the extra option.
        """
        dialog = QFileDialog(self, "Save session")
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setNameFilter(_SESSION_FILTER)
        dialog.setDefaultSuffix(_SESSION_SUFFIX.lstrip("."))
        if self._session_file is not None:
            dialog.setDirectory(str(self._session_file.parent))
            dialog.selectFile(self._session_file.name)
        else:
            dialog.selectFile("session" + _SESSION_SUFFIX)

        paths = QComboBox()
        paths.addItem("Relative to the session file", True)
        paths.addItem("Absolute", False)
        paths.setCurrentIndex(0 if self._relative_paths else 1)
        paths.setToolTip(
            "Relative: the session keeps working when its folder is moved or copied "
            "together with the GPS logs and media (e.g. to another computer).\n"
            "Absolute: the session keeps working when only the session file is moved, "
            "as long as the GPS logs and media stay where they are."
        )
        layout = dialog.layout()
        if isinstance(layout, QGridLayout):
            row = layout.rowCount()
            layout.addWidget(QLabel("File paths:"), row, 0)
            layout.addWidget(paths, row, 1)
        else:  # unexpected dialog layout: still offer the option
            dialog.layout().addWidget(paths)

        if dialog.exec() != QFileDialog.DialogCode.Accepted or not dialog.selectedFiles():
            return None
        return Path(dialog.selectedFiles()[0]), bool(paths.currentData())

    # --- drag and drop --------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if local_files(event):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = local_files(event)
        if paths:
            event.acceptProposedAction()
            self._load_files(paths)
