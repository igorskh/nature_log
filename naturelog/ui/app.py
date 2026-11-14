"""Application entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from naturelog.ui.main_window import MainWindow

_ICON_DIR = Path(__file__).parent / "resources" / "icons"
_ICON_SIZES = (16, 32, 48, 64, 128, 256, 512, 1024)


def app_icon() -> QIcon:
    """Return the NatureLog application icon in all bundled raster sizes."""
    icon = QIcon()
    for size in _ICON_SIZES:
        icon.addFile(str(_ICON_DIR / f"naturelog_{size}.png"))
    return icon


def main() -> int:
    if sys.platform == "win32":
        # Give the process its own taskbar identity so Windows shows our icon.
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NatureLog.NatureLog")
    app = QApplication(sys.argv)
    app.setOrganizationName("NatureLog")
    app.setApplicationName("NatureLog")
    app.setWindowIcon(app_icon())
    window = MainWindow()
    window.show()
    return app.exec()
