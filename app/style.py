"""진행바·슬라이더를 눈에 잘 띄는 색으로 (주황 진행 + 노란 손잡이)"""
from PySide6.QtWidgets import QApplication

SLIDER_CSS = """
QSlider::groove:horizontal { height: 6px; background: #3a3a3a; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #ff9800; border-radius: 3px; }
QSlider::add-page:horizontal { background: #3a3a3a; border-radius: 3px; }
QSlider::handle:horizontal {
    background: #ffeb3b; border: 2px solid #ffffff;
    width: 16px; height: 16px; margin: -7px 0; border-radius: 10px;
}
QSlider::handle:horizontal:hover { background: #ffffff; border-color: #ff9800; }
"""


def apply():
    app = QApplication.instance()
    if app is not None:
        app.setStyleSheet(app.styleSheet() + SLIDER_CSS)
