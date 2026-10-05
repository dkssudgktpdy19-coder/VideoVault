"""빠른 종료 (요구사항 16번): 소리 끔 → 창 숨김 → 크롬 네이버 → 종료"""
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QLabel

from app.browser import open_start_page
from app.config import QUIT_HOTKEY
from app.hotkey import GlobalHotkey, pretty


def quick_exit(window):
    pw = window.player
    # 1) 가장 먼저 소리 끄기
    try:
        if pw._alive():
            pw.player.mute = True
            pw.player.pause = True
    except Exception:
        pass
    # 2) 모든 창 즉시 숨기기
    for w in QApplication.topLevelWidgets():
        w.hide()
    # 3) 크롬 네이버
    open_start_page()
    # 4) 이어보기 위치 저장 후 바로 종료
    try:
        pw._save_resume()
        window.conn.commit()
        window.conn.close()
    except Exception:
        pass
    os._exit(0)


def install(window):
    """메인 화면에 빠른 종료 단축키를 연결"""
    hk = GlobalHotkey(QUIT_HOTKEY, window)
    hk.pressed.connect(lambda: quick_exit(window))
    window._quit_hotkey = hk

    # 전역 등록에 실패해도 프로그램 창 안에서는 동작하게
    sc = QShortcut(QKeySequence(pretty(QUIT_HOTKEY)), window)
    sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
    sc.activated.connect(lambda: quick_exit(window))

    if hk.ok:
        label = QLabel(f"⚡ 빠른 종료: {pretty(QUIT_HOTKEY)}")
        label.setStyleSheet("color:#8c8; padding-right:6px;")
    else:
        label = QLabel(f"⚠ {pretty(QUIT_HOTKEY)} 등록 실패(다른 프로그램이 사용 중) · 창 안에서만 동작")
        label.setStyleSheet("color:#fb8; padding-right:6px;")
    window.statusBar().addPermanentWidget(label)

