"""Window with an embedded iNaturalist site for uploading selected media.

The user signs in on iNaturalist in the embedded browser (the login is kept in
a persistent, app-private browser profile). "Add selected files" opens the
iNaturalist uploader and hands the selected files to it, as if they had been
chosen in its file dialog. iNaturalist reads date and location of photos from
their EXIF; for recordings NatureLog fills the date, the track position and the
most probable detected species into their observation cards (empty fields only).
Reviewing and submitting observations stays on iNaturalist. Files are only
read, never changed.

The species suggestions iNaturalist's computer vision offers for a photo are
shown with their scores (iNaturalist computes them but does not display them),
see ``resources/inat_vision_scores.js``.
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from naturelog.inaturalist import (
    SITE_URL,
    UPLOAD_PATH,
    UPLOAD_URL,
    ObservationHint,
    UploadFile,
    UploadSelection,
    is_allowed_host,
    upload_selection,
)
from naturelog.media import MediaFile

_PROFILE_NAME = "naturelog-inaturalist"
#: Page script that shows computer-vision scores next to species suggestions.
_VISION_SCORES_JS = Path(__file__).parent / "resources" / "inat_vision_scores.js"
#: Our scripts run isolated from the page's own JavaScript; DOM and events are shared.
_WORLD = QWebEngineScript.ScriptWorldId.ApplicationWorld
#: Filling cards needs the page's React state, which only the page's own world sees.
_PAGE_WORLD = QWebEngineScript.ScriptWorldId.MainWorld
#: How long to wait for the uploader's file input to appear (React renders it late).
_INPUT_POLL_MS = 500
_INPUT_POLL_TRIES = 40
#: How long to wait for the observation cards of delivered recordings.
_FILL_POLL_MS = 500
_FILL_POLL_TRIES = 60

#: Card values to fill, each with the ``name|size|lastModified`` key of its file.
_JS_FILL_SET = "window.__naturelogFill = {pending: %(hints)s, filled: 0};"

#: Fill date, location and species into the uploader cards of the pending
#: recordings, through the card's own ``updateObsCard`` (as if typed by the
#: user). Only empty fields are set. Returns the number of cards not found yet.
_JS_FILL = """
(() => {
  const S = window.__naturelogFill;
  if (!S) return 0;
  const fiberOf = el => {
    for (const k in el) {
      if (k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$')) return el[k];
    }
    return null;
  };
  const cardProps = el => {
    for (let f = fiberOf(el); f; f = f.return) {
      const p = f.memoizedProps;
      if (p && p.obsCard && typeof p.updateObsCard === 'function') return p;
    }
    return null;
  };
  const keyOf = f => [f.name, f.size, f.lastModified].join('|');
  const findCard = key => {
    for (const el of document.querySelectorAll('.ObsCardComponent')) {
      const p = cardProps(el);
      if (!p) continue;
      const own = Object.values(p.obsCard.files || {});
      const all = Object.values(p.files || {}).filter(f => f.cardID === p.obsCard.id);
      if (own.concat(all).some(f => keyOf(f) === key)) return p;
    }
    return null;
  };
  let timeFormat = 'HH:mm';
  try { timeFormat = I18n.t('momentjs.time_hours') || timeFormat; } catch (e) {}
  const twelveHour = /a/i.test(timeFormat);
  const pad = n => String(n).padStart(2, '0');
  // Same formats as the uploader uses for photo EXIF dates.
  const dateText = h => twelveHour
    ? `${h.date} ${h.hour % 12 || 12}:${pad(h.minute)} ${h.hour < 12 ? 'AM' : 'PM'}`
    : `${h.date} ${pad(h.hour)}:${pad(h.minute)}`;
  const setTaxon = (key, name) => {
    fetch('https://api.inaturalist.org/v1/taxa?per_page=30&q=' + encodeURIComponent(name))
      .then(r => r.json())
      .then(data => {
        const wanted = name.toLowerCase();
        const results = data.results || [];
        const taxon = results.find(t => (t.name || '').toLowerCase() === wanted)
                   || results.find(t => (t.matched_term || '').toLowerCase() === wanted);
        const p = findCard(key);
        if (!taxon || !p || p.obsCard.taxon_id || p.obsCard.species_guess) return;
        // The card's taxon field loads the taxon by id and shows it.
        p.updateObsCard(p.obsCard, {taxon_id: taxon.id, species_guess: taxon.name});
      })
      .catch(() => {});
  };
  S.pending = S.pending.filter(h => {
    const p = findCard(h.key);
    if (!p) return true;
    const card = p.obsCard;
    const attrs = {};
    if (!card.date) attrs.date = attrs.selected_date = dateText(h);
    if (h.latitude != null && h.longitude != null && card.latitude == null) {
      attrs.latitude = h.latitude;
      attrs.longitude = h.longitude;
    }
    if (Object.keys(attrs).length) p.updateObsCard(card, attrs);
    if (h.scientificName && !card.taxon_id && !card.species_guess) setTaxon(h.key, h.scientificName);
    S.filled += 1;
    return false;
  });
  return S.pending.length;
})()
"""

_JS_RESET = "window.__naturelogFiles = [];"

_JS_ADD = """
(() => {
  const bin = atob(%(data)s);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  window.__naturelogFiles.push(new File([bytes], %(name)s, {type: %(type)s, lastModified: %(mtime)d}));
  return window.__naturelogFiles.length;
})()
"""

#: Hand the collected files to the uploader. Returns "input", "drop" or "none".
_JS_DELIVER = """
(() => {
  const files = window.__naturelogFiles || [];
  const transfer = new DataTransfer();
  for (const file of files) transfer.items.add(file);
  const input = document.querySelector('input[type=file][multiple]')
             || document.querySelector('input[type=file]');
  if (input) {
    input.files = transfer.files;
    input.dispatchEvent(new Event('change', {bubbles: true}));
    window.__naturelogFiles = [];
    return 'input';
  }
  if (!%(drop)s) return 'none';
  const target = document.querySelector('[class*=dropzone], [class*=uploader], main') || document.body;
  for (const type of ['dragenter', 'dragover', 'drop']) {
    target.dispatchEvent(new DragEvent(type, {bubbles: true, cancelable: true, dataTransfer: transfer}));
  }
  window.__naturelogFiles = [];
  return 'drop';
})()
"""


# --- background reading --------------------------------------------------


class _ReadSignals(QObject):
    file = Signal(object, str)  # UploadFile, base64 data
    failed = Signal(object, str)  # UploadFile, error
    finished = Signal()


class _ReadTask(QRunnable):
    """Read and base64-encode files off the UI thread."""

    def __init__(self, files: list[UploadFile]) -> None:
        super().__init__()
        self.files = files
        self.cancelled = threading.Event()
        self.signals = _ReadSignals()

    def run(self) -> None:
        for upload in self.files:
            if self.cancelled.is_set():
                break
            try:
                data = base64.b64encode(upload.path.read_bytes()).decode("ascii")
            except OSError as exc:
                self.signals.failed.emit(upload, exc.strerror or str(exc))
            else:
                self.signals.file.emit(upload, data)
        self.signals.finished.emit()


# --- web view ------------------------------------------------------------


def _profile(parent: QObject) -> QWebEngineProfile:
    """Persistent profile so the iNaturalist login survives restarts."""
    profile = QWebEngineProfile(_PROFILE_NAME, parent)
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
    )
    return profile


def _vision_scores_script() -> QWebEngineScript:
    """Installed before the site's own scripts run, in the page's world (it wraps fetch)."""
    script = QWebEngineScript()
    script.setName("naturelog-vision-scores")
    script.setSourceCode(_VISION_SCORES_JS.read_text(encoding="utf-8"))
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(_PAGE_WORLD)
    script.setRunsOnSubFrames(False)
    return script


class _Page(QWebEnginePage):
    """Keeps navigation on iNaturalist and its sign-in providers."""

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        if not is_main_frame or url.scheme() in ("about", "data", "blob"):
            return True
        if url.scheme() == "https" and is_allowed_host(url.host()):
            return True
        # Anything else (help pages, external links) opens in the system browser.
        QDesktopServices.openUrl(url)
        return False

    def createWindow(self, _type) -> QWebEnginePage:
        # Pop-ups and target=_blank links load in this view.
        return self


class InaturalistWindow(QWidget):
    """Embedded iNaturalist with a button that sends the selected media to its uploader."""

    #: Status messages for the main window's status bar.
    message = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        hint_source: Callable[[MediaFile], ObservationHint | None] | None = None,
    ) -> None:
        """``hint_source`` gives the card values to fill for a file (recordings)."""
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Upload to iNaturalist")
        self.resize(1200, 860)

        self._media: list[MediaFile] = []
        self._hint_source = hint_source
        #: Card values of the files being transferred, by path.
        self._hints: dict[Path, ObservationHint] = {}
        #: Card values of transferred files, keyed by ``name|size|lastModified``.
        self._fill: list[dict] = []
        self._fill_tries = 0
        self._fill_timer = QTimer(self)
        self._fill_timer.setSingleShot(True)
        self._fill_timer.setInterval(_FILL_POLL_MS)
        self._fill_timer.timeout.connect(self._fill_cards)
        #: Files waiting for the uploader page to be shown.
        self._pending: UploadSelection | None = None
        self._task: _ReadTask | None = None
        self._added = 0
        self._failed: list[tuple[Path, str]] = []
        self._poll_tries = 0
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_INPUT_POLL_MS)
        self._poll_timer.timeout.connect(self._deliver)

        # Created before the profile so it (and its page) is deleted first.
        self._view = QWebEngineView(self)
        self._profile = _profile(self)
        self._page = _Page(self._profile, self._view)
        self._page.scripts().insert(_vision_scores_script())
        self._view.setPage(self._page)
        self._view.urlChanged.connect(self._on_url_changed)
        self._view.loadStarted.connect(self._on_load_started)
        self._view.loadFinished.connect(self._on_load_finished)

        style = self.style()
        back = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_ArrowBack), "")
        back.setToolTip("Back")
        back.clicked.connect(self._view.back)
        forward = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_ArrowForward), "")
        forward.setToolTip("Forward")
        forward.clicked.connect(self._view.forward)
        reload = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload), "")
        reload.setToolTip("Reload")
        reload.clicked.connect(self._view.reload)
        home = QPushButton("iNaturalist")
        home.setToolTip("Open the iNaturalist home page (sign in here)")
        home.clicked.connect(lambda: self._view.load(QUrl(SITE_URL)))

        self._url_label = QLabel()
        self._url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._status_label = QLabel()
        self._send_button = QPushButton()
        self._send_button.setDefault(True)
        self._send_button.clicked.connect(self.send_files)

        bar = QHBoxLayout()
        bar.addWidget(back)
        bar.addWidget(forward)
        bar.addWidget(reload)
        bar.addWidget(home)
        bar.addWidget(self._url_label, 1)
        bar.addWidget(self._status_label)
        bar.addWidget(self._send_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(bar)
        layout.addWidget(self._view, 1)

        self._update_button()
        self._view.load(QUrl(SITE_URL))

    # --- public API -----------------------------------------------------

    def set_files(self, media: list[MediaFile]) -> None:
        """Files that "Add selected files" sends to the uploader."""
        self._media = list(media)
        self._update_button()

    def send_files(self) -> None:
        """Open the uploader and add the selected files to it."""
        if self._busy():
            return
        selection = upload_selection(self._media)
        if not selection.files:
            self._status("No selected file can be uploaded" + _skipped_note(selection.skipped))
            return
        self._pending = selection
        self._hints = {}
        if self._hint_source is not None:
            by_path = {m.path: m for m in self._media}
            for upload in selection.files:
                media = by_path.get(upload.path)
                hint = self._hint_source(media) if media is not None else None
                if hint is not None:
                    self._hints[upload.path] = hint
        if self._on_upload_page() and not self._view.page().isLoading():
            self._start_transfer()
        else:
            self._status("Opening the iNaturalist uploader…")
            self._view.load(QUrl(UPLOAD_URL))
        self._update_button()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._cancel()
        super().closeEvent(event)

    # --- transfer -------------------------------------------------------

    def _start_transfer(self) -> None:
        selection = self._pending
        self._pending = None
        if selection is None:
            return
        self._added = 0
        self._fill = []
        self._fill_timer.stop()
        self._failed = list(selection.skipped)
        self._page.runJavaScript(_JS_RESET, _WORLD)
        task = _ReadTask(selection.files)
        task.signals.file.connect(self._on_file_read)
        task.signals.failed.connect(lambda upload, error: self._failed.append((upload.path, error)))
        task.signals.finished.connect(lambda: self._on_read_finished(task))
        self._task = task
        self._status(f"Transferring {len(selection.files)} file(s)…")
        QThreadPool.globalInstance().start(task)
        self._update_button()

    def _on_file_read(self, upload: UploadFile, data: str) -> None:
        if self._task is None or self._task.cancelled.is_set():
            return
        try:
            mtime = int(upload.path.stat().st_mtime * 1000)
        except OSError:
            mtime = 0
        hint = self._hints.get(upload.path)
        if hint is not None:
            size = len(data) // 4 * 3 - data[-2:].count("=")  # decoded base64 length
            self._fill.append({**hint.to_json(), "key": f"{upload.path.name}|{size}|{mtime}"})
        script = _JS_ADD % {
            "data": json.dumps(data),
            "name": json.dumps(upload.path.name),
            "type": json.dumps(upload.mime_type),
            "mtime": mtime,
        }
        self._page.runJavaScript(script, _WORLD, self._on_file_added)

    def _on_file_added(self, count) -> None:
        if isinstance(count, (int, float)):
            self._added = int(count)
            self._status(f"Transferring… {self._added} file(s) ready")

    def _on_read_finished(self, task: _ReadTask) -> None:
        if task is not self._task or task.cancelled.is_set():
            return
        # JavaScript calls run in order: delivering now sees every added file.
        self._poll_tries = 0
        self._deliver()

    def _deliver(self) -> None:
        self._poll_timer.stop()
        last_try = self._poll_tries >= _INPUT_POLL_TRIES
        script = _JS_DELIVER % {"drop": "true" if last_try else "false"}
        self._page.runJavaScript(script, _WORLD, self._on_delivered)

    def _on_delivered(self, result) -> None:
        if self._task is None:
            return
        if result == "none":
            self._poll_tries += 1
            self._poll_timer.start()
            return
        self._task = None
        if result in ("input", "drop"):
            text = f"Added {self._added} file(s) to the iNaturalist uploader"
            if self._fill:
                self._start_fill()
        else:
            text = "Could not add files to the iNaturalist uploader"
        self._status(text + _skipped_note(self._failed))
        self._update_button()

    # --- filling recording cards ----------------------------------------

    def _start_fill(self) -> None:
        script = _JS_FILL_SET % {"hints": json.dumps(self._fill)}
        self._page.runJavaScript(script, _PAGE_WORLD)
        self._fill_tries = 0
        self._fill_timer.start()

    def _fill_cards(self) -> None:
        self._page.runJavaScript(_JS_FILL, _PAGE_WORLD, self._on_filled)

    def _on_filled(self, remaining) -> None:
        if not self._fill:
            return  # cancelled
        total = len(self._fill)
        self._fill_tries += 1
        if isinstance(remaining, (int, float)) and remaining <= 0:
            self._fill = []
            self._status(
                f"Filled date, location and species of {total} recording(s) – "
                "check them before submitting"
            )
        elif self._fill_tries >= _FILL_POLL_TRIES:
            self._fill = []
            if isinstance(remaining, (int, float)):
                done = total - int(remaining)
                self._status(f"Filled {done} of {total} recording card(s); fill the rest by hand")
            else:
                self._status("Could not fill the recording cards; fill them by hand")
        else:
            self._fill_timer.start()

    def _cancel(self) -> None:
        self._poll_timer.stop()
        self._fill_timer.stop()
        self._fill = []
        if self._task is not None:
            self._task.cancelled.set()
            self._task = None
        self._update_button()

    def _busy(self) -> bool:
        return self._task is not None

    # --- page events ----------------------------------------------------

    def _on_upload_page(self) -> bool:
        url = self._view.url()
        return is_allowed_host(url.host()) and url.path().rstrip("/") == UPLOAD_PATH

    def _on_url_changed(self, url: QUrl) -> None:
        self._url_label.setText(url.toDisplayString())

    def _on_load_started(self) -> None:
        if self._fill:
            # The uploader with the recording cards is going away.
            self._fill_timer.stop()
            self._fill = []
        if self._busy():
            # The page with the collected files is going away.
            self._cancel()
            self._status("Transfer interrupted by navigation")

    def _on_load_finished(self, ok: bool) -> None:
        if self._pending is None:
            return
        if not ok:
            self._status("Could not load iNaturalist – check the connection")
        elif self._on_upload_page():
            self._start_transfer()
        else:
            # Usually the sign-in page: keep the files and add them once the
            # uploader is shown (iNaturalist returns there after signing in).
            self._status("Sign in to iNaturalist; the files are added when the uploader opens")

    # --- helpers --------------------------------------------------------

    def _status(self, text: str) -> None:
        self._status_label.setText(text)
        self.message.emit(text)

    def _update_button(self) -> None:
        count = len(self._media)
        self._send_button.setText(f"Add {count} selected file(s) to upload")
        self._send_button.setEnabled(count > 0 and not self._busy())
        self._send_button.setToolTip(
            "Open the iNaturalist uploader and add the files selected in NatureLog "
            "(photos and sounds up to 20 MB)"
        )


def _skipped_note(skipped: list[tuple[Path, str]]) -> str:
    if not skipped:
        return ""
    names = ", ".join(f"{path.name}: {reason}" for path, reason in skipped[:3])
    more = f", +{len(skipped) - 3} more" if len(skipped) > 3 else ""
    return f" ({len(skipped)} skipped – {names}{more})"
