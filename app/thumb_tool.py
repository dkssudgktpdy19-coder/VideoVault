"""썸네일 바꾸기: 원하는 장면 고르기 / 이미지 파일 / 재생 중 현재 장면 / 자동으로 되돌리기"""
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QAction, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QFileDialog, QHBoxLayout,
                               QLabel, QMessageBox, QPushButton, QSlider, QToolBar,
                               QVBoxLayout, QWidget)

from app import config

DATA_DIR = Path(config.DATA_DIR)
THUMB_DIR = Path(getattr(config, "THUMB_DIR", DATA_DIR / "thumbs"))
BASE_DIR = Path(getattr(config, "BASE_DIR", DATA_DIR.parent))
VIDEO_EXTS = {(e if str(e).startswith(".") else "." + str(e)).lower()
              for e in getattr(config, "VIDEO_EXTS", [])}
THUMB_W = 320
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
THUMB_DIR.mkdir(parents=True, exist_ok=True)


def _fmt(t):
    t = max(0.0, float(t or 0))
    h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
    return f"{h}:{m:02d}:{s:04.1f}" if h else f"{m}:{s:04.1f}"


# ---------------- ffmpeg 도우미 ----------------
def probe_duration(path):
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, creationflags=NO_WINDOW)
        return float(r.stdout.strip().splitlines()[0])
    except Exception:
        return 0.0


def grab_frame(path, t, width=THUMB_W):
    """지정 시각의 한 장면을 QImage로 (실패 시 None)"""
    out = Path(tempfile.gettempdir()) / f"vv_grab_{os.getpid()}.jpg"
    out.unlink(missing_ok=True)
    try:
        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", f"{max(t, 0):.3f}",
             "-i", path, "-frames:v", "1", "-vf", f"scale={width}:-2", "-y", str(out)],
            capture_output=True, timeout=30, creationflags=NO_WINDOW)
    except Exception:
        return None
    img = QImage(str(out))
    return None if img.isNull() else img.copy()


# ---------------- 메인 화면 목록에서 영상 정보 얻기 ----------------
def _toolbar(window):
    bars = window.findChildren(QToolBar)
    return bars[0] if bars else window.addToolBar("도구")


def _grid(window):
    from app.video_grid import VideoModel
    for v in window.findChildren(QAbstractItemView):
        if isinstance(v.model(), VideoModel):
            return v
    return None


def _info(x):
    if x is None:
        return None
    if isinstance(x, int):
        return {"id": x}
    d = None
    if isinstance(x, dict):
        d = x
    else:
        try:
            d = dict(x)            # sqlite3.Row
        except Exception:
            try:
                d = dict(vars(x))
            except Exception:
                return None
    if "id" not in d and "video_id" in d:
        d = dict(d, id=d["video_id"])
    return d


def _row_at(model, r):
    for name in ("rows", "_rows", "videos", "_videos", "items", "_items", "data_rows"):
        rows = getattr(model, name, None)
        if isinstance(rows, (list, tuple)):
            if 0 <= r < len(rows):
                return _info(rows[r])
            break
    idx = model.index(r, 0)
    for role in (Qt.UserRole, Qt.UserRole + 1):
        d = _info(idx.data(role))
        if d and "id" in d:
            return d
    return None


def _find_path(d):
    for k in ("path", "full_path", "abs_path", "filepath", "file_path"):
        v = d.get(k)
        if isinstance(v, str) and os.path.isfile(v):
            return v
    for v in d.values():
        if (isinstance(v, str) and len(v) > 3
                and Path(v).suffix.lower() in VIDEO_EXTS and os.path.isfile(v)):
            return v
    return None


def current_video(window):
    view = _grid(window)
    if not view:
        return None
    idx = view.currentIndex()
    sm = view.selectionModel()
    sel = sm.selectedIndexes() if sm else []
    if sel and (not idx.isValid() or idx not in sel):
        idx = sel[0]
    if not idx.isValid():
        return None
    return _row_at(view.model(), idx.row())


def find_by_path(window, path):
    view = _grid(window)
    if not view or not path:
        return None
    model = view.model()
    target = os.path.normcase(os.path.normpath(path))
    for r in range(model.rowCount()):
        d = _row_at(model, r)
        if not d:
            continue
        for v in d.values():
            if isinstance(v, str) and len(v) > 3 and \
                    os.path.normcase(os.path.normpath(v)) == target:
                return d
    return None


def _debug(window):
    view = _grid(window)
    if view:
        print("[thumb_tool] model 속성:", list(vars(view.model()).keys()))
    else:
        print("[thumb_tool] 영상 목록(VideoModel)을 찾지 못함")


# ---------------- DB 저장 ----------------
def _custom_col(conn):
    for row in conn.execute("PRAGMA table_info(videos)"):
        name, typ = row[1], (row[2] or "").upper()
        if "custom" in name.lower():
            return name, ("INT" in typ or typ == "")
    return None, False


def _thumb_value(conn, abs_path):
    """기존 썸네일 저장 방식(전체 경로/상대 경로)에 맞춰 값 만들기"""
    row = conn.execute("SELECT thumb_path FROM videos WHERE thumb_path IS NOT NULL "
                       "AND thumb_path <> '' LIMIT 1").fetchone()
    sample = row[0] if row else ""
    if not sample or os.path.isabs(sample):
        return str(abs_path)
    for base in (THUMB_DIR, DATA_DIR, BASE_DIR):
        if (base / sample).exists():
            return os.path.relpath(abs_path, base)
    return str(abs_path)


def _remove_custom_files(vid):
    for f in THUMB_DIR.glob(f"v{vid}_custom_*.jpg"):
        try:
            f.unlink()
        except OSError:
            pass


def _forget(*keys):
    try:
        from app import video_grid
        fn = getattr(video_grid, "forget_thumb", None)
    except Exception:
        fn = None
    if fn:
        for k in keys:
            if k:
                try:
                    fn(k)
                except Exception:
                    pass


def _refresh(window):
    try:
        window.reload(keep=True)
    except TypeError:
        window.reload()


def save_custom(window, vid, img):
    conn = window.conn
    old = conn.execute("SELECT thumb_path FROM videos WHERE id=?", (vid,)).fetchone()
    old = old[0] if old else None
    if img.width() > THUMB_W:
        img = img.scaledToWidth(THUMB_W, Qt.SmoothTransformation)
    _remove_custom_files(vid)
    out = THUMB_DIR / f"v{vid}_custom_{int(time.time() * 1000)}.jpg"
    if not img.save(str(out), "JPG", 90):
        raise OSError("썸네일 파일을 저장하지 못했습니다.")
    value = _thumb_value(conn, out)
    col, is_int = _custom_col(conn)
    if col:
        conn.execute(f"UPDATE videos SET thumb_path=?, {col}=? WHERE id=?",
                     (value, 1 if is_int else value, vid))
    else:
        conn.execute("UPDATE videos SET thumb_path=? WHERE id=?", (value, vid))
    conn.commit()
    _forget(old, value, str(out))
    _refresh(window)


def revert_auto(window, vid):
    conn = window.conn
    old = conn.execute("SELECT thumb_path FROM videos WHERE id=?", (vid,)).fetchone()
    _remove_custom_files(vid)
    col, is_int = _custom_col(conn)
    if col:
        conn.execute(f"UPDATE videos SET thumb_path=NULL, {col}=? WHERE id=?",
                     (0 if is_int else None, vid))
    else:
        conn.execute("UPDATE videos SET thumb_path=NULL WHERE id=?", (vid,))
    conn.commit()
    _forget(old[0] if old else None)
    _refresh(window)
    if hasattr(window, "rescan_all"):
        QTimer.singleShot(300, lambda: window.rescan_all(quiet=True))


# ---------------- 장면 고르기 창 ----------------
class ThumbDialog(QDialog):
    def __init__(self, window, info, path, start=None):
        super().__init__(window)
        self.main = window
        self.vid = info["id"]
        self.path = path
        name = info.get("title") or Path(path).name
        self.setWindowTitle(f"썸네일 바꾸기 - {name}")
        self.duration = probe_duration(path) or float(info.get("duration") or 0)
        self.tmp = Path(tempfile.gettempdir()) / f"vv_preview_{os.getpid()}.jpg"
        self.image = None
        self._again = False

        self.proc = QProcess(self)
        self.proc.finished.connect(self._done)
        self.proc.errorOccurred.connect(
            lambda e: self.preview.setText("ffmpeg를 실행하지 못했습니다."))
        self.wait = QTimer(self)
        self.wait.setSingleShot(True)
        self.wait.setInterval(150)
        self.wait.timeout.connect(self._render)

        self.preview = QLabel("장면 불러오는 중…")
        self.preview.setFixedSize(640, 360)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet("background:#000; color:#aaa;")

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, max(1, int(self.duration * 10)))
        self.slider.setSingleStep(10)      # ←/→ = 1초
        self.slider.setPageStep(100)       # PgUp/PgDn = 10초
        self.slider.valueChanged.connect(self._moved)
        self.time_lbl = QLabel()
        self.time_lbl.setMinimumWidth(160)

        steps = QHBoxLayout()
        for label, sec in (("-1분", -60), ("-10초", -10), ("-1초", -1),
                           ("+1초", 1), ("+10초", 10), ("+1분", 60)):
            b = QPushButton(label)
            b.setFocusPolicy(Qt.NoFocus)
            b.setAutoDefault(False)
            b.clicked.connect(lambda _=False, s=sec:
                              self.slider.setValue(self.slider.value() + s * 10))
            steps.addWidget(b)
        steps.addStretch(1)
        steps.addWidget(self.time_lbl)

        hint = QLabel("←/→: 1초   PgUp/PgDn: 10초   Home/End: 처음/끝   Enter: 지정")
        hint.setStyleSheet("color:#888;")

        self.btn_set = QPushButton("✅ 이 장면으로 지정 (Enter)")
        self.btn_set.setDefault(True)
        btn_file = QPushButton("🖼 이미지 파일로…")
        btn_auto = QPushButton("↩ 자동 썸네일로 되돌리기")
        btn_close = QPushButton("닫기")
        for b in (btn_file, btn_auto, btn_close):
            b.setAutoDefault(False)
        self.btn_set.clicked.connect(self._apply)
        btn_file.clicked.connect(self._from_file)
        btn_auto.clicked.connect(self._auto)
        btn_close.clicked.connect(self.reject)

        bottom = QHBoxLayout()
        bottom.addWidget(self.btn_set)
        bottom.addWidget(btn_file)
        bottom.addWidget(btn_auto)
        bottom.addStretch(1)
        bottom.addWidget(btn_close)

        lay = QVBoxLayout(self)
        lay.addWidget(self.preview, alignment=Qt.AlignCenter)
        lay.addWidget(self.slider)
        lay.addLayout(steps)
        lay.addWidget(hint)
        lay.addLayout(bottom)

        if start is None:
            start = self.duration * 0.1
        self.slider.setValue(int(max(0.0, start) * 10))
        self._moved()
        self.wait.stop()
        self._render()
        self.slider.setFocus()

    def _t(self):
        return self.slider.value() / 10

    def _moved(self, *_):
        self.time_lbl.setText(f"{_fmt(self._t())} / {_fmt(self.duration)}")
        self.wait.start()

    def _render(self):
        if self.proc.state() != QProcess.ProcessState.NotRunning:
            self._again = True
            return
        self._again = False
        self.tmp.unlink(missing_ok=True)
        self.proc.start(FFMPEG, ["-hide_banner", "-loglevel", "error",
                                 "-ss", f"{self._t():.2f}", "-i", self.path,
                                 "-frames:v", "1", "-vf", "scale=640:-2",
                                 "-y", str(self.tmp)])

    def _done(self, *_):
        img = QImage(str(self.tmp)) if self.tmp.exists() else QImage()
        if not img.isNull():
            self.image = img.copy()
            self.preview.setPixmap(QPixmap.fromImage(self.image).scaled(
                self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.image = None
            self.preview.setPixmap(QPixmap())
            self.preview.setText("이 위치의 장면을 읽지 못했습니다.\n다른 위치를 골라 보세요.")
        if self._again:
            self._render()

    def _apply(self):
        img = grab_frame(self.path, self._t()) or self.image
        if img is None:
            QMessageBox.warning(self, "썸네일", "장면을 읽지 못했습니다.\n"
                                "다른 위치를 고르거나 '이미지 파일로'를 써 보세요.")
            return
        try:
            save_custom(self.main, self.vid, img)
        except Exception as e:
            QMessageBox.critical(self, "썸네일 저장 실패", str(e))
            return
        self.accept()

    def _from_file(self):
        f, _ = QFileDialog.getOpenFileName(self, "썸네일로 쓸 이미지 선택", "",
                                           "이미지 (*.jpg *.jpeg *.png *.webp *.bmp)")
        if not f:
            return
        img = QImage(f)
        if img.isNull():
            QMessageBox.warning(self, "썸네일", "이미지를 열 수 없습니다.")
            return
        try:
            save_custom(self.main, self.vid, img)
        except Exception as e:
            QMessageBox.critical(self, "썸네일 저장 실패", str(e))
            return
        self.accept()

    def _auto(self):
        revert_auto(self.main, self.vid)
        QMessageBox.information(self, "썸네일",
                                "자동 썸네일로 되돌렸습니다.\n잠시 후 자동으로 다시 만들어집니다.")
        self.accept()

    def done(self, r):
        if self.proc.state() != QProcess.ProcessState.NotRunning:
            self.proc.kill()
            self.proc.waitForFinished(1000)
        self.tmp.unlink(missing_ok=True)
        super().done(r)


# ---------------- 실행 진입점 ----------------
def _mpv(window):
    return getattr(getattr(window, "player", None), "player", None)


def open_dialog(window):
    info = current_video(window)
    if not info or "id" not in info:
        _debug(window)
        QMessageBox.information(window, "썸네일 바꾸기", "썸네일을 바꿀 영상을 먼저 클릭해 주세요.")
        return
    path = _find_path(info)
    if not path:
        QMessageBox.warning(window, "썸네일 바꾸기",
                            "영상 파일을 찾을 수 없습니다.\n(외장하드가 연결되어 있는지 확인해 주세요)")
        return
    start = None
    try:                                   # 재생 중인 영상이면 현재 위치에서 시작
        mp = _mpv(window)
        if mp and mp.path and os.path.normcase(os.path.normpath(mp.path)) == \
                os.path.normcase(os.path.normpath(path)):
            start = float(mp.time_pos or 0)
    except Exception:
        pass
    ThumbDialog(window, info, path, start).exec()


def capture_from_player(window):
    pw = getattr(window, "player", None)
    mp = _mpv(window)
    try:
        path, t = mp.path, float(mp.time_pos or 0)
    except Exception:
        path, t = None, 0.0
    if not path:
        QMessageBox.information(pw or window, "썸네일", "재생 중인 영상이 없습니다.")
        return
    info = find_by_path(window, path)
    if not info or "id" not in info:
        QMessageBox.information(pw, "썸네일", "메인 화면 목록에서 이 영상을 찾지 못했습니다.\n"
                                "검색·필터를 해제하고 다시 시도해 주세요.")
        return
    img = None
    shot = Path(tempfile.gettempdir()) / f"vv_shot_{os.getpid()}.png"
    try:
        shot.unlink(missing_ok=True)
        mp.screenshot_to_file(str(shot), includes="video")
        q = QImage(str(shot))
        if not q.isNull():
            img = q.copy()
    except Exception as e:
        print("[썸네일] mpv 캡처 실패, ffmpeg로 재시도:", e)
    if img is None:
        img = grab_frame(path, t)
    if img is None:
        QMessageBox.warning(pw, "썸네일", "현재 장면을 캡처하지 못했습니다.")
        return
    try:
        save_custom(window, info["id"], img)
    except Exception as e:
        QMessageBox.critical(pw, "썸네일 저장 실패", str(e))
        return
    try:
        mp.show_text("썸네일을 현재 장면으로 바꿨습니다", "2000")
    except Exception:
        pass


def install(window):
    tb = _toolbar(window)
    act = QAction("🖼 썸네일", window)
    act.setToolTip("선택한 영상의 썸네일 바꾸기 (Ctrl+T)")
    act.triggered.connect(lambda: open_dialog(window))
    tb.addAction(act)

    sc = QShortcut(QKeySequence("Ctrl+T"), window)
    sc.activated.connect(lambda: open_dialog(window))

    pw = getattr(window, "player", None)
    if isinstance(pw, QWidget):
        sc2 = QShortcut(QKeySequence("Ctrl+T"), pw)
        sc2.activated.connect(lambda: capture_from_player(window))
