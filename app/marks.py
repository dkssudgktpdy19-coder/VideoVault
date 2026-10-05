"""장면 북마크: 재생 중 B로 표시, [ / ] 로 이동, 북마크 모음에서 바로 재생"""
import os
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)

from app import thumb_tool as tt

MARK_DIR = tt.THUMB_DIR / "marks"
MARK_W = 240
MARK_DIR.mkdir(parents=True, exist_ok=True)


def ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS scene_marks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        t REAL NOT NULL,
        memo TEXT DEFAULT '',
        thumb TEXT DEFAULT '',
        created REAL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_marks_video ON scene_marks(video_id, t)")
    conn.commit()


# ---------------- 도우미 ----------------
def _norm(p):
    return os.path.normcase(os.path.normpath(p)) if p else ""


def _playing(window):
    mp = tt._mpv(window)
    try:
        return mp.path, float(mp.time_pos or 0)
    except Exception:
        return None, 0.0


def _osd(window, text):
    try:
        tt._mpv(window).show_text(text, "2000")
    except Exception:
        pass


def _seek(mp, t):
    try:
        mp.seek(t, "absolute", "exact")
    except Exception:
        mp.time_pos = t


def vid_for_path(window, path):
    d = tt.find_by_path(window, path)
    if d and "id" in d:
        return d["id"]
    target = _norm(path)
    rows = window.conn.execute("SELECT id, rel_path FROM videos WHERE rel_path LIKE ?",
                               ("%" + os.path.basename(path),)).fetchall()
    for vid, rel in rows:
        if rel and target.endswith(_norm(rel).lstrip("\\/")):
            return vid
    return rows[0][0] if len(rows) == 1 else None


def _grid_row(window, vid):
    view = tt._grid(window)
    if not view:
        return None, None, None
    model = view.model()
    for r in range(model.rowCount()):
        d = tt._row_at(model, r)
        if d and d.get("id") == vid:
            return view, r, d
    return view, None, None


def _emit_play(view, idx):
    """메인 화면에서 더블클릭한 것과 똑같이 재생 시작"""
    mo = view.metaObject()
    for name, sig in (("doubleClicked(QModelIndex)", view.doubleClicked),
                      ("activated(QModelIndex)", view.activated)):
        i = mo.indexOfSignal(name)
        try:
            connected = i >= 0 and view.isSignalConnected(mo.method(i))
        except Exception:
            connected = False
        if connected:
            sig.emit(idx)
            return
    view.doubleClicked.emit(idx)


def _seek_when_ready(window, path, t):
    """새 영상이 열릴 때까지 기다렸다가 북마크 위치로 이동 (이어보기보다 나중에 한 번 더)"""
    target = _norm(path)
    timer = QTimer(window)
    timer.setInterval(150)
    st = {"n": 0, "hit": None}

    def tick():
        st["n"] += 1
        mp = tt._mpv(window)
        try:
            ready = bool(mp and mp.path and _norm(mp.path) == target and mp.duration)
        except Exception:
            ready = False
        if ready and st["hit"] is None:
            st["hit"] = st["n"]
            _seek(mp, t)
        elif ready and st["n"] - st["hit"] >= 3:
            _seek(mp, t)
            _osd(window, f"🔖 {tt._fmt(t)}")
            timer.stop()
            timer.deleteLater()
            return
        if st["n"] > 60:
            timer.stop()
            timer.deleteLater()

    timer.timeout.connect(tick)
    timer.start()


def play_at(window, vid, t):
    pw = getattr(window, "player", None)
    cur, _ = _playing(window)
    if cur and vid_for_path(window, cur) == vid:          # 이미 열려 있는 영상
        _seek(tt._mpv(window), t)
        if pw:
            pw.show()
            pw.raise_()
            pw.activateWindow()
        _osd(window, f"🔖 {tt._fmt(t)}")
        return True
    view, r, d = _grid_row(window, vid)
    path = tt._find_path(d) if d else None
    if r is None or not path:
        return False
    idx = view.model().index(r, 0)
    view.setCurrentIndex(idx)
    view.scrollTo(idx)
    _emit_play(view, idx)
    _seek_when_ready(window, path, t)
    return True


# ---------------- 북마크 추가 / 이동 ----------------
def _snapshot(window, path, t, mark_id):
    out = MARK_DIR / f"m{mark_id}.jpg"
    img = None
    shot = Path(tempfile.gettempdir()) / f"vv_mark_{os.getpid()}.png"
    try:
        shot.unlink(missing_ok=True)
        tt._mpv(window).screenshot_to_file(str(shot), includes="video")
        q = QImage(str(shot))
        if not q.isNull():
            img = q.copy()
    except Exception:
        pass
    if img is None:
        img = tt.grab_frame(path, t, MARK_W)
    if img is None:
        return ""
    if img.width() > MARK_W:
        img = img.scaledToWidth(MARK_W, Qt.SmoothTransformation)
    return str(out) if img.save(str(out), "JPG", 85) else ""


def add_mark(window, ask_memo=False):
    pw = getattr(window, "player", None)
    path, t = _playing(window)
    if not path:
        return
    vid = vid_for_path(window, path)
    if vid is None:
        QMessageBox.information(pw, "북마크", "이 영상을 목록에서 찾지 못했습니다.\n"
                                "검색·필터를 해제하고 다시 시도해 주세요.")
        return
    memo = ""
    if ask_memo:
        mp = tt._mpv(window)
        try:
            was_paused = bool(mp.pause)
            mp.pause = True
        except Exception:
            was_paused = True
        text, ok = QInputDialog.getText(pw, "북마크 메모", f"{tt._fmt(t)} 장면 메모:",
                                        QLineEdit.EchoMode.Normal, "")
        try:
            mp.pause = was_paused
        except Exception:
            pass
        if not ok:
            return
        memo = text.strip()
    conn = window.conn
    cur = conn.execute("INSERT INTO scene_marks(video_id, t, memo, created) VALUES (?,?,?,?)",
                       (vid, t, memo, time.time()))
    mid = cur.lastrowid
    conn.execute("UPDATE scene_marks SET thumb=? WHERE id=?",
                 (_snapshot(window, path, t, mid), mid))
    conn.commit()
    _osd(window, f"🔖 북마크 추가 {tt._fmt(t)}" + (f"  {memo}" if memo else ""))


def jump(window, direction):
    path, t = _playing(window)
    if not path:
        return
    vid = vid_for_path(window, path)
    times = [r[0] for r in window.conn.execute(
        "SELECT t FROM scene_marks WHERE video_id=? ORDER BY t", (vid,))]
    if not times:
        _osd(window, "이 영상에는 북마크가 없습니다 (B로 추가)")
        return
    if direction > 0:
        cand = [x for x in times if x > t + 0.5]
        target = cand[0] if cand else None
    else:
        cand = [x for x in times if x < t - 1.5]
        target = cand[-1] if cand else None
    if target is None:
        _osd(window, "다음 북마크 없음" if direction > 0 else "이전 북마크 없음")
        return
    _seek(tt._mpv(window), target)
    _osd(window, f"🔖 {times.index(target) + 1}/{len(times)}  {tt._fmt(target)}")


# ---------------- 북마크 모음 창 ----------------
class MarksDialog(QDialog):
    def __init__(self, window, parent, vid=None, only=False):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.main = window
        self.vid = vid
        self.setWindowTitle("🔖 장면 북마크")
        self.resize(760, 580)

        self.search = QLineEdit()
        self.search.setPlaceholderText("제목·메모 검색")
        self.search.textChanged.connect(self.load)
        self.only = QCheckBox("이 영상만")
        self.only.setEnabled(vid is not None)
        self.only.setChecked(bool(only and vid is not None))
        self.only.toggled.connect(self.load)
        self.count = QLabel()
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.only)
        top.addWidget(self.count)

        self.list = QListWidget()
        self.list.setIconSize(QSize(160, 90))
        self.list.setSpacing(2)
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list.itemActivated.connect(self.play)      # 더블클릭 / Enter

        b_play = QPushButton("▶ 이 장면부터 재생 (Enter)")
        b_memo = QPushButton("✏ 메모 (F2)")
        b_del = QPushButton("🗑 삭제 (Del)")
        b_close = QPushButton("닫기")
        for b in (b_play, b_memo, b_del, b_close):
            b.setAutoDefault(False)
        b_play.clicked.connect(self.play)
        b_memo.clicked.connect(self.edit_memo)
        b_del.clicked.connect(self.delete)
        b_close.clicked.connect(self.reject)
        bottom = QHBoxLayout()
        for b in (b_play, b_memo, b_del):
            bottom.addWidget(b)
        bottom.addStretch(1)
        bottom.addWidget(b_close)

        QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.list).activated.connect(self.delete)
        QShortcut(QKeySequence("F2"), self.list).activated.connect(self.edit_memo)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.list, 1)
        lay.addLayout(bottom)
        self.load()
        self.list.setFocus()

    def load(self, *_):
        conn = self.main.conn
        cols = {r[1] for r in conn.execute("PRAGMA table_info(videos)")}
        tcol = "v.title" if "title" in cols else "NULL"
        sql = (f"SELECT m.id, m.video_id, m.t, m.memo, m.thumb, {tcol}, v.rel_path "
               "FROM scene_marks m JOIN videos v ON v.id = m.video_id")
        args = []
        if self.only.isChecked():
            sql += " WHERE m.video_id = ?"
            args.append(self.vid)
        sql += " ORDER BY (m.video_id = ?) DESC, v.rel_path, m.t"
        args.append(self.vid if self.vid is not None else -1)
        q = self.search.text().strip().lower()
        self.list.clear()
        n = 0
        for mid, vid, t, memo, thumb, title, rel in conn.execute(sql, args):
            name = title or Path(rel or "").stem
            memo = memo or ""
            if q and q not in name.lower() and q not in memo.lower():
                continue
            it = QListWidgetItem(f"{name}\n{tt._fmt(t)}    {memo}")
            if thumb and os.path.isfile(thumb):
                it.setIcon(QIcon(QPixmap(thumb)))
            it.setData(Qt.UserRole, (mid, vid, t, memo))
            self.list.addItem(it)
            n += 1
        self.count.setText(f"{n}개")
        if n:
            self.list.setCurrentRow(0)

    def _cur(self):
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def play(self, *_):
        d = self._cur()
        if not d:
            return
        _, vid, t, _memo = d
        if play_at(self.main, vid, t):
            self.accept()
        else:
            QMessageBox.information(self, "북마크", "메인 화면 목록에서 이 영상을 찾지 못했습니다.\n"
                                    "검색·필터를 해제하거나 외장하드 연결을 확인해 주세요.")

    def edit_memo(self):
        d = self._cur()
        if not d:
            return
        mid, _vid, t, memo = d
        text, ok = QInputDialog.getText(self, "북마크 메모", f"{tt._fmt(t)} 장면 메모:",
                                        QLineEdit.EchoMode.Normal, memo)
        if ok:
            self.main.conn.execute("UPDATE scene_marks SET memo=? WHERE id=?", (text.strip(), mid))
            self.main.conn.commit()
            self.load()

    def delete(self):
        items = self.list.selectedItems()
        if not items:
            return
        if QMessageBox.question(self, "북마크 삭제", f"북마크 {len(items)}개를 삭제할까요?") \
                != QMessageBox.StandardButton.Yes:
            return
        conn = self.main.conn
        for it in items:
            mid = it.data(Qt.UserRole)[0]
            row = conn.execute("SELECT thumb FROM scene_marks WHERE id=?", (mid,)).fetchone()
            conn.execute("DELETE FROM scene_marks WHERE id=?", (mid,))
            if row and row[0]:
                try:
                    os.remove(row[0])
                except OSError:
                    pass
        conn.commit()
        self.load()


def open_from_main(window):
    info = tt.current_video(window)
    MarksDialog(window, window, info.get("id") if info else None, only=False).exec()


def open_from_player(window):
    pw = getattr(window, "player", None)
    path, _ = _playing(window)
    vid = vid_for_path(window, path) if path else None
    MarksDialog(window, pw, vid, only=vid is not None).exec()


def install(window):
    ensure_table(window.conn)
    tb = tt._toolbar(window)
    act = QAction("🔖 북마크", window)
    act.setToolTip("장면 북마크 모음 (Ctrl+B)")
    act.triggered.connect(lambda: open_from_main(window))
    tb.addAction(act)
    QShortcut(QKeySequence("Ctrl+B"), window).activated.connect(lambda: open_from_main(window))

    pw = getattr(window, "player", None)
    if isinstance(pw, QWidget):
        for key, fn in (("B", lambda: add_mark(window)),
                        ("Shift+B", lambda: add_mark(window, True)),
                        ("Ctrl+B", lambda: open_from_player(window)),
                        ("[", lambda: jump(window, -1)),
                        ("]", lambda: jump(window, 1))):
            QShortcut(QKeySequence(key), pw).activated.connect(fn)
