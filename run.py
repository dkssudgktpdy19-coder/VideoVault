"""VideoVault 실행"""
import locale
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ["PATH"] = BASE_DIR + os.pathsep + os.environ.get("PATH", "")   # libmpv-2.dll 위치

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from app.main_window import MainWindow


def apply_dark_theme(app):
    app.setStyle("Fusion")
    pal = QPalette()
    colors = {
        QPalette.ColorRole.Window: (30, 30, 30),
        QPalette.ColorRole.WindowText: (220, 220, 220),
        QPalette.ColorRole.Base: (22, 22, 22),
        QPalette.ColorRole.AlternateBase: (36, 36, 36),
        QPalette.ColorRole.Text: (220, 220, 220),
        QPalette.ColorRole.Button: (45, 45, 45),
        QPalette.ColorRole.ButtonText: (220, 220, 220),
        QPalette.ColorRole.Highlight: (45, 90, 140),
        QPalette.ColorRole.HighlightedText: (255, 255, 255),
        QPalette.ColorRole.ToolTipBase: (50, 50, 50),
        QPalette.ColorRole.ToolTipText: (230, 230, 230),
        QPalette.ColorRole.PlaceholderText: (130, 130, 130),
    }
    for role, rgb in colors.items():
        pal.setColor(role, QColor(*rgb))
    app.setPalette(pal)


def main():
    app = QApplication(sys.argv)
    locale.setlocale(locale.LC_NUMERIC, "C")   # mpv 필수 설정
    apply_dark_theme(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
