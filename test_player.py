import os
import sys
import locale

# 이 파일이 있는 폴더(libmpv-2.dll 위치)를 DLL 검색 경로에 추가
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ["PATH"] = BASE_DIR + os.pathsep + os.environ.get("PATH", "")

import mpv
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QWidget


class TestPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("재생 테스트 | O:파일열기  Space:재생/정지  M:소리켜기/끄기  ←→:10초")
        self.resize(1000, 600)

        # 영상이 그려질 영역
        self.video = QWidget(self)
        self.video.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.video.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setCentralWidget(self.video)

        # mpv 플레이어 생성
        self.player = mpv.MPV(
            wid=str(int(self.video.winId())),
            mute=True,                    # 요구사항 13번: 시작은 무조건 무음
            keep_open="yes",
            input_default_bindings=False,
        )

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_O:
            path, _ = QFileDialog.getOpenFileName(self, "테스트할 영상 선택")
            if path:
                self.player.play(path)
        elif key == Qt.Key.Key_Space:
            self.player.pause = not self.player.pause
        elif key == Qt.Key.Key_M:
            self.player.mute = not self.player.mute
        elif key == Qt.Key.Key_Right:
            self.player.seek(10)
        elif key == Qt.Key.Key_Left:
            self.player.seek(-10)

    def closeEvent(self, event):
        self.player.terminate()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    locale.setlocale(locale.LC_NUMERIC, "C")  # mpv 필수 설정
    window = TestPlayer()
    window.show()
    sys.exit(app.exec())
