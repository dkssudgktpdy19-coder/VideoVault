"""얼굴 정리 화면 (3단계-2)
- 묶인 얼굴에 이름 붙이기 → 배우 등록 + 그 얼굴이 나온 영상에 배우 자동 추가
- 같은 사람 합치기 / 잘못 묶인 얼굴 빼기 / 숨기기 / 비슷한 사람 찾기
- 새 영상 얼굴 자동 분석 (재생 중에는 잠시 멈춤)
"""
import json
import os
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QSpinBox, QSplitter, QVBoxLayout,
                               QWidget)

from app import faces as fc
from app import thumb_tool as tt

STATE_FILE = fc.DATA_DIR / "face_state.json"
AUTO_FIRST = 90 * 1000          # 프로그램 켜고 90초 뒤 첫 자동 분석
AUTO_EVERY = 10 * 60 * 1000     # 그 뒤 10분마다 새 영상 확인
YES = QMessageBox.StandardButton.Yes
ROLE_ID, ROLE_NAME, ROLE_CEN, ROLE_CNT = (Qt.UserRole, Qt.UserRole + 1,
                                          Qt.UserRole + 2, Qt.UserRole + 3)


# ---------------- 설정 ----------------
def _state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _set_state(**kw):
    s = _state()
    s.update(kw)
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- DB ----------------
def ensure(conn):
    fc.ensure_tables(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS face_actor_sync("
                 "video_id INTEGER, actor_id INTEGER, PRIMARY KEY(video_id, actor_id))")
    conn.commit()


def _va(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(video_actors)")]
    v = "video_id" if "video_id" in cols else next(c for c in cols if "video" in c)
    a = "actor_id" if "actor_id" in cols else next(c for c in cols if "actor" in c)
    return v, a, "source" in cols


def actor_id(conn, name):
    row = conn.execute("SELECT id FROM actors WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row[0]
    try:
        from app import tags
        r = tags.get_or_create(conn, "actor", name)
        if isinstance(r, int):
            return r
    except Exception:
        pass
    return conn.execute("INSERT INTO actors(name) VALUES (?)", (name,)).lastrowid


def sync_actors(conn):
    """이름 붙은 얼굴이 나온 영상에 배우 추가 (한 번 추가한 건 다시 안 함 → 직접 지운 건 유지)"""
    v, a, has_src = _va(conn)
    pairs = conn.execute("""
        SELECT DISTINCT f.video_id, p.actor_id FROM video_faces f
        JOIN face_people p ON p.id = f.person_id
        JOIN actors ac ON ac.id = p.actor_id
        WHERE p.hidden = 0 AND NOT EXISTS (SELECT 1 FROM face_actor_sync s
              WHERE s.video_id = f.video_id AND s.actor_id = p.actor_id)""").fetchall()
    added = 0
    for vid, aid in pairs:
        if not conn.execute(f"SELECT 1 FROM video_actors WHERE {v}=? AND {a}=?",
                            (vid, aid)).fetchone():
            if has_src:
                conn.execute(f"INSERT INTO video_actors({v}, {a}, source) VALUES (?,?,'face')",
                             (vid, aid))
            else:
                conn.execute(f"INSERT INTO video_actors({v}, {a}) VALUES (?,?)", (vid, aid))
            added += 1
        conn.execute("INSERT OR IGNORE INTO face_actor_sync VALUES (?,?)", (vid, aid))
    conn.commit()
    return added


def _unsync(conn, aid, vids):
    """얼굴 때문에 자동으로 붙였던 배우만 떼기"""
    v, a, _ = _va(conn)
    for vid in vids:
        if conn.execute("SELECT 1 FROM face_actor_sync WHERE video_id=? AND actor_id=?",
                        (vid, aid)).fetchone():
            conn.execute(f"DELETE FROM video_actors WHERE {v}=? AND {a}=?", (vid, aid))
            conn.execute("DELETE FROM face_actor_sync WHERE video_id=? AND actor_id=?", (vid, aid))


def _videos_of(conn, pid):
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT video_id FROM video_faces WHERE person_id=?", (pid,))]


def recompute(conn, pid):
    embs = [np.frombuffer(e, np.float32) for (e,) in
            conn.execute("SELECT emb FROM video_faces WHERE person_id=?", (pid,))]
    if not embs:
        conn.execute("DELETE FROM face_people WHERE id=?", (pid,))
        return
    s = np.sum(embs, axis=0)
    s = s / np.linalg.norm(s)
    conn.execute("UPDATE face_people SET centroid=?, n=? WHERE id=?",
                 (s.astype(np.float32).tobytes(), len(embs), pid))


def merge(conn, target, sources):
    t_aid = conn.execute("SELECT actor_id FROM face_people WHERE id=?", (target,)).fetchone()[0]
    for src in sources:
        row = conn.execute("SELECT actor_id, name FROM face_people WHERE id=?", (src,)).fetchone()
        if not row:
            continue
        s_aid, s_name = row
        if t_aid is None and s_aid is not None:
            t_aid = s_aid
            conn.execute("UPDATE face_people SET actor_id=?, name=? WHERE id=?",
                         (s_aid, s_name, target))
        elif s_aid is not None and s_aid != t_aid:
            _unsync(conn, s_aid, _videos_of(conn, src))
        conn.execute("UPDATE video_faces SET person_id=? WHERE person_id=?", (target, src))
        conn.execute("DELETE FROM face_people WHERE id=?", (src,))
    recompute(conn, target)
    conn.commit()
    return sync_actors(conn)


def name_person(conn, pid, name):
    aid = actor_id(conn, name)
    old = conn.execute("SELECT actor_id FROM face_people WHERE id=?", (pid,)).fetchone()
    if old and old[0] is not None and old[0] != aid:
        _unsync(conn, old[0], _videos_of(conn, pid))
    conn.execute("UPDATE face_people SET actor_id=?, name=?, hidden=0 WHERE id=?",
                 (aid, name, pid))
    others = [r[0] for r in conn.execute(
        "SELECT id FROM face_people WHERE actor_id=? AND id<>?", (aid, pid))]
    conn.commit()
    if others:
        return merge(conn, pid, others)
    return sync_actors(conn)


def not_this_person(conn, face_ids):
    touched = set()
    for fid in face_ids:
        row = conn.execute("""SELECT f.person_id, f.video_id, f.emb, p.actor_id FROM video_faces f
                              LEFT JOIN face_people p ON p.id = f.person_id
                              WHERE f.id=?""", (fid,)).fetchone()
        if not row:
            continue
        pid, vid, emb, aid = row
        new = conn.execute("INSERT INTO face_people(centroid, n, created) VALUES (?,1,?)",
                           (emb, time.time())).lastrowid
        conn.execute("UPDATE video_faces SET person_id=? WHERE id=?", (new, fid))
        if aid and not conn.execute("SELECT 1 FROM video_faces WHERE person_id=? AND video_id=?",
                                    (pid, vid)).fetchone():
            _unsync(conn, aid, [vid])
        if pid:
            touched.add(pid)
    for pid in touched:
        recompute(conn, pid)
    conn.commit()


def load_people(conn):
    return conn.execute("""
        SELECT p.id, COALESCE(a.name, p.name, ''), p.actor_id, p.hidden, p.centroid,
               COUNT(DISTINCT f.video_id),
               (SELECT thumb FROM video_faces WHERE person_id = p.id
                ORDER BY hits * score DESC LIMIT 1)
        FROM face_people p
        JOIN video_faces f ON f.person_id = p.id
        LEFT JOIN actors a ON a.id = p.actor_id
        GROUP BY p.id ORDER BY COUNT(DISTINCT f.video_id) DESC""").fetchall()


# ---------------- 화면 도우미 ----------------
_ICONS = {}


def _icon(path):
    if not path:
        return QIcon()
    ic = _ICONS.get(path)
    if ic is None:
        pm = QPixmap(path)
        ic = QIcon(pm) if not pm.isNull() else QIcon()
        _ICONS[path] = ic
    return ic


def _refresh_main(window):
    try:
        tt._refresh(window)
    except Exception:
        pass
    try:
        from app import presets
        sb = presets._sidebar(window)
    except Exception:
        sb = None
    if sb is None:
        return
    for name in ("refresh", "reload", "refresh_lists", "reload_lists", "load_lists"):
        fn = getattr(sb, name, None)
        if callable(fn):
            for args in ((), (window.conn,)):
                try:
                    fn(*args)
                    return
                except TypeError:
                    continue
                except Exception:
                    return


def _folders(conn):
    try:
        from app import library
        res = library.folder_paths(conn)
    except Exception as e:
        print("[얼굴] 등록 폴더를 읽지 못함:", e)
        return []
    if isinstance(res, tuple) and res and isinstance(res[0], (list, tuple)):
        res = res[0]
    out = []
    for p in res or []:
        if isinstance(p, (list, tuple)):
            p = next((x for x in p if isinstance(x, str)), None)
        if isinstance(p, str) and os.path.isdir(p):
            out.append(p)
    return out


def _shortcut(key, widget, fn):
    sc = QShortcut(QKeySequence(key), widget)
    sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
    sc.activated.connect(fn)


# ---------------- 백그라운드 분석 ----------------
class AnalyzeThread(QThread):
    progress = Signal(str)

    def __init__(self, folders):
        super().__init__()
        self.folders = folders
        self.stop = False
        self.paused = False
        self.done_n = 0

    def run(self):
        conn = None
        try:
            conn = fc.open_db()
            fc.ensure_tables(conn)
            done = {r[0] for r in conn.execute("SELECT video_id FROM face_scan")}
            todo = {}
            for f in self.folders:
                for v in fc.find_videos(conn, f):
                    if v[0] not in done:
                        todo[v[0]] = v
            todo = list(todo.values())
            if not todo:
                self.progress.emit("")
                return
            self.progress.emit(f"👤 얼굴 분석 준비 중… ({len(todo)}개)")
            eng = fc.FaceEngine()
            for i, (vid, path, dur) in enumerate(todo, 1):
                while self.paused and not self.stop:
                    self.msleep(500)
                if self.stop:
                    break
                try:
                    fc.process_video(eng, conn, vid, path, dur)
                except Exception as e:
                    print("[얼굴 분석 오류]", Path(path).name, e)
                self.done_n = i
                if i % 20 == 0:
                    try:
                        fc.cluster(conn)
                    except Exception as e:
                        print("[얼굴 묶기 오류]", e)
                self.progress.emit(f"👤 얼굴 분석 {i}/{len(todo)}")
            fc.cluster(conn)
        except Exception as e:
            print("[얼굴 분석 실패]", e)
            self.progress.emit("⚠ 얼굴 분석 실패 (터미널 확인)")
        finally:
            if conn is not None:
                conn.close()


class Analyzer:
    def __init__(self, window):
        self.w = window
        self.th = None
        self.text = ""
        self.label = QLabel("")
        try:
            window.statusBar().addPermanentWidget(self.label)
        except Exception:
            pass
        self.pause_timer = QTimer(window)
        self.pause_timer.setInterval(1000)
        self.pause_timer.timeout.connect(self._tick)
        self.auto_timer = QTimer(window)
        self.auto_timer.setInterval(AUTO_EVERY)
        self.auto_timer.timeout.connect(lambda: self.start())

    def running(self):
        return bool(self.th and self.th.isRunning())

    def start(self, manual=False):
        if getattr(self.w, "_closing", False):
            return
        if self.running():
            if manual:
                QMessageBox.information(self.w, "얼굴 분석", "이미 분석 중입니다.")
            return
        folders = _folders(self.w.conn)
        if not folders:
            if manual:
                QMessageBox.information(self.w, "얼굴 분석",
                                        "분석할 폴더가 없습니다.\n(등록 폴더·외장하드 연결 확인)")
            return
        self.th = AnalyzeThread(folders)
        self.th.progress.connect(self._progress)
        self.th.finished.connect(self._finished)
        self.th.start(QThread.Priority.LowPriority)
        self.pause_timer.start()
        self._progress("👤 새 영상 확인 중…")

    def _tick(self):
        pw = getattr(self.w, "player", None)
        if self.th:
            self.th.paused = bool(isinstance(pw, QWidget) and pw.isVisible())
        self._show()

    def _progress(self, text):
        self.text = text
        self._show()

    def _show(self):
        t = self.text
        if t and self.th and self.th.paused:
            t += "  ⏸ 재생 중 일시정지"
        self.label.setText(t)
        dlg = getattr(self.w, "_vv_face_dlg", None)
        if dlg is not None:
            dlg.status.setText(t or "대기 중")

    def _finished(self):
        self.pause_timer.stop()
        done = self.th.done_n if self.th else 0
        n = 0
        try:
            n = sync_actors(self.w.conn)
        except Exception as e:
            print("[배우 자동 추가 실패]", e)
        if self.text.startswith("👤"):
            self.text = ""
        self._show()
        if done:
            msg = f"👤 얼굴 분석 완료: 영상 {done}개" + (f", 배우 자동 추가 {n}건" if n else "")
            try:
                self.w.statusBar().showMessage(msg, 10000)
            except Exception:
                pass
        if n:
            _refresh_main(self.w)
        dlg = getattr(self.w, "_vv_face_dlg", None)
        if dlg is not None and dlg.isVisible():
            dlg.load()

    def set_auto(self, on):
        _set_state(auto=bool(on))
        if on:
            self.auto_timer.start()
            QTimer.singleShot(2000, lambda: self.start())
        else:
            self.auto_timer.stop()

    def stop(self):
        self.auto_timer.stop()
        if self.running():
            self.th.stop = True
            if not self.th.wait(5000):
                os._exit(0)


# ---------------- 얼굴 정리 창 ----------------
class FacesDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.main = window
        self.conn = window.conn
        self.sim_ref = None
        self.setWindowTitle("👤 얼굴 정리")
        self.resize(1150, 720)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)

        # 위쪽 조건
        self.search = QLineEdit()
        self.search.setPlaceholderText("이름 검색 (예: 원이, P12)")
        self.search.textChanged.connect(self.load)
        self.min_v = QSpinBox()
        self.min_v.setRange(1, 50)
        self.min_v.setValue(int(_state().get("min_videos", 2)))
        self.min_v.setPrefix("영상 ")
        self.min_v.setSuffix("개 이상")
        self.min_v.valueChanged.connect(self._min_changed)
        self.show_hidden = QCheckBox("숨긴 것도 보기")
        self.show_hidden.toggled.connect(self.load)
        self.info = QLabel()
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.min_v)
        top.addWidget(self.show_hidden)
        top.addWidget(self.info)

        # 왼쪽: 사람 목록
        self.people = QListWidget()
        self.people.setViewMode(QListView.ViewMode.IconMode)
        self.people.setIconSize(QSize(96, 96))
        self.people.setGridSize(QSize(124, 150))
        self.people.setResizeMode(QListView.ResizeMode.Adjust)
        self.people.setMovement(QListView.Movement.Static)
        self.people.setWordWrap(True)
        self.people.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.people.currentItemChanged.connect(self.show_faces)
        self.people.itemActivated.connect(self.name_selected)

        b_name = QPushButton("✏ 이름 붙이기 (F2)")
        b_merge = QPushButton("🔗 합치기 (M)")
        self.b_sim = QPushButton("🔎 비슷한 순 (S)")
        self.b_sim.setCheckable(True)
        b_hide = QPushButton("🙈 숨기기/보이기 (H)")
        b_name.clicked.connect(self.name_selected)
        b_merge.clicked.connect(self.merge_selected)
        self.b_sim.toggled.connect(self.sim_toggled)
        b_hide.clicked.connect(self.hide_selected)
        lb = QHBoxLayout()
        for b in (b_name, b_merge, self.b_sim, b_hide):
            lb.addWidget(b)
        lb.addStretch(1)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("사람 (Ctrl·Shift+클릭으로 여러 명 선택)"))
        ll.addWidget(self.people, 1)
        ll.addLayout(lb)

        # 오른쪽: 그 사람의 얼굴들
        self.face_title = QLabel("왼쪽에서 사람을 고르세요")
        self.face_title.setWordWrap(True)
        self.faces = QListWidget()
        self.faces.setViewMode(QListView.ViewMode.IconMode)
        self.faces.setIconSize(QSize(80, 80))
        self.faces.setGridSize(QSize(100, 116))
        self.faces.setResizeMode(QListView.ResizeMode.Adjust)
        self.faces.setMovement(QListView.Movement.Static)
        self.faces.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.faces.itemActivated.connect(self.play_face)
        b_not = QPushButton("🚫 이 사람 아님 (Del)")
        b_play = QPushButton("▶ 이 장면 재생 (더블클릭)")
        b_not.clicked.connect(self.not_this)
        b_play.clicked.connect(self.play_face)
        rb = QHBoxLayout()
        rb.addWidget(b_not)
        rb.addWidget(b_play)
        rb.addStretch(1)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.face_title)
        rl.addWidget(self.faces, 1)
        rl.addLayout(rb)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([700, 450])

        # 아래쪽: 분석
        b_an = QPushButton("🔍 새 영상 얼굴 분석")
        b_an.clicked.connect(lambda: self.main._vv_faces.start(manual=True))
        self.chk_auto = QCheckBox("새 영상 자동 분석 (재생 중엔 멈춤)")
        self.chk_auto.setChecked(bool(_state().get("auto", True)))
        self.chk_auto.toggled.connect(lambda on: self.main._vv_faces.set_auto(on))
        self.status = QLabel("대기 중")
        self.status.setStyleSheet("color:#aaa;")
        b_close = QPushButton("닫기")
        b_close.clicked.connect(self.hide)
        bottom = QHBoxLayout()
        bottom.addWidget(b_an)
        bottom.addWidget(self.chk_auto)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(b_close)

        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)
            b.setDefault(False)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        lay.addLayout(bottom)

        _shortcut("F2", self.people, self.name_selected)
        _shortcut("M", self.people, self.merge_selected)
        _shortcut("S", self.people, self.b_sim.toggle)
        _shortcut("H", self.people, self.hide_selected)
        _shortcut("Del", self.faces, self.not_this)

    # ----- 목록 -----
    def _min_changed(self, v):
        _set_state(min_videos=int(v))
        self.load()

    def load(self, *_, select=None):
        if select is None:
            cur = self.people.currentItem()
            select = cur.data(ROLE_ID) if cur else None
        q = self.search.text().strip().lower()
        rows = []
        for pid, name, aid, hidden, cen, cnt, thumb in load_people(self.conn):
            if hidden and not self.show_hidden.isChecked():
                continue
            if not name and cnt < self.min_v.value():
                continue
            if q and q not in (name or "").lower() and q != f"p{pid}":
                continue
            sim = None
            if self.sim_ref is not None and cen:
                sim = float(np.frombuffer(cen, np.float32) @ self.sim_ref)
            rows.append((pid, name, hidden, cen, cnt, thumb, sim))
        if self.sim_ref is not None:
            rows.sort(key=lambda r: -(r[6] if r[6] is not None else -1))
        self.people.blockSignals(True)
        self.people.clear()
        target, named = None, 0
        for pid, name, hidden, cen, cnt, thumb, sim in rows:
            label = name or f"P{pid}"
            text = f"{label}\n영상 {cnt}개"
            if sim is not None:
                text += f" · {sim * 100:.0f}%"
            if hidden:
                text += " 🙈"
            it = QListWidgetItem(_icon(thumb), text)
            it.setData(ROLE_ID, pid)
            it.setData(ROLE_NAME, name)
            it.setData(ROLE_CEN, cen)
            it.setData(ROLE_CNT, cnt)
            if name:
                named += 1
                it.setForeground(QColor("#7cc4ff"))
            self.people.addItem(it)
            if pid == select:
                target = it
        self.people.blockSignals(False)
        self.info.setText(f"{len(rows)}명 표시 · 이름 붙임 {named}명")
        if target is not None:
            self.people.setCurrentItem(target)
            self.people.scrollToItem(target)
        self.show_faces(self.people.currentItem())

    def show_faces(self, cur, *_):
        self.faces.clear()
        if cur is None:
            self.face_title.setText("왼쪽에서 사람을 고르세요")
            return
        pid = cur.data(ROLE_ID)
        label = cur.data(ROLE_NAME) or f"P{pid}"
        rows = self.conn.execute("""SELECT f.id, f.video_id, f.thumb, f.t, v.rel_path
                                    FROM video_faces f JOIN videos v ON v.id = f.video_id
                                    WHERE f.person_id=? ORDER BY f.hits * f.score DESC""",
                                 (pid,)).fetchall()
        for fid, vid, thumb, t, rel in rows:
            name = Path(rel or "").stem
            it = QListWidgetItem(_icon(thumb), name[:12])
            it.setToolTip(f"{name}\n{tt._fmt(t)}")
            it.setData(Qt.UserRole, (fid, vid, t or 0.0))
            self.faces.addItem(it)
        self.face_title.setText(f"<b>{label}</b>: 얼굴 {len(rows)}개  "
                                "(더블클릭 = 그 장면 재생, Del = 이 사람 아님)")

    def _sel_people(self):
        items = self.people.selectedItems()
        if not items and self.people.currentItem() is not None:
            items = [self.people.currentItem()]
        return items

    # ----- 동작 -----
    def name_selected(self, *_):
        items = self._sel_people()
        if not items:
            return
        cur_name = next((it.data(ROLE_NAME) for it in items if it.data(ROLE_NAME)), "")
        names = [r[0] for r in self.conn.execute("SELECT name FROM actors ORDER BY name")]
        choices = [cur_name] + [n for n in names if n != cur_name]
        text, ok = QInputDialog.getItem(
            self, "이름 붙이기",
            f"선택한 {len(items)}명에게 붙일 배우 이름\n"
            "(목록에서 고르거나 새로 입력. 같은 이름이면 한 사람으로 합쳐집니다)",
            choices, 0, True)
        name = (text or "").strip()
        if not ok or not name:
            return
        added = 0
        last = None
        for it in items:
            last = it.data(ROLE_ID)
            added += name_person(self.conn, last, name)
        keep = self.conn.execute("SELECT id FROM face_people WHERE name=? LIMIT 1",
                                 (name,)).fetchone()
        self.status.setText(f"✅ '{name}' 연결: 영상 {added}개에 배우 추가")
        _refresh_main(self.main)
        self.load(select=keep[0] if keep else last)

    def merge_selected(self):
        items = self._sel_people()
        if len(items) < 2:
            QMessageBox.information(self, "합치기", "합칠 사람을 2명 이상 선택하세요 (Ctrl+클릭).")
            return
        named = [it for it in items if it.data(ROLE_NAME)]
        target = named[0] if named else max(items, key=lambda it: it.data(ROLE_CNT) or 0)
        tid = target.data(ROLE_ID)
        label = target.data(ROLE_NAME) or f"P{tid}"
        msg = f"선택한 {len(items)}명을 한 사람으로 합칠까요?\n→ '{label}'"
        names = sorted({it.data(ROLE_NAME) for it in named})
        if len(names) > 1:
            msg += f"\n\n⚠ 서로 다른 이름이 섞여 있습니다: {', '.join(names)}\n'{label}'만 남습니다."
        if QMessageBox.question(self, "합치기", msg) != YES:
            return
        added = merge(self.conn, tid, [it.data(ROLE_ID) for it in items if it is not target])
        if added:
            _refresh_main(self.main)
        self.status.setText(f"🔗 합침 → '{label}'" + (f", 배우 추가 {added}건" if added else ""))
        self.load(select=tid)

    def sim_toggled(self, on):
        if on:
            cur = self.people.currentItem()
            if cur is None or not cur.data(ROLE_CEN):
                self.b_sim.blockSignals(True)
                self.b_sim.setChecked(False)
                self.b_sim.blockSignals(False)
                QMessageBox.information(self, "비슷한 순", "기준이 될 사람을 먼저 클릭하세요.")
                return
            self.sim_ref = np.frombuffer(cur.data(ROLE_CEN), np.float32)
            self.load(select=cur.data(ROLE_ID))
            self.people.scrollToTop()
        else:
            self.sim_ref = None
            self.load()

    def hide_selected(self):
        items = self._sel_people()
        if not items:
            return
        self.conn.executemany("UPDATE face_people SET hidden = 1 - hidden WHERE id=?",
                              [(it.data(ROLE_ID),) for it in items])
        self.conn.commit()
        self.load()

    def not_this(self):
        items = self.faces.selectedItems()
        if not items:
            return
        not_this_person(self.conn, [it.data(Qt.UserRole)[0] for it in items])
        _refresh_main(self.main)
        self.status.setText(f"🚫 얼굴 {len(items)}개를 이 사람에서 뺐습니다")
        self.load()

    def play_face(self, *_):
        it = self.faces.currentItem()
        if it is None:
            return
        _fid, vid, t = it.data(Qt.UserRole)
        from app import marks
        if not marks.play_at(self.main, vid, max(0.0, t - 2)):
            QMessageBox.information(self, "재생", "메인 화면 목록에서 이 영상을 찾지 못했습니다.\n"
                                    "검색·필터를 해제하거나 외장하드 연결을 확인해 주세요.")


# ---------------- 설치 ----------------
def open_dialog(window):
    dlg = getattr(window, "_vv_face_dlg", None)
    if dlg is None:
        dlg = FacesDialog(window)
        window._vv_face_dlg = dlg
    try:
        if sync_actors(window.conn):
            _refresh_main(window)
    except Exception as e:
        print("[배우 자동 추가 실패]", e)
    dlg.load()
    window._vv_faces._show()
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()


def install(window):
    ensure(window.conn)
    window._vv_face_dlg = None
    an = Analyzer(window)
    window._vv_faces = an

    tb = tt._toolbar(window)
    act = QAction("👤 얼굴", window)
    act.setToolTip("얼굴 정리 (Ctrl+Shift+F)")
    act.triggered.connect(lambda: open_dialog(window))
    tb.addAction(act)
    QShortcut(QKeySequence("Ctrl+Shift+F"), window).activated.connect(lambda: open_dialog(window))

    try:   # 터미널에서 분석한 결과도 반영
        if sync_actors(window.conn):
            QTimer.singleShot(500, lambda: _refresh_main(window))
    except Exception as e:
        print("[배우 자동 추가 실패]", e)

    if _state().get("auto", True):
        QTimer.singleShot(AUTO_FIRST, lambda: an.start())
        an.auto_timer.start()
    QApplication.instance().aboutToQuit.connect(an.stop)
