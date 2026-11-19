"""Leaflet map embedded in a Qt WebEngine view."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

from naturelog import __version__
from naturelog.gps.models import Track

_RESOURCES = Path(__file__).parent / "resources"


def _map_html() -> str:
    """The map page with Leaflet inlined, so it needs no file access to load."""
    leaflet = _RESOURCES / "leaflet"
    return (
        (_RESOURCES / "map.html")
        .read_text(encoding="utf-8")
        .replace("/*LEAFLET_CSS*/", (leaflet / "leaflet.css").read_text(encoding="utf-8"))
        .replace("/*LEAFLET_JS*/", (leaflet / "leaflet.js").read_text(encoding="utf-8"))
    )


def local_files(event: QDropEvent) -> list[str]:
    """Paths of the local files carried by a drag-and-drop event."""
    mime = event.mimeData()
    if not mime.hasUrls():
        return []
    return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]


#: Prefix of console lines the map page uses to send messages to the application.
_MESSAGE_PREFIX = "naturelog:"


class _MapPage(QWebEnginePage):
    """Receives messages the page logs as ``naturelog:{json}`` console lines."""

    message = Signal(dict)

    def javaScriptConsoleMessage(self, level, text: str, line: int, source: str) -> None:
        if not text.startswith(_MESSAGE_PREFIX):
            return
        try:
            data = json.loads(text[len(_MESSAGE_PREFIX):])
        except ValueError:
            return
        if isinstance(data, dict):
            self.message.emit(data)


class MapView(QWebEngineView):
    """Shows GPS tracks and a movable position marker on a Leaflet map."""

    #: Local file paths dropped onto the map.
    filesDropped = Signal(list)
    #: (latitude, longitude) clicked with the move tool active.
    moveRequested = Signal(float, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._ready = False
        self._queue: list[str] = []
        page = _MapPage(self)
        page.message.connect(self._on_message)
        self.setPage(page)
        # Tile servers require an identifying user agent.
        self.page().profile().setHttpUserAgent(f"NatureLog/{__version__} (QtWebEngine)")
        self.loadFinished.connect(self._on_load_finished)
        self.setHtml(_map_html())

    def set_tracks(self, tracks: list[Track], colors: list[str], fit: bool = True) -> None:
        """Draw every track in its colour; ``fit`` zooms the map to all of them."""
        data = [
            {
                "name": track.name,
                "color": color,
                "segments": [
                    [[round(p.latitude, 7), round(p.longitude, 7)] for p in segment.points]
                    for segment in track.segments
                ],
            }
            for track, color in zip(tracks, colors, strict=True)
        ]
        self._run(
            f"setTracks({json.dumps(data, separators=(',', ':'))}, {'true' if fit else 'false'})"
        )

    def highlight_tracks(self, indices: list[int]) -> None:
        """Emphasise and zoom to the tracks at these indices (empty: none emphasised)."""
        self._run(f"highlightTracks({json.dumps(indices)})")

    def clear(self) -> None:
        """Remove the track, position and media markers."""
        self._run("clearAll()")

    def set_position(self, latitude: float, longitude: float) -> None:
        self._run(f"setPosition({latitude!r}, {longitude!r})")

    def clear_position(self) -> None:
        self._run("clearPosition()")

    def set_media_markers(self, markers: list[dict]) -> None:
        """Show media icons, clustered when they overlap.

        Each marker has ``lat``, ``lon``, ``kind`` (image|file), ``label``,
        ``selected``, ``moved`` and ``orig``; selected markers are drawn green,
        above the others. Moved markers are orange, with a dashed line from
        their original position ``orig`` (``[lat, lon]`` or None).
        """
        self._run(f"setMediaMarkers({json.dumps(markers, separators=(',', ':'))})")

    def _on_message(self, data: dict) -> None:
        if data.get("type") == "move":
            try:
                self.moveRequested.emit(float(data["lat"]), float(data["lon"]))
            except (KeyError, TypeError, ValueError):
                pass

    def _run(self, script: str) -> None:
        if self._ready:
            self.page().runJavaScript(script)
        else:
            self._queue.append(script)

    def _on_load_finished(self, ok: bool) -> None:
        self._ready = ok
        if ok:
            for script in self._queue:
                self.page().runJavaScript(script)
            self._queue.clear()

    # Without these overrides the web view would navigate to a dropped file.
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
            self.filesDropped.emit(paths)
        else:
            event.ignore()
