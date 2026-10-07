"""VideoVault 실행"""
import ctypes
import locale
import os
import sys
from ctypes import wintypes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ["PATH"] = BASE_DIR + os.pathsep + os.environ.get("PATH", "")   # libmpv-2.dll 위치

# 깨진 글자가 있어도 출력하다 멈추지 않게
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from app import quick_exit
from app.main_window import MainWindow

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateMutexW.restype = wintypes.HANDLE
_u32 = ctypes.WinDLL("user32")
_u32.FindWindowW.restype = wintypes.HWND
_u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_u32.SetForegroundWindow.argtypes = [wintypes.HWND]
_mutex = None


def already_running():
    """이미 켜져 있으면 그 창을 앞으로 가져오고 True"""
    global _mutex
    _mutex = _k32.CreateMutexW(None, False, "VideoVault_SingleInstance")
    if ctypes.get_last_error() != 183:      # 183 = 이미 있음
        return False
    hwnd = _u32.FindWindowW(None, "VideoVault")
    if hwnd:
        _u32.ShowWindow(hwnd, 9)            # 최소화되어 있으면 복원
        _u32.SetForegroundWindow(hwnd)
    return True


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
    if already_running():
        return
    app = QApplication(sys.argv)
    locale.setlocale(locale.LC_NUMERIC, "C")   # mpv 필수 설정
    apply_dark_theme(app)
    window = MainWindow()
    quick_exit.install(window)
    from app import style
    style.apply()
    from app import backup, thumb_tool
    backup.install(window)
    thumb_tool.install(window)
    from app import marks, presets
    marks.install(window)
    presets.install(window)
    from app import face_ui
    face_ui.install(window)
    from app import sub_ui
    sub_ui.install(window)
    from app import hover_preview
    hover_preview.install(window)
    from app import manage
    manage.install(window)
    from app import folders
    folders.install(window)







    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
