"""어느 창에 있든 동작하는 전역 단축키 (Windows RegisterHotKey 사용)"""
import ctypes
import threading
from ctypes import wintypes

from PySide6.QtCore import QObject, Signal

_user32 = ctypes.WinDLL("user32")
_kernel32 = ctypes.WinDLL("kernel32")

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_NOREPEAT = 0x4000
MODS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
SPECIAL = {"pause": 0x13, "esc": 0x1B, "space": 0x20, "home": 0x24, "end": 0x23,
           "insert": 0x2D, "delete": 0x2E, "scrolllock": 0x91, "`": 0xC0}


def parse_hotkey(text):
    """'ctrl+alt+q' → (수정키 값, 키 코드)"""
    mods, vk = 0, None
    for part in [p.strip().lower() for p in text.split("+") if p.strip()]:
        if part in MODS:
            mods |= MODS[part]
        elif part in SPECIAL:
            vk = SPECIAL[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x6F + int(part[1:])
    if vk is None:
        raise ValueError(f"알 수 없는 단축키: {text}")
    return mods, vk


def pretty(text):
    """'ctrl+alt+q' → 'Ctrl+Alt+Q'"""
    return "+".join(p.strip().capitalize() for p in text.split("+"))


class GlobalHotkey(QObject):
    pressed = Signal()

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.text = text
        self.ok = False
        self._tid = None
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(2)

    def _run(self):
        self._tid = _kernel32.GetCurrentThreadId()
        try:
            mods, vk = parse_hotkey(self.text)
            self.ok = bool(_user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk))
        except ValueError:
            self.ok = False
        self._ready.set()
        if not self.ok:
            return
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.pressed.emit()
        _user32.UnregisterHotKey(None, 1)

    def stop(self):
        if self._tid and self.ok:
            _user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
