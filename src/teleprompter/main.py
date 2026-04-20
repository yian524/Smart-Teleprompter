"""程式入口。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

# 相容兩種呼叫方式：
# - `python -m teleprompter.main` → relative import
# - 直接執行 main.py / PyInstaller → absolute import fallback
try:
    from .config import load_config
    from .ui.main_window import MainWindow
except ImportError:
    from teleprompter.config import load_config
    from teleprompter.ui.main_window import MainWindow


def _load_stylesheet() -> str:
    qss = Path(__file__).parent / "resources" / "styles.qss"
    if qss.exists():
        try:
            return qss.read_text(encoding="utf-8")
        except OSError:
            return ""
    return ""


def make_app_icon() -> QIcon:
    """產生 app icon：綠底圓角 + 白色 T（Teleprompter）。"""
    pix = QPixmap(128, 128)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # 綠色漸層圓角底
    painter.setBrush(QColor("#4CAF50"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(0, 0, 128, 128, 20, 20)
    # 白色 T
    painter.setPen(QColor("white"))
    f = QFont("Segoe UI", 72, QFont.Weight.Bold)
    painter.setFont(f)
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "T")
    painter.end()
    return QIcon(pix)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Smart Teleprompter")
    app.setOrganizationName("SmartTeleprompter")
    app.setWindowIcon(make_app_icon())
    qss = _load_stylesheet()
    if qss:
        app.setStyleSheet(qss)

    cfg = load_config()
    win = MainWindow(cfg)
    win.setWindowIcon(make_app_icon())
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
