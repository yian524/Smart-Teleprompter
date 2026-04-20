"""程式入口。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .config import load_config
from .ui.main_window import MainWindow


def _load_stylesheet() -> str:
    qss = Path(__file__).parent / "resources" / "styles.qss"
    if qss.exists():
        try:
            return qss.read_text(encoding="utf-8")
        except OSError:
            return ""
    return ""


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Smart Teleprompter")
    app.setOrganizationName("SmartTeleprompter")
    qss = _load_stylesheet()
    if qss:
        app.setStyleSheet(qss)

    cfg = load_config()
    win = MainWindow(cfg)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
