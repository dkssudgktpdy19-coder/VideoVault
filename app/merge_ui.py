"""영상 합치기 화면 (5-1): 나뉜 영상 찾기, 순서 정하고 합치기, 원본 보관함(자동 삭제·직접 정리)"""
import math
import os
import subprocess
import time
from datetime import datetime

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDialog, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QProgressBar, QPushButton, QSpinBox, QVBoxLayout)

from app import library, manage, merge
from app import thumb_tool as tt
from app.actor_ui import _icon, _thumb_file
from app.db import init_db
from app.drives import drive_letters
from app.utils import fmt_duration, fmt_size

ROLE = Qt.ItemDataRole.UserRole
YES = QMessageBox.StandardButton.Yes


def _btn(text, fn):
    b = QPushButton(text)
    b.setAutoDefault(False)
    b.clicked.connect(lambda checked=False: fn())
    return b


def _menu_item(menu, text, fn):
    a = menu.addAction(text)
    a.triggered.connect(lambda checked=False: fn())
    return a


# ---------------- 파일 지우기 (메인 화면 스레드에서) ----------------
def delete_video(window, vid):
    """합친 영상 등 하나를 파일까지 삭제 → 오류 문구 또는 None"""
    conn = window.conn
    v = library.get_video(conn, vid)
    if not v:
        return None
    pw = window.player
    if getattr(pw, "video", None) and pw.video["id"] == vid:
        pw.close()
        QApplication.processEvents()
        time.sleep(0.5)
    path = v.get("full_path")
    if path and os.path.exists(path):
        err = manage._remove_file(path)
        if err:
            return err
    manage._cleanup(conn, v)
    for t in manage._tables_with_video_id(conn):
        conn.execute(f'DELETE FROM "{t}" WHERE video_id = ?', (vid,))
    conn.execute("DELETE FROM videos WHERE id = ?", (vid,))
    conn.commit()
    return None


def purge_job(window, job_id):
    """보관한 원본 조각을 영구 삭제 → (지운 수, 확보 용량, 실패 목록)"""
    conn = window.conn
    merge.ensure(conn)
    letters = drive_letters(conn)
    tables = manage._tables_with_video_id(conn)
    done, freed, fails = 0, 0, []
    for it in conn.execute("SELECT * FROM merge_items WHERE job_id=?", (job_id,)).fetchall():
        name = os.path.basename(it["orig_rel"])
        L = letters.get(it["drive_id"])
        if not L:
            fails.append(f"{name}: 외장하드 연결 안 됨")
            continue
        root = L + "\\"
        p = os.path.join(root, it["arch_rel"])
        arch = os.path.normcase(os.path.join(root, merge.ARCHIVE_NAME)) + os.sep
        if not os.path.normcase(os.path.abspath(p)).startswith(arch):   # 안전장치
            fails.append(f"{name}: 보관 폴더 밖의 파일이라 건너뜀")
            continue
        if os.path.exists(p):
            size = os.path.getsize(p)
            err = manage._remove_file(p)
            if err:
                fails.append(f"{name}: {err}")
                continue
            freed += size
        v = conn.execute("SELECT id, thumb_path FROM videos WHERE id=?", (it["video_id"],)).fetchone()
        if v:
            manage._cleanup(conn, dict(v))
        for t in tables:
            conn.execute(f'DELETE FROM "{t}" WHERE video_id = ?', (it["video_id"],))
        conn.execute("DELETE FROM videos WHERE id=?", (it["video_id"],))
        conn.commit()
        done += 1
        merge.rmdir_up(os.path.dirname(p), root)
    left = conn.execute("SELECT COUNT(*) FROM merge_items WHERE job_id=?", (job_id,)).fetchone()[0]
    if not left:
        conn.execute("DELETE FROM merge_jobs WHERE id=?", (job_id,))
        conn.commit()
    return done, freed, fails


def auto_purge(window):
    if getattr(window, "_vv_merge_busy", False):
        return
    try:
        days = merge.get_days(window.conn)
        if not days:
            return
        n, freed = 0, 0
        for j in merge.expired_jobs(window.conn):
            if not j["online"]:
                continue
            d, f, _ = purge_job(window, j["id"])
            n += 1 if d else 0
            freed += f
        if n:
            window.statusBar().showMessage(
                f"🗄 보관 {days}일이 지난 원본 {n}묶음 자동 삭제 · {fmt_size(freed)} 확보", 15000)
    except Exception as e:
        print("[보관함 자동 삭제 오류]", e)


# ---------------- 합치기 ----------------
class MergeThread(QThread):
    progress = Signal(float, str)

    def __init__(self, rows, name, parent=None):
        super().__init__(parent)
        self.rows, self.name = rows, name
        self._stop = False
        self.result = self.error = None

    def stop(self):
        self._stop = True

    def run(self):
        conn = init_db()
        try:
            self.result = merge.run_merge(conn, self.rows, self.name,
                                          progress=lambda p, s: self.progress.emit(p, s),
                                          stop=lambda: self._stop)
        except merge.Cancelled:
            self.error = "취소했습니다 (만들던 파일은 지웠고, 원본은 그대로입니다)"
        except Exception as e:
            self.error = str(e)
        finally:
            conn.close()


class MergeDialog(QDialog):
    def __init__(self, window, rows):
        super().__init__(window)
        self.w = window
        self.thread = None
        self.t0 = 0.0
        self.probes = {r["id"]: merge.probe(r["full_path"]) for r in rows}
        self.setWindowTitle("🧩 영상 합치기")
        self.resize(880, 620)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)

        tip = QLabel("순서를 확인하세요. 끌어서 옮기거나 ↑↓ 버튼으로 바꿀 수 있습니다 · 더블클릭 = 재생")
        tip.setStyleSheet("color:#aaa;")
        self.list = QListWidget()
        self.list.setIconSize(QSize(160, 90))
        self.list.setSpacing(3)
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        for r in rows:
            it = QListWidgetItem(_icon(_thumb_file(r.get("thumb_path"))), "")
            it.setData(ROLE, r)
            self.list.addItem(it)
        self.list.itemActivated.connect(lambda it: self.w.play_video(it.data(ROLE)))
        self.list.model().rowsMoved.connect(lambda *a: QTimer.singleShot(0, self.update_plan))
        side = QVBoxLayout()
        side.addWidget(_btn("↑ 위로", lambda: self.move(-1)))
        side.addWidget(_btn("↓ 아래로", lambda: self.move(1)))
        side.addStretch(1)
        mid = QHBoxLayout()
        mid.addWidget(self.list, 1)
        mid.addLayout(side)

        self.name = QLineEdit(merge.suggest_name(rows))
        self.name.textChanged.connect(lambda _: self.update_plan())
        nrow = QHBoxLayout()
        nrow.addWidget(QLabel("합친 파일 이름:"))
        nrow.addWidget(self.name, 1)
        self.plan_lbl = QLabel()
        self.plan_lbl.setWordWrap(True)
        self.plan_lbl.setStyleSheet("color:#7cc4ff;")
        note = QLabel(f"원본 조각은 같은 드라이브의 숨김 폴더 '{merge.ARCHIVE_NAME}'로 옮겨 보관합니다 "
                      "→ ☰ 메뉴의 🗄 합치기 보관함에서 되돌리기·삭제")
        note.setWordWrap(True)
        note.setStyleSheet("color:#aaa;")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setVisible(False)
        self.state = QLabel("")
        self.b_start = _btn("🧩 합치기 시작", self.start)
        self.b_start.setStyleSheet("font-weight:bold;")
        bottom = QHBoxLayout()
        bottom.addWidget(self.state, 1)
        bottom.addWidget(self.b_start)
        bottom.addWidget(_btn("닫기", self.reject))

        lay = QVBoxLayout(self)
        lay.addWidget(tip)
        lay.addLayout(mid, 1)
        lay.addLayout(nrow)
        lay.addWidget(self.plan_lbl)
        lay.addWidget(note)
        lay.addWidget(self.bar)
        lay.addLayout(bottom)
        self.update_plan()

    def rows(self):
        return [self.list.item(i).data(ROLE) for i in range(self.list.count())]

    def move(self, d):
        r = self.list.currentRow()
        t = r + d
        if r < 0 or not 0 <= t < self.list.count():
            return
        it = self.list.takeItem(r)
        self.list.insertItem(t, it)
        self.list.setCurrentRow(t)
        self.update_plan()

    def update_plan(self):
        rows = self.rows()
        for i in range(self.list.count()):
            it = self.list.item(i)
            r = it.data(ROLE)
            p = self.probes.get(r["id"]) or {}
            res = f"{p.get('w') or r.get('width') or '?'}×{p.get('h') or r.get('height') or '?'}"
            it.setText(f"{i + 1}.  {r['filename']}\n"
                       f"{fmt_duration(p.get('dur') or r.get('duration') or 0)} · {res} · "
                       f"{p.get('codec') or r.get('video_codec') or '?'} · {p.get('fps') or '?'}fps · "
                       f"{fmt_size(r.get('size') or 0)}")
        pl = merge.plan(rows, [self.probes.get(r["id"]) for r in rows])
        total = sum((self.probes.get(r["id"]) or {}).get("dur") or r.get("duration") or 0 for r in rows)
        size = sum(r.get("size") or 0 for r in rows)
        if pl["mode"]:
            icon = "⚡" if pl["mode"] == "copy" else "🎞"
            self.plan_lbl.setText(f"조각 {len(rows)}개 · 합계 {fmt_duration(total)} · {fmt_size(size)}\n"
                                  f"{icon} {pl['why']}\n결과: {merge._clean_name(self.name.text())}{pl['ext']}")
        else:
            self.plan_lbl.setText(f"❌ 합칠 수 없음: {pl['why']}")
        running = bool(self.thread and self.thread.isRunning())
        self.b_start.setEnabled(bool(pl["mode"]) and not running)

    def _busy(self, on):
        self.list.setEnabled(not on)
        self.name.setEnabled(not on)
        self.b_start.setEnabled(not on)
        self.bar.setVisible(on)
        self.w._vv_merge_busy = on

    def start(self):
        if getattr(self.w, "_vv_merge_busy", False):
            QMessageBox.information(self, "합치기", "다른 합치기가 진행 중입니다. 끝난 뒤 다시 시도하세요.")
            return
        rows = self.rows()
        pw = self.w.player
        if getattr(pw, "video", None) and pw.video["id"] in {r["id"] for r in rows}:
            pw.close()
            QApplication.processEvents()
            time.sleep(0.5)
        self.thread = MergeThread(rows, self.name.text(), self)
        self.thread.progress.connect(self._prog)
        self.thread.finished.connect(self._finished)
        self.w._vv_merge_thread = self.thread
        self._busy(True)
        self.t0 = time.time()
        self.state.setText("⏳ 시작하는 중…")
        self.thread.start()

    def _prog(self, p, label):
        self.bar.setValue(int(p * 1000))
        el = time.time() - self.t0
        eta = f" · 남은 시간 약 {fmt_duration(el * (1 - p) / p)}" if p > 0.03 else ""
        self.state.setText(f"⏳ {label} {p * 100:.0f}%{eta}")

    def _finished(self):
        t = self.thread
        self._busy(False)
        if t.error:
            self.state.setText("❌ " + t.error.splitlines()[0])
            self.update_plan()
            QMessageBox.warning(self, "합치기 안 됨", t.error)
            return
        r = t.result
        days = merge.get_days(self.w.conn)
        keep = f"{days}일 뒤 자동 삭제" if days else "자동 삭제 꺼짐 (보관함에서 직접 정리)"
        how = "그대로 이어 붙임" if r["mode"] == "copy" else "다시 인코딩"
        msg = (f"✅ 합치기 완료 ({how}, {r['secs']:.0f}초)\n\n{r['name']}\n길이 {fmt_duration(r['dur'])}\n\n"
               f"원본 조각 {r['moved']}개 → 🗄 보관함 ({keep})\n"
               f"옮긴 정보: 태그·배우·별점·메모 · 북마크 {r['marks']}개 · 자막 {r['subs']}줄")
        if r["failed"]:
            msg += "\n\n⚠ 확인 필요:\n" + "\n".join(r["failed"][:10])
        QMessageBox.information(self, "합치기 완료", msg)
        self.w.after_edit()
        self.accept()

    def reject(self):
        if self.thread and self.thread.isRunning():
            if QMessageBox.question(self, "취소", "합치는 중입니다. 취소할까요?\n(만들던 파일은 지우고 원본은 그대로 둡니다)") == YES:
                self.thread.stop()
                self.state.setText("⏹ 취소하는 중…")
            return
        super().reject()


def open_merge(window, rows):
    seen, uniq = set(), []
    for r in rows:
        if r and r["id"] not in seen:
            seen.add(r["id"])
            uniq.append(r)
    if len(uniq) < 2:
        QMessageBox.information(window, "합치기", "합칠 영상을 2개 이상 선택하세요.")
        return None
    if len(uniq) > merge.MAX_PARTS:
        QMessageBox.information(window, "합치기", f"한 번에 {merge.MAX_PARTS}개까지 합칠 수 있습니다.")
        return None
    if any(not r.get("online") for r in uniq):
        QMessageBox.information(window, "합치기", "외장하드가 연결되지 않은 영상이 있습니다.")
        return None
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        dlg = MergeDialog(window, merge.order_rows(uniq))
    finally:
        QApplication.restoreOverrideCursor()
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.show()
    return dlg


# ---------------- 나뉜 영상 찾기 ----------------
class SplitDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.w = window
        self.groups = []
        self.setWindowTitle("🧩 나뉜 영상 찾기·합치기")
        self.resize(980, 660)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.list = QListWidget()
        self.list.setIconSize(QSize(128, 72))
        self.list.setSpacing(3)
        self.list.itemActivated.connect(lambda *_: self.merge_selected())
        row = QHBoxLayout()
        b = _btn("🧩 합치기…", self.merge_selected)
        b.setStyleSheet("font-weight:bold;")
        row.addWidget(b)
        row.addWidget(_btn("▶ 첫 조각 재생", self.play))
        row.addWidget(_btn("📂 폴더 열기", self.open_folder))
        row.addStretch(1)
        row.addWidget(_btn("🔄 다시 찾기", self.refresh))
        row.addWidget(_btn("닫기", self.reject))
        lay = QVBoxLayout(self)
        lay.addWidget(self.info)
        lay.addWidget(self.list, 1)
        lay.addLayout(row)

    def refresh(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.groups = merge.find_groups(self.w.conn)
        finally:
            QApplication.restoreOverrideCursor()
        self.list.clear()
        for i, g in enumerate(self.groups):
            rows = g["rows"]
            total = sum(r.get("duration") or 0 for r in rows)
            size = sum(r.get("size") or 0 for r in rows)
            names = "\n".join("   " + r["filename"] for r in rows[:6])
            if len(rows) > 6:
                names += f"\n   … 외 {len(rows) - 6}개"
            it = QListWidgetItem(_icon(_thumb_file(rows[0].get("thumb_path"))),
                                 f"{'✅ 확실' if g['sure'] else '❔ 추정'} · {len(rows)}조각 · "
                                 f"{fmt_duration(total)} · {fmt_size(size)}\n{names}")
            it.setData(ROLE, i)
            self.list.addItem(it)
        sure = sum(1 for g in self.groups if g["sure"])
        self.info.setText(f"나뉜 영상 묶음 {len(self.groups)}개 (확실 {sure} · 추정 {len(self.groups) - sure}) "
                          "· 연결된 드라이브 기준\n❔ 추정은 이름만 비슷한 다른 영상일 수 있으니 "
                          "재생해서 확인하세요 · 더블클릭 = 합치기")
        if self.groups:
            self.list.setCurrentRow(0)

    def _group(self):
        it = self.list.currentItem()
        return self.groups[it.data(ROLE)] if it else None

    def merge_selected(self):
        g = self._group()
        if g:
            dlg = open_merge(self.w, g["rows"])
            if dlg:
                dlg.finished.connect(lambda *_: self.refresh())

    def play(self):
        g = self._group()
        if g:
            self.w.play_video(g["rows"][0])

    def open_folder(self):
        g = self._group()
        if g:
            subprocess.Popen(f'explorer /select,"{os.path.normpath(g["rows"][0]["full_path"])}"')


def open_split(window):
    dlg = SplitDialog(window)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.refresh()
    dlg.show()


def _merge_one(window, v):
    g = merge.group_for(window.conn, v["id"])
    if not g:
        QMessageBox.information(window, "합치기",
                                "이 영상과 짝이 되는 조각(CD1·CD2, part1·part2 등)을 찾지 못했습니다.\n"
                                "조각들을 Ctrl+클릭으로 함께 선택한 뒤 우클릭 → 합치기를 쓰세요.")
        return
    open_merge(window, g["rows"])


# ---------------- 보관함 ----------------
class ArchiveDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.w = window
        self.setWindowTitle("🗄 합치기 보관함 (원본 조각)")
        self.resize(900, 560)
        self.days = QSpinBox()
        self.days.setRange(0, 365)
        self.days.setSuffix("일 뒤 자동 삭제")
        self.days.setSpecialValueText("자동 삭제 끔 (직접 정리)")
        self.days.setValue(merge.get_days(window.conn))
        self.days.valueChanged.connect(self._set_days)
        top = QHBoxLayout()
        top.addWidget(QLabel("보관 기간:"))
        top.addWidget(self.days)
        top.addStretch(1)
        self.info = QLabel()
        top.addWidget(self.info)
        self.list = QListWidget()
        self.list.setSpacing(3)
        row = QHBoxLayout()
        row.addWidget(_btn("↩ 되돌리기 (원래 자리로)", self.restore))
        row.addWidget(_btn("📂 보관 폴더 열기", self.open_folder))
        row.addStretch(1)
        row.addWidget(_btn("🧹 기한 지난 것 지금 삭제", self.purge_expired))
        b = _btn("⛔ 선택한 묶음 지금 삭제", self.purge)
        b.setStyleSheet("color:#ff8a8a;")
        row.addWidget(b)
        row.addWidget(_btn("닫기", self.reject))
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.list, 1)
        lay.addLayout(row)
        self.refresh()

    def _set_days(self, v):
        merge.set_days(self.w.conn, v)
        self.refresh()

    def refresh(self):
        self.jobs = merge.list_jobs(self.w.conn)
        self.list.clear()
        for j in self.jobs:
            when = datetime.fromtimestamp(j["created"] or 0).strftime("%Y-%m-%d %H:%M")
            if j["left"] is None:
                left = "자동 삭제 안 함"
            elif j["left"] <= 0:
                left = "⏰ 기한 지남 (다음 확인 때 삭제)"
            else:
                left = f"{math.ceil(j['left'])}일 뒤 삭제"
            off = "" if j["online"] else " · ⚠ 외장하드 연결 안 됨"
            it = QListWidgetItem(f"{when} · 합친 영상: {j['merged_name']}\n"
                                 f"   원본 {j['n']}개 · {fmt_size(j['size'])} · {left}{off}\n   "
                                 + " / ".join(j["names"][:5]))
            it.setData(ROLE, j)
            self.list.addItem(it)
        self.info.setText(f"{len(self.jobs)}묶음 · {fmt_size(sum(j['size'] for j in self.jobs))}")
        if self.jobs:
            self.list.setCurrentRow(0)

    def _job(self):
        it = self.list.currentItem()
        return it.data(ROLE) if it else None

    def restore(self):
        j = self._job()
        if not j:
            return
        if QMessageBox.question(self, "되돌리기", f"원본 {j['n']}개를 원래 자리로 되돌릴까요?") != YES:
            return
        ok, fails, mid, full = merge.restore_job(self.w.conn, j["id"])
        msg = f"↩ {ok}개 되돌림"
        if fails:
            msg += "\n\n못 되돌림:\n" + "\n".join(fails[:10])
        QMessageBox.information(self, "되돌리기", msg)
        if full and mid and library.get_video(self.w.conn, mid):
            if QMessageBox.question(self, "합친 영상",
                                    f"합친 영상 '{j['merged_name']}'도 파일까지 지울까요?") == YES:
                err = delete_video(self.w, mid)
                if err:
                    QMessageBox.warning(self, "못 지움", err)
        self.w.after_edit()
        self.refresh()

    def purge(self):
        j = self._job()
        if not j:
            return
        if QMessageBox.warning(self, "⛔ 영구 삭제",
                               f"원본 {j['n']}개({fmt_size(j['size'])})를 영구 삭제합니다.\n되돌릴 수 없습니다.",
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                               QMessageBox.StandardButton.No) != YES:
            return
        done, freed, fails = purge_job(self.w, j["id"])
        msg = f"⛔ {done}개 삭제 · {fmt_size(freed)} 확보"
        if fails:
            msg += "\n\n" + "\n".join(fails[:10])
        QMessageBox.information(self, "삭제", msg)
        self.w.after_edit()
        self.refresh()

    def purge_expired(self):
        auto_purge(self.w)
        self.w.after_edit()
        self.refresh()

    def open_folder(self):
        j = self._job()
        if j and j["first"] and os.path.exists(j["first"]):
            subprocess.Popen(f'explorer /select,"{os.path.normpath(j["first"])}"')


def open_archive(window):
    dlg = ArchiveDialog(window)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.show()


# ---------------- 연결 ----------------
def install(window):
    merge.ensure(window.conn)
    window._vv_merge_busy = False
    window._vv_merge_thread = None
    tb = tt._toolbar(window)
    for text, tip, fn in (
            ("🧩 나뉜 영상 합치기", "CD1·CD2, part1·part2 같은 조각을 찾아 하나로 (Ctrl+Shift+J)",
             lambda: open_split(window)),
            ("🗄 합치기 보관함", "합친 뒤 보관한 원본 조각: 되돌리기·삭제·자동 삭제 기간",
             lambda: open_archive(window))):
        act = QAction(text, window)
        act.setToolTip(tip)
        act.triggered.connect(lambda checked=False, fn=fn: fn())
        tb.addAction(act)
    QShortcut(QKeySequence("Ctrl+Shift+J"), window).activated.connect(lambda: open_split(window))
    QShortcut(QKeySequence("Ctrl+Shift+M"), window).activated.connect(
        lambda: open_merge(window, window.selected_videos()))

    old = getattr(window, "_vv_actor_menu", None)

    def menu_hook(menu, vids):
        if old:
            old(menu, vids)
        menu.addSeparator()
        if len(vids) >= 2:
            _menu_item(menu, f"🧩 선택한 {len(vids)}개 합치기…    Ctrl+Shift+M",
                       lambda: open_merge(window, vids))
        elif len(vids) == 1:
            _menu_item(menu, "🧩 나뉜 조각 찾아 합치기…", lambda: _merge_one(window, vids[0]))
    window._vv_actor_menu = menu_hook

    # 보관 기간이 지난 원본 자동 삭제: 시작 1분 뒤, 그 뒤 1시간마다
    QTimer.singleShot(60 * 1000, lambda: auto_purge(window))
    timer = QTimer(window)
    timer.setInterval(60 * 60 * 1000)
    timer.timeout.connect(lambda: auto_purge(window))
    timer.start()
    window._vv_merge_timer = timer

    def on_quit():
        t = window._vv_merge_thread
        if t and t.isRunning():
            t.stop()
            t.wait(10000)
    QApplication.instance().aboutToQuit.connect(on_quit)
