"""같은 영상 찾기 화면: 묶음 보기, 남길 영상 고르기(나머지 영구 삭제), 다른 영상 표시"""
import os
import subprocess
import time

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton, QSplitter, QVBoxLayout,
                               QWidget)

from app import dups, library, manage
from app import thumb_tool as tt
from app.actor_ui import _icon, _thumb_file
from app.db import init_db
from app.utils import fmt_duration, fmt_size

ROLE = Qt.ItemDataRole.UserRole
YES = QMessageBox.StandardButton.Yes


def full_row(conn, vid):
    r = conn.execute(library.BASE_SQL + " WHERE v.id = ?", (vid,)).fetchone()
    return library._attach_paths(conn, [dict(r)])[0] if r else None


def _kbps(v):
    br = v.get("bitrate")
    if not br and v.get("size") and v.get("duration"):
        br = v["size"] * 8 / v["duration"]
    return int((br or 0) / 1000)


def suggest(rows):
    """해상도 → 비트레이트 순으로 가장 좋은 영상 추천"""
    def q(v):
        return ((v.get("width") or 0) * (v.get("height") or 0), _kbps(v))
    best = max(rows, key=q)
    others = [v for v in rows if v is not best]
    if all(q(best)[0] > q(o)[0] for o in others):
        why = "해상도가 가장 높음"
    elif all(q(best)[1] > q(o)[1] for o in others):
        why = "화질(비트레이트)이 가장 높음"
    else:
        why = "화질이 가장 좋아 보임"
    return best, why


def remove_others(window, keep, others, merge):
    """others를 파일까지 지우고, merge면 정보를 keep으로 합침 → (지운 수, 실패 목록)"""
    conn = window.conn
    pw = window.player
    ids = {o["id"] for o in others} | {keep["id"]}
    if getattr(pw, "video", None) and pw.video["id"] in ids:
        pw.close()
        QApplication.processEvents()
        time.sleep(0.5)
    tables = manage._tables_with_video_id(conn)
    done, fails = 0, []
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        for o in others:
            if merge:
                dups.copy_links(conn, keep["id"], o["id"])
                conn.commit()
            path = o.get("full_path")
            if path and os.path.exists(path):
                err = manage._remove_file(path)
                if err:
                    fails.append(f"{o['filename']}\n   → {err}")
                    continue
            if merge:
                dups.move_extras(conn, keep["id"], o["id"])
                dups.add_stats(conn, keep["id"], o["id"])
            manage._cleanup(conn, o)
            for t in tables:
                conn.execute(f'DELETE FROM "{t}" WHERE video_id = ?', (o["id"],))
            conn.execute("DELETE FROM videos WHERE id = ?", (o["id"],))
            conn.commit()
            done += 1
    finally:
        QApplication.restoreOverrideCursor()
    return done, fails


class FpThread(QThread):
    progress = Signal(int, int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop = False
        self.paused = False
        self.result = None

    def stop(self):
        self._stop = True

    def run(self):
        conn = init_db()
        try:
            self.result = dups.build(conn, progress=lambda d, t, n: self.progress.emit(d, t, n),
                                     stop=lambda: self._stop, paused=lambda: self.paused)
        except Exception as e:
            self.result = {"error": str(e)}
        finally:
            conn.close()


class DupDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.w, self.conn = window, window.conn
        self.thread = None
        self.groups, self.rows = [], []
        self.setWindowTitle("🧬 같은 영상 찾기")
        self.resize(1200, 760)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)

        self.info = QLabel()
        self.info.setWordWrap(True)
        self.b_scan = self._btn("🔍 찾기 시작 / 이어서", self.start)
        self.b_stop = self._btn("⏹ 멈추기", self.stop)
        self.b_stop.setEnabled(False)
        top = QHBoxLayout()
        top.addWidget(self.info, 1)
        top.addWidget(self.b_scan)
        top.addWidget(self.b_stop)

        self.glist = QListWidget()
        self.glist.setIconSize(QSize(96, 54))
        self.glist.currentItemChanged.connect(self.show_group)

        self.gtitle = QLabel("")
        self.gtitle.setStyleSheet("font-weight:bold; color:#7cc4ff;")
        self.vlist = QListWidget()
        self.vlist.setIconSize(QSize(192, 108))
        self.vlist.setSpacing(4)
        self.vlist.setWordWrap(True)
        self.vlist.itemActivated.connect(lambda *_: self.play())
        self.merge = QCheckBox("지우는 영상의 태그·배우·별점·본 횟수·메모·북마크·자막을 남길 영상으로 합치기")
        self.merge.setChecked(True)
        row = QHBoxLayout()
        row.addWidget(self._btn("▶ 재생", self.play))
        row.addWidget(self._btn("📂 폴더 열기", self.open_folder))
        row.addStretch(1)
        row.addWidget(self._btn("🙅 선택한 영상은 다른 영상", self.not_same))
        b_keep = self._btn("✅ 선택한 영상만 남기기 (나머지 영구 삭제)", self.keep_selected)
        b_keep.setStyleSheet("font-weight:bold;")
        row.addWidget(b_keep)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.gtitle)
        rl.addWidget(self.vlist, 1)
        rl.addWidget(self.merge)
        rl.addLayout(row)

        split = QSplitter()
        split.addWidget(self.glist)
        split.addWidget(right)
        split.setSizes([380, 820])

        self.status = QLabel("묶음 클릭 → 남길 영상 클릭 → ✅ · 영상 더블클릭 = 재생")
        self.status.setStyleSheet("color:#aaa;")
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self._btn("닫기", self.reject))

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        lay.addLayout(bottom)

        self.pause_timer = QTimer(self)
        self.pause_timer.setInterval(1000)
        self.pause_timer.timeout.connect(self._check_pause)
        self.pause_timer.start()

    def _btn(self, text, fn):
        b = QPushButton(text)
        b.setAutoDefault(False)
        b.clicked.connect(lambda checked=False: fn())
        return b

    # ---------- 목록 ----------
    def refresh(self):
        data = dups.find_groups(self.conn)
        cur = self.glist.currentRow()
        self.glist.blockSignals(True)
        self.glist.clear()
        self.groups = []
        total_save = 0
        for g in data["groups"]:
            rows = [r for r in (full_row(self.conn, i) for i in g["ids"]) if r]
            if len(rows) < 2:
                continue
            self.groups.append((g, rows))
            sizes = [r.get("size") or 0 for r in rows]
            total_save += sum(sizes) - max(sizes)
            exts = " + ".join(sorted({os.path.splitext(r["filename"])[1].lower() or "?" for r in rows}))
            it = QListWidgetItem(_icon(_thumb_file(rows[0].get("thumb_path"))),
                                 f"{len(rows)}개 · {fmt_duration(rows[0]['duration'])} · "
                                 f"일치 {g['pct']}% · {exts}\n{rows[0]['filename'][:46]}")
            it.setData(ROLE, len(self.groups) - 1)
            self.glist.addItem(it)
        self.glist.blockSignals(False)
        text = f"같은 영상 묶음 {len(self.groups)}개 · 정리하면 최대 {fmt_size(total_save)} 확보"
        if data["pending"]:
            text += f"\n⏳ 아직 비교 못 한 후보 {data['pending']}개 → 🔍 찾기 시작 (외장하드 연결 필요)"
        else:
            text += " · ✅ 후보 비교 모두 끝남"
        self.info.setText(text)
        if self.groups:
            self.glist.setCurrentRow(min(max(cur, 0), self.glist.count() - 1))
        else:
            self.show_group(None)

    def _text(self, v, best):
        ext = os.path.splitext(v["filename"])[1].lstrip(".").upper() or "?"
        parts = [ext, f"{v.get('width') or '?'}×{v.get('height') or '?'}",
                 fmt_size(v.get("size") or 0), f"{_kbps(v):,} kbps"]
        if v.get("video_codec"):
            parts.append(str(v["video_codec"]))
        nt = len([x for x in (v.get("tag_names") or "").split(", ") if x])
        na = len([x for x in (v.get("actor_names") or "").split(", ") if x])
        meta = [f"★{v['rating']}" if v.get("rating") else "별점 없음",
                f"본 {v.get('play_count') or 0}회", f"태그 {nt}", f"배우 {na}"]
        if v.get("has_sub"):
            meta.append("자막")
        where = os.path.dirname(v["full_path"]) if v.get("online") else "(외장하드 연결 안 됨)"
        return (("⭐ 추천  " if best else "") + v["filename"] + "\n" + " · ".join(parts)
                + "\n" + " · ".join(meta) + "\n📁 " + where)

    def show_group(self, cur, prev=None):
        self.vlist.clear()
        if cur is None:
            self.rows = []
            self.gtitle.setText("같은 영상 묶음이 없습니다")
            return
        g, rows = self.groups[cur.data(ROLE)]
        self.rows = rows
        best, why = suggest(rows)
        self.gtitle.setText(f"길이 {fmt_duration(rows[0]['duration'])} · 장면 일치 {g['pct']}% — ⭐ 추천: {why}")
        for v in sorted(rows, key=lambda r: r is not best):
            it = QListWidgetItem(_icon(_thumb_file(v.get("thumb_path"))), self._text(v, v is best))
            it.setData(ROLE, v)
            it.setToolTip(v.get("full_path") or v["filename"])
            self.vlist.addItem(it)
        self.vlist.setCurrentRow(0)

    def _picked(self):
        it = self.vlist.currentItem()
        return it.data(ROLE) if it is not None else None

    # ---------- 동작 ----------
    def play(self):
        v = self._picked()
        if v:
            self.w.play_video(v)

    def open_folder(self):
        v = self._picked()
        if v and v.get("online"):
            subprocess.Popen(f'explorer /select,"{os.path.normpath(v["full_path"])}"')

    def not_same(self):
        v = self._picked()
        if not v:
            return
        others = [r for r in self.rows if r["id"] != v["id"]]
        if QMessageBox.question(self, "다른 영상",
                                f"'{v['filename']}'은(는) 이 묶음의 다른 영상과 다른 영상입니다.\n"
                                "앞으로 같은 영상으로 묶지 않을까요?") != YES:
            return
        dups.ignore_pairs(self.conn, [(v["id"], o["id"]) for o in others])
        self.refresh()

    def keep_selected(self):
        keep = self._picked()
        if not keep:
            return
        others = [r for r in self.rows if r["id"] != keep["id"]]
        if any(not o.get("online") for o in others):
            QMessageBox.information(self, "같은 영상 정리", "지울 영상이 있는 외장하드를 먼저 연결해 주세요.")
            return
        freed = sum(o.get("size") or 0 for o in others)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("⛔ 영구 삭제")
        box.setText(f"남길 영상:\n   {keep['filename']}\n\n아래 {len(others)}개({fmt_size(freed)})를 "
                    "파일까지 영구 삭제합니다.\n휴지통으로 가지 않아 되돌릴 수 없습니다.")
        box.setInformativeText("\n".join("· " + o["filename"] for o in others))
        b_del = box.addButton("영구 삭제", QMessageBox.ButtonRole.DestructiveRole)
        b_no = box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(b_no)
        box.exec()
        if box.clickedButton() is not b_del:
            return
        done, fails = remove_others(self.w, keep, others, self.merge.isChecked())
        self.w.after_edit()
        self.status.setText(f"✅ {done}개 지움 · 남긴 영상: {keep['filename']}")
        if fails:
            QMessageBox.warning(self, "일부 못 지움", "\n".join(fails[:10]))
        self.refresh()

    # ---------- 지문 만들기 (뒤에서) ----------
    def start(self):
        if self.thread and self.thread.isRunning():
            return
        self.thread = FpThread(self)
        self.thread.progress.connect(self._progress)
        self.thread.finished.connect(self._finished)
        self.b_scan.setEnabled(False)
        self.b_stop.setEnabled(True)
        self.status.setText("⏳ 후보 고르는 중…")
        self.thread.start()

    def stop(self):
        if self.thread and self.thread.isRunning():
            self.thread.stop()
            self.status.setText("⏹ 멈추는 중… (지금 영상까지만)")

    def _progress(self, d, t, name):
        p = " (재생 중이라 잠시 멈춤)" if self.thread and self.thread.paused else ""
        self.status.setText(f"⏳ 장면 지문 만드는 중 {d}/{t}{p} · {name[:50]}")

    def _finished(self):
        r = (self.thread.result if self.thread else None) or {}
        self.b_scan.setEnabled(True)
        self.b_stop.setEnabled(False)
        if "error" in r:
            QMessageBox.warning(self, "오류", r["error"])
        else:
            msg = f"✅ 지문 {r.get('done', 0)}개 만듦"
            if r.get("err"):
                msg += f" · 못 읽음 {r['err']}개"
            if r.get("offline"):
                msg += f" · 외장하드 연결 안 됨 {r['offline']}개"
            self.status.setText(msg)
        self.refresh()

    def _check_pause(self):
        if self.thread and self.thread.isRunning():
            self.thread.paused = self.w.player.isVisible()


def open_dialog(window):
    dlg = getattr(window, "_vv_dup_dlg", None)
    if dlg is None:
        dlg = DupDialog(window)
        window._vv_dup_dlg = dlg
    dlg.refresh()
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()


def install(window):
    dups.ensure(window.conn)
    window._vv_dup_dlg = None
    act = QAction("🧬 같은 영상 찾기", window)
    act.setToolTip("이름·확장자·인코딩이 달라도 같은 영상 찾기 (Ctrl+Shift+D)")
    act.triggered.connect(lambda checked=False: open_dialog(window))
    tt._toolbar(window).addAction(act)
    QShortcut(QKeySequence("Ctrl+Shift+D"), window).activated.connect(lambda: open_dialog(window))

    def on_quit():
        d = window._vv_dup_dlg
        if d and d.thread and d.thread.isRunning():
            d.thread.stop()
            if not d.thread.wait(5000):
                os._exit(0)
    QApplication.instance().aboutToQuit.connect(on_quit)
