"""영상 재생 창 (mpv 엔진)"""
import os

import mpv
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget

from app import library

KEY_HELP = "Space 재생/정지 · M 소리 · ←→ 10초 · ↑↓ 볼륨 · Home 처음부터 · F 전체화면 · Esc 닫기"


def _t(sec):
    sec = int(sec or 0)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class PlayerWindow(QWidget):
    video_changed = Signal(int)     # 본 횟수·이어보기가 바뀌면 메인 화면에 알림

    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.video = None
        self.player = None
        self._pending_seek = None
        self._counted = False
        self._quitting = False
        self._muted = True          # 요구사항 13번: 프로그램 시작 후 첫 재생은 무조건 무음
        self._volume = 100
        self.setWindowTitle("VideoVault 플레이어")
        self.resize(1280, 780)
        self.setStyleSheet("QWidget{background:#111;color:#ddd;}"
                           "QPushButton{border:none;font-size:16px;padding:4px 8px;}"
                           "QPushButton:hover{background:#333;}")

        # 영상 영역
        self.video_area = QWidget(self)
        self.video_area.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.video_area.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)

        # 조작 막대
        self.btn_play = self._button("⏸", self.toggle_pause, "재생/일시정지 (Space)")
        self.btn_back = self._button("⏪", lambda: self.seek(-10), "10초 뒤로 (←)")
        self.btn_fwd = self._button("⏩", lambda: self.seek(10), "10초 앞으로 (→)")
        self.lbl_time = QLabel("00:00")
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.slider.sliderReleased.connect(self._slider_seek)
        self.lbl_total = QLabel("00:00")
        self.btn_mute = self._button("🔇", self.toggle_mute, "소리 켜기/끄기 (M)")
        self.lbl_vol = QLabel("")
        self.btn_full = self._button("⛶", self.toggle_fullscreen, "전체화면 (F / Enter)")

        bar = QHBoxLayout()
        bar.setContentsMargins(8, 4, 8, 6)
        for w in (self.btn_play, self.btn_back, self.btn_fwd, self.lbl_time):
            bar.addWidget(w)
        bar.addWidget(self.slider, 1)
        for w in (self.lbl_total, self.btn_mute, self.lbl_vol, self.btn_full):
            bar.addWidget(w)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.video_area, 1)
        layout.addLayout(bar)

        self._create_player()

        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._tick)

    # ---------- 엔진 만들기 / 살아있는지 확인 ----------
    def _create_player(self):
        old, self.player = self.player, None
        if old is not None:
            try:
                old.terminate()
            except Exception:
                pass
        self.player = mpv.MPV(
            wid=str(int(self.video_area.winId())),
            mute=self._muted,
            volume=self._volume,
            keep_open="yes",
            idle=True,
            input_default_bindings=False,
            input_vo_keyboard=False,
            hwdec="auto-safe",          # 그래픽카드로 디코딩 (4K도 부드럽게)
        )

    def _alive(self):
        if self.player is None:
            return False
        try:
            self.player.check_core_alive()
            return True
        except Exception:
            return False

    def _button(self, text, fn, tip):
        b = QPushButton(text)
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.clicked.connect(lambda checked=False: fn())
        return b

    def osd(self, text, ms=1500):
        if not self._alive():
            return
        try:
            self.player.command("show-text", text, str(ms))
        except Exception:
            pass

    # ---------- 열기 / 닫기 ----------
    def open_video(self, v):
        self._save_resume()
        path = v.get("full_path")
        if not path or not os.path.exists(path):
            return False
        if not self._alive():
            self._create_player()       # 엔진이 꺼져 있으면 다시 켬
        self.video = v
        self._counted = False
        resume = v.get("resume_pos") or 0
        self._pending_seek = resume if resume > 5 else None
        library.clear_new(self.conn, [v["id"]])
        self.video_changed.emit(v["id"])
        try:
            self.player.play(path)
            self.player.pause = False
        except mpv.ShutdownError:
            self._create_player()
            self.player.play(path)
        self.setWindowTitle(f"{v['filename']}   |   {KEY_HELP}")
        self.show()
        self.raise_()
        self.activateWindow()
        self.timer.start()
        return True

    def _save_resume(self):
        if not self.video or self._pending_seek is not None or not self._alive():
            return
        try:
            pos, dur = self.player.time_pos, self.player.duration
        except Exception:
            return
        library.save_resume(self.conn, self.video["id"], pos, dur)
        self.video_changed.emit(self.video["id"])

    def _stop(self):
        self._save_resume()
        self.timer.stop()
        self.video = None
        if self._alive():
            try:
                self.player.command("stop")
            except Exception:
                pass
        if self.isFullScreen():
            self.showNormal()

    def closeEvent(self, event):
        if self._quitting:
            event.accept()
            return
        # 진짜로 닫지 않고 숨기기만 함 (엔진이 꺼지지 않게)
        self._stop()
        event.ignore()
        self.hide()

    def shutdown(self):
        """프로그램 종료 시 호출"""
        self._quitting = True
        self._save_resume()
        self.video = None
        self.timer.stop()
        self.hide()
        if self.player is not None:
            try:
                self.player.terminate()
            except Exception:
                pass

    # ---------- 주기적 갱신 ----------
    def _tick(self):
        if not self.video or not self._alive():
            return
        try:
            pos, dur = self.player.time_pos, self.player.duration
            paused, muted, vol = self.player.pause, self.player.mute, self.player.volume
        except Exception:
            return
        self._muted, self._volume = bool(muted), (vol or 100)   # 엔진을 다시 켤 때 유지
        if dur and self._pending_seek is not None:
            target = self._pending_seek
            self._pending_seek = None
            try:
                self.player.seek(target, "absolute")
                self.osd(f"이어보기: {_t(target)}부터   (Home: 처음부터)", 3000)
            except Exception:
                pass
        if dur:
            if not self.slider.isSliderDown():
                self.slider.setValue(int((pos or 0) / dur * 1000))
            self.lbl_total.setText(_t(dur))
        self.lbl_time.setText(_t(pos))
        self.btn_play.setText("▶" if paused else "⏸")
        self.btn_mute.setText("🔇" if muted else "🔊")
        self.lbl_vol.setText(f"{int(vol or 0)}%")

        # 30초(짧은 영상은 30%) 이상 봤을 때 본 횟수 +1
        if not self._counted and pos and dur and pos >= min(30, dur * 0.3):
            self._counted = True
            library.mark_played(self.conn, self.video["id"])
            self.video_changed.emit(self.video["id"])

    # ---------- 조작 ----------
    def toggle_pause(self):
        if self._alive():
            self.player.pause = not self.player.pause

    def toggle_mute(self):
        if not self._alive():
            return
        self.player.mute = not self.player.mute
        self._muted = bool(self.player.mute)
        self.osd("음소거" if self._muted else "소리 켜짐")

    def seek(self, sec):
        if not self._alive():
            return
        try:
            self.player.seek(sec, "relative")
            self.osd(f"{'+' if sec > 0 else ''}{sec}초")
        except Exception:
            pass

    def seek_start(self):
        if not self._alive():
            return
        try:
            self.player.seek(0, "absolute")
            self.osd("처음부터")
        except Exception:
            pass

    def _slider_seek(self):
        if not self._alive():
            return
        try:
            self.player.seek(self.slider.value() / 10, "absolute-percent")
        except Exception:
            pass

    def change_volume(self, d):
        if not self._alive():
            return
        vol = max(0, min(130, (self.player.volume or 100) + d))
        self.player.volume = vol
        self._volume = vol
        self.osd(f"볼륨 {int(vol)}%")

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key.Key_Space:
            self.toggle_pause()
        elif k == Qt.Key.Key_M:
            self.toggle_mute()
        elif k == Qt.Key.Key_Right:
            self.seek(10)
        elif k == Qt.Key.Key_Left:
            self.seek(-10)
        elif k == Qt.Key.Key_Up:
            self.change_volume(5)
        elif k == Qt.Key.Key_Down:
            self.change_volume(-5)
        elif k == Qt.Key.Key_Home:
            self.seek_start()
        elif k in (Qt.Key.Key_F, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.toggle_fullscreen()
        elif k == Qt.Key.Key_Escape:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.close()
        else:
            super().keyPressEvent(e)
