"""영상 재생 창 (mpv 엔진)"""
import os

import mpv
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton, QSlider,
                               QVBoxLayout, QWidget)

from app import audio, library

HELP_TEXT = """[이동]
←  →              10초
Shift + ←  →      1초
Ctrl + ←  →       1분
,  .              이전 / 다음 프레임 (자동 일시정지)
Home              처음부터
PgUp / PgDn       이전 / 다음 영상

[재생]
Space             재생 / 일시정지
L                 구간 반복 (A 지정 → B 지정 → 해제)
A                 자동 재생 켜기/끄기 (끝나면 다음 영상)
M                 소리 켜기/끄기
↑  ↓              볼륨

[화면]
E                 필터 패널 열기/닫기
1 / 2             밝기 - / +
3 / 4             대비 - / +
5 / 6             채도 - / +
7 / 8             감마 - / +
0                 필터 초기화
F, Enter          전체화면
Esc               전체화면 해제 / 닫기
F1                이 도움말"""

FILTERS = [("brightness", "밝기"), ("contrast", "대비"), ("saturation", "채도"),
           ("gamma", "감마"), ("hue", "색조")]
FILTER_NAMES = dict(FILTERS)
FILTER_KEYS = {
    ord("1"): ("brightness", -5), ord("2"): ("brightness", 5),
    ord("3"): ("contrast", -5), ord("4"): ("contrast", 5),
    ord("5"): ("saturation", -5), ord("6"): ("saturation", 5),
    ord("7"): ("gamma", -5), ord("8"): ("gamma", 5),
}


def _t(sec):
    sec = int(sec or 0)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _t_ms(sec):
    if sec is None:
        return "--"
    total_ms = int(round(sec * 1000))
    s, ms = divmod(total_ms, 1000)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}" if h else f"{m:02d}:{s:02d}.{ms:03d}"


class PlayerWindow(QWidget):
    video_changed = Signal(int)     # 본 횟수·이어보기가 바뀌면 메인 화면에 알림

    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.video = None
        self.player = None
        self.next_provider = None   # 메인 화면이 지정: (영상 id, +1 또는 -1) → 이웃 영상
        self._pending_seek = None
        self._counted = False
        self._quitting = False
        self._advancing = False
        self._muted = True          # 요구사항 13번: 프로그램 시작 후 첫 재생은 무조건 무음
        self._volume = 100
        self._eq = {k: 0 for k, _ in FILTERS}
        self._loop_a = None
        self._loop_b = None
        self._autoplay = bool(library.get_setting(conn, "autoplay", False))
        self._audio_device = library.get_setting(conn, "audio_device", "auto")

        self.setWindowTitle("VideoVault 플레이어")
        self.resize(1280, 800)
        self.setStyleSheet("QWidget{background:#111;color:#ddd;}"
                           "QPushButton{border:none;font-size:15px;padding:4px 8px;}"
                           "QPushButton:hover{background:#333;}")

        # 영상 영역
        self.video_area = QWidget(self)
        self.video_area.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.video_area.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)

        # 1줄: 조작 막대
        self.btn_play = self._button("⏸", self.toggle_pause, "재생/일시정지 (Space)")
        self.btn_prev = self._button("⏮", lambda: self.play_neighbor(-1), "이전 영상 (PgUp)")
        self.btn_back = self._button("⏪", lambda: self.seek(-10), "10초 뒤로 (←)  Shift: 1초  Ctrl: 1분")
        self.btn_fwd = self._button("⏩", lambda: self.seek(10), "10초 앞으로 (→)  Shift: 1초  Ctrl: 1분")
        self.btn_next = self._button("⏭", lambda: self.play_neighbor(1), "다음 영상 (PgDn)")
        self.lbl_time = QLabel("00:00")
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.slider.sliderReleased.connect(self._slider_seek)
        self.lbl_total = QLabel("00:00")
        self.btn_loop = self._button("🔁 구간반복", self.cycle_loop, "구간 반복: A 지정 → B 지정 → 해제 (L)")
        self.btn_auto = self._button("", self.toggle_autoplay, "끝나면 다음 영상 자동 재생 (A)")
        self.btn_filter = self._button("🎨 필터", self.toggle_filter_panel, "밝기·대비·채도·감마 (E)")
        self.btn_mute = self._button("🔇", self.toggle_mute, "소리 켜기/끄기 (M)")
        self.lbl_vol = QLabel("")
        self.btn_full = self._button("⛶", self.toggle_fullscreen, "전체화면 (F / Enter)")
        self.btn_help = self._button("❔", self.show_help, "단축키 도움말 (F1)")

        bar = QHBoxLayout()
        bar.setContentsMargins(8, 4, 8, 0)
        for w in (self.btn_play, self.btn_prev, self.btn_back, self.btn_fwd, self.btn_next,
                  self.lbl_time):
            bar.addWidget(w)
        bar.addWidget(self.slider, 1)
        for w in (self.lbl_total, self.btn_loop, self.btn_auto, self.btn_filter, self.btn_mute,
                  self.lbl_vol, self.btn_full, self.btn_help):
            bar.addWidget(w)

        # 2줄: 사운드 출력 장치 (요구사항 14번)
        self.btn_audio = self._button("🔈 사운드 장치 확인 중...", self.show_audio_menu,
                                      "클릭해서 출력 장치 바꾸기")
        self.btn_audio.setStyleSheet("font-size:12px;color:#9ad;")
        row2 = QHBoxLayout()
        row2.setContentsMargins(8, 0, 8, 4)
        row2.addWidget(self.btn_audio)
        row2.addStretch(1)

        # 3줄: 필터 패널 (요구사항 10번, 처음에는 숨김)
        self.filter_panel = QWidget()
        fp = QHBoxLayout(self.filter_panel)
        fp.setContentsMargins(12, 2, 12, 6)
        self.filter_sliders, self.filter_values = {}, {}
        for key, name in FILTERS:
            s = QSlider(Qt.Orientation.Horizontal)
            s.setRange(-100, 100)
            s.setFixedWidth(130)
            s.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            val = QLabel("0")
            val.setFixedWidth(34)
            s.valueChanged.connect(lambda v, k=key: self._on_filter(k, v))
            self.filter_sliders[key], self.filter_values[key] = s, val
            fp.addWidget(QLabel(name))
            fp.addWidget(s)
            fp.addWidget(val)
            fp.addSpacing(10)
        fp.addWidget(self._button("초기화", self.reset_filters, "필터 모두 0으로 (0)"))
        fp.addStretch(1)
        self.filter_panel.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.video_area, 1)
        layout.addLayout(bar)
        layout.addLayout(row2)
        layout.addWidget(self.filter_panel)

        self._create_player()
        self._update_auto_button()
        self._update_loop_button()

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
            hr_seek="yes",              # 1초·10초 이동도 정확하게
            osd_font_size=36,
        )
        for k, v in self._eq.items():
            self._set_prop(k, v)
        self._apply_audio_device(self._audio_device, save=False)

    def _alive(self):
        if self.player is None:
            return False
        try:
            self.player.check_core_alive()
            return True
        except Exception:
            return False

    def _set_prop(self, name, value):
        try:
            setattr(self.player, name, value)
        except Exception:
            pass

    def _pos(self):
        try:
            return self.player.time_pos
        except Exception:
            return None

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
        self._advancing = False
        self._clear_loop()
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
        self.setWindowTitle(f"{v['filename']}   |   F1: 단축키 도움말")
        self.show()
        self.raise_()
        self.activateWindow()
        self.timer.start()
        return True

    def play_neighbor(self, step):
        if not self.video or not self.next_provider:
            return
        nxt = self.next_provider(self.video["id"], step)
        if not nxt:
            self.osd("마지막 영상입니다" if step > 0 else "첫 영상입니다")
            return
        self.open_video(nxt)

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
        self._clear_loop()
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
            eof = bool(self.player.eof_reached)
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

        # 자동 재생: 끝까지 가면 다음 영상
        if not eof:
            self._advancing = False
        elif self._autoplay and not self._advancing:
            self._advancing = True
            self.osd("다음 영상으로 넘어갑니다", 1000)
            QTimer.singleShot(800, lambda: self.play_neighbor(1))

    # ---------- 이동 ----------
    def toggle_pause(self):
        if self._alive():
            self.player.pause = not self.player.pause

    def seek(self, sec):
        if not self._alive():
            return
        try:
            self.player.seek(sec, "relative")
        except Exception:
            return
        amount = f"{abs(sec) // 60}분" if abs(sec) >= 60 else f"{abs(sec)}초"
        self.osd(f"{'+' if sec > 0 else '-'}{amount}")

    def frame_step(self, forward):
        if not self._alive():
            return
        try:
            self.player.command("frame-step" if forward else "frame-back-step")
        except Exception:
            return
        arrow = "다음 프레임 ▶" if forward else "◀ 이전 프레임"
        QTimer.singleShot(150, lambda: self.osd(f"{arrow}   {_t_ms(self._pos())}"))

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

    # ---------- 구간 반복 (요구사항 9번) ----------
    def cycle_loop(self):
        if not self._alive() or not self.video:
            return
        pos = self._pos()
        if pos is None:
            return
        if self._loop_a is None:
            self._loop_a = pos
            self.osd(f"구간 반복 시작점 A: {_t_ms(pos)}   (L을 한 번 더 누르면 끝점 B)", 2500)
        elif self._loop_b is None:
            if pos <= self._loop_a + 0.2:
                self.osd("끝점 B는 시작점 A보다 뒤여야 합니다")
                return
            self._loop_b = pos
            self._set_prop("ab_loop_a", self._loop_a)
            self._set_prop("ab_loop_b", self._loop_b)
            self.player.seek(self._loop_a, "absolute")
            self.osd(f"구간 반복: {_t(self._loop_a)} ~ {_t(self._loop_b)}   (L: 해제)", 2500)
        else:
            self._clear_loop()
            self.osd("구간 반복 해제")
        self._update_loop_button()

    def _clear_loop(self):
        self._loop_a = self._loop_b = None
        if self._alive():
            self._set_prop("ab_loop_a", "no")
            self._set_prop("ab_loop_b", "no")
        self._update_loop_button()

    def _update_loop_button(self):
        if self._loop_a is None:
            self.btn_loop.setText("🔁 구간반복")
            self.btn_loop.setStyleSheet("")
        elif self._loop_b is None:
            self.btn_loop.setText(f"🔁 A {_t(self._loop_a)} ~ ?")
            self.btn_loop.setStyleSheet("color:#ffb74d;")
        else:
            self.btn_loop.setText(f"🔁 {_t(self._loop_a)} ~ {_t(self._loop_b)}")
            self.btn_loop.setStyleSheet("color:#4fc3f7;")

    # ---------- 자동 재생 (요구사항 9번) ----------
    def toggle_autoplay(self):
        self._autoplay = not self._autoplay
        library.set_setting(self.conn, "autoplay", self._autoplay)
        self._update_auto_button()
        self.osd("자동 재생 켜짐: 끝나면 다음 영상" if self._autoplay else "자동 재생 꺼짐")

    def _update_auto_button(self):
        self.btn_auto.setText("⏭ 자동재생 ON" if self._autoplay else "⏭ 자동재생 OFF")
        self.btn_auto.setStyleSheet("color:#66bb6a;" if self._autoplay else "color:#888;")

    # ---------- 필터 (요구사항 10번) ----------
    def toggle_filter_panel(self):
        self.filter_panel.setVisible(not self.filter_panel.isVisible())

    def _on_filter(self, key, value):
        self._eq[key] = value
        self.filter_values[key].setText(f"{value:+d}" if value else "0")
        if self._alive():
            self._set_prop(key, value)
        changed = any(self._eq.values())
        self.btn_filter.setText("🎨 필터 ●" if changed else "🎨 필터")
        self.btn_filter.setStyleSheet("color:#ffb74d;" if changed else "")

    def nudge_filter(self, key, d):
        s = self.filter_sliders[key]
        s.setValue(s.value() + d)
        self.osd(f"{FILTER_NAMES[key]} {s.value():+d}")

    def reset_filters(self):
        for s in self.filter_sliders.values():
            s.setValue(0)
        self.osd("필터 초기화")

    # ---------- 소리 / 사운드 장치 (요구사항 14번) ----------
    def toggle_mute(self):
        if not self._alive():
            return
        self.player.mute = not self.player.mute
        self._muted = bool(self.player.mute)
        self.osd("음소거" if self._muted else "소리 켜짐")

    def change_volume(self, d):
        if not self._alive():
            return
        vol = max(0, min(130, (self.player.volume or 100) + d))
        self.player.volume = vol
        self._volume = vol
        self.osd(f"볼륨 {int(vol)}%")

    def _device_list(self):
        if not self._alive():
            return []
        try:
            devices = list(self.player.audio_device_list or [])
        except Exception:
            devices = []
        if not any(d.get("name") == "auto" for d in devices):
            devices.insert(0, {"name": "auto", "description": "auto"})
        return devices

    def _apply_audio_device(self, name, save=True):
        names = [d.get("name") for d in self._device_list()]
        if name != "auto" and name not in names:
            name = "auto"           # 저장된 장치가 지금 연결되어 있지 않으면 자동으로
        self._set_prop("audio_device", name)
        self._audio_device = name
        if save:
            library.set_setting(self.conn, "audio_device", name)
            self.osd(f"출력 장치 변경: {self.refresh_audio_label()}")
        else:
            self.refresh_audio_label()

    def refresh_audio_label(self):
        """현재 출력 장치 이름을 버튼에 표시하고, 그 글자를 돌려줌"""
        if self._audio_device == "auto":
            name = audio.default_output_name() or "시스템 기본 장치"
            text = f"🔈 {name} (자동)"
        else:
            desc = next((d.get("description") for d in self._device_list()
                         if d.get("name") == self._audio_device), self._audio_device)
            text = f"🔈 {desc}"
        self.btn_audio.setText(text)
        return text

    def show_audio_menu(self):
        if not self._alive():
            return
        menu = QMenu(self)
        for d in self._device_list():
            name = d.get("name")
            label = "자동 (Windows 기본 장치를 따라감)" if name == "auto" else (d.get("description") or name)
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(name == self._audio_device)
            act.triggered.connect(lambda checked=False, n=name: self._apply_audio_device(n))
        menu.exec(self.btn_audio.mapToGlobal(self.btn_audio.rect().bottomLeft()))

    # ---------- 화면 ----------
    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def show_help(self):
        QMessageBox.information(self, "단축키 도움말", HELP_TEXT)

    def keyPressEvent(self, e):
        k, mod = e.key(), e.modifiers()
        shift = bool(mod & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(mod & Qt.KeyboardModifier.ControlModifier)
        if k in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step = 60 if ctrl else 1 if shift else 10
            self.seek(step if k == Qt.Key.Key_Right else -step)
        elif k == Qt.Key.Key_Period:
            self.frame_step(True)
        elif k == Qt.Key.Key_Comma:
            self.frame_step(False)
        elif k == Qt.Key.Key_Space:
            self.toggle_pause()
        elif k == Qt.Key.Key_M:
            self.toggle_mute()
        elif k == Qt.Key.Key_L:
            self.cycle_loop()
        elif k == Qt.Key.Key_A:
            self.toggle_autoplay()
        elif k == Qt.Key.Key_E:
            self.toggle_filter_panel()
        elif k in FILTER_KEYS:
            key, d = FILTER_KEYS[k]
            self.nudge_filter(key, d)
        elif k == ord("0"):
            self.reset_filters()
        elif k == Qt.Key.Key_Up:
            self.change_volume(5)
        elif k == Qt.Key.Key_Down:
            self.change_volume(-5)
        elif k == Qt.Key.Key_PageDown:
            self.play_neighbor(1)
        elif k == Qt.Key.Key_PageUp:
            self.play_neighbor(-1)
        elif k == Qt.Key.Key_Home:
            self.seek_start()
        elif k == Qt.Key.Key_F1:
            self.show_help()
        elif k in (Qt.Key.Key_F, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.toggle_fullscreen()
        elif k == Qt.Key.Key_Escape:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.close()
        else:
            super().keyPressEvent(e)
