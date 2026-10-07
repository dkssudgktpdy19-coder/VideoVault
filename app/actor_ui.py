"""배우 정리 (#1): 프로필(사진·별명·별점·메모)과 출연작 모아 보기 + 품번 도구 (#4)"""
import os
import re
from collections import Counter
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListView, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QSplitter, QToolButton, QVBoxLayout, QWidget)

from app import codes, library, presets, tags
from app import thumb_tool as tt
from app.config import DATA_DIR, THUMB_DIR
from app.utils import fmt_duration

ACTOR_DIR = Path(DATA_DIR) / "actors"
ACTOR_DIR.mkdir(parents=True, exist_ok=True)
ROLE = Qt.ItemDataRole.UserRole
YES = QMessageBox.StandardButton.Yes
STARS = ["☆ 별점 없음", "★", "★★", "★★★", "★★★★", "★★★★★"]
SORTS = [
    ("영상 많은 순", lambda r: (-r["n"], r["name"].lower())),
    ("이름순", lambda r: r["name"].lower()),
    ("별점 높은 순", lambda r: (-r["rating"], -r["n"])),
    ("많이 본 순", lambda r: (-r["plays"], -r["n"])),
    ("새로 추가된 순", lambda r: -r["id"]),
]


# ---------------- 도우미 ----------------
def _refresh_main(window):
    if getattr(window, "_closing", False):
        return
    try:
        from app.face_ui import _refresh_main as fr
        fr(window)
    except Exception as e:
        print("[배우] 메인 화면 새로고침 실패:", e)


_ICONS = {}


def _icon(path):
    if not path:
        return QIcon()
    try:
        key = f"{path}|{os.path.getmtime(path)}"
    except OSError:
        return QIcon()
    ic = _ICONS.get(key)
    if ic is None:
        pm = QPixmap(path)
        ic = QIcon(pm) if not pm.isNull() else QIcon()
        _ICONS[key] = ic
    return ic


def _thumb_file(tp):
    if not tp:
        return ""
    p = Path(tp)
    if not p.is_absolute():
        p = Path(THUMB_DIR) / p
    return str(p) if p.is_file() else ""


def photo_file(name):
    if not name:
        return None
    p = ACTOR_DIR / Path(name).name
    return p if p.is_file() else None


def face_map(conn):
    """배우마다 가장 또렷한 얼굴 사진 (직접 지정한 사진이 없을 때 대신 씀)"""
    try:
        rows = conn.execute("""SELECT p.actor_id, f.thumb, MAX(f.hits * f.score) FROM video_faces f
                               JOIN face_people p ON p.id = f.person_id
                               WHERE p.actor_id IS NOT NULL AND COALESCE(f.thumb, '') <> ''
                               GROUP BY p.actor_id""").fetchall()
    except Exception:
        return {}
    return {r[0]: r[1] for r in rows}


def face_thumbs(conn, aid, limit=40):
    try:
        rows = conn.execute("""SELECT f.thumb FROM video_faces f JOIN face_people p ON p.id = f.person_id
                               WHERE p.actor_id = ? AND COALESCE(f.thumb, '') <> ''
                               ORDER BY f.hits * f.score DESC LIMIT ?""", (aid, limit)).fetchall()
    except Exception:
        return []
    return [r[0] for r in rows if os.path.isfile(r[0])]


def icon_path(a):
    p = photo_file(a.get("photo"))
    if p:
        return str(p)
    f = a.get("face")
    return f if f and os.path.isfile(f) else ""


def load_actors(conn):
    library.ensure_excluded(conn)
    rows = conn.execute("""SELECT a.id, a.name, IFNULL(a.aliases, '') AS aliases,
                                  IFNULL(a.rating, 0) AS rating, IFNULL(a.photo_path, '') AS photo,
                                  COUNT(v.id) AS n, IFNULL(SUM(v.duration), 0) AS dur,
                                  IFNULL(SUM(v.play_count), 0) AS plays
                           FROM actors a
                           LEFT JOIN video_actors va ON va.actor_id = a.id
                           LEFT JOIN videos v ON v.id = va.video_id AND IFNULL(v.excluded, 0) = 0
                           GROUP BY a.id""").fetchall()
    faces = face_map(conn)
    out = []
    for r in rows:
        d = dict(r)
        d["face"] = faces.get(d["id"], "")
        out.append(d)
    return out


def save_photo(aid, pm):
    """가운데를 정사각형으로 잘라 300px로 저장 → 파일 이름"""
    side = min(pm.width(), pm.height())
    pm = pm.copy((pm.width() - side) // 2, (pm.height() - side) // 2, side, side)
    pm = pm.scaled(300, 300, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    out = ACTOR_DIR / f"a{aid}.jpg"
    pm.save(str(out), "JPG", 90)
    return out.name


def merge_actor(conn, src, dst):
    """배우 src를 dst로 합침: 영상·얼굴·품번 연결, 별명, 사진, 별점, 메모까지 옮김"""
    for sql in ("UPDATE OR IGNORE code_actors SET actor_id=? WHERE actor_id=?",
                "UPDATE OR IGNORE code_actor_sync SET actor_id=? WHERE actor_id=?",
                "UPDATE OR IGNORE face_actor_sync SET actor_id=? WHERE actor_id=?",
                "UPDATE face_people SET actor_id=? WHERE actor_id=?"):
        try:
            conn.execute(sql, (dst, src))
        except Exception:
            pass
    s = conn.execute("SELECT name, aliases, photo_path, rating, memo FROM actors WHERE id=?", (src,)).fetchone()
    d = conn.execute("SELECT name, aliases, photo_path, rating, memo FROM actors WHERE id=?", (dst,)).fetchone()
    words = []
    for w in [*(d["aliases"] or "").split(","), s["name"], *(s["aliases"] or "").split(",")]:
        w = tags.clean_name(w)
        if w and w.lower() != d["name"].lower() and w.lower() not in [x.lower() for x in words]:
            words.append(w)
    memo = "\n".join(m for m in (d["memo"], s["memo"]) if m) or None
    conn.execute("UPDATE actors SET aliases=?, photo_path=COALESCE(photo_path, ?), "
                 "rating=MAX(IFNULL(rating, 0), ?), memo=? WHERE id=?",
                 (", ".join(words) or None, s["photo_path"], s["rating"] or 0, memo, dst))
    try:
        conn.execute("UPDATE face_people SET name=? WHERE actor_id=?", (d["name"], dst))
    except Exception:
        pass
    tags.merge(conn, "actor", src, dst)
    conn.commit()


def pick_face(parent, conn, aid):
    paths = face_thumbs(conn, aid)
    if not paths:
        QMessageBox.information(parent, "얼굴에서 고르기",
                                "이 배우에게 연결된 얼굴이 없습니다.\n(👤 얼굴 창에서 이 이름을 붙이면 생깁니다)")
        return None
    dlg = QDialog(parent)
    dlg.setWindowTitle("사진으로 쓸 얼굴 고르기 (더블클릭)")
    dlg.resize(720, 480)
    lw = QListWidget()
    lw.setViewMode(QListView.ViewMode.IconMode)
    lw.setIconSize(QSize(120, 120))
    lw.setGridSize(QSize(136, 136))
    lw.setResizeMode(QListView.ResizeMode.Adjust)
    lw.setMovement(QListView.Movement.Static)
    for p in paths:
        it = QListWidgetItem(_icon(p), "")
        it.setData(ROLE, p)
        lw.addItem(it)
    lw.setCurrentRow(0)
    lw.itemActivated.connect(lambda *_: dlg.accept())
    b_ok, b_no = QPushButton("이 얼굴로"), QPushButton("취소")
    b_ok.clicked.connect(dlg.accept)
    b_no.clicked.connect(dlg.reject)
    row = QHBoxLayout()
    row.addStretch(1)
    row.addWidget(b_ok)
    row.addWidget(b_no)
    lay = QVBoxLayout(dlg)
    lay.addWidget(lw, 1)
    lay.addLayout(row)
    if dlg.exec() and lw.currentItem() is not None:
        return lw.currentItem().data(ROLE)
    return None


# ---------------- 배우 정리 창 ----------------
class ActorDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.main, self.conn = window, window.conn
        self.cur, self.rows = None, {}
        self.changed, self._loading = False, False
        self.setWindowTitle("🎭 배우 정리")
        self.resize(1200, 760)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)

        # 위: 검색·정렬
        self.search = QLineEdit()
        self.search.setPlaceholderText("배우 이름·별명 검색")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.load)
        self.sort = QComboBox()
        self.sort.addItems([s[0] for s in SORTS])
        i = int(library.get_setting(self.conn, "actor_sort", 0) or 0)
        self.sort.setCurrentIndex(i if 0 <= i < len(SORTS) else 0)
        self.sort.currentIndexChanged.connect(self._sort_changed)
        b_new = QPushButton("+ 새 배우")
        b_new.clicked.connect(lambda checked=False: self.new_actor())
        self.info = QLabel()
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.sort)
        top.addWidget(b_new)
        top.addWidget(self.info)

        # 왼쪽: 배우 목록
        self.list = QListWidget()
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setIconSize(QSize(96, 96))
        self.list.setGridSize(QSize(124, 150))
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setWordWrap(True)
        self.list.currentItemChanged.connect(self.show_actor)
        self.list.itemActivated.connect(lambda *_: self.show_in_main())

        # 오른쪽: 프로필
        self.right = QWidget()
        self.photo = QLabel("사진 없음")
        self.photo.setFixedSize(160, 160)
        self.photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.photo.setStyleSheet("border:1px solid #555; background:#222; color:#888;")
        self.photo_hint = QLabel("")
        self.photo_hint.setStyleSheet("color:#888; font-size:11px;")
        b_photo = QToolButton()
        b_photo.setText("📷 사진 바꾸기")
        b_photo.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        pmenu = QMenu(b_photo)
        pmenu.addAction("👤 인식된 얼굴에서 고르기").triggered.connect(lambda: self.photo_from_face())
        pmenu.addAction("🖼 그림 파일에서 고르기…").triggered.connect(lambda: self.photo_from_file())
        pmenu.addSeparator()
        pmenu.addAction("🗑 사진 지우기").triggered.connect(lambda: self.photo_clear())
        b_photo.setMenu(pmenu)
        pcol = QVBoxLayout()
        pcol.addWidget(self.photo)
        pcol.addWidget(self.photo_hint)
        pcol.addWidget(b_photo)
        pcol.addStretch(1)

        self.name = QLabel("")
        self.name.setStyleSheet("font-size:20px; font-weight:bold; color:#7cc4ff;")
        b_rename = QPushButton("✏ 이름 바꾸기·합치기 (F2)")
        b_rename.clicked.connect(lambda checked=False: self.rename())
        self.aliases = QLineEdit()
        self.aliases.setPlaceholderText("별명 (쉼표로 구분, 예: Wonee, 원희) — 파일명 자동 인식에 사용")
        self.aliases.editingFinished.connect(self.save_profile)
        self.rating = QComboBox()
        self.rating.addItems(STARS)
        self.rating.currentIndexChanged.connect(lambda _: None if self._loading else self.save_profile())
        self.memo = QPlainTextEdit()
        self.memo.setPlaceholderText("메모 (다른 배우를 고르거나 창을 닫으면 저장)")
        self.memo.setMaximumHeight(90)
        self.stats = QLabel("")
        self.stats.setWordWrap(True)
        self.stats.setStyleSheet("color:#bbb;")
        form = QGridLayout()
        form.addWidget(self.name, 0, 0, 1, 2)
        form.addWidget(b_rename, 0, 2)
        form.addWidget(QLabel("별명"), 1, 0)
        form.addWidget(self.aliases, 1, 1, 1, 2)
        form.addWidget(QLabel("별점"), 2, 0)
        form.addWidget(self.rating, 2, 1)
        form.addWidget(QLabel("메모"), 3, 0, Qt.AlignmentFlag.AlignTop)
        form.addWidget(self.memo, 3, 1, 1, 2)
        form.addWidget(self.stats, 4, 0, 1, 3)
        form.setColumnStretch(1, 1)
        head = QHBoxLayout()
        head.addLayout(pcol)
        head.addLayout(form, 1)

        self.works_title = QLabel("출연작")
        self.works = QListWidget()
        self.works.setViewMode(QListView.ViewMode.IconMode)
        self.works.setIconSize(QSize(192, 108))
        self.works.setGridSize(QSize(208, 156))
        self.works.setResizeMode(QListView.ResizeMode.Adjust)
        self.works.setMovement(QListView.Movement.Static)
        self.works.setWordWrap(True)
        self.works.itemActivated.connect(self.play)
        b_main = QPushButton("👁 메인 화면에서 이 배우만 보기")
        b_main.clicked.connect(lambda checked=False: self.show_in_main())
        b_del = QPushButton("🗑 배우 삭제")
        b_del.clicked.connect(lambda checked=False: self.delete())
        rb = QHBoxLayout()
        rb.addWidget(b_main)
        rb.addStretch(1)
        rb.addWidget(b_del)
        rl = QVBoxLayout(self.right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addLayout(head)
        rl.addWidget(self.works_title)
        rl.addWidget(self.works, 1)
        rl.addLayout(rb)

        split = QSplitter()
        split.addWidget(self.list)
        split.addWidget(self.right)
        split.setSizes([430, 770])

        self.status = QLabel("배우 클릭 = 프로필 · 배우 더블클릭 = 메인 화면에서 이 배우만 · 출연작 더블클릭 = 재생")
        self.status.setStyleSheet("color:#aaa;")
        b_save = QPushButton("💾 저장")
        b_save.clicked.connect(lambda checked=False: self.save_profile())
        b_close = QPushButton("닫기")
        b_close.clicked.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(b_save)
        bottom.addWidget(b_close)

        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)
            b.setDefault(False)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        lay.addLayout(bottom)
        QShortcut(QKeySequence("F2"), self).activated.connect(self.rename)

    # ---------- 목록 ----------
    def _sort_changed(self, i):
        library.set_setting(self.conn, "actor_sort", int(i))
        self.load()

    def _label(self, a):
        text = f"{a['name']}\n영상 {a['n']}개"
        if a["rating"]:
            text += "  " + "★" * int(a["rating"])
        return text

    def load(self, *_, select=None):
        if select is None and self.cur:
            select = self.cur["id"]
        q = self.search.text().strip().lower()
        rows = load_actors(self.conn)
        if q:
            rows = [r for r in rows if q in r["name"].lower() or q in r["aliases"].lower()]
        rows.sort(key=SORTS[self.sort.currentIndex()][1])
        self.rows = {r["id"]: r for r in rows}
        self.list.blockSignals(True)
        self.list.clear()
        target = None
        for r in rows:
            it = QListWidgetItem(_icon(icon_path(r)), self._label(r))
            it.setData(ROLE, r["id"])
            if r["id"] == select:
                target = it
            self.list.addItem(it)
        self.list.blockSignals(False)
        no_photo = sum(1 for r in rows if not icon_path(r))
        self.info.setText(f"{len(rows)}명 · 사진 없음 {no_photo}명")
        if target is None and self.list.count():
            target = self.list.item(0)
        if target is not None:
            self.list.setCurrentItem(target)
            self.list.scrollToItem(target)
        else:
            self.show_actor(None)

    def _list_item(self, aid):
        for i in range(self.list.count()):
            if self.list.item(i).data(ROLE) == aid:
                return self.list.item(i)
        return None

    # ---------- 프로필 ----------
    def show_actor(self, cur, prev=None):
        self.save_profile()
        self.cur = self.rows.get(cur.data(ROLE)) if cur is not None else None
        self.right.setEnabled(self.cur is not None)
        self._loading = True
        if self.cur is None:
            self.name.setText("")
            self.aliases.setText("")
            self.rating.setCurrentIndex(0)
            self.memo.setPlainText("")
            self.stats.setText("")
            self.works.clear()
            self.photo.clear()
            self.photo.setText("사진 없음")
            self._loading = False
            return
        a = self.cur
        row = self.conn.execute("SELECT IFNULL(memo, '') FROM actors WHERE id=?", (a["id"],)).fetchone()
        a["memo"] = row[0] if row else ""
        self.name.setText(a["name"])
        self.aliases.setText(a["aliases"])
        self.rating.setCurrentIndex(int(a["rating"] or 0))
        self.memo.setPlainText(a["memo"])
        self._loading = False
        self._show_photo()
        self._load_works()

    def save_profile(self):
        a = self.cur
        if a is None or self._loading:
            return
        aliases = self.aliases.text().strip()
        rating = self.rating.currentIndex()
        memo = self.memo.toPlainText().strip()
        if aliases == a["aliases"] and rating == a["rating"] and memo == a.get("memo", ""):
            return
        tags.set_aliases(self.conn, a["id"], aliases)
        self.conn.execute("UPDATE actors SET rating=?, memo=? WHERE id=?", (rating, memo or None, a["id"]))
        self.conn.commit()
        a.update(aliases=tags.get_aliases(self.conn, a["id"]), rating=rating, memo=memo)
        self.changed = True
        it = self._list_item(a["id"])
        if it is not None:
            it.setText(self._label(a))
        self.status.setText(f"💾 '{a['name']}' 저장함")

    def _show_photo(self):
        a = self.cur
        p = photo_file(a["photo"])
        src = str(p) if p else (a["face"] if a["face"] and os.path.isfile(a["face"]) else "")
        pm = QPixmap(src) if src else QPixmap()
        if pm.isNull():
            self.photo.clear()
            self.photo.setText("사진 없음\n📷 버튼으로 지정")
        else:
            self.photo.setPixmap(pm.scaled(160, 160, Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))
        self.photo_hint.setText("" if p else ("(얼굴 인식 사진)" if src else ""))
        it = self._list_item(a["id"])
        if it is not None:
            it.setIcon(_icon(icon_path(a)))

    def _load_works(self):
        a = self.cur
        rows = self.conn.execute("""SELECT v.id, v.filename, v.duration, v.rating, v.play_count,
                                           v.thumb_path, IFNULL(vc.code, '') AS code, vc.series
                                    FROM video_actors va JOIN videos v ON v.id = va.video_id
                                    LEFT JOIN video_codes vc ON vc.video_id = v.id
                                    WHERE va.actor_id = ? AND IFNULL(v.excluded, 0) = 0
                                    ORDER BY v.rating DESC, v.play_count DESC, v.id DESC""",
                                 (a["id"],)).fetchall()
        self.works.clear()
        series, total, plays, rated = Counter(), 0.0, 0, []
        for r in rows:
            label = r["code"] or Path(r["filename"]).stem
            text = f"{label[:24]}\n{fmt_duration(r['duration'])}"
            if r["rating"]:
                text += "  " + "★" * int(r["rating"])
                rated.append(r["rating"])
            it = QListWidgetItem(_icon(_thumb_file(r["thumb_path"])), text)
            it.setToolTip(r["filename"])
            it.setData(ROLE, r["id"])
            self.works.addItem(it)
            total += r["duration"] or 0
            plays += r["play_count"] or 0
            if r["series"]:
                series[r["series"]] += 1
        try:
            nfaces = self.conn.execute("SELECT COUNT(*) FROM face_people WHERE actor_id=?",
                                       (a["id"],)).fetchone()[0]
        except Exception:
            nfaces = 0
        line = f"영상 {len(rows)}개 · 총 {fmt_duration(total)} · 본 횟수 {plays}회"
        if rated:
            line += f" · 평균 별점 {sum(rated) / len(rated):.1f}"
        if nfaces:
            line += f" · 얼굴 묶음 {nfaces}개"
        if series:
            line += "\n품번 시리즈: " + ", ".join(f"{s} {n}개" for s, n in series.most_common(6))
        self.stats.setText(line)
        self.works_title.setText(f"출연작 {len(rows)}개 (별점·많이 본 순, 더블클릭 = 재생)")

    # ---------- 사진 ----------
    def _set_photo(self, pm):
        if pm.isNull():
            QMessageBox.warning(self, "사진", "그림을 읽지 못했습니다.")
            return
        name = save_photo(self.cur["id"], pm)
        self.conn.execute("UPDATE actors SET photo_path=? WHERE id=?", (name, self.cur["id"]))
        self.conn.commit()
        self.cur["photo"] = name
        self._show_photo()
        self.changed = True

    def photo_from_face(self):
        if self.cur:
            path = pick_face(self, self.conn, self.cur["id"])
            if path:
                self._set_photo(QPixmap(path))

    def photo_from_file(self):
        if not self.cur:
            return
        path, _ = QFileDialog.getOpenFileName(self, "배우 사진 고르기", "",
                                              "그림 (*.jpg *.jpeg *.png *.webp *.bmp)")
        if path:
            self._set_photo(QPixmap(path))

    def photo_clear(self):
        if not self.cur:
            return
        p = photo_file(self.cur["photo"])
        if p:
            try:
                p.unlink()
            except OSError:
                pass
        self.conn.execute("UPDATE actors SET photo_path=NULL WHERE id=?", (self.cur["id"],))
        self.conn.commit()
        self.cur["photo"] = ""
        self._show_photo()

    # ---------- 이름·삭제·새로 ----------
    def rename(self):
        a = self.cur
        if not a:
            return
        self.save_profile()
        others = [n for n in tags.all_names(self.conn, "actor") if n != a["name"]]
        new, ok = QInputDialog.getItem(
            self, "이름 바꾸기·합치기",
            f"'{a['name']}'의 새 이름\n(이미 있는 배우 이름을 고르면 그 배우로 합쳐집니다)",
            [a["name"]] + others, 0, True)
        new = tags.clean_name(new)
        if not ok or not new or new == a["name"]:
            return
        other = tags.find_id(self.conn, "actor", new)
        if other is not None and other != a["id"]:
            if QMessageBox.question(self, "합치기",
                                    f"'{a['name']}'을(를) '{new}'에 합칠까요?\n"
                                    f"영상·얼굴·품번 연결이 모두 '{new}'로 옮겨지고,\n"
                                    f"'{a['name']}'은(는) '{new}'의 별명으로 남습니다.") != YES:
                return
            merge_actor(self.conn, a["id"], other)
            select = other
        else:
            tags.rename(self.conn, "actor", a["id"], new)
            try:
                self.conn.execute("UPDATE face_people SET name=? WHERE actor_id=?", (new, a["id"]))
                self.conn.commit()
            except Exception:
                pass
            select = a["id"]
        self.cur = None
        self.changed = True
        self.load(select=select)

    def delete(self):
        a = self.cur
        if not a:
            return
        if QMessageBox.question(self, "배우 삭제",
                                f"배우 '{a['name']}'을(를) 삭제할까요?\n"
                                "영상에서 이 배우만 빠지고, 영상 파일은 그대로입니다.\n"
                                "(👤 얼굴 창의 이 얼굴 묶음은 이름 없는 상태로 돌아갑니다)") != YES:
            return
        try:
            self.conn.execute("UPDATE face_people SET actor_id=NULL, name='' WHERE actor_id=?", (a["id"],))
        except Exception:
            pass
        p = photo_file(a["photo"])
        if p:
            try:
                p.unlink()
            except OSError:
                pass
        tags.delete(self.conn, "actor", a["id"])
        self.cur = None
        self.changed = True
        self.load()

    def new_actor(self):
        name, ok = QInputDialog.getText(self, "새 배우", "배우 이름:")
        name = tags.clean_name(name)
        if ok and name:
            aid = tags.get_or_create(self.conn, "actor", name)
            self.conn.commit()
            self.changed = True
            self.load(select=aid)

    # ---------- 메인 화면 연결 ----------
    def show_in_main(self):
        a = self.cur
        if not a:
            return
        self.save_profile()
        if self.changed:
            _refresh_main(self.main)
            self.changed = False
        sb = presets._sidebar(self.main)
        if sb is None:
            return
        presets.clear_all(self.main)
        lst = sb.actor_list
        lst.find.setText("")
        lst.refresh()
        for i in range(lst.list.count()):
            it = lst.list.item(i)
            if it.data(ROLE) == a["id"]:
                lst.only_this(it)
                break
        self.main.statusBar().showMessage(f"🎭 '{a['name']}' 출연작만 보는 중 (필터 해제: ✖ 필터 모두 해제)", 8000)
        self.main.raise_()
        self.main.activateWindow()

    def _play_in_grid(self, vid):
        from app import marks
        view, r, _d = marks._grid_row(self.main, vid)
        if r is None:
            return False
        idx = view.model().index(r, 0)
        view.setCurrentIndex(idx)
        view.scrollTo(idx)
        marks._emit_play(view, idx)
        return True

    def play(self, item):
        vid = item.data(ROLE)
        if self._play_in_grid(vid):
            return
        self.show_in_main()          # 검색·필터 때문에 목록에 없으면 이 배우만 보기로 바꾼 뒤 재생

        def retry():
            if not self._play_in_grid(vid):
                QMessageBox.information(self, "재생", "메인 화면 목록에서 이 영상을 찾지 못했습니다.\n"
                                                      "외장하드 연결을 확인해 주세요.")
        QTimer.singleShot(900, retry)

    def done(self, r):
        self.save_profile()
        if self.changed:
            _refresh_main(self.main)
            self.changed = False
        super().done(r)


def open_dialog(window, select_name=None):
    dlg = getattr(window, "_vv_actor_dlg", None)
    if dlg is None:
        dlg = ActorDialog(window)
        window._vv_actor_dlg = dlg
    select = tags.find_id(window.conn, "actor", select_name) if select_name else None
    if select is not None:
        dlg.search.blockSignals(True)
        dlg.search.setText("")
        dlg.search.blockSignals(False)
    dlg.load(select=select)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()


# ---------------- 품번 도구 (#4) ----------------
def code_sync(window, quiet=True):
    if getattr(window, "_closing", False):
        return 0
    try:
        added = codes.sync(window.conn)
    except Exception as e:
        print("[품번] 자동 맞추기 실패:", e)
        return 0
    if added:
        window.statusBar().showMessage(f"🔢 품번으로 배우 자동 추가 {added}건", 8000)
        _refresh_main(window)
    if not quiet:
        QMessageBox.information(window, "품번", f"같은 품번 영상에 배우 {added}건을 붙였습니다."
                                if added else "새로 붙일 배우가 없습니다.")
    return added


def assign_code(window, code=None):
    conn = window.conn
    codes.update_codes(conn)
    if code is None:
        text, ok = QInputDialog.getText(window, "품번 배우 지정", "품번 (예: 200GANA-2394, SSIS-123):")
        if not ok or not text.strip():
            return
        c = codes.extract(text.strip().upper())
        if not c:
            QMessageBox.information(window, "품번", "품번 모양이 아닙니다.")
            return
        code = c[0]
    vids = codes.videos_of_code(conn, code)
    cur = [r[1] for r in codes.current_names(conn, code)]
    text, ok = QInputDialog.getText(
        window, "품번 배우 지정",
        f"품번 {code}   (영상 {len(vids)}개)\n출연 배우 이름 (여러 명은 쉼표로 구분)\n\n"
        "같은 품번 영상에 모두 붙고, 나중에 들어오는 같은 품번 영상에도 자동으로 붙습니다.\n"
        "지운 이름은 이 품번 영상들에서 빠집니다.",
        QLineEdit.EchoMode.Normal, ", ".join(cur))
    if not ok:
        return
    names = [tags.clean_name(n) for n in re.split(r"[,/／、]", text) if tags.clean_name(n)]
    added, removed = codes.set_code_actors(conn, code, names)
    window.statusBar().showMessage(f"🔢 {code}: 배우 붙임 {added}건" + (f", 뺌 {removed}명" if removed else ""), 8000)
    _refresh_main(window)


def export_codes(window):
    default = str(Path.home() / "Desktop" / "품번_배우.csv")
    path, _ = QFileDialog.getSaveFileName(window, "품번 → 배우 목록 저장", default, "CSV (*.csv)")
    if not path:
        return
    try:
        n = codes.export_file(window.conn, path)
    except Exception as e:
        QMessageBox.critical(window, "내보내기 실패", str(e))
        return
    QMessageBox.information(window, "내보내기",
                            f"품번 {n}개를 저장했습니다.\n{path}\n\n"
                            "엑셀에서 '배우' 칸을 채우고 (여러 명은 / 로 구분)\n"
                            "'CSV UTF-8' 형식으로 저장한 뒤 📥 가져오기 하세요.")


def import_codes(window):
    path, _ = QFileDialog.getOpenFileName(window, "품번 → 배우 목록 파일", str(Path.home() / "Desktop"),
                                          "목록 파일 (*.csv *.txt *.tsv);;모든 파일 (*)")
    if not path:
        return
    try:
        ok, mapped, bad = codes.import_file(window.conn, path)
        added = codes.apply_mappings(window.conn)
    except Exception as e:
        QMessageBox.critical(window, "가져오기 실패", str(e))
        return
    msg = f"읽은 줄 {ok}개 · 새 품번-배우 연결 {mapped}개 · 영상에 배우 붙임 {added}건"
    if bad:
        msg += f"\n\n⚠ 품번을 못 읽은 줄 {len(bad)}개: " + ", ".join(bad[:8])
    QMessageBox.information(window, "가져오기", msg)
    _refresh_main(window)


def series_tags(window):
    if QMessageBox.question(window, "시리즈 태그",
                            "품번 시리즈(예: 200GANA, SSIS)를 태그로 붙일까요?\n"
                            "사이드바 🏷 태그에서 시리즈별로 골라 볼 수 있게 됩니다.") != YES:
        return
    n, k = codes.tag_series(window.conn)
    window.statusBar().showMessage(f"🏷 시리즈 태그 {k}종류, {n}건 붙임", 8000)
    _refresh_main(window)


def show_stats(window):
    s = codes.stats(window.conn)
    text = (f"품번을 찾은 영상: {s['coded']}개 / 전체 {s['total']}개\n"
            f"품번 종류: {s['codes']}개\n"
            f"배우가 붙은 품번: {s['with_actor']}개\n"
            f"품번→배우 목록에 등록된 품번: {s['mapped']}개\n\n많은 시리즈:\n")
    text += "\n".join(f"   {name}   {n}개" for name, n in s["top"]) or "   (없음)"
    if s["missing"]:
        text += "\n\n품번을 못 찾은 영상 예:\n" + "\n".join("   " + f[:60] for f in s["missing"])
    QMessageBox.information(window, "📊 품번 현황", text)


def _code_menu(window, menu):
    menu.clear()
    for text, fn in (("🔄 같은 품번 영상에 배우 맞추기 (지금)", lambda: code_sync(window, quiet=False)),
                     ("✏ 품번 하나 배우 지정… (영상 우클릭에서도 가능)", lambda: assign_code(window)),
                     (None, None),
                     ("📤 품번 → 배우 목록 내보내기 (CSV)…", lambda: export_codes(window)),
                     ("📥 품번 → 배우 목록 가져오기 (CSV/TXT)…", lambda: import_codes(window)),
                     (None, None),
                     ("🏷 품번 시리즈를 태그로 붙이기…", lambda: series_tags(window)),
                     ("📊 품번 현황", lambda: show_stats(window))):
        if text is None:
            menu.addSeparator()
        else:
            menu.addAction(text).triggered.connect(lambda checked=False, f=fn: f())
    menu.addSeparator()
    auto = menu.addAction("자동 맞추기 켜기 (새 영상에도 자동으로)")
    auto.setCheckable(True)
    auto.setChecked(bool(library.get_setting(window.conn, "code_auto", True)))
    auto.toggled.connect(lambda on: library.set_setting(window.conn, "code_auto", bool(on)))


def actor_menu(window, menu, vids):
    """메인 화면 우클릭: 배우 프로필, 품번 배우 지정"""
    if not vids:
        return
    menu.addSeparator()
    names = []
    for v in vids[:30]:
        for n in (v.get("actor_names") or "").split(", "):
            if n and n not in names:
                names.append(n)
    if names:
        sub = menu.addMenu("🎭 배우 프로필 보기")
        for n in names[:15]:
            sub.addAction(n).triggered.connect(lambda checked=False, x=n: open_dialog(window, x))
    try:
        codes.update_codes(window.conn)
        found = sorted({c for c in (codes.code_of(window.conn, v["id"]) for v in vids) if c})
    except Exception:
        found = []
    if len(found) == 1:
        menu.addAction(f"🔢 품번 {found[0]} 배우 지정…").triggered.connect(
            lambda checked=False, c=found[0]: assign_code(window, c))
    elif len(found) > 1:
        menu.addAction(f"🔢 품번 {len(found)}종류 선택됨 (배우 지정은 하나씩)").setEnabled(False)
    else:
        menu.addAction("🔢 파일명에서 품번을 못 찾음").setEnabled(False)


def install(window):
    codes.ensure(window.conn)
    window._vv_actor_dlg = None
    window._vv_actor_menu = lambda menu, vids: actor_menu(window, menu, vids)
    tb = tt._toolbar(window)
    act = QAction("🎭 배우", window)
    act.setToolTip("배우 정리: 사진·별명·별점·메모, 출연작 (Ctrl+Shift+A)")
    act.triggered.connect(lambda checked=False: open_dialog(window))
    tb.addAction(act)
    QShortcut(QKeySequence("Ctrl+Shift+A"), window).activated.connect(lambda: open_dialog(window))

    btn = QToolButton(window)
    btn.setText("🔢 품번")
    btn.setToolTip("품번으로 배우 자동 태그, 목록 가져오기/내보내기, 시리즈 태그")
    btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    menu = QMenu(btn)
    menu.aboutToShow.connect(lambda: _code_menu(window, menu))
    btn.setMenu(menu)
    tb.addWidget(btn)

    def auto():
        if library.get_setting(window.conn, "code_auto", True):
            code_sync(window, quiet=True)

    timer = QTimer(window)
    timer.setSingleShot(True)
    timer.setInterval(5000)
    timer.timeout.connect(auto)
    window._vv_code_timer = timer
    window.model.modelReset.connect(timer.start)      # 새 영상 확인 등으로 목록이 바뀌면 5초 뒤 자동
    QTimer.singleShot(4000, auto)
