"""썸네일에 커서를 올리면 장면 8개를 차례로 보여 주는 미리보기"""
import os
import shutil
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtGui import QAction, QPixmap

from app import library
from app import thumb_tool as tt
from app.config import THUMB_DIR

PREVIEW_DIR = Path(THUMB_DIR) / "preview"
FRAMES = 8              # 장면 개수
WIDTH = 400             # 미리보기 그림 폭
HOVER_DELAY = 500       # 커서를 올리고 몇 ms 뒤 시작
FRAME_GAP = 700         # 장면이 바뀌는 간격(ms)
NO_WINDOW = 0x08000000
FFMPEG = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"


def frame_path(vid, i):
    return PREVIEW_DIR / f"{vid}_{i}.jpg"


def _run(cmd):
    try:
        subprocess.run(cmd, capture_output=True, timeout=30, creationflags=NO_WINDOW)
    except Exception:
        pass


def _grab(path, t, out):
    """한 장면 저장 (빠른 방식 → 실패하면 정확한 방식). 다 쓴 뒤 이름을 바꿔서 반쯤 쓴 파일을 읽지 않게"""
    tmp = out.with_name(out.stem + ".tmp.jpg")
    tail = ["-i", path, "-an", "-sn", "-frames:v", "1", "-vf", f"scale={WIDTH}:-2",
            "-q:v", "5", str(tmp)]
    head = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    _run(head + ["-skip_frame", "nokey", "-noaccurate_seek", "-ss", f"{t:.2f}"] + tail)
    if not (tmp.exists() and tmp.stat().st_size > 0):
        _run(head + ["-ss", f"{t:.2f}"] + tail)
    try:
        if tmp.exists() and tmp.stat().st_size > 0:
            os.replace(tmp, out)
        elif tmp.exists():
            tmp.unlink()
    except OSError:
        pass


class HoverPreview(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.w = window
        self.view = window.view
        self.delegate = self.view.itemDelegate()
        self.enabled = bool(library.get_setting(window.conn, "hover_preview", True))
        self.vid = None
        self.row = None
        self.frame = -1
        self.busy, self.failed = set(), set()
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

        self.delay = QTimer(self)
        self.delay.setSingleShot(True)
        self.delay.setInterval(HOVER_DELAY)
        self.delay.timeout.connect(self._start)
        self.flip = QTimer(self)
        self.flip.setInterval(FRAME_GAP)
        self.flip.timeout.connect(self._next)

        self.view.viewport().installEventFilter(self)
        window.model.modelReset.connect(self._clear)

    # ---------- 마우스 ----------
    def eventFilter(self, obj, e):
        t = e.type()
        if t == QEvent.Type.MouseMove:
            self._hover(e.position().toPoint())
        elif t in (QEvent.Type.Leave, QEvent.Type.MouseButtonPress,
                   QEvent.Type.MouseButtonDblClick, QEvent.Type.Wheel):
            self._clear()
        return False

    def _hover(self, pos):
        if not self.enabled:
            return
        idx = self.view.indexAt(pos)
        rows = self.w.model.rows
        vid = rows[idx.row()]["id"] if idx.isValid() and idx.row() < len(rows) else None
        if vid == self.vid:
            return
        self._clear()
        if vid is not None:
            self.vid, self.row = vid, idx.row()
            self.delay.start()

    def _clear(self):
        self.delay.stop()
        self.flip.stop()
        had = self.delegate.preview_id is not None
        self.delegate.preview_id = None
        self.delegate.preview_pix = None
        if had:
            self._repaint()
        self.vid = self.row = None
        self.frame = -1

    def _video(self):
        rows = self.w.model.rows
        if self.row is None or self.row >= len(rows) or rows[self.row]["id"] != self.vid:
            return None
        return rows[self.row]

    def _repaint(self):
        if self.row is not None and self.row < len(self.w.model.rows):
            self.view.update(self.w.model.index(self.row))

    # ---------- 장면 만들기 / 넘기기 ----------
    def _start(self):
        v = self._video()
        if not v or not v.get("online") or not v.get("duration") or v["id"] in self.failed:
            return
        missing = [i for i in range(FRAMES) if not frame_path(v["id"], i).exists()]
        if missing and v["id"] not in self.busy:
            self.busy.add(v["id"])
            threading.Thread(target=self._make, daemon=True,
                             args=(v["id"], v["full_path"], float(v["duration"]), missing)).start()
        self.frame = -1
        self._next()
        self.flip.start()

    def _make(self, vid, path, dur, missing):
        try:
            for i in missing:
                _grab(path, dur * (i + 0.5) / FRAMES, frame_path(vid, i))
            if not any(frame_path(vid, i).exists() for i in range(FRAMES)):
                self.failed.add(vid)       # 장면을 못 읽는 영상은 다시 시도 안 함
        finally:
            self.busy.discard(vid)

    def _next(self):
        if self.vid is None:
            return
        ready = [i for i in range(FRAMES) if frame_path(self.vid, i).exists()]
        if not ready:
            return                         # 아직 준비 중이면 원래 썸네일 그대로
        self.frame = next((i for i in ready if i > self.frame), ready[0])
        pix = QPixmap(str(frame_path(self.vid, self.frame)))
        if pix.isNull():
            return
        self.delegate.preview_id = self.vid
        self.delegate.preview_pix = pix
        self._repaint()

    # ---------- 켜기/끄기 ----------
    def set_enabled(self, on):
        self.enabled = bool(on)
        library.set_setting(self.w.conn, "hover_preview", self.enabled)
        if not on:
            self._clear()


def install(window):
    hp = HoverPreview(window)
    window._vv_preview = hp
    act = QAction("👁 미리보기", window)
    act.setCheckable(True)
    act.setChecked(hp.enabled)
    act.setToolTip("썸네일에 커서를 올리면 장면 8개를 차례로 보여 줌 (켜기/끄기)")
    act.toggled.connect(hp.set_enabled)
    tt._toolbar(window).addAction(act)
